"""Adversarial offline subprocess tests: no Salesforce or other network calls."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cloud" / "studio-controller"))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient
from app.main import create_app
from app.settings import Settings
from app.state import StateConflict
from app.workers.lead_facts import LeadFactsWorker, UNAVAILABLE
from workers import lead_facts_process as process
from workers.org_facts import OrgFacts
from test_studio_controller import CountingWorker, IDs, MemoryStore, make_controller, settings
from test_studio_lead_facts import ORG_ID, ENV, command


ANSWER = {"org_id": ORG_ID, "org_name": "Test dev org", "org_type": "Developer Edition",
          "total": 26, "site_total": 4, "site_last_7_days": 2,
          "observed_at": "2026-09-24T12:00:00Z", "source": "live SOQL on the Lead object"}


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.children = []
        real_popen = subprocess.Popen

        def recorded(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            self.children.append(child)
            return child

        patcher = mock.patch.object(process.subprocess, "Popen", side_effect=recorded)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.assert_reaped)

    def assert_reaped(self):
        for child in self.children:
            self.assertIsNotNone(child.poll(), "provider child must be dead and reaped")
            self.assertTrue(child.stdout.closed)

    def fake_program(self, code):
        original = process._run_child
        return mock.patch.object(process, "_run_child", side_effect=lambda command, env, deadline, cancel:
                                 original([sys.executable, "-I", "-B", "-c", code], env, deadline, cancel))

    def test_normal_child_result_and_strict_environment(self):
        with mock.patch.dict(os.environ, {**ENV, "ANTHROPIC_API_KEY": "must-not-inherit",
                                         "PYTHONPATH": "must-not-inherit"}), self.fake_program(
                "import json,os; assert 'ANTHROPIC_API_KEY' not in os.environ; assert 'PYTHONPATH' not in os.environ; "
                "print(" + repr(json.dumps(ANSWER)) + ")"):
            self.assertEqual(ANSWER, process.fetch_lead_facts(ORG_ID, 2))
        self.assertEqual(1, len(self.children))

    def test_fixed_production_command_never_contains_credentials(self):
        with mock.patch.dict(os.environ, ENV, clear=True), mock.patch.object(
                process, "_run_child", return_value=json.dumps(ANSWER).encode()) as run:
            process.fetch_lead_facts(ORG_ID, 2)
        argv, env, _, _ = run.call_args.args
        self.assertEqual([sys.executable, "-I", "-B", str(Path(process.__file__).resolve())], argv)
        for value in ENV.values():
            self.assertNotIn(value, " ".join(argv))
        self.assertEqual(set(ENV) | {"STUDIO_SALESFORCE_ORG_ID", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE"}, set(env))

    def test_malformed_oversized_and_nonzero_children_have_redacted_failure(self):
        programs = ["print('fake-secret not json')", "print('x' * 1000000)",
                    "import sys; print('fake-secret', file=sys.stderr); sys.exit(3)",
                    "print('{}')", "print(" + repr(json.dumps({**ANSWER, "total": True})) + ")",
                    "print(" + repr(json.dumps({**ANSWER, "org_id": "00D000000000002AAA"})) + ")",
                    "print(" + repr(json.dumps({**ANSWER, "site_total": 27})) + ")",
                    "print(" + repr(json.dumps({**ANSWER, "site_last_7_days": 5})) + ")",
                    "print(" + repr(json.dumps({**ANSWER, "extra": "fake-secret"})) + ")",
                    "print(" + repr(json.dumps(ANSWER)[:-1] + ',"total":1}') + ")"]
        for code in programs:
            with self.subTest(code=code[:35]), mock.patch.dict(os.environ, ENV, clear=True), self.fake_program(code):
                with self.assertRaises(process.FactsUnavailable) as caught:
                    process.fetch_lead_facts(ORG_ID, 2)
                self.assertNotIn("fake-secret", str(caught.exception))

    def test_hard_hang_and_slow_output_are_killed_with_no_late_work(self):
        for slow_output in (False, True):
            with self.subTest(slow_output=slow_output), tempfile.TemporaryDirectory() as folder:
                ready, late = Path(folder) / "ready", Path(folder) / "late"
                code = "import time,sys; from pathlib import Path; Path(" + repr(str(ready)) + ").touch(); "
                if slow_output:
                    code += "[(sys.stdout.write('x'),sys.stdout.flush(),time.sleep(.03)) for _ in range(40)]; "
                else:
                    code += "time.sleep(1.2); "
                code += "Path(" + repr(str(late)) + ").touch()"
                with mock.patch.dict(os.environ, ENV, clear=True), self.fake_program(code):
                    start = time.monotonic()
                    with self.assertRaises(process.FactsUnavailable):
                        process.fetch_lead_facts(ORG_ID, .4)
                    elapsed = time.monotonic() - start
                self.assertTrue(ready.exists(), "test must actually start the child")
                self.assertLess(elapsed, 1.0)
                self.assert_reaped()
                self.assertFalse(any(t.name == "lead-facts-output" for t in threading.enumerate()))
                time.sleep(1.25)
                self.assertFalse(late.exists())
                print("deadline transport=%s configured=0.4s measured=%.3fs child_reaped=true" %
                      ("slow-output" if slow_output else "hard-hang", elapsed))

    def test_timeout_is_committed_unavailable_and_replay_never_spawns_again(self):
        worker = LeadFactsWorker(CountingWorker(), ORG_ID, .15)
        controller, store, _ = make_controller(worker=worker)
        state, _ = controller.create_session()
        cmd = command(state)
        with mock.patch.dict(os.environ, ENV, clear=True), self.fake_program("import time; time.sleep(30)"):
            first = controller.execute(state["session_id"], cmd)
            restarted, _, _ = make_controller(store=store, worker=LeadFactsWorker(CountingWorker(), ORG_ID, .15))
            self.assertEqual(first, restarted.execute(state["session_id"], cmd))
        self.assertEqual(1, len(self.children))
        self.assertEqual(UNAVAILABLE, first["events"][0]["payload"]["text"])
        saved = controller.repository.load(state["session_id"]).state
        self.assertEqual("completed", saved["commands"][cmd["command_id"]]["status"])
        self.assertEqual(state["artifact"], saved["artifact"])
        self.assertEqual(state["artifact_version"], saved["artifact_version"])

    def test_local_stop_is_fast_kills_child_and_fences_the_result(self):
        worker = LeadFactsWorker(CountingWorker(), ORG_ID, 5)
        controller, _, _ = make_controller(worker=worker)
        state, _ = controller.create_session()
        with tempfile.TemporaryDirectory() as folder:
            ready, late = Path(folder) / "ready", Path(folder) / "late"
            code = "import time; from pathlib import Path; Path(" + repr(str(ready)) + ").touch(); time.sleep(1); Path(" + repr(str(late)) + ").touch()"
            with mock.patch.dict(os.environ, ENV, clear=True), self.fake_program(code), ThreadPoolExecutor(1) as pool:
                pending = pool.submit(controller.execute, state["session_id"], command(state))
                until = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                start = time.monotonic()
                stop = {"command_id": "stop-leads", "session_id": state["session_id"], "type": "stop", "expected_version": 1}
                stopped = controller.execute(state["session_id"], stop)
                stop_elapsed = time.monotonic() - start
                self.assertEqual(stopped, controller.execute(state["session_id"], stop))
                with self.assertRaises(StateConflict):
                    pending.result(timeout=1)
                killed_elapsed = time.monotonic() - start
            self.assertLess(stop_elapsed, .2)
            self.assertLess(killed_elapsed, 1)
            self.assert_reaped()
            time.sleep(1.05)
            self.assertFalse(late.exists())
            saved = controller.repository.load(state["session_id"]).state
            self.assertTrue(saved["stopped"])
            self.assertFalse(any(e["type"] == "confirm" for e in saved["events"]))
            print("local Stop committed=%.3fs child_reaped=%.3fs no_late_event=true" % (stop_elapsed, killed_elapsed))

    def test_cancel_before_registration_prevents_child_launch_and_is_session_scoped(self):
        worker = LeadFactsWorker(CountingWorker(), ORG_ID, 2)
        worker.cancel_session("stopped")
        worker.cancel_session("stopped")
        with mock.patch.dict(os.environ, ENV, clear=True), self.fake_program("print(" + repr(json.dumps(ANSWER)) + ")"):
            first = worker.on_turn({"session_id": "stopped", "artifact": {"id": "root"}},
                                   {"kind": "utterance", "text": "Count leads"})
            self.assertEqual(UNAVAILABLE, first["events"][0]["payload"]["text"])
            self.assertEqual(0, len(self.children))
            worker.on_turn({"session_id": "other", "artifact": {"id": "root"}},
                           {"kind": "utterance", "text": "Count leads"})
            self.assertEqual(1, len(self.children))

    def test_stop_does_not_cancel_provider_until_durable_save_succeeds(self):
        worker = LeadFactsWorker(CountingWorker(), ORG_ID, 2)
        controller, _, _ = make_controller(worker=worker)
        state, _ = controller.create_session()
        stop = {"command_id": "stop-fails", "session_id": state["session_id"], "type": "stop", "expected_version": 1}
        with mock.patch.object(controller.repository, "save", side_effect=StateConflict("fake unavailable")), mock.patch.object(
                worker, "cancel_session") as cancel:
            with self.assertRaises(StateConflict):
                controller.execute(state["session_id"], stop)
            cancel.assert_not_called()

    def test_child_entrypoint_only_emits_valid_facts_and_never_exception_details(self):
        output = mock.Mock()
        output.buffer = io.BytesIO()
        facts = mock.Mock()
        facts.lead_counts.return_value = ANSWER
        with mock.patch.dict(os.environ, {"STUDIO_SALESFORCE_ORG_ID": ORG_ID}), mock.patch.object(
                OrgFacts, "from_env", return_value=facts), mock.patch.object(process.sys, "stdout", output):
            self.assertEqual(0, process.child_main())
            self.assertEqual(ANSWER, json.loads(output.buffer.getvalue()))
            output.buffer.seek(0)
            output.buffer.truncate()
            facts.lead_counts.side_effect = RuntimeError("fake-secret private-url")
            self.assertEqual(1, process.child_main())
            self.assertEqual(b"", output.buffer.getvalue())

    def test_deadline_config_bounds_and_health_never_touch_provider(self):
        self.assertEqual(12, settings().lead_facts_timeout_seconds)
        with mock.patch.dict(os.environ, {"STUDIO_LEAD_FACTS_TIMEOUT_SECONDS": "ignored-invalid-disabled"}, clear=True):
            self.assertEqual(12, Settings.from_env().lead_facts_timeout_seconds)
        with mock.patch.dict(os.environ, {"STUDIO_ENABLE_LEAD_FACTS": "true",
                                         "STUDIO_LEAD_FACTS_TIMEOUT_SECONDS": "15"}, clear=True):
            self.assertEqual(15, Settings.from_env().lead_facts_timeout_seconds)
        for bad in (0, -1, 21, 40, float("nan"), float("inf"), True, "20", None):
            with self.subTest(timeout=bad), self.assertRaisesRegex(RuntimeError, "STUDIO_LEAD_FACTS_TIMEOUT_SECONDS"):
                settings(lead_facts_enabled=True, salesforce_org_id=ORG_ID, lead_facts_timeout_seconds=bad).validate()
        settings(lead_facts_enabled=True, salesforce_org_id=ORG_ID, lead_facts_timeout_seconds=20).validate()
        for enabled in (False, True):
            app = create_app(settings=settings(lead_facts_enabled=enabled, salesforce_org_id=ORG_ID),
                             store=MemoryStore(), worker=CountingWorker(), clock=lambda: 1000, id_factory=IDs())
            with TestClient(app) as client, mock.patch.dict(os.environ, {}, clear=True):
                health = client.get("/health").json()
            self.assertEqual({"voice": False, "lead_facts": enabled, "talk": False, "agents": [],
                              "analyst": False},
                             health["features"])
            self.assertEqual({"ok", "worker", "state_backend", "features"}, set(health))
            self.assertNotIn(ORG_ID, json.dumps(health))
        self.assertEqual([], self.children)


if __name__ == "__main__":
    unittest.main()

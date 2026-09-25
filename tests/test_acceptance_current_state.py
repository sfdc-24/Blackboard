"""The current-state acceptance runner judges correctly - offline, every reader faked."""
from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acceptance", REPO / "scripts" / "acceptance_current_state.py")
acc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acc)

NOW = datetime(2026, 9, 25, 4, 30, tzinfo=timezone.utc)


def pages(live, main, s1=200, s2=200):
    def get(url):
        return (s1, live) if url.startswith(acc.WWW) else (s2, main)
    return get


def execution(done):
    return {"status": {"conditions": [{"type": "Completed", "status": done}]}}


class Pages(unittest.TestCase):
    def test_same_bytes_pass_line_endings_ignored(self):
        self.assertEqual(acc.PASS, acc.check_pages_serves_main(get=pages(b"a\r\nb", b"a\nb"))[0])

    def test_different_bytes_fail_and_unreadable_is_unknown(self):
        self.assertEqual(acc.FAIL, acc.check_pages_serves_main(get=pages(b"old", b"new"))[0])
        self.assertEqual(acc.UNKNOWN, acc.check_pages_serves_main(get=pages(b"", b"new", s1=503))[0])

    def test_a_missing_asset_fails_by_name(self):
        status, evidence = acc.check_site_paths(get=lambda url: (404 if url.endswith("prototype-canvas.js") else 200, b""))
        self.assertEqual(acc.FAIL, status)
        self.assertIn("/assets/prototype-canvas.js=404", evidence)


class Controller(unittest.TestCase):
    def health(self, **features):
        body = json.dumps({"ok": True, "features": features}).encode()
        return lambda url: (200, body)

    def test_all_lanes_on_pass(self):
        status, evidence = acc.check_controller(get=self.health(voice=True, talk=True, analyst=True,
                                                                voices=["host", "architect"]))
        self.assertEqual(acc.PASS, status)
        self.assertIn("host", evidence)

    def test_a_lane_off_fails_and_names_it(self):
        status, evidence = acc.check_controller(get=self.health(voice=True, talk=True, analyst=False))
        self.assertEqual(acc.FAIL, status)
        self.assertIn("analyst", evidence)

    def test_down_or_garbled_fails(self):
        self.assertEqual(acc.FAIL, acc.check_controller(get=lambda url: (503, b""))[0])
        self.assertEqual(acc.FAIL, acc.check_controller(get=lambda url: (200, b"<html>"))[0])


class Vms(unittest.TestCase):
    def listing(self, *vms):
        return lambda args: [dict(name=n, status=s, scheduling={"provisioningModel": m}) for n, s, m in vms]

    def test_expected_states_pass(self):
        run = self.listing(("blackboard-bus", "RUNNING", "STANDARD"), ("zoom-presenter-tmp", "TERMINATED", "STANDARD"))
        self.assertEqual(acc.PASS, acc.check_vms(run=run)[0])
        run = self.listing(("blackboard-bus", "RUNNING", "STANDARD"), ("zoom-presenter-tmp", "RUNNING", "STANDARD"))
        self.assertEqual(acc.PASS, acc.check_vms(run=run)[0])

    def test_a_stopped_bus_a_spot_vm_or_a_missing_one_fails(self):
        for vms, word in (((("blackboard-bus", "TERMINATED", "STANDARD"), ("zoom-presenter-tmp", "TERMINATED", "STANDARD")), "is TERMINATED"),
                          ((("blackboard-bus", "RUNNING", "SPOT"), ("zoom-presenter-tmp", "TERMINATED", "STANDARD")), "Spot"),
                          ((("blackboard-bus", "RUNNING", "STANDARD"),), "missing")):
            status, evidence = acc.check_vms(run=self.listing(*vms))
            self.assertEqual(acc.FAIL, status, vms)
            self.assertIn(word, evidence)

    def test_gcloud_trouble_is_unknown(self):
        def boom(args):
            raise RuntimeError("gcloud is not on PATH")
        self.assertEqual(acc.UNKNOWN, acc.check_vms(run=boom)[0])


class Jobs(unittest.TestCase):
    def test_the_newest_finished_execution_decides(self):
        runs = {job: [execution("Unknown"), execution("True")] for job in acc.JOBS}
        self.assertEqual(acc.PASS, acc.check_jobs(run=lambda args: runs[args[args.index("--job") + 1]])[0])

    def test_a_failed_latest_fails_by_name(self):
        def run(args):
            job = args[args.index("--job") + 1]
            return [execution("False" if job == "wa-outbox" else "True")]
        status, evidence = acc.check_jobs(run=run)
        self.assertEqual(acc.FAIL, status)
        self.assertIn("wa-outbox", evidence)

    def test_nothing_finished_is_unknown_not_pass(self):
        self.assertEqual(acc.UNKNOWN, acc.check_jobs(run=lambda args: [execution("Unknown")])[0])


class Schedulers(unittest.TestCase):
    def job(self, name, schedule, minutes_ago, state="ENABLED"):
        last = (NOW.timestamp() - minutes_ago * 60)
        return {"name": "projects/p/locations/l/jobs/" + name, "schedule": schedule, "state": state,
                "lastAttemptTime": datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def test_on_time_passes(self):
        jobs = [self.job("watcher", "* * * * *", 1), self.job("probe", "15 * * * *", 50),
                self.job("sweep", "*/5 * * * *", 6)]
        self.assertEqual(acc.PASS, acc.check_schedulers(run=lambda args: jobs, now=NOW)[0])

    def test_stale_paused_or_never_attempted_fails(self):
        for job in (self.job("watcher", "* * * * *", 10), self.job("probe", "15 * * * *", 200),
                    self.job("probe", "15 * * * *", 1, state="PAUSED")):
            self.assertEqual(acc.FAIL, acc.check_schedulers(run=lambda args, j=job: [j], now=NOW)[0], job)
        never = {"name": "x/jobs/new", "schedule": "* * * * *", "state": "ENABLED"}
        self.assertEqual(acc.FAIL, acc.check_schedulers(run=lambda args: [never], now=NOW)[0])

    def test_intervals(self):
        self.assertEqual((1, 5, 60, 1440), (acc._interval_minutes("* * * * *"), acc._interval_minutes("*/5 * * * *"),
                                            acc._interval_minutes("45 * * * *"), acc._interval_minutes("0 6 * * *")))


class Runner(unittest.TestCase):
    def test_a_crashing_check_is_unknown_and_never_hides_the_others(self):
        def crashes(**_):
            raise SystemExit("no credentials")
        results = acc.run_all(checks=(("a", crashes), ("b", lambda **_: (acc.PASS, "fine"))))
        self.assertEqual([acc.UNKNOWN, acc.PASS], [r["status"] for r in results])

    def test_the_zoom_agent_is_never_pass_by_absence(self):
        self.assertEqual(acc.UNKNOWN, acc.check_zoom_agent()[0])

    def test_exit_code_is_one_only_on_a_fail(self):
        original = acc.CHECKS
        try:
            acc.CHECKS = (("x", lambda **_: (acc.UNKNOWN, "?")),)
            self.assertEqual(0, acc.main(["--json"]))
            acc.CHECKS = (("x", lambda **_: (acc.FAIL, "!")),)
            self.assertEqual(1, acc.main([]))
        finally:
            acc.CHECKS = original


if __name__ == "__main__":
    unittest.main()

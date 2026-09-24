"""Offline controller tests: no provider, network, microphone, or credentials."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sys
import threading
import time
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "cloud" / "studio-controller"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CONTROLLER))

from app.core import CommandError, StudioController, reduce_event  # noqa: E402
from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.state import StateConflict, StudioRepository  # noqa: E402
from app.tokens import verify_token  # noqa: E402
from app.workers.synthetic import SyntheticWorker  # noqa: E402
from scripts.state_store import Conflict  # noqa: E402


class MemoryStore:
    """Thread-safe generation CAS; shaped like scripts.state_store.GcsStore."""

    def __init__(self):
        self.data = {}
        self.generation = {}
        self.lock = threading.Lock()

    def describe(self):
        return "memory:test"

    def load(self, name):
        with self.lock:
            if name not in self.data:
                return {}, None
            return copy.deepcopy(self.data[name]), str(self.generation[name])

    def save(self, name, state, token):
        with self.lock:
            current = str(self.generation[name]) if name in self.data else None
            if token != current:
                raise Conflict("lost CAS")
            generation = self.generation.get(name, 0) + 1
            self.data[name] = copy.deepcopy(state)
            self.generation[name] = generation
            return str(generation)


class IDs:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def __call__(self, prefix):
        with self.lock:
            self.value += 1
            return "%s-%d" % (prefix, self.value)


class CountingWorker(SyntheticWorker):
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def on_turn(self, state, trigger):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic worker failed")
        return super().on_turn(state, trigger)


class BatchWorker(CountingWorker):
    def initial_questions(self):
        first = super().initial_questions()[0]
        second = copy.deepcopy(first)
        second.update({
            "question_id": "q-tone",
            "prompt": "Which visual tone should lead?",
            "affected_artifact_ids": ["hero"],
        })
        second["options"] = [
            {"option_id": "calm", "label": "Calm", "consequence": "Uses quiet contrast"},
            {"option_id": "bold", "label": "Bold", "consequence": "Uses stronger contrast"},
        ]
        return [first, second]


class FakeResponse:
    def __init__(self, *, status_code=200, text="v=0\r\no=answer", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"Location": "/v1/realtime/calls/rtc_test_call"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("synthetic HTTP failure")


class FakeVoiceClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


class EmailSender:
    def __init__(self):
        self.calls = []

    def __call__(self, email, code):
        self.calls.append((email, code))


def settings(**overrides):
    values = dict(
        allowed_origins=("https://www.sfdc24.com",),
        session_secret="test-secret-that-is-long-enough-for-tests",
        state_uri="file://unused",
        max_session_seconds=600,
        daily_session_cap=20,
        voice_mint_cap=3,
        max_events=200,
        max_commands=100,
        sse_poll_seconds=0.001,
        sse_wait_seconds=0,
        worker="synthetic",
        operator_emails=("operator@example.com",),
        operator_token_seconds=28800,
        email_sender_url="",
        email_sender_secret="",
        voice_enabled=False,
        maintenance_secret="",
        openai_api_key="",
        realtime_model="gpt-realtime-2.1",
        realtime_voice="marin",
    )
    values.update(overrides)
    return Settings(**values)


def make_controller(*, store=None, worker=None, cap=20, max_events=200,
                    max_commands=100, now=1000):
    store = store or MemoryStore()
    worker = worker or CountingWorker()
    clock = lambda: now
    controller = StudioController(
        StudioRepository(store, clock=clock), worker, clock=clock, id_factory=IDs(),
        max_seconds=600, daily_cap=cap, max_events=max_events,
        max_commands=max_commands,
    )
    return controller, store, worker


class CoreTests(unittest.TestCase):
    def test_session_starts_with_strict_sequence_snapshot_and_question(self):
        controller, _, _ = make_controller()
        state, admission = controller.create_session("Homepage redesign")
        self.assertEqual(admission, 1)
        self.assertEqual([1, 2, 3], [event["seq"] for event in state["events"]])
        self.assertEqual(
            ["session.started", "artifact.snapshot", "question.asked"],
            [event["type"] for event in state["events"]],
        )
        self.assertEqual(1, state["artifact_version"])
        self.assertEqual("q-cta", state["questions"][0]["question_id"])

    def test_command_is_durable_and_idempotent(self):
        controller, store, worker = make_controller()
        state, _ = controller.create_session()
        command = {
            "command_id": "cmd-1", "session_id": state["session_id"],
            "type": "answer", "expected_version": 1, "question_id": "q-cta",
            "option_id": "describe", "answer_source": "tap",
        }
        first = controller.execute(state["session_id"], command)
        second = controller.execute(state["session_id"], command)
        self.assertEqual(first, second)
        self.assertEqual(1, worker.calls)
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual(2, saved["artifact_version"])
        self.assertEqual("Describe a problem", saved["artifact"]["children"][1]["children"][2]["label"])
        self.assertEqual("completed", saved["commands"]["cmd-1"]["status"])
        self.assertEqual(sorted(event["seq"] for event in saved["events"]), [event["seq"] for event in saved["events"]])
        emitted_types = [event["type"] for event in first["events"]]
        self.assertEqual(["artifact.patch", "question.answered", "confirm"], emitted_types)
        self.assertEqual(2, first["events"][1]["artifact_version"])
        self.assertEqual(2, first["events"][1]["payload"]["question"]["artifact_version_after"])

        changed = dict(command, option_id="book")
        with self.assertRaisesRegex(CommandError, "bound to a different payload") as caught:
            controller.execute(state["session_id"], changed)
        self.assertEqual(409, caught.exception.status)
        self.assertEqual(1, worker.calls)

    def test_changed_cta_decision_updates_prototype_and_replays_exactly(self):
        controller, store, worker = make_controller()
        state, _ = controller.create_session()
        session_id = state["session_id"]
        first = controller.execute(session_id, {
            "command_id": "answer-book", "session_id": session_id,
            "type": "answer", "expected_version": 1,
            "question_id": "q-cta", "option_id": "book",
        })
        self.assertEqual(2, first["artifact_version"])
        changed = controller.execute(session_id, {
            "command_id": "change-cta", "session_id": session_id,
            "type": "change_decision", "expected_version": 2,
            "question_id": "q-cta",
        })
        replacement = next(event["payload"]["question"] for event in changed["events"]
                           if event["type"] == "question.asked")
        self.assertEqual("q-cta", replacement["parent_question_id"])
        command = {
            "command_id": "answer-work", "session_id": session_id,
            "type": "answer", "expected_version": 2,
            "question_id": replacement["question_id"], "option_id": "work",
        }
        answer = controller.execute(session_id, command)
        self.assertEqual(answer, controller.execute(session_id, command))
        self.assertEqual(["artifact.patch", "question.answered", "confirm"],
                         [event["type"] for event in answer["events"]])
        self.assertEqual(3, answer["artifact_version"])
        saved = StudioRepository(store).load(session_id).state
        cta = saved["artifact"]["children"][1]["children"][2]
        self.assertEqual("See the work", cta["label"])
        self.assertEqual("Opens case studies", cta["detail"])
        self.assertEqual("answered", saved["questions"][-1]["status"])
        self.assertEqual("work", saved["questions"][-1]["selected_option"])
        self.assertEqual(2, worker.calls)

    def test_stale_version_refused_before_worker(self):
        controller, _, worker = make_controller()
        state, _ = controller.create_session()
        with self.assertRaisesRegex(CommandError, "stale expected_version") as caught:
            controller.execute(state["session_id"], {
                "command_id": "cmd-stale", "session_id": state["session_id"],
                "type": "answer", "expected_version": 0, "question_id": "q-cta",
                "option_id": "book",
            })
        self.assertEqual(409, caught.exception.status)
        self.assertEqual(0, worker.calls)

    def test_unknown_command_field_is_refused_strictly(self):
        controller, _, worker = make_controller()
        state, _ = controller.create_session()
        with self.assertRaisesRegex(CommandError, "unknown fields"):
            controller.execute(state["session_id"], {
                "command_id": "cmd-pause", "session_id": state["session_id"],
                "type": "pause", "expected_version": 1, "unexpected": True,
            })
        self.assertEqual(0, worker.calls)

    def test_page_shaped_answer_batch_is_accepted_and_bound_to_batch(self):
        controller, store, worker = make_controller(worker=BatchWorker())
        state, _ = controller.create_session()
        batch = next(event for event in state["events"] if event["type"] == "decision.batch")
        command = {
            "command_id": "cmd-batch", "session_id": state["session_id"],
            "type": "answer_batch", "expected_version": 1,
            "batch_id": batch["payload"]["batch_id"],
            "answers": [
                {"question_id": "q-cta", "option_id": "book"},
                {"question_id": "q-tone", "option_id": "calm"},
            ],
        }
        result = controller.execute(state["session_id"], command)
        replay = controller.execute(state["session_id"], command)
        self.assertEqual(result, replay)
        self.assertEqual(1, worker.calls)
        self.assertEqual("cmd-batch", result["command_id"])
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual("answered", saved["batches"][command["batch_id"]]["status"])
        changed_replay = copy.deepcopy(command)
        changed_replay["answers"][0]["option_id"] = "describe"
        with self.assertRaisesRegex(CommandError, "bound to a different payload"):
            controller.execute(state["session_id"], changed_replay)
        bad = dict(command, command_id="cmd-wrong", batch_id="batch-wrong")
        with self.assertRaisesRegex(CommandError, "open decision batch"):
            controller.execute(state["session_id"], bad)

    def test_utterance_is_completed_only_and_deduplicated_by_item_id(self):
        controller, _, worker = make_controller()
        state, _ = controller.create_session()
        first = {
            "command_id": "spoken-1", "session_id": state["session_id"],
            "type": "utterance", "expected_version": 1,
            "item_id": "item-voice-1", "transcript": "Make the hero warmer",
        }
        controller.execute(state["session_id"], first)
        duplicate = dict(first, command_id="spoken-2")
        result = controller.execute(state["session_id"], duplicate)
        self.assertTrue(result["deduplicated"])
        self.assertEqual(1, worker.calls)

    def test_worker_failure_does_not_partially_advance_state_or_replay(self):
        controller, store, worker = make_controller(worker=CountingWorker(fail=True))
        state, _ = controller.create_session()
        command = {
            "command_id": "cmd-fail", "session_id": state["session_id"],
            "type": "answer", "expected_version": 1, "question_id": "q-cta",
            "option_id": "describe",
        }
        with self.assertRaisesRegex(RuntimeError, "synthetic worker failed"):
            controller.execute(state["session_id"], command)
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual(1, saved["artifact_version"])
        self.assertEqual(3, saved["last_seq"])
        self.assertEqual("open", saved["questions"][0]["status"])
        self.assertEqual("failed", saved["commands"]["cmd-fail"]["status"])
        with self.assertRaises(CommandError):
            controller.execute(state["session_id"], command)
        self.assertEqual(1, worker.calls)

    def test_pause_and_stop_are_enforced_and_duplicate_stop_replays(self):
        controller, _, worker = make_controller()
        state, _ = controller.create_session()
        base = {"session_id": state["session_id"], "expected_version": 1}
        controller.execute(state["session_id"], {**base, "command_id": "pause-1", "type": "pause"})
        with self.assertRaisesRegex(CommandError, "paused"):
            controller.execute(state["session_id"], {
                **base, "command_id": "answer-paused", "type": "answer",
                "question_id": "q-cta", "option_id": "book",
            })
        controller.execute(state["session_id"], {**base, "command_id": "resume-1", "type": "resume"})
        stop = {**base, "command_id": "stop-1", "type": "stop"}
        first = controller.execute(state["session_id"], stop)
        self.assertEqual(first, controller.execute(state["session_id"], stop))
        self.assertEqual(0, worker.calls)

    def test_command_limit_refuses_growth_without_evicting_receipts(self):
        controller, store, _ = make_controller(max_commands=2)
        state, _ = controller.create_session()
        base = {"session_id": state["session_id"], "expected_version": 1}
        controller.execute(state["session_id"], {**base, "command_id": "pause-1", "type": "pause"})
        controller.execute(state["session_id"], {**base, "command_id": "resume-1", "type": "resume"})
        with self.assertRaisesRegex(CommandError, "command limit") as caught:
            controller.execute(state["session_id"], {**base, "command_id": "pause-2", "type": "pause"})
        self.assertEqual(429, caught.exception.status)
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual({"pause-1", "resume-1"}, set(saved["commands"]))

    def test_stale_inflight_is_terminal_unknown_and_no_longer_wedges_session(self):
        controller, store, worker = make_controller(now=1000)
        state, _ = controller.create_session()
        record = StudioRepository(store).load(state["session_id"])
        record.state["commands"]["lost-1"] = {
            "status": "inflight", "started_at": 800, "expected_version": 1,
        }
        record.state["active_command"] = "lost-1"
        StudioRepository(store).save(state["session_id"], record.state, record.token)
        controller.execute(state["session_id"], {
            "command_id": "pause-after-crash", "session_id": state["session_id"],
            "type": "pause", "expected_version": 1,
        })
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertTrue(saved["commands"]["lost-1"]["outcome_unknown"])
        self.assertIsNone(saved["active_command"])
        self.assertEqual(0, worker.calls)

    def test_stop_fences_inflight_and_voice_cannot_activate_after_stop(self):
        controller, store, _ = make_controller(max_commands=1)
        state, _ = controller.create_session()
        controller.begin_voice(state["session_id"], "voice-1", 1600)
        record = StudioRepository(store).load(state["session_id"])
        record.state["commands"]["lost-1"] = {
            "status": "inflight", "started_at": 1000, "expected_version": 1,
        }
        record.state["active_command"] = "lost-1"
        StudioRepository(store).save(state["session_id"], record.state, record.token)
        result = controller.execute(state["session_id"], {
            "command_id": "stop-priority", "session_id": state["session_id"],
            "type": "stop", "expected_version": 0,
        })
        self.assertEqual("session.ended", result["events"][0]["type"])
        with self.assertRaisesRegex(CommandError, "stopped"):
            controller.activate_voice(state["session_id"], "voice-1", "rtc_late")
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertTrue(saved["commands"]["lost-1"]["outcome_unknown"])

    def test_ambiguous_voice_open_keeps_single_call_admission_closed(self):
        controller, store, _ = make_controller()
        state, _ = controller.create_session()
        controller.begin_voice(state["session_id"], "voice-unknown", 1600)
        controller.mark_voice_unknown(state["session_id"], "voice-unknown")
        with self.assertRaisesRegex(CommandError, "already has a voice call"):
            controller.begin_voice(state["session_id"], "voice-second", 1600)
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual("unknown", saved["voice_call"]["status"])

    def test_daily_cap_is_strict_under_concurrent_admission(self):
        store = MemoryStore()
        controller, _, _ = make_controller(store=store, cap=5)

        def create(_):
            try:
                controller.create_session()
                return "ok"
            except StateConflict:
                return "full"

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(create, range(20)))
        self.assertEqual(5, results.count("ok"))
        state, _ = store.load("studio_admission_19700101")
        self.assertEqual(5, state["count"])

    def test_reducer_fences_session_generation_revision_and_artifact(self):
        controller, _, _ = make_controller()
        state, _ = controller.create_session()
        baseline = copy.deepcopy(state)
        foreign = copy.deepcopy(state["events"][-1])
        foreign.update({"session_id": "other", "seq": 4, "op_id": "foreign"})
        self.assertFalse(reduce_event(state, foreign))
        self.assertEqual(baseline, state)

        old_revision = copy.deepcopy(state["events"][-1])
        old_revision.update({"seq": 4, "op_id": "old-rev", "task_revision": 0})
        self.assertFalse(reduce_event(state, old_revision))

        gap = copy.deepcopy(state["events"][-1])
        gap.update({"seq": 5, "op_id": "gap"})
        self.assertFalse(reduce_event(state, gap))

        stale_patch = copy.deepcopy(state["events"][1])
        stale_patch.update({
            "seq": 4, "op_id": "stale-patch", "type": "artifact.patch",
            "artifact_version": 1, "payload": {"ops": []},
        })
        self.assertFalse(reduce_event(state, stale_patch))

        new_generation = copy.deepcopy(state["events"][1])
        new_generation.update({"generation": 2, "seq": 1, "op_id": "gen-2", "artifact_version": 1})
        self.assertTrue(reduce_event(state, new_generation))
        self.assertEqual((2, 1), (state["generation"], state["last_seq"]))

        non_snapshot = copy.deepcopy(new_generation)
        non_snapshot.update({
            "generation": 3, "seq": 1, "op_id": "gen-3-progress",
            "type": "progress", "payload": {"artifact_ids": ["screen-home"], "text": "No"},
        })
        self.assertFalse(reduce_event(state, non_snapshot))

        before = copy.deepcopy(state)
        bad_new_generation = copy.deepcopy(new_generation)
        bad_new_generation.update({"generation": 3, "op_id": "gen-3-bad", "task_revision": 0})
        self.assertFalse(reduce_event(state, bad_new_generation))
        self.assertEqual(before, state)

    def test_bounded_replay_returns_snapshot_repair_for_a_gap(self):
        controller, store, _ = make_controller(max_events=2)
        state, _ = controller.create_session()
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual([2, 3], [event["seq"] for event in saved["events"]])
        events, repaired, repaired_state = controller.events_after(state["session_id"], 0)
        self.assertTrue(repaired)
        self.assertEqual("artifact.snapshot", events[0]["type"])
        self.assertEqual({"root"}, set(events[0]["payload"]))
        self.assertEqual((2, 1), (events[0]["generation"], events[0]["seq"]))
        self.assertEqual(2, repaired_state["generation"])
        events, repaired, _ = controller.events_after(state["session_id"], 1)
        self.assertFalse(repaired)
        self.assertEqual([2], [event["seq"] for event in events])


class ApiTests(unittest.TestCase):
    origin = {"Origin": "https://www.sfdc24.com"}

    def app(self, **setting_overrides):
        self.store = MemoryStore()
        self.ids = IDs()
        self.voice_client = FakeVoiceClient()
        self.email_sender = EmailSender()
        self.creation_seq = 0
        configured = settings(**setting_overrides)
        app = create_app(
            settings=configured, store=self.store, worker=CountingWorker(),
            clock=lambda: 1000, id_factory=self.ids, voice_client=self.voice_client,
            email_sender=self.email_sender,
        )
        return app, configured

    def authenticate(self, client):
        client_key = "browser-instance-1234567890"
        sends_before = len(self.email_sender.calls)
        started = client.post("/v1/auth/start", headers=self.origin, json={
            "email": "operator@example.com", "client_key": client_key,
        })
        self.assertEqual(200, started.status_code, started.text)
        self.assertEqual(sends_before + 1, len(self.email_sender.calls))
        code = self.email_sender.calls[-1][1]
        verified = client.post("/v1/auth/verify", headers=self.origin, json={
            "challenge_id": started.json()["challenge_id"],
            "email": "operator@example.com",
            "code": code,
            "client_key": client_key,
        })
        self.assertEqual(200, verified.status_code, verified.text)
        return verified.json()["token"]

    def create_session(self, client, creation_id=None):
        operator_token = self.authenticate(client)
        if creation_id is None:
            self.creation_seq += 1
            creation_id = "create-%d" % self.creation_seq
        response = client.post(
            "/v1/session",
            headers={**self.origin, "Authorization": "Bearer " + operator_token},
            json={"title": "Test", "creation_id": creation_id},
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def test_origin_is_fail_closed_and_daily_cap_returns_429(self):
        app, _ = self.app(daily_session_cap=1)
        with TestClient(app) as client:
            self.assertEqual(403, client.post("/v1/session", headers={"Origin": "https://evil.example"}).status_code)
            self.assertEqual(401, client.post("/v1/session", headers=self.origin, json={}).status_code)
            self.assertFalse(any(name.startswith("studio_admission_") for name in self.store.data))
            self.create_session(client)
            operator = self.authenticate(client)
            self.assertEqual(429, client.post(
                "/v1/session",
                headers={**self.origin, "Authorization": "Bearer " + operator},
                json={"creation_id": "at-cap"},
            ).status_code)

    def test_auth_start_is_enumeration_safe_and_wrong_scope_cannot_admit(self):
        app, _ = self.app()
        with TestClient(app) as client:
            unknown = client.post("/v1/auth/start", headers=self.origin, json={
                "email": "unknown@example.com", "client_key": "browser-unknown",
            })
            allowed = client.post("/v1/auth/start", headers=self.origin, json={
                "email": "operator@example.com", "client_key": "browser-allowed",
            })
            self.assertEqual(set(unknown.json()), set(allowed.json()))
            self.assertEqual(1, len(self.email_sender.calls))
            created = self.create_session(client)
            denied = client.post(
                "/v1/session",
                headers={**self.origin, "Authorization": "Bearer " + created["token"]},
                json={"creation_id": "wrong-scope"},
            )
            self.assertEqual(401, denied.status_code)

    def test_session_creation_id_is_idempotent_and_consumes_one_admission(self):
        app, _ = self.app(daily_session_cap=2)
        with TestClient(app) as client:
            operator = self.authenticate(client)
            headers = {**self.origin, "Authorization": "Bearer " + operator}
            body = {"title": "Same", "creation_id": "stable-create-1"}
            first = client.post("/v1/session", headers=headers, json=body)
            second = client.post("/v1/session", headers=headers, json=body)
            self.assertEqual(200, first.status_code, first.text)
            self.assertEqual(first.json()["session_id"], second.json()["session_id"])
            ledger = next(
                value for name, value in self.store.data.items()
                if name.startswith("studio_admission_")
            )
            self.assertEqual(1, ledger["count"])

    def test_production_rejects_weak_signing_and_missing_email_configuration(self):
        with mock.patch.dict("os.environ", {"K_SERVICE": "studio"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "32 bytes"):
                settings(
                    session_secret="short",
                    state_uri="gs://bucket/prefix",
                    email_sender_url="https://sender.example",
                    email_sender_secret="x" * 32,
                ).validate()
            with self.assertRaisesRegex(RuntimeError, "MAINTENANCE_SECRET"):
                settings(
                    state_uri="gs://bucket/prefix",
                    email_sender_url="https://sender.example",
                    email_sender_secret="x" * 32,
                    voice_enabled=True,
                    openai_api_key="server-key",
                    maintenance_secret="",
                ).validate()
            with self.assertRaisesRegex(RuntimeError, "STUDIO_OPERATOR_EMAILS"):
                settings(
                    state_uri="gs://bucket/prefix",
                    operator_emails=(),
                    email_sender_url="https://sender.example",
                    email_sender_secret="x" * 32,
                ).validate()

    def test_apps_script_redirect_receipt_is_followed_and_request_is_signed(self):
        store = MemoryStore()
        ids = IDs()
        secret = "email-adapter-secret-that-is-at-least-32-bytes"
        observed = {"requests": []}

        def route(request):
            observed["requests"].append(request)
            if request.url.host == "script.google.com":
                observed["payload"] = json.loads(request.content)
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://script.googleusercontent.com/macros/echo?receipt=1"
                    },
                )
            return httpx.Response(200, json={"ok": True})

        email_client = httpx.Client(
            transport=httpx.MockTransport(route), follow_redirects=True, max_redirects=3
        )
        app = create_app(
            settings=settings(
                email_sender_url="https://script.google.com/macros/s/test/exec",
                email_sender_secret=secret,
            ),
            store=store,
            worker=CountingWorker(),
            clock=lambda: 1000,
            id_factory=ids,
            voice_client=FakeVoiceClient(),
            email_client=email_client,
        )
        try:
            with TestClient(app) as client:
                response = client.post("/v1/auth/start", headers=self.origin, json={
                    "email": "operator@example.com", "client_key": "browser-http-adapter",
                })
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual({"challenge_id", "expires_in"}, set(response.json()))
        finally:
            email_client.close()

        self.assertEqual(2, len(observed["requests"]))
        payload = observed["payload"]
        self.assertEqual(
            {"timestamp", "nonce", "email", "code", "signature"}, set(payload)
        )
        canonical = "\n".join(
            (payload["timestamp"], payload["nonce"], payload["email"], payload["code"])
        )
        expected = hmac.new(
            secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        self.assertTrue(hmac.compare_digest(expected, payload["signature"]))
        self.assertNotIn(payload["code"], json.dumps(response.json()))

    def test_signed_token_sse_resume_and_session_binding(self):
        app, configured = self.app()
        with TestClient(app) as client:
            created = self.create_session(client)
            claims = verify_token(created["token"], configured.session_secret, now=1000, scope="session")
            self.assertEqual(created["session_id"], claims["sid"])
            response = client.get(
                created["events_url"],
                headers={**self.origin, "Last-Event-ID": "2", "Authorization": "Bearer " + created["token"]},
                params={"once": "true"},
            )
            self.assertEqual(200, response.status_code, response.text)
            self.assertIn("id: 3", response.text)
            self.assertIn("event: question.asked", response.text)
            self.assertNotIn("id: 1", response.text)
            unauthenticated = client.get(
                created["events_url"], headers=self.origin, params={"once": "true"}
            )
            self.assertEqual(401, unauthenticated.status_code)

    def test_command_endpoint_replays_same_receipt(self):
        app, _ = self.app()
        with TestClient(app) as client:
            created = self.create_session(client)
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            command = {
                "command_id": "cmd-api", "session_id": created["session_id"],
                "type": "answer", "expected_version": 1, "question_id": "q-cta",
                "option_id": "book", "answer_source": "tap",
            }
            url = "/v1/session/%s/commands" % created["session_id"]
            first = client.post(url, headers=headers, json=command)
            second = client.post(url, headers=headers, json=command)
            self.assertEqual(200, first.status_code, first.text)
            self.assertEqual(first.json(), second.json())

    def test_voice_relays_unified_sdp_and_keeps_standard_key_server_side(self):
        app, configured = self.app(
            openai_api_key="standard-key-must-stay-server-side", voice_enabled=True,
            maintenance_secret="m" * 32,
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            response = client.post(
                "/v1/session/%s/voice" % created["session_id"],
                headers={**self.origin, "Authorization": "Bearer " + created["token"]},
                json={"sdp": "v=0\r\no=browser-offer"},
            )
            self.assertEqual(200, response.status_code, response.text)
            body = response.json()
            self.assertEqual("v=0\r\no=answer", body["sdp"])
            self.assertRegex(body["voice_id"], r"^voice-[0-9a-f]{32}$")
            self.assertEqual(1600, body["ends_at"])
            self.assertNotIn(configured.openai_api_key, json.dumps(body))
            self.assertEqual(1, len(self.voice_client.calls))
            url, kwargs = self.voice_client.calls[0]
            self.assertEqual("https://api.openai.com/v1/realtime/calls", url)
            self.assertEqual("Bearer " + configured.openai_api_key, kwargs["headers"]["Authorization"])
            self.assertIn("OpenAI-Safety-Identifier", kwargs["headers"])
            self.assertEqual("v=0\r\no=browser-offer", kwargs["files"]["sdp"][1])
            session_config = json.loads(kwargs["files"]["session"][1])
            self.assertEqual("realtime", session_config["type"])
            self.assertEqual("gpt-transcribe", session_config["audio"]["input"]["transcription"]["model"])
            self.assertFalse(session_config["audio"]["input"]["turn_detection"]["create_response"])
            self.assertNotIn(configured.openai_api_key, json.dumps(self.store.data))
            saved = StudioRepository(self.store).load(created["session_id"]).state
            self.assertEqual("rtc_test_call", saved["voice_call"]["call_id"])
            self.assertNotIn("v=0", json.dumps(saved))

    def test_voice_is_one_per_session_and_unconfigured_voice_burns_no_slot(self):
        app, _ = self.app(openai_api_key="", voice_enabled=True, maintenance_secret="m" * 32)
        with TestClient(app) as client:
            created = self.create_session(client)
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            url = "/v1/session/%s/voice" % created["session_id"]
            self.assertEqual(503, client.post(url, headers=headers, json={"sdp": "offer"}).status_code)
            state = StudioRepository(self.store).load(created["session_id"]).state
            self.assertNotIn("voice_call", state)

        app, _ = self.app(
            openai_api_key="server-key", voice_enabled=True, maintenance_secret="m" * 32
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            url = "/v1/session/%s/voice" % created["session_id"]
            self.assertEqual(200, client.post(url, headers=headers, json={"sdp": "offer"}).status_code)
            self.assertEqual(409, client.post(url, headers=headers, json={"sdp": "offer-2"}).status_code)
            self.assertEqual(1, len(self.voice_client.calls))

    def test_stop_hangs_up_active_voice_and_stays_available_with_stale_version(self):
        app, _ = self.app(
            openai_api_key="server-key", voice_enabled=True, maintenance_secret="m" * 32
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            voice_url = "/v1/session/%s/voice" % created["session_id"]
            self.assertEqual(200, client.post(voice_url, headers=headers, json={"sdp": "offer"}).status_code)
            command_url = "/v1/session/%s/commands" % created["session_id"]
            stopped = client.post(command_url, headers=headers, json={
                "command_id": "stop-api", "session_id": created["session_id"],
                "type": "stop", "expected_version": 0,
            })
            self.assertEqual(200, stopped.status_code, stopped.text)
            self.assertEqual(
                "https://api.openai.com/v1/realtime/calls/rtc_test_call/hangup",
                self.voice_client.calls[-1][0],
            )
            saved = StudioRepository(self.store).load(created["session_id"]).state
            self.assertEqual("ended", saved["voice_call"]["status"])

    def test_maintenance_voice_sweep_requires_its_separate_bearer(self):
        app, _ = self.app(
            openai_api_key="server-key", voice_enabled=True, maintenance_secret="m" * 32
        )
        with TestClient(app) as client:
            endpoint = "/v1/maintenance/voice-sweep"
            self.assertEqual(401, client.post(endpoint).status_code)
            self.assertEqual(
                401,
                client.post(endpoint, headers={"Authorization": "Bearer wrong"}).status_code,
            )
            accepted = client.post(
                endpoint, headers={"Authorization": "Bearer " + "m" * 32}
            )
            self.assertEqual(200, accepted.status_code, accepted.text)
            self.assertEqual({"ok": True, "checked_at": 1000}, accepted.json())



# ---- The Claude worker inside this controller (claude-code-cli, after #204) ----
# A fake Anthropic client stands in for the model: no network, no key.

class _FakeAnthropic:
    def __init__(self, *drafts):
        from types import SimpleNamespace
        self.drafts = list(drafts)
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        from types import SimpleNamespace
        draft = self.drafts[min(self.calls, len(self.drafts) - 1)]
        self.calls += 1
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps(draft))])


def _claude(*drafts):
    from workers.claude_worker import ClaudeWorker
    worker = ClaudeWorker(client=_FakeAnthropic(*drafts))
    seed = SyntheticWorker()                    # the same bootstrap app.main uses
    worker.initial_artifact = seed.initial_artifact
    worker.initial_questions = seed.initial_questions
    return worker


_NO_NODE = {"id": "", "kind": "text", "label": "", "detail": ""}
_BOOK_BY_VOICE = {
    "ops": [{"op": "set_label", "node_id": "hero-cta", "value": "Book a consultation", "new_node": _NO_NODE}],
    "confirm": "Changed the main action to Book a consultation.",
    "questions": [], "batch_title": "",
    "resolves": {"question_id": "q-cta", "option_id": "book", "freeform_answer": ""},
}


class _Resolver:
    """Names an answer without checking it - the controller must."""
    def __init__(self, resolves):
        self.resolves = resolves

    def initial_artifact(self):
        return SyntheticWorker().initial_artifact()

    def initial_questions(self):
        # A second open question, so a claim can point somewhere other than
        # the question a tap just answered.
        other = copy.deepcopy(SyntheticWorker().initial_questions()[0])
        other.update(question_id="q-other", affected_artifact_ids=["hero-heading"])
        return SyntheticWorker().initial_questions() + [other]

    def on_turn(self, state, trigger):
        return {"events": [], "problems": [], "resolves": dict(self.resolves)}


def _say(state, n, text, version):
    return {"command_id": "spoken-%d" % n, "session_id": state["session_id"], "type": "utterance",
            "expected_version": version, "item_id": "item-%d" % n, "transcript": text}


class ClaudeWorkerInControllerTests(unittest.TestCase):
    def test_a_spoken_answer_changes_the_prototype_and_closes_the_question(self):
        controller, store, _ = make_controller(worker=_claude(_BOOK_BY_VOICE))
        state, _ = controller.create_session()
        result = controller.execute(state["session_id"], _say(state, 1, "Let them book a call", 1))
        self.assertEqual([], result["problems"])
        self.assertEqual(["artifact.patch", "question.answered", "confirm"],
                         [e["type"] for e in result["events"]])
        answered = result["events"][1]["payload"]["question"]
        self.assertEqual(("q-cta", "answered", "book", "voice"),
                         (answered["question_id"], answered["status"], answered["selected_option"],
                          answered["answer_source"]))
        self.assertEqual((1, 2), (answered["artifact_version_before"], answered["artifact_version_after"]))
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual("answered", _find(saved, "q-cta")["status"])
        self.assertEqual(2, saved["artifact_version"])

    def test_a_second_claim_on_a_closed_question_is_a_problem_not_an_answer(self):
        stale, _, _ = make_controller(worker=_Resolver({"question_id": "q-cta", "option_id": "book"}))
        s2, _ = stale.create_session()
        stale.execute(s2["session_id"], {"command_id": "tap-1", "session_id": s2["session_id"], "type": "answer",
                                         "expected_version": 1, "question_id": "q-cta", "option_id": "describe"})
        result = stale.execute(s2["session_id"], _say(s2, 2, "book it", 1))
        self.assertEqual([], [e for e in result["events"] if e["type"] == "question.answered"])
        self.assertTrue(any("resolves 'q-cta' refused" in p for p in result["problems"]), result["problems"])

    def test_unknown_questions_and_foreign_options_are_refused(self):
        for resolves, why in (({"question_id": "q-nope", "option_id": "book"}, "unknown question_id"),
                              ({"question_id": "q-cta", "option_id": "delete-everything"}, "does not belong")):
            controller, store, _ = make_controller(worker=_Resolver(resolves))
            state, _ = controller.create_session()
            result = controller.execute(state["session_id"], _say(state, 1, "whatever", 1))
            self.assertTrue(any(why in p for p in result["problems"]), (resolves, result["problems"]))
            saved = StudioRepository(store).load(state["session_id"]).state
            self.assertEqual("open", _find(saved, "q-cta")["status"])

    def test_a_tap_never_lets_resolves_answer_another_question(self):
        controller, store, _ = make_controller(worker=_Resolver({"question_id": "q-other", "option_id": "work"}))
        state, _ = controller.create_session()
        controller.execute(state["session_id"], {"command_id": "tap-1", "session_id": state["session_id"],
                                                 "type": "answer", "expected_version": 1,
                                                 "question_id": "q-cta", "option_id": "book"})
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual(("book", "tap"), (_find(saved, "q-cta")["selected_option"],
                                           _find(saved, "q-cta")["answer_source"]))
        self.assertEqual("open", _find(saved, "q-other")["status"])

    def test_the_image_carries_the_worker_and_its_sdk(self):
        docker = (CONTROLLER / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY cloud/studio-controller/workers ./workers", docker)
        reqs = (CONTROLLER / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(reqs, r"(?m)^anthropic==\d")

    def test_studio_worker_claude_builds_through_app_main(self):
        from app.main import _worker
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-offline-test"}):
            worker = _worker(settings(worker="claude"))
        from workers.claude_worker import ClaudeWorker
        self.assertIsInstance(worker, ClaudeWorker)
        self.assertEqual("q-cta", worker.initial_questions()[0]["question_id"])


def _find(state, qid):
    return next(q for q in state["questions"] if q["question_id"] == qid)


if __name__ == "__main__":
    unittest.main()

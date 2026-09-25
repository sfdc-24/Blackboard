"""Offline controller tests: no provider, network, microphone, or credentials."""
from __future__ import annotations

import copy
import contextlib
import hashlib
import hmac
import io
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
from app import sweep_once  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.state import (  # noqa: E402
    SessionNotFound,
    StateConflict,
    StudioRepository,
    VoiceCapacityExceeded,
)
from app.tokens import mint_token, verify_token  # noqa: E402
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

    def test_a_blank_start_is_one_empty_screen_named_by_the_request(self):
        controller, _, _ = make_controller()
        state, _ = controller.create_session(
            "A lead intake form for a dental clinic", start="blank")
        self.assertEqual(["session.started", "artifact.snapshot"],
                         [event["type"] for event in state["events"]])
        root = state["events"][1]["payload"]["root"]
        self.assertEqual({"id": "screen", "kind": "screen",
                          "label": "A lead intake form for a dental clinic",
                          "children": []}, root)
        self.assertEqual([], state["questions"])
        self.assertEqual(1, state["artifact_version"])

    def test_the_template_start_is_unchanged(self):
        controller, _, _ = make_controller()
        state, _ = controller.create_session("Homepage", start="template")
        self.assertEqual("q-cta", state["questions"][0]["question_id"])
        self.assertEqual("screen-home", state["artifact"]["id"])

    def test_an_unknown_start_is_refused_before_admission(self):
        controller, store, _ = make_controller()
        with self.assertRaises(CommandError):
            controller.create_session("x", start="anything")
        self.assertFalse(any(name.startswith("studio_admission_") for name in store.data))

    def test_a_blank_session_takes_an_utterance_as_its_first_command(self):
        controller, _, worker = make_controller()
        state, _ = controller.create_session("A booking page", start="blank")
        controller.execute(state["session_id"], {
            "command_id": "cmd-1", "session_id": state["session_id"],
            "type": "utterance", "expected_version": 1,
            "item_id": "typed-1", "transcript": "A booking page for a yoga studio",
        })
        self.assertEqual(1, worker.calls)

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
        self.assertEqual("You ended this session.", first["events"][0]["payload"]["reason"])
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
        self.assertEqual("You ended this session.", result["events"][0]["payload"]["reason"])
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

    def test_voice_open_cap_is_strict_under_concurrent_reservations(self):
        store = MemoryStore()

        def reserve(index):
            repository = StudioRepository(store, clock=lambda: 1000, attempts=50)
            try:
                repository.reserve_voice_open(3, "voice-%d" % index)
                return "ok"
            except VoiceCapacityExceeded:
                return "full"

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(20)))
        self.assertEqual(3, results.count("ok"))
        self.assertEqual(17, results.count("full"))
        state, _ = store.load("studio_voice_open_19700101")
        self.assertEqual(3, state["count"])
        self.assertEqual(3, len(state["reservations"]))

    def test_voice_open_reservation_is_idempotent_and_bound_to_utc_day(self):
        store = MemoryStore()
        now = [1000]
        repository = StudioRepository(store, clock=lambda: now[0])
        first_reservation = repository.reserve_voice_open(3, "voice-stable")
        replay = repository.reserve_voice_open(3, "voice-stable")
        self.assertEqual(("19700101", 1), (
            first_reservation.day, first_reservation.number
        ))
        self.assertEqual(first_reservation, replay)
        first, _ = store.load("studio_voice_open_19700101")
        self.assertEqual(1, first["count"])

        now[0] += 86400
        restarted = StudioRepository(store, clock=lambda: now[0])
        next_day = restarted.reserve_voice_open(3, "voice-stable")
        self.assertEqual(("19700102", 1), (next_day.day, next_day.number))
        second, _ = store.load("studio_voice_open_19700102")
        self.assertEqual(1, second["count"])

    def test_voice_index_removal_is_owned_by_voice_id(self):
        store = MemoryStore()
        repository = StudioRepository(store, clock=lambda: 1000)
        repository.register_voice("session-stable", "voice-old", 1200)
        with self.assertRaisesRegex(StateConflict, "owned by another call"):
            repository.register_voice("session-stable", "voice-new", 1300)
        self.assertFalse(repository.unregister_voice(
            "session-stable", "voice-new"
        ))
        index, _ = store.load("studio_voice_index")
        self.assertEqual(
            {"voice_id": "voice-old", "ends_at": 1200},
            index["sessions"]["session-stable"],
        )
        self.assertTrue(repository.unregister_voice(
            "session-stable", "voice-old"
        ))
        self.assertEqual([], repository.due_voice_sessions(2000))

    def test_voice_index_reads_and_explicitly_adopts_legacy_entries(self):
        store = MemoryStore()
        store.save(
            "studio_voice_index",
            {"sessions": {"session-legacy": 1000}, "updated_at": 900},
            None,
        )
        repository = StudioRepository(store, clock=lambda: 1000)
        self.assertEqual(["session-legacy"], repository.due_voice_sessions())
        with self.assertRaisesRegex(StateConflict, "ownership is unknown"):
            repository.register_voice("session-legacy", "voice-current", 1100)
        self.assertFalse(repository.unregister_voice(
            "session-legacy", "voice-current"
        ))
        preserved, _ = store.load("studio_voice_index")
        self.assertEqual(1000, preserved["sessions"]["session-legacy"])
        repository.register_voice(
            "session-legacy", "voice-current", 1100, adopt_legacy=True
        )
        index, _ = store.load("studio_voice_index")
        self.assertEqual(
            {"voice_id": "voice-current", "ends_at": 1100},
            index["sessions"]["session-legacy"],
        )

    def test_voice_index_refuses_ownerless_structured_entry(self):
        store = MemoryStore()
        store.save(
            "studio_voice_index",
            {
                "sessions": {
                    "session-malformed": {"voice_id": "", "ends_at": 1000}
                },
                "updated_at": 900,
            },
            None,
        )
        repository = StudioRepository(store, clock=lambda: 1000)
        with self.assertRaisesRegex(StateConflict, "owned by another call"):
            repository.register_voice(
                "session-malformed", "voice-new", 1100
            )
        self.assertFalse(repository.unregister_voice(
            "session-malformed", "voice-new"
        ))
        index, _ = store.load("studio_voice_index")
        self.assertEqual(
            {"voice_id": "", "ends_at": 1000},
            index["sessions"]["session-malformed"],
        )

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


class FakeTalk:
    """Stands in for workers.talk.TalkClient: records calls, never touches a provider."""

    def __init__(self, agents=("claude", "openai"), reply="Building that now.", fail=False):
        self._agents = list(agents)
        self.text = reply
        self.fail = fail
        self.calls = []

    def agents(self):
        return list(self._agents)

    def reply(self, agent, text, history, canvas):
        self.calls.append({"agent": agent, "text": text, "history": history, "canvas": canvas})
        if self.fail:
            raise RuntimeError("provider down")
        return self.text


class TalkLaneTests(unittest.TestCase):
    origin = {"Origin": "https://www.sfdc24.com"}

    def make(self, talk=None, **overrides):
        self.store = MemoryStore()
        self.email_sender = EmailSender()
        self.talk = talk if talk is not None else FakeTalk()
        self.now = [1000.0]
        app = create_app(
            settings=settings(**overrides), store=self.store, worker=CountingWorker(),
            clock=lambda: self.now[0], id_factory=IDs(), voice_client=FakeVoiceClient(),
            email_sender=self.email_sender, talk_client=self.talk,
        )
        return app

    def post_spaced(self, client, url, headers, body):
        self.now[0] += 2.0
        return client.post(url, headers=headers, json=body)

    def session(self, client):
        started = client.post("/v1/auth/start", headers=self.origin, json={
            "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
        code = self.email_sender.calls[-1][1]
        operator = client.post("/v1/auth/verify", headers=self.origin, json={
            "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
            "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
        created = client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + operator},
                              json={"creation_id": "talk-1"}).json()
        return created["session_id"], {**self.origin, "Authorization": "Bearer " + created["token"]}

    def test_health_lists_the_configured_agents(self):
        with TestClient(self.make()) as client:
            features = client.get("/health").json()["features"]
        self.assertEqual(True, features["talk"])
        self.assertEqual(["claude", "openai"], features["agents"])

    def test_a_turn_gets_the_selected_agents_reply_grounded_in_the_canvas(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            response = client.post("/v1/session/%s/talk" % sid, headers=headers, json={
                "text": "Make a logo for my cafe", "agent": "openai",
                "history": [{"who": "you", "text": "hello"}, {"who": "openai", "text": "Hi there."}]})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"reply": "Building that now.", "speaker": "openai", "turn": 0}, response.json())
        call = self.talk.calls[0]
        self.assertEqual("openai", call["agent"])
        self.assertEqual("Make a logo for my cafe", call["text"])
        self.assertEqual(2, len(call["history"]))
        self.assertIn("parts", call["canvas"])

    def test_the_turn_number_is_echoed_and_validated(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/talk" % sid
            ok = self.post_spaced(client, url, headers, {"text": "hi", "turn": 7})
            bad = [self.post_spaced(client, url, headers, {"text": "hi", "turn": value}).status_code
                   for value in (-1, "7", 1.5, True, 10 ** 7)]
        self.assertEqual(7, ok.json()["turn"])
        self.assertEqual([400, 400, 400, 400, 400], bad)

    def test_the_agent_defaults_to_the_first_configured_one(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            response = client.post("/v1/session/%s/talk" % sid, headers=headers, json={"text": "hi"})
        self.assertEqual("claude", response.json()["speaker"])

    def test_an_unconfigured_agent_is_refused_not_simulated(self):
        with TestClient(self.make(FakeTalk(agents=("claude",)))) as client:
            sid, headers = self.session(client)
            response = client.post("/v1/session/%s/talk" % sid, headers=headers, json={
                "text": "hi", "agent": "openai"})
        self.assertEqual(400, response.status_code)
        self.assertIn("not configured", response.text)
        self.assertEqual([], self.talk.calls)

    def test_bad_bodies_are_refused(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/talk" % sid
            for body in ({"text": ""}, {"text": "   "}, {"text": "x" * 601}, {"text": "hi", "extra": 1},
                         {"text": "hi", "history": [{"who": "robot", "text": "x"}]},
                         {"text": "hi", "history": [{"who": "you", "text": "x"}] * 9}):
                with self.subTest(body=str(body)[:60]):
                    self.assertEqual(400, client.post(url, headers=headers, json=body).status_code)
        self.assertEqual([], self.talk.calls)

    def test_it_needs_the_sessions_own_token_and_an_allowed_origin(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/talk" % sid
            self.assertEqual(401, client.post(url, headers=self.origin, json={"text": "hi"}).status_code)
            self.assertEqual(403, client.post(url, headers={**headers, "Origin": "https://evil.example"},
                                              json={"text": "hi"}).status_code)
            self.assertEqual(403, client.post("/v1/session/other/talk", headers=headers,
                                              json={"text": "hi"}).status_code)
        self.assertEqual([], self.talk.calls)

    def test_the_per_session_cap_returns_429(self):
        with TestClient(self.make(talk_cap=2)) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/talk" % sid
            codes = [self.post_spaced(client, url, headers, {"text": "hi"}).status_code for _ in range(3)]
        self.assertEqual([200, 200, 429], codes)

    def test_turns_closer_than_the_spacing_are_refused(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/talk" % sid
            first = client.post(url, headers=headers, json={"text": "hi"}).status_code
            self.now[0] += 0.5
            too_soon = client.post(url, headers=headers, json={"text": "again"})
            self.now[0] += 1.5
            later = client.post(url, headers=headers, json={"text": "again"}).status_code
        self.assertEqual(200, first)
        self.assertEqual(429, too_soon.status_code)
        self.assertIn("one turn every", too_soon.text)
        self.assertEqual(200, later)
        self.assertEqual(2, len(self.talk.calls))

    def test_a_provider_failure_is_a_503_and_the_session_lives_on(self):
        with TestClient(self.make(FakeTalk(fail=True))) as client:
            sid, headers = self.session(client)
            failed = client.post("/v1/session/%s/talk" % sid, headers=headers, json={"text": "hi"})
            events = client.get("/v1/session/%s/events?once=1" % sid, headers=headers)
        self.assertEqual(503, failed.status_code)
        self.assertEqual(200, events.status_code)

    def test_a_stopped_session_does_not_talk(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "cmd-stop", "session_id": sid, "type": "stop", "expected_version": 1})
            response = client.post("/v1/session/%s/talk" % sid, headers=headers, json={"text": "hi"})
        self.assertEqual(410, response.status_code)
        self.assertEqual([], self.talk.calls)


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

    def test_the_session_route_takes_a_blank_start_and_refuses_others(self):
        app, _ = self.app()
        with TestClient(app) as client:
            operator = self.authenticate(client)
            headers = {**self.origin, "Authorization": "Bearer " + operator}
            ok = client.post("/v1/session", headers=headers, json={
                "creation_id": "blank-1", "title": "A quote request form", "start": "blank"})
            self.assertEqual(200, ok.status_code, ok.text)
            sid = ok.json()["session_id"]
            stored = [v for v in self.store.data.values()
                      if isinstance(v, dict) and v.get("session_id") == sid]
            self.assertEqual(1, len(stored))
            self.assertEqual([], stored[0]["artifact"]["children"])
            self.assertEqual("A quote request form", stored[0]["artifact"]["label"])
            self.assertEqual([], stored[0]["questions"])
            bad = client.post("/v1/session", headers=headers, json={
                "creation_id": "blank-2", "start": "freeform"})
            self.assertEqual(400, bad.status_code)
            unknown = client.post("/v1/session", headers=headers, json={
                "creation_id": "blank-3", "brief": "x"})
            self.assertEqual(400, unknown.status_code)

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

    def test_sse_missing_session_is_json_404_before_stream_headers(self):
        app, configured = self.app()
        token = mint_token("missing", 1600, configured.session_secret)
        headers = {**self.origin, "Authorization": "Bearer " + token}
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/v1/session/missing/events", headers=headers)
            self.assertEqual(404, response.status_code, response.text)
            self.assertEqual({"detail": "session not found"}, response.json())
            self.assertIn("application/json", response.headers["content-type"])
            self.assertEqual("no-store", response.headers["cache-control"])
            # Authentication/origin still precede any state disclosure.
            self.assertEqual(401, client.get(
                "/v1/session/missing/events", headers=self.origin).status_code)
            self.assertEqual(403, client.get(
                "/v1/session/missing/events", headers={**headers, "Origin": "https://evil.example"}
            ).status_code)

    def test_sse_repair_conflict_is_json_409_before_stream_headers(self):
        app, configured = self.app()
        token = mint_token("conflict", 1600, configured.session_secret)
        with TestClient(app, raise_server_exceptions=False) as client, mock.patch.object(
            app.state.controller, "events_after", side_effect=StateConflict("snapshot repair is busy; reconnect")
        ) as read:
            response = client.get("/v1/session/conflict/events", headers={
                **self.origin, "Authorization": "Bearer " + token,
            })
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn("application/json", response.headers["content-type"])
            self.assertEqual("no-store", response.headers["cache-control"])
            self.assertEqual(1, read.call_count)

    def test_sse_preflight_batch_is_reused_and_repair_generation_is_exposed(self):
        app, _ = self.app()
        with TestClient(app) as client:
            created = self.create_session(client)
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            with mock.patch.object(app.state.controller, "events_after", wraps=app.state.controller.events_after) as read:
                response = client.get(created["events_url"], headers=headers, params={"once": "true"})
                self.assertEqual(200, response.status_code)
                self.assertEqual(1, read.call_count, "preflight must not read/replay the initial batch twice")
                self.assertEqual(1, response.text.count("id: 1\n"))
                self.assertEqual("1", response.headers.get("x-studio-generation"))
            repaired = client.get(created["events_url"], headers={**headers, "Last-Event-ID": "999"}, params={"once": "true"})
            self.assertEqual(200, repaired.status_code, repaired.text)
            self.assertEqual("2", repaired.headers.get("x-studio-generation"))
            self.assertTrue(repaired.text.startswith("id: 1\nevent: artifact.snapshot\n"))
            self.assertEqual(400, client.get(created["events_url"], headers={**headers, "Last-Event-ID": "not-an-integer"}).status_code)

    def test_sse_midstream_state_failure_closes_without_forging_an_event(self):
        for failure in (SessionNotFound("gone"), StateConflict("busy")):
            with self.subTest(failure=type(failure).__name__):
                app, _ = self.app(sse_wait_seconds=25, sse_poll_seconds=0)
                with TestClient(app) as client:
                    created = self.create_session(client)
                    headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
                    initial = app.state.controller.events_after(created["session_id"], 0)
                    with mock.patch.object(app.state.controller, "events_after", side_effect=[initial, failure]) as read:
                        response = client.get(created["events_url"], headers=headers)
                        self.assertEqual(200, response.status_code)
                        self.assertEqual(2, read.call_count)
                        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                        self.assertEqual(initial[0], frames, "do not fabricate durable events or hide already-sent data")
                    # The next connection can now receive a real HTTP refusal.
                    with mock.patch.object(app.state.controller, "events_after", side_effect=failure):
                        retry = client.get(created["events_url"], headers=headers)
                        self.assertEqual(404 if isinstance(failure, SessionNotFound) else 409, retry.status_code)

    def test_canonical_health_and_slash_variants_never_redirect_bearers(self):
        app, _ = self.app()
        # Emulate TLS termination: the application sees HTTP plus forwarded HTTPS.
        with TestClient(app, base_url="http://service.example", follow_redirects=False) as client:
            headers = {**self.origin, "X-Forwarded-Proto": "https", "Authorization": "Bearer synthetic-test-token"}
            health = client.get("/health", headers=headers)
            self.assertEqual(200, health.status_code)
            self.assertEqual({"ok": True, "worker": "synthetic", "state_backend": "file",
                          "features": {"voice": False, "lead_facts": False, "talk": False, "agents": [],
                               "analyst": False, "governance": True, "muse": False, "topics": True, "voices": [], "public_visitors": False, "rating": True,
                               "summary_email": False, "routing": True}},
                         health.json())
            self.assertEqual(health.json(), client.get("/healthz").json())
            for method, path in (("GET", "/health/"), ("GET", "/healthz/"),
                                 ("POST", "/v1/auth/start/"), ("POST", "/v1/auth/verify/"),
                                 ("POST", "/v1/session/"), ("POST", "/v1/session/test/commands/"),
                                 ("POST", "/v1/session/test/voice/"), ("GET", "/v1/session/test/events/"),
                                 ("POST", "/v1/maintenance/voice-sweep/")):
                with self.subTest(path=path):
                    response = client.request(method, path, headers=headers, json={"synthetic": "sensitive-body"})
                    self.assertEqual(404, response.status_code)
                    self.assertNotIn("location", response.headers)
            self.assertEqual([], self.email_sender.calls)
            self.assertEqual([], self.voice_client.calls)
            self.assertEqual({}, self.store.data)

    def test_refused_slash_preflight_and_health_do_not_sweep_due_voice(self):
        app, _ = self.app(voice_enabled=True, openai_api_key="fake-provider-key", maintenance_secret="m" * 32)
        state, _ = app.state.controller.create_session()
        session_id = state["session_id"]
        app.state.controller.begin_voice(session_id, "voice-test", 1000)
        app.state.controller.activate_voice(session_id, "voice-test", "rtc_due_test")
        StudioRepository(self.store).register_voice(session_id, "voice-test", 1000)
        before = copy.deepcopy(self.store.data)
        with TestClient(app, base_url="http://service.example", follow_redirects=False) as client:
            for method, path in (("GET", "/health"), ("GET", "/healthz"),
                                 ("POST", "/v1/session/"), ("OPTIONS", "/v1/auth/start/")):
                with self.subTest(method=method, path=path):
                    response = client.request(method, path, headers={
                        **self.origin, "X-Forwarded-Proto": "https", "Access-Control-Request-Method": "POST",
                    })
                    self.assertEqual(200 if path in {"/health", "/healthz"} else 404, response.status_code)
                    self.assertNotIn("location", response.headers)
                    self.assertEqual([], self.voice_client.calls)
                    self.assertEqual(before, self.store.data)
            # The separately authenticated maintenance path still performs cleanup.
            response = client.post("/v1/maintenance/voice-sweep", headers={"Authorization": "Bearer " + "m" * 32})
            self.assertEqual(200, response.status_code)
            self.assertEqual({
                "ok": True,
                "checked_at": 1000,
                "due": 1,
                "attempted": 1,
                "completed": 1,
                "pending": 0,
                "index_available": True,
            }, response.json())
            self.assertEqual(1, len(self.voice_client.calls))
            self.assertEqual("ended", StudioRepository(self.store).load(session_id).state["voice_call"]["status"])

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
            from app.main import HOST_INSTRUCTIONS
            self.assertEqual(HOST_INSTRUCTIONS, session_config["instructions"])
            self.assertNotIn(configured.openai_api_key, json.dumps(self.store.data))
            saved = StudioRepository(self.store).load(created["session_id"]).state
            self.assertEqual("rtc_test_call", saved["voice_call"]["call_id"])
            self.assertNotIn("v=0", json.dumps(saved))

    def test_voice_open_cap_allows_exactly_three_provider_posts(self):
        app, _ = self.app(
            openai_api_key="server-key", voice_enabled=True,
            maintenance_secret="m" * 32, voice_mint_cap=3,
        )
        with TestClient(app) as client:
            operator_token = self.authenticate(client)
            created = []
            for index in range(4):
                response = client.post(
                    "/v1/session",
                    headers={
                        **self.origin,
                        "Authorization": "Bearer " + operator_token,
                    },
                    json={"title": "Test", "creation_id": "voice-cap-%d" % index},
                )
                self.assertEqual(200, response.status_code, response.text)
                created.append(response.json())
            responses = [client.post(
                "/v1/session/%s/voice" % item["session_id"],
                headers={**self.origin, "Authorization": "Bearer " + item["token"]},
                json={"sdp": "offer-%d" % index},
            ) for index, item in enumerate(created)]
        self.assertEqual([200, 200, 200, 429], [item.status_code for item in responses])
        self.assertEqual(3, len(self.voice_client.calls))
        ledger, _ = self.store.load("studio_voice_open_19700101")
        self.assertEqual(3, ledger["count"])
        denied = StudioRepository(self.store).load(created[-1]["session_id"]).state
        self.assertNotIn("voice_call", denied)
        index, _ = self.store.load("studio_voice_index")
        self.assertNotIn(created[-1]["session_id"], index.get("sessions") or {})

    def test_prior_day_reservation_cannot_authorize_a_new_day_provider_post(self):
        now = [86400]
        self.store = MemoryStore()
        repository = StudioRepository(self.store, clock=lambda: now[0])
        for index in range(3):
            repository.reserve_voice_open(3, "new-day-existing-%d" % index)
        now[0] = 86399
        self.ids = IDs()
        self.voice_client = FakeVoiceClient()
        self.email_sender = EmailSender()
        self.creation_seq = 0
        app = create_app(
            settings=settings(
                openai_api_key="server-key", voice_enabled=True,
                maintenance_secret="m" * 32, voice_mint_cap=3,
            ),
            store=self.store,
            worker=CountingWorker(),
            clock=lambda: now[0],
            id_factory=self.ids,
            voice_client=self.voice_client,
            email_sender=self.email_sender,
        )
        original_reserve = StudioRepository.reserve_voice_open
        first_reservation = [True]

        def reserve_then_cross_midnight(repository, limit, reservation_id):
            result = original_reserve(repository, limit, reservation_id)
            if first_reservation[0]:
                first_reservation[0] = False
                now[0] = 86400
            return result

        with TestClient(app) as client:
            created = self.create_session(client)
            url = "/v1/session/%s/voice" % created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            with mock.patch.object(
                    StudioRepository, "reserve_voice_open",
                    new=reserve_then_cross_midnight):
                response = client.post(url, headers=headers, json={"sdp": "offer"})
        self.assertEqual(429, response.status_code, response.text)
        self.assertEqual([], self.voice_client.calls)
        old_day, _ = self.store.load("studio_voice_open_19700101")
        new_day, _ = self.store.load("studio_voice_open_19700102")
        self.assertEqual(1, old_day["count"])
        self.assertEqual(3, new_day["count"])
        saved = StudioRepository(self.store).load(created["session_id"]).state
        self.assertNotIn("voice_call", saved)
        index, _ = self.store.load("studio_voice_index")
        self.assertNotIn(created["session_id"], index.get("sessions") or {})

    def test_invalid_voice_requests_never_debit_open_capacity(self):
        app, _ = self.app(
            openai_api_key="server-key", voice_enabled=True,
            maintenance_secret="m" * 32, voice_mint_cap=3,
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            url = "/v1/session/%s/voice" % created["session_id"]
            self.assertEqual(403, client.post(
                url, headers={"Origin": "https://evil.example"}, json={"sdp": "offer"}
            ).status_code)
            self.assertEqual(401, client.post(
                url, headers=self.origin, json={"sdp": "offer"}
            ).status_code)
            self.assertEqual(400, client.post(
                url,
                headers={**self.origin, "Authorization": "Bearer " + created["token"]},
                json={"sdp": ""},
            ).status_code)
        self.assertEqual([], self.voice_client.calls)
        self.assertFalse(any(
            name.startswith("studio_voice_open_") for name in self.store.data
        ))

    def test_provider_rejections_consume_capacity_and_are_non_retryable(self):
        class RefusingVoiceClient(FakeVoiceClient):
            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse(status_code=400)

        self.store = MemoryStore()
        self.ids = IDs()
        self.voice_client = RefusingVoiceClient()
        self.email_sender = EmailSender()
        self.creation_seq = 0
        app = create_app(
            settings=settings(
                openai_api_key="server-key", voice_enabled=True,
                maintenance_secret="m" * 32, voice_mint_cap=3,
            ),
            store=self.store,
            worker=CountingWorker(),
            clock=lambda: 1000,
            id_factory=self.ids,
            voice_client=self.voice_client,
            email_sender=self.email_sender,
        )
        with TestClient(app) as client:
            operator_token = self.authenticate(client)
            created = []
            for index in range(4):
                admitted = client.post(
                    "/v1/session",
                    headers={
                        **self.origin,
                        "Authorization": "Bearer " + operator_token,
                    },
                    json={
                        "title": "Test",
                        "creation_id": "voice-rejection-%d" % index,
                    },
                )
                self.assertEqual(200, admitted.status_code, admitted.text)
                created.append(admitted.json())
            responses = [client.post(
                "/v1/session/%s/voice" % item["session_id"],
                headers={**self.origin, "Authorization": "Bearer " + item["token"]},
                json={"sdp": "offer-%d" % index},
            ) for index, item in enumerate(created)]
        self.assertEqual([502, 502, 502, 429], [item.status_code for item in responses])
        self.assertEqual(3, len(self.voice_client.calls))
        ledger, _ = self.store.load("studio_voice_open_19700101")
        self.assertEqual(3, ledger["count"])
        for item in created[:3]:
            saved = StudioRepository(self.store).load(item["session_id"]).state
            self.assertEqual("unknown", saved["voice_call"]["status"])
            self.assertIn(
                item["session_id"], StudioRepository(self.store).due_voice_sessions(1600)
            )
        denied = StudioRepository(self.store).load(created[-1]["session_id"]).state
        self.assertNotIn("voice_call", denied)

    def test_pre_provider_cleanup_retains_reservation_until_index_is_removed(self):
        self.store = MemoryStore()
        repository = StudioRepository(self.store, clock=lambda: 1000)
        for index in range(3):
            repository.reserve_voice_open(3, "capacity-existing-%d" % index)
        self.ids = IDs()
        self.voice_client = FakeVoiceClient()
        self.email_sender = EmailSender()
        self.creation_seq = 0
        app = create_app(
            settings=settings(
                openai_api_key="server-key", voice_enabled=True,
                maintenance_secret="m" * 32, voice_mint_cap=3,
            ),
            store=self.store,
            worker=CountingWorker(),
            clock=lambda: 1000,
            id_factory=self.ids,
            voice_client=self.voice_client,
            email_sender=self.email_sender,
        )
        index_removed = threading.Event()
        allow_session_release = threading.Event()
        original_unregister = StudioRepository.unregister_voice

        with TestClient(app) as client:
            created = self.create_session(client)
            url = "/v1/session/%s/voice" % created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}

            def block_after_owned_index_removal(
                    repository, session_id, voice_id=None, *, force=False,
                    allow_legacy=False):
                removed = original_unregister(
                    repository,
                    session_id,
                    voice_id,
                    force=force,
                    allow_legacy=allow_legacy,
                )
                if session_id == created["session_id"] and voice_id:
                    index_removed.set()
                    if not allow_session_release.wait(5):
                        raise RuntimeError("test barrier timed out")
                return removed

            with mock.patch.object(
                    StudioRepository, "unregister_voice",
                    new=block_after_owned_index_removal):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        client.post, url, headers=headers, json={"sdp": "offer"}
                    )
                    self.assertTrue(index_removed.wait(5))
                    during = StudioRepository(self.store).load(
                        created["session_id"]
                    ).state
                    self.assertEqual("opening", during["voice_call"]["status"])
                    with self.assertRaisesRegex(
                            CommandError, "already has a voice call"):
                        app.state.controller.begin_voice(
                            created["session_id"], "voice-racing", 1600
                        )
                    index, _ = self.store.load("studio_voice_index")
                    self.assertNotIn(
                        created["session_id"], index.get("sessions") or {}
                    )
                    allow_session_release.set()
                    response = pending.result(timeout=5)
        self.assertEqual(429, response.status_code, response.text)
        after = StudioRepository(self.store).load(created["session_id"]).state
        self.assertNotIn("voice_call", after)
        self.assertEqual([], self.voice_client.calls)

    def test_provider_5xx_is_ambiguous_indexed_and_non_retryable(self):
        class AmbiguousVoiceClient(FakeVoiceClient):
            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse(status_code=503)

        self.store = MemoryStore()
        self.ids = IDs()
        self.voice_client = AmbiguousVoiceClient()
        self.email_sender = EmailSender()
        self.creation_seq = 0
        app = create_app(
            settings=settings(
                openai_api_key="server-key", voice_enabled=True,
                maintenance_secret="m" * 32, voice_mint_cap=3,
            ),
            store=self.store,
            worker=CountingWorker(),
            clock=lambda: 1000,
            id_factory=self.ids,
            voice_client=self.voice_client,
            email_sender=self.email_sender,
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            url = "/v1/session/%s/voice" % created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            first = client.post(url, headers=headers, json={"sdp": "offer"})
            second = client.post(url, headers=headers, json={"sdp": "retry"})
        self.assertEqual(502, first.status_code, first.text)
        self.assertEqual(409, second.status_code, second.text)
        self.assertEqual(1, len(self.voice_client.calls))
        ledger, _ = self.store.load("studio_voice_open_19700101")
        self.assertEqual(1, ledger["count"])
        saved = StudioRepository(self.store).load(created["session_id"]).state
        self.assertEqual("unknown", saved["voice_call"]["status"])
        self.assertIn(
            created["session_id"], StudioRepository(self.store).due_voice_sessions(1600)
        )

    def test_malformed_provider_success_is_ambiguous_and_non_retryable(self):
        class MissingCallIdClient(FakeVoiceClient):
            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse(headers={"Location": ""})

        self.store = MemoryStore()
        self.ids = IDs()
        self.voice_client = MissingCallIdClient()
        self.email_sender = EmailSender()
        self.creation_seq = 0
        app = create_app(
            settings=settings(
                openai_api_key="server-key", voice_enabled=True,
                maintenance_secret="m" * 32, voice_mint_cap=3,
            ),
            store=self.store,
            worker=CountingWorker(),
            clock=lambda: 1000,
            id_factory=self.ids,
            voice_client=self.voice_client,
            email_sender=self.email_sender,
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            url = "/v1/session/%s/voice" % created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            first = client.post(url, headers=headers, json={"sdp": "offer"})
            second = client.post(url, headers=headers, json={"sdp": "retry"})
        self.assertEqual(502, first.status_code, first.text)
        self.assertEqual(409, second.status_code, second.text)
        self.assertEqual(1, len(self.voice_client.calls))
        saved = StudioRepository(self.store).load(created["session_id"]).state
        self.assertEqual("unknown", saved["voice_call"]["status"])
        self.assertIn(
            created["session_id"], StudioRepository(self.store).due_voice_sessions(1600)
        )

    def test_provider_transport_unknown_consumes_capacity_and_is_non_retryable(self):
        class TransportUnknownVoiceClient(FakeVoiceClient):
            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                raise httpx.ReadError(
                    "provider result was not received",
                    request=httpx.Request("POST", url),
                )

        self.store = MemoryStore()
        self.ids = IDs()
        self.voice_client = TransportUnknownVoiceClient()
        self.email_sender = EmailSender()
        self.creation_seq = 0
        app = create_app(
            settings=settings(
                openai_api_key="server-key", voice_enabled=True,
                maintenance_secret="m" * 32, voice_mint_cap=3,
            ),
            store=self.store,
            worker=CountingWorker(),
            clock=lambda: 1000,
            id_factory=self.ids,
            voice_client=self.voice_client,
            email_sender=self.email_sender,
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            url = "/v1/session/%s/voice" % created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            first = client.post(url, headers=headers, json={"sdp": "offer"})
            second = client.post(url, headers=headers, json={"sdp": "retry"})
        self.assertEqual(502, first.status_code, first.text)
        self.assertEqual(409, second.status_code, second.text)
        self.assertEqual(1, len(self.voice_client.calls))
        ledger, _ = self.store.load("studio_voice_open_19700101")
        self.assertEqual(1, ledger["count"])
        saved = StudioRepository(self.store).load(created["session_id"]).state
        self.assertEqual("unknown", saved["voice_call"]["status"])
        self.assertIn(
            created["session_id"], StudioRepository(self.store).due_voice_sessions(1600)
        )

    def test_voice_quota_store_failure_is_fail_closed_before_provider(self):
        app, _ = self.app(
            openai_api_key="server-key", voice_enabled=True,
            maintenance_secret="m" * 32, voice_mint_cap=3,
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            url = "/v1/session/%s/voice" % created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            with mock.patch.object(
                    StudioRepository, "reserve_voice_open",
                    side_effect=RuntimeError("quota unavailable")):
                response = client.post(url, headers=headers, json={"sdp": "offer"})
        self.assertEqual(503, response.status_code, response.text)
        self.assertEqual([], self.voice_client.calls)
        saved = StudioRepository(self.store).load(created["session_id"]).state
        self.assertNotIn("voice_call", saved)
        index, _ = self.store.load("studio_voice_index")
        self.assertNotIn(created["session_id"], index.get("sessions") or {})

    def test_voice_index_failure_releases_opening_before_provider(self):
        app, _ = self.app(
            openai_api_key="server-key", voice_enabled=True,
            maintenance_secret="m" * 32, voice_mint_cap=3,
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            url = "/v1/session/%s/voice" % created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            with mock.patch.object(
                    StudioRepository, "register_voice",
                    side_effect=StateConflict("index unavailable")):
                response = client.post(url, headers=headers, json={"sdp": "offer"})
        self.assertEqual(503, response.status_code, response.text)
        self.assertEqual([], self.voice_client.calls)
        saved = StudioRepository(self.store).load(created["session_id"]).state
        self.assertNotIn("voice_call", saved)
        self.assertFalse(any(
            name.startswith("studio_voice_open_") for name in self.store.data
        ))

    def test_new_open_cannot_delete_a_conflicting_legacy_index_entry(self):
        app, _ = self.app(
            openai_api_key="server-key", voice_enabled=True,
            maintenance_secret="m" * 32, voice_mint_cap=3,
        )
        with TestClient(app) as client:
            created = self.create_session(client)
            self.store.save(
                "studio_voice_index",
                {
                    "sessions": {created["session_id"]: 1000},
                    "updated_at": 900,
                },
                None,
            )
            url = "/v1/session/%s/voice" % created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            response = client.post(url, headers=headers, json={"sdp": "offer"})
        self.assertEqual(503, response.status_code, response.text)
        self.assertEqual([], self.voice_client.calls)
        saved = StudioRepository(self.store).load(created["session_id"]).state
        self.assertNotIn("voice_call", saved)
        index, _ = self.store.load("studio_voice_index")
        self.assertEqual(1000, index["sessions"][created["session_id"]])
        self.assertFalse(any(
            name.startswith("studio_voice_open_") for name in self.store.data
        ))

    def test_activation_failure_requires_confirmed_provider_hangup(self):
        class ActivationVoiceClient(FakeVoiceClient):
            def __init__(self, hangup_status):
                super().__init__()
                self.hangup_status = hangup_status

            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if url.endswith("/hangup"):
                    return FakeResponse(status_code=self.hangup_status)
                return FakeResponse()

        for hangup_status, expected_state, indexed in (
            (200, "ended", False),
            (302, "unknown", True),
            (503, "unknown", True),
        ):
            with self.subTest(hangup_status=hangup_status):
                store = MemoryStore()
                voice_client = ActivationVoiceClient(hangup_status)
                email_sender = EmailSender()
                app = create_app(
                    settings=settings(
                        openai_api_key="server-key", voice_enabled=True,
                        maintenance_secret="m" * 32, voice_mint_cap=3,
                    ),
                    store=store,
                    worker=CountingWorker(),
                    clock=lambda: 1000,
                    id_factory=IDs(),
                    voice_client=voice_client,
                    email_sender=email_sender,
                )
                self.store = store
                self.email_sender = email_sender
                self.creation_seq = 0
                with TestClient(app) as client:
                    created = self.create_session(client)
                    url = "/v1/session/%s/voice" % created["session_id"]
                    headers = {
                        **self.origin, "Authorization": "Bearer " + created["token"]
                    }
                    with mock.patch.object(
                            app.state.controller, "activate_voice",
                            side_effect=StateConflict("activation race")):
                        response = client.post(
                            url, headers=headers, json={"sdp": "offer"}
                        )
                self.assertEqual(502, response.status_code, response.text)
                self.assertEqual(2, len(voice_client.calls))
                saved = StudioRepository(store).load(created["session_id"]).state
                self.assertEqual(expected_state, saved["voice_call"]["status"])
                if expected_state == "ended":
                    self.assertEqual(
                        "activation failed after provider hangup",
                        saved["voice_call"]["end_reason"],
                    )
                due = StudioRepository(store).due_voice_sessions(1600)
                self.assertEqual(indexed, created["session_id"] in due)

    def test_activation_result_loss_after_commit_settles_the_exact_hung_up_call(self):
        app, _ = self.app(
            openai_api_key="server-key", voice_enabled=True,
            maintenance_secret="m" * 32, voice_mint_cap=3,
        )
        original_activate = app.state.controller.activate_voice

        def commit_then_lose_result(*args, **kwargs):
            original_activate(*args, **kwargs)
            raise RuntimeError("activation result was lost")

        with TestClient(app) as client:
            created = self.create_session(client)
            url = "/v1/session/%s/voice" % created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            with mock.patch.object(
                    app.state.controller, "activate_voice",
                    side_effect=commit_then_lose_result):
                response = client.post(url, headers=headers, json={"sdp": "offer"})
        self.assertEqual(502, response.status_code, response.text)
        self.assertEqual(2, len(self.voice_client.calls))
        saved = StudioRepository(self.store).load(created["session_id"]).state
        self.assertEqual("ended", saved["voice_call"]["status"])
        self.assertEqual("rtc_test_call", saved["voice_call"]["call_id"])
        self.assertNotIn(
            created["session_id"], StudioRepository(self.store).due_voice_sessions(1600)
        )

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
                "You ended this session.", stopped.json()["events"][0]["payload"]["reason"],
            )
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
            self.assertEqual({
                "ok": True,
                "checked_at": 1000,
                "due": 0,
                "attempted": 0,
                "completed": 0,
                "pending": 0,
                "index_available": True,
            }, accepted.json())

    def test_maintenance_sweep_fails_when_hangup_or_index_reconciliation_is_pending(self):
        class RefusingVoiceClient(FakeVoiceClient):
            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse(status_code=503)

        refusing = RefusingVoiceClient()
        store = MemoryStore()
        email_sender = EmailSender()
        app = create_app(
            settings=settings(
                openai_api_key="server-key", voice_enabled=True,
                maintenance_secret="m" * 32,
            ),
            store=store,
            worker=CountingWorker(),
            clock=lambda: 1000,
            id_factory=IDs(),
            voice_client=refusing,
            email_sender=email_sender,
        )
        state, _ = app.state.controller.create_session()
        session_id = state["session_id"]
        app.state.controller.begin_voice(session_id, "voice-pending", 1000)
        app.state.controller.activate_voice(session_id, "voice-pending", "rtc_pending")
        StudioRepository(store).register_voice(session_id, "voice-pending", 1000)

        endpoint = "/v1/maintenance/voice-sweep"
        bearer = {"Authorization": "Bearer " + "m" * 32}
        with TestClient(app) as client:
            refused = client.post(endpoint, headers=bearer)
            self.assertEqual(503, refused.status_code, refused.text)
            self.assertEqual({
                "ok": False,
                "checked_at": 1000,
                "due": 1,
                "attempted": 1,
                "completed": 0,
                "pending": 1,
                "index_available": True,
            }, refused.json())
            self.assertEqual(1, len(refusing.calls))
            saved = StudioRepository(store).load(session_id).state
            self.assertEqual("active", saved["voice_call"]["status"])
            self.assertIn(session_id, StudioRepository(store).due_voice_sessions(1000))

            with mock.patch.object(
                    StudioRepository, "due_voice_sessions", side_effect=RuntimeError("index unavailable")):
                unavailable = client.post(endpoint, headers=bearer)
            self.assertEqual(503, unavailable.status_code, unavailable.text)
            self.assertEqual(False, unavailable.json()["index_available"])
            self.assertEqual(0, unavailable.json()["completed"])

    def test_maintenance_sweep_does_not_treat_redirect_as_hangup(self):
        class RedirectingVoiceClient(FakeVoiceClient):
            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse(status_code=302)

        store = MemoryStore()
        voice_client = RedirectingVoiceClient()
        app = create_app(
            settings=settings(
                openai_api_key="server-key", voice_enabled=True,
                maintenance_secret="m" * 32,
            ),
            store=store,
            worker=CountingWorker(),
            clock=lambda: 1000,
            id_factory=IDs(),
            voice_client=voice_client,
            email_sender=EmailSender(),
        )
        state, _ = app.state.controller.create_session()
        session_id = state["session_id"]
        app.state.controller.begin_voice(session_id, "voice-redirect", 1000)
        app.state.controller.activate_voice(
            session_id, "voice-redirect", "rtc_redirect"
        )
        StudioRepository(store).register_voice(
            session_id, "voice-redirect", 1000
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/maintenance/voice-sweep",
                headers={"Authorization": "Bearer " + "m" * 32},
            )
        self.assertEqual(503, response.status_code, response.text)
        self.assertEqual(1, len(voice_client.calls))
        saved = StudioRepository(store).load(session_id).state
        self.assertEqual("active", saved["voice_call"]["status"])
        self.assertIn(session_id, StudioRepository(store).due_voice_sessions(1000))

    def test_maintenance_sweep_keeps_opening_and_unknown_reservations_pending(self):
        endpoint = "/v1/maintenance/voice-sweep"
        bearer = {"Authorization": "Bearer " + "m" * 32}

        for voice_status in ("opening", "unknown"):
            with self.subTest(voice_status=voice_status):
                store = MemoryStore()
                voice_client = FakeVoiceClient()
                app = create_app(
                    settings=settings(
                        openai_api_key="server-key", voice_enabled=True,
                        maintenance_secret="m" * 32,
                    ),
                    store=store,
                    worker=CountingWorker(),
                    clock=lambda: 1000,
                    id_factory=IDs(),
                    voice_client=voice_client,
                    email_sender=EmailSender(),
                )
                state, _ = app.state.controller.create_session()
                session_id = state["session_id"]
                app.state.controller.begin_voice(session_id, "voice-pending", 1000)
                if voice_status == "unknown":
                    app.state.controller.mark_voice_unknown(session_id, "voice-pending")
                StudioRepository(store).register_voice(
                    session_id, "voice-pending", 1000
                )

                with TestClient(app) as client:
                    response = client.post(endpoint, headers=bearer)
                    self.assertEqual(503, response.status_code, response.text)
                    self.assertEqual({
                        "ok": False,
                        "checked_at": 1000,
                        "due": 1,
                        "attempted": 1,
                        "completed": 0,
                        "pending": 1,
                        "index_available": True,
                    }, response.json())

                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), self.assertRaisesRegex(
                            RuntimeError, "HTTP 503"):
                        sweep_once.run_once(
                            url="https://controller.example.run.app" + endpoint,
                            secret="m" * 32,
                            post=lambda _url, **kwargs: client.post(
                                endpoint, headers=kwargs["headers"]
                            ),
                        )
                    self.assertEqual("", output.getvalue())

                saved = StudioRepository(store).load(session_id).state
                self.assertEqual(voice_status, saved["voice_call"]["status"])
                self.assertIn(
                    session_id, StudioRepository(store).due_voice_sessions(1000)
                )
                self.assertEqual([], voice_client.calls)



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


class _Refuser(SyntheticWorker):
    """The builder dropped the whole patch, as the live logo turns did."""
    def on_turn(self, state, trigger):
        return {"events": [], "problems": ["op 2 changes 'logo_scene'; the whole patch is dropped"],
                "resolves": {}}


class _AnswerNoChange(_Resolver):
    """A spoken answer the controller accepts, with no change and a dropped question."""
    def on_turn(self, state, trigger):
        return {"events": [], "problems": ["question dropped: its option ids repeat"],
                "resolves": {"question_id": "q-cta", "option_id": "book"}}


class ARefusedSpokenChangeIsSaid(unittest.TestCase):
    def test_only_a_new_blank_session_carries_the_analyst_flag(self):
        controller, _, _ = make_controller()
        template, _ = controller.create_session(start="template", analyst=True)
        blank, _ = controller.create_session(start="blank", analyst=True)
        plain, _ = controller.create_session(start="blank")
        self.assertEqual((None, True, None), (template.get("analyst"), blank.get("analyst"), plain.get("analyst")))

    def test_a_recorded_answer_is_not_contradicted(self):
        # Cursor NO-GO on e22eb9e: question.answered and "did not go through"
        # in the same turn. An answer was recorded, so nothing was refused.
        controller, _, _ = make_controller(worker=_AnswerNoChange(None))
        state, _ = controller.create_session()
        result = controller.execute(state["session_id"], _say(state, 1, "book a call", 1))
        self.assertEqual(["question.answered"], [e["type"] for e in result["events"]])

    def test_the_visitor_hears_that_it_did_not_go_through(self):
        from app.core import REFUSED_CHANGE_TEXT
        controller, store, _ = make_controller(worker=_Refuser())
        state, _ = controller.create_session()
        result = controller.execute(state["session_id"], _say(state, 1, "make the logo blue", 1))
        self.assertEqual(["confirm"], [e["type"] for e in result["events"]])
        confirm = result["events"][0]
        self.assertEqual(REFUSED_CHANGE_TEXT, confirm["payload"]["text"])
        self.assertEqual(1, confirm["artifact_version"])       # the page shows a confirm at its own version
        self.assertEqual(1, StudioRepository(store).load(state["session_id"]).state["artifact_version"])

    def test_a_tap_is_not_answered_with_it(self):
        controller, _, _ = make_controller(worker=_Refuser())
        state, _ = controller.create_session()
        result = controller.execute(state["session_id"], {
            "command_id": "tap-1", "session_id": state["session_id"], "type": "answer",
            "expected_version": 1, "question_id": "q-cta", "option_id": "book"})
        self.assertNotIn("confirm", [e["type"] for e in result["events"]])



class _SequenceResolver(_Resolver):
    """Starts with the two-question form BatchWorker opens, and names a
    different answer on each turn."""
    def __init__(self, *claims):
        self.claims = list(claims)

    def initial_questions(self):
        return BatchWorker().initial_questions()

    def on_turn(self, state, trigger):
        claim = self.claims.pop(0) if self.claims else {}
        return {"events": [], "problems": [], "resolves": claim}


def _batch_cmd(state, cid, batch_id, answers, version=1):
    return {"command_id": cid, "session_id": state["session_id"], "type": "answer_batch",
            "expected_version": version, "batch_id": batch_id, "answers": answers}


class CodexReview206ControllerTests(unittest.TestCase):
    """CODEX-PR206-REVIEW-20260924T0625Z."""

    def test_a_refused_change_does_not_spend_the_spoken_answer(self):
        bad = dict(_BOOK_BY_VOICE, ops=_BOOK_BY_VOICE["ops"] + [
            {"op": "set_label", "node_id": "no-such-node", "value": "x", "new_node": _NO_NODE}])
        controller, store, _ = make_controller(worker=_claude(bad, _BOOK_BY_VOICE))
        state, _ = controller.create_session()
        first = controller.execute(state["session_id"], _say(state, 1, "book a call", 1))
        self.assertEqual([], [e for e in first["events"] if e["type"] == "question.answered"])
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual((1, "open"), (saved["artifact_version"], _find(saved, "q-cta")["status"]))
        second = controller.execute(state["session_id"], _say(state, 2, "book a call, please", 1))
        self.assertEqual(["artifact.patch", "question.answered", "confirm"], [e["type"] for e in second["events"]])

    def test_a_voice_answer_to_one_form_question_leaves_the_rest_of_the_form(self):
        controller, store, _ = make_controller(worker=_SequenceResolver({"question_id": "q-cta", "option_id": "book"}))
        state, _ = controller.create_session()
        batch_id = next(e for e in state["events"] if e["type"] == "decision.batch")["payload"]["batch_id"]
        spoken = controller.execute(state["session_id"], _say(state, 1, "book a call", 1))
        self.assertEqual(["question.answered"], [e["type"] for e in spoken["events"]])
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual(("open", ["q-cta"]), (saved["batches"][batch_id]["status"], saved["batches"][batch_id]["answered_ids"]))
        with self.assertRaisesRegex(CommandError, "do not match"):
            controller.execute(state["session_id"], _batch_cmd(state, "b-all", batch_id, [
                {"question_id": "q-cta", "option_id": "describe"}, {"question_id": "q-tone", "option_id": "calm"}]))
        controller.execute(state["session_id"], _batch_cmd(state, "b-rest", batch_id,
                                                           [{"question_id": "q-tone", "option_id": "calm"}]))
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual("answered", saved["batches"][batch_id]["status"])
        self.assertEqual(("book", "voice"), (_find(saved, "q-cta")["selected_option"], _find(saved, "q-cta")["answer_source"]))
        self.assertEqual(("calm", "tap"), (_find(saved, "q-tone")["selected_option"], _find(saved, "q-tone")["answer_source"]))

    def test_voice_answers_to_every_form_question_close_the_form(self):
        controller, store, _ = make_controller(worker=_SequenceResolver(
            {"question_id": "q-cta", "option_id": "book"}, {"question_id": "q-tone", "option_id": "bold"}))
        state, _ = controller.create_session()
        batch_id = next(e for e in state["events"] if e["type"] == "decision.batch")["payload"]["batch_id"]
        controller.execute(state["session_id"], _say(state, 1, "book a call", 1))
        controller.execute(state["session_id"], _say(state, 2, "and make it bold", 1))
        saved = StudioRepository(store).load(state["session_id"]).state
        self.assertEqual("answered", saved["batches"][batch_id]["status"])
        with self.assertRaisesRegex(CommandError, "open decision batch"):
            controller.execute(state["session_id"], _batch_cmd(state, "b-late", batch_id,
                                                               [{"question_id": "q-tone", "option_id": "calm"}]))


if __name__ == "__main__":
    unittest.main()

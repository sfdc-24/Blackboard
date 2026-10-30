"""The board watcher decides WHEN a job runs. The ways that goes wrong: a ring
dropped (a row nobody wakes for), a ring doubled (two copies of a job posting
the same answer), and a cursor that moves backwards."""
import importlib.util
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import state_store  # noqa: E402

spec = importlib.util.spec_from_file_location("board_watcher_main", REPO / "cloud" / "board-watcher" / "main.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def row(rid, ts, source="claude-code-cli", target="gemini", payload="BCB|v=1|id=X|phase=DISPATCH"):
    return [rid, ts, source, target, "APPEND", payload]


class MemStore:
    def __init__(self, state):
        self.state, self.gen = state, (1 if state is not None else None)

    def load(self, name):
        return (json.loads(json.dumps(self.state)) if self.state is not None else {}), self.gen

    def save(self, name, state, token):
        if token != self.gen:
            raise state_store.Conflict("stale")
        self.state, self.gen = state, self.gen + 1
        return self.gen


NOW = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)


class Routing(unittest.TestCase):
    def setUp(self):
        self.routes = w._routes()

    def test_a_row_for_gemini_starts_gemini(self):
        wanted, _, _, _ = w.plan([row("R1", "2026-09-24T03:50:00Z")], set(), self.routes)
        self.assertEqual(list(wanted), ["gemini-waker"])

    def test_his_whatsapp_prefix_starts_gemini(self):
        r = row("R2", "2026-09-24T03:50:00Z", source="whatsapp", target="Blackboard Alpha DB",
                payload="Gemini - review the watcher")
        self.assertIn("gemini-waker", w.plan([r], set(), self.routes)[0])

    def test_a_waker_reply_to_his_whatsapp_starts_the_outbox_not_gemini(self):
        r = row("GEMINI-WAKE-R2", "2026-09-24T03:51:00Z", source="gemini", target="whatsapp;ALL",
                payload="BCB|v=1|id=GEMINI-WAKE-R2|phase=DONE|class=NOTE|from=gemini|to=whatsapp,ALL|wakerreply=1|answers=R2|REPLY: ok")
        self.assertEqual(list(w.plan([r], set(), self.routes)[0]), ["wa-outbox"])

    def test_gemini_answering_itself_is_not_a_ring(self):
        r = row("R3", "2026-09-24T03:50:00Z", source="gemini", target="gemini")
        self.assertEqual(w.plan([r], set(), self.routes)[0], {})

    def test_the_claude_standby_is_routed_and_foundry_is_gone(self):
        self.assertNotIn("foundry-waker", self.routes)
        self.assertEqual(w.plan([row("F1", "2026-09-24T03:50:00Z", target="foundry")], set(), self.routes)[0], {})
        his = row("W1", "2026-09-24T03:50:00Z", source="whatsapp", target="Blackboard Alpha DB",
                  payload="claude-code-cli can you check the soak")
        self.assertIn("claude-api-waker", w.plan([his], set(), self.routes)[0])

    def test_grok_is_never_woken(self):
        # Reserved for his exclusive use; out of allowance. A row to grok starts nothing.
        self.assertNotIn("grok-waker", self.routes)
        r = row("G1", "2026-09-24T03:50:00Z", target="grok")
        self.assertEqual(w.plan([r], set(), self.routes)[0], {})

    def test_grok_can_reach_everyone(self):
        # Mr Salam: grok "should be welcome and allowed everywhere ... create
        # tasks, approach and talk to everyone else". Not woken for routine
        # work, but what it writes is served like anyone's.
        for tag, job in (("gemini", "gemini-waker"), ("claude-api", "claude-api-waker")):
            for src in ("grok", "grok-bot"):
                r = row("G-%s-%s" % (src, tag), "2026-09-24T03:50:00Z", source=src, target=tag,
                        payload="BCB|v=1|id=GROK-TASK-1|phase=DISPATCH|from=%s|to=%s" % (src, tag))
                self.assertIn(job, w.plan([r], set(), self.routes)[0], (src, tag))
        send = row("G-WA", "2026-09-24T03:50:00Z", source="grok", target="wa-outbox",
                   payload="BCB|v=1|id=GROK-WA-1|phase=WA_SEND|from=grok|to=wa-outbox|Heads up from grok")
        self.assertIn("wa-outbox", w.plan([send], set(), self.routes)[0])

    def test_a_row_already_seen_is_not_routed_twice(self):
        self.assertEqual(w.plan([row("R1", "2026-09-24T03:50:00Z")], {"R1"}, self.routes)[0], {})


class Tick(unittest.TestCase):
    def go(self, store, rows, running=(), fail=()):
        started = []

        def start(job):
            if job in fail:
                raise RuntimeError("403")
            started.append(job)
            return "exec-1"
        out = w.tick(store, read_since=lambda since: rows, routes=w._routes(),
                     running=lambda job: job in running, start=start, now=NOW)
        return out, started

    def test_refuses_unseeded(self):
        out, started = self.go(MemStore(None), [row("R1", "2026-09-24T03:50:00Z")])
        self.assertFalse(out["ran"])
        self.assertEqual(started, [])

    def test_one_start_per_job_however_many_rows(self):
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": []})
        rows = [row("R%d" % i, "2026-09-24T03:5%d:00Z" % i) for i in range(4)]
        out, started = self.go(store, rows)
        self.assertEqual(started, ["gemini-waker"])
        self.assertEqual(store.state["watermark"], "2026-09-24T03:53:00Z")

    def test_nothing_new_starts_nothing(self):
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": ["R1"]})
        out, started = self.go(store, [row("R1", "2026-09-24T03:35:00Z")])
        self.assertEqual(started, [])
        self.assertEqual(store.state["watermark"], "2026-09-24T03:40:00Z")

    def test_a_running_job_is_not_started_again_and_the_row_is_kept(self):
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": []})
        rows = [row("R1", "2026-09-24T03:50:00Z")]
        out, started = self.go(store, rows, running={"gemini-waker"})
        self.assertEqual(started, [])
        self.assertNotIn("R1", store.state["seen"])
        self.assertLess(store.state["watermark"], "2026-09-24T03:50:00Z")
        # next tick, the job has finished: the held row rings now
        out, started = self.go(store, rows)
        self.assertEqual(started, ["gemini-waker"])

    def test_a_failed_start_holds_the_row(self):
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": []})
        rows = [row("R1", "2026-09-24T03:50:00Z")]
        out, _ = self.go(store, rows, fail={"gemini-waker"})
        self.assertIn("gemini-waker", out["failed"])
        self.assertNotIn("R1", store.state["seen"])
        _, started = self.go(store, rows)
        self.assertEqual(started, ["gemini-waker"])

    def test_a_route_that_raises_holds_the_row_and_says_why(self):
        # Found live: a probe to foundry and claude-api was seen and started
        # nothing, because an exception in a route read as "not for me".
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": []})

        def boom(row):
            raise KeyError("x")
        out = w.tick(store, read_since=lambda s: [row("R1", "2026-09-24T03:50:00Z")],
                     routes={"gemini-waker": boom}, running=lambda j: False,
                     start=lambda j: "", now=NOW)
        self.assertIn("R1", out["route_errors"])
        self.assertNotIn("R1", store.state["seen"])
        self.assertLess(store.state["watermark"], "2026-09-24T03:50:00Z")

    def test_the_watermark_never_moves_backwards(self):
        store = MemStore({"watermark": "2026-09-24T03:55:00Z", "seen": []})
        self.go(store, [row("R9", "2026-09-24T03:50:00Z")], running={"gemini-waker"})
        self.assertEqual(store.state["watermark"], "2026-09-24T03:55:00Z")

    def test_a_second_tick_inside_the_lease_does_nothing(self):
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": [],
                          "lease_until": "2026-09-24T04:01:00Z"})
        out, started = self.go(store, [row("R1", "2026-09-24T03:50:00Z")])
        self.assertFalse(out["ran"])
        self.assertEqual(started, [])

    def test_the_lease_is_released_and_an_expired_one_is_taken(self):
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": [],
                          "lease_until": "2026-09-24T03:59:00Z"})
        out, started = self.go(store, [row("R1", "2026-09-24T03:50:00Z")])
        self.assertTrue(out["ran"])
        self.assertEqual(started, ["gemini-waker"])
        self.assertNotIn("lease_until", store.state)

    def test_two_racing_ticks_only_one_starts_anything(self):
        # Both load the same state; the lease write is compare-and-swap, so the
        # second tick's lease save is refused before it can start a job.
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": []})
        stale = store.load("x")
        self.go(store, [row("R1", "2026-09-24T03:50:00Z")])
        store.load = lambda name: stale
        out, started = self.go(store, [row("R1", "2026-09-24T03:50:00Z")])
        self.assertFalse(out["ran"])
        self.assertEqual(started, [])

    def test_the_read_overlaps_the_watermark(self):
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": []})
        asked = []
        w.tick(store, read_since=lambda s: asked.append(s) or [], routes={},
               running=lambda j: False, start=lambda j: "", now=NOW)
        self.assertEqual(asked, ["2026-09-24T03:30:00Z"])

    def test_a_start_is_refused_once_the_lease_has_moved(self):
        # A slow read can outlive the lease. The tick that then owns it is
        # the one that may start a job; this one must not.
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": []})
        started = []

        def read_since(since):
            current, token = store.load("board_watcher")
            current["lease_owner"] = "rival-tick"
            current["lease_until"] = "2026-09-24T04:05:00Z"
            store.save("board_watcher", current, token)
            return [row("R1", "2026-09-24T03:50:00Z")]

        out = w.tick(store, read_since=read_since, routes=w._routes(),
                     running=lambda job: False,
                     start=lambda job: started.append(job) or "exec",
                     now=NOW)
        self.assertEqual(started, [])
        self.assertEqual(out.get("reason"), "lease moved before start")
        self.assertEqual(store.state.get("lease_owner"), "rival-tick")

    def test_a_status_error_stays_on_that_route_and_the_next_still_starts(self):
        # The first route's status lookup throws. That route stays pending.
        # The healthy route still starts, and the cursor is saved.
        import urllib.error
        store = MemStore({"watermark": "2026-09-24T03:40:00Z", "seen": []})
        rows = [row("R1", "2026-09-24T03:50:00Z", target="gemini"),
                row("R2", "2026-09-24T03:51:00Z", target="claude-api")]
        started = []

        def running(job):
            if job == "gemini-waker":
                raise urllib.error.URLError("timed out")
            return False

        out = w.tick(store, read_since=lambda since: rows, routes=w._routes(),
                     running=running,
                     start=lambda job: started.append(job) or "exec-1",
                     now=NOW)
        self.assertTrue(out["ran"])
        self.assertEqual(started, ["claude-api-waker"])
        self.assertNotIn("gemini-waker", out["started"])
        self.assertIn("gemini-waker", out["failed"])
        self.assertIn("URLError", out["failed"]["gemini-waker"])
        self.assertIn("watermark", store.state)
        self.assertGreaterEqual(store.state["watermark"], "2026-09-24T03:40:00Z")
        pending = store.state.get("pending") or {}
        self.assertIn("R1", [item["row_id"] for item in pending.get("gemini-waker") or []])
        self.assertNotIn("R1", store.state.get("seen") or [])


class NamedStore:
    """One generation per cursor name, which is how the real store is keyed."""

    def __init__(self, cursors):
        self.slots = {name: [json.loads(json.dumps(state)), 1]
                      for name, state in cursors.items()}

    def load(self, name):
        slot = self.slots.get(name)
        if slot is None:
            return {}, None
        return json.loads(json.dumps(slot[0])), slot[1]

    def save(self, name, state, token):
        slot = self.slots.get(name)
        current = None if slot is None else slot[1]
        if token != current:
            raise state_store.Conflict("stale %s" % name)
        gen = (current or 0) + 1
        self.slots[name] = [json.loads(json.dumps(state)), gen]
        return gen


class PendingUntilTheJobFinishes(unittest.TestCase):
    def test_four_asks_are_woken_again_and_the_fourth_is_answered_once(self):
        import agent_waker as aw
        saved_env = {key: os.environ.get(key) for key in ("AGENT", "CURSOR_NAME")}
        os.environ.pop("AGENT", None)
        os.environ.pop("CURSOR_NAME", None)
        spec = importlib.util.spec_from_file_location(
            "agent_waker_cloud_for_watcher", REPO / "cloud" / "agent-waker" / "main.py")
        cloud = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cloud)
        asks = [row("ROW-%d" % n, "2026-09-24T03:50:%02dZ" % n,
                    payload="BCB|v=1|id=ASK-%d|phase=DISPATCH|to=gemini|ask=number %d" % (n, n))
                for n in range(1, 5)]
        store = NamedStore({
            "board_watcher": {"watermark": "2026-09-24T03:40:00Z", "seen": []},
            cloud.CURSOR: {"watermark": "2026-09-24T03:40:00Z", "answered_ids": []},
        })
        posts, starts, phases = [], [], []

        class Adapter(object):
            @staticmethod
            def ask(prompt, max_tokens=None):
                return ("an answer", "fake-route")

        saved_module = aw.AGENTS["gemini"]["module"]
        saved = (aw.read_since, aw.load_env, aw.log, aw.post_reply)
        aw.sys.modules["fake_watcher_drain_adapter"] = Adapter
        aw.AGENTS["gemini"]["module"] = "fake_watcher_drain_adapter"

        def read_since(env, since, tries=3):
            kept = [r for r in asks if str(r[1]) > since]
            return {"rows": kept, "total": len(asks), "filtered": len(kept)}

        def post_reply(me, cfg, text, to, answers, verbose, phase="DONE"):
            posts.append(answers)
            phases.append(phase)
            return True

        aw.read_since = read_since
        aw.load_env = lambda: {}
        aw.log = lambda me, line: None
        aw.post_reply = post_reply

        def start(job):
            starts.append(job)
            cloud.run(store=store, waker=aw, board_contains=lambda rid: False,
                      argv=["--agent", "gemini", "--max", "3"])
            return "exec-%d" % len(starts)

        # The second and third ticks read nothing. The fourth ask is already
        # past the watermark; only the pending list can wake it.
        watcher_reads = {"n": 0}

        def watcher_read(since):
            watcher_reads["n"] += 1
            return asks if watcher_reads["n"] == 1 else []

        try:
            for _ in range(3):
                w.tick(store, read_since=watcher_read, routes=w._routes(),
                       running=lambda job: False, start=start, now=NOW)
        finally:
            aw.AGENTS["gemini"]["module"] = saved_module
            aw.read_since, aw.load_env, aw.log, aw.post_reply = saved
            aw.sys.modules.pop("fake_watcher_drain_adapter", None)
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(cloud.CURSOR, w.ROUTE_CURSORS["gemini-waker"])
        self.assertEqual(starts, ["gemini-waker", "gemini-waker"])
        self.assertEqual(posts, ["ASK-1", "ASK-2", "ASK-3", "ASK-4"])
        # Each ask is phase=DISPATCH, so the cloud wrapper posts each answer as a RESULT (#297).
        self.assertEqual(phases, ["RESULT"] * 4)
        answered = store.slots[cloud.CURSOR][0].get("answered_ids") or []
        self.assertEqual(answered, ["ASK-1", "ASK-2", "ASK-3", "ASK-4"])


class ExecutionRunning(unittest.TestCase):
    """A dead execution must never read as a running one: that skips the route forever."""

    def test_failed_before_start_is_not_running(self):
        # The exact latestCreatedExecution the Run v2 API returned for gemini-waker on 2026-10-30.
        latest = {"name": "gemini-waker-rcz5p", "createTime": "2026-10-27T16:40:05.138051Z",
                  "completionStatus": "EXECUTION_FAILED"}
        self.assertFalse(w.execution_running(latest))

    def test_finished_with_a_completion_time_is_not_running(self):
        self.assertFalse(w.execution_running({"name": "x", "completionTime": "2026-10-27T16:36:46Z",
                                              "completionStatus": "EXECUTION_SUCCEEDED"}))

    def test_cancelled_is_not_running(self):
        self.assertFalse(w.execution_running({"name": "x", "completionStatus": "EXECUTION_CANCELLED"}))

    def test_started_and_unfinished_is_running(self):
        self.assertTrue(w.execution_running({"name": "x", "createTime": "2026-10-30T14:00:00Z"}))

    def test_empty_execution_is_unknown_and_blocks(self):
        self.assertTrue(w.execution_running({}))


class ExecutionStateSafety(unittest.TestCase):
    BLOCKED = ("EXECUTION_RUNNING", "EXECUTION_PENDING", "COMPLETION_STATUS_UNSPECIFIED",
               "EXECUTION_TIMED_OUT", "FUTURE_UNKNOWN", "", None, 0, [], {})

    def test_only_documented_terminal_statuses_allow_start(self):
        for status in ("EXECUTION_SUCCEEDED", "EXECUTION_FAILED", "EXECUTION_CANCELLED"):
            with self.subTest(status=status):
                self.assertFalse(w.execution_running({"name": "x", "completionStatus": status}))

    def test_active_unknown_and_malformed_statuses_block_even_with_time(self):
        for status in self.BLOCKED:
            for extra in ({}, {"completionTime": "2026-10-27T16:36:46Z"}):
                with self.subTest(status=status, extra=extra):
                    self.assertTrue(w.execution_running(dict(name="x", completionStatus=status, **extra)))

    def test_legacy_completion_time_requires_valid_zoned_timestamp(self):
        self.assertFalse(w.execution_running({"name": "x", "completionTime": "2026-10-27T16:36:46Z"}))
        for value in (None, "", "garbage", True, {}, "2026-10-27", "2026-10-27T16:36:46"):
            with self.subTest(value=value):
                self.assertTrue(w.execution_running({"name": "x", "completionTime": value}))

    def test_malformed_execution_reference_blocks(self):
        for latest in (None, {}, [], "", False, 0, {"completionStatus": "EXECUTION_FAILED"},
                       {"name": "", "completionStatus": "EXECUTION_FAILED"}):
            with self.subTest(latest=latest):
                self.assertTrue(w.execution_running(latest))

    def job_response(self, **fields):
        name = (w.RUN_API % (w.PROJECT, w.REGION, "gemini-waker")).split("/v2/", 1)[1]
        # Schema-backed GetJob fixture, not a live response. Job uid/createTime
        # are server outputs; template is required. ProtoJSON can omit count=0.
        response = dict(name=name, uid="12345678-1234-4234-8234-123456789abc",
                        createTime="2026-09-24T03:00:00Z", generation="1",
                        template={"template": {"containers": [{"image": "example.invalid/image:fixture"}]}})
        response.update(fields)
        return response

    def test_known_no_prior_execution_is_distinct_from_missing_state(self):
        from unittest.mock import patch
        with patch.object(w, "_call", return_value=self.job_response(executionCount=0)):
            self.assertFalse(w.job_running("gemini-waker", "fake"))
        with patch.object(w, "_call", return_value=self.job_response()):
            self.assertFalse(w.job_running("gemini-waker", "fake"))
        responses = [None, {}, [], "", {"name": "wrong", "executionCount": 0},
                     {"name": self.job_response()["name"]}]
        responses += [self.job_response(executionCount=n) for n in (None, "0", False, -1, 1)]
        responses += [self.job_response(executionCount=0, latestCreatedExecution=x)
                      for x in (None, {}, [], "", False)]
        for response in responses:
            with self.subTest(response=response), patch.object(w, "_call", return_value=response):
                self.assertTrue(w.job_running("gemini-waker", "fake"))

    def run_tick_with_response(self, response):
        from unittest.mock import patch
        store = NamedStore({"board_watcher": {"watermark": "2026-09-24T03:40:00Z", "seen": []}})
        started = []
        with patch.object(w, "_call", return_value=response):
            out = w.tick(store, read_since=lambda since: [row("R1", "2026-09-24T03:50:00Z")],
                         routes=w._routes(), running=lambda job: w.job_running(job, "fake"),
                         start=lambda job: started.append(job) or "fake-exec", now=NOW)
        return out, started, store.slots["board_watcher"][0]

    def test_integrated_active_unknown_states_never_duplicate_start(self):
        for status in self.BLOCKED:
            for extra in ({}, {"completionTime": "2026-10-27T16:36:46Z"}):
                with self.subTest(status=status, extra=extra):
                    response = self.job_response(latestCreatedExecution=dict(name="x", completionStatus=status, **extra))
                    out, started, state = self.run_tick_with_response(response)
                    self.assertEqual(started, [])
                    self.assertIn("gemini-waker", out["skipped_running"])
                    self.assertEqual(state["pending"]["gemini-waker"], [{"row_id": "R1", "work_id": "X"}])
                    self.assertNotIn("R1", state["seen"])
                    self.assertLess(state["watermark"], "2026-09-24T03:50:00Z")

    def test_integrated_malformed_response_blocks_and_preserves_pending(self):
        for response in (None, {}, {"name": self.job_response()["name"]}, self.job_response(latestCreatedExecution=None)):
            with self.subTest(response=response):
                out, started, state = self.run_tick_with_response(response)
                self.assertEqual(started, [])
                self.assertIn("gemini-waker", state["pending"])
                self.assertNotIn("R1", state["seen"])

    def test_integrated_terminal_and_known_no_prior_execution_can_start(self):
        responses = [self.job_response(executionCount=0), self.job_response()]
        responses += [self.job_response(latestCreatedExecution={"name": "x", "completionStatus": status})
                      for status in ("EXECUTION_FAILED", "EXECUTION_SUCCEEDED", "EXECUTION_CANCELLED")]
        for response in responses:
            with self.subTest(response=response):
                out, started, state = self.run_tick_with_response(response)
                self.assertEqual(started, ["gemini-waker"])
                self.assertIn("gemini-waker", state["pending"])

    def test_first_use_partial_and_malformed_jobs_block_for_both_zero_encodings(self):
        from unittest.mock import patch
        for count in ({}, {"executionCount": 0}):
            valid = self.job_response(**count)
            responses = []
            for field in ("name", "uid", "createTime", "template"):
                partial = dict(valid)
                del partial[field]
                responses.append(partial)
            for field, values in {
                "uid": (None, "", [], "not-a-uuid"),
                "createTime": (None, "", [], "not-a-time", "2026-09-24"),
                "template": (None, {}, [], {"template": {}}, {"template": {"containers": []}},
                             {"template": {"containers": [None]}},
                             {"template": {"containers": [{"image": ""}]}}),
            }.items():
                responses.extend(dict(valid, **{field: value}) for value in values)
            for response in responses:
                with self.subTest(count=count, response=response), patch.object(w, "_call", return_value=response):
                    self.assertTrue(w.job_running("gemini-waker", "fake"))
                    out, started, state = self.run_tick_with_response(response)
                    self.assertEqual(started, [])
                    self.assertIn("gemini-waker", state["pending"])

    def test_first_use_contradictory_counts_and_references_block(self):
        from unittest.mock import patch
        for count in (None, "0", False, -1, 1, 2):
            response = self.job_response(executionCount=count)
            with self.subTest(count=count), patch.object(w, "_call", return_value=response):
                self.assertTrue(w.job_running("gemini-waker", "fake"))
        for count in ({}, {"executionCount": 0}):
            for latest in (None, {}, [], "", {"name": "x", "completionStatus": "EXECUTION_RUNNING"},
                           {"name": "x", "completionStatus": "FUTURE_UNKNOWN"}):
                response = self.job_response(latestCreatedExecution=latest, **count)
                with self.subTest(count=count, latest=latest), patch.object(w, "_call", return_value=response):
                    self.assertTrue(w.job_running("gemini-waker", "fake"))

    def test_get_job_is_full_authenticated_get_and_failure_is_not_zero(self):
        from unittest.mock import patch, MagicMock
        import urllib.error
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(self.job_response()).encode()
        with patch.object(w.urllib.request, "urlopen", return_value=response) as opened:
            self.assertFalse(w.job_running("gemini-waker", "inert-test-token"))
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, w.RUN_API % (w.PROJECT, w.REGION, "gemini-waker"))
        self.assertEqual(request.get_header("Authorization"), "Bearer inert-test-token")
        self.assertIsNone(request.data)
        with patch.object(w, "_call", side_effect=urllib.error.URLError("offline failure")):
            with self.assertRaises(urllib.error.URLError):
                w.job_running("gemini-waker", "fake")


class ProjectAliasSafety(ExecutionStateSafety):
    # Inherit the entire state suite so the alias response exercises every guard.
    def job_response(self, **fields):
        response = super().job_response(**fields)
        if "name" not in fields:
            response["name"] = "projects/123456789012/locations/%s/jobs/gemini-waker" % w.REGION
        return response

    def test_id_request_number_response_terminal_and_first_use(self):
        from unittest.mock import patch
        with patch.object(w, "PROJECT", "example-project"):
            for fields in ({}, {"executionCount": 0},
                           {"latestCreatedExecution": {"name": "x", "completionStatus": "EXECUTION_FAILED"}}):
                response = self.job_response(**fields)
                out, started, state = self.run_tick_with_response(response)
                self.assertEqual(started, ["gemini-waker"])
                self.assertIn("gemini-waker", state["pending"])

    def test_malformed_or_wrong_location_job_names_block(self):
        prefix = "projects/123456789012/locations/%s/jobs/" % w.REGION
        for name in (None, "", [], "projects", prefix + "different-job", prefix + "gemini-waker/extra",
                     "projects//locations/%s/jobs/gemini-waker" % w.REGION,
                     "projects/123 456/locations/%s/jobs/gemini-waker" % w.REGION,
                     "projects/123?fields=x/locations/%s/jobs/gemini-waker" % w.REGION,
                     "projects/123456789012/locations/wrong-region/jobs/gemini-waker"):
            for fields in ({}, {"latestCreatedExecution": {"name": "x", "completionStatus": "EXECUTION_FAILED"}}):
                with self.subTest(name=name, fields=fields):
                    out, started, state = self.run_tick_with_response(self.job_response(name=name, **fields))
                    self.assertEqual(started, [])
                    self.assertIn("gemini-waker", state["pending"])

    def test_alias_http_and_malformed_body_failures_do_not_start(self):
        from unittest.mock import patch, MagicMock
        import urllib.error
        for body in (b"", b"{}", b"null", b"[]", b"not-json", b"\xff"):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = body
            store = NamedStore({"board_watcher": {"watermark": "2026-09-24T03:40:00Z", "seen": []}})
            started = []
            with self.subTest(body=body), patch.object(w.urllib.request, "urlopen", return_value=response):
                out = w.tick(store, read_since=lambda since: [row("R1", "2026-09-24T03:50:00Z")],
                             routes=w._routes(), running=lambda job: w.job_running(job, "fake"),
                             start=lambda job: started.append(job), now=NOW)
            self.assertEqual(started, [])
            self.assertIn("gemini-waker", store.slots["board_watcher"][0]["pending"])
        for error in (urllib.error.HTTPError("https://run.googleapis.com", 403, "fixture", {}, None),
                      urllib.error.URLError("fixture timeout")):
            with self.subTest(error=error), patch.object(w.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(urllib.error.URLError):
                    w.job_running("gemini-waker", "fake")


if __name__ == "__main__":
    unittest.main()

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
        posts, starts = [], []

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

        def post_reply(me, cfg, text, to, answers, verbose):
            posts.append(answers)
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
        answered = store.slots[cloud.CURSOR][0].get("answered_ids") or []
        self.assertEqual(answered, ["ASK-1", "ASK-2", "ASK-3", "ASK-4"])


if __name__ == "__main__":
    unittest.main()

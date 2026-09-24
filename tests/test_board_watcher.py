"""The board watcher decides WHEN a job runs. The ways that goes wrong: a ring
dropped (a row nobody wakes for), a ring doubled (two copies of a job posting
the same answer), and a cursor that moves backwards."""
import importlib.util
import json
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
        wanted, _, _ = w.plan([row("R1", "2026-09-24T03:50:00Z")], set(), self.routes)
        self.assertEqual(list(wanted), ["gemini-waker"])

    def test_his_whatsapp_prefix_starts_gemini(self):
        r = row("R2", "2026-09-24T03:50:00Z", source="whatsapp", target="Blackboard Alpha DB",
                payload="Gemini - review the watcher")
        self.assertIn("gemini-waker", w.plan([r], set(), self.routes)[0])

    def test_a_waker_reply_to_his_whatsapp_starts_the_outbox_not_gemini(self):
        r = row("GEMINI-WAKE-R2", "2026-09-24T03:51:00Z", source="gemini", target="whatsapp;ALL",
                payload="BCB|v=1|id=GEMINI-WAKE-R2|from=gemini|to=whatsapp,ALL|wakerreply=1|answers=R2|REPLY: ok")
        self.assertEqual(list(w.plan([r], set(), self.routes)[0]), ["wa-outbox"])

    def test_gemini_answering_itself_is_not_a_ring(self):
        r = row("R3", "2026-09-24T03:50:00Z", source="gemini", target="gemini")
        self.assertEqual(w.plan([r], set(), self.routes)[0], {})

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


if __name__ == "__main__":
    unittest.main()

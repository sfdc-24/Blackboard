"""The cloud gemini waker: where its cursor lives decides whether a reply is sent twice."""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import state_store  # noqa: E402

spec = importlib.util.spec_from_file_location("gemini_cloud_main", REPO / "cloud" / "agent-waker" / "main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)


class MemStore:
    def __init__(self, state=None):
        self.state, self.gen, self.saves = state, (1 if state is not None else None), []

    def load(self, name):
        return (json.loads(json.dumps(self.state)) if self.state is not None else {}), self.gen

    def save(self, name, state, token):
        if token != self.gen:
            raise state_store.Conflict("stale")
        self.state = json.loads(json.dumps(state))
        self.gen = (self.gen or 0) + 1
        self.saves.append(self.state)
        return self.gen


class FakeWaker:
    """Stands in for agent_waker: posts the given ids, optionally dies midway."""

    def __init__(self, post_ids, die_after=None):
        self.post_ids, self.die_after, self.posted = post_ids, die_after, []

    def post_reply(self, me, cfg, text, to, answers, verbose):
        self.posted.append(answers)
        return True

    def main(self, argv):
        path = os.path.join(os.environ["BLACKBOARD_STATE_DIR"], ".gemini_waker_state.json")
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        for n, rid in enumerate(self.post_ids):
            if rid in state["answered_ids"]:
                continue
            if self.die_after is not None and n >= self.die_after:
                raise RuntimeError("container killed")
            if self.post_reply("gemini", {}, "x", "a;ALL", rid, False):
                state["answered_ids"].append(rid)
        state["watermark"] = "2026-09-24T03:00:00Z"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        return 0


class GeminiCloudWakerTest(unittest.TestCase):
    def test_refuses_without_a_durable_uri(self):
        os.environ.pop("BLACKBOARD_STATE_URI", None)
        self.assertEqual(main.run(), 2)

    def test_refuses_without_a_seeded_cursor(self):
        w = FakeWaker(["A"])
        self.assertEqual(main.run(store=MemStore(None), waker=w), 2)
        self.assertEqual(w.posted, [])

    def test_answers_and_records(self):
        store = MemStore({"watermark": "2026-09-24T02:00:00Z", "answered_ids": ["OLD"]})
        self.assertEqual(main.run(store=store, waker=FakeWaker(["A", "B"])), 0)
        self.assertEqual(store.state["answered_ids"], ["OLD", "A", "B"])
        self.assertEqual(store.state["watermark"], "2026-09-24T03:00:00Z")

    def test_already_answered_is_not_posted_again(self):
        store = MemStore({"watermark": "", "answered_ids": ["A"]})
        w = FakeWaker(["A", "B"])
        main.run(store=store, waker=w)
        self.assertEqual(w.posted, ["B"])

    def test_a_run_killed_midway_keeps_what_it_posted(self):
        # The damaging path: reply A landed, then the container died. If A is not
        # already in durable state, the next run posts A a second time.
        store = MemStore({"watermark": "", "answered_ids": []})
        with self.assertRaises(RuntimeError):
            main.run(store=store, waker=FakeWaker(["A", "B"], die_after=1))
        self.assertEqual(store.state["answered_ids"], ["A"])
        w2 = FakeWaker(["A", "B"])
        main.run(store=store, waker=w2)
        self.assertEqual(w2.posted, ["B"])

    def test_a_stale_writer_is_refused_not_merged(self):
        store = MemStore({"watermark": "", "answered_ids": []})
        w = FakeWaker(["A"])
        real_post = w.post_reply

        def racing_post(*a):
            store.gen += 1  # someone else wrote in between
            return real_post(*a)
        w.post_reply = racing_post
        with self.assertRaises(state_store.Conflict):
            main.run(store=store, waker=w)


if __name__ == "__main__":
    unittest.main()

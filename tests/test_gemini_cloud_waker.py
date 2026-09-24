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
        claim = getattr(self, "claim_answer", None)
        for n, rid in enumerate(self.post_ids):
            if rid in state["answered_ids"]:
                continue
            if self.die_after is not None and n >= self.die_after:
                raise RuntimeError("container killed")
            if claim is not None and not claim(rid):
                continue
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


class _ClaimingWaker:
    """Calls claim_answer before the model, which is the cloud hook.

    The model call is `calls.append`. post_reply is the confirmed board append.
    """

    def __init__(self, calls, spawn=None, on_post=None):
        self.calls, self.spawn, self.on_post = calls, spawn, on_post

    def post_reply(self, me, cfg, text, to, answers, verbose):
        if self.on_post:
            self.on_post(me, answers)
        return True

    def main(self, argv):
        path = os.path.join(os.environ["BLACKBOARD_STATE_DIR"], ".gemini_waker_state.json")
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        if self.spawn is not None and not self.spawn.get("done"):
            self.spawn["done"] = True
            self.spawn["go"]()
        claim = getattr(self, "claim_answer", lambda _rid: True)
        rid = "A"
        if rid not in (state.get("answered_ids") or []):
            if claim(rid):
                self.calls.append(rid)
                if self.post_reply("gemini", {}, "x", "whatsapp;ALL", rid, False):
                    state.setdefault("answered_ids", []).append(rid)
        state["watermark"] = "2026-09-24T03:00:00Z"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        return 0


class NoDuplicateAnswers(unittest.TestCase):
    def test_kill_after_confirmed_post_before_cursor_save_does_not_post_again(self):
        """Append succeeded, cursor save did not. The retry must not answer again."""
        store = MemStore({"watermark": "2026-09-24T02:00:00Z", "answered_ids": []})
        board = set()
        calls = []
        real_save = store.save

        def save(name, state, token):
            if "A" in (state.get("answered_ids") or []):
                raise RuntimeError("killed after confirmed post, before cursor save")
            return real_save(name, state, token)

        store.save = save
        w = _ClaimingWaker(calls, on_post=lambda me, answers: board.add(
            "%s-WAKE-%s" % (me.upper(), answers)))
        w.reply_on_board = lambda rid: rid in board
        with self.assertRaises(RuntimeError):
            main.run(store=store, waker=w)
        self.assertEqual(calls, ["A"])
        self.assertNotIn("A", store.state.get("answered_ids") or [])

        store.save = real_save
        again = []
        w2 = _ClaimingWaker(again)
        w2.reply_on_board = lambda rid: rid in board
        main.run(store=store, waker=w2)
        self.assertEqual(again, [], "the confirmed reply was posted a second time")
        self.assertIn("A", store.state.get("answered_ids") or [])

    def test_two_overlapping_runs_only_the_claim_winner_calls_the_model(self):
        store = MemStore({"watermark": "2026-09-24T02:00:00Z", "answered_ids": []})
        calls = []
        spawn = {}

        def go():
            try:
                main.run(store=store, waker=_ClaimingWaker(calls))
            except state_store.Conflict:
                pass

        spawn["go"] = go
        try:
            main.run(store=store, waker=_ClaimingWaker(calls, spawn=spawn))
        except state_store.Conflict:
            pass
        self.assertEqual(calls, ["A"],
                         "both overlapping runs called the model: %r" % (calls,))


if __name__ == "__main__":
    unittest.main()

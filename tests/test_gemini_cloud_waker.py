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

    def __init__(self, calls, spawn=None, on_post=None, posts=None):
        self.calls, self.spawn, self.on_post, self.posts = calls, spawn, on_post, posts

    def post_reply(self, me, cfg, text, to, answers, verbose):
        # The append is on the wire here. A second run that arrives now sees
        # no reply yet; phase=posting must already have been saved.
        if (self.spawn is not None and not self.spawn.get("done")
                and self.spawn.get("when") == "during-post"):
            self.spawn["done"] = True
            self.spawn["go"]()
        if self.posts is not None:
            self.posts.append(answers)
        if self.on_post:
            self.on_post(me, answers)
        return True

    def main(self, argv):
        path = os.path.join(os.environ["BLACKBOARD_STATE_DIR"], ".gemini_waker_state.json")
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        # Default spawn is before the claim: both runs loaded, neither owns it yet.
        # spawn["when"] == "after-claim" is the other hole: B arrives after A
        # has saved the claim and before A's model call has returned.
        when = (self.spawn or {}).get("when")
        # during-post fires inside the append, after phase=posting is saved.
        early = self.spawn is not None and not self.spawn.get("done") and when not in ("after-claim", "during-post")
        if early:
            self.spawn["done"] = True
            self.spawn["go"]()
        claim = getattr(self, "claim_answer", lambda _rid: True)
        rid = "A"
        if rid not in (state.get("answered_ids") or []) and rid not in (state.get("unknown_ids") or []):
            if claim(rid):
                self.calls.append(rid)
                if (self.spawn is not None and not self.spawn.get("done")
                        and self.spawn.get("when") == "after-claim"):
                    self.spawn["done"] = True
                    self.spawn["go"]()
                before_post = getattr(self, "before_post", None)
                if before_post:
                    before_post()
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

    def test_a_second_run_during_a_live_claim_does_not_call_the_model_or_post(self):
        """B starts after A saved the claim and before A's model returns.

        No board reply yet is not proof A is dead. B must not take a live claim.
        """
        store = MemStore({"watermark": "2026-09-24T02:00:00Z", "answered_ids": []})
        calls, posts = [], []
        spawn = {"when": "after-claim"}

        def quiet(rid):
            return False

        def go():
            other = _ClaimingWaker(calls, posts=posts)
            other.reply_on_board = quiet
            try:
                main.run(store=store, waker=other)
            except state_store.Conflict:
                pass

        spawn["go"] = go
        first = _ClaimingWaker(calls, spawn=spawn, posts=posts)
        first.reply_on_board = quiet
        try:
            main.run(store=store, waker=first)
        except state_store.Conflict:
            pass
        self.assertEqual(calls, ["A"],
                         "the run that arrived during a live claim also called the model: %r" % (calls,))
        self.assertEqual(posts, ["A"],
                         "both runs appended: %r" % (posts,))

    def test_an_expired_lease_can_be_taken_and_the_old_owner_does_not_post(self):
        """After the lease, B may own the row. A must not append once it does not."""
        clock = {"now": "2026-09-24T05:00:00Z"}
        store = MemStore({"watermark": "2026-09-24T02:00:00Z", "answered_ids": []})
        calls, posts = [], []
        spawn = {"when": "after-claim"}

        def quiet(rid):
            return False

        def go():
            clock["now"] = "2026-09-24T05:05:00Z"
            other = _ClaimingWaker(calls, posts=posts)
            other.reply_on_board = quiet
            try:
                main.run(store=store, waker=other,
                         now=lambda: clock["now"], lease_seconds=60)
            except state_store.Conflict:
                pass

        spawn["go"] = go
        first = _ClaimingWaker(calls, spawn=spawn, posts=posts)
        first.reply_on_board = quiet
        try:
            main.run(store=store, waker=first,
                     now=lambda: clock["now"], lease_seconds=60)
        except state_store.Conflict:
            pass
        self.assertEqual(posts, ["A"],
                         "the stale owner appended after the lease was taken: %r" % (posts,))
        self.assertIn("A", store.state.get("answered_ids") or [])

    def test_a_lease_that_expires_during_the_append_is_not_taken(self):
        """Model returns at 250s. The append takes 360s. B arrives at 610s.

        A's 600s claim lease is over, and the reply is not on the board yet.
        B must not call the model or append. A's append, already on the wire, lands.
        """
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)
        clock = {"now": start}

        def now():
            return clock["now"]

        store = MemStore({"watermark": "2026-09-24T02:00:00Z", "answered_ids": []})
        calls, posts = [], []
        spawn = {"when": "during-post"}

        def go():
            clock["now"] = start + timedelta(seconds=610)
            other = _ClaimingWaker(calls, posts=posts)
            other.reply_on_board = lambda rid: False
            try:
                main.run(store=store, waker=other, now=now, lease_seconds=600)
            except state_store.Conflict:
                pass

        spawn["go"] = go
        first = _ClaimingWaker(calls, spawn=spawn, posts=posts)
        first.reply_on_board = lambda rid: False
        first.before_post = lambda: clock.__setitem__("now", start + timedelta(seconds=250))
        try:
            main.run(store=store, waker=first, now=now, lease_seconds=600)
        except state_store.Conflict:
            pass
        self.assertEqual(calls, ["A"],
                         "a second run called the model while the append was in flight: %r" % (calls,))
        self.assertEqual(posts, ["A"],
                         "a second run appended while the first append was on the wire: %r" % (posts,))

    def _posting_cursor(self):
        return {"watermark": "2026-09-24T02:00:00Z", "answered_ids": [],
                "inflight": "A", "claim_owner": "dead-run",
                "claim_until": "2026-09-24T05:00:00Z", "claim_phase": "posting"}

    def test_a_crash_after_posting_with_no_reply_is_quarantined(self):
        """phase=posting and no board reply. The next run must not post it again."""
        store = MemStore(self._posting_cursor())
        calls, posts = [], []
        w = _ClaimingWaker(calls, posts=posts)
        w.reply_on_board = lambda rid: False
        main.run(store=store, waker=w, now="2026-09-24T06:00:00Z")
        self.assertEqual(calls, [], "a posting claim was answered again: %r" % (calls,))
        self.assertEqual(posts, [], "a posting claim was appended again: %r" % (posts,))
        self.assertIn("A", store.state.get("unknown_ids") or [])
        self.assertNotIn("A", store.state.get("answered_ids") or [])
        self.assertEqual(store.state.get("claim_phase") or "", "")

    def test_a_crash_after_posting_with_a_reply_is_marked_done(self):
        """phase=posting and the reply is already on the board. Mark it answered, do not append."""
        store = MemStore(self._posting_cursor())
        calls, posts = [], []
        w = _ClaimingWaker(calls, posts=posts)
        w.reply_on_board = lambda rid: rid == "GEMINI-WAKE-A"
        main.run(store=store, waker=w, now="2026-09-24T06:00:00Z")
        self.assertEqual(calls, [])
        self.assertEqual(posts, [])
        self.assertIn("A", store.state.get("answered_ids") or [])
        self.assertNotIn("A", store.state.get("unknown_ids") or [])
        self.assertEqual(store.state.get("inflight") or "", "")


class QuarantineDoesNotStarveNewWork(unittest.TestCase):
    """Three unknown ids must not consume the whole pass.

    claim_answer already refuses them. The real loop still used to take
    pending[:max] before that refusal, so U1, U2 and U3 filled every pass
    and NEW was never called.
    """

    def test_one_pass_answers_the_new_row_and_leaves_the_quarantine(self):
        import agent_waker as aw

        posts, calls = [], []

        class Adapter(object):
            @staticmethod
            def ask(prompt, max_tokens=None):
                calls.append(prompt)
                return ("an answer", "fake-route")

        rows = []
        for i, rid in enumerate(("U1", "U2", "U3", "NEW")):
            rows.append([
                "ROW-%s" % rid,
                "2026-09-24T05:00:%02dZ" % i,
                "cowork-chrome",
                "gemini",
                "APPEND",
                "BCB|v=1|id=%s|to=gemini|ask=x" % rid,
            ])

        def read_since(env, since, tries=3):
            kept = [r for r in rows if str(r[1]) > since]
            return {"rows": kept, "total": len(rows), "filtered": len(kept)}

        store = MemStore({
            "watermark": "2026-09-24T04:59:59Z",
            "answered_ids": [],
            "unknown_ids": ["U1", "U2", "U3"],
        })
        saved_module = aw.AGENTS["gemini"]["module"]
        saved = (aw.read_since, aw.load_env, aw.log, aw.post_reply)
        aw.sys.modules["fake_quarantine_adapter"] = Adapter
        aw.AGENTS["gemini"]["module"] = "fake_quarantine_adapter"
        aw.read_since = read_since
        aw.load_env = lambda: {}
        aw.log = lambda me, line: None

        def post_reply(me, cfg, text, to, answers, verbose):
            posts.append(answers)
            return True

        aw.post_reply = post_reply
        try:
            main.run(store=store, waker=aw, board_contains=lambda rid: False,
                     argv=["--agent", "gemini", "--max", "3"])
            main.run(store=store, waker=aw, board_contains=lambda rid: False,
                     argv=["--agent", "gemini", "--max", "3"])
        finally:
            aw.AGENTS["gemini"]["module"] = saved_module
            aw.read_since, aw.load_env, aw.log, aw.post_reply = saved
            aw.sys.modules.pop("fake_quarantine_adapter", None)

        self.assertEqual(posts, ["NEW"],
                         "the new row was not answered once: posts=%r calls=%d"
                         % (posts, len(calls)))
        self.assertEqual(len(calls), 1)
        self.assertIn("NEW", store.state.get("answered_ids") or [])
        for rid in ("U1", "U2", "U3"):
            self.assertIn(rid, store.state.get("unknown_ids") or [])
            self.assertNotIn(rid, store.state.get("answered_ids") or [])


if __name__ == "__main__":
    unittest.main()

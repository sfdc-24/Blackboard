"""The cloud gemini waker: where its cursor lives decides whether a reply is sent twice."""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import agent_waker as aw  # noqa: E402
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

    def post_reply(self, me, cfg, text, to, answers, verbose, phase="DONE"):
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

        def racing_post(*a, **kw):
            store.gen += 1  # someone else wrote in between
            return real_post(*a, **kw)
        w.post_reply = racing_post
        with self.assertRaises(state_store.Conflict):
            main.run(store=store, waker=w)


class _ClaimingWaker:
    """Calls claim_answer before the model, which is the cloud hook.

    The model call is `calls.append`. post_reply is the confirmed board append.
    """

    def __init__(self, calls, spawn=None, on_post=None, posts=None):
        self.calls, self.spawn, self.on_post, self.posts = calls, spawn, on_post, posts

    def post_reply(self, me, cfg, text, to, answers, verbose, phase="DONE"):
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
            aw.reply_row_id(me, answers)))
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
        w.reply_on_board = lambda rid: rid == aw.reply_row_id("gemini", "A")
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

        phases = []

        def post_reply(me, cfg, text, to, answers, verbose, phase="DONE"):
            posts.append(answers)
            phases.append(phase)
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
        # The row carries no phase, so the cloud wrapper posts its answer as DONE (#297's control).
        self.assertEqual(phases, ["DONE"])
        self.assertIn("NEW", store.state.get("answered_ids") or [])
        for rid in ("U1", "U2", "U3"):
            self.assertIn(rid, store.state.get("unknown_ids") or [])
            self.assertNotIn(rid, store.state.get("answered_ids") or [])


def _legacy_reply(answers_id):
    """A row in the shape written before reply ids carried a hash."""
    return [
        aw.legacy_reply_row_id("gemini", answers_id),
        "2026-09-24T05:00:00Z",
        "gemini",
        "claude-code-cli;ALL",
        "DONE",
        "wakerreply=1|answers=%s|evidence=STATED|REPLY: ok" % answers_id,
    ]


class _Probe:
    """Sees whether recovery already closed the claim, and does not post."""

    def __init__(self, answers_id):
        self.answers_id = answers_id
        self.answered_at_start = None

    def post_reply(self, *args, **kwargs):
        raise AssertionError("recovery posted %s" % (args[4] if len(args) > 4 else args,))

    def main(self, argv):
        path = os.path.join(os.environ["BLACKBOARD_STATE_DIR"], ".gemini_waker_state.json")
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        self.answered_at_start = self.answers_id in (state.get("answered_ids") or [])
        return 0


class ReplyIdentity(unittest.TestCase):
    """A receipt closes the ask it names, not the ask that shares a Row_ID."""

    def _posting(self, answers_id, phase="posting"):
        return {"watermark": "2026-09-24T02:00:00Z", "answered_ids": [],
                "inflight": answers_id, "claim_owner": "dead-run",
                "claim_until": "2026-09-24T05:00:00Z", "claim_phase": phase}

    def _recover(self, inflight, rows, phase="posting"):
        store = MemStore(self._posting(inflight, phase))
        probe = _Probe(inflight)

        def read_rows(env, title="Blackboard - Alpha DB", tries=4,
                      since=None, limit=None, match=None):
            return {"rows": rows}

        with mock.patch("bus.load_env", return_value={"BUS_URL": "u", "BUS_SECRET": "s"}), \
             mock.patch("bus.read_rows", side_effect=read_rows):
            main.run(store=store, waker=probe, now="2026-09-24T06:00:00Z")
        return store, probe

    def test_a_dotted_receipt_does_not_close_the_underscore_ask(self):
        first, second = "SYNTHETIC.ASK", "SYNTHETIC_ASK"
        self.assertEqual(aw.legacy_reply_row_id("gemini", first),
                         aw.legacy_reply_row_id("gemini", second))
        store, probe = self._recover(second, [_legacy_reply(first)])
        self.assertFalse(probe.answered_at_start)
        self.assertNotIn(second, store.state.get("answered_ids") or [])
        self.assertIn(second, store.state.get("unknown_ids") or [])

    def test_a_long_prefix_receipt_does_not_close_the_other_long_id(self):
        first, second = ("L" * 40) + "ONE", ("L" * 40) + "TWO"
        self.assertEqual(aw.legacy_reply_row_id("gemini", first),
                         aw.legacy_reply_row_id("gemini", second))
        store, probe = self._recover(second, [_legacy_reply(first)])
        self.assertFalse(probe.answered_at_start)
        self.assertNotIn(second, store.state.get("answered_ids") or [])
        self.assertIn(second, store.state.get("unknown_ids") or [])

    def test_a_legacy_receipt_with_the_full_answers_field_still_closes(self):
        """The old Row_ID is how the row is found. answers= is why it counts."""
        answers_id = "SYNTHETIC_ASK"
        store, probe = self._recover(answers_id, [_legacy_reply(answers_id)])
        self.assertTrue(probe.answered_at_start)
        self.assertIn(answers_id, store.state.get("answered_ids") or [])
        self.assertNotIn(answers_id, store.state.get("unknown_ids") or [])

    def _assert_open(self, answers_id, rows, phase):
        store, probe = self._recover(answers_id, rows, phase)
        self.assertFalse(probe.answered_at_start)
        self.assertNotIn(answers_id, store.state.get("answered_ids") or [])
        if phase == "posting":
            self.assertIn(answers_id, store.state.get("unknown_ids") or [])
            self.assertEqual(store.state.get("inflight") or "", "")
        else:
            self.assertEqual(store.state.get("inflight"), answers_id)
            self.assertNotIn(answers_id, store.state.get("unknown_ids") or [])

    def _assert_closed(self, answers_id, rows, phase):
        store, probe = self._recover(answers_id, rows, phase)
        self.assertTrue(probe.answered_at_start)
        self.assertIn(answers_id, store.state.get("answered_ids") or [])
        self.assertNotIn(answers_id, store.state.get("unknown_ids") or [])
        self.assertEqual(store.state.get("inflight") or "", "")

    def test_another_agents_receipt_does_not_complete_this_claim(self):
        """A claude-api reply to a shared ask is not Gemini's reply."""
        shared = "SHARED-ASK"
        rows = [
            _own_reply(shared, agent="claude-api"),
            _own_reply(shared, agent="claude-api", legacy=True),
        ]
        for phase in ("claimed", "posting"):
            with self.subTest(phase=phase):
                self._assert_open(shared, rows, phase)

    def test_an_ordinary_note_does_not_complete_the_claim(self):
        """A Codex NOTE that carries answers= is not this waker's reply."""
        shared = "SHARED-ASK"
        rows = [_codex_note(shared)]
        for phase in ("claimed", "posting"):
            with self.subTest(phase=phase):
                self._assert_open(shared, rows, phase)

    def test_the_wrong_row_id_does_not_complete_the_claim(self):
        """Producer markers are not enough when the Row_ID is not this reply."""
        shared = "SHARED-ASK"
        rows = [_own_reply(shared, row_id=aw.reply_row_id("gemini", "OTHER-ASK"))]
        for phase in ("claimed", "posting"):
            with self.subTest(phase=phase):
                self._assert_open(shared, rows, phase)

    def test_this_agents_own_reply_still_completes_the_claim(self):
        shared = "SHARED-ASK"
        rows = [_own_reply(shared)]
        for phase in ("claimed", "posting"):
            with self.subTest(phase=phase):
                self._assert_closed(shared, rows, phase)

    def test_a_legacy_done_note_from_this_agent_still_completes_the_claim(self):
        """Rows written before wakerreply=1 are this waker's DONE/NOTE kind."""
        shared = "SHARED-ASK"
        rid = aw.legacy_reply_row_id("gemini", shared)
        payload = ("BCB|v=1|id=%s|phase=DONE|class=NOTE|from=gemini|"
                   "to=claude-code-cli,ALL|answers=%s|evidence=STATED|REPLY: ok"
                   % (rid, shared))
        rows = [_own_reply(shared, legacy=True, payload=payload)]
        self._assert_closed(shared, rows, "posting")


def _own_reply(answers_id, *, agent="gemini", legacy=False, source=None,
               row_id=None, payload=None):
    """A reply row in the shape fleet_agent writes for this agent."""
    rid = row_id or (aw.legacy_reply_row_id(agent, answers_id) if legacy
                     else aw.reply_row_id(agent, answers_id))
    src = agent if source is None else source
    if payload is None:
        payload = ("BCB|v=1|id=%s|phase=DONE|class=NOTE|from=%s|"
                   "to=claude-code-cli,ALL|wakerreply=1|answers=%s|"
                   "evidence=STATED|REPLY: ok" % (rid, src, answers_id))
    return [rid, "2026-09-24T05:00:00Z", src, "claude-code-cli;ALL", "DONE", payload]


def _codex_note(answers_id):
    """An ordinary NOTE. Quoting the marker in prose does not make it a reply."""
    rid = "CODEX-NOTE-%s" % answers_id
    payload = ("BCB|v=1|id=%s|phase=DONE|class=NOTE|from=codex|to=ALL|"
               "answers=%s|evidence=STATED|REPLY: quoting wakerreply=1 is not a reply"
               % (rid, answers_id))
    return [rid, "2026-09-24T05:00:00Z", "codex", "ALL", "DONE", payload]


class ModelFromTheJob(unittest.TestCase):
    """The job picks Gemini's model and timeout (GEMINI_MODEL, GEMINI_TIMEOUT_SECONDS); unset, the
    adapter keeps Flash and 60 s (the owner approved the Pro tier, 2026-09-29)."""

    def _load(self, env):
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("GEMINI_MODEL", "GEMINI_TIMEOUT_SECONDS"):
                if name not in env:
                    os.environ.pop(name, None)
            spec = importlib.util.spec_from_file_location("gemini_agent_for_model", REPO / "scripts" / "gemini_agent.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def _sent(self, module):
        seen = []

        def post(url, headers, payload, timeout=60):
            seen.append((payload["model"], timeout))
            return 200, json.dumps({"output_text": "an answer"})
        with mock.patch.object(module, "_post", side_effect=post), \
                mock.patch.object(module, "api_key", return_value=("GEMINI_API_KEY", "k")):
            module.ask("a question")
        return seen

    def test_the_job_s_model_and_timeout_reach_the_request(self):
        module = self._load({"GEMINI_MODEL": "gemini-pro-latest", "GEMINI_TIMEOUT_SECONDS": "150"})
        self.assertEqual([("gemini-pro-latest", 150)], self._sent(module))

    def test_unset_it_stays_flash_with_the_old_timeout(self):
        module = self._load({})
        self.assertEqual([("gemini-3.8-flash", 60)], self._sent(module))

    def test_a_caller_s_model_still_wins(self):
        module = self._load({"GEMINI_MODEL": "gemini-pro-latest"})
        with mock.patch.object(module, "_post", return_value=(200, "{}")) as post, \
                mock.patch.object(module, "api_key", return_value=("GEMINI_API_KEY", "k")):
            module.ask("a question", model="gemini-3.8-flash")
        self.assertEqual("gemini-3.8-flash", post.call_args[0][2]["model"])

class RepoContextForGemini(unittest.TestCase):
    """A row that names a PUBLIC PR gets that PR's read-only diff attached; nothing else does (the
    owner approved a read-only GitHub token for Gemini's adapter, 2026-09-29; Codex on #299 fde4ff6:
    a row naming a private PR must cause no GitHub request and no private content)."""

    HEAD_A, HEAD_B = "37eee95abcdef0123456", "99aa00bbccddeeff0011"

    def setUp(self):
        import repo_context
        self.rc = repo_context

    def fake_get(self, calls, fail=None, files=None, private=False, heads=None, changed=None, bases=None):
        import urllib.error
        files = files if files is not None else [
            {"filename": "gateway/app.py", "status": "modified", "additions": 3, "deletions": 1,
             "patch": "@@ -1 +1 @@\n-old\n+new"},
            {"filename": "big.txt", "status": "added", "additions": 9000, "deletions": 0, "patch": "x" * 9000}]
        heads = list(heads or [self.HEAD_A])
        bases = list(bases or ["b0b0b0b0b0b0b0"])

        def get(path, token, timeout=20):
            calls.append((path, token))
            if fail:
                raise urllib.error.HTTPError(path, fail, "nope", {}, None)
            if "/files?" in path:
                page = int(path.rsplit("page=", 1)[1])
                return files[(page - 1) * 100: page * 100]
            head = heads.pop(0) if len(heads) > 1 else heads[0]
            base = bases.pop(0) if len(bases) > 1 else bases[0]
            return {"title": "a change", "state": "open", "merged_at": None, "head": {"sha": head},
                    "changed_files": len(files) if changed is None else changed,
                    "base": {"sha": base, "repo": {"private": private}}}
        return get

    def test_it_finds_only_explicitly_named_prs_in_public_repositories(self):
        cases = {
            "Blackboard PR #297 and site #256": [("Blackboard", 297), ("sfdc24-site", 256)],
            "see github.com/sfdc-24/Blackboard/pull/299": [("Blackboard", 299)],
            "sfdc-24/sfdc24-site#12": [("sfdc24-site", 12)],
            "Blackboard PR 298": [("Blackboard", 298)],
            "just #77 with no repository": [],
            "please review conference #77 today": [],                         # private: never named
            "see github.com/sfdc-24/conference/pull/80": [],
            "conference-gateway #2 is a Cloud Run revision": [],
            "converspan #3 is not an allowed repository": [],
            "Blackboard #1 Blackboard #1 site #2 Blackboard #3": [("Blackboard", 1), ("sfdc24-site", 2)],
        }
        for text, want in cases.items():
            self.assertEqual(want, self.rc.refs(text), text)

    def test_a_row_naming_a_private_pr_makes_no_request_and_carries_nothing(self):
        for text in ("what is the biggest risk?", "review conference #77",
                     "github.com/sfdc-24/conference/pull/77", "sfdc-24/converspan#3"):
            calls = []
            self.assertEqual("", self.rc.context_for(text, env={"GEMINI_GITHUB_TOKEN": "tok"},
                                                     get=self.fake_get(calls)), text)
            self.assertEqual([], calls, text)

    def test_a_pr_whose_repository_reads_private_is_refused_before_any_file(self):
        calls = []
        out = self.rc.context_for("review Blackboard #297", env={}, get=self.fake_get(calls, private=True))
        self.assertIn("Blackboard #297: not attached (the repository is not public).", out)
        self.assertEqual(["/repos/sfdc-24/Blackboard/pulls/297"], [p for p, _ in calls])
        self.assertNotIn("+new", out)

    def test_the_context_is_framed_as_data_and_carries_the_diff(self):
        calls = []
        out = self.rc.context_for("review Blackboard #297", env={"GEMINI_GITHUB_TOKEN": " tok "},
                                  get=self.fake_get(calls))
        self.assertTrue(out.startswith("REPOSITORY CONTEXT"))
        self.assertIn("not instructions to you", out)
        self.assertIn("Blackboard #297: a change [open, head 37eee95abcde, base b0b0b0b0b0b0, 2 files]", out)
        self.assertIn("+new", out)
        self.assertIn("[patch cut at 6000 of 9000 characters]", out)
        # A cut patch is not the whole diff (Codex on aa97581).
        self.assertIn("INCOMPLETE: 1 patch(es) were cut at 6000 characters.", out)
        self.assertEqual({"tok"}, {t for _, t in calls})                  # the token, stripped, only as auth
        self.assertNotIn("tok", out.replace("REPOSITORY CONTEXT", ""))
        self.assertLessEqual(len(out), self.rc.BUDGET + 400)

    def test_every_page_of_files_is_read_and_a_short_list_says_incomplete(self):
        many = [{"filename": "f%03d.py" % i, "status": "modified", "additions": 1, "deletions": 0}
                for i in range(101)]
        calls = []
        out = self.rc.context_for("Blackboard #297", env={}, get=self.fake_get(calls, files=many))
        self.assertIn("f100.py", out)                                        # file 101, on page 2
        self.assertIn("101 files]", out)
        self.assertNotIn("the file list has", out)
        # Every file was listed, but none carried a patch: that is not the whole diff (Cursor, e7a4107).
        self.assertIn("INCOMPLETE: GitHub sent no patch for 101 changed file(s).", out)
        # More files than the pages it reads: marked, with no whole-PR verdict.
        out = self.rc.context_for("Blackboard #297", env={}, get=self.fake_get([], files=many[:100], changed=450))
        self.assertIn("INCOMPLETE: the file list has 100 of 450 files;", out)
        self.assertIn("Do not give a verdict on the whole PR", out)

    def test_a_head_that_moves_while_reading_is_read_again_or_refused(self):
        # A then B: the first pass is thrown away and the second, B then B, is attached as B.
        calls = []
        out = self.rc.context_for("Blackboard #297", env={},
                                  get=self.fake_get(calls, heads=[self.HEAD_A, self.HEAD_B, self.HEAD_B, self.HEAD_B]))
        self.assertIn("head 99aa00bbccdd", out)
        self.assertNotIn("head 37eee95abcde", out)
        # It keeps moving: no patch is labelled with a head it may not belong to.
        out = self.rc.context_for("Blackboard #297", env={},
                                  get=self.fake_get([], heads=["a1", "b2", "c3", "d4", "e5"]))
        self.assertIn("Blackboard #297: not attached (its head or base moved while it was being read).", out)
        self.assertNotIn("+new", out)

    def test_a_base_that_moves_under_a_fixed_head_is_read_again_or_refused(self):
        out = self.rc.context_for("Blackboard #297", env={},
                                  get=self.fake_get([], bases=["base-A", "base-B", "base-B", "base-B"]))
        self.assertIn("base base-B,", out)
        out = self.rc.context_for("Blackboard #297", env={},
                                  get=self.fake_get([], bases=["b1", "b2", "b3", "b4", "b5"]))
        self.assertIn("not attached (its head or base moved while it was being read).", out)
        self.assertNotIn("+new", out)

    def _small(self, n=2):
        return [{"filename": "f%d.py" % i, "status": "modified", "additions": 1, "deletions": 1, "changes": 2,
                 "patch": "@@ -1 +1 @@\n-a%d\n+b%d" % (i, i)} for i in range(n)]

    def _marker(self, out):
        first = out.split("===\n", 1)[1]
        return first.split("\n")[1]                     # the line right after the PR's header

    def test_complete_only_when_every_full_patch_is_there(self):
        out = self.rc.context_for("Blackboard #297", env={}, get=self.fake_get([], files=self._small()))
        self.assertEqual("COMPLETE: every changed file and its full patch is below.", self._marker(out))
        self.assertNotIn("INCOMPLETE", out)

    def test_each_kind_of_gap_is_marked_incomplete_before_any_content(self):
        binary = self._small() + [{"filename": "logo.png", "status": "added", "additions": 0,
                                   "deletions": 0, "changes": 0}]
        big = self._small() + [{"filename": "big%d.py" % i, "status": "modified", "additions": 900,
                                "deletions": 0, "changes": 900, "patch": "y" * 5900} for i in range(5)]
        workflow_out = {"filename": "docs/required.yml", "previous_filename": ".github/workflows/required.yml",
                        "status": "renamed", "additions": 0, "deletions": 0, "changes": 0}
        cases = [
            (dict(files=binary), "GitHub sent no patch for 1 changed file(s)"),
            (dict(files=big), "patch(es) were left out to fit"),
            (dict(files=self._small(), changed=None), None),
            (dict(files=self._small(), changed=9), "the file list has 2 of 9 files"),
        ]
        for kw, reason in cases:
            get = self.fake_get([], **kw)
            if kw.get("changed", 0) is None:
                def get(path, token, timeout=20, _g=self.fake_get([], files=self._small())):
                    r = _g(path, token, timeout)
                    if isinstance(r, dict):
                        r.pop("changed_files")
                    return r
                reason = "its changed-file count is unknown"
            out = self.rc.context_for("Blackboard #297", env={}, get=get)
            marker = self._marker(out)
            self.assertTrue(marker.startswith("INCOMPLETE: "), (reason, marker))
            self.assertIn(reason, marker)
            self.assertIn("Do not give a verdict on the whole PR", marker)
            self.assertLess(out.index("INCOMPLETE"), out.find("\n--- ") if "\n--- " in out else len(out))
            self.assertLessEqual(len(out), self.rc.BUDGET + 400)
        # A proven pure rename carries no patch and hides nothing, and its source path is shown: a
        # required workflow moved out of .github/workflows/ is visible (Codex, e7a4107).
        out = self.rc.context_for("Blackboard #297", env={},
                                  get=self.fake_get([], files=self._small() + [workflow_out]))
        self.assertTrue(self._marker(out).startswith("COMPLETE"), self._marker(out))
        self.assertIn("  renamed .github/workflows/required.yml -> docs/required.yml (+0 -0)", out)

    def test_a_file_without_a_patch_is_a_gap_unless_it_is_a_proven_pure_rename(self):
        gaps = {
            "modified, additions, no changes key": {"filename": "a.py", "status": "modified", "additions": 1},
            "modified, additions 5, changes 0": {"filename": "a.py", "status": "modified", "additions": 5,
                                                 "deletions": 0, "changes": 0},
            "modified, deletions 4, no changes key": {"filename": "a.py", "status": "modified", "deletions": 4},
            "modified, no counts at all": {"filename": "a.py", "status": "modified"},
            "rename without its source path": {"filename": "b.py", "status": "renamed", "additions": 0,
                                               "deletions": 0, "changes": 0},
            "rename with content changes": {"filename": "b.py", "previous_filename": "a.py", "status": "renamed",
                                            "additions": 3, "deletions": 1, "changes": 4},
            "rename with no counts": {"filename": "b.py", "previous_filename": "a.py", "status": "renamed"},
            "added binary": {"filename": "logo.png", "status": "added", "additions": 0, "deletions": 0,
                             "changes": 0},
            "removed with no patch": {"filename": "old.py", "status": "removed", "additions": 0,
                                      "deletions": 0, "changes": 0},
        }
        for label, f in gaps.items():
            out = self.rc.context_for("Blackboard #297", env={}, get=self.fake_get([], files=self._small() + [f]))
            self.assertIn("INCOMPLETE: GitHub sent no patch for 1 changed file(s)", self._marker(out), label)
        # A rename with content changes and its patch is COMPLETE, and names both paths.
        moved = {"filename": "b.py", "previous_filename": "a.py", "status": "renamed", "additions": 1,
                 "deletions": 1, "changes": 2, "patch": "@@ -1 +1 @@\n-x\n+y"}
        out = self.rc.context_for("Blackboard #297", env={}, get=self.fake_get([], files=[moved]))
        self.assertTrue(self._marker(out).startswith("COMPLETE"))
        self.assertIn("--- a.py -> b.py", out)

    def test_a_list_too_long_for_the_room_is_cut_and_marked(self):
        many = [{"filename": "deep/path/to/a/module/number_%04d_with_a_long_name.py" % i, "status": "modified",
                 "additions": 1, "deletions": 0, "changes": 1, "patch": "+x"} for i in range(300)]
        out = self.rc.context_for("Blackboard #297", env={}, get=self.fake_get([], files=many))
        self.assertIn("the file list was cut to fit", self._marker(out))
        self.assertLessEqual(len(out), self.rc.BUDGET + 400)

    def test_a_second_pr_with_no_room_left_is_named_not_half_read(self):
        big = [{"filename": "big%d.py" % i, "status": "modified", "additions": 900, "deletions": 0,
                "changes": 900, "patch": "y" * 5900} for i in range(5)]
        from unittest import mock
        calls = []
        # The first PR spends about 18,000 of the 24,000 characters; with MIN_ROOM above what is left,
        # the second is named, not fetched.
        with mock.patch.object(self.rc, "MIN_ROOM", 10000):
            out = self.rc.context_for("Blackboard #297 and site #256", env={}, get=self.fake_get(calls, files=big))
        self.assertIn("sfdc24-site #256: not attached (the context budget was spent on the PR before it).", out)
        self.assertFalse(any("sfdc24-site" in p for p, _ in calls))
        self.assertLessEqual(len(out), self.rc.BUDGET + 400)

    def test_a_response_over_the_byte_cap_is_refused_before_parsing(self):
        from unittest import mock
        body = mock.MagicMock()
        body.read.return_value = b"x" * (self.rc.MAX_RESPONSE_BYTES + 1)
        body.__enter__.return_value = body
        with mock.patch.object(self.rc.urllib.request, "urlopen", return_value=body):
            out = self.rc.context_for("Blackboard #297", env={})
        body.read.assert_called_with(self.rc.MAX_RESPONSE_BYTES + 1)
        self.assertIn("not attached (a response was over 2000000 bytes)", out)

    def test_a_spent_read_budget_stops_the_requests(self):
        from unittest import mock
        calls = []
        ticks = iter([0.0, 1.0, 50.0, 60.0, 70.0, 80.0])
        with mock.patch.object(self.rc.time, "monotonic", side_effect=lambda: next(ticks)):
            out = self.rc.context_for("Blackboard #297", env={}, get=self.fake_get(calls))
        self.assertIn("not attached (the 45 s read budget was spent)", out)
        self.assertEqual(1, len(calls))

    def test_an_unreadable_pr_is_a_note_and_the_answer_goes_on(self):
        out = self.rc.context_for("review Blackboard #297", env={}, get=self.fake_get([], fail=404))
        self.assertIn("Blackboard #297: not readable (HTTP 404).", out)

    def test_the_waker_attaches_it_for_gemini_only(self):
        self.assertTrue(aw.AGENTS["gemini"].get("repo_context"))
        for tag, cfg in aw.AGENTS.items():
            if tag != "gemini":
                self.assertFalse(cfg.get("repo_context"), tag)

    def test_the_prompt_the_model_sees_carries_the_context(self):
        prompts = []

        class Adapter(object):
            @staticmethod
            def ask(prompt, max_tokens=None):
                prompts.append(prompt)
                return ("an answer", "fake-route")

        rows = [["ROW-A", "2026-09-29T07:00:00Z", "grok", "gemini", "APPEND",
                 "BCB|v=1|id=RC-1|phase=DISPATCH|to=gemini|review Blackboard #297"],
                ["ROW-B", "2026-09-29T07:00:01Z", "grok", "gemini", "APPEND",
                 "BCB|v=1|id=RC-2|phase=DISPATCH|to=gemini|no pull request named here"]]
        saved = (aw.read_since, aw.load_env, aw.log, aw.post_reply, aw.state_path)
        saved_module = aw.AGENTS["gemini"]["module"]
        saved_context = self.rc.context_for
        tmp = __import__("tempfile").mkdtemp()
        aw.sys.modules["fake_repo_context_adapter"] = Adapter
        aw.AGENTS["gemini"]["module"] = "fake_repo_context_adapter"
        aw.read_since = lambda env, since, tries=3: {"rows": rows, "total": 2, "filtered": 2}
        aw.load_env = lambda: {}
        aw.log = lambda me, line: None
        aw.post_reply = lambda *a, **k: True
        aw.state_path = lambda me: os.path.join(tmp, ".gemini_state.json")
        self.rc.context_for = lambda text, env=None, get=None: (
            "REPOSITORY CONTEXT fake for Blackboard #297" if "Blackboard #297" in text else "")
        try:
            aw.main(["--agent", "gemini", "--max", "3"])
        finally:
            aw.read_since, aw.load_env, aw.log, aw.post_reply, aw.state_path = saved
            aw.AGENTS["gemini"]["module"] = saved_module
            self.rc.context_for = saved_context
            aw.sys.modules.pop("fake_repo_context_adapter", None)
        self.assertEqual(2, len(prompts))
        self.assertIn("---\n\nREPOSITORY CONTEXT fake for Blackboard #297", prompts[0])
        self.assertNotIn("REPOSITORY CONTEXT", prompts[1])

    def test_the_image_carries_every_script_the_waker_imports(self):
        # A module on disk is not a module in the image (the conference Dockerfile dropped two
        # console modules on 2026-09-28). Every scripts/ module agent_waker imports must be COPY'd.
        import re as _re
        docker = (REPO / "cloud" / "agent-waker" / "Dockerfile").read_text(encoding="utf-8")
        copied = set(_re.findall(r"scripts/([A-Za-z_]+)\.py", docker))
        source = (REPO / "scripts" / "agent_waker.py").read_text(encoding="utf-8")
        imported = set(_re.findall(r"^(?:import|from) ([A-Za-z_]+)", source, _re.M))
        local = {m for m in imported if (REPO / "scripts" / (m + ".py")).is_file()}
        self.assertIn("repo_context", local)
        self.assertEqual(set(), local - copied)

    def test_every_script_the_image_copies_is_uploaded_to_cloud_build(self):
        # The Dockerfile's COPY list is not the upload list. .gcloudignore is an allowlist, and a
        # script it does not re-admit never reaches Cloud Build: the agent-waker build at 523500b
        # failed with "stat scripts/repo_context.py: file does not exist".
        import re as _re
        docker = (REPO / "cloud" / "agent-waker" / "Dockerfile").read_text(encoding="utf-8")
        copied = set(_re.findall(r"scripts/([A-Za-z_]+)\.py", docker))
        allow = (REPO / ".gcloudignore").read_text(encoding="utf-8").splitlines()
        readmitted = {line[len("!/scripts/"):-3] for line in allow
                      if line.startswith("!/scripts/") and line.endswith(".py")}
        self.assertIn("/scripts/*", allow)                    # the directory is an allowlist
        self.assertIn("repo_context", copied)
        self.assertEqual(set(), copied - readmitted)


class APostThatOutlivesItsTimeout(unittest.TestCase):
    """gemini-waker-tmm8l, 2026-09-29: the RESULT row landed, the read-back hung past 400 s, and the
    uncaught TimeoutExpired ended the pass with exit 1. Not confirmed is not failed: the pass ends
    cleanly, the claim stays phase=posting, and the next run reconciles it without posting again."""

    def test_the_pass_ends_cleanly_and_the_next_run_reconciles_without_a_second_post(self):
        import subprocess
        calls, attempts = [], []

        class Adapter(object):
            @staticmethod
            def ask(prompt, max_tokens=None):
                calls.append(prompt)
                return ("an answer", "fake-route")

        rows = [["ROW-T", "2026-09-29T07:21:00Z", "grok", "gemini", "APPEND",
                 "BCB|v=1|id=SLOW-1|phase=DISPATCH|to=gemini|ask=x"]]

        def hang(args, **kw):
            attempts.append(args)
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))

        store = MemStore({"watermark": "2026-09-29T07:00:00Z", "answered_ids": []})
        saved = (aw.read_since, aw.load_env, aw.log)
        saved_module = aw.AGENTS["gemini"]["module"]
        aw.sys.modules["fake_slow_post_adapter"] = Adapter
        aw.AGENTS["gemini"]["module"] = "fake_slow_post_adapter"
        aw.read_since = lambda env, since, tries=3: {"rows": [r for r in rows if str(r[1]) > since],
                                                     "total": 1, "filtered": 1}
        aw.load_env = lambda: {}
        aw.log = lambda me, line: None
        import io
        from contextlib import redirect_stdout
        printed = io.StringIO()
        try:
            with mock.patch.object(aw.subprocess, "run", side_effect=hang), redirect_stdout(printed):
                main.run(store=store, waker=aw, board_contains=lambda rid: False,
                         argv=["--agent", "gemini", "--max", "3"])       # no exception: a clean pass
            # The whole pass says NOT CONFIRMED and claims neither failure nor delivery (Codex on #300).
            text = printed.getvalue()
            self.assertIn("post NOT CONFIRMED after 400 s", text)
            self.assertIn("POST NOT CONFIRMED for SLOW-1", text)
            for claim in ("FAILED", "posted,", "VERIFIED"):
                self.assertNotIn(claim, text)
            self.assertEqual(1, len(attempts))
            self.assertEqual(aw.POST_TIMEOUT_SECONDS, 400)
            self.assertEqual("posting", store.state.get("claim_phase"))
            self.assertEqual("SLOW-1", store.state.get("inflight"))
            self.assertNotIn("SLOW-1", store.state.get("answered_ids") or [])
            # The reply had landed. The next run finds it on the board and records it: no model
            # call and no second post.
            with mock.patch.object(aw.subprocess, "run", side_effect=hang):
                main.run(store=store, waker=aw, board_contains=lambda rid: True,
                         argv=["--agent", "gemini", "--max", "3"])
        finally:
            aw.read_since, aw.load_env, aw.log = saved
            aw.AGENTS["gemini"]["module"] = saved_module
            aw.sys.modules.pop("fake_slow_post_adapter", None)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(attempts))
        self.assertIn("SLOW-1", store.state.get("answered_ids") or [])
        self.assertNotEqual("posting", store.state.get("claim_phase"))

    def test_post_reply_returns_false_and_says_not_confirmed(self):
        import io
        import subprocess
        from contextlib import redirect_stdout

        def hang(args, **kw):
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))
        buf = io.StringIO()
        with mock.patch.object(aw.subprocess, "run", side_effect=hang), redirect_stdout(buf):
            self.assertFalse(aw.post_reply("gemini", {"project": "FLEET"}, "t", "grok;ALL", "X", False,
                                           phase="RESULT"))
        self.assertIn("NOT CONFIRMED after 400 s", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

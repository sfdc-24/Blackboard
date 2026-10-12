#!/usr/bin/env python3
"""A row that fails once must still be there on the next pass.

FOUND BY chatgpt-codex-desktop, 2026-09-20, with a live repro of that morning's
16:01:44Z grok pass:

    WRK-0ba298fa         posted
    CCC-RW-PROOF-001     could not answer: HTTP Error 429
    CCC-CONSOLE-403-001  posted

The watermark advanced to the LATER success. CCC-RW-PROOF-001 never reached
answered_ids, so on the next pass it fell before `since` and was never selected
again. Answered rows are remembered; failed ones were simply dropped, silently,
with the pass reporting success.

The hole was opened by catching adapter failures so one 429 could not take the
doorbell down for every later row. That behaviour is worth keeping - so the fix
is a floor on the watermark, not a break.

Run: python tests/test_waker_watermark.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import agent_waker as aw  # noqa: E402


def row(rid, ts, payload="BCB|v=1|id=%s|ask=x"):
    return [rid, ts, "claude-code-cli", "grok", "APPEND", payload % rid]


class WatermarkFloor(unittest.TestCase):
    """main() is driven end to end with a fake adapter and a fake poster."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.calls = []
        self.posted = []

        outer = self

        class Adapter(object):
            @staticmethod
            def ask(prompt, max_tokens=None):
                outer.calls.append(prompt)
                # second row of the pass fails, exactly like the live 429
                if "CCC-RW-PROOF-001" in prompt:
                    raise RuntimeError("HTTP Error 429: Too Many Requests")
                return ("an answer", "fake-route")

        self._real_state_path = aw.state_path
        self._real_read = aw.read_since
        self._real_post = aw.post_reply
        self._real_log = aw.log

        aw.sys.modules["fake_wm_adapter"] = Adapter
        aw.AGENTS["grok"] = dict(aw.AGENTS["grok"], module="fake_wm_adapter")
        aw.state_path = lambda me: os.path.join(self.tmp, "." + me + "_state.json")
        aw.log = lambda me, line: None
        aw.post_reply = self._fake_post
        aw.read_since = self._fake_read
        aw.load_env = lambda: {}

    def tearDown(self):
        aw.state_path = self._real_state_path
        aw.read_since = self._real_read
        aw.post_reply = self._real_post
        aw.log = self._real_log
        aw.sys.modules.pop("fake_wm_adapter", None)

    def _fake_post(self, me, cfg, text, to, answers, verbose, phase="DONE"):
        self.posted.append(answers)
        return True

    def _fake_read(self, env, since):
        rows = [r for r in self.board
                if str(r[1]) > since]
        return {"rows": rows, "total": len(self.board), "filtered": len(rows)}

    def _state(self):
        with open(aw.state_path("grok"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_failed_row_is_still_selectable_next_pass(self):
        """The exact live sequence: posted, failed, posted."""
        self.board = [
            row("WRK-0ba298fa", "2026-09-20T16:00:00Z"),
            row("CCC-RW-PROOF-001", "2026-09-20T16:01:00Z"),
            row("CCC-CONSOLE-403-001", "2026-09-20T16:02:00Z"),
        ]
        rc = aw.main(["--agent", "grok", "--max", "3", "--since-hours", "999999"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.posted, ["WRK-0ba298fa", "CCC-CONSOLE-403-001"])

        st = self._state()
        self.assertNotIn("CCC-RW-PROOF-001", st["answered_ids"],
                         "it failed, so it must not be marked answered")
        self.assertLess(st["watermark"], "2026-09-20T16:01:00Z",
                        "the watermark must stay BELOW the failed row, or that "
                        "row falls out of the window for ever")

        # second pass: the failed row must come back, the answered ones must not
        self.posted = []
        rc = aw.main(["--agent", "grok", "--max", "3"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.posted, [],
                         "the adapter still fails it, so nothing new posts")
        self.assertIn("CCC-RW-PROOF-001",
                      " ".join(self.calls),
                      "the failed row must be asked again")

    def test_a_clean_pass_still_advances_the_watermark(self):
        """The floor must not freeze a healthy waker in place."""
        self.board = [
            row("A-1", "2026-09-20T16:00:00Z"),
            row("A-2", "2026-09-20T16:02:00Z"),
        ]
        aw.main(["--agent", "grok", "--max", "3", "--since-hours", "999999"])
        self.assertEqual(self.posted, ["A-1", "A-2"])
        self.assertEqual(self._state()["watermark"], "2026-09-20T16:02:00Z")

    def test_the_watermark_never_moves_backward(self):
        """A failure older than where the agent already is must not re-open
        rows it has finished with."""
        os.makedirs(self.tmp, exist_ok=True)
        with open(aw.state_path("grok"), "w", encoding="utf-8") as fh:
            json.dump({"watermark": "2026-09-20T15:00:00Z", "answered_ids": []}, fh)
        self.board = [
            row("CCC-RW-PROOF-001", "2026-09-20T16:01:00Z"),
            row("B-2", "2026-09-20T16:02:00Z"),
        ]
        aw.main(["--agent", "grok", "--max", "3"])
        self.assertGreaterEqual(self._state()["watermark"], "2026-09-20T15:00:00Z")


class LaptopRedisClaim(unittest.TestCase):
    """GROK'S API ROUTE IS A LAPTOP PASS, and until this a laptop pass refused every REDIS-OP line,
    because nothing on the laptop could fence a retried ask. Now the pass holds an OS lock, writes
    its claim to a synced file BEFORE the request append, and the next pass settles any claim left
    behind from the board: answered when the reply is there, quarantined when it is not, and never
    asked again either way. Driven end to end through main() with a fake model, board and poster."""

    SRC = "CCC-SIDE-ASK-001"
    LINE = "Done.\nREDIS-OP|op=xadd|key=conf:side:20261011|val=the lease shape holds"

    def setUp(self):
        import types
        self.tmp = tempfile.mkdtemp()
        self.model_calls, self.requests, self.replies = [], [], []
        self.request_answer = ("posted", None)
        self.reply_answer = True
        self.claims_seen_at_request = None
        self.board = [row(self.SRC, "2026-10-11T15:00:00Z")]
        outer = self

        def ask(prompt, max_tokens=None):
            outer.model_calls.append(prompt)
            return (outer.LINE, "fake-route")
        ask.last_finish = "STOP"
        self.real = (aw.state_path, aw.read_since, aw.post_reply, aw.post_request, aw.log,
                     aw.load_env, aw.reserve_redis_side_effect, aw.AGENTS["grok"])
        aw.sys.modules["fake_claim_adapter"] = types.SimpleNamespace(ask=ask)
        aw.AGENTS["grok"] = dict(aw.AGENTS["grok"], module="fake_claim_adapter")
        aw.state_path = lambda me: os.path.join(self.tmp, "." + me + "_waker_state.json")
        aw.log = lambda me, line: None
        aw.load_env = lambda: {}
        aw.read_since = lambda env, since: {"rows": [r for r in self.board if str(r[1]) > since],
                                            "total": len(self.board), "filtered": len(self.board)}

        def post_request(me, cfg, request, verbose, answers=""):
            outer.requests.append(answers)
            outer.claims_seen_at_request = outer.claims()     # None under a runner's own claim
            state, detail = outer.request_answer
            return state, (detail or aw.request_id(me, answers))
        aw.post_request = post_request

        def post_reply(me, cfg, text, to, answers, verbose, phase="DONE"):
            outer.replies.append(text)
            if isinstance(outer.reply_answer, BaseException):
                raise outer.reply_answer
            return outer.reply_answer
        aw.post_reply = post_reply

    def tearDown(self):
        (aw.state_path, aw.read_since, aw.post_reply, aw.post_request, aw.log,
         aw.load_env, aw.reserve_redis_side_effect, aw.AGENTS["grok"]) = self.real
        aw.sys.modules.pop("fake_claim_adapter", None)

    def run_pass(self):
        return aw.main(["--agent", "grok", "--max", "3", "--since-hours", "999999"])

    def claims(self):
        try:
            with open(aw.claims_path("grok"), encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None

    def state(self):
        with open(aw.state_path("grok"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_claim_is_on_disk_before_the_request_append_and_gone_after_the_reply(self):
        self.assertEqual(0, self.run_pass())
        self.assertEqual([self.SRC], self.requests, "the laptop pass now sends the request")
        claim = self.claims_seen_at_request[self.SRC]
        self.assertEqual(("posting", aw.request_id("grok", self.SRC)), (claim["phase"], claim["request"]))
        self.assertEqual({}, self.claims(), "the reply landed: the claim is cleared")
        self.assertIn(self.SRC, self.state()["answered_ids"])
        self.assertIn("redis_state=posted", self.replies[0])

    def test_an_unconfirmed_reply_leaves_the_claim_and_the_next_pass_quarantines_without_asking(self):
        self.reply_answer = False
        self.run_pass()
        self.assertIn(self.SRC, self.claims())
        self.run_pass()
        self.assertEqual((1, [self.SRC]), (len(self.model_calls), self.requests),
                         "never asked again, never requested again")
        self.assertIn(self.SRC, self.state().get("unknown_ids") or [])
        self.assertNotIn(self.SRC, self.state()["answered_ids"])
        self.assertEqual({}, self.claims())

    def test_a_reply_that_did_land_is_reconciled_as_answered(self):
        self.reply_answer = False
        self.run_pass()
        self.board.append([aw.reply_row_id("grok", self.SRC), "2026-10-11T15:05:00Z", "grok",
                           "claude-code-cli;ALL", "DONE",
                           "BCB|v=1|%s|answers=%s|evidence=STATED|" % (aw.WAKER_REPLY_MARK, self.SRC)])
        self.run_pass()
        self.assertEqual(1, len(self.model_calls))
        self.assertIn(self.SRC, self.state()["answered_ids"])
        self.assertNotIn(self.SRC, self.state().get("unknown_ids") or [])
        self.assertEqual({}, self.claims())

    def test_a_crash_after_the_request_leaves_a_claim_and_the_lock_goes_with_the_process(self):
        self.reply_answer = RuntimeError("the pass died mid-reply")
        with self.assertRaises(RuntimeError):
            self.run_pass()
        self.assertIn(self.SRC, self.claims(), "the claim reached the disk before the request")
        self.reply_answer = True
        self.assertEqual(0, self.run_pass(), "the lock did not outlive the pass")
        self.assertEqual(1, len(self.model_calls))
        self.assertIn(self.SRC, self.state().get("unknown_ids") or [])

    def test_a_not_sent_reply_after_a_refused_request_clears_the_claim_and_the_ask_is_asked_again(self):
        self.request_answer = ("refused", "refused before posting; nothing was sent")
        self.reply_answer = aw.NOT_SENT
        self.run_pass()
        self.assertEqual({}, self.claims(), "nothing of this ask reached the board")
        self.request_answer, self.reply_answer = ("posted", None), True
        self.run_pass()
        self.assertEqual(2, len(self.model_calls), "asked again on the next pass")
        self.assertIn(self.SRC, self.state()["answered_ids"])

    def test_a_not_sent_reply_after_a_posted_request_keeps_the_claim(self):
        self.reply_answer = aw.NOT_SENT
        self.run_pass()
        self.assertIn(self.SRC, self.claims(), "the request may have run")
        self.run_pass()
        self.assertEqual(1, len(self.model_calls))
        self.assertIn(self.SRC, self.state().get("unknown_ids") or [])

    def test_a_second_pass_while_one_holds_the_lock_does_nothing(self):
        held = aw._PassLock(aw.state_path("grok") + ".lock")
        self.assertTrue(held.acquire())
        try:
            self.assertEqual(0, self.run_pass())
        finally:
            held.release()
        self.assertEqual(([], []), (self.model_calls, self.requests))
        self.assertFalse(os.path.exists(aw.state_path("grok")), "no state written by the pass that yielded")

    def test_a_runner_with_its_own_claim_takes_no_lock_and_writes_no_claim_file(self):
        aw.reserve_redis_side_effect = lambda answers_id: True
        self.run_pass()
        self.assertEqual([self.SRC], self.requests)
        self.assertFalse(os.path.exists(aw.state_path("grok") + ".lock"))
        self.assertIsNone(self.claims())

    def test_an_unreadable_claim_file_stops_the_pass(self):
        with open(aw.claims_path("grok"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(1, self.run_pass())
        self.assertEqual(([], []), (self.model_calls, self.requests))


class AdapterSignature(unittest.TestCase):
    """A TypeError from inside an adapter must not buy a second model call."""

    def test_an_internal_typeerror_is_not_retried_without_the_budget(self):
        calls = []

        class Adapter(object):
            @staticmethod
            def ask(prompt, max_tokens=None):
                calls.append(max_tokens)
                raise TypeError("something inside the adapter went wrong")

        aw.sys.modules["fake_sig_adapter"] = Adapter
        try:
            text, route = aw.call_agent({"module": "fake_sig_adapter"}, "hi", 4096)
        finally:
            del aw.sys.modules["fake_sig_adapter"]
        self.assertIsNone(text)
        self.assertEqual(len(calls), 1,
                         "the model must be called ONCE and billed once; the old "
                         "TypeError fallback called it twice")

    def test_an_adapter_without_the_argument_is_detected_by_signature(self):
        calls = []

        class Adapter(object):
            @staticmethod
            def ask(prompt):
                calls.append(prompt)
                return ("ok", "route")

        aw.sys.modules["fake_plain_sig"] = Adapter
        try:
            text, _ = aw.call_agent({"module": "fake_plain_sig"}, "hi", 4096)
        finally:
            del aw.sys.modules["fake_plain_sig"]
        self.assertEqual(text, "ok")
        self.assertEqual(calls, ["hi"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

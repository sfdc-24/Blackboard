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

    def _fake_post(self, me, cfg, text, to, answers, verbose):
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

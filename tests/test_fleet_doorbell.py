"""scripts/fleet_doorbell.py: the doorbell for Grok, Codex and Gemini (O3 gap 1, owner orders 2026-10-28).

What matters: a row addressed to the agent by a whole name rings it and no other agent; its own rows, waker replies
and information rows do not; the call bridge's wake row is told apart; the bookmark moves only after printing and
never on a failed read. No network.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import fleet_doorbell as fd  # noqa: E402

SINCE = datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)


def row(ts, sender, target, payload):
    return ["id-" + ts, ts, sender, target, "APPEND", payload]


WAKE = row("2026-10-29T01:00:05Z", "bus-reconciler", "grok",
           "BCB|v=1|id=CALLBRIDGE-WAKE-call-0123456789ab|phase=ASK|from=bus-reconciler|to=grok|ask=call-0123456789ab|"
           "text=Work for Grok from the room")
TO_GROK = row("2026-10-29T01:00:06Z", "aya", "grok", "BCB|v=1|id=AYA-X|phase=ASK|to=grok|check this")
CC_GEMINI = row("2026-10-29T01:00:07Z", "aya", "claude-code-cli", "BCB|v=1|id=AYA-Y|to=claude-code-cli|cc=gemini|x")
TO_CODEX = row("2026-10-29T01:00:08Z", "grok", "aya", "BCB|v=1|id=GROK-Z|phase=ASK|to=aya|review")
OWN = row("2026-10-29T01:00:09Z", "grok", "grok", "BCB|v=1|id=GROK-SELF|to=grok|note to self")
RESULT = row("2026-10-29T01:00:10Z", "bus-reconciler", "grok", "BCB|v=1|id=R|phase=AYA_RESULT|to=grok|OK hput")
WAKER_REPLY = row("2026-10-29T01:00:11Z", "gemini", "grok;ALL", "BCB|v=1|id=G|to=grok;ALL|wakerreply=1|answer")
WA = row("2026-10-29T01:00:12Z", "whatsapp", "Blackboard Alpha DB", "Gemini, look at the plan")
SUBSTRING = row("2026-10-29T01:00:13Z", "aya", "grok-sfdc24-other", "BCB|v=1|id=S|to=grokker|not grok")
OLD = row("2026-10-29T00:50:00Z", "aya", "grok", "BCB|v=1|id=OLD|to=grok|before the bookmark")
ALL = [WAKE, TO_GROK, CC_GEMINI, TO_CODEX, OWN, RESULT, WAKER_REPLY, WA, SUBSTRING, OLD]


def ids(found):
    return [r[0] for _, r in found]


class WhoIsRung(unittest.TestCase):
    def test_each_agent_gets_only_its_rows(self):
        self.assertEqual([WAKE[0], TO_GROK[0]], ids(fd.new_rows(ALL, SINCE, "grok")))
        self.assertEqual([CC_GEMINI[0], WA[0]], ids(fd.new_rows(ALL, SINCE, "gemini")))
        self.assertEqual([TO_CODEX[0]], ids(fd.new_rows(ALL, SINCE, "codex")))

    def test_calls_only_rings_for_the_bridge_wake_row_alone(self):
        self.assertEqual([WAKE[0]], ids(fd.new_rows(ALL, SINCE, "grok", calls_only=True)))
        self.assertTrue(fd.is_wake(WAKE))
        forged = row("2026-10-29T01:00:20Z", "aya", "grok", WAKE[5])
        self.assertFalse(fd.is_wake(forged), "only the worker's row is a wake")


class Bookmark(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "bm"
        self.path.write_text(SINCE.isoformat(), encoding="utf-8")

    def test_it_moves_after_printing_and_peek_keeps_it(self):
        out = []
        self.assertEqual(2, fd.check(lambda since: ALL, "grok", path=self.path, peek=True, out=out.append))
        self.assertEqual(SINCE.isoformat(), self.path.read_text(encoding="utf-8"))
        self.assertTrue(out[0].startswith("RING "))
        self.assertEqual(2, fd.check(lambda since: ALL, "grok", path=self.path, out=out.append))
        self.assertIn("2026-10-29T01:00:06", self.path.read_text(encoding="utf-8"))
        self.assertEqual(0, fd.check(lambda since: ALL, "grok", path=self.path, out=out.append))

    def test_a_failed_read_keeps_it(self):
        def boom(since):
            raise OSError("bus down")
        out = []
        self.assertIsNone(fd.check(boom, "grok", path=self.path, out=out.append))
        self.assertEqual(SINCE.isoformat(), self.path.read_text(encoding="utf-8"))
        self.assertIn("READ FAILED", out[0])

    def test_bookmarks_are_per_agent(self):
        os.environ["FLEET_DOORBELL_BOOKMARK_DIR"] = self.dir
        try:
            self.assertNotEqual(fd.bookmark("grok"), fd.bookmark("gemini"))
        finally:
            del os.environ["FLEET_DOORBELL_BOOKMARK_DIR"]

    def test_an_unknown_agent_is_refused(self):
        self.assertEqual(2, fd.main(["--agent", "claude"]))
        self.assertEqual(2, fd.main([]))


if __name__ == "__main__":
    unittest.main()

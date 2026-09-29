"""claude-code-cli's inbox: which board rows reach the Claude Code session, and that none is skipped.

Mr. Salam, 2026-09-29: four WhatsApp messages to claude-code-cli went unanswered for 20 minutes;
"fix and poka yoke that".
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import claude_cli_inbox as inbox  # noqa: E402

T0 = datetime(2026, 9, 29, 0, 20, tzinfo=timezone.utc)


def row(ts, sender, target, payload):
    return ["id-" + ts, ts, sender, target, "APPEND", payload]


WA = row("2026-09-29T00:25:02.100Z", "whatsapp", "Blackboard Alpha DB",
         "Claude-code-cli - add following to steelworksco website")
WA_PLAIN = row("2026-09-29T00:48:24.500Z", "whatsapp", "Blackboard Alpha DB",
               "Use photos that are available without copyright or trademark for now")
TO_ME = row("2026-09-29T00:30:00Z", "grok", "vm-claude-code-cli", "BCB|v=1|id=X|task=merge PR 66")
FLEET = row("2026-09-29T00:31:00Z", "grok", "fleet", "BCB|v=1|id=Y|phase=NOTE|to=fleet|nobody waits")
WAKER_ALL = row("2026-09-29T00:32:00Z", "gemini", "grok;ALL", "BCB|v=1|id=Z|to=grok,ALL|wakerreply=1")
STANDBY = row("2026-09-29T00:31:06Z", "claude-api", "whatsapp;ALL", "BCB|v=1|to=whatsapp,ALL|answers=WRK-1")
MINE_OUT = row("2026-09-29T00:40:00Z", "claude-code-cli", "grok", "BCB|v=1|from=claude-code-cli|RESULT")
OLD = row("2026-09-29T00:10:00Z", "whatsapp", "Blackboard Alpha DB", "Claude-code-cli earlier")
JUNK = row("Open", "whatsapp", "", "a row with no time")


class InboxTest(unittest.TestCase):
    def test_his_whatsapp_rows_and_rows_naming_me_reach_me(self):
        for r in (WA, WA_PLAIN, TO_ME, FLEET):
            self.assertTrue(inbox.is_mine(r), r)

    def test_waker_copies_to_all_and_my_own_rows_do_not(self):
        for r in (WAKER_ALL, STANDBY, MINE_OUT):
            self.assertFalse(inbox.is_mine(r), r)
        codex_fleet = row("2026-09-29T00:33:00Z", "gemini", "fleet", "BCB|to=fleet")
        self.assertFalse(inbox.is_mine(codex_fleet))                    # fleet only from a lead

    def test_only_rows_newer_than_the_bookmark_oldest_first(self):
        rows = [WA_PLAIN, OLD, WA, WAKER_ALL, JUNK, TO_ME]
        got = [r for _, r in inbox.new_rows(rows, T0)]
        self.assertEqual([WA, TO_ME, WA_PLAIN], got)

    def test_the_bookmark_moves_past_printed_rows_and_never_on_a_failed_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            mark = Path(tmp) / "bookmark"
            mark.write_text(T0.isoformat(), encoding="utf-8")
            printed = []
            self.assertEqual(3, inbox.check(lambda since: [WA, TO_ME, WA_PLAIN], mark, out=printed.append))
            self.assertEqual(inbox.parse_ts("2026-09-29T00:48:24.500Z"), inbox.read_bookmark(mark))
            self.assertIn("from whatsapp", printed[0])
            self.assertEqual(0, inbox.check(lambda since: [WA, TO_ME, WA_PLAIN], mark, out=printed.append))

            def down(since):
                raise SystemExit("bus never returned rows")
            before = mark.read_text(encoding="utf-8")
            self.assertIsNone(inbox.check(down, mark, out=printed.append))
            self.assertEqual(before, mark.read_text(encoding="utf-8"))  # a failed read never moves it
            self.assertTrue(printed[-1].startswith("READ FAILED"))

    def test_peek_keeps_the_bookmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            mark = Path(tmp) / "bookmark"
            mark.write_text(T0.isoformat(), encoding="utf-8")
            self.assertEqual(1, inbox.check(lambda since: [WA], mark, peek=True, out=lambda _: None))
            self.assertEqual(T0, inbox.read_bookmark(mark))

    def test_a_row_in_the_bookmark_s_own_second_is_not_skipped(self):
        # The bookmark is a full timestamp; a later row in the same second is still new.
        with tempfile.TemporaryDirectory() as tmp:
            mark = Path(tmp) / "bookmark"
            mark.write_text("2026-09-29T00:48:24.100000+00:00", encoding="utf-8")
            self.assertEqual(1, inbox.check(lambda since: [WA_PLAIN], mark, out=lambda _: None))


if __name__ == "__main__":
    unittest.main()

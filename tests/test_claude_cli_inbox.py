"""claude-code-cli's inbox: which board rows reach the Claude Code session, and that none is skipped.

Mr. Salam, 2026-09-29: four WhatsApp messages to claude-code-cli went unanswered for 20 minutes;
"fix and poka yoke that".
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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
            self.assertEqual(inbox.parse_ts("2026-09-29T00:48:24.500Z"), inbox.read_bookmark(mark)[0])
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
            self.assertEqual(T0, inbox.read_bookmark(mark)[0])

    def test_a_row_in_the_bookmark_s_own_second_is_not_skipped(self):
        # The bookmark is a full timestamp; a later row in the same second is still new.
        with tempfile.TemporaryDirectory() as tmp:
            mark = Path(tmp) / "bookmark"
            mark.write_text("2026-09-29T00:48:24.100000+00:00", encoding="utf-8")
            self.assertEqual(1, inbox.check(lambda since: [WA_PLAIN], mark, out=lambda _: None))

    def test_rows_to_plain_claude_reach_me(self):
        # Grok's 35 conference dispatches of 2026-09-28 23:35-00:16Z were addressed this way and
        # none reached the session.
        for target in ("claude,codex,gemini,all", "gemini,claude,chatgpt-codex-desktop,codex",
                       "chatgpt-codex-desktop,codex,claude", "claude,codex"):
            self.assertTrue(inbox.is_mine(row("2026-09-28T23:43:46Z", "grok", target,
                                              "BCB|v=1|id=CONF-RERUN-RECORD-ARM|to=" + target)), target)
        by_payload = row("2026-09-28T23:44:00Z", "gemini", "Blackboard", "BCB|v=1|to=codex,Claude|cc=grok")
        self.assertTrue(inbox.is_mine(by_payload))

    def test_the_standby_and_a_mention_are_not_an_address(self):
        for target, payload in (("claude-api", "BCB|to=claude-api"), ("grok", "BCB|to=grok|claude said so"),
                                ("claudeX,codex", "BCB|to=claudeX")):
            self.assertFalse(inbox.is_mine(row("2026-09-28T23:44:00Z", "gemini", target, payload)), target)

    def test_a_lead_s_all_reaches_me_and_a_waker_s_all_does_not(self):
        self.assertTrue(inbox.is_mine(row("2026-09-28T23:44:00Z", "grok", "codex,gemini,all", "BCB|to=codex,gemini,all")))
        self.assertFalse(inbox.is_mine(WAKER_ALL))

    def test_codex_s_other_sender_tags_are_leads(self):
        # Cursor on #294: chatgpt-codex-desktop-<session> and CODEX-DESKTOP were not matched.
        for sender in ("chatgpt-codex-desktop-7f2a", "CODEX-DESKTOP", "grok-bot", "codex"):
            self.assertTrue(inbox.is_mine(row("2026-09-29T00:31:00Z", sender, "fleet", "to=fleet")), sender)


def bounded_sleep(limit=10):
    """A stand-in for time.sleep that fails the test after `limit` polls: a doorbell that never
    rings must fail, not hang (the first version's --wait never rang)."""
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) >= limit:
            raise AssertionError("the doorbell polled %d times and never rang" % limit)
    return sleep


class MainTest(unittest.TestCase):
    """main() through the bus's real shape (Cursor on #294: read_rows returns a dict, and the
    first version iterated its keys, so --wait never printed a row)."""

    def run_main(self, bookmark_text, rows, argv=()):
        asked, printed = [], []
        with tempfile.TemporaryDirectory() as tmp:
            mark = Path(tmp) / "bookmark"
            if bookmark_text is not None:
                mark.write_text(bookmark_text, encoding="utf-8")
            with mock.patch.object(inbox, "BOOKMARK", mark), \
                    mock.patch.object(inbox.bus, "load_env", return_value={}), \
                    mock.patch.object(inbox.bus, "read_rows",
                                      side_effect=lambda env, since=None: asked.append(since) or
                                      {"rows": rows, "total": 99, "filtered": len(rows), "title": "t"}), \
                    mock.patch("builtins.print", side_effect=lambda *a: printed.append(" ".join(map(str, a)))), \
                    mock.patch.object(inbox.time, "sleep", side_effect=bounded_sleep()):
                code = inbox.main(list(argv))
            after = mark.read_text(encoding="utf-8") if mark.exists() else None
        return code, asked, printed, after

    def test_main_reads_the_rows_out_of_the_bus_reply_and_moves_the_bookmark(self):
        code, asked, printed, after = self.run_main("2026-09-29T00:20:00+00:00", [WA, WAKER_ALL, WA_PLAIN])
        self.assertEqual(0, code)
        self.assertEqual(2, len(printed), printed)
        self.assertIn("steelworksco", printed[0])
        self.assertEqual(inbox.parse_ts("2026-09-29T00:48:24.500Z"), inbox.parse_ts(after))

    def test_the_bus_is_asked_from_a_whole_second_before_the_bookmark(self):
        # Cursor on #294: the bus compares text, and "...:24.500Z" sorts before "...:24Z".
        _, asked, _, _ = self.run_main("2026-09-29T00:48:24.100000+00:00", [])
        self.assertEqual(["2026-09-29T00:48:23Z"], asked)
        self.assertLess("2026-09-29T00:48:23Z", "2026-09-29T00:48:24.500Z")    # the later row passes

    def test_the_doorbell_rings_with_the_first_new_row(self):
        code, _, printed, _ = self.run_main("2026-09-29T00:20:00+00:00", [WA], argv=("--wait", "0"))
        self.assertEqual(0, code)
        self.assertIn("from whatsapp", printed[0])

    def test_a_cold_start_looks_back_rather_than_starting_now(self):
        recent = row((datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "whatsapp", "Blackboard Alpha DB", "Claude-code-cli are you there")
        code, _, printed, _ = self.run_main(None, [recent])
        self.assertEqual(0, code)
        self.assertEqual(2, len(printed), printed)                  # the cold-start line, then the row
        self.assertIn("are you there", printed[1])                 # an hour-old message still shows


    def test_a_cold_start_writes_its_baseline_before_the_first_read(self):
        # Codex on #294: a fresh "now" on every poll lost what arrived between polls.
        code, asked, printed, after = self.run_main(None, [])
        self.assertTrue(printed[0].startswith("BOOKMARK missing: starting from"), printed)
        written = inbox.parse_ts(after)
        self.assertLess(abs((datetime.now(timezone.utc) - inbox.COLD_START - written).total_seconds()), 60)
        self.assertEqual(inbox.since_arg(written), asked[0])            # asked from the written baseline

    def test_a_corrupt_bookmark_is_said_out_loud_and_recovered(self):
        code, _, printed, after = self.run_main("not a time", [])
        self.assertTrue(printed[0].startswith("BOOKMARK corrupt: 'not a time'"), printed)
        self.assertIsNotNone(inbox.parse_ts(after))

    def test_the_doorbell_keeps_one_start_across_empty_polls(self):
        replies = [[], [], [WA]]
        asked = []
        with tempfile.TemporaryDirectory() as tmp:
            mark = Path(tmp) / "bookmark"
            with mock.patch.object(inbox, "BOOKMARK", mark), \
                    mock.patch.object(inbox.bus, "load_env", return_value={}), \
                    mock.patch.object(inbox.bus, "read_rows",
                                      side_effect=lambda env, since=None: asked.append(since) or
                                      {"rows": [r for r in replies.pop(0)
                                                if inbox.parse_ts(r[1]) > inbox.parse_ts(since)],
                                       "total": 9, "filtered": 1, "title": "t"}), \
                    mock.patch("builtins.print"), \
                    mock.patch.object(inbox.time, "sleep", side_effect=bounded_sleep()):
                mark.write_text("2026-09-29T00:20:00+00:00", encoding="utf-8")
                self.assertEqual(0, inbox.main(["--wait", "0"]))
        self.assertEqual(3, len(asked))
        self.assertEqual(1, len(set(asked)))                            # the same start every poll

    def test_a_failed_bus_read_keeps_the_bookmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            mark = Path(tmp) / "bookmark"
            mark.write_text("2026-09-29T00:20:00+00:00", encoding="utf-8")
            printed = []

            def down(env, since=None):
                raise SystemExit("bus never returned rows")
            with mock.patch.object(inbox, "BOOKMARK", mark), \
                    mock.patch.object(inbox.bus, "load_env", return_value={}), \
                    mock.patch.object(inbox.bus, "read_rows", side_effect=down), \
                    mock.patch("builtins.print", side_effect=lambda *a: printed.append(" ".join(map(str, a)))):
                self.assertEqual(2, inbox.main([]))
            self.assertEqual("2026-09-29T00:20:00+00:00", mark.read_text(encoding="utf-8"))
            self.assertTrue(printed[0].startswith("READ FAILED"), printed)


if __name__ == "__main__":
    unittest.main()

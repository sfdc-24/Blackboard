#!/usr/bin/env python3
r"""The scheduled waker's doorbell, and the two ways it was silently not ringing.

run-waker.ps1 decides whether to start a model from the peek's EXIT CODE:

    if ($pk.ExitCode -ne 0 -and $pk.ExitCode -ne 10) { ... ABORT ... exit 2 }

So 10 wakes, 0 is a quiet board, and ANY other value means no model starts at
all. Both failures fixed here produced a non-0, non-10 exit, which is why
neither was visible as an error: the launcher logged ABORT and waited.

FAILURE 1 - one row from 3 September held the gate shut for ever.
    check_board returned UNKNOWN for the whole read on the first row whose
    timestamp would not parse. Index 317 of the 2,304 rows the gateway returns
    for match=claude-code-cli is VM-ONBOARD-001, written by vm-chrome on
    2026-09-03, whose Row_ID cell holds "Thursday, September 3, 2026 at 3:52 PM
    EDT" and whose TIMESTAMP cell holds the whole BCB payload. Eighteen rows in
    that read are shaped that way. They are permanent history, so the check could
    never pass again. Measured before the fix: exit 2, every invocation.

FAILURE 2 - a board UNKNOWN swallowed a message from him.
    The peek's own comment says a message from him is always worth starting a
    session for. The combined verdict then checked UNKNOWN before NEWS, so
    WhatsApp NEWS plus a broken board read produced UNKNOWN and no wake. Six
    messages he sent between 19 and 21 September sat behind it - the same silence
    check_whatsapp exists to end, re-opened one line below it.

FAILURE 3 - a payload could kill the run by containing an arrow.
    Once the peek got far enough to PRINT its fresh rows it died with
    UnicodeEncodeError on '→' against this cp1252 console. exit 1, which the
    launcher also treats as ABORT. Fixing only the first two would have moved the
    failure, not removed it.

Offline: bus_get and load_env are replaced throughout. No board, no network.

Run: python3 tests/test_waker_peek_verdict.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import board_waker as bw  # noqa: E402

GOOD_TS = "2026-09-22T10:00:00Z"


def row(rid, ts, payload="BCB|v=1|id=X|from=someone-else|ask=do a thing",
        tag="claude-code-cli"):
    """Six cells is the minimum check_board accepts."""
    return [rid, ts, tag, "claude-code-cli", "APPEND", payload]


# The real shape of the row that held the gate shut, kept verbatim in structure:
# a human-readable date in the ID cell and the entire BCB payload in the
# timestamp cell, every other column blank.
VM_ONBOARD_001 = [
    "Thursday, September 3, 2026 at 3:52 PM EDT",
    "BCB|v=1|id=VM-ONBOARD-001|phase=RESULT|from=vm-chrome|to=claude-code-cli",
    "", "", "", "", "", "", "", "", "", "",
]


def reading(rows, total=None, code=200):
    def fake_get(_env, _params):
        return code, json.dumps({"rows": rows, "total": total if total is not None
                                 else len(rows)})
    return fake_get


@contextlib.contextmanager
def board(rows, total=None, code=200):
    # A failing code is now asked again after a pause; offline tests never wait.
    with mock.patch.object(bw, "load_env", return_value={}), \
         mock.patch.object(bw, "bus_get", side_effect=reading(rows, total, code)), \
         mock.patch.object(bw, "_pause", lambda _seconds: None):
        yield


class AnUndateableRowIsNotAFailedRead(unittest.TestCase):
    def test_the_row_that_held_the_gate_shut_is_now_skipped(self):
        status, note, fresh = None, None, None
        with board([VM_ONBOARD_001, row("R-1", GOOD_TS)]):
            status, note, fresh = bw.check_board({})
        self.assertEqual(status, "NEWS", "the dateable row must still be news")
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["ts"], GOOD_TS[:19])

    def test_the_skip_is_counted_and_printed_not_swallowed(self):
        # If this number ever grows, a writer has started producing rows this
        # fleet cannot order, and that must be visible.
        with board([VM_ONBOARD_001, VM_ONBOARD_001, row("R-1", GOOD_TS)]):
            _, note, _ = bw.check_board({})
        self.assertIn("2 row(s) skipped", note)
        self.assertIn("timestamp unreadable", note)

    def test_a_board_of_nothing_but_undateable_rows_is_QUIET_not_UNKNOWN(self):
        with board([VM_ONBOARD_001]):
            status, note, fresh = bw.check_board({})
        self.assertEqual(status, "QUIET")
        self.assertEqual(fresh, [])
        self.assertIn("1 row(s) skipped", note)

    def test_every_undateable_shape_is_skipped_rather_than_fatal(self):
        for ts in ("Friday, September 4, 2026 at 2:04 AM EDT",
                   "2026-09-08 5:12 AM EDT",
                   "Timestamp",
                   "BCB|v=1|id=X|phase=RESULT",
                   "2026-09-22T10:00:00+04:00",
                   "2026-13-45T99:99:99Z",
                   ""):
            with self.subTest(ts=ts):
                with board([row("R-bad", ts), row("R-ok", GOOD_TS)]):
                    status, note, fresh = bw.check_board({})
                self.assertEqual(status, "NEWS")
                self.assertEqual(len(fresh), 1)
                self.assertIn("1 row(s) skipped", note)

    def test_a_noncanonical_offset_is_skipped_because_the_cursor_is_lexical(self):
        # Not a cosmetic case. The watermark is compared lexically, so an offset
        # timestamp cannot be ordered against it and must not be counted.
        with board([row("R-off", "2026-09-22T10:00:00-04:00")]):
            status, _, fresh = bw.check_board({})
        self.assertEqual(fresh, [])
        self.assertEqual(status, "QUIET")

    def test_a_clean_board_says_nothing_about_skipping(self):
        with board([row("R-1", GOOD_TS)]):
            _, note, _ = bw.check_board({})
        self.assertNotIn("skipped", note)


class UNKNOWNStillMeansTheReadDidNotHappen(unittest.TestCase):
    """The fix must not turn a genuinely failed read into a cheerful QUIET."""

    def test_a_non_200_is_UNKNOWN(self):
        with board([row("R-1", GOOD_TS)], code=503):
            status, _, _ = bw.check_board({})
        self.assertEqual(status, "UNKNOWN")

    def test_a_health_blob_with_no_rows_is_UNKNOWN(self):
        # The gateway answers a read missing `title` with ok/service/time and no
        # rows, at HTTP 200 - indistinguishable from an empty board unless this
        # check holds.
        def fake_get(_env, _params):
            return 200, json.dumps({"ok": True, "service": "bus",
                                    "time": GOOD_TS})
        with mock.patch.object(bw, "load_env", return_value={}), \
             mock.patch.object(bw, "bus_get", side_effect=fake_get):
            status, _, _ = bw.check_board({})
        self.assertEqual(status, "UNKNOWN")

    def test_unparseable_json_is_UNKNOWN(self):
        def fake_get(_env, _params):
            return 200, "<html>proxy interstitial</html>"
        with mock.patch.object(bw, "load_env", return_value={}), \
             mock.patch.object(bw, "bus_get", side_effect=fake_get), \
             mock.patch.object(bw, "_pause", lambda _seconds: None):
            status, _, _ = bw.check_board({})
        self.assertEqual(status, "UNKNOWN")

    def test_a_row_that_is_not_a_row_is_still_UNKNOWN(self):
        for bad in ("a string, not a row", 42, None, ["too", "few"], {}):
            with self.subTest(bad=bad):
                with board([bad]):
                    status, _, _ = bw.check_board({})
                self.assertEqual(status, "UNKNOWN")

    def test_a_response_reporting_failure_is_UNKNOWN(self):
        def fake_get(_env, _params):
            return 200, json.dumps({"ok": False, "rows": [], "error": "nope"})
        with mock.patch.object(bw, "load_env", return_value={}), \
             mock.patch.object(bw, "bus_get", side_effect=fake_get):
            status, _, _ = bw.check_board({})
        self.assertEqual(status, "UNKNOWN")


class HisMessageOutranksABrokenBoardRead(unittest.TestCase):
    """The peek's exit code is the doorbell. 10 wakes; anything but 0 aborts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_state = bw.STATE
        bw.STATE = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        bw.STATE = self.real_state
        self.tmp.cleanup()

    def peek(self, board_verdict, wa_verdict, wa_fresh=(), fresh=()):
        buf = io.StringIO()
        with mock.patch.object(bw, "check_board",
                              return_value=(board_verdict, "board note", list(fresh))), \
             mock.patch.object(bw, "check_whatsapp",
                              return_value=(wa_verdict, "wa note", list(wa_fresh))), \
             mock.patch.object(sys, "argv", ["board_waker.py", "--peek"]), \
             contextlib.redirect_stdout(buf):
            code = bw.main()
        return code, buf.getvalue()

    HIS_MESSAGE = [{"ts": "2026-09-21T01:53:06", "text": "Claude-code-cli update me"}]

    def test_his_message_wakes_a_session_even_when_the_board_read_failed(self):
        code, out = self.peek("UNKNOWN", "NEWS", wa_fresh=self.HIS_MESSAGE)
        self.assertEqual(code, 10, "exit 10 is the only value that starts a model")
        self.assertTrue(out.startswith("NEWS"), out.splitlines()[:1])
        self.assertIn("update me", out)

    def test_board_news_wakes_a_session_even_when_whatsapp_is_unreadable(self):
        code, out = self.peek("NEWS", "UNKNOWN",
                              fresh=[{"ts": GOOD_TS, "payload": "something for me"}])
        self.assertEqual(code, 10)

    def test_unknown_still_wins_when_neither_lane_has_news(self):
        # A genuinely failed read must not advance a cursor, and there is nothing
        # to lose by waiting for the next tick.
        code, out = self.peek("UNKNOWN", "QUIET")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("UNKNOWN"))
        self.assertNotIn("ADVANCE_THROUGH", out,
                         "a failed read must never print a cutoff")

    def test_two_quiet_lanes_cost_nothing(self):
        code, out = self.peek("QUIET", "QUIET")
        self.assertEqual(code, 0)
        self.assertIn("ADVANCE_THROUGH", out)

    def test_a_successful_board_read_prints_a_cutoff(self):
        """The cutoff is a promise that every row up to that instant was seen."""
        for board_verdict in ("NEWS", "QUIET"):
            with self.subTest(board=board_verdict):
                _, out = self.peek(board_verdict, "QUIET",
                                   fresh=[{"ts": GOOD_TS, "payload": "for me"}]
                                   if board_verdict == "NEWS" else ())
                self.assertIn("ADVANCE_THROUGH", out)

    def test_a_failed_board_read_issues_no_cutoff_even_when_waking(self):
        """The rule my first attempt got wrong, and an older test caught.

        Waking and advancing are different promises. His message is reason
        enough to start a session; it is not evidence that the board was read.
        Printing a cutoff here would move a cursor past rows nothing has seen.
        """
        code, out = self.peek("UNKNOWN", "NEWS", wa_fresh=self.HIS_MESSAGE)
        self.assertEqual(code, 10, "he still gets a session")
        self.assertNotIn("ADVANCE_THROUGH", out, "and the board cursor stays put")

    def test_the_verdict_line_is_the_first_line_and_matches_the_launchers_regex(self):
        # run-waker.ps1 greps for ^(NEWS|QUIET|UNKNOWN)\s and ABORTS if no line
        # matches, so the format is a contract, not a display choice.
        import re
        for b, w in (("QUIET", "QUIET"), ("UNKNOWN", "QUIET"), ("UNKNOWN", "NEWS"),
                     ("NEWS", "QUIET")):
            with self.subTest(board=b, wa=w):
                _, out = self.peek(b, w, wa_fresh=self.HIS_MESSAGE)
                first = out.splitlines()[0]
                self.assertRegex(first, r"^(NEWS|QUIET|UNKNOWN)\s")


class TheAdvancePathStaysStrict(unittest.TestCase):
    """The peek got laxer. The call that moves a cursor did not.

    This is the half of the change that is a deliberate NON-change, and it is
    here because a mutant flipping strict=True to strict=False on the advance
    path survived this suite while passing everything else.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_state = bw.STATE
        bw.STATE = Path(self.tmp.name) / "state.json"
        bw.save_state({"watermark": "2026-09-21T00:00:00Z"})

    def tearDown(self):
        bw.STATE = self.real_state
        self.tmp.cleanup()

    def test_strict_makes_an_undateable_row_fatal_again(self):
        with board([VM_ONBOARD_001, row("R-1", GOOD_TS)]):
            status, note, fresh = bw.check_board({}, strict=True)
        self.assertEqual(status, "UNKNOWN")
        self.assertEqual(fresh, [])
        self.assertIn("timestamp is invalid", note)

    def test_the_same_read_is_NEWS_without_strict(self):
        """The two contracts, side by side, on one board."""
        with board([VM_ONBOARD_001, row("R-1", GOOD_TS)]):
            lax, _, lax_fresh = bw.check_board({})
            strict, _, strict_fresh = bw.check_board({}, strict=True)
        self.assertEqual((lax, len(lax_fresh)), ("NEWS", 1))
        self.assertEqual((strict, len(strict_fresh)), ("UNKNOWN", 0))

    def test_a_repairable_timestamp_still_blocks_an_advance(self):
        # These are the cases that justify keeping strictness: a Feb 30 and an
        # unpadded month are writer bugs, not permanent history. If the writer is
        # fixed the rows become readable, and a cursor already past them has lost
        # them.
        for ts in ("2026-02-30T00:01:00Z", "2026-9-21T00:01:00Z", "bad"):
            with self.subTest(ts=ts):
                with board([row("R-x", ts)]):
                    status, _, _ = bw.check_board({}, strict=True)
                self.assertEqual(status, "UNKNOWN")

    def test_the_advance_command_refuses_and_saves_nothing(self):
        before = bw.STATE.read_bytes()
        with board([VM_ONBOARD_001]), \
             mock.patch.object(bw, "save_state") as save, \
             mock.patch.object(sys, "argv", ["board_waker.py", "--advance",
                                             "--advance-through",
                                             "2026-09-22T00:00:00Z"]):
            self.assertEqual(bw.main(), 2)
        save.assert_not_called()
        self.assertEqual(bw.STATE.read_bytes(), before)

    def test_a_clean_board_still_advances(self):
        # The refusal must be about undateable rows, not about advancing at all.
        with board([row("R-1", "2026-09-21T00:01:00Z")]), \
             mock.patch.object(sys, "argv", ["board_waker.py", "--advance",
                                             "--advance-through",
                                             "2026-09-21T00:01:29Z"]):
            self.assertEqual(bw.main(), 0)
        self.assertEqual(bw.load_state()["watermark"], "2026-09-21T00:01:29Z")


class APayloadCannotKillTheRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_state = bw.STATE
        bw.STATE = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        bw.STATE = self.real_state
        self.tmp.cleanup()

    def test_an_unmappable_character_does_not_end_the_peek(self):
        r"""The measured crash: '→' in a row, cp1252 console, exit 1.

        Written against a stream that genuinely cannot encode the character,
        rather than against the test runner's UTF-8 stdout - which would have
        passed without the fix and proved nothing.
        """
        payload = "BCB|v=1|id=X|ask=use the arrow → here, and │ too"
        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict",
                               write_through=True)
        with mock.patch.object(bw, "check_board",
                              return_value=("NEWS", "note",
                                            [{"ts": GOOD_TS, "payload": payload}])), \
             mock.patch.object(bw, "check_whatsapp",
                              return_value=("QUIET", "none", [])), \
             mock.patch.object(sys, "argv", ["board_waker.py", "--peek"]), \
             mock.patch.object(sys, "stdout", buf):
            code = bw.main()
        self.assertEqual(code, 10)

    def test_the_negative_control_confirms_that_stream_really_cannot_encode_it(self):
        """Without the fix the test above must fail. Prove the stream is strict."""
        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict",
                               write_through=True)
        with self.assertRaises(UnicodeEncodeError):
            buf.write("→")
            buf.flush()

    def test_a_stream_that_cannot_be_reconfigured_is_not_fatal(self):
        # Pipes, captures and test doubles are not ours to reconfigure, and
        # printing is not worth crashing over either way.
        class Stubborn:
            def reconfigure(self, **kw):
                raise OSError("not a real stream")

            def write(self, s):
                return len(s)

            def flush(self):
                pass

        with mock.patch.object(sys, "stdout", Stubborn()), \
             mock.patch.object(sys, "stderr", Stubborn()):
            bw._make_output_unkillable()   # must not raise


GOOGLE_404 = "<!DOCTYPE html><html><title>Page Not Found</title></html>"


class AFlappingGatewayIsAskedAgain(unittest.TestCase):
    """Cloud soak, 2026-09-24: 4 of 25 shadow runs were UNKNOWN on a single 404
    page for both reads. A read that comes back as a page is retried; one that
    never recovers is still UNKNOWN."""

    def run_check(self, check, answers):
        calls, pauses = [], []

        def fake_get(_env, params):
            calls.append(params)
            return answers[min(len(calls), len(answers)) - 1]
        with mock.patch.object(bw, "load_env", return_value={}), \
             mock.patch.object(bw, "bus_get", side_effect=fake_get), \
             mock.patch.object(bw, "_pause", side_effect=pauses.append):
            result = check({})
        return result, calls, pauses

    def good(self, rows):
        return 200, json.dumps({"rows": rows, "total": len(rows)})

    def test_a_404_page_then_data_is_the_data(self):
        (status, _, fresh), calls, pauses = self.run_check(
            bw.check_board, [(404, GOOGLE_404), self.good([row("R-1", GOOD_TS)])])
        self.assertEqual(status, "NEWS")
        self.assertEqual(len(fresh), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(pauses, [3.0])

    def test_a_gateway_that_never_recovers_is_still_UNKNOWN(self):
        (status, note, fresh), calls, pauses = self.run_check(
            bw.check_board, [(404, GOOGLE_404)])
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("HTTP 404", note)
        self.assertEqual(fresh, [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(pauses, [3.0, 8.0], "no pause after the last attempt")

    def test_a_200_page_that_is_not_json_is_retried_too(self):
        (status, _, _), calls, _ = self.run_check(
            bw.check_board, [(200, GOOGLE_404), self.good([])])
        self.assertEqual(status, "QUIET")
        self.assertEqual(len(calls), 2)

    def test_the_first_good_answer_is_not_asked_twice(self):
        (status, _, _), calls, pauses = self.run_check(
            bw.check_board, [self.good([row("R-1", GOOD_TS)])])
        self.assertEqual(status, "NEWS")
        self.assertEqual((len(calls), pauses), (1, []))

    def test_whatsapp_is_asked_again_as_well(self):
        wa = ["WA-1", GOOD_TS, "whatsapp", "", "OPEN", "Claude-code-cli are you there"]
        (status, _, fresh), calls, pauses = self.run_check(
            bw.check_whatsapp, [(404, GOOGLE_404), self.good([wa])])
        self.assertEqual(status, "NEWS")
        self.assertEqual(len(fresh), 1)
        self.assertEqual((len(calls), pauses), (2, [3.0]))

    def test_whatsapp_that_never_recovers_is_still_UNKNOWN(self):
        (status, note, _), calls, _ = self.run_check(
            bw.check_whatsapp, [(404, GOOGLE_404)])
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("HTTP 404", note)
        self.assertEqual(len(calls), 3)

    def test_a_failed_read_says_how_long_each_attempt_took(self):
        ticks = iter([0.0, 31.5, 40.0, 70.25, 80.0, 81.0])
        with mock.patch.object(bw, "_clock", lambda: next(ticks)):
            (status, note, _), calls, _ = self.run_check(
                bw.check_board, [(404, GOOGLE_404)])
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("board read failed (HTTP 404); attempts: "
                      "404 in 31.5s, 404 in 30.2s, 404 in 1.0s", note)

    def test_whatsapp_failure_notes_carry_the_attempts_too(self):
        (status, note, _), _, _ = self.run_check(bw.check_whatsapp, [(404, GOOGLE_404)])
        self.assertEqual(status, "UNKNOWN")
        self.assertRegex(note, r"\(HTTP 404\); attempts: 404 in [0-9.]+s, 404 in [0-9.]+s, 404 in [0-9.]+s$")

    def test_a_recovered_read_leaves_only_its_own_attempts(self):
        (status, _, _), _, _ = self.run_check(
            bw.check_board, [(404, GOOGLE_404), self.good([row("R-1", GOOD_TS)])])
        self.assertEqual(status, "NEWS")
        self.assertEqual([c for c, _ in bw.last_read_attempts], [404, 200])

    def test_a_transport_exception_is_not_retried(self):
        calls = []

        def boom(_env, _params):
            calls.append(1)
            raise TimeoutError("read timed out")
        with mock.patch.object(bw, "load_env", return_value={}), \
             mock.patch.object(bw, "bus_get", side_effect=boom), \
             mock.patch.object(bw, "_pause", side_effect=AssertionError("no pause")):
            status, _, _ = bw.check_board({})
        self.assertEqual(status, "UNKNOWN")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Tests for scripts/pi1_wake.py: the Pi's doorbell rings for its own new rows, once, and not for
a fortnight of old ones.

Nothing on the board can push to a Raspberry Pi behind a home NAT, so the Pi pulls. What it must
not do is what the WhatsApp outbox did at 23:58Z on 2026-10-03: deliver everything that had piled
up since Sep 21 the first time anybody ran it.

Run: python3 -m unittest tests.test_pi1_wake
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import pi1_wake as pw                                                   # noqa: E402


def row(rid, source="grok", target="pi1-cli", payload=None, ts=None, action="DISPATCH"):
    when = ts or pw.stamp(pw.now())
    body = payload or ("BCB|v=1|id=%s|phase=DISPATCH|from=%s|to=pi1-cli|task=join the line" %
                       (rid, source))
    return [rid, when, source, target, action, body]


def done(code=0):
    return mock.Mock(returncode=code)


class Addressing(unittest.TestCase):
    def test_a_row_addressed_to_the_pi_is_a_ring(self):
        self.assertTrue(pw.for_me(row("R1"), "pi1-cli"))

    def test_a_row_addressed_to_everyone_is_a_ring(self):
        self.assertTrue(pw.for_me(row("R2", target="ALL"), "pi1-cli"))

    def test_its_own_rows_are_not(self):
        self.assertFalse(pw.for_me(row("R3", source="pi1-cli"), "pi1-cli"))

    def test_another_agents_row_is_not(self):
        self.assertFalse(pw.for_me(row("R4", target="gemini", payload="BCB|v=1|id=R4|to=gemini|x"),
                                   "pi1-cli"))

    def test_a_waker_reply_is_not_a_ring(self):
        reply = row("R5", source="gemini", target="pi1-cli",
                    payload=("BCB|v=1|id=R5|phase=DONE|from=gemini|to=pi1-cli|wakerreply=1|"
                             "answers=WRK-1|evidence=STATED|Answered by the waker."))
        self.assertFalse(pw.for_me(reply, "pi1-cli"))

    def test_a_short_row_is_not_a_ring(self):
        self.assertFalse(pw.for_me(["R6", pw.stamp(pw.now())], "pi1-cli"))


class Passes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.inbox = Path(self.tmp.name) / "inbox.jsonl"
        self.env = {"BUS_URL": "https://example.invalid", "BUS_SECRET": "x"}

    def pass_with(self, rows, state=None, **kw):
        ran = mock.Mock(return_value=done(kw.pop("exit_code", 0)))
        with mock.patch.object(pw, "rows_since", return_value=rows):
            rc = pw.one_pass(self.env, state if state is not None else self.saved(),
                             me="pi1-cli", cmd=kw.pop("cmd", "wake.sh"), inbox=self.inbox,
                             state_path=self.state_path,
                             max_age_hours=kw.pop("max_age_hours", 24.0),
                             dry_run=kw.pop("dry_run", False), run=ran)
        return rc, ran

    def saved(self):
        return pw.load_state(self.state_path)

    def inbox_ids(self):
        if not self.inbox.exists():
            return []
        return [json.loads(l)["row_id"] for l in
                self.inbox.read_text(encoding="utf-8").splitlines() if l.strip()]

    # --- the first run -------------------------------------------------------------------------
    def test_the_first_run_primes_and_rings_nothing(self):
        rc, ran = self.pass_with([row("OLD-1"), row("OLD-2")], state={})
        self.assertEqual(0, rc)
        ran.assert_not_called()
        saved = self.saved()
        self.assertEqual(["OLD-1", "OLD-2"], saved["seen_row_ids"])
        self.assertTrue(saved.get("primed_at"))
        self.assertEqual([], self.inbox_ids())

    def test_after_priming_a_new_row_rings_once(self):
        self.pass_with([row("OLD-1")], state={})
        rc, ran = self.pass_with([row("OLD-1"), row("NEW-1")])
        self.assertEqual(0, rc)
        self.assertEqual(1, ran.call_count)
        self.assertEqual(["NEW-1"], self.inbox_ids())
        rc, again = self.pass_with([row("OLD-1"), row("NEW-1")])
        again.assert_not_called()
        self.assertEqual(["NEW-1"], self.inbox_ids(), "the same row rang twice")

    def test_one_wake_for_a_pass_however_many_rows(self):
        self.pass_with([], state={})
        rc, ran = self.pass_with([row("N1"), row("N2"), row("N3")])
        self.assertEqual(1, ran.call_count)
        self.assertEqual(["N1", "N2", "N3"], self.inbox_ids())
        self.assertEqual("3", ran.call_args.kwargs["env"]["PI1_WAKE_ROWS"])
        self.assertEqual(str(self.inbox), ran.call_args.kwargs["env"]["PI1_WAKE_INBOX"])

    # --- old news -----------------------------------------------------------------------------
    def test_a_row_older_than_the_limit_is_recorded_and_does_not_ring(self):
        old = pw.stamp(pw.now() - timedelta(hours=72))
        self.pass_with([], state={})
        rc, ran = self.pass_with([row("STALE", ts=old)])
        self.assertEqual(0, rc)
        ran.assert_not_called()
        self.assertIn("STALE", self.saved()["seen_row_ids"])
        self.assertEqual([], self.inbox_ids())

    def test_a_timestamp_that_cannot_be_read_counts_as_old(self):
        self.assertIsNone(pw.read_ts("BCB|v=1|id=the-payload-went-in-the-timestamp-column"))
        self.pass_with([], state={})
        rc, ran = self.pass_with([row("ODD", ts="BCB|v=1|id=x|phase=NOTE")])
        ran.assert_not_called()
        self.assertIn("ODD", self.saved()["seen_row_ids"])

    def test_the_age_limit_can_be_turned_off(self):
        old = pw.stamp(pw.now() - timedelta(hours=72))
        self.pass_with([], state={})
        rc, ran = self.pass_with([row("STALE", ts=old)], max_age_hours=0)
        self.assertEqual(1, ran.call_count)

    # --- a failed wake ------------------------------------------------------------------------
    def test_a_failed_wake_holds_the_watermark_and_rings_again(self):
        self.pass_with([], state={})
        before = self.saved()["watermark"]
        rc, ran = self.pass_with([row("N1")], exit_code=3)
        self.assertEqual(1, rc)
        self.assertEqual(1, ran.call_count)
        saved = self.saved()
        self.assertEqual(before, saved["watermark"], "a failed wake moved the watermark")
        self.assertNotIn("N1", saved["seen_row_ids"], "a failed wake was recorded as handled")
        rc, again = self.pass_with([row("N1")])
        self.assertEqual(1, again.call_count, "the row did not ring again")

    def test_a_wake_command_that_cannot_start_is_a_failure_not_a_pass(self):
        self.pass_with([], state={})
        with mock.patch.object(pw, "rows_since", return_value=[row("N1")]):
            rc = pw.one_pass(self.env, self.saved(), me="pi1-cli", cmd="wake.sh",
                             inbox=self.inbox, state_path=self.state_path, max_age_hours=24.0,
                             dry_run=False, run=mock.Mock(side_effect=OSError("no such file")))
        self.assertEqual(1, rc)
        self.assertNotIn("N1", self.saved()["seen_row_ids"])

    # --- the quiet paths ----------------------------------------------------------------------
    def test_no_command_queues_the_row_and_still_advances(self):
        self.pass_with([], state={})
        rc, ran = self.pass_with([row("N1")], cmd="")
        self.assertEqual(0, rc)
        ran.assert_not_called()
        self.assertEqual(["N1"], self.inbox_ids())
        self.assertIn("N1", self.saved()["seen_row_ids"])

    def test_a_dry_run_touches_neither_the_state_nor_the_command(self):
        self.pass_with([], state={})
        before = self.state_path.read_text(encoding="utf-8")
        rc, ran = self.pass_with([row("N1")], dry_run=True)
        self.assertEqual(0, rc)
        ran.assert_not_called()
        self.assertEqual(before, self.state_path.read_text(encoding="utf-8"))
        self.assertEqual([], self.inbox_ids())

    def test_the_state_file_never_holds_crlf(self):
        self.pass_with([row("OLD-1")], state={})
        self.assertNotIn(b"\r\n", self.state_path.read_bytes())

    def test_the_seen_list_is_capped(self):
        state = {"watermark": pw.stamp(pw.now()), "seen_row_ids": ["x%d" % i for i in range(600)]}
        pw.save_state(state, self.state_path)
        self.assertEqual(pw.SEEN_CAP, len(self.saved()["seen_row_ids"]))


class TheReadIsFiltered(unittest.TestCase):
    def test_it_asks_only_for_rows_since_its_watermark_minus_the_overlap(self):
        when = pw.now()
        with mock.patch.object(pw.bus, "read_rows", return_value={"rows": []}) as read:
            pw.rows_since({"BUS_URL": "u", "BUS_SECRET": "s"}, when)
        asked = read.call_args.kwargs["since"]
        self.assertEqual(pw.stamp(when - timedelta(seconds=pw.OVERLAP_SECONDS)), asked)

    def test_no_watermark_asks_for_no_since(self):
        with mock.patch.object(pw.bus, "read_rows", return_value={"rows": []}) as read:
            pw.rows_since({"BUS_URL": "u", "BUS_SECRET": "s"}, None)
        self.assertIsNone(read.call_args.kwargs["since"])

    def test_a_bare_list_from_the_bus_is_accepted_too(self):
        with mock.patch.object(pw.bus, "read_rows", return_value=[row("R1"), "junk"]):
            rows = pw.rows_since({"BUS_URL": "u", "BUS_SECRET": "s"}, None)
        self.assertEqual(["R1"], [r[0] for r in rows])


if __name__ == "__main__":
    unittest.main()

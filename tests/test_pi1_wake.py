#!/usr/bin/env python3
"""Tests for scripts/pi1_wake.py: the Pi's doorbell rings for its own new rows, once each, and
never for a fortnight of old ones.

Nothing on the board can push to a Raspberry Pi behind a home NAT, so the Pi pulls. The controls
below are in two groups: what it must do, and the four things Codex found it doing wrong on head
0a440cc (NO-GO, CODEX-PI-WA-QC-RESULT-20261004T012631Z) - a cursor that skipped rows arriving
during a slow wake, a corrupt cursor that silently primed pending work away, two inbox entries for
one row across a failed then successful wake, and a genuine ALL row missed because the fixture
meant to test ALL also carried `to=pi1-cli`.

Run: python3 -m unittest tests.test_pi1_wake
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import pi1_wake as pw                                                   # noqa: E402

T0 = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


def row(rid, source="grok", target="pi1-cli", payload=None, ts=None, action="DISPATCH"):
    when = ts or pw.stamp(T0)
    body = payload or ("BCB|v=1|id=%s|phase=DISPATCH|from=%s|to=pi1-cli|task=join the line" %
                       (rid, source))
    return [rid, when, source, target, action, body]


def everyone_row(rid, source="grok"):
    """A GENUINE row to the whole fleet: it names nobody, which is the point."""
    return [rid, pw.stamp(T0), source, "ALL", "DISPATCH",
            "BCB|v=1|id=%s|phase=DISPATCH|from=%s|to=ALL|task=everyone on the line at the hour"
            % (rid, source)]


def done(code=0):
    return mock.Mock(returncode=code)


class Clock:
    """A clock the test moves, so "before the read" and "after the work" are distinguishable."""

    def __init__(self, start=T0):
        self.at = start

    def __call__(self):
        return self.at

    def tick(self, seconds):
        self.at = self.at + timedelta(seconds=seconds)


class Addressing(unittest.TestCase):
    def test_a_row_addressed_to_the_pi_is_a_ring(self):
        self.assertTrue(pw.for_me(row("R1"), "pi1-cli"))

    def test_a_genuine_row_to_everyone_is_a_ring(self):
        """Target ALL, payload to=ALL, this tag named nowhere. agent_waker.addressed_to answers for
        a NAMED tag and said no, which is Codex's fourth finding: ALL was missed."""
        fleet = everyone_row("R2")
        self.assertFalse(pw.aw.addressed_to(fleet, "pi1-cli"),
                         "if the fleet predicate starts matching ALL, drop to_everyone()")
        self.assertTrue(pw.to_everyone(fleet))
        self.assertTrue(pw.for_me(fleet, "pi1-cli"))

    def test_everyone_in_the_cast_field_too(self):
        cast = ["R3", pw.stamp(T0), "grok", "gemini", "SCHEDULE",
                "BCB|v=1|id=R3|phase=SCHEDULE|from=grok|cast=ALL|start=2026-10-04T18:00:00Z"]
        self.assertTrue(pw.to_everyone(cast))
        self.assertTrue(pw.for_me(cast, "pi1-cli"))

    def test_its_own_row_to_everyone_is_still_not_a_ring(self):
        self.assertFalse(pw.for_me(everyone_row("R4", source="pi1-cli"), "pi1-cli"))

    def test_a_waker_reply_to_everyone_is_still_not_a_ring(self):
        reply = ["R5", pw.stamp(T0), "gemini", "ALL", "DONE",
                 "BCB|v=1|id=R5|phase=DONE|from=gemini|to=ALL|wakerreply=1|answers=WRK-1|"
                 "evidence=STATED|Answered by the waker."]
        self.assertFalse(pw.for_me(reply, "pi1-cli"))

    def test_another_agents_row_is_not_a_ring(self):
        self.assertFalse(pw.for_me(row("R6", target="gemini",
                                       payload="BCB|v=1|id=R6|to=gemini|x"), "pi1-cli"))

    def test_its_own_rows_are_not(self):
        self.assertFalse(pw.for_me(row("R7", source="pi1-cli"), "pi1-cli"))

    def test_a_short_row_is_not_a_ring(self):
        self.assertFalse(pw.for_me(["R8", pw.stamp(T0)], "pi1-cli"))
        self.assertFalse(pw.to_everyone(["R8", pw.stamp(T0)]))


class Passes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.inbox = Path(self.tmp.name) / "inbox.jsonl"
        self.env = {"BUS_URL": "https://example.invalid", "BUS_SECRET": "x"}
        self.clock = Clock()
        patched = mock.patch.object(pw, "now", self.clock)
        patched.start()
        self.addCleanup(patched.stop)

    def pass_with(self, rows, state=None, **kw):
        code = kw.pop("exit_code", 0)
        ticks = kw.pop("wake_takes", 0)

        def ran(*a, **k):
            if ticks:
                self.clock.tick(ticks)
            return done(code)

        run = mock.Mock(side_effect=ran)
        with mock.patch.object(pw, "rows_since", return_value=rows):
            rc = pw.one_pass(self.env, state if state is not None else self.saved(),
                             me="pi1-cli", cmd=kw.pop("cmd", "wake.sh"), inbox=self.inbox,
                             state_path=self.state_path,
                             max_age_hours=kw.pop("max_age_hours", 24.0),
                             dry_run=kw.pop("dry_run", False), limit=kw.pop("limit", None),
                             run=run)
        return rc, run

    def saved(self):
        return pw.load_state(self.state_path)

    def inbox_ids(self):
        if not self.inbox.exists():
            return []
        return [json.loads(l)["row_id"] for l in
                self.inbox.read_text(encoding="utf-8").splitlines() if l.strip()]

    # --- the first run -------------------------------------------------------------------------
    def test_the_first_run_primes_and_rings_nothing(self):
        rc, run = self.pass_with([row("OLD-1"), row("OLD-2")], state={})
        self.assertEqual(0, rc)
        run.assert_not_called()
        saved = self.saved()
        self.assertEqual(["OLD-1", "OLD-2"], saved["seen_row_ids"])
        self.assertEqual(pw.stamp(T0), saved["watermark"])
        self.assertEqual([], self.inbox_ids())

    def test_after_priming_a_new_row_rings_once(self):
        self.pass_with([row("OLD-1")], state={})
        rc, run = self.pass_with([row("OLD-1"), row("NEW-1")])
        self.assertEqual(0, rc)
        self.assertEqual(1, run.call_count)
        self.assertEqual(["NEW-1"], self.inbox_ids())
        rc, again = self.pass_with([row("OLD-1"), row("NEW-1")])
        again.assert_not_called()
        self.assertEqual(["NEW-1"], self.inbox_ids(), "the same row rang twice")

    def test_one_wake_for_a_pass_however_many_rows(self):
        self.pass_with([], state={})
        rc, run = self.pass_with([row("N1"), row("N2"), row("N3")])
        self.assertEqual(1, run.call_count)
        self.assertEqual(["N1", "N2", "N3"], self.inbox_ids())
        self.assertEqual("3", run.call_args.kwargs["env"]["PI1_WAKE_ROWS"])
        self.assertEqual(str(self.inbox), run.call_args.kwargs["env"]["PI1_WAKE_INBOX"])

    def test_the_row_itself_never_reaches_the_wake_command(self):
        """Codex's R2: a notice is not task authority. Only the count and the path are exported."""
        self.pass_with([], state={})
        rc, run = self.pass_with([row("N1", payload="BCB|v=1|id=N1|to=pi1-cli|task=rm -rf /")])
        exported = run.call_args.kwargs["env"]
        self.assertNotIn("rm -rf", json.dumps({k: v for k, v in exported.items()
                                               if k.startswith("PI1_")}))
        self.assertEqual("wake.sh", run.call_args.args[0])

    # --- Codex 1: a covered-read cursor -------------------------------------------------------
    def test_a_row_arriving_during_a_slow_wake_is_not_skipped(self):
        """The watermark is the READ's moment, not the clock after the work. With the old code a
        five-minute wake moved the cursor past everything that arrived while it ran."""
        self.pass_with([], state={})
        read_at = self.clock.at
        rc, run = self.pass_with([row("N1")], wake_takes=300)
        self.assertEqual(0, rc)
        self.assertEqual(1, run.call_count)
        self.assertEqual(pw.stamp(read_at), self.saved()["watermark"],
                         "the cursor moved to after the wake and would skip its arrivals")
        during = row("DURING", ts=pw.stamp(read_at + timedelta(seconds=120)))
        rc, again = self.pass_with([row("N1"), during])
        self.assertEqual(1, again.call_count)
        self.assertIn("DURING", self.inbox_ids())

    def test_a_capped_read_holds_the_cursor(self):
        """As many rows as the limit allows is not evidence that the window was covered."""
        self.pass_with([], state={})
        before = self.saved()["watermark"]
        rc, run = self.pass_with([row("N1"), row("N2")], limit=2)
        self.assertEqual(0, rc)
        self.assertEqual(1, run.call_count, "a capped read must still ring for what it saw")
        self.assertEqual(before, self.saved()["watermark"], "a capped read moved the cursor")
        self.assertEqual({"N1", "N2"}, set(self.saved()["seen_row_ids"]) & {"N1", "N2"},
                         "a capped read must still record what it handled, or it rings twice")

    def test_a_read_that_returns_fewer_than_the_limit_is_covered(self):
        self.pass_with([], state={})
        read_at = self.clock.at
        rc, run = self.pass_with([row("N1")], limit=5)
        self.assertEqual(pw.stamp(read_at), self.saved()["watermark"])

    # --- Codex 2: an unreadable cursor fails closed -------------------------------------------
    def test_a_corrupt_cursor_refuses_and_touches_nothing(self):
        self.state_path.write_text('{"watermark": "2026-10-04T12:00:00Z", "seen_row_i',
                                   encoding="utf-8")
        before = self.state_path.read_text(encoding="utf-8")
        with self.assertRaises(pw.StateUnreadable):
            pw.load_state(self.state_path)
        run = mock.Mock(return_value=done(0))
        with mock.patch.object(pw.bus, "load_env", return_value=self.env), \
             mock.patch.object(pw, "rows_since", return_value=[row("N1")]), \
             mock.patch.object(pw.subprocess, "run", run):
            rc = pw.main(["--state", str(self.state_path), "--inbox", str(self.inbox),
                          "--cmd", "wake.sh"])
        self.assertEqual(2, rc)
        run.assert_not_called()
        self.assertEqual(before, self.state_path.read_text(encoding="utf-8"),
                         "a refusal rewrote the cursor")
        self.assertEqual([], self.inbox_ids())

    def test_a_cursor_of_the_wrong_shape_also_refuses(self):
        for junk in ("[1, 2, 3]", '{"seen_row_ids": "N1"}', '{"watermark": 12}'):
            self.state_path.write_text(junk, encoding="utf-8")
            with self.assertRaises(pw.StateUnreadable, msg=junk):
                pw.load_state(self.state_path)

    def test_a_missing_cursor_is_a_first_run_and_not_a_refusal(self):
        self.assertEqual({}, pw.load_state(Path(self.tmp.name) / "not-here.json"))

    def test_the_cursor_is_written_atomically_and_leaves_no_fragment(self):
        pw.save_state({"watermark": pw.stamp(T0), "seen_row_ids": ["a"]}, self.state_path)
        pw.save_state({"watermark": pw.stamp(T0), "seen_row_ids": ["a", "b"]}, self.state_path)
        self.assertEqual(["a", "b"], self.saved()["seen_row_ids"])
        self.assertEqual([], [p.name for p in Path(self.tmp.name).glob("*.tmp")])
        self.assertNotIn(b"\r\n", self.state_path.read_bytes())

    # --- Codex 3: one row, one inbox entry ----------------------------------------------------
    def test_one_row_is_one_inbox_entry_across_a_failed_then_successful_wake(self):
        self.pass_with([], state={})
        rc, run = self.pass_with([row("N1")], exit_code=3)
        self.assertEqual(1, rc)
        self.assertEqual(["N1"], self.inbox_ids())
        self.assertIn("N1", self.saved().get("enqueued_row_ids") or [])
        self.assertNotIn("N1", self.saved()["seen_row_ids"],
                         "a failed wake was recorded as handled")
        rc, again = self.pass_with([row("N1")])
        self.assertEqual(0, rc)
        self.assertEqual(1, again.call_count, "the row did not ring again")
        self.assertEqual(["N1"], self.inbox_ids(), "one row, two inbox entries")
        self.assertIn("N1", self.saved()["seen_row_ids"])

    def test_a_failed_wake_holds_the_watermark(self):
        self.pass_with([], state={})
        before = self.saved()["watermark"]
        rc, run = self.pass_with([row("N1")], exit_code=3, wake_takes=30)
        self.assertEqual(1, rc)
        self.assertEqual(before, self.saved()["watermark"])

    def test_a_wake_command_that_cannot_start_is_a_failure_not_a_pass(self):
        self.pass_with([], state={})
        with mock.patch.object(pw, "rows_since", return_value=[row("N1")]):
            rc = pw.one_pass(self.env, self.saved(), me="pi1-cli", cmd="wake.sh",
                             inbox=self.inbox, state_path=self.state_path, max_age_hours=24.0,
                             dry_run=False, run=mock.Mock(side_effect=OSError("no such file")))
        self.assertEqual(1, rc)
        self.assertNotIn("N1", self.saved()["seen_row_ids"])
        self.assertEqual(["N1"], self.inbox_ids(), "it was written down, so it stays written down")

    # --- old news -----------------------------------------------------------------------------
    def test_a_row_older_than_the_limit_is_recorded_and_does_not_ring(self):
        old = pw.stamp(T0 - timedelta(hours=72))
        self.pass_with([], state={})
        rc, run = self.pass_with([row("STALE", ts=old)])
        self.assertEqual(0, rc)
        run.assert_not_called()
        self.assertIn("STALE", self.saved()["seen_row_ids"])
        self.assertEqual([], self.inbox_ids())

    def test_a_timestamp_that_cannot_be_read_counts_as_old(self):
        self.assertIsNone(pw.read_ts("BCB|v=1|id=the-payload-went-in-the-timestamp-column"))
        self.pass_with([], state={})
        rc, run = self.pass_with([row("ODD", ts="BCB|v=1|id=x|phase=NOTE")])
        run.assert_not_called()
        self.assertIn("ODD", self.saved()["seen_row_ids"])

    def test_the_age_limit_can_be_turned_off(self):
        old = pw.stamp(T0 - timedelta(hours=72))
        self.pass_with([], state={})
        rc, run = self.pass_with([row("STALE", ts=old)], max_age_hours=0)
        self.assertEqual(1, run.call_count)

    def test_an_age_must_be_a_finite_number_of_hours(self):
        self.assertEqual(12.0, pw.sane_hours("12"))
        self.assertEqual(0.0, pw.sane_hours("0"))
        for junk in ("nan", "inf", "-inf", "-1"):
            with self.assertRaises(Exception, msg=junk):
                pw.sane_hours(junk)

    # --- the quiet paths ----------------------------------------------------------------------
    def test_no_command_queues_the_row_and_still_advances(self):
        self.pass_with([], state={})
        read_at = self.clock.at
        rc, run = self.pass_with([row("N1")], cmd="")
        self.assertEqual(0, rc)
        run.assert_not_called()
        self.assertEqual(["N1"], self.inbox_ids())
        self.assertIn("N1", self.saved()["seen_row_ids"])
        self.assertEqual(pw.stamp(read_at), self.saved()["watermark"])

    def test_a_dry_run_touches_neither_the_state_nor_the_command(self):
        self.pass_with([], state={})
        before = self.state_path.read_text(encoding="utf-8")
        rc, run = self.pass_with([row("N1")], dry_run=True)
        self.assertEqual(0, rc)
        run.assert_not_called()
        self.assertEqual(before, self.state_path.read_text(encoding="utf-8"))
        self.assertEqual([], self.inbox_ids())

    def test_the_seen_list_is_capped(self):
        state = {"watermark": pw.stamp(T0), "seen_row_ids": ["x%d" % i for i in range(600)]}
        pw.save_state(state, self.state_path)
        self.assertEqual(pw.SEEN_CAP, len(self.saved()["seen_row_ids"]))


class TheReadIsFiltered(unittest.TestCase):
    def test_it_asks_only_for_rows_since_its_watermark_minus_the_overlap(self):
        with mock.patch.object(pw.bus, "read_rows", return_value={"rows": []}) as read:
            pw.rows_since({"BUS_URL": "u", "BUS_SECRET": "s"}, T0)
        asked = read.call_args.kwargs["since"]
        self.assertEqual(pw.stamp(T0 - timedelta(seconds=pw.OVERLAP_SECONDS)), asked)

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

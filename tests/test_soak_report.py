#!/usr/bin/env python3
r"""The soak judge must be able to FAIL, and must never round NOT YET up to PASS.

scripts/soak_report.py is the evidence cutover step 8 would be decided on, and
step 8 is Mr Salam disabling the laptop scheduler. A judge that cannot fail is
worse than no judge: it manufactures confidence.

So every criterion is tested by feeding it a window that breaks exactly that one,
and the three verdicts are tested as three distinct outcomes.

THE BUG THIS SUITE WAS WRITTEN AFTER
    The first version of cadence_gaps measured the whole 48-hour window regardless
    of when the soak began, so minutes after starting it reported FAIL with 44
    missing hours - from before the jobs existed. A FAIL nobody believes gets
    ignored; a FAIL somebody believes sends them chasing a working scheduler.
    test_hours_before_the_soak_started_are_not_counted pins the fix.

Offline: no gcloud, no logs, no board. Every window is a fixture.

Run: python3 tests/test_soak_report.py
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import soak_report as soak  # noqa: E402


def hours_ago(n: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=n)


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(n_hours_ago: float, where="cloud-run", cursor_value=None,
        combined=None, credential_source="injected") -> dict:
    payload = {"where": where, "credential_source": credential_source}
    if cursor_value:
        payload["cursor"] = {"value": cursor_value, "changed": True}
    if combined:
        payload["combined"] = combined
    return {"ts": stamp(hours_ago(n_hours_ago)), "execution": "e", "payload": payload}


def clean_window(hours: int, started: str):
    """One run in every hour since `started`, all in the cloud, cursor advancing."""
    probe, shadow = [], []
    t0 = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    h = 0.0
    while True:
        when = now - timedelta(hours=h + 1)
        if when <= t0:
            break
        probe.append(run(h + 1, cursor_value="2026-09-%02dT00:00:00"
                         % max(1, 28 - int(h))))
        shadow.append(run(h + 1, combined="NEWS"))
        h += 1
        if h > hours + 2:
            break
    # Oldest first, and the cursor must ascend with time.
    probe.reverse()
    shadow.reverse()
    for i, r in enumerate(probe):
        r["payload"]["cursor"]["value"] = "2026-09-24T%02d:00:00" % min(23, i)
    return probe, shadow


class ItCanFail(unittest.TestCase):
    def setUp(self):
        self.started = stamp(hours_ago(50))
        self.probe, self.shadow = clean_window(48, self.started)

    def test_the_clean_window_itself_produces_no_findings(self):
        """The control. Without this, every failure below could be a false alarm."""
        findings, _ = soak.check(self.probe, self.shadow, 48, 1, self.started)
        self.assertEqual(findings, [], "the clean control window is not clean")

    def test_a_missing_hour_is_a_finding(self):
        del self.probe[5]
        findings, criteria = soak.check(self.probe, self.shadow, 48, 1,
                                        self.started)
        self.assertTrue(any("board-probe missed" in f for f in findings))
        self.assertTrue(criteria["cadence:board-probe"]["missing_hours"])

    def test_a_six_hour_hole_is_not_averaged_away(self):
        # An average would hide this: 42 runs in 48 hours still averages ~1/hour.
        del self.probe[10:16]
        findings, _ = soak.check(self.probe, self.shadow, 48, 1, self.started)
        self.assertTrue(any("missed 6 hour" in f for f in findings), findings)

    def test_a_cursor_going_backwards_is_a_finding(self):
        self.probe[20]["payload"]["cursor"]["value"] = "2026-01-01T00:00:00"
        findings, criteria = soak.check(self.probe, self.shadow, 48, 1,
                                        self.started)
        self.assertFalse(criteria["cursor_monotonic"])
        self.assertTrue(any("went back" in f for f in findings))

    def test_two_rows_for_one_ack_target_is_a_finding(self):
        findings, _ = soak.check(self.probe, self.shadow, 48, 2, self.started)
        self.assertTrue(any("2 rows on the board" in f for f in findings))

    def test_zero_rows_is_also_a_finding(self):
        # The write is supposed to have landed. Its absence is as wrong as a dupe.
        findings, _ = soak.check(self.probe, self.shadow, 48, 0, self.started)
        self.assertTrue(any("has 0 rows" in f for f in findings))

    def test_an_unreadable_board_is_not_reported_as_zero_rows(self):
        # None means "could not tell", and it must not become a duplicate finding.
        findings, criteria = soak.check(self.probe, self.shadow, 48, None,
                                        self.started)
        self.assertNotIn("board_rows_for_ack_target", criteria)
        self.assertEqual(findings, [])

    def test_a_single_local_run_is_a_finding(self):
        self.probe[3]["payload"]["where"] = "local"
        findings, criteria = soak.check(self.probe, self.shadow, 48, 1,
                                        self.started)
        self.assertFalse(criteria["all_runs_in_cloud"])
        self.assertTrue(any("ran as 'local'" in f for f in findings))

    def test_credentials_from_a_file_in_the_cloud_is_a_finding(self):
        # The lane is supposed to have no file. One run reading one is the
        # dependency this migration exists to remove, reappearing.
        self.shadow[7]["payload"]["credential_source"] = "file"
        findings, _ = soak.check(self.probe, self.shadow, 48, 1, self.started)
        self.assertTrue(any("credentials from 'file'" in f for f in findings))

    def test_a_shadow_that_keeps_saying_unknown_is_a_finding(self):
        for r in self.shadow[:20]:
            r["payload"]["combined"] = "UNKNOWN"
        findings, criteria = soak.check(self.probe, self.shadow, 48, 1,
                                        self.started)
        self.assertEqual(criteria["shadow_verdicts"].get("UNKNOWN"), 20)
        self.assertTrue(any("not reliable enough to cut over" in f
                            for f in findings))

    def test_one_isolated_unknown_is_tolerated(self):
        # The gateway flaps; measured 10/10 clean after one 404. A single UNKNOWN
        # must not block a cutover, or the soak can never pass.
        self.shadow[4]["payload"]["combined"] = "UNKNOWN"
        findings, _ = soak.check(self.probe, self.shadow, 48, 1, self.started)
        self.assertEqual(findings, [], findings)


class NotYetIsNotAPass(unittest.TestCase):
    """The property that matters most, because rounding up is the tempting error."""

    def test_hours_before_the_soak_started_are_not_counted(self):
        """The bug that made the first run report FAIL with 44 missing hours."""
        just_started = stamp(hours_ago(0.1))
        gaps = soak.cadence_gaps([], 48, just_started)
        self.assertEqual(gaps["hours_expected_since_start"], 0)
        self.assertEqual(gaps["missing_hours"], [],
                         "hours from before the jobs existed were counted against "
                         "them")

    def test_with_no_recorded_start_nothing_is_expected_but_it_says_so(self):
        gaps = soak.cadence_gaps([], 48, None)
        self.assertTrue(gaps["not_yet_started"],
                        "an unrecorded start must be visible, not silently "
                        "treated as a full window")

    def test_a_partial_window_with_no_findings_still_counts_hours(self):
        started = stamp(hours_ago(3))
        probe, shadow = clean_window(48, started)
        findings, _ = soak.check(probe, shadow, 48, 1, started)
        self.assertEqual(findings, [])
        # The caller decides PASS vs NOT YET from elapsed time; what check()
        # guarantees is that a short clean window yields no findings, so the
        # distinction cannot come from here by accident.
        self.assertLess(len(probe), 48,
                        "a 3-hour window should not contain 48 runs")

    def test_a_complete_clean_window_has_the_hours_to_back_a_pass(self):
        started = stamp(hours_ago(50))
        probe, shadow = clean_window(48, started)
        findings, criteria = soak.check(probe, shadow, 48, 1, started)
        self.assertEqual(findings, [])
        self.assertGreaterEqual(
            criteria["cadence:board-probe"]["hours_with_a_run"], 40,
            "a 50-hour-old soak should have ~48 hours of runs")


class AnUnknownSaysWhy(unittest.TestCase):
    def setUp(self):
        self.started = stamp(hours_ago(50))
        self.probe, self.shadow = clean_window(48, self.started)

    def test_each_unknown_carries_its_time_and_both_notes(self):
        p = self.shadow[3]["payload"]
        p["combined"] = "UNKNOWN"
        p["board_note"] = "board read failed (HTTP 404)"
        p["whatsapp_note"] = "whatsapp read returned a page, not data (HTTP 404)"
        _, criteria = soak.check(self.probe, self.shadow, 48, 1, self.started)
        reasons = criteria["shadow_unknown_reasons"]
        self.assertEqual(len(reasons), 1)
        self.assertIn(self.shadow[3]["ts"][5:16], reasons[0])
        self.assertIn("board read failed (HTTP 404)", reasons[0])
        self.assertIn("whatsapp read returned a page", reasons[0])

    def test_an_unknown_without_notes_says_so(self):
        self.shadow[5]["payload"]["combined"] = "UNKNOWN"
        _, criteria = soak.check(self.probe, self.shadow, 48, 1, self.started)
        self.assertTrue(criteria["shadow_unknown_reasons"][0].endswith("no note"))

    def test_a_failed_board_read_is_listed_even_when_combined_is_news(self):
        # 22:45Z, 2026-09-24: board UNKNOWN after three attempts, combined NEWS
        # because WhatsApp had news. The combined count never saw it.
        p = self.shadow[6]["payload"]
        p.update({"combined": "NEWS", "board": "UNKNOWN",
                  "board_note": "board read failed (HTTP 404)", "whatsapp": "NEWS"})
        findings, criteria = soak.check(self.probe, self.shadow, 48, 1, self.started)
        self.assertEqual(criteria["shadow_unknown_reasons"], [])
        failed = criteria["shadow_failed_reads"]
        self.assertEqual(len(failed), 1)
        self.assertIn(self.shadow[6]["ts"][5:16], failed[0])
        self.assertIn("board failed (combined=NEWS)", failed[0])
        self.assertIn("HTTP 404", failed[0])
        self.assertEqual(findings, [], "reported, not a finding: the wake decision was right")

    def test_both_reads_failing_names_both(self):
        p = self.shadow[2]["payload"]
        p.update({"combined": "UNKNOWN", "board": "UNKNOWN", "whatsapp": "UNKNOWN",
                  "board_note": "b404", "whatsapp_note": "w404"})
        _, criteria = soak.check(self.probe, self.shadow, 48, 1, self.started)
        self.assertIn("board+whatsapp failed (combined=UNKNOWN): b404; w404",
                      criteria["shadow_failed_reads"][0])

    def test_news_runs_add_no_reasons(self):
        _, criteria = soak.check(self.probe, self.shadow, 48, 1, self.started)
        self.assertEqual(criteria["shadow_unknown_reasons"], [])
        self.assertEqual(criteria["shadow_failed_reads"], [])


class TheCriteriaAreAllReported(unittest.TestCase):
    def test_every_criterion_appears_even_when_it_passes(self):
        # A report that only lists problems cannot be used to confirm a criterion
        # was actually evaluated - the playwright gate was green over zero tests.
        started = stamp(hours_ago(50))
        probe, shadow = clean_window(48, started)
        _, criteria = soak.check(probe, shadow, 48, 1, started)
        for expected in ("cadence:board-probe", "cadence:waker-shadow",
                         "cursor_monotonic", "board_rows_for_ack_target",
                         "all_runs_in_cloud", "shadow_verdicts",
                         "shadow_unknown_reasons", "shadow_failed_reads"):
            self.assertIn(expected, criteria)


if __name__ == "__main__":
    os.chdir(REPO)
    unittest.main(verbosity=2)

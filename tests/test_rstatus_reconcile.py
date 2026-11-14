"""R-STATUS pure state machine. docs/BOARD-STATUS-FRAMEWORK.md is the spec."""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rstatus_reconcile as rs  # noqa: E402

NOW = datetime(2026, 11, 14, 3, 0, 0, tzinfo=timezone.utc)


def row(ts, payload, source="claude-code-cli", category="OPEN"):
    return ["RID-" + payload[:8], ts, source, "ALL", "ASK", payload, category, "", "", ""]


def bcb(id_, phase, **fields):
    parts = ["BCB", "v=1", "id=" + id_, "phase=" + phase]
    for k, v in fields.items():
        parts.append("%s=%s" % (k, v))
    return "|".join(parts)


class TheFieldParsing(unittest.TestCase):
    def test_field_reads_the_first_occurrence(self):
        self.assertEqual("x", rs.field("BCB|v=1|id=X|from=x|from=y", "from"))

    def test_field_is_none_when_absent(self):
        self.assertIsNone(rs.field("BCB|v=1|id=X", "answers"))

    def test_parse_ts_skips_what_will_not_parse(self):
        self.assertIsNone(rs.parse_ts("Open"))
        self.assertIsNone(rs.parse_ts("Friday, September 4, 2026"))
        self.assertIsNone(rs.parse_ts(""))
        self.assertEqual(2026, rs.parse_ts("2026-11-14T00:00:00Z").year)


class TheRowParsing(unittest.TestCase):
    def test_a_row_with_no_bcb_id_is_skipped(self):
        self.assertIsNone(rs.parse_row(0, row("2026-11-14T00:00:00Z", "not a bcb payload at all")))

    def test_parse_rows_keeps_only_ids_rows(self):
        rows = [row("2026-11-14T00:00:00Z", "junk"),
                row("2026-11-14T00:00:01Z", bcb("A", "ASK"))]
        parsed = rs.parse_rows(rows)
        self.assertEqual(["A"], [p["id"] for p in parsed])


class TheLifecycle(unittest.TestCase):
    """One ask at a time, using reconcile_all() so the index-building is exercised too."""

    def test_a_fresh_ask_with_a_future_due_is_open(self):
        rows = [row("2026-11-14T00:00:00Z",
                     bcb("A", "ASK", expect_from="grok", expect="result",
                        done_when="ships", due="2026-11-15T00:00:00Z"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"])
        self.assertEqual("grok", result["expected_from"])
        self.assertEqual("result: ships", result["expected_response"])
        self.assertIsNone(result["closed_by"])

    def test_a_non_ask_phase_is_info_never_open(self):
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "RESULT", answers="SOMETHING"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("INFO", result["status"])
        self.assertIsNone(result["expected_from"])

    def test_an_ack_with_eta_moves_the_ask_to_acked(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "ACK", answers="A", eta="2026-11-14T01:00:00Z")),
        ]
        self.assertEqual("ACKED", rs.reconcile_all(rows, now=NOW)["A"]["status"])

    def test_a_result_with_answers_is_answered_not_closed(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", answers="A")),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("ANSWERED", result["status"])
        self.assertEqual("B", result["closed_by"])

    def test_a_result_that_also_carries_closes_is_closed(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", answers="A", closes="A")),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("CLOSED", result["status"])
        self.assertEqual("B", result["closed_by"])

    def test_an_answer_then_a_separate_close_ends_closed(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", answers="A")),
            row("2026-11-14T00:10:00Z", bcb("C", "RESULT", closes="A")),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("CLOSED", result["status"])
        self.assertEqual("C", result["closed_by"])

    def test_a_disagreeing_asker_reopens_to_open_with_a_new_due(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-14T01:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", answers="A", closes="A")),
            row("2026-11-14T00:10:00Z", bcb("C", "RESULT", reopen="A", due="2026-11-16T00:00:00Z",
                                            **{"reason": "did-not-meet-done-when"})),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"])
        self.assertIsNone(result["closed_by"])
        self.assertEqual(datetime(2026, 11, 16, tzinfo=timezone.utc), result["due_utc"])

    def test_reopen_with_no_due_of_its_own_keeps_the_old_due(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-14T01:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", closes="A")),
            row("2026-11-14T00:10:00Z", bcb("C", "RESULT", reopen="A")),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual(datetime(2026, 11, 14, 1, tzinfo=timezone.utc), result["due_utc"])


class TheHold(unittest.TestCase):
    """HELD is the status of the ROW ITSELF that carries hold= (blackboard-5e's
    ruling, 2026-11-14), not a link to some other ask. Lifecycle is
    open_for_me.hold_lifecycle, reused, not reimplemented: only the SAME
    placer (from= AND Source_Tag) may clear it, with clears=/supersedes=
    naming the hold row's own id=, or a later GO/MERGED on a different full
    head for the same pr=."""

    def test_a_hold_row_is_held_in_its_own_right(self):
        rows = [row("2026-11-14T00:00:00Z",
                     bcb("H1", "RESULT", hold="PRODUCT_DECISION_NEEDED_FROM_MR_SALAM"),
                     source="claude-code-cli")]
        result = rs.reconcile_all(rows, now=NOW)["H1"]
        self.assertEqual("HELD", result["status"])
        self.assertEqual("claude-code-cli", result["expected_from"])
        self.assertEqual("hold:PRODUCT_DECISION_NEEDED_FROM_MR_SALAM", result["closed_by"])

    def test_a_hold_never_expires_even_if_it_carries_a_passed_due(self):
        rows = [row("2026-11-14T00:00:00Z",
                     bcb("H1", "ASK", hold="release", due="2026-11-14T00:30:00Z"),
                     source="claude-code-cli")]
        result = rs.reconcile_all(rows, now=NOW)["H1"]
        self.assertEqual("HELD", result["status"])

    def test_an_unauthorised_clear_from_a_different_tag_leaves_it_held(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("H1", "RESULT", hold="release"), source="claude-code-cli"),
            row("2026-11-14T00:05:00Z", bcb("H2", "RESULT", clears="H1", **{"from": "grok"}),
                source="grok"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["H1"]
        self.assertEqual("HELD", result["status"], "only the placer may clear its own hold")

    def test_the_same_placer_clearing_its_own_hold_by_id_lifts_it(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("H1", "RESULT", hold="release"), source="claude-code-cli"),
            row("2026-11-14T00:05:00Z",
               bcb("H2", "RESULT", clears="H1", **{"from": "claude-code-cli"}),
                source="claude-code-cli"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["H1"]
        self.assertEqual("INFO", result["status"], "cleared, falls through to its own phase (RESULT)")

    def test_supersedes_by_the_same_placer_lifts_it_the_same_way(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("H1", "ASK", hold="release", due="2026-11-20T00:00:00Z"),
               source="claude-code-cli"),
            row("2026-11-14T00:05:00Z",
               bcb("H2", "RESULT", supersedes="H1", **{"from": "claude-code-cli"}),
                source="claude-code-cli"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["H1"]
        self.assertEqual("OPEN", result["status"], "cleared, falls through to its own ASK phase")

    def test_a_cleared_hold_row_that_is_itself_an_ask_still_runs_the_ask_machine(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("H1", "ASK", hold="release", due="2026-11-14T01:00:00Z"),
               source="claude-code-cli"),
            row("2026-11-14T00:05:00Z",
               bcb("H2", "RESULT", clears="H1", **{"from": "claude-code-cli"}),
                source="claude-code-cli"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["H1"]
        self.assertEqual("EXPIRED", result["status"], "cleared hold, but its own due has passed")


class TheHoldReverification(unittest.TestCase):
    def test_is_owner_reason_matches_the_three_markers(self):
        self.assertTrue(rs.is_owner_reason("PRODUCT_DECISION_NEEDED_FROM_MR_SALAM"))
        self.assertTrue(rs.is_owner_reason("mr_salam_decision"))
        self.assertTrue(rs.is_owner_reason("OWNER"))
        self.assertFalse(rs.is_owner_reason("release"))
        self.assertFalse(rs.is_owner_reason(""))

    def test_a_fresh_hold_under_the_age_floor_is_not_listed(self):
        young = NOW - timedelta(days=1)
        rows = [row(young.strftime("%Y-%m-%dT%H:%M:%SZ"), bcb("H1", "RESULT", hold="release"),
                   source="claude-code-cli")]
        out = rs.holds_for_reverification(rows, now=NOW)
        self.assertEqual([], out["owner_decision"])
        self.assertEqual([], out["other"])

    def test_an_old_hold_is_grouped_by_placer_and_reason(self):
        old = NOW - timedelta(days=10)
        rows = [row(old.strftime("%Y-%m-%dT%H:%M:%SZ"), bcb("H1", "RESULT", hold="release"),
                   source="claude-code-cli")]
        out = rs.holds_for_reverification(rows, now=NOW)
        self.assertEqual(1, len(out["other"]))
        self.assertEqual("claude-code-cli", out["other"][0]["placer"])
        self.assertEqual("release", out["other"][0]["reason"])
        self.assertEqual(["H1"], [h["id"] for h in out["other"][0]["holds"]])

    def test_an_owner_named_reason_goes_on_the_owner_decision_list(self):
        old = NOW - timedelta(days=10)
        rows = [row(old.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   bcb("H1", "RESULT", hold="PRODUCT_DECISION_NEEDED_FROM_MR_SALAM"),
                   source="claude-code-cli")]
        out = rs.holds_for_reverification(rows, now=NOW)
        self.assertEqual(1, len(out["owner_decision"]))
        self.assertEqual([], out["other"])

    def test_a_cleared_old_hold_is_not_listed_at_all(self):
        old = NOW - timedelta(days=10)
        rows = [
            row(old.strftime("%Y-%m-%dT%H:%M:%SZ"), bcb("H1", "RESULT", hold="release"),
                source="claude-code-cli"),
            row((old + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               bcb("H2", "RESULT", clears="H1", **{"from": "claude-code-cli"}),
                source="claude-code-cli"),
        ]
        out = rs.holds_for_reverification(rows, now=NOW)
        self.assertEqual([], out["other"])
        self.assertEqual([], out["owner_decision"])


class TheExpiry(unittest.TestCase):
    def test_a_rule1_compliant_ask_past_its_due_with_no_result_expires(self):
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-14T01:00:00Z"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("EXPIRED", result["status"])
        self.assertEqual("reconciler:expired_past_due", result["closed_by"])

    def test_acked_past_due_with_no_result_still_expires(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-14T01:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "ACK", answers="A", eta="2026-11-14T00:10:00Z")),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("EXPIRED", result["status"])

    def test_a_legacy_ask_over_30_days_old_with_no_due_backfills_expired(self):
        old = NOW - timedelta(days=45)
        rows = [row(old.strftime("%Y-%m-%dT%H:%M:%SZ"), bcb("A", "ASK"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("EXPIRED", result["status"])
        self.assertEqual("backfill:expired>30d", result["closed_by"])

    def test_a_legacy_ask_7_to_30_days_old_with_no_due_backfills_the_other_reason(self):
        old = NOW - timedelta(days=10)
        rows = [row(old.strftime("%Y-%m-%dT%H:%M:%SZ"), bcb("A", "ASK"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("EXPIRED", result["status"])
        self.assertEqual("backfill:expired7-30d", result["closed_by"])

    def test_a_legacy_ask_under_7_days_old_stays_open_with_a_synthetic_24h_due(self):
        young = NOW - timedelta(days=2)
        rows = [row(young.strftime("%Y-%m-%dT%H:%M:%SZ"), bcb("A", "ASK"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"])
        self.assertEqual(NOW + timedelta(hours=24), result["due_utc"])

    def test_a_row_still_within_due_never_expires(self):
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-12-01T00:00:00Z"))]
        self.assertEqual("OPEN", rs.reconcile_all(rows, now=NOW)["A"]["status"])


class TheEscalationList(unittest.TestCase):
    def test_a_fresh_expiry_is_listed(self):
        new = {"A": {"status": "EXPIRED", "expected_from": "grok", "closed_by": "x", "due_utc": None}}
        out = rs.newly_expired({"A": "OPEN"}, new)
        self.assertEqual(["A"], [e["id"] for e in out])

    def test_an_ask_already_expired_last_run_is_not_relisted(self):
        new = {"A": {"status": "EXPIRED", "expected_from": "grok", "closed_by": "x", "due_utc": None}}
        self.assertEqual([], rs.newly_expired({"A": "EXPIRED"}, new))

    def test_a_non_expired_result_is_never_listed(self):
        new = {"A": {"status": "OPEN", "expected_from": None, "closed_by": None, "due_utc": None}}
        self.assertEqual([], rs.newly_expired({}, new))


class TheSheetFormatting(unittest.TestCase):
    def test_to_row_values_is_six_strings_in_order(self):
        result = {"status": "ANSWERED", "expected_from": "grok",
                  "expected_response": "result: ships",
                  "due_utc": datetime(2026, 11, 15, tzinfo=timezone.utc),
                  "closed_by": "B", "status_updated_utc": datetime(2026, 11, 14, 1, tzinfo=timezone.utc)}
        self.assertEqual(
            ["ANSWERED", "grok", "result: ships", "2026-11-15T00:00:00Z", "B", "2026-11-14T01:00:00Z"],
            rs.to_row_values(result))

    def test_blank_fields_become_empty_strings_not_none_or_the_word_none(self):
        result = {"status": "OPEN", "expected_from": None, "expected_response": None,
                  "due_utc": None, "closed_by": None, "status_updated_utc": None}
        values = rs.to_row_values(result)
        self.assertEqual(["OPEN", "", "", "", "", ""], values)


class AgainstTheRealBoard(unittest.TestCase):
    """Sanity-checks reconcile_all() against the actual 21,227-row dump the doc's own
    figures came from (docs/BOARD-STATUS-FRAMEWORK.md, "Why the board looked all-open"),
    at the same moment the doc was measured - not a synthetic fixture."""

    @classmethod
    def setUpClass(cls):
        path = (r"C:\Users\salam\AppData\Local\Temp\claude\8185ffcdf2062aa0"
                r"\48854f9b-a840-469a-bb58-4dcb0b27f12b\scratchpad\bstatus\board.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError:
            cls.rows = None
            return
        cls.rows = data["rows"][1:]
        cls.now = datetime(2026, 11, 14, 2, 15, 0, tzinfo=timezone.utc)
        # Computed ONCE for the whole class - not per test. The hold-clearing
        # pre-filter in _sorted_timeline brings one full-board reconcile_all()
        # to well under two minutes (was ~2 hours before it: 136 holds x a
        # 58.7s open_for_me.hold_lifecycle call each over the unfiltered
        # board); still too slow to pay 3+ times over in one test run.
        cls.results = rs.reconcile_all(cls.rows, now=cls.now)

    def test_it_runs_to_completion_on_every_real_row_without_raising(self):
        if self.rows is None:
            self.skipTest("board.json dump not present on this machine")
        self.assertGreater(len(self.results), 0)
        for result in self.results.values():
            self.assertIn(result["status"], rs.STATUSES)

    def test_ask_type_counts_are_in_the_neighbourhood_the_doc_reports(self):
        # The doc: 2,846 open asks (ASK/REQUEST/DISPATCH/REVIEW_REQUEST/TASK); 1,728
        # already answered; 1,118 with no reply (850 >30d, 218 7-30d, 50 <7d); 136
        # carry hold=. Not an exact match is expected (the doc counted raw ask rows,
        # not reconciled ids, and this module de-dupes/links differently) - this is a
        # sanity band, not a byte-for-byte oracle.
        if self.rows is None:
            self.skipTest("board.json dump not present on this machine")
        from collections import Counter
        counts = Counter(r["status"] for r in self.results.values())
        self.assertGreater(counts["ANSWERED"] + counts["CLOSED"], 1000)
        self.assertGreater(counts["EXPIRED"], 500)
        self.assertGreater(counts["INFO"], 10000)

    def test_most_real_holds_are_held_in_their_own_right(self):
        # blackboard-5e's corrected ruling (2026-11-14): HELD is the status of the row
        # that carries hold=, not of some other ask it points at - the 136 real hold=
        # values (PRODUCT_DECISION_NEEDED_FROM_MR_SALAM, release, ...) are free-text
        # reason tags, never a reference to another row's id=, so the first draft's
        # "hold=<ask id>" model found zero matches. Most of the 136 should still be
        # ACTIVE (not yet cleared by their own placer), so HELD should be substantial,
        # not zero and not necessarily all 136 (open_for_me.hold_lifecycle may find a
        # real same-placer clear/supersede/GO on some of them).
        if self.rows is None:
            self.skipTest("board.json dump not present on this machine")
        from collections import Counter
        counts = Counter(r["status"] for r in self.results.values())
        self.assertGreater(counts["HELD"], 50)
        self.assertLessEqual(counts["HELD"], 136)


if __name__ == "__main__":
    unittest.main()

"""R-STATUS pure state machine. docs/BOARD-STATUS-FRAMEWORK.md is the spec."""
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rstatus_reconcile as rs  # noqa: E402

NOW = datetime(2026, 11, 14, 3, 0, 0, tzinfo=timezone.utc)
_ID_RE = re.compile(r"(?:^|\|)id=([^|]+)")


def row(ts, payload, source="claude-code-cli", category="OPEN", row_id=None):
    """A board row. row_id (column A) defaults to the payload's own BCB id= -
    the two are DIFFERENT identities in general (see TheRowIdentity below,
    and rstatus_reconcile's IDENTITY) but most of this file's tests are about
    the ask-status machinery, not the row-key mapping itself, so keeping them
    equal by default lets every existing `results["A"]` lookup keep working."""
    if row_id is None:
        m = _ID_RE.search(payload)
        row_id = m.group(1) if m else "RID-" + payload[:8]
    return [row_id, ts, source, "ALL", "ASK", payload, category, "", "", ""]


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

    def test_reopen_with_no_due_of_its_own_keeps_the_asks_existing_due(self):
        # Distinct from TheReopenWithoutAnyDue below: THIS ask already has a
        # due=, so there is a real due to fall back to - no backfill age
        # bucket is ever consulted.
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

    def test_a_garbage_row_timestamp_with_no_due_fails_closed_not_crashes(self):
        # Codex, PR 502 review: `now - ask["ts"]` raised TypeError for any ask
        # with no due= whose OWN column-B timestamp parse_ts cannot read,
        # aborting reconcile_all for the whole board on one bad row. It must
        # instead leave the ask exactly where the event loop put it (OPEN,
        # here) - never auto-expired on an age it cannot measure.
        rows = [row("not-a-timestamp-at-all", bcb("A", "ASK"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"])
        self.assertIsNone(result["due_utc"])

    def test_a_garbage_row_timestamp_never_auto_expires_even_with_many_other_rows(self):
        # The crash this guards against took down reconcile_all for every row
        # on the board, not just the bad one - prove it alongside normal rows.
        rows = [
            row("2026-11-14T00:00:00Z", bcb("GOOD", "ASK", due="2026-11-20T00:00:00Z")),
            row("garbage", bcb("A", "ASK")),
        ]
        results = rs.reconcile_all(rows, now=NOW)
        self.assertEqual("OPEN", results["GOOD"]["status"])
        self.assertEqual("OPEN", results["A"]["status"])

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
        # No local path lives in this public repo (the dump is a 25MB personal
        # scratchpad fixture, never committed). Point RSTATUS_BOARD_DUMP at a
        # copy of the real board (a v1 bus `action=read` response, {"rows": [...]})
        # to run this class for real; everywhere else it skips.
        path = os.environ.get("RSTATUS_BOARD_DUMP")
        if not path:
            cls.rows = None
            return
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


class TheRowIdentity(unittest.TestCase):
    """Adversarial review finding #1: results are keyed by the sheet's own
    Row_ID (column A), which status_write matches on - never the BCB id=,
    which 6,186 of 20,163 real BCB ids never were. Today's tests enshrined the
    bug (results looked up by the BCB id itself); this is the row whose
    Row_ID and BCB id genuinely differ."""

    def test_results_are_keyed_by_row_id_not_bcb_id(self):
        payload = bcb("SOME-BCB-ID", "RESULT")
        r = row("2026-11-14T00:00:00Z", payload, row_id="f381e439-sheet-generated")
        results = rs.reconcile_all([r], now=NOW)
        self.assertIn("f381e439-sheet-generated", results)
        self.assertNotIn("SOME-BCB-ID", results)

    def test_links_still_resolve_by_bcb_id_even_though_the_result_key_is_row_id(self):
        ask = row("2026-11-14T00:00:00Z", bcb("ASK-1", "ASK", due="2026-11-20T00:00:00Z"),
                 row_id="sheet-row-aaa")
        answer = row("2026-11-14T00:05:00Z", bcb("RESULT-1", "RESULT", answers="ASK-1"),
                     row_id="sheet-row-bbb")
        results = rs.reconcile_all([ask, answer], now=NOW)
        self.assertEqual("ANSWERED", results["sheet-row-aaa"]["status"])
        self.assertEqual("RESULT-1", results["sheet-row-aaa"]["closed_by"])

    def test_a_row_with_no_row_id_at_all_is_skipped(self):
        bare = row("2026-11-14T00:00:00Z", bcb("X", "RESULT"), row_id="")
        self.assertEqual({}, rs.reconcile_all([bare], now=NOW))


class TheMinutePrecisionDue(unittest.TestCase):
    """Finding #3: due= on the real board is frequently minute-precision
    (2026-11-14T15:00Z, 16 characters) - the old parser needed seconds and
    silently dropped every one of them."""

    def test_minute_precision_due_parses(self):
        self.assertEqual(datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc),
                         rs.parse_ts("2026-11-14T15:00Z"))

    def test_minute_precision_due_drives_a_real_expiry(self):
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-14T01:00Z"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("EXPIRED", result["status"])
        self.assertEqual("reconciler:expired_past_due", result["closed_by"])

    def test_free_text_due_still_correctly_fails_to_parse(self):
        self.assertIsNone(rs.parse_ts("2026-11-10 10:45 pm ET"))
        self.assertIsNone(rs.parse_ts("6:00 PM ET (22:00Z)"))


class TheRule4Authorization(unittest.TestCase):
    """Finding #4: only Expected_From or the asker may ack/answer/close/reopen
    an ask - when expect_from= is set. A pre-Rule-1 ask with none at all keeps
    the backfill's original fail-open reading (TheLifecycle's existing tests
    already prove that side, with no expect_from= anywhere)."""

    def test_an_unrelated_agent_cannot_close_someone_elses_ask(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", expect_from="grok", due="2026-11-20T00:00:00Z"),
               source="asker-x"),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", closes="A"), source="unrelated-y"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"], "an unauthorised close must be ignored")

    def test_expected_from_itself_may_close_it(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", expect_from="grok", due="2026-11-20T00:00:00Z"),
               source="asker-x"),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", closes="A"), source="grok"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("CLOSED", result["status"])

    def test_the_original_asker_may_also_close_its_own_ask(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", expect_from="grok", due="2026-11-20T00:00:00Z"),
               source="asker-x"),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", closes="A"), source="asker-x"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("CLOSED", result["status"])

    def test_an_unrelated_agent_cannot_answer_it_either(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", expect_from="grok", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", answers="A"), source="unrelated-y"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"])

    def test_an_unrelated_agent_cannot_reopen_it_either(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", expect_from="grok", due="2026-11-20T00:00:00Z"),
               source="asker-x"),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", answers="A"), source="grok"),
            row("2026-11-14T00:10:00Z", bcb("C", "RESULT", reopen="A"), source="unrelated-y"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("ANSWERED", result["status"], "the unauthorised reopen must be ignored")

    def test_ack_requires_expected_from_specifically_not_the_asker(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", expect_from="grok", due="2026-11-20T00:00:00Z"),
               source="asker-x"),
            row("2026-11-14T00:05:00Z", bcb("B", "ACK", answers="A", eta="2026-11-14T01:00:00Z"),
               source="asker-x"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"], "the asker acking its own ask is not in the spec")

    def test_ack_from_expected_from_without_eta_is_incomplete_and_ignored(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", expect_from="grok", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "ACK", answers="A"), source="grok"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"])

    def test_a_pre_rule1_ask_with_no_expect_from_keeps_the_backfill_fail_open_reading(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", closes="A"), source="anyone-at-all"),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("CLOSED", result["status"])


class TheListValuedLinks(unittest.TestCase):
    """Finding #5: a batch closes=/answers= is a ;/,/whitespace-separated
    list on the real board (923 of 8,943 answers= values) - matched as one
    opaque string, it answered nothing."""

    def test_a_semicolon_separated_answers_list_answers_every_named_ask(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:01:00Z", bcb("B", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("C", "RESULT", answers="A;B")),
        ]
        results = rs.reconcile_all(rows, now=NOW)
        self.assertEqual("ANSWERED", results["A"]["status"])
        self.assertEqual("ANSWERED", results["B"]["status"])

    def test_a_comma_separated_closes_list_closes_every_named_ask(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:01:00Z", bcb("B", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("C", "RESULT", closes="A,B")),
        ]
        results = rs.reconcile_all(rows, now=NOW)
        self.assertEqual("CLOSED", results["A"]["status"])
        self.assertEqual("CLOSED", results["B"]["status"])

    def test_tokens_splits_on_comma_semicolon_and_whitespace(self):
        self.assertEqual(["A", "B", "C"], rs.tokens("A; B,C"))
        self.assertEqual([], rs.tokens(None))
        self.assertEqual([], rs.tokens(""))


class TheOnlyRealAnswerCounts(unittest.TestCase):
    """Finding #8: the spec says "Expected_From posts THE RESULT with
    answers=" - a follow-up ASK/REQUEST/DISPATCH/REVIEW_REQUEST or a NOTE
    that merely carries answers= is not an answer (116 asks on the real
    board were wrongly marked ANSWERED by exactly this)."""

    def test_a_follow_up_ask_carrying_answers_does_not_answer_it(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "ASK", answers="A")),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"])

    def test_a_note_carrying_answers_does_not_answer_it_either(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "NOTE", answers="A")),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"])

    def test_a_real_result_still_answers_it(self):
        rows = [
            row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z")),
            row("2026-11-14T00:05:00Z", bcb("B", "RESULT", answers="A")),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("ANSWERED", result["status"])


class TheReopenWithoutAnyDue(unittest.TestCase):
    """Finding #6: Rule 3 says a reopen "returns to OPEN with a new due
    time" - an ask with NO due= at all (the common backfill case: it was
    ANSWERED from history with no due=, then reopened) must not fall into
    the age-bucket backfill classification using its own, often 30+ day,
    original age."""

    def test_a_reopened_ask_with_no_due_anywhere_is_open_not_expired(self):
        old = NOW - timedelta(days=45)
        rows = [
            row(old.strftime("%Y-%m-%dT%H:%M:%SZ"), bcb("A", "ASK")),
            row((old + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               bcb("B", "RESULT", answers="A")),
            row(NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), bcb("C", "RESULT", reopen="A")),
        ]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", result["status"],
                         "a just-reopened ask must not backfill-expire on its original age")
        self.assertEqual(NOW + timedelta(hours=24), result["due_utc"])


class TheHoldNone(unittest.TestCase):
    """Finding #16: hold=none (a GO row explicitly declaring no hold) is not
    a hold - CODEX-SITE209-06D975D-GO-... on the real board carries exactly
    this and must not become HELD."""

    def test_hold_equals_none_is_not_held(self):
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "RESULT", hold="none"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("INFO", result["status"])

    def test_hold_equals_none_case_insensitively_is_also_not_held(self):
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "RESULT", hold="None"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("INFO", result["status"])

    def test_a_real_reason_named_none_as_a_substring_is_still_a_hold(self):
        # "none" must be the WHOLE value, not a substring match.
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "RESULT", hold="nonetheless-hold"))]
        result = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("HELD", result["status"])


class TheIdempotence(unittest.TestCase):
    """Findings #7 and #9: a result's status_updated_utc (and the backfilled
    24h due=) must only move when the actual answer changes - not be
    re-stamped to `now` on every 5-minute run."""

    def test_an_unchanged_expired_ask_keeps_its_old_status_updated_utc(self):
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-14T01:00:00Z"))]
        first = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("EXPIRED", first["status"])
        later = NOW + timedelta(minutes=5)
        second = rs.reconcile_all(rows, now=later, previous={"A": first})["A"]
        self.assertEqual("EXPIRED", second["status"])
        self.assertEqual(first["status_updated_utc"], second["status_updated_utc"],
                         "an unchanged EXPIRED result must not be restamped to the new `now`")

    def test_a_newly_expired_ask_does_get_a_fresh_status_updated_utc(self):
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-14T01:00:00Z"))]
        before_due = datetime(2026, 11, 14, 0, 30, tzinfo=timezone.utc)
        first = rs.reconcile_all(rows, now=before_due)["A"]
        self.assertEqual("OPEN", first["status"])
        second = rs.reconcile_all(rows, now=NOW, previous={"A": first})["A"]
        self.assertEqual("EXPIRED", second["status"])
        self.assertNotEqual(first["status_updated_utc"], second["status_updated_utc"])

    def test_the_backfilled_24h_due_is_stamped_once_and_carried_forward(self):
        young = NOW - timedelta(days=1)
        rows = [row(young.strftime("%Y-%m-%dT%H:%M:%SZ"), bcb("A", "ASK"))]
        first = rs.reconcile_all(rows, now=NOW)["A"]
        first_due = first["due_utc"]
        self.assertEqual(NOW + timedelta(hours=24), first_due)
        later = NOW + timedelta(minutes=5)
        second = rs.reconcile_all(rows, now=later, previous={"A": first})["A"]
        self.assertEqual(first_due, second["due_utc"],
                         "the 24h backfill due must not slide forward on every run")

    def test_a_result_that_actually_changes_still_gets_a_new_status_updated_utc(self):
        rows = [row("2026-11-14T00:00:00Z", bcb("A", "ASK", due="2026-11-20T00:00:00Z"))]
        first = rs.reconcile_all(rows, now=NOW)["A"]
        self.assertEqual("OPEN", first["status"])
        rows.append(row("2026-11-14T00:05:00Z", bcb("B", "RESULT", answers="A")))
        later = NOW + timedelta(minutes=5)
        second = rs.reconcile_all(rows, now=later, previous={"A": first})["A"]
        self.assertEqual("ANSWERED", second["status"])
        self.assertNotEqual(first["status_updated_utc"], second["status_updated_utc"])


if __name__ == "__main__":
    unittest.main()

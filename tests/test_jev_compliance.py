"""J1 compliance pre-filter: regex-primary bare-PR, Jev-decisive availability, in-code
no-ack. Synthetic only; no network, no real board."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jev_compliance as jc  # noqa: E402


class TheBarePrRegex(unittest.TestCase):
    def test_a_prefixed_number_is_not_bare(self):
        self.assertEqual([], jc.bare_pr_numbers("Shipped sfdc-24/conference #440 today."))
        self.assertEqual([], jc.bare_pr_numbers("See sfdc-24/conference#445 for details."))

    def test_a_list_only_prefixes_the_first_is_bare_after(self):
        self.assertEqual(["441", "442"],
                         jc.bare_pr_numbers("opened sfdc-24/conference #440, #441, #442"))

    def test_a_slash_pair_the_second_is_bare(self):
        self.assertEqual(["485"], jc.bare_pr_numbers("sfdc-24/Blackboard #484/#485 still open"))

    def test_pr_prefix_form_is_also_caught(self):
        self.assertEqual(["441"], jc.bare_pr_numbers("verifying PR441 exact-head receipt"))

    def test_a_fully_prefixed_list_is_clean(self):
        self.assertEqual([], jc.bare_pr_numbers(
            "sfdc-24/Blackboard #484 and sfdc-24/Blackboard #485"))


class TheNoAckCheck(unittest.TestCase):
    def test_an_ask_with_no_later_row_naming_it_is_flagged(self):
        ask = {"id": "ASK-1", "phase": "ASK", "text": "to=grok|text=do the thing"}
        later = [{"id": "OTHER", "phase": "RESULT", "text": "unrelated"}]
        self.assertTrue(jc.no_ack(ask, later))

    def test_a_later_row_naming_the_ask_id_clears_it(self):
        ask = {"id": "ASK-1", "phase": "ASK", "text": "do the thing"}
        later = [{"id": "ACK-1", "phase": "RESULT", "text": "answers=ASK-1| done"}]
        self.assertFalse(jc.no_ack(ask, later))

    def test_a_result_row_is_never_flagged_it_is_not_an_ask(self):
        row = {"id": "R-1", "phase": "RESULT", "text": "status update"}
        self.assertFalse(jc.no_ack(row, []))


class TheCheckedRows(unittest.TestCase):
    def test_bare_pr_logs_both_regex_and_jev_and_flags_on_regex_alone(self):
        rows = [{"id": "X", "from": "claude-code-cli", "phase": "RESULT", "ts": "t",
                 "text": "sfdc-24/conference #440, #441"}]
        with mock.patch.object(jc.jev_agent, "ask_yesno", return_value=(0.1, None)):
            results = jc.check_rows(rows, use_jev=True)
        self.assertTrue(results[0]["bare_pr"]["flagged"])
        self.assertEqual(["441"], results[0]["bare_pr"]["numbers"])
        self.assertEqual(0.1, results[0]["bare_pr"]["jev_p"])

    def test_availability_is_jev_decisive_no_regex_fallback(self):
        rows = [{"id": "X", "from": "claude-code-cli", "phase": "RESULT", "ts": "t",
                 "text": "the owner is away until 3pm"}]
        with mock.patch.object(jc.jev_agent, "ask_yesno", return_value=(0.95, None)):
            results = jc.check_rows(rows, use_jev=True)
        self.assertTrue(results[0]["availability"]["flagged"])
        self.assertEqual(0.95, results[0]["availability"]["jev_p"])

    def test_below_threshold_availability_does_not_flag(self):
        rows = [{"id": "X", "from": "claude-code-cli", "phase": "RESULT", "ts": "t", "text": "fine."}]
        with mock.patch.object(jc.jev_agent, "ask_yesno", return_value=(0.4, None)):
            results = jc.check_rows(rows, use_jev=True)
        self.assertFalse(results[0]["availability"]["flagged"])

    def test_a_jev_error_is_logged_not_dropped_and_regex_still_gates(self):
        rows = [{"id": "X", "from": "claude-code-cli", "phase": "RESULT", "ts": "t",
                 "text": "sfdc-24/conference #440 and #441"}]
        with mock.patch.object(jc.jev_agent, "ask_yesno", return_value=(None, "systemone HTTP 500")):
            results = jc.check_rows(rows, use_jev=True)
        self.assertTrue(results[0]["bare_pr"]["flagged"], "regex still gates on a Jev error")
        self.assertIsNone(results[0]["bare_pr"]["jev_p"])
        self.assertEqual("systemone HTTP 500", results[0]["bare_pr"]["jev_error"])

    def test_dry_mode_never_calls_jev(self):
        rows = [{"id": "X", "from": "claude-code-cli", "phase": "RESULT", "ts": "t", "text": "fine."}]
        with mock.patch.object(jc.jev_agent, "ask_yesno", side_effect=AssertionError("must not call")):
            results = jc.check_rows(rows, use_jev=False)
        self.assertIsNone(results[0]["availability"]["jev_p"])
        self.assertFalse(results[0]["availability"]["flagged"])


class TheRowParsing(unittest.TestCase):
    def test_rows_with_no_id_are_skipped(self):
        raw = [["r1", "2026-11-14T00:00:00Z", "x", "y", "APPEND", "no id here"],
              ["r2", "2026-11-14T00:00:00Z", "x", "y", "APPEND",
               "BCB|v=1|id=GOOD|phase=RESULT|text=hi"]]
        parsed = jc.parse_rows(raw)
        self.assertEqual(["GOOD"], [p["id"] for p in parsed])

    def test_flagged_only_filters_clean_rows(self):
        results = [
            {"bare_pr": {"flagged": False}, "no_ack": {"flagged": False},
             "availability": {"flagged": False}},
            {"bare_pr": {"flagged": True}, "no_ack": {"flagged": False},
             "availability": {"flagged": False}},
        ]
        self.assertEqual(1, len(jc.flagged_only(results)))


class TheMainEntrypoint(unittest.TestCase):
    def test_dry_run_reads_rows_and_calls_no_jev(self):
        rows = [["r1", "2026-11-14T00:00:00Z", "claude-code-cli", "all", "APPEND",
                 "BCB|v=1|id=ROW-1|phase=RESULT|text=hi"]]
        with mock.patch.object(jc.board, "load_env", return_value={"BUS_SECRET": "s"}), \
             mock.patch.object(jc.board, "bus", return_value=(200, json.dumps({"rows": rows}))), \
             mock.patch.object(jc.jev_agent, "ask_yesno", side_effect=AssertionError("must not call")):
            with mock.patch.object(sys, "argv", ["jev_compliance.py", "2026-11-13T00:00:00Z", "--dry"]):
                code = jc.main()
        self.assertEqual(0, code)

    def test_a_bad_since_argument_fails_loudly(self):
        with mock.patch.object(sys, "argv", ["jev_compliance.py", "not-a-date"]):
            self.assertEqual(2, jc.main())


if __name__ == "__main__":
    unittest.main()

"""J2 inbox triage: four Jev labels, nothing dropped, 0.5-0.8 confidence flagged "check".
Synthetic only; no network, no real board."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jev_inbox as ji  # noqa: E402


class TheAddressing(unittest.TestCase):
    def test_to_claude_code_cli_matches_a_single_recipient(self):
        self.assertTrue(ji.addressed_to("to=claude-code-cli|text=x", "claude-code-cli"))
        self.assertFalse(ji.addressed_to("to=grok|text=x", "claude-code-cli"))

    def test_to_matches_inside_a_semicolon_list(self):
        self.assertTrue(ji.addressed_to("to=grok;claude-code-cli|text=x", "claude-code-cli"))

    def test_all_matches_every_row_regardless_of_to(self):
        self.assertTrue(ji.addressed_to("to=grok|text=x", "all"))


class TheClassifier(unittest.TestCase):
    def test_a_confident_label_passes_through(self):
        with mock.patch.object(ji.jev_agent, "ask_choice",
                               return_value=(("needs-action", 0.93), None)):
            label, conf, status, error = ji.classify("ASK: please ship it")
        self.assertEqual(("needs-action", 0.93, "confident", None), (label, conf, status, error))

    def test_confidence_in_the_check_band_is_flagged(self):
        with mock.patch.object(ji.jev_agent, "ask_choice",
                               return_value=(("verdict", 0.6), None)):
            _label, _conf, status, _error = ji.classify("borderline row")
        self.assertEqual("check", status)

    def test_a_reply_row_still_gets_a_label_not_dropped(self):
        with mock.patch.object(ji.jev_agent, "ask_choice",
                               return_value=(("owner-decision", 0.9), None)):
            label, _conf, status, _error = ji.classify(
                "GEMINI-WAKE reply to CLAUDE-CLI-ASK: REPLY: A. Scope the rule to fleet state.")
        self.assertEqual("owner-decision", label)
        self.assertEqual("confident", status)

    def test_a_jev_error_is_logged_as_error_status_not_dropped(self):
        with mock.patch.object(ji.jev_agent, "ask_choice", return_value=(None, "systemone HTTP 500")):
            label, conf, status, error = ji.classify("some row")
        self.assertIsNone(label)
        self.assertIsNone(conf)
        self.assertEqual("error", status)
        self.assertEqual("systemone HTTP 500", error)


class TheTriage(unittest.TestCase):
    def test_every_row_appears_in_the_output_even_on_error(self):
        rows = [{"id": "A", "from": "x", "text": "row a"}, {"id": "B", "from": "y", "text": "row b"}]
        with mock.patch.object(ji.jev_agent, "ask_choice",
                               side_effect=[(("info", 0.9), None), (None, "boom")]):
            results = ji.triage(rows)
        self.assertEqual(2, len(results))
        self.assertEqual("info", results[0]["label"])
        self.assertEqual("error", results[1]["status"])


class TheMainEntrypoint(unittest.TestCase):
    def test_dry_run_calls_no_jev(self):
        rows = [["r1", "2026-11-14T00:00:00Z", "grok", "claude-code-cli", "APPEND",
                 "BCB|v=1|id=ROW-1|to=claude-code-cli|text=hi"]]
        with mock.patch.object(ji.board, "load_env", return_value={"BUS_SECRET": "s"}), \
             mock.patch.object(ji.board, "bus", return_value=(200, json.dumps({"rows": rows}))), \
             mock.patch.object(ji.jev_agent, "ask_choice", side_effect=AssertionError("must not call")):
            with mock.patch.object(sys, "argv",
                                   ["jev_inbox.py", "2026-11-13T00:00:00Z", "claude-code-cli", "--dry"]):
                code = ji.main()
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()

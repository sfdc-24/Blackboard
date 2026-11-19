"""R-STATUS Rule 5: ASK_PHASES and the default-Category rule every writer shares."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ask_phases as ap  # noqa: E402


class TheAskPhaseSet(unittest.TestCase):
    def test_exactly_the_five_phases_the_doc_names(self):
        self.assertEqual({"ASK", "REQUEST", "DISPATCH", "REVIEW_REQUEST", "TASK"}, set(ap.ASK_PHASES))

    def test_is_ask_phase_is_case_insensitive(self):
        self.assertTrue(ap.is_ask_phase("ask"))
        self.assertTrue(ap.is_ask_phase("Review_Request"))
        self.assertFalse(ap.is_ask_phase("result"))
        self.assertFalse(ap.is_ask_phase(""))
        self.assertFalse(ap.is_ask_phase(None))


class ThePhaseExtraction(unittest.TestCase):
    def test_phase_of_reads_the_bcb_field(self):
        self.assertEqual("ASK", ap.phase_of("BCB|v=1|id=X|phase=ASK|from=a|text=hi"))
        self.assertEqual("RESULT", ap.phase_of("BCB|v=1|id=X|phase=result|from=a"))

    def test_phase_of_is_blank_without_a_phase_field(self):
        self.assertEqual("", ap.phase_of("BCB|v=1|id=X|from=a|text=no phase here"))
        self.assertEqual("", ap.phase_of(""))
        self.assertEqual("", ap.phase_of(None))

    def test_phase_of_does_not_match_a_value_containing_the_word_phase(self):
        self.assertEqual("ASK", ap.phase_of("BCB|v=1|note=the phase= word appears in prose|phase=ASK"))


class TheDefaultCategory(unittest.TestCase):
    def test_an_ask_type_phase_defaults_to_open(self):
        for phase in ("ASK", "REQUEST", "DISPATCH", "REVIEW_REQUEST", "TASK"):
            with self.subTest(phase=phase):
                self.assertEqual("OPEN", ap.default_category("BCB|v=1|id=X|phase=%s" % phase))
                self.assertEqual("OPEN", ap.default_category_for_phase(phase))

    def test_a_reply_or_note_phase_defaults_to_blank(self):
        for phase in ("RESULT", "ACK", "NOTE", "DONE", "STATUS", "DECISION"):
            with self.subTest(phase=phase):
                self.assertEqual("", ap.default_category("BCB|v=1|id=X|phase=%s" % phase))
                self.assertEqual("", ap.default_category_for_phase(phase))

    def test_no_phase_at_all_defaults_to_blank(self):
        self.assertEqual("", ap.default_category("BCB|v=1|id=X|from=a|text=no phase"))


if __name__ == "__main__":
    unittest.main()

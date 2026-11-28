"""J3 PR receipt depth: Jev concurrency check at a 0.3 yes-threshold, with a deterministic
keyword fallback on any Jev error. Synthetic only; no network, no real gh/PR."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jev_pr_depth as jp  # noqa: E402


class TheKeywordFallback(unittest.TestCase):
    def test_concurrency_words_are_caught(self):
        self.assertTrue(jp.keyword_fallback("fixes a race in the lock-held poll thread"))

    def test_plain_prose_is_not_flagged(self):
        self.assertFalse(jp.keyword_fallback("fixes a typo in the README"))


class TheRecommendation(unittest.TestCase):
    def test_a_low_jev_p_below_threshold_is_normal_depth(self):
        data = {"title": "docs: fix typo", "body": "", "files": [{"path": "README.md"}]}
        with mock.patch.object(jp.jev_agent, "ask_yesno", return_value=(0.1, None)):
            result = jp.recommend(data)
        self.assertEqual("normal", result["depth"])
        self.assertEqual("jev", result["source"])
        self.assertIsNone(result["instructions"])

    def test_p_at_or_above_the_low_threshold_is_deep(self):
        data = {"title": "fix queue_link poll race", "body": "", "files": [{"path": "queue_link.py"}]}
        with mock.patch.object(jp.jev_agent, "ask_yesno", return_value=(0.31, None)):
            result = jp.recommend(data)
        self.assertEqual("deep", result["depth"])
        self.assertIn("15+ times on Linux and Windows", result["instructions"])

    def test_a_jev_error_falls_back_to_keywords_and_says_so(self):
        data = {"title": "fix a lock contention bug in the binder", "body": "", "files": []}
        with mock.patch.object(jp.jev_agent, "ask_yesno", return_value=(None, "systemone HTTP 500")):
            result = jp.recommend(data)
        self.assertEqual("keyword_fallback", result["source"])
        self.assertEqual("systemone HTTP 500", result["jev_error"])
        self.assertEqual("deep", result["depth"])

    def test_a_jev_error_with_no_keyword_hit_falls_back_to_normal(self):
        data = {"title": "docs: fix typo", "body": "", "files": []}
        with mock.patch.object(jp.jev_agent, "ask_yesno", return_value=(None, "boom")):
            result = jp.recommend(data)
        self.assertEqual("normal", result["depth"])


class ThePrMetadata(unittest.TestCase):
    def test_a_short_name_gets_the_sfdc24_prefix(self):
        with mock.patch.object(jp.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"title":"t","body":"b","files":[]}')
            jp.pr_metadata("conference", 445)
        called_repo = run.call_args[0][0][run.call_args[0][0].index("--repo") + 1]
        self.assertEqual("sfdc-24/conference", called_repo)

    def test_a_nonzero_exit_is_an_error_not_a_crash(self):
        with mock.patch.object(jp.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stderr="no such PR", stdout="")
            data, error = jp.pr_metadata("conference", 999999)
        self.assertIsNone(data)
        self.assertIn("no such PR", error)


class TheMainEntrypoint(unittest.TestCase):
    def test_dry_run_calls_no_jev(self):
        with mock.patch.object(jp, "pr_metadata",
                               return_value=({"title": "t", "body": "b", "files": []}, None)), \
             mock.patch.object(jp.jev_agent, "ask_yesno", side_effect=AssertionError("must not call")):
            with mock.patch.object(sys, "argv", ["jev_pr_depth.py", "conference", "445", "--dry"]):
                code = jp.main()
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()

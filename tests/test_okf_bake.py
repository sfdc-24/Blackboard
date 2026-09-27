"""Contract tests for docs/okf/bake.py blocker and checks-state logic."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BAKE_PATH = ROOT / "docs" / "okf" / "bake.py"


spec = importlib.util.spec_from_file_location("okf_bake", BAKE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"could not load module from {BAKE_PATH}")
okf_bake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(okf_bake)


class OkfBakeLogicTests(unittest.TestCase):
    def test_checks_state_uses_combined_status_failure(self) -> None:
        check_runs = {
            "check_runs": [
                {"status": "completed", "conclusion": "success"},
            ]
        }
        combined = {"state": "failure"}
        self.assertEqual(okf_bake.checks_state(check_runs, combined), "failure")

    def test_checks_state_pending_for_incomplete_runs(self) -> None:
        check_runs = {
            "check_runs": [
                {"status": "in_progress", "conclusion": None},
            ]
        }
        combined = {"state": "success"}
        self.assertEqual(okf_bake.checks_state(check_runs, combined), "pending")

    def test_checks_state_treats_expected_as_pending(self) -> None:
        check_runs = {
            "check_runs": [
                {"status": "completed", "conclusion": "success"},
            ]
        }
        combined = {"state": "expected"}
        self.assertEqual(okf_bake.checks_state(check_runs, combined), "pending")

    def test_blocker_list_uses_explicit_label_allowlist(self) -> None:
        pr = {
            "draft": True,
            "labels": [
                {"name": "needs-review"},
                {"name": "needs-decision"},
                {"name": "blocker:release"},
            ],
        }

        blockers = okf_bake.blocker_list(pr, "pending")

        self.assertIn("Draft PR", blockers)
        self.assertIn("Checks pending", blockers)
        self.assertIn("Labels: blocker:release, needs-decision", blockers)
        self.assertNotIn("needs-review", " ".join(blockers))

    def test_short_ts_formats_valid_and_preserves_invalid(self) -> None:
        self.assertEqual(okf_bake.short_ts("2026-09-27T02:30:42Z"), "2026-09-27 02:30Z")
        self.assertEqual(okf_bake.short_ts("not-a-time"), "not-a-time")

    def test_build_markdown_includes_frontmatter_and_empty_state(self) -> None:
        text = okf_bake.build_markdown("sfdc-24", "Blackboard", [])
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("title: Blackboard OKF Cooking", text)
        self.assertIn("## Open PRs", text)
        self.assertIn("No open pull requests.", text)
        self.assertIn("## Blocker rollup", text)

    def test_build_markdown_renders_rollup(self) -> None:
        rows = [
            {
                "number": 10,
                "title": "Alpha",
                "url": "https://example.test/10",
                "author": "owner",
                "updated_at": "2026-09-27T02:30:42Z",
                "checks": "pending",
                "blockers": ["Draft PR", "Checks pending"],
            },
            {
                "number": 11,
                "title": "Beta",
                "url": "https://example.test/11",
                "author": "owner",
                "updated_at": "2026-09-27T01:30:42Z",
                "checks": "success",
                "blockers": [],
            },
        ]
        text = okf_bake.build_markdown("sfdc-24", "Blackboard", rows)
        self.assertIn("| [#10](https://example.test/10) | Alpha |", text)
        self.assertIn("- Draft PR: 1", text)
        self.assertIn("- Checks pending: 1", text)
        self.assertIn("- None declared: 1", text)


if __name__ == "__main__":
    unittest.main()

"""Offline contract for the single Governor production deploy source."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SCRIPT_ID = "1lTbqTZ3DBHI2WyJu19Lf1M0a4J01aEuH45c2VcT6Rzxy0vxE2S8FYUEp"
CANONICAL_ROOT = "apps-script/governor-page-api"
# clasp writes server-side JavaScript as .js, so the verbatim pull in #167
# renamed all four script files. The extension is part of the contract: a
# rootDir holding Code.gs BESIDE Code.js is the hazard docs/APPS-SCRIPT-DEPLOY.md
# warns about under "Never leave Auth.gs beside Auth.js" - two copies of one
# server file, and the older can win.
EXPECTED_SOURCE = {
    "Auth.js",
    "Code.js",
    "Index.html",
    "Monitor.js",
    "PublicInbox.js",
    "Reception.html",
    "appsscript.json",
}


def tracked_files(path: str) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "--", path],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


class DeploySourceContractTests(unittest.TestCase):
    def test_root_clasp_targets_the_tracked_governor_source(self) -> None:
        config = json.loads((ROOT / ".clasp.json").read_text(encoding="utf-8"))
        self.assertEqual(set(config), {"scriptId", "rootDir"})
        self.assertEqual(config.get("scriptId"), PRODUCTION_SCRIPT_ID)
        self.assertEqual(config.get("rootDir"), CANONICAL_ROOT)

        source_root = ROOT / CANONICAL_ROOT
        self.assertTrue(source_root.is_dir())
        actual = {path.name for path in source_root.iterdir() if path.is_file()}
        self.assertEqual(actual, EXPECTED_SOURCE)
        self.assertEqual(
            tracked_files(CANONICAL_ROOT),
            {f"{CANONICAL_ROOT}/{name}" for name in EXPECTED_SOURCE},
        )

    def test_legacy_gas_tree_is_scratch_only(self) -> None:
        self.assertEqual(tracked_files("gas"), {"gas/.gitkeep"})
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("gas/*", ignore)
        self.assertIn("!gas/.gitkeep", ignore)
        self.assertIn("never a deploy input", ignore)

    def test_operator_guidance_cannot_pull_over_reviewed_source(self) -> None:
        handover = (ROOT / "docs/HANDOVER.md").read_text(encoding="utf-8")
        cicd = (ROOT / "docs/CICD.md").read_text(encoding="utf-8")
        self.assertIn("Never run `clasp pull` from the repo root", handover)
        self.assertIn("scripts/gas_get_version.py", handover)
        self.assertIn("gas/` is ignored scratch", cicd)
        self.assertIn("apps-script/governor-page-api", cicd)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Exercise the actual fixture harness with controlled runners and file failures.

No mailbox, bus, provider, service or cloud access. Set ORDER_TEST_ENGINE to
select an installed PowerShell; Windows defaults to 5.1, other hosts to pwsh.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
ENGINE = os.environ.get("ORDER_TEST_ENGINE") or shutil.which(
    "powershell.exe" if os.name == "nt" else "pwsh")


class HarnessContract(unittest.TestCase):
    def setUp(self):
        self.assertTrue(ENGINE, "an installed PowerShell is required")
        self.temp = tempfile.TemporaryDirectory(prefix="order-harness-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tests").mkdir()
        (self.root / "scripts").mkdir()
        self.harness = self.root / "tests/order_fixture_crosscheck.ps1"
        shutil.copyfile(REPO / "tests/order_fixture_crosscheck.ps1", self.harness)
        self.fixture = self.root / "fixture.json"
        self.fixture.write_text("{}", encoding="utf-8")
        self.outfile = self.root / "result.json"

    def runner(self, exit_code=0, ok="$true", write_state=True, write_log=True):
        # The stub has no input/output network API and models explicit process
        # outcomes, including exit!=0 despite a success-looking stdout verdict.
        content = r'''
param($Mode, $BoardFixturePath, $StatePath, $LogPath, $WorkspacePath,
      $MaxOrderAgeMinutes, [switch]$ReplayHistorical)
'''
        if write_state:
            content += r'''
[IO.File]::WriteAllText($StatePath, '{"schema":"fixture-state","cursor":"row-1","last_poll":{"identity":"test-user","user_profile":"/home/test"}}')
'''
        if write_log:
            content += r'''
[IO.File]::AppendAllText($LogPath, '{"event":"poll_started","level":"info","code":"","row_id":""}' + "`n")
'''
        content += "@{ok=" + ok + ";status='candidate_observed';mode='Observe'} | ConvertTo-Json -Compress\n"
        content += "exit " + str(exit_code) + "\n"
        (self.root / "scripts/order_supervisor.ps1").write_text(content, encoding="utf-8")

    def run_harness(self, destination=None, real=False):
        child_env = os.environ.copy()
        if Path(ENGINE).name.lower() == "powershell.exe":
            # Python can inherit Core-only module paths from a pwsh parent.
            # Let a fresh 5.1 child construct its own standard module path.
            child_env = {k: v for k, v in child_env.items() if k.lower() != 'psmodulepath'}
        # -ExecutionPolicy Bypass is not optional here, and every other
        # PowerShell-invoking test in this repo passes it. The harness is copied
        # into a temp directory and run from there, so on a machine whose
        # LocalMachine policy is AllSigned the copy is unsigned and refuses to
        # load: 4 failures and 1 error, all UnauthorizedAccess, on the laptop
        # that owns this branch. Hosted runners are permissive, so CI was green
        # while the suite could not run for its own author.
        proc = subprocess.run(
            [ENGINE, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             str(REPO / "tests/order_fixture_crosscheck.ps1" if real else self.harness),
             "-FixturePath", str(REPO / "tests/fixtures/order_supervisor_board.json" if real else self.fixture),
             "-OutFile", str(destination or self.outfile)],
            capture_output=True, text=True, timeout=90, env=child_env)
        return proc.returncode, proc.stdout + proc.stderr

    def test_real_fixture_emits_complete_success(self):
        code, out = self.run_harness(real=True)
        self.assertEqual(code, 0, out)
        report = json.loads(self.outfile.read_text(encoding="utf-8-sig"))
        self.assertEqual({(p["scenario"], p["pass"]) for p in report["passes"]},
                         {(s, n) for s in ("seeded", "replay") for n in (1, 2)})
        self.assertEqual([p["exit_code"] for p in report["passes"]], [0] * 4)
        for state in report["state"].values():
            self.assertEqual(json.loads(state)["last_poll"]["identity"], "<EXECUTION_IDENTITY>")
            self.assertEqual(json.loads(state)["seen"]["timestamp"], "2026-09-06T04:04:00.0000000Z")
        self.assertEqual(json.loads(report["state"]["seeded"])["cursor"]["timestamp"],
                         "2026-09-06T04:04:00.0000000Z")
        self.assertIn("old-order-044", report["passes"][2]["stdout"])

    def test_nonzero_runner_cannot_claim_success(self):
        self.runner(exit_code=9)
        code, out = self.run_harness()
        self.assertNotEqual(code, 0, out)
        report = json.loads(self.outfile.read_text())
        self.assertEqual([p["exit_code"] for p in report["passes"]], [9] * 4)

    def test_string_true_cannot_claim_success(self):
        self.runner(ok="'true'")
        code, out = self.run_harness()
        self.assertNotEqual(code, 0, out)
        self.assertIn("Boolean true", out)

    def test_missing_state_is_failure(self):
        self.runner(write_state=False)
        code, out = self.run_harness()
        self.assertNotEqual(code, 0, out)

    def test_missing_log_is_failure(self):
        self.runner(write_log=False)
        code, out = self.run_harness()
        self.assertNotEqual(code, 0, out)

    def test_missing_parent_does_not_say_wrote(self):
        self.runner()
        destination = self.root / "absent/result.json"
        code, out = self.run_harness(destination)
        self.assertNotEqual(code, 0, out)
        self.assertIn("ARTIFACT_WRITE_FAILED", out)
        self.assertNotIn("wrote ", out)
        self.assertFalse(destination.exists())

    def test_existing_artifact_is_atomically_replaced(self):
        self.runner()
        self.outfile.write_text("stale artifact", encoding="utf-8")
        code, out = self.run_harness()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(json.loads(self.outfile.read_text())["passes"]), 4)
        self.assertEqual(list(self.root.glob("result.json.*.tmp")), [])

    def test_directory_destination_remains_intact(self):
        self.runner()
        self.outfile.mkdir()
        marker = self.outfile / "keep.txt"
        marker.write_text("preserve")
        code, out = self.run_harness()
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("wrote ", out)
        self.assertEqual(marker.read_text(), "preserve")
        self.assertEqual(list(self.root.glob("result.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""cloud/claude-code-cloud/setup.sh (Blackboard #306, Stage C1): the default changes nothing, --apply refuses without
the owner's GO, and the plan holds exactly the least-privilege shape the spec names. Runs offline. A fake `gcloud`
on PATH records every call, so a dry run that calls any mutating gcloud command fails the test."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "cloud" / "claude-code-cloud" / "setup.sh"
FAKE = """#!/usr/bin/env bash
echo "$*" >> "$CCC_FAKE_LOG"
case "$*" in
  "run jobs describe board-watcher"*) echo "board-watcher@sfdc24.iam.gserviceaccount.com" ;;
esac
exit 0
"""


@unittest.skipIf(sys.platform == "win32", "bash and a fake gcloud on PATH: run on Linux CI")
class SetupScriptTest(unittest.TestCase):
    def run_script(self, *args, go=None):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "gcloud"
            fake.write_text(FAKE)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            log = Path(tmp) / "calls.log"
            env = dict(os.environ, PATH="%s:%s" % (tmp, os.environ.get("PATH", "")), CCC_FAKE_LOG=str(log))
            env.pop("CCC_OWNER_GO", None)
            if go:
                env["CCC_OWNER_GO"] = go
            out = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)
            calls = log.read_text().splitlines() if log.exists() else []
            return out, calls

    def test_the_default_is_a_dry_run_whose_only_cloud_call_is_a_read(self):
        out, calls = self.run_script()
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("DRY RUN: nothing is changed", out.stdout)
        self.assertEqual(1, len(calls), calls)                          # the board-watcher identity read, only
        self.assertTrue(calls[0].startswith("run jobs describe board-watcher"), calls)

    def test_apply_refuses_without_the_owner_go(self):
        out, calls = self.run_script("--apply")
        self.assertEqual(2, out.returncode)
        self.assertIn("REFUSED", out.stderr)
        self.assertEqual([], calls)

    def test_the_plan_has_the_least_privilege_shape(self):
        out, _ = self.run_script()
        plan = out.stdout
        for needed in ("--no-allow-unauthenticated", "--max-retries 0", "ccc-receipts-only",
                       "--location northamerica-northeast2", "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY_CLOUD:latest",
                       "--member serviceAccount:claude-code-cloud@sfdc24.iam.gserviceaccount.com --role "
                       "roles/run.invoker",
                       "--member serviceAccount:board-watcher@sfdc24.iam.gserviceaccount.com --role roles/run.invoker"):
            self.assertIn(needed, plan)
        for forbidden in ("allUsers", "allAuthenticatedUsers", "roles/owner", "roles/editor",
                          "roles/secretmanager.admin", " delete ", "remove-iam-policy-binding", "--condition=None"):
            self.assertNotIn(forbidden, plan)
        # The C2 App keys are named but not granted in C1.
        self.assertIn("C2 ONLY, not applied in C1", plan)
        self.assertNotIn("add-iam-policy-binding GITHUB_APP", plan)

    def test_apply_with_a_go_runs_the_same_commands_it_printed(self):
        dry, _ = self.run_script()
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST")
        self.assertEqual(0, out.returncode, out.stderr)
        printed = [line.strip()[len("gcloud "):] for line in dry.stdout.splitlines() if line.strip().startswith("gcloud ")]
        self.assertEqual(printed, calls[1:])                            # after the identity read


if __name__ == "__main__":
    unittest.main()

"""cloud/claude-code-cloud/setup.sh (Blackboard #306, Stage C1): the default changes nothing, --apply refuses without
the owner's GO and Cursor's exact-head GO, every read runs before the first change (create-or-refuse), and the plan
holds exactly the least-privilege shape the spec names. Runs offline. A fake `gcloud` on PATH records every call, so
a dry run or a refusal that calls any mutating gcloud command fails the test."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "cloud" / "claude-code-cloud" / "setup.sh"
# describe: PRESENT when its arguments hold an entry of CCC_FAKE_PRESENT (';'-separated), a non-gcloud failure for
# CCC_FAKE_DENIED, otherwise gcloud's own NOT_FOUND. Every other command succeeds.
FAKE = """#!/usr/bin/env bash
echo "$*" >> "$CCC_FAKE_LOG"
case "$*" in
  *" describe "*)
    IFS=';' read -ra denied <<< "${CCC_FAKE_DENIED:-}"
    for d in "${denied[@]}"; do
      if [ -n "$d" ] && [[ "$*" == *"$d"* ]]; then
        echo "ERROR: (gcloud.fake) PERMISSION_DENIED: caller lacks permission" >&2; exit 1
      fi
    done
    IFS=';' read -ra present <<< "${CCC_FAKE_PRESENT:-}"
    for p in "${present[@]}"; do
      if [ -n "$p" ] && [[ "$*" == *"$p"* ]]; then
        case "$*" in
          "run jobs describe board-watcher"*) echo "${CCC_FAKE_WATCHER:-board-watcher@sfdc24.iam.gserviceaccount.com}" ;;
          *) echo "found" ;;
        esac
        exit 0
      fi
    done
    echo "ERROR: (gcloud.fake.describe) NOT_FOUND: Resource not found" >&2; exit 1 ;;
esac
exit 0
"""
TAG = "0123abc"
REG = "us-central1-docker.pkg.dev/sfdc24/cloud-run-source-deploy/"
IMAGES = ("images describe %sccc-broker:%s" % (REG, TAG), "images describe %sclaude-code-cloud:%s" % (REG, TAG))
EXISTING = ";".join(("secrets describe BUS_URL", "secrets describe BUS_SECRET", "run jobs describe board-watcher")
                    + IMAGES)
NEW = ("iam service-accounts describe claude-code-cloud@", "iam service-accounts describe ccc-broker@",
       "secrets describe ANTHROPIC_API_KEY_CLOUD", "firestore databases describe --database ccc-receipts",
       "run services describe ccc-broker", "run jobs describe claude-code-cloud")
CONDITION = 'expression=resource.name=="projects/sfdc24/databases/ccc-receipts"'


def mutating(calls):
    return [c for c in calls if " describe " not in " %s " % c]


@unittest.skipIf(sys.platform == "win32", "bash and a fake gcloud on PATH: run on Linux CI")
class SetupScriptTest(unittest.TestCase):
    def setUp(self):
        # A committed copy of setup.sh in its own repo, so the exact-head gate has a HEAD to check against.
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        script = self.repo / "cloud" / "claude-code-cloud" / "setup.sh"
        script.parent.mkdir(parents=True)
        shutil.copy(SCRIPT, script)
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "c1"], check=True)
        self.head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True,
                                   check=True).stdout.strip()
        self.script = script
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake = bindir / "gcloud"
        fake.write_text(FAKE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.bindir = bindir

    def run_script(self, *args, go=None, sha=None, present=EXISTING, denied="", watcher=None, broker_tag=TAG,
                   job_tag=TAG):
        log = self.tmp / ("calls-%d.log" % len(list(self.tmp.glob("calls-*.log"))))
        env = dict(os.environ, PATH="%s:%s" % (self.bindir, os.environ.get("PATH", "")), CCC_FAKE_LOG=str(log),
                   CCC_FAKE_PRESENT=present, CCC_FAKE_DENIED=denied)
        for k in ("CCC_OWNER_GO", "CCC_CURSOR_GO_SHA", "CCC_FAKE_WATCHER", "CCC_PROJECT", "CCC_REGION",
                  "CCC_BROKER_TAG", "CCC_JOB_TAG"):
            env.pop(k, None)
        if go:
            env["CCC_OWNER_GO"] = go
        if sha:
            env["CCC_CURSOR_GO_SHA"] = sha
        if broker_tag is not None:
            env["CCC_BROKER_TAG"] = broker_tag
        if job_tag is not None:
            env["CCC_JOB_TAG"] = job_tag
        if watcher is not None:
            env["CCC_FAKE_WATCHER"] = watcher
        out = subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, env=env)
        calls = log.read_text().splitlines() if log.exists() else []
        return out, calls

    def test_the_default_is_a_dry_run_whose_cloud_calls_are_reads(self):
        out, calls = self.run_script()
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("DRY RUN: nothing is changed", out.stdout)
        self.assertEqual([], mutating(calls), calls)
        self.assertEqual(11, len(calls), calls)    # 8 existence reads, the board-watcher identity, 2 images

    def test_apply_refuses_without_both_gos_before_any_cloud_call(self):
        cases = {
            "no owner GO": dict(sha=None),
            "no Cursor GO": dict(go="OWNER-GO-TEST"),
            "short sha": dict(go="OWNER-GO-TEST", sha="HEAD7SHA"),
            "another commit": dict(go="OWNER-GO-TEST", sha="0" * 40),
        }
        for why, kw in cases.items():
            out, calls = self.run_script("--apply", **kw)
            self.assertEqual(2, out.returncode, why)
            self.assertIn("REFUSED", out.stderr, why)
            self.assertEqual([], calls, why)

    def test_apply_refuses_when_the_script_differs_from_the_reviewed_commit(self):
        self.script.write_text(self.script.read_text() + "\n# an unreviewed edit\n")
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head)
        self.assertEqual(2, out.returncode)
        self.assertIn("differs", out.stderr)
        self.assertEqual([], calls)

    def test_the_plan_has_the_least_privilege_shape(self):
        out, _ = self.run_script()
        plan = out.stdout
        for needed in ("--no-allow-unauthenticated", "--max-retries 0", "title=ccc-receipts-only," + CONDITION,
                       "--location northamerica-northeast2", "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY_CLOUD:latest",
                       "--member serviceAccount:claude-code-cloud@sfdc24.iam.gserviceaccount.com --role "
                       "roles/run.invoker",
                       "--member serviceAccount:board-watcher@sfdc24.iam.gserviceaccount.com --role roles/run.invoker",
                       "--image %sccc-broker:%s " % (REG, TAG), "--image %sclaude-code-cloud:%s " % (REG, TAG)):
            self.assertIn(needed, plan)
        for forbidden in ("allUsers", "allAuthenticatedUsers", "roles/owner", "roles/editor", "startsWith", "UNSET",
                          "roles/secretmanager.admin", " delete ", "remove-iam-policy-binding", "--condition=None"):
            self.assertNotIn(forbidden, plan)
        # The C2 App keys are named but not granted in C1.
        self.assertIn("C2 ONLY, not applied in C1", plan)
        self.assertNotIn("add-iam-policy-binding GITHUB_APP", plan)

    def test_apply_with_both_gos_runs_every_read_first_then_the_commands_it_printed(self):
        dry, dry_calls = self.run_script()
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head)
        self.assertEqual(0, out.returncode, out.stderr)
        printed = [line.strip()[len("gcloud "):] for line in dry.stdout.splitlines() if line.strip().startswith("gcloud ")]
        self.assertEqual(printed, mutating(calls))            # the describes are removed by content, not position
        reads = [c for c in calls if c not in printed]
        self.assertEqual(dry_calls, reads)
        self.assertEqual(reads, calls[:len(reads)])           # every read before the first change

    def test_apply_refuses_on_any_collision_missing_input_or_unclear_read(self):
        cases = {}
        for new in NEW:
            cases["%s exists" % new] = dict(present=EXISTING + ";" + new)
        for gone in ("secrets describe BUS_URL", "secrets describe BUS_SECRET", "run jobs describe board-watcher"):
            cases["%s missing" % gone] = dict(present=EXISTING.replace(gone, "x-absent-x"))
        cases["permission denied on a new resource"] = dict(denied="run services describe ccc-broker")
        cases["permission denied on board-watcher"] = dict(denied="run jobs describe board-watcher")
        cases["board-watcher identity not an SA"] = dict(watcher="<read live>")
        # Codex P1 on 21ff80e: an unset tag failed the deploy only after the first creates.
        cases["broker tag unset"] = dict(broker_tag=None)
        cases["job tag unset"] = dict(job_tag=None)
        cases["broker tag not a tag"] = dict(broker_tag="x y")
        cases["job tag not a tag"] = dict(job_tag="-rf")
        for image in IMAGES:
            cases["%s not pushed" % image] = dict(present=EXISTING.replace(image, "x-absent-x"))
        for why, kw in cases.items():
            out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head, **kw)
            self.assertEqual(3, out.returncode, "%s: %s" % (why, out.stderr))
            self.assertIn("REFUSED: the preflight", out.stderr, why)
            self.assertEqual([], mutating(calls), why)
            # A dry run with the same reads reports it and still changes nothing.
            dry, dry_calls = self.run_script(**kw)
            self.assertEqual(0, dry.returncode, why)
            self.assertIn("--apply would REFUSE", dry.stderr, why)
            self.assertEqual([], mutating(dry_calls), why)


if __name__ == "__main__":
    unittest.main()

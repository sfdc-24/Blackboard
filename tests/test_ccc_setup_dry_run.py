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
  *"run deploy ccc-broker"*|*"run jobs create claude-code-cloud"*)
    # CCC_FAKE_FLAKY="<pattern>:<n>;...": the first n calls matching <pattern> fail as an IAM grant still propagating.
    IFS=';' read -ra flaky <<< "${CCC_FAKE_FLAKY:-}"
    for f in "${flaky[@]}"; do
      pat="${f%:*}"; n="${f##*:}"
      if [ -n "$pat" ] && [[ "$*" == *"$pat"* ]] && [ "$(grep -cF -- "$pat" "$CCC_FAKE_LOG")" -le "$n" ]; then
        echo "ERROR: (gcloud.fake) PERMISSION_DENIED: Permission 'secretmanager.versions.access' denied" >&2; exit 1
      fi
    done
    exit 0 ;;
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
          "secrets versions describe"*) echo "${CCC_FAKE_STATE:-ENABLED}" ;;
          "projects describe"*) echo "${CCC_FAKE_PROJECT_NUMBER-123456789012}" ;;
          *) echo "found" ;;
        esac
        exit 0
      fi
    done
    echo "ERROR: (gcloud.fake.describe) NOT_FOUND: Resource not found" >&2; exit 1 ;;
esac
exit 0
"""
TAG = "0123abc" * 5 + "01234"   # a full 40-hex commit SHA (Codex P1 on cbce156)
REG = "us-central1-docker.pkg.dev/sfdc24/cloud-run-source-deploy/"
IMAGES = ("images describe %sccc-broker:%s" % (REG, TAG), "images describe %sclaude-code-cloud:%s" % (REG, TAG))
# Each bound secret, and its latest version (Copilot on 21ff80e: Cloud Run checks :latest at deploy time).
SECRETS = tuple("secrets describe %s " % s for s in ("ANTHROPIC_API_KEY_CLOUD", "BUS_URL", "BUS_SECRET"))
VERSIONS = tuple("versions describe latest --secret %s " % s for s in ("ANTHROPIC_API_KEY_CLOUD", "BUS_URL",
                                                                        "BUS_SECRET"))
PROJECT_READ = "projects describe sfdc24 "
EXISTING = ";".join(SECRETS + VERSIONS + ("run jobs describe board-watcher", PROJECT_READ) + IMAGES)
BROKER_URL = "https://ccc-broker-123456789012.us-central1.run.app"
NEW = ("iam service-accounts describe claude-code-cloud@", "iam service-accounts describe ccc-broker@",
       "firestore databases describe --database ccc-receipts", "run services describe ccc-broker",
       "run jobs describe claude-code-cloud")
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
                   job_tag=TAG, state=None, project_number=None, flaky="", waits="0 0 0"):
        log = self.tmp / ("calls-%d.log" % len(list(self.tmp.glob("calls-*.log"))))
        env = dict(os.environ, PATH="%s:%s" % (self.bindir, os.environ.get("PATH", "")), CCC_FAKE_LOG=str(log),
                   CCC_FAKE_PRESENT=present, CCC_FAKE_DENIED=denied, CCC_FAKE_FLAKY=flaky, CCC_IAM_WAITS=waits)
        for k in ("CCC_OWNER_GO", "CCC_CURSOR_GO_SHA", "CCC_FAKE_WATCHER", "CCC_PROJECT", "CCC_REGION",
                  "CCC_BROKER_TAG", "CCC_JOB_TAG", "CCC_FAKE_STATE", "CCC_FAKE_PROJECT_NUMBER"):
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
        if state is not None:
            env["CCC_FAKE_STATE"] = state
        if project_number is not None:
            env["CCC_FAKE_PROJECT_NUMBER"] = project_number
        out = subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, env=env)
        calls = log.read_text().splitlines() if log.exists() else []
        return out, calls

    def test_the_default_is_a_dry_run_whose_cloud_calls_are_reads(self):
        out, calls = self.run_script()
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("DRY RUN: nothing is changed", out.stdout)
        self.assertEqual([], mutating(calls), calls)
        # 5 new resources, 3 secrets and their latest versions, the project number, the board-watcher identity,
        # 2 images
        self.assertEqual(15, len(calls), calls)

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
                       # Codex P1 on 80d4820: the envelope is a per-execution override
                       "--member serviceAccount:board-watcher@sfdc24.iam.gserviceaccount.com --role "
                       "roles/run.jobsExecutorWithOverrides",
                       # Copilot on 80d4820: the job is told the broker's URL and audience
                       "CCC_BROKER_URL=%s,CCC_BROKER_AUDIENCE=%s" % (BROKER_URL, BROKER_URL),
                       "--image %sccc-broker:%s " % (REG, TAG), "--image %sclaude-code-cloud:%s " % (REG, TAG)):
            self.assertIn(needed, plan)
        for forbidden in ("allUsers", "allAuthenticatedUsers", "roles/owner", "roles/editor", "startsWith", "UNSET",
                          "secrets create", "<broker URL",
                          "board-watcher@sfdc24.iam.gserviceaccount.com --role roles/run.invoker",
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

    def test_a_runtime_step_waits_for_new_iam_grants_and_is_retried_alone(self):
        # Copilot BLOCKER on b6fa11e: the grants are eventually consistent, and a refused first deploy must not strand
        # the apply. The deploy fails twice and the job create once, then both succeed; nothing else is repeated.
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head,
                                     flaky="run deploy ccc-broker:2;run jobs create claude-code-cloud:1")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual(3, sum(1 for c in calls if c.startswith("run deploy ccc-broker")))
        self.assertEqual(2, sum(1 for c in calls if c.startswith("run jobs create claude-code-cloud")))
        self.assertIn("new IAM grants can take minutes", out.stderr)
        once = [c for c in mutating(calls) if not c.startswith(("run deploy ccc-broker", "run jobs create claude-code-cloud"))]
        self.assertEqual(len(once), len(set(once)))           # every other change ran exactly once

    def test_a_runtime_step_that_never_succeeds_stops_the_apply_with_the_one_command_to_rerun(self):
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head, flaky="run deploy ccc-broker:99",
                                     waits="0 0")
        self.assertNotEqual(0, out.returncode)
        self.assertEqual(3, sum(1 for c in calls if c.startswith("run deploy ccc-broker")))   # 1 + one per wait
        self.assertIn("FAILED: deploy ccc-broker", out.stderr)
        self.assertIn("run this one command by hand: gcloud run deploy ccc-broker", out.stderr)
        self.assertFalse([c for c in calls if c.startswith("run jobs create")])          # nothing after it ran

    def test_bad_iam_waits_are_refused_before_any_cloud_call(self):
        for waits in ("", "x", "30,60", "-5"):
            out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head, waits=waits)
            self.assertNotEqual(0, out.returncode, waits)
            self.assertEqual([], mutating(calls), waits)

    def test_apply_refuses_on_any_collision_missing_input_or_unclear_read(self):
        cases = {}
        for new in NEW:
            cases["%s exists" % new] = dict(present=EXISTING + ";" + new)
        # Copilot on 21ff80e: a bound secret with no enabled version failed the deploy after the first creates.
        for gone in SECRETS + VERSIONS + ("run jobs describe board-watcher",):
            cases["%s missing" % gone] = dict(present=EXISTING.replace(gone, "x-absent-x"))
        cases["latest versions disabled"] = dict(state="DISABLED")
        cases["latest versions destroyed"] = dict(state="DESTROYED")
        cases["permission denied on a new resource"] = dict(denied="run services describe ccc-broker")
        cases["permission denied on board-watcher"] = dict(denied="run jobs describe board-watcher")
        cases["board-watcher identity not an SA"] = dict(watcher="<read live>")
        # Codex P1 on 21ff80e: an unset tag failed the deploy only after the first creates.
        cases["broker tag unset"] = dict(broker_tag=None)
        cases["job tag unset"] = dict(job_tag=None)
        cases["broker tag not a tag"] = dict(broker_tag="x y")
        cases["job tag not a tag"] = dict(job_tag="-rf")
        # Codex P1 on cbce156 and Cursor on 80d4820: only a full lowercase commit SHA passes.
        for bad in ("latest", "v1", "0123abc", TAG.upper(), TAG + "0", TAG[:-1]):
            cases["broker tag %s" % bad] = dict(broker_tag=bad)
            cases["job tag %s" % bad] = dict(job_tag=bad)
        # Copilot on 80d4820: the broker URL comes from the project number.
        cases["project number unread"] = dict(present=EXISTING.replace(PROJECT_READ, "x-absent-x"))
        cases["project number not a number"] = dict(project_number="")
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

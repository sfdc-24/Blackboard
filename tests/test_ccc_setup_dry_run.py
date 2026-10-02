"""cloud/claude-code-cloud/setup.sh (Blackboard #306, Stage C1): the default changes nothing, --apply refuses without
the owner's GO and Cursor's exact-head GO, every read runs before the first change (create-or-refuse), and the plan
holds exactly the least-privilege shape the spec names. Runs offline. A fake `gcloud` on PATH records every call, so
a dry run or a refusal that calls any mutating gcloud command fails the test."""
import os
import shlex
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
# CCC_FAKE_FLAKY="<pattern>:<n>;...": the first n calls matching <pattern> fail with CCC_FAKE_FLAKY_ERR (by default an
# IAM grant still propagating). Describes are never flaky.
if [[ "$*" != *" describe "* ]]; then
  IFS=';' read -ra flaky <<< "${CCC_FAKE_FLAKY:-}"
  for f in "${flaky[@]}"; do
    pat="${f%:*}"; n="${f##*:}"
    if [ -n "$pat" ] && [[ "$*" == *"$pat"* ]] && [ "$(grep -cF -- "$pat" "$CCC_FAKE_LOG")" -le "$n" ]; then
      echo "ERROR: (gcloud.fake) ${CCC_FAKE_FLAKY_ERR:-PERMISSION_DENIED: Permission 'secretmanager.versions.access' denied}" >&2
      exit 1
    fi
  done
fi
case "$*" in
  *" describe "*)
    IFS=';' read -ra denied <<< "${CCC_FAKE_DENIED:-}"
    for d in "${denied[@]}"; do
      if [ -n "$d" ] && [[ "$*" == *"$d"* ]]; then
        echo "ERROR: (gcloud.fake) PERMISSION_DENIED: caller lacks permission" >&2; exit 1
      fi
    done
    # CCC_FAKE_PROBE_FAILS="<describe pattern>@<create pattern>": once the create was attempted, that describe fails
    # with an error that is not a not-found (a network or permission failure of the probe itself).
    IFS=';' read -ra pfails <<< "${CCC_FAKE_PROBE_FAILS:-}"
    for e in "${pfails[@]}"; do
      dpat="${e%@*}"; cpat="${e#*@}"
      if [ -n "$dpat" ] && [[ "$*" == *"$dpat"* ]] && grep -qF -- "$cpat" "$CCC_FAKE_LOG"; then
        echo "ERROR: (gcloud.fake) UNAVAILABLE: the connection was reset" >&2; exit 1
      fi
    done
    IFS=';' read -ra leaves <<< "${CCC_FAKE_LEAVES:-}"
    for e in "${leaves[@]}"; do
      dpat="${e%@*}"; cpat="${e#*@}"
      if [ -n "$dpat" ] && [[ "$*" == *"$dpat"* ]] && grep -qF -- "$cpat" "$CCC_FAKE_LOG"; then echo "found"; exit 0; fi
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


def decoded(stdout):
    """The plan with each printed command un-quoted (the script prints them shell-quoted), joined like the fake's log."""
    return "\n".join(" ".join(shlex.split(l.strip())) if l.strip().startswith("gcloud ") else l
                     for l in stdout.splitlines())


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
                   job_tag=TAG, state=None, project_number=None, flaky="", waits="0 0 0", flaky_err=None,
                   leaves="", probe_fails=""):
        log = self.tmp / ("calls-%d.log" % len(list(self.tmp.glob("calls-*.log"))))
        env = dict(os.environ, PATH="%s:%s" % (self.bindir, os.environ.get("PATH", "")), CCC_FAKE_LOG=str(log),
                   CCC_FAKE_PRESENT=present, CCC_FAKE_DENIED=denied, CCC_FAKE_FLAKY=flaky, CCC_IAM_WAITS=waits)
        env.pop("CCC_FAKE_FLAKY_ERR", None)
        env["CCC_FAKE_LEAVES"] = leaves
        env["CCC_FAKE_PROBE_FAILS"] = probe_fails
        if flaky_err is not None:
            env["CCC_FAKE_FLAKY_ERR"] = flaky_err
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
        plan = decoded(out.stdout)
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
        printed = [line.strip()[len("gcloud "):] for line in decoded(dry.stdout).splitlines()
                   if line.strip().startswith("gcloud ")]
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
        self.assertIn("was refused while IAM propagates. Retrying", out.stderr)
        once = [c for c in mutating(calls) if not c.startswith(("run deploy ccc-broker", "run jobs create claude-code-cloud"))]
        self.assertEqual(len(once), len(set(once)))           # every other change ran exactly once

    def test_a_runtime_step_that_never_succeeds_stops_the_apply_with_the_one_command_to_rerun(self):
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head, flaky="run deploy ccc-broker:99",
                                     waits="0 0")
        self.assertNotEqual(0, out.returncode)
        self.assertEqual(3, sum(1 for c in calls if c.startswith("run deploy ccc-broker")))   # 1 + one per wait
        self.assertRegex(out.stderr, r"FAILED at step \d+: deploy ccc-broker \(no public invoker\) \(still refused")
        self.assertIn("Check it first: gcloud run services describe ccc-broker", out.stderr)
        self.assertRegex(out.stderr, r"bash cloud/claude-code-cloud/setup.sh --print-from \d+")
        self.assertFalse([c for c in calls if c.startswith("run jobs create")])          # nothing after it ran

    def test_a_failure_that_is_not_iam_propagation_is_never_retried(self):
        # Copilot on 5a98651: a lost answer may follow a real create, so repeating it could hide a partial deployment.
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head, flaky="run deploy ccc-broker:9",
                                     flaky_err="INTERNAL: the connection was reset")
        self.assertNotEqual(0, out.returncode)
        self.assertEqual(1, sum(1 for c in calls if c.startswith("run deploy ccc-broker")))   # exactly one attempt
        self.assertRegex(out.stderr, r"FAILED at step \d+: deploy ccc-broker \(no public invoker\)\. Steps 1 to \d+ "
                                     r"are applied; nothing after this one ran\.")
        self.assertIn("The server may or may not have applied step", out.stderr)
        self.assertFalse([c for c in calls if c.startswith(("run services add-iam-policy-binding", "run jobs create"))])

    def test_a_binding_on_a_just_created_account_is_retried_while_iam_propagates(self):
        # Codex P1 on 5a98651: the new service account may not be visible to IAM yet.
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head,
                                     flaky="secrets add-iam-policy-binding BUS_URL:2",
                                     flaky_err="INVALID_ARGUMENT: Service account ccc-broker@sfdc24.iam.gserviceaccount.com does not exist.")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual(3, sum(1 for c in calls if c.startswith("secrets add-iam-policy-binding BUS_URL")))

    def test_print_from_prints_the_rest_of_the_plan_in_order_and_changes_nothing(self):
        dry, _ = self.run_script()
        everything = [l.strip() for l in dry.stdout.splitlines() if l.strip().startswith("gcloud ")]
        numbered = [l for l in dry.stdout.splitlines() if l.startswith("DRY RUN: [")]
        self.assertGreaterEqual(len(numbered), 10)
        k = 7
        tail, calls = self.run_script("--print-from", str(k))
        self.assertEqual(0, tail.returncode, tail.stderr)
        printed = [l.strip() for l in tail.stdout.splitlines() if l.strip().startswith("gcloud ")]
        self.assertEqual(everything[k - 1:], printed)                   # step k and every step after it, in order
        self.assertEqual([], mutating(calls))
        for bad in ("0", "x", ""):
            out, calls = self.run_script("--print-from", bad)
            self.assertEqual(2, out.returncode, bad)
            self.assertEqual([], mutating(calls), bad)

    def test_a_create_that_left_its_resource_is_never_sent_again(self):
        # Cursor NO-GO on 27e1f5a: a permission error does not prove nothing was created. The deploy fails with an IAM
        # error but leaves the service; the describe finds it, so the deploy is not repeated and recovery starts after it.
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head, flaky="run deploy ccc-broker:9",
                                     leaves="run services describe ccc-broker@run deploy ccc-broker")
        self.assertNotEqual(0, out.returncode)
        self.assertEqual(1, sum(1 for c in calls if c.startswith("run deploy ccc-broker")))
        # Cursor on de83893: a service a failed deploy left may have no ready revision, so recovery re-runs the deploy
        # itself (an update), never the invoker binding first.
        self.assertIn("the service now exists; a failed deploy can leave it without a ready revision", out.stderr)
        self.assertIn("`run deploy` updates the existing service", out.stderr)
        deploy_step = int(out.stderr.split("FAILED at step ")[1].split(":")[0])
        self.assertIn("--print-from %d" % deploy_step, out.stderr)
        self.assertFalse([c for c in calls if c.startswith(("run services add-iam-policy-binding", "run jobs create"))])

    def test_a_job_create_that_left_its_job_continues_at_the_next_step(self):
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head,
                                     flaky="run jobs create claude-code-cloud:9",
                                     leaves="run jobs describe claude-code-cloud@run jobs create claude-code-cloud")
        self.assertNotEqual(0, out.returncode)
        self.assertEqual(1, sum(1 for c in calls if c.startswith("run jobs create claude-code-cloud")))
        self.assertIn("what it creates now exists, so it is not repeated", out.stderr)
        job_step = int(out.stderr.split("FAILED at step ")[1].split(":")[0])
        self.assertIn("--print-from %d" % (job_step + 1), out.stderr)
        self.assertFalse([c for c in calls if c.startswith("run jobs add-iam-policy-binding")])

    def test_an_inconclusive_describe_stops_instead_of_retrying_the_create(self):
        # Codex P1 on 708b0a2: only a confirmed not-found may lead to a second create.
        out, calls = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head, flaky="run deploy ccc-broker:9",
                                     probe_fails="run services describe ccc-broker@run deploy ccc-broker")
        self.assertNotEqual(0, out.returncode)
        self.assertEqual(1, sum(1 for c in calls if c.startswith("run deploy ccc-broker")))
        self.assertIn("could not tell whether what it creates exists, so it is not retried", out.stderr)
        deploy_step = int(out.stderr.split("FAILED at step ")[1].split(":")[0])
        self.assertIn("--print-from %d" % deploy_step, out.stderr)            # this step, after a person looks

    def test_the_recovery_line_carries_the_apply_inputs_and_print_from_needs_them(self):
        # Codex P1 on 27e1f5a: a bare --print-from printed :UNSET images and the default project.
        out, _ = self.run_script("--apply", go="OWNER-GO-TEST", sha=self.head, flaky="run deploy ccc-broker:9",
                                 flaky_err="INTERNAL: reset")
        line = [l.strip() for l in out.stderr.splitlines() if "--print-from" in l and "bash" in l][0]
        for part in ("CCC_PROJECT=sfdc24", "CCC_REGION=us-central1", "CCC_BROKER_TAG=" + TAG, "CCC_JOB_TAG=" + TAG):
            self.assertIn(part, line)
        bare, calls = self.run_script("--print-from", "3", broker_tag=None, job_tag=None)
        self.assertEqual(2, bare.returncode)
        self.assertIn("--print-from needs the same CCC_BROKER_TAG and CCC_JOB_TAG", bare.stderr)
        self.assertEqual([], mutating(calls))

    def test_printed_commands_are_shell_quoted_and_round_trip(self):
        # Codex P1 and Copilot on 27e1f5a: $* lost the argument boundaries, and "(" broke a pasted command.
        dry, _ = self.run_script()
        create = [l.strip() for l in dry.stdout.splitlines() if l.strip().startswith("gcloud iam service-accounts create claude-code-cloud")][0]
        argv = shlex.split(create)
        self.assertEqual("claude-code-cloud (Console agent job, Blackboard #306)", argv[argv.index("--display-name") + 1])
        cond = [l.strip() for l in dry.stdout.splitlines() if l.strip().startswith("gcloud projects add-iam-policy-binding")][0]
        argv = shlex.split(cond)
        self.assertEqual("title=ccc-receipts-only," + CONDITION, argv[argv.index("--condition") + 1])

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

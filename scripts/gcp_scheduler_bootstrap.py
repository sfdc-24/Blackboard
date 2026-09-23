#!/usr/bin/env python3
r"""Provision the unattended trigger: Cloud Scheduler -> Cloud Run job.

WHY THIS EXISTS RATHER THAN A RUNBOOK
    This replaces Windows Task Scheduler for the cloud lane, which is dependency
    1 of docs/OPENAI-CLOUD-MIGRATION.md - "SFDC24 Blackboard Waker is a Windows
    Task Scheduler job". That dependency was recorded as needing Mr Salam's
    elevated shell. It does not: a trigger that lives in GCP needs no laptop at
    all, and he approved that route on 2026-09-23.

    Two of the four steps below are things that are easy to get wrong and that
    FAIL SILENTLY, so they belong in code rather than in someone's memory.

THE TWO THAT FAIL SILENTLY, both cost time on 2026-09-23

    1. THE CLOUD SCHEDULER SERVICE AGENT MUST BE ABLE TO ACT AS THE INVOKER.
       A job created with --oauth-service-account-email will sit at
       `status: code: -1` with NO lastAttemptTime and no logs at all - it never
       even attempts - until
       service-<PROJECT_NUMBER>@gcp-sa-cloudscheduler.iam.gserviceaccount.com
       holds roles/iam.serviceAccountTokenCreator ON THE INVOKER ACCOUNT. The
       invoker SA had no IAM policy bindings whatsoever, and nothing anywhere
       said so. "Never attempted" looks exactly like "attempted and the target
       refused" unless you check lastAttemptTime.

    2. USE THE v2 RUN ENDPOINT, NOT v1 namespaces.
       roles/run.invoker grants run.jobs.run, which is what v2 :run needs. The v1
       /apis/run.googleapis.com/v1/namespaces/.../jobs/NAME:run form also reads
       the job, which run.invoker does not grant - so v1 forces you to widen the
       role to run.developer for no benefit.

LEAST PRIVILEGE, DELIBERATELY
    A dedicated service account that can do exactly one thing: run one job. It
    holds no secret access and no write role, and run.invoker is bound ON THE JOB
    rather than on the project. The existing sfdc24-stt-relay runs as the default
    compute account, which is broadly privileged; this is the shape the fleet
    should move toward rather than copy.

    python scripts/gcp_scheduler_bootstrap.py --dry-run
    python scripts/gcp_scheduler_bootstrap.py --schedule "15 * * * *"
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT = "sfdc24"
PROJECT_NUMBER = "96522051727"
REGION = "us-central1"
JOB = "board-probe"
SCHED_JOB = "board-probe-hourly"
INVOKER = "board-probe-invoker"
INVOKER_EMAIL = "%s@%s.iam.gserviceaccount.com" % (INVOKER, PROJECT)
SCHEDULER_AGENT = ("service-%s@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
                   % PROJECT_NUMBER)
# v2, not v1 namespaces. See the note above - this choice is what lets the
# invoker keep roles/run.invoker instead of needing roles/run.developer.
RUN_URI = ("https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s:run"
           % (PROJECT, REGION, JOB))

CANDIDATES = [
    os.environ.get("GCLOUD_BIN") or "",
    "gcloud",
    str(Path(os.environ.get("LOCALAPPDATA", "")) /
        "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd"),
]


def gcloud_bin() -> str:
    for c in CANDIDATES:
        if not c:
            continue
        try:
            subprocess.run([c, "--version"], capture_output=True, timeout=120,
                           check=True)
            return c
        except Exception:  # noqa: BLE001
            continue
    raise SystemExit("gcloud not found. Set GCLOUD_BIN to its full path.")


def step(gb: str, label: str, args: list, dry: bool, ok_if_exists=()) -> bool:
    print("  %-52s" % label, end="")
    if dry:
        print("would run")
        return True
    p = subprocess.run([gb] + args, capture_output=True, text=True, timeout=600)
    err = (p.stderr or "").strip()
    if p.returncode == 0:
        print("ok")
        return True
    if any(s in err for s in ok_if_exists):
        print("already present")
        return True
    print("FAILED")
    print("      %s" % err.replace("\n", " ")[:220])
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--schedule", default="15 * * * *")
    ap.add_argument("--timezone", default="America/Toronto")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    gb = gcloud_bin()
    dry = args.dry_run

    print("project=%s region=%s job=%s" % (PROJECT, REGION, JOB))
    print("schedule=%r %s" % (args.schedule, args.timezone))
    print()

    ok = True
    ok &= step(gb, "enable Cloud Scheduler API",
               ["services", "enable", "cloudscheduler.googleapis.com",
                "--project", PROJECT], dry)

    ok &= step(gb, "create the dedicated invoker service account",
               ["iam", "service-accounts", "create", INVOKER,
                "--project", PROJECT,
                "--display-name", "Cloud Scheduler -> %s job" % JOB,
                "--description",
                "Invokes the read-only board probe. No secret access, no write role."],
               dry, ok_if_exists=("already exists",))

    ok &= step(gb, "grant run.invoker ON THE JOB (not project-wide)",
               ["run", "jobs", "add-iam-policy-binding", JOB,
                "--project", PROJECT, "--region", REGION,
                "--member", "serviceAccount:" + INVOKER_EMAIL,
                "--role", "roles/run.invoker"], dry)

    # THE STEP WHOSE ABSENCE IS INVISIBLE. Without it the scheduler never
    # attempts: no lastAttemptTime, no logs, status code -1, and an executions
    # list that simply does not grow.
    ok &= step(gb, "let the scheduler agent mint tokens as the invoker",
               ["iam", "service-accounts", "add-iam-policy-binding", INVOKER_EMAIL,
                "--project", PROJECT,
                "--member", "serviceAccount:" + SCHEDULER_AGENT,
                "--role", "roles/iam.serviceAccountTokenCreator"], dry)

    ok &= step(gb, "create the scheduler job",
               ["scheduler", "jobs", "create", "http", SCHED_JOB,
                "--project", PROJECT, "--location", REGION,
                "--schedule", args.schedule,
                "--time-zone", args.timezone,
                "--uri", RUN_URI,
                "--http-method", "POST",
                "--oauth-service-account-email", INVOKER_EMAIL,
                "--oauth-token-scope",
                "https://www.googleapis.com/auth/cloud-platform",
                "--attempt-deadline", "30s",
                "--description",
                "Read-only board parity probe. Replaces the laptop Task "
                "Scheduler dependency for this lane."],
               dry, ok_if_exists=("ALREADY_EXISTS", "already exists"))

    print()
    if dry:
        print("dry run: nothing was created")
        return 0
    if not ok:
        print("one or more steps failed - the trigger is NOT provisioned")
        return 1

    print("Provisioned. VERIFY IT, do not assume:")
    print("  gcloud scheduler jobs run %s --location %s --project %s"
          % (SCHED_JOB, REGION, PROJECT))
    print("  gcloud scheduler jobs describe %s --location %s --project %s \\"
          % (SCHED_JOB, REGION, PROJECT))
    print("      --format='yaml(lastAttemptTime,status)'")
    print()
    print("  A MISSING lastAttemptTime MEANS IT NEVER ATTEMPTED, which is a")
    print("  different failure from one the target refused, and it is the shape")
    print("  the token-creator binding above produces when it is absent.")
    print("  Then confirm the execution count actually grew:")
    print("  gcloud run jobs executions list --job %s --region %s --project %s"
          % (JOB, REGION, PROJECT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

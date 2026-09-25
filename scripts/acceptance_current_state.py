#!/usr/bin/env python3
"""Is the current-state architecture actually working? Read-only.

One line per component of the current-state architecture (the 24 Sep
architecture document, page 1), each PASS, FAIL or UNKNOWN with the evidence
it was judged on. It reads public pages, `gcloud ... describe/list` output and
the board; it never deploys, starts or stops anything, never writes to the
board and never sends a message.

    python scripts/acceptance_current_state.py            # table
    python scripts/acceptance_current_state.py --json     # one JSON object

Exit 0 when nothing FAILed, 1 when anything did. UNKNOWN never passes for
PASS: it means the check could not see, and says why. With --strict, UNKNOWN
also exits 1 - an alarm must not stay green because it could not look.

What each check calls PASS is written next to it. Components that are not
deployed anywhere yet (the Zoom agent) are UNKNOWN with the reason, never
PASS by absence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJECT = "sfdc24"
REGION = "us-central1"
WWW = "https://www.sfdc24.com"
MAIN_INDEX = "https://raw.githubusercontent.com/sfdc-24/sfdc24-site/main/index.html"
CONTROLLER = "https://sfdc24-studio-controller-yzet4vuplq-uc.a.run.app"
SITE_PATHS = ("/", "/assets/voice-conversation.js", "/assets/prototype-canvas.js", "/data/next-release.json")
VMS = {"blackboard-bus": ("RUNNING",), "zoom-presenter-tmp": ("TERMINATED", "RUNNING")}
JOBS = ("board-watcher", "board-probe", "gemini-waker", "claude-api-waker", "wa-outbox", "waker-shadow",
        "studio-voice-sweep")
# The fleet schedulers (docs/CLOUD-FLEET-RUNBOOK.md): each must exist, not just
# whatever the list happens to return.
SCHEDULERS = ("board-watcher-2min", "board-probe-hourly", "waker-shadow-hourly",
              "studio-voice-sweep-every-minute")
# A scheduler that has not attempted within this many of its own intervals is stale.
SCHEDULE_SLACK = 3
CONTROLLER_FEATURES = ("voice", "talk", "analyst")
CONTROLLER_VOICES = ("host", "architect")

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"


def http_get(url: str, timeout: float = 20.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "sfdc24-acceptance/1", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def gcloud(args: list[str]) -> object:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        raise RuntimeError("gcloud is not on PATH")
    out = subprocess.run([exe] + args + ["--project", PROJECT, "--format=json"], capture_output=True,
                         text=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "gcloud failed").strip().splitlines()[-1][:160])
    return json.loads(out.stdout or "null")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


# ---------------------------------------------------------------- checks --
# Each check takes the injected readers and returns (status, evidence).

def check_pages_serves_main(get=http_get, **_):
    """PASS: www's index.html is byte-identical (line endings aside) to main's."""
    s1, live = get(WWW + "/?acceptance=%d" % int(time.time()))
    s2, main = get(MAIN_INDEX)
    if s1 != 200 or s2 != 200:
        return UNKNOWN, "could not read both (www %s, main %s)" % (s1, s2)
    a, b = _sha(live), _sha(main)
    if a == b:
        return PASS, "www index.html == main (%s)" % a[:12]
    return FAIL, "www %s != main %s (Pages behind main, or a failed build)" % (a[:12], b[:12])


def check_site_paths(get=http_get, **_):
    """PASS: the homepage and the assets it now depends on all answer 200."""
    bad = []
    for path in SITE_PATHS:
        status, _ = get(WWW + path)
        if status != 200:
            bad.append("%s=%s" % (path, status))
    if bad:
        return FAIL, "not 200: " + ", ".join(bad)
    return PASS, "%d paths 200" % len(SITE_PATHS)


def check_controller(get=http_get, **_):
    """PASS: the studio controller answers /health ok with voice, talk and analyst on and both voices."""
    status, body = get(CONTROLLER + "/health")
    if status != 200:
        return FAIL, "/health answered %s" % status
    try:
        health = json.loads(body.decode("utf-8"))
    except ValueError:
        return FAIL, "/health is not JSON"
    features = health.get("features") or {}
    off = [name for name in CONTROLLER_FEATURES if not features.get(name)]
    voices = features.get("voices") or []
    off += ["voice:" + v for v in CONTROLLER_VOICES if v not in voices]
    if not health.get("ok") or off:
        return FAIL, "ok=%s, off: %s" % (health.get("ok"), ", ".join(off) or "-")
    return PASS, "ok; %s on; voices %s" % ("/".join(CONTROLLER_FEATURES), features.get("voices") or [])


def check_vms(run=gcloud, **_):
    """PASS: blackboard-bus RUNNING and non-Spot; the presenter in an expected state and non-Spot."""
    try:
        listed = run(["compute", "instances", "list"])
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return UNKNOWN, str(exc)
    seen = {vm.get("name"): vm for vm in listed or []}
    problems, notes = [], []
    for name, allowed in VMS.items():
        vm = seen.get(name)
        if vm is None:
            problems.append("%s missing" % name)
            continue
        state = vm.get("status")
        model = ((vm.get("scheduling") or {}).get("provisioningModel") or "STANDARD")
        notes.append("%s %s/%s" % (name, state, model))
        if state not in allowed:
            problems.append("%s is %s" % (name, state))
        if model == "SPOT":
            problems.append("%s is Spot" % name)
    return (FAIL, "; ".join(problems)) if problems else (PASS, "; ".join(notes))


def check_jobs(run=gcloud, **_):
    """PASS: the latest execution of every fleet job succeeded."""
    failed, ok, unknown = [], [], []
    for job in JOBS:
        try:
            runs = run(["run", "jobs", "executions", "list", "--job", job, "--region", REGION, "--limit", "5"])
        except Exception as exc:  # noqa: BLE001
            if "not found" in str(exc).lower() or "not_found" in str(exc).lower():
                failed.append("%s (missing)" % job)      # a job that does not exist is not "could not see"
            else:
                unknown.append("%s (%s)" % (job, str(exc)[:60]))
            continue
        # The newest FINISHED execution decides; a job that runs every minute is
        # usually mid-run when looked at.
        verdict = None
        for execution in runs or []:
            conditions = {c.get("type"): c.get("status")
                          for c in (execution.get("status") or {}).get("conditions") or []}
            if conditions.get("Completed") in ("True", "False"):
                verdict = conditions["Completed"]
                break
        if verdict == "True":
            ok.append(job)
        elif verdict == "False":
            failed.append(job)
        else:
            unknown.append("%s (no finished execution)" % job)
    if failed:
        return FAIL, "latest failed: " + ", ".join(failed)
    if unknown:
        return UNKNOWN, "%d ok; unknown: %s" % (len(ok), ", ".join(unknown))
    return PASS, "%d jobs, latest execution succeeded" % len(ok)


def _interval_minutes(schedule: str) -> int:
    """Minutes between runs for the simple cron shapes the fleet uses; 60 when unsure."""
    minute, hour = (schedule.split() + ["*", "*"])[:2]
    if minute.startswith("*/"):
        return max(1, int(minute[2:]))
    if minute == "*":
        return 1
    if hour == "*":
        return 60
    return 24 * 60


def check_schedulers(run=gcloud, now=None, **_):
    """PASS: every scheduler is ENABLED and has attempted within SCHEDULE_SLACK of its interval."""
    now = now or datetime.now(timezone.utc)
    try:
        jobs = run(["scheduler", "jobs", "list", "--location", REGION])
    except Exception as exc:  # noqa: BLE001
        return UNKNOWN, str(exc)
    problems, count = [], 0
    present = {(job.get("name") or "").rsplit("/", 1)[-1] for job in jobs or []}
    problems += ["%s missing" % name for name in SCHEDULERS if name not in present]
    for job in jobs or []:
        name = (job.get("name") or "").rsplit("/", 1)[-1]
        count += 1
        if job.get("state") != "ENABLED":
            problems.append("%s %s" % (name, job.get("state")))
            continue
        last = job.get("lastAttemptTime")
        if not last:
            problems.append("%s never attempted" % name)
            continue
        age = now - datetime.fromisoformat(last.replace("Z", "+00:00"))
        if age > timedelta(minutes=SCHEDULE_SLACK * _interval_minutes(job.get("schedule") or "")):
            problems.append("%s last attempted %d min ago" % (name, age.total_seconds() // 60))
    if not count:
        return FAIL, "no schedulers found"
    return (FAIL, "; ".join(problems)) if problems else (PASS, "%d schedulers enabled and on time" % count)


def check_governor(**_):
    """PASS: the live Governor Apps Script serves exactly main (scripts/governor_live_check.py)."""
    script = REPO / "scripts" / "governor_live_check.py"
    if not script.exists():
        return UNKNOWN, "governor_live_check.py missing"
    try:
        out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=180)
    except Exception as exc:  # noqa: BLE001
        return UNKNOWN, str(exc)[:120]
    tail = (out.stdout.strip().splitlines() or [""])[-1][:140]
    return {0: PASS, 1: FAIL}.get(out.returncode, UNKNOWN), tail or "exit %d" % out.returncode


def check_board_read(**_):
    """PASS: the Apps Script bus answers a read of the last hour with rows."""
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import agent_waker  # noqa: WPS433 - the fleet's own reader
        env = agent_waker.load_env()
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = agent_waker.read_since(env, since)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - load_env exits when no credentials are set
        return UNKNOWN, "read failed: %s" % (str(exc)[:100] or "no bus credentials (set BLACKBOARD_ENV)")
    rows = result.get("rows") or []
    if result.get("ok") is False:
        return FAIL, "bus answered not ok"
    return (PASS, "%d rows in the last hour" % len(rows)) if rows else (UNKNOWN, "no rows in the last hour")


def check_zoom_agent(**_):
    """Not deployed anywhere yet: no Zoom credentials in Secret Manager, no host. Owner steps pending."""
    return UNKNOWN, "not deployed: needs RTMS entitlement, OAuth consent and a host (owner)"


CHECKS = (
    ("pages_serves_main", check_pages_serves_main),
    ("site_paths", check_site_paths),
    ("studio_controller", check_controller),
    ("vms", check_vms),
    ("cloud_run_jobs", check_jobs),
    ("schedulers", check_schedulers),
    ("governor_live", check_governor),
    ("board_read", check_board_read),
    ("zoom_agent", check_zoom_agent),
)


def run_all(checks=CHECKS, **readers) -> list[dict]:
    results = []
    for name, fn in checks:
        try:
            status, evidence = fn(**readers)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - one broken check never hides the others
            status, evidence = UNKNOWN, "check crashed: %s" % str(exc)[:120]
        results.append({"component": name, "status": status, "evidence": evidence})
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--only", help="comma-separated component names")
    parser.add_argument("--strict", action="store_true", help="UNKNOWN also exits 1 (for alarms)")
    args = parser.parse_args(argv)
    checks = CHECKS
    if args.only:
        wanted = [name.strip() for name in args.only.split(",") if name.strip()]
        known = {name for name, _ in CHECKS}
        unknown_names = [name for name in wanted if name not in known]
        if not wanted or unknown_names:
            # A typo must never become a clean run of nothing.
            print("unknown component(s): %s; known: %s" % (", ".join(unknown_names) or "(none given)",
                                                           ", ".join(sorted(known))), file=sys.stderr)
            return 2
        checks = tuple(c for c in CHECKS if c[0] in wanted)
    results = run_all(checks)
    if args.json:
        print(json.dumps({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "results": results}))
    else:
        for r in results:
            print("%-8s %-18s %s" % (r["status"], r["component"], r["evidence"]))
    bad = (FAIL, UNKNOWN) if args.strict else (FAIL,)
    return 1 if any(r["status"] in bad for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

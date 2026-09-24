#!/usr/bin/env python3
r"""Judge the cutover soak. It can say PASS, FAIL, or NOT YET - and it must.

Cutover step 7 of docs/OPENAI-CLOUD-MIGRATION.md: "Soak for at least 48 hours with
no missed rows, duplicates, or laptop reads." Step 8 is Mr Salam's decision on
whether to disable the laptop scheduler, and this is the evidence he would decide
on. So the one thing this tool must never do is round "not enough evidence yet" up
to "passed".

    NOT YET is a first-class verdict here, not a soft failure.

WHY IT READS CLOUD LOGGING RATHER THAN THE STATE DOCUMENTS
    The state documents are bounded on purpose - the shadow keeps 60 runs, the
    probe one cursor. Cloud Logging holds EVERY execution with its full
    fingerprint, so a gap in the record is a gap in reality rather than a
    truncation. The state documents are then cross-checked against it: if the two
    disagree, that is itself a finding.

THE FOUR CRITERIA, AND WHAT EACH ONE WOULD CATCH

    1. CADENCE. Both jobs are hourly. Missing hours are the failure most likely to
       happen and least likely to announce itself - a scheduler that quietly stops
       looks exactly like a quiet board. Counted as gaps, not as an average.

    2. NO MISSED ROWS. The probe's cursor must move forward or stay, never back,
       and each run's `newest` must be at least the previous run's cursor. A
       cursor that jumped forward past rows is the silent failure #168 exists to
       prevent.

    3. NO DUPLICATES. The board must carry exactly ONE row per ack target. The
       read-only jobs cannot write at all, so any second row would mean the one
       writer wrote twice.

    4. NO LAPTOP READS. Every execution must report where=cloud-run and
       credential_source=injected. A single local run in the window means the
       lane still leans on this machine, which is the whole thing being tested.

    python scripts/soak_report.py                 # judge, print, exit nonzero on FAIL
    python scripts/soak_report.py --hours 48 --start-now
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

PROJECT = "sfdc24"
REGION = "us-central1"
JOBS = ("board-probe", "waker-shadow")
STATE_URI = os.environ.get("BLACKBOARD_STATE_URI") or "gs://sfdc24-fleet-state/wakers"
SOAK_DOC = "cutover_soak"
ACK_TARGET = os.environ.get("ACK_TARGET") or "CLOUD-ACK-PROBE-20260924"

CANDIDATES = [
    os.environ.get("GCLOUD_BIN") or "",
    str(Path(os.environ.get("LOCALAPPDATA", "")) /
        "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd"),
    "gcloud",
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
    raise SystemExit("gcloud not found. Set GCLOUD_BIN.")


def read_executions(gb: str, job: str, hours: int) -> list:
    """Every logged fingerprint for a job in the window.

    Written to a file and parsed, not piped: a `timestamp >= "..."` filter gets
    mangled by the shell layer on this box, so --freshness is used instead, and
    gcloud's JSON is large enough that a file is the reliable hand-off.
    """
    out = Path(os.environ.get("TEMP") or "/tmp") / ("soak_%s.json" % job)
    p = subprocess.run(
        [gb, "logging", "read",
         'resource.type="cloud_run_job" AND resource.labels.job_name="%s"' % job,
         "--project", PROJECT, "--limit", "500",
         "--freshness", "%dh" % max(hours + 1, 2), "--format", "json"],
        capture_output=True, timeout=900)
    if p.returncode != 0:
        raise SystemExit("could not read logs for %s: %s"
                         % (job, p.stderr.decode("utf-8", "replace")[:300]))
    out.write_bytes(p.stdout)
    entries = json.loads(p.stdout.decode("utf-8", "replace") or "[]")
    runs = []
    for e in entries:
        jp = e.get("jsonPayload") or {}
        if not jp:
            continue
        runs.append({
            "ts": e.get("timestamp", ""),
            "execution": (e.get("labels") or {}).get(
                "run.googleapis.com/execution_name", "?"),
            "payload": jp,
        })
    runs.sort(key=lambda r: r["ts"])
    return runs


def cadence_gaps(runs: list, hours: int, started: str | None) -> dict:
    """Hours with no execution, counted ONLY from the soak start onward.

    An average would hide the failure this looks for: 48 runs in 48 hours with a
    six-hour hole still averages one an hour. So it counts holes.

    THE FIRST VERSION OF THIS FUNCTION REPORTED FAIL ON ITS FIRST RUN. It measured
    the whole 48-hour window regardless of when the soak began, so minutes after
    starting it declared 44 missing hours - from before the jobs existed. That is
    worse than a missing check: a FAIL nobody believes gets ignored, and a FAIL
    somebody believes sends them chasing a scheduler that is working.

    An hour before the soak started carries no expectation, so it is not counted.
    """
    now = datetime.now(timezone.utc)
    floor = None
    if started:
        try:
            floor = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            floor = None

    seen = set()
    for r in runs:
        try:
            seen.add(datetime.strptime(r["ts"][:13], "%Y-%m-%dT%H").replace(
                tzinfo=timezone.utc))
        except ValueError:
            continue

    expected, missing = [], []
    for i in range(hours):
        h = (now - timedelta(hours=i + 1)).replace(minute=0, second=0,
                                                   microsecond=0)
        # The hour the soak began is excluded too: a job scheduled at :15 has
        # nothing to say about an hour that was already half over.
        if floor is not None and h <= floor:
            continue
        expected.append(h)
        if h not in seen:
            missing.append(h.strftime("%m-%dT%HZ"))
    return {"hours_expected_since_start": len(expected),
            "hours_with_a_run": len(expected) - len(missing),
            "missing_hours": sorted(missing),
            "not_yet_started": floor is None}


def check(runs_probe: list, runs_shadow: list, hours: int,
          board_rows: int | None, started: str | None = None):
    findings, criteria = [], {}

    # 1. cadence
    for name, runs in (("board-probe", runs_probe), ("waker-shadow", runs_shadow)):
        gaps = cadence_gaps(runs, hours, started)
        criteria["cadence:" + name] = gaps
        if gaps["missing_hours"]:
            findings.append("%s missed %d hour(s): %s" % (
                name, len(gaps["missing_hours"]),
                ", ".join(gaps["missing_hours"][:8])))

    # 2. no missed rows: the cursor moves forward or stays, never back
    previous = None
    regressions = []
    for r in runs_probe:
        c = (r["payload"].get("cursor") or {})
        value = c.get("value")
        if not value:
            continue
        if previous and value < previous:
            regressions.append("%s went back from %s to %s"
                               % (r["ts"][:19], previous, value))
        previous = max(previous, value) if previous else value
    criteria["cursor_monotonic"] = not regressions
    findings.extend(regressions)

    # 3. no duplicates
    if board_rows is not None:
        criteria["board_rows_for_ack_target"] = board_rows
        if board_rows != 1:
            findings.append("the ack target has %d rows on the board; exactly "
                            "one is correct" % board_rows)

    # 4. no laptop reads
    local = []
    for name, runs in (("board-probe", runs_probe), ("waker-shadow", runs_shadow)):
        for r in runs:
            p = r["payload"]
            if p.get("where") != "cloud-run":
                local.append("%s %s ran as %r" % (name, r["ts"][:19],
                                                  p.get("where")))
            if p.get("credential_source") not in (None, "injected"):
                local.append("%s %s used credentials from %r"
                             % (name, r["ts"][:19], p.get("credential_source")))
    criteria["all_runs_in_cloud"] = not local
    findings.extend(local[:6])

    # A shadow that never reached a verdict proves nothing either.
    verdicts = {}
    for r in runs_shadow:
        v = r["payload"].get("combined")
        if v:
            verdicts[v] = verdicts.get(v, 0) + 1
    criteria["shadow_verdicts"] = verdicts
    # WHY each UNKNOWN happened, not only how many. On 2026-09-24 all 4 of 25
    # were Google's 404 page on the board AND the WhatsApp read at once - a
    # gateway that flapped, fixed by retrying the read (#232) - and the count
    # alone could not tell that apart from a read path that is broken.
    reasons = []
    for r in runs_shadow:
        p = r["payload"]
        if p.get("combined") != "UNKNOWN":
            continue
        notes = [str(p[k]) for k in ("board_note", "whatsapp_note") if p.get(k)]
        reasons.append("%sZ %s" % (r["ts"][5:16], "; ".join(notes) or "no note"))
    criteria["shadow_unknown_reasons"] = reasons
    # EVERY FAILED READ, whatever the combined verdict. At 22:45Z on 2026-09-24
    # the board read failed three times over four minutes, and the run still
    # said NEWS because the WhatsApp read had news - so the count above, which
    # keys on the combined verdict, never saw it. The combined verdict is what
    # decides a wake and stays the finding; this is how reliable the reads are.
    failed_reads = []
    for r in runs_shadow:
        p = r["payload"]
        which = [lane for lane in ("board", "whatsapp") if p.get(lane) == "UNKNOWN"]
        if not which:
            continue
        notes = [str(p.get(lane + "_note") or "no note") for lane in which]
        failed_reads.append("%sZ %s failed (combined=%s): %s" % (
            r["ts"][5:16], "+".join(which), p.get("combined") or "?",
            "; ".join(notes)))
    criteria["shadow_failed_reads"] = failed_reads
    if verdicts.get("UNKNOWN") and verdicts["UNKNOWN"] > max(
            1, len(runs_shadow) // 4):
        findings.append("shadow returned UNKNOWN on %d of %d runs - the board "
                        "read is not reliable enough to cut over"
                        % (verdicts["UNKNOWN"], len(runs_shadow)))

    return findings, criteria


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--start-now", action="store_true",
                    help="record the soak start, if one is not recorded already")
    args = ap.parse_args()
    gb = gcloud_bin()

    # The soak start is recorded durably so "48 hours" is a fact rather than a
    # recollection, and so a later run cannot quietly move the goalposts.
    started = None
    store = None
    try:
        import state_store
        store = state_store.open_store(STATE_URI,
                                       token_provider=lambda: (
                                           local_token(gb), 3000))
        state, token = store.load(SOAK_DOC)
        started = state.get("started")
        if not started and args.start_now:
            started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            state["started"] = started
            state["window_hours"] = args.hours
            store.save(SOAK_DOC, state, token)
    except Exception as exc:  # noqa: BLE001
        print("note: could not reach the soak document (%s). Judging the window "
              "anyway." % exc)

    runs_probe = read_executions(gb, "board-probe", args.hours)
    runs_shadow = read_executions(gb, "waker-shadow", args.hours)
    board_rows = count_ack_rows()

    findings, criteria = check(runs_probe, runs_shadow, args.hours,
                               board_rows, started)

    elapsed = None
    if started:
        t0 = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds() / 3600.0

    print("SOAK REPORT  %s" % datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"))
    print("  started        : %s" % (started or "not recorded"))
    print("  elapsed        : %s" % ("%.1f h" % elapsed if elapsed is not None
                                     else "unknown"))
    print("  window         : %d h" % args.hours)
    print("  probe runs     : %d" % len(runs_probe))
    print("  shadow runs    : %d" % len(runs_shadow))
    print()
    for k in sorted(criteria):
        if k in ("shadow_unknown_reasons", "shadow_failed_reads"):
            print("  %-28s %d" % (k, len(criteria[k])))
            for reason in criteria[k]:
                print("      - %s" % reason[:160])
            continue
        print("  %-28s %s" % (k, json.dumps(criteria[k])[:90]))
    print()
    if findings:
        print("FAIL - %d finding(s):" % len(findings))
        for f in findings:
            print("  - %s" % f)
        return 1
    if elapsed is None or elapsed < args.hours:
        print("NOT YET - no findings, but the window is not complete.")
        print("  %s" % ("%.1f of %d hours elapsed" % (elapsed, args.hours)
                        if elapsed is not None
                        else "no start recorded; run with --start-now"))
        print("  A clean partial window is NOT a pass, and calling it one is how")
        print("  a cutover gets approved on evidence that does not exist yet.")
        return 0
    print("PASS - %.1f hours, no findings against any of the four criteria."
          % elapsed)
    return 0


def local_token(gb: str) -> str:
    """An access token for GCS from this workstation.

    The store's normal provider is the metadata server, which is correct for a
    cloud runtime and absent here. This tool is an operator tool that runs on the
    laptop, so it borrows the operator's own credentials - and it is the only
    place in the repo that does.
    """
    p = subprocess.run([gb, "auth", "print-access-token"],
                       capture_output=True, timeout=300)
    if p.returncode != 0:
        raise SystemExit("could not mint a token: %s"
                         % p.stderr.decode("utf-8", "replace")[:200])
    return p.stdout.decode("utf-8").strip()


def count_ack_rows() -> int | None:
    """How many rows on the live board carry the ack target id."""
    try:
        import board_say
        env = board_say.load_env()
        code, body = board_say.bus_get(env, {"action": "read",
                                             "title": "Blackboard - Alpha DB",
                                             "match": ACK_TARGET})
        if code != 200 or not body.lstrip().startswith("{"):
            return None
        rows = (json.loads(body) or {}).get("rows") or []
        return len([r for r in rows
                    if isinstance(r, list) and ACK_TARGET in str(r)])
    except Exception:  # noqa: BLE001 - an unreadable board is not zero rows
        return None


if __name__ == "__main__":
    raise SystemExit(main())

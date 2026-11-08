#!/usr/bin/env python3
"""Post a task estimate to the board, and later its actual, so estimates can be measured against reality.

The owner, 2026-11-08: every agent puts an estimate of when it will complete a task on the board, so the fleet can
learn which tasks take how long and how far each agent's estimates are off.

    python scripts/estimate.py start --task one-speaker-fix --minutes 240 --what "chair: one speaker, PR to release"
    python scripts/estimate.py start --task one-speaker-codex --parent one-speaker-fix --minutes 30 --what "review"
    python scripts/estimate.py reestimate --task one-speaker-fix --minutes 90 --reason "second Codex round"
    python scripts/estimate.py done  --task one-speaker-fix [--note "2 Codex rounds"]
    python scripts/estimate.py report                    # this machine's estimate vs actual so far

Every agent, every task and subtask (owner, 2026-11-08). Set ESTIMATE_BY to your instance tag (a roster id). A second
session of the same instance sets ESTIMATE_SESSION (posted as session=) and its own ESTIMATE_LEDGER.
Rows (thread TH-ESTIMATES):
    msg_type=ESTIMATE       task, parent, by, started (UTC), est_minutes, est_done (UTC and Toronto), what
    msg_type=ESTIMATE       (reestimate=N) task, by, started, new_est_done, reason
    msg_type=ESTIMATE_DONE  task, parent, by, started, promised, delivered (UTC and Toronto), delta_minutes
                            (+ late / - early, against the FIRST estimate), est_minutes, actual_minutes, ratio,
                            reestimates, note

Times are read from the clock, never typed. The started time and estimate are kept in a local ledger
(~/.claude/estimates.json) so the done row can compute the actual; the board rows are the record.
Any agent without this script posts the same fields by hand, with times from its own clock.
"""
import argparse
import datetime
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Two sessions on one machine keep separate ledgers (ESTIMATE_LEDGER), or one save can drop the other's entry.
LEDGER = Path(os.environ.get("ESTIMATE_LEDGER") or (Path.home() / ".claude" / "estimates.json"))
# No default: a default once posted Codex's estimate under claude-code-cli (2026-11-08). The author must name itself.
ME = os.environ.get("ESTIMATE_BY", "").strip()
# ESTIMATE_BY is the INSTANCE (a scripts/agent_roster.json id; the append guard refuses anything else). Two sessions of
# one instance (two claude-code-cli terminals) tell their rows apart with ESTIMATE_SESSION, e.g. b76 or vscode: it is
# posted as session= and goes into the row id whole (never cut), so two sessions never share an id. Every row id also
# ends in a random suffix, so even two blank-session writers posting in the same second cannot collide.
SESSION = os.environ.get("ESTIMATE_SESSION", "").strip()
SAFE_SESSION = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
try:
    from zoneinfo import ZoneInfo
    TORONTO = ZoneInfo("America/Toronto")
except Exception:                                            # noqa: BLE001 - no tz database: UTC only
    TORONTO = None


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def et(t):
    return t.astimezone(TORONTO).strftime("%I:%M %p ET %a").lstrip("0") if TORONTO else iso(t)


def load():
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(d):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, indent=1), encoding="utf-8")


def clean(s):
    return " ".join(str(s).replace("|", "/").split())


def post(kind, fields):
    who = ME.upper() + (("-" + SESSION.upper()) if SESSION else "")
    rid = "%s-%s-%s-%s" % (who, kind.replace("_", "-"), now().strftime("%Y%m%dT%H%M%SZ"), secrets.token_hex(2).upper())
    if SESSION:
        fields = list(fields[:3]) + [("session", SESSION)] + list(fields[3:])   # near the front, where a reader looks
    parts = ["BCB", "v=1", "id=" + rid, "phase=RESULT", "class=STATUS", "from=" + ME, "to=all",
             "msg_type=" + kind, "thread_id=TH-ESTIMATES"] + ["%s=%s" % (k, clean(v)) for k, v in fields]
    payload = "|".join(parts)
    assert len(payload) <= 1500, len(payload)
    row = {"row_id": rid, "source_tag": ME, "target_surface": "all", "action_type": "APPEND", "category": "STATUS",
           "project_tag": "Blackboard", "gist": "estimate " + kind.lower(), "subgist": dict(fields).get("task", ""),
           "payload": payload}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(row, fh)
    try:
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "append.py"), path], cwd=str(REPO),
                           capture_output=True, text=True, timeout=180)
    finally:
        os.unlink(path)
    ok = "READ-BACK OK" in (r.stdout + r.stderr)
    print(("posted " if ok else "NOT POSTED ") + rid)
    if not ok:
        print((r.stdout + r.stderr)[-600:])
    return ok


def cmd_start(a):
    t0 = now()
    due = t0 + datetime.timedelta(minutes=a.minutes)
    if not post("ESTIMATE", [("task", a.task), ("parent", a.parent or "-"), ("by", ME), ("started", iso(t0)),
                             ("est_minutes", a.minutes), ("est_done", iso(due)), ("est_done_et", et(due)),
                             ("what", a.what)]):
        sys.exit("the ESTIMATE row did not read back; the ledger is unchanged, so this task is NOT recorded")
    d = load()                                   # the ledger records only what the board holds
    d[a.task] = {"started": iso(t0), "est_minutes": a.minutes, "what": a.what, "by": ME, "parent": a.parent}
    save(d)


def cmd_reestimate(a):
    """A slip, said out loud: a new finish time and the reason. The first estimate stays the one judged."""
    d = load()
    e = d.get(a.task)
    if not e:
        sys.exit("no estimate for task %r on this machine" % a.task)
    t = now()
    due = t + datetime.timedelta(minutes=a.minutes)
    n = len(e.get("reestimates", [])) + 1
    if not post("ESTIMATE", [("task", a.task), ("by", ME), ("reestimate", n),
                             ("started", e["started"]), ("new_est_done", iso(due)), ("new_est_done_et", et(due)),
                             ("reason", a.reason)]):
        sys.exit("the re-estimate row did not read back; the ledger is unchanged")
    e.setdefault("reestimates", []).append({"at": iso(t), "new_done": iso(due), "reason": a.reason})
    save(d)


def cmd_done(a):
    d = load()
    e = d.get(a.task)
    if not e:
        sys.exit("no estimate for task %r on this machine; post the ESTIMATE_DONE fields by hand" % a.task)
    t1 = now()
    t0 = datetime.datetime.strptime(e["started"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    actual = round((t1 - t0).total_seconds() / 60)
    est = e["est_minutes"]
    promised = t0 + datetime.timedelta(minutes=est)            # the FIRST estimate's promised delivery time
    delta = round((t1 - promised).total_seconds() / 60)        # + late, - early
    ok = post("ESTIMATE_DONE", [("task", a.task), ("parent", e.get("parent") or "-"), ("by", ME), ("started", e["started"]),
                           ("promised", iso(promised)), ("promised_et", et(promised)),
                           ("delivered", iso(t1)), ("delivered_et", et(t1)),
                           ("delta_minutes", ("+%d" % delta) if delta > 0 else str(delta)),
                           ("est_minutes", est), ("actual_minutes", actual),
                           ("ratio", "%.2f" % (actual / est if est else 0)),
                           ("reestimates", len(e.get("reestimates", []))), ("note", a.note or "")])
    if not ok:
        sys.exit("the ESTIMATE_DONE row did not read back; the task stays open in the ledger, run done again")
    e.update(done=iso(t1), actual_minutes=actual, note=a.note)
    save(d)


def cmd_report(_a):
    for task, e in load().items():
        if "actual_minutes" in e:
            print("%-28s est %4d  actual %4d  ratio %.2f" % (task, e["est_minutes"], e["actual_minutes"],
                                                            e["actual_minutes"] / e["est_minutes"]))
        else:
            print("%-28s est %4d  open since %s" % (task, e["est_minutes"], e["started"]))


def roster_ids():
    try:
        d = json.loads((REPO / "scripts" / "agent_roster.json").read_text(encoding="utf-8"))
        return {i["id"] for f in d.get("families", []) for i in f.get("instances", [])}
    except (OSError, ValueError, KeyError, TypeError):
        return None                                          # no roster here: append.py's own guard decides


def main():
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="cmd", required=True)
    a = s.add_parser("start"); a.add_argument("--task", required=True); a.add_argument("--minutes", type=int, required=True)
    a.add_argument("--what", required=True)
    a.add_argument("--parent", default="", help="the task this is a subtask of")
    b = s.add_parser("done"); b.add_argument("--task", required=True); b.add_argument("--note", default="")
    r = s.add_parser("reestimate"); r.add_argument("--task", required=True)
    r.add_argument("--minutes", type=int, required=True, help="minutes from now"); r.add_argument("--reason", required=True)
    s.add_parser("report")
    args = p.parse_args()
    if args.cmd != "report" and not ME:
        sys.exit("set ESTIMATE_BY to your instance tag (e.g. ESTIMATE_BY=codex); nothing was posted")
    if args.cmd != "report" and SESSION and not SAFE_SESSION.match(SESSION):
        sys.exit("ESTIMATE_SESSION must be 1-24 of a-z 0-9 - (lower case, starting with a letter or digit); "
                 "nothing was posted")
    ids = roster_ids()
    if args.cmd != "report" and ids is not None and ME not in ids:
        sys.exit("ESTIMATE_BY=%s is not an instance in scripts/agent_roster.json, so the board would refuse it. Use the "
                 "instance tag and name the session with ESTIMATE_SESSION (e.g. ESTIMATE_BY=claude-code-cli "
                 "ESTIMATE_SESSION=b76); nothing was posted" % ME)
    {"start": cmd_start, "done": cmd_done, "reestimate": cmd_reestimate, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()

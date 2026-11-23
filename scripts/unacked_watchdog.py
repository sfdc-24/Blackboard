#!/usr/bin/env python3
"""unacked_watchdog - an ask to a live agent that goes 15 minutes unanswered raises an alert.

WHY THIS EXISTS
  The owner's order of 2026-11-23 (CLAUDE-MOBILE-OWNER-ORDER-GEMINI-NOT-RESPONDING-20261123T2232Z)
  and Gemini's ruling on it (GEMINI-RULING-ALL-OPEN-OWNER-ORDERS-20261123T2241Z): an ask can sit
  on the board with nobody answering and nothing notices. A poka-yoke, not a reminder: one pass of
  this script finds every such ask and says so on the board, once.

WHAT COUNTS AS AN ASK
  A canonical BCB-1 row (open_for_me.is_canonical_bcb) whose phase= is in ask_phases.ASK_PHASES
  (ASK, REQUEST, DISPATCH, REVIEW_REQUEST, TASK - whatever its class=, ORDER and ARCH_REVIEW
  included) and whose to= names at least one LIVE agent. Only to= addresses an agent: cc= is a copy
  for reading, and Target_Surface is a routing hint the writer may not have set.

  LIVE comes from scripts/agent_roster.json, the one list of who exists: an instance whose state
  starts with "active", outside the owner's channels and the retired family. An alias ("Alias of
  chatgpt-codex-desktop." in its wake line) is resolved to the instance it names, so to=codex is
  chatgpt-codex-desktop and a row signed codex-desktop answers for it. A family name that is not an
  instance (to=claude) addresses nobody this script can hold to an SLA, and is skipped.

  Re-asks under one BCB id= are ONE ask: the clock starts at the earliest copy, and an answer naming
  the id or any copy's Row_ID answers all of them.

WHAT COUNTS AS AN ANSWER
  A later row FROM THAT AGENT (Source_Tag or from=, after alias resolution) whose answers=,
  reply_to= or closes= names the ask's id= or one of its Row_IDs (exact token, case-insensitive;
  lists split on comma, semicolon or whitespace like every other reader here). Any phase counts:
  an ACK is an answer for this purpose, because the failure being caught is silence, not slowness.
  A row from a different agent does not answer for the one that was asked.

  The asker can withdraw an ask: a row from the asker with supersedes= or closes= naming it, or
  whose text says "supersedes <its id>" (the fleet's re-ask-at-a-new-head habit), retires it for
  every addressee.

WHEN IT ALERTS
  When a pass runs at least 15 minutes after the ask and some addressed agent still has no answering
  row. An ask to several agents where one answered alerts for the others only. An answer that lands
  late, but before the pass, is an answer: alerting after the reply exists would be noise.

THE ALERT
  One row per ask, Row_ID and id= both SLA-ALERT-UNACKED-15M-<ask id>, on thread TH-BINDING,
  to= the silent agents (so their doorbells ring on it), cc= the asker. The id is deterministic on
  purpose: it is what makes "once per ask, never repeated" hold across machines and a lost state
  file. A pass skips any ask whose alert row is already on the board, and also any ask in the local
  state file. The alert names the ask with ask=, never answers=, so the alert can never be mistaken
  for an answer from its own poster.

THE WAKE
  After posting, each silent agent that scripts/agent_waker.py has an adapter for (its AGENTS table,
  read from the source so this never drifts and never imports the waker's dependencies) gets one
  foreground pass: python scripts/agent_waker.py --agent <tag>. Never backgrounded. One pass per
  agent per run, however many alerts name it. An agent with no agent_waker path is reached by the
  alert row's to= alone, which is what fleet_ring.py and the other doorbells ring on; the row says
  which of the two each agent got.

USAGE
  python scripts/unacked_watchdog.py --dry-run     print what it would alert, post and wake nothing
  python scripts/unacked_watchdog.py               one pass: alert, then wake

  Runs once per invocation and exits; schedule it from outside. State (alerted ask ids) lives in
  .unacked_watchdog_state.json under BLACKBOARD_STATE_DIR or the repo root, gitignored like the
  waker watermarks.

EXIT CODES
  0  pass completed (alerts posted or none due)
  1  an alert could not be confirmed on the board (it is retried next pass; the board check stops a
     duplicate if it did land)
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import open_for_me  # noqa: E402
from ask_phases import ASK_PHASES  # noqa: E402

REPO = os.path.dirname(HERE)
ROSTER_PATH = os.path.join(HERE, "agent_roster.json")
WAKER_PATH = os.path.join(HERE, "agent_waker.py")
APPEND_PATH = os.path.join(HERE, "append.py")

SLA_MINUTES = 15
ALERT_PREFIX = "SLA-ALERT-UNACKED-15M-"
THREAD = "TH-BINDING"
ANSWER_KEYS = ("answers", "reply_to", "closes")
WITHDRAW_KEYS = ("supersedes", "closes")
# A re-ask that retires an older one says so in prose as often as in a field: "it supersedes
# CLAUDE-CLI-ASK-REVIEW-642-...". Counted only when the row is FROM THE ASKER of the older ask.
_SUPERSEDES_TEXT = re.compile(r"\bsupersedes\s+([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)", re.I)
NOT_AGENT_FAMILIES = ("owner", "retired")
MAX_ROW = 1500
STATE_KEEP_DAYS = 14

C_ROW_ID, C_TS, C_SOURCE, C_PAYLOAD = 0, 1, 2, 5
_ID_SAFE = re.compile(r"[^A-Za-z0-9._:-]")
_ALIAS_RE = re.compile(r"^\s*alias of\s+([A-Za-z0-9_-]+)", re.I)


# ------------------------------------------------------------------ who is live

def load_roster(path=ROSTER_PATH):
    """(live instance ids, alias -> instance id), all lower-case, from the roster file."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    live, alias = set(), {}
    for fam in data.get("families") or []:
        family = str(fam.get("family") or "").lower()
        for inst in fam.get("instances") or []:
            tag = str(inst.get("id") or "").strip().lower()
            if not tag:
                continue
            m = _ALIAS_RE.match(str(inst.get("wake") or ""))
            if m:
                alias[tag] = m.group(1).lower()
                continue
            if family in NOT_AGENT_FAMILIES:
                continue
            if str(inst.get("state") or "").strip().lower().startswith("active"):
                live.add(tag)
    return live, alias


def canonical(tag, alias):
    t = str(tag or "").strip().lower()
    return alias.get(t, t)


def waker_agents(path=WAKER_PATH):
    """The keys of agent_waker.AGENTS, read from its source without importing it.

    Importing would pull in repo_context, cloud_context and okf_land just to learn five names, and a
    watchdog that dies on an ImportError in a module it never calls is a watchdog that does not bark.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return set()
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == "AGENTS" for t in node.targets)):
            return {k.value.lower() for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


# ------------------------------------------------------------------ reading rows

def _cell(row, i):
    return str(row[i]) if isinstance(row, list) and len(row) > i and row[i] is not None else ""


def _senders(row, payload, alias):
    """Who a row is from: its Source_Tag and its from=, both resolved. Some writers omit from=."""
    out = {canonical(_cell(row, C_SOURCE), alias)}
    frm = open_for_me.field(payload, "from")
    if frm:
        out.add(canonical(frm, alias))
    out.discard("")
    return out


def _refs(payload, keys):
    out = set()
    for k in keys:
        out.update(t.lower() for t in open_for_me.tokens(payload, k))
    return out


def safe_id(text, limit=160):
    """An id fit to sit inside a BCB field: no pipe, no space, bounded."""
    return _ID_SAFE.sub("", str(text or ""))[:limit]


def find_unanswered(rows, now, live, alias, sla_minutes=SLA_MINUTES, skip=()):
    """Every ask past its SLA with at least one silent live addressee, oldest first.

    `skip` is a set of lower-case ask ids already alerted (local state). Asks whose alert row is on
    the board are skipped too, from the rows themselves.
    """
    sla = timedelta(minutes=sla_minutes)
    skip = {s.lower() for s in skip}
    answered = {}        # ref (lower) -> set of canonical senders whose row names it
    withdrawn = {}       # ref (lower) -> set of canonical senders who superseded or closed it
    alerted = set()      # ask ids (lower) whose alert row is on the board
    asks = {}            # ask key (lower) -> ask record

    for row in rows:
        payload = _cell(row, C_PAYLOAD)
        row_id = _cell(row, C_ROW_ID).strip()
        bcb = (open_for_me.field(payload, "id") or "").strip()
        for name in (row_id, bcb):
            if name.upper().startswith(ALERT_PREFIX):
                alerted.add(name[len(ALERT_PREFIX):].lower())
        senders = _senders(row, payload, alias)
        for ref in _refs(payload, ANSWER_KEYS):
            answered.setdefault(ref, set()).update(senders)
        for ref in _refs(payload, WITHDRAW_KEYS) | {
                m.group(1).lower() for m in _SUPERSEDES_TEXT.finditer(payload)}:
            withdrawn.setdefault(ref, set()).update(senders)

        if not open_for_me.is_canonical_bcb(payload):
            continue
        phase = (open_for_me.field(payload, "phase") or "").upper()
        if phase not in ASK_PHASES:
            continue
        ts = open_for_me.parse_ts(_cell(row, C_TS))
        if ts is None:
            continue                       # an unparseable stamp is skipped, never guessed
        asker = canonical(open_for_me.field(payload, "from") or _cell(row, C_SOURCE), alias)
        to = []
        for t in open_for_me.tokens(payload, "to"):
            c = canonical(t, alias)
            if c in live and c != asker and c not in to:
                to.append(c)
        if not to:
            continue
        key = (bcb or row_id).lower()
        rec = asks.get(key)
        if rec is None:
            rec = asks[key] = {"ask_id": bcb or row_id, "names": set(), "ts": ts, "asker": asker,
                               "class": open_for_me.field(payload, "class") or "", "phase": phase,
                               "to": []}
        rec["names"].update(n.lower() for n in (bcb, row_id) if n)
        rec["ts"] = min(rec["ts"], ts)
        rec["to"].extend(a for a in to if a not in rec["to"])

    out = []
    for key, rec in asks.items():
        if key in skip or any(n in alerted for n in rec["names"]):
            continue
        if any(rec["asker"] in withdrawn.get(n, ()) for n in rec["names"]):
            continue
        age = now - rec["ts"]
        if age < sla:
            continue
        silent = [a for a in rec["to"]
                  if not any(a in answered.get(n, ()) for n in rec["names"])]
        if not silent:
            continue
        out.append({"ask_id": rec["ask_id"], "asked": rec["ts"], "asker": rec["asker"],
                    "class": rec["class"], "phase": rec["phase"], "to": list(rec["to"]),
                    "silent": silent, "age_min": int(age.total_seconds() // 60)})
    out.sort(key=lambda c: c["asked"])
    return out


# ------------------------------------------------------------------ the alert row

def alert_row_id(ask_id):
    return ALERT_PREFIX + safe_id(ask_id)


def build_payload(cand, me, wakeable, now):
    rid = alert_row_id(cand["ask_id"])
    ask = safe_id(cand["ask_id"])
    silent = [safe_id(a, 40) for a in cand["silent"]]
    wake = ";".join("%s:%s" % (a, "agent_waker" if a in wakeable else "ring-only") for a in silent)
    text = ("No answering row from %s to %s %s %s from %s within %d minutes (asked %s, %d min ago). "
            "Waking through scripts/agent_waker.py where it has an adapter; ring-only agents are "
            "reached by this row's to= alone. Reply with an answers field naming %s."
            % (", ".join(silent), safe_id(cand["phase"], 20), safe_id(cand["class"], 30) or "-",
               ask, safe_id(cand["asker"], 40), SLA_MINUTES,
               cand["asked"].strftime("%Y-%m-%dT%H:%M:%SZ"), cand["age_min"], ask))
    payload = ("BCB|v=1|id=%s|phase=ALERT|class=SLA|from=%s|to=%s|cc=%s|thread_id=%s|ask=%s"
               "|agents=%s|asked_utc=%s|sla_min=%d|age_min=%d|wake=%s|posted_utc=%s|text=%s"
               % (rid, me, ";".join(silent), safe_id(cand["asker"], 40), THREAD, ask,
                  ";".join(silent), cand["asked"].strftime("%Y-%m-%dT%H:%M:%SZ"), SLA_MINUTES,
                  cand["age_min"], wake, now.strftime("%Y-%m-%dT%H:%M:%SZ"), text))
    payload = payload.replace("\n", " ").replace("\r", " ")
    if len(payload) > MAX_ROW:
        payload = payload[:MAX_ROW]
    return rid, payload


# ------------------------------------------------------------------ state

def state_path():
    root = os.environ.get("BLACKBOARD_STATE_DIR") or REPO
    return os.path.join(root, ".unacked_watchdog_state.json")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            s = json.load(fh)
    except (OSError, ValueError):
        s = {}
    if not isinstance(s.get("alerted"), dict):
        s["alerted"] = {}
    return s


def save_state(path, state, now):
    cutoff = now - timedelta(days=STATE_KEEP_DAYS)
    keep = {}
    for k, v in state["alerted"].items():
        at = open_for_me.parse_ts((v or {}).get("at", ""))
        if at is None or at >= cutoff:
            keep[k] = v
    state["alerted"] = keep
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ------------------------------------------------------------------ side effects

def read_window(since_iso):
    import bus
    return bus.read_rows(bus.load_env(), since=since_iso)["rows"]


def post_row(spec, run=subprocess.run, timeout=300):
    """Append through scripts/append.py (its guards and its read-back). True only on READ-BACK OK
    or a duplicate it reports - either way the row is on the board."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="unacked-alert-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
        done = run([sys.executable, "-B", APPEND_PATH, path], capture_output=True, text=True,
                   timeout=timeout, cwd=HERE)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = (done.stdout or "")
    return ("READ-BACK OK" in out) or done.returncode == 2, out[-400:] + (done.stderr or "")[-200:]


def wake_agent(agent, run=subprocess.run, timeout=600):
    """One foreground agent_waker pass for this agent. Never backgrounded."""
    try:
        done = run([sys.executable, "-B", WAKER_PATH, "--agent", agent], capture_output=True,
                   text=True, timeout=timeout, cwd=HERE)
    except subprocess.TimeoutExpired:
        return "timeout after %ds" % timeout
    return "exit %d" % done.returncode


# ------------------------------------------------------------------ main

def main(argv=None, read=None, post=None, wake=None, now=None):
    ap = argparse.ArgumentParser(description="Alert on asks a live agent has not answered in 15 min")
    ap.add_argument("--dry-run", action="store_true", help="print what would be alerted; no writes")
    ap.add_argument("--since-hours", type=float, default=6.0,
                    help="how far back to look for asks (default 6)")
    ap.add_argument("--max", type=int, default=10,
                    help="most alerts posted in one pass, oldest first (default 10); the rest wait "
                         "for the next pass")
    ap.add_argument("--from", dest="me", default="claude-code-cli",
                    help="the roster tag the alert rows are signed with (default claude-code-cli)")
    ap.add_argument("--no-wake", action="store_true", help="post alerts but run no agent_waker pass")
    args = ap.parse_args(argv)

    now = now or datetime.now(timezone.utc)
    read = read or read_window
    post = post or post_row
    wake = wake or wake_agent

    live, alias = load_roster()
    wakeable = waker_agents()
    spath = state_path()
    state = load_state(spath)

    since = (now - timedelta(hours=args.since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = read(since)
    cands = find_unanswered(rows, now, live, alias, skip=state["alerted"].keys())
    print("=== unacked_watchdog %s | %d rows since %s | %d ask(s) past %d min unanswered%s"
          % (now.strftime("%Y-%m-%dT%H:%M:%SZ"), len(rows), since, len(cands), SLA_MINUTES,
             " | DRY RUN" if args.dry_run else ""))

    failed = 0
    to_wake = []
    for cand in cands[: max(0, args.max)]:
        rid, payload = build_payload(cand, args.me, wakeable, now)
        print("  ALERT %s silent=%s asker=%s age=%dmin" % (
            cand["ask_id"], ",".join(cand["silent"]), cand["asker"], cand["age_min"]))
        if args.dry_run:
            print("    would post (%d chars): %s" % (len(payload), payload))
            continue
        spec = {"row_id": rid, "source_tag": args.me, "target_surface": ";".join(cand["silent"]),
                "action_type": "APPEND", "payload": payload, "category": "ALERT",
                "project_tag": "binding", "gist": "sla-alert unacked 15m", "subgist": rid}
        ok, detail = post(spec)
        if not ok:
            failed += 1
            print("    POST UNCONFIRMED; retried next pass: %s" % detail.strip()[-200:])
            continue
        state["alerted"][cand["ask_id"].lower()] = {
            "row_id": rid, "agents": cand["silent"], "at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        save_state(spath, state, now)      # before any wake: a crash mid-wake never re-posts
        print("    posted %s" % rid)
        to_wake.extend(a for a in cand["silent"] if a in wakeable and a not in to_wake)
    if len(cands) > args.max:
        print("  %d more wait for the next pass (--max %d)" % (len(cands) - args.max, args.max))

    if args.dry_run:
        would = sorted({a for c in cands[: max(0, args.max)] for a in c["silent"] if a in wakeable})
        print("  would wake through agent_waker: %s" % (", ".join(would) or "nobody"))
        return 0
    if not args.no_wake:
        for agent in to_wake:
            print("  wake %s via agent_waker: %s" % (agent, wake(agent)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

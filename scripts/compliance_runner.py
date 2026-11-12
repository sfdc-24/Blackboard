#!/usr/bin/env python3
"""Fleet compliance runner: read the board, find rule gaps, ring each gap ONCE at the agent that owns it.

    python scripts/compliance_runner.py                  one unattended cycle (the scheduled task)
    python scripts/compliance_runner.py --dry-run        print findings; post nothing, no WhatsApp, no state
    python scripts/compliance_runner.py --since-hours 6  look further back (default 3)
    python scripts/compliance_runner.py --no-whatsapp    ring on the board but never escalate

WHAT ONE CYCLE DOES
    1. Reads the board rows of the last N hours (bus.read_rows, credentials exactly as bus.py finds them).
    2. Runs every detector. Each one is a pure function over parsed rows (and injected PR lists, process
       command lines and free RAM) so the tests run offline on fake fixtures:
         R-PR      a PR number in a row without its repo name (bare "#N", "PR N", "PRN").
         R9        an owner PROMPT row with no PROMPT_RESPONSE/OWNER_REPLY (reply_to= or answers=) within
                   15 min; and owner-log rows on a thread that is not TH-<AGENT>-OWNER-<Toronto YYYYMMDD>.
         R10a      an ESTIMATE with no ESTIMATE_DONE for its task by est_done + 15 min.
         R-ACK     a phase=ASK row with no answers=<ask id> row from the addressee within 15 min.
         R-THREAD  a row with no thread_id= (thread= alone does not count).
         R-PRCAP   open PRs per repo over the cap (6 until the end of Fri Nov 13 2026 ET, then 3).
         R-HEALTH  free RAM under 4 GB, or a binder process (run_clay, run_cody, run_jemma, clay_wait) gone.
    3. Compares with the state file (%USERPROFILE%/.claude/compliance-runner/state.json). A gap is keyed by
       rule + offending row id (or agent + Toronto day) and rings ONCE: one ASK row to the failing agent on
       TH-COMPLIANCE, at most 8 per cycle, the rest in one summary row. Its own ring rows on the board also
       count as "already rung", so a lost state file does not ring everything again.
    4. A gap still open 2 cycles (30 min) after its ring goes to the owner on WhatsApp once (3 lines, ids
       only). A cleared gap is dropped. With nothing to say it posts one heartbeat RESULT per 6 hours.

It never raises out of main: errors go to runner.log in the state directory and the exit code is 0,
because it runs unattended. --dry-run exits 1 when it found gaps, so a person can use it as a check.

LIMITS, stated so nobody reads more into it
    A ring to claude-code-cli is written BY claude-code-cli, and that session's inbox skips its own rows,
    so a self-gap reaches that session only through the escalation. The R-PR scan is a pattern; it can
    flag a non-PR "#12" in prose. Rows outside the look-back window are not judged at all.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:
    from zoneinfo import ZoneInfo
    TORONTO = ZoneInfo("America/Toronto")
except Exception:  # noqa: BLE001 - no tzdata: fall back to the fixed DST rule below
    TORONTO = None

SELF = "claude-code-cli"
FLEET = ("claude-code-cli", "grok", "chatgpt-codex-desktop", "gemini", "cursor")
NOT_AGENTS = {"", "-", "all", "fleet", "owner", "everyone", "none"}
COMPLIANCE_THREAD = "TH-COMPLIANCE"
MAX_RINGS = 8
WINDOW = timedelta(minutes=15)
ESCALATE_CYCLES = 2
ESCALATE_AFTER = timedelta(minutes=30)
HEARTBEAT_EVERY = timedelta(hours=6)
MAX_ROW_CHARS = 1500
MIN_FREE_RAM = 4 * 1024 ** 3
BINDERS = ("run_clay", "run_cody", "run_jemma", "clay_wait")
REPOS = ("sfdc-24/conference", "sfdc-24/Blackboard", "sfdc-24/sfdc24-site")
# Owner ruling: 6 open PRs per repo until the end of Friday Nov 13 2026 (Toronto), then 3.
PRCAP_WIDE_UNTIL = datetime(2026, 11, 14, 5, 0, tzinfo=timezone.utc)   # 2026-11-14 00:00 EST

PROMPT_TYPES = {"PROMPT", "OWNER_PROMPT"}
REPLY_TYPES = {"PROMPT_RESPONSE", "OWNER_REPLY"}
ACK_CLEARABLE = {"R-PR", "R-THREAD", "R9-THREAD"}
LOG_LABEL = {"claude-code-cli": "CLI", "grok": "GROK", "chatgpt-codex-desktop": "CODEX", "gemini": "GEMINI",
             "cursor": "CURSOR"}


# ---- time -----------------------------------------------------------------------------------------

ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?")


def parse_ts(value):
    """The first ISO-8601 timestamp in a value, as aware UTC; no zone means UTC. None if there is none."""
    m = ISO.search(str(value or ""))
    if not m:
        return None
    text = m.group(0).replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _us_dst(ts):
    """Eastern DST without tzdata: second Sunday of March 07:00Z to first Sunday of November 06:00Z."""
    y = ts.year
    march = datetime(y, 3, 8, 7, tzinfo=timezone.utc)
    start = march + timedelta(days=(6 - march.weekday()) % 7)
    nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return start <= ts < end


def toronto(ts):
    if TORONTO is not None:
        return ts.astimezone(TORONTO)
    return ts.astimezone(timezone(timedelta(hours=-4 if _us_dst(ts) else -5)))


def toronto_day(ts):
    return toronto(ts).strftime("%Y%m%d")


def stamp(ts):
    return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")


def iso(ts):
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- rows -----------------------------------------------------------------------------------------

KEY = r"[a-z][a-z0-9_]{0,30}"
HYBRID_KEY = re.compile(r"(?:^|\|\s*|\s+)(" + KEY + r")=")


def parse_payload(payload):
    """The key=value fields of one payload, first value per key.

    Two shapes are live: BCB rows ("BCB|v=1|id=..|text=..", pipe-delimited, no escaping) and rows that
    start "|id=X|" and then separate keys with spaces or "| ". In the second shape a value runs to the
    next " key=" or "|", and text= takes the rest of the row, since prose can hold "a=b".
    """
    p = str(payload or "")
    out = {}
    if p.startswith("BCB|"):
        for part in p.split("|")[1:]:
            k, sep, v = part.partition("=")
            k = k.strip()
            if sep and re.fullmatch(KEY, k):
                out.setdefault(k, v.strip())
        return out
    marks = list(HYBRID_KEY.finditer(p))
    for i, m in enumerate(marks):
        k = m.group(1)
        if k == "text":
            out.setdefault(k, p[m.end():].strip().strip("'\""))
            break
        end = marks[i + 1].start() if i + 1 < len(marks) else len(p)
        out.setdefault(k, p[m.end():end].strip().strip("|").strip().strip("'\""))
    return out


def canonical(tag):
    """One fleet name per agent, whatever instance tag it wrote under."""
    t = str(tag or "").strip().lower()
    if not t:
        return ""
    if t in ("claude", "clay") or t.startswith("claude-code-cli") or t.startswith("vm-claude-code-cli"):
        return "claude-code-cli"
    if t.startswith("grok"):
        return "grok"
    if "codex" in t:
        return "chatgpt-codex-desktop"
    if t.startswith("gemini"):
        return "gemini"
    if t.startswith("cursor"):
        return "cursor"
    return t


def split_ids(value):
    return [x for x in re.split(r"[;,\s]+", str(value or "")) if x]


class Row:
    __slots__ = ("cell_id", "ts", "source", "target", "payload", "f", "id", "sender", "thread",
                 "phase", "cls", "msg_type", "refs", "who")

    def __init__(self, cells):
        cells = list(cells) + [""] * 12
        self.cell_id = str(cells[0] or "").strip()
        self.ts = parse_ts(cells[1])
        self.source = str(cells[2] or "").strip()
        self.target = str(cells[3] or "").strip()
        self.payload = str(cells[5] or "")
        self.f = parse_payload(self.payload)
        self.id = self.f.get("id") or self.cell_id
        self.sender = canonical(self.f.get("from") or self.source)
        self.thread = self.f.get("thread_id", "")
        self.phase = self.f.get("phase", "").upper()
        self.cls = self.f.get("class", "").upper()
        self.msg_type = self.f.get("msg_type", "").upper()
        self.refs = set(split_ids(self.f.get("answers")) + split_ids(self.f.get("reply_to")))
        by = split_ids(self.f.get("by"))[:1]
        self.who = {canonical(x) for x in [self.f.get("from"), self.source, self.f.get("claimed_author")] + by
                    if x} - {""}

    def addressees(self):
        raw = self.f.get("to") or self.target
        return {canonical(x) for x in re.split(r"[;,\s]+", raw)} - NOT_AGENTS - {self.sender}


def parse_rows(cells_list):
    rows = [Row(c) for c in cells_list if isinstance(c, (list, tuple)) and c]
    return sorted([r for r in rows if r.ts is not None], key=lambda r: r.ts)


def finding(rule, key, agent, row_id, missing, action, **extra):
    key = re.sub(r"[|\s]+", "_", key)
    out = {"rule": rule, "key": key, "agent": agent, "row_id": row_id or "", "missing": missing,
           "action": action}
    out.update(extra)
    return out


def own_ring(row):
    return row.sender == SELF and row.cls == "COMPLIANCE"


# ---- detectors (pure) -----------------------------------------------------------------------------

REPO = r"(?:sfdc-24/)?(?:conference|blackboard|sfdc24-site|converspan|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
# "conference #428", "conference PR #428", "Blackboard PRs #484", "conference: #428", and the
# parenthesis form "Blackboard 2 open (#484".
REPO_BEFORE = re.compile(REPO + r"(?:\s*(?:PRs?|:)?\s*|(?:\s+[\w/:.-]+){0,3}\s*\(\s*)$", re.I)
PR_REF = re.compile(r"(?<![A-Za-z0-9_/#-])(?:PR\s*#?\s*(\d{2,5})|#(\d{2,5}))(?![A-Za-z0-9_-])", re.I)
# Between two numbers of one list: "conference #427 and #428", "#484,#485", "#427/#428", "#420 #428".
LIST_GAP = re.compile(r"[\s,/;&+]*(?:and|or)?[\s,/;&+]*", re.I)


def bare_pr_refs(text):
    """The PR numbers in a text that do not carry a repo name. A number carries it when the repo is
    named right before it, or when it continues a list that started with a repo-named number. Ids such as
    X-PR428-Y are not references (a hyphen on either side rules a match out), and a quoted number
    ('#428') is a mention of the pattern, not a reference to a PR."""
    text = str(text or "")
    found = []
    prev_ok, prev_end = False, -1
    for m in PR_REF.finditer(text):
        before = text[max(0, m.start() - 48):m.start()]
        quoted = m.start() > 0 and text[m.start() - 1] in "'\"`"
        continues = prev_ok and LIST_GAP.fullmatch(text[prev_end:m.start()]) is not None
        ok = quoted or continues or REPO_BEFORE.search(before) is not None
        if not ok:
            found.append(m.group(1) or m.group(2))
        prev_ok, prev_end = ok, m.end()
    return found


def detect_pr_names(rows, now=None):
    out = []
    for r in rows:
        if r.sender not in FLEET or own_ring(r):
            continue
        nums = bare_pr_refs(r.payload)
        pr = r.f.get("pr", "")
        if re.fullmatch(r"#?\d{2,5}", pr) and not r.f.get("repo"):
            nums.append(pr.lstrip("#"))
        if nums:
            out.append(finding(
                "R-PR", "R-PR:%s" % r.id, r.sender, r.id,
                "%d PR number(s) without the repo name" % len(nums),
                "Post a correction row with answers=%s naming each PR with its repo (conference #N, "
                "Blackboard #N, sfdc24-site #N)" % r.id))
    return out


def detect_owner_log(rows, now):
    out = []
    replies = [r for r in rows if r.msg_type in REPLY_TYPES]
    for p in rows:
        if p.msg_type not in PROMPT_TYPES or p.sender not in FLEET:
            continue
        if now - p.ts < WINDOW:
            continue
        if any(p.id in r.refs and r.ts >= p.ts for r in replies):
            continue
        out.append(finding(
            "R9", "R9:%s" % p.id, p.sender, p.id, "no PROMPT_RESPONSE or OWNER_REPLY within 15 min",
            "Post the reply row verbatim with reply_to=%s on the same owner thread" % p.id))
    seen = set()
    for r in rows:
        if r.msg_type not in PROMPT_TYPES | REPLY_TYPES or r.sender not in FLEET:
            continue
        thread = r.thread or r.f.get("thread", "")
        said = parse_ts(r.f.get("said_at")) or r.ts
        days = {toronto_day(said), toronto_day(r.ts)}
        m = re.fullmatch(r"TH-([A-Z0-9]+(?:-[A-Z0-9]+)*)-OWNER-(\d{8})", thread)
        if m and m.group(2) in days:
            continue
        day = toronto_day(r.ts)
        key = "R9-THREAD:%s:%s" % (r.sender, day)
        if key in seen:
            continue
        seen.add(key)
        label = m.group(1) if m else LOG_LABEL.get(r.sender, r.sender.upper())
        out.append(finding(
            "R9", key, r.sender, r.id, "owner-log thread is not TH-<AGENT>-OWNER-<Toronto date>",
            "Log owner prompts and replies on TH-%s-OWNER-%s from now on; ACK this row with answers= its id"
            % (label, day), sub="R9-THREAD"))
    return out


def _is_estimate_done(r):
    return r.msg_type == "ESTIMATE_DONE" or "ESTIMATE-DONE" in r.id.upper() or "ESTIMATE_DONE" in r.id.upper()


def _is_estimate(r):
    if _is_estimate_done(r):
        return False
    on_thread = (r.thread or r.f.get("thread", "")) == "TH-ESTIMATES"
    return on_thread and (r.msg_type == "ESTIMATE" or r.phase == "ESTIMATE" or "-ESTIMATE-" in r.id.upper())


def _deadline(r):
    done = parse_ts(r.f.get("est_done"))
    if done:
        return done
    start = parse_ts(r.f.get("started"))
    m = re.match(r"\d+", r.f.get("est_minutes", ""))
    if start and m:
        return start + timedelta(minutes=int(m.group(0)))
    return None


def detect_estimates(rows, now):
    out = []
    latest = {}
    done_tasks = set()
    for r in rows:
        task = r.f.get("task", "").strip().lower()
        if _is_estimate_done(r):
            if task:
                done_tasks.add(task)
            continue
        if not _is_estimate(r) or r.sender not in FLEET:
            continue
        if not task:
            out.append(finding("R10a", "R10a:%s" % r.id, r.sender, r.id, "ESTIMATE row has no task=",
                               "Re-post the estimate with task= and est_done="))
            continue
        latest[task] = r                                   # rows are oldest first: a re-estimate wins
    for task, r in latest.items():
        if task in done_tasks:
            continue
        due = _deadline(r)
        if due is None:
            out.append(finding("R10a", "R10a:%s" % r.id, r.sender, r.id, "ESTIMATE row has no est_done=",
                               "Re-post the estimate for task=%s with est_done= in UTC" % task))
            continue
        if now >= due + WINDOW:
            out.append(finding(
                "R10a", "R10a:%s" % r.id, r.sender, r.id, "no ESTIMATE_DONE by est_done + 15 min",
                "Post ESTIMATE_DONE for task=%s on TH-ESTIMATES, or a re-estimate with a new est_done" % task))
    return out


def detect_acks(rows, now):
    out = []
    for a in rows:
        if a.phase != "ASK" or a.cls == "COMPLIANCE" or now - a.ts < WINDOW:
            continue
        for agent in sorted(a.addressees()):
            if agent not in FLEET:
                continue
            if any(a.id in r.refs and agent in r.who and r.ts >= a.ts for r in rows):
                continue
            out.append(finding(
                "R-ACK", "R-ACK:%s:%s" % (a.id, agent), agent, a.id,
                "no ACK, RESULT or DONE with answers= this ask within 15 min",
                "Post ACK (or RESULT/DONE) with answers=%s" % a.id))
    return out


def detect_threads(rows, now=None):
    groups = {}
    for r in rows:
        if r.sender not in FLEET or r.thread:
            continue
        groups.setdefault((r.sender, toronto_day(r.ts)), []).append(r)
    out = []
    for (agent, day), bad in sorted(groups.items()):
        alias = sum(1 for r in bad if r.f.get("thread"))
        hint = " (%d used thread= instead of thread_id=)" % alias if alias else ""
        out.append(finding(
            "R-THREAD", "R-THREAD:%s:%s" % (agent, day), agent, bad[0].id,
            "%d row(s) without thread_id%s" % (len(bad), hint),
            "Put thread_id= on every row from now on; ACK this row with answers= its id", count=len(bad)))
    return out


def prcap(now):
    return 6 if now < PRCAP_WIDE_UNTIL else 3


def branch_agent(branch):
    head = str(branch or "").split("/", 1)[0]
    agent = canonical(head)
    return agent if agent in FLEET else SELF


def detect_prcap(pr_lists, now):
    """pr_lists: {"sfdc-24/conference": [{"number": 1, "headRefName": "grok/x"}, ...]}; None = unknown."""
    cap = prcap(now)
    out = []
    for repo, prs in sorted((pr_lists or {}).items()):
        if prs is None or len(prs) <= cap:
            continue
        owners = {}
        for pr in prs:
            a = branch_agent(pr.get("headRefName"))
            owners[a] = owners.get(a, 0) + 1
        agent = sorted(owners.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        short = repo.split("/")[-1]
        out.append(finding(
            "R-PRCAP", "R-PRCAP:%s" % repo, agent, "", "%s has %d open PRs, cap %d" % (repo, len(prs), cap),
            "Merge or close %d %s PR(s) before opening another" % (len(prs) - cap, short)))
    return out


def detect_health(free_ram, cmdlines, now=None):
    out = []
    if free_ram is not None and free_ram < MIN_FREE_RAM:
        out.append(finding("R-HEALTH", "R-HEALTH:ram", SELF, "",
                           "laptop free RAM %.1f GB, floor 4 GB" % (free_ram / 1024 ** 3),
                           "Free memory: stop an idle agent or a stray process"))
    if cmdlines is not None:
        for name in BINDERS:
            if not any(name in c for c in cmdlines):
                out.append(finding("R-HEALTH", "R-HEALTH:proc:%s" % name, SELF, "",
                                   "no live process matches %s" % name, "Restart %s" % name))
    return out


def detect_all(rows, now, pr_lists=None, free_ram=None, cmdlines=None):
    return (detect_pr_names(rows, now) + detect_owner_log(rows, now) + detect_estimates(rows, now)
            + detect_acks(rows, now) + detect_threads(rows, now) + detect_prcap(pr_lists, now)
            + detect_health(free_ram, cmdlines, now))


def rule_of(f):
    return f.get("sub") or f["rule"]


def ack_cleared(f, rows, ring_ids):
    """A row-shaped gap whose fix is an acknowledgement is clear once the agent answered the ring row
    (or, for R-PR, posted a correction answering the offending row)."""
    if rule_of(f) not in ACK_CLEARABLE:
        return False
    wanted = set(ring_ids)
    if f["rule"] == "R-PR" and f["row_id"]:
        wanted.add(f["row_id"])
    return any(wanted & r.refs and f["agent"] in r.who for r in rows if not own_ring(r))


def board_rings(rows):
    """{gap key: (ring id, ts)} for this runner's own ring rows already on the board."""
    out = {}
    for r in rows:
        if own_ring(r) and r.phase == "ASK" and r.f.get("gap"):
            out.setdefault(r.f["gap"], (r.id, r.ts))
    return out


# ---- rows this runner writes ----------------------------------------------------------------------

def clean(text):
    return re.sub(r"\s+", " ", str(text).replace("|", "/")).strip()


def bcb(fields, text):
    head = "BCB|" + "|".join("%s=%s" % (k, clean(v)) for k, v in fields if v not in (None, ""))
    room = MAX_ROW_CHARS - len(head) - len("|text=") - 10
    t = clean(text)
    if len(t) > room:
        t = t[:max(0, room - 3)].rstrip() + "..."
    return head + "|text=" + t


def spec(row_id, payload, gist):
    return {"row_id": row_id, "source_tag": SELF, "target_surface": "ALL", "action_type": "APPEND",
            "category": "COMPLIANCE", "project_tag": "Blackboard", "gist": gist[:60],
            "subgist": COMPLIANCE_THREAD, "payload": payload}


def ring_spec(f, now, n):
    rid = "CLAUDE-CLI-COMPLIANCE-RING-%s-%s-%02d" % (rule_of(f), stamp(now), n)
    to = "grok" if f["agent"] == "cursor" else f["agent"]
    who = "cursor (via grok)" if f["agent"] == "cursor" else f["agent"]
    text = "Rule %s gap for %s. Missing: %s. Clear it: %s." % (f["rule"], who, f["missing"], f["action"])
    fields = [("v", "1"), ("id", rid), ("phase", "ASK"), ("class", "COMPLIANCE"), ("from", SELF), ("to", to),
              ("thread_id", COMPLIANCE_THREAD), ("answers", f["row_id"]), ("rule", f["rule"]),
              ("gap", f["key"]), ("via", "direct")]
    return spec(rid, bcb(fields, text), rid)


def summary_spec(extra, now):
    rid = "CLAUDE-CLI-COMPLIANCE-RING-SUMMARY-%s" % stamp(now)
    agents = sorted({"grok" if f["agent"] == "cursor" else f["agent"] for f in extra})
    parts = ["%s %s %s" % (f["rule"], f["agent"], f["row_id"] or f["key"]) for f in extra]
    text = ("%d more compliance gaps this cycle beyond the %d rung one by one. Each line is rule, agent, row: "
            "%s. Clear each one the way its rule says (ACK an ask, log a reply, post ESTIMATE_DONE, add "
            "thread_id, name the repo with the PR)." % (len(extra), MAX_RINGS, "; ".join(parts)))
    fields = [("v", "1"), ("id", rid), ("phase", "ASK"), ("class", "COMPLIANCE"), ("from", SELF),
              ("to", ";".join(agents)), ("thread_id", COMPLIANCE_THREAD), ("rule", "SUMMARY"), ("via", "direct")]
    return spec(rid, bcb(fields, text), rid)


def counts(items):
    c = {}
    for f in items:
        c[f["rule"]] = c.get(f["rule"], 0) + 1
    return c


def heartbeat_spec(open_gaps, rung, escalated, now):
    rid = "CLAUDE-CLI-COMPLIANCE-RING-HEARTBEAT-%s" % stamp(now)
    c = counts(open_gaps)
    per = ", ".join("%s %d" % kv for kv in sorted(c.items())) or "none"
    text = ("Compliance runner heartbeat. Open gaps: %d (%s). Rung this cycle: %d. Escalated this cycle: %d."
            % (len(open_gaps), per, rung, escalated))
    fields = [("v", "1"), ("id", rid), ("phase", "RESULT"), ("class", "COMPLIANCE"), ("from", SELF),
              ("to", "gemini"), ("thread_id", COMPLIANCE_THREAD), ("via", "direct")]
    return spec(rid, bcb(fields, text), rid)


def escalation_text(gaps):
    by_agent = {}
    for g in gaps:
        by_agent.setdefault(g["agent"], []).append(g["rule"])
    agents = "; ".join("%s %d (%s)" % (a, len(r), ", ".join(sorted(set(r)))) for a, r in sorted(by_agent.items()))
    ids = [g.get("ring_id") or g["key"] for g in gaps]
    shown = ", ".join(ids[:3]) + (" +%d more" % (len(ids) - 3) if len(ids) > 3 else "")
    return "\n".join([
        "Compliance: %d gap(s) still open 30+ min after the agent was rung." % len(gaps),
        "By agent: %s." % agents,
        "Rings on TH-COMPLIANCE: %s." % shown,
    ])


# ---- state ----------------------------------------------------------------------------------------

def state_dir():
    d = os.environ.get("COMPLIANCE_STATE_DIR")
    if d:
        return Path(d)
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".claude" / "compliance-runner"


def load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("gaps"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"gaps": {}, "last_heartbeat": None}


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def save_state(path, state):
    write_text(path, json.dumps(state, indent=1, sort_keys=True) + "\n")


def log(msg):
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / "runner.log"
        if p.exists() and p.stat().st_size > 1_000_000:
            os.replace(p, d / "runner.log.1")
        with open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("%s %s\n" % (iso(datetime.now(timezone.utc)), msg))
    except OSError:
        pass


# ---- the cycle ------------------------------------------------------------------------------------

def run_cycle(deps, now, dry_run=False, whatsapp=True, out=print):
    """One cycle. deps: read_rows(), pr_lists(), free_ram(), cmdlines(), post(spec)->status, notify(text)->bool,
    state_path. Returns the findings still open (after acknowledgement checks)."""
    errors = []

    def safe(name, fn, default=None):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one source failing must not stop the others
            errors.append("%s: %s: %s" % (name, type(exc).__name__, exc))
            return default

    raw = deps["read_rows"]()                       # a failed board read fails the cycle (nothing to judge)
    rows = parse_rows(raw)
    findings = detect_all(rows, now, pr_lists=safe("pr_lists", deps["pr_lists"]),
                          free_ram=safe("free_ram", deps["free_ram"]),
                          cmdlines=safe("cmdlines", deps["cmdlines"]))
    for e in errors:
        log("source error: " + e)
        out("SOURCE ERROR " + e)

    state = load_state(deps["state_path"])
    gaps = state["gaps"]
    on_board = board_rings(rows)

    def ring_ids(key):
        ids = [gaps[key]["ring_id"]] if key in gaps and gaps[key].get("ring_id") else []
        if key in on_board:
            ids.append(on_board[key][0])
        return ids

    current = {}
    for f in findings:
        if f["key"] not in current and not ack_cleared(f, rows, ring_ids(f["key"])):
            current[f["key"]] = f
    open_findings = list(current.values())

    cleared = [k for k in gaps if k not in current]
    for k in cleared:
        del gaps[k]

    new = []
    for k, f in current.items():
        g = gaps.get(k)
        if g is None and k in on_board:
            rid, ts = on_board[k]
            gaps[k] = g = {"rule": f["rule"], "agent": f["agent"], "row_id": f["row_id"], "key": k,
                           "first_seen": iso(ts), "rung_at": iso(ts), "ring_id": rid, "cycles_open": 0,
                           "escalated": False}
            continue
        if g is None:
            new.append(f)
        else:
            g["cycles_open"] = int(g.get("cycles_open", 0)) + 1

    c = counts(open_findings)
    out("FINDINGS %d: %s" % (len(open_findings), ", ".join("%s=%d" % kv for kv in sorted(c.items())) or "none"))
    for f in open_findings:
        out("  %-8s %-22s %s %s" % (f["rule"], f["agent"], f["row_id"] or f["key"], f["missing"]))
    out("NEW %d, CLEARED %d" % (len(new), len(cleared)))

    to_ring, extra = new[:MAX_RINGS], new[MAX_RINGS:]
    plan = [(f, ring_spec(f, now, i + 1)) for i, f in enumerate(to_ring)]
    summary = summary_spec(extra, now) if extra else None

    due = [g for k, g in gaps.items()
           if k in current and not g.get("escalated") and int(g.get("cycles_open", 0)) >= ESCALATE_CYCLES
           and (parse_ts(g.get("rung_at")) or now) <= now - ESCALATE_AFTER]

    if dry_run:
        for f, s in plan:
            out("WOULD RING %s to %s" % (s["row_id"], f["agent"]))
        if summary:
            out("WOULD SUMMARIZE %d more in %s" % (len(extra), summary["row_id"]))
        if due:
            out("WOULD ESCALATE %d gap(s) to WhatsApp" % len(due))
        return open_findings

    rung = 0

    def record(f, ring_id):
        gaps[f["key"]] = {"rule": f["rule"], "agent": f["agent"], "row_id": f["row_id"], "key": f["key"],
                          "first_seen": iso(now), "rung_at": iso(now), "ring_id": ring_id, "cycles_open": 0,
                          "escalated": False}

    for f, s in plan:
        status = deps["post"](s)
        log("ring %s %s -> %s" % (s["row_id"], f["key"], status))
        if status in ("ok", "uncertain"):              # uncertain: never post it twice
            record(f, s["row_id"])
            rung += 1
    if summary:
        status = deps["post"](summary)
        log("summary %s (%d gaps) -> %s" % (summary["row_id"], len(extra), status))
        if status in ("ok", "uncertain"):
            for f in extra:
                record(f, summary["row_id"])
            rung += 1

    escalated = 0
    if due:
        if whatsapp:
            sent = deps["notify"](escalation_text(due))
            log("escalate %d gap(s) -> %s" % (len(due), "sent" if sent else "FAILED"))
            if sent:
                for g in due:
                    g["escalated"] = True
                    g["escalated_at"] = iso(now)
                escalated = len(due)
        else:
            log("escalation due for %d gap(s), --no-whatsapp" % len(due))

    last = parse_ts(state.get("last_heartbeat"))
    if last is None or now - last >= HEARTBEAT_EVERY:
        status = deps["post"](heartbeat_spec(open_findings, rung, escalated, now))
        log("heartbeat -> %s" % status)
        if status in ("ok", "uncertain"):
            state["last_heartbeat"] = iso(now)

    state["last_run"] = iso(now)
    save_state(deps["state_path"], state)
    return open_findings


# ---- the real sources -----------------------------------------------------------------------------

def real_read_rows(since_hours):
    sys.path.insert(0, str(HERE))
    import bus  # noqa: E402 - credentials found exactly as bus.py finds them; nothing is read here
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    return bus.read_rows(bus.load_env(), since=iso(since))["rows"]


def real_pr_lists():
    out = {}
    for repo in REPOS:
        try:
            p = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
                                "--json", "number,headRefName"], capture_output=True, text=True,
                               encoding="utf-8", timeout=90)
            out[repo] = json.loads(p.stdout) if p.returncode == 0 else None
            if p.returncode != 0:
                log("gh pr list %s failed: rc=%d" % (repo, p.returncode))
        except Exception as exc:  # noqa: BLE001
            log("gh pr list %s: %s" % (repo, exc))
            out[repo] = None
    return out


def real_free_ram():
    if os.name != "nt":
        return None
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return int(st.ullAvailPhys)


def real_cmdlines():
    if os.name != "nt":
        p = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=60)
        lines = p.stdout.splitlines()
    else:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        if p.returncode != 0:
            raise RuntimeError("process list failed rc=%d" % p.returncode)
        lines = p.stdout.splitlines()
    # This runner's own probe and command line are not the processes being looked for.
    return [l for l in lines if l.strip() and "Win32_Process" not in l and "compliance_runner" not in l]


def real_post(spec_obj):
    spool = state_dir() / "spool"
    path = spool / ("%s.json" % spec_obj["row_id"])
    write_text(path, json.dumps(spec_obj, ensure_ascii=False, indent=1) + "\n")
    try:
        p = subprocess.run([sys.executable, str(HERE / "append.py"), str(path)], cwd=str(HERE),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        return "uncertain"
    text = (p.stdout or "") + (p.stderr or "")
    if "READ-BACK OK" in text:
        return "ok"
    if re.search(r"BCB_|Refusing to append|ROW_ATTRIBUTION|must be a BCB|needs non-empty|No \.env", text):
        log("append refused %s: %s" % (spec_obj["row_id"], text.strip().splitlines()[-1:]))
        return "refused"
    return "uncertain"


def real_notify(text):
    d = state_dir()
    f = d / "escalation.txt"
    write_text(f, text + "\n")
    cmd = [sys.executable, str(HERE / "wa_notify.py"), "--text-file", str(f), "--kind", "ANDON",
           "--tag", SELF]
    env_file = os.environ.get("COMPLIANCE_WA_ENV") or os.environ.get("BLACKBOARD_ENV")
    if env_file:
        cmd += ["--env-file", env_file]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    return p.returncode == 0


def main(argv=None, deps=None, now=None):
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        ap = argparse.ArgumentParser(description="Fleet compliance runner")
        ap.add_argument("--dry-run", action="store_true")
        ap.add_argument("--since-hours", type=float, default=3.0)
        ap.add_argument("--no-whatsapp", action="store_true")
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        deps = deps or {
            "read_rows": lambda: real_read_rows(args.since_hours),
            "pr_lists": real_pr_lists,
            "free_ram": real_free_ram,
            "cmdlines": real_cmdlines,
            "post": real_post,
            "notify": real_notify,
            "state_path": state_dir() / "state.json",
        }
        found = run_cycle(deps, now or datetime.now(timezone.utc), dry_run=args.dry_run,
                          whatsapp=not args.no_whatsapp)
        return 1 if (args.dry_run and found) else 0
    except BaseException as exc:  # noqa: BLE001 - unattended: never raise out of main
        if isinstance(exc, KeyboardInterrupt):
            return 130
        log("cycle failed: %s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc()))
        print("CYCLE FAILED (%s); see runner.log in %s" % (type(exc).__name__, state_dir()))
        return 0


if __name__ == "__main__":
    sys.exit(main())

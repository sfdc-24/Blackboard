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
         R-PR      a PR number in a row without its repo name directly before it ("conference #N",
                   "sfdc-24/Blackboard PR #N"). A bare "#N", "PR N", "PRN", or the second number of
                   "conference #427 and #428" is a gap. The owner's own words are not judged.
         R9        an owner PROMPT row with no PROMPT_RESPONSE/OWNER_REPLY (reply_to= or answers=) within
                   15 min of when the prompt was SAID (said_at=, else the row time); owner-log rows written
                   more than 15 min after said_at (R9-LAG); owner-log rows on a thread that is not
                   TH-<that agent's label>-OWNER-<Toronto YYYYMMDD> (R9-THREAD); owner-log rows with no
                   msg_type (R9-TYPE).
         R10a      an ESTIMATE with no ESTIMATE_DONE for its task by est_done + 15 min. A done row matches
                   by the estimate id it names (answers/reply_to/estimate/parent) or by the leading task id
                   ("c47-chair-x" and "C47" are one task). ESTIMATE_SUPERSEDED closes the ids it names.
         R-ACK     an ASK (phase=ASK; or no phase and msg_type ASK; or neither and an "...-ASK-..." id) with
                   no answers=<ask id> row from the addressee within 15 min. Grok answers for cursor.
         R-THREAD  a row with no thread_id= (thread= alone does not count).
         R-PRCAP   open PRs per repo over the cap (6 until the end of Fri Nov 13 2026 ET, then 3).
         R-HEALTH  free RAM under 4 GB, or a binder interpreter (run_clay, run_cody, run_jemma, clay_wait) gone.
       A rule broken LATE (an answer, reply or done row that came after its 15 min) is a "late" finding: it
       rings once and is never escalated, since nothing is left to fix.
    3. Compares with the state file (%USERPROFILE%/.claude/compliance-runner/state.json). A gap is keyed by
       rule + offending row id (or agent + Toronto day) and rings ONCE: one ASK row on TH-COMPLIANCE, at
       most 8 per cycle (other agents and higher-impact rules first), the rest in one summary row per agent
       carrying gaps=<keys>. A gap is recorded as rung only if its key is in a row that posted; what did not
       fit waits for the next cycle. Its own ring rows on the board also count as "already rung".
    4. A gap still open 2 cycles (30 min) after its ring goes to the owner on WhatsApp once (3 lines, ids
       only); an escalated key is not escalated again for 24 h even if it clears and comes back. A source
       that failed (gh, process list, RAM, an empty board read) clears nothing. With nothing to say it posts
       one heartbeat RESULT per 6 hours.

It never raises out of main: errors go to runner.log in the state directory and the exit code is 0,
because it runs unattended. --dry-run exits 1 when it found gaps, so a person can use it as a check.
One cycle holds a lock file and stops posting after a 10-minute budget; state is saved after every post.

LIMITS, stated so nobody reads more into it
    A ring to claude-code-cli is written BY claude-code-cli, and that session's inbox skips its own rows,
    so a self-gap reaches that session only through the escalation; self-gaps ring last. The R-PR scan is a
    pattern; it can flag a non-PR "#12" in prose. Rows outside the look-back window are not judged at all,
    and an owner prompt that never reached the board cannot be seen from the board. Ring ids carry seconds
    and a -NN suffix (CLAUDE-CLI-COMPLIANCE-RING-<rule>-<UTC YYYYMMDDTHHMMSSZ>-NN) so two in one minute
    cannot collide.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
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
KNOWN_SENDERS = set(FLEET) | {"owner"}
NOT_AGENTS = {"", "-", "all", "fleet", "owner", "everyone", "none"}
COMPLIANCE_THREAD = "TH-COMPLIANCE"
MAX_RINGS = 8
WINDOW = timedelta(minutes=15)
SKEW = timedelta(minutes=1)              # board clocks: an answer stamped a moment before its ask still counts
ESCALATE_CYCLES = 2
ESCALATE_AFTER = timedelta(minutes=30)
ESCALATED_MEMORY = timedelta(hours=24)
ESCALATE_RETRY = timedelta(hours=1)
HEARTBEAT_EVERY = timedelta(hours=6)
CYCLE_BUDGET_S = 600
LOCK_STALE_S = 20 * 60
MAX_ROW_CHARS = 1500
MIN_FREE_RAM = 4 * 1024 ** 3
BINDERS = ("run_clay", "run_cody", "run_jemma", "clay_wait")
REPOS = ("sfdc-24/conference", "sfdc-24/Blackboard", "sfdc-24/sfdc24-site")
# Owner ruling: 6 open PRs per repo until the end of Friday Nov 13 2026 (Toronto), then 3.
PRCAP_WIDE_UNTIL = datetime(2026, 11, 14, 5, 0, tzinfo=timezone.utc)   # 2026-11-14 00:00 EST

PROMPT_TYPES = {"PROMPT", "OWNER_PROMPT"}
REPLY_TYPES = {"PROMPT_RESPONSE", "OWNER_REPLY"}
OWNER_WORD_TYPES = PROMPT_TYPES | {"OWNER_REPLY"}
ACK_CLEARABLE = {"R-PR", "R-THREAD", "R9-THREAD", "R9-TYPE", "R9-LAG"}
LOG_LABEL = {"claude-code-cli": "CLI", "grok": "GROK", "chatgpt-codex-desktop": "CODEX", "gemini": "GEMINI",
             "cursor": "CURSOR"}
# Ring order: what costs the fleet most first. Self-gaps go after every other agent's.
SEVERITY = {"R-HEALTH": 0, "R-PRCAP": 1, "R-ACK": 2, "R9": 3, "R9-LAG": 3, "R10a": 4, "R9-THREAD": 5,
            "R9-TYPE": 5, "R-THREAD": 5, "R-PR": 6}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
    return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso(ts):
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes(delta):
    """Whole minutes, rounded up: 15 min 20 s past is "16 min", never a misleading "15"."""
    return -int(-delta.total_seconds() // 60)


# ---- rows -----------------------------------------------------------------------------------------

KEY = r"[a-z][a-z0-9_]{0,30}"
HYBRID_KEY = re.compile(r"(?:^|(\|)\s*|\s+)(" + KEY + r")=")
ENUM_FIELDS = ("phase", "class", "msg_type")
ENUM_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?=\s|$)")
TAG = re.compile(r"[A-Za-z0-9_.@-]+")


def _tidy(out):
    """Field values from prose must not pass as fields: an enumerated field keeps its first word only when
    that word stands alone ("ASK Owner (...)" -> ASK; "ASK, with a flag" is prose and is dropped); from=
    must be one tag and to= a list of tags."""
    for k in ENUM_FIELDS:
        if k in out:
            m = ENUM_TOKEN.match(out[k])
            if m:
                out[k] = m.group(0)
            else:
                del out[k]
    if "from" in out and not TAG.fullmatch(out["from"]):
        del out["from"]
    if "to" in out:
        parts = [x.strip() for x in re.split(r"[;,]", out["to"])]
        if not all(TAG.fullmatch(x) for x in parts if x):
            del out["to"]
    return out


def parse_payload(payload):
    """The key=value fields of one payload, first value per key.

    Two shapes are live: BCB rows ("BCB|v=1|id=..|text=..", pipe-delimited, no escaping) and rows that
    start "|id=X|" and then separate keys with "| " or with spaces. When a hybrid row mostly uses "| key="
    (three or more, and over twice the space-separated ones) it is read as pipe-separated, so a "key=" inside
    prose is not a field; otherwise a value runs to the next " key=". Either way a value ends at the first "|", and text= takes the rest.
    """
    p = str(payload or "")
    out = {}
    if p.startswith("BCB|"):
        for part in p.split("|")[1:]:
            k, sep, v = part.partition("=")
            k = k.strip()
            if sep and re.fullmatch(KEY, k):
                out.setdefault(k, v.strip())
        return _tidy(out)
    marks = list(HYBRID_KEY.finditer(p))
    piped = [m for m in marks if m.group(1) or m.start() == 0]
    if len(piped) >= 3 and len(piped) > 2 * (len(marks) - len(piped)):
        marks = piped
    for i, m in enumerate(marks):
        k = m.group(2)
        if k == "text":
            out.setdefault(k, p[m.end():].strip().strip("|").strip().strip("'\""))
            break
        end = marks[i + 1].start() if i + 1 < len(marks) else len(p)
        value = p[m.end():end].split("|", 1)[0]
        out.setdefault(k, value.strip().strip("'\""))
    return _tidy(out)


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
        # from= wins only when it names a known sender: grok writes from=<base commit> on some rows.
        frm = canonical(self.f.get("from"))
        self.sender = frm if frm in KNOWN_SENDERS else canonical(self.source)
        self.thread = self.f.get("thread_id", "")
        self.phase = self.f.get("phase", "").upper()
        self.cls = self.f.get("class", "").upper()
        self.msg_type = self.f.get("msg_type", "").upper()
        self.refs = set(split_ids(self.f.get("answers")) + split_ids(self.f.get("reply_to")))
        by = split_ids(self.f.get("by"))[:1]
        self.who = ({canonical(x) for x in [self.source, self.f.get("claimed_author")] + by if x}
                    | ({frm} if frm in KNOWN_SENDERS else set())) - {""}

    def addressees(self):
        raw = self.f.get("to") or self.target
        return {canonical(x) for x in re.split(r"[;,\s]+", raw)} - NOT_AGENTS - {self.sender}


def parse_rows(cells_list):
    rows = [Row(c) for c in cells_list if isinstance(c, (list, tuple)) and c]
    return sorted([r for r in rows if r.ts is not None], key=lambda r: r.ts)


def finding(rule, key, agent, row_id, missing, action, **extra):
    key = re.sub(r"[|;\s]+", "_", key)
    out = {"rule": rule, "key": key, "agent": agent, "row_id": row_id or "", "missing": missing,
           "action": action}
    out.update(extra)
    return out


def own_ring(row):
    return row.sender == SELF and row.cls == "COMPLIANCE"


def owner_words(row):
    """The owner's own words, relayed: a prompt row, an untyped owner-log row, or a row from the owner."""
    if row.msg_type in OWNER_WORD_TYPES or row.sender == "owner" or canonical(row.source) == "owner":
        return True
    return untyped_owner_log(row)


def untyped_owner_log(row):
    to = {canonical(x) for x in re.split(r"[;,\s]+", row.f.get("to") or row.target) if x}
    return row.cls == "LOG" and "owner" in to and not row.msg_type and not row.refs


# ---- detectors (pure) -----------------------------------------------------------------------------

REPO = r"(?:sfdc-24/)?(?:conference|blackboard|sfdc24-site|converspan)\b"
# The repo directly before each number: "conference #428", "conference PR #428", "Blackboard PRs #484",
# "conference: #428". "conference #427 and #428" names the repo for #427 only.
REPO_BEFORE = re.compile(REPO + r"\s*(?:PRs?\s*)?:?\s*$", re.I)
PR_REF = re.compile(r"(?<![A-Za-z0-9_#-])(?:PR\s*#?\s*(\d{2,5})|#(\d{2,5}))(?![A-Za-z0-9_-])", re.I)
QUOTE_LIST_GAP = re.compile(r"[/,]")


def bare_pr_refs(text):
    """The PR numbers in a text without the repo name directly before them. Ids such as X-PR428-Y are not
    references (a hyphen on either side rules a match out), and a quoted number ('#428', or '#427/#428') is
    a mention of the pattern, not a reference to a PR."""
    text = str(text or "")
    found = []
    prev_quoted, prev_end = False, -1
    for m in PR_REF.finditer(text):
        before = text[max(0, m.start() - 48):m.start()]
        quoted = (m.start() > 0 and text[m.start() - 1] in "'\"`") or (
            prev_quoted and QUOTE_LIST_GAP.fullmatch(text[prev_end:m.start()]) is not None)
        if not quoted and REPO_BEFORE.search(before) is None:
            found.append(m.group(1) or m.group(2))
        prev_quoted, prev_end = quoted, m.end()
    return found


def detect_pr_names(rows, now=None):
    out = []
    for r in rows:
        if r.sender not in FLEET or own_ring(r) or owner_words(r):
            continue
        nums = bare_pr_refs(r.payload)
        pr = r.f.get("pr", "")
        if re.fullmatch(r"#?\d{2,5}", pr) and not r.f.get("repo"):
            nums.append(pr.lstrip("#"))
        if nums:
            out.append(finding(
                "R-PR", "R-PR:%s" % r.id, r.sender, r.id,
                "%d PR number(s) without the repo name directly before each" % len(nums),
                "Post a row with msg_type=CORRECTION and answers=%s naming each PR with its repo (conference "
                "#N, Blackboard #N, sfdc24-site #N), or ACK this ring" % r.id))
    return out


def _said(r):
    return parse_ts(r.f.get("said_at")) or r.ts


def is_prompt(r):
    return r.msg_type in PROMPT_TYPES or untyped_owner_log(r)


def detect_owner_log(rows, now):
    out = []
    replies = [r for r in rows if r.msg_type in REPLY_TYPES and not own_ring(r)]
    for p in rows:
        if not is_prompt(p) or p.sender not in FLEET or own_ring(p):
            continue
        said = _said(p)
        matched = [r for r in replies if p.id in r.refs]
        if any(r.ts <= said + WINDOW for r in matched):
            continue
        if matched:
            if p.ts - said > WINDOW:
                continue                                     # the prompt row itself was late: R9-LAG says so
            late = min(r.ts for r in matched) - said
            out.append(finding(
                "R9", "R9:%s" % p.id, p.sender, p.id,
                "reply logged %d min after the prompt was said, rule is 15" % minutes(late),
                "Log each owner reply within 15 min; ACK this ring", historic=True))
            continue
        if now - p.ts < WINDOW:
            continue
        out.append(finding(
            "R9", "R9:%s" % p.id, p.sender, p.id, "no PROMPT_RESPONSE or OWNER_REPLY within 15 min",
            "Post the reply row verbatim with reply_to=%s on the same owner thread" % p.id))
    seen = set()
    for r in rows:
        logged = r.msg_type in PROMPT_TYPES | REPLY_TYPES or untyped_owner_log(r)
        if not logged or r.sender not in FLEET or own_ring(r):
            continue
        day = toronto_day(r.ts)
        label = LOG_LABEL.get(r.sender, r.sender.upper())
        said_at = parse_ts(r.f.get("said_at"))
        if said_at and r.ts - said_at > WINDOW:
            key = "R9-LAG:%s:%s" % (r.sender, day)
            if key not in seen:
                seen.add(key)
                out.append(finding(
                    "R9", key, r.sender, r.id,
                    "owner-log row written %d min after said_at, rule is 15" % minutes(r.ts - said_at),
                    "Log owner prompts and replies within 15 min of when they were said; ACK this ring",
                    sub="R9-LAG", historic=True))
        if untyped_owner_log(r):
            key = "R9-TYPE:%s:%s" % (r.sender, day)
            if key not in seen:
                seen.add(key)
                out.append(finding(
                    "R9", key, r.sender, r.id, "owner-log row has no msg_type (PROMPT or PROMPT_RESPONSE)",
                    "Put msg_type=PROMPT on owner prompt rows and msg_type=PROMPT_RESPONSE on replies; ACK this "
                    "ring", sub="R9-TYPE"))
        thread = r.thread or r.f.get("thread", "")
        said = said_at or r.ts
        days = {toronto_day(said), day}
        m = re.fullmatch(r"TH-([A-Z0-9]+(?:-[A-Z0-9]+)*)-OWNER-(\d{8})", thread)
        if m and m.group(1) == label and m.group(2) in days:
            continue
        key = "R9-THREAD:%s:%s" % (r.sender, day)
        if key in seen:
            continue
        seen.add(key)
        out.append(finding(
            "R9", key, r.sender, r.id, "owner-log thread is not TH-%s-OWNER-<Toronto date>" % label,
            "Log owner prompts and replies on TH-%s-OWNER-%s from now on; ACK this ring" % (label, day),
            sub="R9-THREAD"))
    return out


def _is_estimate_done(r):
    rid = r.id.upper()
    return (r.msg_type == "ESTIMATE_DONE" or r.phase == "ESTIMATE_DONE" or "ESTIMATE-DONE" in rid
            or "ESTIMATE_DONE" in rid or "-ESTDONE-" in rid or rid.startswith("ESTDONE-"))


def _thread(r):
    return r.thread or r.f.get("thread", "")


def _is_estimate(r):
    """An estimate row: typed ESTIMATE, or (untyped) an "...-ESTIMATE-..." id with task=; on TH-ESTIMATES."""
    if _is_estimate_done(r) or _thread(r) != "TH-ESTIMATES":
        return False
    if r.msg_type == "ESTIMATE" or r.phase == "ESTIMATE":
        return True
    if r.msg_type or r.phase:
        return False
    return "-ESTIMATE-" in r.id.upper() and "task" in r.f


def task_key(task):
    """"c47-chair-trailing-name" and "C47" are one task; free text is matched whole, case aside."""
    t = re.sub(r"\s+", " ", str(task or "").strip().lower())
    m = re.match(r"[a-z]+\d+(?:[a-z](?![a-z]))?(?![0-9])", t)
    return m.group(0) if m else t


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
    dones = []
    superseded = set()
    for r in rows:
        if r.msg_type == "ESTIMATE_SUPERSEDED":
            superseded.update(split_ids(r.f.get("supersedes")))
            continue
        if _is_estimate_done(r):
            ids = set(r.refs)
            for k in ("estimate", "parent_estimate", "parent"):
                ids.update(split_ids(r.f.get(k)))
            dones.append((r, ids, task_key(r.f.get("task")) if r.f.get("task") else None))
            continue
        if not _is_estimate(r) or r.sender not in FLEET:
            continue
        task = r.f.get("task", "").strip()
        if not task:
            out.append(finding("R10a", "R10a:%s" % r.id, r.sender, r.id, "ESTIMATE row has no task=",
                               "Re-post the estimate with task= and est_done="))
            continue
        latest[task_key(task)] = r                         # rows are oldest first: a re-estimate wins
    for key, r in latest.items():
        if r.id in superseded:
            continue
        matched = [d for d, ids, k in dones if d.ts >= r.ts - SKEW and (r.id in ids or k == key)]
        due = _deadline(r)
        if matched:
            if due is None:
                continue
            first = min(d.ts for d in matched)
            if first <= due + WINDOW:
                continue
            out.append(finding(
                "R10a", "R10a:%s" % r.id, r.sender, r.id,
                "ESTIMATE_DONE came %d min after est_done, rule is 15" % minutes(first - due),
                "Post ESTIMATE_DONE by est_done + 15 min, or re-estimate before then; ACK this ring",
                historic=True))
            continue
        if due is None:
            out.append(finding("R10a", "R10a:%s" % r.id, r.sender, r.id, "ESTIMATE row has no est_done=",
                               "Re-post the estimate for task=%s with est_done= in UTC" % r.f.get("task")))
            continue
        if now >= due + WINDOW:
            out.append(finding(
                "R10a", "R10a:%s" % r.id, r.sender, r.id, "no ESTIMATE_DONE by est_done + 15 min",
                "Post ESTIMATE_DONE for task=%s on TH-ESTIMATES, or a re-estimate with a new est_done"
                % r.f.get("task")))
    return out


ASK_ID = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+){0,2}-ASK-")


def is_ask(r):
    if r.phase:
        return r.phase == "ASK"
    if r.msg_type:
        return r.msg_type == "ASK"
    return ASK_ID.match(r.id.upper()) is not None


def _answers_for(agent, r):
    return agent in r.who or (agent == "cursor" and "grok" in r.who)


def detect_acks(rows, now):
    out = []
    for a in rows:
        if not is_ask(a) or a.cls == "COMPLIANCE" or own_ring(a) or a.sender not in KNOWN_SENDERS:
            continue
        for agent in sorted(a.addressees()):
            if agent not in FLEET:
                continue
            answers = [r for r in rows if a.id in r.refs and not own_ring(r) and _answers_for(agent, r)
                       and r.ts >= a.ts - SKEW]
            if any(r.ts <= a.ts + WINDOW for r in answers):
                continue
            key = "R-ACK:%s:%s" % (a.id, agent)
            if answers:
                late = min(r.ts for r in answers) - a.ts
                out.append(finding(
                    "R-ACK", key, agent, a.id, "answered %d min after the ask, rule is 15" % minutes(late),
                    "ACK every ask within 15 min (ACK first, work after); ACK this ring", historic=True))
                continue
            if now - a.ts < WINDOW:
                continue
            out.append(finding(
                "R-ACK", key, agent, a.id, "no ACK, RESULT or DONE with answers= this ask within 15 min",
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
            "Put thread_id= on every row from now on; ACK this ring", count=len(bad)))
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


def binder_alive(name, cmdlines):
    """Only a command line that STARTS with a python interpreter running <name>.py counts; a launcher
    shell whose -c string mentions the name does not."""
    exe = r"(?:python[\d.]*w?|pyw?)(?:\.exe)?"
    pat = re.compile(r"^\s*(?:\"(?:[^\"]*[\\/])?%s\"|(?:\S*[\\/])?%s)\s+(?:-\S+\s+)*(?:\"[^\"]*?|\S*?)\b%s\.py\b"
                     % (exe, exe, re.escape(name)), re.I)
    return any(pat.search(c) for c in cmdlines)


def detect_health(free_ram, cmdlines, now=None):
    out = []
    if free_ram is not None and free_ram < MIN_FREE_RAM:
        out.append(finding("R-HEALTH", "R-HEALTH:ram", SELF, "",
                           "laptop free RAM %.1f GB, floor 4 GB" % (free_ram / 1024 ** 3),
                           "Free memory: stop an idle agent or a stray process"))
    if cmdlines is not None:
        for name in BINDERS:
            if not binder_alive(name, cmdlines):
                out.append(finding("R-HEALTH", "R-HEALTH:proc:%s" % name, SELF, "",
                                   "no live python process runs %s.py" % name, "Restart %s" % name))
    return out


def detect_all(rows, now, pr_lists=None, free_ram=None, cmdlines=None):
    return (detect_pr_names(rows, now) + detect_owner_log(rows, now) + detect_estimates(rows, now)
            + detect_acks(rows, now) + detect_threads(rows, now) + detect_prcap(pr_lists, now)
            + detect_health(free_ram, cmdlines, now))


def rule_of(f):
    return f.get("sub") or f["rule"]


def ack_cleared(f, rows, ring_ids):
    """A gap whose fix is an acknowledgement (or a late one, with nothing left to fix) is clear once the
    agent answered its ring row. An R-PR gap also clears with a msg_type=CORRECTION row answering the
    offending row; any other reply to the offending row does not."""
    clearable = rule_of(f) in ACK_CLEARABLE or f.get("historic")
    if not clearable:
        return False
    wanted = set(ring_ids)
    for r in rows:
        if own_ring(r) or not _answers_for(f["agent"], r):
            continue
        if wanted & r.refs:
            return True
        if f["rule"] == "R-PR" and f["row_id"] and r.msg_type == "CORRECTION" and f["row_id"] in r.refs:
            return True
    return False


def board_rings(rows):
    """{gap key: (ring id, ts)} for this runner's own ring rows already on the board (gap= or gaps=)."""
    out = {}
    for r in rows:
        if own_ring(r) and r.phase == "ASK":
            for k in split_ids(r.f.get("gap")) + split_ids(r.f.get("gaps")):
                out.setdefault(k, (r.id, r.ts))
    return out


# ---- rows this runner writes ----------------------------------------------------------------------

def clean(text):
    return re.sub(r"\s+", " ", str(text).replace("|", "/")).strip()


def _head(fields):
    return "BCB|" + "|".join("%s=%s" % (k, clean(v)) for k, v in fields if v not in (None, ""))


def bcb(fields, text):
    head = _head(fields)
    room = MAX_ROW_CHARS - len(head) - len("|text=") - 10
    t = clean(text)
    if len(t) > room:
        t = t[:max(0, room - 3)].rstrip() + "..."
    return head + "|text=" + t


def spec(row_id, payload, gist):
    return {"row_id": row_id, "source_tag": SELF, "target_surface": "ALL", "action_type": "APPEND",
            "category": "COMPLIANCE", "project_tag": "Blackboard", "gist": gist[:60],
            "subgist": COMPLIANCE_THREAD, "payload": payload}


def ring_to(agent):
    return "grok" if agent == "cursor" else agent


def ring_spec(f, now, n):
    rid = "CLAUDE-CLI-COMPLIANCE-RING-%s-%s-%02d" % (rule_of(f), stamp(now), n)
    who = "cursor (via grok)" if f["agent"] == "cursor" else f["agent"]
    text = "Rule %s gap for %s. Missing: %s. Clear it: %s." % (f["rule"], who, f["missing"], f["action"])
    # gap_row=, never answers=: a ring must not read as the addressee's answer to the row it is about.
    fields = [("v", "1"), ("id", rid), ("phase", "ASK"), ("class", "COMPLIANCE"), ("from", SELF),
              ("to", ring_to(f["agent"])), ("thread_id", COMPLIANCE_THREAD), ("gap_row", f["row_id"]),
              ("rule", f["rule"]), ("gap", f["key"]), ("via", "direct")]
    return spec(rid, bcb(fields, text), rid)


def summary_spec(agent, items, now):
    """One summary row for one addressee, built from whole entries until the next would pass the cap.
    Returns (spec, the items it carries); the rest wait for the next cycle."""
    rid = "CLAUDE-CLI-COMPLIANCE-RING-SUMMARY-%s-%s" % (LOG_LABEL.get(agent, agent.upper()), stamp(now))
    taken, best = [], None
    for f in items:
        trial = taken + [f]
        parts = ["%s %s" % (rule_of(x), x["row_id"] or x["key"]) for x in trial]
        text = ("%d more compliance gaps for %s this cycle, rule then row: %s. Clear each the way its rule says "
                "(ACK an ask, log a reply, post ESTIMATE_DONE, add thread_id, name the repo before each PR "
                "number), then ACK this row." % (len(trial), agent, "; ".join(parts)))
        fields = [("v", "1"), ("id", rid), ("phase", "ASK"), ("class", "COMPLIANCE"), ("from", SELF),
                  ("to", agent), ("thread_id", COMPLIANCE_THREAD), ("rule", "SUMMARY"),
                  ("gaps", ";".join(x["key"] for x in trial)), ("via", "direct")]
        payload = _head(fields) + "|text=" + clean(text)
        if len(payload) > MAX_ROW_CHARS:
            break
        taken, best = trial, payload
    if not taken:
        return None, []
    return spec(rid, best, rid), taken


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
    ids = list(dict.fromkeys(g.get("ring_id") or g["key"] for g in gaps))
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
            if not isinstance(data.get("escalated_keys"), dict):
                data["escalated_keys"] = {}
            return data
    except (OSError, ValueError):
        pass
    return {"gaps": {}, "last_heartbeat": None, "escalated_keys": {}}


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


class Busy(Exception):
    pass


def take_lock(path, now_s=None):
    """An exclusive lock file; one older than 20 minutes is a dead run's and is taken over."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return path
        except FileExistsError:
            try:
                age = (now_s if now_s is not None else time.time()) - path.stat().st_mtime
            except OSError:
                continue
            if age < LOCK_STALE_S:
                raise Busy("another cycle holds %s (%d s old)" % (path, age))
            try:
                os.remove(str(path))
            except OSError:
                pass
    raise Busy("could not take %s" % path)


def drop_lock(path):
    try:
        os.remove(str(path))
    except OSError:
        pass


# ---- the cycle ------------------------------------------------------------------------------------

def _source_unknown(key, rows, pr_lists, free_ram, cmdlines):
    """True when this cycle cannot tell whether the gap behind key is still open."""
    if not rows:
        return True
    if key.startswith("R-PRCAP:"):
        return pr_lists is None or (pr_lists or {}).get(key[len("R-PRCAP:"):]) is None
    if key == "R-HEALTH:ram":
        return free_ram is None
    if key.startswith("R-HEALTH:proc:"):
        return cmdlines is None
    return False


def run_cycle(deps, now, dry_run=False, whatsapp=True, out=print):
    """One cycle. deps: read_rows(), pr_lists(), free_ram(), cmdlines(), post(spec)->status,
    notify(text)->"sent"|"failed"|"uncertain" (True/False also accepted), state_path, optional clock()
    in seconds. Returns the findings still open (after acknowledgement checks)."""
    errors = []
    clock = deps.get("clock") or time.monotonic
    started = clock()

    def safe(name, fn, default=None):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one source failing must not stop the others
            errors.append("%s: %s: %s" % (name, type(exc).__name__, exc))
            return default

    raw = deps["read_rows"]()                       # a failed board read fails the cycle (nothing to judge)
    rows = parse_rows(raw)
    pr_lists = safe("pr_lists", deps["pr_lists"])
    free_ram = safe("free_ram", deps["free_ram"])
    cmdlines = safe("cmdlines", deps["cmdlines"])
    findings = detect_all(rows, now, pr_lists=pr_lists, free_ram=free_ram, cmdlines=cmdlines)
    for e in errors:
        log("source error: " + e)
        out("SOURCE ERROR " + e)

    state_path = deps["state_path"]
    state = load_state(state_path)
    gaps = state["gaps"]
    esc_keys = state["escalated_keys"]
    for k in [k for k, v in esc_keys.items() if (parse_ts(v) or now) <= now - ESCALATED_MEMORY]:
        del esc_keys[k]
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

    cleared = [k for k in gaps if k not in current and not _source_unknown(k, rows, pr_lists, free_ram, cmdlines)]
    if not dry_run:
        for k in cleared:
            del gaps[k]

    new = []
    for k, f in current.items():
        g = gaps.get(k)
        if g is None and k in on_board:
            rid, ts = on_board[k]
            if not dry_run:
                gaps[k] = {"rule": f["rule"], "agent": f["agent"], "row_id": f["row_id"], "key": k,
                           "first_seen": iso(ts), "rung_at": iso(ts), "ring_id": rid, "cycles_open": 0,
                           "escalated": k in esc_keys, "historic": bool(f.get("historic"))}
            continue
        if g is None:
            new.append(f)
        elif not dry_run:
            g["cycles_open"] = int(g.get("cycles_open", 0)) + 1
            g["historic"] = bool(f.get("historic"))
    new.sort(key=lambda f: (f["agent"] == SELF, SEVERITY.get(rule_of(f), 9), f["key"]))

    c = counts(open_findings)
    out("FINDINGS %d: %s" % (len(open_findings), ", ".join("%s=%d" % kv for kv in sorted(c.items())) or "none"))
    for f in open_findings:
        out("  %-8s %-22s %s %s%s" % (rule_of(f), f["agent"], f["row_id"] or f["key"], f["missing"],
                                      " (late)" if f.get("historic") else ""))
    out("NEW %d, CLEARED %d" % (len(new), len(cleared)))

    to_ring, extra = new[:MAX_RINGS], new[MAX_RINGS:]
    plan = [(f, ring_spec(f, now, i + 1)) for i, f in enumerate(to_ring)]
    by_agent = {}
    for f in extra:
        by_agent.setdefault(ring_to(f["agent"]), []).append(f)
    summaries = []
    for agent in sorted(by_agent, key=lambda a: (a == SELF, a)):
        s, carried = summary_spec(agent, by_agent[agent], now)
        if s:
            summaries.append((s, carried))

    def escalation_due():
        return [g for k, g in gaps.items()
                if k in current and not current[k].get("historic") and not g.get("escalated")
                and k not in esc_keys and int(g.get("cycles_open", 0)) >= ESCALATE_CYCLES
                and (parse_ts(g.get("rung_at")) or now) <= now - ESCALATE_AFTER
                and (parse_ts(g.get("escalate_failed_at")) or now - ESCALATE_RETRY) <= now - ESCALATE_RETRY]

    if dry_run:
        for f, s in plan:
            out("WOULD RING %s to %s" % (s["row_id"], ring_to(f["agent"])))
        for s, carried in summaries:
            out("WOULD SUMMARIZE %d more in %s" % (len(carried), s["row_id"]))
        left = len(extra) - sum(len(c) for _, c in summaries)
        if left:
            out("WOULD LEAVE %d for the next cycle" % left)
        due = escalation_due()
        if due:
            out("WOULD ESCALATE %d gap(s) to WhatsApp" % len(due))
        return open_findings

    rung = 0

    def record(f, ring_id):
        gaps[f["key"]] = {"rule": f["rule"], "agent": f["agent"], "row_id": f["row_id"], "key": f["key"],
                          "first_seen": iso(now), "rung_at": iso(now), "ring_id": ring_id, "cycles_open": 0,
                          "escalated": f["key"] in esc_keys, "historic": bool(f.get("historic"))}

    def over_budget():
        if clock() - started > CYCLE_BUDGET_S:
            log("cycle budget spent; the rest waits for the next cycle")
            return True
        return False

    save_state(state_path, state)                   # clears and cycle counts land even if a post hangs
    for f, s in plan:
        if over_budget():
            break
        status = deps["post"](s)
        log("ring %s %s -> %s" % (s["row_id"], f["key"], status))
        if status in ("ok", "uncertain"):              # uncertain: never post it twice
            record(f, s["row_id"])
            rung += 1
            save_state(state_path, state)
    for s, carried in summaries:
        if over_budget():
            break
        status = deps["post"](s)
        log("summary %s (%d gaps) -> %s" % (s["row_id"], len(carried), status))
        if status in ("ok", "uncertain"):
            for f in carried:
                record(f, s["row_id"])
            rung += 1
            save_state(state_path, state)

    escalated = 0
    due = escalation_due()
    if due:
        if whatsapp and not over_budget():
            try:
                result = deps["notify"](escalation_text(due))
            except Exception as exc:  # noqa: BLE001 - a timeout may still have sent it: never send twice
                log("escalate notify raised %s: %s" % (type(exc).__name__, exc))
                result = "uncertain"
            if result is True:
                result = "sent"
            elif result is False or result is None:
                result = "failed"
            log("escalate %d gap(s) -> %s" % (len(due), result))
            for g in due:
                if result in ("sent", "uncertain"):
                    g["escalated"] = True
                    g["escalated_at"] = iso(now)
                    if result == "uncertain":
                        g["escalation_uncertain"] = True
                    esc_keys[g["key"]] = iso(now)
                else:
                    g["escalate_failed_at"] = iso(now)
            if result in ("sent", "uncertain"):
                escalated = len(due)
            save_state(state_path, state)
        elif not whatsapp:
            log("escalation due for %d gap(s), --no-whatsapp" % len(due))

    last = parse_ts(state.get("last_heartbeat"))
    if (last is None or now - last >= HEARTBEAT_EVERY) and not over_budget():
        status = deps["post"](heartbeat_spec(open_findings, rung, escalated, now))
        log("heartbeat -> %s" % status)
        if status in ("ok", "uncertain"):
            state["last_heartbeat"] = iso(now)

    state["last_run"] = iso(now)
    save_state(state_path, state)
    return open_findings


# ---- the real sources -----------------------------------------------------------------------------

def child_python():
    """The console interpreter for child scripts: under pythonw.exe, python.exe beside it (with no window)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.exists():
            return str(alt)
    return str(exe)


def run_hidden(cmd, **kw):
    return subprocess.run(cmd, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL, **kw)


def real_read_rows(since_hours):
    sys.path.insert(0, str(HERE))
    import bus  # noqa: E402 - credentials found exactly as bus.py finds them; nothing is read here
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    return bus.read_rows(bus.load_env(), since=iso(since))["rows"]


def real_pr_lists():
    out = {}
    for repo in REPOS:
        try:
            p = run_hidden(["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
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
        p = run_hidden(["ps", "-eo", "args"], capture_output=True, text=True, timeout=60)
        lines = p.stdout.splitlines()
    else:
        p = run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command",
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
        p = run_hidden([child_python(), str(HERE / "append.py"), str(path)], cwd=str(HERE),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
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
    """sent (rc 0), failed (an HTTP 4xx: Graph refused it, nothing went out), else uncertain."""
    d = state_dir()
    f = d / "escalation.txt"
    write_text(f, text + "\n")
    cmd = [child_python(), str(HERE / "wa_notify.py"), "--text-file", str(f), "--kind", "ANDON",
           "--tag", SELF]
    env_file = os.environ.get("COMPLIANCE_WA_ENV") or os.environ.get("BLACKBOARD_ENV")
    if env_file:
        cmd += ["--env-file", env_file]
    try:
        p = run_hidden(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return "uncertain"
    if p.returncode == 0:
        return "sent"
    if re.search(r"HTTP 4\d\d\b", (p.stdout or "") + (p.stderr or "")) or p.returncode == 2:
        return "failed"
    return "uncertain"


def main(argv=None, deps=None, now=None):
    try:
        if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        ap = argparse.ArgumentParser(description="Fleet compliance runner")
        ap.add_argument("--dry-run", action="store_true")
        ap.add_argument("--since-hours", type=float, default=3.0)
        ap.add_argument("--no-whatsapp", action="store_true")
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    lock = None
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
        if not args.dry_run:
            lock = take_lock(Path(deps["state_path"]).with_name("lock"))
        found = run_cycle(deps, now or datetime.now(timezone.utc), dry_run=args.dry_run,
                          whatsapp=not args.no_whatsapp)
        return 1 if (args.dry_run and found) else 0
    except Busy as exc:
        log("skipped: %s" % exc)
        print("SKIPPED: %s" % exc)
        return 0
    except BaseException as exc:  # noqa: BLE001 - unattended: never raise out of main
        if isinstance(exc, KeyboardInterrupt):
            return 130
        log("cycle failed: %s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc()))
        print("CYCLE FAILED (%s); see runner.log in %s" % (type(exc).__name__, state_dir()))
        return 0
    finally:
        if lock is not None:
            drop_lock(lock)


if __name__ == "__main__":
    sys.exit(main())

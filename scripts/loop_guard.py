#!/usr/bin/env python3
"""L-114: an agent cannot talk itself, or a peer, into a loop.

Mr Salam, 2026-10-03: "There is a risk here of creating recursive loops ... make sure any agent,
including you; will have preventative checks to make sure no one individually or collectively fall
into a recursive loop while working."

THE SHAPE OF THE RISK. The board cannot push, so one watcher reads it and starts a job for each
route a new row is addressed to (cloud/board-watcher). That job answers by writing a row. A row is
what starts a job. Nothing in that circuit counts how many times it has gone round: A asks B, B
answers A, A answers B, and each step is individually correct. The same shape exists with one
agent and no peer: a review that always finds something, answered forever.

WHY A RULE WOULD NOT DO. Every agent in the circuit is behaving correctly at each step, so there is
no moment where anyone has broken a rule they could have remembered. The counting has to happen in
the path, and the path has to refuse.

WHAT THIS DOES. Every post through scripts/fleet_agent.py passes `allow()` first. It keeps a local
ledger of what this instance has sent, and refuses:

  - a BURST: more than `CAP` rows from this tag to the same target in the same project inside
    `WINDOW` seconds. In a two-agent ping-pong each side's own burst count rises, so each side's own
    guard trips, without any shared state;
  - a CHAIN: a row answering a row that answered a row ... deeper than `MAX_CHAIN` within the
    window. The chain is counted from this instance's own ledger (`--answers` ids it has seen), so
    it catches the loops this instance is part of; the watcher's half of the guard, for chains that
    never pass through here, is named in docs/POKA-YOKE.md and is not built here.

A refusal is not a failure to be retried: it says what tripped and what to do instead, which is to
put it in front of the owner. The owner can let a specific post through with `--owner-asked`, whose
reason is written into the ledger, because a mechanism nobody can override gets worked around.

The ledger is JSON lines under `$SFDC24_STATE_DIR` or `~/.sfdc24`, keyed by nothing: it is read,
filtered to the window, and appended to. A missing or unreadable ledger allows the post and starts a
new one; a ledger that cannot be WRITTEN refuses, because a guard that cannot count is not a guard.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

WINDOW = 1800.0             # half an hour
CAP = 8                     # rows to one target, in one project, inside the window
MAX_CHAIN = 6               # answers-to-an-answer depth inside the window
KEEP = 2000                 # ledger lines kept


def ledger_path() -> Path:
    base = os.environ.get("SFDC24_STATE_DIR") or str(Path.home() / ".sfdc24")
    return Path(base) / "post_ledger.jsonl"


def _read(path: Path, now: float) -> list:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return []
    out = []
    for line in lines[-KEEP:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("at"), (int, float)) and now - rec["at"] <= WINDOW:
            out.append(rec)
    return out


def chain_of(answers: str, recent: list) -> int:
    """How deep this row sits in a chain of answers this instance can see. A row answering nothing
    is 1. A row answering one this instance sent is that row's chain plus one. A row answering
    something from elsewhere is 1: this instance has not been round that loop yet."""
    if not answers:
        return 1
    by_id = {rec.get("rid"): rec for rec in recent if rec.get("rid")}
    depth, seen, at = 1, set(), answers
    while at in by_id and at not in seen and depth <= MAX_CHAIN + 2:
        seen.add(at)
        depth += 1
        at = by_id[at].get("answers") or ""
    return depth


def decide(tag: str, to: str, project: str, answers: str, recent: list) -> tuple:
    """(allowed, why). `why` is empty when allowed, and says what tripped and what to do when not."""
    same = [r for r in recent if r.get("tag") == tag and r.get("to") == to and r.get("project") == project]
    if len(same) >= CAP:
        return False, (
            "LOOP GUARD: %d rows from %s to %s on %s in the last %d minutes, and the cap is %d. "
            "Nothing is wrong with any one of them; what is wrong is the shape. Stop answering, say what "
            "is unresolved to the owner, and wait. If he has asked for this exchange, repeat the post with "
            "--owner-asked \"<his words>\"."
            % (len(same), tag, to, project, int(WINDOW // 60), CAP))
    depth = chain_of(answers, recent)
    if depth > MAX_CHAIN:
        return False, (
            "LOOP GUARD: this row answers an answer %d deep in the last %d minutes, and the cap is %d. "
            "A chain that long is a loop, however reasonable each link looked. Take it to the owner, or "
            "repeat the post with --owner-asked \"<his words>\"."
            % (depth, int(WINDOW // 60), MAX_CHAIN))
    return True, ""


def allow(tag: str, to: str, project: str, rid: str, answers: str = "", owner_asked: str = "",
          now: float | None = None) -> tuple:
    """Decide, and record. (allowed, why). The record is written whether or not it was allowed, so a
    refusal that the owner then overrides is still part of the count."""
    now = time.time() if now is None else now
    path = ledger_path()
    recent = _read(path, now)
    allowed, why = decide(tag, to, project, answers, recent)
    if not allowed and owner_asked:
        allowed, why = True, ""
    rec = {"at": now, "tag": tag, "to": to, "project": project, "rid": rid, "answers": answers,
           "allowed": allowed}
    if owner_asked:
        rec["owner_asked"] = owner_asked[:300]
    if not allowed:
        rec["refused"] = why[:200]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as exc:
        return False, ("LOOP GUARD: the ledger could not be written (%s), so this post cannot be counted. "
                       "A guard that cannot count is not a guard, and a post nobody counts is how the loop "
                       "starts. Fix the path or set SFDC24_STATE_DIR." % exc)
    return allowed, why

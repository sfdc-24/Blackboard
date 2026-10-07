#!/usr/bin/env python3
"""L-115: an agent cannot talk itself, or a peer, into a loop.

The contract this serves is L-114, `docs/AGENT-LOOP-PREVENTION.md` (Codex, PR #318). This is the one
narrow mechanism, in the path every LOCAL agent posts through. It is not the whole contract: the
lineage, the aggregate fan-out budgets, the terminal and ACK suppression, the no-progress detection,
the effect dedupe and the scoped breaker are L-114's, and the watcher and the cloud wakers post by
their own paths.

Mr Salam, 2026-10-03: "There is a risk here of creating recursive loops ... make sure any agent,
including you; will have preventative checks to make sure no one individually or collectively fall
into a recursive loop while working."

THE SHAPE OF THE RISK. The board cannot push, so one watcher reads it and starts a job for each
route a new row is addressed to (`cloud/board-watcher`). That job answers by writing a row. A row is
what starts a job. Nothing in that circuit counts how many times it has gone round: A asks B, B
answers A, A answers B, and each step is individually correct. The same shape exists with one agent
and no peer: a review that always finds something, answered forever.

WHY A RULE WOULD NOT DO. Every agent in the circuit is behaving correctly at each step, so there is
no moment where anyone has broken a rule they could have remembered. The counting has to happen in
the path, and the path has to refuse.

WHAT THIS DOES. Every post through `scripts/fleet_agent.py` passes `allow()` first. It refuses:

  - a BURST: more than `CAP` rows from this tag to the same target in the same project inside
    `WINDOW` seconds. In a two-agent ping-pong each side's own burst count rises, so each side's own
    guard trips, without any shared state;
  - a CHAIN: a row answering a row that answered a row ... deeper than `MAX_CHAIN` within the
    window, counted from this instance's own ledger of `--answers` ids.

ORDER MATTERS, AND IT IS WRITE THEN DECIDE. The intent is appended to the ledger BEFORE the decision
is taken, and the decision counts what is in the file. Two processes racing therefore each see the
other's intent and both refuse, instead of each reading an under-cap history and both going ahead
(Codex on #317 294bc100: the cap was not atomic). One short line appended is the only atomicity this
needs; there is no lock, and a lock is what a loop guard must not depend on.

A LEDGER THAT CANNOT BE DECODED IS QUARANTINED, NOT IGNORED. One invalid byte used to make every
later read return an empty history while the guard went on appending to the same file, which
disabled it permanently and silently (Codex on #317). Now the unreadable file is renamed aside, this
intent is written again to a fresh one, and the guard keeps counting from there.

A refusal is not a failure to be retried: it says what tripped and what to do instead, which is to
put it in front of the owner. `--owner-asked` lets a specific post through with his words written
into the ledger, because a mechanism nobody can override gets worked around.

The ledger lives under `$SFDC24_STATE_DIR` or `~/.sfdc24`. A ledger that cannot be WRITTEN refuses
the post, because a guard that cannot count is not a guard.
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


def quarantine(path: Path, now: float) -> Path | None:
    """Move a ledger that cannot be decoded aside, keeping it. Where it went, or None."""
    aside = path.with_name("%s.corrupt-%d.jsonl" % (path.stem, int(now)))
    try:
        path.replace(aside)
        return aside
    except OSError:
        return None


def _read(path: Path, now: float) -> tuple:
    """(the intents inside the window, where a corrupt ledger was moved to or None).

    A line that is not JSON is skipped: one bad line is not a bad ledger. A FILE that is not UTF-8
    cannot be read at all, and that one is moved aside so the next write starts a ledger that counts.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except (UnicodeDecodeError, UnicodeError):
        return [], quarantine(path, now)
    except OSError:
        return [], None
    out = []
    for line in text.splitlines()[-KEEP:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (isinstance(rec, dict) and rec.get("kind") == "intent"
                and isinstance(rec.get("at"), (int, float)) and now - rec["at"] <= WINDOW):
            out.append(rec)
    return out, None


def _append(path: Path, rec: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


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


def decide(tag: str, to: str, project: str, answers: str, recent: list, rid: str = "") -> tuple:
    """(allowed, why). `recent` holds this post's own intent too, which `rid` names so it is not
    counted against itself. `why` is empty when allowed."""
    mine_out = [r for r in recent if r.get("rid") != rid]
    same = [r for r in mine_out if r.get("tag") == tag and r.get("to") == to and r.get("project") == project]
    if len(same) >= CAP:
        return False, (
            "LOOP GUARD: %d rows from %s to %s on %s in the last %d minutes, and the cap is %d. "
            "Nothing is wrong with any one of them; what is wrong is the shape. Stop answering, say what "
            "is unresolved to the owner, and wait. If he has asked for this exchange, repeat the post with "
            "--owner-asked \"<his words>\"."
            % (len(same), tag, to, project, int(WINDOW // 60), CAP))
    depth = chain_of(answers, mine_out)
    if depth > MAX_CHAIN:
        return False, (
            "LOOP GUARD: this row answers an answer %d deep in the last %d minutes, and the cap is %d. "
            "A chain that long is a loop, however reasonable each link looked. Take it to the owner, or "
            "repeat the post with --owner-asked \"<his words>\"."
            % (depth, int(WINDOW // 60), MAX_CHAIN))
    return True, ""


def allow(tag: str, to: str, project: str, rid: str, answers: str = "", owner_asked: str = "",
          now: float | None = None) -> tuple:
    """Decide, having recorded first. (allowed, why)."""
    now = time.time() if now is None else now
    path = ledger_path()
    intent = {"kind": "intent", "at": now, "tag": tag, "to": to, "project": project, "rid": rid,
              "answers": answers}
    if owner_asked:
        intent["owner_asked"] = owner_asked[:300]
    if not _append(path, intent):
        return False, ("LOOP GUARD: the ledger could not be written, so this post cannot be counted. A "
                       "guard that cannot count is not a guard, and a post nobody counts is how the loop "
                       "starts. Fix the path or set SFDC24_STATE_DIR.")
    recent, moved = _read(path, now)
    if moved is not None and not _append(path, intent):      # the corrupt file took this intent with it
        return False, ("LOOP GUARD: the ledger at %s could not be decoded and the replacement could not "
                       "be written. Fix the path or set SFDC24_STATE_DIR." % moved)
    allowed, why = decide(tag, to, project, answers, recent, rid)
    if not allowed and owner_asked:
        allowed, why = True, ""
    outcome = {"kind": "decision", "at": now, "rid": rid, "allowed": allowed}
    if moved is not None:
        outcome["quarantined"] = str(moved)
    if not allowed:
        outcome["refused"] = why[:200]
    _append(path, outcome)
    return allowed, why

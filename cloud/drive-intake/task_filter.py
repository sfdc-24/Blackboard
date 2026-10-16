#!/usr/bin/env python3
"""Which docs in the owner's folder are TASKS. Everything else is a NOTE, and a note is never dispatched.

THE OWNER'S SPEC (Abdus, 2026-10-16 08:28 ET), fail-closed: a doc is dispatched as a task only if it
passes ALL five gates. Ambiguous means NOTE.

  G1  Title prefix allowlist, exact and case-sensitive. 'LOU CALL GO' is a rehearsal_call, 'TASK:' is a
      general task. Anything else ('Claude ->', 'GROK ->', 'CONVERSPAN', drafts, RESULT, PLAN, HOLD,
      CONSUMED, STALE ...) is a note. Extending the list is a code change with review, never an env var,
      so a deploy cannot loosen it.
  G2  The body (the request half, above RECEIPTS) has exactly ONE standalone JSON action line - a line
      that is nothing but a JSON object with an "action" key that matches the prefix - and a
      'TASK-OK: YYYY-MM-DD' line naming today's date in America/Toronto. Two action lines, a malformed
      one, a missing or stale TASK-OK: note.
  G3  The doc's createdTime is within FRESH_SECONDS (10 minutes) of now when it is first admitted. A
      later pass finishing a delivery already claimed is not re-judged on age (main.decide does that).
  G4  Idempotency: a doc already consumed (the processed-id set) is never admitted again, and a
      dispatched doc is renamed 'CONSUMED <task id> <timestamp>' (main.decide, after DISPATCHED).
  G5  An action that dials or spends needs a 'CONFIRM: <token>' line whose token matches the deploy's
      DRIVE_INTAKE_CONFIRM_TOKEN (constant-time compare; unset means no token ever matches). Without it
      the task still goes out, as mode=dry_run: validate only, no dial, no spend. rehearsal_call also
      needs callee 'abdus' from the allowlist (never a raw number) and, when live, the rate limits:
      one per 180 s and ten per Toronto day, reserved atomically before the claim.

This module is pure: no socket, no clock of its own, no store. main.py hands it the doc, the time and
the token, and owns the store calls.
"""
from __future__ import annotations

import hmac
import json
import re
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    TORONTO = ZoneInfo("America/Toronto")
except Exception:  # noqa: BLE001 - a missing tz database must not fall open
    TORONTO = None

TASK = "task"
NOTE = "note"
LIVE = "live"
DRY_RUN = "dry_run"

FRESH_SECONDS = 600          # G3: ten minutes
MIN_GAP_SECONDS = 180        # G5: one live dial per 180 s
DAY_MAX = 10                 # G5: ten live dials per Toronto day

# G1. prefix -> (action the JSON line must name, whether it dials or spends, the keys the JSON may carry)
# Order matters only for readability; prefixes are matched exactly at the start of the title.
PREFIXES = {
    # rename=False: the dialer for a LOU CALL GO is Grok's call watcher on its box, which finds the doc BY
    # TITLE and renames it CONSUMED itself after the call. If this service renamed it first (its poll is
    # every 2 minutes, the watcher's every 5), the call would never be placed. The processed-id set is
    # still this service's G4 guard for the doc.
    "LOU CALL GO": {"action": "rehearsal_call", "costly": True, "rename": False,
                    "keys": frozenset(("action", "callee", "caller"))},
    # A general task goes to the API worker, which spends when its switch is on: costly.
    "TASK:": {"action": "task", "costly": True, "rename": True, "keys": None},
}
# G5: the only callees a rehearsal_call may name. Never a phone number.
CALLEES = frozenset(("abdus",))
CALLER_OK = re.compile(r"[a-z0-9_-]{1,32}\Z")

TASK_OK_LINE = re.compile(r"^TASK-OK:\s*(\d{4}-\d{2}-\d{2})\s*$")
CONFIRM_LINE = re.compile(r"^CONFIRM:\s*(\S+)\s*$")


def toronto_day(epoch: float) -> str:
    if TORONTO is None:
        raise RuntimeError("no America/Toronto zone; refusing to guess the owner's day")
    return datetime.fromtimestamp(epoch, TORONTO).strftime("%Y-%m-%d")


def epoch_of(rfc3339) -> float | None:
    if not rfc3339 or not isinstance(rfc3339, str):
        return None
    try:
        return datetime.fromisoformat(rfc3339.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def note(reason: str, **extra) -> dict:
    out = {"kind": NOTE, "reason": reason}
    out.update(extra)
    return out


def prefix_of(title) -> str | None:
    """G1: the allowlisted prefix the title starts with, exactly and case-sensitively, or None."""
    if not isinstance(title, str):
        return None
    for prefix in PREFIXES:
        if title.startswith(prefix):
            return prefix
    return None


def action_lines(request_text: str) -> list:
    """Every line that is nothing but a JSON object with an "action" key. A line that LOOKS like one
    ({...} mentioning action) but does not parse is returned as None, so the caller can call it ambiguous."""
    found = []
    for raw in (request_text or "").splitlines():
        line = raw.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            if '"action"' in line:
                found.append(None)
            continue
        if isinstance(value, dict) and "action" in value:
            found.append(value)
    return found


def _lines(request_text: str, pattern) -> list:
    return [m.group(1) for m in (pattern.match(l.strip()) for l in (request_text or "").splitlines()) if m]


def confirm_ok(given, expected: str) -> bool:
    """Constant-time; an unset expected token matches nothing."""
    if not expected or not given:
        return False
    return hmac.compare_digest(str(given).encode("utf-8"), str(expected).encode("utf-8"))


def classify(doc: dict, request_text: str, now: float, confirm_token: str = "", fresh: bool = True) -> dict:
    """G1, G2, G3 (when fresh=True) and the CONFIRM half of G5. Rate limits and G4 need the store: main.py.

    Returns {"kind": "note", "reason": ...} or
            {"kind": "task", "prefix", "action", "costly", "mode": live|dry_run, "args": {...}}.
    """
    if not isinstance(doc, dict):
        return note("no_doc")
    prefix = prefix_of(doc.get("title"))
    if prefix is None:
        return note("title_not_allowlisted")
    rule = PREFIXES[prefix]

    actions = action_lines(request_text)
    if len(actions) != 1 or actions[0] is None:
        return note("action_line_count" if all(a is not None for a in actions) else "action_line_unparsed",
                    count=len(actions))
    args = actions[0]
    if args.get("action") != rule["action"]:
        return note("action_mismatch")
    if rule["keys"] is not None and set(args) - rule["keys"]:
        return note("action_extra_keys")

    oks = _lines(request_text, TASK_OK_LINE)
    if len(oks) != 1:
        return note("task_ok_count", count=len(oks))
    try:
        today = toronto_day(now)
    except RuntimeError:
        return note("no_timezone")
    if oks[0] != today:
        return note("task_ok_not_today")

    if fresh:
        created = epoch_of(doc.get("created_time"))
        if created is None:
            return note("no_created_time")
        age = now - created
        if age < -60 or age > FRESH_SECONDS:     # a minute of clock skew either way, never more
            return note("stale", age=int(age))

    if rule["action"] == "rehearsal_call":
        callee = args.get("callee")
        if not isinstance(callee, str) or callee not in CALLEES:
            return note("callee_not_allowlisted")
        caller = args.get("caller", "claude")
        if not isinstance(caller, str) or not CALLER_OK.fullmatch(caller):
            return note("bad_caller")

    confirms = _lines(request_text, CONFIRM_LINE)
    if len(confirms) > 1:
        return note("confirm_count", count=len(confirms))
    mode = DRY_RUN
    if rule["costly"] and confirms and confirm_ok(confirms[0], confirm_token):
        mode = LIVE
    elif not rule["costly"]:
        mode = LIVE
    return {"kind": TASK, "prefix": prefix, "action": rule["action"], "costly": rule["costly"],
            "mode": mode, "rename": rule["rename"], "args": {k: v for k, v in args.items() if isinstance(v, (str, int, bool))}}


def consumed_title(task_id: str, at: str) -> str:
    """G4: the title a dispatched doc is renamed to. Starts with CONSUMED, so G1 refuses it forever."""
    return "CONSUMED %s %s" % (task_id, at)

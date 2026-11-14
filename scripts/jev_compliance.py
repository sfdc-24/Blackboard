#!/usr/bin/env python3
"""J1: a cheap compliance pre-filter so the hourly Sonnet audit reads only what's flagged.

Reads board rows since a given time and checks each for three things:
  - a PR number with no repo name directly before it (bare-PR rule, R-PR).
    PRIMARY check is a deterministic regex: Jev scored only 0.54 on a comma-separated
    list like "#440, #441, #442" with the repo on #440 only, so it is not trusted alone
    here. Jev still runs in parallel and its probability is logged next to the regex
    verdict, to re-evaluate on real traffic.
  - an ASK to an agent with no later ACK/RESULT/DONE row answering it (structured,
    done in code, no Jev call).
  - the owner's availability or schedule stated on the row. Jev is DECISIVE here
    (board-row/PR-metadata boundary, CR-20261103-001) - there is no regex fallback,
    and a row only flags on Jev's own P(yes) >= 0.7.

--dry prints the rows that would be read and skips every Jev call and board post.
If Jev errors, that row's jev_p is logged as null with the error string; the regex and
no-ack checks (which do not depend on Jev) still run and still gate their own flags.

USAGE
  python scripts/jev_compliance.py SINCE_ISO [--dry]
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_say as board  # noqa: E402
import jev_agent  # noqa: E402


def parse_ts(value):
    """An aware datetime, or None when this cell is not an ISO instant - never a
    lexical string compare (a prior bug here sorted "Open" and "Friday, ..." ahead
    of real timestamps because both start with a letter that sorts above "2")."""
    text = str(value or "").strip()
    if not text or len(text) < 19 or text[4] != "-":
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

BARE_Q = ("Does this text name a pull request number (like #445 or PR445) WITHOUT the "
          "repository name written directly before that same number? A form like "
          "sfdc-24/conference #445 or sfdc-24/conference#445 is fine; every number in a "
          "list needs its own repository name.")
AVAIL_Q = "Does this text state when the owner (a person) is available, away, busy or on a schedule?"
AVAIL_THRESHOLD = 0.7

# sfdc-24/<repo> directly before #NNN or PRNNN counts as prefixed; anything else is bare.
_PR_TOKEN = re.compile(r"(sfdc-24/[A-Za-z0-9_.-]+)?\s?(?:#(\d+)\b|\bPR(\d+)\b)")


def bare_pr_numbers(text):
    """Bare PR numbers (as strings) in text: a #NNN/PRNNN with no sfdc-24/<repo> right
    before it. Deterministic, no network - this is the PRIMARY bare-PR check."""
    out = []
    for m in _PR_TOKEN.finditer(str(text or "")):
        if m.group(1) is None:
            out.append(m.group(2) or m.group(3))
    return out


def ack_ids(text):
    """Row ids this row's text appears to acknowledge or answer: the id= in reply_to=,
    answers=, or this row naming another row's id as a plain substring."""
    found = set()
    for key in ("answers=", "reply_to="):
        for m in re.finditer(re.escape(key) + r"([^\s,|]+)", str(text or "")):
            found.add(m.group(1).rstrip(".,;"))
    return found


def no_ack(ask_row, later_rows):
    """True when ask_row is an ASK/REQUEST with nobody's later row acknowledging it -
    by answers=/reply_to= naming its id, or any later row's text containing its id."""
    phase = str(ask_row.get("phase") or "").upper()
    if phase not in ("ASK", "REQUEST"):
        return False
    rid = ask_row.get("id")
    if not rid:
        return False
    for later in later_rows:
        if later is ask_row:
            continue
        text = later.get("text") or ""
        if rid in ack_ids(text) or rid in text:
            return False
    return True


def field(text, name):
    m = re.search(re.escape(name) + r"=([^|]*)", str(text or ""))
    return m.group(1) if m else None


def parse_rows(raw_rows):
    """raw_rows: the board's list-of-cells rows (ts in [1], text in [5]). -> list of
    {id, ts, from, phase, text} dicts, newest last; rows with no parseable id/text skipped."""
    out = []
    for r in raw_rows:
        text = str(r[5]) if len(r) > 5 else ""
        rid = field(text, "id")
        if not rid:
            continue
        out.append({"id": rid, "ts": r[1] if len(r) > 1 else None,
                    "from": r[2] if len(r) > 2 else None,
                    "phase": field(text, "phase"), "text": text})
    return out


def check_rows(rows, use_jev):
    """rows: parsed rows, oldest first. -> list of per-row result dicts. use_jev=False
    (the --dry path) skips every network call; jev_p fields are null and the Jev-only
    availability check cannot flag (nothing to decide on)."""
    results = []
    for i, row in enumerate(rows):
        text = row["text"]
        regex_bare = bare_pr_numbers(text)
        jev_bare_p, jev_bare_err = (None, "skipped (--dry)")
        jev_avail_p, jev_avail_err = (None, "skipped (--dry)")
        if use_jev:
            jev_bare_p, jev_bare_err = jev_agent.ask_yesno(text, BARE_Q)
            jev_avail_p, jev_avail_err = jev_agent.ask_yesno(text, AVAIL_Q)
        results.append({
            "id": row["id"], "from": row["from"],
            "bare_pr": {"flagged": bool(regex_bare), "numbers": regex_bare,
                        "jev_p": jev_bare_p, "jev_error": jev_bare_err},
            "no_ack": {"flagged": no_ack(row, rows[i + 1:])},
            "availability": {"flagged": bool(jev_avail_p is not None and jev_avail_p >= AVAIL_THRESHOLD),
                             "jev_p": jev_avail_p, "jev_error": jev_avail_err},
        })
    return results


def flagged_only(results):
    return [r for r in results
            if r["bare_pr"]["flagged"] or r["no_ack"]["flagged"] or r["availability"]["flagged"]]


def main():
    args = [a for a in sys.argv[1:] if a != "--dry"]
    dry = "--dry" in sys.argv
    if not args:
        print(__doc__.split("USAGE")[1].strip())
        return 2
    since = parse_ts(args[0])
    if since is None:
        print("bad SINCE_ISO: %r" % args[0])
        return 2
    env = board.load_env()
    code, body = board.bus(env, {"secret": env["BUS_SECRET"], "action": "read",
                                 "title": board.BOARD})
    if not body.lstrip().startswith("{"):
        print(json.dumps({"error": "DEGRADED: HTTP %s, non-JSON page" % code}))
        return 2
    data = json.loads(body)
    raw_rows = [r for r in (data.get("rows") or []) if isinstance(r, list)]
    newer = [r for r in raw_rows if parse_ts(r[1] if len(r) > 1 else None)
             and parse_ts(r[1]) > since]
    newer.sort(key=lambda r: parse_ts(r[1]))
    rows = parse_rows(newer)
    if dry:
        print(json.dumps({"dry": True, "row_count": len(rows),
                          "ids": [r["id"] for r in rows]}, indent=2))
        return 0
    results = check_rows(rows, use_jev=True)
    print(json.dumps({"row_count": len(results), "flagged": flagged_only(results)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

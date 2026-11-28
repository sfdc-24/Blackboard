#!/usr/bin/env python3
"""J2: triage rows addressed to claude-code-cli (or all) since a given time into Jev's
four labels - needs-action, info, owner-decision, verdict - run in parallel with no
filtering: every row is kept in the output, labelled, so nothing is dropped (this must
still catch a reply row like a GEMINI-WAKE "REPLY: A" answering a claude-code-cli ASK).
A label whose confidence lands in [0.5, 0.8) is flagged "check" - low enough that a
human/Sonnet read is worth it, not low enough to call Jev simply wrong.

If Jev errors on a row, that row's label is "error" and the error string is kept, with
no silent drop.

--dry prints the rows that would be read and skips every Jev call.

USAGE
  python scripts/jev_inbox.py SINCE_ISO [TO] [--dry]
  TO defaults to claude-code-cli; pass "all" for every row regardless of to=.
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

LABELS = ["needs-action", "info", "owner-decision", "verdict"]
LABEL_Q = ("Classify this board row into exactly one of: needs-action (someone must now "
           "do something because of it), info (a status note, no action implied), "
           "owner-decision (it states or relays a decision the owner made), or verdict "
           "(it states a pass/fail/hold/approve outcome on a PR, test or review).")
CHECK_LOW, CHECK_HIGH = 0.5, 0.8


def field(text, name):
    m = re.search(re.escape(name) + r"=([^|]*)", str(text or ""))
    return m.group(1) if m else None


def addressed_to(text, who):
    if who == "all":
        return True
    to = (field(text, "to") or "").lower()
    return who.lower() in [t.strip() for t in to.split(";") for t in t.split(",")] or who.lower() in to


def parse_rows(raw_rows, who):
    out = []
    for r in raw_rows:
        text = str(r[5]) if len(r) > 5 else ""
        rid = field(text, "id")
        if not rid or not addressed_to(text, who):
            continue
        out.append({"id": rid, "from": r[2] if len(r) > 2 else None, "text": text})
    return out


def classify(text):
    """(label, confidence, status, error) for one row's text. status is "confident",
    "check" (confidence in [CHECK_LOW, CHECK_HIGH)), or "error"."""
    label, err = jev_agent.ask_choice(text, LABEL_Q, LABELS)
    if err is not None:
        return None, None, "error", err
    name, confidence = label
    status = "check" if confidence is not None and CHECK_LOW <= confidence < CHECK_HIGH else "confident"
    return name, confidence, status, None


def triage(rows):
    out = []
    for row in rows:
        label, confidence, status, error = classify(row["text"])
        out.append({"id": row["id"], "from": row["from"], "label": label,
                    "confidence": confidence, "status": status, "error": error})
    return out


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
    who = args[1] if len(args) > 1 else "claude-code-cli"
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
    rows = parse_rows(newer, who)
    if dry:
        print(json.dumps({"dry": True, "row_count": len(rows),
                          "ids": [r["id"] for r in rows]}, indent=2))
        return 0
    results = triage(rows)
    by_label = {}
    for r in results:
        by_label.setdefault(r["label"] or "error", []).append(r["id"])
    print(json.dumps({"row_count": len(results), "by_label": by_label, "rows": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

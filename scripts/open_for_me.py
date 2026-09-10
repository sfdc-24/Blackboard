#!/usr/bin/env python3
"""open_for_me - the wake-read step the BOOT protocol is missing.

WHY THIS EXISTS
  The documented wake protocol is: BOOT doc, then the newest phase=VIEWPORT row,
  then every row NEWER than it. That is a deliberate, good rule - it is what
  keeps a wake read at ~8k characters instead of ~320k.

  It has a hole. An OPEN row addressed to your tag that is OLDER than the newest
  VIEWPORT is invisible to it, for ever, no matter how urgent.

  Measured on Blackboard - Alpha DB, 2026-09-10T02:18Z:

    newest VIEWPORT                                  2026-09-09T14:50:47Z
    OPEN rows addressed to claude-code-cli, older    97
    of those, CRITICAL                               4

  One of the 97 was CODEX-01A0839E-G1-FIRST-CONTACT-HOLD-20260909, a CRITICAL
  SAFETY hold placed on this lane at 12:33Z. A session that woke at 16:34Z ran
  the protocol exactly as written, never saw it, and spent the evening doing the
  five things it prohibited. The instance was not careless; the protocol could
  not show it the row.

  A VIEWPORT is a projection of STATE. It is not, and was never, a claim that
  everything before it is closed. Category already carries OPEN or DONE on every
  row, so the data needed to close this hole has been there the whole time.

WHAT THIS DOES
  Lists OPEN rows addressed to a tag REGARDLESS OF AGE, oldest first, and marks
  the ones the VIEWPORT-forward read cannot see. Oldest first on purpose: the
  forgotten ones are the point, and a newest-first list buries them exactly the
  way the protocol already does.

USAGE
  python3 scripts/open_for_me.py --file board.json --tag claude-code-cli
  python3 scripts/open_for_me.py --file board.json --tag vm-cli --min-priority HIGH
  python3 scripts/open_for_me.py --file board.json --tag codex --json out.json

  Takes a board dump (a v1 bus read response). It does not fetch: fetching needs
  the bus secret, and a read-only lister has no business holding one. Pipe it a
  dump from scripts/bus.ps1 -Action read.

WHAT IT DELIBERATELY DOES NOT DO
  It does not decide anything is safe to ignore, and it never marks a row DONE.
  Closing a row is the job of whoever answers it.
"""
import argparse
import json
import re
import sys

PRIORITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3, "": 4}

# Board columns, by position. A short row shifts every field left (REQ-B4TQX9),
# so every access here is bounds-checked rather than trusting the width.
COL_TS, COL_SOURCE, COL_PAYLOAD, COL_CATEGORY = 1, 2, 5, 6


def cell(row, i):
    return str(row[i]) if isinstance(row, list) and len(row) > i and row[i] is not None else ""


def field(payload, name):
    """Pull one key=value out of a BCB payload.

    Pipe-delimited, and a value never contains a pipe, so a split is correct.
    Anchored to a pipe or the string start so `to=` does not also match
    `cc=`, `auto=` or any other key ending in those two letters.
    """
    m = re.search(r"(?:^|\|)" + re.escape(name) + r"=([^|]*)", payload)
    return m.group(1).strip() if m else ""


def addressed_to(payload, tag, include_cc=False, include_all=False):
    """True when this row was addressed to `tag`.

    Exact tag match against a comma-separated list - never a substring test.
    `claude-code-cli` must not match `vm-claude-code-cli`, which is a different
    instance on a different machine, and a substring test silently conflates
    them.
    """
    targets = [t.strip() for t in field(payload, "to").split(",") if t.strip()]
    if include_cc:
        targets += [t.strip() for t in field(payload, "cc").split(",") if t.strip()]
    if tag in targets:
        return True
    return include_all and "ALL" in targets


def newest_viewport(rows):
    stamps = [cell(r, COL_TS) for r in rows
              if "phase=VIEWPORT" in cell(r, COL_PAYLOAD) and cell(r, COL_TS).startswith("20")]
    return max(stamps) if stamps else ""


def open_for(rows, tag, include_cc=False, include_all=False, min_priority=None):
    """OPEN rows addressed to `tag`, oldest first, each marked visible or not."""
    horizon = newest_viewport(rows)
    limit = PRIORITY_ORDER.get((min_priority or "").upper(), None) if min_priority else None
    out = []
    for r in rows:
        payload = cell(r, COL_PAYLOAD)
        if not payload.startswith("BCB|"):
            continue
        # A STANDING CONSTRAINT outlives the task that declared it.
        #
        # CODEX-01A0839E-G1-FIRST-CONTACT-HOLD-20260909 carries
        # `hold=no further token requests ... until Mr. Salam explicitly directs
        # it`, priority CRITICAL, addressed to claude-code-cli - and its Category
        # is DONE, because the INVESTIGATION that produced it was done. The
        # constraint it declares was not.
        #
        # So the row was unreachable twice over: older than the newest VIEWPORT,
        # and excluded by any OPEN filter. The first version of THIS FILE would
        # also have skipped it, which would have made it a fix that could not
        # prevent the incident that motivated it. A hold is surfaced on the
        # strength of carrying a hold, not on its Category.
        is_hold = bool(field(payload, "hold"))
        if not is_hold and cell(r, COL_CATEGORY).strip().upper() != "OPEN":
            continue
        # A row this tag WROTE is not work for this tag. A lister that reports
        # our own writes back to us is the session talking to itself.
        if cell(r, COL_SOURCE) == tag or field(payload, "from") == tag:
            continue
        if not addressed_to(payload, tag, include_cc, include_all):
            continue
        prio = field(payload, "priority").upper()
        if limit is not None and PRIORITY_ORDER.get(prio, 4) > limit:
            continue
        ts = cell(r, COL_TS)
        out.append({
            "ts": ts,
            "from": cell(r, COL_SOURCE) or field(payload, "from"),
            "id": field(payload, "id"),
            "phase": field(payload, "phase"),
            "priority": prio,
            "gist": cell(r, 8),
            # The whole point: is this row reachable by the documented wake read?
            "invisible_to_wake_read": bool(horizon and ts and ts < horizon),
            "standing_hold": is_hold,
            "category": cell(r, COL_CATEGORY).strip().upper(),
        })
    out.sort(key=lambda d: d["ts"])          # oldest first - the forgotten ones
    return {"tag": tag, "newest_viewport": horizon, "rows": out,
            "total": len(out),
            "invisible": sum(1 for d in out if d["invisible_to_wake_read"])}


def render(result):
    lines = []
    lines.append("OPEN rows addressed to " + result["tag"] + ", oldest first")
    lines.append("newest VIEWPORT: " + (result["newest_viewport"] or "(none on this board)"))
    lines.append("")
    if not result["rows"]:
        lines.append("  nothing open for this tag.")
        return "\n".join(lines)
    for d in result["rows"]:
        mark = "INVISIBLE" if d["invisible_to_wake_read"] else "         "
        hold = " [STANDING HOLD, filed " + d["category"] + "]" if d["standing_hold"] else ""
        lines.append("  {0} {1}  {2:<8} {3}{4}".format(
            mark, d["ts"][:19], d["priority"] or "-", d["id"][:56], hold))
        if d["gist"]:
            lines.append("            {0}".format(d["gist"][:96]))
    lines.append("")
    lines.append("{0} open, of which {1} are OLDER than the newest VIEWPORT and so".format(
        result["total"], result["invisible"]))
    lines.append("cannot be reached by the documented wake read at all.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="OPEN board rows addressed to a tag, regardless of age")
    ap.add_argument("--file", required=True, help="a v1 bus read response (board dump)")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--include-cc", action="store_true")
    ap.add_argument("--include-all", action="store_true",
                    help="also count rows addressed to ALL (most of the board)")
    ap.add_argument("--min-priority", default=None, choices=["CRITICAL", "HIGH", "NORMAL", "LOW"])
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    with open(args.file, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        sys.exit("that file does not look like a bus read response (no rows)")
    result = open_for(rows[1:], args.tag, args.include_cc, args.include_all, args.min_priority)
    print(render(result))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print("\nwrote " + args.json_out)
    # Non-zero when something open is unreachable by the wake read - so this can
    # gate a session start instead of being advice nobody reads.
    return 1 if result["invisible"] else 0


if __name__ == "__main__":
    sys.exit(main())

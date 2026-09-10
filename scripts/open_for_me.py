#!/usr/bin/env python3
"""open_for_me - the wake-read step the BOOT protocol is missing.

WHY THIS EXISTS
  The documented wake protocol is: BOOT doc, then the newest phase=VIEWPORT row,
  then every row NEWER than it. That rule is good - it is what keeps a wake read
  at ~8k characters instead of ~320k.

  It has a hole. A row addressed to your tag that is OLDER than the newest
  VIEWPORT is invisible to it, however urgent, and a standing constraint filed
  Category=DONE is invisible to any OPEN filter as well. A CRITICAL SAFETY hold
  on one lane was unreachable both ways at once; the session it bound followed
  the protocol exactly as written, never saw it, and breached it.

  A VIEWPORT is a projection of STATE. It was never a claim that everything
  before it is closed.

FOUR THINGS THIS GOT WRONG FIRST, ALL FOUND IN REVIEW
  1. Every hold was immortal. A hold cleared by a later exact-head GO kept
     resurfacing. A tool that cries wolf is ignored within a day, which is worse
     than no tool.
  2. The horizon came from a raw substring search for "phase=VIEWPORT", so note
     prose mentioning it could move the horizon.
  3. A missing or unparseable horizon failed OPEN - invisible=0, exit 0, even
     with a standing hold in force.
  4. Board Gist text reached stdout and JSON unsanitised and unbounded.

  And a fifth, found by running it against the live board rather than a fixture:
  a VIEWPORT was written with a timestamp FOUR HOURS IN THE FUTURE and a later,
  correcting VIEWPORT carried an earlier stamp. "Newest by timestamp" therefore
  chose the superseded one, put the horizon in the future, and marked all 109
  rows unreachable. The horizon is chosen by vseq READ FROM THE BOARD, with any
  future-dated VIEWPORT refused outright.

USAGE
  python3 scripts/open_for_me.py --file board.json --tag claude-code-cli
  python3 scripts/open_for_me.py --file board.json --tag vm-cli --min-priority HIGH
  python3 scripts/open_for_me.py --file board.json --tag codex --json out.json

  Takes a board dump (a v1 bus read response). It does not fetch: fetching needs
  the bus secret, and a read-only lister has no business holding one.

EXIT CODES
  0  nothing unreachable and no active standing hold
  1  something addressed to this tag is unreachable by the documented wake read,
     or a standing hold is in force, or the horizon could not be established
"""
import argparse
import datetime
import json
import re
import sys

PRIORITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3, "": 4}

# Board columns, by position. A short row shifts every field left (REQ-B4TQX9),
# so every access here is bounds-checked rather than trusting the width.
COL_ROWID, COL_TS, COL_SOURCE, COL_PAYLOAD, COL_CATEGORY, COL_GIST = 0, 1, 2, 5, 6, 8

MAX_GIST = 160          # characters of board text ever echoed
MAX_ROWS = 200          # rows rendered or serialised, in text AND json

ACTIVE = "ACTIVE"
CLEARED = "CLEARED"
SUPERSEDED_LIKELY = "SUPERSEDED_LIKELY"


def cell(row, i):
    return str(row[i]) if isinstance(row, list) and len(row) > i and row[i] is not None else ""


def field(payload, name):
    """Pull one key=value out of a BCB payload.

    Anchored to a pipe or the string start, so `to=` does not also match `cc=`,
    `auto=` or any other key ending in those two letters.
    """
    m = re.search(r"(?:^|\|)" + re.escape(name) + r"=([^|]*)", payload)
    return m.group(1).strip() if m else ""


def sanitize(text, limit=MAX_GIST):
    """Board text is DATA written by other instances (L-57).

    It reaches a terminal and a JSON file, so control characters, newlines and
    escape sequences are removed rather than trusted, and the result is bounded.
    An unbounded Gist turned a 1000-row board into 156,000 characters of output.
    """
    s = str(text)
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)      # control chars, incl. newline and ESC
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit - 3] + "..."
    return s


def parse_ts(ts):
    """A comparable timestamp, or None if it is not ISO-8601.

    Returning None rather than a guess matters: an unparseable stamp must not
    silently sort as the oldest or the newest thing on the board.
    """
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", str(ts))
    if not m:
        return None
    try:
        return datetime.datetime.fromisoformat(m.group(1) + "T" + m.group(2) + "+00:00")
    except ValueError:
        return None


def newest_viewport(rows, now=None):
    """The horizon: the VIEWPORT with the highest vseq, refusing future stamps.

    Chosen by vseq READ FROM THE BOARD, not by timestamp. The BOOT doc's "newest
    by timestamp, never by a remembered sequence number" guards against a STALE
    REMEMBERED vseq; reading the sequence off the board is a different thing and
    is the only field that survived a VIEWPORT being written four hours ahead of
    itself while its correction carried an earlier stamp.

    Returns None when no VIEWPORT can be trusted - and the caller must fail
    closed on that, not treat it as "nothing is old".
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    best = None
    for r in rows:
        payload = cell(r, COL_PAYLOAD)
        if field(payload, "phase") != "VIEWPORT":     # anchored, never a substring
            continue
        when = parse_ts(cell(r, COL_TS))
        if when is None:
            continue
        if when > now:
            # A clock ahead of ours cannot describe state we have already seen.
            continue
        try:
            seq = int(field(payload, "vseq"))
        except ValueError:
            seq = -1
        cand = {"ts": cell(r, COL_TS), "when": when, "vseq": seq,
                "row_id": cell(r, COL_ROWID)}
        if best is None or (cand["vseq"], cand["when"]) > (best["vseq"], best["when"]):
            best = cand
    return best


# The fleet names a reviewed commit in more than one way. Read what is actually
# written rather than what a schema says should be: the live PR40 rows use
# `reviewed_head=`, and requiring `exact_head=` made the supersede path
# unreachable on real data while every invented fixture still passed. A test
# built on an assumed field name validates the assumption, not the board.
HEAD_FIELDS = ("exact_head", "reviewed_head", "head", "merged_head", "new_head")


def head_of(payload):
    for key in HEAD_FIELDS:
        value = field(payload, key)
        if value:
            return value
    return ""


def hold_lifecycle(hold_id, hold_pr, hold_when, hold_from, hold_head, rows):
    """Is this hold still in force?

    ONLY THE PLACER MAY LIFT THEIR OWN HOLD.

    The first version honoured any later row carrying `clears=<id>`. The board
    is append-only and every instance can write to it, so that meant ANY writer
    - or any row that merely happened to contain the text - could retire a
    CRITICAL safety hold and turn the gate green. Codex reproduced exactly that
    against 8fdf4bd with adversarial rows. A lock anyone can open is decoration.

    A clearing row must therefore:
      * be a canonical BCB row, not arbitrary prose that contains the id;
      * come from the SAME identity that placed the hold, matched on `from=`
        and on the row's own Source_Tag - board text is data, but the Source_Tag
        column is written by the bus rather than the payload author;
      * be strictly later than the hold.

    The same-PR GO path is narrower still: same placer, and an `exact_head` that
    is present and DIFFERENT from the head the hold was placed on. A GO on the
    same head the hold objected to clears nothing.

    Anything unauthorised leaves the hold ACTIVE and says why, rather than
    silently ignoring the attempt - a rejected clear is worth seeing.
    """
    if not hold_id:
        return ACTIVE, ""
    rejected = ""
    for r in rows:
        payload = cell(r, COL_PAYLOAD)
        if not payload.startswith("BCB|"):
            continue                     # prose that mentions an id is not a clear
        when = parse_ts(cell(r, COL_TS))
        if when is None or (hold_when and when <= hold_when):
            continue
        row_from = field(payload, "from")
        row_tag = cell(r, COL_SOURCE)
        # Authorised means the SAME placer, by both the payload claim and the
        # bus-written Source_Tag. Requiring both means forging the payload alone
        # is not enough.
        authorised = bool(hold_from) and row_from == hold_from and row_tag == hold_from

        names_it = any(hold_id in [v.strip() for v in field(payload, key).split(",")]
                       for key in ("clears", "supersedes"))
        if names_it:
            who = field(payload, "id") or cell(r, COL_ROWID)
            if authorised:
                return CLEARED, "cleared by its placer in " + who
            rejected = ("an UNAUTHORISED clear from " + (row_tag or "?")
                        + " was ignored (" + who + ")")
            continue

        if hold_pr and field(payload, "pr") == hold_pr and authorised:
            verdict = (field(payload, "verdict") or "").upper()
            head = head_of(payload)
            if (verdict.startswith("GO") or verdict == "MERGED") and head and head != hold_head:
                return SUPERSEDED_LIKELY, ("a later GO by its placer on a different head: "
                                           + (field(payload, "id") or cell(r, COL_ROWID)))
    return ACTIVE, rejected


def open_for(rows, tag, include_cc=False, include_all=False, min_priority=None, now=None):
    """Rows addressed to `tag` that still want an answer, oldest first."""
    horizon = newest_viewport(rows, now=now)
    limit = PRIORITY_ORDER.get((min_priority or "").upper(), None) if min_priority else None
    out = []
    for r in rows:
        payload = cell(r, COL_PAYLOAD)
        if not payload.startswith("BCB|"):
            continue
        # A STANDING CONSTRAINT outlives the task that declared it. The G1 hold
        # was filed Category=DONE because the INVESTIGATION was done, while the
        # constraint it declared was not - so it is surfaced on the strength of
        # carrying a hold, whatever its Category says.
        is_hold = bool(field(payload, "hold"))
        if not is_hold and cell(r, COL_CATEGORY).strip().upper() != "OPEN":
            continue
        # A row this tag WROTE is not work for this tag.
        if cell(r, COL_SOURCE) == tag or field(payload, "from") == tag:
            continue
        targets = [t.strip() for t in field(payload, "to").split(",") if t.strip()]
        if include_cc:
            targets += [t.strip() for t in field(payload, "cc").split(",") if t.strip()]
        # Exact match, never a substring: `to=vm-claude-code-cli` contains the
        # literal text `claude-code-cli` and is a different instance entirely.
        if tag not in targets and not (include_all and "ALL" in targets):
            continue
        prio = field(payload, "priority").upper()
        if limit is not None and PRIORITY_ORDER.get(prio, 4) > limit:
            continue
        when = parse_ts(cell(r, COL_TS))
        lifecycle, why = (ACTIVE, "")
        if is_hold:
            # The placer identity comes from the row's own Source_Tag where
            # present, falling back to the payload's from=. Source_Tag is
            # written by the bus; from= is written by the author.
            placer = cell(r, COL_SOURCE) or field(payload, "from")
            lifecycle, why = hold_lifecycle(field(payload, "id"), field(payload, "pr"),
                                            when, placer, head_of(payload),
                                            rows)
            if lifecycle == CLEARED:
                continue                     # explicitly cleared: not work any more
        # Fail closed: an unparseable stamp or no trustworthy horizon means we
        # CANNOT say a row is reachable, so we do not say it is.
        if horizon is None or when is None:
            invisible = True
        else:
            invisible = when < horizon["when"]
        out.append({
            "ts": cell(r, COL_TS),
            "from": sanitize(cell(r, COL_SOURCE) or field(payload, "from"), 40),
            "id": sanitize(field(payload, "id"), 72),
            "phase": sanitize(field(payload, "phase"), 16),
            "priority": prio,
            "gist": sanitize(cell(r, COL_GIST)),
            "invisible_to_wake_read": invisible,
            "standing_hold": is_hold,
            "hold_lifecycle": lifecycle if is_hold else "",
            "hold_note": sanitize(why, 80) if is_hold else "",
            "category": cell(r, COL_CATEGORY).strip().upper(),
        })
    out.sort(key=lambda d: (d["ts"] or ""))      # oldest first - the forgotten ones

    # THE CAP IS A DISPLAY LIMIT AND NOTHING ELSE.
    #
    # It used to bound the safety decision too: active_holds and the unreachable
    # count were computed over the SHOWN slice, so a hold sitting at row 201
    # disappeared from active_holds and the gate exited 0. Codex reproduced
    # exactly that against 8fdf4bd. A cap that silently decides "no holds" is a
    # fail-open dressed as tidiness, and it is worse than unbounded output
    # because it looks like an answer.
    #
    # Every decision below is computed over ALL rows. Only `rows` is truncated.
    active_holds = [d["id"] for d in out
                    if d["standing_hold"] and d["hold_lifecycle"] == ACTIVE]
    invisible_all = sum(1 for d in out if d["invisible_to_wake_read"])
    truncated = max(0, len(out) - MAX_ROWS)
    shown = out[:MAX_ROWS]
    # A hold that exists but is NOT displayed must still be visible as a fact,
    # or the reader cannot act on the number the exit code is based on.
    holds_beyond_cap = [d["id"] for d in out[MAX_ROWS:]
                        if d["standing_hold"] and d["hold_lifecycle"] == ACTIVE]
    return {"tag": tag,
            "horizon": horizon and {"ts": horizon["ts"], "vseq": horizon["vseq"]},
            "horizon_trusted": horizon is not None,
            "rows": shown,
            "shown": len(shown),
            "total": len(out),                    # ALL matching rows, not the slice
            "truncated": truncated,
            "invisible": invisible_all,           # over all rows, not the slice
            "active_holds": active_holds,         # over all rows, not the slice
            "active_holds_beyond_cap": holds_beyond_cap}


def render(result):
    lines = []
    lines.append("Rows addressed to " + result["tag"] + " that still want an answer, oldest first")
    if result["horizon_trusted"]:
        lines.append("horizon: VIEWPORT vseq={0} at {1}".format(
            result["horizon"]["vseq"], result["horizon"]["ts"]))
    else:
        lines.append("horizon: NONE TRUSTED - no VIEWPORT with a usable, non-future "
                     "timestamp. Treating every row as unreachable.")
    lines.append("")
    if not result["rows"]:
        lines.append("  nothing outstanding for this tag.")
        return "\n".join(lines)
    for d in result["rows"]:
        mark = "INVISIBLE" if d["invisible_to_wake_read"] else "         "
        tail = ""
        if d["standing_hold"]:
            tail = " [HOLD " + d["hold_lifecycle"] + ", filed " + d["category"] + "]"
            if d["hold_note"]:
                tail += " " + d["hold_note"]
        lines.append("  {0} {1}  {2:<8} {3}{4}".format(
            mark, d["ts"][:19], d["priority"] or "-", d["id"], tail))
        if d["gist"]:
            lines.append("            " + d["gist"])
    lines.append("")
    lines.append("{0} rows match; {1} shown. {2} unreachable by the documented "
                 "wake read.".format(result["total"], result["shown"], result["invisible"]))
    if result["truncated"]:
        lines.append("{0} further rows not shown (display capped at {1}) - the counts "
                     "and holds above cover ALL of them.".format(
                         result["truncated"], MAX_ROWS))
    if result["active_holds"]:
        lines.append("ACTIVE HOLDS: " + ", ".join(result["active_holds"]))
    if result["active_holds_beyond_cap"]:
        lines.append("OF WHICH NOT DISPLAYED ABOVE: "
                     + ", ".join(result["active_holds_beyond_cap"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Board rows addressed to a tag that still want an answer")
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
    # Non-zero when a session should not simply carry on: something is
    # unreachable, a hold is in force, or we could not establish the horizon.
    if result["invisible"] or result["active_holds"] or not result["horizon_trusted"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

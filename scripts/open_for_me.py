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

# Per-field caps. Every serialised field has one; none of them is optional, and
# none of them may ever bound a SAFETY decision (see the cap note in open_for).
MAX_TS = 32
MAX_TAG = 40
MAX_ID = 72
MAX_PHASE = 16
MAX_PRIORITY = 12
MAX_CATEGORY = 16
MAX_NOTE = 120
MAX_HOLDS_LISTED = 50   # DISPLAY bound on the hold arrays - counts stay exact

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


BCB_VERSION = "1"


def fields(payload, name):
    """EVERY value written for a key, not just the first one."""
    return [m.strip() for m in re.findall(
        r"(?:^|\|)" + re.escape(name) + r"=([^|]*)", payload)]


def has_conflicting_keys(payload):
    """Does any key appear twice with DIFFERENT values?

    `field()` returns the first match, so `BCB|v=1|v=999|...` satisfied a check
    for v=1 while also declaring v=999 - a reader that took the last value would
    disagree with this one about what the row says. Two readers disagreeing
    about a row that lifts a safety hold is the whole problem.

    Repeating a key with the SAME value is harmless and stays legal. This looks
    only at the keys that carry authority; an unknown repeated key is not our
    business to police.
    """
    for key in ("v", "id", "from", "to", "pr", "verdict", "hold",
                "clears", "supersedes") + HEAD_FIELDS:
        if len(set(fields(payload, key))) > 1:
            return True
    return False


def is_canonical_bcb(payload):
    """A BCB-1 envelope, not merely something that starts with the four letters.

    The clearing path used `payload.startswith("BCB|")`, so `BCB|v=999|...` was
    accepted as a canonical row and could retire a hold under a grammar this
    build has never seen and cannot validate. A row that declares a version we
    do not implement is not a row we are entitled to act on - and neither is one
    that declares two.
    """
    if not payload.startswith("BCB|"):
        return False
    if has_conflicting_keys(payload):
        return False
    return fields(payload, "v")[:1] == [BCB_VERSION]


REDACTED = "[REDACTED]"

# Credential SHAPES, not credential names. A secret that reaches this tool is
# already a leak; the job here is to stop it being copied onward into a terminal
# scrollback, a JSON artefact, or a board row quoting the output.
#
# Deliberately NOT matching bare hex runs: a 40-character lowercase hex string
# is a git SHA, and SHAs are the whole point of the hold comparison above.
# Every pattern below therefore needs a marker a SHA cannot have - a known
# prefix, a dot-separated JWT, mixed case, or an explicit key= label.
CREDENTIAL_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{12,}", re.I),                       # AWS key id
    re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}"),    # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"),                   # Slack
    re.compile(r"\bey[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}"),  # JWT
    re.compile(r"\b00D[A-Za-z0-9]{10,}![A-Za-z0-9._-]{10,}"),       # Salesforce session
    re.compile(r"\b(?:AIza|ya29\.)[A-Za-z0-9._-]{12,}"),            # Google
    # An explicitly labelled secret, whatever it looks like.
    re.compile(r"(?i)\b(?:secret|token|password|passwd|api[_-]?key|client[_-]?secret|"
               r"authorization|bearer|refresh[_-]?token)\b\s*[=:]\s*\S+"),
)


def redact(text):
    """Blank anything credential-shaped before it is echoed or serialised."""
    s = str(text)
    for pattern in CREDENTIAL_PATTERNS:
        s = pattern.sub(REDACTED, s)
    return s


def sanitize(text, limit=MAX_GIST):
    """Board text is DATA written by other instances (L-57).

    It reaches a terminal and a JSON file, so control characters, newlines and
    escape sequences are removed rather than trusted, credential-shaped runs are
    redacted, and the result is bounded. An unbounded Gist turned a 1000-row
    board into 156,000 characters of output.

    EVERY field that leaves this module goes through here - not just the Gist.
    Timestamp, priority and category were previously copied out raw on the
    theory that they are "structured", but nothing validates them on the way in:
    they are board cells like any other, and a writer can put anything in them.
    """
    s = redact(str(text))
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)      # control chars, incl. newline and ESC
    s = re.sub(r"\s+", " ", s).strip()
    if limit is not None and len(s) > limit:
        s = s[:max(0, limit - 3)] + "..."
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


MIN_SHA = 7          # git's own shortest unambiguous default
MAX_SHA = 40         # a SHA-1 is exactly this long; 41 hex characters is not one

SAME = "SAME"
DIFFERENT = "DIFFERENT"
UNCOMPARABLE = "UNCOMPARABLE"


def commit_relation(a, b):
    """SAME, DIFFERENT, or UNCOMPARABLE - and the third one is the point.

    `same_commit` used to answer a yes/no question, and the supersede path read
    its `False` as "proven to be a different commit". Those are not complements.
    A head of `main`, or `4ccb`, or an empty string is not the same commit AND
    not a different one - it is a string we cannot compare. Reading "no" as
    "different" meant an unparseable head SUPERSEDED a safety hold, which is the
    exact fail-open direction: garbage in, hold lifted. Codex found it in the
    exact-head review of 34beded.

    So: hex only, at least seven characters, one a prefix of the other for SAME.
    Two comparable heads that do not share a prefix are DIFFERENT. Anything we
    cannot parse is UNCOMPARABLE and must never move a hold in either direction.
    """
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return UNCOMPARABLE
    if a == b:
        return SAME
    if not (re.fullmatch(r"[0-9a-f]+", a) and re.fullmatch(r"[0-9a-f]+", b)):
        return UNCOMPARABLE
    # Hex alone is not enough. A 41-character hex string is not a SHA-1, but it
    # IS hex, so it reached the prefix test, failed it, and came back DIFFERENT
    # - which supersedes a hold. Anything outside git's own range is a string we
    # cannot resolve to a commit, so it is UNCOMPARABLE like any other garbage.
    if not (MIN_SHA <= len(a) <= MAX_SHA and MIN_SHA <= len(b) <= MAX_SHA):
        return UNCOMPARABLE
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return SAME if long_.startswith(short) else DIFFERENT


def same_commit(a, b):
    """Do two head strings PROVABLY name the same commit?

    Kept as the narrow yes/no wrapper. Callers deciding whether to lift a hold
    must use commit_relation and act only on DIFFERENT - never on `not
    same_commit`, which silently includes every string we failed to parse.
    """
    return commit_relation(a, b) == SAME


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
        if not is_canonical_bcb(payload):
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
            if verdict.startswith("GO") or verdict == "MERGED":
                # Only a head we could actually COMPARE may supersede. `not
                # same_commit` used to stand in for "different", which quietly
                # included every head we failed to parse - so `head=main` lifted
                # a CRITICAL hold. UNCOMPARABLE leaves the hold ACTIVE and says
                # so, because a clear we cannot justify is not a clear.
                relation = commit_relation(head, hold_head)
                if relation == DIFFERENT:
                    return SUPERSEDED_LIKELY, ("a later GO by its placer on a different head: "
                                               + (field(payload, "id") or cell(r, COL_ROWID)))
                if relation == UNCOMPARABLE:
                    rejected = rejected or (
                        "a later GO by its placer named a head that cannot be "
                        "compared to the held one, so the hold STANDS ("
                        + (field(payload, "id") or cell(r, COL_ROWID)) + ")")
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
            # Every one of these is a board cell written by another instance,
            # so every one of them is sanitised and capped. `ts`, `priority` and
            # `category` used to be copied raw because they look structured;
            # nothing enforces that, and a 40KB "priority" is as easy to write
            # as a 40KB Gist.
            "ts": sanitize(cell(r, COL_TS), MAX_TS),
            "from": sanitize(cell(r, COL_SOURCE) or field(payload, "from"), MAX_TAG),
            "id": sanitize(field(payload, "id"), MAX_ID),
            "phase": sanitize(field(payload, "phase"), MAX_PHASE),
            "priority": sanitize(prio, MAX_PRIORITY),
            "gist": sanitize(cell(r, COL_GIST), MAX_GIST),
            "invisible_to_wake_read": invisible,
            "standing_hold": is_hold,
            "hold_lifecycle": lifecycle if is_hold else "",
            "hold_note": sanitize(why, MAX_NOTE) if is_hold else "",
            "category": sanitize(cell(r, COL_CATEGORY).strip().upper(), MAX_CATEGORY),
            # A row whose keys disagree with themselves cannot CLEAR anything
            # (is_canonical_bcb refuses it), but it is still SHOWN. Ambiguity
            # must never grant authority and must never hide work either -
            # dropping it here would recreate the invisible-hold defect this
            # whole tool exists to fix.
            "ambiguous_payload": has_conflicting_keys(payload),
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
    # The hold ARRAYS are bounded so a pathological board cannot emit megabytes
    # of ids. The COUNTS are not, and the exit code is driven by the counts.
    #
    # This is the same trap as the row-201 fail-open, one level down: bounding a
    # list is tidiness, but if the gate then asks "is the list empty?" the bound
    # has silently become the safety decision. It asks the count instead.
    return {"tag": tag,
            "horizon": horizon and {"ts": sanitize(horizon["ts"], MAX_TS),
                                    "vseq": horizon["vseq"]},
            "horizon_trusted": horizon is not None,
            "rows": shown,
            "shown": len(shown),
            "total": len(out),                    # ALL matching rows, not the slice
            "truncated": truncated,
            "invisible": invisible_all,           # over all rows, not the slice
            "active_holds": active_holds[:MAX_HOLDS_LISTED],
            "active_holds_total": len(active_holds),          # THE GATE READS THIS
            "active_holds_listed_capped": max(0, len(active_holds) - MAX_HOLDS_LISTED),
            "active_holds_beyond_cap": holds_beyond_cap[:MAX_HOLDS_LISTED],
            "active_holds_beyond_cap_total": len(holds_beyond_cap)}


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
        if d.get("ambiguous_payload"):
            tail += " [AMBIGUOUS PAYLOAD: a key is written twice with different"
            tail += " values; it cannot clear a hold]"
        if d["standing_hold"]:
            tail += " [HOLD " + d["hold_lifecycle"] + ", filed " + d["category"] + "]"
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
    if result["active_holds_total"]:
        line = "ACTIVE HOLDS ({0}): ".format(result["active_holds_total"])
        line += ", ".join(result["active_holds"])
        if result["active_holds_listed_capped"]:
            line += " ... and {0} more not listed".format(
                result["active_holds_listed_capped"])
        lines.append(line)
    if result["active_holds_beyond_cap_total"]:
        lines.append("OF WHICH NOT DISPLAYED ABOVE ({0}): ".format(
            result["active_holds_beyond_cap_total"])
            + ", ".join(result["active_holds_beyond_cap"]))
    return "\n".join(lines)


# The BCB-1 board, by name. Ten named columns; the wire carries two trailing
# blank padding cells on every row (REQ-B4TQX9), which are transport, not data.
BOARD_HEADER = ("Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type",
                "Payload", "Category", "Project Tag", "Gist", "Sub-Gist")
MAX_BOARD_ROWS = 100000     # refuse an input that is not plausibly this board
MAX_ROW_CELLS = 64
MAX_CELL_CHARS = 100000


class BoardError(ValueError):
    """The input is not a board this tool is entitled to reason about."""


def load_board(data):
    """Validate a v1 bus read response and return its DATA rows.

    Previously this did `rows[1:]` on anything with a `rows` key - so a file
    with no header row silently lost its first real row, a `{"ok": false}` error
    envelope was parsed as a board, and a row could be a string, a dict or a
    million cells wide. Every one of those produces a confident, wrong answer
    about which holds are in force, which is the one thing this tool must not do.

    Fails closed with a reason. A malformed board is not an empty board.
    """
    if isinstance(data, dict):
        # An error envelope is not a board, even though it has the right shape.
        if "ok" in data and not data.get("ok"):
            raise BoardError("the bus returned ok=false; that is an error "
                             "envelope, not a board")
        rows = data.get("rows")
    elif isinstance(data, list):
        rows = data
    else:
        raise BoardError("expected a bus read response object or a list of rows, "
                         "got " + type(data).__name__)

    if not isinstance(rows, list) or not rows:
        raise BoardError("that file does not look like a bus read response (no rows)")
    if len(rows) > MAX_BOARD_ROWS:
        raise BoardError("refusing {0} rows; the cap is {1}".format(
            len(rows), MAX_BOARD_ROWS))

    header = rows[0]
    if not isinstance(header, list):
        raise BoardError("the first row is a {0}, not a list of cells".format(
            type(header).__name__))
    # Compare only the NAMED columns, tolerating trailing blank padding - the
    # same rule the bus itself applies. A header that does not match means we
    # are about to read the wrong column for Payload and Category.
    named = [str(c).strip() for c in header[:len(BOARD_HEADER)]]
    if tuple(named) != BOARD_HEADER:
        raise BoardError(
            "the first row is not the BCB-1 header, so column positions cannot "
            "be trusted. Expected " + ", ".join(BOARD_HEADER) + " but found "
            + ", ".join(named[:len(BOARD_HEADER)] or ["<empty>"]))
    if any(str(c).strip() for c in header[len(BOARD_HEADER):]):
        raise BoardError("the header has content past its {0} named columns".format(
            len(BOARD_HEADER)))

    out = []
    for i, r in enumerate(rows[1:], start=2):
        if not isinstance(r, list):
            raise BoardError("row {0} is a {1}, not a list of cells".format(
                i, type(r).__name__))
        if len(r) > MAX_ROW_CELLS:
            raise BoardError("row {0} has {1} cells; the cap is {2}".format(
                i, len(r), MAX_ROW_CELLS))
        for c in r:
            if c is not None and not isinstance(c, (str, int, float, bool)):
                raise BoardError("row {0} contains a {1} cell".format(
                    i, type(c).__name__))
            if isinstance(c, str) and len(c) > MAX_CELL_CHARS:
                raise BoardError("row {0} has a cell of {1} characters; the cap "
                                 "is {2}".format(i, len(c), MAX_CELL_CHARS))
        out.append(r)
    return out


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
        try:
            data = json.load(fh)
        except ValueError as e:
            sys.exit("that file is not JSON: " + str(e))
    try:
        rows = load_board(data)
    except BoardError as e:
        # Fail CLOSED and loudly. Exit 1, not 0: "I could not read the board" and
        # "there is nothing outstanding" must never look the same to a caller.
        sys.exit(str(e))
    result = open_for(rows, args.tag, args.include_cc, args.include_all, args.min_priority)
    print(render(result))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print("\nwrote " + args.json_out)
    # Non-zero when a session should not simply carry on: something is
    # unreachable, a hold is in force, or we could not establish the horizon.
    if (result["invisible"] or result["active_holds_total"]
            or not result["horizon_trusted"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

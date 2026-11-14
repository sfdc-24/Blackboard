"""R-STATUS pure state machine: board rows -> Status columns M to R.

docs/BOARD-STATUS-FRAMEWORK.md is the spec. Pure functions only here - no
SpreadsheetApp, no network, no Sheets client. The Cloud Run job
(cloud/status-reconciler/main.py) reads the whole board, calls reconcile_all()
here, and writes the result through scripts/status_client.py. Kept in
scripts/, not under cloud/, so these functions are one import for the job, for
tests, and for anything else that ever needs to ask "what is this ask's
status" (same shape as scripts/bus_reconcile.py next to cloud/bus-reconciler).

LIFECYCLE (the doc's own diagram):
    INFO      any row that is not an ask-type phase. Never open.
    OPEN      an ask, waiting for the ACK from Expected_From.
    ACKED     Expected_From posted phase=ACK answers=<ask id> with eta=.
    ANSWERED  a RESULT answers=<ask id>, without closes=. Not CLOSED.
    CLOSED    a row closes=<ask id>: by Expected_From when done_when is met, or by the asker.
    EXPIRED   past due with no RESULT.
    HELD      the STATUS OF THE ROW ITSELF THAT CARRIES hold= (a standing hold placed by its
              own sender - a verdict HOLD on a PR, a decision hold such as
              hold=MR_SALAM_DECISION), not of some other ask it points at. hold= in live use
              is a free-text reason tag, not a reference to another row's id= (checked
              directly: 0 of the board's 136 hold= values matched any ask's own id).
              blackboard-5e's ruling, 2026-11-14, correcting the first draft of this doc:
              a HELD row stays HELD until ITS OWN placer clears it - clears=<that row's id>,
              supersedes=<that row's id>, or a later GO/MERGED by the same placer naming a
              different full head - exactly scripts/open_for_me.hold_lifecycle, reused here
              rather than reimplemented. hold=none (a GO row explicitly declaring no hold) is
              not a hold.

ASK_PHASES (Gemini's ruling, 2026-11-14): ASK, REQUEST, DISPATCH, REVIEW_REQUEST, TASK -
imported from ask_phases.py, the one constant every writer, the lint rule and this module
share.

RULE 4 - only Expected_From or the asker may ack/answer/close/reopen an ask (adversarial
review, 2026-11-14): checked against ask["expect_from"] and the ask's own placer whenever
expect_from is set. A pre-Rule-1 ask with no expect_from= has nobody to check an event's
sender against, so it keeps the backfill's original fail-open reading (any linked answer
counts) - that is the whole point of the backfill table's 1,728-row ANSWERED count, measured
with no identity check at all. ACK is narrower still: only Expected_From itself may ACK (the
asker acking its own ask is not in the spec), and only with eta=.

IDENTITY: every row is keyed by its OWN sheet Row_ID (board column A), not its BCB id=
(adversarial review, 2026-11-14: status_write matches column A, and 6,186 of 20,163 real BCB
ids are not themselves a Row_ID - keying by id= made 31% of rows permanently unwritable,
re-sent on every run). BCB id= is still what answers=/closes=/reopen=/hold= reference - the
link index is built on id=, the OUTPUT dict is keyed on Row_ID.

IDEMPOTENCE: a result's status_updated_utc only moves when (status, due_utc, closed_by)
actually changed from the previous cycle (adversarial review: 1,321 EXPIRED rows and the
"due in 24h" backfill due were both being restamped to `now` on every 5-minute run,
rewriting thousands of unchanged rows and never letting the 24h backfill due arrive).
reconcile_all() takes `previous` - the sheet's own current M/P/Q/R values, read back by the
caller - so a cycle whose answer hasn't changed carries the old due/updated forward instead
of inventing a new one.

REASON STRINGS for Closed_By, where it is not simply "the row id that closed it"
(blackboard-5e's rulings, 2026-11-14):
    backfill:expired>30d      first reconciler run, no due= (pre-R-STATUS row), age > 30 days
    backfill:expired7-30d     first reconciler run, no due= (pre-R-STATUS row), age 7..30 days
    reconciler:expired_past_due   a row with a real due= (Rule-1-compliant) that passed it
"""
import bisect
import re
from datetime import datetime, timedelta, timezone

from ask_phases import ASK_PHASES
import open_for_me

STATUSES = ("INFO", "OPEN", "ACKED", "ANSWERED", "CLOSED", "EXPIRED", "HELD")

# A hold whose reason names the owner goes on blackboard-5e's separate owner-decision
# list (her ruling, 2026-11-14) instead of the ordinary per-placer re-verify list.
OWNER_REASON_MARKERS = ("MR_SALAM", "OWNER", "PRODUCT_DECISION")

_FIELD_RE_CACHE = {}
_LIST_SEP = re.compile(r"[,;\s]+")

# Minute precision allowed (the fleet's own due= values are frequently
# "2026-11-14T15:00Z", 16 characters, no seconds) - adversarial review finding #3:
# the old >=19-character/full-HH:MM:SS rule rejected every real due= on the board.
_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$")


def cell(row, i):
    return str(row[i]) if len(row) > i and row[i] is not None else ""


def field(payload, name):
    """One BCB field's value, or None when absent. First occurrence wins -
    matches every other reader on this fleet (append.py's _conflicting_keys
    guards against a row ever disagreeing with itself)."""
    rx = _FIELD_RE_CACHE.get(name)
    if rx is None:
        rx = _FIELD_RE_CACHE[name] = re.compile(r"(?:^|\|)" + re.escape(name) + r"=([^|]*)")
    m = rx.search(payload)
    return m.group(1).strip() if m else None


def tokens(value):
    """A list-valued field (answers=/closes=/reopen=), split the same way
    open_for_me.tokens() splits clears=/supersedes= - comma, semicolon or
    whitespace separated, empties dropped. A batch close/answer is a list on
    the real board (923 of 8,943 answers= values; adversarial review #5), and
    treating it as one opaque string matched nothing."""
    return [t for t in (p.strip() for p in _LIST_SEP.split(value or "")) if t]


def parse_ts(value):
    """An aware datetime, or None when this cell is not an ISO instant (never a
    lexical string compare - scripts/_board_latest.py's own history: "Open" and
    "Friday, ..." both sort ahead of a real timestamp under one). Seconds are
    optional (see _TS_RE); a non-ISO form such as "2026-11-10 10:45 pm ET" or
    free text still correctly returns None rather than a guess."""
    text = str(value or "").strip()
    m = _TS_RE.match(text)
    if not m:
        return None
    year, month, day, hour, minute, second, frac, offset = m.groups()
    second = int(second) if second else 0
    micro = int((frac or "0").ljust(6, "0")[:6])
    if offset in (None, "Z"):
        tz = timezone.utc
    else:
        sign = 1 if offset[0] == "+" else -1
        body = offset[1:].replace(":", "")
        try:
            delta = timedelta(hours=int(body[:2]), minutes=int(body[2:4]))
        except ValueError:
            return None
        if delta >= timedelta(hours=24):
            return None
        tz = timezone(sign * delta)
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), second, micro, tz)
    except ValueError:
        return None
    return moment.astimezone(timezone.utc)


def parse_row(index, row):
    """One board row -> a dict of what this module needs, or None when the row
    carries no BCB id (nothing to reconcile - stays whatever Category already
    says, this module has no opinion on a non-BCB row). `row_id` is the
    sheet's OWN Row_ID (column A) - the identity status_write matches on,
    never the BCB id= (see the module docstring, IDENTITY)."""
    payload = cell(row, 5)
    rid = field(payload, "id")
    if not rid:
        return None
    row_id = cell(row, 0).strip()
    if not row_id:
        return None
    ts = parse_ts(cell(row, 1))
    phase = (field(payload, "phase") or "").strip().upper()
    source_tag = cell(row, 2)
    hold = field(payload, "hold")
    if hold and hold.strip().lower() == "none":
        hold = None   # a GO row explicitly declaring no hold is not a hold (finding #16)
    return {
        "index": index, "id": rid, "row_id": row_id, "ts": ts, "phase": phase,
        "payload": payload, "row": row, "source_tag": source_tag,
        "placer": source_tag or field(payload, "from"),
        "expect_from": field(payload, "expect_from"),
        "expect": field(payload, "expect"),
        "done_when": field(payload, "done_when"),
        "due": parse_ts(field(payload, "due")),
        "answers": field(payload, "answers"),
        "closes": field(payload, "closes"),
        "reopen": field(payload, "reopen"),
        "hold": hold,
        "eta": field(payload, "eta"),
        "pr": field(payload, "pr"),
    }


def parse_rows(rows):
    """parse_row() over every row, skipping the ones with no BCB id or no Row_ID."""
    out = []
    for i, row in enumerate(rows):
        parsed = parse_row(i, row)
        if parsed is not None:
            out.append(parsed)
    return out


def build_link_index(parsed_rows):
    """target BCB id -> list of (ts, parsed_row) events referencing it, one
    list per link type, each sorted oldest first. A row missing a timestamp is
    dropped from the index - it cannot be placed in the replay order, and
    guessing where it goes is exactly the defect class this fleet keeps
    finding (an unparseable timestamp is skipped, never silently sorted).
    List-valued fields (finding #5) register the row under EVERY token.

    hold= is deliberately NOT indexed here - blackboard-5e's ruling, 2026-11-14:
    HELD is the status of the row that carries hold=, never a link to some
    other ask (see compute_hold_status)."""
    index = {kind: {} for kind in ("answers", "closes", "reopen")}
    for r in parsed_rows:
        if r["ts"] is None:
            continue
        for kind in index:
            for target in tokens(r[kind]):
                index[kind].setdefault(target, []).append((r["ts"], r))
    for kind in index:
        for target in index[kind]:
            index[kind][target].sort(key=lambda pair: pair[0])
    # ACK events are a RESULT-free subset of "answers=": phase=ACK answers=<id>.
    # Kept separate from the generic "answers" index so a later real answer can
    # still be told apart from an ACK of the same ask.
    acks = {}
    for r in parsed_rows:
        if r["ts"] is not None and r["phase"] == "ACK":
            for target in tokens(r["answers"]):
                acks.setdefault(target, []).append((r["ts"], r))
    for target in acks:
        acks[target].sort(key=lambda pair: pair[0])
    index["acks"] = acks
    # "Answered" per the spec's own words - "Expected_From posts THE RESULT with
    # answers=" (finding #8): only a phase=RESULT row counts. An ASK/REQUEST/
    # DISPATCH/REVIEW_REQUEST/NOTE that merely carries answers= (a follow-up
    # question, a passing mention) is not an answer.
    answers_result = {}
    for target, events in index["answers"].items():
        filtered = [(ts, r) for ts, r in events if r["phase"] == "RESULT"]
        if filtered:
            answers_result[target] = filtered
    index["answers_result"] = answers_result
    return index


def _authorized(event_row, ask, require_expect_from_only=False):
    """Rule 4: only Expected_From or the asker may act on an ask - except a
    pre-Rule-1 ask with no expect_from= at all, which has no identity to check
    an event against and keeps the backfill's original fail-open reading (any
    linked event counts; this is what the spec's own 1,728-row ANSWERED
    backfill count was measured against). `require_expect_from_only` is for
    ACK: the spec never says the asker may ACK its own ask, only
    Expected_From."""
    if not ask["expect_from"]:
        return True
    placer = event_row["placer"]
    if not placer:
        return False
    if require_expect_from_only:
        return placer == ask["expect_from"]
    return placer in (ask["expect_from"], ask["placer"])


def compute_ask_status(ask, index, now, previous=None):
    """One ask-type row's (status, expected_from, expected_response, due_utc,
    closed_by, status_updated_utc). `ask` is a parsed row whose phase is in
    ASK_PHASES and that is not itself currently HELD (compute_hold_status() is
    checked first by reconcile_all() - see there). `index` is
    build_link_index()'s result over the WHOLE board - an ask can be
    acked/answered/closed/reopened by rows anywhere in time after it, not
    just immediately after. `previous` is this SAME row's last-written result
    (by Row_ID), or None on a row's first cycle - see the module docstring,
    IDEMPOTENCE."""
    status = "OPEN"
    closed_by = None
    updated = ask["ts"]
    due = ask["due"]
    reopened = False

    events = []
    for ts, r in index["acks"].get(ask["id"], []):
        events.append((ts, "ack", r))
    for ts, r in index["answers_result"].get(ask["id"], []):
        events.append((ts, "answer", r))
    for ts, r in index["closes"].get(ask["id"], []):
        events.append((ts, "close", r))
    for ts, r in index["reopen"].get(ask["id"], []):
        events.append((ts, "reopen", r))
    events.sort(key=lambda e: e[0])

    for ts, kind, r in events:
        if kind == "ack":
            if not _authorized(r, ask, require_expect_from_only=True) or not r["eta"]:
                continue   # Rule 4 + "an ACK without an eta is incomplete"
            status, updated = "ACKED", ts
        elif kind == "answer":
            if not _authorized(r, ask):
                continue
            status, closed_by, updated = "ANSWERED", r["id"], ts
        elif kind == "close":
            if not _authorized(r, ask):
                continue
            status, closed_by, updated = "CLOSED", r["id"], ts
        elif kind == "reopen":
            if not _authorized(r, ask):
                continue
            status, closed_by, updated, reopened = "OPEN", None, ts, True
            due = r["due"] or due   # a new due= on the reopen row, else filled in below

    if reopened and due is None:
        # Rule 3: "the ask returns to OPEN with a new due time" - a reopen with
        # no due= of its own is a fresh grace window, not a re-entry into the
        # backfill's age-bucket classification (finding #6: the ask's ORIGINAL
        # age, often 30+ days, was making a just-reopened ask EXPIRED on the
        # same run it was reopened).
        due = now + timedelta(hours=24)

    if status in ("OPEN", "ACKED"):
        if due is not None:
            if now > due:
                status, closed_by, updated = "EXPIRED", "reconciler:expired_past_due", now
        else:
            # No due= at all: a pre-R-STATUS row. Backfill by the ask's own age,
            # exactly the doc's three buckets (docs/BOARD-STATUS-FRAMEWORK.md,
            # "Backfill of the history").
            age_days = (now - ask["ts"]).total_seconds() / 86400.0
            if age_days > 30:
                status, closed_by, updated = "EXPIRED", "backfill:expired>30d", now
            elif age_days >= 7:
                status, closed_by, updated = "EXPIRED", "backfill:expired7-30d", now
            elif status == "OPEN":
                if previous and previous.get("due_utc"):
                    # Stamped once, carried forward (finding #7): recomputing
                    # now+24h on every 5-minute run meant it never arrived.
                    due = previous["due_utc"]
                else:
                    due = now + timedelta(hours=24)   # "due in 24 hours" - the backfill's own words

    if (previous and previous.get("status") == status and previous.get("due_utc") == due
            and previous.get("closed_by") == closed_by):
        # Finding #9 (generalised to every status, not only EXPIRED): R moves
        # only when M, P or Q actually changed - "When the reconciler last set
        # M to Q", not "when the reconciler last ran".
        updated = previous.get("status_updated_utc") or updated

    return {"status": status, "expected_from": ask["expect_from"],
            "expected_response": _expected_response(ask), "due_utc": due,
            "closed_by": closed_by, "status_updated_utc": updated}


def _sorted_timeline(parsed_rows, rows):
    """(sorted_timestamps, same-order raw rows) over every row that could
    possibly clear SOME hold, built once per reconcile_all() call.

    Measured against the real 21,227-row board: one open_for_me.hold_lifecycle
    call over the whole board took 58.7s (it was built for a single CLI check
    of a few currently-open asks, not a batch job re-running it per hold). Of
    those 21,227 rows, only 195 carry clears=, supersedes=, or a pr=+verdict
    that could ever matter to hold_lifecycle's own loop - every other row is
    skipped by it anyway (`if not is_canonical_bcb(...): continue`, then
    neither `names_it` nor the pr/verdict branch can fire without one of
    those fields). Pre-filtering to that candidate set, with MY OWN cached
    field() (not open_for_me's, which re-finds the pattern on every call with
    no cache), cuts 136 full-board hold_lifecycle calls from roughly 2 hours
    to well under a second, without changing which clearing row a hold finds -
    hold_lifecycle's own authorisation and head-comparison logic still runs
    unchanged on whatever is left after this filter. The timestamp bisect
    below additionally drops anything at or before the hold's own timestamp,
    exactly hold_lifecycle's own `when <= hold_when: continue` - a cut this
    makes already, done once instead of 136 times."""
    pairs = []
    for p in parsed_rows:
        if p["ts"] is None:
            continue
        payload = p["payload"]
        if field(payload, "clears") or field(payload, "supersedes"):
            pairs.append((p["ts"], rows[p["index"]]))
        elif field(payload, "pr") and (field(payload, "verdict") or "").upper() in ("GO", "MERGED"):
            pairs.append((p["ts"], rows[p["index"]]))
    pairs.sort(key=lambda pair: pair[0])
    return [ts for ts, _ in pairs], [r for _, r in pairs]


def compute_hold_status(row, timeline):
    """The HELD result for a row that carries hold=, or None when the hold is
    no longer in force (CLEARED or SUPERSEDED_LIKELY - open_for_me.hold_lifecycle's
    own words). None means: fall through to the normal phase-based classification,
    the hold no longer governs this row's status. `timeline` is
    _sorted_timeline()'s result."""
    stamps, later_rows = timeline
    cut = bisect.bisect_right(stamps, row["ts"]) if row["ts"] is not None else 0
    lifecycle, _why = open_for_me.hold_lifecycle(
        row["id"], row["pr"], row["ts"], row["placer"],
        open_for_me.head_of(row["payload"]), later_rows[cut:])
    if lifecycle != open_for_me.ACTIVE:
        return None
    return {"status": "HELD", "expected_from": row["placer"], "expected_response": None,
            "due_utc": None, "closed_by": "hold:%s" % row["hold"], "status_updated_utc": row["ts"]}


def is_owner_reason(reason):
    text = str(reason or "").upper()
    return any(marker in text for marker in OWNER_REASON_MARKERS)


def _expected_response(ask):
    expect, done_when = ask["expect"], ask["done_when"]
    if expect and done_when:
        return "%s: %s" % (expect, done_when)
    return expect or done_when or None


def reconcile_all(rows, now=None, previous=None):
    """Every row's M-R result, keyed by the row's OWN sheet Row_ID (column A -
    see the module docstring, IDENTITY), not its BCB id=. Checked in order
    per row:
    1. hold= present and still ACTIVE -> HELD (of the row itself).
    2. else an ask-type phase -> the ack/answer/close/reopen/expire machine.
    3. else INFO (never OPEN) - Rule 5: "Category stays a free label. Status
       comes only from column M."
    Rows with no BCB id or no Row_ID are skipped (nothing for this module to
    say). `previous`: {row_id: {"status", "due_utc", "closed_by",
    "status_updated_utc"}} - the sheet's own current M/P/Q/R values, parsed -
    for the idempotence rule (see IDEMPOTENCE); omit on a row's first cycle."""
    now = now or datetime.now(timezone.utc)
    previous = previous or {}
    parsed = parse_rows(rows)
    index = build_link_index(parsed)
    timeline = _sorted_timeline(parsed, rows)
    results = {}
    for r in parsed:
        prev = previous.get(r["row_id"])
        held = compute_hold_status(r, timeline) if r["hold"] else None
        if held is not None:
            results[r["row_id"]] = held
        elif r["phase"] in ASK_PHASES:
            results[r["row_id"]] = compute_ask_status(r, index, now, previous=prev)
        else:
            results[r["row_id"]] = {"status": "INFO", "expected_from": None,
                                    "expected_response": None, "due_utc": None,
                                    "closed_by": None, "status_updated_utc": r["ts"] or now}
    return results


def holds_for_reverification(rows, now=None, min_age_days=7):
    """Active holds at least min_age_days old, grouped by (placer, reason) -
    Rule 4: "every hold older than 7 days is re-verified by its placer, and
    the reconciler lists those holds daily." Split in two:
    "owner_decision" (the reason names the owner - blackboard-5e brings this
    list to him) and "other" (the placer's own re-verify list). Each group:
    {"placer", "reason", "holds": [{"id", "ts_iso"}, ...]}."""
    now = now or datetime.now(timezone.utc)
    parsed = parse_rows(rows)
    timeline = _sorted_timeline(parsed, rows)
    groups = {}
    for r in parsed:
        if not r["hold"] or r["ts"] is None:
            continue
        if compute_hold_status(r, timeline) is None:
            continue   # cleared or superseded - not a standing hold any more
        age_days = (now - r["ts"]).total_seconds() / 86400.0
        if age_days < min_age_days:
            continue
        key = (r["placer"], r["hold"])
        groups.setdefault(key, []).append(r)

    out = {"owner_decision": [], "other": []}
    for (placer, reason), group_rows in groups.items():
        bucket = "owner_decision" if is_owner_reason(reason) else "other"
        out[bucket].append({
            "placer": placer, "reason": reason,
            "holds": [{"id": r["id"], "ts_iso": r["ts"].strftime("%Y-%m-%dT%H:%M:%SZ")}
                     for r in sorted(group_rows, key=lambda r: r["ts"])],
        })
    return out


def newly_expired(old_statuses, new_results):
    """Row ids that are EXPIRED in new_results but were not EXPIRED (or did not
    exist) in old_statuses - the escalation list the compliance ring rings the
    owing agent from. Posting it is out of scope here; this only computes it.
    old_statuses: {row_id: previous Status string or None}."""
    out = []
    for rid, result in new_results.items():
        if result["status"] != "EXPIRED":
            continue
        if old_statuses.get(rid) == "EXPIRED":
            continue
        out.append({"id": rid, "expected_from": result["expected_from"],
                    "closed_by": result["closed_by"], "due_utc": result["due_utc"]})
    return out


def to_row_values(result):
    """One result dict -> the six M-R cell values, in column order, ready for
    a Sheets write. Timestamps go out as ISO-8601 UTC strings; everything
    sheet-shaped (strings only) lives here, not in reconcile_all()."""
    def iso(value):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""

    return [
        result["status"],
        result["expected_from"] or "",
        result["expected_response"] or "",
        iso(result["due_utc"]),
        result["closed_by"] or "",
        iso(result["status_updated_utc"]),
    ]

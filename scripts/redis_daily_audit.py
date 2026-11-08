#!/usr/bin/env python3
"""Copy one UTC day of every Redis agent lane onto Alpha DB, for the owner's audit. Default OFF.

HIS DIRECTION (2026-11-08, on the board as CLAUDE-CLI-REDIS-FIX-QUEUE-20261108T0125Z): Redis is the
communication channel, a lane per agent. Alpha DB records every prompt shown to the owner and his
reply, by thread id. The communication on Redis is copied into Alpha DB daily, so he can audit it
and read it.

WHAT ONE RUN DOES
    For one UTC day (yesterday, unless --day names another), read each lane fleet:inbox:<agent>.
    The agents are the principals in scripts/redis_acl.json plus claude-api. The read is XRANGE
    between that day's stream-id bounds, paged, and it keeps no cursor: a re-run reads the same
    day again and writes nothing it has already written. That is the same shape as
    scripts/bus_reconcile.py, and for the same reason. A cursor that advances on a partial read
    loses entries silently.

    Each entry becomes ONE board row, appended through scripts/bus.py the way fleet_agent.post
    appends: action=append, one transport attempt, the ten columns. It does not go through
    scripts/append.py. That path refuses a Source_Tag the roster does not know, and this tag
    (redis-audit) is the audit's, not an agent's.

    Row_ID is REDIS-AUDIT-<agent>-<stream id, with - written as _>. It is stable, so the run reads
    the day's existing REDIS-AUDIT ids first and skips them. A second run cannot duplicate a row
    on a board that cannot delete one.

    The row is Source_Tag redis-audit, Target_Surface owner, Action_Type AUDIT, Project FLEET.
    The payload is
        BCB|v=1|id=...|phase=AUDIT|lane=<agent>|stream_id=...|by=<by>|at=<at>|
        thread_id=<when the entry has one>|text=<the entry text>
    A long text goes through board_row.fit, which spills instead of cutting quietly, and the row
    is refused if it is still over 1500 characters.

    Call-bridge entries carry the owner's words. The board route already withholds those
    (redis_gov.WITHHELD): the text is replaced, and the ask id and a hash of the text stay, so a
    later check can confirm which ask it was without the words being on the board. The words are
    not written into the gist or into the spill either.

    The run ends with REDIS-AUDIT-SUMMARY-<YYYYMMDD>: per-lane counts, the first and last stream
    ids, and a verdict of COMPLETE, PARTIAL (with why) or UNKNOWN. Redis unreachable, or a day
    that came back empty, is UNKNOWN, never a completed audit of zero. Same rule as bus_reconcile.

WHAT IT NEVER DOES UNLESS ASKED
    REDIS_DAILY_AUDIT must be exactly the string on. Anything else, including true, 1, yes, ON,
    or the variable being absent, and the process connects to nothing and appends nothing.
    The Cloud Scheduler entry (06:10 UTC) is documented in cloud/bus-reconciler/deploy.sh and is
    not created by deploy.

    python scripts/redis_daily_audit.py                 # yesterday UTC, only if the flag is on
    python scripts/redis_daily_audit.py --day 2026-11-07
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_row                                                        # noqa: E402
import redis_gov                                                        # noqa: E402

FLAG = "REDIS_DAILY_AUDIT"
LANE = "fleet:inbox:%s"
SOURCE_TAG = "redis-audit"
TARGET = "owner"
ACTION = "AUDIT"
PROJECT = "FLEET"
CATEGORY = "DONE"
BOARD_TITLE = "Blackboard - Alpha DB"
PAGE = 200
COMPLETE, PARTIAL, UNKNOWN = "COMPLETE", "PARTIAL", "UNKNOWN"
EXIT = {COMPLETE: 0, PARTIAL: 1, UNKNOWN: 2}

# A principal's name, and nothing that could change which key fleet:inbox:<agent> is.
AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
STREAM_ID = re.compile(r"^[0-9]{1,20}-[0-9]{1,20}$")
# The stable id, parsed back so a re-run can recognise the rows that already belong to this day.
ENTRY_ROW = re.compile(
    r"^REDIS-AUDIT-(?P<agent>[A-Za-z0-9][A-Za-z0-9._-]*)-(?P<ms>[0-9]+)_(?P<seq>[0-9]+)$")
_UNSAFE = re.compile(r"[|\r\n\t\x00-\x1f]")


def enabled(environ=None) -> bool:
    """True only when the flag is exactly `on`. No other spelling turns the audit on."""
    environ = os.environ if environ is None else environ
    return environ.get(FLAG) == "on"


def acl_file() -> Path:
    """The access list beside this module: scripts/ in a checkout, /app in the image."""
    return Path(__file__).resolve().parent / "redis_acl.json"


def agents(acl=None) -> list:
    """Principals in the access list, plus claude-api, in a stable order.

    claude-api is named even though it is already a principal: the lane exists for that worker,
    and dropping it from this list because someone re-read the file must take an edit here.
    Aliases are not lanes. An unsafe name is not a lane either; the caller hears about it.
    """
    if acl is None:
        acl = json.loads(acl_file().read_text(encoding="utf-8"))
    names = set((acl or {}).get("principals") or {})
    names.add("claude-api")
    return sorted(names)


def day_bounds(day) -> tuple:
    """(start_ms, end_ms) for a UTC date. end_ms is the next midnight and is not in the day."""
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
    end = start + datetime.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def default_day(now):
    """The last complete UTC day. At the 06:10 tick that is yesterday, and it is yesterday at
    any other hour too: today is not finished, so it is not what a daily copy audits."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).date() - datetime.timedelta(days=1)


def summary_id(day) -> str:
    return "REDIS-AUDIT-SUMMARY-%s" % day.strftime("%Y%m%d")


def entry_id(agent, stream_id) -> str:
    """Stable Row_ID. The stream id's dash becomes an underscore so the id has one shape."""
    return "REDIS-AUDIT-%s-%s" % (agent, stream_id.replace("-", "_"))


def safe(value) -> str:
    """A payload value. BCB-1 has no escaping, so a pipe would start a new field."""
    if value is None:
        return ""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return _UNSAFE.sub(" ", text).strip()


def text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def fields_of(raw) -> dict:
    out = {}
    for key, value in (raw or {}).items():
        name = text_of(key)
        out[name] = text_of(value)
    return out


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def assert_row_limit(payload: str) -> str:
    """1500 characters is the board's row limit. fit() already refuses a silent cut; this is the
    audit's own check, so a row that reached the append was measured, not hoped."""
    if len(payload) > board_row.ROW_LIMIT:
        raise board_row.RowRejected(
            "audit row is %d characters, over the %d limit" % (len(payload), board_row.ROW_LIMIT))
    return payload


def fit_audit(header: str, body: str, key: str, spill):
    """board_row.fit, then the 1500-character limit.

    fit() cuts on a word and appends an ellipsis. That ellipsis is not part of the room it
    measured, so a spilled row can come back a few characters over the limit it was given. Ask
    again with that overrun taken off the limit. The spill and the visible mark stay; a row that
    is still over 1500 is refused.
    """
    limit = board_row.ROW_LIMIT
    payload, info = board_row.fit(header, body, key=key, spill=spill, write=False, limit=limit)
    for _ in range(4):
        if len(payload) <= board_row.ROW_LIMIT:
            return assert_row_limit(payload), info
        limit -= len(payload) - board_row.ROW_LIMIT
        if limit < 1:
            break
        payload, info = board_row.fit(header, body, key=key, spill=spill, write=False, limit=limit)
    return assert_row_limit(payload), info


def ids_on(rows, day) -> set:
    """Row_IDs already on the board for this day: entry rows whose stream time is in the day,
    and this day's summary. Anything else sharing the prefix belongs to another day."""
    start_ms, end_ms = day_bounds(day)
    wanted = summary_id(day)
    found = set()
    for row in rows or []:
        if isinstance(row, dict):
            rid = str(row.get("Row_ID") or row.get("row_id") or "")
        elif isinstance(row, (list, tuple)) and row:
            rid = str(row[0] or "")
        else:
            continue
        if rid == wanted:
            found.add(rid)
            continue
        match = ENTRY_ROW.match(rid)
        if not match:
            continue
        ms = int(match.group("ms"))
        if start_ms <= ms < end_ms:
            found.add(rid)
    return found


def existing_ids(env, day, reader=None) -> set:
    """The day's REDIS-AUDIT ids, from a filtered board read. since= is the day's start, so a
    row written later in the week is still found; the id, not the stamp, decides which day."""
    if reader is None:
        from bus import read_rows                                      # noqa: PLC0415
        reader = read_rows
    start_ms, _end_ms = day_bounds(day)
    since = (datetime.datetime.fromtimestamp(start_ms / 1000, datetime.timezone.utc)
             - datetime.timedelta(seconds=1))
    found = reader(env, since=since.strftime("%Y-%m-%dT%H:%M:%SZ"), match="REDIS-AUDIT")
    rows = found.get("rows") if isinstance(found, dict) else found
    return ids_on(rows or [], day)


def read_lane(conn, key, start_ms, end_ms, page_size):
    """Entries whose stream id falls in [start_ms, end_ms), oldest first.

    XRANGE is inclusive on both ends, so the upper bound is exclusive: `(<end_ms>-0`. The page
    advances by an exclusive minimum, which is a bound, not a cursor the next run stores.
    An entry at or after end_ms stops the read even if the server returned it. That is the day
    boundary, and it is why a page that straddles midnight cannot leak into the next day.
    Yields ('ok', stream_id, fields) or ('bad', raw_id, {}) for an id that is not <ms>-<seq>.
    """
    minimum = "%d-0" % start_ms
    maximum = "(%d-0" % end_ms
    while True:
        page = conn.xrange(key, min=minimum, max=maximum, count=page_size) or []
        if not page:
            return
        for entry_id, raw in page:
            sid = text_of(entry_id).strip()
            if not STREAM_ID.match(sid):
                yield ("bad", sid, {})
                continue
            ms = int(sid.split("-", 1)[0])
            if ms >= end_ms:
                return
            if ms < start_ms:
                continue
            yield ("ok", sid, fields_of(raw))
        if len(page) < page_size:
            return
        last = text_of(page[-1][0]).strip()
        nxt = "(" + last
        if not last or nxt == minimum:
            return
        minimum = nxt


def _ask(fields) -> str:
    ask = fields.get("ask")
    if ask is None or str(ask) == "":
        ask = fields.get("task_id")
    if ask is None or str(ask) == "":
        return ""
    return safe(ask)


def entry_body(fields) -> tuple:
    """(body after by= and at=, gist). Call-bridge text is withheld here, before fit(), so the
    words never enter the payload, the gist, or the spill fit() would store."""
    raw = text_of(fields.get("text"))
    by = safe(fields.get("by"))
    parts = []
    thread = fields.get("thread_id")
    if thread is not None and str(thread) != "":
        parts.append("thread_id=%s" % safe(thread))
    if by == redis_gov.CALL_BRIDGE_BY:  # withhold: the call's words stay off the board
        ask = _ask(fields)
        if ask:
            parts.append("ask=%s" % ask)
        parts.append("hash=%s" % text_hash(raw))
        parts.append("text=%s" % redis_gov.WITHHELD)
        gist = "redis audit withheld"
    else:
        shown = safe(raw)
        parts.append("text=%s" % shown)
        gist = shown[:180]
    return "|".join(parts), gist, by


def build_entry(agent, stream_id, fields, spill, clock) -> tuple:
    """(row, fit_info). The row is the ten columns fleet_agent.post appends."""
    rid = entry_id(agent, stream_id)
    body, gist, by = entry_body(fields)
    at = safe(fields.get("at"))
    header = "BCB|v=1|id=%s|phase=AUDIT|lane=%s|stream_id=%s|by=%s|at=%s|" % (
        rid, safe(agent), safe(stream_id), by, at)
    if by == redis_gov.CALL_BRIDGE_BY:
        gist = "redis audit %s %s withheld" % (safe(agent), safe(stream_id))
    elif not gist:
        gist = "redis audit %s %s" % (safe(agent), safe(stream_id))
    payload, info = fit_audit(header, body, rid, spill)
    stamp = clock().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row = [rid, stamp, SOURCE_TAG, TARGET, ACTION, payload, CATEGORY, PROJECT, gist[:180], ""]
    return row, info


def lane_token(lane) -> str:
    """One lane in the summary: agent:count:first..last, or agent:0, or agent:unmeasured."""
    agent = lane["agent"]
    if lane["count"] is None:
        return "%s:unmeasured" % agent
    if not lane["count"]:
        return "%s:0" % agent
    return "%s:%d:%s..%s" % (agent, lane["count"], lane["first"], lane["last"])


def build_summary(day, verdict, lanes, entries, why, spill, clock) -> tuple:
    rid = summary_id(day)
    shown = "unmeasured" if entries is None else str(entries)
    body = "day=%s|verdict=%s|entries=%s|lanes=%s|why=%s" % (
        day.isoformat(), verdict, shown,
        safe(",".join(lane_token(lane) for lane in lanes)), safe(why))
    header = "BCB|v=1|id=%s|phase=AUDIT|" % rid
    payload, info = fit_audit(header, body, rid, spill)
    gist = "Redis audit %s %s" % (day.isoformat(), verdict)
    stamp = clock().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row = [rid, stamp, SOURCE_TAG, TARGET, ACTION, payload, CATEGORY, PROJECT, gist[:180], ""]
    return row, info


def decide(conn_missing, lanes, append_errors) -> tuple:
    """COMPLETE only when every lane was read and at least one entry was there.

    Unreachable, or a day that was read and was empty, is UNKNOWN. A zero from a store that was
    not read is the lie bus_reconcile refuses, and an empty lane-set is the same lie: nobody
    copied the day. A lane that failed, an entry that could not be keyed, or an append that
    failed is PARTIAL, and the why names the exception type, never its message.
    """
    read_errors = [lane for lane in lanes if lane.get("error")]
    bad = [lane for lane in lanes if lane.get("bad")]
    measured = [lane for lane in lanes if lane["count"] is not None]
    if conn_missing or not measured:
        if conn_missing:
            why = "redis unreachable: nothing was copied, which is not a count of zero"
        else:
            kinds = ",".join(sorted({lane["error"] for lane in read_errors})) or "unread"
            why = ("the store could not be used (%s): nothing was copied, which is not a count "
                   "of zero" % kinds)
        return UNKNOWN, why
    entries = sum(lane["count"] for lane in measured)
    problems = []
    if read_errors:
        problems.append("read failed: " + ",".join(
            "%s:%s" % (lane["agent"], lane["error"]) for lane in read_errors))
    if bad:
        problems.append("unreadable stream ids: " + ",".join(
            "%s:%d" % (lane["agent"], lane["bad"]) for lane in bad))
    if append_errors:
        problems.append("append failed: " + ",".join(append_errors))
    if problems:
        return PARTIAL, "; ".join(problems)
    if entries == 0:
        return UNKNOWN, "no entries in the day: an empty read is not a complete audit of zero"
    return COMPLETE, ""


def _commit(info, spill, agent, append_errors) -> None:
    if not info.get("deferred") or spill is None:
        return
    try:
        board_row.commit(info, spill)
    except Exception as error:                                          # noqa: BLE001
        # The row may already be on the board. Name the failure; do not quote the text.
        append_errors.append("%s:spill:%s" % (agent, type(error).__name__))


def run(conn, day, *, agents, already, append, spill=None, page_size=PAGE, clock=None) -> dict:
    """Copy the day. `already` is the set of Row_IDs on the board, or None when that read failed.

    None appends nothing. Duplicating onto an append-only board is worse than a missed day, and
    a missed day is UNKNOWN, which is what the summary is for when the board read did work.
    """
    clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
    if already is None:
        return {"verdict": UNKNOWN, "day": day.isoformat(), "entries": None, "appended": 0,
                "skipped": 0, "lanes": [], "note": "existing REDIS-AUDIT ids could not be read; "
                "nothing was appended, so a re-run cannot duplicate",
                "recorded": False, "summary": summary_id(day)}

    lanes = []
    append_errors = []
    appended = 0
    skipped = 0
    if conn is None:
        lanes = [{"agent": agent, "count": None, "first": "", "last": "", "error": "unreachable",
                  "bad": 0} for agent in agents]
    else:
        start_ms, end_ms = day_bounds(day)
        for agent in agents:
            if not AGENT_NAME.match(agent):
                lanes.append({"agent": agent, "count": None, "first": "", "last": "",
                              "error": "unsafe-name", "bad": 0})
                continue
            seen, bad, error_name = [], 0, None
            try:
                for kind, sid, fields in read_lane(conn, LANE % agent, start_ms, end_ms, page_size):
                    if kind == "bad":
                        bad += 1
                        continue
                    seen.append((sid, fields))
            except Exception as error:                                  # noqa: BLE001
                error_name = type(error).__name__
            if error_name:
                lanes.append({"agent": agent, "count": None, "first": "", "last": "",
                              "error": error_name, "bad": bad})
                # A partial page is not this lane's count. Leave the entries for a re-run that
                # sees the whole day; copying them now and calling the lane unmeasured would put
                # rows on the board the summary cannot describe.
                continue
            first = seen[0][0] if seen else ""
            last = seen[-1][0] if seen else ""
            lanes.append({"agent": agent, "count": len(seen), "first": first, "last": last,
                          "error": None, "bad": bad})
            for sid, fields in seen:
                rid = entry_id(agent, sid)
                if rid in already:  # dedupe: a re-run must not append this row again
                    skipped += 1
                    continue
                try:
                    row, info = build_entry(agent, sid, fields, spill, clock)
                    append(row)
                except Exception as error:                              # noqa: BLE001
                    append_errors.append("%s:%s" % (agent, type(error).__name__))
                    continue
                already.add(rid)
                appended += 1
                _commit(info, spill, agent, append_errors)

    measured = [lane for lane in lanes if lane["count"] is not None]
    entries = None if not measured or any(lane["count"] is None for lane in lanes) else sum(
        lane["count"] for lane in measured)
    # decide() treats "not every lane measured" as UNKNOWN only when NOTHING was measured.
    # A mix of measured and failed lanes is PARTIAL, and the total is then unmeasured: publishing
    # a sum of the lanes that happened to answer would read as the day's count.
    if measured and any(lane["count"] is None for lane in lanes):
        entries = None
    verdict, why = decide(conn is None, lanes, append_errors)
    recorded = summary_id(day) in already
    if not recorded:
        try:
            row, info = build_summary(day, verdict, lanes, entries, why, spill, clock)
            append(row)
            appended += 1
            recorded = True
            _commit(info, spill, "summary", append_errors)
            if append_errors and verdict == COMPLETE:
                verdict, why = PARTIAL, "summary:spill"
        except Exception as error:                                      # noqa: BLE001
            recorded = False
            if verdict != UNKNOWN:
                verdict = PARTIAL
                extra = "summary:%s" % type(error).__name__
                why = "%s; %s" % (why, extra) if why else extra
    return {"verdict": verdict, "day": day.isoformat(), "entries": entries,
            "appended": appended, "skipped": skipped, "lanes": lanes, "note": why,
            "recorded": recorded, "summary": summary_id(day)}


def post_row(env, row, fetch) -> str:
    """The append fleet_agent.post makes: one bus.fetch, tries=1, never a replay."""
    return fetch(env["BUS_URL"], {
        "action": "append",
        "secret": env["BUS_SECRET"],
        "title": BOARD_TITLE,
        "sheetRow": row,
    }, tries=1)


def _parse_day(value, now):
    if not value:
        return default_day(now)
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as error:
        raise SystemExit("REFUSED: --day must be YYYY-MM-DD, a UTC day") from error


def main(argv=None, environ=None, connect=None, append=None, read_existing=None,
         spill=None, now=None, env=None) -> int:
    environ = os.environ if environ is None else environ
    if not enabled(environ):
        # Nothing else in this function runs. No parse of the day, no socket, no board read.
        print(json.dumps({"status": "disabled", "flag": FLAG,
                          "note": "REDIS_DAILY_AUDIT must be exactly on, or nothing runs"}))
        return 0

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", default=None, help="UTC day YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--page-size", type=int, default=PAGE)
    args = parser.parse_args(argv)
    if args.page_size < 1:
        print(json.dumps({"verdict": UNKNOWN, "note": "page size must be a positive integer"}))
        return EXIT[UNKNOWN]

    moment = now or datetime.datetime.now(datetime.timezone.utc)
    day = _parse_day(args.day, moment)
    names = agents()

    if connect is None:
        import redis_dual                                            # noqa: PLC0415
        settings = redis_dual.Settings(environ=environ)

        def connect(settings=settings):
            return redis_dual.client(settings, precheck=False)

    if env is None or append is None or read_existing is None:
        from bus import fetch, load_env                              # noqa: PLC0415
        if env is None:
            env = load_env()
        if append is None:
            def append(row, _env=env):
                return post_row(_env, row, fetch)
        if read_existing is None:
            def read_existing(board_env, board_day):
                return existing_ids(board_env, board_day)

    if spill is None:
        spill = board_row.store_spill()

    try:
        already = read_existing(env, day)
    except Exception:                                                   # noqa: BLE001
        already = None
    try:
        conn = connect()
    except Exception:                                                   # noqa: BLE001
        conn = None
    result = run(conn, day, agents=names, already=already, append=append, spill=spill,
                 page_size=args.page_size, clock=lambda: moment)
    print(json.dumps(result, sort_keys=True))
    return EXIT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())

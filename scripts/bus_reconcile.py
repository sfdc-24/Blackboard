"""Phase 1 of the bus roll-up: mirror a window of the board into Redis, then prove the two agree.

His instruction of 2026-10-05: *"Do not break what's already working, Build it in Redis, do parallel
checks and compare results first."* This is the parallel check. It changes nothing on the board, it
never serves a read to anybody, and its only product is a count.

WHAT IT DELIBERATELY IS NOT
It is not the relay. A continuous relay needs a stored watermark, and a watermark is the thing most
likely to be wrong: one that advances on a partial read loses rows SILENTLY, which our own waker
state taught us once already. So this is STATELESS - it mirrors a bounded window named on the command
line, keyed by Row_ID, and holds no cursor at all. There is nothing here for a cursor bug to live in.

RATIFIED BY GEMINI, architect lead, 2026-10-06 14:34:52Z: "Advancing a watermark on partial reads
guarantees silent data loss during a crash. Discard the cursor for the compare phase and design for
idempotent processing or atomic batch reads." Mirroring is already idempotent - the entry is keyed on
Row_ID, so a flapping read that returns the same rows twice mirrors them once. That is a RULING and
not my preference. A future continuous relay still needs a cursor design, and it needs its own.

THE PROPERTY THAT MATTERS MOST: AN EMPTY OR UNREACHABLE REDIS IS **UNKNOWN**, NEVER ZERO.
A reconciler written the obvious way returns "0 differences" when Redis holds nothing, and 0 is
exactly what the phase gate is looking for. That would turn "nobody looked" into "they agree" - the
same lie as a green tick over no assertions, and the reason `verdict` has three values and not two.
Codex's gate is zero divergence over a span; this refuses to contribute a PASS it did not earn.

CONTENTS NEVER LEAVE. A divergence is reported as the Row_ID and the NAMES of the columns that
differ. Never a value, on either side. The board carries his words and other people's, and a
comparison log is not a place to copy them.

    python scripts/bus_reconcile.py --since 2026-10-05T00:00:00Z            # compare only, read-only
    python scripts/bus_reconcile.py --since 2026-10-05T00:00:00Z --mirror   # backfill the window first
    python scripts/bus_reconcile.py --limit 200 --json                      # the newest 200 rows

Exit codes: 0 they agree, 1 they diverge, 2 the comparison could not be made (UNKNOWN).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redis_dual                                                        # noqa: E402

# The board's ten cells, in the order scripts/append.py writes them. Named so a divergence can say
# WHICH column differs without quoting what it holds.
COLUMNS = ("row_id", "ts", "source_tag", "target_surface", "action_type",
           "payload", "category", "project_tag", "gist", "subgist")
ROW_KEY = redis_dual.KEY_VERSION + "bus:row:%s"              # HASH, one per Row_ID
INDEX_KEY = redis_dual.KEY_VERSION + "bus:rowids"            # SET of every mirrored Row_ID
COMPARE_KEY = redis_dual.KEY_VERSION + "bus:compare"         # STREAM, one entry per run: counts only
AGREE, DIVERGE, UNKNOWN = "AGREE", "DIVERGE", "UNKNOWN"


def canonical(cell) -> str:
    """One string form for a cell, so a type difference is not reported as a content difference.

    The gateway returns a column typed however the Sheet felt about it - a timestamp as a date, a
    number as a float - and Redis stores strings. Comparing those raw reports divergence on every
    row and teaches everyone to ignore the reconciler."""
    if cell is None:
        return ""
    text = cell if isinstance(cell, str) else str(cell)
    return text.replace("\r\n", "\n").strip()


def row_digest(cells) -> str:
    """A digest of the whole row, so agreement is one comparison and contents stay out of the log."""
    joined = "\u001f".join(canonical(c) for c in cells)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def as_mapping(cells) -> dict:
    """A board row as the hash we mirror. Short rows are padded, long rows keep their extra cells
    under positional names rather than being silently truncated: a cell this does not know about is
    still a difference somebody may care about."""
    out = {name: canonical(cells[i] if i < len(cells) else "") for i, name in enumerate(COLUMNS)}
    for i in range(len(COLUMNS), len(cells)):
        out["col%d" % (i + 1)] = canonical(cells[i])
    return out


def board_rows(env, since=None, limit=None, reader=None) -> list:
    """The window of the board to compare, from the authoritative gateway.

    bus.read_rows returns a DICT, {"rows": [...]}, not a list. The first version of this iterated the
    return value directly, which yields the dict's KEYS - one string - so `isinstance(r, list)`
    rejected everything and every window came back empty, for every filter, forever.

    It took a Cloud Run execution to find, and the only reason it was found rather than believed is
    that an empty read is UNKNOWN here and not zero divergence. Written the obvious way this bug
    would have reported AGREE on a comparison it never made, on every run, and the phase gate would
    have opened on it."""
    if reader is not None:
        found = reader(since=since, limit=limit)
    else:
        from bus import read_rows                                        # noqa: PLC0415
        found = read_rows(env, since=since, limit=limit)
    if isinstance(found, dict):
        found = found.get("rows") or []
    return [r for r in found if isinstance(r, list)]


def mirror(conn, rows) -> dict:
    """Write the window into Redis, keyed by Row_ID. Idempotent: the same row mirrors once.

    Keyed by Row_ID and not by arrival, so a flapping read that returns the same rows twice - which
    this gateway does - writes them once instead of inventing duplicates.

    REPLACED, NOT MERGED. Copilot, PR 323: HSET merges fields, so mirroring a row that had a
    non-empty col11 and then mirroring its shortened version left the old col11 behind, and every
    later comparison kept reporting a difference that the backfill had already fixed. A stale field
    nobody can clear is a permanent false DIVERGE. The DELETE and the HSET go in one transaction so
    a reader never sees the gap between them."""
    wrote, skipped = 0, 0
    for cells in rows:
        row_id = canonical(cells[0] if cells else "")
        if not row_id:
            skipped += 1                       # a row with no id cannot be compared or mirrored
            continue
        key = ROW_KEY % row_id
        mapping = as_mapping(cells)
        pipe = conn.pipeline(transaction=True) if hasattr(conn, "pipeline") else None
        if pipe is None:                       # a stub without pipelines, in tests
            conn.delete(key)
            conn.hset(key, mapping=mapping)
            conn.sadd(INDEX_KEY, row_id)
        else:
            pipe.delete(key)
            pipe.hset(key, mapping=mapping)
            pipe.sadd(INDEX_KEY, row_id)
            pipe.execute()
        wrote += 1
    return {"mirrored": wrote, "unmirrorable": skipped}


def read_ts(cell):
    """An aware datetime from a board timestamp, or None when the cell is not one.

    PARSED, NOT COMPARED AS TEXT. The first version of this fence said ISO-8601 stamps "sort
    correctly as text, which is why no parsing happens here". They do not, and Codex reproduced it
    on its first pass:

        "2026-10-05T10:00:00.500Z" < "2026-10-05T10:00:00Z"   is True

    because "." sorts below "Z". A row stamped mid-second therefore fell OUTSIDE a window that
    plainly contains it, and the comparison returned AGREE where the answer was DIVERGE. Not a
    corner case either: scripts/append.py stamps microseconds, so most rows this fleet writes look
    like 03:32:22.588753Z. The reasoning in that docstring WAS the defect - avoiding a parser so as
    not to disagree with the gateway produced a comparison that disagreed with arithmetic."""
    text = canonical(cell)
    if len(text) < 19 or text[4:5] != "-":
        return None
    try:
        value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def window_bounds(board) -> tuple:
    """(oldest, newest, unreadable): two datetimes and a count, or (None, None, n).

    `unreadable` counts board rows whose own stamp would not parse. Those do more than fail to
    contribute a bound - they mean the window's edges are not fully known, so the caller degrades
    its verdict rather than fencing against a boundary it cannot trust."""
    stamps, bad = [], 0
    for mapped in board.values():
        when = read_ts(mapped.get("ts"))
        if when is None:
            bad += 1
        else:
            stamps.append(when)
    if not stamps:
        return (None, None, bad)
    return (min(stamps), max(stamps), bad)


def compare(conn, rows) -> dict:
    """Diff the board window against Redis. Counts, Row_IDs and column NAMES; never a value."""
    board, duplicates = {}, []
    no_id = 0
    for cells in rows:
        row_id = canonical(cells[0] if cells else "")
        if not row_id:
            no_id += 1
            continue
        if row_id in board:
            # An append-only board should never hold a Row_ID twice. This is a finding in its own
            # right, not an error in the comparison, so it is counted and named rather than hidden.
            duplicates.append(row_id)
            continue
        board[row_id] = as_mapping(cells)

    if not board:
        # An empty window is UNKNOWN and needs no connection to say so. The first execution of this
        # job spent a connection reaching SMEMBERS to learn that the board had been quiet.
        return {"verdict": UNKNOWN, "checked": 0, "agreed": 0, "missing_from_redis": 0,
                "differing": 0, "extra_in_redis": 0, "outside_the_window": 0,
                "unplaceable_in_redis": 0, "boundary_ties": 0,
                "board_rows_with_unreadable_ts": 0, "duplicate_row_ids": len(duplicates),
                "rows_without_an_id": no_id, "window_oldest": "", "window_newest": "",
                "columns_that_differ": {},
                "missing_ids": [], "differing_ids": [], "extra_ids": [], "duplicate_ids": [],
                "note": "no board rows in the window: nothing to compare, which is not agreement"}

    missing, differing, columns = [], [], {}
    for row_id, mapped in board.items():
        stored = conn.hgetall(ROW_KEY % row_id) or {}
        if not stored:
            missing.append(row_id)
            continue
        changed = sorted(name for name in set(mapped) | set(stored)
                         if canonical(mapped.get(name)) != canonical(stored.get(name)))
        if changed:
            differing.append(row_id)
            for name in changed:
                columns[name] = columns.get(name, 0) + 1

    # EXTRA MEANS EXTRA INSIDE THE WINDOW, NOT EVERYTHING OLDER THAN IT.
    #
    # Copilot, PR 323: bus:rowids holds every row ever mirrored, while `board` holds only the rows
    # the window asked for. So after mirroring two rows, a later `--limit 1` comparison called the
    # older row "extra" and returned DIVERGE while the two stores agreed perfectly. Advancing
    # `--since` did the same. A reconciler that reports divergence for narrowing its own question
    # teaches everyone to ignore it - which is the failure mode this file was written against.
    #
    # The fence is the window the BOARD actually returned: its oldest and newest timestamps,
    # PARSED - see read_ts for why comparing them as text produced a false AGREE. A stored id
    # outside that span was never in scope. An id whose own timestamp cannot be placed is not waved
    # through either: "I could not tell" is not "they agree".
    #
    # AND A TIE AT THE EDGE IS AMBIGUOUS, NOT EXTRA. Codex, reviewing 314bead: with `--limit 1` a
    # healthy older row sharing the selected row's timestamp was reported as extra and the verdict
    # as DIVERGE. It is neither. `limit` selects the n most recent ROWS, so when several share the
    # boundary instant the gateway's choice among them is arbitrary and this comparison cannot know
    # which it meant. Counted and degraded to UNKNOWN rather than guessed in either direction.
    oldest, newest, board_unreadable = window_bounds(board)
    stored_ids = set(conn.smembers(INDEX_KEY) or set())
    extra, out_of_window, unplaceable, boundary_ties = [], 0, 0, 0
    for row_id in sorted(stored_ids - set(board)):
        when = read_ts((conn.hgetall(ROW_KEY % row_id) or {}).get("ts"))
        if when is None:
            # Empty OR malformed. The old code only caught empty, so "not-a-timestamp" compared as
            # text, fell outside the window and produced a clean AGREE.
            unplaceable += 1
        elif oldest is None or newest is None:
            unplaceable += 1
        elif when == oldest or when == newest:
            boundary_ties += 1
        elif oldest < when < newest:
            extra.append(row_id)
        else:
            out_of_window += 1

    checked = len(board)
    agreed = checked - len(missing) - len(differing)
    # UNKNOWN, not AGREE: nothing in Redis is not agreement, it is an unasked question. The phase gate
    # is looking for zero, and zero-because-empty is the lie this exists to refuse.
    if checked == 0:
        verdict = UNKNOWN
    elif len(missing) == checked:
        verdict = UNKNOWN
    elif missing or differing or extra or duplicates:
        verdict = DIVERGE
    elif (no_id or unplaceable or boundary_ties or board_unreadable
          or oldest is None or newest is None):
        # AGREEMENT ON A WINDOW THAT WAS NOT WHOLLY COMPARED IS NOT AGREEMENT.
        #
        # Copilot, PR 323: rows without an id were counted and skipped but did not touch the
        # verdict, so one matching row beside one id-less row returned AGREE with a count of 1 - a
        # clean answer about a window nobody fully examined. DIVERGE still wins when a real
        # difference was found; it is only the otherwise-clean case that degrades to UNKNOWN.
        #
        # Codex added two more ways to be unsure, both of which used to read as AGREE: a stored row
        # whose stamp is malformed rather than empty, and a board row whose own stamp will not
        # parse - which leaves the window's own edges unknown.
        verdict = UNKNOWN
    else:
        verdict = AGREE
    return {
        "verdict": verdict,
        "checked": checked,
        "agreed": agreed,
        "missing_from_redis": len(missing),
        "differing": len(differing),
        "extra_in_redis": len(extra),
        "outside_the_window": out_of_window,
        "unplaceable_in_redis": unplaceable,
        "boundary_ties": boundary_ties,
        "board_rows_with_unreadable_ts": board_unreadable,
        "duplicate_row_ids": len(duplicates),
        "rows_without_an_id": no_id,
        "window_oldest": oldest.isoformat() if oldest else "",
        "window_newest": newest.isoformat() if newest else "",
        "columns_that_differ": columns,
        "missing_ids": missing[:20],
        "differing_ids": differing[:20],
        "extra_ids": extra[:20],
        "duplicate_ids": duplicates[:20],
    }


def record(conn, result, window) -> None:
    """Append the run to bus:compare. Counts and the verdict; no ids, no columns, no contents.

    IT CARRIES ITS OWN PROVENANCE. Codex, reviewing 314bead: run() returned `measured` and
    `after_backfill`, and the DURABLE entry carried neither - so a gate reading this stream could
    not tell a compare-only measurement from a run that had repaired the store first. Codex's
    answer to the question was explicit: for a zero-divergence span it wants compare-only runs. It
    cannot have them unless the stream says which these were.

    `measured` and the caveat counts are therefore part of the entry, not just the return value. A
    verdict whose provenance lives only in a log line the gate does not read is a verdict the gate
    has to take on trust."""
    entry = {
        "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": result["verdict"],
        # compare_only or pre_backfill. A gate selecting a span filters on this.
        "measured": result.get("measured", "unknown"),
        "checked": result["checked"],
        "agreed": result["agreed"],
        "missing": result["missing_from_redis"],
        "differing": result["differing"],
        "extra": result["extra_in_redis"],
        "duplicates": result["duplicate_row_ids"],
        # Why a verdict might be UNKNOWN, so a reader can tell "nothing to compare" from "could not
        # place a row" without going back to the logs.
        "no_id": result.get("rows_without_an_id", 0),
        "unplaceable": result.get("unplaceable_in_redis", 0),
        "boundary_ties": result.get("boundary_ties", 0),
        "bad_board_ts": result.get("board_rows_with_unreadable_ts", 0),
        "window": window,
    }
    after = result.get("after_backfill") or {}
    if after:
        # Named so it can never be mistaken for the run's own verdict: this is the state the run
        # LEFT, and the gate must not count it as the state it FOUND.
        entry["after_backfill_verdict"] = after.get("verdict", "")
        entry["after_backfill_agreed"] = after.get("agreed", "")
    conn.xadd(COMPARE_KEY, entry)


def run(conn, rows, do_mirror=False, window="") -> dict:
    """Mirror and compare, turning any store failure into UNKNOWN rather than a traceback.

    The first execution of this job died with a redis AuthenticationError stack trace. A stack trace
    in a scheduled job's log is a failure nobody reads; UNKNOWN with one line of reason is one
    somebody acts on - and UNKNOWN is the honest verdict either way, because nothing was compared.
    The reason names the EXCEPTION TYPE and never its message: a client's error text can quote what
    it was sent.

    THE PRE-REPAIR VERDICT IS RECORDED SEPARATELY, AND IT IS THE MEASUREMENT.

    Copilot, PR 323: the job defaulted to mirroring and deploy.sh set RECONCILE_MIRROR=1, so run()
    overwrote Redis and then compared - against the rows it had just written. An empty or badly
    stale store therefore produced an AGREE record with no trace of what it had been. Those records
    cannot establish zero divergence over a span, which is the entire gate Codex set, because a
    backfill followed by a comparison always agrees.

    So a mirroring run now compares FIRST, keeps that verdict as `verdict` - the honest one - and
    reports the post-backfill state beside it as `after_backfill`. A run with do_mirror=False is a
    pure measurement and has no second comparison to make. `measured` says which kind of run it was,
    so nobody downstream has to infer it.
    """
    result = {}
    try:
        if do_mirror:
            before = compare(conn, rows)             # what the stores looked like BEFORE any repair
            backfill = mirror(conn, rows)
            after = compare(conn, rows)
            result.update(before)
            result.update(backfill)
            result["measured"] = "pre_backfill"
            result["after_backfill"] = {k: after[k] for k in
                                        ("verdict", "checked", "agreed", "missing_from_redis",
                                         "differing", "extra_in_redis") if k in after}
        else:
            result.update(compare(conn, rows))
            result["measured"] = "compare_only"
    except Exception as error:
        return {"verdict": UNKNOWN, "checked": 0, "agreed": 0, "missing_from_redis": 0,
                "differing": 0, "extra_in_redis": 0, "duplicate_row_ids": 0,
                "rows_without_an_id": 0, "boundary_ties": 0,
                "board_rows_with_unreadable_ts": 0, "outside_the_window": 0,
                "unplaceable_in_redis": 0, "columns_that_differ": {}, "missing_ids": [],
                "differing_ids": [], "extra_ids": [], "duplicate_ids": [], "recorded": False,
                "note": "the store could not be used (%s): nothing was compared, which is not "
                        "agreement" % type(error).__name__}
    try:
        record(conn, result, window)
        result["recorded"] = True
    except Exception:
        # A failure to record the run is not a failure of the run, and must not turn a real verdict
        # into an exception the caller reads as UNKNOWN.
        result["recorded"] = False
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default=None, help="ISO instant; compare rows newer than it")
    parser.add_argument("--limit", type=int, default=None, help="the newest N rows instead")
    parser.add_argument("--mirror", action="store_true",
                        help="write the window into Redis first (idempotent, keyed by Row_ID)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.since and not args.limit:
        print("REFUSED: name a window with --since or --limit. An unbounded read of this board has "
              "timed out before and left an append looking like a failure.")
        return 2

    settings = redis_dual.Settings()
    conn = redis_dual.client(settings)
    if conn is None:
        report = redis_dual.status(settings)
        print("UNKNOWN: no Redis connection, so nothing was compared. This is NOT zero divergence.")
        for key in ("enabled", "host", "would_attempt_connection", "reachable",
                    "redis_library_installed", "auth_string_found"):
            print("  %-26s %s" % (key, report.get(key)))
        return 2

    from bus import load_env                                             # noqa: PLC0415
    rows = board_rows(load_env(), since=args.since, limit=args.limit)
    window = "since=%s" % args.since if args.since else "limit=%d" % args.limit
    result = run(conn, rows, do_mirror=args.mirror, window=window)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("%s: %d checked, %d agreed" % (result["verdict"], result["checked"], result["agreed"]))
        for name in ("missing_from_redis", "differing", "extra_in_redis", "duplicate_row_ids",
                     "rows_without_an_id"):
            if result.get(name):
                print("  %-22s %d" % (name, result[name]))
        if result.get("columns_that_differ"):
            print("  columns that differ    %s" % ", ".join(
                "%s x%d" % kv for kv in sorted(result["columns_that_differ"].items())))
        if not result.get("recorded"):
            print("  (the run itself could not be written to %s)" % COMPARE_KEY)
    return {AGREE: 0, DIVERGE: 1, UNKNOWN: 2}[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())

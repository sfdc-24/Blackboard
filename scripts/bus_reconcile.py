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
ROW_KEY = "bus:row:%s"              # HASH, one per Row_ID
INDEX_KEY = "bus:rowids"            # SET of every mirrored Row_ID
COMPARE_KEY = "bus:compare"         # STREAM, one entry per run: counts only
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
    this gateway does - writes them once instead of inventing duplicates."""
    wrote, skipped = 0, 0
    for cells in rows:
        row_id = canonical(cells[0] if cells else "")
        if not row_id:
            skipped += 1                       # a row with no id cannot be compared or mirrored
            continue
        conn.hset(ROW_KEY % row_id, mapping=as_mapping(cells))
        conn.sadd(INDEX_KEY, row_id)
        wrote += 1
    return {"mirrored": wrote, "unmirrorable": skipped}


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
                "differing": 0, "extra_in_redis": 0, "duplicate_row_ids": len(duplicates),
                "rows_without_an_id": no_id, "columns_that_differ": {},
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

    extra = sorted(set(conn.smembers(INDEX_KEY) or set()) - set(board))

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
    else:
        verdict = AGREE
    return {
        "verdict": verdict,
        "checked": checked,
        "agreed": agreed,
        "missing_from_redis": len(missing),
        "differing": len(differing),
        "extra_in_redis": len(extra),
        "duplicate_row_ids": len(duplicates),
        "rows_without_an_id": no_id,
        "columns_that_differ": columns,
        "missing_ids": missing[:20],
        "differing_ids": differing[:20],
        "extra_ids": extra[:20],
        "duplicate_ids": duplicates[:20],
    }


def record(conn, result, window) -> None:
    """Append the run to bus:compare. Counts and the verdict; no ids, no columns, no contents."""
    conn.xadd(COMPARE_KEY, {
        "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": result["verdict"],
        "checked": result["checked"],
        "agreed": result["agreed"],
        "missing": result["missing_from_redis"],
        "differing": result["differing"],
        "extra": result["extra_in_redis"],
        "duplicates": result["duplicate_row_ids"],
        "window": window,
    })


def run(conn, rows, do_mirror=False, window="") -> dict:
    """Mirror and compare, turning any store failure into UNKNOWN rather than a traceback.

    The first execution of this job died with a redis AuthenticationError stack trace. A stack trace
    in a scheduled job's log is a failure nobody reads; UNKNOWN with one line of reason is one
    somebody acts on - and UNKNOWN is the honest verdict either way, because nothing was compared.
    The reason names the EXCEPTION TYPE and never its message: a client's error text can quote what
    it was sent."""
    result = {}
    try:
        if do_mirror:
            result.update(mirror(conn, rows))
        result.update(compare(conn, rows))
    except Exception as error:
        return {"verdict": UNKNOWN, "checked": 0, "agreed": 0, "missing_from_redis": 0,
                "differing": 0, "extra_in_redis": 0, "duplicate_row_ids": 0,
                "rows_without_an_id": 0, "columns_that_differ": {}, "missing_ids": [],
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

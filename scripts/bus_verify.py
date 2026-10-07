"""Verify the board against its Redis mirror: coverage from positions, content from rows.

WHY THIS FILE EXISTS, AND WHAT IT REPLACES
    Two previous attempts, each right about half of it.

    bus_reconcile.py compared each board row against its own mirror - the right CONTENT comparison -
    inside a time window it had to infer, which Codex refused three times because the sheet's row
    order is not its timestamp order and no predicate over the record could tell genuine agreement
    from a window that missed a row.

    bus_ranges.py enumerated rows by closed physical range - the right COVERAGE - and then compared
    a chunk digest against a digest WRITTEN FROM THE BOARD, so it never read the Redis mirror at
    all. Codex broke it in one move: sheet row NEW, mirror still OLD, verdict AGREE, gate_eligible
    true. It proved the board had not changed since the last digest and nothing whatever about
    Redis. I had called that predicate the deliverable; it was sound about the wrong two things.

    So: positions for coverage, row mirrors for content, and every caveat in the record. Both
    halves, which is what the gate needed all along.

THE PREDICATE
    gate_eligible() is the expression and nothing else. A run counts for a zero-divergence span
    only when the record says it walked the WHOLE board, compared every data row against its own
    mirror, found no difference of any kind, could not have been reading a shifting sheet, and
    WROTE NOTHING. Codex's list, implemented.

WHAT IS GONE BY CONSTRUCTION, not by guard
    No timestamp is parsed, so fractional seconds, naive stamps, lexical ordering and boundary ties
    cannot reach a verdict. No cross-row digest, so the separator collisions Codex found
    (['a','b'] versus ['a\\u001fb']) cannot exist. No inferred window, so there is no ceiling to get
    wrong. Extras are unambiguous because the walk enumerates the entire board rather than a slice.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redis_dual                                                       # noqa: E402

# The SAME keyspace the mirror is written into. Not imported from bus_reconcile on purpose - that
# module is legacy and will be deleted once the job stops calling it - but tests/test_bus_verify.py
# asserts the two agree, so they cannot drift apart while both exist.
ROW_KEY = redis_dual.KEY_VERSION + "bus:row:%s"
INDEX_KEY = redis_dual.KEY_VERSION + "bus:rowids"
VERIFY_LOG = redis_dual.KEY_VERSION + "bus:verify"

COLUMNS = ("row_id", "ts", "source_tag", "target_surface", "action_type",
           "payload", "category", "project_tag", "gist", "subgist")

AGREE, DIVERGE, UNKNOWN, NO_SAMPLE = "AGREE", "DIVERGE", "UNKNOWN", "NO_SAMPLE"
DEFAULT_CHUNK = 200
RECHECK_POSITIONS = 8


def canonical(cell) -> str:
    """One string form for a cell, so a type difference is not a content difference."""
    if cell is None:
        return ""
    text = cell if isinstance(cell, str) else str(cell)
    return text.replace("\r\n", "\n").strip()


def as_mapping(cells) -> dict:
    """A board row as the hash the mirror holds. Extra cells keep positional names rather than
    being truncated: a cell this does not know about is still a difference somebody may care
    about."""
    out = {name: canonical(cells[i] if i < len(cells) else "") for i, name in enumerate(COLUMNS)}
    for i in range(len(COLUMNS), len(cells)):
        out["col%d" % (i + 1)] = canonical(cells[i])
    return out


def looks_like_header(cells) -> bool:
    """Physical row 1 is the header. Identified by content rather than assumed by position, so a
    sheet that has lost its header is a finding instead of an off-by-one."""
    return bool(cells) and canonical(cells[0]).lower() in ("row_id", "rowid", "row id")


# WHAT THE SHEET ACTUALLY CALLS ITS COLUMNS, which is not what this code calls them.
#
# COLUMNS above are INTERNAL names used for the mirror's hash fields. Two of them were never the
# sheet's labels: the sheet says "Timestamp" where the code says `ts`, and "Sub-Gist" where the code
# says `subgist`. The first live run reported header_matches_schema=False against a perfectly healthy
# board - my checker comparing one vocabulary to the other, and normalising spaces but not hyphens.
#
# It mattered more than a wrong flag: _verdict degrades to UNKNOWN on a header it does not
# recognise, so once the mirror was populated EVERY run would have been ineligible, forever, for a
# schema that had never drifted. No unit test could find it - the fixture used the internal names,
# so the fixture agreed with the bug.
HEADER_LABELS = ("row_id", "timestamp", "source_tag", "target_surface", "action_type",
                 "payload", "category", "project_tag", "gist", "sub_gist")


def normalise_label(cell) -> str:
    """A header cell as a comparable token: lowercased, spaces AND hyphens to underscores."""
    return canonical(cell).lower().replace(" ", "_").replace("-", "_")


def header_matches(cells) -> bool:
    """Whether the header names the columns the board is expected to have, IN ORDER.

    Checked against the sheet's own labels, because a schema that has really drifted makes every
    comparison below it meaningless - and a checker that cries drift at a healthy board makes every
    run ineligible, which is the same damage from the other direction."""
    got = [normalise_label(c) for c in cells[:len(HEADER_LABELS)]]
    return got == list(HEADER_LABELS)


def check_reply(reply, first, count) -> str:
    """"" when the gateway answered the question asked, else why it did not.

    Codex: read_range required `start` to be PRESENT and never that it EQUAL the request, so a
    gateway returning the header for both row 1 and row 2 - echoing start=1 each time - produced a
    record claiming coverage 1..2 while row 2 was never returned. The echoed values are the only
    evidence the right rows came back, so they are checked rather than trusted."""
    if not isinstance(reply, dict):
        return "the reply was not an object"
    if reply.get("start") != first:
        return "asked for start=%s and the gateway echoed start=%r" % (first, reply.get("start"))
    rows = [r for r in (reply.get("rows") or []) if r]
    if reply.get("count") not in (None, len(rows)):
        return "the gateway echoed count=%r and sent %d row(s)" % (reply.get("count"), len(rows))
    if len(rows) != count:
        return "asked for %d row(s) at %d and received %d" % (count, first, len(rows))
    return ""


def chunk_bounds(total, size=DEFAULT_CHUNK, start=1):
    """[(first, count)] covering start..total inclusive. Closed and contiguous by construction."""
    if total < start or size < 1:
        return []
    out, at = [], start
    while at <= total:
        n = min(size, total - at + 1)
        out.append((at, n))
        at += n
    return out


def recheck_points(total, how_many=RECHECK_POSITIONS):
    """Positions to re-read at the END of a walk, to notice the sheet moving underneath it.

    Gemini said to freeze the ceiling; Codex pointed out that a frozen ceiling is only a valid
    PREFIX claim if positions below it cannot shift, and a middle insert or delete shifts them. A
    Sheet has no revision token we can read through this gateway and no lock, so the honest
    substitute is to look again: remember the Row_ID seen at a spread of positions and check they
    are still there afterwards. It cannot prove stability - nothing available here can - but it
    catches a shift rather than claiming one did not happen."""
    if total < 1:
        return []
    if total <= how_many:
        return list(range(1, total + 1))
    step = (total - 1) / float(how_many - 1)
    return sorted({int(round(1 + i * step)) for i in range(how_many)})


def verify(conn, reader, total_at_start, size=DEFAULT_CHUNK, mirror=False, total_at_end=None,
           remember=()) -> dict:
    """Walk the whole board, compare every data row against its own mirror, report every caveat.

    `reader(first, count)` returns the gateway's reply dict for that closed range.
    `remember` is the positions whose Row_ID to record as the walk passes them, so a caller can
    look at those positions again afterwards and notice the sheet moving underneath it. The
    comparison is done by `apply_recheck`, not here, because this function must not read the board
    a second time - a walk that re-reads is a walk with two opinions about what it saw.
    """
    record = {
        "verdict": UNKNOWN, "frozen_total": total_at_start, "total_at_end": total_at_end,
        "covered_from": 0, "covered_to": 0,
        "header_present": False, "header_matches_schema": False,
        "data_rows": 0, "unique_row_ids": 0,
        "matched": 0, "missing_from_redis": 0, "differing": 0, "extra_in_redis": 0,
        "duplicate_row_ids": 0, "rows_without_an_id": 0,
        "chunks_total": 0, "chunks_read": 0, "read_failures": 0,
        "positions_rechecked": 0, "positions_shifted": 0, "observed_positions": {},
        "writes_performed": 0, "chunk_size": size,
        "columns_that_differ": {}, "missing_ids": [], "differing_ids": [], "extra_ids": [],
        "duplicate_ids": [], "read_failure_reasons": [],
    }

    bounds = chunk_bounds(total_at_start, size)
    record["chunks_total"] = len(bounds)
    if not bounds:
        record["verdict"] = NO_SAMPLE
        record["note"] = "the board reported no rows: nothing was compared, which is not agreement"
        return record

    seen, positions = {}, {}
    wanted = set(remember or ())
    covered_to = 0
    for first, count in bounds:
        try:
            reply = reader(first, count)
        except Exception as error:
            record["read_failures"] += 1
            record["read_failure_reasons"].append("%d:%s" % (first, type(error).__name__))
            break
        why = check_reply(reply, first, count)
        if why:
            record["read_failures"] += 1
            record["read_failure_reasons"].append("%d:%s" % (first, why))
            break
        record["chunks_read"] += 1
        rows = [r for r in (reply.get("rows") or []) if r]

        for offset, cells in enumerate(rows):
            at = first + offset
            if at == 1:
                record["header_present"] = looks_like_header(cells)
                record["header_matches_schema"] = header_matches(cells)
                continue
            row_id = canonical(cells[0] if cells else "")
            positions[at] = row_id
            if at in wanted:
                record["observed_positions"][str(at)] = row_id
            if not row_id:
                record["rows_without_an_id"] += 1
                continue
            record["data_rows"] += 1
            if row_id in seen:
                record["duplicate_row_ids"] += 1
                record["duplicate_ids"].append(row_id)
                continue
            seen[row_id] = at

            mapped = as_mapping(cells)
            try:
                stored = conn.hgetall(ROW_KEY % row_id) or {}
            except Exception as error:
                record["read_failures"] += 1
                record["read_failure_reasons"].append("%s:store:%s" % (row_id, type(error).__name__))
                stored = None
                break
            if not stored:
                record["missing_from_redis"] += 1
                record["missing_ids"].append(row_id)
                if mirror:
                    _write(conn, row_id, mapped, record)
                continue
            changed = sorted(name for name in set(mapped) | set(stored)
                             if canonical(mapped.get(name)) != canonical(stored.get(name)))
            if changed:
                record["differing"] += 1
                record["differing_ids"].append(row_id)
                for name in changed:
                    record["columns_that_differ"][name] = \
                        record["columns_that_differ"].get(name, 0) + 1
                if mirror:
                    _write(conn, row_id, mapped, record)
            else:
                record["matched"] += 1
        else:
            covered_to = first + count - 1
            continue
        break

    record["covered_from"] = 1 if covered_to else 0
    record["covered_to"] = covered_to
    record["unique_row_ids"] = len(seen)

    # EXTRAS ARE UNAMBIGUOUS HERE, which is the dividend of walking the whole board: anything in the
    # index that the enumeration did not see is extra, with no window to argue about.
    if covered_to == total_at_start and not record["read_failures"]:
        try:
            stored_ids = set(conn.smembers(INDEX_KEY) or set())
            extra = sorted(stored_ids - set(seen))
            record["extra_in_redis"] = len(extra)
            record["extra_ids"] = extra[:20]
        except Exception as error:
            record["read_failures"] += 1
            record["read_failure_reasons"].append("index:%s" % type(error).__name__)

    record["verdict"] = _verdict(record)
    record["missing_ids"] = record["missing_ids"][:20]
    record["differing_ids"] = record["differing_ids"][:20]
    record["duplicate_ids"] = record["duplicate_ids"][:20]
    return record


def apply_recheck(record, after) -> dict:
    """Fold a second look at remembered positions into the record, then re-decide the verdict.

    `after` maps position -> the Row_ID found there NOW. A position whose Row_ID changed means rows
    moved while the walk was running, which invalidates the whole claim: every comparison after the
    shift was against a row at a position it no longer occupies.

    This cannot PROVE the sheet held still. Nothing reachable through this gateway can - there is no
    revision token and no lock, which Codex was right to name. It detects a shift instead of
    assuming one did not happen, and says how many positions it looked at so a reader can judge the
    strength of that for themselves."""
    observed = record.get("observed_positions") or {}
    checked = {str(k): canonical(v) for k, v in (after or {}).items()}
    record["positions_rechecked"] = len(checked)
    record["positions_shifted"] = sum(
        1 for at, row_id in checked.items()
        if at in observed and row_id != canonical(observed[at]))
    record["verdict"] = _verdict(record)
    return record


def _write(conn, row_id, mapping, record) -> None:
    """Mirror one row. A write makes the run INELIGIBLE, which is the point of counting them."""
    try:
        conn.delete(ROW_KEY % row_id)
        conn.hset(ROW_KEY % row_id, mapping=mapping)
        conn.sadd(INDEX_KEY, row_id)
        record["writes_performed"] += 1
    except Exception as error:
        record["read_failures"] += 1
        record["read_failure_reasons"].append("%s:write:%s" % (row_id, type(error).__name__))


def _verdict(record) -> str:
    if record["read_failures"] or record["covered_to"] != record["frozen_total"]:
        return UNKNOWN
    if record["positions_shifted"]:
        return UNKNOWN
    if (record["missing_from_redis"] or record["differing"] or record["extra_in_redis"]
            or record["duplicate_row_ids"]):
        return DIVERGE
    if record["rows_without_an_id"] or not record["header_matches_schema"]:
        return UNKNOWN
    if record["data_rows"] == 0:
        # A header and nothing else. Codex: a header-only sheet produced an eligible AGREE with zero
        # data rows, which is agreement about nothing.
        return NO_SAMPLE
    return AGREE


def gate_eligible(record) -> bool:
    """THE PREDICATE. Codex's list, as one expression over fields in the record."""
    return bool(
        record.get("verdict") == AGREE
        and record.get("covered_from") == 1
        and record.get("covered_to") == record.get("frozen_total")
        and record.get("frozen_total") == record.get("total_at_end")
        and record.get("data_rows", 0) > 0
        and record.get("unique_row_ids") == record.get("data_rows")
        and record.get("matched") == record.get("data_rows")
        and record.get("header_matches_schema") is True
        and record.get("writes_performed", 1) == 0
        and record.get("read_failures", 1) == 0
        and record.get("positions_shifted", 1) == 0
        and record.get("positions_rechecked", 0) > 0
        and record.get("missing_from_redis") == 0
        and record.get("differing") == 0
        and record.get("extra_in_redis") == 0
        and record.get("duplicate_row_ids") == 0
        and record.get("rows_without_an_id") == 0
    )


def record_run(conn, result, log=None) -> bool:
    """The durable line first, the Redis stream second. Persistence is DISABLED on this instance."""
    line = dict(result)
    line["at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line["gate_eligible"] = gate_eligible(result)
    if log is not None:
        log(json.dumps(line, sort_keys=True))
    # EVERY VALUE AS A STRING. redis-py refuses to encode a bool, and this record carries four of
    # them - so the stream copy raised, record_run returned False, and NOTHING said why. The live
    # run wrote its durable line and left no Redis copy at all, which I noticed only because the
    # keyspace viewer showed v1:bus:* empty afterwards.
    flat = {}
    for key, value in line.items():
        if isinstance(value, bool):
            flat[key] = "true" if value else "false"
        elif isinstance(value, (list, dict)):
            flat[key] = json.dumps(value, sort_keys=True)
        elif value is None:
            flat[key] = ""
        else:
            flat[key] = value
    try:
        conn.xadd(VERIFY_LOG, flat)
        return True
    except Exception as error:
        # SAY SO. A silent False is how a missing copy looks exactly like a copy nobody looked for.
        if log is not None:
            log(json.dumps({"stream_write_failed": type(error).__name__,
                            "note": "the durable line above is the record; the Redis copy is an "
                                    "accelerator and this run has none"}, sort_keys=True))
        return False


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="Verify the whole board against its Redis mirror")
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK)
    ap.add_argument("--mirror", action="store_true",
                    help="write the mirror for rows that are missing or differ. A BACKFILL: the "
                         "run is then INELIGIBLE for a span, by construction.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    settings = redis_dual.Settings()
    conn = redis_dual.client(settings, precheck=False)
    if conn is None:
        print("UNKNOWN: no Redis connection, so nothing was compared. NOT zero divergence.")
        return 2

    from bus import load_env, read_range                                # noqa: PLC0415
    env = load_env()
    head = read_range(env, 1, 1)
    total = head.get("total")
    if not isinstance(total, int) or total < 1:
        print("UNKNOWN: the gateway reported no usable row count. Nothing was compared.")
        return 2

    points = recheck_points(total)
    result = verify(conn, lambda f, c: read_range(env, f, c), total,
                    size=args.chunk, mirror=args.mirror, remember=points)

    # LOOK AGAIN: the ceiling, and the Row_ID at a spread of positions the walk already passed.
    # Gemini said to freeze the ceiling; Codex pointed out that a frozen ceiling is only a valid
    # PREFIX claim if positions below it cannot shift, and a middle insert or delete shifts them.
    try:
        after = {}
        for at in points:
            reply = read_range(env, at, 1)
            rows = [r for r in (reply.get("rows") or []) if r]
            after[at] = canonical(rows[0][0]) if rows else ""
        tail = read_range(env, 1, 1)
        result["total_at_end"] = tail.get("total")
        apply_recheck(result, after)
    except Exception as error:
        result["read_failures"] += 1
        result["read_failure_reasons"].append("recheck:%s" % type(error).__name__)
        result["verdict"] = _verdict(result)

    record_run(conn, result, log=print)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("%s: %d data row(s), %d matched, rows %d-%d of %d"
              % (result["verdict"], result["data_rows"], result["matched"],
                 result["covered_from"], result["covered_to"], result["frozen_total"]))
        print("gate eligible: %s" % gate_eligible(result))
        for name in ("missing_from_redis", "differing", "extra_in_redis", "duplicate_row_ids",
                     "rows_without_an_id", "read_failures", "positions_shifted",
                     "writes_performed"):
            if result.get(name):
                print("  %-22s %d" % (name, result[name]))
    return {AGREE: 0, DIVERGE: 1, UNKNOWN: 2, NO_SAMPLE: 3}[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())

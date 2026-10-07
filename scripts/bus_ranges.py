"""Compare the board against Redis by CLOSED PHYSICAL INDEX RANGE, so a verdict can be checked.

WHY THIS EXISTS AND bus_reconcile.py DOES NOT ANSWER IT
    Codex, reviewing the time-windowed reconciler for the third time:

        "No gate yet. A bus:compare entry can say measured=compare_only, verdict=AGREE, and have
         zero caveat counts while an in-window row was missed. No predicate over the current entry
         fields can distinguish that record from genuine agreement."

    Four rounds of fixes made those verdicts honest and none of them made the design able to answer
    the question, because two assumptions underneath it are false: a window cannot be inferred from
    the rows that came back, and THE SHEET'S ROW ORDER IS NOT ITS TIMESTAMP ORDER. Both existing
    filters lean on time; the store is ordered by arrival.

    Gemini, architect lead, 2026-10-07: "Drop time completely. Physical index range is the correct
    architectural boundary. Comparing a closed interval [start, end] turns an indeterminate time
    query into an exact identity and count match, eliminating timestamp jitter and lexical
    comparison bugs."

    So: no timestamps anywhere in this file. Not in the fence, not in the digest, not in the
    verdict. Every class of defect Codex found - fractional seconds, naive stamps, lexical
    ordering, boundary ties, an inferred ceiling - is gone by construction rather than by guard.

THE PREDICATE, which is the whole deliverable
    A run is gate-eligible when, and only when, the record says:

        verdict == "AGREE"
        and covered_from == 1
        and covered_to == frozen_total
        and chunks_matched == chunks_total
        and chunks_unreadable == 0

    Every term is a field in the record. That is what Codex asked for and could not get before:
    agreement about an ENUMERATED set, with its coverage stated, rather than agreement about a
    window nobody can reconstruct.

THREE THINGS GEMINI NAMED, all handled here
    1. The gateway needed a range verb. It has start+count now (gas-bus/Code.js) - ADDITIVE, and
       bus.read_range refuses an answer that does not echo `start`, because a gateway that ignores
       the parameter returns the whole board and the caller would never know.
    2. "Read total first, freeze end_index to that snapshot. A floating upper bound reintroduces
       edge leaks." Done: total is frozen on the first read and every later read is bounded by it.
       The board is append-only, so a row arriving mid-walk lands above the ceiling and is the next
       run's work, not a leak.
    3. "With persistence disabled the verified high-water mark cannot live solely in Redis." It does
       not. The durable record is the RUN LOG - one structured line per verified range, in Cloud
       Logging, which outlives any Redis failover. Redis holds the chunk digests, which are an
       accelerator: lose them and the next run rebuilds them from the board.

WHAT A CHUNK DIGEST IS
    sha256 over the canonicalised cells of every row in the chunk, in physical order, with a unit
    separator between cells and a record separator between rows. Order is part of the digest because
    physical order is the thing being compared. Contents never leave this process: the digest
    travels, the rows do not.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redis_dual                                                       # noqa: E402

CHUNK_KEY = redis_dual.KEY_VERSION + "bus:chunk:%d"      # STRING: digest of rows [start, start+n)
RANGE_LOG = redis_dual.KEY_VERSION + "bus:ranges"        # STREAM: one entry per run, counts only
DEFAULT_CHUNK = 200

AGREE, DIVERGE, UNKNOWN, NO_SAMPLE = "AGREE", "DIVERGE", "UNKNOWN", "NO_SAMPLE"


def canonical(cell) -> str:
    """One string form for a cell, so a type difference is not a content difference.

    The same normalisation bus_reconcile uses, and for the same reason: the gateway returns a column
    typed however the Sheet felt about it. Timestamps are canonicalised as TEXT here and never
    parsed - this file compares bytes, not instants, which is the point of the design."""
    if cell is None:
        return ""
    text = cell if isinstance(cell, str) else str(cell)
    return text.replace("\r\n", "\n").strip()


def chunk_digest(rows) -> str:
    """sha256 of a chunk's rows, in order. Order is part of the answer, so it is part of the hash."""
    joined = "\u001e".join("\u001f".join(canonical(c) for c in row) for row in rows)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def chunk_bounds(total, size=DEFAULT_CHUNK, start=1):
    """[(first, count)] covering `start`..`total` inclusive, in chunks of `size`.

    Closed and contiguous by construction: the next chunk begins exactly where the last ended, so
    the covered set is an interval and not a union of guesses."""
    if total < start or size < 1:
        return []
    out, at = [], start
    while at <= total:
        n = min(size, total - at + 1)
        out.append((at, n))
        at += n
    return out


def compare_range(conn, reader, total, size=DEFAULT_CHUNK, start=1, store=False) -> dict:
    """Walk [start, total] in chunks, comparing each chunk's digest against the one in Redis.

    `reader(first, count)` returns the rows at those physical positions. `total` is the FROZEN
    ceiling - this function never asks the board how big it is, because a function that re-reads the
    ceiling mid-walk is the floating bound Gemini warned about.

    `store=True` writes the digest of any chunk Redis does not hold. That is a backfill, and like
    the reconciler's it is reported separately from the measurement: a chunk that was absent and is
    now written did NOT match, and saying otherwise is the self-grading failure again.
    """
    bounds = chunk_bounds(total, size, start)
    if not bounds:
        return {"verdict": NO_SAMPLE, "frozen_total": total, "covered_from": 0, "covered_to": 0,
                "chunks_total": 0, "chunks_matched": 0, "chunks_differing": 0,
                "chunks_absent": 0, "chunks_unreadable": 0, "chunks_stored": 0,
                "rows_compared": 0, "differing_chunks": [],
                "note": "no rows in the range: nothing was compared, which is not agreement"}

    matched = differing = absent = unreadable = stored = rows_seen = 0
    differing_chunks = []
    covered_to = start - 1

    for first, count in bounds:
        try:
            got = reader(first, count)
        except Exception as error:
            # A chunk we could not READ leaves the interval incomplete. Stop rather than skip: the
            # covered range must stay contiguous or `covered_to` becomes a lie.
            unreadable += 1
            differing_chunks.append("%d:read-failed:%s" % (first, type(error).__name__))
            break
        rows = got.get("rows") if isinstance(got, dict) else got
        rows = [r for r in (rows or []) if r]
        if len(rows) != count:
            # The gateway returned a different number of rows than the closed range asked for. That
            # is not a difference between the stores, it is a failure to ask the question.
            unreadable += 1
            differing_chunks.append("%d:expected-%d-got-%d" % (first, count, len(rows)))
            break

        here = chunk_digest(rows)
        rows_seen += len(rows)
        try:
            theirs = conn.get(CHUNK_KEY % first)
        except Exception as error:
            unreadable += 1
            differing_chunks.append("%d:store-failed:%s" % (first, type(error).__name__))
            break
        theirs = theirs.decode() if isinstance(theirs, bytes) else theirs

        if theirs is None:
            absent += 1
            differing_chunks.append("%d:absent" % first)
            if store:
                conn.set(CHUNK_KEY % first, here)
                stored += 1
        elif theirs == here:
            matched += 1
        else:
            differing += 1
            differing_chunks.append("%d:differs" % first)
            if store:
                conn.set(CHUNK_KEY % first, here)
                stored += 1
        covered_to = first + count - 1

    chunks_total = len(bounds)
    if unreadable:
        verdict = UNKNOWN
    elif differing or absent:
        verdict = DIVERGE
    elif matched == chunks_total:
        verdict = AGREE
    else:
        # Belt and braces: anything that is not every chunk matched is not agreement.
        verdict = UNKNOWN

    return {
        "verdict": verdict,
        "frozen_total": total,
        "covered_from": start if covered_to >= start else 0,
        "covered_to": covered_to,
        "chunks_total": chunks_total,
        "chunks_matched": matched,
        "chunks_differing": differing,
        "chunks_absent": absent,
        "chunks_unreadable": unreadable,
        "chunks_stored": stored,
        "rows_compared": rows_seen,
        "chunk_size": size,
        # Positions and reasons. Never a Row_ID and never a cell: this walks his words and other
        # people's, and a divergence report does not need to quote them to be actionable.
        "differing_chunks": differing_chunks[:20],
    }


def gate_eligible(record) -> bool:
    """THE PREDICATE. Every term is a field in the record, which is the whole point.

    Codex: no predicate over the old entry's fields could tell genuine agreement from a verdict
    about a window that missed a row. This one can, because the record states which rows were
    compared and how far the coverage reached."""
    return bool(
        record.get("verdict") == AGREE
        and record.get("covered_from") == 1
        and record.get("covered_to") == record.get("frozen_total")
        and record.get("chunks_matched") == record.get("chunks_total")
        and record.get("chunks_unreadable") == 0
        and record.get("chunks_total", 0) > 0
    )


def record_run(conn, result, log=None) -> bool:
    """Write the run to the Redis stream AND to the durable log. Returns whether Redis took it.

    THE LOG IS THE RECORD, NOT THE STREAM. redis-central has persistenceMode DISABLED, so a
    failover loses the stream; Gemini: "the verified high-water mark cannot live solely in Redis."
    One structured line goes to stdout, which in Cloud Run is Cloud Logging, which outlives any
    failover. The stream is the fast path for a gate that is already connected."""
    line = dict(result)
    line["at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line["gate_eligible"] = gate_eligible(result)
    if log is not None:
        log(json.dumps(line, sort_keys=True))
    flat = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in line.items()}
    try:
        conn.xadd(RANGE_LOG, flat)
        return True
    except Exception:
        # Losing the fast copy is not losing the answer: the log line above already carries it.
        return False


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="Compare board and Redis by closed physical range")
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="rows per chunk")
    ap.add_argument("--from", dest="start", type=int, default=1, help="first physical row, 1-based")
    ap.add_argument("--store", action="store_true",
                    help="write the digest of any chunk Redis lacks. A BACKFILL: the verdict stays "
                         "the pre-repair one and absent chunks still count as divergence.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    settings = redis_dual.Settings()
    conn = redis_dual.client(settings, precheck=False)
    if conn is None:
        print("UNKNOWN: no Redis connection, so nothing was compared. NOT zero divergence.")
        return 2

    from bus import load_env, read_range                                # noqa: PLC0415
    env = load_env()

    # TOTAL IS READ FIRST AND FROZEN. One row, so the ceiling is known before the walk starts.
    head = read_range(env, 1, 1)
    total = head.get("total")
    if not isinstance(total, int) or total < 1:
        print("UNKNOWN: the gateway did not report a usable row count, so no ceiling could be "
              "frozen. Nothing was compared.")
        return 2

    result = compare_range(conn, lambda first, count: read_range(env, first, count),
                           total, size=args.chunk, start=args.start, store=args.store)
    record_run(conn, result, log=print)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("%s: %d of %d chunk(s) matched, rows %d-%d of %d"
              % (result["verdict"], result["chunks_matched"], result["chunks_total"],
                 result["covered_from"], result["covered_to"], result["frozen_total"]))
        print("gate eligible: %s" % gate_eligible(result))
        for name in ("chunks_differing", "chunks_absent", "chunks_unreadable", "chunks_stored"):
            if result.get(name):
                print("  %-20s %d" % (name, result[name]))
    return {AGREE: 0, DIVERGE: 1, UNKNOWN: 2, NO_SAMPLE: 3}[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())

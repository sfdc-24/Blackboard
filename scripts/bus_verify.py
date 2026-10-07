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
import hashlib
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

# The digest the owner ratified on 2026-10-07, board row
# CCC-RATIFY-KNOWN-DUPLICATES-20261007T1900Z. Pinned HERE as well as in the file, so a file
# edited together with its own digest still fails the predicate.
RATIFIED_DIGEST = "ced284b2333c27dd"

# The CENSUS digest, pinned the same way and for a sharper reason.
#
# CODEX FOUND THE HOLE, 2026-10-07 22:05Z, on exact head 7bfa1be: naming an id forgave it with
# UNBOUNDED MULTIPLICITY. duplicates_ratified simply incremented for every repeat of a listed id, so
# a NEW duplicate of an already-listed id - a 56th occurrence - still produced AGREE and
# gate_eligible=True. It proved it synthetically: two occurrences AGREE, three occurrences of the
# SAME listed id, still AGREE. The test for a 47th DISTINCT id never covered it, and the stored
# duplicate_occurrences_at_ratification=55 was decoration - nothing read it.
#
# So the exemption is now bounded by a per-id census: each listed id is forgiven UP TO the number of
# duplicate occurrences it had when the owner ratified it, and one more fails.
#
# None, deliberately, until a measured census is ratified. While this is None, ANY board holding a
# duplicate is ineligible - the HOLD is enforced in code rather than promised in a comment. The
# census cannot be invented; it has to come off a run that walked the whole board.
#
# AND IT COVERS THE IDLESS POSITIONS TOO, which aya found on the same head: id_list_digest hashes
# duplicated_ids and NOTHING ELSE, so idless_positions could be widened quietly - add a position,
# the digest still matches, four forgiven rows become five. One new ratification closes both holes
# rather than two ratifications closing one each.
RATIFIED_CENSUS_DIGEST = None

AGREE, DIVERGE, UNKNOWN, NO_SAMPLE = "AGREE", "DIVERGE", "UNKNOWN", "NO_SAMPLE"
DEFAULT_CHUNK = 200
RECHECK_POSITIONS = 8


RATIFIED = Path(__file__).resolve().parent / "board_known_duplicates.json"


def ratified(path=None) -> dict:
    """The exemption the owner ratified, with its own digest checked before it is trusted.

    BY IDENTITY, NOT BY POSITION, and measurement forced that: the newest duplicate sits at physical
    row 3736, the LAST row on the board. A position cut-off set there checks nothing and set lower
    fails forever on everything above it. Naming the ids means a 47th duplicate - any id not on the
    list - is unratified and fails the gate, which is the property worth having.

    THE DIGEST IS THE GUARD. It is recomputed from the list and compared with the stored value, so a
    quiet edit that slips another id in fails loudly instead of widening the exemption. A deliberate
    addition means a new digest and a new board row, reviewable in git beside its reason.

    A missing or unreadable file means NOTHING is exempt. That direction is deliberate: an absent
    exemption makes the gate stricter, never looser.

    THE CENSUS is the second half, and the half that was missing. Naming an id said WHETHER it was
    forgiven; the census says HOW MANY TIMES. It is carried in its own field with its own digest, so
    the id list keeps the digest the owner already ratified and the multiplicity bound is a separate,
    separately-ratified fact. An absent census is reported as absent and forgives no multiplicity -
    again stricter, never looser.
    """
    try:
        data = json.loads(Path(path or RATIFIED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ids": set(), "idless": set(), "digest": "", "readable": False,
                "census": {}, "census_digest": "", "census_present": False}
    ids = sorted(str(i) for i in data.get("duplicated_ids") or [])
    computed = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16]
    if computed != str(data.get("id_list_digest") or ""):
        # Refuse the whole file rather than part of it. A list whose digest disagrees with itself
        # has been edited by something that did not understand what the digest was for.
        return {"ids": set(), "idless": set(), "digest": "",
                "readable": False, "digest_mismatch": True,
                "computed": computed, "stored": data.get("id_list_digest"),
                "census": {}, "census_digest": "", "census_present": False}
    idless = {int(p) for p in data.get("idless_positions") or []}
    census, digest, present = _census(data, set(ids), idless)
    return {"ids": set(ids), "idless": idless,
            "digest": computed, "readable": True,
            "census": census, "census_digest": digest, "census_present": present}


def census_digest(census, idless) -> str:
    """The digest over EVERY bounded claim: each id with its count, and each idless position.

    Separate from id_list_digest on purpose - that one is the digest the owner already ratified over
    the names alone, and it stays as it is. This one covers what the names alone could not say: how
    many times each is forgiven, and which id-less positions are forgiven at all. Two prefixes keep
    an id and a position from ever colliding in the body."""
    body = "\n".join(
        ["count:%s=%d" % (i, int(census[i])) for i in sorted(census)]
        + ["idless:%d" % p for p in sorted(idless)])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _census(data, ids, idless) -> tuple:
    """Load duplicate_census if it is present, self-consistent, and about exactly the listed ids.

    Every rejection below returns an ABSENT census rather than a partial one, because a partial
    census is the same failure as the unbounded exemption: some ids bounded, some not, and no way to
    tell from the record which."""
    raw = data.get("duplicate_census")
    if not isinstance(raw, dict) or not raw:
        return {}, "", False
    try:
        census = {str(k): int(v) for k, v in raw.items()}
    except (TypeError, ValueError):
        return {}, "", False
    if any(v < 1 for v in census.values()):
        # A census entry of zero would say "listed but never duplicated", which the list itself
        # contradicts. Refuse rather than guess which of the two is wrong.
        return {}, "", False
    if set(census) != ids:
        # The census must cover the ratified ids and nothing else. A census naming an id the list
        # does not would forgive multiplicity for an id with no exemption at all.
        return {}, "", False
    digest = census_digest(census, idless)
    if digest != str(data.get("census_digest") or ""):
        return {}, "", False
    return census, digest, True


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
    if type(reply.get("start")) is not int or reply["start"] != first:
        return "asked for start=%s and the gateway echoed start=%r" % (first, reply.get("start"))
    rows = reply.get("rows")
    if not isinstance(rows, list) or not all(isinstance(r, list) and r for r in rows):
        return "the gateway did not return a list of nonempty physical rows"
    echoed_count = reply.get("count")
    if echoed_count is not None and (type(echoed_count) is not int or echoed_count != len(rows)):
        return "the gateway echoed count=%r and sent %d row(s)" % (echoed_count, len(rows))
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


def empty_record(total_at_start=None, size=DEFAULT_CHUNK, total_at_end=None, remember=()):
    return {
        "verdict": UNKNOWN, "frozen_total": total_at_start, "total_at_end": total_at_end,
        "covered_from": 0, "covered_to": 0,
        "header_present": False, "header_matches_schema": False,
        "data_rows": 0, "unique_row_ids": 0,
        "matched": 0, "missing_from_redis": 0, "differing": 0, "extra_in_redis": 0,
        "duplicate_row_ids": 0, "rows_without_an_id": 0,
        "duplicates_ratified": 0, "duplicates_unratified": 0,
        "duplicates_over_census": 0, "over_census_ids": [],
        "duplicate_counts": {},
        "idless_ratified": 0, "idless_unratified": 0,
        "ratification_digest": "", "ratification_readable": False,
        "census_present": False, "census_digest": "",
        "chunks_total": 0, "chunks_read": 0, "read_failures": 0,
        "positions_expected": len(set(remember or ())), "positions_rechecked": 0, "positions_shifted": 0, "observed_positions": {},
        "writes_performed": 0, "chunk_size": size,
        "columns_that_differ": {}, "missing_ids": [], "differing_ids": [], "extra_ids": [],
        "duplicate_ids": [], "read_failure_reasons": [],
    }


def verify(conn, reader, total_at_start, size=DEFAULT_CHUNK, mirror=False, total_at_end=None,
           remember=(), exempt=None) -> dict:
    """Walk the whole board, compare every data row against its own mirror, report every caveat.

    `reader(first, count)` returns the gateway's reply dict for that closed range.
    `remember` is the positions whose Row_ID to record as the walk passes them, so a caller can
    look at those positions again afterwards and notice the sheet moving underneath it. The
    comparison is done by `apply_recheck`, not here, because this function must not read the board
    a second time - a walk that re-reads is a walk with two opinions about what it saw.
    """
    record = empty_record(total_at_start, size, total_at_end, remember)

    bounds = chunk_bounds(total_at_start, size)
    record["chunks_total"] = len(bounds)
    if not bounds:
        record["verdict"] = NO_SAMPLE
        record["note"] = "the board reported no rows: nothing was compared, which is not agreement"
        return record

    seen, positions = {}, {}
    wanted = set(remember or ())
    exempt = ratified() if exempt is None else exempt
    record["ratification_digest"] = exempt.get("digest", "")
    record["ratification_readable"] = bool(exempt.get("readable"))
    record["census_present"] = bool(exempt.get("census_present"))
    record["census_digest"] = exempt.get("census_digest", "")
    covered_to = 0
    for first, count in bounds:
        try:
            reply = reader(first, count)
        except (Exception, SystemExit) as error:
            # SystemExit IS caught here, deliberately. bus.read_range raises it when the gateway
            # answers four times without rows - a flap, which this gateway does - and SystemExit is
            # a BaseException, so `except Exception` let it straight through. The first live
            # verification after the backfill died on exactly that: the job exited 1 and wrote NO
            # RECORD AT ALL, which is the one outcome this whole design exists to prevent. A flap
            # mid-walk is a counted read failure and an UNKNOWN with a record, not silence.
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
                if at in wanted:
                    record["observed_positions"][str(at)] = canonical(cells[0] if cells else "")
                record["header_present"] = looks_like_header(cells)
                record["header_matches_schema"] = header_matches(cells)
                continue
            row_id = canonical(cells[0] if cells else "")
            positions[at] = row_id
            if at in wanted:
                record["observed_positions"][str(at)] = row_id
            if not row_id:
                record["rows_without_an_id"] += 1
                # Ratified by POSITION for these four, because a row with no id has no identity to
                # name. They are four consecutive rows, 1563-1566, and the board is append-only, so
                # the set cannot grow by accident.
                if at in exempt.get("idless", set()):
                    record["idless_ratified"] += 1
                else:
                    record["idless_unratified"] += 1
                continue
            record["data_rows"] += 1
            if row_id in seen:
                # STILL COUNTED IN FULL. The exemption changes the VERDICT, never the measurement:
                # a reader of this record can always see how many duplicates the board holds.
                record["duplicate_row_ids"] += 1
                record["duplicate_ids"].append(row_id)
                # PER ID, because the total never could have caught a new repeat of a listed id.
                # Counted for every duplicate, listed or not, so the census comparison below is a
                # predicate over the whole record rather than a decision taken row by row.
                record["duplicate_counts"][row_id] = record["duplicate_counts"].get(row_id, 0) + 1
                if row_id in exempt.get("ids", set()):
                    record["duplicates_ratified"] += 1
                else:
                    record["duplicates_unratified"] += 1
                continue
            seen[row_id] = at

            mapped = as_mapping(cells)
            try:
                stored = conn.hgetall(ROW_KEY % row_id) or {}
            except (Exception, SystemExit) as error:
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
        except (Exception, SystemExit) as error:
            record["read_failures"] += 1
            record["read_failure_reasons"].append("index:%s" % type(error).__name__)

    _apply_census(record, exempt)
    record["verdict"] = _verdict(record)
    record["missing_ids"] = record["missing_ids"][:20]
    record["differing_ids"] = record["differing_ids"][:20]
    record["duplicate_ids"] = record["duplicate_ids"][:20]
    return record


def _apply_census(record, exempt) -> None:
    """Bound the named exemption by the ratified per-id count. One occurrence over, and it fails.

    This is the repair for Codex's 22:05Z finding on 7bfa1be. It is deliberately a pass over the
    FINISHED tally rather than a test inside the row loop: the question "has this id appeared more
    often than the owner forgave" cannot be answered while the walk is still running, and the
    previous code answered a different question - "is this id on the list" - because that one can be.

    `duplicate_counts` is left whole. The exemption has never been allowed to reduce the
    measurement, and an over-census id is still reported as the duplicate it is; it is the verdict
    that changes.
    """
    census = exempt.get("census") or {}
    if not record.get("census_present"):
        # No ratified census: nothing is bounded, so nothing is forgiven by multiplicity. The
        # over-count is every duplicate occurrence of a listed id, which is what makes a board with
        # duplicates ineligible until a census is ratified.
        over = {i: n for i, n in record["duplicate_counts"].items()
                if i in exempt.get("ids", set())}
    else:
        over = {i: n - census[i] for i, n in record["duplicate_counts"].items()
                if i in census and n > census[i]}
    record["duplicates_over_census"] = sum(over.values())
    record["over_census_ids"] = sorted(over)[:20]


def apply_recheck(record, after) -> dict:
    """Count only remembered positions, and reject an incomplete or unrelated recheck."""
    observed = record.get("observed_positions") or {}
    checked = {str(k): canonical(v) for k, v in (after or {}).items()}
    common = set(checked) & set(observed)
    record["positions_rechecked"] = len(common)
    record["positions_shifted"] = sum(
        checked[at] != canonical(observed[at]) for at in common)
    if (not observed or set(checked) != set(observed)
            or len(observed) != record.get("positions_expected")):
        record["read_failures"] += 1
        record["read_failure_reasons"].append("recheck:incomplete_or_unexpected_positions")
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
            or record["duplicates_unratified"]
            # An occurrence beyond the ratified count is divergence, not a forgiven repeat. Without
            # this the verdict said AGREE while the record itself showed the id had grown.
            or record.get("duplicates_over_census", 0)):
        return DIVERGE
    if record["idless_unratified"] or not record["header_matches_schema"]:
        return UNKNOWN
    if record["duplicate_row_ids"] and not record["ratification_readable"]:
        # Duplicates exist and no ratification could be read: that is not agreement, and it is not
        # the moment to assume the exemption file was meant to be there.
        return DIVERGE
    if record["idless_ratified"] and not record.get("census_present"):
        # An ID-only v1 list does not bind the idless positions. This applies even
        # on a board with no duplicate IDs, which previously bypassed the census gate.
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
        # data_rows counts every row with an id; unique_row_ids counts the ids. They differ by
        # exactly the duplicate occurrences, so the identity to check is that EVERY unique id
        # matched its mirror - not that there were no duplicates.
        and record.get("matched") == record.get("unique_row_ids")
        and record.get("header_matches_schema") is True
        and record.get("writes_performed", 1) == 0
        and record.get("read_failures", 1) == 0
        and record.get("positions_shifted", 1) == 0
        and record.get("positions_rechecked", 0) > 0
        and record.get("positions_rechecked") == record.get("positions_expected")
        and record.get("positions_expected") == len(recheck_points(record["frozen_total"]))
        and set(record.get("observed_positions") or {})
            == {str(at) for at in recheck_points(record["frozen_total"])}
        and record.get("missing_from_redis") == 0
        and record.get("differing") == 0
        and record.get("extra_in_redis") == 0
        and record.get("duplicates_unratified") == 0
        and record.get("idless_unratified") == 0
        # The ratified exemption must have been READ, and its digest must be the one the owner
        # ratified. A record that forgives duplicates without saying which list it used is a record
        # a gate cannot check.
        and ((record.get("duplicate_row_ids", 0) == 0
              and record.get("rows_without_an_id", 0) == 0)
             or (record.get("ratification_readable") is True
                 and record.get("ratification_digest") == RATIFIED_DIGEST))
        # AND THE EXEMPTION MUST BE BOUNDED. Naming an id said whether it was forgiven and never how
        # many times, so a 56th occurrence of a listed id passed. A board with duplicates is now
        # eligible only against a ratified census, and only while no id has exceeded its count.
        and record.get("duplicates_over_census", 1) == 0
        and ((record.get("duplicate_row_ids", 0) == 0
              and record.get("rows_without_an_id", 0) == 0)
             or (record.get("census_present") is True
                 and bool(RATIFIED_CENSUS_DIGEST)
                 and record.get("census_digest") == RATIFIED_CENSUS_DIGEST))
    )


def record_run(conn, result, log=None) -> bool:
    """The durable line first, the Redis stream second. Persistence is DISABLED on this instance."""
    line = dict(result)
    line["at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line["gate_eligible"] = gate_eligible(result)
    if log is not None:
        log(json.dumps(line, sort_keys=True))
    if conn is None:
        return False
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


def failed_record(stage, error):
    result = empty_record()
    result["read_failures"] = 1
    result["read_failure_reasons"] = ["%s:%s" % (stage, type(error).__name__)]
    return result



def verified_read(reader, first, count):
    reply = reader(first, count)
    why = check_reply(reply, first, count)
    if why:
        raise ValueError(why)
    return reply



def run_checked(conn, reader, size=DEFAULT_CHUNK, mirror=False):
    """Shared CLI/cloud lifecycle. Every operational read failure returns a receipt."""
    try:
        if type(size) is not int or size < 1:
            raise ValueError("chunk size must be a positive integer")
        head = reader(1, 1)
        # An explicitly empty physical board is a sample of zero, not a failed read.
        if (isinstance(head, dict) and type(head.get("total")) is int
                and head["total"] == 0 and type(head.get("start")) is int
                and head["start"] == 1 and type(head.get("count")) is int
                and head["count"] == 0 and head.get("rows") == []):
            result = empty_record(0, size, 0)
            result.update(verdict=NO_SAMPLE, note="the gateway explicitly returned an empty board")
            return result
        why = check_reply(head, 1, 1)
        if why:
            raise ValueError(why)
        total = head.get("total")
        if type(total) is not int or total < 1:
            raise ValueError("no usable row count")
    except (Exception, SystemExit) as error:
        return failed_record("initial_read", error)
    points = recheck_points(total)
    result = verify(conn, reader, total, size=size, mirror=mirror, remember=points)
    # A partial walk already has an honest failure. Do not obscure it with more reads.
    if result["read_failures"]:
        return result
    try:
        after = {}
        for at in points:
            reply = verified_read(reader, at, 1)
            after[at] = canonical(reply["rows"][0][0])
        tail = verified_read(reader, 1, 1)
        end = tail.get("total")
        if type(end) is not int or end < 1:
            raise ValueError("no usable final row count")
        result["total_at_end"] = end
        apply_recheck(result, after)
    except (Exception, SystemExit) as error:
        result["read_failures"] += 1
        result["read_failure_reasons"].append("recheck:%s" % type(error).__name__)
        result["verdict"] = _verdict(result)
    return result



def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="Verify the whole board against its Redis mirror")
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK)
    ap.add_argument("--mirror", action="store_true",
                    help="write the mirror for rows that are missing or differ. A BACKFILL: the "
                         "run is then INELIGIBLE for a span, by construction.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    conn = None
    try:
        settings = redis_dual.Settings()
        conn = redis_dual.client(settings, precheck=False)
        if conn is None:
            raise ConnectionError("no Redis connection")
        from bus import load_env, read_range                            # noqa: PLC0415
        env = load_env()
        result = run_checked(conn, lambda f, c: read_range(env, f, c),
                             size=args.chunk, mirror=args.mirror)
    except (Exception, SystemExit) as error:
        result = failed_record("startup", error)

    record_run(conn, result, log=print)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("%s: %d data row(s), %d matched, rows %d-%d of %s"
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

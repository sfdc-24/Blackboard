"""scripts/bus_verify.py: coverage from positions, CONTENT FROM THE MIRROR, every caveat recorded.

THE TEST THAT MATTERS is test_a_stale_mirror_cannot_be_eligible. Codex broke the previous runner in
one move: it hashed the board and compared that digest to one written FROM THE BOARD, so it proved
the sheet had not changed since the last digest and nothing whatever about Redis. Sheet row NEW,
mirror still OLD, verdict AGREE, gate_eligible true.

So every case here asks the same question: can a record satisfy gate_eligible() while the two stores
actually disagree, or while a row was never compared? The adversarial cases Codex supplied are each
a named test below, including the ones he used to break the old design.

No network, no redis library, no clock, no timestamp parsing anywhere in the file under test.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bus_verify as bv                                                 # noqa: E402

HEADER = list(bv.COLUMNS)


def row(i, gist=None):
    return ["R-%d" % i, "2026-10-05T10:00:00Z", "grok", "ALL", "APPEND",
            "BCB|v=1|id=R-%d" % i, "OPEN", "Blackboard", gist or "gist %d" % i, ""]


BOARD = [HEADER] + [row(i) for i in range(1, 7)]        # header + six data rows, positions 1..7


class FakeStore:
    def __init__(self, fail_hgetall=False, fail_xadd=False):
        self.hashes, self.sets, self.stream = {}, {}, []
        self.fail_hgetall, self.fail_xadd = fail_hgetall, fail_xadd

    def hgetall(self, key):
        if self.fail_hgetall:
            raise RuntimeError("fake store failure")
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def delete(self, key):
        self.hashes.pop(key, None)

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def xadd(self, key, fields):
        if self.fail_xadd:
            raise RuntimeError("fake xadd failure")
        self.stream.append(dict(fields))


def gateway(board):
    """A gateway that honours a closed range and echoes it honestly."""
    def read(first, count):
        return {"rows": board[first - 1:first - 1 + count], "total": len(board),
                "start": first, "count": min(count, max(0, len(board) - first + 1))}
    return read


def seeded(board):
    """A store whose mirror matches the board exactly - the only state that may be eligible."""
    conn = FakeStore()
    bv.verify(conn, gateway(board), len(board), size=3, mirror=True)
    return conn


def full(conn, board, **kw):
    """A complete, clean walk with the recheck applied, as main() performs it."""
    points = bv.recheck_points(len(board))
    result = bv.verify(conn, gateway(board), len(board), size=kw.pop("size", 3),
                       remember=points, total_at_end=len(board), **kw)
    after = {at: (board[at - 1][0] if at - 1 < len(board) else "") for at in points}
    return bv.apply_recheck(result, after)


class TheMirrorIsWhatIsCompared(unittest.TestCase):
    def test_a_clean_board_and_mirror_is_eligible(self):
        result = full(seeded(BOARD), BOARD)
        self.assertEqual(bv.AGREE, result["verdict"])
        self.assertTrue(bv.gate_eligible(result))
        self.assertEqual(6, result["data_rows"])
        self.assertEqual(6, result["matched"])

    def test_a_stale_mirror_cannot_be_eligible(self):
        """THE ONE CODEX BROKE. The board changes, the mirror does not, and the record must say so.
        The previous runner reported AGREE here because it never read the mirror at all."""
        conn = seeded(BOARD)
        changed = [list(r) for r in BOARD]
        changed[3][8] = "somebody edited this"          # physical row 4, a data row
        result = full(conn, changed)
        self.assertEqual(bv.DIVERGE, result["verdict"])
        self.assertEqual(1, result["differing"])
        self.assertIn("gist", result["columns_that_differ"])
        self.assertFalse(bv.gate_eligible(result))

    def test_a_backfill_run_is_never_eligible(self):
        """--mirror repairs and therefore disqualifies itself. The run AFTER it may agree."""
        conn = FakeStore()
        first = full(conn, BOARD, mirror=True)
        self.assertEqual(6, first["writes_performed"])
        self.assertFalse(bv.gate_eligible(first))
        self.assertTrue(bv.gate_eligible(full(conn, BOARD)))

    def test_a_missing_mirror_row_is_divergence(self):
        conn = seeded(BOARD)
        conn.delete(bv.ROW_KEY % "R-3")
        result = full(conn, BOARD)
        self.assertEqual(bv.DIVERGE, result["verdict"])
        self.assertEqual(1, result["missing_from_redis"])

    def test_a_row_only_in_redis_is_divergence_with_no_window_to_argue_about(self):
        """The dividend of walking the WHOLE board: an extra needs no window to be unambiguous."""
        conn = seeded(BOARD)
        conn.hset(bv.ROW_KEY % "R-GHOST", mapping={"row_id": "R-GHOST"})
        conn.sadd(bv.INDEX_KEY, "R-GHOST")
        result = full(conn, BOARD)
        self.assertEqual(bv.DIVERGE, result["verdict"])
        self.assertEqual(1, result["extra_in_redis"])
        self.assertEqual(["R-GHOST"], result["extra_ids"])


class TheAdversarialCasesCodexSupplied(unittest.TestCase):
    def test_a_gateway_echoing_the_wrong_start_is_caught(self):
        """He returned the header for both row 1 and row 2, echoing start=1 each time, and the old
        record claimed coverage 1..2 while row 2 was never read."""
        def liar(first, count):
            return {"rows": BOARD[0:count], "total": len(BOARD), "start": 1, "count": count}

        result = bv.verify(seeded(BOARD), liar, len(BOARD), size=3)
        self.assertEqual(bv.UNKNOWN, result["verdict"])
        self.assertTrue(any("echoed start=1" in r for r in result["read_failure_reasons"]))
        self.assertFalse(bv.gate_eligible(result))

    def test_a_short_chunk_is_a_failure_to_ask_not_a_difference(self):
        def short(first, count):
            rows = BOARD[first - 1:first - 1 + count - 1]
            return {"rows": rows, "total": len(BOARD), "start": first, "count": len(rows)}

        result = bv.verify(seeded(BOARD), short, len(BOARD), size=3)
        self.assertEqual(bv.UNKNOWN, result["verdict"])
        self.assertFalse(bv.gate_eligible(result))

    def test_a_header_only_sheet_is_no_sample_not_agreement(self):
        """He got an eligible AGREE out of this: zero data rows, agreement about nothing."""
        board = [HEADER]
        result = full(seeded(board), board)
        self.assertEqual(bv.NO_SAMPLE, result["verdict"])
        self.assertFalse(bv.gate_eligible(result))
        self.assertEqual(0, result["data_rows"])

    def test_a_shifted_position_invalidates_the_whole_claim(self):
        """A middle insert moves every row below it, so comparisons after the shift were against
        rows at positions they no longer occupy. The recheck detects it."""
        conn = seeded(BOARD)
        result = bv.verify(conn, gateway(BOARD), len(BOARD), size=3,
                           remember=bv.recheck_points(len(BOARD)), total_at_end=len(BOARD))
        self.assertEqual(bv.AGREE, result["verdict"])
        moved = {at: "R-SOMETHING-ELSE" for at in bv.recheck_points(len(BOARD)) if at > 1}
        bv.apply_recheck(result, moved)
        self.assertEqual(bv.UNKNOWN, result["verdict"])
        self.assertGreater(result["positions_shifted"], 0)
        self.assertFalse(bv.gate_eligible(result))

    def test_an_append_during_the_walk_makes_the_run_ineligible(self):
        """A prefix claim is not a whole-board claim. The totals must match at both ends."""
        conn = seeded(BOARD)
        result = full(conn, BOARD)
        self.assertTrue(bv.gate_eligible(result))
        result["total_at_end"] = result["frozen_total"] + 1
        self.assertFalse(bv.gate_eligible(result))

    def test_no_cross_row_digest_exists_to_collide(self):
        """['a','b'] and ['a\\u001fb'] hashed identically in the previous design. There is no
        cross-row digest here at all: rows are compared field by field against their own mirror."""
        source = (Path(__file__).resolve().parents[1] / "scripts" / "bus_verify.py").read_text(
            encoding="utf-8")
        self.assertNotIn("hashlib", source)
        self.assertNotIn("sha256", source)

    def test_no_timestamp_is_parsed_anywhere(self):
        source = (Path(__file__).resolve().parents[1] / "scripts" / "bus_verify.py").read_text(
            encoding="utf-8")
        for forbidden in ("fromisoformat", "strptime", "read_ts"):
            self.assertNotIn(forbidden, source)

    def test_a_drifted_header_makes_every_comparison_below_it_unknown(self):
        board = [["something_else"] + HEADER[1:]] + BOARD[1:]
        result = full(seeded(BOARD), board)
        self.assertEqual(bv.UNKNOWN, result["verdict"])
        self.assertFalse(result["header_matches_schema"])
        self.assertFalse(bv.gate_eligible(result))

    def test_a_row_with_no_id_degrades_the_verdict(self):
        board = [HEADER] + [row(1), ["", "x", "y"], row(3)]
        result = full(seeded(board), board)
        self.assertEqual(bv.UNKNOWN, result["verdict"])
        self.assertEqual(1, result["rows_without_an_id"])
        self.assertFalse(bv.gate_eligible(result))

    def test_a_duplicate_row_id_is_divergence(self):
        board = [HEADER] + [row(1), row(2), row(1)]
        result = full(seeded(board), board)
        self.assertEqual(bv.DIVERGE, result["verdict"])
        self.assertEqual(1, result["duplicate_row_ids"])

    def test_a_store_failure_is_unknown(self):
        conn = FakeStore(fail_hgetall=True)
        result = bv.verify(conn, gateway(BOARD), len(BOARD), size=3)
        self.assertEqual(bv.UNKNOWN, result["verdict"])
        self.assertFalse(bv.gate_eligible(result))


class ThePredicate(unittest.TestCase):
    def test_a_run_with_no_recheck_is_not_eligible(self):
        """An unexamined claim about position stability is not a claim. The record must say how
        many positions were looked at again, and zero is not enough."""
        conn = seeded(BOARD)
        result = bv.verify(conn, gateway(BOARD), len(BOARD), size=3, total_at_end=len(BOARD))
        self.assertEqual(bv.AGREE, result["verdict"])
        self.assertEqual(0, result["positions_rechecked"])
        self.assertFalse(bv.gate_eligible(result))

    def test_every_term_is_a_field_in_the_record(self):
        """Codex's requirement: a gate must be able to evaluate eligibility from the RECORD alone,
        with nothing recomputed and nothing taken on trust."""
        result = full(seeded(BOARD), BOARD)
        for field in ("verdict", "covered_from", "covered_to", "frozen_total", "total_at_end",
                      "data_rows", "unique_row_ids", "matched", "header_matches_schema",
                      "writes_performed", "read_failures", "positions_shifted",
                      "positions_rechecked", "missing_from_redis", "differing", "extra_in_redis",
                      "duplicate_row_ids", "rows_without_an_id"):
            self.assertIn(field, result, field)

    def test_the_durable_line_carries_eligibility_already_evaluated(self):
        conn = seeded(BOARD)
        result = full(conn, BOARD)
        lines = []
        self.assertTrue(bv.record_run(conn, result, log=lines.append))
        written = json.loads(lines[0])
        self.assertTrue(written["gate_eligible"])
        self.assertIn("at", written)

    def test_losing_the_stream_does_not_lose_the_answer(self):
        conn = FakeStore(fail_xadd=True)
        lines = []
        self.assertFalse(bv.record_run(conn, {"verdict": bv.AGREE}, log=lines.append))
        self.assertEqual(1, len(lines))

    def test_no_cell_contents_reach_the_record(self):
        """It walks his words and other people's. A divergence names ids and COLUMN NAMES."""
        conn = seeded(BOARD)
        changed = [list(r) for r in BOARD]
        changed[3][8] = "something private he wrote"
        blob = repr(full(conn, changed))
        self.assertNotIn("something private", blob)


class ItSharesTheMirrorKeyspace(unittest.TestCase):
    def test_the_keys_match_the_legacy_reconcilers(self):
        """Both read and write the same mirror while bus_reconcile.py still exists. If these ever
        disagree, one of them is verifying a keyspace nothing writes."""
        import bus_reconcile
        self.assertEqual(bus_reconcile.ROW_KEY, bv.ROW_KEY)
        self.assertEqual(bus_reconcile.INDEX_KEY, bv.INDEX_KEY)

    def test_the_columns_match_the_writers(self):
        import bus_reconcile
        self.assertEqual(bus_reconcile.COLUMNS, bv.COLUMNS)


if __name__ == "__main__":
    unittest.main(verbosity=2)

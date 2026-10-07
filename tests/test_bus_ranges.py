"""scripts/bus_ranges.py: a verdict a gate can actually check.

THE TEST THAT MATTERS is test_the_predicate_rejects_the_record_codex_described. Codex refused the
time-windowed reconciler three times and the third refusal was the design, not a defect:

    "No gate yet. A bus:compare entry can say measured=compare_only, verdict=AGREE, and have zero
     caveat counts while an in-window row was missed. No predicate over the current entry fields
     can distinguish that record from genuine agreement."

So the question this suite asks of every case is not "did it say AGREE" but "can a predicate over
the RECORD tell this from genuine agreement". Gemini ruled closed physical index ranges for exactly
that reason, and the whole class of timestamp defects - fractional seconds, naive stamps, lexical
ordering, boundary ties, an inferred ceiling - is absent here by construction rather than by guard.

No network, no redis library, no clock.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bus_ranges as br                                                 # noqa: E402


def row(i):
    return ["R-%d" % i, "2026-10-05T10:00:00Z", "grok", "ALL", "APPEND",
            "BCB|v=1|id=R-%d" % i, "OPEN", "Blackboard", "gist %d" % i, ""]


BOARD = [row(i) for i in range(1, 11)]          # ten physical rows, 1-based


class FakeStore:
    def __init__(self, fail_get=False, fail_xadd=False):
        self.data, self.stream = {}, []
        self.fail_get, self.fail_xadd = fail_get, fail_xadd

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("fake store failure")
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def xadd(self, key, fields):
        if self.fail_xadd:
            raise RuntimeError("fake xadd failure")
        self.stream.append(dict(fields))


def reader(board):
    """A gateway that honours a CLOSED range, 1-based inclusive, as the real one now does."""
    def read(first, count):
        return {"rows": board[first - 1:first - 1 + count], "total": len(board),
                "start": first, "count": count}
    return read


class ChunkBoundsCoverTheInterval(unittest.TestCase):
    def test_the_chunks_are_contiguous_and_cover_everything(self):
        bounds = br.chunk_bounds(10, size=3)
        self.assertEqual([(1, 3), (4, 3), (7, 3), (10, 1)], bounds)
        covered = [i for first, n in bounds for i in range(first, first + n)]
        self.assertEqual(list(range(1, 11)), covered, "the covered set must be the interval itself")

    def test_a_chunk_larger_than_the_board_is_one_chunk(self):
        self.assertEqual([(1, 10)], br.chunk_bounds(10, size=500))

    def test_an_empty_board_has_no_chunks(self):
        self.assertEqual([], br.chunk_bounds(0, size=3))

    def test_a_start_past_the_total_has_no_chunks(self):
        self.assertEqual([], br.chunk_bounds(10, size=3, start=11))


class TheDigestIsOverOrderedBYTES(unittest.TestCase):
    def test_the_same_rows_give_the_same_digest(self):
        self.assertEqual(br.chunk_digest(BOARD[:3]), br.chunk_digest([list(r) for r in BOARD[:3]]))

    def test_reordering_changes_the_digest(self):
        """Physical order is what is being compared, so it belongs in the hash."""
        swapped = [BOARD[1], BOARD[0], BOARD[2]]
        self.assertNotEqual(br.chunk_digest(BOARD[:3]), br.chunk_digest(swapped))

    def test_a_changed_cell_changes_the_digest(self):
        changed = [list(r) for r in BOARD[:3]]
        changed[1][8] = "a different gist"
        self.assertNotEqual(br.chunk_digest(BOARD[:3]), br.chunk_digest(changed))

    def test_a_type_difference_is_not_a_content_difference(self):
        """The gateway types a column however the Sheet felt about it. 5 and "5" are one value."""
        as_text = [["R-1", "5"]]
        as_number = [["R-1", 5]]
        self.assertEqual(br.chunk_digest(as_text), br.chunk_digest(as_number))

    def test_no_timestamp_is_ever_parsed(self):
        """The design is 'drop time completely'. A stamp is bytes here, like every other cell, so
        fractional seconds and missing zones cannot change a verdict."""
        source = (Path(__file__).resolve().parents[1] / "scripts" / "bus_ranges.py").read_text(
            encoding="utf-8")
        for forbidden in ("fromisoformat", "strptime", "read_ts"):
            self.assertNotIn(forbidden, source, "time parsing has no business in this file")


class ThePredicate(unittest.TestCase):
    """What a gate reads. Every term is a field in the record."""

    def _clean(self):
        conn = FakeStore()
        br.compare_range(conn, reader(BOARD), len(BOARD), size=3, store=True)
        return conn

    def test_a_full_matching_walk_is_gate_eligible(self):
        conn = self._clean()
        result = br.compare_range(conn, reader(BOARD), len(BOARD), size=3)
        self.assertEqual(br.AGREE, result["verdict"])
        self.assertTrue(br.gate_eligible(result))
        self.assertEqual(1, result["covered_from"])
        self.assertEqual(10, result["covered_to"])
        self.assertEqual(10, result["rows_compared"])

    def test_the_predicate_rejects_the_record_codex_described(self):
        """AGREE, no caveats, and a row was missed - impossible to hide here, because coverage is
        stated. A partial walk that matched everything it looked at is still not gate-eligible."""
        conn = self._clean()
        result = br.compare_range(conn, reader(BOARD), len(BOARD), size=3, start=4)
        self.assertEqual(br.AGREE, result["verdict"], "every chunk it compared did match")
        self.assertFalse(br.gate_eligible(result), "but it never looked at rows 1-3")
        self.assertEqual(4, result["covered_from"])

    def test_a_walk_that_stops_short_of_the_ceiling_is_not_eligible(self):
        conn = self._clean()
        result = br.compare_range(conn, reader(BOARD), 10, size=3)
        result["covered_to"] = 9                      # as a truncated walk would report
        self.assertFalse(br.gate_eligible(result))

    def test_an_empty_range_is_no_sample_and_not_eligible(self):
        result = br.compare_range(FakeStore(), reader([]), 0, size=3)
        self.assertEqual(br.NO_SAMPLE, result["verdict"])
        self.assertFalse(br.gate_eligible(result))
        self.assertIn("not agreement", result["note"])

    def test_zero_chunks_can_never_be_eligible(self):
        self.assertFalse(br.gate_eligible(
            {"verdict": br.AGREE, "covered_from": 1, "covered_to": 0, "frozen_total": 0,
             "chunks_matched": 0, "chunks_total": 0, "chunks_unreadable": 0}))


class WhatCountsAsDivergence(unittest.TestCase):
    def test_an_absent_chunk_is_divergence_not_agreement(self):
        """An empty store is the oldest failure on this fleet. Nothing in Redis is not agreement."""
        result = br.compare_range(FakeStore(), reader(BOARD), len(BOARD), size=3)
        self.assertEqual(br.DIVERGE, result["verdict"])
        self.assertEqual(4, result["chunks_absent"])
        self.assertFalse(br.gate_eligible(result))

    def test_a_changed_row_shows_up_in_its_own_chunk(self):
        conn = FakeStore()
        br.compare_range(conn, reader(BOARD), len(BOARD), size=3, store=True)
        edited = [list(r) for r in BOARD]
        edited[4][8] = "somebody changed this"        # physical row 5, inside chunk (4, 3)
        result = br.compare_range(conn, reader(edited), len(edited), size=3)
        self.assertEqual(br.DIVERGE, result["verdict"])
        self.assertEqual(1, result["chunks_differing"])
        self.assertIn("4:differs", result["differing_chunks"])

    def test_a_backfill_does_not_grade_itself(self):
        """The reconciler's old sin, which this must not repeat: a chunk that was absent and is now
        written did NOT match, and the verdict says so."""
        conn = FakeStore()
        result = br.compare_range(conn, reader(BOARD), len(BOARD), size=3, store=True)
        self.assertEqual(br.DIVERGE, result["verdict"])
        self.assertEqual(4, result["chunks_stored"])
        self.assertFalse(br.gate_eligible(result))
        # And the run AFTER the backfill is the one that may agree.
        self.assertTrue(br.gate_eligible(
            br.compare_range(conn, reader(BOARD), len(BOARD), size=3)))


class AFailureIsUnknownAndStopsTheWalk(unittest.TestCase):
    def test_a_store_failure_is_unknown(self):
        conn = FakeStore(fail_get=True)
        result = br.compare_range(conn, reader(BOARD), len(BOARD), size=3)
        self.assertEqual(br.UNKNOWN, result["verdict"])
        self.assertFalse(br.gate_eligible(result))

    def test_a_read_failure_stops_rather_than_skips(self):
        """Skipping a chunk would leave `covered_to` claiming an interval with a hole in it."""
        def flaky(first, count):
            if first == 4:
                raise RuntimeError("gateway flapped")
            return {"rows": BOARD[first - 1:first - 1 + count], "total": len(BOARD)}

        result = br.compare_range(FakeStore(), flaky, len(BOARD), size=3)
        self.assertEqual(br.UNKNOWN, result["verdict"])
        self.assertEqual(3, result["covered_to"], "coverage must stay contiguous")
        self.assertFalse(br.gate_eligible(result))

    def test_a_short_chunk_is_a_failure_to_ask_not_a_difference(self):
        """The gateway returning fewer rows than the closed range asked for is a broken question,
        and calling it divergence would blame the stores for the reader."""
        def short(first, count):
            return {"rows": BOARD[first - 1:first - 1 + count - 1], "total": len(BOARD)}

        result = br.compare_range(FakeStore(), short, len(BOARD), size=3)
        self.assertEqual(br.UNKNOWN, result["verdict"])
        self.assertTrue(any("expected-3-got-2" in d for d in result["differing_chunks"]))


class TheRunIsRecordedWhereItSurvives(unittest.TestCase):
    def test_the_durable_line_carries_the_predicate_already_evaluated(self):
        """redis-central has persistence DISABLED, so the log line is the record and the stream is
        the accelerator. A reader of either must not have to recompute eligibility."""
        conn = FakeStore()
        br.compare_range(conn, reader(BOARD), len(BOARD), size=3, store=True)
        result = br.compare_range(conn, reader(BOARD), len(BOARD), size=3)
        lines = []
        self.assertTrue(br.record_run(conn, result, log=lines.append))
        import json
        written = json.loads(lines[0])
        self.assertTrue(written["gate_eligible"])
        self.assertEqual(10, written["frozen_total"])
        self.assertIn("at", written)

    def test_losing_the_stream_does_not_lose_the_answer(self):
        conn = FakeStore(fail_xadd=True)
        lines = []
        self.assertFalse(br.record_run(conn, {"verdict": br.AGREE}, log=lines.append))
        self.assertEqual(1, len(lines), "the durable line is written before the stream is tried")

    def test_no_row_id_or_cell_reaches_the_record(self):
        """It walks his words and other people's. A divergence report names POSITIONS."""
        conn = FakeStore()
        result = br.compare_range(conn, reader(BOARD), len(BOARD), size=3)
        blob = repr(result)
        self.assertNotIn("R-1", blob)
        self.assertNotIn("gist", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)

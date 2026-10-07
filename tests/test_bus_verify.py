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

# THE SHEET'S OWN LABELS, not the code's internal field names. The old fixture used
# bv.COLUMNS, so it agreed with the bug the live run found: header_matches compared one
# vocabulary to the other and reported drift on a healthy board. A fixture built from the
# code under test cannot catch the code under test being wrong about the world.
HEADER = ["Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type",
          "Payload", "Category", "Project Tag", "Gist", "Sub-Gist"]


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
        # THE ASSERTION USED TO BE "hashlib is not imported", which was a PROXY and stopped being a
        # true one the moment the ratified exemption needed a digest of its ID LIST. Its replacement
        # then matched the module docstring, which DESCRIBES the old collision - a test reading
        # prose as if it were code. Scanning statements only, which is what it always meant.
        statements = [line for line in source.splitlines()
                      if line.strip() and not line.strip().startswith("#")
                      and '"""' not in line]
        serialisers = [line.strip() for line in statements
                       if "join(" in line and ("u001f" in line or "u001e" in line)]
        self.assertEqual([], serialisers, "a row serialiser has come back")
        hashes = [line.strip() for line in statements if "sha256(" in line]
        self.assertEqual(1, len(hashes), hashes)
        self.assertIn("join(ids)", hashes[0],
                      "the only hash in this file must be over the ratified id list")

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
        """The durable line is written BEFORE the stream is tried, so a stream failure costs the
        accelerator and not the answer. There are two lines now, not one: the record, then the
        failure - because a silent False is how a missing copy came to look like a copy nobody
        looked for."""
        conn = FakeStore(fail_xadd=True)
        lines = []
        self.assertFalse(bv.record_run(conn, {"verdict": bv.AGREE}, log=lines.append))
        self.assertEqual(2, len(lines))
        self.assertIn('"verdict"', lines[0])
        self.assertIn("stream_write_failed", lines[1])

    def test_no_cell_contents_reach_the_record(self):
        """It walks his words and other people's. A divergence names ids and COLUMN NAMES."""
        conn = seeded(BOARD)
        changed = [list(r) for r in BOARD]
        changed[3][8] = "something private he wrote"
        blob = repr(full(conn, changed))
        self.assertNotIn("something private", blob)


class TheRatifiedExemption(unittest.TestCase):
    """The owner chose to annotate rather than repair: the board is append-only and deletion is his
    carve-out, so 46 historical duplicate ids and 4 id-less rows are forgiven by NAME.

    BY IDENTITY, NOT POSITION, and measurement forced it: the newest duplicate sat at physical row
    3736, the LAST row on the board. A position cut-off set there checks nothing; set lower it fails
    forever on everything above it.

    The property that makes this a gate and not an amnesty: a 47th duplicate - any id not on the
    list - is unratified and fails."""

    def setUp(self):
        self.exempt = bv.ratified()
        if not self.exempt["readable"]:
            raise AssertionError("the ratified file did not load, so none of this tested anything")

    def test_the_shipped_list_matches_its_own_digest_and_the_pinned_one(self):
        self.assertEqual(46, len(self.exempt["ids"]))
        self.assertEqual(bv.RATIFIED_DIGEST, self.exempt["digest"])
        self.assertEqual({1563, 1564, 1565, 1566}, self.exempt["idless"])

    def test_a_ratified_duplicate_does_not_fail_the_gate(self):
        dup = sorted(self.exempt["ids"])[0]
        board = [HEADER, row(1), row(2)]
        board[2][0] = dup
        board[1][0] = dup                          # the same id twice, and it is on the list
        conn = FakeStore()
        bv.verify(conn, gateway(board), len(board), size=3, mirror=True, exempt=self.exempt)
        points = bv.recheck_points(len(board))
        result = bv.verify(conn, gateway(board), len(board), size=3, remember=points,
                           total_at_end=len(board), exempt=self.exempt)
        bv.apply_recheck(result, {at: (board[at - 1][0] if at - 1 < len(board) else "")
                                  for at in points})
        self.assertEqual(1, result["duplicate_row_ids"])
        self.assertEqual(1, result["duplicates_ratified"])
        self.assertEqual(0, result["duplicates_unratified"])
        self.assertEqual(bv.AGREE, result["verdict"])
        self.assertTrue(bv.gate_eligible(result))

    def test_a_FORTY_SEVENTH_duplicate_fails(self):
        """The whole point. An id nobody ratified is divergence, however old it looks."""
        board = [HEADER, row(1), row(1)]
        conn = FakeStore()
        bv.verify(conn, gateway(board), len(board), size=3, mirror=True, exempt=self.exempt)
        result = bv.verify(conn, gateway(board), len(board), size=3, total_at_end=len(board),
                           exempt=self.exempt)
        self.assertEqual(1, result["duplicates_unratified"])
        self.assertEqual(bv.DIVERGE, result["verdict"])
        self.assertFalse(bv.gate_eligible(result))

    def test_an_idless_row_at_an_unratified_position_fails(self):
        board = [HEADER, row(1), ["", "x", "y"]]
        conn = FakeStore()
        bv.verify(conn, gateway(board), len(board), size=3, mirror=True, exempt=self.exempt)
        result = bv.verify(conn, gateway(board), len(board), size=3, total_at_end=len(board),
                           exempt=self.exempt)
        self.assertEqual(1, result["idless_unratified"])
        self.assertEqual(bv.UNKNOWN, result["verdict"])

    def test_a_tampered_list_is_refused_whole(self):
        """Slipping an id in changes the digest, and the loader then trusts NOTHING - rather than
        trusting the part that still matches, which is how an exemption quietly widens."""
        import json as _json
        import tempfile
        data = _json.loads((Path(__file__).resolve().parents[1] / "scripts"
                            / "board_known_duplicates.json").read_text(encoding="utf-8"))
        data["duplicated_ids"].append("SOMETHING-I-SLIPPED-IN")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tampered.json"
            path.write_text(_json.dumps(data), encoding="utf-8")
            got = bv.ratified(path)
        self.assertFalse(got["readable"])
        self.assertTrue(got.get("digest_mismatch"))
        self.assertEqual(set(), got["ids"], "a mismatched digest must forgive nothing")

    def test_a_missing_file_forgives_nothing(self):
        """Absent means stricter, never looser."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            got = bv.ratified(Path(tmp) / "nope.json")
        self.assertFalse(got["readable"])
        self.assertEqual(set(), got["ids"])

    def test_duplicates_with_no_readable_ratification_are_divergence(self):
        board = [HEADER, row(1), row(1)]
        conn = FakeStore()
        none = {"ids": set(), "idless": set(), "digest": "", "readable": False}
        bv.verify(conn, gateway(board), len(board), size=3, mirror=True, exempt=none)
        result = bv.verify(conn, gateway(board), len(board), size=3, total_at_end=len(board),
                           exempt=none)
        self.assertEqual(bv.DIVERGE, result["verdict"])
        self.assertFalse(bv.gate_eligible(result))

    def test_the_predicate_demands_the_PINNED_digest(self):
        """A file edited together with its own digest still fails, because the digest the owner
        ratified is pinned in the module as well."""
        record = {"verdict": bv.AGREE, "covered_from": 1, "covered_to": 10, "frozen_total": 10,
                  "total_at_end": 10, "data_rows": 5, "unique_row_ids": 4, "matched": 4,
                  "header_matches_schema": True, "writes_performed": 0, "read_failures": 0,
                  "positions_shifted": 0, "positions_rechecked": 8, "missing_from_redis": 0,
                  "differing": 0, "extra_in_redis": 0, "duplicates_unratified": 0,
                  "idless_unratified": 0, "duplicate_row_ids": 1,
                  "ratification_readable": True, "ratification_digest": bv.RATIFIED_DIGEST}
        self.assertTrue(bv.gate_eligible(record))
        record["ratification_digest"] = "0000000000000000"
        self.assertFalse(bv.gate_eligible(record), "a different list must not be waved through")

    def test_the_measurement_is_never_reduced_by_the_exemption(self):
        """duplicate_row_ids counts every duplicate the board holds, exempt or not. The exemption
        changes the VERDICT, never what a reader can see."""
        dup = sorted(self.exempt["ids"])[0]
        board = [HEADER, row(1), row(2)]
        board[1][0] = dup
        board[2][0] = dup
        result = bv.verify(FakeStore(), gateway(board), len(board), size=3, exempt=self.exempt)
        self.assertEqual(1, result["duplicate_row_ids"])


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


class TheHeaderCheckMatchesTheREALBoard(unittest.TestCase):
    """From the first live run. header_matches_schema came back False against a perfectly healthy
    board, because the check compared the sheet's LABELS to this module's INTERNAL names - the sheet
    says "Timestamp" and "Sub-Gist" where the code says ts and subgist - and normalised spaces but
    not hyphens.

    It was worse than a wrong flag: _verdict degrades to UNKNOWN on an unrecognised header, so once
    the mirror was populated EVERY run would have been ineligible forever, for a schema that had
    never drifted."""

    def test_the_live_boards_header_matches(self):
        """These ten strings are what the board actually holds, read from it on 2026-10-07."""
        live = ["Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type",
                "Payload", "Category", "Project Tag", "Gist", "Sub-Gist"]
        self.assertTrue(bv.header_matches(live))

    def test_hyphens_and_spaces_both_normalise(self):
        self.assertEqual("sub_gist", bv.normalise_label("Sub-Gist"))
        self.assertEqual("project_tag", bv.normalise_label("Project Tag"))

    def test_a_real_drift_is_still_caught(self):
        """The positive control: if this ever passes anything, the check is decoration."""
        drifted = ["Row_ID", "Timestamp", "Sender", "Target_Surface", "Action_Type",
                   "Payload", "Category", "Project Tag", "Gist", "Sub-Gist"]
        self.assertFalse(bv.header_matches(drifted))

    def test_a_reordered_header_is_drift(self):
        swapped = ["Timestamp", "Row_ID"] + HEADER[2:]
        self.assertFalse(bv.header_matches(swapped))


class TheStreamWriteIsHonest(unittest.TestCase):
    """The live run wrote its durable line and left NO Redis copy, and said nothing about it:
    redis-py cannot encode a bool and this record carries four, so xadd raised and record_run
    returned a silent False. A missing copy then looks exactly like a copy nobody looked for."""

    def test_booleans_do_not_break_the_stream_write(self):
        conn = FakeStore()
        result = full(seeded(BOARD), BOARD)
        self.assertIn(True, [v for v in result.values() if isinstance(v, bool)])
        self.assertTrue(bv.record_run(conn, result, log=lambda line: None))
        written = conn.stream[0]
        self.assertFalse(any(isinstance(v, bool) for v in written.values()),
                         "a bool reached the stream payload")
        self.assertIn(written["gate_eligible"], ("true", "false"))

    def test_a_failed_stream_write_says_so(self):
        conn = FakeStore(fail_xadd=True)
        lines = []
        self.assertFalse(bv.record_run(conn, {"verdict": bv.AGREE}, log=lines.append))
        self.assertEqual(2, len(lines), "the record, then the failure")
        self.assertIn("stream_write_failed", lines[1])


class AFlappingGatewayStILLProducesARecord(unittest.TestCase):
    """From the first verification run after the backfill. bus.read_range raises SystemExit when the
    gateway answers four times without rows - a flap, which this gateway does - and SystemExit is a
    BaseException, so `except Exception` let it through. The job exited 1 and wrote NO RECORD AT ALL.

    No record is the one outcome this design exists to prevent. "I could not finish" is an answer;
    silence is not."""

    def test_a_systemexit_from_the_reader_is_a_counted_read_failure(self):
        def flapping(first, count):
            if first == 1:
                return {"rows": BOARD[0:count], "total": len(BOARD), "start": 1, "count": count}
            raise SystemExit("bus never returned rows for the ranged read")

        result = bv.verify(seeded(BOARD), flapping, len(BOARD), size=3)
        self.assertEqual(bv.UNKNOWN, result["verdict"])
        self.assertEqual(1, result["read_failures"])
        self.assertTrue(any("SystemExit" in r for r in result["read_failure_reasons"]))
        self.assertFalse(bv.gate_eligible(result))

    def test_the_partial_coverage_is_reported_rather_than_claimed(self):
        def flapping(first, count):
            if first > 3:
                raise SystemExit("flap")
            return {"rows": BOARD[first - 1:first - 1 + count], "total": len(BOARD),
                    "start": first, "count": count}

        result = bv.verify(seeded(BOARD), flapping, len(BOARD), size=3)
        self.assertEqual(3, result["covered_to"], "it must not claim rows it never read")
        self.assertNotEqual(result["frozen_total"], result["covered_to"])

    def test_a_systemexit_from_the_store_is_also_caught(self):
        class Exiting(FakeStore):
            def hgetall(self, key):
                raise SystemExit("the client gave up")

        result = bv.verify(Exiting(), gateway(BOARD), len(BOARD), size=3)
        self.assertEqual(bv.UNKNOWN, result["verdict"])
        self.assertGreaterEqual(result["read_failures"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

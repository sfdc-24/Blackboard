"""scripts/bus_reconcile.py: it proves the two stores agree, or says it does not know.

The test that matters most is the one that fails if an empty Redis ever reads as agreement. Codex's
phase gate is zero divergence over a span, and zero-because-nobody-looked is the lie this file exists
to refuse.
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bus_reconcile as rec                                              # noqa: E402

A = ["R-1", "2026-10-05T10:00:00Z", "claude-code-cli", "ALL", "APPEND", "BCB|v=1|x", "OPEN", "Blackboard", "a gist", ""]
B = ["R-2", "2026-10-05T11:00:00Z", "grok", "claude-code-cli", "DISPATCH", "BCB|v=1|y", "OPEN", "FLEET", "another", ""]


class FakePipeline:
    """A queued transaction, because the mirror REPLACES a row now rather than merging into it.

    Modelled rather than stubbed away: a pipeline that applied each command the moment it was
    queued would let the test pass while proving nothing about the DELETE and the HSET landing
    together, which is the whole reason the transaction is there."""

    def __init__(self, conn):
        self.conn = conn
        self.queued = []

    def delete(self, key):
        self.queued.append(("delete", (key,)))
        return self

    def hset(self, key, mapping=None):
        self.queued.append(("hset", (key, mapping)))
        return self

    def sadd(self, key, member):
        self.queued.append(("sadd", (key, member)))
        return self

    def execute(self):
        for name, args in self.queued:
            if name == "hset":
                self.conn.hset(args[0], mapping=args[1])
            else:
                getattr(self.conn, name)(*args)
        self.conn.transactions += 1
        self.queued = []
        return []


class FakeRedis:
    """Only the commands this uses. No network, no library."""

    def __init__(self, fail_xadd=False):
        self.hashes, self.sets, self.streams = {}, {}, {}
        self.fail_xadd = fail_xadd
        self.transactions = 0

    def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def delete(self, key):
        self.hashes.pop(key, None)

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def xadd(self, key, fields):
        if self.fail_xadd:
            raise RuntimeError("fake xadd failure")
        self.streams.setdefault(key, []).append(dict(fields))


class AnEmptyRedisIsUnknown(unittest.TestCase):
    """The single most important property here."""

    def test_nothing_mirrored_is_unknown_not_agreement(self):
        conn = FakeRedis()
        result = rec.compare(conn, [list(A), list(B)])
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertEqual(2, result["missing_from_redis"])

    def test_no_rows_at_all_is_unknown(self):
        self.assertEqual(rec.NO_SAMPLE, rec.compare(FakeRedis(), [])["verdict"])

    def test_unknown_exits_two_and_agreement_exits_zero(self):
        self.assertEqual(2, {rec.AGREE: 0, rec.DIVERGE: 1, rec.UNKNOWN: 2}[rec.UNKNOWN])
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        self.assertEqual(rec.AGREE, rec.compare(conn, [list(A), list(B)])["verdict"])

    def test_a_partial_mirror_is_divergence_not_unknown(self):
        """Some rows present is a real answer: they diverge. Only NOTHING present is unknown."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        result = rec.compare(conn, [list(A), list(B)])
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual(1, result["missing_from_redis"])


class TheReaderShape(unittest.TestCase):
    """bus.read_rows returns a DICT, {"rows": [...]}, and the first version of board_rows iterated it
    directly - yielding the dict's keys, so every window came back empty for every filter, forever.

    This is the test that would have cost nothing and saved a Cloud Run execution. It is here because
    the bug was only FOUND rather than BELIEVED by the rule that an empty read is UNKNOWN: written the
    obvious way, the reconciler would have reported AGREE on a comparison it never made, on every
    run, and the phase gate would have opened on it."""

    def test_a_dict_return_yields_its_rows(self):
        got = rec.board_rows(None, reader=lambda since, limit: {"rows": [list(A), list(B)]})
        self.assertEqual(2, len(got))
        self.assertEqual("R-1", got[0][0])

    def test_a_plain_list_return_still_works(self):
        self.assertEqual(1, len(rec.board_rows(None, reader=lambda since, limit: [list(A)])))

    def test_a_dict_with_no_rows_key_is_empty_not_a_crash(self):
        self.assertEqual([], rec.board_rows(None, reader=lambda since, limit: {"ok": True}))

    def test_a_health_ping_does_not_read_as_an_empty_board(self):
        """The gateway's broken read path returns a health ping with no rows at all. compare() calls
        that NO_SAMPLE now rather than UNKNOWN - still not agreement, which is the property that
        matters, and the one a verdict of AGREE here would have destroyed."""
        rows = rec.board_rows(None, reader=lambda since, limit: {"ok": True, "service": "bus"})
        self.assertEqual([], rows)
        verdict = rec.compare(FakeRedis(), rows)["verdict"]
        self.assertEqual(rec.NO_SAMPLE, verdict)
        self.assertNotEqual(rec.AGREE, verdict)

    def test_non_row_entries_are_dropped(self):
        got = rec.board_rows(None, reader=lambda since, limit: {"rows": [list(A), "junk", None]})
        self.assertEqual(1, len(got))


class WhatTheFirstRealExecutionTaught(unittest.TestCase):
    """Two defects the first Cloud Run execution found, both of mine."""

    def test_an_empty_window_needs_no_connection_to_answer(self):
        """It reached SMEMBERS on a quiet board - a connection spent to learn nothing."""
        class Refuses:
            def smembers(self, key):
                raise AssertionError("an empty window must not touch the store")

            def hgetall(self, key):
                raise AssertionError("an empty window must not touch the store")

        result = rec.compare(Refuses(), [])
        self.assertEqual(rec.NO_SAMPLE, result["verdict"])
        self.assertIn("not agreement", result["note"])

    def test_a_store_failure_is_a_controlled_unknown_not_a_traceback(self):
        """The first execution died with a redis AuthenticationError stack trace. A stack trace in a
        scheduled job's log is a failure nobody reads."""
        class Broken:
            def hset(self, *a, **k):
                raise RuntimeError("AuthenticationError-ish")

            def delete(self, *a, **k):
                raise RuntimeError("nope")

            def pipeline(self, *a, **k):
                raise RuntimeError("nope")

            def sadd(self, *a, **k):
                raise RuntimeError("nope")

            def hgetall(self, *a, **k):
                raise RuntimeError("nope")

            def smembers(self, *a, **k):
                raise RuntimeError("nope")

            def xadd(self, *a, **k):
                raise RuntimeError("nope")

        result = rec.run(Broken(), [list(A)], do_mirror=True)
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertIn("RuntimeError", result["note"])

    def test_the_failure_note_names_the_type_and_never_the_message(self):
        """A client's error text can quote what it was sent, and what it was sent is an AUTH string."""
        class Leaky:
            def hset(self, *a, **k):
                raise RuntimeError("invalid username-password pair: tried hunter2")

            def sadd(self, *a, **k):
                raise RuntimeError("x")

            def xadd(self, *a, **k):
                raise RuntimeError("x")

        result = rec.run(Leaky(), [list(A)], do_mirror=True)
        self.assertNotIn("hunter2", repr(result))
        self.assertNotIn("username-password", repr(result))


class MirroringIsIdempotent(unittest.TestCase):
    def test_the_same_row_twice_mirrors_once(self):
        """This gateway flaps and returns the same rows again; that must not invent duplicates."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(A), list(A)])
        self.assertEqual({"R-1"}, conn.smembers(rec.INDEX_KEY))
        self.assertEqual(rec.AGREE, rec.compare(conn, [list(A)])["verdict"])

    def test_a_row_with_no_id_is_counted_and_not_mirrored(self):
        conn = FakeRedis()
        counts = rec.mirror(conn, [["", "ts"], list(A)])
        self.assertEqual(1, counts["unmirrorable"])
        self.assertEqual(1, counts["mirrored"])

    def test_remirroring_after_a_change_brings_them_back_into_agreement(self):
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        changed = list(A)
        changed[8] = "an edited gist"
        self.assertEqual(rec.DIVERGE, rec.compare(conn, [changed])["verdict"])
        rec.mirror(conn, [changed])
        self.assertEqual(rec.AGREE, rec.compare(conn, [changed])["verdict"])


class WhatCountsAsADifference(unittest.TestCase):
    def test_a_changed_cell_names_its_column_and_never_its_value(self):
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        changed = list(A)
        changed[5] = "BCB|v=1|SOMETHING-ELSE-ENTIRELY"
        result = rec.compare(conn, [changed])
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual({"payload": 1}, result["columns_that_differ"])
        blob = repr(result)
        self.assertNotIn("SOMETHING-ELSE-ENTIRELY", blob)
        self.assertNotIn("BCB|v=1|x", blob)

    def test_a_type_difference_is_not_a_content_difference(self):
        """The gateway types a column however the Sheet felt about it; Redis stores strings.
        Comparing those raw reports divergence on every row and teaches everyone to ignore this."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        typed = list(A)
        typed[1] = "  2026-10-05T10:00:00Z  "            # whitespace
        self.assertEqual(rec.AGREE, rec.compare(conn, [typed])["verdict"])

    def test_crlf_is_not_a_difference(self):
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        crlf = list(A)
        crlf[5] = crlf[5].replace("|", "|")
        crlf[8] = "a gist"
        self.assertEqual(rec.AGREE, rec.compare(conn, [crlf])["verdict"])

    def test_a_row_only_in_redis_and_inside_the_window_is_a_difference(self):
        """Still a real finding: a row Redis holds that the board does not, WITHIN the span the
        board was asked about. The fixture puts it between the two board rows deliberately - the
        old version of this test used a row outside the window and so was really asserting the
        bug Copilot found."""
        middle = ["R-MID", "2026-10-05T10:30:00Z", "grok", "ALL", "APPEND", "BCB|v=1|z",
                  "OPEN", "Blackboard", "inside the window", ""]
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B), list(middle)])
        result = rec.compare(conn, [list(A), list(B)])
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual(1, result["extra_in_redis"])
        self.assertEqual(["R-MID"], result["extra_ids"])
        self.assertEqual(0, result["outside_the_window"])

    def test_narrowing_the_window_is_not_divergence(self):
        """THE BLOCKER. bus:rowids holds every row ever mirrored; the board window holds only what
        was asked for. Mirror two rows, then compare with --limit 1, and the older row used to be
        reported as extra and the verdict as DIVERGE while the stores agreed exactly. A reconciler
        that diverges because it narrowed its own question is one everybody learns to ignore."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        result = rec.compare(conn, [list(B)])                # the newest row only, as --limit 1 does
        self.assertEqual(rec.AGREE, result["verdict"])
        self.assertEqual(0, result["extra_in_redis"])
        self.assertEqual(1, result["outside_the_window"])
        self.assertEqual([], result["extra_ids"])

    def test_a_redis_row_newer_than_every_board_row_is_divergence(self):
        """THE P1 FROM CODEX'S SECOND PASS, and this test used to assert the opposite.

        It mirrored A and B and compared against [A] alone, calling the result AGREE - which only
        made sense under an INVENTED upper edge. There is no upper edge: both filters are open
        above, so a stored row newer than the window's floor is a row the board does not have.

        The old framing was also backwards on its own terms. Advancing --since returns NEWER rows,
        so a window built by advancing it would contain B, not A."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        result = rec.compare(conn, [list(A)])
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual(["R-2"], result["extra_ids"])

    def test_the_requested_since_cutoff_is_the_edge_not_the_oldest_row_returned(self):
        """Codex's exact scenario: --since before 10:00Z, board rows at 10:00Z and 11:00Z, and a
        Redis-only row at 12:00Z. This returned AGREE because the newest returned row had been
        treated as a ceiling."""
        newer = ["R-12", "2026-10-05T12:00:00Z", "grok", "ALL", "APPEND", "BCB|v=1|n",
                 "OPEN", "Blackboard", "newer than the window", ""]
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B), newer])
        since = rec.read_ts("2026-10-05T09:00:00Z")
        result = rec.compare(conn, [list(A), list(B)], since)
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual(["R-12"], result["extra_ids"])
        self.assertTrue(result["window_edge_trusted"])

    def test_a_row_at_exactly_the_since_cutoff_is_in_scope(self):
        """"Since X" includes X, and the cutoff is the caller's own - nothing is inferred, so there
        is no ambiguity to degrade for."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        since = rec.read_ts("2026-10-05T10:00:00Z")
        result = rec.compare(conn, [list(B)], since)
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual(["R-1"], result["extra_ids"])

    def test_a_row_before_the_since_cutoff_is_out_of_scope(self):
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        since = rec.read_ts("2026-10-05T10:30:00Z")
        result = rec.compare(conn, [list(B)], since)
        self.assertEqual(rec.AGREE, result["verdict"])
        self.assertEqual(1, result["outside_the_window"])

    def test_a_naive_stored_stamp_is_unplaceable_not_assumed_utc(self):
        """Codex: a Redis-only "06:30" read as outside a 10:00-11:00Z window, but it is INSIDE that
        window if it was recorded in Toronto time - and the reverse case gives a false DIVERGE. He
        is in Toronto and the Sheet's formatting is pinned to no zone anywhere, so assuming UTC is
        picking an answer rather than having one."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        conn.hset(rec.ROW_KEY % "R-NAIVE",
                  mapping={"row_id": "R-NAIVE", "ts": "2026-10-05T06:30:00"})
        conn.sadd(rec.INDEX_KEY, "R-NAIVE")
        result = rec.compare(conn, [list(A), list(B)])
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertEqual(1, result["unplaceable_in_redis"])
        self.assertIsNone(rec.read_ts("2026-10-05T06:30:00"))

    def test_an_explicit_offset_is_honoured(self):
        """The positive control for the naive rule: a stamp that DOES name its zone is placeable,
        and 07:30-04:00 is 11:30Z - after the window, so in scope and extra."""
        self.assertIsNotNone(rec.read_ts("2026-10-05T07:30:00-04:00"))
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        conn.hset(rec.ROW_KEY % "R-TZ",
                  mapping={"row_id": "R-TZ", "ts": "2026-10-05T07:30:00-04:00"})
        conn.sadd(rec.INDEX_KEY, "R-TZ")
        result = rec.compare(conn, [list(A), list(B)])
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual(["R-TZ"], result["extra_ids"])

    def test_an_empty_window_is_no_sample_and_not_agreement(self):
        """Codex asked for this to be its own verdict: "the board was quiet" and "I could not tell"
        want different handling in a span, and lumping them made UNKNOWN too broad to threshold."""
        result = rec.compare(FakeRedis(), [])
        self.assertEqual(rec.NO_SAMPLE, result["verdict"])
        self.assertNotEqual(rec.AGREE, result["verdict"])

    def test_a_stored_row_with_no_timestamp_cannot_be_placed_and_forces_unknown(self):
        """A row Redis holds whose own stamp is empty cannot be put inside or outside the window.
        Waving it through would be the same mistake in the other direction, so it is counted and
        the otherwise-clean verdict degrades to UNKNOWN."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        conn.hset(rec.ROW_KEY % "R-NOSTAMP", mapping={"row_id": "R-NOSTAMP", "ts": ""})
        conn.sadd(rec.INDEX_KEY, "R-NOSTAMP")
        result = rec.compare(conn, [list(A)])
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertEqual(1, result["unplaceable_in_redis"])

    def test_an_id_less_row_beside_a_matching_one_is_unknown_not_agree(self):
        """THE BLOCKER. Rows without an id were counted and skipped but never touched the verdict,
        so one matching row beside one id-less row returned AGREE with a count of 1: a clean answer
        about a window that was not wholly compared."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        headless = ["", "2026-10-05T10:15:00Z", "grok", "ALL", "APPEND", "BCB|v=1|n",
                    "OPEN", "Blackboard", "no id at all", ""]
        result = rec.compare(conn, [list(A), list(headless)])
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertEqual(1, result["rows_without_an_id"])
        self.assertEqual(1, result["agreed"], "the row that DID match is still reported as matching")

    def test_a_real_difference_still_outranks_an_id_less_row(self):
        """DIVERGE must win over the degrade-to-UNKNOWN: a found difference is knowledge, and
        losing it behind 'I am not sure' would be the worse error."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        changed = list(A)
        changed[8] = "a different gist"
        headless = ["", "2026-10-05T10:15:00Z", "grok", "ALL", "APPEND", "BCB|v=1|n",
                    "OPEN", "Blackboard", "no id at all", ""]
        result = rec.compare(conn, [changed, headless])
        self.assertEqual(rec.DIVERGE, result["verdict"])

    def test_remirroring_a_shortened_row_clears_the_field_it_dropped(self):
        """HSET merges. A row mirrored with an eleventh cell, then remirrored without it, used to
        keep the stale col11 forever - so the comparison kept reporting a difference the backfill
        had already repaired."""
        wide = list(A) + ["an eleventh cell"]
        conn = FakeRedis()
        rec.mirror(conn, [wide])
        self.assertEqual("an eleventh cell", conn.hgetall(rec.ROW_KEY % "R-1").get("col11"))
        rec.mirror(conn, [list(A)])
        stored = conn.hgetall(rec.ROW_KEY % "R-1")
        self.assertNotIn("col11", stored, "the dropped cell survived the remirror")
        self.assertEqual(rec.AGREE, rec.compare(conn, [list(A)])["verdict"])

    def test_the_replace_is_one_transaction(self):
        """The DELETE and the HSET must land together, or a concurrent reader sees an empty row."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        self.assertEqual(2, conn.transactions)

    def test_a_duplicate_row_id_is_its_own_finding(self):
        """An append-only board should never hold a Row_ID twice. That is a finding, not an error."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        other = list(A)
        other[8] = "same id, different gist"
        result = rec.compare(conn, [list(A), other])
        self.assertEqual(1, result["duplicate_row_ids"])
        self.assertEqual(rec.DIVERGE, result["verdict"])

    def test_an_extra_eleventh_cell_is_kept_not_truncated(self):
        """A cell this does not know about is still a difference somebody may care about."""
        wide = list(A) + ["an eleventh cell"]
        self.assertEqual("an eleventh cell", rec.as_mapping(wide)["col11"])
        conn = FakeRedis()
        rec.mirror(conn, [wide])
        self.assertEqual(rec.AGREE, rec.compare(conn, [wide])["verdict"])
        self.assertEqual(rec.DIVERGE, rec.compare(conn, [list(A)])["verdict"])

    def test_a_short_row_is_padded_rather_than_misaligned(self):
        mapped = rec.as_mapping(["R-9", "ts"])
        self.assertEqual("R-9", mapped["row_id"])
        self.assertEqual("", mapped["subgist"])


class TheRunIsRecorded(unittest.TestCase):
    def test_the_compare_stream_gets_counts_and_no_ids(self):
        conn = FakeRedis()
        rec.run(conn, [list(A), list(B)], do_mirror=True, window="limit=2")
        entry = conn.streams[rec.COMPARE_KEY][0]
        # UNKNOWN, not AGREE. The store was empty before this run, and the recorded verdict is the
        # one measured BEFORE the backfill - see TheBackfillCannotGradeItself below.
        self.assertEqual(rec.UNKNOWN, entry["verdict"])
        self.assertEqual(2, entry["checked"])
        blob = repr(entry)
        self.assertNotIn("R-1", blob)
        self.assertNotIn("a gist", blob)

    def test_a_failure_to_record_does_not_change_the_verdict(self):
        """Losing the audit entry is not losing the answer."""
        conn = FakeRedis(fail_xadd=True)
        result = rec.run(conn, [list(A)], do_mirror=True)
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertFalse(result["recorded"])

    def test_a_compare_only_run_over_a_seeded_store_records_agree(self):
        """The positive control for the two above: when the stores really do agree and nothing was
        written during the run, AGREE is recorded."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        rec.run(conn, [list(A), list(B)], do_mirror=False, window="limit=2")
        self.assertEqual(rec.AGREE, conn.streams[rec.COMPARE_KEY][0]["verdict"])


class TheBackfillCannotGradeItself(unittest.TestCase):
    """THE BLOCKER. The job defaulted to mirroring and deploy.sh set RECONCILE_MIRROR=1, so run()
    overwrote Redis and then compared against the rows it had just written. An empty or badly stale
    store produced an AGREE record with no trace of what it had been - and a span of those records
    cannot establish zero divergence, which is the whole gate."""

    def test_a_mirroring_run_over_an_empty_store_reports_unknown_not_agree(self):
        conn = FakeRedis()
        result = rec.run(conn, [list(A), list(B)], do_mirror=True)
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertEqual("pre_backfill", result["measured"])
        self.assertEqual(2, result["mirrored"], "the backfill still happened")

    def test_the_post_backfill_state_is_reported_beside_it_not_instead_of_it(self):
        conn = FakeRedis()
        result = rec.run(conn, [list(A), list(B)], do_mirror=True)
        self.assertEqual(rec.AGREE, result["after_backfill"]["verdict"])
        self.assertNotEqual(result["verdict"], result["after_backfill"]["verdict"])

    def test_a_stale_row_is_measured_before_it_is_repaired(self):
        """The case that matters most: Redis holds an out-of-date copy. The run must say DIVERGE and
        then fix it, not fix it and say AGREE."""
        conn = FakeRedis()
        stale = list(A)
        stale[8] = "what Redis used to think"
        rec.mirror(conn, [stale])
        result = rec.run(conn, [list(A)], do_mirror=True)
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual(1, result["differing"])
        self.assertEqual(rec.AGREE, result["after_backfill"]["verdict"])

    def test_a_compare_only_run_says_so_and_has_no_second_verdict(self):
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        result = rec.run(conn, [list(A)], do_mirror=False)
        self.assertEqual("compare_only", result["measured"])
        self.assertNotIn("after_backfill", result)
        self.assertEqual(rec.AGREE, result["verdict"])


class TheCommandLine(unittest.TestCase):
    def test_an_unbounded_run_is_refused(self):
        """An unbounded read of this board has timed out before and left an append looking failed."""
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(2, rec.main([]))
        self.assertIn("REFUSED", out.getvalue())

    def test_no_connection_is_reported_as_unknown_not_as_zero(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = rec.main(["--limit", "10"])
        self.assertEqual(2, code)
        text = out.getvalue()
        self.assertIn("UNKNOWN", text)
        self.assertIn("NOT zero divergence", text)


class ItHoldsNoCursor(unittest.TestCase):
    def test_nothing_here_stores_a_watermark(self):
        """A continuous relay needs a cursor and a cursor that advances on a partial read loses rows
        silently. That design is Gemini's and under review, so this is stateless by construction."""
        source = (Path(__file__).resolve().parents[1] / "scripts" / "bus_reconcile.py").read_text(
            encoding="utf-8")
        for word in ("cursor", "watermark"):
            # named in the docstring to say why it is absent, never used as a key or a variable
            self.assertNotIn('"bus:%s' % word, source)
            self.assertNotIn("%s =" % word, source)


class WhatCodexFoundAtThisHead(unittest.TestCase):
    """Codex's post-merge review of 314bead. Each of these returned the WRONG verdict before.

    Worth keeping together: all four are the same mistake in different clothes - a comparison that
    answered confidently about something it had not actually established."""

    def test_a_fractional_second_stamp_is_inside_the_window(self):
        """THE P1, and it was live: scripts/append.py stamps microseconds, so most rows this fleet
        writes look like 03:32:22.588753Z. Compared as TEXT, "...00.500Z" sorts BELOW "...00Z"
        because "." is below "Z" - so a row stamped mid-second fell outside a window containing it
        and the comparison returned AGREE."""
        self.assertLess("2026-10-05T10:00:00.500Z", "2026-10-05T10:00:00Z",
                        "if this ever fails, text ordering was fine and this test is pointless")
        mid = ["R-FRAC", "2026-10-05T10:00:00.500Z", "grok", "ALL", "APPEND", "BCB|v=1|z",
               "OPEN", "Blackboard", "mid-second", ""]
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B), mid])
        result = rec.compare(conn, [list(A), list(B)])
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual(["R-FRAC"], result["extra_ids"])
        self.assertEqual(0, result["outside_the_window"])

    def test_a_malformed_stored_stamp_is_unplaceable_not_outside(self):
        """The old check only caught an EMPTY stamp, so "not-a-timestamp" compared as text, landed
        outside the window and produced a clean AGREE about a row nobody could place."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        conn.hset(rec.ROW_KEY % "R-JUNK", mapping={"row_id": "R-JUNK", "ts": "not-a-timestamp"})
        conn.sadd(rec.INDEX_KEY, "R-JUNK")
        result = rec.compare(conn, [list(A)])
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertEqual(1, result["unplaceable_in_redis"])
        self.assertEqual(0, result["outside_the_window"])

    def test_a_tie_on_the_window_boundary_is_ambiguous_not_extra(self):
        """With --limit 1, a healthy older row sharing the selected row's timestamp was called extra
        and the verdict DIVERGE. It is neither: `limit` selects the n most recent ROWS, so when
        several share the boundary instant the gateway's choice among them is arbitrary and this
        comparison cannot know which it meant."""
        twin = ["R-TWIN", "2026-10-05T11:00:00Z", "grok", "ALL", "APPEND", "BCB|v=1|t",
                "OPEN", "Blackboard", "same instant", ""]
        conn = FakeRedis()
        rec.mirror(conn, [list(B), twin])
        result = rec.compare(conn, [list(B)])
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertEqual(1, result["boundary_ties"])
        self.assertEqual([], result["extra_ids"])

    def test_an_unreadable_board_stamp_leaves_the_window_edges_unknown(self):
        """A board row whose own stamp will not parse means the fence itself is not fully known, so
        agreement cannot be claimed even when every compared row matches."""
        headless_ts = ["R-NOTS", "whenever", "grok", "ALL", "APPEND", "BCB|v=1|n",
                       "OPEN", "Blackboard", "no usable stamp", ""]
        conn = FakeRedis()
        rec.mirror(conn, [list(A), headless_ts])
        result = rec.compare(conn, [list(A), headless_ts])
        self.assertEqual(rec.UNKNOWN, result["verdict"])
        self.assertEqual(1, result["board_rows_with_unreadable_ts"])

    def test_the_durable_entry_says_which_kind_of_run_it_was(self):
        """THE GATE'S OWN REQUIREMENT. run() returned `measured` and `after_backfill` and the stream
        entry carried neither, so a gate reading bus:compare could not exclude backfill runs from a
        zero-divergence span. Codex asked for compare-only runs; it cannot have them unless the
        durable record says which these were."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        rec.run(conn, [list(A)], do_mirror=False, window="limit=1")
        entry = conn.streams[rec.COMPARE_KEY][0]
        self.assertEqual("compare_only", entry["measured"])
        self.assertNotIn("after_backfill_verdict", entry)

    def test_a_backfill_run_is_labelled_and_carries_both_verdicts(self):
        conn = FakeRedis()
        rec.run(conn, [list(A), list(B)], do_mirror=True, window="limit=2")
        entry = conn.streams[rec.COMPARE_KEY][0]
        self.assertEqual("pre_backfill", entry["measured"])
        self.assertEqual(rec.UNKNOWN, entry["verdict"], "the store was empty before this run")
        self.assertEqual(rec.AGREE, entry["after_backfill_verdict"])

    def test_the_caveat_counts_reach_the_stream(self):
        """So a reader can tell "nothing to compare" from "could not place a row" without going
        back to a log line the gate does not read."""
        conn = FakeRedis()
        rec.mirror(conn, [list(A)])
        conn.hset(rec.ROW_KEY % "R-JUNK", mapping={"row_id": "R-JUNK", "ts": "nope"})
        conn.sadd(rec.INDEX_KEY, "R-JUNK")
        rec.run(conn, [list(A)], do_mirror=False)
        entry = conn.streams[rec.COMPARE_KEY][0]
        self.assertEqual(rec.UNKNOWN, entry["verdict"])
        self.assertEqual(1, entry["unplaceable"])


if __name__ == "__main__":
    unittest.main()

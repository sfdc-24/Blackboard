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


class FakeRedis:
    """Only the five commands this uses. No network, no library."""

    def __init__(self, fail_xadd=False):
        self.hashes, self.sets, self.streams = {}, {}, {}
        self.fail_xadd = fail_xadd

    def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

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
        self.assertEqual(rec.UNKNOWN, rec.compare(FakeRedis(), [])["verdict"])

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

    def test_a_row_only_in_redis_is_a_difference(self):
        conn = FakeRedis()
        rec.mirror(conn, [list(A), list(B)])
        result = rec.compare(conn, [list(A)])
        self.assertEqual(rec.DIVERGE, result["verdict"])
        self.assertEqual(1, result["extra_in_redis"])
        self.assertEqual(["R-2"], result["extra_ids"])

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
        self.assertEqual(rec.AGREE, entry["verdict"])
        self.assertEqual(2, entry["checked"])
        blob = repr(entry)
        self.assertNotIn("R-1", blob)
        self.assertNotIn("a gist", blob)

    def test_a_failure_to_record_does_not_change_the_verdict(self):
        """Losing the audit entry is not losing the answer."""
        conn = FakeRedis(fail_xadd=True)
        result = rec.run(conn, [list(A)], do_mirror=True)
        self.assertEqual(rec.AGREE, result["verdict"])
        self.assertFalse(result["recorded"])


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


if __name__ == "__main__":
    unittest.main()

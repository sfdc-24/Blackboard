"""R-STATUS compatibility: every bus reader must keep working once rows carry
18 cells (columns A to R) instead of 12 (A to L). docs/BOARD-STATUS-FRAMEWORK.md,
"Further requirements": tests must prove this BEFORE columns M to R exist for
real. scripts/bcb_lint.py's own tolerance fix has its tests in test_bcb_lint.py;
this file covers append.py's read-back, bus.py, and open_for_me.py.
scripts/board_row.py does no row-length indexing at all (it only ever handles
payload text and the spill store), so it has nothing to compat-test here."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import append  # noqa: E402
import bus  # noqa: E402
import open_for_me  # noqa: E402

GOOD10 = ["RID-1", "2026-11-14T00:00:00.000Z", "claude-code-cli", "ALL", "APPEND",
          "BCB|v=1|id=TEST-1|phase=RESULT|from=claude-code-cli|to=ALL", "", "SFDC24", "a gist", "a sub-gist"]
# K, L (sender stamp), M, N, O, P, Q, R - 8 cells, 10 + 8 = 18.
STATUS_TAIL = ["", "sender=unverified", "OPEN", "grok", "ack: result",
              "2026-11-20T00:00:00Z", "", ""]
STATUS_18 = GOOD10 + STATUS_TAIL
assert len(STATUS_18) == 18


class TheAppendReadBack(unittest.TestCase):
    """append.main()'s read-back compares only the first 10 cells (skipping
    index 1, the timestamp it stamped itself) and never indexes past what it
    is given - an 18-cell hit must read back exactly as cleanly as a 12-cell
    one."""

    def _run(self, hit_row, require_sender_identity=False):
        spec = {"row_id": "RID-1", "source_tag": "claude-code-cli",
                "target_surface": GOOD10[3], "action_type": GOOD10[4],
                "payload": GOOD10[5], "category": GOOD10[6],
                "project_tag": GOOD10[7], "gist": GOOD10[8], "subgist": GOOD10[9]}
        handle, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
        argv = sys.argv
        sys.argv = ["append.py", path]
        buf = io.StringIO()
        try:
            with mock.patch.object(append, "fetch", return_value='{"ok": true}'), \
                 mock.patch.object(append, "load_env",
                                   return_value={"BUS_URL": "https://example.invalid", "BUS_SECRET": "s"}), \
                 mock.patch.object(append, "read_rows", return_value={"rows": [hit_row]}), \
                 mock.patch.object(append, "with_sender_token", side_effect=lambda body, **kw: body), \
                 redirect_stdout(buf):
                append.main(require_sender_identity=require_sender_identity)
        finally:
            sys.argv = argv
            os.unlink(path)
        return buf.getvalue()

    def test_an_18_cell_hit_reads_back_ok(self):
        row = ["RID-1"] + STATUS_18[1:]
        out = self._run(row)
        self.assertIn("READ-BACK OK: row present exactly once, 18 cells", out)

    def test_an_18_cell_hit_still_honours_the_sender_stamp_at_column_l(self):
        row = ["RID-1"] + STATUS_18[1:]
        row[11] = "sender=verified;who=bus-reconciler@sfdc24.iam.gserviceaccount.com"
        out = self._run(row, require_sender_identity=True)
        self.assertIn("READ-BACK OK", out)

    def test_an_18_cell_row_with_changed_content_in_the_first_10_is_still_a_conflict(self):
        row = ["RID-1"] + STATUS_18[1:]
        row[5] = "BCB|v=1|id=TEST-1|phase=RESULT|from=someone-else|to=ALL"
        with self.assertRaises(SystemExit) as ctx:
            self._run(row)
        self.assertIn("READ-BACK CONFLICT", str(ctx.exception))

    def test_an_18_cell_row_where_only_m_to_r_differ_is_not_a_conflict(self):
        # The reconciler is the only writer of M-R; a row read back with those
        # six cells filled in (by the reconciler, after append.py's own write)
        # must not look like append.py's own content changed.
        row = ["RID-1"] + STATUS_18[1:]
        row[12:18] = ["CLOSED", "grok", "decision: done", "2026-11-01T00:00:00Z", "R2", "2026-11-02T00:00:00Z"]
        out = self._run(row)
        self.assertIn("READ-BACK OK", out)


class TheBusSenderCheck(unittest.TestCase):
    """bus.sender_check() reads column L (SENDER_COLUMN, index 11) by position -
    unaffected by anything appended after it, but worth proving directly since
    a future "just append more columns past L" change could tempt someone to
    move it."""

    def test_sender_check_still_reads_column_l_on_an_18_cell_row(self):
        row = list(STATUS_18)
        row[11] = "sender=verified;who=x@sfdc24.iam.gserviceaccount.com"
        self.assertEqual(("verified", "who=x@sfdc24.iam.gserviceaccount.com"), bus.sender_check(row))

    def test_an_unstamped_18_cell_row_is_still_unstamped(self):
        row = list(STATUS_18)
        row[11] = ""
        self.assertEqual(("unstamped", ""), bus.sender_check(row))


class TheOpenForMeIngest(unittest.TestCase):
    """open_for_me.load_board() must accept a header that has grown to the
    R-STATUS names in M1:R1, keep refusing one with something else there, and
    its row validator (MAX_ROW_CELLS=64) must accept an 18-cell data row."""

    def _envelope(self, header_tail):
        header = list(open_for_me.BOARD_HEADER) + header_tail
        return {"ok": True, "title": open_for_me.BOARD_TITLE, "rows": [header, STATUS_18]}

    def test_a_legacy_header_with_no_status_columns_still_loads(self):
        rows = open_for_me.load_board(self._envelope(["", ""]))
        self.assertEqual(1, len(rows))

    def test_a_header_with_the_real_status_names_in_m_to_r_loads(self):
        tail = ["", ""] + list(open_for_me.STATUS_HEADER_NAMES)
        rows = open_for_me.load_board(self._envelope(tail))
        self.assertEqual(1, len(rows))

    def test_a_header_with_the_sender_stamp_column_l_blank_still_loads(self):
        # L never carries a header NAME (the sender stamp is a per-row value) -
        # this just proves the K/L slice is still required blank, not silently
        # widened along with M-R.
        tail = ["", ""] + list(open_for_me.STATUS_HEADER_NAMES)
        rows = open_for_me.load_board(self._envelope(tail))
        self.assertEqual(1, len(rows))

    def test_a_header_with_wrong_text_in_the_status_columns_is_refused(self):
        tail = ["", "", "NotStatus", "", "", "", "", ""]
        with self.assertRaises(open_for_me.BoardError):
            open_for_me.load_board(self._envelope(tail))

    def test_a_header_with_real_content_past_column_r_is_still_refused(self):
        tail = ["", ""] + list(open_for_me.STATUS_HEADER_NAMES) + ["leaked"]
        with self.assertRaises(open_for_me.BoardError):
            open_for_me.load_board(self._envelope(tail))

    def test_an_18_cell_data_row_is_well_within_the_64_cell_cap(self):
        tail = ["", ""] + list(open_for_me.STATUS_HEADER_NAMES)
        rows = open_for_me.load_board(self._envelope(tail))
        self.assertEqual(18, len(rows[0]))

    def test_open_for_still_resolves_gist_and_category_by_their_fixed_low_index(self):
        # Rule 5 (Category stays a free label) means an ask-type row keeps
        # Category=OPEN regardless of M-R - open_for() must still see it.
        # Source_Tag/from=grok, addressed to claude-code-cli (a row this tag
        # itself wrote is never its own work, per open_for()'s own rule).
        row = list(STATUS_18)
        row[2] = "grok"          # Source_Tag
        row[3] = "claude-code-cli"   # Target_Surface
        row[4] = "ASK"           # Action_Type (irrelevant to open_for, kept realistic)
        row[5] = "BCB|v=1|id=TEST-ASK|phase=ASK|from=grok|to=claude-code-cli"
        row[6] = "OPEN"          # Category
        tail = ["", ""] + list(open_for_me.STATUS_HEADER_NAMES)
        header = list(open_for_me.BOARD_HEADER) + tail
        rows = open_for_me.load_board({"ok": True, "title": open_for_me.BOARD_TITLE, "rows": [header, row]})
        self.assertEqual(18, len(rows[0]), "the compat fixture itself must stay 18 cells wide")
        out = open_for_me.open_for(rows, "claude-code-cli")
        self.assertEqual(1, len(out["rows"]))
        self.assertEqual("TEST-ASK", out["rows"][0]["id"])


if __name__ == "__main__":
    unittest.main()

"""cloud/status-reconciler/main.py's pure helpers: the previous-state read
(by Row_ID - adversarial review finding #1), the current-state read, the
diff that decides what actually needs a write, and that --dry makes no
write/StatusClient call at all."""
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import rstatus_reconcile as rs  # noqa: E402

# A bare `import main` would collide with every OTHER cloud job's main.py that
# some other test file has already imported under that same top-level name
# (sys.modules caches by name, not by path) - confirmed under
# `unittest discover`: it silently imported the wrong module and every test
# failed with AttributeError. Switched to spec_from_file_location with a
# unique name, matching the established convention (test_board_watcher.py,
# test_drive_intake.py, test_wa_outbox_cloud.py, ...).
_spec = importlib.util.spec_from_file_location(
    "status_reconciler_main", REPO / "cloud" / "status-reconciler" / "main.py")
recon_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recon_main)


def row(row_id, payload, status_tail=None):
    """row_id is the sheet's OWN Row_ID (column A) - deliberately NOT the
    same string as the payload's BCB id= in most of these tests, matching
    the real board (finding #1: 6,186 of 20,163 BCB ids are not themselves a
    Row_ID)."""
    base = [row_id, "2026-11-14T00:00:00Z", "claude-code-cli", "ALL", "APPEND", payload, "OPEN",
           "", "", "", "", ""]
    return base + (status_tail or ["", "", "", "", "", ""])


class ThePreviousResults(unittest.TestCase):
    def test_reads_by_row_id_not_by_the_payloads_bcb_id(self):
        rows = [row("sheet-row-1", "BCB|v=1|id=SOME-OTHER-BCB-ID|phase=RESULT",
                   ["ANSWERED", "", "", "", "B", "2026-11-14T00:00:00Z"])]
        previous = recon_main.previous_results(rows)
        self.assertIn("sheet-row-1", previous)
        self.assertNotIn("SOME-OTHER-BCB-ID", previous)
        self.assertEqual("ANSWERED", previous["sheet-row-1"]["status"])

    def test_parses_due_utc_and_status_updated_utc_back_into_datetimes(self):
        rows = [row("R1", "BCB|v=1|id=A|phase=ASK",
                   ["OPEN", "grok", "result: x", "2026-11-20T00:00:00Z", "", "2026-11-14T00:05:00Z"])]
        previous = recon_main.previous_results(rows)
        self.assertEqual(rs.parse_ts("2026-11-20T00:00:00Z"), previous["R1"]["due_utc"])
        self.assertEqual(rs.parse_ts("2026-11-14T00:05:00Z"), previous["R1"]["status_updated_utc"])
        self.assertEqual("", previous["R1"]["closed_by"] or "")

    def test_a_row_short_of_column_m_has_no_previous_result_at_all(self):
        rows = [["R1", "2026-11-14T00:00:00Z", "x", "ALL", "APPEND", "BCB|v=1|id=A|phase=RESULT", "OPEN"]]
        self.assertEqual({}, recon_main.previous_results(rows))

    def test_a_row_with_no_row_id_is_skipped(self):
        rows = [["", "2026-11-14T00:00:00Z", "x", "ALL", "APPEND", "BCB|v=1|id=A|phase=RESULT", "OPEN"]]
        self.assertEqual({}, recon_main.previous_results(rows))


class TheCurrentValues(unittest.TestCase):
    def test_reads_all_six_m_to_r_cells_by_row_id(self):
        rows = [row("sheet-row-1", "BCB|v=1|id=A|phase=RESULT",
                    ["ANSWERED", "grok", "result: x", "2026-11-20T00:00:00Z", "B", "2026-11-14T00:00:00Z"])]
        self.assertEqual(
            {"sheet-row-1": ["ANSWERED", "grok", "result: x", "2026-11-20T00:00:00Z", "B",
                            "2026-11-14T00:00:00Z"]},
            recon_main.current_sheet_values(rows))


class TheDiff(unittest.TestCase):
    def test_an_unchanged_result_is_not_in_the_update_list(self):
        results = {"R1": {"status": "INFO", "expected_from": None, "expected_response": None,
                          "due_utc": None, "closed_by": None, "status_updated_utc": None}}
        current = {"R1": ["INFO", "", "", "", "", ""]}
        self.assertEqual([], recon_main.updates_for(results, current))

    def test_a_changed_result_is_queued_with_the_new_values_under_its_row_id(self):
        results = {"R1": {"status": "ANSWERED", "expected_from": "grok", "expected_response": None,
                          "due_utc": None, "closed_by": "B", "status_updated_utc": None}}
        current = {"R1": ["OPEN", "grok", "", "", "", ""]}
        updates = recon_main.updates_for(results, current)
        self.assertEqual([{"row_id": "R1", "values": ["ANSWERED", "grok", "", "", "B", ""]}], updates)

    def test_a_brand_new_row_id_with_no_current_value_is_queued(self):
        results = {"R1": {"status": "INFO", "expected_from": None, "expected_response": None,
                          "due_utc": None, "closed_by": None, "status_updated_utc": None}}
        updates = recon_main.updates_for(results, {})
        self.assertEqual([{"row_id": "R1", "values": ["INFO", "", "", "", "", ""]}], updates)


class TheDryRun(unittest.TestCase):
    def test_dry_makes_no_statusclient_call(self):
        rows = [row("R1", "BCB|v=1|id=A|phase=RESULT")]
        with mock.patch.object(recon_main, "read_board", return_value=({"BUS_URL": "u", "BUS_SECRET": "s"}, rows)), \
             mock.patch.object(recon_main, "StatusClient") as client_cls:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = recon_main.main(["--dry"])
        self.assertEqual(0, code)
        client_cls.assert_not_called()
        out = json.loads(buf.getvalue())
        self.assertTrue(out["dry"])
        self.assertIn("by_status", out)
        self.assertIn("reverify", out)

    def test_a_live_run_with_nothing_changed_never_calls_write(self):
        # status_updated_utc for an INFO row is the row's own fixed timestamp
        # (never "now") - matching it here is what a real prior write left behind,
        # not a placeholder.
        rows = [row("R1", "BCB|v=1|id=A|phase=RESULT", ["INFO", "", "", "", "", "2026-11-14T00:00:00Z"])]
        with mock.patch.object(recon_main, "read_board", return_value=({"BUS_URL": "u", "BUS_SECRET": "s"}, rows)), \
             mock.patch.object(recon_main, "StatusClient") as client_cls:
            code = recon_main.main([])
        self.assertEqual(0, code)
        client_cls.return_value.write.assert_not_called()

    def test_a_live_run_with_a_real_change_calls_write_once_keyed_by_row_id(self):
        rows = [row("sheet-row-1", "BCB|v=1|id=SOME-BCB-ID|phase=ASK|due=2099-01-01T00:00:00Z")]
        fake_client = mock.Mock()
        fake_client.write.return_value = {"written": 1, "misses": []}
        with mock.patch.object(recon_main, "read_board", return_value=({"BUS_URL": "u", "BUS_SECRET": "s"}, rows)), \
             mock.patch.object(recon_main, "StatusClient", return_value=fake_client):
            code = recon_main.main([])
        self.assertEqual(0, code)
        fake_client.write.assert_called_once()
        updates = fake_client.write.call_args[0][0]
        self.assertEqual(["sheet-row-1"], [u["row_id"] for u in updates],
                         "the gateway matches column A Row_ID, never the payload's BCB id=")
        self.assertEqual("OPEN", updates[0]["values"][0])

    def test_a_second_run_with_nothing_changed_carries_the_due_and_updated_forward(self):
        # End-to-end idempotence (findings #7/#9) through main(), not just
        # rstatus_reconcile directly: a second run reading back its own first
        # write must queue no update at all.
        rows = [row("sheet-row-1", "BCB|v=1|id=A|phase=ASK|due=2099-01-01T00:00:00Z")]
        first_result = rs.reconcile_all(rows, now=datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc))
        values = rs.to_row_values(first_result["sheet-row-1"])
        rows[0][12:18] = values
        with mock.patch.object(recon_main, "read_board",
                               return_value=({"BUS_URL": "u", "BUS_SECRET": "s"}, rows)), \
             mock.patch.object(recon_main, "StatusClient") as client_cls:
            code = recon_main.main([])
        self.assertEqual(0, code)
        client_cls.return_value.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()

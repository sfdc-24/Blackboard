"""cloud/status-reconciler/main.py's pure helpers: the previous-state read, the
current-state read, the diff that decides what actually needs a write, and
that --dry makes no write/StatusClient call at all."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud" / "status-reconciler"))
import main as recon_main  # noqa: E402
import rstatus_reconcile as rs  # noqa: E402


def row(rid, payload, status_tail=None):
    base = [rid, "2026-11-14T00:00:00Z", "claude-code-cli", "ALL", "APPEND", payload, "OPEN",
           "", "", "", "", ""]
    return base + (status_tail or ["", "", "", "", "", ""])


class ThePreviousStatus(unittest.TestCase):
    def test_reads_column_m_by_id_not_by_position(self):
        rows = [row("R1", "BCB|v=1|id=A|phase=RESULT", ["ANSWERED", "", "", "", "B", ""])]
        self.assertEqual({"A": "ANSWERED"}, recon_main.previous_statuses(rows))

    def test_a_row_short_of_column_m_has_no_previous_status(self):
        rows = [["R1", "2026-11-14T00:00:00Z", "x", "ALL", "APPEND", "BCB|v=1|id=A|phase=RESULT", "OPEN"]]
        self.assertEqual({"A": None}, recon_main.previous_statuses(rows))

    def test_a_row_with_no_bcb_id_is_skipped(self):
        rows = [row("R1", "not bcb at all")]
        self.assertEqual({}, recon_main.previous_statuses(rows))


class TheCurrentValues(unittest.TestCase):
    def test_reads_all_six_m_to_r_cells(self):
        rows = [row("R1", "BCB|v=1|id=A|phase=RESULT",
                    ["ANSWERED", "grok", "result: x", "2026-11-20T00:00:00Z", "B", "2026-11-14T00:00:00Z"])]
        self.assertEqual(
            {"A": ["ANSWERED", "grok", "result: x", "2026-11-20T00:00:00Z", "B", "2026-11-14T00:00:00Z"]},
            recon_main.current_sheet_values(rows))


class TheDiff(unittest.TestCase):
    def test_an_unchanged_result_is_not_in_the_update_list(self):
        results = {"A": {"status": "INFO", "expected_from": None, "expected_response": None,
                         "due_utc": None, "closed_by": None, "status_updated_utc": None}}
        current = {"A": ["INFO", "", "", "", "", ""]}
        self.assertEqual([], recon_main.updates_for([], results, current))

    def test_a_changed_result_is_queued_with_the_new_values(self):
        results = {"A": {"status": "ANSWERED", "expected_from": "grok", "expected_response": None,
                         "due_utc": None, "closed_by": "B", "status_updated_utc": None}}
        current = {"A": ["OPEN", "grok", "", "", "", ""]}
        updates = recon_main.updates_for([], results, current)
        self.assertEqual([{"row_id": "A", "values": ["ANSWERED", "grok", "", "", "B", ""]}], updates)

    def test_a_brand_new_id_with_no_current_value_is_queued(self):
        results = {"A": {"status": "INFO", "expected_from": None, "expected_response": None,
                         "due_utc": None, "closed_by": None, "status_updated_utc": None}}
        updates = recon_main.updates_for([], results, {})
        self.assertEqual([{"row_id": "A", "values": ["INFO", "", "", "", "", ""]}], updates)


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

    def test_a_live_run_with_a_real_change_calls_write_once(self):
        rows = [row("R1", "BCB|v=1|id=A|phase=ASK|due=2099-01-01T00:00:00Z")]
        fake_client = mock.Mock()
        fake_client.write.return_value = {"written": 1, "misses": []}
        with mock.patch.object(recon_main, "read_board", return_value=({"BUS_URL": "u", "BUS_SECRET": "s"}, rows)), \
             mock.patch.object(recon_main, "StatusClient", return_value=fake_client):
            code = recon_main.main([])
        self.assertEqual(0, code)
        fake_client.write.assert_called_once()
        updates = fake_client.write.call_args[0][0]
        self.assertEqual(["A"], [u["row_id"] for u in updates])
        self.assertEqual("OPEN", updates[0]["values"][0])


if __name__ == "__main__":
    unittest.main()

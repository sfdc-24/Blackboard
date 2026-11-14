"""scripts/status_sheet_setup.py: --dry by default, makes no call at all."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import status_sheet_setup as setup  # noqa: E402


class TheDryDefault(unittest.TestCase):
    def test_no_flag_is_dry_and_calls_nothing(self):
        with mock.patch.object(setup, "StatusClient") as client_cls, \
             mock.patch.object(sys, "argv", ["status_sheet_setup.py"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = setup.main()
        self.assertEqual(0, code)
        client_cls.assert_not_called()
        out = json.loads(buf.getvalue())
        self.assertTrue(out["dry"])

    def test_live_calls_setup_and_prints_the_report(self):
        fake_client = mock.Mock()
        fake_client.setup.return_value = {"headers": True, "validation": True,
                                          "protection": True, "filter_view": True}
        with mock.patch.object(setup, "load_env", return_value={"BUS_URL": "u", "BUS_SECRET": "s"}), \
             mock.patch.object(setup, "StatusClient", return_value=fake_client), \
             mock.patch.object(sys, "argv", ["status_sheet_setup.py", "--live"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = setup.main()
        self.assertEqual(0, code)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["dry"])
        self.assertTrue(out["report"]["headers"])

    def test_a_skipped_filter_view_is_noted_not_hidden(self):
        fake_client = mock.Mock()
        fake_client.setup.return_value = {"headers": True, "validation": True,
                                          "protection": True, "filter_view": False,
                                          "filter_view_skipped_reason": "Advanced Sheets service not enabled"}
        with mock.patch.object(setup, "load_env", return_value={"BUS_URL": "u", "BUS_SECRET": "s"}), \
             mock.patch.object(setup, "StatusClient", return_value=fake_client), \
             mock.patch.object(sys, "argv", ["status_sheet_setup.py", "--live"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                setup.main()
        self.assertIn("filter view skipped", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

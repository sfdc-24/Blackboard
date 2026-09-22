#!/usr/bin/env python3
"""Regression coverage for the scheduled Waker's WhatsApp receipt path."""

import sys
import contextlib
import io
import json
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import board_waker as bw  # noqa: E402


class WhatsAppAcknowledgement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.message = [{"id": "WRK-ack-test", "ts": "2026-09-21T00:00:00Z", "text": "hello"}]
        self.real_repo, self.real_scripts, self.real_state, self.real_home = (
            bw.REPO, bw.SCRIPTS, bw.STATE, bw.HOME_ENV)
        bw.REPO = Path(self.tmp.name)
        bw.SCRIPTS = REPO / "scripts"
        bw.STATE = self.state
        bw.HOME_ENV = Path(r"C:\canonical\Blackboard\.env")

    def tearDown(self):
        bw.REPO, bw.SCRIPTS, bw.STATE, bw.HOME_ENV = (
            self.real_repo, self.real_scripts, self.real_state, self.real_home)
        self.tmp.cleanup()

    def assert_whatsapp_row_is_retriable(self, state):
        rows = [["WRK-ack-test", "2026-09-21T00:00:00Z", "whatsapp", "ALL", "APPEND", "hello"]]
        def fake_get(_env, _params):
            return 200, __import__("json").dumps({"rows": rows, "total": 1})
        with mock.patch.object(bw, "load_env", return_value={}), \
             mock.patch.object(bw, "bus_get", side_effect=fake_get):
            status, _, retry = bw.check_whatsapp(state)
        self.assertEqual(status, "NEWS")
        self.assertEqual(retry[0]["id"], "WRK-ack-test")

    @mock.patch.object(bw, "subprocess")
    def test_ack_passes_canonical_environment_path(self, subprocess_mock):
        subprocess_mock.run.return_value = type("Result", (), {"returncode": 0, "stdout": "sent\n", "stderr": ""})()
        ok, detail = bw.ack_whatsapp(self.message)
        args = subprocess_mock.run.call_args.args[0]
        self.assertTrue(ok)
        self.assertEqual(detail, "sent")
        self.assertEqual(args[args.index("-EnvFile") + 1], r"C:\canonical\Blackboard\.env")

    def test_successful_ack_records_id_and_advances_wa_boundary(self):
        with mock.patch.object(bw, "check_board", return_value=("QUIET", "none", [])), \
             mock.patch.object(bw, "check_whatsapp", return_value=("NEWS", "one", self.message)), \
             mock.patch.object(bw, "ack_whatsapp", return_value=(True, "sent")), \
             mock.patch.object(bw, "check_main_green", return_value=("OK", "green")), \
             mock.patch.object(bw, "check_live_site", return_value=("OK", "clean")), \
             mock.patch.object(bw, "check_assistant", return_value=("OK", "healthy")), \
             mock.patch.object(bw, "write_digest", return_value=Path(self.tmp.name) / "digest"), \
             mock.patch.object(bw, "post_alarm", return_value=""), \
             mock.patch.object(bw, "now_iso", return_value="2026-09-21T00:02:00Z"):
            with mock.patch.object(sys, "argv", ["board_waker.py"]):
                self.assertEqual(bw.main(), 0)
        state = bw.load_state()
        self.assertIn("WRK-ack-test", state["wa_acked"])
        self.assertEqual(state["wa_watermark"], "2026-09-21T00:02:00Z")

    def test_failed_ack_is_not_recorded_as_acknowledged(self):
        with mock.patch.object(bw, "check_board", return_value=("QUIET", "none", [])), \
             mock.patch.object(bw, "check_whatsapp", return_value=("NEWS", "one", self.message)), \
             mock.patch.object(bw, "ack_whatsapp", return_value=(False, "env file not found")), \
             mock.patch.object(bw, "check_main_green", return_value=("OK", "green")), \
             mock.patch.object(bw, "check_live_site", return_value=("OK", "clean")), \
             mock.patch.object(bw, "check_assistant", return_value=("OK", "healthy")), \
             mock.patch.object(bw, "write_digest", return_value=Path(self.tmp.name) / "digest"), \
             mock.patch.object(bw, "post_alarm", return_value=""):
            with mock.patch.object(sys, "argv", ["board_waker.py"]):
                self.assertEqual(bw.main(), 0)
        state = bw.load_state()
        self.assertNotIn("WRK-ack-test", state.get("wa_acked", []))
        self.assertEqual(state.get("wa_watermark"), "")

    def test_failed_ack_keeps_an_independent_retry_boundary_after_advance(self):
        """A later general advance must not hide a failed WhatsApp receipt."""
        bw.save_state({"watermark": "2026-09-20T23:00:00Z"})
        with mock.patch.object(bw, "check_board", return_value=("NEWS", "one", [{"ts": "2026-09-21T00:01:00Z", "payload": "to=claude-code-cli"}])), \
             mock.patch.object(bw, "check_whatsapp", return_value=("NEWS", "one", self.message)), \
             mock.patch.object(bw, "ack_whatsapp", return_value=(False, "sender failed")), \
             mock.patch.object(bw, "check_main_green", return_value=("OK", "green")), \
             mock.patch.object(bw, "check_live_site", return_value=("OK", "clean")), \
             mock.patch.object(bw, "check_assistant", return_value=("OK", "healthy")), \
             mock.patch.object(bw, "write_digest", return_value=Path(self.tmp.name) / "digest"), \
             mock.patch.object(bw, "post_alarm", return_value=""), \
             mock.patch.object(bw, "now_iso", return_value="2026-09-21T00:02:00Z"):
            with mock.patch.object(sys, "argv", ["board_waker.py"]):
                self.assertEqual(bw.main(), 0)
        state = bw.load_state()
        self.assertEqual(state["watermark"], "2026-09-20T23:00:00Z")
        with mock.patch.object(bw, "check_board", return_value=("NEWS", "one", [{"ts": "2026-09-21T00:01:00Z", "payload": "to=claude-code-cli"}])), \
             mock.patch.object(bw, "now_iso", return_value="2026-09-21T00:02:00Z"):
            with mock.patch.object(sys, "argv", ["board_waker.py", "--advance", "--advance-through", "2026-09-21T00:02:00Z"]):
                self.assertEqual(bw.main(), 0)
        state = bw.load_state()
        self.assertEqual(state["watermark"], "2026-09-21T00:02:00Z")
        self.assertEqual(state["wa_watermark"], "2026-09-20T23:00:00Z")
        self.assertNotIn("WRK-ack-test", state.get("wa_acked", []))
        self.assert_whatsapp_row_is_retriable(state)

    def test_failed_ack_without_prior_cursor_survives_advance(self):
        """An intentional empty WA boundary must not fall back after advance."""
        with mock.patch.object(bw, "check_board", return_value=("NEWS", "one", [{"ts": "2026-09-21T00:01:00Z", "payload": "to=claude-code-cli"}])), \
             mock.patch.object(bw, "check_whatsapp", return_value=("NEWS", "one", self.message)), \
             mock.patch.object(bw, "ack_whatsapp", return_value=(False, "sender failed")), \
             mock.patch.object(bw, "check_main_green", return_value=("OK", "green")), \
             mock.patch.object(bw, "check_live_site", return_value=("OK", "clean")), \
             mock.patch.object(bw, "check_assistant", return_value=("OK", "healthy")), \
             mock.patch.object(bw, "write_digest", return_value=Path(self.tmp.name) / "digest"), \
             mock.patch.object(bw, "post_alarm", return_value=""), \
             mock.patch.object(bw, "now_iso", return_value="2026-09-21T00:02:00Z"):
            with mock.patch.object(sys, "argv", ["board_waker.py"]):
                self.assertEqual(bw.main(), 0)
        state = bw.load_state()
        self.assertNotIn("watermark", state)
        with mock.patch.object(bw, "check_board", return_value=("NEWS", "one", [{"ts": "2026-09-21T00:01:00Z", "payload": "to=claude-code-cli"}])), \
             mock.patch.object(bw, "now_iso", return_value="2026-09-21T00:02:00Z"):
            with mock.patch.object(sys, "argv", ["board_waker.py", "--advance", "--advance-through", "2026-09-21T00:02:00Z"]):
                self.assertEqual(bw.main(), 0)
        state = bw.load_state()
        self.assertEqual(state["watermark"], "2026-09-21T00:02:00Z")
        self.assertIn("wa_watermark", state)
        self.assertEqual(state["wa_watermark"], "")
        self.assert_whatsapp_row_is_retriable(state)

    def test_explicit_empty_wa_cursor_does_not_fall_back_to_general_cursor(self):
        state = {"wa_watermark": "", "watermark": "2026-09-21T00:02:00Z"}
        self.assert_whatsapp_row_is_retriable(state)

    def test_advance_commits_board_cursor_without_running_health_or_ack(self):
        """A clean model exit cannot be held by unrelated health work."""
        fresh = [{"ts": "2026-09-21T00:01:00Z", "payload": "to=claude-code-cli"}]
        with mock.patch.object(bw, "check_board", return_value=("NEWS", "one", fresh)), \
             mock.patch.object(bw, "check_whatsapp") as whatsapp, \
             mock.patch.object(bw, "ack_whatsapp") as ack, \
             mock.patch.object(bw, "check_main_green") as ci, \
             mock.patch.object(bw, "check_live_site") as site, \
             mock.patch.object(bw, "check_assistant") as assistant, \
             mock.patch.object(bw, "post_alarm") as alarm, \
             mock.patch.object(bw, "write_digest") as digest, \
             mock.patch.object(bw, "now_iso", return_value="2026-09-21T00:02:00Z"):
            with mock.patch.object(sys, "argv", ["board_waker.py", "--advance", "--advance-through", "2026-09-21T00:02:00Z"]):
                self.assertEqual(bw.main(), 0)
        self.assertEqual(bw.load_state()["watermark"], "2026-09-21T00:02:00Z")
        for dependency in (whatsapp, ack, ci, site, assistant, alarm, digest):
            dependency.assert_not_called()

    def test_advance_unknown_board_leaves_cursor_unchanged_and_does_not_write(self):
        """An untrusted read must not turn a clean model exit into skipped work."""
        before = {"watermark": "2026-09-20T23:00:00Z", "last_status": {"ci": "OK"}}
        bw.save_state(before)
        with mock.patch.object(bw, "check_board", return_value=("UNKNOWN", "network", [])), \
             mock.patch.object(bw, "save_state") as save:
            with mock.patch.object(sys, "argv", ["board_waker.py", "--advance", "--advance-through", "2026-09-21T00:02:00Z"]):
                self.assertEqual(bw.main(), 2)
        save.assert_not_called()
        self.assertEqual(bw.load_state(), before)

    def test_advance_freezes_inherited_wa_boundary_for_a_message_arriving_during_model(self):
        """A new receipt must not inherit the board cursor written after it arrived."""
        bw.save_state({"watermark": "2026-09-21T00:00:00Z"})
        fresh = [{"ts": "2026-09-21T00:00:30Z", "payload": "to=claude-code-cli"}]
        with mock.patch.object(bw, "check_board", return_value=("NEWS", "one", fresh)), \
             mock.patch.object(bw, "now_iso", return_value="2026-09-21T00:02:00Z"):
            with mock.patch.object(sys, "argv", ["board_waker.py", "--advance", "--advance-through", "2026-09-21T00:02:00Z"]):
                self.assertEqual(bw.main(), 0)
        state = bw.load_state()
        self.assertEqual(state["watermark"], "2026-09-21T00:02:00Z")
        self.assertEqual(state["wa_watermark"], "2026-09-21T00:00:00Z")
        arrived = [["WA-arrived-during-model", "2026-09-21T00:01:00Z", "whatsapp", "ALL", "APPEND", "hello"]]
        with mock.patch.object(bw, "load_env", return_value={}), \
             mock.patch.object(bw, "bus_get", return_value=(200, __import__("json").dumps({"rows": arrived, "total": 1}))):
            status, _, pending = bw.check_whatsapp(state)
        self.assertEqual(status, "NEWS")
        self.assertEqual([row["id"] for row in pending], ["WA-arrived-during-model"])

    @mock.patch.object(bw, "subprocess")
    def test_ack_exception_is_not_recorded_as_acknowledged(self, subprocess_mock):
        subprocess_mock.run.side_effect = TimeoutError("sender timed out")
        self.assertEqual(bw.ack_whatsapp(self.message), (False, "ack failed: sender timed out"))
        with mock.patch.object(bw, "check_board", return_value=("QUIET", "none", [])), \
             mock.patch.object(bw, "check_whatsapp", return_value=("NEWS", "one", self.message)), \
             mock.patch.object(bw, "ack_whatsapp", return_value=(False, "ack failed: sender timed out")), \
             mock.patch.object(bw, "check_main_green", return_value=("OK", "green")), \
             mock.patch.object(bw, "check_live_site", return_value=("OK", "clean")), \
             mock.patch.object(bw, "check_assistant", return_value=("OK", "healthy")), \
             mock.patch.object(bw, "write_digest", return_value=Path(self.tmp.name) / "digest"), \
             mock.patch.object(bw, "post_alarm", return_value=""):
            with mock.patch.object(sys, "argv", ["board_waker.py"]):
                self.assertEqual(bw.main(), 0)
        self.assertNotIn("WRK-ack-test", bw.load_state().get("wa_acked", []))

    def test_peek_never_sends_or_mutates_state(self):
        before = {"watermark": "2026-09-20T23:00:00Z"}
        bw.save_state(before)
        with mock.patch.object(bw, "check_board", return_value=("QUIET", "none", [])), \
             mock.patch.object(bw, "check_whatsapp", return_value=("NEWS", "one", self.message)), \
             mock.patch.object(bw, "ack_whatsapp") as ack:
            with mock.patch.object(sys, "argv", ["board_waker.py", "--peek"]):
                self.assertEqual(bw.main(), 10)
        ack.assert_not_called()
        self.assertEqual(bw.load_state(), before)

    def test_consumed_peek_cutoff_keeps_row_arriving_during_model(self):
        before = {"watermark": "2026-09-21T00:00:00Z"}
        bw.save_state(before)
        rows = [["A", "2026-09-21T00:01:00Z", "codex", "claude-code-cli", "APPEND", "first"]]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 21, 0, 1, 30, tzinfo=timezone.utc)
        output = io.StringIO()
        with mock.patch.object(bw, "load_env", return_value={}), \
             mock.patch.object(bw, "bus_get", side_effect=lambda *_: (200, json.dumps({"rows": rows, "total": len(rows)}))), \
             mock.patch.object(bw, "check_whatsapp", return_value=("QUIET", "none", [])):
            with mock.patch.object(bw, "datetime", Clock), contextlib.redirect_stdout(output), \
                 mock.patch.object(sys, "argv", ["board_waker.py", "--peek"]):
                self.assertEqual(bw.main(), 10)
            cutoff = next(line.split()[1] for line in output.getvalue().splitlines() if line.startswith("ADVANCE_THROUGH "))
            rows.append(["B", "2026-09-21T00:02:00Z", "codex", "claude-code-cli", "APPEND", "second"])
            with mock.patch.object(sys, "argv", ["board_waker.py", "--advance", "--advance-through", cutoff]):
                self.assertEqual(bw.main(), 0)
            status, _, fresh = bw.check_board(bw.load_state())
        self.assertEqual(status, "NEWS")
        self.assertEqual([row["payload"] for row in fresh], ["second"])
        self.assertEqual(bw.load_state()["watermark"], cutoff)

    def test_advance_without_consumed_cutoff_fails_without_read_or_write(self):
        bw.save_state({"watermark": "2026-09-21T00:00:00Z"})
        before = self.state.read_bytes()
        for cutoff in (None, "bad", "2999-01-01T00:00:00Z", "2026-9-21T00:01:29Z", "2026-09-21T0:1:29Z"):
            args = ["board_waker.py", "--advance"]
            if cutoff is not None:
                args += ["--advance-through", cutoff]
            with mock.patch.object(sys, "argv", args), mock.patch.object(bw, "check_board") as read:
                self.assertEqual(bw.main(), 2)
                read.assert_not_called()
            self.assertEqual(self.state.read_bytes(), before)

    def test_advance_malformed_or_failed_board_read_never_saves(self):
        bw.save_state({"watermark": "2026-09-21T00:00:00Z"})
        before = self.state.read_bytes()
        responses = [(200, body) for body in ('{', '[]', 'null', '{}', '{"rows":null}', '{"rows":{}}', '{"rows":""}', '{"rows":[{}]}')]
        responses.append((503, '{"rows":[]}'))
        for response in responses + [TimeoutError("offline transport failure")]:
            with self.subTest(response=response), \
                 mock.patch.object(bw, "load_env", return_value={}), \
                 mock.patch.object(bw, "bus_get") as bus, \
                 mock.patch.object(bw, "save_state") as save, \
                 mock.patch.object(sys, "argv", ["board_waker.py", "--advance", "--advance-through", "2026-09-21T00:01:29Z"]):
                if isinstance(response, Exception):
                    bus.side_effect = response
                else:
                    bus.return_value = response
                self.assertEqual(bw.main(), 2)
                save.assert_not_called()
                self.assertEqual(self.state.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)

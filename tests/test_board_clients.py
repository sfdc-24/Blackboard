#!/usr/bin/env python3
"""Offline safety contract for the shared Python Blackboard clients."""

import contextlib
import io
import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import append as append_client  # noqa: E402
import bus  # noqa: E402
import env_check  # noqa: E402
import tail_full  # noqa: E402


HEADER = [
    "Row_ID",
    "Timestamp",
    "Source_Tag",
    "Target_Surface",
    "Action_Type",
    "Payload",
    "Category",
    "Project Tag",
    "Gist",
    "Sub-Gist",
]


def http_404():
    return urllib.error.HTTPError("https://example.invalid", 404, "not found", {}, None)


def row(row_id, payload="BCB|v=1|id=TEST|phase=RESULT"):
    return [row_id, "2026-09-08T00:00:00.000000Z", "tester", "ALL", "APPEND", payload, "DONE", "", "", ""]


class FetchSafetyTests(unittest.TestCase):
    def test_append_defaults_to_exactly_one_attempt(self):
        error = http_404()
        with mock.patch.object(bus, "_fetch_once", side_effect=error) as send, mock.patch.object(bus.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                bus.fetch("https://example.invalid", {"action": "append"})
        error.close()
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()

    def test_append_rejects_configured_replay_before_network(self):
        with mock.patch.object(bus, "_fetch_once") as send:
            with self.assertRaisesRegex(ValueError, "exactly one"):
                bus.fetch("https://example.invalid", {"action": "append"}, tries=2)
        send.assert_not_called()

    def test_post_without_action_uses_bus_append_default_and_cannot_replay(self):
        error = http_404()
        with mock.patch.object(bus, "_fetch_once", side_effect=error) as send, mock.patch.object(bus.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                bus.fetch("https://example.invalid", {"sheetRow": ["WRK-one"]})
        error.close()
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()

    def test_read_can_retry_after_transport_404(self):
        error = http_404()
        with mock.patch.object(bus, "_fetch_once", side_effect=[error, '{"ok":true}']) as send, mock.patch.object(bus.time, "sleep") as sleep:
            body = bus.fetch("https://example.invalid", {"action": "read"}, tries=2)
        error.close()
        self.assertEqual(body, '{"ok":true}')
        self.assertEqual(send.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_invalid_try_count_fails_before_network(self):
        for invalid in (0, -1, True, 1.5):
            with self.subTest(tries=invalid), mock.patch.object(bus, "_fetch_once") as send:
                with self.assertRaises(ValueError):
                    bus.fetch("https://example.invalid", {"action": "read"}, tries=invalid)
                send.assert_not_called()


class AppendClientTests(unittest.TestCase):
    def run_client(self, spec, reads, write_result='{"ok":true}'):
        output = io.StringIO()
        opened = mock.mock_open(read_data=json.dumps(spec))
        patches = (
            mock.patch.object(sys, "argv", ["append.py", "row.json"]),
            mock.patch("builtins.open", opened),
            mock.patch.object(append_client, "load_env", return_value={"BUS_URL": "https://example.invalid", "BUS_SECRET": "CANARY_SECRET"}),
            mock.patch.object(append_client, "fetch"),
            mock.patch.object(append_client, "read_board", side_effect=reads),
        )
        result = None
        raised = None
        with patches[0], patches[1], patches[2], patches[3] as send, patches[4] as read, contextlib.redirect_stdout(output):
            if isinstance(write_result, BaseException):
                send.side_effect = write_result
            else:
                send.return_value = write_result
            try:
                result = append_client.main()
            except SystemExit as exc:
                raised = exc
        return result, raised, output.getvalue(), send, read

    def test_ambiguous_write_is_not_replayed_and_resolves_by_full_count(self):
        spec = {"row_id": "WRK-one", "source_tag": "test-agent", "payload": "BCB|v=1|id=ONE|phase=RESULT"}
        reads = [
            {"rows": [HEADER, row("older")]},
            {"rows": [HEADER, row("WRK-one", spec["payload"])]},
        ]
        _, raised, output, send, read = self.run_client(spec, reads, write_result=OSError("response lost"))
        self.assertIsNone(raised)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["tries"], 1)
        self.assertEqual(read.call_count, 2)
        self.assertIn("READ-BACK OK", output)

    def test_nonadjacent_full_sheet_duplicates_exit_two_without_repost(self):
        spec = {"row_id": "WRK-dupe", "source_tag": "test-agent", "payload": "BCB|v=1|id=DUPE|phase=RESULT"}
        rows = [HEADER, row("WRK-dupe"), row("other"), row("WRK-dupe")]
        _, raised, _, send, read = self.run_client(spec, [{"rows": rows}])
        self.assertEqual(raised.code, 2)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 1)

    def test_success_looking_response_still_uses_count_and_detects_duplicate(self):
        spec = {"row_id": "WRK-dupe", "source_tag": "test-agent", "payload": "BCB|v=1|id=DUPE|phase=RESULT"}
        rows = [HEADER, row("WRK-dupe"), row("WRK-dupe")]
        _, raised, _, send, read = self.run_client(spec, [{"rows": rows}], write_result='{"ok":true}')
        self.assertEqual(raised.code, 2)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 1)

    def test_three_zero_hit_reads_exit_unresolved_without_repost(self):
        spec = {"row_id": "WRK-missing", "source_tag": "test-agent", "payload": "BCB|v=1|id=MISSING|phase=RESULT"}
        empty = {"rows": [HEADER, row("other")]}
        _, raised, output, send, read = self.run_client(spec, [empty, empty, empty])
        self.assertEqual(raised.code, 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 3)
        self.assertIn("before re-running", output)

    def test_source_tag_is_required_before_write(self):
        spec = {"row_id": "WRK-wrong-owner", "payload": "BCB|v=1|id=OWNER|phase=RESULT"}
        opened = mock.mock_open(read_data=json.dumps(spec))
        with mock.patch.object(sys, "argv", ["append.py", "row.json"]), mock.patch("builtins.open", opened), mock.patch.object(
            append_client, "load_env", return_value={"BUS_URL": "https://example.invalid", "BUS_SECRET": "CANARY_SECRET"}
        ), mock.patch.object(append_client, "fetch") as send:
            with self.assertRaisesRegex(SystemExit, "source_tag"):
                append_client.main()
        send.assert_not_called()


class ReaderAndDiagnosticTests(unittest.TestCase):
    def test_full_tail_does_not_hide_or_truncate_vm_cli_rows(self):
        payload = "BCB|v=1|id=VISIBLE|phase=RESULT|text=" + ("x" * 3000)
        data = {"rows": [HEADER, row("WRK-vmcli-visible", payload)]}
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["tail_full.py", "1"]), mock.patch.object(
            tail_full, "load_env", return_value={}
        ), mock.patch.object(tail_full, "read_board", return_value=data), contextlib.redirect_stdout(output):
            tail_full.main()
        text = output.getvalue()
        self.assertIn("WRK-vmcli-visible", text)
        self.assertIn(payload, text)

    def test_env_secret_description_never_echoes_value(self):
        canary = "DO_NOT_ECHO_THIS_SECRET"
        description = env_check.describe("BUS_SECRET", canary)
        self.assertNotIn(canary, description)
        self.assertIn("PRESENT", description)


if __name__ == "__main__":
    unittest.main()

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
            mock.patch.object(append_client, "read_rows", side_effect=reads),
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
            {"rows": [row("older")], "total": 9, "filtered": 1},
            {"rows": [row("WRK-one", spec["payload"])], "total": 9, "filtered": 1},
        ]
        _, raised, output, send, read = self.run_client(spec, reads, write_result=OSError("response lost"))
        self.assertIsNone(raised)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["tries"], 1)
        self.assertEqual(read.call_count, 2)
        self.assertIn("READ-BACK OK", output)

    def test_nonadjacent_full_sheet_duplicates_exit_two_without_repost(self):
        spec = {"row_id": "WRK-dupe", "source_tag": "test-agent", "payload": "BCB|v=1|id=DUPE|phase=RESULT"}
        rows = [row("WRK-dupe"), row("other"), row("WRK-dupe")]
        _, raised, _, send, read = self.run_client(spec, [{"rows": rows}])
        self.assertEqual(raised.code, 2)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 1)

    def test_success_looking_response_still_uses_count_and_detects_duplicate(self):
        spec = {"row_id": "WRK-dupe", "source_tag": "test-agent", "payload": "BCB|v=1|id=DUPE|phase=RESULT"}
        rows = [row("WRK-dupe"), row("WRK-dupe")]
        _, raised, _, send, read = self.run_client(spec, [{"rows": rows}], write_result='{"ok":true}')
        self.assertEqual(raised.code, 2)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 1)

    def test_three_zero_hit_reads_exit_unresolved_without_repost(self):
        spec = {"row_id": "WRK-missing", "source_tag": "test-agent", "payload": "BCB|v=1|id=MISSING|phase=RESULT"}
        empty = {"rows": [row("other")], "total": 9, "filtered": 1}
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


class FilteredReadTests(unittest.TestCase):
    """read_rows() — the filtered read that keeps the read-back from timing out.

    The live behaviour these pin down was MEASURED against the bus on
    2026-09-19, not assumed from the endpoint's documentation.
    """

    ENV = {"BUS_URL": "https://example.invalid", "BUS_SECRET": "CANARY_SECRET"}

    def reply(self, rows, **extra):
        body = {"ok": True, "rows": rows, "total": 2953}
        body.setdefault("filtered", len(rows))
        body.update(extra)
        return json.dumps(body)

    def test_filters_reach_the_wire_and_unasked_keys_stay_absent(self):
        with mock.patch.object(bus, "fetch", return_value=self.reply([row("a")])) as send:
            bus.read_rows(self.ENV, since="2026-09-19T03:00:00Z", limit=5, match="WRK-x")
        sent = send.call_args.args[1]
        self.assertEqual(sent["since"], "2026-09-19T03:00:00Z")
        self.assertEqual(sent["limit"], 5)
        self.assertEqual(sent["match"], "WRK-x")
        self.assertEqual(sent["action"], "read")
        with mock.patch.object(bus, "fetch", return_value=self.reply([row("a")])) as send:
            bus.read_rows(self.ENV, match="WRK-x")
        sent = send.call_args.args[1]
        self.assertNotIn("since", sent)
        self.assertNotIn("limit", sent)

    def test_counts_pass_through_so_a_caller_can_say_what_it_skipped(self):
        with mock.patch.object(bus, "fetch", return_value=self.reply([row("a"), row("b")])):
            out = bus.read_rows(self.ENV, since="2026-09-19T00:00:00Z")
        self.assertEqual(out["total"], 2953)
        self.assertEqual(out["filtered"], 2)
        self.assertEqual(len(out["rows"]), 2)

    def test_a_bus_that_ignored_the_filter_is_refused_not_relabelled(self):
        # An older deployment answers an unknown key by returning the whole
        # board. Silently handing that back as "recent rows" is the worst
        # outcome available, so the client refuses it.
        whole_board = json.dumps({"ok": True, "rows": [row("a"), row("b")], "total": 2953})
        with mock.patch.object(bus, "fetch", return_value=whole_board):
            with self.assertRaisesRegex(SystemExit, "ignored since"):
                bus.read_rows(self.ENV, since="2026-09-19T00:00:00Z")

    def test_an_unfiltered_call_does_not_demand_a_filtered_count(self):
        no_count = json.dumps({"ok": True, "rows": [row("a")], "total": 2953})
        with mock.patch.object(bus, "fetch", return_value=no_count):
            out = bus.read_rows(self.ENV)
        self.assertEqual(out["filtered"], 1)

    def test_limit_must_be_a_positive_int_and_bool_is_not_one(self):
        for bad in (0, -1, True, 1.5, "5"):
            with self.subTest(limit=bad), mock.patch.object(bus, "fetch") as send:
                with self.assertRaises(ValueError):
                    bus.read_rows(self.ENV, limit=bad)
                send.assert_not_called()

    def test_a_header_in_a_filtered_reply_is_dropped_not_served_as_a_row(self):
        with mock.patch.object(bus, "fetch", return_value=self.reply([HEADER, row("a")])):
            out = bus.read_rows(self.ENV, match="a")
        self.assertEqual([r[0] for r in out["rows"]], ["a"])


class MatchIsNotIdentityTests(unittest.TestCase):
    """The trap that would turn a healthy append into a false DUPLICATE.

    match= is a case-insensitive substring over the WHOLE row, so rows that
    merely QUOTE a row_id in their payload come back too. MEASURED on
    2026-09-19: match on one row id returned 3 rows, one real and two quoting
    it. If the read-back counted match hits instead of comparing Row_ID it
    would exit 2 and tell the operator to post a correction for a duplicate
    that does not exist.
    """

    def test_quoting_rows_do_not_read_as_duplicates(self):
        spec = {"row_id": "WRK-cited", "source_tag": "test-agent", "payload": "BCB|v=1|id=CITED|phase=RESULT"}
        quoting = row("WRK-later", "BCB|v=1|id=LATER|phase=RESULT|note=supersedes WRK-cited")
        also = row("WRK-later2", "BCB|v=1|id=LATER2|phase=RESULT|note=see WRK-cited above")
        served = {"rows": [row("WRK-cited", spec["payload"]), quoting, also], "total": 2953, "filtered": 3}
        _, raised, output, send, read = AppendClientTests().run_client(spec, [served])
        self.assertIsNone(raised, "three match hits for one real row must not exit non-zero")
        self.assertIn("present exactly once", output)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 1)

    def test_a_real_duplicate_is_still_caught_through_the_same_filter(self):
        spec = {"row_id": "WRK-real-dupe", "source_tag": "test-agent", "payload": "BCB|v=1|id=RD|phase=RESULT"}
        served = {"rows": [row("WRK-real-dupe"), row("WRK-real-dupe")], "total": 2953, "filtered": 2}
        _, raised, output, _, _ = AppendClientTests().run_client(spec, [served])
        self.assertEqual(raised.code, 2)
        self.assertIn("DUPLICATE", output)

    def test_the_read_back_narrows_by_this_row_id(self):
        spec = {"row_id": "WRK-narrow", "source_tag": "test-agent", "payload": "BCB|v=1|id=N|phase=RESULT"}
        served = {"rows": [row("WRK-narrow", spec["payload"])], "total": 2953, "filtered": 1}
        _, _, _, _, read = AppendClientTests().run_client(spec, [served])
        self.assertEqual(read.call_args.kwargs.get("match"), "WRK-narrow")


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

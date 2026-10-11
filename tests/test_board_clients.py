#!/usr/bin/env python3
"""Offline safety contract for the shared Python Blackboard clients."""

import contextlib
import io
import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        spec = {"row_id": "WRK-one", "source_tag": "claude-code-cli", "payload": "BCB|v=1|id=ONE|from=claude-code-cli|phase=RESULT"}
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
        spec = {"row_id": "WRK-dupe", "source_tag": "claude-code-cli", "payload": "BCB|v=1|id=DUPE|from=claude-code-cli|phase=RESULT"}
        rows = [row("WRK-dupe"), row("other"), row("WRK-dupe")]
        _, raised, _, send, read = self.run_client(spec, [{"rows": rows}])
        self.assertEqual(raised.code, 2)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 1)

    def test_success_looking_response_still_uses_count_and_detects_duplicate(self):
        spec = {"row_id": "WRK-dupe", "source_tag": "claude-code-cli", "payload": "BCB|v=1|id=DUPE|from=claude-code-cli|phase=RESULT"}
        rows = [row("WRK-dupe"), row("WRK-dupe")]
        _, raised, _, send, read = self.run_client(spec, [{"rows": rows}], write_result='{"ok":true}')
        self.assertEqual(raised.code, 2)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 1)

    def test_three_zero_hit_reads_exit_unresolved_without_repost(self):
        spec = {"row_id": "WRK-missing", "source_tag": "claude-code-cli", "payload": "BCB|v=1|id=MISSING|from=claude-code-cli|phase=RESULT"}
        empty = {"rows": [row("other")], "total": 9, "filtered": 1}
        _, raised, output, send, read = self.run_client(spec, [empty, empty, empty])
        self.assertEqual(raised.code, 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 3)
        self.assertIn("before re-running", output)

    def test_source_tag_is_required_before_write(self):
        spec = {"row_id": "WRK-wrong-owner", "payload": "BCB|v=1|id=OWNER|from=claude-code-cli|phase=RESULT"}
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
        spec = {"row_id": "WRK-cited", "source_tag": "claude-code-cli", "payload": "BCB|v=1|id=CITED|from=claude-code-cli|phase=RESULT"}
        quoting = row("WRK-later", "BCB|v=1|id=LATER|phase=RESULT|note=supersedes WRK-cited")
        also = row("WRK-later2", "BCB|v=1|id=LATER2|phase=RESULT|note=see WRK-cited above")
        served = {"rows": [row("WRK-cited", spec["payload"]), quoting, also], "total": 2953, "filtered": 3}
        _, raised, output, send, read = AppendClientTests().run_client(spec, [served])
        self.assertIsNone(raised, "three match hits for one real row must not exit non-zero")
        self.assertIn("present exactly once", output)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(read.call_count, 1)

    def test_a_real_duplicate_is_still_caught_through_the_same_filter(self):
        spec = {"row_id": "WRK-real-dupe", "source_tag": "claude-code-cli", "payload": "BCB|v=1|id=RD|from=claude-code-cli|phase=RESULT"}
        served = {"rows": [row("WRK-real-dupe"), row("WRK-real-dupe")], "total": 2953, "filtered": 2}
        _, raised, output, _, _ = AppendClientTests().run_client(spec, [served])
        self.assertEqual(raised.code, 2)
        self.assertIn("DUPLICATE", output)

    def test_the_read_back_narrows_by_this_row_id(self):
        spec = {"row_id": "WRK-narrow", "source_tag": "claude-code-cli", "payload": "BCB|v=1|id=N|from=claude-code-cli|phase=RESULT"}
        served = {"rows": [row("WRK-narrow", spec["payload"])], "total": 2953, "filtered": 1}
        _, _, _, _, read = AppendClientTests().run_client(spec, [served])
        self.assertEqual(read.call_args.kwargs.get("match"), "WRK-narrow")


class ExecRedirectTests(unittest.TestCase):
    """The /exec 302 is followed as a bare GET. The automatic handler 404s."""

    def serve(self, location):
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                hits.append((self.command, self.path, self.headers.get("Content-Type")))
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()

            def do_GET(self):
                content_type = self.headers.get("Content-Type")
                hits.append((self.command, self.path, content_type))
                if content_type:
                    body = b"content-type-404"
                    code = 404
                elif self.path.startswith("/echo"):
                    body = b""
                    code = 302
                    self.send_response(code)
                    self.send_header("Location", "/final")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                elif self.path.startswith("/final"):
                    body = b'{"ok":true,"rows":[["R1"]]}'
                    code = 200
                else:
                    body = b"missing"
                    code = 404
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, hits

    def test_exec_redirect_is_a_bare_get_on_every_hop(self):
        server, hits = self.serve("/echo?k=1")
        url = "http://127.0.0.1:%d/exec" % server.server_address[1]
        try:
            with mock.patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("automatic redirect")
            ):
                body = bus._fetch_once(url, {"action": "read", "match": "R1"})
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual('{"ok":true,"rows":[["R1"]]}', body)
        self.assertEqual("POST", hits[0][0])
        self.assertEqual("/exec", hits[0][1])
        self.assertEqual("application/json", hits[0][2])
        self.assertEqual(("GET", "/echo?k=1", None), hits[1])
        self.assertEqual(("GET", "/final", None), hits[2])

    def test_a_redirect_off_the_script_host_is_not_followed(self):
        server, hits = self.serve("https://evil.example/steal")
        url = "http://127.0.0.1:%d/exec" % server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError):
                bus._fetch_once(url, {"action": "read"})
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([("POST", "/exec", "application/json")], hits)


# The three reviews bus-requests-6ct9q could not read. The row id is the UUID.
# The review id lives in the payload. Prefixes are what a caller already has.
REVIEWS = (
    ("53778e84", "53778e84-0000-4000-8000-000000000004", "CM-20261010-004"),
    ("bc9e1a16", "bc9e1a16-0000-4000-8000-000000000001", "GR-20261010-001"),
    ("aef36ae2", "aef36ae2-0000-4000-8000-000000000040", "GEMINI-20261010-0040"),
)


class ReviewSourceReadTests(unittest.TestCase):
    """bus-requests-6ct9q: two HTTP 404s on the board source read, then stop.

    POST /exec answers 302. A follower that keeps Content-Type on the echo GET
    gets 404. The source read follows that hop as a bare GET and returns the
    three reviews by stable row id.
    """

    def serve(self):
        hits = []
        rows = []
        for _prefix, full, review in REVIEWS:
            rows.append([full, "2026-10-10T00:00:00Z", "review", "all", "REVIEW", review,
                         "OPEN", "Blackboard", "", ""])
        body = json.dumps({
            "ok": True, "rows": rows, "total": 3, "filtered": 3,
        }).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                hits.append((self.command, self.path, self.headers.get("Content-Type"), raw))
                self.send_response(302)
                self.send_header("Location", "/echo")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                content_type = self.headers.get("Content-Type")
                hits.append((self.command, self.path, content_type, b""))
                if content_type or not self.path.startswith("/echo"):
                    code, payload = 404, b"content-type-404"
                else:
                    code, payload = 200, body
                self.send_response(code)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, hits

    def _content_type_follow(self, url):
        """The follower that 404s: the echo GET still carries Content-Type."""
        opener = urllib.request.build_opener(bus.NoRedirect)
        request = urllib.request.Request(
            url, data=b"{}", headers={"Content-Type": "application/json"})
        try:
            opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            if error.code != 302:
                raise
            loc = urllib.parse.urljoin(url, error.headers.get("Location"))
            error.close()
            echoed = urllib.request.Request(
                loc, method="GET", headers={"Content-Type": "application/json"})
            opener.open(echoed, timeout=5)
        raise AssertionError("the content-type hop should 404")

    def test_302_then_404_and_source_rows_fetches_the_three_reviews(self):
        import bus_reconcile
        server, hits = self.serve()
        url = "http://127.0.0.1:%d/exec" % server.server_address[1]
        try:
            codes = []
            for _ in range(2):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._content_type_follow(url)
                codes.append(caught.exception.code)
            self.assertEqual([404, 404], codes)
            env = {"BUS_URL": url, "BUS_SECRET": "test-secret"}
            with mock.patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("automatic redirect")
            ):
                got = bus_reconcile.source_rows(
                    env, row_ids=[item[0] for item in REVIEWS])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([item[2] for item in REVIEWS], [row[5] for row in got])
        self.assertEqual([item[1] for item in REVIEWS], [row[0] for row in got])
        posts = [hit for hit in hits if hit[0] == "POST"]
        gets = [hit for hit in hits if hit[0] == "GET"]
        # Two broken follows, then one bare GET per review.
        self.assertEqual(5, len(posts))
        self.assertEqual(5, len(gets))
        bare = [hit for hit in gets if hit[2] is None]
        typed = [hit for hit in gets if hit[2]]
        self.assertEqual(2, len(typed))
        self.assertEqual(3, len(bare))
        self.assertTrue(all(hit[1] == "/echo" for hit in bare))
        matches = []
        for hit in posts[-3:]:
            matches.append(json.loads(hit[3].decode("utf-8"))["match"])
        self.assertEqual([item[0] for item in REVIEWS], matches)
        entry = (ROOT / "cloud" / "bus-reconciler" / "serve_requests.py").read_text(encoding="utf-8")
        self.assertIn("bus_reconcile.source_rows", entry)
        self.assertLess(entry.index("source_rows"), entry.index("bus_request.handle("))

    def test_a_hex_prefix_is_unique_and_a_glued_id_is_not_that_row(self):
        self.assertTrue(bus.row_matches("53778e84", "53778e84-0000-4000-8000-000000000004"))
        self.assertFalse(bus.row_matches("53778e84", "53778e84999"))
        rows = {
            "rows": [
                ["53778e84-aaaa-4000-8000-000000000004", "", "", "", "", "CM-20261010-004"],
                ["53778e84-bbbb-4000-8000-000000000099", "", "", "", "", "other"],
            ],
            "filtered": 2,
        }
        with mock.patch.object(bus, "read_rows", return_value=rows):
            with self.assertRaises(LookupError):
                bus.read_row({"BUS_URL": "http://127.0.0.1/exec", "BUS_SECRET": "x"}, "53778e84")
        one = {"rows": [rows["rows"][0]], "filtered": 1}
        with mock.patch.object(bus, "read_rows", return_value=one) as read:
            found = bus.read_row({"BUS_URL": "u", "BUS_SECRET": "s"}, "53778e84")
        self.assertEqual("CM-20261010-004", found[5])
        self.assertEqual("53778e84", read.call_args.kwargs.get("match"))


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

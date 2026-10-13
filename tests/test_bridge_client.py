#!/usr/bin/env python3
"""The direct route's client: scripts/bridge_client.py and its seam in scripts/redis_inbox.py.

Offline by construction: gcloud is a fake callable, the HTTP opener is a fake object, the board
poster is patched, and the breaker's state lives in a temp directory. Nothing here mints a token
or opens a socket; CI proves that by running it in an empty network namespace.

Run: python tests/test_bridge_client.py   (or: python -m unittest tests.test_bridge_client)

WHAT IS BEING GUARDED (docs/REDIS-PRIMARY.md Phase 3b and section 8.8)
  - the switch is per agent and OFF by default, and off is exactly the old behaviour;
  - the token comes from impersonation for one audience, is never a key file, and is never shown;
  - every non-2xx is a failure said out loud, and a refusal by the access rules is not retried on
    the board route, which would refuse it again;
  - three failures in a row open the breaker for fifteen minutes and that is said in ONE row.
"""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bridge_client as bc  # noqa: E402
import redis_inbox as inbox  # noqa: E402

URL = "https://redis-tool-bridge-example-uc.a.run.app"
SA = "claude-code-cli@sfdc24.iam.gserviceaccount.com"
ON = {"REDIS_BRIDGE": "on", "REDIS_BRIDGE_URL": URL, "REDIS_BRIDGE_IMPERSONATE": SA}
TOKEN = "aaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc"
SOURCE = (ROOT / "scripts" / "bridge_client.py").read_text(encoding="utf-8")


class Answer:
    """What opener.open returns: a context manager with a status and a body."""

    def __init__(self, status, body):
        self.status = status
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=-1):
        return self._raw


class Opener:
    """A fake urllib opener. `reply` is an Answer, or an exception to raise."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def http_error(code, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(URL + "/op", code, "x", {}, io.BytesIO(raw))


class TheSwitchIsPerAgentAndOffByDefault(unittest.TestCase):
    def test_unset_is_off_and_silent(self):
        said = []
        self.assertIsNone(bc.config({}))
        self.assertIsNone(bc.attempt("xadd", "fleet:inbox:grok", value="x", environ={}, say=said.append,
                                     minter=lambda *a: self.fail("minted with the switch off")))
        self.assertEqual([], said, "an agent that never asked for the bridge is told nothing")

    def test_only_the_word_on_turns_it_on(self):
        for value in ("", "off", "0", "false", "true", "1", "yes", "enabled"):
            self.assertIsNone(bc.config(dict(ON, REDIS_BRIDGE=value)), value)
        self.assertEqual((URL, SA), bc.config(ON))
        self.assertEqual((URL, SA), bc.config(dict(ON, REDIS_BRIDGE=" ON ")))

    def test_on_but_malformed_is_said_and_falls_back(self):
        for bad in (dict(ON, REDIS_BRIDGE_URL="http://plain.example.com"),
                    dict(ON, REDIS_BRIDGE_URL=URL + "/op?token=x"),
                    dict(ON, REDIS_BRIDGE_URL="https://user:pw@host.example.com"),
                    dict(ON, REDIS_BRIDGE_URL=""),
                    dict(ON, REDIS_BRIDGE_IMPERSONATE="someone@gmail.com"),
                    dict(ON, REDIS_BRIDGE_IMPERSONATE="")):
            said = []
            out = bc.attempt("xrange", "fleet:inbox:x", environ=bad, say=said.append,
                             minter=lambda *a: self.fail("minted on a malformed configuration"))
            self.assertIsNone(out)
            self.assertEqual(1, len(said), bad)
            self.assertIn("BRIDGE NOT USED", said[0])


class TheTokenIsImpersonatedForOneAudienceAndNeverShown(unittest.TestCase):
    def run_gcloud(self, returncode=0, stdout=TOKEN, stderr=""):
        seen = []

        def run(argv, **kwargs):
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

        return run, seen

    def test_the_command_is_exactly_impersonation_for_this_audience(self):
        run, seen = self.run_gcloud()
        self.assertEqual(TOKEN, bc.mint(URL, SA, run=run, which=lambda name: "/bin/gcloud"))
        argv, kwargs = seen[0]
        self.assertEqual(["/bin/gcloud", "auth", "print-identity-token",
                          "--impersonate-service-account=" + SA, "--audiences=" + URL,
                          "--include-email"], argv)
        self.assertTrue(kwargs.get("capture_output"), "gcloud's output is the credential: captured")
        self.assertNotIn("shell", {k for k, v in kwargs.items() if v}, "never through a shell")

    def test_no_key_file_route_exists_in_the_source(self):
        for forbidden in ("--key-file", "GOOGLE_APPLICATION_CREDENTIALS", "activate-service-account",
                          "service_account.Credentials", ".json\", \"r"):
            self.assertNotIn(forbidden, SOURCE)

    def test_a_failed_mint_says_the_exit_code_and_nothing_gcloud_printed(self):
        run, _ = self.run_gcloud(returncode=1, stdout="", stderr="ERROR: owner@example.com lacks x")
        with self.assertRaises(bc.BridgeFailure) as raised:
            bc.mint(URL, SA, run=run, which=lambda name: "/bin/gcloud")
        self.assertIn("exit 1", str(raised.exception))
        self.assertNotIn("owner@example.com", str(raised.exception))

    def test_output_that_is_not_a_token_is_refused_and_not_echoed(self):
        run, _ = self.run_gcloud(stdout="Updates are available: hunter2")
        with self.assertRaises(bc.BridgeFailure) as raised:
            bc.mint(URL, SA, run=run, which=lambda name: "/bin/gcloud")
        self.assertNotIn("hunter2", str(raised.exception))

    def test_no_gcloud_and_a_timeout_are_failures_not_crashes(self):
        with self.assertRaises(bc.BridgeFailure):
            bc.mint(URL, SA, which=lambda name: None)

        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        with self.assertRaises(bc.BridgeFailure):
            bc.mint(URL, SA, run=slow, which=lambda name: "/bin/gcloud")

    def test_the_token_is_never_printed_or_written(self):
        for forbidden in ("print(token", "say(token", "% token", "write(token", "+ token)"):
            self.assertNotIn(forbidden, SOURCE.replace('"Bearer " + token)', ""))


class EveryNon2xxIsAFailureSaidOutLoud(unittest.TestCase):
    def test_a_200_with_ok_true_is_the_result(self):
        opener = Opener(Answer(200, {"ok": True, "principal": "claude-code-cli", "value": []}))
        out = bc.call(URL, TOKEN, {"op": "xrange", "key": "fleet:inbox:x"}, opener=opener)
        self.assertEqual("claude-code-cli", out["principal"])
        request, timeout = opener.requests[0]
        self.assertEqual(URL + "/op", request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual("Bearer " + TOKEN, request.get_header("Authorization"))
        self.assertNotIn(TOKEN, request.full_url, "a token in a URL ends up in a log")
        self.assertEqual({"op": "xrange", "key": "fleet:inbox:x"}, json.loads(request.data))
        self.assertEqual(bc.HTTP_TIMEOUT, timeout)

    def test_every_other_status_is_a_bridge_failure_that_names_it(self):
        for code, body, expect in ((401, {"error": "unauthenticated"}, "HTTP 401 unauthenticated"),
                                   (403, {"error": "not authorised"}, "HTTP 403 not authorised"),
                                   (400, {"error": "bad request"}, "HTTP 400 bad request"),
                                   (404, {"error": "no such endpoint"}, "HTTP 404 no such endpoint"),
                                   (413, {"error": "body too large"}, "HTTP 413"),
                                   (500, b"<html>oops</html>", "HTTP 500"),
                                   (502, {"error": "failed", "type": "RuntimeError"}, "HTTP 502 failed"),
                                   (503, {"error": "redis unreachable"}, "HTTP 503 redis unreachable")):
            with self.assertRaises(bc.BridgeFailure) as raised:
                bc.call(URL, TOKEN, {"op": "get", "key": "fleet:x"}, opener=Opener(http_error(code, body)))
            self.assertIn(expect, str(raised.exception))

    def test_a_refusal_by_the_access_rules_is_its_own_thing_with_the_reason(self):
        opener = Opener(http_error(403, {"error": "refused", "reason": "gov: is protected"}))
        with self.assertRaises(bc.Refused) as raised:
            bc.call(URL, TOKEN, {"op": "set", "key": "gov:audit"}, opener=opener)
        self.assertEqual("gov: is protected", str(raised.exception))

    def test_a_200_that_is_not_the_answer_is_a_failure(self):
        for body in (b"not json", [1, 2], {"ok": False}, {"ok": "true"}, {}):
            with self.assertRaises(bc.BridgeFailure):
                bc.call(URL, TOKEN, {"op": "get", "key": "fleet:x"}, opener=Opener(Answer(200, body)))
        with self.assertRaises(bc.BridgeFailure):
            bc.call(URL, TOKEN, {"op": "get", "key": "fleet:x"}, opener=Opener(Answer(204, {"ok": True})))

    def test_no_answer_reports_the_type_and_not_the_message(self):
        with self.assertRaises(bc.BridgeFailure) as raised:
            bc.call(URL, TOKEN, {"op": "get", "key": "fleet:x"},
                    opener=Opener(ConnectionResetError("peer said hunter2")))
        self.assertIn("ConnectionResetError", str(raised.exception))
        self.assertNotIn("hunter2", str(raised.exception))

    def test_a_redirect_is_refused_so_the_token_goes_nowhere_else(self):
        handler = bc._NoRedirect()
        request = mock.Mock(full_url=URL + "/op")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {},
                                     "https://elsewhere.example.com/op")
        self.assertEqual(302, raised.exception.code)
        self.assertIn("build_opener(_NoRedirect)", SOURCE, "the default opener must be the refusing one")


class TheBreaker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "breaker.json")
        self.said, self.announced, self.minted = [], [], 0

    def go(self, now, outcome, **kw):
        def minter(url, account):
            self.minted += 1
            return TOKEN

        def caller(url, token, request):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return bc.attempt("xadd", "fleet:inbox:grok", value="hello", environ=ON, now=now,
                          state_path=self.state, say=self.said.append,
                          announce=kw.get("announce", self.announced.append),
                          minter=minter, caller=caller)

    def test_a_success_returns_the_result_and_writes_no_state(self):
        out = self.go(1000.0, {"ok": True, "principal": "claude-code-cli"})
        self.assertEqual("claude-code-cli", out["principal"])
        self.assertEqual([], self.said)
        self.assertFalse(os.path.exists(self.state))

    def test_the_value_travels_base64_as_on_the_board_route(self):
        seen = {}

        def caller(url, token, request):
            seen.update(request)
            return {"ok": True}

        bc.attempt("xadd", "fleet:inbox:grok", value="a|b\nc", environ=ON, now=1.0,
                   state_path=self.state, minter=lambda u, a: TOKEN, caller=caller)
        import base64
        self.assertEqual("b64", seen["enc"])
        self.assertEqual("a|b\nc", base64.b64decode(seen["val"]).decode("utf-8"))

    def test_two_failures_fall_back_and_say_so_each_time_without_a_row(self):
        for n in (1, 2):
            self.assertIsNone(self.go(1000.0 + n, bc.BridgeFailure("HTTP 503 redis unreachable")))
            self.assertIn("BRIDGE FAILED (%d of 3)" % n, self.said[-1])
            self.assertIn("Using the board route for this message", self.said[-1])
        self.assertEqual([], self.announced)
        self.assertEqual(2, bc.load_state(self.state)["failures"])

    def test_the_third_failure_opens_it_for_fifteen_minutes_and_says_so_in_one_row(self):
        for n in range(3):
            self.go(1000.0 + n, bc.BridgeFailure("HTTP 404 no such endpoint"))
        self.assertEqual(1, len(self.announced))
        self.assertIn("failed 3 times in a row", self.announced[0])
        self.assertIn("HTTP 404", self.announced[0])
        self.assertEqual(1002.0 + bc.OPEN_SECONDS, bc.load_state(self.state)["open_until"])
        self.assertEqual(900, bc.OPEN_SECONDS)
        self.assertEqual(3, bc.TRIP_AFTER)

    def test_while_open_nothing_is_minted_or_called_and_no_second_row_is_posted(self):
        for n in range(3):
            self.go(1000.0 + n, bc.BridgeFailure("HTTP 503"))
        minted = self.minted
        for later in (1003.0, 1500.0, 1002.0 + bc.OPEN_SECONDS - 1):
            self.assertIsNone(self.go(later, {"ok": True}))
            self.assertIn("breaker is open until", self.said[-1])
        self.assertEqual(minted, self.minted, "an open breaker must not mint or call")
        self.assertEqual(1, len(self.announced), "one row per trip, not one per message")

    def test_after_the_window_it_tries_again_and_a_success_closes_it(self):
        for n in range(3):
            self.go(1000.0 + n, bc.BridgeFailure("HTTP 503"))
        out = self.go(1002.0 + bc.OPEN_SECONDS, {"ok": True})
        self.assertEqual({"ok": True}, out)
        self.assertEqual({"failures": 0, "open_until": 0.0}, bc.load_state(self.state))

    def test_a_success_between_failures_resets_the_count(self):
        self.go(1.0, bc.BridgeFailure("HTTP 503"))
        self.go(2.0, bc.BridgeFailure("HTTP 503"))
        self.go(3.0, {"ok": True})
        self.go(4.0, bc.BridgeFailure("HTTP 503"))
        self.assertEqual(1, bc.load_state(self.state)["failures"])
        self.assertEqual([], self.announced, "three failures, but not in a row")

    def test_a_governed_refusal_is_raised_and_is_not_a_bridge_failure(self):
        self.go(1.0, bc.BridgeFailure("HTTP 503"))
        self.go(2.0, bc.BridgeFailure("HTTP 503"))
        with self.assertRaises(bc.Refused):
            self.go(3.0, bc.Refused("gov: is protected"))
        self.assertEqual(0, bc.load_state(self.state)["failures"],
                         "a refusal proves the bridge is answering")
        self.assertEqual([], self.announced)

    def test_a_row_that_cannot_be_posted_does_not_lose_the_message(self):
        def broken(text):
            raise RuntimeError("the board is down")

        for n in range(3):
            self.assertIsNone(self.go(1000.0 + n, bc.BridgeFailure("HTTP 503"), announce=broken))
        self.assertIn("could not be posted", self.said[-1])
        self.assertGreater(bc.load_state(self.state)["open_until"], 0, "the breaker opened anyway")

    def test_a_state_file_that_cannot_be_written_is_said_not_fatal(self):
        self.state = os.path.join(self.tmp.name, "no-such-dir", "breaker.json")
        self.assertIsNone(self.go(1.0, bc.BridgeFailure("HTTP 503")))
        self.assertTrue(any("could not be saved" in line for line in self.said), self.said)

    def test_a_corrupt_state_file_reads_as_closed(self):
        pathlib.Path(self.state).write_text("not json", encoding="utf-8")
        self.assertEqual({"failures": 0, "open_until": 0.0}, bc.load_state(self.state))


class TheInboxUsesItAndFallsBackToTheBoard(unittest.TestCase):
    """scripts/redis_inbox.py: the seam. `post` and `answer` are the board route; if they are
    called, a board row was spent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.posted = []
        patches = [
            mock.patch.object(inbox, "BOOKMARK", os.path.join(self.tmp.name, "seen.json")),
            mock.patch.object(bc, "STATE", os.path.join(self.tmp.name, "breaker.json")),
            mock.patch.object(inbox, "post", side_effect=lambda row: self.posted.append(row) or row["row_id"]),
            mock.patch.object(inbox, "answer", return_value={
                "from": "bus-reconciler",
                "payload": 'text=OK xrange fleet:inbox:claude-code-cli: '
                           '[{"id": "5-0", "by": "grok", "at": "t", "text": "from the board"}]'}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_cmd(self, argv, environ, minter=None, caller=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=False), \
                mock.patch.object(bc, "mint", minter or (lambda u, a: TOKEN)), \
                mock.patch.object(bc, "call", caller or (lambda u, t, r: {"ok": True})), \
                contextlib.redirect_stdout(out):
            for name in ("REDIS_BRIDGE", "REDIS_BRIDGE_URL", "REDIS_BRIDGE_IMPERSONATE"):
                if name not in environ:
                    os.environ.pop(name, None)
            code = inbox.main(argv)
        return code, out.getvalue()

    def requests(self):
        return [row for row in self.posted if row["action_type"] == inbox.REQUEST_ACTION]

    def test_off_is_exactly_the_old_behaviour(self):
        def never(*args):
            self.fail("the bridge was touched with the switch off")

        code, said = self.run_cmd(["send", "--to", "grok", "--text", "hello", "--wait", "0"], {},
                                  minter=never, caller=never)
        self.assertEqual(0, code)
        self.assertEqual(1, len(self.requests()), "one governed request row, as before")
        self.assertNotIn("bridge", said.lower())

    def test_a_send_through_the_bridge_spends_no_board_row(self):
        calls = []

        def caller(url, token, request):
            calls.append(request)
            return {"ok": True, "principal": "claude-code-cli"}

        code, said = self.run_cmd(["send", "--to", "grok", "--text", "hello there"], ON, caller=caller)
        self.assertEqual(0, code)
        self.assertEqual([], self.posted, "the direct route posted a row")
        self.assertEqual(("xadd", "fleet:inbox:grok"), (calls[0]["op"], calls[0]["key"]))
        self.assertIn("no board row", said)

    def test_a_read_through_the_bridge_shows_entries_and_moves_the_bookmark(self):
        entries = [{"id": "7-0", "by": "aya", "at": "2026-10-13T00:00:00Z", "text": "direct hello"}]
        code, said = self.run_cmd(["read"], ON, caller=lambda u, t, r: {"ok": True, "value": entries})
        self.assertEqual(0, code)
        self.assertEqual([], self.posted)
        self.assertIn("direct hello", said)
        self.assertIn("1 new of 1", said)
        self.assertEqual("7-0", inbox.seen()["last_id"])
        code, said = self.run_cmd(["read"], ON, caller=lambda u, t, r: {"ok": True, "value": entries})
        self.assertIn("0 new of 1", said, "the bookmark is shared by both routes")

    def test_a_bridge_failure_is_said_and_the_message_still_goes_by_the_board(self):
        def down(url, token, request):
            raise bc.BridgeFailure("HTTP 404 no such endpoint")

        code, said = self.run_cmd(["send", "--to", "grok", "--text", "hello", "--wait", "0"], ON, caller=down)
        self.assertEqual(0, code)
        self.assertIn("BRIDGE FAILED (1 of 3): HTTP 404 no such endpoint", said)
        self.assertEqual(1, len(self.requests()), "the message was not lost")

    def test_a_refusal_is_said_and_is_not_retried_on_the_board(self):
        def refuse(url, token, request):
            raise bc.Refused("claude-code-cli may not write gov:")

        code, said = self.run_cmd(["send", "--to", "grok", "--text", "hello"], ON, caller=refuse)
        self.assertEqual(inbox.REFUSED_EXIT, code)
        self.assertIn("REFUSED by the access rules: claude-code-cli may not write gov:", said)
        self.assertEqual([], self.posted, "a refused operation was retried on the route that refuses it too")

    def test_ok_true_without_a_list_is_not_printed_as_an_empty_inbox(self):
        code, said = self.run_cmd(["read", "--wait", "0"], ON,
                                  caller=lambda u, t, r: {"ok": True, "value": "oops"})
        self.assertIn("BRIDGE ANSWER UNREADABLE", said)
        self.assertEqual(1, len(self.requests()), "it fell back to the board route")
        self.assertIn("from the board", said)

    def test_the_third_failure_posts_exactly_one_breaker_row_with_no_message_content(self):
        def down(url, token, request):
            raise bc.BridgeFailure("HTTP 503 redis unreachable")

        for _ in range(4):
            self.run_cmd(["send", "--to", "grok", "--text", "the secret plan", "--wait", "0"], ON, caller=down)
        notes = [row for row in self.posted if row["action_type"] == "NOTE"]
        self.assertEqual(1, len(notes), "one row per trip")
        payload = notes[0]["payload"]
        self.assertTrue(payload.startswith("BCB|v=1|id=CCC-BRIDGE-BREAKER-"))
        self.assertIn("failed 3 times in a row", payload)
        self.assertNotIn("the secret plan", payload, "the board is public")
        self.assertEqual(4, len(self.requests()), "all four messages still went by the board")

    def test_the_breaker_row_cannot_carry_a_pipe_into_the_payload(self):
        inbox.breaker_row("HTTP 500 a|b\nc")
        text = self.posted[-1]["payload"].split("|text=", 1)[1]
        self.assertNotIn("|", text)
        self.assertNotIn("\n", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

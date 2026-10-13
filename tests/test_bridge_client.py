#!/usr/bin/env python3
"""The direct route's client: scripts/bridge_client.py and its seam in scripts/redis_inbox.py.

Offline by construction: gcloud is a fake callable, the HTTP opener is a fake object, and the
breaker and journal live in a temp directory. The "bridge" in the integrated tests is
redis_gov.execute over the governed suite's FakeRedis - the function the real endpoint calls - and
the "board" is PR 397's own Worker, which runs bus_request.handle. So a page that comes by either
route is produced by the code that would produce it live. CI runs this in an empty network
namespace; nothing here mints a token or opens a socket.

Run: python tests/test_bridge_client.py   (or: python -m unittest tests.test_bridge_client)

WHAT IS BEING GUARDED
  Aya's independent review of a125337 (AYA-REDIS-DIRECT-INTEGRATION-FIXES-20261013), five blockers:
  1. a write whose answer was lost was sent again by the board although the first may have committed;
  2. a caller the bridge denied was moved to the board route, where identity is only a claim;
  3. the direct read was a second reader: no after=, ids compared as strings, previews bookmarked;
  4. the client could act under a principal that is not its own;
  5. words from a server's error body could reach the public board in the breaker's row.
  Each of her named regressions is a test below, under the class that carries its number.
"""
import base64
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
import urllib.parse
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud" / "redis-tool-bridge"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bridge_client as bc  # noqa: E402
import redis_gov  # noqa: E402
import redis_inbox as inbox  # noqa: E402
from test_redis_gov import ACL, FakeRedis, FakePipeline, run  # noqa: E402
from test_redis_inbox import KEY, Worker as BoardWorker, stream  # noqa: E402
import main as bridge_main  # noqa: E402   cloud/redis-tool-bridge: the service's real HTTP handler
import op as bridge_op  # noqa: E402       and its real /op
from test_redis_tool_bridge import Verifier  # noqa: E402   the stand-in for Google's token verifier

REAL_CALL = bc.call      # the harness puts a BridgeSim where bc.call is; this is the function it wraps


class Worker(BoardWorker):
    """PR 397's board, plus the one thing it never had to carry: a row that is not a request. The
    breaker's NOTE is kept in .notes and is NOT in .asked, so .asked is exactly the governed requests
    this agent sent by the board route - every one of them a mutation or a read that cost a row."""

    def __init__(self, conn, legacy=False):
        super().__init__(conn, legacy)
        self.notes = []

    def post(self, spec):
        if spec.get("action_type") != "AYA_REQ":
            self.notes.append(spec["payload"])
            return spec["row_id"]
        return super().post(spec)

URL = "https://redis-tool-bridge-example-uc.a.run.app"
SA = "claude-code-cli@sfdc24.iam.gserviceaccount.com"
ME = "claude-code-cli"
ON = {"REDIS_BRIDGE": "on", "REDIS_BRIDGE_URL": URL, "REDIS_BRIDGE_IMPERSONATE": SA}
MINE = {SA: ME}                                   # callers.json, as it must read for this agent
TOKEN = "aaaaaaaaaaaa.bbbbbbbbbbbb.cccccccccccc"
SOURCE = (ROOT / "scripts" / "bridge_client.py").read_text(encoding="utf-8")
# What a hostile or careless server might put in an error body. None of it may ever be repeated.
HOSTILE = ("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXNlY3JldA", "hunter2-the-message-text",
           "sk-live-0123456789abcdefghijklmnop", "the secret plan is at dawn")


class Answer:
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
        self.reply, self.requests = reply, []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def http_error(code, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(URL + "/op", code, "x", {}, io.BytesIO(raw))


class BridgeSim:
    """THE REAL BOUNDARY, in one process (Aya on 0baa197: the combined test mocked the endpoint).

    One call is bridge_client.call -> an HTTP request -> main.Handler.do_POST -> main.verify_caller ->
    op.handle -> redis_gov.execute over the shared FakeRedis -> the handler's own bytes -> and
    bridge_client.call's own reading of them. Three things are stood in, and only these: the socket (the
    handler is handed the request, and its reply is read off its wfile), Google's token verifier
    (test_redis_tool_bridge.Verifier), and Redis (test_redis_gov.FakeRedis).

    principal  who callers.json binds the account to ON THE SERVER; "file" leaves the real file in place.
    fault      fault(sim, request) replaces a call. It can still run the real thing: sim.execute(...).
    claims     what the verifier says the token carries, when it is not this account, verified.
    """

    def __init__(self, conn, principal=ME, fault=None, account=SA, claims=None, environ=None):
        self.conn, self.principal, self.fault, self.account = conn, principal, fault, account
        self.claims = {"email": account, "email_verified": True} if claims is None else claims
        self.environ = ({bridge_main.AUDIENCE_ENV: URL, bridge_main.CALLERS_ENV: account}
                        if environ is None else environ)
        self.requests, self.held, self.down, self.log = [], [], False, io.StringIO()

    def __call__(self, url, token, request):
        self.requests.append(dict(request))
        if self.fault is not None:
            return self.fault(self, request)
        return self.execute(request, url=url, token=token)

    def execute(self, request, lose=None, url=URL, token=TOKEN):
        """One request through the real client and the real service.
        lose="after"   the service runs it and its answer never arrives
        lose="before"  it never reaches the service
        lose="held"    it is still on its way when the client gives up; deliver() lets it arrive"""
        return REAL_CALL(url, token, request, opener=_Wire(self, lose))

    def serve(self, request):
        """(status, body bytes): the real handler's answer to one urllib request."""
        handler = object.__new__(bridge_main.Handler)
        handler.path = urllib.parse.urlsplit(request.full_url).path
        handler.command, handler.request_version = "POST", "HTTP/1.1"
        handler.requestline = "POST %s HTTP/1.1" % handler.path
        handler.client_address = ("127.0.0.1", 0)
        body = request.data or b""
        handler.headers = {"Content-Length": str(len(body)),
                           "Authorization": request.get_header("Authorization", "")}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()

        def connect():
            if self.down:
                raise ConnectionError("redis unreachable")
            return self.conn

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.environ))
            for name in (bridge_main.AUDIENCE_ENV, bridge_main.CALLERS_ENV):
                if name not in self.environ:
                    os.environ.pop(name, None)
            stack.enter_context(mock.patch.object(bridge_main, "connect", connect))
            if self.principal != "file":
                stack.enter_context(mock.patch.object(
                    bridge_op, "load_callers", lambda path=None: {self.account: self.principal}))
            stack.enter_context(Verifier(self.claims))
            stack.enter_context(contextlib.redirect_stderr(self.log))
            handler.do_POST()
        head, _, raw = handler.wfile.getvalue().partition(b"\r\n\r\n")
        return int(head.split()[1]), raw

    def deliver(self):
        """The requests that were still on their way now reach the service. [(status, body)]."""
        late, self.held = self.held, []
        return [(status, json.loads(raw.decode("utf-8"))) for status, raw in map(self.serve, late)]


class _Wire:
    """The opener bridge_client.call is handed. The request goes to the real handler and its bytes come
    back as urllib gives them: an answer on a 200, an HTTPError on anything else."""

    def __init__(self, sim, lose):
        self.sim, self.lose = sim, lose

    def open(self, request, timeout=None):
        if self.lose == "before":
            raise TimeoutError("timed out")
        if self.lose == "held":
            self.sim.held.append(request)
            raise TimeoutError("timed out")
        status, raw = self.sim.serve(request)
        if self.lose == "after":
            raise TimeoutError("timed out")
        if status == 200:
            return Answer(200, raw)
        raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(raw))


# =====================================================================================================
class TheSwitchIsPerAgentAndOffByDefault(unittest.TestCase):
    def test_unset_is_off_and_silent(self):
        said = []
        self.assertIsNone(bc.config({}))
        self.assertIsNone(bc.attempt("xadd", "fleet:inbox:grok", me=ME, value="x", environ={},
                                     say=said.append, callers=MINE,
                                     minter=lambda *a: self.fail("minted with the switch off")))
        self.assertEqual([], said, "an agent that never asked for the bridge is told nothing")

    def test_only_the_word_on_turns_it_on(self):
        for value in ("", "off", "0", "false", "true", "1", "yes", "enabled"):
            self.assertIsNone(bc.config(dict(ON, REDIS_BRIDGE=value)), value)
        self.assertEqual((URL, SA), bc.config(ON))
        self.assertEqual((URL, SA), bc.config(dict(ON, REDIS_BRIDGE=" ON ")))

    def test_on_but_malformed_is_said_and_nothing_is_sent(self):
        for bad in (dict(ON, REDIS_BRIDGE_URL="http://plain.example.com"),
                    dict(ON, REDIS_BRIDGE_URL=URL + "/op?token=x"),
                    dict(ON, REDIS_BRIDGE_URL="https://user:pw@host.example.com"),
                    dict(ON, REDIS_BRIDGE_URL=""),
                    dict(ON, REDIS_BRIDGE_IMPERSONATE="someone@gmail.com"),
                    dict(ON, REDIS_BRIDGE_IMPERSONATE="")):
            said = []
            out = bc.attempt("xrange", "fleet:inbox:x", me=ME, environ=bad, say=said.append, callers=MINE,
                             minter=lambda *a: self.fail("minted on a malformed configuration"))
            self.assertIsNone(out)
            self.assertEqual(1, len(said), bad)
            self.assertIn("BRIDGE NOT USED", said[0])


class TheTokenIsImpersonatedForOneAudienceAndNeverShown(unittest.TestCase):
    def run_gcloud(self, returncode=0, stdout=TOKEN, stderr=""):
        seen = []

        def fake(argv, **kwargs):
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

        return fake, seen

    def test_the_command_is_exactly_impersonation_for_this_audience(self):
        fake, seen = self.run_gcloud()
        self.assertEqual(TOKEN, bc.mint(URL, SA, run=fake, which=lambda name: "/bin/gcloud"))
        argv, kwargs = seen[0]
        self.assertEqual(["/bin/gcloud", "auth", "print-identity-token",
                          "--impersonate-service-account=" + SA, "--audiences=" + URL,
                          "--include-email"], argv)
        self.assertTrue(kwargs.get("capture_output"), "gcloud's output is the credential: captured")
        self.assertFalse(kwargs.get("shell"), "never through a shell")

    def test_no_key_file_route_exists_in_the_source(self):
        for forbidden in ("--key-file", "GOOGLE_APPLICATION_CREDENTIALS", "activate-service-account",
                          "service_account.Credentials"):
            self.assertNotIn(forbidden, SOURCE)

    def test_a_failed_mint_says_the_exit_code_and_nothing_gcloud_printed(self):
        fake, _ = self.run_gcloud(returncode=1, stdout="", stderr="ERROR: owner@example.com lacks x")
        with self.assertRaises(bc.NotSent) as raised:
            bc.mint(URL, SA, run=fake, which=lambda name: "/bin/gcloud")
        self.assertIn("exit 1", str(raised.exception))
        self.assertNotIn("owner@example.com", str(raised.exception))

    def test_output_that_is_not_a_token_is_refused_and_not_echoed(self):
        fake, _ = self.run_gcloud(stdout="Updates are available: hunter2")
        with self.assertRaises(bc.NotSent) as raised:
            bc.mint(URL, SA, run=fake, which=lambda name: "/bin/gcloud")
        self.assertNotIn("hunter2", str(raised.exception))

    def test_no_gcloud_and_a_timeout_are_not_sent_not_crashes(self):
        with self.assertRaises(bc.NotSent):
            bc.mint(URL, SA, which=lambda name: None)

        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        with self.assertRaises(bc.NotSent):
            bc.mint(URL, SA, run=slow, which=lambda name: "/bin/gcloud")

    def test_the_token_is_never_printed_or_written(self):
        code = SOURCE.replace('req.add_header("Authorization", "Bearer " + token)', "")
        for forbidden in ("print(token", "say(token", "% token", "write(token", "+ token", "token +"):
            self.assertNotIn(forbidden, code)

    def test_a_redirect_is_refused_so_the_token_goes_nowhere_else(self):
        handler = bc._NoRedirect()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.redirect_request(mock.Mock(full_url=URL + "/op"), io.BytesIO(b""), 302, "Found", {},
                                     "https://elsewhere.example.com/op")
        self.assertEqual(302, raised.exception.code)
        self.assertIn("build_opener(_NoRedirect)", SOURCE, "the default opener must be the refusing one")
        with self.assertRaises(bc.Unknown):          # and a refused redirect is not a success
            bc.call(URL, TOKEN, {"op": "get", "key": "fleet:x"}, opener=Opener(http_error(302, b"")))


# =====================================================================================================
class Blocker1_WhatEachAnswerPermits(unittest.TestCase):
    """call(): the status, and two closed members of the body, decide the outcome. Nothing else does."""

    def outcome(self, reply):
        try:
            return bc.call(URL, TOKEN, {"op": "xadd", "key": "fleet:inbox:grok"}, opener=Opener(reply))
        except (bc.BridgeProblem, bc.Refused) as error:
            return error

    def test_a_200_with_ok_true_is_the_result_and_the_request_is_one_post_to_op(self):
        opener = Opener(Answer(200, {"ok": True, "principal": ME, "value": []}))
        out = bc.call(URL, TOKEN, {"op": "xrange", "key": "fleet:inbox:x", "expect": ME}, opener=opener)
        self.assertEqual(ME, out["principal"])
        request, timeout = opener.requests[0]
        self.assertEqual((URL + "/op", "POST"), (request.full_url, request.get_method()))
        self.assertEqual("Bearer " + TOKEN, request.get_header("Authorization"))
        self.assertNotIn(TOKEN, request.full_url, "a token in a URL ends up in a log")
        self.assertEqual({"op": "xrange", "key": "fleet:inbox:x", "expect": ME}, json.loads(request.data))
        self.assertEqual(bc.HTTP_TIMEOUT, timeout)

    def test_only_these_answers_prove_nothing_ran(self):
        for code, body in ((400, {"error": "bad request", "executed": False}), (400, b"<html>"),
                           (404, {"error": "no such endpoint"}), (413, {"error": "body too large"}),
                           (503, {"error": "redis unreachable", "executed": False}),
                           (503, {"error": "service unavailable", "executed": False})):
            self.assertIsInstance(self.outcome(http_error(code, body)), bc.NotExecuted, (code, body))

    def test_a_503_that_does_not_say_executed_false_is_unknown(self):
        """A platform 503 can follow a request that already ran. Only the endpoint's own promise counts."""
        for body in ({"error": "redis unreachable"}, {"error": "x", "executed": "false"},
                     {"error": "x", "executed": 0}, {"error": "x", "executed": None}, b"<html>503</html>",
                     {"error": "x", "executed": True}):
            self.assertIsInstance(self.outcome(http_error(503, body)), bc.Unknown, body)

    def test_everything_else_once_sent_is_unknown(self):
        for reply in (http_error(502, {"error": "outcome unknown", "type": "ConnectionError"}),
                      http_error(502, {"error": "failed"}), http_error(500, b"oops"),
                      http_error(504, b""), http_error(429, b""), http_error(418, b""),
                      TimeoutError("read timed out"), ConnectionResetError("reset"),
                      urllib.error.URLError("no route"), Answer(200, b"not json"), Answer(200, [1]),
                      Answer(200, {"ok": False}), Answer(200, {"ok": "true"}), Answer(200, {}),
                      Answer(204, {"ok": True})):
            self.assertIsInstance(self.outcome(reply), bc.Unknown, repr(reply))

    def test_a_governed_refusal_carries_its_reason_and_whether_it_was_audited(self):
        out = self.outcome(http_error(403, {"error": "refused", "reason": "gov: is protected", "audit": "ok"}))
        self.assertIsInstance(out, bc.Refused)
        self.assertEqual(("gov: is protected", "ok"), (out.reason, out.audit))
        out = self.outcome(http_error(403, {"error": "refused", "reason": "gov: is protected",
                                            "audit": "FAILED:ConnectionError"}))
        self.assertEqual("not confirmed", out.audit, "a failed audit is never reported as ok")


class Blocker2_ADeniedCallerIsNeverDowngraded(unittest.TestCase):
    def test_401_and_every_other_403_are_denied(self):
        for code, body, label in ((401, {"error": "unauthenticated"}, "HTTP 401 unauthenticated"),
                                  (403, {"error": "not authorised"}, "HTTP 403 not authorised"),
                                  (403, {"error": "principal mismatch", "executed": False},
                                   "HTTP 403 principal mismatch"),
                                  (403, b"<html>forbidden</html>", "HTTP 403 not authorised"),
                                  (403, {"error": "something new"}, "HTTP 403 not authorised")):
            with self.assertRaises(bc.Denied) as raised:
                bc.call(URL, TOKEN, {"op": "get", "key": "fleet:x"}, opener=Opener(http_error(code, body)))
            self.assertEqual(label, str(raised.exception))

    def test_denied_is_terminal_for_a_read_and_for_a_write_and_is_not_a_bridge_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "breaker.json")
            for op in ("xrange", "xadd", "set"):
                for _ in range(4):
                    with self.assertRaises(bc.Denied):
                        bc.attempt(op, "fleet:inbox:grok", me=ME, value="v", environ=ON, callers=MINE,
                                   state_path=state, say=lambda line: None, minter=lambda u, a: TOKEN,
                                   caller=mock.Mock(side_effect=bc.Denied("HTTP 401 unauthenticated")))
            self.assertEqual({"failures": 0, "open_until": 0.0}, bc.load_state(state),
                             "a denial proves the bridge is answering")


# =====================================================================================================
class Outcomes(unittest.TestCase):
    """attempt(): what the caller is told it may do next."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "breaker.json")
        self.journal = os.path.join(self.tmp.name, "unknown.jsonl")
        self.said, self.announced, self.minted = [], [], 0

    def go(self, op, outcome, now=1000.0, value="hello", callers=MINE, me=ME, **kw):
        def minter(url, account):
            self.minted += 1
            if isinstance(kw.get("mint"), BaseException):
                raise kw["mint"]
            return TOKEN

        def caller(url, token, request):
            self.request = dict(request)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return bc.attempt(op, "fleet:inbox:grok", me=me, value=value if op not in bc.READS else None,
                          environ=ON, now=now, state_path=self.state, journal_path=self.journal,
                          callers=callers, say=self.said.append, announce=self.announced.append,
                          minter=minter, caller=caller)

    def journalled(self):
        try:
            with open(self.journal, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    # --- blocker 1
    def test_a_write_with_an_unknown_outcome_is_terminal_and_journalled_without_its_content(self):
        with self.assertRaises(bc.Unknown):
            self.go("xadd", bc.Unknown("no answer (TimeoutError)"), value="the secret plan")
        (entry,) = self.journalled()
        self.assertEqual(("xadd", "fleet:inbox:grok", "UNKNOWN", 15),
                         (entry["op"], entry["key"], entry["outcome"], entry["value_chars"]))
        self.assertEqual(64, len(entry["value_sha256"]))
        self.assertNotIn("the secret plan", json.dumps(entry), "the journal never holds the value")
        self.assertFalse(any("Using the board route" in line for line in self.said),
                         "an unknown write must never be offered the board route")

    def test_a_read_with_an_unknown_outcome_may_use_the_board(self):
        self.assertIsNone(self.go("xrange", bc.Unknown("no answer (TimeoutError)")))
        self.assertEqual([], self.journalled(), "a read changes nothing, so there is nothing to reconcile")
        self.assertIn("Using the board route for this read", self.said[-1])

    def test_not_sent_and_not_executed_may_use_the_board_even_for_a_write(self):
        self.assertIsNone(self.go("xadd", {"ok": True, "principal": ME}, mint=bc.NotSent("gcloud exit 1")))
        self.assertIsNone(self.go("xadd", bc.NotExecuted("HTTP 404 no such endpoint")))
        self.assertEqual([], self.journalled())

    def test_every_op_that_is_not_a_read_is_a_write(self):
        self.assertEqual(tuple(redis_gov.OPS_READ), bc.READS)
        for op in redis_gov.OPS_WRITE + ("haudit", "anything-new"):
            if os.path.exists(self.state):
                os.unlink(self.state)                 # each op meets a closed breaker
            with self.assertRaises(bc.Unknown, msg=op):
                self.go(op, bc.Unknown("HTTP 502 failed upstream"))

    # --- blocker 4
    def test_every_request_says_who_this_agent_expects_to_be(self):
        self.go("xadd", {"ok": True, "principal": ME})
        self.assertEqual(ME, self.request["expect"])

    def test_an_account_bound_to_another_principal_is_never_used(self):
        for callers in ({SA: "chatgpt-codex-desktop"}, {}, {"other@sfdc24.iam.gserviceaccount.com": ME}):
            self.minted = 0
            self.assertIsNone(self.go("xadd", {"ok": True, "principal": ME}, callers=callers))
            self.assertEqual(0, self.minted, "no token is minted for an identity that is not this agent's")
            self.assertIn("would not be acting as itself", self.said[-1])

    def test_no_principal_of_my_own_means_no_bridge(self):
        self.assertIsNone(self.go("xadd", {"ok": True, "principal": ""}, me="", callers={SA: ""}))
        self.assertEqual(0, self.minted)

    def test_an_endpoint_that_answers_as_someone_else_is_never_treated_as_success(self):
        with self.assertRaises(bc.Unknown):
            self.go("xadd", {"ok": True, "principal": "chatgpt-codex-desktop"})
        self.assertEqual(1, len(self.journalled()), "the write may have happened under another name")
        with self.assertRaises(bc.Denied):
            self.go("xrange", {"ok": True, "principal": "chatgpt-codex-desktop"})
        with self.assertRaises(bc.Unknown):
            self.go("xadd", {"ok": True})

    def test_the_missing_callers_file_maps_nobody(self):
        self.assertEqual({}, bc.load_callers(os.path.join(self.tmp.name, "absent.json")))
        bad = os.path.join(self.tmp.name, "bad.json")
        for text in ("not json", "[]", '{"callers": []}', "{}"):
            pathlib.Path(bad).write_text(text, encoding="utf-8")
            self.assertEqual({}, bc.load_callers(bad), text)

    # --- the breaker
    def test_a_success_returns_the_result_and_writes_no_state(self):
        self.assertEqual({"ok": True, "principal": ME}, self.go("xadd", {"ok": True, "principal": ME}))
        self.assertEqual([], self.said)
        self.assertFalse(os.path.exists(self.state))

    def test_the_value_travels_base64_as_on_the_board_route(self):
        self.go("xadd", {"ok": True, "principal": ME}, value="a|b\nc")
        self.assertEqual("b64", self.request["enc"])
        self.assertEqual("a|b\nc", base64.b64decode(self.request["val"]).decode("utf-8"))

    def test_three_failures_in_a_row_open_it_for_fifteen_minutes_and_say_so_in_one_row(self):
        for n in range(3):
            self.assertIsNone(self.go("xrange", bc.NotExecuted("HTTP 404 no such endpoint"), now=1000.0 + n))
        self.assertEqual(1, len(self.announced))
        self.assertIn("failed 3 times in a row (last: HTTP 404 no such endpoint)", self.announced[0])
        self.assertEqual(1002.0 + bc.OPEN_SECONDS, bc.load_state(self.state)["open_until"])
        self.assertEqual((900, 3), (bc.OPEN_SECONDS, bc.TRIP_AFTER))

    def test_while_open_nothing_is_minted_and_no_second_row_is_posted(self):
        for n in range(3):
            self.go("xrange", bc.NotExecuted("HTTP 503 unavailable"), now=1000.0 + n)
        minted = self.minted
        for later in (1003.0, 1500.0, 1002.0 + bc.OPEN_SECONDS - 1):
            self.assertIsNone(self.go("xadd", {"ok": True, "principal": ME}, now=later))
            self.assertIn("breaker is open until", self.said[-1])
        self.assertEqual(minted, self.minted, "an open breaker must not mint or call")
        self.assertEqual(1, len(self.announced), "one row per trip, not one per message")

    def test_after_the_window_a_success_closes_it(self):
        for n in range(3):
            self.go("xrange", bc.NotExecuted("HTTP 503 unavailable"), now=1000.0 + n)
        self.go("xrange", {"ok": True, "principal": ME}, now=1002.0 + bc.OPEN_SECONDS)
        self.assertEqual({"failures": 0, "open_until": 0.0}, bc.load_state(self.state))

    def test_a_success_or_a_refusal_between_failures_resets_the_count(self):
        self.go("xrange", bc.NotExecuted("HTTP 503 unavailable"), now=1.0)
        self.go("xrange", bc.NotExecuted("HTTP 503 unavailable"), now=2.0)
        with self.assertRaises(bc.Refused):
            self.go("xadd", bc.Refused("gov: is protected", "ok"), now=3.0)
        self.assertEqual(0, bc.load_state(self.state)["failures"])
        self.go("xrange", bc.NotExecuted("HTTP 503 unavailable"), now=4.0)
        self.assertEqual(1, bc.load_state(self.state)["failures"])
        self.assertEqual([], self.announced, "three failures, but not in a row")

    def test_an_unknown_write_counts_against_the_bridge_too(self):
        for n in range(3):
            with self.assertRaises(bc.Unknown):
                self.go("xadd", bc.Unknown("no answer (TimeoutError)"), now=1000.0 + n)
        self.assertEqual(1, len(self.announced))
        self.assertEqual(3, len(self.journalled()))

    def test_a_row_that_cannot_be_posted_and_a_state_file_that_cannot_be_saved_are_said_not_fatal(self):
        self.state = os.path.join(self.tmp.name, "no-such-dir", "breaker.json")
        self.assertIsNone(self.go("xrange", bc.NotExecuted("HTTP 503 unavailable")))
        self.assertTrue(any("could not be saved" in line for line in self.said), self.said)

        def broken(text):
            raise RuntimeError("the board is down")

        state = os.path.join(self.tmp.name, "b2.json")
        for n in range(3):
            self.assertIsNone(bc.attempt("xrange", "fleet:inbox:x", me=ME, environ=ON, now=1.0 + n,
                                         state_path=state, callers=MINE, say=self.said.append,
                                         announce=broken, minter=lambda u, a: TOKEN,
                                         caller=mock.Mock(side_effect=bc.NotExecuted("HTTP 503 unavailable"))))
        self.assertIn("could not be posted", " ".join(self.said))
        self.assertGreater(bc.load_state(state)["open_until"], 0, "the breaker opened anyway")

    def test_a_corrupt_state_file_reads_as_closed(self):
        pathlib.Path(self.state).write_text("not json", encoding="utf-8")
        self.assertEqual({"failures": 0, "open_until": 0.0}, bc.load_state(self.state))


# =====================================================================================================
class Harness:
    """redis_inbox over both routes at once: the bridge is redis_gov.execute, the board is PR 397's
    Worker, and they share one FakeRedis. A board row spent is an entry in worker.asked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bookmark = os.path.join(self.tmp.name, "seen.json")
        self.journal = os.path.join(self.tmp.name, "unknown.jsonl")

    def run_cmd(self, argv, worker, bridge=None, environ=ON, callers=MINE, opener=None, clock=None):
        printed = []
        patches = [
            mock.patch.object(inbox, "post", worker.post), mock.patch.object(inbox, "answer", worker.answer),
            mock.patch.object(inbox, "BOOKMARK", self.bookmark),
            mock.patch.object(bc, "STATE", os.path.join(self.tmp.name, "breaker.json")),
            mock.patch.object(bc, "JOURNAL", self.journal),
            mock.patch.object(bc, "load_callers", lambda path=None: callers),
            mock.patch.object(bc, "mint", lambda url, account: TOKEN),
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))),
        ]
        if clock is not None:
            patches.append(mock.patch("time.time", return_value=float(clock)))
        if opener is not None:
            real_call = bc.call
            patches.append(mock.patch.object(bc, "call", lambda u, t, r: real_call(u, t, r, opener=opener)))
        else:
            patches.append(mock.patch.object(bc, "call", bridge or (lambda u, t, r: self.fail("bridge called"))))
        with contextlib.ExitStack() as stack, mock.patch.dict(os.environ, environ, clear=False):
            for name in ("REDIS_BRIDGE", "REDIS_BRIDGE_URL", "REDIS_BRIDGE_IMPERSONATE"):
                if name not in environ:
                    os.environ.pop(name, None)
            for patch in patches:
                stack.enter_context(patch)
            code = inbox.main(argv)
        return code, "\n".join(printed)

    def read(self, worker, bridge=None, *argv, **kw):
        return self.run_cmd(["read", "--wait", "0"] + list(argv), worker, bridge, **kw)

    def send(self, worker, bridge=None, text="hello", **kw):
        return self.run_cmd(["send", "--to", "grok", "--text", text, "--wait", "0"], worker, bridge, **kw)

    @staticmethod
    def shown(out):
        return [line.split(" | ")[3] for line in out.splitlines() if line.count(" | ") >= 3]

    def cursor(self):
        with open(self.bookmark, encoding="utf-8") as fh:
            return json.load(fh)["last_id"]

    def inbox_of(self, conn, agent="grok"):
        return [fields.get("text") for _, fields in conn.data.get("fleet:inbox:%s" % agent, [])]


class Integrated(Harness, unittest.TestCase):
    """Aya's named regressions, each over both routes."""

    # ------------------------------------------------------------------------------- the flag is OFF
    def test_off_is_exactly_the_old_behaviour(self):
        conn = stream(["m1", "m2"])
        worker = Worker(conn)
        code, out = self.read(worker, environ={})
        self.assertEqual(0, code)
        self.assertEqual(["m1", "m2"], self.shown(out))
        self.assertNotIn("bridge", out.lower())
        code, out = self.send(worker, environ={})
        self.assertEqual(["hello"], self.inbox_of(conn))
        self.assertTrue(all("do=redis-op" in asked for asked in worker.asked), "the board route, as before")

    # ------------------------------------------------------------------------ blocker 1, integrated
    def test_a_committed_write_with_a_lost_response_is_never_written_again(self):
        """REGRESSION (Aya): committed write + lost response, with NO second board write."""
        conn = FakeRedis()
        worker = Worker(conn)

        code, out = self.send(worker, BridgeSim(conn, fault=lost_after_commit), text="only once")
        self.assertEqual(inbox.UNKNOWN_EXIT, code)
        self.assertEqual([], worker.asked, "a second mutation went out by the board route")
        self.assertEqual(["only once"], self.inbox_of(conn), "the message is in the inbox exactly once")
        self.assertIn("MAY HAVE COMMITTED", out)
        self.assertIn("NOT sent", out)
        with open(self.journal, encoding="utf-8") as fh:
            (entry,) = [json.loads(line) for line in fh]
        self.assertEqual("UNKNOWN", entry["outcome"])
        self.assertNotIn("only once", json.dumps(entry))

    def test_the_same_lost_response_over_real_http_shapes(self):
        for reply in (TimeoutError("timed out"), http_error(502, {"error": "outcome unknown"}),
                      http_error(503, b"upstream connect error"), Answer(200, b"<html>"),
                      http_error(500, b"")):
            conn = FakeRedis()
            worker = Worker(conn)
            breaker = os.path.join(self.tmp.name, "breaker.json")
            if os.path.exists(breaker):
                os.unlink(breaker)                    # every shape meets a closed breaker
            code, out = self.send(worker, opener=Opener(reply))
            self.assertEqual(inbox.UNKNOWN_EXIT, code, repr(reply))
            self.assertEqual([], worker.asked, repr(reply))
            self.assertEqual([], self.inbox_of(conn), repr(reply))

    def test_a_failed_audit_is_reported_and_nothing_is_written_by_the_board(self):
        """REGRESSION (Aya): failed audit, with NO second board write."""
        class AuditDown(FakeRedis):
            def xadd(self, key, fields, maxlen=None, approximate=None):
                if key == redis_gov.AUDIT_STREAM:
                    raise ConnectionError("audit stream unreachable")
                return super().xadd(key, fields, maxlen=maxlen, approximate=approximate)

        conn = AuditDown()
        worker = Worker(conn)
        # (a) a write: redis_gov commits the write and its audit entry together, so a failed audit
        # fails the transaction and the outcome is UNKNOWN, never a second attempt.
        code, out = self.send(worker, BridgeSim(conn))
        self.assertEqual(inbox.UNKNOWN_EXIT, code)
        self.assertEqual([], worker.asked)
        # (b) a refusal whose own audit entry failed: still a refusal, and the failed audit is said.
        def refuse(sim, request):                     # the same call, aimed at a key nobody may write
            return sim.execute(dict(request, key="gov:audit"))

        code, out = self.send(worker, BridgeSim(conn, fault=refuse))
        self.assertEqual(inbox.REFUSED_EXIT, code)
        self.assertIn("audit entry: not confirmed", out)
        self.assertEqual([], worker.asked)

    def test_a_not_executed_send_does_go_by_the_board_once(self):
        conn = FakeRedis()
        worker = Worker(conn)
        code, out = self.send(worker, opener=Opener(http_error(404, {"error": "no such endpoint"})))
        self.assertEqual(0, code)
        self.assertEqual(["hello"], self.inbox_of(conn), "nothing ran the first time, so once by the board")
        self.assertIn("BRIDGE FAILED (1 of 3): HTTP 404 no such endpoint", out)

    # ------------------------------------------------------------------------ blocker 2, integrated
    def test_a_401_or_403_is_terminal_for_send_and_for_read(self):
        """REGRESSION (Aya): terminal 401/403, no downgrade to the claimed-identity route."""
        for reply in (http_error(401, {"error": "unauthenticated"}), http_error(403, {"error": "not authorised"}),
                      http_error(403, {"error": "principal mismatch", "executed": False})):
            conn = stream(["m1"])
            worker = Worker(conn)
            code, out = self.send(worker, opener=Opener(reply))
            self.assertEqual(inbox.DENIED_EXIT, code)
            self.assertIn("DENIED by the bridge", out)
            code, out = self.read(worker, opener=Opener(reply))
            self.assertEqual(inbox.DENIED_EXIT, code)
            self.assertEqual([], self.shown(out), "a denied read showed messages it fetched another way")
            self.assertEqual([], worker.asked, "a denied caller was moved to the board route")
            self.assertEqual([], self.inbox_of(conn))

    def test_a_refusal_is_terminal_and_the_protected_keys_stay_protected(self):
        conn = FakeRedis()
        worker = Worker(conn)
        with self.assertRaises(bc.Refused) as raised:
            BridgeSim(conn)(URL, TOKEN, {"op": "set", "key": "gov:audit", "val": "eA==", "enc": "b64",
                                         "expect": ME})
        self.assertTrue(raised.exception.reason)
        self.assertNotIn("gov:audit", [k for k in conn.data if k != redis_gov.AUDIT_STREAM] + ["x"])

        def refuse(sim, request):
            return sim.execute(dict(request, key="gov:audit"))

        code, out = self.send(worker, BridgeSim(conn, fault=refuse))
        self.assertEqual(inbox.REFUSED_EXIT, code)
        self.assertEqual([], worker.asked, "a refused operation was retried on the route that refuses it too")

    # ------------------------------------------------------------------------ blocker 3, integrated
    def test_a_burst_past_ten_is_read_whole_oldest_first_with_numeric_ids(self):
        """REGRESSION (Aya): burst > N, and "9-0" against "10-0"."""
        conn = stream(["m%d" % k for k in range(1, 12)])      # ids 1-0 .. 11-0: "9-0" > "10-0" as strings
        worker = Worker(conn)
        bridge = BridgeSim(conn)
        code, out = self.read(worker, bridge)
        self.assertEqual(0, code, out)
        self.assertEqual(["m%d" % k for k in range(1, 12)], self.shown(out))
        self.assertEqual("11-0", self.cursor())
        self.assertEqual([], worker.asked, "the whole read, and the cursor's save, went by the bridge")
        self.assertTrue(all(r.get("after") for r in bridge.requests if r["op"] == "xrange"),
                        "every page was asked for after a cursor")
        self.assertEqual("11-0", conn.data["fleet:cursor:claude-code-cli"], "the cursor's copy in Redis")
        code, out = self.read(worker, bridge)
        self.assertEqual([], self.shown(out), "nothing is shown twice")

    def test_a_message_cut_at_the_preview_bound_stops_the_cursor_before_it(self):
        """REGRESSION (Aya): preview. The bridge quotes a value at 200 like the board does."""
        conn = stream(["short one", "L" * 450, "after the long one"])
        worker = Worker(conn)
        code, out = self.read(worker, BridgeSim(conn))
        self.assertEqual("1-0", self.cursor(), "the cursor moved past a message that arrived cut")
        self.assertIn("cut", out)
        self.assertNotIn("after the long one", self.shown(out))

    def test_a_malformed_page_from_the_bridge_moves_nothing(self):
        """REGRESSION (Aya): malformed metadata, and a field named id shown over the stream id."""
        good = {"ok": True, "op": "xrange", "key": KEY, "after": "0-0", "more": False, "last": "2-0",
                "principal": ME, "value": [{"id": "1-0", "text": "a"}, {"id": "2-0", "text": "b"}]}
        faults = {
            "no after: the endpoint ignored the cursor": {k: v for k, v in good.items() if k != "after"},
            "another cursor": dict(good, after="5-0"),
            "another key": dict(good, key="fleet:inbox:grok"),
            "last beyond the entries shown": dict(good, last="9-0"),
            "last is not a stream id": dict(good, last="banana"),
            "more is not a boolean": dict(good, more="false"),
            "ids out of numeric order": dict(good, value=[{"id": "10-0", "text": "a"}, {"id": "9-0", "text": "b"}],
                                             last="9-0"),
            "a field named id over the stream id": dict(good, value=[{"id": "hello", "text": "a"}], last="1-0"),
            "entries are not a list": dict(good, value="oops"),
            "not an xrange result": dict(good, op="get"),
        }
        for name, page in faults.items():
            if os.path.exists(self.bookmark):
                os.unlink(self.bookmark)
            conn = stream(["a", "b"])
            worker = Worker(conn)
            code, out = self.read(worker, lambda u, t, r, page=page: page if r["op"] == "xrange" else
                                  self.fail("the cursor was saved after an unread page"))
            self.assertEqual(3, code, name)
            self.assertIn("NOT READ, cursor stays at 0-0", out, name)
            self.assertFalse(os.path.exists(self.bookmark), name)
            self.assertEqual([], self.shown(out), name)

    def test_the_route_may_change_between_two_pages_and_nothing_is_lost_or_repeated(self):
        """REGRESSION (Aya): route change, one cursor across routes."""
        conn = stream(["m%d" % k for k in range(1, 12)])
        worker = Worker(conn)
        calls = {"n": 0}

        def first_page_then_down(sim, request):
            calls["n"] += 1
            sim.down = calls["n"] > 1                 # the service cannot reach Redis: its own 503
            return sim.execute(request)

        code, out = self.read(worker, BridgeSim(conn, fault=first_page_then_down))
        self.assertEqual(["m%d" % k for k in range(1, 12)], self.shown(out))
        self.assertEqual("11-0", self.cursor())
        self.assertTrue(worker.asked, "the later pages came by the board")
        self.assertIn("after=4-0", worker.asked[0], "the board was asked from where the bridge stopped")
        # and back again: the next read starts from the same cursor on the bridge
        assert run(conn, "grok", "xadd", KEY, val="m12")["ok"]
        code, out = self.read(worker, BridgeSim(conn))
        self.assertEqual(["m12"], self.shown(out))

    def test_an_unknown_cursor_save_holds_every_later_save_by_either_route(self):
        """Aya on 0baa197: a blind re-save after UNKNOWN is not proven monotonic. So there is none."""
        conn = stream(["m1"])
        worker = Worker(conn)
        code, out = self.read(worker, BridgeSim(conn, fault=save_lost("before")))
        self.assertEqual(4, code, out)
        self.assertEqual([], worker.asked, "the unknown save was repeated by the board route")
        self.assertEqual("1-0", self.cursor(), "the local cursor stands")
        self.assertIn("held until reconciled", out)
        assert run(conn, "grok", "xadd", KEY, val="m2")["ok"]
        bridge = BridgeSim(conn)
        code, out = self.read(worker, bridge)                       # the bridge is healthy again
        self.assertEqual(inbox.UNKNOWN_EXIT, code, out)
        self.assertEqual(["m2"], self.shown(out), "reading goes on; only the save waits")
        self.assertIn("in Redis: HELD", out)
        self.assertEqual(["xrange"], [r["op"] for r in bridge.requests], "a later save went out by the bridge")
        assert run(conn, "grok", "xadd", KEY, val="m3")["ok"]
        code, out = self.read(worker, environ={})                   # and with the bridge off
        self.assertEqual(["m3"], self.shown(out))
        self.assertIn("in Redis: HELD", out)
        self.assertFalse([a for a in worker.asked if "op=set" in a], "a later save went out by the board")
        self.assertNotIn("fleet:cursor:claude-code-cli", conn.data)
        code, out = self.read(worker, bridge)                       # nothing new: still owed, still held
        self.assertEqual(inbox.UNKNOWN_EXIT, code, out)
        self.assertEqual(["xrange", "xrange"], [r["op"] for r in bridge.requests])

    # ------------------------------------------------------------------------ blocker 4, integrated
    def test_a_client_given_another_agents_account_does_not_use_the_bridge(self):
        """REGRESSION (Aya): principal binding."""
        conn = FakeRedis()
        worker = Worker(conn)
        code, out = self.send(worker, callers={SA: "chatgpt-codex-desktop"})   # bridge=None: a call fails the test
        self.assertEqual(0, code)
        self.assertIn("would not be acting as itself", out)
        self.assertEqual(["hello"], self.inbox_of(conn))
        self.assertEqual(ME, [f["by"] for _, f in conn.data["fleet:inbox:grok"]][0],
                         "the message is by this agent, on the board route, not by the account's owner")

    def test_an_endpoint_bound_to_another_principal_refuses_before_writing(self):
        conn = FakeRedis()
        worker = Worker(conn)
        bridge = BridgeSim(conn, principal="chatgpt-codex-desktop")      # the server-side check of `expect`
        code, out = self.send(worker, bridge)
        self.assertEqual(inbox.DENIED_EXIT, code)
        self.assertEqual(ME, bridge.requests[0]["expect"])
        self.assertEqual([], self.inbox_of(conn), "nothing was written under another agent's name")
        self.assertEqual([], worker.asked)

    # ------------------------------------------------------------------------ blocker 5, integrated
    def test_nothing_from_an_error_body_reaches_the_breaker_row_or_the_screen(self):
        """REGRESSION (Aya): token-like and message-like error content is never repeated."""
        said, rows = [], []
        for status in (400, 404, 413, 500, 502, 503, 504):
            for field in ("error", "reason", "type", "detail"):
                for hostile in HOSTILE:
                    conn = FakeRedis()
                    worker = Worker(conn)
                    body = {field: hostile, "executed": False}
                    code, out = self.read(worker, opener=Opener(http_error(status, body)))
                    said.append(out)
                    rows += worker.notes
                    breaker = os.path.join(self.tmp.name, "breaker.json")
                    if worker.notes and os.path.exists(breaker):
                        os.unlink(breaker)            # tripped: close it so the next status is sent too
        self.assertTrue(rows, "the breaker tripped and posted its row during this test")
        everything = "\n".join(said + rows)
        for hostile in HOSTILE:
            self.assertNotIn(hostile, everything)
        for row in rows:
            text = row.split("|text=", 1)[1]
            self.assertRegex(text, r"^the Redis bridge failed 3 times in a row \(last: (HTTP \d{3} [a-z ]+|"
                                   r"no answer \([A-Za-z]+\))\); this agent uses the board route until "
                                   r"\d\d:\d\d:\d\dZ$")

    def test_a_refusals_reason_is_cleaned_and_printed_locally_only(self):
        conn = FakeRedis()
        worker = Worker(conn)
        reason = "may not write gov: " + HOSTILE[0] + " <script>|pipe\nnewline"
        code, out = self.send(worker, opener=Opener(http_error(403, {"error": "refused", "reason": reason})))
        self.assertEqual(inbox.REFUSED_EXIT, code)
        self.assertIn("may not write gov:", out)
        self.assertNotIn(HOSTILE[0], out)
        self.assertNotIn("|pipe", out)
        self.assertEqual([], worker.asked, "a refusal's reason is never put in a row")

    def test_every_status_label_is_closed_words(self):
        for status in list(bc.STATUS_LABELS) + [418, 599]:
            self.assertRegex(bc.status_label(status), r"^HTTP \d{3} [a-z ]+$")

    # ---------------------------------------------------------------------- the governor boundary
    def test_the_client_talks_to_one_endpoint_and_holds_no_redis_client(self):
        self.assertEqual(1, SOURCE.count('url + "/op"'))
        for forbidden in ("import redis", "redis.Redis", "redis_dual", "REDIS_AUTH", "/probe"):
            self.assertNotIn(forbidden, SOURCE)

    def test_one_judgement_for_both_routes(self):
        """The board page and the bridge page end in the same function."""
        source = (ROOT / "scripts" / "redis_inbox.py").read_text(encoding="utf-8")
        self.assertEqual(3, source.count("whole_page("), "defined once, called by page_of and page_of_result")


AYA_SA = "aya-runtime@sfdc24.iam.gserviceaccount.com"
AYA = "chatgpt-codex-desktop"


class TheRealBoundary(Harness, unittest.TestCase):
    """What only the real client, the real handler and the real governor TOGETHER can show (Aya on
    a743a94 and 0baa197: integrate PR 397's governor, and test the actual boundary)."""

    def attempt(self, sim, op="xadd", key="fleet:inbox:grok", me=ME, account=SA, callers=MINE, caller=None,
                **kw):
        said = []
        out = bc.attempt(op, key, me=me, environ=dict(ON, REDIS_BRIDGE_IMPERSONATE=account), callers=callers,
                         caller=caller or sim, minter=lambda url, sa: TOKEN, say=said.append,
                         state_path=os.path.join(self.tmp.name, "breaker.json"), journal_path=self.journal,
                         **kw)
        return out, said

    def test_the_committed_callers_file_means_the_same_to_both_sides(self):
        """callers.json is read by the client (may I use this account?) and by the service (who is this
        caller?). With the file as committed, Aya's runtime account writes as her principal, end to end."""
        conn = FakeRedis()
        sim = BridgeSim(conn, principal="file", account=AYA_SA)
        out, said = self.attempt(sim, me=AYA, account=AYA_SA, callers=None, value="from aya")
        self.assertEqual((True, AYA, AYA_SA), (out["ok"], out["principal"], out["caller"]), said)
        ((_, entry),) = conn.data["fleet:inbox:grok"]
        self.assertEqual(("from aya", AYA), (entry["text"], entry["by"]))
        self.assertEqual("bridge:" + AYA_SA, conn.audit()[-1]["via"], "the audit names the verified account")
        # and this agent's account is not in the committed file: the client does not even ask
        out, said = self.attempt(BridgeSim(conn, principal="file"), callers=None, value="x",
                                 caller=lambda *a: self.fail("called with an unbound account"))
        self.assertIsNone(out)
        self.assertIn("would not be acting as itself", said[0])

    def test_a_field_named_id_cannot_ride_over_the_stream_id_through_the_bridge(self):
        """Aya on a743a94: PR 400 alone carried a governor whose read let a field named id overwrite the
        stream id. Integrated, the bridge shows the stream id and the reader's cursor follows it."""
        conn = stream(["m1"])
        conn.xadd(KEY, {"id": "999-0", "text": "carries its own id", "by": "grok", "at": "2026-10-13T00:00:00Z"})
        worker = Worker(conn)
        bridge = BridgeSim(conn)
        code, out = self.read(worker, bridge)
        self.assertEqual(0, code, out)
        self.assertEqual(["m1", "carries its own id"], self.shown(out))
        self.assertEqual("2-0", self.cursor(), "the cursor took the field named id for the stream id")
        page = bridge.execute({"op": "xrange", "key": KEY, "after": "1-0", "count": 4, "expect": ME})
        self.assertEqual(("2-0", "999-0"), (page["value"][0]["id"], page["value"][0]["field:id"]))

    def test_the_service_and_the_client_show_neither_the_message_nor_the_token(self):
        conn = FakeRedis()
        worker = Worker(conn)
        bridge = BridgeSim(conn)
        code, out = self.send(worker, bridge, text="the secret plan")
        self.assertEqual(0, code, out)
        self.assertEqual(["the secret plan"], self.inbox_of(conn))
        log = bridge.log.getvalue()
        self.assertIn("op by %s as %s: xadd ok" % (SA, ME), log, "the service logs who and what op")
        for hidden in ("the secret plan", TOKEN, "fleet:inbox"):
            self.assertNotIn(hidden, log)
        for hidden in ("the secret plan", TOKEN):
            self.assertNotIn(hidden, out)

    def test_a_caller_the_service_does_not_verify_is_denied_and_never_moved_to_the_board(self):
        for name, sim in (
                ("the token's email is not verified",
                 lambda c: BridgeSim(c, claims={"email": SA, "email_verified": "true"})),
                ("a verified account that is not on the allowlist",
                 lambda c: BridgeSim(c, environ={bridge_main.AUDIENCE_ENV: URL, bridge_main.CALLERS_ENV: AYA_SA})),
                ("an account bound to another principal", lambda c: BridgeSim(c, principal=AYA))):
            conn = stream(["m1"])
            worker = Worker(conn)
            code, out = self.send(worker, sim(conn))
            self.assertEqual(inbox.DENIED_EXIT, code, name)
            code, out = self.read(worker, sim(conn))
            self.assertEqual(inbox.DENIED_EXIT, code, name)
            self.assertEqual(([], []), (self.shown(out), worker.asked), name)
            self.assertEqual([], self.inbox_of(conn), name)

    def test_what_the_service_proves_it_did_not_run_goes_by_the_board_exactly_once(self):
        """Its own "executed": false answers: no audience configured, Redis unreachable, a body over the
        bound. Each is the real handler's answer, and each lets the message go once by the board."""
        def misconfigured(conn):
            return BridgeSim(conn, environ={bridge_main.CALLERS_ENV: SA})

        def unreachable(conn):
            sim = BridgeSim(conn)
            sim.down = True
            return sim

        for name, make in (("no audience", misconfigured), ("redis unreachable", unreachable)):
            for leftover in ("breaker.json",):
                path = os.path.join(self.tmp.name, leftover)
                if os.path.exists(path):
                    os.unlink(path)
            conn = FakeRedis()
            worker = Worker(conn)
            code, out = self.send(worker, make(conn))
            self.assertEqual(0, code, (name, out))
            self.assertEqual(["hello"], self.inbox_of(conn), name)
            self.assertEqual(1, len(worker.asked), name)
        conn = FakeRedis()
        sim = BridgeSim(conn)
        out, said = self.attempt(sim, key="fleet:test:big", op="set", value="x" * 20000)
        self.assertIsNone(out, "a 413 is the service saying it read nothing")
        self.assertIn("HTTP 413", said[0])
        self.assertNotIn("fleet:test:big", conn.data)
        self.assertFalse(os.path.exists(self.journal), "nothing ran, so nothing is unknown")

    def test_a_refusal_carries_the_governors_own_reason_and_audit_through_both_layers(self):
        conn = FakeRedis()
        with self.assertRaises(bc.Refused) as raised:
            self.attempt(BridgeSim(conn), op="set", key="v1:bus:1", value="x")
        self.assertEqual("ok", raised.exception.audit)
        self.assertTrue(raised.exception.reason)
        self.assertEqual(("0", "bridge:" + SA), (conn.audit()[-1]["ok"], conn.audit()[-1]["via"]))
        self.assertNotIn("v1:bus:1", conn.data)


def lost_after_commit(sim, request):
    return sim.execute(request, lose="after")         # the service commits it; its answer never arrives


def lost_before_commit(sim, request):
    return sim.execute(request, lose="before")        # nothing landed; the client cannot know that


def save_lost(how):
    """A fault: everything answers, but the save of the read cursor is lost in the way named."""
    def fault(sim, request):
        return sim.execute(request, lose=how if request["op"] == "set" else None)
    return fault


class Reconciling(Harness, unittest.TestCase):
    """`redis_inbox.py reconcile`: an UNKNOWN write is settled by READING, never by sending again
    (Aya's Stage 1 exit check 5). The fake Redis numbers its stream ids 1-0, 2-0, ..., so the clock
    here is near zero: a write journalled at second 300 searches from id 0-0."""

    SENT_AT = 300

    def unknown_send(self, conn, worker, fault, text="only once"):
        code, out = self.send(worker, BridgeSim(conn, fault=fault), text=text, clock=self.SENT_AT)
        self.assertEqual(inbox.UNKNOWN_EXIT, code, out)
        return out

    def reconcile(self, worker, bridge=None, clock=None, **kw):
        return self.run_cmd(["reconcile", "--wait", "0"], worker, bridge,
                            clock=self.SENT_AT + 700 if clock is None else clock, **kw)

    def lines(self):
        with open(self.journal, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_a_write_that_did_commit_is_found_and_never_sent_again(self):
        conn = FakeRedis()
        worker = Worker(conn)
        out = self.unknown_send(conn, worker, lost_after_commit)
        self.assertIn("reconcile", out)
        code, out = self.reconcile(worker, BridgeSim(conn))
        self.assertEqual(0, code, out)
        self.assertIn("COMMITTED", out)
        self.assertIn("it is entry 1-0. Do NOT send it again.", out)
        self.assertEqual(["only once"], self.inbox_of(conn), "reconciling wrote something")
        self.assertEqual([], worker.asked, "the whole reconciliation went by the bridge, and wrote nothing")
        unknown, settled = self.lines()
        self.assertEqual((unknown["id"], "COMMITTED", "entry 1-0"),
                         (settled["resolves"], settled["outcome"], settled["detail"]))
        code, out = self.reconcile(worker, BridgeSim(conn))
        self.assertEqual((0, "nothing to reconcile: no write has an unknown outcome"), (code, out))

    def test_a_write_that_never_landed_is_absent_and_the_command_sends_nothing(self):
        conn = FakeRedis()
        worker = Worker(conn)
        self.unknown_send(conn, worker, lost_before_commit)
        code, out = self.reconcile(worker, BridgeSim(conn))
        self.assertEqual(0, code, out)
        self.assertIn("ABSENT", out)
        self.assertIn("this command sends nothing", out)
        self.assertEqual([], self.inbox_of(conn), "reconcile resent the message itself")
        self.assertEqual("ABSENT", self.lines()[-1]["outcome"])

    def test_absent_is_not_said_of_a_write_that_could_still_land(self):
        conn = FakeRedis()
        worker = Worker(conn)
        self.unknown_send(conn, worker, lost_before_commit)
        code, out = self.reconcile(worker, BridgeSim(conn), clock=self.SENT_AT + inbox.SETTLE_MIN_AGE - 1)
        self.assertEqual(inbox.UNSETTLED_EXIT, code)
        self.assertIn("could still land", out)
        self.assertEqual(1, len(self.lines()), "nothing was settled, so nothing was appended")
        code, out = self.reconcile(worker, BridgeSim(conn), clock=self.SENT_AT + inbox.SETTLE_MIN_AGE)
        self.assertEqual(0, code, out)

    def test_it_is_matched_as_a_read_shows_it_long_or_with_a_pipe(self):
        for text in ("L" * 450, "a | b and more", "plain"):
            if os.path.exists(self.journal):
                os.unlink(self.journal)
            conn = FakeRedis()
            worker = Worker(conn)
            self.unknown_send(conn, worker, lost_after_commit, text=text)
            code, out = self.reconcile(worker, BridgeSim(conn))
            self.assertEqual(0, code, (text[:20], out))
            self.assertIn("COMMITTED", out, text[:20])

    def test_shown_is_exactly_redis_govs_preview(self):
        self.assertEqual(redis_gov.PREVIEW_CHARS, bc.PREVIEW_CHARS)
        for value in ("", "plain", "a|b", "line\nbreak", "cr\rhere", "x" * 200, "x" * 201, "y" * 999, None, 7):
            self.assertEqual(redis_gov.preview(value), bc.shown(value), repr(value)[:30])

    def test_the_same_words_from_another_agent_are_not_my_write(self):
        conn = FakeRedis()
        worker = Worker(conn)
        assert run(conn, "grok", "xadd", "fleet:inbox:grok", val="only once")["ok"]    # by grok, not me
        self.unknown_send(conn, worker, lost_before_commit)
        code, out = self.reconcile(worker, BridgeSim(conn))
        self.assertIn("ABSENT", out, "another agent's identical text was taken for this agent's write")

    def test_an_entry_outside_the_window_is_not_my_write(self):
        conn = FakeRedis()
        worker = Worker(conn)
        self.unknown_send(conn, worker, lost_after_commit)        # committed as 1-0, journalled at 300
        with mock.patch.object(inbox, "SETTLE_BEFORE", -1), mock.patch.object(inbox, "SETTLE_AFTER", 2):
            code, out = self.reconcile(worker, BridgeSim(conn))   # the window is now ids 301000-0 .. 302000-0
        self.assertIn("ABSENT", out, "an entry before the window was matched")
        os.unlink(self.journal)
        conn = FakeRedis()
        worker = Worker(conn)
        self.unknown_send(conn, worker, lost_after_commit)        # 1-0 again
        with mock.patch.object(inbox, "SETTLE_AFTER", -300):      # the window now ENDS at id 0: 1-0 is after it
            code, out = self.reconcile(worker, BridgeSim(conn))
        self.assertIn("ABSENT", out, "an entry after the window was matched")

    def test_a_stream_that_cannot_be_read_leaves_it_unknown(self):
        conn = FakeRedis()
        worker = Worker(conn)
        self.unknown_send(conn, worker, lost_after_commit)
        worker.drop.add("xrange")                                  # the board never answers the read
        code, out = self.reconcile(worker, opener=Opener(http_error(404, {"error": "no such endpoint"})))
        self.assertEqual(inbox.UNSETTLED_EXIT, code)
        self.assertIn("could not be read", out)
        self.assertEqual(1, len(self.lines()))
        denied = Opener(http_error(403, {"error": "not authorised"}))
        code, out = self.reconcile(worker, opener=denied)
        self.assertEqual(inbox.UNSETTLED_EXIT, code, "a denied read settled something")

    def test_by_the_board_route_it_reads_and_only_reads(self):
        conn = FakeRedis()
        worker = Worker(conn)
        self.unknown_send(conn, worker, lost_after_commit)
        code, out = self.reconcile(worker, environ={})             # the bridge is off: the board carries it
        self.assertEqual(0, code, out)
        self.assertIn("COMMITTED", out)
        self.assertTrue(worker.asked)
        self.assertTrue(all("op=xrange" in asked for asked in worker.asked), worker.asked)
        self.assertEqual(["only once"], self.inbox_of(conn))

    def test_a_window_longer_than_the_page_cap_stays_unknown_and_says_how_to_go_on(self):
        conn = FakeRedis()
        for k in range(1, 40):
            assert run(conn, "grok", "xadd", "fleet:inbox:grok", val="noise %d" % k)["ok"]
        worker = Worker(conn)
        self.unknown_send(conn, worker, lost_after_commit)         # entry 40-0
        code, out = self.reconcile(worker, BridgeSim(conn))
        self.assertEqual(inbox.UNSETTLED_EXIT, code)
        self.assertIn("--pages", out)
        code, out = self.run_cmd(["reconcile", "--wait", "0", "--pages", "20"], worker, BridgeSim(conn),
                                 clock=self.SENT_AT + 700)
        self.assertEqual(0, code, out)
        self.assertIn("it is entry 40-0", out)

    CURSOR = "fleet:cursor:claude-code-cli"

    def test_a_cursor_save_that_did_commit_is_found_by_reading_the_key(self):
        conn = stream(["m1"])
        worker = Worker(conn)
        self.read(worker, BridgeSim(conn, fault=save_lost("after")), clock=self.SENT_AT)
        self.assertEqual("1-0", conn.data[self.CURSOR])
        bridge = BridgeSim(conn)
        code, out = self.reconcile(worker, bridge, clock=self.SENT_AT + 1)      # no need to wait for a yes
        self.assertEqual(0, code, out)
        self.assertIn("COMMITTED", out)
        self.assertEqual(["get"], [r["op"] for r in bridge.requests], "it reads the key and sends nothing")
        self.assertEqual("COMMITTED", self.lines()[-1]["outcome"])
        code, out = self.read(worker, bridge, clock=self.SENT_AT + 2)
        self.assertEqual(0, code, out)
        self.assertIn("in Redis: saved", out, "once settled, the save goes again")

    def test_THE_DELAYED_COMPLETION_an_old_save_that_arrives_after_a_newer_one_is_refused(self):
        """REGRESSION (Aya on 0baa197): "Delayed old SET may land after a newer confirmed checkpoint."
        Through the real handler and the real governor: it is held, settled, overtaken - and refused."""
        conn = stream(["m1"])
        worker = Worker(conn)
        slow = BridgeSim(conn, fault=save_lost("held"))
        code, out = self.read(worker, slow, clock=self.SENT_AT)     # the save of 1-0 is still on its way
        self.assertEqual(1, len(slow.held))
        self.assertNotIn(self.CURSOR, conn.data)
        assert run(conn, "grok", "xadd", KEY, val="m2")["ok"]
        bridge = BridgeSim(conn)
        code, out = self.read(worker, bridge, clock=self.SENT_AT + 5)
        self.assertIn("in Redis: HELD", out)                        # 1. no later checkpoint while unknown
        self.assertNotIn(self.CURSOR, conn.data)
        code, out = self.reconcile(worker, bridge, clock=self.SENT_AT + 5)
        self.assertEqual(inbox.UNSETTLED_EXIT, code)                # 2. nor is it called settled too early
        self.assertIn("could still land", out)
        code, out = self.reconcile(worker, bridge, clock=self.SENT_AT + inbox.SETTLE_MIN_AGE)
        self.assertEqual(0, code, out)
        self.assertIn("NOT HELD", out)
        self.assertEqual("NOT_HELD", self.lines()[-1]["outcome"])
        code, out = self.read(worker, bridge, clock=self.SENT_AT + inbox.SETTLE_MIN_AGE + 1)
        self.assertEqual(0, code, out)
        self.assertEqual("2-0", conn.data[self.CURSOR], "the newer checkpoint is confirmed")
        ((status, body),) = slow.deliver()                          # 3. NOW the old save arrives
        self.assertEqual(403, status)
        self.assertEqual(("refused", False), (body["error"], body["executed"]))
        self.assertIn("only moves forward", body["reason"])
        self.assertEqual("2-0", conn.data[self.CURSOR], "the delayed old save moved the cursor back")
        self.assertEqual(self.cursor(), conn.data[self.CURSOR], "the local cursor and the stored one agree")

    def test_an_old_save_that_arrives_DURING_the_hold_is_found_and_the_next_one_moves_forward(self):
        conn = stream(["m1"])
        worker = Worker(conn)
        slow = BridgeSim(conn, fault=save_lost("held"))
        self.read(worker, slow, clock=self.SENT_AT)
        assert run(conn, "grok", "xadd", KEY, val="m2")["ok"]
        bridge = BridgeSim(conn)
        self.read(worker, bridge, clock=self.SENT_AT + 5)           # held; the local cursor is 2-0
        ((status, body),) = slow.deliver()
        self.assertEqual((200, "1-0"), (status, conn.data[self.CURSOR]))
        code, out = self.reconcile(worker, bridge, clock=self.SENT_AT + 6)
        self.assertEqual("COMMITTED", self.lines()[-1]["outcome"], out)
        code, out = self.read(worker, bridge, clock=self.SENT_AT + 7)
        self.assertEqual("2-0", conn.data[self.CURSOR])

    def test_only_an_unknown_save_of_MY_cursor_holds_my_saves(self):
        bc.journal_unknown("xadd", "fleet:inbox:grok", "a message", "no answer (TimeoutError)", 300.0, self.journal)
        bc.journal_unknown("set", "fleet:cursor:grok", "9-0", "no answer (TimeoutError)", 300.0, self.journal)
        bc.journal_unknown("del", self.CURSOR, None, "no answer (TimeoutError)", 300.0, self.journal)
        conn = stream(["m1"])
        code, out = self.read(Worker(conn), BridgeSim(conn))
        self.assertEqual(0, code, out)
        self.assertEqual("1-0", conn.data[self.CURSOR], "an unrelated unknown write held this reader's save")

    def test_an_answer_that_is_not_a_get_of_the_cursor_key_settles_nothing(self):
        conn = stream(["m1"])
        worker = Worker(conn)
        self.read(worker, BridgeSim(conn, fault=save_lost("after")), clock=self.SENT_AT)
        good = {"ok": True, "op": "get", "key": self.CURSOR, "found": True, "value": "1-0", "principal": ME}
        for name, answer in (("another key", dict(good, key="fleet:cursor:grok")),
                             ("another op", dict(good, op="xrange")),
                             ("no value", {k: v for k, v in good.items() if k != "value"}),
                             ("a value that is not text", dict(good, value=["1-0"]))):
            code, out = self.reconcile(worker, lambda u, t, r, answer=answer: answer)
            self.assertEqual(inbox.UNSETTLED_EXIT, code, name)
            self.assertEqual(1, len(self.lines()), name)
        code, out = self.reconcile(worker, lambda u, t, r: good)
        self.assertEqual(0, code, out)

    def test_a_cursor_key_that_cannot_be_read_settles_nothing(self):
        conn = stream(["m1"])
        worker = Worker(conn)
        self.read(worker, BridgeSim(conn, fault=save_lost("after")), clock=self.SENT_AT)
        code, out = self.reconcile(worker, BridgeSim(conn, principal="chatgpt-codex-desktop"))   # denied
        self.assertEqual(inbox.UNSETTLED_EXIT, code, out)
        worker.drop.add("get")                                      # and by the board, no answer comes
        code, out = self.reconcile(worker, environ={})
        self.assertEqual(inbox.UNSETTLED_EXIT, code, out)
        self.assertIn("could not be read", out)
        self.assertEqual(1, len(self.lines()), "nothing was settled")

    def test_a_cursor_save_is_settled_by_the_board_route_too_and_only_by_reading(self):
        for how, word in (("after", "COMMITTED"), ("before", "NOT HELD")):
            if os.path.exists(self.journal):
                os.unlink(self.journal)
                os.unlink(self.bookmark)
            conn = stream(["m1"])
            worker = Worker(conn)
            self.read(worker, BridgeSim(conn, fault=save_lost(how)), clock=self.SENT_AT)
            def governed():                       # everything but gov: (the audit, the worker's own locks)
                return {k: v for k, v in conn.data.items() if not k.startswith("gov:")}

            before = governed()
            code, out = self.reconcile(worker, environ={})
            self.assertEqual(0, code, out)
            self.assertIn(word, out)
            self.assertEqual(["op=get"], [a.split("|")[7] for a in worker.asked], "one read, and no write")
            self.assertEqual(before, governed(), "reconciling changed something")

    def test_the_board_rows_value_is_read_whole_or_not_at_all(self):
        key = self.CURSOR
        row = "BCB|v=1|id=X|phase=AYA_RESULT|from=bus-reconciler|text=OK get %s: %s"
        self.assertEqual(("12-0", ""), inbox.value_of(row % (key, '"12-0"'), key))
        self.assertEqual(("", ""), inbox.value_of(row % (key, '""'), key), "an absent key shows as empty")
        for bad in (row % ("fleet:cursor:grok", '"12-0"'), row % (key, "12-0"), row % (key, '["12-0"]'),
                    row % (key, '"12-0"') + "|truncated=1", "BCB|v=1|id=X|no text here",
                    "BCB|v=1|id=X|truncated=1|text=OK get %s: %s" % (key, '"12-0"'),
                    "BCB|v=1|id=X|full=file:x|text=OK get %s: %s" % (key, '"12-0"'),
                    "BCB|v=1|text=REFUSED: no (audited: ok)", "BCB|v=1|text=OK xrange %s: []" % key):
            value, why = inbox.value_of(bad, key)
            self.assertIsNone(value, bad)
            self.assertTrue(why, bad)

    def test_any_other_op_stays_unknown_for_a_person(self):
        bc.journal_unknown("del", "fleet:test:x", None, "no answer (TimeoutError)", 300.0, self.journal)
        bc.journal_unknown("set", "fleet:test:not-the-cursor", "v", "no answer (TimeoutError)", 300.0,
                           self.journal)
        code, out = self.reconcile(Worker(FakeRedis()), BridgeSim(FakeRedis()))
        self.assertEqual(inbox.UNSETTLED_EXIT, code)
        self.assertEqual(2, out.count("settle it by hand"), "only an xadd and the read cursor's own SET are looked for")

    def test_one_alert_row_per_unknown_write_and_it_holds_no_message(self):
        conn = FakeRedis()
        worker = Worker(conn)
        self.unknown_send(conn, worker, lost_after_commit, text="the secret plan")
        alerts = [note for note in worker.notes if "BRIDGE-UNKNOWN" in note]
        self.assertEqual(1, len(alerts))
        text = alerts[0].split("|text=", 1)[1]
        self.assertEqual("UNKNOWN outcome for one xadd on fleet:inbox:grok through the Redis bridge; it was NOT "
                         "sent again; reconcile is pending on the sender's machine", text)
        self.assertNotIn("the secret plan", alerts[0])

    def test_an_alert_that_cannot_be_posted_does_not_turn_unknown_into_a_crash(self):
        conn = FakeRedis()
        worker = Worker(conn)
        real_post = worker.post

        def post(spec):
            if spec.get("action_type") == "NOTE":
                raise SystemExit("the request row did not read back")
            return real_post(spec)

        worker.post = post
        out = self.unknown_send(conn, worker, lost_after_commit)
        self.assertIn("alert row could not be posted", out)
        self.assertEqual(1, len(self.lines()), "the journal still has it")

    def test_the_journal_is_append_only_and_a_damaged_line_hides_nothing(self):
        first = bc.journal_unknown("xadd", "fleet:inbox:grok", "one", "x", 300.0, self.journal)
        with open(self.journal, "a", encoding="utf-8") as fh:
            fh.write("this line is not json\n")
        second = bc.journal_unknown("xadd", "fleet:inbox:grok", "two", "x", 301.0, self.journal)
        self.assertEqual([first["id"], second["id"]], [r["id"] for r in bc.pending(self.journal)])
        before = pathlib.Path(self.journal).read_text(encoding="utf-8")
        bc.settle(first, "COMMITTED", "entry 1-0", 900.0, self.journal)
        after = pathlib.Path(self.journal).read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before), "settling rewrote the journal instead of appending")
        self.assertEqual([second["id"]], [r["id"] for r in bc.pending(self.journal)])
        for record in bc.journal(self.journal):
            self.assertNotIn("one", [record.get("value"), record.get("text")])


if __name__ == "__main__":
    unittest.main(verbosity=2)

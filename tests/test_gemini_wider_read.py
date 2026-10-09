"""Gemini's wider read access (owner, 2026-10-08): a private repository behind an explicit switch,
and read-only Cloud Run state with values withheld and logs redacted.

The tests that matter are the ones about what can reach the board: Gemini's reply is posted there,
so an env VALUE, a private address or a token in a log line must never be in what it is given.
"""
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cloud_context as cc                                               # noqa: E402
import repo_context as rc                                                # noqa: E402

ON = {"GEMINI_GITHUB_TOKEN": "tok", "GEMINI_PRIVATE_REPOS": "conference"}


def pr_get(calls, private=True):
    def get(path, token, timeout=20):
        calls.append(path)
        if "/files?" in path:
            return [{"filename": "chair/x.py", "status": "modified", "additions": 1, "deletions": 0,
                     "patch": "@@ -1 +1 @@\n+secret_sauce"}]
        return {"title": "t", "state": "open", "merged_at": None, "head": {"sha": "a" * 40},
                "changed_files": 1, "base": {"sha": "b" * 40, "repo": {"private": private}}}
    return get


class APrivateRepositoryOnlyBehindTheSwitch(unittest.TestCase):
    def test_off_by_default_no_request_nothing_carried(self):
        calls = []
        self.assertEqual("", rc.context_for("review conference #168", env={"GEMINI_GITHUB_TOKEN": "tok"},
                                            get=pr_get(calls)))
        self.assertEqual([], calls)

    def test_on_the_named_private_pr_is_read_and_marked_private(self):
        calls = []
        out = rc.context_for("review conference #168", env=ON, get=pr_get(calls))
        self.assertIn("PRIVATE REPOSITORY", out)
        self.assertIn("+secret_sauce", out)
        self.assertEqual("/repos/sfdc-24/conference/pulls/168", calls[0])

    def test_the_switch_cannot_add_a_repository_the_code_does_not_list(self):
        env = {"GEMINI_GITHUB_TOKEN": "tok", "GEMINI_PRIVATE_REPOS": "conference,converspan,secrets"}
        self.assertEqual({"conference"}, rc.private_allowed(env))
        calls = []
        self.assertEqual("", rc.context_for("review converspan #3", env=env, get=pr_get(calls)))
        self.assertEqual([], calls)

    def test_a_public_repo_reading_private_is_still_refused(self):
        # The switch opens conference; it does not open a public name whose record says private.
        calls = []
        out = rc.context_for("review Blackboard #297", env=ON, get=pr_get(calls, private=True))
        self.assertIn("not attached (the repository is not public)", out)
        self.assertNotIn("+secret_sauce", out)


SERVICE = {"name": "projects/sfdc24/locations/us-central1/services/conference-gateway",
           "latestReadyRevision": "projects/x/revisions/conference-gateway-00019-k6c",
           "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED", "message": ""}],
           "template": {"serviceAccount": "gw@sfdc24.iam.gserviceaccount.com",
                        "vpcAccess": {"egress": "PRIVATE_RANGES_ONLY"},
                        "containers": [{"image": "us-docker.pkg.dev/x/gw:1",
                                        "env": [{"name": "REDIS_HOST", "value": "10.54.72.180"},
                                                {"name": "LIVEKIT_API_SECRET",
                                                 "valueSource": {"secretKeyRef": {
                                                     "secret": "projects/1/secrets/LIVEKIT_API_SECRET"}}}]}]}}
JOB = {"name": "projects/sfdc24/locations/us-central1/jobs/bus-requests",
       "latestCreatedExecution": {"name": "projects/x/executions/bus-requests-xktq4"},
       "template": {"template": {"containers": [{"image": "img:gov-4d13e97", "env": []}]}}}
LOG = {"timestamp": "2026-10-08T17:00:00.123Z", "severity": "ERROR",
       "textPayload": "connect to 10.54.72.180:6378 failed for abdus@sfdc24.com with Bearer abc.def "
                      "key AIzaSyA1234567890abcdefghijklmnopqrstu url https://x.test/cb?code=zzz"}


def fake_call(calls, fail=None):
    def call(url, token, body=None, timeout=15):
        calls.append((url, body))
        if fail:
            raise urllib.error.URLError(fail)
        if url.endswith("/services?pageSize=100"):
            return {"services": [SERVICE]}
        if url.endswith("/jobs?pageSize=100"):
            return {"jobs": [JOB]}
        if url.endswith("/workerPools?pageSize=100"):
            return {}
        if url == cc.LOGGING:
            return {"entries": [LOG]}
        raise AssertionError(url)
    return call


class CloudStateIsReadOnlyValuesWithheldLogsRedacted(unittest.TestCase):
    def test_a_row_naming_no_resource_gets_nothing(self):
        self.assertEqual("", cc.context_for("what is the biggest risk?", token="t", call=fake_call([])))

    def test_a_named_service_is_described_and_its_env_values_never_appear(self):
        out = cc.context_for("is conference-gateway healthy?", token="t", call=fake_call([]))
        self.assertIn("service conference-gateway", out)
        self.assertIn("conference-gateway-00019-k6c", out)
        self.assertIn("REDIS_HOST", out)
        self.assertIn("LIVEKIT_API_SECRET <- secret LIVEKIT_API_SECRET", out)
        self.assertNotIn("10.54.72.180", out, "an env value reached the context")

    def test_logs_only_when_asked_and_always_redacted(self):
        calls = []
        quiet = cc.context_for("describe bus-requests", token="t", call=fake_call(calls))
        self.assertNotIn("logs, WARNING", quiet)
        self.assertFalse(any(u == cc.LOGGING for u, _ in calls))
        out = cc.context_for("why did bus-requests fail? show the logs", token="t", call=fake_call([]))
        for leaked in ("10.54.72.180", "abdus@sfdc24.com", "abc.def", "AIzaSyA1234567890", "code=zzz"):
            self.assertNotIn(leaked, out, leaked)
        self.assertIn("[ip]", out)
        self.assertIn("[email]", out)

    def test_names_match_whole_words_only(self):
        self.assertEqual("", cc.context_for("bus-requests-old is unrelated", token="t", call=fake_call([])))
        self.assertEqual("", cc.context_for("xconference-gateway", token="t", call=fake_call([])))

    def test_no_token_off_cloud_run_means_no_request(self):
        calls = []
        self.assertEqual("", cc.metadata_token(env={}))
        self.assertEqual("", cc.context_for("conference-gateway", token="", call=fake_call(calls)))
        self.assertEqual([], calls)

    def test_an_api_failure_is_one_line_never_a_crash(self):
        # The row says "service": a list failure is attached only to a row about Cloud Run (#343).
        out = cc.context_for("conference-gateway service", token="t", call=fake_call([], fail="down"))
        self.assertIn("not attached", out)

    def test_it_never_writes(self):
        calls = []
        cc.context_for("conference-gateway bus-requests logs", token="t", call=fake_call(calls))
        posts = [u for u, body in calls if body is not None]
        self.assertEqual({cc.LOGGING}, set(posts), "the only POST is the logging READ endpoint")



# ------------------------------------------------------------------ Cursor's FAIL on #343, 4933cac
# Every value below is fake: AWS's documented example key pair, a made-up ya29. string, a JWT whose
# payload is {"sub":"fake"}, RFC 5737/3849 and private-range addresses.

FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJlLWZha2U"
SHAPES = {
    "redis password": ("redis://:p@ssw0rd@10.0.0.1:6378/0", ("ssw0rd", "10.0.0.1")),
    "scheme user:pass": ("postgres://admin:hunter2pw@db.internal.test/x", ("hunter2pw", "admin:")),
    "ya29 token": ("token ya29.a0AfB_byFAKEfakeFAKEfake0123456789 used", ("ya29.a0AfB",)),
    "AWS key id": ("key AKIAIOSFODNN7EXAMPLE rejected", ("AKIAIOSFODNN7EXAMPLE",)),
    "AWS secret": ("secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY end", ("wJalrXUtnFEMI", "bPxRfiCY")),
    "Basic": ("Authorization: Basic dXNlcjpwYXNzd29yZA== sent", ("dXNlcjpwYXNzd29yZA",)),
    "JWT": ("jwt " + FAKE_JWT + " expired", tuple(FAKE_JWT.split("."))),
    "IPv6 full": ("from 2001:0db8:0000:0000:0000:ff00:0042:8329 port", ("2001:0db8", "0042:8329")),
    "IPv6 compressed": ("from fd00:ec2::254 and 2001:db8::5 port", ("fd00:ec2", "2001:db8::5")),
}


class Finding1LogRedactionKeepsCredentialsOffTheBoard(unittest.TestCase):
    def test_every_shape_cursor_found_is_removed_from_a_log_line(self):
        for label, (line, leaks) in SHAPES.items():
            out = cc.redact(line)
            for leak in leaks:
                self.assertNotIn(leak, out, "%s survived redact(): %r" % (label, out))

    def test_the_log_lines_in_the_prompt_carry_none_of_them(self):
        payload = " | ".join(line for line, _ in SHAPES.values())
        entry = dict(LOG, textPayload=payload)

        def call(url, token, body=None, timeout=15):
            return {"entries": [entry]} if url == cc.LOGGING else fake_call([])(url, token, body, timeout)
        out = cc.context_for("bus-requests failed, show the logs", token="t", call=call)
        self.assertIn("logs, WARNING or worse", out)
        for label, (_, leaks) in SHAPES.items():
            for leak in leaks:
                self.assertNotIn(leak, out, label)

    def test_it_is_the_okf_scrubber_not_a_third_one(self):
        import okf_land
        line = "redis://:p@ssw0rd@10.0.0.1:6378/0"
        self.assertEqual(okf_land.scrub_secrets(line), cc.redact(line))
        # And the OKF file gets the same list.
        self.assertNotIn("ssw0rd", okf_land.scrub(line))

    def test_what_a_review_cites_survives(self):
        import okf_land
        text = ("head 4933caca3c67b3644817e52b3c444e7a2898b471 at 2026-10-08T22:04:01Z, v1.2.3, "
                "Bearer authentication, Basic credentials, x[::2], std::string")
        self.assertEqual(text, okf_land.scrub_secrets(text))


class Finding1TheReplyIsScrubbedBeforeTheBoard(unittest.TestCase):
    def run_waker(self, reply, environ, ask="what is the risk here?", landed=None):
        import io
        import os
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock
        import agent_waker as aw

        posted = []

        class Adapter(object):
            @staticmethod
            def ask(prompt, max_tokens=None):
                return (reply, "fake-route")

        rows = [["ROW-S", "2026-10-08T22:00:00Z", "grok", "gemini", "APPEND",
                 "BCB|v=1|id=SCRUB-1|phase=DISPATCH|to=gemini|" + ask]]
        tmp = tempfile.mkdtemp()
        saved_module = aw.AGENTS["gemini"]["module"]
        aw.sys.modules["fake_scrub_adapter"] = Adapter
        aw.AGENTS["gemini"]["module"] = "fake_scrub_adapter"
        try:
            with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, environ), \
                    mock.patch.object(aw, "read_since",
                                      lambda env, since, tries=3: {"rows": rows, "total": 1, "filtered": 1}), \
                    mock.patch.object(aw, "load_env", lambda: {}), \
                    mock.patch.object(aw, "log", lambda me, line: None), \
                    mock.patch.object(aw, "post_reply",
                                      lambda me, cfg, text, *a, **k: posted.append(text) or True), \
                    mock.patch.object(aw, "state_path", lambda me: os.path.join(tmp, ".g.json")), \
                    mock.patch.object(aw.okf_land, "land", lambda **k: landed or {"skipped": "test"}):
                aw.main(["--agent", "gemini", "--max", "1"])
        finally:
            aw.AGENTS["gemini"]["module"] = saved_module
            aw.sys.modules.pop("fake_scrub_adapter", None)
        self.assertEqual(1, len(posted))
        return posted[0]

    def test_a_secret_in_the_model_reply_never_reaches_the_board(self):
        reply = "The log said " + " and ".join(line for line, _ in SHAPES.values())
        posted = self.run_waker(reply, {"GEMINI_PRIVATE_REPOS": ""})
        for label, (_, leaks) in SHAPES.items():
            for leak in leaks:
                self.assertNotIn(leak, posted, label)
        self.assertIn("The log said", posted)


class Finding2TheViewerTokenNeverCrossesARedirect(unittest.TestCase):
    def test_a_redirect_is_refused_and_the_token_never_reaches_its_target(self):
        import http.server
        import os
        import threading
        from unittest import mock

        seen = []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        target = http.server.HTTPServer(("127.0.0.1", 0), Target)

        class Redirect(Target):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/steal" % target.server_port)
                self.end_headers()

        source = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        for server in (target, source):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    cc._call("http://127.0.0.1:%d/v2/x" % source.server_port, "viewer-token-FAKE",
                             timeout=5)
                refused.exception.close()
        finally:
            for server in (target, source):
                server.shutdown()
                server.server_close()
        self.assertEqual([], [h for h in seen if "viewer-token-FAKE" in str(h)],
                         "the viewer token reached the redirect's target")
        self.assertEqual([], seen, "a redirect was followed")

    def test_the_token_is_an_unredirected_header(self):
        # Belt and braces: even a redirect the opener did follow would not carry it.
        from unittest import mock
        body = mock.MagicMock()
        body.read.return_value = b"{}"
        body.__enter__.return_value = body
        with mock.patch.object(cc._OPENER, "open", return_value=body) as opened:
            cc._call(cc.RUN + "/services", "viewer-token-FAKE")
        req = opened.call_args[0][0]
        self.assertEqual("Bearer viewer-token-FAKE", req.unredirected_hdrs.get("Authorization"))
        self.assertNotIn("Authorization", req.headers)


class Finding3TheFooterStatesTheAccessTheSwitchesGive(unittest.TestCase):
    run_waker = Finding1TheReplyIsScrubbedBeforeTheBoard.run_waker

    def test_switches_on_the_footer_names_the_private_repo_and_the_cloud_reads(self):
        posted = self.run_waker("ok", {"GEMINI_PRIVATE_REPOS": "conference", "GEMINI_CLOUD_CONTEXT": ""})
        self.assertIn("PRIVATE conference repository", posted)
        self.assertIn("Cloud Run describe and Logging reads", posted)
        for denial in ("no private repo", "no repo, no cloud CLI"):
            self.assertNotIn(denial, posted)

    def test_switches_off_the_footer_claims_neither(self):
        posted = self.run_waker("ok", {"GEMINI_PRIVATE_REPOS": "", "GEMINI_CLOUD_CONTEXT": "off"})
        self.assertNotIn("PRIVATE conference repository", posted)
        self.assertNotIn("Cloud Run", posted)
        self.assertIn("read-only diffs of the pull requests a row names in Blackboard", posted)

    def test_a_landed_okf_reply_no_longer_denies_the_private_repo(self):
        posted = self.run_waker("notes", {"GEMINI_PRIVATE_REPOS": "conference", "GEMINI_CLOUD_CONTEXT": ""},
                                ask="land=okf|notes please",
                                landed={"ok": True, "pr": "https://github.com/sfdc-24/conference/pull/1"})
        self.assertIn("okf=https://github.com/sfdc-24/conference/pull/1", posted)
        self.assertNotIn("no private repo", posted)
        self.assertIn("PRIVATE conference repository", posted)
        self.assertIn("Cloud Run describe and Logging reads", posted)

    def test_an_agent_with_no_adapter_reads_still_says_so(self):
        import agent_waker as aw
        self.assertIn("no shell, no repo, no cloud CLI", aw.hands_note("grok", aw.AGENTS["grok"], env={}))


class Finding4TheSmallerItems(unittest.TestCase):
    def test_a_list_failure_is_not_attached_to_a_row_that_names_no_resource(self):
        self.assertEqual("", cc.context_for("what is the biggest risk?", token="t",
                                            call=fake_call([], fail="down")))

    def test_a_malformed_resource_is_one_line_never_a_crash(self):
        bad = dict(SERVICE, conditions=["x"], template={"containers": ["not a dict"], "vpcAccess": "x"})

        def call(url, token, body=None, timeout=15):
            if url == cc.LOGGING:
                return {"entries": ["not a dict", LOG]}
            return {"services": [bad]} if "/services" in url else {}
        out = cc.context_for("is conference-gateway healthy? show errors", token="t", call=call)
        self.assertIn("service conference-gateway", out)
        self.assertIn("  image: ?", out)
        self.assertIn("  ready: ? ", out)
        self.assertIn("  vpc egress: none", out)
        self.assertIn("logs, WARNING or worse", out)

    def test_one_resource_that_cannot_be_described_is_one_line(self):
        from unittest import mock
        with mock.patch.object(cc, "describe", side_effect=KeyError("x")):
            out = cc.context_for("is conference-gateway healthy?", token="t", call=fake_call([]))
        self.assertIn("service conference-gateway\n  state: not attached (KeyError)", out)

    def test_anything_unexpected_never_fails_the_answer(self):
        def call(url, token, body=None, timeout=15):
            raise RuntimeError("boom")
        out = cc.context_for("conference-gateway service", token="t", call=call)
        self.assertIn("not attached (RuntimeError)", out)

    def test_the_switch_turns_every_read_off(self):
        calls = []
        self.assertTrue(cc.enabled({}))
        for off in ("off", "0", "false", "No"):
            self.assertFalse(cc.enabled({"GEMINI_CLOUD_CONTEXT": off}), off)
            self.assertEqual("", cc.context_for("conference-gateway logs", token="t", call=fake_call(calls),
                                                env={"GEMINI_CLOUD_CONTEXT": off}))
        self.assertEqual([], calls)

    def test_the_list_follows_next_page_token(self):
        calls = []

        def call(url, token, body=None, timeout=15):
            calls.append(url)
            if "/services" in url:
                if "pageToken=p%2F2" in url:
                    return {"services": [SERVICE]}
                return {"services": [], "nextPageToken": "p/2"}
            return {}
        out = cc.context_for("is conference-gateway healthy?", token="t", call=call)
        self.assertIn("service conference-gateway", out)
        self.assertEqual(2, sum("/services" in u for u in calls))

    def test_the_log_filter_is_pinned_to_the_region(self):
        calls = []
        cc.context_for("bus-requests logs", token="t", call=fake_call(calls))
        filters = [body["filter"] for url, body in calls if url == cc.LOGGING]
        self.assertTrue(filters)
        for f in filters:
            self.assertIn('resource.labels.location="%s"' % cc.REGION, f)


if __name__ == "__main__":
    unittest.main()

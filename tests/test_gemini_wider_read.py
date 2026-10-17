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


class FlagValuesForTheChecker(unittest.TestCase):
    """Gate 3 Step 3: Gemini must read the live config back; the allowlisted flags show their values,
    and nothing else does (owner, 2026-10-15: "yes, option 1")."""

    def service(self, env):
        return {"name": "projects/sfdc24/locations/us-central1/services/drive-intake",
                "template": {"containers": [{"image": "x", "env": env}]}}

    def test_the_step3_flags_are_shown_and_everything_else_is_withheld(self):
        text = cc.describe("service", self.service([
            {"name": "DRIVE_INTAKE_ENABLED", "value": "on"},
            {"name": "DRIVE_INTAKE_FOLDER_ID", "value": "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"},
            {"name": "DRIVE_INTAKE_RECIPIENTS", "value": "claude-api"},
            {"name": "DRIVE_INTAKE_FIRST_POLL", "value": "baseline"},
            {"name": "REDIS_HOST", "value": "10.1.2.3"},
            {"name": "INBOX_WAKE_URL", "value": "https://inbox-wake.example/wake"},
            {"name": "DRIVE_INTAKE_POLL_TOKEN", "valueSource": {"secretKeyRef": {"secret": "projects/1/secrets/DRIVE_INTAKE_POLL_TOKEN"}}},
        ]))
        self.assertIn("DRIVE_INTAKE_ENABLED=on", text)
        self.assertIn("DRIVE_INTAKE_FOLDER_ID=1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP", text)
        self.assertIn("DRIVE_INTAKE_RECIPIENTS=claude-api", text)
        self.assertIn("DRIVE_INTAKE_FIRST_POLL=baseline", text)
        for hidden in ("10.1.2.3", "inbox-wake.example", "DRIVE_INTAKE_POLL_TOKEN="):
            self.assertNotIn(hidden, text)

    def test_an_allowlisted_name_with_an_unsafe_value_is_still_withheld(self):
        for value in ("10.0.0.7", "a b", "x" * 65, "https://evil.example/", ""):
            text = cc.describe("service", self.service([{"name": "INBOX_WAKE_AGENTS", "value": value}]))
            self.assertIn("INBOX_WAKE_AGENTS=(withheld: not a plain flag value)", text, value)
            if value:
                self.assertNotIn(value, text.split("flag values")[1], value)
        ref = cc.describe("service", self.service([{"name": "INBOX_WAKE_ENABLED",
                                                    "valueSource": {"secretKeyRef": {"secret": "s"}}}]))
        self.assertNotIn("flag values", ref, "a secret reference is never a shown flag")

    def test_the_list_holds_no_secret_shaped_name(self):
        for name in cc.SHOWN_ENV:
            for bad in ("TOKEN", "SECRET", "KEY", "PASS", "HOST", "URL", "AUTH"):
                self.assertNotIn(bad, name)
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


class MergeCommitAndReviewThreads(unittest.TestCase):
    """Gate 3 Step 2's proof is a merge hash and 0 unresolved threads; the owner made Gemini its
    checker, 2026-10-15 ("have Gemini check Step 2"), so its excerpt must carry both."""

    def get(self, merged=True, private=False):
        def get(path, token, timeout=20):
            if "/files?" in path:
                return [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0,
                         "patch": "@@ -1 +1 @@\n+x"}]
            return {"title": "t", "state": "closed", "changed_files": 1,
                    "merged_at": "2026-10-15T17:16:07Z" if merged else None,
                    "merge_commit_sha": "e" * 40, "head": {"sha": "a" * 40},
                    "base": {"sha": "b" * 40, "repo": {"private": private}}}
        return get

    def test_a_merged_pr_says_its_merge_commit_in_full(self):
        out = rc.context_for("Blackboard #411", env={}, get=self.get())
        self.assertIn("merged 2026-10-15T17:16:07Z as merge commit " + "e" * 40, out)

    def test_an_unmerged_pr_names_no_merge_commit(self):
        out = rc.context_for("Blackboard #411", env={}, get=self.get(merged=False))
        self.assertIn("[closed, head", out)
        self.assertNotIn("merge commit", out)

    def test_the_threads_line_counts_the_unresolved(self):
        seen = []

        def threads(repo, number, token, timeout):
            seen.append((repo, number))
            return {"totalCount": 3, "nodes": [{"isResolved": True}, {"isResolved": False}, {"isResolved": True}]}
        out = rc.context_for("Blackboard #362", env={}, get=self.get(), threads=threads)
        self.assertEqual([("Blackboard", 362)], seen)
        self.assertRegex(out, r"review threads: 3; unresolved: 1 \(read 20\d\d-\d\d-\d\d \d\d:\d\dZ\)")
        self.assertLess(out.index("review threads"), out.index("COMPLETE"), "the line sits under the header")

    def test_threads_past_the_first_page_are_said_not_read(self):
        out = rc.context_for("Blackboard #362", env={}, get=self.get(),
                             threads=lambda *a: {"totalCount": 150, "nodes": [{"isResolved": True}] * 100})
        self.assertIn("review threads: 150; unresolved: 0 among the first 100 read, the other 50 NOT read", out)

    def test_a_failed_thread_read_is_a_note_and_the_excerpt_goes_out(self):
        def threads(*a):
            raise ValueError("graphql errors")
        out = rc.context_for("Blackboard #362", env={}, get=self.get(), threads=threads)
        self.assertIn("review threads: not readable (ValueError).", out)
        self.assertIn("+x", out)
        for shape in ({"totalCount": "9", "nodes": []}, {"totalCount": 9}, {}):
            out = rc.context_for("Blackboard #362", env={}, get=self.get(), threads=lambda *a, s=shape: s)
            self.assertIn("review threads: not readable", out, shape)

    def test_a_refused_private_pr_asks_for_no_threads(self):
        seen = []
        out = rc.context_for("Blackboard #362", env={}, get=self.get(private=True),
                             threads=lambda *a: seen.append(a))
        self.assertIn("not attached (the repository is not public)", out)
        self.assertEqual([], seen)

    def test_a_faked_get_makes_no_live_thread_request(self):
        from unittest import mock
        with mock.patch.object(rc, "_threads", side_effect=AssertionError("live call")):
            out = rc.context_for("Blackboard #362", env={}, get=self.get())
        self.assertNotIn("review threads", out)

    def test_the_live_thread_read_posts_graphql_with_the_token_unredirected(self):
        from unittest import mock
        body = mock.MagicMock()
        body.read.return_value = (b'{"data":{"repository":{"pullRequest":{"reviewThreads":'
                                  b'{"totalCount":1,"nodes":[{"isResolved":true}]}}}}}')
        body.__enter__.return_value = body
        with mock.patch.object(rc._OPENER, "open", return_value=body) as opened:
            got = rc._threads("Blackboard", 362, "tok")
        self.assertEqual({"totalCount": 1, "nodes": [{"isResolved": True}]}, got)
        req = opened.call_args[0][0]
        self.assertEqual(("POST", rc.GRAPHQL), (req.get_method(), req.full_url))
        self.assertEqual("Bearer tok", req.unredirected_hdrs.get("Authorization"))
        self.assertNotIn("Authorization", req.headers)
        self.assertIn(b'"n": 362', req.data)
        body.read.return_value = b'{"errors":[{"message":"x"}]}'
        with mock.patch.object(rc._OPENER, "open", return_value=body):
            with self.assertRaises(ValueError):
                rc._threads("Blackboard", 362, "tok")


SERVICE = {"name": "projects/sfdc24/locations/us-central1/services/conference-gateway",
           "latestReadyRevision": "projects/x/revisions/conference-gateway-00019-k6c",
           "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED", "message": ""}],
           "template": {"serviceAccount": "gw@sfdc24.iam.gserviceaccount.com",
                        "vpcAccess": {"egress": "PRIVATE_RANGES_ONLY"},
                        "containers": [{"image": "us-docker.pkg.dev/x/gw:1",
                                        "env": [{"name": "REDIS_HOST", "value": "10.200.0.9"},
                                                {"name": "LIVEKIT_API_SECRET",
                                                 "valueSource": {"secretKeyRef": {
                                                     "secret": "projects/1/secrets/LIVEKIT_API_SECRET"}}}]}]}}
JOB = {"name": "projects/sfdc24/locations/us-central1/jobs/bus-requests",
       "latestCreatedExecution": {"name": "projects/x/executions/bus-requests-xktq4"},
       "template": {"template": {"containers": [{"image": "img:gov-4d13e97", "env": []}]}}}
LOG = {"timestamp": "2026-10-08T17:00:00.123Z", "severity": "ERROR",
       "textPayload": "connect to 10.200.0.9:6378 failed for abdus@sfdc24.com with Bearer abc.def "
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
        self.assertNotIn("10.200.0.9", out, "an env value reached the context")

    def test_logs_only_when_asked_and_always_redacted(self):
        calls = []
        quiet = cc.context_for("describe bus-requests", token="t", call=fake_call(calls))
        self.assertNotIn("logs, WARNING", quiet)
        self.assertFalse(any(u == cc.LOGGING for u, _ in calls))
        out = cc.context_for("why did bus-requests fail? show the logs", token="t", call=fake_call([]))
        for leaked in ("10.200.0.9", "abdus@sfdc24.com", "abc.def", "AIzaSyA1234567890", "code=zzz"):
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
        # Grok was this test's example until it gained redis_requests (#365); an agent config with no
        # read and no write is the case the line exists for.
        import agent_waker as aw
        self.assertIn("no shell, no repo, no cloud CLI", aw.hands_note("grok", {}, env={}))


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


# ------------------------------------------------------------------ a PAST state, for a re-check
# Gate 3 Step 1b (owner, 2026-10-17): Gemini re-checks a run of 2026-10-15 15:53Z. Its first re-check
# could only FAIL: it saw the current revision and the last two hours, and no flag value. The shapes
# below are the REAL API's, read 2026-10-17 14:45Z (worker pool revision conference-chair-pool-00034-s48,
# service revision drive-intake-00006-4vc, and the pool's log entries); the values are fake except
# names, the commit label and the image digest the check is about.

POOL = {"name": "projects/sfdc24/locations/us-central1/workerPools/conference-chair-pool",
        "latestReadyRevision": "projects/sfdc24/locations/us-central1/workerPools/conference-chair-pool/"
                               "revisions/conference-chair-pool-00035-hhh",
        "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED", "message": ""}],
        "template": {"containers": [{"image": "img@sha256:" + "8" * 64, "env": []}]}}
CHAIR = {"name": "projects/sfdc24/locations/us-central1/services/conference-chair",
         "template": {"containers": [{"image": "img", "env": []}]}}
DIGEST = "sha256:e078a2c35ba9ea77b6ed2c023a8ae3789ff50cebb1b2a1e619492d1547281fb7"
REVISION = {
    "name": "projects/sfdc24/locations/us-central1/workerPools/conference-chair-pool/revisions/"
            "conference-chair-pool-00034-s48",
    "uid": "u", "generation": "34", "labels": {"commit": "d157cda4c941", "package": "wp1-chair"},
    "createTime": "2026-10-15T15:52:44.487270Z", "updateTime": "2026-10-15T15:52:44Z",
    "launchStage": "BETA", "vpcAccess": {"egress": "PRIVATE_RANGES_ONLY",
                                         "networkInterfaces": [{"network": "default", "subnetwork": "default"}]},
    "serviceAccount": "chair@sfdc24.iam.gserviceaccount.com",
    "containers": [{"name": "chair", "image": "us-central1-docker.pkg.dev/sfdc24/x/conference-chair@" + DIGEST,
                    "env": [{"name": "REDIS_HOST", "value": "10.200.0.9"},
                            {"name": "LIVEKIT_API_SECRET",
                             "valueSource": {"secretKeyRef": {"secret": "projects/1/secrets/LIVEKIT_API_SECRET",
                                                              "version": "latest"}}},
                            {"name": "CONF_REDIS_CHECK", "value": "1"},
                            {"name": "CONF_REDIS_CHECK_ROOMS", "value": "conf-synthetic-gate3-20261014"},
                            {"name": "CONF_REDIS_CHECK_OWNER", "value": "0f1e2d3c4b5a6978"}],
                    "resources": {"limits": {"cpu": "2", "memory": "2Gi"}}}],
    "executionEnvironment": "EXECUTION_ENVIRONMENT_GEN2",
    "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED", "message": "",
                    "lastTransitionTime": "2026-10-15T15:53:00Z", "revisionReason": "RETIRED"}],
    "observedGeneration": "34", "logUri": "https://console.cloud.google.com/logs?x=y",
    "creator": "abdus@sfdc24.com", "etag": "e"}
# The real entries carry no severity at all (the chair writes plain text to stdout).
CHECK_LOGS = [
    {"insertId": "a", "labels": {"commit": "d157cda4c941", "instanceId": "i", "package": "wp1-chair"},
     "logName": "projects/sfdc24/logs/run.googleapis.com%2Fstderr", "receiveTimestamp": "x",
     "resource": {"type": "cloud_run_worker_pool", "labels": {"worker_pool_name": "conference-chair-pool"}},
     "textPayload": "INFO:conference.chair:redis check 10: ok in 29 ms", "timestamp": "2026-10-15T15:53:47.002914Z"},
    {"insertId": "b", "labels": {}, "logName": "l", "receiveTimestamp": "x", "resource": {},
     "textPayload": "INFO:conference.chair:redis round: Redis check 10. Proved, read and said: Grok, Claude, "
                    "Codex, Gemini. 4 of 4 proved. These are reads the chair made for each voice.",
     "timestamp": "2026-10-15T15:54:09.960909Z"},
]
NOW = 1792248000.0          # 2026-10-17T14:40:00Z
ASK = ('@Gemini STEP 1B RE-CHECK ASK. (a) conference-chair-pool in sfdc24 us-central1, revision '
       'conference-chair-pool-00034-s48: its commit label and its image digest. (b) Pool logs, worker_pool_name '
       'conference-chair-pool, 2026-10-15 15:53:20Z to 15:55:30Z. Look for "redis check 10: ok", the round, and '
       '"Redis check 10. Proved, read and said: Grok, Claude, Codex, Gemini. 4 of 4 proved." (c) CONF_REDIS_CHECK=1.')


def past_call(calls, revision=REVISION, entries=CHECK_LOGS, fail=None):
    def call(url, token, body=None, timeout=15):
        calls.append((url, body))
        if url.endswith("/workerPools?pageSize=100"):
            return {"workerPools": [POOL]}
        if url.endswith("/services?pageSize=100"):
            return {"services": [CHAIR]}
        if url.endswith("/jobs?pageSize=100"):
            return {}
        if "/revisions/" in url:
            if fail:
                raise fail
            return revision
        if url == cc.LOGGING:
            return {"entries": entries}
        raise AssertionError(url)
    return call


class APastStateForARecheck(unittest.TestCase):
    def context(self, text, calls=None, **kw):
        from unittest import mock
        with mock.patch.object(cc.time, "time", return_value=NOW):
            return cc.context_for(text, token="t", call=past_call([] if calls is None else calls, **kw))

    def test_the_step1b_recheck_sees_the_revision_digest_flags_and_the_window_lines(self):
        calls = []
        out = self.context(ASK, calls)
        self.assertIn("worker pool revision conference-chair-pool-00034-s48", out)
        self.assertIn("  image: us-central1-docker.pkg.dev/sfdc24/x/conference-chair@" + DIGEST, out)
        self.assertIn("  created: 2026-10-15T15:52:44.487270Z", out)
        self.assertIn("  labels: commit=d157cda4c941, package=wp1-chair", out)
        self.assertIn("CONF_REDIS_CHECK=1, CONF_REDIS_CHECK_ROOMS=conf-synthetic-gate3-20261014", out)
        self.assertIn("2026-10-15T15:53:47 DEFAULT INFO:conference.chair:redis check 10: ok in 29 ms", out)
        self.assertIn("4 of 4 proved.", out)
        self.assertIn("  logs from 2026-10-15T15:53:20Z to 2026-10-15T15:55:30Z, every severity", out)
        for hidden in ("10.200.0.9", "0f1e2d3c4b5a6978", "abdus@sfdc24.com"):
            self.assertNotIn(hidden, out, hidden)
        revision_urls = [u for u, _ in calls if "/revisions/" in u]
        self.assertEqual([cc.RUN + "/workerPools/conference-chair-pool/revisions/conference-chair-pool-00034-s48"],
                         revision_urls)

    def test_the_window_filter_is_bounded_every_severity_oldest_first_and_narrowed_by_the_quotes(self):
        calls = []
        self.context(ASK, calls)
        bodies = [b for u, b in calls if u == cc.LOGGING]
        self.assertEqual(1, len(bodies))
        f = bodies[0]["filter"]
        self.assertIn('timestamp>="2026-10-15T15:53:20Z" AND timestamp<="2026-10-15T15:55:30Z"', f)
        self.assertNotIn("severity", f)
        self.assertIn('resource.labels.worker_pool_name="conference-chair-pool"', f)
        self.assertIn('(textPayload:"redis check 10: ok" OR textPayload:"Redis check 10. Proved, read and said: '
                      'Grok, Claude, Codex, Gemini. 4 of 4 proved.")', f)
        self.assertEqual("timestamp asc", bodies[0]["orderBy"])

    def test_no_window_keeps_the_two_hour_warning_read(self):
        calls = []
        self.context("conference-chair-pool logs please", calls)
        f = [b for u, b in calls if u == cc.LOGGING][0]["filter"]
        self.assertIn("severity>=WARNING", f)
        self.assertNotIn("timestamp<=", f)

    def test_a_window_reads_logs_even_without_the_word_logs(self):
        calls = []
        self.context("conference-chair-pool 2026-10-15T15:53:20Z to 2026-10-15T15:55:30Z", calls)
        self.assertTrue(any(u == cc.LOGGING for u, _ in calls))

    def test_both_window_forms_parse(self):
        want = ("2026-10-15T15:53:20Z", "2026-10-15T15:55:30Z")
        self.assertEqual(want, cc.window("2026-10-15 15:53:20Z to 15:55:30Z", now=NOW))
        self.assertEqual(want, cc.window("2026-10-15T15:53:20Z to 2026-10-15T15:55:30Z", now=NOW))
        self.assertEqual(("2026-10-15T15:53:00Z", "2026-10-15T15:55:00Z"),
                         cc.window("2026-10-15 15:53Z to 15:55Z", now=NOW))
        self.assertIsNone(cc.window("by 2026-10-18 14:27Z, whichever first", now=NOW))

    def test_a_window_that_cannot_be_a_check_is_refused_and_says_why(self):
        self.assertEqual("over 15 minutes", cc.window("2026-10-15 15:00:00Z to 15:15:01Z", now=NOW))
        self.assertEqual(("2026-10-15T15:00:00Z", "2026-10-15T15:15:00Z"),
                         cc.window("2026-10-15 15:00:00Z to 15:15:00Z", now=NOW))
        self.assertEqual("it ends before it starts", cc.window("2026-10-15 15:55Z to 15:53Z", now=NOW))
        self.assertEqual("it ends before it starts", cc.window("2026-10-15 15:55Z to 15:55Z", now=NOW))
        self.assertEqual("it ends in the future", cc.window("2026-10-17 14:35Z to 14:41Z", now=NOW))
        self.assertEqual(("2026-10-17T14:35:00Z", "2026-10-17T14:40:00Z"),
                         cc.window("2026-10-17 14:35Z to 14:40Z", now=NOW))
        self.assertEqual("it starts over 30 days ago, past log retention",
                         cc.window("2026-09-17 14:39Z to 14:45Z", now=NOW))
        self.assertEqual(("2026-09-17T14:40:00Z", "2026-09-17T14:45:00Z"),
                         cc.window("2026-09-17 14:40Z to 14:45Z", now=NOW))
        self.assertEqual("it starts over 30 days ago, past log retention",
                         cc.window("2026-09-17 14:39:59Z to 14:45Z", now=NOW))
        self.assertEqual(("2026-10-15T23:55:00Z", "2026-10-16T00:05:00Z"),
                         cc.window("2026-10-15 23:55Z to 2026-10-16 00:05Z", now=NOW), "a window across midnight")
        self.assertEqual("not a date and time", cc.window("2026-13-15 15:53Z to 15:55Z", now=NOW))
        out = self.context("conference-chair-pool 2026-10-15 15:00Z to 16:00Z")
        self.assertIn("logs: the stated window is refused (over 15 minutes)", out)

    def test_a_quote_can_never_reach_the_filter(self):
        self.assertEqual(["a", "b"], cc.phrases('x "a" OR severity>=DEFAULT OR "b" y'))
        self.assertEqual(["ok"], cc.phrases('"ok" and an unclosed "quote'))
        self.assertEqual([], cc.phrases('"back\\slash" "new\nline" "%s"' % ("x" * 121)))
        self.assertEqual(["x" * 120], cc.phrases('"%s"' % ("x" * 120)))
        self.assertEqual(["1", "2", "3", "4"], cc.phrases('"1" "2" "3" "4" "5" "1"'))
        self.assertEqual(["a"], cc.phrases('"a" "a"'))
        for p in cc.phrases(ASK + ' "x\\" OR 1"'):
            self.assertNotIn('"', p)
            self.assertNotIn("\\", p)

    def test_a_revision_is_named_only_of_a_named_resource_and_never_guessed(self):
        calls = []
        self.context("revision conference-chair-pool-00034-s48 alone", calls)
        self.assertFalse(any("/revisions/" in u for u, _ in calls), "the pool itself was not named")
        calls = []
        self.context("conference-chair-pool, which revision ran?", calls)
        self.assertFalse(any("/revisions/" in u for u, _ in calls), "no revision was named")

    def test_a_resource_name_is_never_read_as_a_revision_of_a_shorter_one(self):
        found = [("service", CHAIR), ("worker pool", POOL)]
        self.assertEqual([], cc.revisions_named("conference-chair and conference-chair-pool", found, found))
        self.assertEqual([("worker pool", "conference-chair-pool", "conference-chair-pool-00034-s48")],
                         cc.revisions_named("conference-chair conference-chair-pool conference-chair-pool-00034-s48",
                                            found, found))
        self.assertEqual([("service", "conference-chair", "conference-chair-00017-c2x")],
                         cc.revisions_named("conference-chair conference-chair-00017-c2x", found, found))

    def test_at_most_two_revisions_and_only_whole_words(self):
        found = [("worker pool", POOL)]
        text = ("conference-chair-pool-00001-aaa conference-chair-pool-00002-bbb conference-chair-pool-00003-ccc "
                "conference-chair-pool-00001-aaa xconference-chair-pool-00009-zzz conference-chair-pool-")
        self.assertEqual(["conference-chair-pool-00001-aaa", "conference-chair-pool-00002-bbb"],
                         [r for _, _, r in cc.revisions_named(text, found, found)])
        self.assertEqual(2, cc.MAX_REVISIONS)
        self.assertEqual(["conference-chair-pool-00001-aaa", "conference-chair-pool-00002-bbb"],
                         [r for _, _, r in cc.revisions_named(
                             "conference-chair-pool- conference-chair-poolx-00007-q conference-chair-pool-00001-aaa "
                             "conference-chair-pool-00001-aaa conference-chair-pool-00002-bbb", found, found)],
                         "a trailing dash, a longer word and a repeat are not revisions")

    def test_a_job_has_no_revisions(self):
        found = [("job", JOB)]
        self.assertEqual([], cc.revisions_named("bus-requests bus-requests-xktq4", found, found))

    def test_a_revision_the_api_does_not_have_is_one_line(self):
        err = urllib.error.HTTPError("u", 404, "not found", {}, None)
        out = self.context("conference-chair-pool conference-chair-pool-00099-zzz", fail=err)
        self.assertIn("worker pool revision conference-chair-pool-00099-zzz\n  state: not attached (HTTP 404)", out)
        out = self.context("conference-chair-pool conference-chair-pool-00099-zzz", fail=urllib.error.URLError("x"))
        self.assertIn("conference-chair-pool-00099-zzz\n  state: not attached (URLError)", out)

    def test_a_malformed_revision_is_described_without_a_crash(self):
        out = self.context("conference-chair-pool conference-chair-pool-00034-s48",
                           revision={"containers": ["x"], "labels": "x", "conditions": "x"})
        self.assertIn("  image: ?", out)
        self.assertIn("  labels: none", out)
        self.assertIn("  service account: default compute", out)
        out = cc.describe_revision("worker pool", dict(REVISION, labels={"commit": "d157cda4c941",
                                                                          "who": "abdus@sfdc24.com"}))
        self.assertIn("  labels: commit=d157cda4c941, who=[email]", out)

    def test_a_window_with_no_lines_says_so(self):
        out = self.context(ASK, entries=[])
        self.assertIn('logs: no lines from 2026-10-15T15:53:20Z to 2026-10-15T15:55:30Z, lines holding "redis check 10: ok"',
                      out)

    def test_window_log_lines_are_redacted_and_capped(self):
        out = self.context("conference-chair-pool 2026-10-15 15:53Z to 15:55Z",
                           entries=[LOG] + [dict(CHECK_LOGS[0])] * 20)
        self.assertNotIn("10.200.0.9", out)
        self.assertIn("2026-10-08T17:00:00 ERROR", out)
        self.assertEqual(cc.MAX_LOG_LINES, out.count("redis check 10: ok") + 1)

    def test_the_check_owner_is_never_shown(self):
        self.assertIn("CONF_REDIS_CHECK", cc.SHOWN_ENV)
        self.assertIn("CONF_REDIS_CHECK_ROOMS", cc.SHOWN_ENV)
        self.assertNotIn("CONF_REDIS_CHECK_OWNER", cc.SHOWN_ENV)
        out = cc.describe_revision("worker pool", REVISION)
        self.assertIn("CONF_REDIS_CHECK_OWNER", out.split("flag values")[0], "its name is listed")
        self.assertNotIn("0f1e2d3c4b5a6978", out)

    def test_it_still_never_writes(self):
        calls = []
        self.context(ASK, calls)
        self.assertEqual({cc.LOGGING}, {u for u, body in calls if body is not None})


if __name__ == "__main__":
    unittest.main()

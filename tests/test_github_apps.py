"""The two GitHub Apps for the Console route (Blackboard #306): their manifests ask for exactly the planned
permissions, the owner's click page carries the same manifests, and the converter never shows the private key or the
one-time code. The converter's request goes to a fake GitHub inside the process: nothing leaves it."""
import email.message
import http.client
import io
import json
import re
import socket
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import github_app_convert as conv  # noqa: E402

APPS = ROOT / "docs" / "github-apps"
PLANNED = {
    "sfdc24-cloud-clone": {"contents": "read", "metadata": "read"},
    "sfdc24-ccc-broker": {"contents": "write", "pull_requests": "write", "metadata": "read"},
}
PEM = "-----BEGIN RSA PRIVATE KEY-----\nTESTKEYMATERIAL123\n-----END RSA PRIVATE KEY-----\n"
CODE = "c0de" * 10


def manifest(slug):
    return json.loads((APPS / ("%s.manifest.json" % slug)).read_text(encoding="utf-8"))


class ManifestTest(unittest.TestCase):
    def test_each_app_asks_for_exactly_its_planned_permissions_and_nothing_else(self):
        for slug, perms in PLANNED.items():
            m = manifest(slug)
            self.assertEqual(slug, m["name"])
            self.assertEqual(perms, m["default_permissions"], slug)     # no administration, workflows, actions...
            self.assertFalse(m["public"], slug)                         # installable only by sfdc-24
            self.assertFalse(m["hook_attributes"]["active"], slug)      # no webhook receives anything
            self.assertEqual([], m["default_events"], slug)
            # The one-time code rides on this redirect: it must stay on the laptop, never reach a public site's logs
            # (Codex on #309). Browsers refuse port 9, so no request is even made.
            self.assertEqual("http://localhost:9/", m["redirect_url"], slug)

    def test_the_owner_page_posts_the_same_manifests_to_the_personal_account_page(self):
        html = (APPS / "create-apps.html").read_text(encoding="utf-8")
        inline = json.loads(re.search(r'<script id="manifests" type="application/json">(.*?)</script>', html,
                                      re.S).group(1))
        for slug in PLANNED:
            self.assertEqual(manifest(slug), inline[slug], slug)
            self.assertIn('action="https://github.com/settings/apps/new?state=%s"' % slug, html)


class FakeGitHub(urllib.request.BaseHandler):
    """Answers https requests inside the process, ahead of urllib's real HTTPS handler, and remembers each one."""
    handler_order = 10

    def __init__(self, status=201, body=b"", location=None, error=None, read_error=None):
        self.status, self.body, self.location, self.error, self.requests = status, body, location, error, []
        self.read_error = read_error                                    # raised by response.read(), after the status

    def https_open(self, req):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        headers = email.message.Message()
        if self.location:
            headers["Location"] = self.location
        body = io.BytesIO(self.body)
        if self.read_error is not None:
            def cut_off(*_):
                raise self.read_error
            body.read = cut_off
        response = urllib.response.addinfourl(body, headers, req.full_url, self.status)
        response.msg = "fake"
        return response

    def opener(self):
        return conv.opener_for(self)


class Recorder:
    """Stands in for subprocess.run: answers gcloud, and remembers every call."""

    def __init__(self, gcloud_rc=0):
        self.gcloud_rc, self.calls = gcloud_rc, []

    def __call__(self, args, input=None, capture_output=True, text=True):
        self.calls.append((list(args), input))
        return subprocess.CompletedProcess(args, self.gcloud_rc, "", "ERROR: " + (input or ""))


def app(slug, perms=None, pem=PEM, owner="sfdc-24", events=()):
    return {"id": 123, "slug": slug, "permissions": perms or PLANNED[slug], "pem": pem, "events": list(events),
            "owner": {"login": owner, "type": "User"} if owner is not None else None,
            "webhook_secret": "WEBHOOKSECRET", "client_secret": "CLIENTSECRET"}


def github_with(answer, **kw):
    return FakeGitHub(body=json.dumps(answer).encode(), **kw)


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self._gcloud = conv.gcloud_cmd
        conv.gcloud_cmd = lambda: ["gcloud"]                            # as on a machine with gcloud on PATH

    def tearDown(self):
        conv.gcloud_cmd = self._gcloud

    def run_main(self, argv, github, run=None, code=CODE):
        said, asked = [], []
        run = run or Recorder()

        def ask():
            asked.append(True)
            return code + "\n"
        rc = conv.main(argv, run=run, say=said.append, ask=ask, opener=github.opener())
        return rc, "\n".join(said), run, asked

    def assert_no_secret_shown(self, out, run):
        for secret in ("TESTKEYMATERIAL", "WEBHOOKSECRET", "CLIENTSECRET", CODE):
            self.assertNotIn(secret, out)
        for args, _ in run.calls:
            self.assertNotIn(CODE, " ".join(args))                      # the code reaches no command line

    def test_no_gcloud_anywhere_stops_before_anything_is_sent(self):
        # The real lookup, on a host with no gcloud on PATH and no Cloud SDK (Copilot and Cursor on 148104b: this test
        # used to depend on the machine it ran on, and still expected the old wording).
        conv.gcloud_cmd = self._gcloud
        which, env = conv.shutil.which, dict(conv.os.environ)
        conv.shutil.which = lambda name: None
        conv.os.environ["LOCALAPPDATA"] = str(Path(__file__).resolve().parent / "no-such-sdk-here")
        try:
            with self.assertRaises(conv.Stop) as caught:
                conv.gcloud_cmd()
            self.assertIn("nothing sent, and the code is still unused", str(caught.exception))
        finally:
            conv.shutil.which = which
            conv.os.environ.clear()
            conv.os.environ.update(env)

    def test_a_gcloud_that_cannot_start_stops_without_storing(self):
        def missing(args, **_):
            raise FileNotFoundError(args[0])
        rc, out, _, _ = self.run_main(["sfdc24-cloud-clone"], github_with(app("sfdc24-cloud-clone")), run=missing)
        self.assertEqual(1, rc)
        self.assertIn("could not start", out)
        self.assertNotIn("TESTKEYMATERIAL", out)

    def test_the_conversion_is_one_unauthenticated_post_made_in_process(self):
        github = github_with(app("sfdc24-ccc-broker"))
        rc, _, _, _ = self.run_main(["sfdc24-ccc-broker"], github)
        self.assertEqual(0, rc)
        self.assertEqual(1, len(github.requests))
        req = github.requests[0]
        self.assertEqual("https://api.github.com/app-manifests/%s/conversions" % CODE, req.full_url)
        self.assertEqual("POST", req.get_method())
        self.assertFalse([h for h in req.header_items() if h[0].lower() == "authorization"])   # no gh login needed
        self.assertEqual(conv.TIMEOUT, req.timeout)

    def test_the_key_goes_to_secret_manager_on_stdin_and_is_never_shown(self):
        rc, out, run, _ = self.run_main(["sfdc24-ccc-broker"], github_with(app("sfdc24-ccc-broker")))
        self.assertEqual(0, rc)
        gcloud = [c for c in run.calls if c[0][0] == "gcloud"]
        self.assertEqual(1, len(gcloud))
        args, stdin = gcloud[0]
        self.assertEqual(PEM, stdin)                                    # on stdin...
        self.assertNotIn(PEM, " ".join(args))                           # ...never in argv
        self.assertIn("GITHUB_APP_BROKER_PRIVATE_KEY", args)
        self.assertIn("--data-file=-", args)
        self.assert_no_secret_shown(out, run)
        self.assertIn("https://github.com/apps/sfdc24-ccc-broker/installations/new", out)

    def test_a_code_on_the_command_line_is_refused_before_anything_is_sent(self):
        # Copilot on #309: argv lands in shell history and the process list.
        github = github_with(app("sfdc24-cloud-clone"))
        rc, out, run, asked = self.run_main([CODE, "sfdc24-cloud-clone"], github)
        self.assertEqual(2, rc)
        self.assertIn("must not go on the command line", out)
        self.assertNotIn(CODE, out)
        self.assertEqual(([], [], []), (github.requests, run.calls, asked))

    def test_a_redirect_is_refused_not_followed(self):
        github = FakeGitHub(status=302, location="https://elsewhere.example/app-manifests/x/conversions")
        rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], github)
        self.assertEqual(1, rc)
        self.assertIn("redirect (HTTP 302)", out)
        self.assertEqual(1, len(github.requests))                       # the first request only
        self.assertEqual([], run.calls)

    def test_a_failed_or_unanswered_conversion_says_why_without_echoing_any_response(self):
        failures = {
            "HTTP 404": FakeGitHub(status=404, body=json.dumps(app("sfdc24-cloud-clone")).encode()),
            "URLError": FakeGitHub(error=urllib.error.URLError(OSError("no route"))),
            "timeout": FakeGitHub(error=socket.timeout("timed out")),
            "not JSON": FakeGitHub(body=b"<html>TESTKEYMATERIAL</html>"),
        }
        for why, github in failures.items():
            rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], github)
            self.assertEqual(1, rc, why)
            self.assertIn("nothing stored", out, why)
            self.assertEqual([], run.calls, why)
            self.assert_no_secret_shown(out, run)
        self.assertIn("HTTP 404", self.run_main(["sfdc24-cloud-clone"], failures["HTTP 404"])[1])

    def test_an_answer_cut_off_or_garbled_after_an_accepted_conversion_stops_safely(self):
        # Copilot on a450a15: IncompleteRead is an http.client.HTTPException, not an OSError, and escaped main().
        cases = {
            "IncompleteRead": FakeGitHub(read_error=http.client.IncompleteRead(b'{"pem": "TESTKEYMATERIAL', 4096)),
            "BadStatusLine": FakeGitHub(error=http.client.BadStatusLine("TESTKEYMATERIAL")),
            "RemoteDisconnected": FakeGitHub(read_error=http.client.RemoteDisconnected("TESTKEYMATERIAL")),
        }
        for why, github in cases.items():
            rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], github)
            self.assertEqual(1, rc, why)
            self.assertIn("STOPPED: conversion got no complete answer from GitHub (%s)" % why, out)
            self.assertIn("the code is spent and the key was never shown", out, why)
            self.assertEqual([], run.calls, why)
            self.assert_no_secret_shown(out, run)

    def test_json_that_is_not_an_app_object_stops_safely(self):
        # Copilot on a450a15: null or [] reached check() and raised AttributeError, past the STOPPED handling.
        for body in (b"null", b"[]", b'"TESTKEYMATERIAL"', b"5", b"true", b"[" * 100000 + b"]" * 100000):
            rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], FakeGitHub(body=body))
            self.assertEqual(1, rc, body[:20])
            self.assertIn("STOPPED:", out, body[:20])
            self.assertIn("nothing stored", out, body[:20])
            self.assertIn("the code is spent", out, body[:20])
            self.assertEqual([], run.calls, body[:20])
            self.assert_no_secret_shown(out, run)

    def test_permissions_that_are_not_an_object_store_nothing(self):
        # dict() of a string or a number raised ValueError or TypeError, past the STOPPED handling.
        for perms in ("ab", "TESTKEYMATERIAL", 5, [["contents", "read"], ["metadata", "read"]], True):
            bad = app("sfdc24-cloud-clone")
            bad["permissions"] = perms
            rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], github_with(bad))
            self.assertEqual(1, rc, perms)
            self.assertIn("not an object: nothing stored", out, perms)
            self.assertFalse(run.calls, perms)
            self.assert_no_secret_shown(out, run)

    def test_a_different_owner_slug_or_wider_permissions_store_nothing(self):
        for bad in (app("sfdc24-cloud-clone", owner="someone-else"),       # Codex on 0928901: the wrong account
                    app("sfdc24-cloud-clone", owner=None),
                    app("sfdc24-cloud-clone", owner="SFDC-24-other"),
                    app("someone-else", PLANNED["sfdc24-cloud-clone"]),
                    app("sfdc24-cloud-clone", {"contents": "write", "metadata": "read"}),
                    app("sfdc24-cloud-clone", dict(PLANNED["sfdc24-cloud-clone"], administration="write"))):
            rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], github_with(bad))
            self.assertEqual(1, rc, bad["slug"])
            self.assertFalse(run.calls, "nothing stored")
            self.assert_no_secret_shown(out, run)

    def test_an_app_with_event_subscriptions_or_no_events_field_stores_nothing(self):
        # Copilot on cecaa74: the registration page is editable, and the manifests subscribe to no events.
        with_events = app("sfdc24-cloud-clone", events=["push", "TESTKEYMATERIAL"])
        no_field = app("sfdc24-cloud-clone")
        del no_field["events"]
        not_a_list = app("sfdc24-cloud-clone")
        not_a_list["events"] = "push"
        for bad in (with_events, no_field, not_a_list):
            rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], github_with(bad))
            self.assertEqual(1, rc)
            self.assertEqual([], run.calls)
            self.assertIn("subscribes to events; the manifest subscribes to none", out)
            self.assert_no_secret_shown(out, run)

    def test_a_refusal_never_echoes_a_response_value(self):
        # Copilot on 4f03668: the permissions refusal printed the response's own object, which can carry any text.
        cases = [app("sfdc24-cloud-clone", {"contents": "TESTKEYMATERIAL", "metadata": "read"}),
                 app("sfdc24-cloud-clone", owner="TESTKEYMATERIAL"),
                 app("TESTKEYMATERIAL", PLANNED["sfdc24-cloud-clone"])]
        for bad in cases:
            rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], github_with(bad))
            self.assertEqual(1, rc)
            self.assertEqual([], run.calls)
            self.assert_no_secret_shown(out, run)               # TESTKEYMATERIAL is never printed
        rc, out, _, _ = self.run_main(["sfdc24-cloud-clone"], github_with(cases[0]))
        self.assertIn('not exactly the manifest\'s {"contents": "read", "metadata": "read"}', out)

    def test_a_refusal_after_the_conversion_says_the_code_is_spent_and_to_delete_the_app(self):
        for bad in (app("sfdc24-cloud-clone", owner="someone-else"), app("other-slug", PLANNED["sfdc24-cloud-clone"]),
                    app("sfdc24-cloud-clone", {"contents": "write", "metadata": "read"}),
                    app("sfdc24-cloud-clone", pem="")):
            rc, out, _, _ = self.run_main(["sfdc24-cloud-clone"], github_with(bad))
            self.assertEqual(1, rc)
            self.assertIn("nothing stored. The code is spent: delete this App on GitHub", out)

    def test_gcloud_is_found_before_the_code_is_spent(self):
        # Copilot on 4f03668: a missing gcloud used to be found only after the one-time conversion.
        def missing():
            raise conv.Stop("gcloud was not found (not on PATH, and no Cloud SDK under LOCALAPPDATA): nothing sent, "
                            "and the code is still unused")
        conv.gcloud_cmd = missing
        github = github_with(app("sfdc24-cloud-clone"))
        rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], github)
        self.assertEqual(1, rc)
        self.assertEqual([], github.requests)                   # the code was never sent
        self.assertIn("the code is still unused", out)
        self.assertNotIn("nothing stored", out)

    def test_gcloud_failing_to_start_after_the_conversion_says_how_to_get_a_new_key(self):
        def missing(args, **_):
            raise FileNotFoundError(args[0])
        rc, out, _, _ = self.run_main(["sfdc24-cloud-clone"], github_with(app("sfdc24-cloud-clone")), run=missing)
        self.assertEqual(1, rc)
        self.assertIn("The code is spent and the key was discarded: generate a new private key", out)

    def test_the_ok_line_prints_the_manifest_and_the_slug_asked_for_never_response_text(self):
        good = app("sfdc24-cloud-clone")
        good["id"] = "TESTKEYMATERIAL"                          # a non-int id is not echoed
        rc, out, run, _ = self.run_main(["sfdc24-cloud-clone"], github_with(good))
        self.assertEqual(0, rc)
        self.assertIn("App sfdc24-cloud-clone (id unknown)", out)
        self.assert_no_secret_shown(out, run)

    def test_a_failed_store_says_to_check_the_secret_without_echoing_it(self):
        # Copilot on #309: once the create has started, a failure is not proof that nothing was stored.
        run = Recorder(gcloud_rc=1)                                     # gcloud's stderr echoes its input here
        rc, out, _, _ = self.run_main(["sfdc24-cloud-clone"], github_with(app("sfdc24-cloud-clone")), run=run)
        self.assertEqual(1, rc)
        self.assertIn("GITHUB_APP_READONLY_PRIVATE_KEY", out)
        self.assertIn("may still have stored the key", out)
        self.assertIn("gcloud secrets versions list GITHUB_APP_READONLY_PRIVATE_KEY --project sfdc24", out)
        self.assertNotIn("nothing stored", out)
        self.assert_no_secret_shown(out, run)

    def test_a_response_without_a_key_stores_nothing(self):
        rc, _, run, _ = self.run_main(["sfdc24-cloud-clone"], github_with(app("sfdc24-cloud-clone", pem="")))
        self.assertEqual(1, rc)
        self.assertFalse(run.calls)

    def test_bad_arguments_or_a_bad_code_send_nothing(self):
        rc, out, _, _ = self.run_main(["sfdc24-cloud-clone"], github_with(app("sfdc24-cloud-clone")), code="nope")
        self.assertIn("nothing sent, and the code is still unused", out)      # the wording the README tells him to match
        for argv, code in (([], CODE), (["other-app"], CODE), (["sfdc24-cloud-clone", "extra"], CODE),
                           (["sfdc24-cloud-clone"], "not-a-code"), (["sfdc24-cloud-clone"], ""),
                           (["sfdc24-cloud-clone"], "../" + CODE)):
            github = github_with(app("sfdc24-cloud-clone"))
            rc, out, run, _ = self.run_main(argv, github, code=code)
            self.assertEqual(2, rc, argv)
            self.assertEqual(([], []), (github.requests, run.calls), argv)


if __name__ == "__main__":
    unittest.main()

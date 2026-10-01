"""The two GitHub Apps for the Console route (Blackboard #306): their manifests ask for exactly the planned
permissions, the owner's click page carries the same manifests, and the converter never shows the private key."""
import json
import re
import subprocess
import sys
import unittest
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

    def test_the_owner_page_posts_the_same_manifests_to_the_personal_account_page(self):
        html = (APPS / "create-apps.html").read_text(encoding="utf-8")
        inline = json.loads(re.search(r'<script id="manifests" type="application/json">(.*?)</script>', html,
                                      re.S).group(1))
        for slug in PLANNED:
            self.assertEqual(manifest(slug), inline[slug], slug)
            self.assertIn('action="https://github.com/settings/apps/new?state=%s"' % slug, html)


class Recorder:
    """Stands in for subprocess.run: answers gh and gcloud, and remembers every call."""

    def __init__(self, app=None, gh_rc=0, gcloud_rc=0):
        self.app, self.gh_rc, self.gcloud_rc, self.calls = app, gh_rc, gcloud_rc, []

    def __call__(self, args, input=None, capture_output=True, text=True):
        self.calls.append((list(args), input))
        if args[0] == "gh":
            body = json.dumps(self.app) if self.app is not None else ""
            return subprocess.CompletedProcess(args, self.gh_rc, body if self.gh_rc == 0 else "",
                                               "" if self.gh_rc == 0 else "gh: Not Found (HTTP 404) " + body)
        return subprocess.CompletedProcess(args, self.gcloud_rc, "", "ERROR: " + (input or ""))


def app(slug, perms=None, pem=PEM):
    return {"id": 123, "slug": slug, "permissions": perms or PLANNED[slug], "pem": pem,
            "webhook_secret": "WEBHOOKSECRET", "client_secret": "CLIENTSECRET"}


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self._gcloud = conv.gcloud_cmd
        conv.gcloud_cmd = lambda: ["gcloud"]                            # as on a machine with gcloud on PATH

    def tearDown(self):
        conv.gcloud_cmd = self._gcloud

    def test_gcloud_not_on_path_falls_back_to_the_sdk_or_stops_storing_nothing(self):
        conv.gcloud_cmd = self._gcloud
        try:
            cmd = conv.gcloud_cmd()
            self.assertTrue(cmd[0])                                     # found one way or the other
        except conv.Stop as stop:
            self.assertIn("nothing stored", str(stop))

    def test_a_tool_that_cannot_start_stops_without_storing(self):
        def missing(args, **_):
            raise FileNotFoundError(args[0])
        rc, out = self.run_main(["f" * 40, "sfdc24-cloud-clone"], missing)
        self.assertEqual(1, rc)
        self.assertIn("could not start", out)

    def run_main(self, argv, run):
        said = []
        rc = conv.main(argv, run=run, say=said.append)
        return rc, "\n".join(said)

    def test_the_key_goes_to_secret_manager_on_stdin_and_is_never_shown(self):
        run = Recorder(app("sfdc24-ccc-broker"))
        rc, out = self.run_main(["a" * 40, "sfdc24-ccc-broker"], run)
        self.assertEqual(0, rc)
        gcloud = [c for c in run.calls if c[0][0] == "gcloud"]
        self.assertEqual(1, len(gcloud))
        args, stdin = gcloud[0]
        self.assertEqual(PEM, stdin)                                    # on stdin...
        self.assertNotIn(PEM, " ".join(args))                           # ...never in argv
        self.assertIn("GH_APP_BROKER_KEY", args)
        self.assertIn("--data-file=-", args)
        for secret in ("TESTKEYMATERIAL", "WEBHOOKSECRET", "CLIENTSECRET"):
            self.assertNotIn(secret, out)
        self.assertIn("https://github.com/apps/sfdc24-ccc-broker/installations/new", out)

    def test_a_different_slug_or_wider_permissions_store_nothing(self):
        for bad in (app("someone-else", PLANNED["sfdc24-cloud-clone"]),
                    app("sfdc24-cloud-clone", {"contents": "write", "metadata": "read"}),
                    app("sfdc24-cloud-clone", dict(PLANNED["sfdc24-cloud-clone"], administration="write"))):
            run = Recorder(bad)
            rc, out = self.run_main(["b" * 40, "sfdc24-cloud-clone"], run)
            self.assertEqual(1, rc, bad["slug"])
            self.assertFalse([c for c in run.calls if c[0][0] == "gcloud"], "nothing stored")
            self.assertNotIn("TESTKEYMATERIAL", out)

    def test_a_failed_conversion_or_store_says_why_without_echoing_any_response(self):
        run = Recorder(app("sfdc24-cloud-clone"), gh_rc=1)              # stderr carries the body; never printed
        rc, out = self.run_main(["c" * 40, "sfdc24-cloud-clone"], run)
        self.assertEqual(1, rc)
        self.assertIn("HTTP 404", out)
        self.assertNotIn("TESTKEYMATERIAL", out)
        run = Recorder(app("sfdc24-cloud-clone"), gcloud_rc=1)          # gcloud's stderr echoes its input here
        rc, out = self.run_main(["c" * 40, "sfdc24-cloud-clone"], run)
        self.assertEqual(1, rc)
        self.assertIn("GH_APP_CLONE_KEY", out)
        self.assertNotIn("TESTKEYMATERIAL", out)

    def test_a_response_without_a_key_stores_nothing(self):
        run = Recorder(app("sfdc24-cloud-clone", pem=""))
        rc, _ = self.run_main(["d" * 40, "sfdc24-cloud-clone"], run)
        self.assertEqual(1, rc)
        self.assertFalse([c for c in run.calls if c[0][0] == "gcloud"])

    def test_bad_arguments_print_usage_and_call_nothing(self):
        for argv in ([], ["not-a-code", "sfdc24-cloud-clone"], ["e" * 40, "other-app"]):
            run = Recorder(app("sfdc24-cloud-clone"))
            rc, out = self.run_main(argv, run)
            self.assertEqual(2, rc, argv)
            self.assertIn("usage", out)
            self.assertEqual([], run.calls)


if __name__ == "__main__":
    unittest.main()

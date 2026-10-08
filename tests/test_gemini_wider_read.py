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
        out = cc.context_for("conference-gateway", token="t", call=fake_call([], fail="down"))
        self.assertIn("not attached", out)

    def test_it_never_writes(self):
        calls = []
        cc.context_for("conference-gateway bus-requests logs", token="t", call=fake_call(calls))
        posts = [u for u, body in calls if body is not None]
        self.assertEqual({cc.LOGGING}, set(posts), "the only POST is the logging READ endpoint")


if __name__ == "__main__":
    unittest.main()

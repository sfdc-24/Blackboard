"""Offline Lead routing acceptance: fake Salesforce, real auth/controller/SSE."""
import copy
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cloud" / "studio-controller"))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient
from app.main import create_app
from app.settings import Settings
from app.workers.lead_facts import LeadFactsWorker, UNAVAILABLE
from workers.org_facts import OrgFacts, QUERIES
from test_studio_controller import CountingWorker, EmailSender, IDs, MemoryStore, make_controller, settings


ORG_ID = "00D000000000001AAA"
ENV = {"Headless_domain": "test.develop.my.salesforce.com",
       "Headless_consumer_key": "fake-id", "Headless_consumer_secret": "fake-secret"}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Salesforce:
    def __init__(self, org_id=ORG_ID):
        self.requests = []
        self.org_id = org_id
        self.overrides = {}
        self.fail_on = None

    def open(self, request, timeout=None):
        self.requests.append(request)
        if request.full_url.endswith("/services/oauth2/token"):
            if self.fail_on == "auth":
                raise urllib.error.HTTPError(request.full_url, 401, "private-auth-details", {}, None)
            body = {"instance_url": "https://test.develop.my.salesforce.com", "access_token": "fake-token"}
        else:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["q"][0]
            name = next(k for k, v in QUERIES.items() if v == query)
            if self.fail_on == name:
                raise urllib.error.HTTPError(request.full_url, 403, "private-query-details", {}, None)
            body = {
                "org": {"records": [{"Id": self.org_id, "Name": "Test dev org", "IsSandbox": False,
                                     "OrganizationType": "Developer Edition"}]},
                "lead_total": {"totalSize": 26},
                "lead_by_source": {"records": [{"LeadSource": "sfdc24.com", "n": 4}], "done": True},
                "lead_site_recent": {"totalSize": 2},
            }[name]
            body = self.overrides.get(name, body)
        return Response(json.dumps(body).encode())

    def queries(self):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query)["q"][0]
                for r in self.requests if "?q=" in r.full_url]


def command(state, command_id="lead-1", item_id="item-lead-1"):
    return {"command_id": command_id, "session_id": state["session_id"],
            "type": "utterance", "expected_version": state["artifact_version"],
            "item_id": item_id, "transcript": "How many leads do we have?"}


class LeadFactsTests(unittest.TestCase):
    def setUp(self):
        # These routing tests retain their fake Salesforce opener. The actual
        # killable process boundary has its own adversarial subprocess suite.
        patcher = mock.patch("app.workers.lead_facts.fetch_lead_facts", side_effect=lambda org_id, timeout, cancel:
                             OrgFacts.from_env(expected_org_id=org_id).lead_counts())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settings_default_off_and_parse_explicit_enable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Settings.from_env().lead_facts_enabled)
        with mock.patch.dict(os.environ, {"STUDIO_ENABLE_LEAD_FACTS": "true",
                                         "STUDIO_SALESFORCE_ORG_ID": ORG_ID}, clear=True):
            configured = Settings.from_env()
        self.assertTrue(configured.lead_facts_enabled)
        self.assertEqual(ORG_ID, configured.salesforce_org_id)

    def test_enabled_settings_reject_invalid_org_id_before_app_initialization(self):
        for bad in ("", None, ORG_ID[:15], ORG_ID + " ", ORG_ID + "\n", "001000000000001AAA"):
            with self.subTest(org_id=bad):
                settings(lead_facts_enabled=False, salesforce_org_id=bad).validate()
                with self.assertRaisesRegex(RuntimeError, "STUDIO_SALESFORCE_ORG_ID"):
                    settings(lead_facts_enabled=True, salesforce_org_id=bad).validate()
        settings(lead_facts_enabled=True, salesforce_org_id=ORG_ID).validate()

    def test_disabled_feature_keeps_original_worker_and_does_not_read_credentials(self):
        delegate = CountingWorker()
        with mock.patch.object(OrgFacts, "from_env", side_effect=AssertionError("must not read config")) as factory:
            app = create_app(settings=settings(), store=MemoryStore(), worker=delegate,
                             clock=lambda: 1000, id_factory=IDs())
            self.assertIs(delegate, app.state.controller.worker)
            state, _ = app.state.controller.create_session()
            app.state.controller.execute(state["session_id"], command(state))
        factory.assert_not_called()
        self.assertEqual(1, delegate.calls)

    def test_unrecognized_utterance_and_answers_delegate_without_reading_org(self):
        delegate = mock.Mock()
        delegate.on_turn.return_value = {"events": [], "problems": []}
        router = LeadFactsWorker(delegate, ORG_ID)
        for trigger in ({"kind": "utterance", "text": "Make the hero blue"},
                        {"kind": "utterance", "text": "How do I convert a Lead?"},
                        {"kind": "answer", "text": "How many leads do we have?"}):
            with mock.patch.object(OrgFacts, "from_env", side_effect=AssertionError("must not read org")):
                self.assertIs(delegate.on_turn.return_value, router.on_turn({}, trigger))
            delegate.on_turn.assert_called_with({}, trigger)

    def test_exact_org_and_fixed_queries_return_counts_without_model(self):
        sf = Salesforce()
        delegate = CountingWorker()
        router = LeadFactsWorker(delegate, ORG_ID)
        with mock.patch.dict(os.environ, ENV, clear=True), mock.patch(
                "workers.org_facts.urllib.request.build_opener", return_value=sf):
            result = router.on_turn({"artifact": {"id": "root"}},
                                    {"kind": "utterance", "text": "How many leads do we have?"})
        self.assertEqual(list(QUERIES.values()), sf.queries())
        self.assertTrue(all(r.get_method() == "GET" for r in sf.requests[1:]))
        self.assertEqual(0, delegate.calls)
        text = result["events"][0]["payload"]["text"]
        for expected in ("Test dev org", "26 leads", "4 came from", "2 of them", "Live count from the Lead object at"):
            self.assertIn(expected, text)
        self.assertNotIn("fake-token", json.dumps(result))
        self.assertEqual(["confirm"], [e["type"] for e in result["events"]])

    def test_different_developer_org_is_refused_before_any_lead_query(self):
        sf = Salesforce("00D000000000002AAA")
        facts = OrgFacts(ENV["Headless_domain"], "fake-id", "fake-secret", sf.open, expected_org_id=ORG_ID)
        with self.assertRaises(PermissionError):
            facts.lead_counts()
        self.assertEqual([QUERIES["org"]], sf.queries())

    def test_missing_or_noncanonical_expected_id_never_contacts_salesforce(self):
        sf = Salesforce()
        for bad in ("", None, ORG_ID[:15], ORG_ID + " ", "001000000000001AAA"):
            with self.subTest(org_id=bad), self.assertRaises(ValueError):
                OrgFacts(ENV["Headless_domain"], "fake-id", "fake-secret", sf.open, expected_org_id=bad)
        self.assertEqual([], sf.requests)

    def test_missing_or_malformed_aggregates_are_not_zero(self):
        cases = [("lead_total", {}), ("lead_total", {"totalSize": True}),
                 ("lead_total", {"totalSize": -1}), ("lead_total", {"totalSize": "26"}),
                 ("lead_by_source", {}), ("lead_by_source", {"records": [], "done": False}),
                 ("lead_by_source", {"records": []}),
                 ("lead_by_source", {"records": [], "done": 0}),
                 ("lead_by_source", {"records": [], "done": 1}),
                 ("lead_by_source", {"records": [{"LeadSource": "sfdc24.com"}], "done": True}),
                 ("lead_by_source", {"records": [{"LeadSource": "sfdc24.com", "n": 4},
                                                  {"LeadSource": "sfdc24.com", "n": 3}], "done": True}),
                 ("lead_by_source", {"records": [{"LeadSource": None, "n": 4},
                                                  {"LeadSource": None, "n": 3}], "done": True}),
                 ("lead_by_source", {"records": [{"LeadSource": None, "n": 4},
                                                  {"LeadSource": "(none)", "n": 3}], "done": True}),
                 ("lead_site_recent", {})]
        for name, response in cases:
            sf = Salesforce()
            sf.overrides[name] = response
            with self.subTest(name=name, response=response), mock.patch.dict(os.environ, ENV, clear=True), mock.patch(
                    "workers.org_facts.urllib.request.build_opener", return_value=sf):
                result = LeadFactsWorker(CountingWorker(), ORG_ID).on_turn(
                    {"artifact": {"id": "root"}}, {"kind": "utterance", "text": "Count leads"})
                self.assertEqual(UNAVAILABLE, result["events"][0]["payload"]["text"])

    def test_config_auth_and_query_failures_never_fall_back_to_model_or_leak_details(self):
        for stage in ("config", "auth", "lead_total", "wrong_org"):
            delegate = CountingWorker()
            sf = Salesforce("00D000000000002AAA" if stage == "wrong_org" else ORG_ID)
            sf.fail_on = stage
            with self.subTest(stage=stage), mock.patch.dict(
                    os.environ, {} if stage == "config" else ENV, clear=True), mock.patch(
                    "workers.org_facts.urllib.request.build_opener", return_value=sf):
                result = LeadFactsWorker(delegate, ORG_ID).on_turn(
                    {"artifact": {"id": "root"}}, {"kind": "utterance", "text": "Count leads"})
            self.assertEqual(0, delegate.calls)
            self.assertEqual(UNAVAILABLE, result["events"][0]["payload"]["text"])
            self.assertNotIn("private", json.dumps(result))
            self.assertEqual({"config": 0, "auth": 1, "lead_total": 3, "wrong_org": 2}[stage], len(sf.requests))

    def test_verified_zero_counts_are_valid(self):
        sf = Salesforce()
        sf.overrides.update({"lead_total": {"totalSize": 0}, "lead_by_source": {"records": [], "done": True},
                             "lead_site_recent": {"totalSize": 0}})
        facts = OrgFacts(ENV["Headless_domain"], "fake-id", "fake-secret", sf.open, expected_org_id=ORG_ID)
        result = facts.lead_counts()
        self.assertEqual((0, 0, 0), (result["total"], result["site_total"], result["site_last_7_days"]))

    def test_committed_fact_and_failure_leave_artifact_unchanged_and_replay_without_query(self):
        for fail in (False, True):
            sf = Salesforce()
            if fail:
                sf.overrides["lead_total"] = {}
            worker = LeadFactsWorker(CountingWorker(), ORG_ID)
            controller, store, _ = make_controller(worker=worker)
            state, _ = controller.create_session()
            before = copy.deepcopy(state)
            cmd = command(state)
            with self.subTest(fail=fail), mock.patch.dict(os.environ, ENV, clear=True), mock.patch(
                    "workers.org_facts.urllib.request.build_opener", return_value=sf):
                first = controller.execute(state["session_id"], cmd)
                request_count = len(sf.requests)
                # A new controller instance uses the durable receipt, not a
                # process-local memo. Item replay also avoids another query.
                resumed, _, _ = make_controller(store=store, worker=worker)
                self.assertEqual(first, resumed.execute(state["session_id"], cmd))
                duplicate = resumed.execute(state["session_id"], dict(cmd, command_id="lead-2"))
                self.assertTrue(duplicate["deduplicated"])
                self.assertEqual(request_count, len(sf.requests))
            saved = controller.repository.load(state["session_id"]).state
            self.assertEqual(before["artifact"], saved["artifact"])
            self.assertEqual(before["artifact_version"], saved["artifact_version"])
            self.assertEqual(before["questions"], saved["questions"])
            facts = [e for e in first["events"] if e["type"] == "confirm"]
            self.assertEqual(1, len(facts))
            self.assertIn(facts[0], saved["events"])
            self.assertEqual("completed", saved["commands"]["lead-1"]["status"])
            if fail:
                self.assertEqual(UNAVAILABLE, facts[0]["payload"]["text"])

    def test_item_replay_after_101_distinct_items_and_restart_does_not_query_again(self):
        sf = Salesforce()
        controller, store, _ = make_controller(
            worker=LeadFactsWorker(CountingWorker(), ORG_ID), max_commands=500)
        state, _ = controller.create_session()
        with mock.patch.dict(os.environ, ENV, clear=True), mock.patch(
                "workers.org_facts.urllib.request.build_opener", return_value=sf):
            for i in range(101):
                controller.execute(state["session_id"], command(state, "lead-%d" % i, "item-%d" % i))
            self.assertEqual(505, len(sf.requests))
            resumed, _, _ = make_controller(
                store=store, worker=LeadFactsWorker(CountingWorker(), ORG_ID), max_commands=500)
            duplicate = resumed.execute(state["session_id"], command(state, "lead-repeat", "item-0"))
            self.assertTrue(duplicate["deduplicated"])
            self.assertEqual([], duplicate["events"])
            self.assertEqual(505, len(sf.requests))
        saved = resumed.repository.load(state["session_id"]).state
        self.assertEqual(101, len(saved["voice_item_ids"]))
        self.assertEqual(state["artifact_version"], saved["artifact_version"])

    def test_authenticated_http_and_sse_path_commits_fact_and_refuses_anonymous(self):
        sf = Salesforce()
        sender = EmailSender()
        delegate = CountingWorker()
        app = create_app(settings=settings(lead_facts_enabled=True, salesforce_org_id=ORG_ID),
                         store=MemoryStore(), worker=delegate, clock=lambda: 1000,
                         id_factory=IDs(), email_sender=sender)
        origin = {"Origin": "https://www.sfdc24.com"}
        with TestClient(app) as client, mock.patch.dict(os.environ, ENV, clear=True), mock.patch(
                "workers.org_facts.urllib.request.build_opener", return_value=sf):
            started = client.post("/v1/auth/start", headers=origin, json={
                "email": "operator@example.com", "client_key": "browser-test-1234567890"})
            self.assertEqual(200, started.status_code)
            verified = client.post("/v1/auth/verify", headers=origin, json={
                "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
                "code": sender.calls[-1][1], "client_key": "browser-test-1234567890"})
            self.assertEqual(200, verified.status_code)
            created = client.post("/v1/session", headers={**origin, "Authorization": "Bearer " + verified.json()["token"]},
                                  json={"creation_id": "create-leads", "title": "Lead facts"})
            self.assertEqual(200, created.status_code)
            session = created.json()
            state = app.state.controller.repository.load(session["session_id"]).state
            endpoint = "/v1/session/%s/commands" % session["session_id"]
            cmd = command(state)
            self.assertEqual(401, client.post(endpoint, headers=origin, json=cmd).status_code)
            self.assertEqual([], sf.requests)
            headers = {**origin, "Authorization": "Bearer " + session["token"]}
            result = client.post(endpoint, headers=headers, json=cmd)
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual(result.json(), client.post(endpoint, headers=headers, json=cmd).json())
            streamed = client.get(session["events_url"], headers=headers, params={"once": "true"})
            self.assertEqual(200, streamed.status_code)
            self.assertIn("event: confirm", streamed.text)
            self.assertIn("Test dev org (Developer Edition) has 26 leads", streamed.text)
            self.assertEqual(5, len(sf.requests))
            self.assertEqual(0, delegate.calls)


if __name__ == "__main__":
    unittest.main()

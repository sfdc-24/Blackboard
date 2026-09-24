"""Offline metadata proposal/confirmation contract; no Salesforce capability."""
import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "cloud" / "studio-controller"))

from app import metadata_contract as contract
from app.core import CommandError, StudioController
from app.state import StudioRepository
from app.main import create_app
from app.settings import Settings
from app.tokens import mint_token
from fastapi.testclient import TestClient
from test_studio_controller import CountingWorker, IDs, MemoryStore, settings

ORG = "00D000000000001AAA"
FIELD = {"parent": "Lead", "name": "Prototype_Interest", "label": "Prototype Interest",
         "type": "Text", "length": 80, "required": False, "unique": False, "external_id": False}


def make_controller(*, store=None, worker=None, clock=lambda: 1000, **kwargs):
    store = store or MemoryStore()
    worker = worker or CountingWorker()
    return StudioController(StudioRepository(store, clock=clock), worker,
                            clock=clock, id_factory=IDs(), **kwargs), store, worker


def command(state, kind="metadata.propose", command_id="metadata-1", **values):
    return {"session_id": state["session_id"], "command_id": command_id, "type": kind,
            "expected_version": state["artifact_version"], **values}


def confirmation(plan):
    return {key: plan[key] for key in contract.CONFIRM_KEYS}


class MetadataContractTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.worker = CountingWorker()
        self.controller, self.store, _ = make_controller(
            worker=self.worker, clock=lambda: self.now,
            metadata_proposals_enabled=True, metadata_org_id=ORG)
        self.state, _ = self.controller.create_session(subject="operator-hash")
        # Fail the suite if this pure contract slice starts acquiring a provider.
        for target in ("urllib.request.urlopen", "subprocess.Popen", "workers.org_facts.OrgFacts.from_env"):
            patcher = mock.patch(target, side_effect=AssertionError("no provider permitted"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, **kwargs):
        return self.controller.execute(self.state["session_id"], command(self.state, **kwargs))

    def propose(self):
        result = self.execute(field=FIELD)
        return result, result["metadata_proposal"]

    def test_settings_default_off_and_enabled_requires_exact_org(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Settings.from_env().metadata_proposals_enabled)
        with mock.patch.dict(os.environ, {"STUDIO_ENABLE_METADATA_PROPOSALS": "true",
                                          "STUDIO_SALESFORCE_ORG_ID": ORG}, clear=True):
            self.assertTrue(Settings.from_env().metadata_proposals_enabled)
        for bad in ("", "00D000000000001", ORG + " ", None, "001000000000001AAA"):
            settings(metadata_proposals_enabled=False, salesforce_org_id=bad).validate()
            with self.assertRaisesRegex(RuntimeError, "STUDIO_SALESFORCE_ORG_ID"):
                settings(metadata_proposals_enabled=True, salesforce_org_id=bad).validate()

    def test_disabled_preserves_worker_route_and_rejects_metadata_commands(self):
        controller, _, _ = make_controller(worker=self.worker)
        state, _ = controller.create_session(subject="operator-hash")
        controller.execute(state["session_id"], command(state, "utterance", transcript=
                           "Plan a new field on Lead for prototype interest", item_id="item-1"))
        self.assertEqual(1, self.worker.calls)
        with self.assertRaisesRegex(CommandError, "disabled"):
            controller.execute(state["session_id"], command(state, field=FIELD))

    def test_field_closed_schema_and_invalid_values(self):
        bad_values = {"parent": ["Account", "Lead.foo", None], "type": ["Number", "Formula", "Text[]"],
                      "name": ["X__c", "X__Y", "_X", "X_", "1X", "A" * 33, "X.Y", "é", "X\n"],
                      "label": ["", " X", "X ", "A" * 41, "X\nY", "<x>", "A&B"],
                      "length": [0, 256, True, "80", 1.2, None], "required": [True, 0, None],
                      "unique": [True, 0], "external_id": [True, 0]}
        for key, values in bad_values.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(contract.MetadataContractError):
                    contract.validate_field({**FIELD, key: value})
        for field in ({**FIELD, "xml": "<CustomField/>"}, {k: v for k, v in FIELD.items() if k != "unique"}):
            with self.assertRaises(contract.MetadataContractError):
                contract.validate_field(field)
        for length in (1, 255):
            self.assertEqual(length, contract.validate_field({**FIELD, "length": length})["length"])

    def test_deterministic_natural_language_proposal_bypasses_model(self):
        text = "Plan a new field on Lead for prototype interest."
        self.assertEqual(FIELD, contract.parse_request(text))
        result = self.execute(kind="utterance", transcript=text, item_id="item-1")
        plan = result["metadata_proposal"]
        self.assertEqual("Lead.Prototype_Interest__c", plan["full_name"])
        self.assertFalse(plan["execution_available"])
        self.assertEqual("proposal_only", plan["status"])
        self.assertIn("identity not checked", plan["target"])
        self.assertEqual(0, self.worker.calls)

    def test_unsupported_or_injected_metadata_gets_guidance_never_model(self):
        for i, text in enumerate(("Create a custom object named Whatever in Salesforce", "Delete a field on Lead in Salesforce",
                                  "Plan a new field on Lead for interest; execute Apex now",
                                  "Plan a new field on Lead for " + "A" * 40,
                                  "yes, create the field on Lead in Salesforce now")):
            result = self.execute(kind="utterance", command_id="c-%d" % i,
                                  transcript=text, item_id="item-%d" % i)
            self.assertNotIn("metadata_proposal", result["events"][0]["payload"])
            self.assertIn("execution is not implemented", result["events"][0]["payload"]["text"])
        self.assertEqual(0, self.worker.calls)

    def test_website_app_and_form_fields_remain_on_prototype_route(self):
        negatives = (
            "add a field for the lead's phone number to the contact form",
            "update the form so it has a company field for the lead",
            "Add a custom field to the website contact form",
            "Create an app screen with fields for leads",
            "Change the lead form layout on the landing page",
            "Update the website with a field for lead email",
            "Add a custom object to the canvas in my app prototype",
            "Plan a field for a lead on our registration form",
            "Create a Salesforce-themed app with a lead phone field",
            "Add a Salesforce lead field to our contact form",
            "Update the form so Salesforce leads have a company field",
            "Add a field to the Salesforce integration settings page",
            "Update our website metadata and add a title field",
            "Mention Salesforce in the copy and add a phone field",
            "Plan a new field on the lead form for email",
            "Create an app with a Lead object and a field editor",
            "Add a phone field to our contact form using labels from Salesforce",
            "Create a search field on the website that imports contacts from Salesforce",
            "Update the field labels on our website to match the labels in Salesforce",
            "Add a phone field to our contact form linked to the Lead object",
        )
        for i, text in enumerate(negatives):
            with self.subTest(text=text):
                self.assertFalse(contract.is_metadata_request(text))
                cmd = command(self.state, "utterance", "prototype-%d" % i,
                              transcript=text, item_id="prototype-item-%d" % i)
                cmd["expected_version"] = self.controller.repository.load(self.state["session_id"]).state["artifact_version"]
                result = self.controller.execute(self.state["session_id"], cmd)
                self.assertNotIn("metadata_proposal", result)
                self.assertEqual(i + 1, self.worker.calls)
        saved = self.controller.repository.load(self.state["session_id"]).state
        self.assertNotIn("metadata_proposal", saved)
        for text in negatives:
            self.assertIn(text, [item.get("text") for item in saved["transcript"]])

    def test_explicit_salesforce_metadata_stays_local_including_unsupported_deletes(self):
        positives = (
            "Delete the Phone field on the Lead object",
            "delete the Phone field on the Lead object in Salesforce",
            "Create a custom field in Salesforce",
            "Create a field within our Salesforce org",
            "Create a Salesforce custom object named Project",
            "Update Salesforce metadata for the Lead object",
            "Add a Text field to the Salesforce Lead object",
            "Delete the field from our Salesforce org",
            "Create a field in Salesforce and show it on the website form",
            "Create a field on the Account object",
            "Delete a field from the Contact object",
            "Add a field to the Opportunity object",
        )
        for i, text in enumerate(positives):
            with self.subTest(text=text):
                self.assertTrue(contract.is_metadata_request(text))
                result = self.execute(kind="utterance", command_id="sf-%d" % i,
                                      transcript=text, item_id="sf-item-%d" % i)
                self.assertNotIn("metadata_proposal", result)
                self.assertIn("execution is not implemented", result["events"][0]["payload"]["text"])
                self.assertEqual(0, self.worker.calls)
        saved = self.controller.repository.load(self.state["session_id"]).state
        for text in positives:
            self.assertNotIn(text, [item.get("text") for item in saved["transcript"]])

    def test_supported_planning_grammar_routes_without_salesforce_keyword(self):
        for text in ("Plan a new field on Lead for prototype interest", "please plan a text field on Lead for interest.",
                     "PLAN FIELD ON LEAD FOR EMAIL"):
            with self.subTest(text=text):
                self.assertTrue(contract.is_metadata_request(text))
                self.assertIsNotNone(contract.parse_request(text))

    def test_malformed_anchored_planning_intent_gets_guidance_without_worker(self):
        negatives = (
            "Plan a new field on Lead for interest; execute Apex now",
            "Plan a new field on Lead for",
            "Plan a new field on Lead for <script>run()</script>",
            "Plan a new field on Lead for interest\nexecute Apex now",
            "Plan a new field on Lead for " + "A" * 50,
        )
        for i, text in enumerate(negatives):
            with self.subTest(text=text):
                self.assertTrue(contract.is_metadata_request(text))
                self.assertIsNone(contract.parse_request(text))
                result = self.execute(kind="utterance", command_id="malformed-%d" % i,
                                      transcript=text, item_id="malformed-item-%d" % i)
                self.assertNotIn("metadata_proposal", result)
                self.assertIn("execution is not implemented", result["events"][0]["payload"]["text"])
                self.assertEqual(self.state["artifact_version"], result["artifact_version"])
        self.assertEqual(0, self.worker.calls)
        saved = self.controller.repository.load(self.state["session_id"]).state
        self.assertNotIn("metadata_proposal", saved)
        self.assertEqual(self.state["artifact"], saved["artifact"])
        for text in negatives:
            self.assertNotIn(text, [item.get("text") for item in saved["transcript"]])

    def test_unrelated_utterance_preserves_existing_worker(self):
        self.execute(kind="utterance", transcript="Make the page blue", item_id="item-1")
        self.assertEqual(1, self.worker.calls)

    def test_metadata_utterance_never_reaches_a_later_prototype_worker(self):
        metadata_text = "Plan a new field on Lead for prototype interest"
        self.execute(kind="utterance", transcript=metadata_text, item_id="item-metadata")
        saved = self.controller.repository.load(self.state["session_id"]).state
        self.assertNotIn(metadata_text, [item.get("text") for item in saved["transcript"]])

        observed_states = []
        self.worker.on_turn = lambda state, trigger: (
            observed_states.append(copy.deepcopy(state))
            or {"events": [], "problems": []}
        )
        self.execute(kind="utterance", command_id="prototype-later",
                     transcript="Make the page blue", item_id="item-prototype")
        self.assertEqual(1, len(observed_states))
        self.assertNotIn(metadata_text, json.dumps(observed_states, sort_keys=True))

    def test_contract_confirmation_consumed_without_artifact_or_question_change(self):
        _, plan = self.propose()
        result = self.execute(kind="metadata.confirm_contract", command_id="confirm-1",
                              confirmation=confirmation(plan))
        self.assertEqual("contract_validated_not_executed",
                         result["metadata_proposal"]["status"])
        self.assertIn("no field has been created", result["events"][0]["payload"]["text"])
        saved = self.controller.repository.load(self.state["session_id"]).state
        for key in ("artifact", "artifact_version", "questions"):
            self.assertEqual(self.state[key], saved[key])
        self.assertEqual(0, self.worker.calls)
        self.assertEqual(result, self.execute(kind="metadata.confirm_contract", command_id="confirm-1",
                                              confirmation=confirmation(plan)))
        with self.assertRaisesRegex(CommandError, "no unconfirmed"):
            self.execute(kind="metadata.confirm_contract", command_id="confirm-2", confirmation=confirmation(plan))

    def test_confirmation_rejects_unknown_fields_voice_and_bool_revision(self):
        _, plan = self.propose()
        for value in ({**confirmation(plan), "execute": True}, {**confirmation(plan), "plan_revision": True},
                      {**confirmation(plan), "plan_hash": "x"}):
            with self.assertRaises(CommandError):
                self.execute(kind="metadata.confirm_contract", confirmation=value)
        with self.assertRaisesRegex(CommandError, "exactly"):
            self.execute(kind="metadata.confirm_contract", confirmation=confirmation(plan), answer_source="voice")

    def test_modified_hash_nonce_revision_or_plan_id_rejected(self):
        _, plan = self.propose()
        for key, value in (("plan_hash", "f" * 64), ("confirmation_nonce", "other"),
                           ("plan_revision", 2), ("plan_id", "other")):
            with self.assertRaisesRegex(CommandError, "exact current"):
                self.execute(kind="metadata.confirm_contract", command_id="bad-" + key,
                             confirmation={**confirmation(plan), key: value})

    def test_new_proposal_supersedes_old_confirmation(self):
        _, old = self.propose()
        new = self.execute(command_id="proposal-2", field={**FIELD, "length": 100})
        self.assertEqual(2, new["metadata_proposal"]["plan_revision"])
        with self.assertRaisesRegex(CommandError, "exact current"):
            self.execute(kind="metadata.confirm_contract", command_id="confirm-old", confirmation=confirmation(old))

    def test_confirmation_binding_and_expiry_fail_closed(self):
        _, public = self.propose()
        saved = self.controller.repository.load(self.state["session_id"]).state["metadata_proposal"]
        args = dict(session_id=self.state["session_id"], subject="operator-hash", org_id=ORG, now=1000)
        for key, value in (("session_id", "other"), ("subject", "other"), ("org_id", "00D000000000002AAA"),
                           ("now", 1600)):
            with self.assertRaises(contract.MetadataContractError):
                contract.check_confirmation(saved, confirmation(public), **{**args, key: value})
        tampered = copy.deepcopy(saved)
        tampered["binding"]["field"]["length"] = 120
        with self.assertRaises(contract.MetadataContractError):
            contract.check_confirmation(tampered, confirmation(public), **args)

    def test_confirmation_expiring_after_reservation_is_a_closed_client_error(self):
        _, public = self.propose()
        with mock.patch.object(
                contract, "check_confirmation",
                side_effect=[None, contract.MetadataContractError("proposal has expired")]):
            with self.assertRaisesRegex(CommandError, "proposal has expired") as caught:
                self.execute(kind="metadata.confirm_contract", command_id="confirm-expiry-race",
                             confirmation=confirmation(public))
        self.assertEqual(400, caught.exception.status)
        receipt = self.controller.repository.load(self.state["session_id"]).state["commands"][
            "confirm-expiry-race"]
        self.assertEqual("failed", receipt["status"])
        self.assertNotIn("metadata_proposal", receipt)

    def test_restart_replay_and_item_dedup_do_not_regenerate_plan(self):
        cmd = command(self.state, "utterance", transcript="Plan a new field on Lead for prototype interest", item_id="item-1")
        first = self.controller.execute(self.state["session_id"], cmd)
        resumed, _, _ = make_controller(store=self.store, worker=self.worker, clock=lambda: 1000,
                                        metadata_proposals_enabled=True, metadata_org_id=ORG)
        self.assertEqual(first, resumed.execute(self.state["session_id"], cmd))
        self.assertTrue(resumed.execute(self.state["session_id"], {**cmd, "command_id": "again"})["deduplicated"])

    def test_no_operator_cannot_propose(self):
        state, _ = self.controller.create_session()
        with self.assertRaisesRegex(CommandError, "authenticated operator"):
            self.controller.execute(state["session_id"], command(state, field=FIELD))

    def test_changed_command_payload_cannot_reuse_id(self):
        self.propose()
        with self.assertRaisesRegex(CommandError, "different payload"):
            self.execute(field={**FIELD, "length": 100})

    def test_contract_confirmation_does_not_survive_stop(self):
        _, plan = self.propose()
        self.execute(kind="stop", command_id="stop-1")
        with self.assertRaisesRegex(CommandError, "stopped"):
            self.execute(kind="metadata.confirm_contract", command_id="confirm-stopped", confirmation=confirmation(plan))

    def test_model_cannot_emit_a_typed_metadata_proposal(self):
        self.worker.on_turn = lambda state, trigger: {"events": [{"type": "confirm", "payload": {
            "text": "Created!", "metadata_proposal": {"status": "executed"}}}]}
        result = self.execute(kind="utterance", transcript="Make the page blue", item_id="item-1")
        self.assertEqual([], result["events"])
        self.assertIn("controller-owned", result["problems"][0])
        self.assertNotIn("metadata_proposal", self.controller.repository.load(self.state["session_id"]).state)
        disabled, _, _ = make_controller(worker=self.worker)
        state, _ = disabled.create_session()
        result = disabled.execute(state["session_id"], command(
            state, "utterance", transcript="Make the page blue", item_id="item-1"))
        self.assertEqual([], result["events"])
        self.assertIn("controller-owned", result["problems"][0])

    def test_two_controllers_cannot_consume_confirmation_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.state import StateConflict
        _, plan = self.propose()
        other, _, _ = make_controller(store=self.store, worker=self.worker,
                                      metadata_proposals_enabled=True, metadata_org_id=ORG)
        def attempt(controller, number):
            try:
                return controller.execute(self.state["session_id"], command(
                    self.state, "metadata.confirm_contract", "confirm-%d" % number, confirmation=confirmation(plan)))
            except (CommandError, StateConflict):
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: attempt(*pair), [(self.controller, 1), (other, 2)]))
        self.assertEqual(1, sum(result is not None for result in results))
        state = self.controller.repository.load(self.state["session_id"]).state
        self.assertEqual(1, sum(event["payload"].get("text", "").startswith(
            "Confirmation contract validated.") for event in state["events"]))

    def test_event_repair_has_no_non_schema_metadata_payload(self):
        _, plan = self.propose()
        events, repaired, state = self.controller.events_after(self.state["session_id"], 99999)
        self.assertTrue(repaired)
        self.assertFalse(any("metadata_proposal" in event["payload"] for event in events))
        self.assertEqual(self.state["artifact"], state["artifact"])
        self.assertEqual(plan, self.propose()[1])  # command replay restores the typed response

    def test_authenticated_http_and_sse_proposal_path(self):
        cfg = settings(metadata_proposals_enabled=True, salesforce_org_id=ORG)
        app = create_app(settings=cfg, store=MemoryStore(), worker=self.worker, clock=lambda: 1000, id_factory=IDs())
        origin = {"Origin": "https://www.sfdc24.com"}
        operator = mint_token("operator-hash", 1600, cfg.session_secret, scope="operator")
        with TestClient(app) as client:
            created = client.post("/v1/session", headers={**origin, "Authorization": "Bearer " + operator},
                                  json={"creation_id": "create-metadata"})
            self.assertEqual(200, created.status_code, created.text)
            session = created.json()
            state = app.state.controller.repository.load(session["session_id"]).state
            endpoint = "/v1/session/%s/commands" % session["session_id"]
            cmd = command(state, field=FIELD)
            self.assertEqual(401, client.post(endpoint, headers=origin, json=cmd).status_code)
            headers = {**origin, "Authorization": "Bearer " + session["token"]}
            result = client.post(endpoint, headers=headers, json=cmd)
            self.assertEqual(200, result.status_code, result.text)
            public = result.json()["metadata_proposal"]
            confirmed = client.post(endpoint, headers=headers, json=command(
                state, "metadata.confirm_contract", "confirm-http", confirmation=confirmation(public)))
            self.assertEqual(200, confirmed.status_code, confirmed.text)
            self.assertIn("not_executed", confirmed.text)
            streamed = client.get(session["events_url"], headers=headers, params={"once": "true"})
            self.assertNotIn("metadata_proposal", streamed.text)
            self.assertIn("execution is not implemented", streamed.text)

    def test_confirm_payload_matches_closed_stage_a_schema(self):
        result, plan = self.propose()
        confirmed = self.execute(kind="metadata.confirm_contract", command_id="confirm-shape",
                                 confirmation=confirmation(plan))
        for event in result["events"] + confirmed["events"]:
            self.assertEqual("confirm", event["type"])
            self.assertEqual({"text", "artifact_ids"}, set(event["payload"]))
            self.assertIsInstance(event["payload"]["text"], str)
            self.assertLessEqual(len(event["payload"]["text"]), 600)
            self.assertIsInstance(event["payload"]["artifact_ids"], list)
        self.assertNotIn("target_fingerprint", plan)
        self.assertNotIn(ORG, str(result))

    @unittest.skipUnless(os.environ.get("STUDIO_SITE_EVENT_SCHEMA"), "optional read-only site schema compatibility check")
    def test_against_external_site_confirm_schema(self):
        # No new runtime dependency or hard-coded sibling checkout. Assert the
        # exact closed envelope/confirm constraints used by this increment.
        schema = json.loads(Path(os.environ["STUDIO_SITE_EVENT_SCHEMA"]).read_text(encoding="utf-8"))
        definitions = schema["$defs"]
        payload_schema = definitions["payloads"]["confirm"]
        self.assertIs(False, payload_schema["additionalProperties"])
        self.assertEqual({"text", "artifact_ids"}, set(payload_schema["properties"]))
        result, _ = self.propose()
        for event in result["events"]:
            self.assertEqual(set(definitions["envelope"]["properties"]), set(event))
            self.assertEqual(set(payload_schema["required"]), set(event["payload"]))
            self.assertLessEqual(len(event["payload"]["text"]), definitions["text"]["maxLength"])
            self.assertGreaterEqual(len(event["payload"]["artifact_ids"]),
                                    payload_schema["properties"]["artifact_ids"]["minItems"])
            for item in event["payload"]["artifact_ids"]:
                self.assertRegex(item, definitions["id"]["pattern"])


if __name__ == "__main__":
    unittest.main()

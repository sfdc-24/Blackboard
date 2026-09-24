"""Offline ledger contract. No provider, credentials or controller imported."""
import copy
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud" / "studio-controller"))
from app.metadata_operations import (MetadataOperationLedger, InvalidOperation,
    OperationConflict, PersistenceUncertain, POLICY, RECEIPT_KEYS, validate_ledger)


class Conflict(Exception):
    pass


class Store:
    atomic_generation_cas = True

    def __init__(self):
        self.documents = {}
        self.lock = threading.Lock()
        self.fail = None
        self.race = False

    def load(self, name):
        with self.lock:
            value, token = self.documents.get(name, ({}, None))
            return json.loads(json.dumps(value)), token

    def save(self, name, value, token):
        with self.lock:
            if self.fail == "before":
                raise RuntimeError("SECRET_PROVIDER_ERROR")
            old, current = self.documents.get(name, ({}, None))
            if self.race or current != token:
                raise Conflict("SECRET_CAS_DETAILS")
            self.documents[name] = (json.loads(json.dumps(value)), (current or 0) + 1)
            if self.fail == "after":
                raise RuntimeError("SECRET_PROVIDER_ERROR")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.ledger = MetadataOperationLedger(self.store, conflict_type=Conflict)
        self.actor = {"operator_id": "actor_private", "session_id": "session_private", "org_binding_id": "org_private"}
        self.field = {"parent": "Lead", "name": "Interest", "label": "Interest", "type": "Text", "length": 80,
                      "required": False, "unique": False, "external_id": False}
        self.spec = {"binding": self.actor, "field": self.field, "confirmation_nonce": "nonce_private",
                     "prepared_at": 100, "expires_at": 600}
        self.revision = 0
        self.at = 101
        self.ledger.prepare("op_1", "prepare", self.spec)

    def apply(self, action, payload=None, **overrides):
        command = {"command_id": "cmd_" + str(self.at), "expected_revision": self.revision,
                   "at": self.at, "action": action, "payload": payload or {}}
        command.update(overrides)
        result = self.ledger.apply("op_1", self.actor, command)
        self.revision = result["revision"]
        self.at += 1
        return result, command

    def confirmed(self):
        self.apply("confirm", self.ledger.confirmation_challenge("op_1", self.actor))

    def preflight(self, exists=False):
        return {"org_binding_id": self.actor["org_binding_id"], "member": "CustomField:Lead.interest__c",
                "exists": exists, "observed_at": self.at}

    def ready(self):
        self.confirmed()
        self.apply("preflight", {"observation": self.preflight()})

    def executing(self):
        self.ready()
        return self.apply("begin")

    def accepted(self):
        self.executing()
        self.apply("dispatch_result", {"outcome": "accepted", "definitive_no_change": False})

    def observation(self):
        return {"org_binding_id": self.actor["org_binding_id"], "field": copy.deepcopy(self.field), "observed_at": self.at}

    def document(self):
        return next(iter(self.store.documents.values()))[0]

    def test_closed_schema_and_json_restart(self):
        validate_ledger(self.document())
        restored = Store()
        restored.documents = json.loads(json.dumps(self.store.documents))
        reader = MetadataOperationLedger(restored, conflict_type=Conflict)
        self.assertEqual(reader.receipt("op_1", self.actor)["status"], "prepared")
        for mutate in (lambda x: x.update(extra=True), lambda x: x["targets"].clear(),
                       lambda x: x["operations"]["op_1"]["plan"]["field"].update(length=999),
                       lambda x: x["operations"]["op_1"].update(provider_attempts=True)):
            bad = copy.deepcopy(self.document())
            mutate(bad)
            with self.assertRaises(InvalidOperation):
                validate_ledger(bad)

    def test_requires_atomic_store_contract(self):
        with self.assertRaises(InvalidOperation):
            MetadataOperationLedger(object(), conflict_type=Conflict)

    def test_lifecycle_timestamp_drift_rejected_against_matching_audit(self):
        self.accepted()
        for key, drifted in (("confirmed_at", 100), ("attempt_reserved_at", 102), ("dispatch_accepted_at", 103)):
            with self.subTest(key=key):
                bad = copy.deepcopy(self.document())
                bad["operations"]["op_1"][key] = drifted
                with self.assertRaisesRegex(InvalidOperation, "committed transition"):
                    validate_ledger(bad)

    def test_missing_or_fabricated_confirmation_after_abort_rejected(self):
        self.confirmed()
        self.apply("abort", {"reason": "cancelled_before_dispatch"})
        validate_ledger(self.document())
        for stamp in (None, 100, 102):
            bad = copy.deepcopy(self.document())
            bad["operations"]["op_1"]["confirmed_at"] = stamp
            with self.assertRaisesRegex(InvalidOperation, "committed transition"):
                validate_ledger(bad)
        self.setUp()
        self.apply("abort", {"reason": "cancelled_before_dispatch"})
        bad = copy.deepcopy(self.document())
        bad["operations"]["op_1"]["confirmed_at"] = 100
        with self.assertRaisesRegex(InvalidOperation, "committed transition"):
            validate_ledger(bad)

    def test_unknown_requires_ack_presence_to_match_audited_path(self):
        self.accepted()
        self.apply("unknown", {"reason": "interrupted"})
        bad = copy.deepcopy(self.document())
        bad["operations"]["op_1"]["dispatch_accepted_at"] = None
        with self.assertRaisesRegex(InvalidOperation, "committed transition"):
            validate_ledger(bad)
        self.setUp()
        self.executing()
        self.apply("unknown", {"reason": "interrupted"})
        bad = copy.deepcopy(self.document())
        bad["operations"]["op_1"]["dispatch_accepted_at"] = 103
        with self.assertRaisesRegex(InvalidOperation, "committed transition"):
            validate_ledger(bad)

    def test_unknown_reason_cannot_fabricate_verification_or_ambiguous_ack(self):
        for accepted, reason in ((False, "verification_mismatch"), (True, "ambiguous_dispatch")):
            with self.subTest(accepted=accepted, reason=reason):
                self.setUp()
                self.accepted() if accepted else self.executing()
                self.apply("unknown", {"reason": "interrupted"})
                bad = copy.deepcopy(self.document())
                bad["operations"]["op_1"]["reason"] = reason
                with self.assertRaisesRegex(InvalidOperation, "acknowledgement history"):
                    validate_ledger(bad)

    def test_preflight_observation_cannot_postdate_its_commit(self):
        self.accepted()
        bad = copy.deepcopy(self.document())
        bad["operations"]["op_1"]["preflight"]["observed_at"] = 103
        with self.assertRaises(InvalidOperation): validate_ledger(bad)
        # Missing evidence for the confirmed->confirmed transition also fails,
        # even before a provider attempt can make it otherwise mandatory.
        self.setUp()
        self.ready()
        bad = copy.deepcopy(self.document())
        bad["operations"]["op_1"]["preflight"] = None
        with self.assertRaisesRegex(InvalidOperation, "preflight evidence is missing"):
            validate_ledger(bad)

    def test_fabricated_preflight_and_erased_provider_attempt_are_rejected(self):
        self.confirmed()
        self.apply("abort", {"reason": "cancelled_before_dispatch"})
        bad = copy.deepcopy(self.document())
        bad["operations"]["op_1"]["preflight"] = {**self.preflight(), "observed_at": 101}
        with self.assertRaisesRegex(InvalidOperation, "matching committed transition"):
            validate_ledger(bad)
        self.setUp()
        self.executing()
        self.apply("dispatch_result", {"outcome": "rejected_no_change", "definitive_no_change": True})
        bad = copy.deepcopy(self.document())
        bad["operations"]["op_1"].update(provider_attempts=0, attempt_reserved_at=None,
                                           reason="precondition_rejected")
        with self.assertRaisesRegex(InvalidOperation, "committed transition"):
            validate_ledger(bad)

    def test_existing_preflight_is_bound_to_failed_commit(self):
        self.confirmed()
        self.apply("preflight", {"observation": self.preflight(True)})
        validate_ledger(self.document())
        bad = copy.deepcopy(self.document())
        bad["operations"]["op_1"]["preflight"]["observed_at"] += 1
        with self.assertRaises(InvalidOperation): validate_ledger(bad)

    def test_ledger_revision_matches_all_retained_operations(self):
        self.apply("abort", {"reason": "cancelled_before_dispatch"})
        self.ledger.prepare("op_2", "prepare2", self.spec)
        validate_ledger(self.document())
        self.assertEqual(self.document()["revision"], 3)
        for revision in (0, 2, 4):
            bad = copy.deepcopy(self.document())
            bad["revision"] = revision
            with self.assertRaisesRegex(InvalidOperation, "retained committed history"):
                validate_ledger(bad)

    def test_corrupted_ack_cannot_verify_before_actual_audited_acceptance(self):
        self.accepted()
        record = self.document()["operations"]["op_1"]
        self.assertEqual(record["dispatch_accepted_at"], 104)
        record["dispatch_accepted_at"] = 103
        observation = self.observation()
        observation["observed_at"] = 103
        with patch.object(self.store, "save", wraps=self.store.save) as save:
            with self.assertRaisesRegex(InvalidOperation, "committed transition"):
                self.apply("verify", {"observation": observation})
            save.assert_not_called()
        self.assertEqual(record["state"], "verify_pending")

    def test_strict_field_binding_and_expanded_inputs_rejected(self):
        for delta in ({"parent": "Account"}, {"name": "Interest__c"}, {"length": True},
                      {"type": "LongTextArea"}, {"required": True}, {"label": "<bad>"}):
            specification = copy.deepcopy(self.spec)
            specification["field"].update(delta)
            with self.assertRaises(InvalidOperation): self.ledger.prepare("other", "p", specification)
        for delta in ({"policy": "anything"}, {"prepared_at": float("nan")},
                      {"confirmation_nonce": "secret@email.test"}, {"expires_at": 10000}):
            with self.assertRaises(InvalidOperation): self.ledger.prepare("other", "p", {**self.spec, **delta})

    def test_capacity_and_oversize_fail_closed_without_pruning(self):
        with patch("app.metadata_operations.MAX_OPERATIONS", 1):
            other = copy.deepcopy(self.spec)
            other["field"]["name"] = "Other"
            with self.assertRaises(OperationConflict): self.ledger.prepare("other", "p", other)
        oversized = copy.deepcopy(self.document())
        oversized["extra"] = "x" * 262144
        with self.assertRaises(InvalidOperation): validate_ledger(oversized)
        self.assertEqual(len(self.document()["targets"]), 1)

    def test_prepare_replay_and_changed_payload(self):
        self.assertTrue(self.ledger.prepare("op_1", "prepare", self.spec)["replayed"])
        changed = copy.deepcopy(self.spec)
        changed["field"]["length"] = 20
        with self.assertRaises(OperationConflict):
            self.ledger.prepare("op_1", "prepare", changed)

    def test_different_id_same_target_case_and_expiry_cannot_bypass(self):
        for name in ("Interest", "interest", "INTEREST"):
            spec = copy.deepcopy(self.spec)
            spec["field"]["name"] = name
            spec.update(prepared_at=1000, expires_at=1500)
            with self.assertRaises(OperationConflict):
                self.ledger.prepare("op_2", "prepare2", spec)

    def test_proposal_confirmation_never_authorizes(self):
        for payload in ({"plan_id": "op_1", "plan_revision": 0, "plan_hash": "a" * 64, "confirmation_nonce": "nonce_private"},
                        {**self.ledger.confirmation_challenge("op_1", self.actor), "execution_policy": "lead-text-proposal-only-v1"},
                        {**self.ledger.confirmation_challenge("op_1", self.actor), "confirmation_nonce": "wrong"}):
            with self.assertRaises(InvalidOperation):
                self.apply("confirm", payload)
        self.assertEqual(self.document()["operations"]["op_1"]["provider_attempts"], 0)

    def test_confirmation_expiry_and_absent_preflight_required(self):
        with self.assertRaises(InvalidOperation):
            self.apply("confirm", self.ledger.confirmation_challenge("op_1", self.actor), at=600)
        self.confirmed()
        with self.assertRaises(OperationConflict):
            self.apply("begin")

    def test_preexisting_matching_target_is_quarantined_not_success(self):
        self.confirmed()
        result, _ = self.apply("preflight", {"observation": self.preflight(True)})
        self.assertEqual(result["receipt"]["status"], "failed")
        self.assertEqual(self.document()["operations"]["op_1"]["provider_attempts"], 0)
        with self.assertRaises(OperationConflict):
            self.ledger.prepare("op_2", "prepare2", self.spec)

    def test_one_attempt_replay_restart_backup_never_regrants_dispatch(self):
        result, command = self.executing()
        self.assertTrue(result["dispatch_allowed"])
        restored = Store()
        restored.documents = json.loads(json.dumps(self.store.documents))
        restarted = MetadataOperationLedger(restored, conflict_type=Conflict)
        replay = restarted.apply("op_1", self.actor, command)
        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["dispatch_allowed"])
        with self.assertRaises(OperationConflict):
            self.apply("begin")
        with self.assertRaises(OperationConflict):
            restarted.prepare("op_2", "other", self.spec)

    def test_exact_readback_required_and_receipt_is_allowlist_only(self):
        self.accepted()
        self.assertEqual(self.ledger.receipt("op_1", self.actor)["status"], "verify_pending")
        result, _ = self.apply("verify", {"observation": self.observation()})
        receipt = result["receipt"]
        self.assertEqual(set(receipt), RECEIPT_KEYS)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["next_action"], "verified_present_no_causality_claim")
        for secret in (*self.actor.values(), "nonce_private", "Interest", "execution_plan_hash"):
            self.assertNotIn(secret, json.dumps(receipt))
        with self.assertRaises(OperationConflict):
            self.ledger.prepare("op_2", "other", self.spec)

    def test_mismatched_or_malformed_verification_is_unknown(self):
        for variation in ("field", "org", "timestamp", "before_ack", "extra", "null"):
            with self.subTest(variation=variation):
                self.setUp()
                self.accepted()
                observed = self.observation()
                if variation == "field": observed["field"]["length"] = 81
                if variation == "org": observed["org_binding_id"] = "another"
                if variation == "timestamp": observed["observed_at"] = 0
                if variation == "before_ack": observed["observed_at"] = self.at - 2
                if variation == "extra": observed["raw_response"] = "SECRET"
                if variation == "null": observed = None
                result, _ = self.apply("verify", {"observation": observed})
                self.assertEqual(result["receipt"]["status"], "outcome_unknown")
                self.assertNotIn("SECRET", json.dumps(self.document()))
                with self.assertRaises(OperationConflict):
                    self.ledger.prepare("op_2", "other", self.spec)

    def test_unknown_retains_hold_and_stale_worker_cannot_overwrite(self):
        self.executing()
        self.apply("unknown", {"reason": "interrupted"})
        for action, payload in (("begin", {}), ("dispatch_result", {"outcome": "accepted", "definitive_no_change": False}),
                                ("verify", {"observation": self.observation()})):
            with self.assertRaises(OperationConflict):
                self.apply(action, payload)
        with self.assertRaises(OperationConflict):
            self.ledger.prepare("op_2", "other", self.spec)
        restored = Store()
        restored.documents = json.loads(json.dumps(self.store.documents))
        restarted = MetadataOperationLedger(restored, conflict_type=Conflict)
        expired = {**self.spec, "prepared_at": 2000, "expires_at": 2100}
        with self.assertRaises(OperationConflict): restarted.prepare("op_3", "p3", expired)

    def test_each_unknown_reason_and_terminal_receipt_is_redacted(self):
        for reason in ("interrupted", "dispatch_timeout", "verification_unavailable"):
            self.setUp()
            self.executing()
            result, _ = self.apply("unknown", {"reason": reason})
            self.assertEqual(set(result["receipt"]), RECEIPT_KEYS)
            for private in (*self.actor.values(), "nonce_private", reason):
                self.assertNotIn(private, json.dumps(result["receipt"]))

    def test_preflight_mismatched_org_member_and_timestamp_are_rejected(self):
        self.confirmed()
        for delta in ({"org_binding_id": "wrong"}, {"member": "CustomField:Lead.other__c"},
                      {"exists": 0}, {"observed_at": 0}):
            with self.assertRaises(InvalidOperation):
                self.apply("preflight", {"observation": {**self.preflight(), **delta}})

    def test_ambiguous_dispatch_is_not_a_retryable_failure(self):
        self.executing()
        result, _ = self.apply("dispatch_result", {"outcome": "ambiguous", "definitive_no_change": False})
        self.assertEqual(result["receipt"]["status"], "outcome_unknown")
        with self.assertRaises(OperationConflict): self.ledger.prepare("op_2", "other", self.spec)

    def test_cancel_only_before_attempt_and_release_is_atomic(self):
        self.apply("abort", {"reason": "cancelled_before_dispatch"})
        self.ledger.prepare("op_2", "other", self.spec)
        with self.assertRaises(OperationConflict): self.apply("begin")
        self.setUp()
        self.executing()
        with self.assertRaises(OperationConflict): self.apply("abort", {"reason": "cancelled_before_dispatch"})

    def test_definitive_rejection_requires_explicit_evidence_classification(self):
        self.executing()
        with self.assertRaises(InvalidOperation):
            self.apply("dispatch_result", {"outcome": "rejected_no_change", "definitive_no_change": False})
        for outcome in ("already_exists", "duplicate", "server_error"):
            with self.assertRaises(InvalidOperation):
                self.apply("dispatch_result", {"outcome": outcome, "definitive_no_change": False})
        self.apply("dispatch_result", {"outcome": "rejected_no_change", "definitive_no_change": True})
        self.ledger.prepare("op_2", "other", self.spec)
        with self.assertRaises(OperationConflict): self.apply("begin")

    def test_stale_revision_and_atomic_cas_race_never_grant_dispatch(self):
        self.ready()
        with self.assertRaises(OperationConflict): self.apply("begin", expected_revision=0)
        self.store.race = True
        with self.assertRaises(OperationConflict): self.apply("begin")
        self.assertEqual(self.document()["operations"]["op_1"]["provider_attempts"], 0)

    def test_ambiguous_save_after_attempt_never_grants_or_repeats(self):
        for mode in ("before", "after"):
            with self.subTest(mode=mode):
                self.setUp()
                self.ready()
                command = {"command_id": "reserve", "expected_revision": self.revision, "at": self.at, "action": "begin", "payload": {}}
                self.store.fail = mode
                with self.assertRaisesRegex(PersistenceUncertain, "no dispatch authorized") as error:
                    self.ledger.apply("op_1", self.actor, command)
                self.assertNotIn("SECRET", str(error.exception))
                self.store.fail = None
                if mode == "after":
                    result = self.ledger.apply("op_1", self.actor, command)
                    self.assertFalse(result["dispatch_allowed"])
                    self.assertTrue(result["replayed"])
                    self.assertEqual(result["receipt"]["status"], "executing")

    def test_cross_tenant_operator_session_receipts_and_confirmation_rejected(self):
        for key in self.actor:
            actor = {**self.actor, key: "unauthorized"}
            with self.assertRaises(InvalidOperation): self.ledger.receipt("op_1", actor)
            with self.assertRaises(InvalidOperation): self.ledger.confirmation_challenge("op_1", actor)

    def test_duplicate_command_changed_payload_rejected(self):
        self.ready()
        _, command = self.apply("begin")
        command["at"] += 1
        with self.assertRaises(OperationConflict): self.ledger.apply("op_1", self.actor, command)

    def test_no_network_subprocess_credentials_or_rollback_actions(self):
        with patch("socket.socket", side_effect=AssertionError("network")), \
             patch("subprocess.Popen", side_effect=AssertionError("process")), \
             patch("os.getenv", side_effect=AssertionError("credentials")):
            self.accepted()
            self.apply("verify", {"observation": self.observation()})
            for action in ("delete", "rollback", "retry", "update", "upsert"):
                with self.assertRaises(InvalidOperation): self.apply(action)

    def test_concurrent_distinct_ids_one_target_only_one_reservation(self):
        store = Store()
        barrier = threading.Barrier(2)
        original_load = store.load
        def simultaneous_load(name):
            result = original_load(name)
            barrier.wait(timeout=5)
            return result
        store.load = simultaneous_load
        outcomes = []
        def run(operation_id):
            try:
                MetadataOperationLedger(store, conflict_type=Conflict).prepare(operation_id, "p", self.spec)
                outcomes.append("prepared")
            except OperationConflict:
                outcomes.append("conflict")
        threads = [threading.Thread(target=run, args=(op,)) for op in ("a", "b")]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertCountEqual(outcomes, ["prepared", "conflict"])
        document = next(iter(store.documents.values()))[0]
        self.assertEqual(len(document["operations"]), 1)
        self.assertEqual(len(document["targets"]), 1)

    def test_hosted_ci_cannot_skip_or_false_green(self):
        workflow = (ROOT / ".github/workflows/python-suites.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(workflow.count('"tests/test_studio_metadata_operations.py"'), 2)
        self.assertIn("tests/test_studio_metadata_operations.py \\", workflow)
        self.assertIn("test -f cloud/studio-controller/app/metadata_operations.py", workflow)
        self.assertIn("test -f tests/test_studio_metadata_operations.py", workflow)
        self.assertIn("-m unittest tests.test_studio_metadata_operations", workflow)
        self.assertIn("&& [ ! -f tests/test_studio_metadata_operations.py ]", workflow)


if __name__ == "__main__":
    unittest.main()

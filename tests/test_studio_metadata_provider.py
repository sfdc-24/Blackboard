"""Adversarial offline contract for the inert Salesforce metadata adapter."""
import ast
from collections import Counter
import copy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import pickle
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud" / "studio-controller"))

import app.metadata_provider as provider
from app.metadata_operations import (
    MetadataOperationLedger,
    PersistenceUncertain,
)
from app.metadata_provider import (
    DispatchPermitError,
    ExecutionBudget,
    MetadataProviderError,
    OrgBinding,
    PREFLIGHT_UNAVAILABLE,
    SalesforceMetadataAdapter,
    VERIFICATION_MISMATCH,
    VERIFICATION_UNAVAILABLE,
)


BINDING_ID = "primary_dev_org_v1"
ORG_ID = "00DabcdefghijklmnO"
ORIGIN = "https://sfdc24.develop.my.salesforce.com"
VERSION = "v62.0"
FIELD = {
    "parent": "Lead", "name": "Interest", "label": "Interest",
    "type": "Text", "length": 80, "required": False,
    "unique": False, "external_id": False,
}
MEMBER = "CustomField:Lead.interest__c"


class Conflict(Exception):
    pass


class Store:
    atomic_generation_cas = True

    def __init__(self):
        self.documents = {}
        self.lock = threading.Lock()
        self.fail_after = False
        self.fail_load_once = False
        self.loads = 0
        self.saves = 0

    def load(self, name):
        with self.lock:
            self.loads += 1
            if self.fail_load_once:
                self.fail_load_once = False
                raise RuntimeError("SECRET_LOAD_FAILURE")
            value, token = self.documents.get(name, ({}, None))
            return json.loads(json.dumps(value)), token

    def save(self, name, value, token):
        with self.lock:
            self.saves += 1
            _, current = self.documents.get(name, ({}, None))
            if current != token:
                raise Conflict()
            self.documents[name] = (json.loads(json.dumps(value)), (current or 0) + 1)
            if self.fail_after:
                self.fail_after = False
                raise RuntimeError("SECRET_AMBIGUOUS_SAVE")


class LedgerHarness:
    def __init__(self):
        self.store = Store()
        self.ledger = MetadataOperationLedger(self.store, conflict_type=Conflict)
        self.actor = {"operator_id": "operator_private", "session_id": "session_private",
                      "org_binding_id": BINDING_ID}
        self.field = copy.deepcopy(FIELD)
        specification = {
            "binding": self.actor, "field": self.field,
            "confirmation_nonce": "nonce_private", "prepared_at": 100,
            "expires_at": 600,
        }
        self.ledger.prepare("operation_1", "prepare", specification)
        challenge = self.ledger.confirmation_challenge("operation_1", self.actor)
        self.ledger.apply("operation_1", self.actor, {
            "command_id": "confirm", "expected_revision": 0,
            "action": "confirm", "at": 101, "payload": challenge,
        })

    def snapshot(self):
        return self.ledger.execution_snapshot("operation_1", self.actor)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, amount):
        self.now += amount


class Transport:
    """The only public callables are the four reviewed provider operations."""

    zero_retry_writes = True
    redirects_disabled = True

    def __init__(self, clock):
        self.calls = []
        self.session_overrides = {}
        self.describe_overrides = {}
        self.ack_overrides = {}
        self.open_error = None
        self.describe_error = None
        self.create_error = None
        self.close_error = None
        self.close_result = None
        self.exists = False
        self.described_field = None
        self.advance_on_open = 0
        self.advance_on_describe = 0
        self.advance_on_create = 0
        self.advance_on_close = 0
        self.block_create_until_cancelled = False
        self._create_entered = threading.Event()
        self._cancel_poll = threading.Event()
        self._clock = clock

    def open_verified_session(self, binding, deadline, cancellation):
        self.calls.append(("open", deadline, None, cancellation))
        if self.advance_on_open:
            self._clock.advance(self.advance_on_open)
        if self.open_error:
            raise self.open_error
        value = {"org_id": binding.org_id, "origin": binding.origin,
                 "version": binding.version, "session_handle": "session-opaque-1"}
        value.update(copy.deepcopy(self.session_overrides))
        return value

    def describe_lead_text(self, session, member, deadline, cancellation):
        self.calls.append(("describe", deadline, member, cancellation))
        if self.advance_on_describe:
            self._clock.advance(self.advance_on_describe)
        if self.describe_error:
            raise self.describe_error
        value = {"org_id": session["org_id"], "origin": session["origin"],
                 "version": session["version"], "member": member,
                 "complete": True, "exists": self.exists,
                 "field": copy.deepcopy(self.described_field if self.exists else None)}
        value.update(copy.deepcopy(self.describe_overrides))
        return value

    def create_lead_text_once(self, session, field, deadline, cancellation):
        self.calls.append(("create", deadline, field["name"], cancellation))
        self._create_entered.set()
        if self.block_create_until_cancelled:
            while not cancellation():
                self._cancel_poll.wait(0.01)
            raise RuntimeError("CANCELLED_DURING_CREATE")
        if self.advance_on_create:
            self._clock.advance(self.advance_on_create)
        if self.create_error:
            raise self.create_error
        value = {"org_id": session["org_id"], "origin": session["origin"],
                 "version": session["version"], "member": MEMBER,
                 "field": copy.deepcopy(field), "created": True}
        value.update(copy.deepcopy(self.ack_overrides))
        return value

    def close(self, session, deadline, cancellation):
        self.calls.append(("close", deadline, None, cancellation))
        if self.advance_on_close:
            self._clock.advance(self.advance_on_close)
        if self.close_error:
            raise self.close_error
        return self.close_result


class MetadataProviderTests(unittest.TestCase):
    def setUp(self):
        self.mono = Clock(1000.0)
        self.wall = Clock(105)
        self.binding = OrgBinding(BINDING_ID, ORG_ID, ORIGIN, VERSION, 1, "developer")

    def adapter(self, transport=None, binding=None):
        transport = transport or Transport(self.mono)
        adapter = SalesforceMetadataAdapter(
            binding or self.binding, transport, clock=self.mono,
            trusted_timestamp=self.wall,
        )
        return adapter, transport

    def prepared(self, transport=None, *, deadline=1015):
        harness = LedgerHarness()
        adapter, transport = self.adapter(transport)
        budget = adapter.issue_execution_budget(
            harness.ledger, "operation_1", harness.actor, deadline=deadline
        )
        return harness, adapter, transport, budget

    def reserved(self, transport=None, *, deadline=1015):
        harness, adapter, transport, budget = self.prepared(transport, deadline=deadline)
        evidence = adapter.preflight(budget)
        self.assertEqual(set(evidence), {"org_binding_id", "member", "exists", "observed_at"})
        adapter.commit_preflight(budget, harness.ledger, harness.actor, command_id="preflight")
        permit = adapter.reserve_dispatch(
            budget, harness.ledger, harness.actor, command_id="begin"
        )
        return harness, adapter, transport, budget, permit

    def dispatched(self, transport=None, *, deadline=1015):
        harness, adapter, transport, budget, permit = self.reserved(transport, deadline=deadline)
        result = adapter.dispatch_once(permit)
        return harness, adapter, transport, budget, result

    def verify_ready(self, transport=None, *, deadline=1015):
        harness, adapter, transport, budget, result = self.dispatched(transport, deadline=deadline)
        self.assertEqual(result["outcome"], "accepted")
        adapter.commit_dispatch_result(
            budget, harness.ledger, harness.actor, command_id="dispatch-result"
        )
        return harness, adapter, transport, budget

    def test_org_binding_separates_opaque_and_actual_identity_and_is_immutable(self):
        value = OrgBinding.from_mapping(self.binding.as_mapping())
        self.assertEqual(value.org_binding_id, BINDING_ID)
        self.assertEqual(value.org_id, ORG_ID)
        self.assertNotEqual(value.org_binding_id, value.org_id)
        self.assertNotIn(ORG_ID, repr(value))
        self.assertNotIn(ORIGIN, repr(value))
        with self.assertRaises(FrozenInstanceError):
            value.binding_version = 2
        expanded = value.as_mapping()
        expanded["extra"] = True
        with self.assertRaises(MetadataProviderError):
            OrgBinding.from_mapping(expanded)
        for args in (
            (ORG_ID, ORG_ID, ORIGIN, VERSION, 1, "developer"),
            (BINDING_ID, "00Dshort", ORIGIN, VERSION, 1, "developer"),
            (BINDING_ID, ORG_ID, "http://sfdc24.develop.my.salesforce.com", VERSION, 1, "developer"),
            (BINDING_ID, ORG_ID, ORIGIN + "/", VERSION, 1, "developer"),
            (BINDING_ID, ORG_ID, ORIGIN, "latest", 1, "developer"),
            (BINDING_ID, ORG_ID, ORIGIN, VERSION, True, "developer"),
            (BINDING_ID, ORG_ID, ORIGIN, VERSION, 1, "sandbox"),
        ):
            with self.subTest(args=args), self.assertRaises(MetadataProviderError):
                OrgBinding(*args)
        sandbox = OrgBinding("sandbox_binding", ORG_ID,
                             "https://tenant--qa.sandbox.my.salesforce.com",
                             VERSION, 4, "sandbox")
        self.assertEqual(sandbox.environment, "sandbox")

    def test_transport_capabilities_and_callable_surface_are_exact(self):
        for keys in (provider.TRANSPORT_METHODS, provider.SESSION_KEYS,
                     provider.DESCRIBE_KEYS, provider.ACK_KEYS,
                     provider.DISPATCH_KEYS, provider.PREFLIGHT_KEYS,
                     provider.OBSERVATION_KEYS):
            self.assertIsInstance(keys, frozenset)
            with self.assertRaises(AttributeError):
                keys.add("extra")
        adapter, transport = self.adapter(Transport(self.mono))
        for name, value in (("binding", object()), ("_transport", object()),
                            ("_transport_callables", ()),
                            ("_clock", lambda: 0), ("_trusted_timestamp", lambda: 0),
                            ("_owner", object()), ("_sealed", False)):
            with self.subTest(assign=name), self.assertRaises(AttributeError):
                setattr(adapter, name, value)
            with self.subTest(delete=name), self.assertRaises(AttributeError):
                delattr(adapter, name)
        # Failed tampering must leave the validated transport and clocks usable.
        harness = LedgerHarness()
        budget = adapter.issue_execution_budget(
            harness.ledger, "operation_1", harness.actor, deadline=1015
        )
        self.assertIsInstance(budget, ExecutionBudget)
        self.assertEqual(transport.calls, [])
        for flag in ("zero_retry_writes", "redirects_disabled"):
            transport = Transport(self.mono)
            setattr(transport, flag, False)
            with self.assertRaises(MetadataProviderError):
                self.adapter(transport)

        class GenericTransport(Transport):
            def query(self):
                return None

        with self.assertRaises(MetadataProviderError):
            self.adapter(GenericTransport(self.mono))

    def test_budget_is_preflight_first_nonrenewable_nonserializable_and_closed(self):
        harness, adapter, transport, budget = self.prepared()
        self.assertIsInstance(budget, ExecutionBudget)
        self.assertNotIn(BINDING_ID, repr(budget))
        for operation in (copy.copy, copy.deepcopy, pickle.dumps, json.dumps):
            with self.subTest(operation=operation), self.assertRaises(TypeError):
                operation(budget)
        with self.assertRaises(AttributeError):
            budget._deadline = 9999
        for name in ("_sealed", "_phase", "_field", "_owner", "_ledger",
                     "_ledger_store"):
            with self.subTest(delete=name), self.assertRaises(AttributeError):
                delattr(budget, name)
        with self.assertRaises(AttributeError):
            budget._cancellation._budget = object()
        with self.assertRaises(AttributeError):
            budget._cancellation._owner = object()
        for name in ("_sealed", "_budget", "_owner"):
            with self.subTest(cancel_delete=name), self.assertRaises(AttributeError):
                delattr(budget._cancellation, name)
        with self.assertRaises(MetadataProviderError):
            adapter.issue_execution_budget(harness.ledger, "operation_1", harness.actor, deadline=1015)
        with self.assertRaises(MetadataProviderError):
            provider.ExecutionBudget(object(), owner=object(), ledger=harness.ledger,
                                     binding=self.binding,
                                     snapshot=harness.snapshot(), field=FIELD,
                                     member=MEMBER, deadline=1015, clock=self.mono,
                                     timestamp=self.wall)
        self.assertEqual(transport.calls, [])

    def test_budget_and_ledger_reject_dependency_or_lookalike_swap_before_io(self):
        issued = LedgerHarness()
        lookalike = LedgerHarness()
        adapter, transport = self.adapter()
        budget = adapter.issue_execution_budget(
            issued.ledger, "operation_1", issued.actor, deadline=1015
        )

        for name, value in (("store", lookalike.store),
                            ("conflict_type", RuntimeError), ("_sealed", False)):
            with self.subTest(assign=name), self.assertRaises(AttributeError):
                setattr(issued.ledger, name, value)
            with self.subTest(delete=name), self.assertRaises(AttributeError):
                delattr(issued.ledger, name)

        adapter.preflight(budget)
        original_store = issued.ledger.store
        object.__setattr__(issued.ledger, "store", lookalike.store)
        lookalike_io = (lookalike.store.loads, lookalike.store.saves)
        with self.assertRaises(MetadataProviderError):
            adapter.commit_preflight(
                budget, issued.ledger, issued.actor, command_id="preflight"
            )
        self.assertEqual((lookalike.store.loads, lookalike.store.saves), lookalike_io)
        object.__setattr__(issued.ledger, "store", original_store)

        lookalike_io = (lookalike.store.loads, lookalike.store.saves)
        with self.assertRaises(MetadataProviderError):
            adapter.commit_preflight(
                budget, lookalike.ledger, lookalike.actor, command_id="preflight"
            )
        self.assertEqual((lookalike.store.loads, lookalike.store.saves), lookalike_io)

        adapter.commit_preflight(
            budget, issued.ledger, issued.actor, command_id="preflight"
        )
        lookalike_io = (lookalike.store.loads, lookalike.store.saves)
        with self.assertRaises(MetadataProviderError):
            adapter.reserve_dispatch(
                budget, lookalike.ledger, lookalike.actor, command_id="begin"
            )
        self.assertEqual((lookalike.store.loads, lookalike.store.saves), lookalike_io)
        self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 0)
        self.assertEqual(issued.snapshot()["provider_attempts"], 0)
        self.assertEqual(lookalike.snapshot()["provider_attempts"], 0)

    def test_post_reservation_ledger_mismatch_precedes_ledger_and_provider_io(self):
        issued, adapter, transport, budget, result = self.dispatched()
        self.assertEqual(result["outcome"], "accepted")
        lookalike = LedgerHarness()

        def assert_rejected_without_io(call):
            ledger_io = (lookalike.store.loads, lookalike.store.saves)
            provider_calls = len(transport.calls)
            with self.assertRaises(MetadataProviderError):
                call()
            self.assertEqual((lookalike.store.loads, lookalike.store.saves), ledger_io)
            self.assertEqual(len(transport.calls), provider_calls)

        assert_rejected_without_io(lambda: adapter.commit_dispatch_result(
            budget, lookalike.ledger, lookalike.actor, command_id="dispatch-result"
        ))
        adapter.commit_dispatch_result(
            budget, issued.ledger, issued.actor, command_id="dispatch-result"
        )
        assert_rejected_without_io(lambda: adapter.verify_independent(
            budget, lookalike.ledger, lookalike.actor
        ))

        transport.exists = True
        transport.described_field = copy.deepcopy(FIELD)
        adapter.verify_independent(budget, issued.ledger, issued.actor)
        assert_rejected_without_io(lambda: adapter.commit_verification(
            budget, lookalike.ledger, lookalike.actor, command_id="verification"
        ))

    def test_transport_declaration_or_callable_drift_after_reserve_never_creates(self):
        modes = ("zero_retry_writes", "redirects_disabled", *provider.TRANSPORT_METHOD_ORDER)
        for mode in modes:
            with self.subTest(mode=mode):
                self.setUp()
                harness, adapter, transport, budget, permit = self.reserved()
                if mode in {"zero_retry_writes", "redirects_disabled"}:
                    setattr(transport, mode, False)
                else:
                    setattr(transport, mode, lambda *args, **kwargs: None)
                result = adapter.dispatch_once(permit)
                self.assertEqual(
                    result,
                    {"outcome": "rejected_no_change", "definitive_no_change": True},
                )
                self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 0)
                receipt = adapter.commit_dispatch_result(
                    budget, harness.ledger, harness.actor, command_id="dispatch-result"
                )
                self.assertEqual(receipt["status"], "failed")

    def test_transport_drift_is_sanitized_at_preflight_and_verification_boundaries(self):
        harness = LedgerHarness()
        adapter, transport = self.adapter()
        budget = adapter.issue_execution_budget(
            harness.ledger, "operation_1", harness.actor, deadline=1015
        )
        transport.redirects_disabled = False
        self.assertEqual(adapter.preflight(budget), PREFLIGHT_UNAVAILABLE)
        self.assertEqual(transport.calls, [])

        self.setUp()
        harness, adapter, transport, budget = self.verify_ready()
        provider_calls = len(transport.calls)
        transport.open_verified_session = lambda *args, **kwargs: None
        self.assertEqual(
            adapter.verify_independent(budget, harness.ledger, harness.actor),
            VERIFICATION_UNAVAILABLE,
        )
        self.assertEqual(len(transport.calls), provider_calls)
        receipt = adapter.commit_verification(
            budget, harness.ledger, harness.actor, command_id="verification"
        )
        self.assertEqual(receipt["status"], "outcome_unknown")

    def test_invalid_budget_and_plan_expiry_fail_before_provider_io(self):
        for deadline in (1000, 999, 1020.001, True, float("nan"), "1010"):
            with self.subTest(deadline=deadline):
                harness = LedgerHarness()
                adapter, transport = self.adapter()
                with self.assertRaises(MetadataProviderError):
                    adapter.issue_execution_budget(harness.ledger, "operation_1", harness.actor,
                                                   deadline=deadline)
                self.assertEqual(transport.calls, [])
        harness = LedgerHarness()
        adapter, transport = self.adapter()
        self.wall.now = 600
        with self.assertRaises(MetadataProviderError):
            adapter.issue_execution_budget(harness.ledger, "operation_1", harness.actor,
                                           deadline=1010)
        self.assertEqual(transport.calls, [])

    def test_preflight_returns_exact_pr223_evidence_and_commits_opaque_binding(self):
        harness, adapter, transport, budget = self.prepared()
        evidence = adapter.preflight(budget)
        self.assertEqual(evidence, {"org_binding_id": BINDING_ID, "member": MEMBER,
                                    "exists": False, "observed_at": 105})
        self.assertNotIn(ORG_ID, json.dumps(evidence))
        self.assertEqual([call[0] for call in transport.calls], ["open", "describe", "close"])
        self.assertEqual(transport.calls[1][2], MEMBER)
        receipt = adapter.commit_preflight(
            budget, harness.ledger, harness.actor, command_id="preflight"
        )
        self.assertEqual(receipt["status"], "confirmed")
        self.assertEqual(harness.snapshot()["preflight"], evidence)

    def test_preflight_identity_mismatch_or_failure_is_sanitized_and_no_write(self):
        modes = ("open", "session", "describe", "describe-binding", "incomplete", "close", "deadline")
        for mode in modes:
            with self.subTest(mode=mode):
                self.setUp()
                transport = Transport(self.mono)
                if mode == "open": transport.open_error = RuntimeError("SECRET_OPEN")
                if mode == "session": transport.session_overrides = {"org_id": "00Dzzzzzzzzzzzzzzz"}
                if mode == "describe": transport.describe_error = RuntimeError("SECRET_DESCRIBE")
                if mode == "describe-binding": transport.describe_overrides = {"origin": "https://other.develop.my.salesforce.com"}
                if mode == "incomplete": transport.describe_overrides = {"complete": False}
                if mode == "close": transport.close_error = RuntimeError("SECRET_CLOSE")
                if mode == "deadline": transport.advance_on_describe = 16
                _, adapter, transport, budget = self.prepared(transport)
                result = adapter.preflight(budget)
                self.assertEqual(result, PREFLIGHT_UNAVAILABLE)
                self.assertNotIn("SECRET", result)
                self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 0)

    def test_preexisting_initial_preflight_commits_quarantine_and_never_reserves(self):
        transport = Transport(self.mono)
        transport.exists = True
        transport.described_field = copy.deepcopy(FIELD)
        harness, adapter, _, budget = self.prepared(transport)
        evidence = adapter.preflight(budget)
        self.assertTrue(evidence["exists"])
        receipt = adapter.commit_preflight(
            budget, harness.ledger, harness.actor, command_id="preflight"
        )
        self.assertEqual(receipt["status"], "failed")
        with self.assertRaises(DispatchPermitError):
            adapter.reserve_dispatch(budget, harness.ledger, harness.actor, command_id="begin")
        self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 0)

    def test_coordinator_invokes_fresh_begin_and_old_portable_mappings_cannot_dispatch(self):
        harness, adapter, transport, budget, permit = self.reserved()
        snapshot = harness.snapshot()
        begin_like = {"receipt": adapter._transport.calls, "revision": snapshot["revision"],
                      "replayed": False, "dispatch_allowed": True}
        for forged in (True, {"dispatch_allowed": True}, begin_like, snapshot):
            with self.subTest(forged=type(forged).__name__), self.assertRaises(DispatchPermitError):
                adapter.dispatch_once(forged)
        self.assertEqual(snapshot["state"], "executing")
        restarted, _ = self.adapter(Transport(self.mono))
        with self.assertRaises(MetadataProviderError):
            restarted.issue_execution_budget(harness.ledger, "operation_1", harness.actor,
                                              deadline=1015)
        self.assertNotIn("create", [call[0] for call in transport.calls])
        self.assertIsNotNone(permit)

    def test_ambiguous_begin_save_never_mints_on_replay(self):
        harness, adapter, _, budget = self.prepared()
        adapter.preflight(budget)
        adapter.commit_preflight(budget, harness.ledger, harness.actor, command_id="preflight")
        harness.store.fail_after = True
        with self.assertRaises(PersistenceUncertain):
            adapter.reserve_dispatch(budget, harness.ledger, harness.actor, command_id="begin")
        self.assertEqual(harness.snapshot()["state"], "executing")
        with self.assertRaises(DispatchPermitError):
            adapter.reserve_dispatch(budget, harness.ledger, harness.actor, command_id="begin")

    def test_preflight_and_dispatch_result_ambiguous_saves_replay_without_provider_io(self):
        harness, adapter, transport, budget = self.prepared()
        adapter.preflight(budget)
        provider_calls = len(transport.calls)
        harness.store.fail_after = True
        with self.assertRaises(PersistenceUncertain):
            adapter.commit_preflight(
                budget, harness.ledger, harness.actor, command_id="preflight"
            )
        receipt = adapter.commit_preflight(
            budget, harness.ledger, harness.actor, command_id="preflight"
        )
        self.assertEqual(receipt["status"], "confirmed")
        self.assertEqual(len(transport.calls), provider_calls)
        permit = adapter.reserve_dispatch(
            budget, harness.ledger, harness.actor, command_id="begin"
        )
        self.assertEqual(adapter.dispatch_once(permit)["outcome"], "accepted")
        provider_calls = len(transport.calls)
        harness.store.fail_after = True
        with self.assertRaises(PersistenceUncertain):
            adapter.commit_dispatch_result(
                budget, harness.ledger, harness.actor, command_id="dispatch-result"
            )
        receipt = adapter.commit_dispatch_result(
            budget, harness.ledger, harness.actor, command_id="dispatch-result"
        )
        self.assertEqual(receipt["status"], "verify_pending")
        self.assertEqual(len(transport.calls), provider_calls)

    def test_preexisting_preflight_ambiguous_save_replays_terminal_receipt(self):
        transport = Transport(self.mono)
        transport.exists = True
        transport.described_field = copy.deepcopy(FIELD)
        harness, adapter, transport, budget = self.prepared(transport)
        evidence = adapter.preflight(budget)
        self.assertTrue(evidence["exists"])
        provider_calls = len(transport.calls)
        harness.store.fail_after = True
        with self.assertRaises(PersistenceUncertain):
            adapter.commit_preflight(
                budget, harness.ledger, harness.actor, command_id="preflight"
            )
        self.assertEqual(harness.snapshot()["state"], "failed")
        receipt = adapter.commit_preflight(
            budget, harness.ledger, harness.actor, command_id="preflight"
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(len(transport.calls), provider_calls)
        replay = adapter.commit_preflight(
            budget, harness.ledger, harness.actor, command_id="preflight"
        )
        self.assertEqual(replay, receipt)
        self.assertEqual(len(transport.calls), provider_calls)

    def test_permit_is_nonserializable_one_use_and_concurrency_safe(self):
        _, adapter, transport, _, permit = self.reserved()
        for operation in (copy.copy, copy.deepcopy, pickle.dumps, json.dumps):
            with self.subTest(operation=operation), self.assertRaises(TypeError):
                operation(permit)
        for name in ("_sealed", "_budget", "_consumed", "_owner"):
            with self.subTest(delete=name), self.assertRaises(AttributeError):
                delattr(permit, name)
        start = threading.Barrier(3)
        outcomes = []

        def run():
            start.wait(timeout=5)
            try:
                outcomes.append(adapter.dispatch_once(permit)["outcome"])
            except DispatchPermitError:
                outcomes.append("permit_error")

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        start.wait(timeout=5)
        for thread in threads: thread.join(5)
        self.assertCountEqual(outcomes, ["accepted", "permit_error"])
        self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 1)
        with self.assertRaises(DispatchPermitError):
            adapter.dispatch_once(permit)
        with self.assertRaises(DispatchPermitError):
            provider._DispatchPermit(object(), owner=object(), budget=True)

    def test_complete_absence_has_one_create_and_one_deadline_across_all_phases(self):
        harness, adapter, transport, budget, result = self.dispatched()
        self.assertEqual(result, {"outcome": "accepted", "definitive_no_change": False})
        adapter.commit_dispatch_result(
            budget, harness.ledger, harness.actor, command_id="dispatch-result"
        )
        transport.exists = True
        transport.described_field = copy.deepcopy(FIELD)
        observation = adapter.verify_independent(budget, harness.ledger, harness.actor)
        self.assertEqual(observation, {"org_binding_id": BINDING_ID,
                                       "field": FIELD, "observed_at": 105})
        receipt = adapter.commit_verification(
            budget, harness.ledger, harness.actor, command_id="verification"
        )
        self.assertEqual(receipt["status"], "succeeded")
        names = [call[0] for call in transport.calls]
        self.assertEqual(names, ["open", "describe", "close",
                                 "open", "describe", "create", "close",
                                 "open", "describe", "close"])
        self.assertEqual({call[1] for call in transport.calls}, {1015.0})
        self.assertEqual(len({id(call[3]) for call in transport.calls}), 1)
        self.assertEqual(Counter(names)["create"], 1)
        self.assertTrue(all(call[2] == MEMBER for call in transport.calls if call[0] == "describe"))

    def test_second_absence_check_catches_preexisting_race_without_create(self):
        harness, adapter, transport, budget, permit = self.reserved()
        transport.exists = True
        transport.described_field = copy.deepcopy(FIELD)
        result = adapter.dispatch_once(permit)
        self.assertEqual(result, {"outcome": "rejected_no_change", "definitive_no_change": True})
        self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 0)
        receipt = adapter.commit_dispatch_result(
            budget, harness.ledger, harness.actor, command_id="dispatch-result"
        )
        self.assertEqual(receipt["status"], "failed")

    def test_session_and_describe_identity_mismatch_never_enters_create(self):
        for stage in ("session", "describe"):
            for key, value in (("org_id", "00Dzzzzzzzzzzzzzzz"),
                               ("origin", "https://other.develop.my.salesforce.com"),
                               ("version", "v63.0")):
                with self.subTest(stage=stage, key=key):
                    self.setUp()
                    transport = Transport(self.mono)
                    harness, adapter, transport, budget, permit = self.reserved(transport)
                    getattr(transport, stage + "_overrides")[key] = value
                    result = adapter.dispatch_once(permit)
                    self.assertEqual(result["outcome"], "rejected_no_change")
                    self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 0)
                    self.assertEqual(harness.snapshot()["state"], "executing")

    def test_every_post_create_error_nonexact_ack_and_deadline_is_ambiguous(self):
        modes = ("create", "base-exception", "ack-org", "ack-origin", "ack-version",
                 "ack-member", "ack-field", "ack-extra", "ack-false", "deadline",
                 "close", "close-expanded")
        for mode in modes:
            with self.subTest(mode=mode):
                self.setUp()
                transport = Transport(self.mono)
                harness, adapter, transport, budget, permit = self.reserved(transport)
                if mode == "create": transport.create_error = RuntimeError("SECRET_CREATE")
                if mode == "base-exception":
                    class FatalProviderExit(BaseException):
                        pass
                    transport.create_error = FatalProviderExit("SECRET_FATAL_CREATE")
                if mode == "ack-org": transport.ack_overrides = {"org_id": "00Dzzzzzzzzzzzzzzz"}
                if mode == "ack-origin": transport.ack_overrides = {"origin": "https://other.develop.my.salesforce.com"}
                if mode == "ack-version": transport.ack_overrides = {"version": "v63.0"}
                if mode == "ack-member": transport.ack_overrides = {"member": "CustomField:Lead.other__c"}
                if mode == "ack-field": transport.ack_overrides = {"field": {**FIELD, "length": 81}}
                if mode == "ack-extra": transport.ack_overrides = {"extra": "SECRET_BODY"}
                if mode == "ack-false": transport.ack_overrides = {"created": False}
                if mode == "deadline": transport.advance_on_create = 16
                if mode == "close": transport.close_error = RuntimeError("SECRET_CLOSE")
                if mode == "close-expanded": transport.close_result = {"extra": "SECRET"}
                result = adapter.dispatch_once(permit)
                self.assertEqual(result, {"outcome": "ambiguous", "definitive_no_change": False})
                self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 1)
                self.assertNotIn("SECRET", json.dumps(result))
                receipt = adapter.commit_dispatch_result(
                    budget, harness.ledger, harness.actor, command_id="dispatch-result"
                )
                self.assertEqual(receipt["status"], "outcome_unknown")

    def test_pre_create_error_cancel_or_expiry_is_definite_no_change(self):
        modes = ("open", "describe", "incomplete", "cancel", "deadline")
        for mode in modes:
            with self.subTest(mode=mode):
                self.setUp()
                transport = Transport(self.mono)
                harness, adapter, transport, budget, permit = self.reserved(transport)
                if mode == "open": transport.open_error = RuntimeError("SECRET_OPEN")
                if mode == "describe": transport.describe_error = RuntimeError("SECRET_DESCRIBE")
                if mode == "incomplete": transport.describe_overrides = {"complete": False}
                if mode == "cancel": adapter.cancel_execution(budget)
                if mode == "deadline": self.mono.now = 1015
                result = adapter.dispatch_once(permit)
                self.assertEqual(result, {"outcome": "rejected_no_change", "definitive_no_change": True})
                self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 0)
                receipt = adapter.commit_dispatch_result(
                    budget, harness.ledger, harness.actor, command_id="dispatch-result"
                )
                self.assertEqual(receipt["status"], "failed")

    def test_independent_verify_returns_provider_observation_not_plan_echo(self):
        transport = Transport(self.mono)
        harness, adapter, transport, budget = self.verify_ready(transport)
        transport.exists = True
        transport.described_field = copy.deepcopy(FIELD)
        result = adapter.verify_independent(budget, harness.ledger, harness.actor)
        self.assertEqual(set(result), {"org_binding_id", "field", "observed_at"})
        self.assertEqual(result["field"], FIELD)
        self.assertIsNot(result["field"], harness.snapshot()["plan"]["field"])
        self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 1)
        self.assertEqual(Counter(call[0] for call in transport.calls)["describe"], 3)
        receipt = adapter.commit_verification(
            budget, harness.ledger, harness.actor, command_id="verification"
        )
        self.assertEqual(receipt["status"], "succeeded")

    def test_verification_mismatch_and_unavailable_are_sanitized(self):
        for mode in ("absent", "different", "session", "describe", "close", "deadline", "plan-expiry"):
            with self.subTest(mode=mode):
                self.setUp()
                transport = Transport(self.mono)
                harness, adapter, transport, budget = self.verify_ready(transport)
                transport.exists = mode != "absent"
                transport.described_field = {**FIELD, "length": 81} if mode == "different" else copy.deepcopy(FIELD)
                if mode == "session": transport.session_overrides = {"origin": "https://other.develop.my.salesforce.com"}
                if mode == "describe": transport.describe_error = RuntimeError("SECRET_DESCRIBE")
                if mode == "close": transport.close_error = RuntimeError("SECRET_CLOSE")
                if mode == "deadline": self.mono.now = 1015
                if mode == "plan-expiry": self.wall.now = 600
                result = adapter.verify_independent(budget, harness.ledger, harness.actor)
                expected = VERIFICATION_MISMATCH if mode in {"absent", "different"} else VERIFICATION_UNAVAILABLE
                self.assertEqual(result, expected)
                self.assertNotIn("Interest", result)
                self.assertNotIn(ORG_ID, result)
                receipt = adapter.commit_verification(
                    budget, harness.ledger, harness.actor, command_id="verification"
                )
                self.assertEqual(receipt["status"], "outcome_unknown")

    def test_verification_commit_uses_only_stored_result_and_replays_ambiguous_save(self):
        transport = Transport(self.mono)
        harness, adapter, transport, budget = self.verify_ready(transport)
        transport.exists = True
        transport.described_field = copy.deepcopy(FIELD)
        observation = adapter.verify_independent(budget, harness.ledger, harness.actor)
        forged = copy.deepcopy(observation)
        forged["field"]["length"] = 81
        with self.assertRaises(TypeError):
            # The commit surface deliberately has no result/observation input.
            adapter.commit_verification(budget, harness.ledger, harness.actor,
                                        command_id="verification", observation=forged)
        harness.store.fail_after = True
        with self.assertRaises(PersistenceUncertain):
            adapter.commit_verification(
                budget, harness.ledger, harness.actor, command_id="verification"
            )
        self.assertEqual(harness.snapshot()["state"], "succeeded")
        receipt = adapter.commit_verification(
            budget, harness.ledger, harness.actor, command_id="verification"
        )
        self.assertEqual(receipt["status"], "succeeded")
        with self.assertRaises(MetadataProviderError):
            adapter.commit_verification(
                budget, harness.ledger, harness.actor, command_id="different"
            )
        self.assertEqual(Counter(call[0] for call in transport.calls)["describe"], 3)

    def test_verification_snapshot_failure_is_unavailable_commit_without_provider_retry(self):
        transport = Transport(self.mono)
        harness, adapter, transport, budget = self.verify_ready(transport)
        calls_before = len(transport.calls)
        harness.store.fail_load_once = True
        result = adapter.verify_independent(budget, harness.ledger, harness.actor)
        self.assertEqual(result, VERIFICATION_UNAVAILABLE)
        self.assertEqual(len(transport.calls), calls_before)
        receipt = adapter.commit_verification(
            budget, harness.ledger, harness.actor, command_id="verification"
        )
        self.assertEqual(receipt["status"], "outcome_unknown")
        self.assertEqual(len(transport.calls), calls_before)

    def test_same_cancellation_signal_interrupts_blocking_create_without_retry(self):
        transport = Transport(self.mono)
        harness, adapter, transport, budget, permit = self.reserved(transport)
        transport.block_create_until_cancelled = True
        results = []

        def run():
            results.append(adapter.dispatch_once(permit))

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(transport._create_entered.wait(5))
        adapter.cancel_execution(budget)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [{"outcome": "ambiguous", "definitive_no_change": False}])
        self.assertEqual(Counter(call[0] for call in transport.calls)["create"], 1)
        self.assertEqual(len({id(call[3]) for call in transport.calls}), 1)
        receipt = adapter.commit_dispatch_result(
            budget, harness.ledger, harness.actor, command_id="dispatch-result"
        )
        self.assertEqual(receipt["status"], "outcome_unknown")

    def test_no_side_effect_clients_generic_interfaces_or_controller_wiring(self):
        source_path = ROOT / "cloud" / "studio-controller" / "app" / "metadata_provider.py"
        source = source_path.read_text(encoding="utf-8")
        imports = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue({"os", "socket", "subprocess", "urllib", "requests", "simple_salesforce"}.isdisjoint(imports))
        for forbidden in ("getenv(", "urlopen(", "requests.", "subprocess.", "socket.", "execute("):
            self.assertNotIn(forbidden, source)
        for wired_file in ("main.py", "core.py", "settings.py"):
            wired = (ROOT / "cloud" / "studio-controller" / "app" / wired_file).read_text(encoding="utf-8")
            self.assertNotIn("metadata_provider", wired)

    def test_hosted_ci_is_mandatory_network_isolated_and_cannot_false_green(self):
        workflow = (ROOT / ".github" / "workflows" / "python-suites.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(workflow.count('"tests/test_studio_metadata_provider.py"'), 2)
        self.assertIn("tests/test_studio_metadata_provider.py \\", workflow)
        self.assertIn("test -f cloud/studio-controller/app/metadata_provider.py", workflow)
        self.assertIn("test -f tests/test_studio_metadata_provider.py", workflow)
        self.assertIn("sudo unshare -n -- \"$python_bin\" -m unittest tests.test_studio_metadata_provider", workflow)
        self.assertIn("&& [ ! -f tests/test_studio_metadata_provider.py ]", workflow)


if __name__ == "__main__":
    unittest.main()

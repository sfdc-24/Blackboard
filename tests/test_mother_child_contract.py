#!/usr/bin/env python3
"""Offline adversarial checks for the mother/child admission contract."""

from __future__ import annotations

import copy
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import mother_child_contract as contract  # noqa: E402


NOW = datetime(2026, 9, 14, 1, 0, 0, tzinfo=timezone.utc)
FENCE_TOKEN = "synthetic-current-fence-token-0001"
INVITE_ISSUER = "https://idp.example.test"


def base_command(operation="child.append", plane="child"):
    command = {
        "schema": contract.COMMAND_SCHEMA,
        "request_id": "request-0001",
        "plane": plane,
        "operation": operation,
        "tenant_id": "tenant-alpha",
        "child_id": "child-one",
        "actor_id": "workload:agent-one",
        "session_id": "session-0001",
        "resource_id": "bb://tenant/tenant-alpha/child/child-one/board/alpha",
        "work_id": "work-0001",
        "attempt_id": "attempt-0001",
        "idempotency_key": "idempotency-0001",
        "issued_at": "2026-09-14T00:59:00Z",
        "expires_at": "2026-09-14T01:04:00Z",
        "security_version": 3,
        "credential_epoch": 7,
        "policy_generation": 11,
        "payload": {"cells": ["synthetic-row", "no-customer-data"]},
    }
    if operation in ("work.claim", "work.transition"):
        command["resource_id"] = "bb://tenant/tenant-alpha/child/child-one/work/work-0001"
    elif operation == "child.event":
        command["resource_id"] = "bb://tenant/tenant-alpha/child/child-one/event/work-0001"
    elif operation.startswith("control."):
        command["resource_id"] = "bb://tenant/tenant-alpha/child/child-one/control/child-one"
        del command["work_id"]
        del command["attempt_id"]
    if operation == "work.transition":
        command["fence"] = {
            "claim_id": "claim-0001",
            "generation": 4,
            "token": FENCE_TOKEN,
            "work_version": 6,
            "child_incarnation": 2,
        }
    return command


def base_principal(plane="child", scopes=None):
    if scopes is None:
        scopes = ["child.read", "child.append", "child.event", "work.claim", "work.transition"]
    return {
        "principal_id": "workload:agent-one",
        "session_id": "session-0001",
        "plane": plane,
        "tenant_id": "tenant-alpha",
        "child_ids": ["child-one"],
        "scopes": scopes,
        "security_version": 3,
        "credential_epoch": 7,
        "expires_at": "2026-09-14T02:00:00Z",
    }


def base_policy():
    operations = list(contract.OPERATIONS)
    return {
        "tenant_id": "tenant-alpha",
        "child_id": "child-one",
        "state": "ACTIVE",
        "security_version": 3,
        "credential_epoch": 7,
        "policy_generation": 11,
        "parent_quota_ceilings": {
            "requests-per-minute": 1000,
            "concurrent-workers": 10,
        },
        "child_quota_limits": {
            "requests-per-minute": 800,
            "concurrent-workers": 8,
        },
        "parent_allowed_operations": list(operations),
        "child_allowed_operations": list(operations),
        "max_command_ttl_seconds": 300,
        "max_clock_skew_seconds": 30,
        "max_payload_bytes": 4096,
    }


def current_fence():
    return {
        "tenant_id": "tenant-alpha",
        "child_id": "child-one",
        "work_id": "work-0001",
        "attempt_id": "attempt-0001",
        "claim_id": "claim-0001",
        "holder_principal_id": "workload:agent-one",
        "generation": 4,
        "token_hash": contract.token_hash(FENCE_TOKEN),
        "lease_until": "2026-09-14T01:10:00Z",
        "work_version": 6,
        "child_incarnation": 2,
    }


def base_invitation(invite_id, raw_token, max_scopes=None):
    return {
        "schema": contract.INVITATION_SCHEMA,
        "invite_id": invite_id,
        "tenant_id": "tenant-alpha",
        "child_id": "child-one",
        "intended_issuer": INVITE_ISSUER,
        "intended_subject": "oidc:subject-one",
        "max_scopes": max_scopes or ["child.read"],
        "policy_version": 11,
        "token_hash": contract.token_hash(raw_token),
        "expires_at": "2026-09-14T02:00:00Z",
    }


def base_verified_identity(**changes):
    identity = {"issuer": INVITE_ISSUER, "subject": "oidc:subject-one"}
    identity.update(changes)
    return identity


def invitation_ledger(invitation, policy=None):
    return contract.InvitationLedger([invitation], [policy or base_policy()])


class MotherChildContractTests(unittest.TestCase):
    def assert_code(self, expected, function, *args, **kwargs):
        with self.assertRaises(contract.ContractError) as caught:
            function(*args, **kwargs)
        self.assertEqual(expected, caught.exception.code)

    def test_valid_admission_is_not_delivery_and_replay_is_exact(self):
        ledger = contract.AdmissionLedger()
        command = base_command()
        first = ledger.admit(command, base_principal(), base_policy(), now=NOW)
        replay = ledger.admit(copy.deepcopy(command), base_principal(), base_policy(), now=NOW)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.receipt, replay.receipt)
        self.assertEqual(1, ledger.durable_operation_count)
        self.assertTrue(first.receipt["accepted"])
        self.assertFalse(first.receipt["destination_readback"])
        self.assertEqual(contract.request_hash(command), first.receipt["request_hash"])

    def test_concurrent_byte_identical_retries_reserve_one_operation(self):
        ledger = contract.AdmissionLedger()
        command = base_command()

        def admit(_):
            return ledger.admit(copy.deepcopy(command), base_principal(), base_policy(), now=NOW)

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(admit, range(100)))

        self.assertEqual(1, sum(not item.replayed for item in results))
        self.assertEqual(99, sum(item.replayed for item in results))
        self.assertEqual(1, len({item.receipt["receipt_id"] for item in results}))
        self.assertEqual(1, ledger.durable_operation_count)

    def test_same_idempotency_key_with_changed_bytes_conflicts(self):
        ledger = contract.AdmissionLedger()
        first = base_command()
        changed = copy.deepcopy(first)
        changed["payload"]["cells"][0] = "different"
        ledger.admit(first, base_principal(), base_policy(), now=NOW)

        self.assert_code(
            "idempotency_conflict", ledger.admit, changed, base_principal(), base_policy(), now=NOW)
        self.assertEqual(1, ledger.durable_operation_count)

    def test_replay_still_requires_current_authorization(self):
        ledger = contract.AdmissionLedger()
        command = base_command()
        ledger.admit(command, base_principal(), base_policy(), now=NOW)
        self.assert_code(
            "expired", ledger.admit, copy.deepcopy(command), base_principal(),
            base_policy(), now=datetime(2026, 9, 14, 1, 5, tzinfo=timezone.utc))
        self.assertEqual(1, ledger.durable_operation_count)

    def test_request_tenant_cannot_override_verified_principal(self):
        command = base_command()
        command["tenant_id"] = "tenant-bravo"
        command["resource_id"] = "bb://tenant/tenant-bravo/child/child-one/board/alpha"
        self.assert_code(
            "tenant_scope_mismatch", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

    def test_sibling_child_is_denied_before_data_access(self):
        command = base_command()
        command["child_id"] = "child-two"
        command["resource_id"] = "bb://tenant/tenant-alpha/child/child-two/board/alpha"
        policy = base_policy()
        policy["child_id"] = "child-two"
        self.assert_code(
            "tenant_scope_mismatch", contract.authorize,
            command, base_principal(), policy, now=NOW)

    def test_resource_scope_must_repeat_authenticated_tenant_and_child(self):
        command = base_command()
        command["resource_id"] = "bb://tenant/tenant-alpha/child/child-two/board/alpha"
        self.assert_code(
            "resource_scope_mismatch", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

    def test_operation_is_bound_to_resource_kind_and_shape(self):
        append = base_command()
        append["resource_id"] = "bb://tenant/tenant-alpha/child/child-one/health/status"
        self.assert_code(
            "operation_resource_mismatch", contract.authorize,
            append, base_principal(), base_policy(), now=NOW)

        control = base_command(operation="control.child.suspend", plane="control")
        control["payload"] = {"reason": "synthetic containment", "ticket": "ticket-0001"}
        control["resource_id"] = "bb://tenant/tenant-alpha/child/child-one/board/alpha"
        principal = base_principal(plane="control", scopes=["control.child.suspend"])
        self.assert_code(
            "operation_resource_mismatch", contract.authorize,
            control, principal, base_policy(), now=NOW)

    def test_actor_and_session_are_bound_to_verified_identity(self):
        command = base_command()
        command["actor_id"] = "workload:other-agent"
        self.assert_code(
            "identity_mismatch", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

        command = base_command()
        command["session_id"] = "session-stolen"
        self.assert_code(
            "identity_mismatch", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

    def test_mother_control_identity_cannot_read_child_payload(self):
        principal = base_principal(
            plane="control", scopes=["control.child.suspend", "child.read"])
        self.assert_code(
            "principal_plane_mismatch", contract.authorize,
            base_command(operation="child.read"), principal, base_policy(), now=NOW)

    def test_child_identity_cannot_issue_mother_control(self):
        command = base_command(operation="control.child.suspend", plane="control")
        command["payload"] = {"reason": "synthetic containment", "ticket": "ticket-0001"}
        principal = base_principal(scopes=["control.child.suspend"])
        self.assert_code(
            "principal_plane_mismatch", contract.authorize,
            command, principal, base_policy(), now=NOW)

    def test_mother_negative_authority_is_metadata_only(self):
        command = base_command(operation="control.child.suspend", plane="control")
        command["payload"] = {"reason": "synthetic containment", "ticket": "ticket-0001"}
        principal = base_principal(plane="control", scopes=["control.child.suspend"])

        decision = contract.authorize(command, principal, base_policy(), now=NOW)
        self.assertEqual("control.child.suspend", decision["operation"])

        command["payload"]["content"] = "attempted child data"
        self.assert_code(
            "control_payload_denied", contract.authorize,
            command, principal, base_policy(), now=NOW)

    def test_negative_authority_cannot_raise_quota_or_roll_back_epoch(self):
        principal = base_principal(
            plane="control",
            scopes=["control.child.lower-quota", "control.child.rotate-epoch"],
        )
        quota = base_command(operation="control.child.lower-quota", plane="control")
        quota["payload"] = {
            "quota_name": "requests-per-minute",
            "new_limit": 999999,
            "reason": "synthetic containment",
            "ticket": "ticket-0001",
        }
        self.assert_code(
            "quota_increase_denied", contract.authorize,
            quota, principal, base_policy(), now=NOW)
        quota["payload"]["new_limit"] = 500
        self.assertEqual(
            "control.child.lower-quota",
            contract.authorize(quota, principal, base_policy(), now=NOW)["operation"],
        )

        rotation = base_command(operation="control.child.rotate-epoch", plane="control")
        rotation["payload"] = {
            "new_epoch": 7,
            "reason": "synthetic containment",
            "ticket": "ticket-0002",
        }
        self.assert_code(
            "invalid_epoch_rotation", contract.authorize,
            rotation, principal, base_policy(), now=NOW)
        rotation["payload"]["new_epoch"] = 8
        self.assertEqual(
            "control.child.rotate-epoch",
            contract.authorize(rotation, principal, base_policy(), now=NOW)["operation"],
        )

        suspended = base_policy()
        suspended["state"] = "SUSPENDED"
        self.assertEqual(
            "control.child.rotate-epoch",
            contract.authorize(rotation, principal, suspended, now=NOW)["operation"],
        )
        self.assert_code(
            "child_suspended", contract.authorize,
            base_command(), base_principal(), suspended, now=NOW)

    def test_parent_ceiling_and_child_grant_are_both_required(self):
        command = base_command()
        policy = base_policy()
        policy["parent_allowed_operations"].remove("child.append")
        self.assert_code(
            "parent_policy_denied", contract.authorize,
            command, base_principal(), policy, now=NOW)

        policy = base_policy()
        policy["child_allowed_operations"].remove("child.append")
        self.assert_code(
            "child_policy_denied", contract.authorize,
            command, base_principal(), policy, now=NOW)

    def test_revocation_versions_fail_closed(self):
        command = base_command()
        command["security_version"] = 2
        self.assert_code(
            "stale_security_version", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

        command = base_command()
        command["credential_epoch"] = 6
        self.assert_code(
            "stale_credential_epoch", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

        command = base_command()
        command["policy_generation"] = 10
        self.assert_code(
            "stale_policy_generation", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

    def test_expiry_clock_skew_and_ttl_fail_closed(self):
        command = base_command()
        command["expires_at"] = "2026-09-14T00:59:59Z"
        self.assert_code(
            "expired", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

        command = base_command()
        command["issued_at"] = "2026-09-14T01:00:31Z"
        command["expires_at"] = "2026-09-14T01:04:31Z"
        self.assert_code(
            "future_command", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

        command = base_command()
        command["expires_at"] = "2026-09-14T01:04:01Z"
        self.assert_code(
            "invalid_ttl", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

        command = base_command()
        principal = base_principal()
        principal["expires_at"] = "2026-09-14T01:02:00Z"
        self.assert_code(
            "session_ttl_exceeded", contract.authorize,
            command, principal, base_policy(), now=NOW)

    def test_unknown_fields_and_credential_shaped_payloads_are_rejected(self):
        command = base_command()
        command["trusted_because_client_says_so"] = True
        self.assert_code(
            "unknown_field", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

        command = base_command()
        command["payload"] = {"nested": {"access-token": "synthetic-canary"}}
        self.assert_code(
            "secret_field_denied", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

        command = base_command()
        command["payload"] = {"nested": {"client_secret": "synthetic-canary"}}
        self.assert_code(
            "secret_field_denied", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)
        for field in (
                "AccessToken", "APIToken", "bearer_token", "credentials", "id_token",
                "IDToken", "JWTToken"):
            command = base_command()
            command["payload"] = {"nested": {field: "synthetic-canary"}}
            self.assert_code(
                "secret_field_denied", contract.authorize,
                command, base_principal(), base_policy(), now=NOW)

        command = base_command()
        command["payload"] = {"not_json": float("nan")}
        self.assert_code(
            "invalid_json", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

    def test_boolean_versions_and_duplicate_policy_scopes_are_rejected(self):
        principal = base_principal()
        principal["security_version"] = True
        self.assert_code(
            "invalid_field", contract.authorize,
            base_command(), principal, base_policy(), now=NOW)

        policy = base_policy()
        policy["parent_allowed_operations"].append("child.append")
        self.assert_code(
            "invalid_policy", contract.authorize,
            base_command(), base_principal(), policy, now=NOW)

        principal = base_principal()
        principal["scopes"] = [[]]
        self.assert_code(
            "invalid_principal", contract.authorize,
            base_command(), principal, base_policy(), now=NOW)

        policy = base_policy()
        policy["parent_allowed_operations"] = [[]]
        self.assert_code(
            "invalid_policy", contract.authorize,
            base_command(), base_principal(), policy, now=NOW)

        policy = base_policy()
        policy["child_quota_limits"]["concurrent-workers"] = 11
        self.assert_code(
            "invalid_policy", contract.authorize,
            base_command(), base_principal(), policy, now=NOW)

    def test_strict_json_boundary_rejects_ambiguous_or_nonfinite_input(self):
        parsed = contract.parse_json_object(b'{"schema":"ok","value":1}')
        self.assertEqual({"schema": "ok", "value": 1}, parsed)
        parsed_command = contract.parse_json_object(
            contract.canonical_json(base_command()).encode("utf-8"))
        self.assertEqual(
            "child.append",
            contract.authorize(
                parsed_command, base_principal(), base_policy(), now=NOW)["operation"],
        )
        self.assert_code(
            "duplicate_field", contract.parse_json_object,
            b'{"tenant_id":"tenant-alpha","tenant_id":"tenant-bravo"}')
        self.assert_code(
            "invalid_json", contract.parse_json_object, b'{"value":NaN}')
        self.assert_code(
            "invalid_json", contract.parse_json_object, b'{"value":1e400}')
        oversized_integer = b'{"value":' + (b'9' * 5000) + b'}'
        self.assert_code("invalid_json", contract.parse_json_object, oversized_integer)
        self.assert_code(
            "invalid_json", contract.parse_json_object, b'{"value":"\xff"}')
        self.assert_code(
            "invalid_shape", contract.parse_json_object, b'["not-an-object"]')
        deep_json = (b'{"x":' * 40) + b'null' + (b'}' * 40)
        self.assert_code("invalid_json", contract.parse_json_object, deep_json)

        command = base_command()
        command["operation"] = []
        self.assert_code(
            "invalid_field", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)
        command = base_command()
        command["payload"] = {"notes": "\ud800"}
        self.assert_code(
            "invalid_json", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)
        command = base_command()
        command["expires_at"] = "9999-12-31T23:59:59-23:59"
        self.assert_code(
            "invalid_time", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)
        command = base_command()
        command["issued_at"] = "0001-01-01T00:00:00+23:59"
        self.assert_code(
            "invalid_time", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

    def test_stale_foreign_and_missing_fences_are_denied(self):
        command = base_command(operation="work.transition")
        decision = contract.authorize(
            command, base_principal(), base_policy(), current_fence=current_fence(), now=NOW)
        self.assertEqual("work.transition", decision["operation"])

        stale = copy.deepcopy(command)
        stale["fence"]["generation"] = 3
        self.assert_code(
            "stale_fence", contract.authorize,
            stale, base_principal(), base_policy(), current_fence=current_fence(), now=NOW)

        foreign = copy.deepcopy(command)
        foreign["fence"]["token"] = "synthetic-foreign-fence-token-0002"
        self.assert_code(
            "stale_fence", contract.authorize,
            foreign, base_principal(), base_policy(), current_fence=current_fence(), now=NOW)

        missing = copy.deepcopy(command)
        del missing["fence"]
        self.assert_code(
            "fence_required", contract.authorize,
            missing, base_principal(), base_policy(), current_fence=current_fence(), now=NOW)

        wrong_work_head = current_fence()
        wrong_work_head["work_id"] = "work-foreign"
        self.assert_code(
            "stale_fence", contract.authorize,
            command, base_principal(), base_policy(),
            current_fence=wrong_work_head, now=NOW)

        expired_head = current_fence()
        expired_head["lease_until"] = "2026-09-14T00:59:59Z"
        self.assert_code(
            "stale_fence", contract.authorize,
            command, base_principal(), base_policy(),
            current_fence=expired_head, now=NOW)

    def test_non_fenced_operation_rejects_ambiguous_extra_fence(self):
        command = base_command()
        command["fence"] = {"generation": 4, "token": FENCE_TOKEN}
        self.assert_code(
            "unexpected_fence", contract.authorize,
            command, base_principal(), base_policy(), current_fence=current_fence(), now=NOW)
        command = base_command()
        command["fence"] = None
        self.assert_code(
            "unexpected_fence", contract.authorize,
            command, base_principal(), base_policy(), now=NOW)

    def test_invitation_is_identity_bound_scoped_and_atomic(self):
        raw_token = "synthetic-invite-token-0000000000000001"
        invitation = base_invitation(
            "invite-0001", raw_token, ["child.read", "child.append"])
        ledger = invitation_ledger(invitation)
        receipt = ledger.redeem(
            "invite-0001", raw_token=raw_token, verified_identity=base_verified_identity(),
            requested_scopes=["child.read"], now=NOW)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(1, ledger.membership_count)
        self.assertNotIn("token", contract.canonical_json(receipt).lower())
        self.assert_code(
            "invite_consumed", ledger.redeem, "invite-0001", raw_token=raw_token,
            verified_identity=base_verified_identity(),
            requested_scopes=["child.read"], now=NOW)

    def test_invitation_wrong_identity_scope_token_and_expiry_fail(self):
        raw_token = "synthetic-invite-token-0000000000000001"
        invitation = base_invitation("invite-0002", raw_token)
        ledger = invitation_ledger(invitation)
        self.assert_code(
            "invite_subject_mismatch", ledger.redeem, "invite-0002", raw_token=raw_token,
            verified_identity=base_verified_identity(subject="oidc:subject-two"),
            requested_scopes=["child.read"], now=NOW)
        self.assert_code(
            "invite_subject_mismatch", ledger.redeem, "invite-0002", raw_token=raw_token,
            verified_identity=base_verified_identity(issuer="https://other-idp.example.test"),
            requested_scopes=["child.read"], now=NOW)
        self.assert_code(
            "invite_scope_denied", ledger.redeem, "invite-0002", raw_token=raw_token,
            verified_identity=base_verified_identity(),
            requested_scopes=["child.append"], now=NOW)
        self.assert_code(
            "invalid_invite", ledger.redeem, "invite-0002",
            raw_token="synthetic-wrong-invite-token-0000000000000002",
            verified_identity=base_verified_identity(),
            requested_scopes=["child.read"], now=NOW)
        expired = copy.deepcopy(invitation)
        expired["expires_at"] = "2026-09-14T00:59:59Z"
        expired_ledger = invitation_ledger(expired)
        self.assert_code(
            "invite_expired", expired_ledger.redeem, "invite-0002", raw_token=raw_token,
            verified_identity=base_verified_identity(),
            requested_scopes=["child.read"], now=NOW)

    def test_invitation_policy_version_and_plane_fail_closed(self):
        raw_token = "synthetic-policy-invite-token-0000000000000001"
        invitation = base_invitation("invite-policy-0001", raw_token)
        stale_policy = base_policy()
        stale_policy["policy_generation"] = 12
        ledger = invitation_ledger(invitation, stale_policy)
        self.assert_code(
            "stale_invitation_policy", ledger.redeem, "invite-policy-0001",
            raw_token=raw_token, verified_identity=base_verified_identity(),
            requested_scopes=["child.read"], now=NOW)

        privileged = copy.deepcopy(invitation)
        privileged["invite_id"] = "invite-policy-0002"
        privileged["max_scopes"] = ["control.child.suspend"]
        privileged_ledger = invitation_ledger(privileged)
        self.assert_code(
            "invite_scope_denied", privileged_ledger.redeem, "invite-policy-0002",
            raw_token=raw_token, verified_identity=base_verified_identity(),
            requested_scopes=["control.child.suspend"], now=NOW)

    def test_invitation_cannot_be_forged_or_cross_policy_scope(self):
        raw_token = "synthetic-stored-invite-token-0000000000000001"
        invitation = base_invitation("invite-stored-0001", raw_token, ["child.append"])
        ledger = invitation_ledger(invitation)
        self.assert_code(
            "invalid_invite", ledger.redeem, "invite-forged-9999", raw_token=raw_token,
            verified_identity=base_verified_identity(),
            requested_scopes=["child.append"], now=NOW)

        foreign_policy = base_policy()
        foreign_policy["tenant_id"] = "tenant-bravo"
        self.assert_code(
            "invalid_invite", invitation_ledger, invitation, foreign_policy)
        suspended_policy = base_policy()
        suspended_policy["state"] = "SUSPENDED"
        suspended_ledger = invitation_ledger(invitation, suspended_policy)
        self.assert_code(
            "child_suspended", suspended_ledger.redeem, "invite-stored-0001", raw_token=raw_token,
            verified_identity=base_verified_identity(),
            requested_scopes=["child.append"], now=NOW)
        updated_ledger = invitation_ledger(invitation)
        suspended_policy["policy_generation"] = 12
        updated_ledger.replace_policy(suspended_policy)
        self.assert_code(
            "child_suspended", updated_ledger.redeem, "invite-stored-0001",
            raw_token=raw_token, verified_identity=base_verified_identity(),
            requested_scopes=["child.append"], now=NOW)
        self.assert_code(
            "stale_policy_generation", updated_ledger.replace_policy, base_policy())
        same_generation = copy.deepcopy(suspended_policy)
        same_generation["state"] = "ACTIVE"
        self.assert_code(
            "stale_policy_generation", updated_ledger.replace_policy, same_generation)
        rollback = copy.deepcopy(suspended_policy)
        rollback["policy_generation"] = 13
        rollback["credential_epoch"] = 6
        self.assert_code("policy_rollback", updated_ledger.replace_policy, rollback)

        narrowed_policy = base_policy()
        narrowed_policy["child_allowed_operations"].remove("child.append")
        narrowed_ledger = invitation_ledger(invitation, narrowed_policy)
        self.assert_code(
            "invite_scope_denied", narrowed_ledger.redeem, "invite-stored-0001", raw_token=raw_token,
            verified_identity=base_verified_identity(),
            requested_scopes=["child.append"], now=NOW)

        invitation["tenant_id"] = "tenant-bravo"
        receipt = ledger.redeem(
            "invite-stored-0001", raw_token=raw_token,
            verified_identity=base_verified_identity(),
            requested_scopes=["child.append"], now=NOW)
        self.assertEqual("tenant-alpha", receipt["tenant_id"])
        self.assertEqual(1, ledger.membership_count)

    def test_one_of_one_hundred_concurrent_invite_redemptions_wins(self):
        raw_token = "synthetic-concurrent-invite-0000000000000001"
        invitation = base_invitation("invite-race-0001", raw_token)
        ledger = invitation_ledger(invitation)

        def redeem(_):
            try:
                ledger.redeem(
                    "invite-race-0001", raw_token=raw_token,
                    verified_identity=base_verified_identity(),
                    requested_scopes=["child.read"], now=NOW)
                return "accepted"
            except contract.ContractError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(redeem, range(100)))
        self.assertEqual(1, outcomes.count("accepted"))
        self.assertEqual(99, outcomes.count("invite_consumed"))
        self.assertEqual(1, ledger.membership_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)

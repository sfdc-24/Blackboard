#!/usr/bin/env python3
"""Offline mother-board / child-board authorization contract.

This module is deliberately not wired into ``bus_server.py``.  It is a small,
stdlib-only reference gate for the next multi-tenant bus surface.  Its purpose
is to make the proposed trust boundary executable before anyone provisions a
database or points a live client at it.

The caller must supply a principal produced by a *verified* identity layer and
the current server-side policy.  Tenant, child, actor, session, security
version, credential epoch, scope, TTL, payload, idempotency and fencing are then
checked as one fail-closed decision.  A request body never creates authority.

Passing this module's tests proves only the contract and its negative controls.
It does not prove deployed authentication, PostgreSQL row-level security,
customer isolation, queue delivery, backup/restore, or destination read-back.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Tuple


COMMAND_SCHEMA = "blackboard.mother-child.command.v1"
ADMISSION_RECEIPT_SCHEMA = "blackboard.mother-child.admission-receipt.v1"
INVITATION_SCHEMA = "blackboard.mother-child.invitation.v1"
MEMBERSHIP_RECEIPT_SCHEMA = "blackboard.mother-child.membership-receipt.v1"

ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
OPAQUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{2,191}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
RESOURCE_RE = re.compile(
    r"^bb://tenant/(?P<tenant>[a-z][a-z0-9-]{2,62})/"
    r"child/(?P<child>[a-z][a-z0-9-]{2,62})/"
    r"(?P<kind>[a-z][a-z0-9-]{1,31})/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)

COMMAND_FIELDS = {
    "schema", "request_id", "plane", "operation", "tenant_id", "child_id",
    "actor_id", "session_id", "resource_id", "work_id", "attempt_id",
    "idempotency_key", "issued_at", "expires_at", "security_version",
    "credential_epoch", "policy_generation", "payload", "fence",
}
COMMAND_REQUIRED = COMMAND_FIELDS - {"fence", "work_id", "attempt_id"}
PRINCIPAL_FIELDS = {
    "principal_id", "session_id", "plane", "tenant_id", "child_ids",
    "scopes", "security_version", "credential_epoch", "expires_at",
}
POLICY_FIELDS = {
    "tenant_id", "child_id", "state", "security_version", "credential_epoch",
    "policy_generation", "parent_quota_ceilings", "child_quota_limits",
    "parent_allowed_operations", "child_allowed_operations",
    "max_command_ttl_seconds", "max_clock_skew_seconds", "max_payload_bytes",
}
VERIFIED_IDENTITY_FIELDS = {"issuer", "subject"}


@dataclass(frozen=True)
class Operation:
    plane: str
    mutation: bool
    resource_kinds: frozenset[str]
    needs_fence: bool = False
    negative_authority: bool = False
    control_payload_fields: Optional[frozenset[str]] = None


OPERATIONS: Dict[str, Operation] = {
    # Child data plane.  A mother/control principal cannot exercise these even
    # when a mistakenly broad scope is present in its identity token.
    "child.read": Operation("child", False, frozenset({"board", "event", "work"})),
    "child.append": Operation("child", True, frozenset({"board"})),
    "child.event": Operation("child", True, frozenset({"event"})),
    "work.claim": Operation("child", True, frozenset({"work"})),
    "work.transition": Operation("child", True, frozenset({"work"}), needs_fence=True),
    # Consequential effects intentionally have no executable operation in this
    # post-auth admission reference.  They require the durable effect-intent and
    # ambiguous-outcome protocol specified in the architecture document.
    # The mother board has negative/ceiling authority, not ordinary content
    # access.  The strict payload allowlists prevent smuggling child data into a
    # superficially administrative operation.
    "control.child.suspend": Operation(
        "control", True, frozenset({"control"}), negative_authority=True,
        control_payload_fields=frozenset({"reason", "ticket"}),
    ),
    "control.child.lower-quota": Operation(
        "control", True, frozenset({"control"}), negative_authority=True,
        control_payload_fields=frozenset({"quota_name", "new_limit", "reason", "ticket"}),
    ),
    "control.child.rotate-epoch": Operation(
        "control", True, frozenset({"control"}), negative_authority=True,
        control_payload_fields=frozenset({"new_epoch", "reason", "ticket"}),
    ),
}

SENSITIVE_FIELD_NAMES = {
    "authorization", "api_key", "apikey", "access_token", "refresh_token",
    "auth_token", "bearer", "client_secret", "cookie", "credential",
    "credentials", "id_token", "jwt", "keystore", "oauth_token", "password",
    "private_key", "secret", "session_token", "set_cookie", "signing_key", "token",
}


class ContractError(ValueError):
    """Fail-closed contract rejection with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Admission:
    receipt: Mapping[str, Any]
    replayed: bool


def _fail(code: str, message: str) -> None:
    raise ContractError(code, message)


def _require_exact_fields(value: Mapping[str, Any], allowed: set[str], required: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        _fail("invalid_shape", f"{label} must be an object")
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        _fail("unknown_field", f"{label} has unknown field(s): {', '.join(unknown)}")
    if missing:
        _fail("missing_field", f"{label} is missing field(s): {', '.join(missing)}")


def _require_string(value: Any, field: str, pattern: re.Pattern[str] = OPAQUE_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail("invalid_field", f"{field} has an invalid format")
    return value


def _require_positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("invalid_field", f"{field} must be an integer >= {minimum}")
    return value


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        _fail("invalid_time", f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            _fail("invalid_time", f"{field} must include an offset")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        _fail("invalid_time", f"{field} must be a valid ISO-8601 time")


def _utc_now(now: Optional[datetime]) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        _fail("invalid_time", "now must be timezone-aware")
    return value.astimezone(timezone.utc)


def parse_json_object(raw: bytes, *, max_bytes: int = 64 * 1024) -> Mapping[str, Any]:
    """Parse strict UTF-8 JSON without duplicate keys or non-finite numbers.

    A normal ``json.loads`` call silently keeps the last duplicate key.  That is
    unsafe at an authorization boundary when a proxy, signer and policy engine
    might each select a different occurrence.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
        _fail("invalid_json", "request must be bounded, non-empty JSON bytes")

    def pairs(values: list[tuple[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in values:
            if key in out:
                _fail("duplicate_field", f"JSON field {key!r} is duplicated")
            out[key] = value
        return out

    def non_finite(value: str) -> None:
        _fail("invalid_json", f"non-finite JSON number {value!r} is forbidden")

    try:
        decoded = raw.decode("utf-8", errors="strict")
        parsed = json.loads(decoded, object_pairs_hook=pairs, parse_constant=non_finite)
    except ContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _fail("invalid_json", "request must be strict UTF-8 JSON")
    if not isinstance(parsed, Mapping):
        _fail("invalid_shape", "top-level JSON must be an object")
    # This also rejects numeric overflow (for example 1e400) and escaped lone
    # surrogates that Python's JSON decoder can otherwise represent in memory.
    canonical_json(parsed)
    return parsed


def _validate_json_tree(value: Any, *, max_depth: int = 32, max_nodes: int = 10_000) -> None:
    """Bound JSON structure and reject Python-only or ambiguous values."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            _fail("invalid_json", "JSON structure exceeds depth or item limits")
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    _fail("invalid_json", "JSON object keys must be strings")
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                _fail("invalid_json", "JSON strings must have a valid UTF-8 representation")
        elif isinstance(item, float):
            if not math.isfinite(item):
                _fail("invalid_json", "non-finite JSON numbers are forbidden")
        elif item is None or isinstance(item, (bool, int)):
            continue
        else:
            _fail("invalid_json", "value contains a non-JSON type")


def canonical_json(value: Any) -> str:
    """Stable JSON used for request hashes and byte-limit decisions."""
    _validate_json_tree(value)
    try:
        serialized = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
        # Reject lone surrogate code points.  They have no valid UTF-8 wire
        # representation and otherwise escape as UnicodeEncodeError later.
        serialized.encode("utf-8", errors="strict")
        return serialized
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        _fail("invalid_json", "value is not canonical JSON")


def request_hash(command: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(command).encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    if not isinstance(token, str) or not 16 <= len(token) <= 4096:
        _fail("invalid_token", "token must be an opaque value of 16 to 4096 characters")
    try:
        encoded = token.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail("invalid_token", "token must have a valid UTF-8 representation")
    return hashlib.sha256(encoded).hexdigest()


def _contains_sensitive_key(value: Any) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                acronym_split = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", key)
                camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", acronym_split)
                normalized = re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")
                parts = set(normalized.split("_"))
                sensitive_component = bool(parts & {
                    "authorization", "cookie", "credential", "credentials", "jwt",
                    "keystore", "password", "secret", "token",
                })
                key_pair = {"api", "key"}.issubset(parts) or {"private", "key"}.issubset(parts)
                if normalized in SENSITIVE_FIELD_NAMES or sensitive_component or key_pair:
                    return True
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _validate_resource(resource_id: Any, tenant_id: str, child_id: str) -> Tuple[str, str]:
    resource = _require_string(resource_id, "resource_id")
    match = RESOURCE_RE.fullmatch(resource)
    if not match:
        _fail("invalid_resource", "resource_id must be a canonical bb:// tenant/child resource")
    if match.group("tenant") != tenant_id or match.group("child") != child_id:
        _fail("resource_scope_mismatch", "resource_id is outside the authenticated tenant/child")
    return resource, match.group("kind")


def _validate_operation_list(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if (not isinstance(value, list) or (nonempty and not value) or
            any(not isinstance(item, str) or item not in OPERATIONS for item in value)):
        _fail("invalid_policy" if label.startswith("policy.") else "invalid_principal",
              f"{label} must contain supported operations")
    if len(value) != len(set(value)):
        _fail("invalid_policy" if label.startswith("policy.") else "invalid_principal",
              f"{label} contains duplicates")
    return value


def _validate_quota_limits(value: Any, label: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        _fail("invalid_policy", f"{label} must be an object")
    for name, limit in value.items():
        if not isinstance(name, str) or not ID_RE.fullmatch(name):
            _fail("invalid_policy", f"{label} contains an invalid quota name")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            _fail("invalid_policy", f"{label} values must be non-negative integers")
    return value


def _validate_policy_document(policy: Mapping[str, Any]) -> Tuple[str, str]:
    """Validate a complete trusted policy snapshot before it can be stored."""
    _require_exact_fields(policy, POLICY_FIELDS, POLICY_FIELDS, "policy")
    tenant_id = _require_string(policy.get("tenant_id"), "policy.tenant_id", ID_RE)
    child_id = _require_string(policy.get("child_id"), "policy.child_id", ID_RE)
    if policy.get("state") not in {"ACTIVE", "SUSPENDED"}:
        _fail("invalid_policy", "policy.state is invalid")
    _require_positive_int(policy.get("security_version"), "policy.security_version")
    _require_positive_int(policy.get("credential_epoch"), "policy.credential_epoch")
    _require_positive_int(policy.get("policy_generation"), "policy.policy_generation")
    _validate_operation_list(
        policy.get("parent_allowed_operations"), "policy.parent_allowed_operations")
    _validate_operation_list(
        policy.get("child_allowed_operations"), "policy.child_allowed_operations")
    parent = _validate_quota_limits(
        policy.get("parent_quota_ceilings"), "policy.parent_quota_ceilings")
    child = _validate_quota_limits(
        policy.get("child_quota_limits"), "policy.child_quota_limits")
    if set(parent) != set(child) or any(child[name] > parent[name] for name in parent):
        _fail("invalid_policy", "child quota policy must remain within the parent ceiling")
    _require_positive_int(
        policy.get("max_command_ttl_seconds"), "policy.max_command_ttl_seconds")
    _require_positive_int(
        policy.get("max_clock_skew_seconds"), "policy.max_clock_skew_seconds",
        allow_zero=True,
    )
    _require_positive_int(policy.get("max_payload_bytes"), "policy.max_payload_bytes")
    return tenant_id, child_id


def _validate_fence(command: Mapping[str, Any], operation: Operation,
                    current_fence: Optional[Mapping[str, Any]], *,
                    principal_id: str, current_time: datetime) -> None:
    supplied = command.get("fence")
    if not operation.needs_fence:
        if "fence" in command:
            _fail("unexpected_fence", "this operation must not carry a fence")
        return
    supplied_fields = {"claim_id", "generation", "token", "work_version", "child_incarnation"}
    if not isinstance(supplied, Mapping) or set(supplied) != supplied_fields:
        _fail("fence_required", "the complete current work fence is required")
    claim_id = _require_string(supplied.get("claim_id"), "fence.claim_id")
    generation = _require_positive_int(supplied.get("generation"), "fence.generation")
    work_version = _require_positive_int(supplied.get("work_version"), "fence.work_version")
    child_incarnation = _require_positive_int(
        supplied.get("child_incarnation"), "fence.child_incarnation")
    raw_token = supplied.get("token")
    current_fields = {
        "tenant_id", "child_id", "work_id", "attempt_id", "claim_id", "holder_principal_id",
        "generation", "token_hash", "lease_until", "work_version", "child_incarnation",
    }
    if not isinstance(current_fence, Mapping) or set(current_fence) != current_fields:
        _fail("fence_unavailable", "server has no complete authoritative work head")
    bound_strings = {
        "tenant_id": command.get("tenant_id"),
        "child_id": command.get("child_id"),
        "work_id": command.get("work_id"),
        "attempt_id": command.get("attempt_id"),
        "claim_id": claim_id,
        "holder_principal_id": principal_id,
    }
    for field, expected in bound_strings.items():
        actual = _require_string(
            current_fence.get(field), f"current_fence.{field}",
            ID_RE if field in {"tenant_id", "child_id"} else OPAQUE_RE,
        )
        if actual != expected:
            _fail("stale_fence", f"work fence is not bound to the current {field}")
    current_generation = _require_positive_int(current_fence.get("generation"), "current_fence.generation")
    current_work_version = _require_positive_int(
        current_fence.get("work_version"), "current_fence.work_version")
    current_child_incarnation = _require_positive_int(
        current_fence.get("child_incarnation"), "current_fence.child_incarnation")
    expected_hash = _require_string(
        current_fence.get("token_hash"), "current_fence.token_hash", SHA256_RE)
    lease_until = _parse_time(current_fence.get("lease_until"), "current_fence.lease_until")
    if lease_until <= current_time:
        _fail("stale_fence", "work fence lease is expired")
    if (generation != current_generation or work_version != current_work_version or
            child_incarnation != current_child_incarnation):
        _fail("stale_fence", "work fence generation, version or incarnation is not current")
    if not isinstance(raw_token, str) or not hmac.compare_digest(token_hash(raw_token), expected_hash):
        _fail("stale_fence", "fence token is not current")


def _validate_control_payload(operation_name: str, payload: Mapping[str, Any]) -> None:
    """Type-check the small control metadata vocabulary.

    Parent control is intentionally not a generic JSON tunnel into the child.
    """
    reason = payload.get("reason")
    ticket = payload.get("ticket")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        _fail("invalid_control_payload", "control reason must be a non-empty bounded string")
    if not isinstance(ticket, str) or not OPAQUE_RE.fullmatch(ticket):
        _fail("invalid_control_payload", "control ticket must be a stable opaque identifier")
    if operation_name == "control.child.lower-quota":
        _require_string(payload.get("quota_name"), "payload.quota_name", ID_RE)
        _require_positive_int(payload.get("new_limit"), "payload.new_limit", allow_zero=True)
    elif operation_name == "control.child.rotate-epoch":
        _require_positive_int(payload.get("new_epoch"), "payload.new_epoch")


def authorize(command: Mapping[str, Any], principal: Mapping[str, Any],
              policy: Mapping[str, Any], *, current_fence: Optional[Mapping[str, Any]] = None,
              now: Optional[datetime] = None) -> Mapping[str, Any]:
    """Validate and authorize one command without performing an effect.

    ``principal`` is trusted only as the output of an upstream signature,
    issuer, audience and session validation step.  This function deliberately
    compares every request scope back to that principal and the current policy.
    """
    _require_exact_fields(command, COMMAND_FIELDS, COMMAND_REQUIRED, "command")
    _require_exact_fields(principal, PRINCIPAL_FIELDS, PRINCIPAL_FIELDS, "principal")
    _require_exact_fields(policy, POLICY_FIELDS, POLICY_FIELDS, "policy")

    if command.get("schema") != COMMAND_SCHEMA:
        _fail("unsupported_schema", "command schema is not supported")

    request_id = _require_string(command.get("request_id"), "request_id")
    tenant_id = _require_string(command.get("tenant_id"), "tenant_id", ID_RE)
    child_id = _require_string(command.get("child_id"), "child_id", ID_RE)
    actor_id = _require_string(command.get("actor_id"), "actor_id")
    session_id = _require_string(command.get("session_id"), "session_id")
    _require_string(command.get("idempotency_key"), "idempotency_key")
    resource_id, resource_kind = _validate_resource(
        command.get("resource_id"), tenant_id, child_id)

    operation_name = _require_string(command.get("operation"), "operation")
    operation = OPERATIONS.get(operation_name)
    if operation is None:
        _fail("unsupported_operation", "operation is not supported")
    if command.get("plane") != operation.plane:
        _fail("plane_mismatch", "operation is not valid on the requested plane")
    if resource_kind not in operation.resource_kinds:
        _fail("operation_resource_mismatch", "operation is not valid for the resource kind")
    work_id: Optional[str] = None
    attempt_id: Optional[str] = None
    if operation_name in {"work.claim", "work.transition"}:
        work_id = _require_string(command.get("work_id"), "work_id")
        attempt_id = _require_string(command.get("attempt_id"), "attempt_id")
        expected_resource = f"bb://tenant/{tenant_id}/child/{child_id}/work/{work_id}"
        if resource_id != expected_resource:
            _fail("operation_resource_mismatch", "work operation must target its canonical work resource")
    else:
        if command.get("work_id") is not None:
            work_id = _require_string(command.get("work_id"), "work_id")
        if command.get("attempt_id") is not None:
            attempt_id = _require_string(command.get("attempt_id"), "attempt_id")
    if operation.negative_authority:
        expected_resource = f"bb://tenant/{tenant_id}/child/{child_id}/control/{child_id}"
        if resource_id != expected_resource:
            _fail("operation_resource_mismatch", "control operation must target its child-control resource")
        if work_id is not None or attempt_id is not None:
            _fail("unexpected_context", "control operation must not carry work or attempt context")

    principal_id = _require_string(principal.get("principal_id"), "principal.principal_id")
    principal_session = _require_string(principal.get("session_id"), "principal.session_id")
    principal_tenant = _require_string(principal.get("tenant_id"), "principal.tenant_id", ID_RE)
    child_ids = principal.get("child_ids")
    scopes = principal.get("scopes")
    if not isinstance(child_ids, list) or not child_ids or any(
            not isinstance(item, str) or not ID_RE.fullmatch(item) for item in child_ids):
        _fail("invalid_principal", "principal.child_ids must be a non-empty list of child IDs")
    if len(child_ids) != len(set(child_ids)):
        _fail("invalid_principal", "principal.child_ids contains duplicates")
    scopes = _validate_operation_list(scopes, "principal.scopes", nonempty=True)

    # These comparisons are the core confused-deputy/IDOR boundary.  The
    # request does not get to choose a different identity, tenant or child.
    if actor_id != principal_id or session_id != principal_session:
        _fail("identity_mismatch", "request actor/session is not the authenticated principal")
    if tenant_id != principal_tenant or child_id not in child_ids:
        _fail("tenant_scope_mismatch", "request is outside the authenticated tenant/child")
    if principal.get("plane") not in {"control", "child", "effect"}:
        _fail("invalid_principal", "principal.plane is invalid")
    if principal.get("plane") != operation.plane:
        _fail("principal_plane_mismatch", "principal cannot cross control, child or effect planes")
    if operation_name not in scopes:
        _fail("scope_denied", "principal scope does not allow the operation")

    policy_tenant = _require_string(policy.get("tenant_id"), "policy.tenant_id", ID_RE)
    policy_child = _require_string(policy.get("child_id"), "policy.child_id", ID_RE)
    if (tenant_id, child_id) != (policy_tenant, policy_child):
        _fail("policy_scope_mismatch", "policy is not for the request tenant/child")
    policy_state = policy.get("state")
    if policy_state not in {"ACTIVE", "SUSPENDED"}:
        _fail("invalid_policy", "policy.state is invalid")
    if policy_state != "ACTIVE" and not operation.negative_authority:
        _fail("child_suspended", "tenant or child is not active")

    security_version = _require_positive_int(command.get("security_version"), "security_version")
    credential_epoch = _require_positive_int(command.get("credential_epoch"), "credential_epoch")
    policy_generation = _require_positive_int(command.get("policy_generation"), "policy_generation")
    principal_security_version = _require_positive_int(
        principal.get("security_version"), "principal.security_version")
    principal_credential_epoch = _require_positive_int(
        principal.get("credential_epoch"), "principal.credential_epoch")
    current_security_version = _require_positive_int(policy.get("security_version"), "policy.security_version")
    current_credential_epoch = _require_positive_int(policy.get("credential_epoch"), "policy.credential_epoch")
    current_policy_generation = _require_positive_int(
        policy.get("policy_generation"), "policy.policy_generation")
    if security_version != current_security_version or principal_security_version != current_security_version:
        _fail("stale_security_version", "membership/policy security version is stale")
    if credential_epoch != current_credential_epoch or principal_credential_epoch != current_credential_epoch:
        _fail("stale_credential_epoch", "credential epoch is stale")
    if policy_generation != current_policy_generation:
        _fail("stale_policy_generation", "command policy generation is stale")

    parent_allowed = _validate_operation_list(
        policy.get("parent_allowed_operations"), "policy.parent_allowed_operations")
    child_allowed = _validate_operation_list(
        policy.get("child_allowed_operations"), "policy.child_allowed_operations")
    parent_quota_ceilings = _validate_quota_limits(
        policy.get("parent_quota_ceilings"), "policy.parent_quota_ceilings")
    child_quota_limits = _validate_quota_limits(
        policy.get("child_quota_limits"), "policy.child_quota_limits")
    if set(parent_quota_ceilings) != set(child_quota_limits):
        _fail("invalid_policy", "parent and child quota policy must cover the same metrics")
    if any(child_quota_limits[name] > parent_quota_ceilings[name]
           for name in parent_quota_ceilings):
        _fail("invalid_policy", "child quota cannot exceed its parent ceiling")
    if operation_name not in parent_allowed:
        _fail("parent_policy_denied", "parent ceiling denies the operation")
    if not operation.negative_authority and operation_name not in child_allowed:
        _fail("child_policy_denied", "child grant denies the operation")

    current_time = _utc_now(now)
    issued_at = _parse_time(command.get("issued_at"), "issued_at")
    expires_at = _parse_time(command.get("expires_at"), "expires_at")
    principal_expires = _parse_time(principal.get("expires_at"), "principal.expires_at")
    max_ttl = _require_positive_int(policy.get("max_command_ttl_seconds"), "policy.max_command_ttl_seconds")
    max_skew = _require_positive_int(
        policy.get("max_clock_skew_seconds"), "policy.max_clock_skew_seconds", allow_zero=True)
    if (expires_at - issued_at).total_seconds() <= 0 or (expires_at - issued_at).total_seconds() > max_ttl:
        _fail("invalid_ttl", "command lifetime exceeds the policy ceiling")
    if issued_at.timestamp() > current_time.timestamp() + max_skew:
        _fail("future_command", "command issue time is beyond allowed clock skew")
    if expires_at <= current_time or principal_expires <= current_time:
        _fail("expired", "command or authenticated session is expired")
    if expires_at > principal_expires:
        _fail("session_ttl_exceeded", "command may not outlive the authenticated session")

    payload = command.get("payload")
    if not isinstance(payload, Mapping):
        _fail("invalid_payload", "payload must be an object")
    payload_size = len(canonical_json(payload).encode("utf-8"))
    max_payload = _require_positive_int(policy.get("max_payload_bytes"), "policy.max_payload_bytes")
    if payload_size > max_payload:
        _fail("payload_too_large", "payload exceeds the child policy byte limit")
    if _contains_sensitive_key(payload):
        _fail("secret_field_denied", "credential-shaped fields do not belong in the bus payload")
    if operation.control_payload_fields is not None:
        unknown_payload = sorted(set(payload) - set(operation.control_payload_fields))
        if unknown_payload:
            _fail("control_payload_denied", "control operation carries non-control data")
        _validate_control_payload(operation_name, payload)
        if operation_name == "control.child.lower-quota":
            quota_name = str(payload["quota_name"])
            if quota_name not in parent_quota_ceilings:
                _fail("unknown_quota", "quota is not present in current server-side policy")
            current_effective_limit = min(
                parent_quota_ceilings[quota_name], child_quota_limits[quota_name])
            if int(payload["new_limit"]) > current_effective_limit:
                _fail("quota_increase_denied", "negative authority cannot increase a quota")
        elif (operation_name == "control.child.rotate-epoch" and
              int(payload["new_epoch"]) != current_credential_epoch + 1):
            _fail("invalid_epoch_rotation", "credential epoch must advance exactly once")

    _validate_fence(
        command, operation, current_fence,
        principal_id=principal_id, current_time=current_time,
    )

    return {
        "request_id": request_id,
        "request_hash": request_hash(command),
        "tenant_id": tenant_id,
        "child_id": child_id,
        "principal_id": principal_id,
        "session_id": session_id,
        "resource_id": resource_id,
        "work_id": work_id,
        "attempt_id": attempt_id,
        "operation": operation_name,
        "mutation": operation.mutation,
        "security_version": current_security_version,
        "credential_epoch": current_credential_epoch,
        "policy_generation": current_policy_generation,
    }


class AdmissionLedger:
    """Thread-safe reference idempotency ledger for offline contract tests.

    Production must put the idempotency row, immutable event, work-head update
    and outbox row in one datastore transaction.  This in-memory class only
    demonstrates the key and replay semantics expected from that transaction.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: MutableMapping[Tuple[str, str, str, str, str], Tuple[str, Dict[str, Any]]] = {}

    def admit(self, command: Mapping[str, Any], principal: Mapping[str, Any],
              policy: Mapping[str, Any], *, current_fence: Optional[Mapping[str, Any]] = None,
              now: Optional[datetime] = None) -> Admission:
        current_time = _utc_now(now)
        decision = authorize(command, principal, policy, current_fence=current_fence, now=current_time)
        key = (
            decision["tenant_id"], decision["child_id"], decision["principal_id"],
            decision["operation"], str(command["idempotency_key"]),
        )
        digest = decision["request_hash"]
        with self._lock:
            previous = self._records.get(key)
            if previous:
                previous_digest, receipt = previous
                if not hmac.compare_digest(previous_digest, digest):
                    _fail("idempotency_conflict", "idempotency key was already used for different bytes")
                return Admission(deepcopy(receipt), replayed=True)
            receipt: Dict[str, Any] = {
                "schema": ADMISSION_RECEIPT_SCHEMA,
                "receipt_id": str(uuid.uuid4()),
                "server_time": current_time.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "request_id": decision["request_id"],
                "request_hash": digest,
                "tenant_id": decision["tenant_id"],
                "child_id": decision["child_id"],
                "principal_id": decision["principal_id"],
                "session_id": decision["session_id"],
                "resource_id": decision["resource_id"],
                "work_id": decision["work_id"],
                "attempt_id": decision["attempt_id"],
                "operation": decision["operation"],
                "security_version": decision["security_version"],
                "credential_epoch": decision["credential_epoch"],
                "policy_generation": decision["policy_generation"],
                "accepted": True,
                # Admission is intentionally not represented as delivery.
                "destination_readback": False,
            }
            self._records[key] = (digest, deepcopy(receipt))
            return Admission(receipt, replayed=False)

    @property
    def durable_operation_count(self) -> int:
        return len(self._records)


class InvitationLedger:
    """Atomic, one-time redemption against a trusted invitation store.

    The constructor represents the server-side datastore boundary.  ``redeem``
    accepts only an invitation ID; a request cannot supply or rewrite the
    authority-bearing invitation record.  This reference does not mint tokens,
    create live accounts, or replace an OIDC/SAML identity layer.
    """

    INVITE_FIELDS = {
        "schema", "invite_id", "tenant_id", "child_id", "intended_subject",
        "intended_issuer", "max_scopes", "policy_version", "token_hash", "expires_at",
    }

    def __init__(self, invitations: Iterable[Mapping[str, Any]],
                 policies: Iterable[Mapping[str, Any]]) -> None:
        self._lock = threading.Lock()
        self._invitations: Dict[str, Mapping[str, Any]] = {}
        self._policies: Dict[Tuple[str, str], Mapping[str, Any]] = {}
        self._consumed: set[str] = set()
        self._memberships: set[Tuple[str, str, str, str]] = set()
        try:
            trusted_invitations = list(invitations)
            trusted_policies = list(policies)
        except TypeError:
            _fail("invalid_invite", "trusted invitation and policy stores must be iterable")
        for policy in trusted_policies:
            _require_exact_fields(policy, POLICY_FIELDS, POLICY_FIELDS, "policy")
            snapshot = deepcopy(dict(policy))
            tenant_id, child_id = _validate_policy_document(snapshot)
            key = (tenant_id, child_id)
            if key in self._policies:
                _fail("invalid_policy", "trusted policy store contains a duplicate tenant/child")
            self._policies[key] = snapshot
        for invitation in trusted_invitations:
            _require_exact_fields(invitation, self.INVITE_FIELDS, self.INVITE_FIELDS, "invitation")
            snapshot = deepcopy(dict(invitation))
            if snapshot.get("schema") != INVITATION_SCHEMA:
                _fail("unsupported_schema", "invitation schema is not supported")
            invite_id = _require_string(snapshot.get("invite_id"), "invitation.invite_id")
            if invite_id in self._invitations:
                _fail("invalid_invite", "trusted invitation store contains a duplicate ID")
            tenant_id = _require_string(snapshot.get("tenant_id"), "invitation.tenant_id", ID_RE)
            child_id = _require_string(snapshot.get("child_id"), "invitation.child_id", ID_RE)
            if (tenant_id, child_id) not in self._policies:
                _fail("invalid_invite", "invitation has no authoritative tenant/child policy")
            self._invitations[invite_id] = snapshot

    def replace_policy(self, policy: Mapping[str, Any]) -> None:
        """Atomically replace trusted policy state for concurrency tests.

        An online implementation performs this update and invitation redemption
        under the datastore's row locks/serializable transaction, not via a
        request-accessible method.
        """
        _require_exact_fields(policy, POLICY_FIELDS, POLICY_FIELDS, "policy")
        snapshot = deepcopy(dict(policy))
        tenant_id, child_id = _validate_policy_document(snapshot)
        with self._lock:
            previous = self._policies.get((tenant_id, child_id))
            if previous is None:
                _fail("invalid_policy", "cannot replace a policy that is not registered")
            previous_generation = _require_positive_int(
                previous.get("policy_generation"), "policy.policy_generation")
            next_generation = _require_positive_int(
                snapshot.get("policy_generation"), "policy.policy_generation")
            if next_generation != previous_generation + 1:
                _fail("stale_policy_generation", "policy replacement must advance exactly once")
            for field in ("security_version", "credential_epoch"):
                previous_version = _require_positive_int(
                    previous.get(field), f"policy.{field}")
                next_version = _require_positive_int(snapshot.get(field), f"policy.{field}")
                if next_version < previous_version:
                    _fail("policy_rollback", f"policy replacement cannot decrease {field}")
            self._policies[(tenant_id, child_id)] = snapshot

    def redeem(self, invite_id: str, *, raw_token: str,
               verified_identity: Mapping[str, Any],
               requested_scopes: list[str],
               now: Optional[datetime] = None) -> Mapping[str, Any]:
        invite_id = _require_string(invite_id, "invite_id")
        _require_exact_fields(
            verified_identity, VERIFIED_IDENTITY_FIELDS, VERIFIED_IDENTITY_FIELDS,
            "verified_identity",
        )
        identity_snapshot = deepcopy(dict(verified_identity))
        if not isinstance(requested_scopes, list):
            _fail("invite_scope_denied", "requested scopes must be a list of child operations")
        requested = deepcopy(requested_scopes)
        with self._lock:
            invitation = self._invitations.get(invite_id)
            if invitation is None:
                _fail("invalid_invite", "invitation does not exist in the trusted store")
            _require_exact_fields(invitation, self.INVITE_FIELDS, self.INVITE_FIELDS, "invitation")
            tenant_id = _require_string(invitation.get("tenant_id"), "invitation.tenant_id", ID_RE)
            child_id = _require_string(invitation.get("child_id"), "invitation.child_id", ID_RE)
            current_policy = self._policies.get((tenant_id, child_id))
            if current_policy is None:
                _fail("invalid_policy", "authoritative invitation policy is unavailable")
            _require_exact_fields(current_policy, POLICY_FIELDS, POLICY_FIELDS, "policy")
            intended_issuer = _require_string(
                invitation.get("intended_issuer"), "invitation.intended_issuer")
            intended_subject = _require_string(
                invitation.get("intended_subject"), "invitation.intended_subject")
            issuer = _require_string(identity_snapshot.get("issuer"), "verified_identity.issuer")
            subject = _require_string(identity_snapshot.get("subject"), "verified_identity.subject")
            if (not hmac.compare_digest(intended_issuer, issuer) or
                    not hmac.compare_digest(intended_subject, subject)):
                _fail("invite_subject_mismatch", "authenticated identity is not the invited subject")
            # Length is only a format floor; entropy is a trusted provisioning
            # precondition and cannot be inferred from a presented string.
            if not isinstance(raw_token, str) or len(raw_token) < 32:
                _fail("invalid_invite", "invitation token must be at least 32 characters")
            expected_hash = _require_string(
                invitation.get("token_hash"), "invitation.token_hash", SHA256_RE)
            if not hmac.compare_digest(expected_hash, token_hash(raw_token)):
                _fail("invalid_invite", "invitation token is invalid")
            if current_policy.get("state") != "ACTIVE":
                _fail("child_suspended", "tenant or child is not active")
            policy_version = _require_positive_int(
                invitation.get("policy_version"), "invitation.policy_version")
            current_policy_version = _require_positive_int(
                current_policy.get("policy_generation"), "policy.policy_generation")
            if policy_version != current_policy_version:
                _fail("stale_invitation_policy", "invitation policy version is stale")
            parent_allowed = _validate_operation_list(
                current_policy.get("parent_allowed_operations"), "policy.parent_allowed_operations")
            child_allowed = _validate_operation_list(
                current_policy.get("child_allowed_operations"), "policy.child_allowed_operations")
            parent_quota_ceilings = _validate_quota_limits(
                current_policy.get("parent_quota_ceilings"), "policy.parent_quota_ceilings")
            child_quota_limits = _validate_quota_limits(
                current_policy.get("child_quota_limits"), "policy.child_quota_limits")
            if (set(parent_quota_ceilings) != set(child_quota_limits) or
                    any(child_quota_limits[name] > parent_quota_ceilings[name]
                        for name in parent_quota_ceilings)):
                _fail("invalid_policy", "current quota policy violates the parent ceiling")
            maximum = invitation.get("max_scopes")
            if (not isinstance(maximum, list) or not maximum or
                    any(not isinstance(item, str) or item not in OPERATIONS for item in maximum) or
                    len(maximum) != len(set(maximum))):
                _fail("invalid_invite", "max_scopes is invalid")
            if any(OPERATIONS[item].plane != "child" for item in maximum):
                _fail("invite_scope_denied", "ordinary child invitation cannot create control authority")
            if any(item not in parent_allowed or item not in child_allowed for item in maximum):
                _fail("invite_scope_denied", "invitation exceeds current parent or child policy")
            if (not requested or
                    any(not isinstance(item, str) or item not in maximum for item in requested) or
                    len(requested) != len(set(requested))):
                _fail("invite_scope_denied", "requested membership exceeds the invitation")
            membership = (tenant_id, child_id, issuer, subject)
            # Read the server clock at the commit boundary, after all bounded
            # validation and while authoritative invitation/policy state is locked.
            current_time = _utc_now(now)
            if _parse_time(invitation.get("expires_at"), "invitation.expires_at") <= current_time:
                _fail("invite_expired", "invitation is expired")
            if invite_id in self._consumed:
                _fail("invite_consumed", "invitation has already been consumed")
            if membership in self._memberships:
                _fail("membership_exists", "the invited membership already exists")
            self._consumed.add(invite_id)
            self._memberships.add(membership)
            return {
                "schema": MEMBERSHIP_RECEIPT_SCHEMA,
                "receipt_id": str(uuid.uuid4()),
                "invite_id": invite_id,
                "tenant_id": tenant_id,
                "child_id": child_id,
                "issuer": issuer,
                "subject": subject,
                "scopes": requested,
                "policy_version": policy_version,
                "accepted": True,
            }

    @property
    def membership_count(self) -> int:
        return len(self._memberships)

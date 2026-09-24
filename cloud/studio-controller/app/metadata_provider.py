"""Inert Salesforce Lead Text adapter contract over an injected transport.

There is no Salesforce client, credential lookup, route, setting, or deployment
wiring here. A future caller must inject a purpose-built transport whose only
public callables are the four operations named in ``TRANSPORT_METHODS``.

The write boundary is capability-based. A sealed execution budget is created
from the trusted ledger before preflight. The adapter records preflight through
that same ledger, invokes ``begin`` itself, re-reads the durable record, and
mints a non-serializable one-use permit only for a fresh, non-replayed begin.
Portable dictionaries and the ledger's historical dispatch boolean are never
accepted by the dispatch method.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field as dataclass_field
import json
import math
import re
import threading
import time

from .metadata_contract import MetadataContractError, validate_field
from .metadata_operations import (
    InvalidOperation,
    MetadataOperationLedger,
    validate_record,
)


MAX_MONOTONIC_BUDGET_SECONDS = 20.0
TRANSPORT_METHOD_ORDER = (
    "open_verified_session",
    "describe_lead_text",
    "create_lead_text_once",
    "close",
)
TRANSPORT_METHODS = frozenset(TRANSPORT_METHOD_ORDER)
SESSION_KEYS = frozenset({"org_id", "origin", "version", "session_handle"})
DESCRIBE_KEYS = frozenset({
    "org_id", "origin", "version", "member", "complete", "exists", "field",
})
ACK_KEYS = frozenset({"org_id", "origin", "version", "member", "field", "created"})
DISPATCH_KEYS = frozenset({"outcome", "definitive_no_change"})
PREFLIGHT_KEYS = frozenset({"org_binding_id", "member", "exists", "observed_at"})
OBSERVATION_KEYS = frozenset({"org_binding_id", "field", "observed_at"})

PREFLIGHT_UNAVAILABLE = "preflight_unavailable"
VERIFICATION_MISMATCH = "verification_mismatch"
VERIFICATION_UNAVAILABLE = "verification_unavailable"

_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_ORG_ID = re.compile(r"00D[A-Za-z0-9]{15}\Z")
_ORIGIN = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(develop|sandbox)\.my\.salesforce\.com\Z"
)
_VERSION = re.compile(r"v[1-9][0-9]{1,2}\.0\Z")
_SESSION_HANDLE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_BUDGET_KEY = object()
_PERMIT_KEY = object()


class MetadataProviderError(ValueError):
    """Closed-contract violation; messages never include provider data."""


class DispatchPermitError(MetadataProviderError):
    """A dispatch capability is absent, forged, duplicated, or consumed."""


@dataclass(frozen=True, slots=True, init=False)
class OrgBinding:
    """Immutable mapping from an opaque ledger binding to one exact org."""

    org_binding_id: str
    org_id: str
    origin: str
    version: str
    binding_version: int
    environment: str
    _sealed: bool = dataclass_field(init=False, repr=False, compare=False)

    def __init__(self, org_binding_id, org_id, origin, version,
                 binding_version, environment) -> None:
        if getattr(self, "_sealed", False):
            raise MetadataProviderError("organization bindings cannot be reinitialized")
        # Validate every value before assigning any slot so construction either
        # produces one complete sealed value or leaves the fresh object empty.
        if type(org_binding_id) is not str or not _OPAQUE_ID.fullmatch(org_binding_id):
            raise MetadataProviderError("a bounded opaque organization binding ID is required")
        if org_binding_id == org_id:
            raise MetadataProviderError("the opaque binding ID must not be the Salesforce organization ID")
        if type(org_id) is not str or not _ORG_ID.fullmatch(org_id):
            raise MetadataProviderError("an exact 18-character Salesforce organization ID is required")
        match = _ORIGIN.fullmatch(origin) if type(origin) is str else None
        if match is None:
            raise MetadataProviderError("a canonical bare HTTPS developer or sandbox origin is required")
        expected_environment = "developer" if match.group(1) == "develop" else "sandbox"
        if type(environment) is not str or environment != expected_environment:
            raise MetadataProviderError("the environment must exactly match the pinned origin")
        if type(version) is not str or not _VERSION.fullmatch(version):
            raise MetadataProviderError("a pinned Salesforce API version is required")
        if type(binding_version) is not int or not 1 <= binding_version <= 2**31 - 1:
            raise MetadataProviderError("a positive immutable binding version is required")
        object.__setattr__(self, "org_binding_id", org_binding_id)
        object.__setattr__(self, "org_id", org_id)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "binding_version", binding_version)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "_sealed", True)

    def __repr__(self) -> str:
        # Actual org identity and origin are internal provider-routing data and
        # must not appear in generic exception/debug logging.
        return ("<OrgBinding sealed environment=%r binding_version=%d>"
                % (self.environment, self.binding_version))

    @classmethod
    def from_mapping(cls, value: dict) -> "OrgBinding":
        _closed(value, {
            "org_binding_id", "org_id", "origin", "version",
            "binding_version", "environment",
        }, "organization binding")
        return cls(**value)

    def as_mapping(self) -> dict:
        return {
            "org_binding_id": self.org_binding_id,
            "org_id": self.org_id,
            "origin": self.origin,
            "version": self.version,
            "binding_version": self.binding_version,
            "environment": self.environment,
        }


class ExecutionBudget:
    """Sealed, process-local, non-renewable capability for one operation."""

    __slots__ = (
        "_owner", "_ledger", "_ledger_store", "_binding", "_operation_id",
        "_plan_hash", "_field",
        "_member", "_plan_expires_at", "_deadline", "_clock", "_timestamp",
        "_last_mono", "_last_stamp", "_phase", "_cancelled", "_lock",
        "_preflight_evidence", "_preflight_command", "_begin_command",
        "_dispatch_result", "_dispatch_command", "_verification_result",
        "_verification_command", "_cancellation", "_sealed",
    )

    def __init__(self, key, *, owner, ledger, binding, snapshot, field, member,
                 deadline, clock, timestamp) -> None:
        if getattr(self, "_sealed", False):
            raise MetadataProviderError("execution budgets cannot be reinitialized")
        if key is not _BUDGET_KEY:
            raise MetadataProviderError("execution budgets may only be issued by the adapter")
        mono = _sample_monotonic(clock)
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise MetadataProviderError("an absolute finite monotonic deadline is required")
        remaining = float(deadline) - mono
        if remaining <= 0 or remaining > MAX_MONOTONIC_BUDGET_SECONDS:
            raise MetadataProviderError("the monotonic execution budget must be greater than zero and at most 20 seconds")
        stamp = _sample_timestamp(timestamp)
        if stamp >= snapshot["plan"]["expires_at"]:
            raise MetadataProviderError("the execution plan is already expired")

        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_ledger", ledger)
        object.__setattr__(self, "_ledger_store", ledger.store)
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_operation_id", snapshot["plan"]["operation_id"])
        object.__setattr__(self, "_plan_hash", snapshot["plan_hash"])
        object.__setattr__(self, "_field", _pack(field))
        object.__setattr__(self, "_member", member)
        object.__setattr__(self, "_plan_expires_at", snapshot["plan"]["expires_at"])
        object.__setattr__(self, "_deadline", float(deadline))
        object.__setattr__(self, "_clock", clock)
        object.__setattr__(self, "_timestamp", timestamp)
        object.__setattr__(self, "_last_mono", mono)
        object.__setattr__(self, "_last_stamp", stamp)
        object.__setattr__(self, "_phase", "created")
        object.__setattr__(self, "_cancelled", False)
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_preflight_evidence", None)
        object.__setattr__(self, "_preflight_command", None)
        object.__setattr__(self, "_begin_command", None)
        object.__setattr__(self, "_dispatch_result", None)
        object.__setattr__(self, "_dispatch_command", None)
        object.__setattr__(self, "_verification_result", None)
        object.__setattr__(self, "_verification_command", None)
        object.__setattr__(self, "_cancellation", _CancellationView(_BUDGET_KEY, self, owner))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("execution budgets are immutable capabilities")
        object.__setattr__(self, name, value)

    def __delattr__(self, name) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("execution budgets are immutable capabilities")
        object.__delattr__(self, name)

    def __repr__(self) -> str:
        return "<ExecutionBudget sealed>"

    def __copy__(self):
        raise TypeError("execution budgets cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("execution budgets cannot be copied")

    def __reduce__(self):
        raise TypeError("execution budgets cannot be serialized")

    def __reduce_ex__(self, protocol):
        raise TypeError("execution budgets cannot be serialized")

    def __getstate__(self):
        raise TypeError("execution budgets cannot be serialized")

    def _assert_owner(self, owner) -> None:
        if owner is not self._owner:
            raise MetadataProviderError("execution budget belongs to another adapter")

    def _assert_ledger(self, owner, ledger) -> None:
        self._assert_owner(owner)
        if (ledger is not self._ledger
                or getattr(ledger, "store", None) is not self._ledger_store):
            raise MetadataProviderError("execution budget belongs to another metadata ledger")

    def _sample(self, owner, *, allow_expired=False) -> tuple[bool, int]:
        self._assert_owner(owner)
        with self._lock:
            try:
                mono = _sample_monotonic(self._clock)
                stamp = _sample_timestamp(self._timestamp)
            except MetadataProviderError:
                return False, self._last_stamp
            if mono < self._last_mono or stamp < self._last_stamp:
                return False, self._last_stamp
            object.__setattr__(self, "_last_mono", mono)
            object.__setattr__(self, "_last_stamp", stamp)
            alive = (not self._cancelled and mono < self._deadline
                     and stamp < self._plan_expires_at)
            return (alive or allow_expired), stamp

    def _begin_phase(self, owner, expected: str, running: str) -> bool:
        self._assert_owner(owner)
        with self._lock:
            if self._phase != expected:
                raise MetadataProviderError("execution capability is in the wrong phase")
            try:
                mono = _sample_monotonic(self._clock)
                stamp = _sample_timestamp(self._timestamp)
            except MetadataProviderError:
                object.__setattr__(self, "_phase", "terminal")
                return False
            if (mono < self._last_mono or stamp < self._last_stamp or self._cancelled
                    or mono >= self._deadline or stamp >= self._plan_expires_at):
                object.__setattr__(self, "_phase", "terminal")
                return False
            object.__setattr__(self, "_last_mono", mono)
            object.__setattr__(self, "_last_stamp", stamp)
            object.__setattr__(self, "_phase", running)
            return True

    def _set_phase(self, owner, phase: str) -> None:
        self._assert_owner(owner)
        with self._lock:
            object.__setattr__(self, "_phase", phase)

    def _cancel(self, owner) -> None:
        self._assert_owner(owner)
        with self._lock:
            object.__setattr__(self, "_cancelled", True)

    def _cancelled_or_expired(self, owner) -> bool:
        self._assert_owner(owner)
        alive, _ = self._sample(owner)
        return not alive


class _CancellationView:
    """Narrow process-local signal passed to every transport call."""

    __slots__ = ("_budget", "_owner", "_sealed")

    def __init__(self, key, budget, owner) -> None:
        if getattr(self, "_sealed", False):
            raise MetadataProviderError("cancellation views cannot be reinitialized")
        if key is not _BUDGET_KEY:
            raise MetadataProviderError("cancellation views may only come from an execution budget")
        object.__setattr__(self, "_budget", budget)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("cancellation views are immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("cancellation views are immutable")
        object.__delattr__(self, name)

    def __call__(self) -> bool:
        return self._budget._cancelled_or_expired(self._owner)

    def __repr__(self) -> str:
        return "<CancellationView sealed>"

    def __copy__(self):
        raise TypeError("cancellation views cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("cancellation views cannot be copied")

    def __reduce__(self):
        raise TypeError("cancellation views cannot be serialized")

    def __reduce_ex__(self, protocol):
        raise TypeError("cancellation views cannot be serialized")

    def __getstate__(self):
        raise TypeError("cancellation views cannot be serialized")


class _DispatchPermit:
    """Opaque one-use write grant tied to one budget and durable begin."""

    __slots__ = ("_owner", "_budget", "_lock", "_consumed", "_sealed")

    def __init__(self, key, *, owner, budget) -> None:
        if getattr(self, "_sealed", False):
            raise DispatchPermitError("dispatch permits cannot be reinitialized")
        if key is not _PERMIT_KEY:
            raise DispatchPermitError("dispatch permits may only be minted by the ledger coordinator")
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_budget", budget)
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("dispatch permits are immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("dispatch permits are immutable")
        object.__delattr__(self, name)

    def __repr__(self) -> str:
        return "<DispatchPermit sealed>"

    def __copy__(self):
        raise TypeError("dispatch permits cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("dispatch permits cannot be copied")

    def __reduce__(self):
        raise TypeError("dispatch permits cannot be serialized")

    def __reduce_ex__(self, protocol):
        raise TypeError("dispatch permits cannot be serialized")

    def __getstate__(self):
        raise TypeError("dispatch permits cannot be serialized")

    def _consume(self, owner) -> ExecutionBudget:
        if owner is not self._owner:
            raise DispatchPermitError("dispatch permit belongs to another adapter")
        with self._lock:
            if self._consumed:
                raise DispatchPermitError("dispatch permit was already consumed")
            object.__setattr__(self, "_consumed", True)
        return self._budget


def _closed(value, keys, label) -> None:
    if type(value) is not dict or set(value) != keys:
        raise MetadataProviderError("invalid closed %s" % label)


def _pack(value) -> str:
    """Keep capability payloads immutable even through private attribute access."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _unpack(value: str):
    return json.loads(value)


def _sample_monotonic(clock) -> float:
    try:
        value = clock()
    except Exception:
        raise MetadataProviderError("the monotonic clock is unavailable") from None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise MetadataProviderError("the monotonic clock is invalid")
    return float(value)


def _sample_timestamp(timestamp) -> int:
    try:
        value = timestamp()
    except Exception:
        raise MetadataProviderError("the trusted timestamp source is unavailable") from None
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise MetadataProviderError("the trusted timestamp is invalid")
    return value


def _validated_field(value) -> dict:
    try:
        return validate_field(value)
    except MetadataContractError:
        raise MetadataProviderError("invalid Lead Text field") from None


def _canonical_member(field: dict) -> str:
    return "CustomField:Lead." + field["name"].lower() + "__c"


def _validate_transport(transport) -> tuple:
    try:
        if type(getattr(transport, "zero_retry_writes", None)) is not bool \
                or transport.zero_retry_writes is not True:
            raise MetadataProviderError("transport must declare zero-retry writes")
        if type(getattr(transport, "redirects_disabled", None)) is not bool \
                or transport.redirects_disabled is not True:
            raise MetadataProviderError("transport must declare redirects disabled")
        public_callables = set()
        for cls in type(transport).__mro__:
            for name, value in vars(cls).items():
                if not name.startswith("_") and callable(value):
                    public_callables.add(name)
        for name, value in getattr(transport, "__dict__", {}).items():
            if not name.startswith("_") and callable(value):
                public_callables.add(name)
        if public_callables != TRANSPORT_METHODS:
            raise MetadataProviderError("transport exposes an unsupported callable surface")
        resolved = tuple(getattr(transport, name) for name in TRANSPORT_METHOD_ORDER)
        if not all(callable(value) for value in resolved):
            raise MetadataProviderError("transport provider callables are unavailable")
        return resolved
    except MetadataProviderError:
        raise
    except BaseException:
        raise MetadataProviderError("transport capability declarations are unavailable") from None


def _same_callable(current, pinned) -> bool:
    if current is pinned:
        return True
    pinned_function = getattr(pinned, "__func__", None)
    return (pinned_function is not None
            and getattr(current, "__self__", None) is getattr(pinned, "__self__", None)
            and getattr(current, "__func__", None) is pinned_function)


def _provider_identity(value: dict, binding: OrgBinding) -> bool:
    return (value["org_id"] == binding.org_id and value["origin"] == binding.origin
            and value["version"] == binding.version)


def _validate_session(value, binding: OrgBinding) -> dict:
    _closed(value, SESSION_KEYS, "verified session")
    if not _provider_identity(value, binding):
        raise MetadataProviderError("verified session binding mismatch")
    if type(value["session_handle"]) is not str or not _SESSION_HANDLE.fullmatch(value["session_handle"]):
        raise MetadataProviderError("invalid verified session handle")
    return copy.deepcopy(value)


def _validate_describe(value, binding: OrgBinding, member: str) -> dict:
    _closed(value, DESCRIBE_KEYS, "Lead Text description")
    if (not _provider_identity(value, binding) or value["member"] != member
            or type(value["complete"]) is not bool or value["complete"] is not True
            or type(value["exists"]) is not bool):
        raise MetadataProviderError("Lead Text description binding mismatch")
    if value["exists"] is False:
        if value["field"] is not None:
            raise MetadataProviderError("absent Lead Text description must not carry field data")
        return copy.deepcopy(value)
    result = copy.deepcopy(value)
    result["field"] = _validated_field(value["field"])
    return result


def _validate_ack(value, binding: OrgBinding, member: str, field: dict) -> None:
    _closed(value, ACK_KEYS, "create acknowledgement")
    if (not _provider_identity(value, binding) or value["member"] != member
            or type(value["created"]) is not bool or value["created"] is not True
            or _validated_field(value["field"]) != field):
        raise MetadataProviderError("create acknowledgement binding mismatch")


def _operation_snapshot(value, binding: OrgBinding, required_state: str) -> tuple[dict, dict, str]:
    if type(value) is not dict:
        raise MetadataProviderError("a closed durable operation snapshot is required")
    try:
        validate_record(value)
    except InvalidOperation:
        raise MetadataProviderError("invalid durable operation snapshot") from None
    snapshot = copy.deepcopy(value)
    if snapshot["state"] != required_state:
        raise MetadataProviderError("operation snapshot is in the wrong state")
    if snapshot["plan"]["binding"]["org_binding_id"] != binding.org_binding_id:
        raise MetadataProviderError("operation and opaque organization binding do not match")
    field = _validated_field(snapshot["plan"]["field"])
    member = _canonical_member(field)
    preflight = snapshot["preflight"]
    if preflight is not None and (
            type(preflight) is not dict
            or preflight.get("org_binding_id") != binding.org_binding_id
            or preflight.get("member") != member):
        raise MetadataProviderError("operation preflight binding mismatch")
    return snapshot, field, member


def _dispatch(outcome: str) -> dict:
    values = {
        "accepted": {"outcome": "accepted", "definitive_no_change": False},
        "rejected": {"outcome": "rejected_no_change", "definitive_no_change": True},
        "ambiguous": {"outcome": "ambiguous", "definitive_no_change": False},
    }
    return copy.deepcopy(values[outcome])


class SalesforceMetadataAdapter:
    """Server-internal issuer/coordinator over one exact ledger and transport."""

    __slots__ = (
        "binding", "_transport", "_transport_callables", "_clock",
        "_trusted_timestamp", "_owner", "_budget_lock",
        "_budgeted_operations", "_sealed",
    )

    def __init__(self, binding: OrgBinding, transport, *, clock=time.monotonic,
                 trusted_timestamp) -> None:
        if getattr(self, "_sealed", False):
            raise MetadataProviderError("metadata adapters cannot be reinitialized")
        if type(binding) is not OrgBinding:
            raise MetadataProviderError("an immutable closed organization binding is required")
        if not callable(clock) or not callable(trusted_timestamp):
            raise MetadataProviderError("monotonic and trusted timestamp callables are required")
        resolved_transport = _validate_transport(transport)
        self.binding = binding
        self._transport = transport
        self._transport_callables = resolved_transport
        self._clock = clock
        self._trusted_timestamp = trusted_timestamp
        self._owner = object()
        self._budget_lock = threading.Lock()
        self._budgeted_operations = frozenset()
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("metadata adapters are immutable issuer capabilities")
        object.__setattr__(self, name, value)

    def __delattr__(self, name) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("metadata adapters are immutable issuer capabilities")
        object.__delattr__(self, name)

    @staticmethod
    def _ledger_snapshot(ledger, operation_id, actor) -> dict:
        if type(ledger) is not MetadataOperationLedger:
            raise MetadataProviderError("the exact reviewed metadata ledger is required")
        try:
            return ledger.execution_snapshot(operation_id, actor)
        except (InvalidOperation, RuntimeError):
            raise MetadataProviderError("durable operation snapshot is unavailable") from None

    def issue_execution_budget(self, ledger, operation_id, actor, *, deadline) -> ExecutionBudget:
        snapshot, field, member = _operation_snapshot(
            self._ledger_snapshot(ledger, operation_id, actor), self.binding, "confirmed"
        )
        if snapshot["preflight"] is not None or snapshot["provider_attempts"] != 0:
            raise MetadataProviderError("execution budget must be issued before provider preflight")
        identity = (operation_id, snapshot["plan_hash"])
        with self._budget_lock:
            if identity in self._budgeted_operations:
                raise MetadataProviderError("an execution budget was already issued for this plan")
            budget = ExecutionBudget(
                _BUDGET_KEY, owner=self._owner, ledger=ledger, binding=self.binding,
                snapshot=snapshot,
                field=field, member=member, deadline=deadline, clock=self._clock,
                timestamp=self._trusted_timestamp,
            )
            object.__setattr__(
                self, "_budgeted_operations",
                self._budgeted_operations.union((identity,)),
            )
        return budget

    def cancel_execution(self, budget: ExecutionBudget) -> None:
        if type(budget) is not ExecutionBudget:
            raise MetadataProviderError("a sealed execution budget is required")
        budget._cancel(self._owner)

    def _assert_budget(self, budget: ExecutionBudget) -> None:
        if type(budget) is not ExecutionBudget:
            raise MetadataProviderError("a sealed execution budget is required")
        budget._assert_owner(self._owner)
        if budget._binding != self.binding:
            raise MetadataProviderError("execution budget organization binding mismatch")

    def _assert_budget_ledger(self, budget: ExecutionBudget, ledger) -> None:
        self._assert_budget(budget)
        budget._assert_ledger(self._owner, ledger)

    def _assert_transport_unchanged(self) -> None:
        current = _validate_transport(self._transport)
        if any(not _same_callable(value, pinned)
               for value, pinned in zip(current, self._transport_callables)):
            raise MetadataProviderError("transport provider capabilities changed")

    def _matches_budget(self, budget: ExecutionBudget, snapshot: dict) -> tuple[dict, dict, str]:
        self._assert_budget(budget)
        required_state = snapshot.get("state", "") if type(snapshot) is dict else ""
        validated, field, member = _operation_snapshot(snapshot, self.binding, required_state)
        if (validated["plan"]["operation_id"] != budget._operation_id
                or validated["plan_hash"] != budget._plan_hash
                or field != _unpack(budget._field) or member != budget._member):
            raise MetadataProviderError("durable operation no longer matches the execution budget")
        return validated, field, member

    def _read_description(self, budget: ExecutionBudget):
        session = None
        description = None
        available = True
        try:
            alive, _ = budget._sample(self._owner)
            if alive:
                raw_session = self._transport_callables[0](
                    self.binding, budget._deadline, budget._cancellation
                )
                session = _validate_session(raw_session, self.binding)
                alive, _ = budget._sample(self._owner)
            if alive and session is not None:
                raw_description = self._transport_callables[1](
                    copy.deepcopy(session), budget._member, budget._deadline,
                    budget._cancellation,
                )
                description = _validate_describe(raw_description, self.binding, budget._member)
                alive, _ = budget._sample(self._owner)
                available = available and alive
            else:
                available = False
        except BaseException:
            available = False
        finally:
            if session is not None:
                try:
                    close_result = self._transport_callables[3](
                        copy.deepcopy(session), budget._deadline, budget._cancellation
                    )
                    alive, _ = budget._sample(self._owner)
                    if close_result is not None or not alive:
                        available = False
                except BaseException:
                    available = False
        return description if available else None

    def preflight(self, budget: ExecutionBudget):
        self._assert_budget(budget)
        if not budget._begin_phase(self._owner, "created", "preflight_running"):
            return PREFLIGHT_UNAVAILABLE
        try:
            self._assert_transport_unchanged()
        except BaseException:
            budget._set_phase(self._owner, "terminal")
            return PREFLIGHT_UNAVAILABLE
        description = self._read_description(budget)
        if description is None:
            budget._set_phase(self._owner, "terminal")
            return PREFLIGHT_UNAVAILABLE
        alive, observed_at = budget._sample(self._owner)
        if not alive:
            budget._set_phase(self._owner, "terminal")
            return PREFLIGHT_UNAVAILABLE
        evidence = {
            "org_binding_id": self.binding.org_binding_id,
            "member": budget._member,
            "exists": description["exists"],
            "observed_at": observed_at,
        }
        if set(evidence) != PREFLIGHT_KEYS:
            raise AssertionError("internal preflight evidence is not closed")
        object.__setattr__(budget, "_preflight_evidence", _pack(evidence))
        budget._set_phase(self._owner, "preflighted")
        return evidence

    def commit_preflight(self, budget: ExecutionBudget, ledger, actor, *, command_id):
        self._assert_budget_ledger(budget, ledger)
        if (budget._phase not in {"preflighted", "terminal"}
                or (budget._phase == "terminal" and budget._preflight_command is None)):
            raise MetadataProviderError("provider preflight is not ready to commit")
        snapshot = self._ledger_snapshot(ledger, budget._operation_id, actor)
        validated, _, _ = self._matches_budget(budget, snapshot)
        preflight_evidence = _unpack(budget._preflight_evidence)
        if budget._preflight_command is None:
            if (validated["state"] != "confirmed" or validated["provider_attempts"] != 0
                    or validated["preflight"] is not None):
                raise MetadataProviderError("durable operation is not awaiting preflight")
            alive, stamp = budget._sample(self._owner)
            if not alive:
                budget._set_phase(self._owner, "terminal")
                raise MetadataProviderError("execution budget expired before preflight commit")
            command = {
                "command_id": command_id, "expected_revision": validated["revision"],
                "action": "preflight", "at": max(stamp, preflight_evidence["observed_at"]),
                "payload": {"observation": copy.deepcopy(preflight_evidence)},
            }
            object.__setattr__(budget, "_preflight_command", _pack(command))
        command = _unpack(budget._preflight_command)
        if command["command_id"] != command_id:
            raise MetadataProviderError("preflight command identity changed")
        if validated["state"] == "confirmed":
            if (validated["provider_attempts"] != 0
                    or validated["preflight"] not in (None, preflight_evidence)):
                raise MetadataProviderError("durable preflight differs from this execution capability")
        elif validated["state"] == "failed":
            terminal_replay = (
                preflight_evidence["exists"] is True
                and validated["preflight"] == preflight_evidence
                and validated["reason"] == "preexisting_target"
                and validated["provider_attempts"] == 0
                and validated["revision"] == command["expected_revision"] + 1
                and validated["updated_at"] == command["at"]
                and validated["audit"][-1]["command_id"] == command_id
                and command_id in validated["commands"]
            )
            if not terminal_replay:
                raise MetadataProviderError("durable terminal preflight is not an exact replay")
        else:
            raise MetadataProviderError("durable operation is not awaiting preflight")
        result = ledger.apply(budget._operation_id, actor, command)
        budget._set_phase(
            self._owner,
            "preflight_committed" if result["receipt"]["status"] == "confirmed" else "terminal",
        )
        return result["receipt"]

    def reserve_dispatch(self, budget: ExecutionBudget, ledger, actor, *, command_id):
        self._assert_budget_ledger(budget, ledger)
        if budget._phase != "preflight_committed":
            raise DispatchPermitError("committed absent-target preflight is required")
        snapshot = self._ledger_snapshot(ledger, budget._operation_id, actor)
        validated, _, _ = self._matches_budget(budget, snapshot)
        preflight_evidence = _unpack(budget._preflight_evidence)
        if (validated["state"] != "confirmed" or validated["preflight"] != preflight_evidence
                or validated["preflight"]["exists"] is not False or validated["provider_attempts"] != 0):
            raise DispatchPermitError("durable operation is not eligible for one-time dispatch")
        if budget._begin_command is None:
            alive, stamp = budget._sample(self._owner)
            if not alive:
                budget._set_phase(self._owner, "terminal")
                raise DispatchPermitError("execution budget expired before durable reservation")
            command = {"command_id": command_id, "expected_revision": validated["revision"],
                       "action": "begin", "at": stamp, "payload": {}}
            object.__setattr__(budget, "_begin_command", _pack(command))
        elif _unpack(budget._begin_command)["command_id"] != command_id:
            raise DispatchPermitError("begin command identity changed")

        result = ledger.apply(budget._operation_id, actor, _unpack(budget._begin_command))
        after = self._ledger_snapshot(ledger, budget._operation_id, actor)
        after, _, _ = self._matches_budget(budget, after)
        expected_receipt = {"operation_id": budget._operation_id, "status": "executing",
                            "updated_at": after["updated_at"],
                            "next_action": "record_outcome_do_not_redispatch"}
        if (type(result) is not dict or set(result) != {"receipt", "revision", "replayed", "dispatch_allowed"}
                or type(result["replayed"]) is not bool or result["replayed"] is not False
                or type(result["dispatch_allowed"]) is not bool or result["dispatch_allowed"] is not True
                or result["revision"] != after["revision"] or result["receipt"] != expected_receipt
                or after["state"] != "executing" or after["provider_attempts"] != 1
                or after["attempt_reserved_at"] != after["updated_at"]):
            budget._set_phase(self._owner, "terminal")
            raise DispatchPermitError("durable begin was replayed or did not produce a fresh reservation")
        budget._set_phase(self._owner, "reserved")
        return _DispatchPermit(_PERMIT_KEY, owner=self._owner, budget=budget)

    def dispatch_once(self, permit) -> dict:
        if type(permit) is not _DispatchPermit:
            raise DispatchPermitError("a sealed dispatch permit is required")
        budget = permit._consume(self._owner)
        self._assert_budget(budget)
        if not budget._begin_phase(self._owner, "reserved", "dispatching"):
            result = _dispatch("rejected")
            object.__setattr__(budget, "_dispatch_result", _pack(result))
            budget._set_phase(self._owner, "dispatched")
            return result

        session = None
        create_entered = False
        result = _dispatch("rejected")
        try:
            self._assert_transport_unchanged()
            alive, _ = budget._sample(self._owner)
            if alive:
                raw_session = self._transport_callables[0](
                    self.binding, budget._deadline, budget._cancellation
                )
                session = _validate_session(raw_session, self.binding)
                alive, _ = budget._sample(self._owner)
            if alive and session is not None:
                raw_description = self._transport_callables[1](
                    copy.deepcopy(session), budget._member, budget._deadline,
                    budget._cancellation,
                )
                description = _validate_describe(raw_description, self.binding, budget._member)
                alive, _ = budget._sample(self._owner)
                if alive and description["exists"] is False:
                    create_entered = True
                    field = _unpack(budget._field)
                    raw_ack = self._transport_callables[2](
                        copy.deepcopy(session), copy.deepcopy(field), budget._deadline,
                        budget._cancellation,
                    )
                    _validate_ack(raw_ack, self.binding, budget._member, field)
                    alive, _ = budget._sample(self._owner)
                    result = _dispatch("accepted" if alive else "ambiguous")
        except BaseException:
            result = _dispatch("ambiguous" if create_entered else "rejected")
        finally:
            if session is not None:
                try:
                    close_result = self._transport_callables[3](
                        copy.deepcopy(session), budget._deadline, budget._cancellation
                    )
                    alive, _ = budget._sample(self._owner)
                    if create_entered and (close_result is not None or not alive):
                        result = _dispatch("ambiguous")
                except BaseException:
                    if create_entered:
                        result = _dispatch("ambiguous")
        if set(result) != DISPATCH_KEYS:
            raise AssertionError("internal dispatch result is not closed")
        object.__setattr__(budget, "_dispatch_result", _pack(result))
        budget._set_phase(self._owner, "dispatched")
        return result

    def commit_dispatch_result(self, budget: ExecutionBudget, ledger, actor, *, command_id):
        self._assert_budget_ledger(budget, ledger)
        if budget._phase != "dispatched" or budget._dispatch_result is None:
            raise MetadataProviderError("no adapter dispatch result is ready to commit")
        snapshot = self._ledger_snapshot(ledger, budget._operation_id, actor)
        validated, _, _ = self._matches_budget(budget, snapshot)
        if validated["state"] not in {"executing", "verify_pending", "failed", "outcome_unknown"}:
            raise MetadataProviderError("durable operation is not awaiting the dispatch result")
        if budget._dispatch_command is None:
            _, stamp = budget._sample(self._owner, allow_expired=True)
            command = {"command_id": command_id, "expected_revision": validated["revision"],
                       "action": "dispatch_result", "at": stamp,
                       "payload": _unpack(budget._dispatch_result)}
            object.__setattr__(budget, "_dispatch_command", _pack(command))
        elif _unpack(budget._dispatch_command)["command_id"] != command_id:
            raise MetadataProviderError("dispatch-result command identity changed")
        result = ledger.apply(budget._operation_id, actor, _unpack(budget._dispatch_command))
        status = result["receipt"]["status"]
        budget._set_phase(self._owner, "dispatch_committed" if status == "verify_pending" else "terminal")
        return result["receipt"]

    def verify_independent(self, budget: ExecutionBudget, ledger, actor):
        self._assert_budget_ledger(budget, ledger)
        if not budget._begin_phase(self._owner, "dispatch_committed", "verify_running"):
            result = VERIFICATION_UNAVAILABLE
            object.__setattr__(budget, "_verification_result", _pack(result))
            budget._set_phase(self._owner, "verification_ready")
            return result
        try:
            self._assert_transport_unchanged()
            snapshot = self._ledger_snapshot(ledger, budget._operation_id, actor)
            validated, expected_field, _ = self._matches_budget(budget, snapshot)
        except BaseException:
            # A failed durable read cannot authorize another provider read and
            # must not strand the process-local phase in verify_running.
            result = VERIFICATION_UNAVAILABLE
            object.__setattr__(budget, "_verification_result", _pack(result))
            budget._set_phase(self._owner, "verification_ready")
            return result
        if validated["state"] != "verify_pending" or validated["dispatch_accepted_at"] is None:
            result = VERIFICATION_UNAVAILABLE
            object.__setattr__(budget, "_verification_result", _pack(result))
            budget._set_phase(self._owner, "verification_ready")
            return result
        description = self._read_description(budget)
        if description is None:
            result = VERIFICATION_UNAVAILABLE
            object.__setattr__(budget, "_verification_result", _pack(result))
            budget._set_phase(self._owner, "verification_ready")
            return result
        if description["exists"] is not True or description["field"] != expected_field:
            result = VERIFICATION_MISMATCH
            object.__setattr__(budget, "_verification_result", _pack(result))
            budget._set_phase(self._owner, "verification_ready")
            return result
        alive, observed_at = budget._sample(self._owner)
        if not alive or observed_at < validated["dispatch_accepted_at"]:
            result = VERIFICATION_UNAVAILABLE
            object.__setattr__(budget, "_verification_result", _pack(result))
            budget._set_phase(self._owner, "verification_ready")
            return result
        observation = {"org_binding_id": self.binding.org_binding_id,
                       "field": copy.deepcopy(description["field"]), "observed_at": observed_at}
        if set(observation) != OBSERVATION_KEYS:
            raise AssertionError("internal verification observation is not closed")
        object.__setattr__(budget, "_verification_result", _pack(observation))
        budget._set_phase(self._owner, "verification_ready")
        return observation

    def commit_verification(self, budget: ExecutionBudget, ledger, actor, *, command_id):
        """Durably maps only this adapter's stored independent verification.

        A retry after an uncertain save reuses the identical ledger command and
        may recover its stored receipt. It never performs another provider read.
        """
        self._assert_budget_ledger(budget, ledger)
        if (budget._verification_result is None
                or budget._phase not in {"verification_ready", "terminal"}):
            raise MetadataProviderError("no independent verification result is ready to commit")
        snapshot = self._ledger_snapshot(ledger, budget._operation_id, actor)
        validated, _, _ = self._matches_budget(budget, snapshot)
        if budget._verification_command is None:
            if validated["state"] != "verify_pending":
                raise MetadataProviderError("durable operation is not awaiting verification")
            _, stamp = budget._sample(self._owner, allow_expired=True)
            result = _unpack(budget._verification_result)
            if type(result) is dict:
                _closed(result, OBSERVATION_KEYS, "verification observation")
                action, payload = "verify", {"observation": copy.deepcopy(result)}
            elif result == VERIFICATION_MISMATCH:
                action, payload = "verify", {"observation": None}
            elif result == VERIFICATION_UNAVAILABLE:
                action, payload = "unknown", {"reason": "verification_unavailable"}
            else:
                raise MetadataProviderError("invalid stored verification result")
            command = {
                "command_id": command_id,
                "expected_revision": validated["revision"],
                "action": action,
                "at": stamp,
                "payload": payload,
            }
            object.__setattr__(budget, "_verification_command", _pack(command))
        elif _unpack(budget._verification_command)["command_id"] != command_id:
            raise MetadataProviderError("verification command identity changed")
        result = ledger.apply(
            budget._operation_id, actor, _unpack(budget._verification_command)
        )
        budget._set_phase(self._owner, "terminal")
        return result["receipt"]

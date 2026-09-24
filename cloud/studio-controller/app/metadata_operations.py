"""Pure, injected-CAS-store metadata execution ledger. No provider or route.

The caller supplies authenticated opaque bindings and trusted server timestamps.
Only a fresh successful begin CAS returns dispatch_allowed=True; never a replay.
That transient result is not a retryable provider token. No I/O except the
explicitly injected store is performed here. Unknown outcomes are terminal.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .metadata_contract import MetadataContractError, validate_field

POLICY = "salesforce-metadata-execution-v1"
STATES = {"prepared", "confirmed", "executing", "verify_pending", "succeeded", "failed", "outcome_unknown"}
TERMINAL = {"succeeded", "failed", "outcome_unknown"}
ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
HASH = re.compile(r"[a-f0-9]{64}")
ACTOR_KEYS = {"operator_id", "session_id", "org_binding_id"}
PLAN_KEYS = {"policy", "operation_id", "binding", "field", "confirmation_nonce", "prepared_at", "expires_at"}
RECORD_KEYS = {"schema_version", "plan", "plan_hash", "state", "revision", "updated_at",
               "provider_attempts", "confirmed_at", "attempt_reserved_at", "dispatch_accepted_at", "preflight", "observation", "reason", "commands", "audit"}
RECEIPT_KEYS = {"operation_id", "status", "updated_at", "next_action"}
REASONS = {"precondition_rejected", "cancelled_before_dispatch", "provider_rejected_no_change",
           "ambiguous_dispatch", "interrupted", "dispatch_timeout", "verification_unavailable", "verification_mismatch", "preexisting_target"}
EDGES = {"prepared": {"confirmed", "failed"}, "confirmed": {"confirmed", "executing", "failed"},
         "executing": {"verify_pending", "failed", "outcome_unknown"},
         "verify_pending": {"succeeded", "outcome_unknown"}}
MAX_COMMANDS = 8
MAX_OPERATIONS = 64
NEXT_ACTION = {"prepared": "confirm_fresh_execution_plan", "confirmed": "preflight_then_reserve_once",
               "executing": "record_outcome_do_not_redispatch", "verify_pending": "verify_exact_readback",
               "succeeded": "verified_present_no_causality_claim", "failed": "review_failure",
               "outcome_unknown": "manual_reconciliation_do_not_retry"}


class InvalidOperation(ValueError):
    pass


class OperationConflict(InvalidOperation):
    pass


class PersistenceUncertain(RuntimeError):
    """Store failed; this call grants no dispatch permission. Re-read only."""


def _closed(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise InvalidOperation("invalid closed operation schema")


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise InvalidOperation("invalid opaque identifier")


def _integer(value, minimum=0, maximum=2**53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidOperation("invalid operation integer")


def _canonical(value, limit=8192):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > limit:
            raise ValueError()
        return encoded
    except (ValueError, TypeError, OverflowError):
        raise InvalidOperation("operation value must be bounded JSON") from None


def fingerprint(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _actor(value):
    _closed(value, ACTOR_KEYS)
    for identifier in value.values():
        _id(identifier)


def _field(value):
    try:
        return validate_field(value)
    except MetadataContractError:
        raise InvalidOperation("invalid approved Lead Text field") from None


def _plan(value):
    _closed(value, PLAN_KEYS)
    if value["policy"] != POLICY:
        raise InvalidOperation("execution requires a fresh execution-policy plan")
    _id(value["operation_id"])
    _id(value["confirmation_nonce"])
    _actor(value["binding"])
    _field(value["field"])
    _integer(value["prepared_at"])
    _integer(value["expires_at"], value["prepared_at"] + 1, value["prepared_at"] + 600)


def _observation_matches(value, record, at):
    try:
        _closed(value, {"org_binding_id", "field", "observed_at"})
        _id(value["org_binding_id"])
        _field(value["field"])
        _integer(value["observed_at"], record["dispatch_accepted_at"], at)
        return (value["org_binding_id"] == record["plan"]["binding"]["org_binding_id"]
                and value["field"] == record["plan"]["field"])
    except (InvalidOperation, TypeError):
        return False


def _receipt(record):
    return {"operation_id": record["plan"]["operation_id"], "status": record["state"],
            "updated_at": record["updated_at"], "next_action": NEXT_ACTION[record["state"]]}


def _target(record):
    return "CustomField:Lead." + record["plan"]["field"]["name"].lower() + "__c"


def _holds_target(record):
    # Conservative permanent reservation, including after success and unknown.
    # Only a trusted definite no-change failure may release it.
    return not (record["state"] == "failed" and record["reason"] in {
        "precondition_rejected", "cancelled_before_dispatch", "provider_rejected_no_change"})


def _preflight(value, record, at):
    _closed(value, {"org_binding_id", "member", "exists", "observed_at"})
    if (value["org_binding_id"] != record["plan"]["binding"]["org_binding_id"]
            or value["member"] != _target(record) or type(value["exists"]) is not bool):
        raise InvalidOperation("preflight binding mismatch")
    _integer(value["observed_at"], record["confirmed_at"], at)


def validate_record(record):
    """Reject corrupt/expanded durable data, never interpret it as a fresh job."""
    _canonical(record, 32768)
    _closed(record, RECORD_KEYS)
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise InvalidOperation("invalid operation schema version")
    _plan(record["plan"])
    if record["plan_hash"] != fingerprint(record["plan"]):
        raise InvalidOperation("operation plan hash mismatch")
    if not isinstance(record["state"], str) or record["state"] not in STATES:
        raise InvalidOperation("invalid operation state")
    _integer(record["revision"], 0, MAX_COMMANDS - 1)
    _integer(record["updated_at"], record["plan"]["prepared_at"])
    _integer(record["provider_attempts"], 0, 1)
    if record["confirmed_at"] is not None:
        _integer(record["confirmed_at"], record["plan"]["prepared_at"], min(record["updated_at"], record["plan"]["expires_at"] - 1))
    if record["preflight"] is not None:
        if record["confirmed_at"] is None:
            raise InvalidOperation("preflight requires confirmation")
        _preflight(record["preflight"], record, record["updated_at"])
    if record["provider_attempts"]:
        if record["confirmed_at"] is None:
            raise InvalidOperation("unconfirmed provider attempt")
        _integer(record["attempt_reserved_at"], record["confirmed_at"], min(record["updated_at"], record["plan"]["expires_at"] - 1))
        if record["preflight"] is None or record["preflight"]["exists"] is not False:
            raise InvalidOperation("attempt requires an absent-target preflight")
    elif record["attempt_reserved_at"] is not None:
        raise InvalidOperation("unexpected provider reservation")
    if record["dispatch_accepted_at"] is not None:
        if record["provider_attempts"] != 1:
            raise InvalidOperation("acknowledgement requires an attempt")
        _integer(record["dispatch_accepted_at"], record["attempt_reserved_at"], record["updated_at"])
    if record["state"] in {"verify_pending", "succeeded"} and record["dispatch_accepted_at"] is None:
        raise InvalidOperation("verification requires accepted dispatch")
    if record["state"] in {"prepared", "confirmed", "executing", "failed"} and record["dispatch_accepted_at"] is not None:
        raise InvalidOperation("unexpected dispatch acknowledgement")
    if record["state"] in {"prepared", "confirmed"} and record["provider_attempts"]:
        raise InvalidOperation("attempt before execution")
    if record["state"] in {"executing", "verify_pending", "succeeded", "outcome_unknown"} and not record["provider_attempts"]:
        raise InvalidOperation("missing provider reservation")
    if record["state"] == "confirmed" and record["confirmed_at"] is None:
        raise InvalidOperation("missing confirmation")
    if record["state"] == "prepared" and record["confirmed_at"] is not None:
        raise InvalidOperation("unexpected confirmation")
    if record["provider_attempts"]:
        _preflight(record["preflight"], record, record["attempt_reserved_at"])
    if record["state"] in {"failed", "outcome_unknown"}:
        reasons = ({"ambiguous_dispatch", "interrupted", "dispatch_timeout", "verification_unavailable", "verification_mismatch"}
                   if record["state"] == "outcome_unknown" else
                   {"provider_rejected_no_change"} if record["provider_attempts"] else
                   {"precondition_rejected", "cancelled_before_dispatch", "preexisting_target"})
        if not isinstance(record["reason"], str) or record["reason"] not in reasons:
            raise InvalidOperation("missing terminal reason")
    elif record["reason"] is not None:
        raise InvalidOperation("unexpected operation reason")
    existing = record["preflight"] is not None and record["preflight"]["exists"]
    if existing != (record["state"] == "failed" and record["reason"] == "preexisting_target"):
        raise InvalidOperation("preexisting target must remain quarantined")
    if record["state"] == "succeeded":
        if not _observation_matches(record["observation"], record, record["updated_at"]):
            raise InvalidOperation("success requires exact read-after-write evidence")
    elif record["observation"] is not None:
        raise InvalidOperation("unexpected verification evidence")
    audit = record["audit"]
    commands = record["commands"]
    if not isinstance(audit, list) or not isinstance(commands, dict) or len(audit) != record["revision"] + 1 or len(commands) != len(audit):
        raise InvalidOperation("invalid bounded operation history")
    previous, stamp = None, record["plan"]["prepared_at"]
    milestones = {"confirmed_at": [], "attempt_reserved_at": [], "dispatch_accepted_at": []}
    milestone_edges = {("prepared", "confirmed"): "confirmed_at",
                       ("confirmed", "executing"): "attempt_reserved_at",
                       ("executing", "verify_pending"): "dispatch_accepted_at"}
    preflight_commits = []
    for revision, event in enumerate(audit):
        _closed(event, {"revision", "from", "to", "at", "command_id"})
        _id(event["command_id"])
        if event["revision"] != revision or type(event["revision"]) is not int or event["from"] != previous:
            raise InvalidOperation("invalid audit order")
        if (revision == 0 and event["to"] != "prepared") or (revision and event["to"] not in EDGES.get(previous, set())):
            raise InvalidOperation("illegal durable transition")
        _integer(event["at"], stamp, record["updated_at"])
        if revision == 0 and event["at"] != record["plan"]["prepared_at"]:
            raise InvalidOperation("preparation timestamp does not match committed history")
        edge = (event["from"], event["to"])
        milestone = milestone_edges.get(edge)
        if milestone:
            milestones[milestone].append(event["at"])
        if edge == ("confirmed", "confirmed") or (
                edge == ("confirmed", "failed") and record["reason"] == "preexisting_target"):
            preflight_commits.append(event["at"])
        previous, stamp = event["to"], event["at"]
        entry = commands.get(event["command_id"])
        _closed(entry, {"fingerprint", "revision", "receipt"})
        if not isinstance(entry["fingerprint"], str) or not HASH.fullmatch(entry["fingerprint"]):
            raise InvalidOperation("invalid command fingerprint")
        receipt = entry["receipt"]
        _closed(receipt, RECEIPT_KEYS)
        if (receipt["operation_id"] != record["plan"]["operation_id"] or receipt["status"] != previous
                or type(entry["revision"]) is not int or entry["revision"] != revision
                or receipt["updated_at"] != stamp or type(receipt["updated_at"]) is not int
                or receipt["next_action"] != NEXT_ACTION[previous]):
            raise InvalidOperation("receipt is not bound to its transition")
    if previous != record["state"] or stamp != record["updated_at"] or commands[audit[-1]["command_id"]]["receipt"] != _receipt(record):
        raise InvalidOperation("state does not match committed history")
    # A scalar in a plausible time range is not evidence that its transition
    # happened then. Derive lifecycle markers from the committed audit path.
    for milestone, timestamps in milestones.items():
        if len(timestamps) > 1 or record[milestone] != (timestamps[0] if timestamps else None):
            raise InvalidOperation("lifecycle timestamp does not match committed transition")
    if record["provider_attempts"] != len(milestones["attempt_reserved_at"]):
        raise InvalidOperation("attempt count does not match committed transition")
    if record["preflight"] is None:
        if preflight_commits:
            raise InvalidOperation("committed preflight evidence is missing")
    else:
        if len(preflight_commits) != 1:
            raise InvalidOperation("preflight requires exactly one matching committed transition")
        _preflight(record["preflight"], record, preflight_commits[0])
    if (record["reason"] == "verification_mismatch" and not milestones["dispatch_accepted_at"]
            or record["reason"] == "ambiguous_dispatch" and milestones["dispatch_accepted_at"]):
        raise InvalidOperation("outcome reason does not match dispatch acknowledgement history")


def validate_ledger(document):
    _canonical(document, 262144)
    _closed(document, {"schema_version", "org_binding_id", "revision", "operations", "targets"})
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise InvalidOperation("invalid ledger version")
    _id(document["org_binding_id"])
    _integer(document["revision"])
    operations = document["operations"]
    if not isinstance(operations, dict) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise InvalidOperation("ledger capacity exceeded or invalid")
    targets = {}
    for operation_id, record in operations.items():
        _id(operation_id)
        validate_record(record)
        if (record["plan"]["operation_id"] != operation_id
                or record["plan"]["binding"]["org_binding_id"] != document["org_binding_id"]):
            raise InvalidOperation("ledger binding mismatch")
        if _holds_target(record):
            target = _target(record)
            if target in targets:
                raise InvalidOperation("multiple operations hold one target")
            targets[target] = operation_id
    if type(document["targets"]) is not dict or document["targets"] != targets:
        raise InvalidOperation("missing or invalid atomic target holds")
    if document["revision"] != sum(len(record["audit"]) for record in operations.values()):
        raise InvalidOperation("ledger revision does not match retained committed history")


class MetadataOperationLedger:
    def __init__(self, store, *, conflict_type):
        # The adapter must implement atomic load/save generation CAS. Do not
        # assume the local FileStore supplies a distributed execution lock.
        if getattr(store, "atomic_generation_cas", None) is not True:
            raise InvalidOperation("explicit atomic generation-CAS adapter required")
        self.store = store
        self.conflict_type = conflict_type

    @staticmethod
    def _name(org_binding_id):
        _id(org_binding_id)
        return "studio_metadata_ledger_" + org_binding_id

    def _load(self, org_binding_id):
        try:
            document, token = self.store.load(self._name(org_binding_id))
        except Exception:
            raise PersistenceUncertain("operation storage unavailable; no dispatch authorized") from None
        if not document:
            if document != {} or token is not None:
                raise InvalidOperation("invalid empty operation record")
            return {"schema_version": 1, "org_binding_id": org_binding_id, "revision": 0,
                    "operations": {}, "targets": {}}, token
        validate_ledger(document)
        if document["org_binding_id"] != org_binding_id:
            raise InvalidOperation("operation storage binding mismatch")
        return copy.deepcopy(document), token

    def _save(self, document, token):
        validate_ledger(document)
        try:
            self.store.save(self._name(document["org_binding_id"]), copy.deepcopy(document), token)
        except self.conflict_type:
            raise OperationConflict("operation changed; re-read before deciding") from None
        except Exception:
            raise PersistenceUncertain("operation save outcome unknown; no dispatch authorized") from None

    @staticmethod
    def _authorize(record, actor):
        _actor(actor)
        if actor != record["plan"]["binding"]:
            raise InvalidOperation("operation binding mismatch")

    @staticmethod
    def _result(receipt, revision, *, replayed=False, dispatch_allowed=False):
        # Only receipt is public. The rest is internal orchestration state.
        return {"receipt": copy.deepcopy(receipt), "revision": revision,
                "replayed": replayed, "dispatch_allowed": dispatch_allowed}

    @staticmethod
    def _replay(record, command_id, digest):
        previous = record["commands"].get(command_id)
        if previous:
            if previous["fingerprint"] != digest:
                raise OperationConflict("command id is bound to another payload")
            return MetadataOperationLedger._result(previous["receipt"], previous["revision"], replayed=True)
        return None

    def prepare(self, operation_id, command_id, specification):
        _id(operation_id)
        _id(command_id)
        _closed(specification, {"binding", "field", "confirmation_nonce", "prepared_at", "expires_at"})
        plan = {**copy.deepcopy(specification), "policy": POLICY, "operation_id": operation_id}
        _plan(plan)
        digest = fingerprint({"action": "prepare", "plan": plan})
        document, token = self._load(plan["binding"]["org_binding_id"])
        existing = document["operations"].get(operation_id)
        if existing:
            self._authorize(existing, plan["binding"])
            replay = self._replay(existing, command_id, digest)
            if replay:
                return replay
            raise OperationConflict("operation already exists; a new command cannot replace its plan")
        record = {"schema_version": 1, "plan": plan, "plan_hash": fingerprint(plan),
                  "state": "prepared", "revision": 0, "updated_at": plan["prepared_at"],
                  "provider_attempts": 0, "confirmed_at": None, "attempt_reserved_at": None, "dispatch_accepted_at": None,
                  "preflight": None, "observation": None, "reason": None, "commands": {}, "audit": []}
        target = _target(record)
        if target in document["targets"]:
            raise OperationConflict("metadata target is held; reconcile without creating another operation")
        if len(document["operations"]) >= MAX_OPERATIONS:
            raise OperationConflict("ledger capacity reached; no implicit pruning permitted")
        self._record_command(record, command_id, digest, None)
        document["operations"][operation_id] = record
        document["targets"][target] = operation_id
        document["revision"] += 1
        self._save(document, token)
        return self._result(_receipt(record), record["revision"])

    def _operation(self, operation_id, actor):
        _id(operation_id)
        _actor(actor)
        document, token = self._load(actor["org_binding_id"])
        record = document["operations"].get(operation_id)
        if record is None:
            raise InvalidOperation("operation unavailable for this binding")
        self._authorize(record, actor)
        return record, document, token

    def receipt(self, operation_id, actor):
        record, _, _ = self._operation(operation_id, actor)
        return _receipt(record)

    def execution_snapshot(self, operation_id, actor):
        """Internal coordinator view; still closed, validated, and actor-bound.

        Provider code must read this immediately around a durable transition,
        never accept a portable caller-supplied snapshot as write authority.
        """
        record, _, _ = self._operation(operation_id, actor)
        validate_record(record)
        return copy.deepcopy(record)

    def confirmation_challenge(self, operation_id, actor):
        record, _, _ = self._operation(operation_id, actor)
        if record["state"] != "prepared":
            raise OperationConflict("operation is not awaiting fresh execution confirmation")
        return self._challenge(record)

    @staticmethod
    def _challenge(record):
        return {"execution_policy": POLICY, "operation_id": record["plan"]["operation_id"],
                "execution_plan_hash": record["plan_hash"], "confirmation_nonce": record["plan"]["confirmation_nonce"]}

    @staticmethod
    def _record_command(record, command_id, digest, previous):
        record["audit"].append({"revision": record["revision"], "from": previous,
                                "to": record["state"], "at": record["updated_at"], "command_id": command_id})
        record["commands"][command_id] = {"fingerprint": digest, "revision": record["revision"], "receipt": _receipt(record)}

    def apply(self, operation_id, actor, command):
        _closed(command, {"command_id", "expected_revision", "action", "at", "payload"})
        _id(command["command_id"])
        _integer(command["expected_revision"], 0, MAX_COMMANDS - 1)
        _integer(command["at"])
        if not isinstance(command["action"], str) or command["action"] not in {"confirm", "preflight", "begin", "dispatch_result", "verify", "unknown", "abort"}:
            raise InvalidOperation("unsupported operation action")
        record, document, token = self._operation(operation_id, actor)
        digest = fingerprint({"operation_id": operation_id, "actor": actor, "command": command})
        replay = self._replay(record, command["command_id"], digest)
        if replay:
            return replay  # never repeats transient dispatch permission
        if command["expected_revision"] != record["revision"]:
            raise OperationConflict("stale operation revision")
        if record["state"] in TERMINAL or len(record["commands"]) >= MAX_COMMANDS:
            raise OperationConflict("operation is terminal; no retry or rollback is allowed")
        _integer(command["at"], record["updated_at"])
        previous = record["state"]
        self._transition(record, command)
        record["revision"] += 1
        record["updated_at"] = command["at"]
        self._record_command(record, command["command_id"], digest, previous)
        if not _holds_target(record):
            del document["targets"][_target(record)]
        document["revision"] += 1
        self._save(document, token)
        return self._result(_receipt(record), record["revision"], dispatch_allowed=command["action"] == "begin")

    @staticmethod
    def _transition(record, command):
        action, payload, at, state = command["action"], command["payload"], command["at"], record["state"]
        allowed = {"confirm": {"prepared"}, "preflight": {"confirmed"}, "begin": {"confirmed"}, "dispatch_result": {"executing"},
                   "verify": {"verify_pending"}, "unknown": {"executing", "verify_pending"},
                   "abort": {"prepared", "confirmed"}}
        if state not in allowed[action]:
            raise OperationConflict("illegal operation transition")
        if action == "confirm":
            _closed(payload, {"execution_policy", "operation_id", "execution_plan_hash", "confirmation_nonce"})
            if payload != MetadataOperationLedger._challenge(record):
                raise InvalidOperation("fresh exact execution confirmation required; proposal-only confirmation is invalid")
            if at >= record["plan"]["expires_at"]:
                raise InvalidOperation("execution confirmation expired")
            record.update(state="confirmed", confirmed_at=at)
        elif action == "preflight":
            _closed(payload, {"observation"})
            if record["preflight"] is not None:
                raise OperationConflict("preflight already recorded")
            _preflight(payload["observation"], record, at)
            record["preflight"] = copy.deepcopy(payload["observation"])
            if record["preflight"]["exists"]:
                record.update(state="failed", reason="preexisting_target")
        elif action == "begin":
            _closed(payload, set())
            if at >= record["plan"]["expires_at"] or record["provider_attempts"] != 0:
                raise OperationConflict("execution expired or attempt already reserved")
            if record["preflight"] is None or record["preflight"]["exists"] is not False:
                raise OperationConflict("execution requires exact absent-target preflight")
            record.update(state="executing", provider_attempts=1, attempt_reserved_at=at)
        elif action == "dispatch_result":
            _closed(payload, {"outcome", "definitive_no_change"})
            if type(payload["definitive_no_change"]) is not bool:
                raise InvalidOperation("invalid no-change evidence classification")
            if payload["definitive_no_change"] != (payload["outcome"] == "rejected_no_change"):
                raise InvalidOperation("definite no-change evidence is required only for definitive rejection")
            # Future adapter must classify duplicate/already-exists, timeouts,
            # lost acknowledgements and ambiguous errors as ambiguous, not this
            # authoritative no-change assertion. This module cannot prove I/O.
            states = {"accepted": ("verify_pending", None), "rejected_no_change": ("failed", "provider_rejected_no_change"),
                      "ambiguous": ("outcome_unknown", "ambiguous_dispatch")}
            if not isinstance(payload["outcome"], str) or payload["outcome"] not in states:
                raise InvalidOperation("invalid dispatch outcome")
            record["state"], record["reason"] = states[payload["outcome"]]
            if payload["outcome"] == "accepted":
                record["dispatch_accepted_at"] = at
        elif action == "verify":
            _closed(payload, {"observation"})
            if _observation_matches(payload["observation"], record, at):
                record.update(state="succeeded", observation=copy.deepcopy(payload["observation"]))
            else:
                record.update(state="outcome_unknown", reason="verification_mismatch")
        elif action == "unknown":
            _closed(payload, {"reason"})
            if not isinstance(payload["reason"], str) or payload["reason"] not in {"interrupted", "dispatch_timeout", "verification_unavailable"}:
                raise InvalidOperation("invalid unknown-outcome reason")
            record.update(state="outcome_unknown", reason=payload["reason"])
        else:
            _closed(payload, {"reason"})
            if not isinstance(payload["reason"], str) or payload["reason"] not in {"precondition_rejected", "cancelled_before_dispatch"}:
                raise InvalidOperation("invalid pre-dispatch failure reason")
            record.update(state="failed", reason=payload["reason"])

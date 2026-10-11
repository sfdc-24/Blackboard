#!/usr/bin/env python3
"""Finite, owner-only phone Doc intake for one cloud worker job tick.

The first enabled observation initializes a baseline and queues nothing. A later
complete OWNER REQUEST block with a new ID is the only admissible request. The
existing drive-intake state machine owns dedupe, queueing and receipt writes.
This module has no socket at import and never claims that a wake was sent.
"""
from __future__ import annotations

import json
import importlib.util
import os
import re
import time
from datetime import datetime, timezone

try:
    import drive_intake as di
except ModuleNotFoundError:  # source-tree tests; the image copies it beside this file
    _contract_path = os.path.join(os.path.dirname(__file__), "..", "drive-intake", "main.py")
    _spec = importlib.util.spec_from_file_location("drive_intake", _contract_path)
    di = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(di)


FOLDER_ID = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"
DOC_IDS = (
    "1pKWJYbJzMqxSA9Vy_PSR93BU2OAAgP-N3WBEkCj7oj4",
    "17Q2vBYvxs8PIu-sXXWj67HAU_DL2RfB9-SoQbpeDoWQ",
)
DOC_SET = frozenset(DOC_IDS)
TARGET_FIELDS = ("source_doc_id", "source_revision", "request_id", "task_id", "recipient_id")
TARGET_LOOKBACK = 500
APPROVAL_RECEIPT_OWED = "approval_failed"
APPROVAL_REFUND_UNKNOWN = "approval_refund_unknown"
BASELINE_TTL = 30 * 86400
APPROVAL_TTL = 30 * 86400
BEGIN = "BEGIN OWNER REQUEST"
END = "END OWNER REQUEST"
BLOCK = re.compile(r"(?m)^BEGIN OWNER REQUEST[ \t]*\n(?P<body>.*?)^END OWNER REQUEST[ \t]*$", re.S)
REQUEST = re.compile(r"(?mi)^[ \t]*REQUEST[ \t]*:[ \t]*([A-Za-z0-9_.:-]{8,128})[ \t]*$")
TO = re.compile(r"(?mi)^[ \t]*TO[ \t]*:[ \t]*([^\s]+)[ \t]*$")
RECEIPT = re.compile(
    r"(?m)^(?:REQUESTED|DISPATCHED|QUEUED|BLOCKED|ACCEPTED|SUCCEEDED|FAILED|UNKNOWN) "
    r"- task task-[0-9a-f]{16} - request [A-Za-z0-9_.:-]{1,128} "
    r"- rev [A-Za-z0-9_.:-]{1,128} - recipient [A-Za-z0-9_.:-]{1,128} "
    r"- by (?:drive-intake|claude-api|claude-code-cli) - [^\r\n]+ "
    r"- id (?P<id>[0-9a-f]{16})[ \t]*$")


class OutOfScope(ValueError):
    pass


class TargetUnavailable(ValueError):
    """A pinned pilot task cannot be selected without touching other work."""


def target_from_env(environ):
    """A pilot run must name one reviewed request, never a generic inbox pass."""
    try:
        target = json.loads(environ["PHONE_DOC_PILOT_TARGET"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TargetUnavailable("missing or malformed pilot target") from exc
    if (not isinstance(target, dict) or set(target) != set(TARGET_FIELDS) or
            any(not isinstance(target[name], str) for name in TARGET_FIELDS) or
            not allowed(target["source_doc_id"]) or
            target["recipient_id"] != "claude-api" or
            not di.ID_OK.fullmatch(target["source_revision"]) or
            not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", target["request_id"]) or
            not re.fullmatch(r"task-[0-9a-f]{16}", target["task_id"])):
        raise TargetUnavailable("invalid pilot target")
    return target


def allowed(file_id: str) -> bool:
    return file_id in DOC_SET


def _scope(file_id: str) -> None:
    if not allowed(file_id):
        raise OutOfScope("document is outside the two-ID pilot")


def _as_text(raw) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")


def _request_head(text: str) -> str:
    return di.strip_receipts(text or "")


def request_blocks(text: str) -> list[dict]:
    """Return complete, unambiguous blocks above the receipts heading.

    A block carries exactly one REQUEST ID and an actual body. Duplicate IDs,
    unfinished blocks and stray markers fail closed. No document text is logged.
    """
    head = _request_head(text)
    matches = list(BLOCK.finditer(head))
    residue = BLOCK.sub("", head)
    if BEGIN in residue or END in residue:
        raise ValueError("incomplete owner request block")
    blocks = []
    ids = set()
    for match in matches:
        body = match.group("body")
        if BEGIN in body or END in body:
            raise ValueError("nested owner request block")
        requests = REQUEST.findall(body)
        tos = TO.findall(body)
        # The shared intake parser also accepts '=' and optional '#' headings.
        # Reject those aliases instead of letting its first header override the
        # envelope validated here (request ID or recipient smuggling).
        if (len(requests) != 1 or tos != ["claude-api"] or
                di.REQUEST_LINE.findall(body) != requests or
                di.TO_LINE.findall(body) != tos):
            raise ValueError("ambiguous owner request block")
        request_id = requests[0]
        if request_id in ids:
            raise ValueError("duplicate owner request ID")
        ids.add(request_id)
        payload = REQUEST.sub("", body)
        payload = TO.sub("", payload).strip()
        if not payload:
            raise ValueError("empty owner request")
        block = match.group(0)
        blocks.append({"id": request_id, "text": block,
                       "revision": di.request_revision(block)})
    return blocks


def focused_text(text: str, expected_id: str = "", expected_revision: str = "") -> str:
    """View the queued request's exact block plus the actual receipt trail.

    This view goes to both intake and the worker. The underlying Google Doc is
    never rewritten or truncated; append/has still operate on its full text.
    """
    blocks = request_blocks(text)
    if not blocks:
        return ""
    if bool(expected_id) != bool(expected_revision):
        return ""
    selected = (next((block for block in blocks if
                     block["id"] == expected_id and block["revision"] == expected_revision), None)
                if expected_id else blocks[-1])
    if selected is None:
        return ""
    match = di.RECEIPTS_HEADING.search(text or "")
    trail = (text or "")[match.start():] if match else ""
    return selected["text"] + ("\n" + trail if trail else "")


class PilotDocs:
    """Exact-ID gate before every read or write, with a request-only read view."""

    def __init__(self, raw_docs, expected=None, require_expected=False):
        self.raw_docs = raw_docs
        self.expected = expected or {}
        self.require_expected = require_expected

    def for_entry(self, fields):
        """Bind one worker read to the approved inbox entry, never the Doc's latest block."""
        record = _entry_tuple(fields) if isinstance(fields, dict) else {}
        file_id = record.get("source_doc_id") or ""
        request_id = record.get("request_id") or ""
        revision = record.get("source_revision") or ""
        expected = ({file_id: (request_id, revision)}
                    if allowed(file_id) and request_id and revision else {})
        return PilotDocs(self.raw_docs, expected, require_expected=True)

    def _read_scoped(self, file_id):
        _scope(file_id)
        doc = self.raw_docs.read(file_id)
        if not isinstance(doc, dict) or doc.get("folder_id") != FOLDER_ID:
            raise OutOfScope("document is outside the approved parent folder")
        return doc

    def read(self, file_id):
        if self.require_expected and file_id not in self.expected:
            raise OutOfScope("no approved request bound to document read")
        doc = self._read_scoped(file_id)
        view = dict(doc)
        request_id, revision = self.expected.get(file_id, ("", ""))
        view["text"] = focused_text(doc.get("text") or "", request_id, revision)
        if self.require_expected and not view["text"]:
            # A stored result can still owe its terminal receipt after the
            # owner edits the block. Expose only the heading so that writer
            # does not add another one; no replacement request reaches a model.
            heading = di.RECEIPTS_HEADING.search(doc.get("text") or "")
            view["text"] = heading.group(0) if heading else ""
        return view

    def append(self, file_id, expected_revision, receipt_id, block):
        if self.require_expected and file_id not in self.expected:
            raise OutOfScope("no approved request bound to document append")
        doc = self._read_scoped(file_id)
        receipt = block.lstrip("\n").removeprefix(di.HEADING_TEXT + "\n")
        # Before a call, an edited request must not reach the model via a
        # refreshed Drive revision. After a call, its stored result still needs
        # a terminal receipt even if the owner changed the request meanwhile.
        if self.require_expected and receipt.startswith("ACCEPTED - task "):
            if not focused_text(doc.get("text") or "", *self.expected[file_id]):
                raise OutOfScope("approved request changed before receipt append")
        return self.raw_docs.append(file_id, expected_revision, receipt_id, block)

    def has(self, file_id, receipt_id):
        if self.require_expected and file_id not in self.expected:
            raise OutOfScope("no approved request bound to document receipt lookup")
        text = self._read_scoped(file_id).get("text") or ""
        heading = di.RECEIPTS_HEADING.search(text)
        if heading is None:
            return False
        return any(match.group("id") == receipt_id
                   for match in RECEIPT.finditer(text[heading.end():]))


def _baseline_key(file_id):
    return "fleet:intake:pilot:baseline:" + file_id


def _approval_key(file_id, request_id):
    # One owner ID approves one exact tuple, even if its text is later edited.
    return "fleet:intake:pilot:approved:" + file_id + ":" + request_id


def _issued_key(file_id, request_id):
    # No TTL: an expired or revoked approval must not become a first request again.
    return "fleet:intake:pilot:issued:" + file_id + ":" + request_id


def _record_json(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


APPROVE_ONCE_LUA = (
    "if redis.call('EXISTS', KEYS[2]) ~= 0 then return 0 end "
    "if redis.call('EXISTS', KEYS[1]) ~= 0 then return 0 end "
    "redis.call('SET', KEYS[2], ARGV[1]) "
    "redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2]) "
    "return 1")


def _issued_matches(client, record):
    raw = client.get(_issued_key(record["source_doc_id"], record["request_id"]))
    try:
        return raw is not None and json.loads(_as_text(raw)) == record
    except ValueError:
        return False


def _remember_existing(client, record):
    """Backfill a durable marker while a legacy approval is still present."""
    key = _issued_key(record["source_doc_id"], record["request_id"])
    if client.get(key) is None:
        client.set(key, _record_json(record), nx=True)
    return _issued_matches(client, record)


def _requested_seen(text, record):
    heading = di.RECEIPTS_HEADING.search(text or "")
    if heading is None:
        return False
    wanted = di.receipt_id_for(record, di.REQUESTED)
    return any(match.group("id") == wanted
               for match in RECEIPT.finditer(text[heading.end():]))


def _baseline(client, file_id, ids):
    key = _baseline_key(file_id)
    raw = client.get(key)
    if raw is None:
        snapshot = json.dumps({"ids": ids}, separators=(",", ":"))
        if client.set(key, snapshot, nx=True, ex=BASELINE_TTL):
            return None
        raw = client.get(key)
    try:
        parsed = json.loads(_as_text(raw))
        old = parsed["ids"]
        if not isinstance(old, list) or any(not isinstance(i, str) for i in old):
            raise ValueError("baseline shape")
        return set(old)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("unreadable baseline") from exc


def _entry_tuple(fields):
    return {name: _as_text(fields.get(name)) for name in (
        "source_doc_id", "source_revision", "request_id", "task_id", "recipient_id")}


def admitted(fields, client) -> bool:
    """Second gate, before a queued entry can be read by the model worker."""
    if not isinstance(fields, dict) or fields.get("kind") != "drive_request":
        return False
    tuple_ = _entry_tuple(fields)
    if not allowed(tuple_["source_doc_id"]) or tuple_["recipient_id"] != "claude-api":
        return False
    if not di.tuple_ok({**tuple_, "drive_revision": "r"}):
        return False
    try:
        raw = client.get(_approval_key(tuple_["source_doc_id"], tuple_["request_id"]))
        return json.loads(_as_text(raw)) == tuple_ if raw is not None else False
    except Exception:  # noqa: BLE001 - an unreadable approval is no admission
        return False


class PilotInbox:
    """Keep the laptop lane's inbox untouched; admit only approved API tasks."""

    def __init__(self, inbox, client, target=None, claims=None):
        self.inbox, self.client, self.target, self.claims = inbox, client, target, claims

    def target_approved_now(self, fields):
        return (self.target is not None and _entry_tuple(fields) == self.target and
                admitted(fields, self.client) and _issued_matches(self.client, self.target))

    def target_recovery_owed(self, fields):
        if self.target is None or self.claims is None or _entry_tuple(fields) != self.target:
            return False
        try:
            state = self.claims.state(self.target["task_id"])
            return state is not None and state.split(":", 1)[0] in (
                APPROVAL_RECEIPT_OWED, APPROVAL_REFUND_UNKNOWN)
        except Exception:
            return False

    def read(self, agent, count):
        if self.target is not None:
            if agent != "claude-api" or count <= 0:
                raise TargetUnavailable("invalid targeted inbox read")
            # Redis streams have no field index. A bounded, read-only lookup may
            # refuse a buried target, but it must never advance the shared cursor.
            rows = self.client.xrevrange("fleet:inbox:claude-api",
                                         count=min(count, TARGET_LOOKBACK)) or ()
            matches = []
            for entry_id, raw in rows:
                fields = {_as_text(key): _as_text(value) for key, value in raw.items()}
                if self.target_approved_now(fields) or self.target_recovery_owed(fields):
                    matches.append((_as_text(entry_id), fields))
            if len(matches) != 1:
                raise TargetUnavailable("target absent, outside lookup bound, or duplicated")
            return matches
        rows = self.inbox.read(agent, count)
        if agent != "claude-api":
            return rows
        approved = [(entry_id, fields) for entry_id, fields in rows if admitted(fields, self.client)]
        if rows and not approved:
            # The worker checkpoints only considered entries. Advance past a page
            # containing no pilot work so unrelated older rows cannot starve it.
            self.inbox.checkpoint(agent, rows[-1][0])
        return approved

    def checkpoint(self, agent, entry_id):
        if self.target is not None:
            return None  # a targeted pass never consumes the ordinary sweep
        return self.inbox.checkpoint(agent, entry_id)


class PilotClaims(di.RedisClaims):
    """Reuse task claims; the explicit two-Doc scan needs no global poll state."""

    def note_poll(self, _now, _watermark=None):
        pass

    def pending(self):
        return set()

    def pending_add(self, _file_id):
        pass

    def pending_remove(self, _file_id):
        pass


def run_prepass(client, raw_docs, *, enabled=False, now=None, at=None, target=None) -> list[dict]:
    """One finite poll over exactly two IDs; no wake transport is configured.

    Call this before the worker inbox pass, then use PilotInbox and PilotDocs
    during that API pass. The first enabled pass only establishes baselines.
    """
    if not enabled:
        return [{"status": "disabled"}]
    if client is None or raw_docs is None:
        return [{"status": "no_store"}]
    now = float(time.time() if now is None else now)
    at = at or datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    outcomes = []
    expected = {}
    for file_id in DOC_IDS:
        try:
            raw = raw_docs.read(file_id)
            if not isinstance(raw, dict) or raw.get("folder_id") != FOLDER_ID:
                outcomes.append({"status": "out_of_scope", "doc": file_id})
                continue
            if not di.ID_OK.fullmatch(str(raw.get("revision_id") or "")):
                outcomes.append({"status": "no_revision", "doc": file_id})
                continue
            text = raw.get("text") or ""
            if len(text) > di.MAX_DOC:
                outcomes.append({"status": "too_large", "doc": file_id})
                continue
            blocks = request_blocks(text)
            if target is not None and client.get(_baseline_key(file_id)) is None:
                outcomes.append({"status": "baseline_missing", "doc": file_id})
                continue
            old_ids = _baseline(client, file_id, [block["id"] for block in blocks])
            if old_ids is None:
                outcomes.append({"status": "baseline_initialized", "doc": file_id})
                continue
            if target is not None:
                if file_id != target["source_doc_id"]:
                    outcomes.append({"status": "non_target", "doc": file_id})
                    continue
                approval = client.get(_approval_key(file_id, target["request_id"]))
                if approval is not None:
                    approved = (admitted({**target, "kind": "drive_request"}, client) and
                                _remember_existing(client, target))
                    outcomes.append({"status": "already_approved" if approved else "approval_mismatch",
                                     "doc": file_id, "task": target["task_id"], "admitted": approved})
                    continue
                if client.get(_issued_key(file_id, target["request_id"])) is not None:
                    outcomes.append({"status": "approval_lapsed", "doc": file_id,
                                     "task": target["task_id"]})
                    continue
                selected = [block for block in blocks if block["id"] == target["request_id"]]
                if (len(selected) != 1 or selected[0]["revision"] != target["source_revision"] or
                        selected[0] is not blocks[-1] or selected[0]["id"] in old_ids or
                        di.correlate(file_id, selected[0]["text"], "r", "claude-api")["task_id"]
                        != target["task_id"]):
                    outcomes.append({"status": "target_mismatch", "doc": file_id})
                    continue
            unseen = [block for block in blocks if block["id"] not in old_ids]
            # Keep the first-observation baseline immutable. A separately
            # approved ID is consumed; an unapproved ID stays pending through
            # queue failures and uncertain receipt readback.
            new = [block for block in unseen if
                   client.get(_approval_key(file_id, block["id"])) is None]
            if target is not None:
                new = [block for block in new if block["id"] == target["request_id"]]
            if not new:
                outcomes.append({"status": "duplicate" if unseen else "no_fresh_request",
                                 "doc": file_id})
                continue
            if len(new) != 1 or new[0] is not blocks[-1]:
                outcomes.append({"status": "ambiguous_new_requests", "doc": file_id})
                continue
            candidate = new[0]
            focus = focused_text(text, candidate["id"], candidate["revision"])
            tuple_ = di.correlate(file_id, focus, str(raw["revision_id"]), "claude-api")
            if client.get(_approval_key(file_id, candidate["id"])) is not None:
                outcomes.append({"status": "duplicate", "doc": file_id, "task": tuple_["task_id"]})
                continue
            if (client.get(_issued_key(file_id, candidate["id"])) is not None or
                    _requested_seen(text, tuple_) or
                    client.get("fleet:intake:put:" + tuple_["task_id"]) is not None):
                outcomes.append({"status": "approval_lapsed", "doc": file_id,
                                 "task": tuple_["task_id"]})
                continue
            expected[file_id] = (candidate["id"], candidate["revision"])
            outcomes.append({"status": "candidate", "doc": file_id, "task": tuple_["task_id"]})
        except Exception as exc:  # failed read/baseline means no admission or spend
            outcomes.append({"status": "intake_error", "doc": file_id, "error": type(exc).__name__})
    if target is not None and (len(outcomes) != len(DOC_IDS) or any(
            result.get("status") not in ("candidate", "non_target", "already_approved")
            for result in outcomes)):
        return outcomes  # validate both Docs before any targeted queue or approval
    if not expected:
        return outcomes
    docs = PilotDocs(raw_docs, expected)
    queue = di.InboxQueue(client)
    deps = di.Deps(enabled=True, docs=docs, receipts=docs, claims=PilotClaims(client),
                   queue=queue, changes=lambda _folder: [
                       {"file_id": file_id, "folder_id": FOLDER_ID} for file_id in expected],
                   ring=di.no_ring, folder_id=FOLDER_ID, api_worker="claude-api", now=now, at=at)
    polled = di.poll(deps)
    for result in polled:
        tuple_ = result.get("tuple") or {}
        file_id = tuple_.get("source_doc_id")
        if file_id not in expected:
            outcomes.append({"status": result.get("status", "unknown"), "doc": file_id})
            continue
        outcome = {"status": result.get("status", "unknown"), "doc": file_id,
                   "task": result.get("task"), "ring": "no_ring"}
        if result.get("receipt") == "written" and tuple_.get("recipient_id") == "claude-api":
            try:
                if queue.holder(tuple_["task_id"]) == "claude-api":
                    record = {name: tuple_[name] for name in (
                        "source_doc_id", "source_revision", "request_id", "task_id", "recipient_id")}
                    created = client.eval(APPROVE_ONCE_LUA, 2,
                                          _approval_key(file_id, tuple_["request_id"]),
                                          _issued_key(file_id, tuple_["request_id"]),
                                          _record_json(record), APPROVAL_TTL)
                    outcome["admitted"] = bool(created) and admitted(
                        {**record, "kind": "drive_request"}, client)
            except Exception as exc:
                outcome.update(status="approval_error", error=type(exc).__name__, admitted=False)
        outcomes.append(outcome)
    return outcomes


def target_ready(outcomes, target):
    """Every Doc was checked and only the pinned request is admitted."""
    return (len(outcomes) in (len(DOC_IDS), len(DOC_IDS) + 1) and
            sum(result.get("task") == target["task_id"] and result.get("admitted") is True
                for result in outcomes) == 1 and
            sum(result.get("status") == "non_target" for result in outcomes) == len(DOC_IDS) - 1 and
            all((result.get("status") == "non_target" and result.get("doc") != target["source_doc_id"])
                or (result.get("doc") == target["source_doc_id"] and
                    result.get("task") == target["task_id"] and
                    (result.get("status") == "candidate" or result.get("admitted") is True))
                for result in outcomes))


def target_recovery_ready(outcomes, target, claims):
    """Allow only an already claimed no-call outcome past a lapsed approval."""
    try:
        state = claims.state(target["task_id"])
    except Exception:
        return False
    if state is None or state.split(":", 1)[0] not in (
            APPROVAL_RECEIPT_OWED, APPROVAL_REFUND_UNKNOWN):
        return False
    return (len(outcomes) == len(DOC_IDS) and
            sum(result.get("doc") == target["source_doc_id"] and
                result.get("status") in ("approval_lapsed", "target_mismatch", "already_approved")
                for result in outcomes) == 1 and
            sum(result.get("status") == "non_target" and
                result.get("doc") != target["source_doc_id"]
                for result in outcomes) == len(DOC_IDS) - 1)

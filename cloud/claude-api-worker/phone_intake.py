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
        return view

    def append(self, file_id, expected_revision, receipt_id, block):
        doc = self._read_scoped(file_id)
        if self.require_expected:
            if file_id not in self.expected or not focused_text(
                    doc.get("text") or "", *self.expected[file_id]):
                raise OutOfScope("approved request changed before receipt append")
        return self.raw_docs.append(file_id, expected_revision, receipt_id, block)

    def has(self, file_id, receipt_id):
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

    def __init__(self, inbox, client):
        self.inbox, self.client = inbox, client

    def read(self, agent, count):
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


def run_prepass(client, raw_docs, *, enabled=False, now=None, at=None) -> list[dict]:
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
            old_ids = _baseline(client, file_id, [block["id"] for block in blocks])
            if old_ids is None:
                outcomes.append({"status": "baseline_initialized", "doc": file_id})
                continue
            unseen = [block for block in blocks if block["id"] not in old_ids]
            # Keep the first-observation baseline immutable. A separately
            # approved ID is consumed; an unapproved ID stays pending through
            # queue failures and uncertain receipt readback.
            new = [block for block in unseen if
                   client.get(_approval_key(file_id, block["id"])) is None]
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
            expected[file_id] = (candidate["id"], candidate["revision"])
            outcomes.append({"status": "candidate", "doc": file_id, "task": tuple_["task_id"]})
        except Exception as exc:  # failed read/baseline means no admission or spend
            outcomes.append({"status": "intake_error", "doc": file_id, "error": type(exc).__name__})
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
                    client.set(_approval_key(file_id, tuple_["request_id"]),
                               json.dumps(record, sort_keys=True, separators=(",", ":")),
                               nx=True, ex=APPROVAL_TTL)
                    outcome["admitted"] = admitted({**record, "kind": "drive_request"}, client)
            except Exception as exc:
                outcome.update(status="approval_error", error=type(exc).__name__, admitted=False)
        outcomes.append(outcome)
    return outcomes

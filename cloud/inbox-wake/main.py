#!/usr/bin/env python3
"""Inbox wake: a scale-to-zero doorbell. Stdlib HTTP. Default off.

Gemini, 2026-10-10: GitHub webhooks hit one Cloud Run service, which routes a
board or inbox message and wakes the recipient. Claude and Codex are woken by
GitHub Actions workflow_dispatch. Gemini is the existing Cloud Run job
gemini-waker. Grok is an HTTP endpoint (GROK_WAKE_URL). Processed ids live in
Redis (fleet:wake:seen:*) so a second delivery does not wake twice. A 15-minute
sweep is the fail-safe for a webhook that never arrived.

THE FLAG. INBOX_WAKE_ENABLED must be exactly "on". Anything else, including
unset, wakes nobody and does not open Redis. The image also sets the variable
to off. Turning it off is the rollback that does not need a new build.

THE RECEIPT. INBOX_WAKE_RECEIPTS must be exactly "on", and it does nothing
unless the wake flag is on too. On pickup, one responder entry is written
under fleet:wake:receipt:entry:<idempotency key> by a single SET NX with the
fleet TTL. That command is the claim. There is no read before it and no second
write after it, so three instances cannot check-then-set a second entry. A
repeat finds the key and writes nothing. A Redis timeout or outage is logged
and returned as error, and that pickup is not a success. Connect and read
timeouts on this service's client are short so a hung Redis fails fast. This
process never writes ACCEPTED; that word belongs to the worker that actually
takes the work (L-116). The image sets the variable to off. Turning it off is
the rollback that does not need a new build, and with it off the replies are
the ones this file already returned.

THIS PROCESS DOES NOT DECIDE THE REPLY. It rings the recipient. The reply still
has to come back through that agent's own path, inside five minutes, with the
laptop closed. The test plan for that is in the pull request, and it is not
run from here.

THE BOARD MIRROR. INBOX_WAKE_BOARD_MIRROR must be exactly "on". Anything else,
including unset, leaves POST /hooks/board as a no-op: no signature check, no
board read, no Redis. The image sets the variable to off. Rollback is that
variable, not a new build: set INBOX_WAKE_BOARD_MIRROR=off.

When the flag is on, an Apps Script trigger POSTs a small HMAC-SHA256 event
(secret name BOARD_HOOK_SECRET, value never in this file). The handler checks
the signature, fetches the row from the board by row_id, and only then writes.
The claim is one SET NX on fleet:board:mirrored:<row_id>. There is no GET
before it. The winner XADDs fleet:board:stream and fleet:inbox:<agent>, reads
each entry back, and returns the stream id and sha256. A mismatch or a Redis
timeout is an error, not a success. to=all is one board entry and one inbox
entry per fleet agent. reconcile_once mirrors the last few minutes in a single
pass and returns. It does not loop and it does not sleep.

Deploy is not this file's job. The command is in the pull request. Nothing
here is applied.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT = os.environ.get("RUN_PROJECT") or "sfdc24"
REGION = os.environ.get("RUN_REGION") or "us-central1"
SEEN_TTL = 2592000  # fleet: ttl in scripts/redis_acl.json; no new grant
# Shorter than scripts/redis_dual.CONNECT_SECONDS (10). A hung Memorystore
# must surface as an error while the request is still the request.
REDIS_CONNECT_TIMEOUT = 2.0
REDIS_READ_TIMEOUT = 2.0
# private-ranges-only routes these through the VPC connector and nothing else.
# RFC1918 plus RFC6598. Link-local is not on that path.
PRIVATE_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
)
MAX_BODY = 65536
ID_OK = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
FILE_ID_OK = re.compile(r"[A-Za-z0-9_-]{10,128}\Z")
KEY_OK = re.compile(r"[0-9a-f]{64}\Z")
COMMENT_LINE = re.compile(r"(?m)^inbox\s+to=(\S+)\s+id=(\S+)\s+by=(\S+)\s+text=(.*)$")
# Legal receipt labels. ACCEPTED is absent on purpose: only the worker writes it.
RECEIVED = "RECEIVED"
DISPATCHED = "DISPATCHED"
RECEIPT_STATES = (RECEIVED, DISPATCHED)
RECEIPT_ENTRY_PREFIX = "fleet:wake:receipt:entry:"
# fleet: is the prefix the ACL already grants. No new grant in this file.
BOARD_STREAM = "fleet:board:stream"
MIRROR_PREFIX = "fleet:board:mirrored:"
INBOX_PREFIX = "fleet:inbox:"
# scripts/redis_acl.json principals, minus owner (a person) and milestones-sync
# (not an agent inbox). to=all writes one entry to each of these.
BOARD_AGENTS = (
    "claude-code-cli",
    "chatgpt-codex-desktop",
    "grok",
    "gemini",
    "cursor",
    "copilot",
    "claude-api",
    "claude-mobile",
    "pi1-cli",
)
BOARD_AGENT_SET = frozenset(BOARD_AGENTS)
BCB_ID = re.compile(r"(?:^|\|)id=([A-Za-z0-9._-]{1,200})(?:\||$)")
# One pass. A wider window is still this many rows, then the function returns.
RECONCILE_MAX = 50
RECONCILE_MINUTES = 10
STREAM_MAXLEN = 10000
LOG = logging.getLogger("inbox_wake")

# Canonical ids are scripts/redis_acl.json principals. Aliases are the ones that
# file already maps onto exactly one of the four routes below.
ALIASES = {
    "vm-claude-code-cli": "claude-code-cli",
    "codex": "chatgpt-codex-desktop",
    "codex-desktop": "chatgpt-codex-desktop",
    "chatgpt-codex": "chatgpt-codex-desktop",
    "aya": "chatgpt-codex-desktop",
    "dot": "chatgpt-codex-desktop",
    "grok-bot": "grok",
}
# kind, target. The target is a workflow file, a Cloud Run job, or an env name.
ROUTES = {
    "claude-code-cli": ("github_actions", "inbox-wake-claude.yml"),
    "chatgpt-codex-desktop": ("github_actions", "inbox-wake-codex.yml"),
    "gemini": ("cloud_run_job", "gemini-waker"),
    "grok": ("http", "GROK_WAKE_URL"),
}


class Unconfigured(Exception):
    """A route has no credential or URL. The id is released so a later sweep can retry."""


def enabled_from_env(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get("INBOX_WAKE_ENABLED") == "on"


def receipts_enabled(environ=None) -> bool:
    """The receipt flag. Exactly "on". Anything else writes nothing back."""
    environ = os.environ if environ is None else environ
    return environ.get("INBOX_WAKE_RECEIPTS") == "on"


def host_on_private_range(host: str) -> bool:
    """True when private-ranges-only will send this host through the VPC connector.

    Memorystore's address is a private service-access IP. A public address is
    not on that route, and a hostname is not one either. Empty is not a host.
    """
    try:
        ip = ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return False
    return any(ip in network for network in PRIVATE_RANGES)


def apply_redis_timeouts(client, connect=REDIS_CONNECT_TIMEOUT, read=REDIS_READ_TIMEOUT):
    """Bound connect and read on a lazy redis-py client. No socket is opened here."""
    pool = getattr(client, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None) if pool is not None else None
    if isinstance(kwargs, dict):
        kwargs["socket_connect_timeout"] = connect
        kwargs["socket_timeout"] = read
    return client


def utc_now() -> str:
    """UTC ISO 8601, second precision, Zulu suffix. The receipt's received_at."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(agent: str):
    name = (agent or "").strip().lower()
    name = ALIASES.get(name, name)
    return name if name in ROUTES else None


class MemorySeen:
    """Process-local stand-in. Production uses RedisSeen; tests use this."""

    def __init__(self):
        self.ids = set()

    def claim(self, key: str) -> bool:
        if key in self.ids:
            return False
        self.ids.add(key)
        return True

    def release(self, key: str) -> None:
        self.ids.discard(key)


class RedisSeen:
    """SET NX on fleet:wake:seen:<agent>:<id>. A lost claim is a delete, not an overwrite."""

    def __init__(self, client):
        self.client = client

    def claim(self, key: str) -> bool:
        return bool(self.client.set("fleet:wake:seen:" + key, "1", nx=True, ex=SEEN_TTL))

    def release(self, key: str) -> None:
        self.client.delete("fleet:wake:seen:" + key)


def _bounded_id(value, pattern) -> str:
    value = str(value or "").strip()
    return value if pattern.fullmatch(value) else ""


def correlation_tuple(message, recipient_id: str) -> dict:
    """The tuple drive-intake carries, plus the recipient we actually picked up.

    source_revision and request_revision are the same value: the request
    revision (a hash of the request half of the doc), not Drive's revisionId.
    Board-origin messages have no doc, so those fields are empty strings.
    The recipient is the canonical agent this process is waking, not a field
    on the message, which a sender could set to someone else.
    """
    revision = _bounded_id(message.get("source_revision"), ID_OK) or _bounded_id(
        message.get("request_revision"), ID_OK)
    return {
        "source_doc_id": _bounded_id(message.get("source_doc_id"), FILE_ID_OK),
        "source_revision": revision,
        "request_revision": revision,
        "request_id": _bounded_id(message.get("request_id"), ID_OK),
        "task_id": _bounded_id(message.get("task_id"), ID_OK),
        "recipient_id": recipient_id,
    }


def idempotency_key(tuple_: dict, message_id: str = "") -> str:
    """sha256 over the request identity, joined by the same separator as drive-intake.

    When the doc, request revision, request id and task id are all present this
    is drive-intake's idempotency_key (PR 362): the recipient is not part of
    that identity. A board-origin message has those fields empty, and hashing
    the empty tuple would make every board message one key, so the message id
    and recipient are included in that case only.
    """
    doc = str(tuple_.get("source_doc_id") or "")
    rev = str(tuple_.get("request_revision") or tuple_.get("source_revision") or "")
    req = str(tuple_.get("request_id") or "")
    task = str(tuple_.get("task_id") or "")
    if doc and rev and req and task:
        parts = (doc, rev, req, task)
    else:
        parts = (str(message_id or ""), str(tuple_.get("recipient_id") or ""), doc, rev, req, task)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def receipt_entry(message, recipient_id: str, received_at: str) -> dict:
    """One responder entry. State is RECEIVED: the ring has not been confirmed yet."""
    mid = str(message.get("id") or "")
    correlation = correlation_tuple(message, recipient_id)
    return {
        "message_id": mid,
        "received_at": received_at,
        "recipient_id": recipient_id,
        "source_doc_id": correlation["source_doc_id"],
        "source_revision": correlation["source_revision"],
        "request_revision": correlation["request_revision"],
        "request_id": correlation["request_id"],
        "task_id": correlation["task_id"],
        "correlation": correlation,
        "idempotency_key": idempotency_key(correlation, mid),
        "state": RECEIVED,
    }


def _stamp(deps) -> str:
    clock = getattr(deps, "clock", None)
    if clock is None:
        return utc_now()
    stamp = clock() if callable(clock) else clock
    return str(stamp)


def _log_receipt(outcome, message_id, recipient_id, error) -> None:
    # Ids and the failure class only. The message text is not a log line.
    LOG.error(
        "inbox-wake receipt %s message_id=%s recipient_id=%s error=%s",
        outcome, message_id, recipient_id, error,
    )


class RedisReceipts:
    """One responder entry under fleet:. The entry is the claim.

    SET NX with the fleet TTL is the whole write. A duplicate NX writes
    nothing. A timeout or a refused write is an error, not a success, and the
    key is left alone: deleting it after a lost reply could let a second
    instance write a second entry.
    """

    def __init__(self, client):
        self.client = client

    def record(self, entry: dict) -> str:
        message_id = str(entry.get("message_id") or "")
        recipient_id = str(entry.get("recipient_id") or "")
        state = entry.get("state")
        if state not in RECEIPT_STATES:
            _log_receipt("error", message_id, recipient_id, "refused-state")
            return "error"
        key = str(entry.get("idempotency_key") or "")
        if not KEY_OK.fullmatch(key):
            _log_receipt("error", message_id, recipient_id, "bad-key")
            return "error"
        redis_key = RECEIPT_ENTRY_PREFIX + key
        try:
            payload = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            _log_receipt("error", message_id, recipient_id, type(exc).__name__)
            return "error"
        try:
            wrote = self.client.set(redis_key, payload, nx=True, ex=SEEN_TTL)
        except OSError as exc:
            # TimeoutError is an OSError. A hung or dead Redis is an error.
            _log_receipt("error", message_id, recipient_id, type(exc).__name__)
            return "error"
        except Exception as exc:  # noqa: BLE001 - not an outage, and not a success
            _log_receipt("unknown", message_id, recipient_id, type(exc).__name__)
            return "unknown"
        if wrote:
            return "written"
        if wrote is None:
            # NX lost: another instance already holds the entry. Nothing new.
            return "duplicate"
        _log_receipt("error", message_id, recipient_id, "write-rejected")
        return "error"


def _record_receipt(message, agent, deps) -> str:
    try:
        return deps.receipts.record(receipt_entry(message, agent, _stamp(deps)))
    except Exception as exc:  # noqa: BLE001 - a receipt that raises is not a received message
        _log_receipt("unknown", str(message.get("id") or ""), agent, type(exc).__name__)
        return "unknown"


def _receipt_failure(agent, mid, outcome) -> dict:
    return {"status": outcome, "woke": False, "to": agent, "id": mid, "receipt": outcome}


class Wakers:
    """One dispatch per route. Callables are injected so a test never opens a socket.

    GitHub Actions receives message_id and by only. The message text is not an
    input, because a workflow `run:` step that expands free text is a shell.
    Grok's endpoint gets the text in a JSON body, which is not a shell.
    """

    def __init__(self, github_token="", grok_url="", token_provider=None,
                 github=None, run_job=None, http_post=None):
        self.github_token = github_token or ""
        self.grok_url = grok_url or ""
        self.token_provider = token_provider
        self.github = github
        self.run_job = run_job
        self.http_post = http_post
        self.calls = []

    def dispatch(self, agent: str, route, message: dict) -> None:
        kind, target = route
        self.calls.append({
            "agent": agent, "kind": kind, "target": target,
            "id": message["id"], "by": message.get("by") or "",
        })
        if kind == "github_actions":
            if not self.github_token:
                raise Unconfigured("GITHUB_WAKE_TOKEN unset")
            inputs = {"message_id": message["id"], "by": message.get("by") or ""}
            if self.github:
                self.github(target, inputs)
                return
            github_dispatch(target, inputs, self.github_token)
            return
        if kind == "cloud_run_job":
            if self.run_job:
                self.run_job(target)
                return
            if not self.token_provider:
                raise Unconfigured("no metadata token for the Cloud Run waker")
            start_job(target, self.token_provider())
            return
        if kind == "http":
            if not self.grok_url:
                raise Unconfigured("GROK_WAKE_URL unset")
            body = {
                "id": message["id"], "to": agent,
                "by": message.get("by") or "", "text": message.get("text") or "",
            }
            if self.http_post:
                self.http_post(self.grok_url, body)
                return
            post_json(self.grok_url, body)
            return
        raise Unconfigured("unknown route %s" % kind)


def decide(message: dict, deps) -> dict:
    """One message. Disabled, unknown, duplicate, and unconfigured all wake nobody."""
    if not deps.enabled:
        return {"status": "disabled", "woke": False}
    agent = canonical(message.get("to") or "")
    if not agent:
        return {"status": "unknown_agent", "woke": False}
    mid = str(message.get("id") or "")
    if not ID_OK.fullmatch(mid):
        return {"status": "bad_id", "woke": False}
    by = str(message.get("by") or "")
    if by and not ID_OK.fullmatch(by):
        by = ""
    if deps.seen is None:
        return {"status": "no_store", "woke": False, "to": agent, "id": mid}
    key = "%s:%s" % (agent, mid)
    # Receipts are a second flag. No writer means the flag is off, and the
    # rest of this function is the path it had before the receipt existed.
    try:
        claimed = deps.seen.claim(key)
    except Exception as exc:  # noqa: BLE001 - a dead Redis must not read as a wake
        if deps.receipts is None:
            raise
        _log_receipt("error", mid, agent, type(exc).__name__)
        try:
            deps.seen.release(key)
        except Exception:  # noqa: BLE001 - the outage is already the result
            _log_receipt("error", mid, agent, "seen-release")
        return {"status": "error", "woke": False, "to": agent, "id": mid,
                "error": type(exc).__name__, "receipt": "error"}
    if not claimed:
        if deps.receipts is not None:
            receipt = _record_receipt(message, agent, deps)
            if receipt in ("error", "unknown"):
                return _receipt_failure(agent, mid, receipt)
        return {"status": "duplicate", "woke": False, "to": agent, "id": mid}
    receipt = None
    if deps.receipts is not None:
        receipt = _record_receipt(message, agent, deps)
        if receipt in ("error", "unknown"):
            deps.seen.release(key)
            return _receipt_failure(agent, mid, receipt)
    try:
        deps.wakers.dispatch(agent, ROUTES[agent], {
            "id": mid, "to": agent, "by": by, "text": message.get("text") or "",
        })
    except Unconfigured as exc:
        deps.seen.release(key)
        return {"status": "unconfigured", "woke": False, "to": agent, "id": mid,
                "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a failed ring must be retryable
        deps.seen.release(key)
        return {"status": "error", "woke": False, "to": agent, "id": mid,
                "error": type(exc).__name__}
    kind, target = ROUTES[agent]
    result = {"status": "woke", "woke": True, "to": agent, "id": mid,
              "route": kind, "target": target}
    if receipt is not None:
        result["receipt"] = receipt
    return result


def sweep(deps) -> list:
    """Read each inbox and wake what the webhook missed. Flag off reads nothing."""
    if not deps.enabled:
        return []
    if deps.reader is None or deps.seen is None:
        return [{"status": "no_store", "woke": False}]
    out = []
    for agent in ROUTES:
        try:
            entries = deps.reader(agent) or []
        except Exception as exc:  # noqa: BLE001 - one inbox must not sink the others
            out.append({"status": "read_error", "woke": False, "to": agent,
                        "error": type(exc).__name__})
            continue
        for entry in entries:
            item = dict(entry)
            item["to"] = agent
            out.append(decide(item, deps))
    return out


def comment_messages(text: str) -> list:
    return [{"to": m.group(1), "id": m.group(2), "by": m.group(3), "text": m.group(4).strip()}
            for m in COMMENT_LINE.finditer(text or "")]


def parse_event(event: str, body: bytes) -> list:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if event == "issue_comment":
        if data.get("action") not in ("created", "edited"):
            return []
        return comment_messages((data.get("comment") or {}).get("body") or "")
    if event == "repository_dispatch":
        payload = data.get("client_payload") or {}
        if payload.get("id") and payload.get("to"):
            return [payload]
        return []
    if isinstance(data, dict) and data.get("id") and data.get("to"):
        return [data]
    return []


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    """GitHub X-Hub-Signature-256. An empty secret is a refusal, not a skip."""
    if not secret:
        return False
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, header or "")


def bearer_ok(secret: str, header: str) -> bool:
    if not secret:
        return False
    return hmac.compare_digest("Bearer " + secret, header or "")


def board_mirror_enabled(environ=None) -> bool:
    """The mirror flag. Exactly "on". Anything else writes nothing."""
    environ = os.environ if environ is None else environ
    return environ.get("INBOX_WAKE_BOARD_MIRROR") == "on"


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if value is None:
        return ""
    return str(value)


def _field(fields, name: str) -> str:
    if not isinstance(fields, dict):
        return ""
    value = fields.get(name)
    if value is None:
        value = fields.get(name.encode("utf-8"))
    return _text(value) if value is not None else ""


def bcb_id(payload: str) -> str:
    match = BCB_ID.search(str(payload or ""))
    return match.group(1) if match else ""


def row_view(cells) -> dict:
    """One board row as the mirror stores it. Content comes from the cells."""
    cells = list(cells or [])
    while len(cells) < 6:
        cells.append("")
    payload = _text(cells[5])
    return {
        "row_id": _text(cells[0]).strip(),
        "appended_at": _text(cells[1]),
        "from": _text(cells[2]),
        "to": _text(cells[3]),
        "phase": _text(cells[4]),
        "payload": payload,
        "id": bcb_id(payload),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def sheet_row_text(value) -> str:
    """A locator from the event. Not a source of row content."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int) and 1 <= value <= 1000000:
        return str(value)
    text = _text(value).strip()
    if text.isdigit() and 1 <= int(text) <= 1000000:
        return str(int(text))
    return ""


def inbox_targets(to_field: str) -> list:
    """The inbox keys a row addresses. `all` is every fleet agent, once."""
    tokens = []
    for part in re.split(r"[;,]", str(to_field or "")):
        name = part.strip().lower()
        if name:
            tokens.append(ALIASES.get(name, name))
    if not tokens:
        return []
    if any(name in ("all", "*") for name in tokens):
        return list(BOARD_AGENTS)
    out = []
    for name in tokens:
        if name in BOARD_AGENT_SET and name not in out:
            out.append(name)
    return out


def stream_fields(view: dict, sheet_row: str) -> dict:
    payload = view["payload"]
    if len(payload.encode("utf-8")) > 2048:
        payload = ""
    return {
        "row_id": view["row_id"],
        "sheet_row": sheet_row,
        "appended_at": view["appended_at"],
        "from": view["from"],
        "to": view["to"],
        "phase": view["phase"],
        "id": view["id"],
        "sha256": view["sha256"],
        "payload": payload,
    }


def _entry_sha(rows, entry_id: str) -> str:
    for eid, fields in rows or ():
        if entry_id and _text(eid) != entry_id:
            continue
        return _field(fields, "sha256")
    return ""


class RedisBoard:
    """SET NX is the claim. XADD happens only after that claim is won."""

    def __init__(self, client):
        self.client = client

    def claim(self, row_id: str, marker: str) -> str:
        try:
            wrote = self.client.set(MIRROR_PREFIX + row_id, marker, nx=True, ex=SEEN_TTL)
        except OSError as exc:
            LOG.error("board mirror error row_id=%s error=%s", row_id, type(exc).__name__)
            return "error"
        except Exception as exc:  # noqa: BLE001 - not a success
            LOG.error("board mirror error row_id=%s error=%s", row_id, type(exc).__name__)
            return "error"
        if wrote:
            return "claimed"
        if wrote is None:
            return "duplicate"
        LOG.error("board mirror error row_id=%s error=write-rejected", row_id)
        return "error"


def _xadd_checked(client, key: str, fields: dict, expected: str, row_id: str):
    """XADD, then read that entry back. A mismatch is an error, not a success."""
    try:
        entry_id = _text(client.xadd(
            key, fields, maxlen=STREAM_MAXLEN, approximate=True))
    except OSError as exc:
        LOG.error("board mirror error row_id=%s stream=%s error=%s",
                  row_id, key, type(exc).__name__)
        return "", "error"
    except Exception as exc:  # noqa: BLE001
        LOG.error("board mirror error row_id=%s stream=%s error=%s",
                  row_id, key, type(exc).__name__)
        return "", "error"
    LOG.info("board mirror stream=%s stream_id=%s row_id=%s sha256=%s",
             key, entry_id, row_id, expected)
    try:
        rows = client.xrange(key, min=entry_id, max=entry_id)
    except OSError as exc:
        LOG.error("board mirror readback row_id=%s stream=%s error=%s",
                  row_id, key, type(exc).__name__)
        return entry_id, "error"
    except Exception as exc:  # noqa: BLE001
        LOG.error("board mirror readback row_id=%s stream=%s error=%s",
                  row_id, key, type(exc).__name__)
        return entry_id, "error"
    if _entry_sha(rows, entry_id) != expected:
        LOG.error("board mirror readback mismatch row_id=%s stream=%s stream_id=%s",
                  row_id, key, entry_id)
        return entry_id, "error"
    return entry_id, "ok"


def mirror_row(cells, deps, sheet_row="") -> dict:
    """Claim the row, then write the board stream and the addressed inboxes.

    The cells are the board's row, already fetched. The event body is not read
    here. A lost claim writes nothing. A Redis failure returns error and does
    not delete the claim: deleting it could let a second instance append again.
    """
    view = row_view(cells)
    row_id = view["row_id"]
    if not ID_OK.fullmatch(row_id):
        return {"enabled": True, "ok": False, "status": "bad_id", "row_id": row_id}
    if deps.board is None:
        return {"enabled": True, "ok": False, "status": "error", "error": "no_store",
                "row_id": row_id}
    claim = deps.board.claim(row_id, view["sha256"])
    if claim == "duplicate":
        return {"enabled": True, "ok": True, "status": "duplicate", "mirrored": False,
                "row_id": row_id}
    if claim != "claimed":
        return {"enabled": True, "ok": False, "status": "error", "error": "redis",
                "row_id": row_id}
    fields = stream_fields(view, sheet_row_text(sheet_row))
    stream_id, outcome = _xadd_checked(
        deps.board.client, BOARD_STREAM, fields, view["sha256"], row_id)
    if outcome != "ok":
        return {"enabled": True, "ok": False, "status": "error", "error": "redis",
                "row_id": row_id, "stream_id": stream_id, "sha256": view["sha256"]}
    inboxes = []
    for agent in inbox_targets(view["to"]):
        key = INBOX_PREFIX + agent
        inbox_id, inbox_outcome = _xadd_checked(
            deps.board.client, key, fields, view["sha256"], row_id)
        if inbox_outcome != "ok":
            return {"enabled": True, "ok": False, "status": "error", "error": "redis",
                    "row_id": row_id, "stream_id": stream_id, "sha256": view["sha256"],
                    "inboxes": inboxes}
        inboxes.append({"key": key, "stream_id": inbox_id, "sha256": view["sha256"]})
    return {
        "enabled": True, "ok": True, "status": "mirrored", "mirrored": True,
        "row_id": row_id, "stream_id": stream_id, "sha256": view["sha256"],
        "inboxes": inboxes,
    }


def fetch_board_row(row_id, env=None, reader=None):
    """The row whose Row_ID is exactly row_id. A substring hit is not a row."""
    if reader is None:
        import bus  # noqa: PLC0415 - the image copies scripts/bus.py to /app
        env = bus.load_env() if env is None else env
        found = bus.read_rows(env, match=row_id)
        rows = found.get("rows") or []
    else:
        rows = reader(row_id) or []
    exact = [row for row in rows if row and _text(row[0]).strip() == str(row_id)]
    if len(exact) != 1:
        raise LookupError("row")
    return exact[0]


def fetch_board_window(since, env=None, reader=None):
    """Rows since `since`, capped. One read. The caller does not loop."""
    if reader is None:
        import bus  # noqa: PLC0415
        env = bus.load_env() if env is None else env
        found = bus.read_rows(env, since=since, limit=RECONCILE_MAX)
        return found.get("rows") or []
    return list(reader(since) or [])


def reconcile_once(deps, minutes=RECONCILE_MINUTES, now=None) -> dict:
    """Mirror recent rows that lack the SET NX marker. One pass, then return.

    There is no sleep and no second pass. The marker test is the SET NX inside
    mirror_row, not an EXISTS followed by a write.
    """
    if not getattr(deps, "board_mirror", False):
        return {"enabled": False, "ok": True, "mirrored": [], "skipped": [], "seen": 0}
    if deps.board is None or deps.board_window is None:
        return {"enabled": True, "ok": False, "status": "error", "error": "no_store",
                "mirrored": [], "skipped": [], "seen": 0}
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = RECONCILE_MINUTES
    minutes = max(1, min(60, minutes))
    when = now or datetime.now(timezone.utc)
    if isinstance(when, str):
        when = datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    since = (when - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = list(deps.board_window(since) or [])
    except Exception as exc:  # noqa: BLE001 - a failed read is not an empty board
        LOG.error("board mirror reconcile error=%s", type(exc).__name__)
        return {"enabled": True, "ok": False, "status": "error", "error": type(exc).__name__,
                "mirrored": [], "skipped": [], "seen": 0, "since": since}
    rows = rows[:RECONCILE_MAX]
    mirrored, skipped = [], []
    failed = False
    for cells in rows:
        result = mirror_row(cells, deps)
        if result.get("status") == "mirrored":
            mirrored.append({
                "row_id": result.get("row_id"),
                "stream_id": result.get("stream_id"),
                "sha256": result.get("sha256"),
            })
        else:
            if result.get("status") == "error":
                failed = True
            skipped.append({"row_id": result.get("row_id"), "status": result.get("status")})
    return {
        "enabled": True, "ok": not failed, "status": "error" if failed else "done",
        "seen": len(rows), "mirrored": mirrored, "skipped": skipped, "since": since,
    }


def _board_status(result) -> int:
    status = result.get("status")
    if result.get("ok") and status in ("mirrored", "duplicate", "done", "disabled"):
        return 200
    if status == "bad_id":
        return 400
    if result.get("error") in ("LookupError", "row"):
        return 502
    return 503


def handle_board(headers: dict, body: bytes, deps) -> tuple:
    """POST /hooks/board. Flag off is a no-op before the signature or Redis."""
    if not getattr(deps, "board_mirror", False):
        return 200, {"enabled": False, "mirrored": False}
    header = headers.get("x-board-signature-256", "")
    if not signature_ok(getattr(deps, "board_secret", "") or "", body, header):
        return 401, {"error": "bad signature"}
    try:
        event = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"ok": False, "status": "bad_json", "error": "bad json"}
    if not isinstance(event, dict):
        return 400, {"ok": False, "status": "bad_json", "error": "bad json"}
    row_id = _text(event.get("row_id")).strip()
    if not ID_OK.fullmatch(row_id):
        return 400, {"ok": False, "status": "bad_id", "error": "bad row_id"}
    fetch = getattr(deps, "board_fetch", None)
    if fetch is None or deps.board is None:
        return 503, {"ok": False, "status": "error", "error": "no_store", "row_id": row_id}
    try:
        cells = fetch(row_id)
    except Exception as exc:  # noqa: BLE001 - the event body is not a substitute
        LOG.error("board mirror fetch row_id=%s error=%s", row_id, type(exc).__name__)
        return 502, {"ok": False, "status": "error", "error": type(exc).__name__,
                     "row_id": row_id}
    if not cells or _text(cells[0]).strip() != row_id:
        return 502, {"ok": False, "status": "error", "error": "row-mismatch", "row_id": row_id}
    result = mirror_row(cells, deps, sheet_row=event.get("sheet_row"))
    return _board_status(result), result


def handle_reconcile(headers: dict, deps) -> tuple:
    """One reconcile pass. Flag off does not read the board."""
    if not getattr(deps, "board_mirror", False):
        return 200, {"enabled": False, "mirrored": []}
    if not bearer_ok(deps.sweep_token, headers.get("authorization", "")):
        return 401, {"error": "bad sweep token"}
    result = reconcile_once(deps)
    return (200 if result.get("ok") else 503), result


def handle(method: str, path: str, headers: dict, body: bytes, deps) -> tuple:
    path = path.split("?", 1)[0]
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if method == "GET" and path == "/healthz":
        return 200, {"ok": True, "enabled": bool(deps.enabled)}
    if method == "POST" and path == "/wake":
        if not deps.enabled:
            return 200, {"enabled": False, "woke": []}
        if not signature_ok(deps.webhook_secret, body, headers.get("x-hub-signature-256", "")):
            return 401, {"error": "bad signature"}
        messages = parse_event(headers.get("x-github-event", ""), body)
        return 200, {"enabled": True, "woke": [decide(m, deps) for m in messages]}
    if method == "POST" and path == "/sweep":
        if not deps.enabled:
            return 200, {"enabled": False, "woke": []}
        if not bearer_ok(deps.sweep_token, headers.get("authorization", "")):
            return 401, {"error": "bad sweep token"}
        return 200, {"enabled": True, "woke": sweep(deps)}
    if method == "POST" and path == "/hooks/board":
        return handle_board(headers, body, deps)
    if method == "POST" and path == "/hooks/board/reconcile":
        return handle_reconcile(headers, deps)
    return 404, {"error": "not found"}


def github_dispatch(workflow: str, inputs: dict, token: str) -> None:
    url = ("https://api.github.com/repos/sfdc-24/Blackboard/actions/workflows/%s/dispatches"
           % workflow)
    raw = json.dumps({"ref": "main", "inputs": inputs}).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "sfdc24-inbox-wake",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def start_job(job: str, token: str) -> None:
    """The same v2 :run call cloud/board-watcher/main.py uses for gemini-waker."""
    url = ("https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s:run"
           % (PROJECT, REGION, job))
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def post_json(url: str, body: dict) -> None:
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST", headers={
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def read_inbox(client, agent: str) -> list:
    """fleet:inbox:<agent> entries, the shape redis_gov's xadd writes."""
    rows = client.xrange("fleet:inbox:" + agent, count=50)
    out = []
    for entry_id, fields in rows or ():
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode("utf-8")

        def field(name, fields=fields):
            value = fields.get(name)
            if value is None:
                value = fields.get(name.encode("utf-8"))
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            return value or ""

        item = {"id": str(entry_id), "text": field("text"), "by": field("by"),
                "at": field("at")}
        for name in ("source_doc_id", "source_revision", "request_revision",
                     "request_id", "task_id", "recipient_id"):
            item[name] = field(name)
        out.append(item)
    return out


class Deps:
    def __init__(self, enabled=False, seen=None, wakers=None, reader=None,
                 webhook_secret="", sweep_token="", receipts=None, clock=None,
                 board_mirror=False, board_secret="", board_fetch=None,
                 board_window=None, board=None):
        self.enabled = enabled
        self.seen = seen
        self.wakers = wakers or Wakers()
        self.reader = reader
        self.webhook_secret = webhook_secret or ""
        self.sweep_token = sweep_token or ""
        self.receipts = receipts
        self.clock = clock
        self.board_mirror = board_mirror
        self.board_secret = board_secret or ""
        self.board_fetch = board_fetch
        self.board_window = board_window
        self.board = board


def attach_store(deps, client, receipts=None):
    """Seen-set and inbox reader on one fleet client. Receipts follow the flag.

    `receipts` None reads INBOX_WAKE_RECEIPTS. The wake flag is the caller's:
    this function is only reached when that flag is already on.
    """
    if receipts is None:
        receipts = receipts_enabled()
    deps.seen = RedisSeen(client)
    deps.reader = lambda agent, client=client: read_inbox(client, agent)
    deps.receipts = RedisReceipts(client) if receipts else None
    return deps


def _attach_board(deps, client):
    """Board mirror on the same client. Wake's seen-set is not this path."""
    deps.board = RedisBoard(client)
    deps.board_fetch = fetch_board_row
    deps.board_window = fetch_board_window
    return deps


def deps_from_env() -> Deps:
    """Build the runtime deps. Redis stays closed until a flag is exactly on.

    The wake flag and the mirror flag are independent. Mirror off does not
    open Redis by itself and does not change a wake. Wake off does not mirror.
    """
    on = enabled_from_env()
    mirror = board_mirror_enabled()
    deps = Deps(
        enabled=on,
        board_mirror=mirror,
        board_secret=os.environ.get("BOARD_HOOK_SECRET") or "",
        webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET") or "",
        sweep_token=os.environ.get("INBOX_WAKE_SWEEP_TOKEN") or "",
        wakers=Wakers(
            github_token=os.environ.get("GITHUB_WAKE_TOKEN") or "",
            grok_url=os.environ.get("GROK_WAKE_URL") or "",
            token_provider=_metadata_token if on else None,
        ),
    )
    if not on and not mirror:
        return deps
    client = _redis_client()
    if client is None:
        return deps
    if on:
        attach_store(deps, client)
    if mirror:
        _attach_board(deps, client)
    return deps


def _metadata_token() -> str:
    import state_store  # noqa: PLC0415 - present on the Cloud Run image path, not required for tests
    return state_store.metadata_token()[0]


def _redis_client():
    """The fleet's client, or None. A missing client refuses wakes; it does not fall open.

    Connect and read are capped here, on this service's client only, after
    redis_dual builds it and before the first command (the client connects
    lazily). A public REDIS_HOST is not on the private-ranges-only path to
    Memorystore, so it is refused before a socket. No network setting is changed.
    """
    try:
        import redis_dual  # noqa: PLC0415
    except ImportError:
        return None
    settings = redis_dual.Settings()
    host = str(settings.host or "").strip()
    if host and not host_on_private_range(host):
        LOG.error("inbox-wake redis host is outside private-ranges-only; not connecting")
        return None
    client = redis_dual.client(settings, precheck=False)
    if client is not None:
        apply_redis_timeouts(client)
    return client


class Handler(BaseHTTPRequestHandler):
    deps = Deps()

    def do_GET(self):
        self._reply(*handle("GET", self.path, dict(self.headers), b"", self.deps))

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            self._reply(413, {"error": "body too large"})
            return
        body = self.rfile.read(n) if n else b""
        self._reply(*handle("POST", self.path, dict(self.headers), body, self.deps))

    def _reply(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return  # a request line can carry the message text; do not write it


def serve() -> None:
    Handler.deps = deps_from_env()
    port = int(os.environ.get("PORT") or "8080")
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()

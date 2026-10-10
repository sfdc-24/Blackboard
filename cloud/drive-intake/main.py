#!/usr/bin/env python3
"""Drive intake: a doc in the owner's folder becomes ONE correlated task. Default off.

WHY THIS EXISTS
  Mr. Salam speaks on claude-mobile, claude-mobile writes a Drive doc, and
  nothing happens until a human notices. On 2026-10-10 his trigger doc sat
  unread for 4 h 38 min on this surface and landed on the board three times
  from WhatsApp with no dedupe. That is the broken telephone.

WHAT IT IS NOT
  It is not a second wake system. The ring is cloud/inbox-wake (Blackboard
  #359) and the queue is fleet:inbox:<canonical agent id>, both already agreed.
  This service only turns a Drive notification into one validated, correlated,
  deduplicated task and writes an honest receipt back into the SAME doc.

THE FLAG
  DRIVE_INTAKE_ENABLED must be exactly "on". Anything else, including unset,
  reads no doc, claims nothing, dispatches nothing and writes no receipt. The
  image sets it off; turning it off is the rollback that needs no new build.

THE FOUR THINGS THAT GO WRONG, AND WHERE EACH IS STOPPED
  1. A receipt retriggers the task it is reporting. The request revision is a
     hash of the doc with the receipt section REMOVED (request_revision), never
     Drive's own revisionId, which every receipt append bumps. See strip_receipts.
  2. The same change arrives twice - push and poll, or Drive's own retry. One
     deterministic idempotency key over the whole correlation tuple, claimed
     SET NX before any dispatch. A failed dispatch RELEASES the claim.
  3. "Picked up" is narrated by something that cannot pick anything up. This
     service can only reach REQUESTED and DISPATCHED. ACCEPTED belongs to the
     named worker and is written by the worker (L-116).
  4. The Drive watch channel expires (7 days, max) and the day-8 outage is
     silent. /healthz reports the expiry and goes DEGRADED before it lands;
     missing evidence reads "unknown", never "healthy".

Deploy is not this file's job: the command, the grants it needs and the test
plan are in the pull request. Nothing here is applied, and this service holds
no credential of its own - every outside call is an injected adapter.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 65536
MAX_DOC = 400000          # a doc larger than this is refused, not truncated (PR 354's rule)
MAX_GIST = 900
CLAIM_TTL = 2592000       # 30 days, the ttl cloud/inbox-wake already uses for fleet: keys
DEFAULT_HEARTBEAT = 300   # the owner's suggested 5 minutes
DEFAULT_RENEW_WARN = 172800

ID_OK = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
FILE_ID_OK = re.compile(r"[A-Za-z0-9_-]{10,128}\Z")

# The heading that separates the owner's REQUEST from the trail of receipts.
# Everything from the first match onwards is receipts and is excluded from the
# request revision, so appending a receipt can never look like a new request.
RECEIPTS_HEADING = re.compile(r"(?mi)^\s*(?:#+\s*)?(?:CONVERSPAN\s+)?RECEIPTS\s*:?\s*$")

TO_LINE = re.compile(r"(?mi)^\s*(?:#+\s*)?to\s*[:=]\s*([A-Za-z0-9_.:-]{1,128})\s*$")
REQUEST_LINE = re.compile(r"(?mi)^\s*(?:#+\s*)?request\s*[:=]\s*([A-Za-z0-9_.:-]{1,128})\s*$")
NEEDS_CLI = re.compile(r"(?i)\b(?:sf cli|sfdx|scratch org|salesforce cli|laptop only|local repo)\b")

# fleet:inbox:<canonical agent id>, published and agreed 2026-10-09 (board row
# CCC-REDIS-INBOX-KEY-20261009T2140Z). Aliases are the ones that file maps.
ALIASES = {
    "claude": "claude-code-cli",
    "vm-claude-code-cli": "claude-code-cli",
    "claude-mobile": "claude-code-cli",
    "codex": "chatgpt-codex-desktop",
    "aya": "chatgpt-codex-desktop",
    "grok-bot": "grok",
}
WORKERS = ("claude-code-cli", "claude-api", "chatgpt-codex-desktop", "gemini", "grok")

REQUESTED = "REQUESTED"
DISPATCHED = "DISPATCHED"
QUEUED = "QUEUED"
BLOCKED = "BLOCKED"
UNKNOWN = "UNKNOWN"


class Unconfigured(Exception):
    """A hop has no credential, URL or named worker. The claim is released so a sweep retries."""


def enabled_from_env(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get("DRIVE_INTAKE_ENABLED") == "on"


def canonical(agent):
    name = (agent or "").strip().lower()
    name = ALIASES.get(name, name)
    return name if name in WORKERS else None


def strip_receipts(text: str) -> str:
    """The REQUEST half of the doc: everything before the receipts heading.

    This is the whole anti-retrigger mechanism. Drive's revisionId changes on
    every append, so a receipt written by this service would otherwise read as
    a new request revision and the doc would trigger itself forever - one
    receipt, one task, one receipt, with no upper bound and his money on every
    turn. Hash the half the owner writes and nothing we write.
    """
    match = RECEIPTS_HEADING.search(text or "")
    return (text or "")[:match.start()] if match else (text or "")


def request_revision(text: str) -> str:
    """sha256 over the request half, whitespace-normalised per line.

    Normalised because Docs rewrites trailing spaces and line endings on its
    own; an invisible edit must not look like a new request.
    """
    body = "\n".join(line.rstrip() for line in strip_receipts(text).splitlines()).strip()
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def gist(text: str, limit: int = MAX_GIST) -> dict:
    """A bounded gist that SAYS when it is short of the whole thing (PR 354: never trim silently)."""
    body = " ".join(strip_receipts(text).split())
    if len(body) <= limit:
        return {"text": body, "chars": len(body), "full": True}
    return {"text": body[:limit], "chars": len(body), "full": False}


def needs_cli_tools(text: str) -> bool:
    return bool(NEEDS_CLI.search(strip_receipts(text) or ""))


def correlate(file_id: str, text: str, drive_revision: str, recipient: str) -> dict:
    """The tuple every hop must carry unchanged. Deterministic in all five parts."""
    revision = request_revision(text)
    found = REQUEST_LINE.search(strip_receipts(text) or "")
    request_id = found.group(1) if found else ("req-" + revision[:16])
    task_id = "task-" + hashlib.sha256(
        "\x1f".join((file_id, revision, request_id, recipient)).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "source_doc_id": file_id,
        "source_revision": revision,          # the REQUEST half, not Drive's revisionId
        "drive_revision": drive_revision or "",   # tracked separately, for the write guard only
        "request_id": request_id,
        "task_id": task_id,
        "recipient_id": recipient,
    }


def idempotency_key(tuple_: dict) -> str:
    return hashlib.sha256("\x1f".join((
        tuple_["source_doc_id"], tuple_["source_revision"],
        tuple_["request_id"], tuple_["task_id"], tuple_["recipient_id"],
    )).encode("utf-8")).hexdigest()


def tuple_ok(tuple_: dict) -> bool:
    """Reject a missing or malformed identity rather than start a generic reader."""
    if not isinstance(tuple_, dict):
        return False
    if not FILE_ID_OK.fullmatch(tuple_.get("source_doc_id") or ""):
        return False
    for key in ("source_revision", "request_id", "task_id"):
        if not ID_OK.fullmatch(tuple_.get(key) or ""):
            return False
    return canonical(tuple_.get("recipient_id")) == tuple_.get("recipient_id")


def pick_worker(needs_cli: bool, heartbeat_age, api_worker: str):
    """The owner's rule: one or the other, NEVER neither and never silently.

    Returns (recipient, state). A heartbeat inside the threshold means the
    laptop is up and takes the work. Otherwise work that needs laptop-only
    tools is QUEUED for it by name, and everything else goes to the named
    always-on worker - but only if one is actually named and accepted. No
    named worker is BLOCKED, which is a receipt the phone can read, not a
    silence.
    """
    if heartbeat_age is not None and heartbeat_age >= 0:
        return "claude-code-cli", REQUESTED
    if needs_cli:
        return "claude-code-cli", QUEUED
    worker = canonical(api_worker)
    if worker:
        return worker, REQUESTED
    return "claude-code-cli", BLOCKED


def heartbeat_age(store, now, agent="claude-code-cli", window=DEFAULT_HEARTBEAT):
    """Seconds since the agent's last alive key, or None when it is stale or absent.

    Absence is offline. A key we cannot read is also offline: an unreadable
    heartbeat must never be read as "probably up".
    """
    if store is None:
        return None
    try:
        at = store.heartbeat(agent)
    except Exception:  # noqa: BLE001 - an unreadable heartbeat is an absent heartbeat
        return None
    if at is None:
        return None
    try:
        age = float(now) - float(at)
    except (TypeError, ValueError):
        return None
    return age if 0 <= age <= window else None


def receipt_line(state: str, tuple_: dict, at: str, detail: str = "") -> str:
    """One line, naming the state, the task and who says so. No claim beyond the state."""
    parts = [
        state,
        "task " + tuple_["task_id"],
        "request " + tuple_["request_id"],
        "rev " + tuple_["source_revision"][:12],
        "recipient " + tuple_["recipient_id"],
        "by drive-intake",
        at,
    ]
    if detail:
        parts.append(detail)
    return " - ".join(parts)


def write_receipt(deps, tuple_: dict, state: str, at: str, detail: str = "") -> str:
    """Append one receipt, then READ IT BACK. A lost response is UNKNOWN, never success.

    The guard is Drive's own revisionId (the write revision), kept apart from
    the request revision above: one is concurrency, the other is identity.
    """
    if deps.receipts is None:
        return "no_receipt_writer"
    line = receipt_line(state, tuple_, at, detail)
    receipt_id = hashlib.sha256((tuple_["task_id"] + state + line).encode("utf-8")).hexdigest()[:16]
    try:
        deps.receipts.append(tuple_["source_doc_id"], tuple_.get("drive_revision") or "",
                             receipt_id, line)
    except Exception as exc:  # noqa: BLE001 - the write may have landed anyway
        try:
            if deps.receipts.has(tuple_["source_doc_id"], receipt_id):
                return "written"
        except Exception:  # noqa: BLE001
            return "unknown:" + type(exc).__name__
        return "unknown:" + type(exc).__name__
    try:
        return "written" if deps.receipts.has(tuple_["source_doc_id"], receipt_id) else "not_read_back"
    except Exception as exc:  # noqa: BLE001
        return "unknown:" + type(exc).__name__


def decide(hint: dict, deps) -> dict:
    """One Drive notification. Disabled, out of scope, duplicate and unreadable all do nothing."""
    if not deps.enabled:
        return {"status": "disabled", "task": None}
    file_id = str(hint.get("file_id") or "")
    if not FILE_ID_OK.fullmatch(file_id):
        return {"status": "bad_file_id", "task": None}
    if deps.folder_id and hint.get("folder_id") and hint["folder_id"] != deps.folder_id:
        return {"status": "out_of_scope", "task": None}
    if deps.docs is None or deps.claims is None:
        return {"status": "no_store", "task": None}

    # A notification is a HINT. The doc is the authority, so read it.
    try:
        doc = deps.docs.read(file_id)
    except Exception as exc:  # noqa: BLE001 - an unread doc is not an absent request
        return {"status": "read_error", "task": None, "error": type(exc).__name__}
    if not isinstance(doc, dict):
        return {"status": "read_error", "task": None, "error": "shape"}
    if deps.folder_id and doc.get("folder_id") and doc["folder_id"] != deps.folder_id:
        return {"status": "out_of_scope", "task": None}
    text = doc.get("text") or ""
    if len(text) > MAX_DOC:
        return {"status": "too_large", "task": None, "chars": len(text)}
    if not strip_receipts(text).strip():
        return {"status": "no_request", "task": None}

    addressed = TO_LINE.search(strip_receipts(text))
    wants_cli = needs_cli_tools(text)
    alive = heartbeat_age(deps.claims, deps.now, window=deps.heartbeat_window)
    if addressed and canonical(addressed.group(1)):
        recipient, state = canonical(addressed.group(1)), REQUESTED
        if recipient == "claude-code-cli" and alive is None:
            state = QUEUED if wants_cli else REQUESTED
    else:
        recipient, state = pick_worker(wants_cli, alive, deps.api_worker)

    tuple_ = correlate(file_id, text, doc.get("revision_id") or "", recipient)
    if not tuple_ok(tuple_):
        return {"status": "bad_correlation", "task": None}
    key = idempotency_key(tuple_)

    try:
        claimed = deps.claims.claim(key)
    except Exception as exc:  # noqa: BLE001 - an unclaimable key is not a free one
        return {"status": "claim_error", "task": tuple_["task_id"], "error": type(exc).__name__}
    if not claimed:
        return {"status": "duplicate", "task": tuple_["task_id"], "tuple": tuple_}

    out = {"status": state.lower(), "state": state, "task": tuple_["task_id"], "tuple": tuple_,
           "gist": gist(text)}
    if state in (QUEUED, BLOCKED):
        # Nothing is dispatched and we say so in the doc. The claim STAYS so the
        # same unchanged request does not queue twice; a real edit is a new
        # revision, so a new key, and it comes straight back through.
        out["receipt"] = write_receipt(
            deps, tuple_, state, deps.at,
            "no capable worker online" if state == BLOCKED else "awaiting the laptop")
        return out

    try:
        deps.queue.put(tuple_, gist(text), deps.at)
    except Unconfigured as exc:
        deps.claims.release(key)
        return {"status": "unconfigured", "task": tuple_["task_id"], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a failed hand-off must be retryable
        deps.claims.release(key)
        return {"status": "queue_error", "task": tuple_["task_id"], "error": type(exc).__name__}
    out["receipt"] = write_receipt(deps, tuple_, REQUESTED, deps.at)

    # The ring is cloud/inbox-wake's job, and it is the LAST thing: a ring whose
    # task is not on the queue wakes an agent to find nothing.
    try:
        deps.ring(tuple_)
    except Unconfigured as exc:
        out["status"] = "queued_not_rung"
        out["error"] = str(exc)
        return out
    except Exception as exc:  # noqa: BLE001
        out["status"] = "queued_not_rung"
        out["error"] = type(exc).__name__
        return out
    out["status"] = DISPATCHED.lower()
    out["state"] = DISPATCHED
    out["receipt_dispatched"] = write_receipt(deps, tuple_, DISPATCHED, deps.at)
    return out


def channel_health(expires_at, now, warn=DEFAULT_RENEW_WARN) -> dict:
    """A watch channel lasts about seven days. The day-eight outage is the silent one.

    No expiry on record is "unknown", not "ok". Nothing here reports healthy
    because the process is running.
    """
    if expires_at is None:
        return {"channel": "unknown", "renew": True}
    try:
        left = float(expires_at) - float(now)
    except (TypeError, ValueError):
        return {"channel": "unknown", "renew": True}
    if left <= 0:
        return {"channel": "expired", "renew": True, "seconds_left": int(left)}
    if left <= warn:
        return {"channel": "degraded", "renew": True, "seconds_left": int(left)}
    return {"channel": "ok", "renew": False, "seconds_left": int(left)}


def channel_token_ok(secret: str, header: str) -> bool:
    """Drive's X-Goog-Channel-Token. An empty secret is a refusal, not a skip."""
    if not secret:
        return False
    return hmac.compare_digest(secret, header or "")


def bearer_ok(secret: str, header: str) -> bool:
    if not secret:
        return False
    return hmac.compare_digest("Bearer " + secret, header or "")


def hints_from_push(headers: dict) -> list:
    """Drive's push notification carries NO body worth trusting: state and ids only."""
    state = (headers.get("x-goog-resource-state") or "").lower()
    if state in ("sync", ""):
        return []
    uri = headers.get("x-goog-resource-uri") or ""
    found = re.search(r"/files/([A-Za-z0-9_-]{10,128})", uri)
    return [{"file_id": found.group(1), "folder_id": ""}] if found else [{"file_id": "", "poll": True}]


def poll(deps) -> list:
    """The changes feed, which is also the recovery path for a notification never delivered."""
    if not deps.enabled:
        return []
    if deps.changes is None:
        return [{"status": "no_store", "task": None}]
    try:
        changed = deps.changes(deps.folder_id) or []
    except Exception as exc:  # noqa: BLE001
        return [{"status": "read_error", "task": None, "error": type(exc).__name__}]
    return [decide({"file_id": c.get("file_id") or "", "folder_id": c.get("folder_id") or ""}, deps)
            for c in changed]


def handle(method: str, path: str, headers: dict, body: bytes, deps) -> tuple:
    path = path.split("?", 1)[0]
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    if method == "GET" and path == "/healthz":
        out = {"ok": True, "enabled": bool(deps.enabled)}
        out.update(channel_health(deps.channel_expires_at, deps.now, deps.renew_warn))
        alive = heartbeat_age(deps.claims, deps.now, window=deps.heartbeat_window)
        out["worker"] = {"claude-code-cli": "online" if alive is not None else "offline",
                         "api": canonical(deps.api_worker) or "none"}
        return 200, out
    if method == "POST" and path == "/hooks/drive":
        if not deps.enabled:
            return 200, {"enabled": False, "intake": []}
        if not channel_token_ok(deps.channel_token, headers.get("x-goog-channel-token", "")):
            return 401, {"error": "bad channel token"}
        return 200, {"enabled": True,
                     "intake": [decide(h, deps) for h in hints_from_push(headers)]}
    if method == "POST" and path == "/poll":
        if not deps.enabled:
            return 200, {"enabled": False, "intake": []}
        if not bearer_ok(deps.poll_token, headers.get("authorization", "")):
            return 401, {"error": "bad poll token"}
        return 200, {"enabled": True, "intake": poll(deps)}
    return 404, {"error": "not found"}


class MemoryClaims:
    """Process-local stand-in. Production uses RedisClaims; tests use this."""

    def __init__(self, beats=None):
        self.keys = set()
        self.beats = dict(beats or {})

    def claim(self, key):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    def release(self, key):
        self.keys.discard(key)

    def heartbeat(self, agent):
        return self.beats.get(agent)


class RedisClaims:
    """SET NX on fleet:intake:claim:<key>; the heartbeat is fleet:alive:<agent>.

    Both live under the fleet: prefix the ACL already grants. No new store: the
    task itself goes to fleet:inbox:<agent>, which is the agreed queue.
    """

    def __init__(self, client):
        self.client = client

    def claim(self, key):
        return bool(self.client.set("fleet:intake:claim:" + key, "1", nx=True, ex=CLAIM_TTL))

    def release(self, key):
        self.client.delete("fleet:intake:claim:" + key)

    def heartbeat(self, agent):
        raw = self.client.get("fleet:alive:" + agent)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


class InboxQueue:
    """One task onto fleet:inbox:<recipient>, carrying the whole tuple unchanged."""

    def __init__(self, client):
        self.client = client

    def put(self, tuple_, gist_, at):
        if self.client is None:
            raise Unconfigured("no Redis client for fleet:inbox")
        fields = {
            "by": "drive-intake", "at": at, "kind": "drive_request",
            "text": gist_["text"], "chars": str(gist_["chars"]),
            "full": "1" if gist_["full"] else "0",
            "source_doc_id": tuple_["source_doc_id"],
            "source_revision": tuple_["source_revision"],
            "request_id": tuple_["request_id"], "task_id": tuple_["task_id"],
            "recipient_id": tuple_["recipient_id"],
        }
        self.client.xadd("fleet:inbox:" + tuple_["recipient_id"], fields)


class Deps:
    def __init__(self, enabled=False, docs=None, claims=None, queue=None, receipts=None,
                 changes=None, ring=None, folder_id="", channel_token="", poll_token="",
                 api_worker="", now=0.0, at="", channel_expires_at=None,
                 heartbeat_window=DEFAULT_HEARTBEAT, renew_warn=DEFAULT_RENEW_WARN):
        self.enabled = enabled
        self.docs = docs
        self.claims = claims
        self.queue = queue
        self.receipts = receipts
        self.changes = changes
        self.ring = ring or (lambda _tuple: None)
        self.folder_id = folder_id
        self.channel_token = channel_token
        self.poll_token = poll_token
        self.api_worker = api_worker
        self.now = now
        self.at = at
        self.channel_expires_at = channel_expires_at
        self.heartbeat_window = heartbeat_window
        self.renew_warn = renew_warn


class Handler(BaseHTTPRequestHandler):
    deps = None

    def _run(self, method):
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(length) if length else b""
        status, payload = handle(method, self.path, dict(self.headers), body, self.deps)
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._run("GET")

    def do_POST(self):  # noqa: N802
        self._run("POST")

    def log_message(self, fmt, *args):   # one line, and never the doc's text
        print("drive-intake %s" % (fmt % args))


def main():  # pragma: no cover - the server loop
    Handler.deps = Deps(enabled=enabled_from_env())
    port = int(os.environ.get("PORT") or 8080)
    ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()

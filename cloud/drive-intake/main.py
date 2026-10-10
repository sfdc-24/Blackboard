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

THE THINGS THAT GO WRONG, AND WHERE EACH IS STOPPED
  1. A receipt retriggers the task it is reporting. The request revision is a
     hash of the doc with the receipt section REMOVED (request_revision), never
     Drive's own revisionId, which every receipt append bumps. The receipt
     writer puts the RECEIPTS heading in itself when the doc has none, and a
     line in our own receipt shape is excluded even without one.
  2. The same change arrives twice - push and poll, or Drive's own retry. One
     deterministic identity per REQUEST (doc, request revision, request id),
     claimed SET NX before anything else. The worker it is routed to is NOT
     part of that identity: Cursor's review of 05a8079 showed a heartbeat
     flip minting a second task for the same request. Routing is a state the
     request moves through, not a new request.
  3. The claim is a small forward-only state machine, and every hop is
     finished by whichever pass comes next: intake -> queued/blocked ->
     delivering:<worker> -> on_inbox:<worker> -> rung:<worker> -> dispatched.
     Cursor's review of d150d84: a ring that failed was never retried, a
     crash between the put and the claim re-put to a second worker, and two
     overlapping passes (push and poll) both delivered. So: the worker is
     recorded in the claim BEFORE the put and a resume delivers to THAT
     worker; each transition is a compare-and-set, so one pass wins and the
     other reports in_progress; the inbox is read back across EVERY worker's
     stream, newest first; and a failed ring or a receipt not read back
     leaves the state where it was, for the next pass to finish.
     RESIDUAL, NAMED NOT CLOSED: two passes that both resume ONE
     delivering:<worker> state (a crash's successor racing a poll) read the
     inbox empty together and can both put. The compare-and-set after the
     put stops the loser from writing a receipt or ringing, so the cost is
     one duplicate inbox entry, same task id, same worker. A lease on the
     delivering state closes it; add one when a live run measures it.
  4. "Picked up" is narrated by something that cannot pick anything up. This
     service can only reach REQUESTED and DISPATCHED. ACCEPTED belongs to the
     named worker and is written by the worker (L-116). With no ring
     configured the status is queued_not_rung, never dispatched.
  5. A receipt is one per (task, state): the id does not carry the time, so a
     retry finds its own line instead of writing it again. An append whose
     response was lost is settled by reading the line back and refreshing the
     doc's write revision from the doc, never by trusting the old revision.
  6. The Drive watch channel expires (7 days, max) and the day-8 outage is
     silent. /healthz reports the expiry against the clock NOW (not the clock
     at boot) and goes DEGRADED before it lands; missing evidence reads
     "unknown", and ok is false until it reads ok.

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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 65536
MAX_DOC = 400000          # a doc larger than this is refused, not truncated (PR 354's rule)
MAX_GIST = 900
CLAIM_TTL = 2592000       # 30 days, the ttl cloud/inbox-wake already uses for fleet: keys
DEFAULT_HEARTBEAT = 300   # the owner's suggested 5 minutes
DEFAULT_RENEW_WARN = 172800
INBOX_LOOKBACK = 500      # newest entries read back per worker stream

ID_OK = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
FILE_ID_OK = re.compile(r"[A-Za-z0-9_-]{10,128}\Z")

# The heading that separates the owner's REQUEST from the trail of receipts.
# Everything from the first match onwards is receipts and is excluded from the
# request revision, so appending a receipt can never look like a new request.
RECEIPTS_HEADING = re.compile(r"(?mi)^\s*(?:#+\s*)?(?:CONVERSPAN\s+)?RECEIPTS\s*:?\s*$")
HEADING_TEXT = "RECEIPTS"
# A line in our own receipt shape is a receipt wherever it sits.
RECEIPT_LINE = re.compile(r"(?m)^\s*(?:REQUESTED|DISPATCHED|QUEUED|BLOCKED) - task task-[0-9a-f]{16} - .*$")

TO_LINE = re.compile(r"(?mi)^\s*(?:#+\s*)?to\s*[:=]\s*(\S+)\s*$")
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

# The claim's states. A request is claimed once and then moves forward only.
# The three worker-bound states carry the worker after a colon.
INTAKE = "intake"            # claimed, nothing delivered yet
WAIT_QUEUED = "queued"       # written QUEUED in the doc, awaiting the laptop
WAIT_BLOCKED = "blocked"     # written BLOCKED in the doc, awaiting a named worker
DELIVERING = "delivering"    # a put to <worker> is in flight (or died mid-way)
ON_INBOX = "on_inbox"        # the task is on <worker>'s inbox, ring owed
RUNG = "rung"                # rung; the DISPATCHED receipt still owed
DONE = "dispatched"          # receipt read back; this service's job is over


class Unconfigured(Exception):
    """A hop has no credential, URL or named worker. Nothing is delivered and the gap is named."""


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
    turn. Hash the half the owner writes and nothing we write. A line in our
    own receipt shape is ours wherever it sits, heading or no heading.
    """
    match = RECEIPTS_HEADING.search(text or "")
    head = (text or "")[:match.start()] if match else (text or "")
    return RECEIPT_LINE.sub("", head)


def request_revision(text: str) -> str:
    """sha256 over the request half, whitespace-normalised per line.

    Normalised because Docs rewrites trailing spaces and line endings on its
    own; an invisible edit must not look like a new request.
    """
    lines = [line.rstrip() for line in strip_receipts(text).splitlines()]
    body = "\n".join(line for line in lines if line.strip()).strip()
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
    """The tuple every hop must carry unchanged.

    Identity is the REQUEST: doc, request revision, request id, and a task id
    derived from exactly those three. The recipient rides along for the hops
    but is not identity - a request routed to a different worker on a later
    pass is the same request, not a second one.
    """
    revision = request_revision(text)
    found = REQUEST_LINE.search(strip_receipts(text) or "")
    request_id = found.group(1) if found else ("req-" + revision[:16])
    task_id = "task-" + hashlib.sha256(
        "\x1f".join((file_id, revision, request_id)).encode("utf-8")
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
    """One key per request. The recipient is deliberately NOT in it (guard 2)."""
    return hashlib.sha256("\x1f".join((
        tuple_["source_doc_id"], tuple_["source_revision"],
        tuple_["request_id"], tuple_["task_id"],
    )).encode("utf-8")).hexdigest()


def tuple_ok(tuple_: dict) -> bool:
    """Reject a missing or malformed identity rather than start a generic reader."""
    if not isinstance(tuple_, dict):
        return False
    if not FILE_ID_OK.fullmatch(tuple_.get("source_doc_id") or ""):
        return False
    for key in ("source_revision", "request_id", "task_id", "drive_revision"):
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


def receipt_block(doc_text: str, line: str) -> str:
    """The text actually appended: the heading first when the doc has none (guard 1)."""
    if RECEIPTS_HEADING.search(doc_text or ""):
        return line
    return "\n" + HEADING_TEXT + "\n" + line


def with_heading(doc_text: str) -> str:
    """The doc as it reads after one receipt: the heading is there now, so write it once."""
    if RECEIPTS_HEADING.search(doc_text or ""):
        return doc_text
    return (doc_text or "") + "\n" + HEADING_TEXT + "\n"


def receipt_id_for(tuple_: dict, state: str) -> str:
    """One receipt per (task, state). No clock in it, so a retry finds its own line (guard 5)."""
    return hashlib.sha256((tuple_["task_id"] + "\x1f" + state).encode("utf-8")).hexdigest()[:16]


def _refresh_revision(deps, tuple_):
    """After a write whose response was lost, the doc's revision is whatever the doc says NOW."""
    try:
        doc = deps.docs.read(tuple_["source_doc_id"]) if deps.docs is not None else None
    except Exception:  # noqa: BLE001 - keep the old revision; the next write will say if it is stale
        return
    revision = str((doc or {}).get("revision_id") or "")
    if ID_OK.fullmatch(revision):
        tuple_["drive_revision"] = revision


def write_receipt(deps, tuple_: dict, doc_text: str, state: str, at: str, detail: str = "") -> str:
    """Append one receipt, then READ IT BACK. A lost response is UNKNOWN, never success.

    The guard is Drive's own revisionId (the write revision), kept apart from
    the request revision above: one is concurrency, the other is identity.
    The adapter returns the revision after its write and the NEXT append is
    guarded with that one. If the response is lost, the line is looked for
    and the revision is re-read from the doc - then ONE retry with that.
    """
    if deps.receipts is None:
        return "no_receipt_writer"
    doc_id = tuple_["source_doc_id"]
    receipt_id = receipt_id_for(tuple_, state)
    try:
        if deps.receipts.has(doc_id, receipt_id):
            return "written"                      # an earlier pass wrote it; nothing to add
    except Exception as exc:  # noqa: BLE001
        return "unknown:" + type(exc).__name__
    line = receipt_line(state, tuple_, at, detail)
    block = receipt_block(doc_text, line)
    for attempt in (1, 2):
        try:
            new_revision = deps.receipts.append(doc_id, tuple_.get("drive_revision") or "",
                                                receipt_id, block)
        except Exception as exc:  # noqa: BLE001 - the write may have landed anyway
            try:
                landed = deps.receipts.has(doc_id, receipt_id)
            except Exception:  # noqa: BLE001
                return "unknown:" + type(exc).__name__
            _refresh_revision(deps, tuple_)
            if landed:
                return "written"
            if attempt == 2:
                return "unknown:" + type(exc).__name__
            continue                              # stale guard or a clean miss: once more, fresh revision
        if new_revision:
            tuple_["drive_revision"] = str(new_revision)
        break
    try:
        return "written" if deps.receipts.has(doc_id, receipt_id) else "not_read_back"
    except Exception as exc:  # noqa: BLE001
        return "unknown:" + type(exc).__name__


def _on_any_inbox(deps, task_id: str):
    """Which worker's inbox already holds this task, or None. Every stream, newest first.

    Cursor on d150d84: looking only at the routed worker's inbox let a resume
    after a heartbeat flip append the same task to a second worker.
    """
    has = getattr(deps.queue, "has", None)
    if has is None:
        return None
    for worker in WORKERS:
        if has(worker, task_id):
            return worker
    return None


def _now(deps) -> float:
    """The clock NOW. deps.now is for tests; a server never freezes it at boot (guard 6)."""
    return float(deps.now) if deps.now is not None else time.time()


def _split(state):
    """'delivering:claude-api' -> ('delivering', 'claude-api'); 'intake' -> ('intake', None)."""
    if not state:
        return None, None
    name, _, worker = str(state).partition(":")
    return name, (worker or None)


def _advance(deps, key, current, new):
    """Compare-and-set. False means another pass moved first; this one must not act."""
    return bool(deps.claims.advance(key, current, new))


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
    # The doc must SAY it is in the approved folder; a doc that says nothing is out.
    if deps.folder_id and doc.get("folder_id") != deps.folder_id:
        return {"status": "out_of_scope", "task": None}
    drive_revision = str(doc.get("revision_id") or "")
    if not ID_OK.fullmatch(drive_revision):
        return {"status": "no_revision", "task": None}
    text = doc.get("text") or ""
    if len(text) > MAX_DOC:
        return {"status": "too_large", "task": None, "chars": len(text)}
    if not strip_receipts(text).strip():
        return {"status": "no_request", "task": None}

    # Who it is for. An address that names nobody we know is refused, not rerouted.
    addressed = TO_LINE.search(strip_receipts(text))
    if addressed and canonical(addressed.group(1)) is None:
        return {"status": "bad_recipient", "task": None, "to": addressed.group(1)[:64]}
    wants_cli = needs_cli_tools(text)
    now = _now(deps)
    alive = heartbeat_age(deps.claims, now, window=deps.heartbeat_window)
    if addressed:
        recipient, state = canonical(addressed.group(1)), REQUESTED
        if recipient == "claude-code-cli" and alive is None:
            state = QUEUED if wants_cli else REQUESTED
    else:
        recipient, state = pick_worker(wants_cli, alive, deps.api_worker)

    tuple_ = correlate(file_id, text, drive_revision, recipient)
    if not tuple_ok(tuple_):
        return {"status": "bad_correlation", "task": None}
    key = idempotency_key(tuple_)

    # The claim is a state the request moves through, forward only.
    try:
        current = deps.claims.state(key)
        if current is None:
            current = INTAKE if deps.claims.claim(key) else deps.claims.state(key)
    except Exception as exc:  # noqa: BLE001 - an unclaimable key is not a free one
        return {"status": "claim_error", "task": tuple_["task_id"], "error": type(exc).__name__}
    if current is None:
        return {"status": "claim_error", "task": tuple_["task_id"], "error": "unreadable"}
    if current == DONE:
        return {"status": "duplicate", "task": tuple_["task_id"], "tuple": tuple_}
    phase, bound = _split(current)
    if bound:
        # A worker was chosen on an earlier pass. Routing is settled; finish THAT delivery.
        recipient, state = bound, REQUESTED
        tuple_["recipient_id"] = bound

    out = {"status": state.lower(), "state": state, "task": tuple_["task_id"], "tuple": tuple_,
           "gist": gist(text)}
    if state in (QUEUED, BLOCKED):
        wait = WAIT_QUEUED if state == QUEUED else WAIT_BLOCKED
        if current == wait:
            # Already said so in the doc; an unchanged request waiting is not news.
            out["status"] = "waiting"
            return out
        if not _advance(deps, key, current, wait):
            out["status"] = "in_progress"
            return out
        out["receipt"] = write_receipt(
            deps, tuple_, text, state, deps.at,
            "no capable worker online" if state == BLOCKED else "awaiting the laptop")
        return out

    # Deliver, one hop per state, each hop idempotent by read-back.
    detail = "" if current == INTAKE else "now that a worker is online"
    if phase in (INTAKE, WAIT_QUEUED, WAIT_BLOCKED):
        # Bind the worker BEFORE the put, so a crash or a flip cannot re-put elsewhere.
        if not _advance(deps, key, current, DELIVERING + ":" + recipient):
            out["status"] = "in_progress"
            return out
        current, phase = DELIVERING + ":" + recipient, DELIVERING
    if phase == DELIVERING:
        try:
            holder = _on_any_inbox(deps, tuple_["task_id"])
            if holder is None:
                try:
                    deps.queue.put(tuple_, gist(text), deps.at)
                except Unconfigured:
                    raise
                except Exception as exc:  # noqa: BLE001 - it may have landed anyway
                    if _on_any_inbox(deps, tuple_["task_id"]) is None:
                        return {"status": "queue_error", "task": tuple_["task_id"],
                                "error": type(exc).__name__}
        except Unconfigured as exc:
            return {"status": "unconfigured", "task": tuple_["task_id"], "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - an inbox that cannot be read back is unknown
            return {"status": "queue_unknown", "task": tuple_["task_id"], "error": type(exc).__name__}
        if not _advance(deps, key, current, ON_INBOX + ":" + recipient):
            out["status"] = "in_progress"
            return out
        current, phase = ON_INBOX + ":" + recipient, ON_INBOX
        out["receipt"] = write_receipt(deps, tuple_, text, REQUESTED, deps.at, detail)
        text = with_heading(text)
    if phase == ON_INBOX:
        # The ring is cloud/inbox-wake's job, and it comes AFTER the put: a ring
        # whose task is not on the queue wakes an agent to find nothing.
        try:
            deps.ring(tuple_)
        except Unconfigured as exc:
            out["status"] = "queued_not_rung"
            out["error"] = str(exc)
            return out
        except Exception as exc:  # noqa: BLE001 - rung again by the next pass
            out["status"] = "queued_not_rung"
            out["error"] = type(exc).__name__
            return out
        if not _advance(deps, key, current, RUNG + ":" + recipient):
            out["status"] = "in_progress"
            return out
        current, phase = RUNG + ":" + recipient, RUNG
    if phase == RUNG:
        out["receipt_dispatched"] = write_receipt(deps, tuple_, text, DISPATCHED, deps.at)
        if out["receipt_dispatched"] not in ("written", "no_receipt_writer"):
            # Rung, but the phone cannot see it yet. Say so; the next pass writes the line.
            out["status"] = "dispatched_receipt_owed"
            return out
        _advance(deps, key, current, DONE)
        out["status"] = DISPATCHED.lower()
        out["state"] = DISPATCHED
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
        now = _now(deps)
        health = channel_health(deps.channel_expires_at, now, deps.renew_warn)
        out = {"ok": health["channel"] == "ok", "enabled": bool(deps.enabled)}
        out.update(health)
        alive = heartbeat_age(deps.claims, now, window=deps.heartbeat_window)
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


def no_ring(_tuple):
    """The default ring: not configured. A ring that silently succeeds would write DISPATCHED."""
    raise Unconfigured("no ring configured (INBOX_WAKE_URL)")


class MemoryClaims:
    """Process-local stand-in. Production uses RedisClaims; tests use this."""

    def __init__(self, beats=None):
        self.states = {}
        self.beats = dict(beats or {})

    def claim(self, key):
        if key in self.states:
            return False
        self.states[key] = INTAKE
        return True

    def state(self, key):
        return self.states.get(key)

    def advance(self, key, current, new):
        if self.states.get(key) != current:
            return False
        self.states[key] = new
        return True

    def heartbeat(self, agent):
        return self.beats.get(agent)


# Compare-and-set in one round trip, so two passes cannot both win a transition.
CAS_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
           "redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]) return 1 else return 0 end")


class RedisClaims:
    """fleet:intake:claim:<key> holds the request's state; the heartbeat is fleet:alive:<agent>.

    Both live under the fleet: prefix the ACL already grants. No new store: the
    task itself goes to fleet:inbox:<agent>, which is the agreed queue.
    """

    def __init__(self, client):
        self.client = client

    def claim(self, key):
        return bool(self.client.set("fleet:intake:claim:" + key, INTAKE, nx=True, ex=CLAIM_TTL))

    def state(self, key):
        raw = self.client.get("fleet:intake:claim:" + key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return raw or None

    def advance(self, key, current, new):
        return bool(self.client.eval(CAS_LUA, 1, "fleet:intake:claim:" + key, current, new, CLAIM_TTL))

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
    """One task onto fleet:inbox:<recipient>, carrying the whole tuple unchanged, read back by task id."""

    def __init__(self, client):
        self.client = client

    def has(self, recipient, task_id):
        if self.client is None:
            raise Unconfigured("no Redis client for fleet:inbox")
        # NEWEST first: the entry a lost response is being settled for is the newest one,
        # and the oldest 500 of a busy stream never contain it (Cursor on d150d84).
        rows = self.client.xrevrange("fleet:inbox:" + recipient, count=INBOX_LOOKBACK) or ()
        for _entry_id, fields in rows:
            value = fields.get("task_id")
            if value is None:
                value = fields.get(b"task_id")
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            if value == task_id:
                return True
        return False

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
                 api_worker="", now=None, at="", channel_expires_at=None,
                 heartbeat_window=DEFAULT_HEARTBEAT, renew_warn=DEFAULT_RENEW_WARN):
        self.enabled = enabled
        self.docs = docs
        self.claims = claims
        self.queue = queue
        self.receipts = receipts
        self.changes = changes
        self.ring = ring or no_ring
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

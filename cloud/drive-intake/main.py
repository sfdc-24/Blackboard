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
     The delivering state carries a LEASE (delivering:<worker>:<since>):
     a second pass that finds it fresh - or AHEAD of its own clock, which
     is skew, not death - reports in_progress instead of resuming. A dead
     or missing lease is TAKEN by compare-and-set before the inbox is
     touched, so of two resumers exactly one puts (Cursor on 57a23ab and
     c408819). A put that outlives its lease is overtaken, and a chain of
     overtakes would append on every pass - so the put itself is one
     atomic Redis script (PUT_ONCE_LUA): SET NX a per-task put marker to
     the worker, XADD only when that set won. Two servers racing the same
     stream land one entry; a resume after the first entry is buried
     under 500 newer ones adds nothing; a claim that aged out while the
     laptop came back cannot put the same task on a second stream, because
     the marker outlives the claim (Cursor on 07a6b1c and 13af1f7).
     Which inbox holds a task is answered from that marker, never from a
     scan. Lines owed so far are re-attempted BEFORE the ring, so a ring
     that is refused pass after pass does not keep the doc empty.
     EVERY REDIS KEY HERE EXPIRES - the claim, the put marker, #359's seen
     key - and Cursor on 96b645c showed each expiry re-doing the hop it
     guarded. The record with no TTL is the DOC: its receipt trail is read
     first on every pass (trail_for). A DISPATCHED line for the task is a
     duplicate whatever Redis remembers; a REQUESTED line names the worker
     whose inbox holds it and the pass resumes from on_inbox with no put;
     a QUEUED or BLOCKED line restores the wait the request came through.
     Below the doc there is one more record that does not expire: the
     inbox stream itself. When the put marker is gone, the holder is
     found by scanning every worker's stream (INBOX_FULL_SCAN deep) and
     the atomic put scans its own stream before the XADD, so a put that
     landed with no receipt and no marker is still not put again
     (Cursor on b47305f). RESIDUALS, NAMED: an entry deeper than
     INBOX_FULL_SCAN, and a wake whose DISPATCHED line fails to land for
     the whole 30 days of the claim - after that the task is rung once
     more, because the other choice is a request that is never rung.
     The ring is sent BEFORE the claim moves to rung, so a crash in between
     rings again - safe only because WakeRing sends id=task_id and
     cloud/inbox-wake claims fleet:wake:seen:<agent>:<id> SET NX, so the
     second ring is a duplicate there. Two gaps on #359's side, measured
     by Cursor at c204039 and owed to that PR, not this one: its ROUTES
     have no claude-api, so a ring for the api worker is unknown_agent
     until step 2 lands; and its sweep keys the seen-set by stream entry
     id, not task_id, so two copies of one task wake twice there.
     EVERY receipt line (QUEUED, BLOCKED, REQUESTED, DISPATCHED) is owed
     until it reads back, and re-attempted on every later pass; the
     receipt id has no clock, so the attempt is a no-op once the line is
     there. dispatched means BOTH its lines read back.
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
DEFAULT_POLL_TICK = 120   # the scheduler's cadence for /poll; health reads degraded past three of them
POLL_TOKEN_HEADER = "x-drive-intake-poll-token"   # the scheduler's header when Authorization carries its OIDC token
DELIVER_LEASE = 60        # seconds a delivering pass owns the put before another may resume it

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


TRAIL_LINE = re.compile(
    r"(?m)^\s*(?P<state>REQUESTED|DISPATCHED|QUEUED|BLOCKED) - task (?P<task>task-[0-9a-f]{16}) - "
    r"request \S+ - rev \S+ - recipient (?P<recipient>\S+) - by drive-intake - ")


def trail_for(doc_text: str, task_id: str) -> dict:
    """What the DOC says already happened to this task: {state: recipient}.

    The doc is the durable record - the owner's requirement, and the only one
    with no TTL. Every Redis key here (claim, put marker, #359's seen key)
    expires, and Cursor on 96b645c showed each expiry re-doing the work: a
    second put at any depth, a second ring after 30 days, a lost wait line.
    So the trail the doc already carries is read FIRST, every pass, and a
    line it shows is a hop that is not repeated whatever Redis remembers.
    """
    found = {}
    for match in TRAIL_LINE.finditer(doc_text or ""):
        if match.group("task") == task_id:
            found[match.group("state")] = match.group("recipient")
    return found


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
    # The id goes INTO the line: a read-back against a real doc looks for "id <receipt_id>",
    # and a line that does not carry it can never be found (cloud/claude-api-worker/gdocs.py).
    line = receipt_line(state, tuple_, at, detail) + " - id " + receipt_id
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
    """Which worker's inbox already holds this task, or None.

    Cursor on d150d84: looking only at the routed worker's inbox let a resume
    after a heartbeat flip append the same task to a second worker. A queue
    that keeps a put marker answers from it (any depth); one that does not
    is asked per worker.
    """
    holder = getattr(deps.queue, "holder", None)
    if holder is not None:
        return holder(task_id)
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
    """'delivering:claude-api:1000:q' -> ('delivering', 'claude-api', 1000.0, 'q').

    The fourth part is how the request got here: 'q' through QUEUED, 'b' through
    BLOCKED, '' straight from intake. It rides every later state so the line owed
    for that wait is still written after a crash (Cursor on 07a6b1c).
    """
    if not state:
        return None, None, None, ""
    parts = str(state).split(":")
    name = parts[0]
    worker = parts[1] if len(parts) > 1 and parts[1] else None
    since = None
    if len(parts) > 2:
        try:
            since = float(parts[2])
        except ValueError:
            since = None
    via = parts[3] if len(parts) > 3 and parts[3] in ("q", "b") else ""
    return name, worker, since, via


def _state(phase, worker, since, via) -> str:
    return "%s:%s:%d:%s" % (phase, worker, int(since or 0), via or "")


def _lines_owed(via) -> tuple:
    """Every receipt line this request passed through, oldest first."""
    head = (QUEUED,) if via == "q" else (BLOCKED,) if via == "b" else ()
    return head + (REQUESTED, DISPATCHED)


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

    # The doc's own trail first: it is the record with no TTL (Cursor on 96b645c).
    trail = trail_for(text, tuple_["task_id"])
    if DISPATCHED in trail:
        return {"status": "duplicate", "task": tuple_["task_id"], "tuple": tuple_, "by": "doc"}
    trail_via = "q" if QUEUED in trail else "b" if BLOCKED in trail else ""

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
    if current == INTAKE and REQUESTED in trail and canonical(trail[REQUESTED]) == trail[REQUESTED]:
        # Redis forgot this request (a claim that aged out) but the doc says it is already on a
        # worker's inbox. Resume from THERE: no put, the ring and the last line still owed.
        seeded = _state(ON_INBOX, trail[REQUESTED], now, trail_via)
        if not _advance(deps, key, current, seeded):
            return {"status": "in_progress", "task": tuple_["task_id"], "tuple": tuple_}
        current = seeded
    phase, bound, since, via = _split(current)
    if bound:
        # A worker was chosen on an earlier pass. Routing is settled; finish THAT delivery.
        recipient, state = bound, REQUESTED
        tuple_["recipient_id"] = bound

    out = {"status": state.lower(), "state": state, "task": tuple_["task_id"], "tuple": tuple_,
           "gist": gist(text)}
    if state in (QUEUED, BLOCKED):
        wait = WAIT_QUEUED if state == QUEUED else WAIT_BLOCKED
        if current != wait and not _advance(deps, key, current, wait):
            out["status"] = "in_progress"
            return out
        # Written every pass: has() makes it a no-op once the line is there, and a line that
        # never read back is owed until it is (Cursor on c408819).
        out["receipt"] = write_receipt(
            deps, tuple_, text, state, deps.at,
            "no capable worker online" if state == BLOCKED else "awaiting the laptop")
        if current == wait:
            # An unchanged request still waiting is not news - unless its line is still owed.
            out["status"] = "waiting" if out["receipt"] in ("written", "no_receipt_writer") \
                else state.lower() + "_receipt_owed"
        return out

    # Deliver, one hop per state, each hop idempotent by read-back.
    detail = "" if current == INTAKE else "now that a worker is online"
    if phase in (INTAKE, WAIT_QUEUED, WAIT_BLOCKED):
        # Bind the worker BEFORE the put, so a crash or a flip cannot re-put elsewhere.
        # The lease (the clock, in the state) keeps a second pass from resuming this one,
        # and the wait it came through rides along so that line stays owed.
        via = "q" if phase == WAIT_QUEUED else "b" if phase == WAIT_BLOCKED else trail_via
        leased = _state(DELIVERING, recipient, now, via)
        if not _advance(deps, key, current, leased):
            out["status"] = "in_progress"
            return out
        current, phase, since, just_bound = leased, DELIVERING, None, True
    else:
        just_bound = False
    if phase == DELIVERING:
        if not just_bound:
            if since is not None and now - since < DELIVER_LEASE:
                # Another pass is inside its lease on this put (a lease AHEAD of this clock
                # is a skewed live pass, not a dead one). Resuming now is the double put.
                out["status"] = "in_progress"
                return out
            # Dead, missing or given-up lease: TAKE it by compare-and-set before touching
            # the inbox, so of two resumers exactly one puts (Cursor on c408819). A put
            # that outlives its lease is then caught by the queue's own atomic dedupe.
            taken = _state(DELIVERING, recipient, now, via)
            if not _advance(deps, key, current, taken):
                out["status"] = "in_progress"
                return out
            current = taken
        if via:
            # The wait line comes first in the trail, so it is (re)attempted before the put;
            # a no-op when it already read back, and owed again at the end if it still has not.
            write_receipt(deps, tuple_, text, QUEUED if via == "q" else BLOCKED, deps.at,
                          "awaiting the laptop" if via == "q" else "no capable worker online")
            text = with_heading(text)
        try:
            holder = _on_any_inbox(deps, tuple_["task_id"])
            if holder is not None and holder != recipient:
                # The task is already on ANOTHER worker's inbox (a claim that aged out while
                # the laptop came back). That worker has it; rebind to it rather than ring
                # the one the heartbeat chose now (Cursor on 13af1f7).
                recipient = holder
                tuple_["recipient_id"] = holder
                rebound = _state(DELIVERING, holder, now, via)
                if not _advance(deps, key, current, rebound):
                    out["status"] = "in_progress"
                    return out
                current = rebound
                out["tuple"] = tuple_
            if holder is None:
                try:
                    deps.queue.put(tuple_, gist(text), deps.at)
                except Unconfigured:
                    raise
                except Exception as exc:  # noqa: BLE001 - it may have landed anyway
                    if _on_any_inbox(deps, tuple_["task_id"]) is None:
                        # Nothing landed, and this pass knows it: give the lease up (worker kept)
                        # so the next pass may retry at once instead of waiting it out.
                        _advance(deps, key, current, _state(DELIVERING, recipient, 0, via))
                        return {"status": "queue_error", "task": tuple_["task_id"],
                                "error": type(exc).__name__}
        except Unconfigured as exc:
            _advance(deps, key, current, _state(DELIVERING, recipient, 0, via))
            return {"status": "unconfigured", "task": tuple_["task_id"], "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - an inbox that cannot be read back is unknown
            return {"status": "queue_unknown", "task": tuple_["task_id"], "error": type(exc).__name__}
        landed_state = _state(ON_INBOX, recipient, now, via)
        if not _advance(deps, key, current, landed_state):
            out["status"] = "in_progress"
            return out
        current, phase = landed_state, ON_INBOX
    if phase == ON_INBOX:
        # Lines owed so far are re-attempted BEFORE the ring: a ring that keeps being
        # refused (#359 answering unknown_agent) must not keep the doc empty (Cursor on
        # 13af1f7). Each is a no-op once it has read back.
        for owed in _lines_owed(via)[:-1]:
            note = ("awaiting the laptop" if owed == QUEUED else
                    "no capable worker online" if owed == BLOCKED else detail)
            result = write_receipt(deps, tuple_, text, owed, deps.at, note)
            if owed == REQUESTED:
                out["receipt"] = result
            text = with_heading(text)
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
        rung_state = _state(RUNG, recipient, now, via)
        if not _advance(deps, key, current, rung_state):
            out["status"] = "in_progress"
            return out
        current, phase = rung_state, RUNG
    if phase == RUNG:
        # EVERY line this request passed through is owed until it reads back: the wait it
        # came through (QUEUED or BLOCKED), REQUESTED, then DISPATCHED. has() makes each a
        # no-op once it is there. dispatched means all of them read back (Cursor on
        # c408819 and 07a6b1c).
        settled = ("written", "no_receipt_writer")
        results = {}
        for owed in _lines_owed(via):
            note = ("awaiting the laptop" if owed == QUEUED else
                    "no capable worker online" if owed == BLOCKED else
                    detail if owed == REQUESTED else
                    str(getattr(deps.ring, "receipt_note", "") or "").replace("{worker}", recipient))
            results[owed] = write_receipt(deps, tuple_, text, owed, deps.at, note)
            text = with_heading(text)
        out["receipt"] = results[REQUESTED]
        out["receipt_dispatched"] = results[DISPATCHED]
        out["receipts"] = results
        if any(result not in settled for result in results.values()):
            # Rung, but the phone cannot see the whole trail yet. Say so; the next pass
            # writes whichever line is still owed.
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


# A decide() result after which a later pass has nothing left to do for this doc as it stands. An
# edit re-lists the doc anyway. Everything else is unfinished and the doc is kept for the next poll.
FINISHED = frozenset(("dispatched", "duplicate", "disabled", "bad_file_id", "out_of_scope", "no_revision",
                      "too_large", "no_request", "bad_recipient", "bad_correlation"))


def poll(deps) -> list:
    """The changes feed, which is also the recovery path for a notification never delivered.

    ONE WRITER, AFTER EVERYTHING (Cursor on 0bee083). The feed lists and writes nothing. Every doc
    it lists is decided, and so is every doc an earlier poll left unfinished - queued for the
    laptop, blocked, in progress, a receipt still owed, a read that failed - because those are no
    longer in the feed's window and would otherwise never be seen again. The poll goes on record,
    with the feed's own watermark, only when no hint raised and the pending set was kept.
    """
    if not deps.enabled:
        return []
    if deps.changes is None or deps.claims is None:
        return [{"status": "no_store", "task": None}]
    try:
        listed = deps.changes(deps.folder_id) or []
    except Exception as exc:  # noqa: BLE001
        return [{"status": "read_error", "task": None, "error": type(exc).__name__}]
    if isinstance(listed, dict):
        hints, watermark = list(listed.get("hints") or []), listed.get("watermark")
    else:
        hints, watermark = list(listed), None
    try:
        pending = [{"file_id": f, "folder_id": ""} for f in sorted(deps.claims.pending() or ())]
    except Exception as exc:  # noqa: BLE001 - the unfinished cannot be found: nothing goes on record
        return [{"status": "pending_unreadable", "task": None, "error": type(exc).__name__}]
    out, seen, undecided = [], set(), False
    for raw in pending + hints:
        try:
            file_id = str((raw or {}).get("file_id") or "")
            folder = str((raw or {}).get("folder_id") or "")
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            result = decide({"file_id": file_id, "folder_id": folder}, deps)
            out.append(result)
            if result.get("status") in FINISHED:
                deps.claims.pending_remove(file_id)
            else:
                deps.claims.pending_add(file_id)
        except Exception as exc:  # noqa: BLE001 - one hint's failure must not lose the others
            undecided = True
            out.append({"status": "decide_error", "task": None, "error": type(exc).__name__})
    if not undecided:
        now = _now(deps)
        try:
            deps.claims.note_poll(now, watermark if watermark is not None else now)
        except Exception:  # noqa: BLE001 - health will read unknown; the intake itself is done
            pass
    return out


def poll_health(last_poll, now, tick=DEFAULT_POLL_TICK) -> dict:
    """The poll is the channel when no watch is registered. No poll on record is unknown, not ok."""
    if last_poll is None:
        return {"poll": "unknown", "last_poll": None}
    try:
        age = float(now) - float(last_poll)
    except (TypeError, ValueError):
        return {"poll": "unknown", "last_poll": None}
    state = "ok" if 0 <= age <= 3 * tick else "degraded"
    return {"poll": state, "last_poll": int(float(last_poll)), "poll_age": int(age)}


def poll_token_ok(secret: str, headers: dict) -> bool:
    """The scheduler's token: as a bearer, or in its own header when Authorization carries the OIDC
    token Cloud Run itself checks. An empty secret is a refusal, not a skip."""
    if not secret:
        return False
    if bearer_ok(secret, headers.get("authorization", "")):
        return True
    return hmac.compare_digest(secret, headers.get(POLL_TOKEN_HEADER, "") or "")


def handle(method: str, path: str, headers: dict, body: bytes, deps) -> tuple:
    path = path.split("?", 1)[0]
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    if method == "GET" and path == "/healthz":
        now = _now(deps)
        health = channel_health(deps.channel_expires_at, now, deps.renew_warn)
        try:
            last_poll = deps.claims.last_poll() if deps.claims is not None else None
        except Exception:  # noqa: BLE001 - unreadable is unknown
            last_poll = None
        polled = poll_health(last_poll, now, deps.poll_tick)
        # ok when EITHER channel is live: a registered watch inside its life, or a poll inside
        # three ticks. Neither on record reads unknown, and ok stays false.
        out = {"ok": health["channel"] == "ok" or polled["poll"] == "ok", "enabled": bool(deps.enabled)}
        out.update(health)
        out.update(polled)
        alive = heartbeat_age(deps.claims, now, window=deps.heartbeat_window)
        out["worker"] = {"claude-code-cli": "online" if alive is not None else "offline",
                         "api": canonical(deps.api_worker) or "none"}
        return 200, out
    # THE TOKEN BEFORE THE FLAG (the loopback smoke of 2026-10-11): on /poll and /hooks/drive a
    # caller with no token learns nothing, not even that the flag is off. 401 is the answer to
    # those two POSTs with no token, whatever the flag says.
    if method == "POST" and path == "/hooks/drive":
        if not channel_token_ok(deps.channel_token, headers.get("x-goog-channel-token", "")):
            return 401, {"error": "bad channel token"}
        if not deps.enabled:
            return 200, {"enabled": False, "intake": []}
        return 200, {"enabled": True,
                     "intake": [decide(h, deps) for h in hints_from_push(headers)]}
    if method == "POST" and path == "/poll":
        if not poll_token_ok(deps.poll_token, headers):
            return 401, {"error": "bad poll token"}
        if not deps.enabled:
            return 200, {"enabled": False, "intake": []}
        return 200, {"enabled": True, "intake": poll(deps)}
    return 404, {"error": "not found"}


def no_ring(_tuple):
    """The default ring: not configured. A ring that silently succeeds would write DISPATCHED."""
    raise Unconfigured("no ring configured (INBOX_WAKE_URL)")


def polled_ring(_tuple):
    """The ring when the worker POLLS its inbox (DRIVE_INTAKE_RING=polled): the put is the hand-off,
    the stream entry under the put marker its evidence, and nothing else is rung. cloud/claude-api-
    worker reads fleet:inbox:claude-api every tick and the laptop lane rings claude-code-cli itself,
    so with #359 undeployed this is the transport that exists (Cursor on 0bee083: a deploy with no
    wake variables could never reach DISPATCHED). Chosen by name in the deploy, never by default."""
    return None


polled_ring.receipt_note = "on fleet:inbox:{worker}, which the worker polls; no wake rung"


class WakeRing:
    """The ring into cloud/inbox-wake (#359): POST /wake, signed the way GitHub signs.

    THE CONTRACT THAT MAKES A REPEATED RING SAFE: the message id IS the task
    id. #359 claims fleet:wake:seen:<agent>:<id> SET NX, so the second ring
    for one task is a duplicate there, not a second wake. Posting is an
    injected callable, so this module still opens no socket and a test can
    read the exact body and header it would send.
    """

    def __init__(self, url="", secret="", post=None):
        self.url, self.secret, self.post = url or "", secret or "", post

    def body_for(self, tuple_) -> bytes:
        return json.dumps({
            "id": tuple_["task_id"], "to": tuple_["recipient_id"], "by": "drive-intake",
            "text": "drive request %s from doc %s" % (tuple_["request_id"], tuple_["source_doc_id"]),
        }, sort_keys=True).encode("utf-8")

    def signature_for(self, body: bytes) -> str:
        return "sha256=" + hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    RUNG_STATUSES = ("woke", "duplicate")   # #359's two answers that mean the recipient has been rung

    def __call__(self, tuple_):
        if not self.url or not self.secret or self.post is None:
            raise Unconfigured("no ring configured (INBOX_WAKE_URL, INBOX_WAKE_SECRET)")
        body = self.body_for(tuple_)
        readout = self.post(self.url, body, {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": self.signature_for(body),
        })
        # A 200 is not a ring. #359 answers {"woke": [{"status": ...}]}; anything but woke
        # or duplicate (unknown_agent, unconfigured, disabled) means nobody was rung, and
        # a post that hands back no readout is not read as a ring either.
        try:
            parsed = json.loads(readout.decode("utf-8") if isinstance(readout, bytes) else readout) \
                if not isinstance(readout, dict) else readout
            statuses = [str(w.get("status")) for w in (parsed.get("woke") or [])]
        except Exception as exc:  # noqa: BLE001 - no readout, no ring
            raise RuntimeError("wake gave no readout: %s" % type(exc).__name__) from exc
        if not statuses or any(s not in self.RUNG_STATUSES for s in statuses):
            raise RuntimeError("wake refused: %s" % (",".join(statuses) or "empty"))


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

    def last_poll(self):
        return self.beats.get("poll")

    def watermark(self):
        return self.beats.get("watermark")

    def note_poll(self, now, watermark=None):
        self.beats["poll"] = float(now)
        self.beats["watermark"] = float(now if watermark is None else watermark)

    def pending(self):
        return set(self.beats.setdefault("pending", set()))

    def pending_add(self, file_id):
        self.beats.setdefault("pending", set()).add(file_id)

    def pending_remove(self, file_id):
        self.beats.setdefault("pending", set()).discard(file_id)


# Compare-and-set in one round trip, so two passes cannot both win a transition.
CAS_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
           "redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]) return 1 else return 0 end")

# Append-at-most-once in one round trip. KEYS[1] is the stream, KEYS[2] the task's
# put marker (fleet:intake:put:<task_id>). The marker is SET NX to the worker the
# task went to, in the same script as the XADD, so a second put - on any stream,
# from any server, however deep the first entry is buried - adds nothing. A scan
# of the newest N entries was the earlier dedupe; Cursor buried the entry under
# 500 newer ones and it appended again (13af1f7).
#
# The marker expires (every fleet: key does) and the stream does not, so when the
# SET NX wins - the first put, or a put after the marker aged out - the stream
# itself is scanned for the task id before the XADD, newest first, up to
# INBOX_FULL_SCAN entries (Cursor on b47305f: marker gone, entry buried, second
# copy). The task id is ARGV[3+] field 'task_id'; the scan reads it from there.
PUT_ONCE_LUA = (
    "if redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', ARGV[2]) then "
    "  local task = '' "
    "  for i = 3, #ARGV - 1, 2 do if ARGV[i] == 'task_id' then task = ARGV[i + 1] end end "
    "  local rows = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', ARGV[#ARGV]) "
    "  for _, row in ipairs(rows) do "
    "    local f = row[2] "
    "    for i = 1, #f, 2 do "
    "      if f[i] == 'task_id' and f[i + 1] == task then return 0 end "
    "    end "
    "  end "
    "  local args = {} "
    "  for i = 3, #ARGV - 1 do args[#args + 1] = ARGV[i] end "
    "  redis.call('XADD', KEYS[1], '*', unpack(args)) "
    "  return 1 "
    "end "
    "return 0")
PUT_TTL = 2 * CLAIM_TTL   # the marker must outlive the claim, or a claim that aged out re-puts
INBOX_FULL_SCAN = 100000  # how deep a marker-miss looks into a stream; beyond this is named, not closed


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

    def last_poll(self):
        """fleet:intake:poll:last - when the last poll listed the folder and decided every hint."""
        raw = self.client.get("fleet:intake:poll:last")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def note_poll(self, now, watermark=None):
        self.client.set("fleet:intake:poll:last", "%d" % int(now), ex=CLAIM_TTL)
        self.client.set("fleet:intake:poll:watermark", "%d" % int(now if watermark is None else watermark),
                        ex=CLAIM_TTL)

    def watermark(self):
        """fleet:intake:poll:watermark - where the last poll got to (not when it ran)."""
        raw = self.client.get("fleet:intake:poll:watermark")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    # fleet:intake:pending - the docs an earlier poll left unfinished, revisited every poll.
    def pending(self):
        members = self.client.smembers("fleet:intake:pending") or ()
        return {m.decode("utf-8", "replace") if isinstance(m, bytes) else str(m) for m in members}

    def pending_add(self, file_id):
        self.client.sadd("fleet:intake:pending", file_id)
        self.client.expire("fleet:intake:pending", CLAIM_TTL)

    def pending_remove(self, file_id):
        self.client.srem("fleet:intake:pending", file_id)


class InboxQueue:
    """One task onto fleet:inbox:<recipient>, carrying the whole tuple unchanged, read back by task id."""

    def __init__(self, client):
        self.client = client

    def holder(self, task_id):
        """The worker whose inbox holds this task, from the put marker, or None.

        The marker, not a scan of the stream, is the answer: a scan sees the newest
        INBOX_LOOKBACK entries and nothing older (Cursor on 13af1f7)."""
        if self.client is None:
            raise Unconfigured("no Redis client for fleet:inbox")
        raw = self.client.get("fleet:intake:put:" + task_id)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if raw:
            return raw
        # Marker gone (it expires; the stream does not): look in every worker's stream,
        # newest first, INBOX_FULL_SCAN deep, and put the marker back when found
        # (Cursor on b47305f: an entry with no marker and no receipt was put again).
        for worker in WORKERS:
            rows = self.client.xrevrange("fleet:inbox:" + worker, count=INBOX_FULL_SCAN) or ()
            for _entry_id, fields in rows:
                value = fields.get("task_id")
                if value is None:
                    value = fields.get(b"task_id")
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                if value == task_id:
                    self.client.set("fleet:intake:put:" + task_id, worker, nx=True, ex=PUT_TTL)
                    return worker
        return None

    def has(self, recipient, task_id):
        return self.holder(task_id) == recipient

    def put(self, tuple_, gist_, at) -> bool:
        """Append at most once, in ONE Redis call: the task's put marker is SET NX to the
        worker and the XADD happens only when that set won, so a put that outlived its
        lease, a resumer's put, and a put to a second worker after the claim aged out all
        add nothing (Cursor on 07a6b1c and 13af1f7). True when added, False when not."""
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
        flat = []
        for name, value in fields.items():
            flat.extend((name, value))
        added = self.client.eval(PUT_ONCE_LUA, 2, "fleet:inbox:" + tuple_["recipient_id"],
                                 "fleet:intake:put:" + tuple_["task_id"],
                                 tuple_["recipient_id"], PUT_TTL, *flat, INBOX_FULL_SCAN)
        return bool(added)


class Deps:
    def __init__(self, enabled=False, docs=None, claims=None, queue=None, receipts=None,
                 changes=None, ring=None, folder_id="", channel_token="", poll_token="",
                 api_worker="", now=None, at="", channel_expires_at=None,
                 heartbeat_window=DEFAULT_HEARTBEAT, renew_warn=DEFAULT_RENEW_WARN,
                 at_provider=None, poll_tick=DEFAULT_POLL_TICK):
        self.enabled = enabled
        self.at_provider = at_provider
        self.poll_tick = poll_tick
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
        self._at = at
        self.channel_expires_at = channel_expires_at
        self.heartbeat_window = heartbeat_window
        self.renew_warn = renew_warn

    @property
    def at(self):
        """The receipt clock: a fixed string in tests, the clock NOW in a server (a line stamped with
        the boot time, days later, would be a receipt that lies about when)."""
        return self.at_provider() if self.at_provider is not None else self._at

    @at.setter
    def at(self, value):
        self._at = value


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


def utc_now_iso() -> str:
    import datetime  # noqa: PLC0415
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int_env(environ, name, default):
    try:
        return int(environ.get(name) or default)
    except ValueError:
        return default


def deps_from_env(environ=None, client=None, docs=None, feed=None, post=None) -> "Deps":
    """The service's dependencies from its environment and the adapters handed in. Pure: no socket
    is opened here, so a test can read exactly what a deploy would wire."""
    environ = os.environ if environ is None else environ
    ring_url, ring_secret = environ.get("INBOX_WAKE_URL") or "", environ.get("INBOX_WAKE_SECRET") or ""
    ring_mode = (environ.get("DRIVE_INTAKE_RING") or "").strip().lower()
    if ring_url and ring_secret:
        ring = WakeRing(ring_url, ring_secret, post=post)
    elif ring_mode == "polled":
        ring = polled_ring
    else:
        ring = no_ring
    return Deps(
        enabled=enabled_from_env(environ),
        docs=docs,
        receipts=docs,
        claims=RedisClaims(client) if client is not None else None,
        queue=InboxQueue(client) if client is not None else None,
        changes=feed,
        ring=ring,
        folder_id=environ.get("DRIVE_INTAKE_FOLDER_ID") or "",
        channel_token=environ.get("DRIVE_INTAKE_CHANNEL_TOKEN") or "",
        poll_token=environ.get("DRIVE_INTAKE_POLL_TOKEN") or "",
        api_worker=environ.get("DRIVE_INTAKE_API_WORKER") or "",
        at_provider=utc_now_iso,
        heartbeat_window=_int_env(environ, "DRIVE_INTAKE_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT),
        poll_tick=_int_env(environ, "DRIVE_INTAKE_POLL_SECONDS", DEFAULT_POLL_TICK),
    )


def main():  # pragma: no cover - the server loop
    import sys  # noqa: PLC0415
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    client = docs = feed = post = None
    if enabled_from_env():
        try:
            import redis_dual  # noqa: PLC0415 - beside this file in the image
            client = redis_dual.client(redis_dual.Settings(), precheck=False)
        except Exception as exc:  # noqa: BLE001
            print("drive-intake no redis: %s" % type(exc).__name__)
        try:
            import gdocs  # noqa: PLC0415
            import drive_feed  # noqa: PLC0415
            docs = gdocs.GoogleDocs()
            post = drive_feed.http_post
            if client is not None:
                feed = drive_feed.DriveFolder(gdocs.metadata_token, gdocs._fetch, RedisClaims(client)).changes
        except Exception as exc:  # noqa: BLE001
            print("drive-intake no docs: %s" % type(exc).__name__)
    Handler.deps = deps_from_env(client=client, docs=docs, feed=feed, post=post)
    # Unset, this listens on every interface, which Cloud Run requires. The loopback smoke sets
    # 127.0.0.1 so that run opens nothing else.
    bind = os.environ.get("DRIVE_INTAKE_BIND") or ""
    port = int(os.environ.get("PORT") or 8080)
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()

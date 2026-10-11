#!/usr/bin/env python3
"""claude-api worker: the always-on stand-in that does BOUNDED work when the laptop is off. Default off.

WHY THIS EXISTS
  The owner's rule (Converspan Channel Architecture v0.1, 2026-10-10): two
  workers, one of them always available. claude-code-cli has the tools and
  lives on his laptop; when the laptop is off, cloud/drive-intake routes a
  request to `claude-api` instead, and this is claude-api. It reads its own
  inbox (fleet:inbox:claude-api), does ONE bounded piece of work per pass -
  a reasoned answer, written into the SAME Drive doc the request came from -
  and leaves a receipt for every state it reached, and nothing beyond it.

HIS THREE DECISIONS (2026-10-10 21:36Z), EACH ENCODED
  1. Heartbeat threshold 300 s: fleet:alive:claude-code-cli inside that window
     means the laptop is up. Read through drive-intake's own heartbeat_age, so
     the two services cannot disagree about "online".
  2. The spend cap holds. No model call without a reservation first in the
     shared day ledger fleet:spend:<Toronto day>, in cents CAD, and a cap
     that is unset reads as ZERO: a worker that forgets its cap calls nothing.
     The reservation is one call, one attempt, no automatic retry. A call
     whose outcome is unknown KEEPS its reservation.
  3. Minimal functions, and it runs only when he is not working: a task on
     its inbox is taken when the laptop is off, or when the doc addresses
     claude-api by name, or when the laptop is up but the task has waited
     REASSIGN_AFTER (his 30-minute rule) with nobody's acceptance in the doc.
     It has no shell, no repo, no cloud CLI and no PR. It writes into one
     doc and one Redis prefix. What needs the laptop, it names in the doc
     for claude-code-cli; it never narrates an action it cannot take (L-116).

THE CONTRACT IT SHARES, AND DOES NOT COPY
  cloud/drive-intake/main.py is loaded as a module (`di`): the correlation
  tuple, the request revision, the TO: line, the receipt grammar and the
  heartbeat window are its. One grammar, one hash, or the two services would
  drift and the second would mint a task the first does not know.

THE THINGS THAT GO WRONG, AND WHERE EACH IS STOPPED
  1. A second model call for a task already answered. The doc's trail is read
     FIRST (the record with no TTL): a terminal line by claude-api means done,
     and a line by any OTHER worker means the task was taken elsewhere. Below
     that, a done marker in Redis saves the doc read, and the claim is a
     forward-only state with compare-and-set, so two passes cannot both run.
  2. The model answered and the receipt did not land: the answer is KEPT in
     Redis (fleet:worker:result:<task>) BEFORE the receipt is written, so a
     later pass writes the line from the stored answer and never calls again.
     And the store is read BEFORE the lease or the attempt count is judged:
     the answer is kept before the claim moves to resulted, so a death
     between the two leaves a claim that still says accepted beside an
     answer that exists (Cursor on 9d46250). An answer that exists is
     finished from; UNKNOWN is written only when there is none.
  3. The request was edited after it was queued: the tuple is recomputed from
     the doc; a task id that no longer matches is superseded (the new revision
     comes through intake as its own task) and this entry is never run.
  4. An outcome nobody can prove: a timeout is UNKNOWN, not FAILED and not a
     reason to run again. One attempt by default (MAX_ATTEMPTS); an attempt
     whose lease died is counted, and when the attempts are spent the doc says
     UNKNOWN rather than nothing. A refusal (any HTTP status but 200) is the
     other way round: not an answer and not billed, so the reservation goes
     back, the attempt is not consumed and the task stays open - a fixed key
     or a quieter API finds it waiting - bounded by MAX_REJECTIONS, after
     which the doc says FAILED with the status (Cursor on 9d46250).
  5. The result block retriggering or forging a receipt: every result line is
     prefixed "> " so no line of the model's text can match a receipt shape,
     and the block lives under the RECEIPTS heading, which the request hash
     excludes. A result longer than RESULT_MAX is cut and SAYS so (PR 354).
  6. ACCEPTED claimed before it is readable: the model is called only after
     the ACCEPTED line read back from the doc. No line, no call.

Deploy is not this file's job: the owner runs it, flag off, from the pull
request. This process holds the model key and the Redis auth it is given and
nothing else; every outside call is an injected adapter.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_contract():
    """cloud/drive-intake/main.py, the one copy of the tuple and the receipt grammar."""
    for candidate in (os.path.join(HERE, "drive_intake.py"),
                      os.path.join(HERE, "..", "drive-intake", "main.py")):
        if os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location("drive_intake", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise ImportError("cloud/drive-intake/main.py is the contract and it is not beside this file")


di = _load_contract()

WORKER = "claude-api"
ACCEPTED = "ACCEPTED"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
QUEUED = di.QUEUED
TERMINAL = (SUCCEEDED, FAILED, UNKNOWN)
INTAKE_WRITER = "drive-intake"

DEFAULT_REASSIGN = 1800      # his rule: a task to the laptop unanswered 30 min while off is reassigned
WORK_LEASE = 600             # longer than the model timeout, so a dead lease means a dead attempt
MODEL_TIMEOUT = 300
DEFAULT_MAX_ATTEMPTS = 1     # one attempt, no automatic retry (the budget rule of 2026-10-08)
MAX_REJECTIONS = 3           # HTTP refusals (401, 429, 529...) are not answers and not billed: retried, bounded
MAX_TASKS_PER_PASS = 1       # one model call per pass; the schedule is the throttle
INBOX_LOOKBACK = 500
REQUEST_MAX = 12000          # chars of the request given to the model; cut is SAID, never silent
RESULT_MAX = 3000            # chars of the answer written into the doc; cut is SAID
DEFAULT_MAX_TOKENS = 1500
CLAIM_TTL = 2592000          # 30 days, like every fleet: key here
DONE_TTL = 5184000           # 60 days: a done marker that outlives the inbox entry's usefulness
RESULT_TTL = 604800          # 7 days for a stored answer whose receipt is still owed
SPEND_TTL = 259200           # the day ledger lives three days, long enough to be read

STATE_ACCEPTED = "accepted"  # accepted:<since>:<attempt>:<rejections> - a call is in flight or died
STATE_CAPPED = "capped"      # capped:<day>:<attempt>:<rejections> - the day's cap refused it; tomorrow retries
STATE_RESULTED = "resulted"  # resulted:<since>:<attempt>:<rejections> - answer stored, terminal line owed
STATE_DONE = "done"

# Any receipt in the shared shape, by anyone: "<STATE> - task <id> - request .. - rev .. - recipient .. - by <who> - "
TRAIL = re.compile(
    r"(?m)^\s*(?P<state>[A-Z]+) - task (?P<task>task-[0-9a-f]{16}) - request \S+ - rev \S+ - "
    r"recipient \S+ - by (?P<by>\S+) - ")
RESULT_PREFIX = "> "

DOCTRINE = (
    "You are claude-api, the always-on stand-in for claude-code-cli on the SFDC24 fleet. "
    "You are a model endpoint with NO tools: no shell, no repository, no Salesforce CLI, no Drive, "
    "no browser, and you cannot open pull requests, deploy, or spend. A small worker writes your "
    "answer into the Google Doc the request came from, under its receipts; that is the only thing "
    "that happens as a result of your words. Answer the request itself: what can be settled by "
    "reasoning, decided, drafted or explained, in plain prose, at most %d characters. Then, under "
    "the heading FOR THE LAPTOP:, list what needs claude-code-cli's tools, one item per line, or "
    "write 'nothing'. Never say you did, ran, checked, opened or sent anything; you cannot. "
    "Address the owner as Mr. Salam."
)


class Unconfigured(Exception):
    """A credential, model id, cap or client is missing. Nothing runs and the gap is named."""


class ModelError(Exception):
    """A 200 that holds no usable answer (no text, unparseable). Billed, and final."""


class ModelRejected(Exception):
    """The API refused the request (any status but 200). Not an answer, not billed: refund and retry, bounded."""


class ModelUnknown(Exception):
    """No answer inside the timeout. It may have been billed; it cannot be proven either way."""


def enabled_from_env(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get("CLAUDE_API_WORKER_ENABLED") == "on"


def trail_for(doc_text: str, task_id: str) -> list:
    """Every receipt in the doc for this task, in order: [(state, by), ...]."""
    found = []
    for match in TRAIL.finditer(doc_text or ""):
        if match.group("task") == task_id:
            found.append((match.group("state"), match.group("by")))
    return found


def taken_elsewhere(trail: list):
    """The first worker other than intake or this one that wrote a line, or None."""
    for _state, by in trail:
        if by not in (INTAKE_WRITER, WORKER):
            return by
    return None


def finished(trail: list) -> bool:
    return any(state in TERMINAL and by == WORKER for state, by in trail)


def addressed_to_worker(doc_text: str) -> bool:
    found = di.TO_LINE.search(di.strip_receipts(doc_text or ""))
    return bool(found) and di.canonical(found.group(1)) == WORKER


def receipt_id_for(task_id: str, state: str) -> str:
    """One per (worker, task, state), and distinct from drive-intake's for the same state."""
    return hashlib.sha256("\x1f".join((WORKER, task_id, state)).encode("utf-8")).hexdigest()[:16]


def receipt_line(state: str, tuple_: dict, at: str, receipt_id: str, detail: str = "") -> str:
    """The shared shape, by claude-api, ending with its id so a read-back can find it."""
    parts = [
        state,
        "task " + tuple_["task_id"],
        "request " + tuple_["request_id"],
        "rev " + tuple_["source_revision"][:12],
        "recipient " + WORKER,
        "by " + WORKER,
        at,
    ]
    if detail:
        parts.append(detail)
    parts.append("id " + receipt_id)
    return " - ".join(parts)


def result_block(text: str, limit: int = RESULT_MAX) -> str:
    """The answer as it goes into the doc: every line prefixed, a cut said out loud (guard 5)."""
    body = (text or "").strip()
    cut = ""
    if len(body) > limit:
        cut = "[cut at %d of %d characters]" % (limit, len(body))
        body = body[:limit]
    lines = [RESULT_PREFIX + line for line in body.splitlines()] or [RESULT_PREFIX]
    if cut:
        lines.append(RESULT_PREFIX + cut)
    return "\n".join(lines)


def request_for_model(doc_text: str, limit: int = REQUEST_MAX) -> str:
    body = di.strip_receipts(doc_text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit] + "\n[request cut at %d of %d characters]" % (limit, len(body))


def _refresh_revision(deps, tuple_):
    try:
        doc = deps.docs.read(tuple_["source_doc_id"]) if deps.docs is not None else None
    except Exception:  # noqa: BLE001 - keep the old revision; the next write says if it is stale
        return
    revision = str((doc or {}).get("revision_id") or "")
    if di.ID_OK.fullmatch(revision):
        tuple_["drive_revision"] = revision


def write_receipt(deps, tuple_: dict, doc_text: str, state: str, at: str, detail: str = "",
                  extra: str = "") -> str:
    """Append one receipt (and, for a terminal line, the answer under it), then READ IT BACK.

    Mirrors drive-intake's writer: has() first so a retry is a no-op; a lost
    response is settled by reading the line back and refreshing the revision
    from the doc, then ONE retry. Not read back is "not_read_back", never
    success.
    """
    if deps.receipts is None:
        return "no_receipt_writer"
    doc_id = tuple_["source_doc_id"]
    receipt_id = receipt_id_for(tuple_["task_id"], state)
    try:
        if deps.receipts.has(doc_id, receipt_id):
            return "written"
    except Exception as exc:  # noqa: BLE001
        return "unknown:" + type(exc).__name__
    line = receipt_line(state, tuple_, at, receipt_id, detail)
    if extra:
        line = line + "\n" + extra
    block = di.receipt_block(doc_text, line)
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
            continue
        if new_revision:
            tuple_["drive_revision"] = str(new_revision)
        break
    try:
        return "written" if deps.receipts.has(doc_id, receipt_id) else "not_read_back"
    except Exception as exc:  # noqa: BLE001
        return "unknown:" + type(exc).__name__


def _now(deps) -> float:
    return float(deps.now) if deps.now is not None else time.time()


def _split(state):
    """'accepted:1000:1:0' -> ('accepted', '1000', 1, 0): phase, a clock or a day, attempts, rejections."""
    if not state:
        return None, "", 0, 0
    parts = str(state).split(":")

    def number(index):
        try:
            return int(parts[index]) if len(parts) > index else 0
        except ValueError:
            return 0
    return parts[0], parts[1] if len(parts) > 1 else "", number(2), number(3)


def _state(phase, middle, attempt, rejected=0) -> str:
    return "%s:%s:%d:%d" % (phase, middle, int(attempt), int(rejected))


def entry_time(entry_id, fields) -> float | None:
    """When the entry landed: the stream id's clock first (the server's), else the writer's `at`."""
    try:
        return int(str(entry_id).split("-")[0]) / 1000.0
    except (ValueError, AttributeError):
        pass
    text = str(fields.get("at") or "").strip()
    if not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def tuple_from_entry(fields: dict):
    """The tuple the inbox entry carries, or None when it is not one of ours."""
    if (fields.get("kind") or "") != "drive_request":
        return None
    tuple_ = {
        "source_doc_id": fields.get("source_doc_id") or "",
        "source_revision": fields.get("source_revision") or "",
        "drive_revision": "r",            # unknown until the doc is read; placeholder for tuple_ok
        "request_id": fields.get("request_id") or "",
        "task_id": fields.get("task_id") or "",
        "recipient_id": fields.get("recipient_id") or "",
    }
    return tuple_ if di.tuple_ok(tuple_) and tuple_["recipient_id"] == WORKER else None


def _mark_done(deps, task_id):
    try:
        deps.claims.mark_done(task_id)
    except Exception:  # noqa: BLE001 - a marker is a saving, not a record; the doc is the record
        pass


def consider(entry_id, fields: dict, deps) -> dict:
    """One inbox entry. Returns what happened; "ran" is True only when the model was called."""
    tuple_ = tuple_from_entry(fields)
    if tuple_ is None:
        return {"status": "not_mine", "task": fields.get("task_id") or None, "ran": False}
    task_id = tuple_["task_id"]
    out = {"status": "", "task": task_id, "ran": False}
    try:
        if deps.claims.done(task_id):
            out["status"] = "done"
            return out
    except Exception as exc:  # noqa: BLE001 - an unreadable marker is an absent one; the doc decides
        out["marker"] = "unknown:" + type(exc).__name__

    # The doc is the authority and the record with no TTL. Read it, every time.
    try:
        doc = deps.docs.read(tuple_["source_doc_id"])
    except Exception as exc:  # noqa: BLE001
        out.update(status="read_error", error=type(exc).__name__)
        return out
    if not isinstance(doc, dict):
        out.update(status="read_error", error="shape")
        return out
    if deps.folder_id and doc.get("folder_id") != deps.folder_id:
        out["status"] = "out_of_scope"
        return out
    drive_revision = str(doc.get("revision_id") or "")
    if not di.ID_OK.fullmatch(drive_revision):
        out["status"] = "no_revision"
        return out
    text = doc.get("text") or ""
    if len(text) > di.MAX_DOC:
        out.update(status="too_large", chars=len(text))
        return out
    current_tuple = di.correlate(tuple_["source_doc_id"], text, drive_revision, WORKER)
    if current_tuple["task_id"] != task_id:
        # Edited since it was queued. The edit is its own task through intake; this one is over.
        _mark_done(deps, task_id)
        out.update(status="superseded", now=current_tuple["task_id"])
        return out
    tuple_ = current_tuple

    trail = trail_for(text, task_id)
    if finished(trail):
        _mark_done(deps, task_id)
        out["status"] = "done"
        return out
    other = taken_elsewhere(trail)
    if other:
        _mark_done(deps, task_id)
        out.update(status="taken", by=other)
        return out

    now = _now(deps)
    alive = di.heartbeat_age(deps.claims, now, window=deps.heartbeat_window)
    if alive is not None and not addressed_to_worker(text):
        landed = entry_time(entry_id, fields)
        waited = (now - landed) if landed is not None else 0.0
        if waited < deps.reassign_after:
            # The laptop is up and the task is inside its 30 minutes. Not ours yet.
            out.update(status="deferred", waited=int(waited))
            return out
        out["reassigned_after"] = int(waited)

    # The claim: forward only, every move a compare-and-set.
    fresh = False
    try:
        current = deps.claims.state(task_id)
        if current is None:
            initial = _state(STATE_ACCEPTED, int(now), 1)
            if deps.claims.claim(task_id, initial):
                current, fresh = initial, True
            else:
                current = deps.claims.state(task_id)
    except Exception as exc:  # noqa: BLE001
        out.update(status="claim_error", error=type(exc).__name__)
        return out
    if current is None:
        out.update(status="claim_error", error="unreadable")
        return out
    phase, middle, attempt, rejected = _split(current)
    if phase == STATE_DONE:
        _mark_done(deps, task_id)
        out["status"] = "done"
        return out

    if phase == STATE_RESULTED:
        # The answer exists; only its line is owed. No model call, whatever the lease says.
        return _finish_from_store(deps, tuple_, text, current, attempt, out)

    if phase == STATE_CAPPED:
        if middle == deps.day(now):
            out.update(status="capped", day=middle)
            return out
        moved = _state(STATE_ACCEPTED, int(now), attempt + 1, rejected)
        if not deps.claims.advance(task_id, current, moved):
            out["status"] = "in_progress"
            return out
        current, attempt = moved, attempt + 1
    elif phase == STATE_ACCEPTED and not fresh:
        # The store FIRST (Cursor on 9d46250): the answer is kept before the claim moves to
        # resulted, so a death or a lost compare-and-set between the two leaves a claim that
        # still says accepted with the answer sitting in Redis. An answer that exists is
        # finished from, whatever the lease or the attempt count says; UNKNOWN is for no answer.
        try:
            kept = deps.claims.result(task_id)
        except Exception as exc:  # noqa: BLE001
            out.update(status="result_unreadable", error=type(exc).__name__)
            return out
        if kept:
            resulted = _state(STATE_RESULTED, int(now), attempt, rejected)
            if not deps.claims.advance(task_id, current, resulted):
                out["status"] = "in_progress"
                return out
            return _finish_from_store(deps, tuple_, text, resulted, attempt, out)
        try:
            since = float(middle)
        except ValueError:
            since = 0.0
        if since and now - since < WORK_LEASE:
            # Inside its lease, or ahead of this clock (skew, not death): another pass owns it.
            out["status"] = "in_progress"
            return out
        if attempt >= deps.max_attempts:
            # The attempts are spent and none left a result. The doc gets UNKNOWN, not silence.
            out["receipt"] = write_receipt(
                deps, tuple_, text, UNKNOWN, deps.at,
                "attempt %d left no result and no further attempt is allowed; the reservation stands"
                % attempt)
            if out["receipt"] in ("written", "no_receipt_writer"):
                deps.claims.advance(task_id, current, STATE_DONE)
                _mark_done(deps, task_id)
                out["status"] = "unknown"
            else:
                out["status"] = "unknown_receipt_owed"
            return out
        moved = _state(STATE_ACCEPTED, int(now), attempt + 1, rejected)
        if not deps.claims.advance(task_id, current, moved):
            out["status"] = "in_progress"
            return out
        current, attempt = moved, attempt + 1

    # ACCEPTED must be READABLE before anything is reported as picked up (guard 6).
    deadline = datetime.fromtimestamp(now + WORK_LEASE, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out["accepted"] = write_receipt(
        deps, tuple_, text, ACCEPTED, deps.at,
        "attempt %d - lease until %s - model %s" % (attempt, deadline, deps.model_name or "unset"))
    text = di.with_heading(text)
    if out["accepted"] not in ("written", "no_receipt_writer"):
        deps.claims.advance(task_id, current, _state(STATE_ACCEPTED, 0, attempt - 1, rejected))  # lease given up
        out["status"] = "accepted_receipt_owed"
        return out

    # The cap, BEFORE the call. No reservation, no call (decision 2).
    day = deps.day(now)
    try:
        reserved = deps.ledger.reserve(day, deps.reserve_cents, deps.cap_cents)
    except Exception as exc:  # noqa: BLE001 - a ledger that cannot answer has not reserved
        reserved = False
        out["ledger"] = "unknown:" + type(exc).__name__
    if not reserved:
        spent = _spent(deps, day)
        detail = ("daily spend cap reached (%s of %d cents CAD reserved on %s); tried again next day"
                  % (spent, deps.cap_cents, day)) if deps.cap_cents > 0 and deps.reserve_cents > 0 \
            else "no spend cap or reservation configured; nothing is called until there is one"
        out["receipt"] = write_receipt(deps, tuple_, text, QUEUED, deps.at, detail)
        deps.claims.advance(task_id, current, _state(STATE_CAPPED, day, attempt - 1, rejected))
        out["status"] = "capped"
        return out

    # ONE call. Whatever it returns is kept BEFORE the receipt is written (guard 2).
    out["ran"] = True
    try:
        answer = deps.model(DOCTRINE % RESULT_MAX, request_for_model(text))
        state, detail, extra, usage = _settle_success(deps, answer, day)
    except ModelUnknown as exc:
        state, detail, extra, usage = UNKNOWN, "no answer within %ds (%s); the reservation stands" % (
            MODEL_TIMEOUT, type(exc).__name__), "", None
    except Unconfigured as exc:
        _refund(deps, day)
        deps.claims.advance(task_id, current, _state(STATE_ACCEPTED, 0, attempt - 1, rejected))
        out.update(status="unconfigured", error=str(exc), ran=False)
        return out
    except ModelRejected as exc:
        # Not an answer and not billed (Cursor on 9d46250): the reservation goes back, the attempt
        # is not consumed, and the task stays open for a later pass - a fixed key or a quieter API
        # reopens nothing because nothing was closed. Bounded: after MAX_REJECTIONS the doc says
        # FAILED with the status, so a request the API will never take does not knock every tick.
        _refund(deps, day)
        rejected += 1
        if rejected < MAX_REJECTIONS:
            deps.claims.advance(task_id, current, _state(STATE_ACCEPTED, 0, attempt - 1, rejected))
            out.update(status="rejected", error=str(exc)[:80], rejections=rejected)
            return out
        state, detail, extra, usage = FAILED, "the API refused %d times, last %s; nothing was billed" % (
            rejected, str(exc)[:80]), "", None
    except ModelError as exc:
        state, detail, extra, usage = FAILED, "the model answered with an error: %s" % str(exc)[:160], "", None
    except Exception as exc:  # noqa: BLE001 - a transport failure of unknown depth is UNKNOWN
        state, detail, extra, usage = UNKNOWN, "the call failed (%s); the reservation stands" % type(exc).__name__, "", None
    stored = {"state": state, "detail": detail, "extra": extra, "usage": usage}
    try:
        deps.claims.keep_result(task_id, json.dumps(stored, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - the line is still attempted now; a retry would re-call
        out["kept"] = "unknown:" + type(exc).__name__
    resulted = _state(STATE_RESULTED, int(now), attempt, rejected)
    if not deps.claims.advance(task_id, current, resulted):
        out["status"] = "in_progress"
        return out
    return _finish_from_store(deps, tuple_, text, resulted, attempt, out, stored)


def _spent(deps, day):
    try:
        return int(deps.ledger.spent(day))
    except Exception:  # noqa: BLE001
        return "?"


def _refund(deps, day):
    try:
        deps.ledger.settle(day, -int(deps.reserve_cents))
    except Exception:  # noqa: BLE001 - a refund that fails leaves the reservation, the safe side
        pass


def _settle_success(deps, answer, day):
    """A real answer: its lines, its usage, and the ledger moved from the reservation to the cost."""
    if not isinstance(answer, dict) or not (answer.get("text") or "").strip():
        raise ModelError("empty answer (stop_reason %s)" % (answer or {}).get("stop_reason"))
    usage = {"in": int(answer.get("input_tokens") or 0), "out": int(answer.get("output_tokens") or 0),
             "model": str(answer.get("model") or deps.model_name or "")}
    cents = actual_cents(usage["in"], usage["out"], deps.price_in, deps.price_out, deps.cad_per_usd)
    if cents is not None:
        try:
            deps.ledger.settle(day, cents - int(deps.reserve_cents))
        except Exception:  # noqa: BLE001 - the reservation stands; never a cheaper number by accident
            cents = None
    try:
        deps.ledger.record(day, {"by": WORKER, "model": usage["model"], "in": str(usage["in"]),
                                 "out": str(usage["out"]),
                                 "cents": str(cents if cents is not None else deps.reserve_cents),
                                 "settled": "1" if cents is not None else "0"})
    except Exception:  # noqa: BLE001
        pass
    detail = "model %s - tokens in %d out %d - %s" % (
        usage["model"], usage["in"], usage["out"],
        ("%d cents CAD" % cents) if cents is not None else ("%d cents CAD reserved" % deps.reserve_cents))
    return SUCCEEDED, detail, result_block(answer["text"]), usage


def actual_cents(tokens_in, tokens_out, price_in, price_out, cad_per_usd):
    """Cents CAD for one call, or None when a price is not configured. Rounded UP, never down."""
    try:
        if price_in is None or price_out is None or cad_per_usd is None:
            return None
        usd = tokens_in / 1e6 * float(price_in) + tokens_out / 1e6 * float(price_out)
        return int(math.ceil(usd * float(cad_per_usd) * 100))
    except (TypeError, ValueError):
        return None


def _finish_from_store(deps, tuple_, text, current, attempt, out, stored=None):
    """Write the terminal line from the stored answer; done only when it read back."""
    task_id = tuple_["task_id"]
    if stored is None:
        try:
            raw = deps.claims.result(task_id)
        except Exception as exc:  # noqa: BLE001
            out.update(status="result_unreadable", error=type(exc).__name__)
            return out
        try:
            stored = json.loads(raw) if raw else None
        except ValueError:
            stored = None
        if not stored:
            # The answer is gone with its receipt unwritten. Say UNKNOWN; never call again for it.
            stored = {"state": UNKNOWN, "detail": "the stored answer expired before its line landed",
                      "extra": ""}
    out["receipt"] = write_receipt(deps, tuple_, text, stored["state"], deps.at,
                                   stored.get("detail") or "", stored.get("extra") or "")
    if out["receipt"] in ("written", "no_receipt_writer"):
        deps.claims.advance(task_id, current, STATE_DONE)
        _mark_done(deps, task_id)
        out["status"] = stored["state"].lower()
    else:
        out["status"] = "terminal_receipt_owed"
    return out


def run_once(deps) -> dict:
    """One pass over claude-api's inbox. Disabled reads nothing, writes nothing, calls nothing."""
    if not deps.enabled:
        return {"status": "disabled", "considered": []}
    if deps.inbox is None or deps.docs is None or deps.claims is None or deps.ledger is None:
        return {"status": "no_store", "considered": []}
    now = _now(deps)
    try:
        deps.claims.beat(WORKER, now)
    except Exception:  # noqa: BLE001 - a missed beat is not a reason to skip the work
        pass
    try:
        entries = list(deps.inbox.read(WORKER, INBOX_LOOKBACK) or [])
    except Exception as exc:  # noqa: BLE001
        return {"status": "inbox_error", "error": type(exc).__name__, "considered": []}
    considered, calls = [], 0
    for entry_id, fields in reversed(entries):        # the stream is read newest first; work oldest first
        if calls >= MAX_TASKS_PER_PASS:
            considered.append({"status": "left_for_next_pass", "task": fields.get("task_id"), "ran": False})
            continue
        result = consider(entry_id, fields, deps)
        if result.get("ran"):
            calls += 1
        considered.append(result)
    return {"status": "ran" if calls else "idle", "calls": calls, "considered": considered}


# ---------------------------------------------------------------------------
# Adapters. Each opens no socket at import; production wires them in main().

class AnthropicModel:
    """One Messages call over urllib. Returns text and usage; raises by outcome class."""

    URL = "https://api.anthropic.com/v1/messages"
    VERSION = "2023-06-01"

    def __init__(self, key="", model="", max_tokens=DEFAULT_MAX_TOKENS, post=None, timeout=MODEL_TIMEOUT):
        self.key, self.model, self.max_tokens, self.post, self.timeout = key or "", model or "", int(max_tokens), post, timeout

    def body_for(self, system: str, prompt: str) -> bytes:
        return json.dumps({"model": self.model, "max_tokens": self.max_tokens, "system": system,
                           "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")

    def __call__(self, system: str, prompt: str) -> dict:
        if not self.key or not self.model:
            raise Unconfigured("no model configured (ANTHROPIC_API_KEY, CLAUDE_API_WORKER_MODEL)")
        body = self.body_for(system, prompt)
        headers = {"content-type": "application/json", "x-api-key": self.key,
                   "anthropic-version": self.VERSION}
        if self.post is not None:
            status, raw = self.post(self.URL, body, headers, self.timeout)
        else:
            status, raw = _post(self.URL, body, headers, self.timeout)
        if status != 200:
            raise ModelRejected("HTTP %s" % status)
        try:
            parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except ValueError as exc:
            raise ModelError("unparseable answer") from exc
        usage = parsed.get("usage") or {}
        text = "\n".join(b.get("text") or "" for b in (parsed.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "text").strip()
        stop = parsed.get("stop_reason")
        if not text:
            raise ModelError("empty answer (stop_reason %s)" % stop)
        return {"text": text, "input_tokens": usage.get("input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "model": parsed.get("model") or self.model, "stop_reason": stop}


def _post(url, body, headers, timeout):
    """(status, raw). A timeout or a transport failure is ModelUnknown: it may have been billed."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise ModelUnknown(type(exc).__name__) from exc


class MemoryClaims(di.MemoryClaims):
    """Process-local stand-in for the worker's keys: claim, done marker, stored result, beats."""

    def __init__(self, beats=None):
        super().__init__(beats)
        self.done_marks, self.results = set(), {}

    def claim(self, task_id, initial):
        if task_id in self.states:
            return False
        self.states[task_id] = initial
        return True

    def done(self, task_id):
        return task_id in self.done_marks

    def mark_done(self, task_id):
        self.done_marks.add(task_id)

    def result(self, task_id):
        return self.results.get(task_id)

    def keep_result(self, task_id, raw):
        self.results[task_id] = raw

    def beat(self, agent, now):
        self.beats[agent] = float(now)


class MemoryLedger:
    def __init__(self):
        self.days, self.calls = {}, []

    def reserve(self, day, cents, cap):
        if cents <= 0 or cap <= 0:
            return False
        if self.days.get(day, 0) + cents > cap:
            return False
        self.days[day] = self.days.get(day, 0) + cents
        return True

    def settle(self, day, delta):
        self.days[day] = max(0, self.days.get(day, 0) + int(delta))

    def spent(self, day):
        return self.days.get(day, 0)

    def record(self, day, fields):
        self.calls.append((day, dict(fields)))


CAS_LUA = di.CAS_LUA
# Reserve in one round trip: add, and take it straight back when that crosses the cap.
RESERVE_LUA = ("local v = redis.call('INCRBY', KEYS[1], ARGV[1]) "
               "redis.call('EXPIRE', KEYS[1], ARGV[3]) "
               "if v > tonumber(ARGV[2]) then redis.call('DECRBY', KEYS[1], ARGV[1]) return 0 end "
               "return 1")


class RedisClaims:
    """fleet:worker:claim:<task>, fleet:worker:done:<task>, fleet:worker:result:<task>, fleet:alive:<agent>."""

    def __init__(self, client):
        self.client = client

    def _get(self, key):
        raw = self.client.get(key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return raw or None

    def claim(self, task_id, initial):
        return bool(self.client.set("fleet:worker:claim:" + task_id, initial, nx=True, ex=CLAIM_TTL))

    def state(self, task_id):
        return self._get("fleet:worker:claim:" + task_id)

    def advance(self, task_id, current, new):
        return bool(self.client.eval(CAS_LUA, 1, "fleet:worker:claim:" + task_id, current, new, CLAIM_TTL))

    def done(self, task_id):
        return self._get("fleet:worker:done:" + task_id) is not None

    def mark_done(self, task_id):
        self.client.set("fleet:worker:done:" + task_id, "1", ex=DONE_TTL)

    def result(self, task_id):
        return self._get("fleet:worker:result:" + task_id)

    def keep_result(self, task_id, raw):
        self.client.set("fleet:worker:result:" + task_id, raw, ex=RESULT_TTL)

    def heartbeat(self, agent):
        raw = self._get("fleet:alive:" + agent)
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def beat(self, agent, now):
        self.client.set("fleet:alive:" + agent, "%d" % int(now), ex=3600)


class RedisLedger:
    """fleet:spend:<day> in cents CAD, shared by every agent that reserves before it calls."""

    def __init__(self, client):
        self.client = client

    def reserve(self, day, cents, cap):
        if int(cents) <= 0 or int(cap) <= 0:
            return False
        return bool(self.client.eval(RESERVE_LUA, 1, "fleet:spend:" + day, int(cents), int(cap), SPEND_TTL))

    def settle(self, day, delta):
        value = self.client.incrby("fleet:spend:" + day, int(delta))
        if value < 0:
            self.client.set("fleet:spend:" + day, "0", ex=SPEND_TTL)
        self.client.expire("fleet:spend:" + day, SPEND_TTL)

    def spent(self, day):
        raw = self.client.get("fleet:spend:" + day)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return int(raw or 0)

    def record(self, day, fields):
        self.client.xadd("fleet:spend:%s:calls" % day, dict(fields))


class RedisInbox:
    def __init__(self, client):
        self.client = client

    def read(self, agent, count):
        rows = self.client.xrevrange("fleet:inbox:" + agent, count=count) or ()
        out = []
        for entry_id, fields in rows:
            decoded = {}
            for name, value in (fields or {}).items():
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                decoded[name] = value
            out.append((entry_id.decode("utf-8") if isinstance(entry_id, bytes) else str(entry_id), decoded))
        return out


def toronto_day(now: float) -> str:
    """The cap is per Toronto calendar day (the owner's budget rule). UTC only if the zone is missing."""
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        return datetime.fromtimestamp(now, tz=ZoneInfo("America/Toronto")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d") + "Z"


def _int_env(environ, name, default=0):
    try:
        return int(environ.get(name) or default)
    except ValueError:
        return default


def _float_env(environ, name):
    raw = environ.get(name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class Deps:
    def __init__(self, enabled=False, inbox=None, docs=None, receipts=None, claims=None, ledger=None,
                 model=None, model_name="", folder_id="", now=None, at="", day=None,
                 heartbeat_window=di.DEFAULT_HEARTBEAT, reassign_after=DEFAULT_REASSIGN,
                 max_attempts=DEFAULT_MAX_ATTEMPTS, cap_cents=0, reserve_cents=0,
                 price_in=None, price_out=None, cad_per_usd=None):
        self.enabled = enabled
        self.inbox = inbox
        self.docs = docs
        self.receipts = receipts
        self.claims = claims
        self.ledger = ledger
        self.model = model or _no_model
        self.model_name = model_name
        self.folder_id = folder_id
        self.now = now
        self.at = at
        self.day = day or toronto_day
        self.heartbeat_window = heartbeat_window
        self.reassign_after = reassign_after
        self.max_attempts = max_attempts
        self.cap_cents = int(cap_cents)
        self.reserve_cents = int(reserve_cents)
        self.price_in = price_in
        self.price_out = price_out
        self.cad_per_usd = cad_per_usd


def _no_model(_system, _prompt):
    raise Unconfigured("no model configured (ANTHROPIC_API_KEY, CLAUDE_API_WORKER_MODEL)")


def deps_from_env(environ=None, client=None, docs=None):  # pragma: no cover - wiring, not logic
    environ = os.environ if environ is None else environ
    model_name = environ.get("CLAUDE_API_WORKER_MODEL") or ""
    model = AnthropicModel(key=environ.get("ANTHROPIC_API_KEY") or "", model=model_name,
                           max_tokens=_int_env(environ, "CLAUDE_API_WORKER_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    return Deps(
        enabled=enabled_from_env(environ),
        inbox=RedisInbox(client) if client is not None else None,
        docs=docs, receipts=docs,
        claims=RedisClaims(client) if client is not None else None,
        ledger=RedisLedger(client) if client is not None else None,
        model=model, model_name=model_name,
        folder_id=environ.get("DRIVE_INTAKE_FOLDER_ID") or "",
        at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        heartbeat_window=_int_env(environ, "DRIVE_INTAKE_HEARTBEAT_SECONDS", di.DEFAULT_HEARTBEAT),
        reassign_after=_int_env(environ, "CLAUDE_API_WORKER_REASSIGN_SECONDS", DEFAULT_REASSIGN),
        max_attempts=_int_env(environ, "CLAUDE_API_WORKER_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
        cap_cents=_int_env(environ, "FLEET_SPEND_CAP_CENTS_CAD", 0),
        reserve_cents=_int_env(environ, "CLAUDE_API_WORKER_RESERVE_CENTS_CAD", 0),
        price_in=_float_env(environ, "CLAUDE_API_WORKER_USD_PER_MTOK_IN"),
        price_out=_float_env(environ, "CLAUDE_API_WORKER_USD_PER_MTOK_OUT"),
        cad_per_usd=_float_env(environ, "FLEET_CAD_PER_USD"),
    )


def main():  # pragma: no cover - the job entry point
    if not enabled_from_env():
        print(json.dumps({"status": "disabled"}))
        return 0
    sys.path.insert(0, HERE)
    client = None
    try:
        import redis_dual  # noqa: PLC0415 - beside this file in the image
        client = redis_dual.client(redis_dual.Settings(), precheck=False)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "no_redis", "error": type(exc).__name__}))
    docs = None
    try:
        import gdocs  # noqa: PLC0415
        docs = gdocs.GoogleDocs()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "no_docs", "error": type(exc).__name__}))
    deps = deps_from_env(client=client, docs=docs)
    out = run_once(deps)
    print(json.dumps(out, sort_keys=True))     # statuses and task ids; never the doc's text
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

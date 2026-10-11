#!/usr/bin/env python3
"""The laptop lane: what the cloud does for claude-code-cli, which can reach neither Redis nor Docs.

WHY THIS EXISTS
  Step 3 of the broken-telephone fix (Converspan Channel Architecture v0.1):
  "laptop ON -> CLI picks up". claude-code-cli runs on the owner's laptop. It
  can read Drive, and it can post ONE governed board row that the bus-requests
  worker executes inside the VPC (scripts/redis_inbox.py). It cannot write a
  Google Doc and it cannot read Redis. So a task routed to it needs three
  things done on its behalf, and this is where they are done, once per tick,
  with no model and no spend:

  1. THE DOORBELL. A task on fleet:inbox:claude-code-cli becomes ONE board row
     addressed to claude-code-cli, which the laptop's doorbell already reads.
     The row carries ids only - task, doc, request - and never the doc's
     text: the board is public (Aya, 2026-10-10). #359's wake for
     claude-code-cli is a GitHub Actions job that echoes; this row is the wake
     that reaches a session. A row is money (the owner, 2026-10-04), so it is
     at most once per task: a marker is claimed BEFORE the post, and a post
     that did not read back is retried once, an hour later, never more.
  2. ACCEPTED. The laptop accepts with a governed `set fleet:worker:accept:<task>`,
     audited in gov:audit under its own principal. That is its authenticated
     acknowledgement; the line in the doc is written here on its behalf and
     SAYS so, naming the key it came from.
  3. THE TERMINAL LINE. The laptop finishes with `set fleet:worker:done:<task>`,
     a short status, a note and a link to the evidence - the owner's own
     shape, "DONE by <tag> - <timestamp> - <link to PR/evidence>". Short,
     because the value travels on the public board: a status and a pointer,
     never the work itself.

  The doc trail is the record with no TTL and is read first; a settled marker
  saves the doc read once the terminal line has read back. Every line is
  read back before anything is marked.
"""
from __future__ import annotations

import json
import re

LAPTOP = "claude-code-cli"
RECORDER = "claude-api"
ACCEPT_VALUE = re.compile(r"^claude-code-cli:(?P<since>\d{9,11})(?::(?P<lease>\d{1,6}))?$")
TERMINAL = ("SUCCEEDED", "FAILED")
NOTE_MAX = 300
EVIDENCE_OK = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{1,400}$")
WAKE_RETRY_AFTER = 3600
WOKE_TTL = 5184000           # 60 days, like the worker's done marker
ROW_TEXT = ("A Drive request is on fleet:inbox:claude-code-cli. Read the doc in Drive, then accept "
            "and finish it with scripts/redis_inbox.py accept / done --task <task>.")


def wake_row(tuple_: dict) -> dict:
    """ONE board row, ids only. The payload has no pipe inside a value (BCB-1 has no escaping)."""
    task = tuple_["task_id"]
    row_id = "CAW-WAKE-" + task.replace("task-", "", 1)
    text = ROW_TEXT.replace("<task>", task)
    fields = ["BCB|v=1", "id=" + row_id, "phase=TASK", "from=" + RECORDER, "to=" + LAPTOP, "via=direct",
              "task=" + task, "doc=" + tuple_["source_doc_id"], "request=" + tuple_["request_id"],
              "text=" + text]
    payload = "|".join(fields)
    assert all("|" not in f.split("=", 1)[1] for f in fields[1:]), payload
    return {
        "row_id": row_id,
        "source_tag": RECORDER,
        "target_surface": LAPTOP,
        "action_type": "TASK",
        "category": "OPEN",
        "project_tag": "Blackboard",
        "gist": "drive request for %s: %s" % (LAPTOP, task),
        "subgist": tuple_["source_doc_id"],
        "payload": payload,
    }


def parse_accept(raw):
    """'claude-code-cli:<epoch>[:<lease seconds>]' -> (since, lease) or None."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    found = ACCEPT_VALUE.match((raw or "").strip())
    if not found:
        return None
    return int(found.group("since")), int(found.group("lease") or 0)


def parse_done(raw):
    """The laptop's terminal word: {"by": "claude-code-cli", "state": SUCCEEDED|FAILED, "note", "evidence"}.

    Anything else is None: a value that is not this shape writes nothing, rather than a line that
    says more or other than the laptop said. The note is bounded and the evidence is an https link.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("by") != LAPTOP or data.get("state") not in TERMINAL:
        return None
    note = " ".join(str(data.get("note") or "").split())
    if len(note) > NOTE_MAX:
        note = note[:NOTE_MAX].rstrip() + " [note cut at %d of %d characters]" % (NOTE_MAX, len(note))
    evidence = str(data.get("evidence") or "").strip()
    if evidence and not EVIDENCE_OK.match(evidence):
        evidence = "(evidence link refused: not an https URL)"
    return {"state": data["state"], "note": note, "evidence": evidence}


def done_detail(done: dict) -> str:
    parts = []
    if done["note"]:
        parts.append(done["note"].replace(" - ", " / "))
    if done["evidence"]:
        parts.append("evidence " + done["evidence"])
    parts.append("recorded by %s from fleet:worker:done" % RECORDER)
    return " - ".join(parts)


def run(wk, deps, now: float) -> list:
    """One pass over the laptop's inbox. Returns one record per entry; writes nothing it cannot read back."""
    if not deps.lane_enabled:
        return []
    if deps.inbox is None or deps.docs is None or deps.claims is None:
        return [{"status": "no_store", "task": None}]
    try:
        entries = list(deps.inbox.read(LAPTOP, wk.INBOX_LOOKBACK) or [])
    except Exception as exc:  # noqa: BLE001
        return [{"status": "inbox_error", "error": type(exc).__name__, "task": None}]
    return [consider(wk, entry_id, fields, deps, now) for entry_id, fields in reversed(entries)]


def consider(wk, entry_id, fields: dict, deps, now: float) -> dict:
    if (fields.get("kind") or "") != "drive_request" or fields.get("recipient_id") != LAPTOP:
        return {"status": "not_lane", "task": fields.get("task_id") or None}
    tuple_ = {
        "source_doc_id": fields.get("source_doc_id") or "",
        "source_revision": fields.get("source_revision") or "",
        "drive_revision": "r",
        "request_id": fields.get("request_id") or "",
        "task_id": fields.get("task_id") or "",
        "recipient_id": LAPTOP,
    }
    if not wk.di.tuple_ok(tuple_):
        return {"status": "bad_entry", "task": fields.get("task_id") or None}
    task_id = tuple_["task_id"]
    out = {"status": "", "task": task_id}
    try:
        if deps.claims.settled(task_id):
            out["status"] = "settled"
            return out
    except Exception as exc:  # noqa: BLE001 - a marker is a saving; the doc decides
        out["marker"] = "unknown:" + type(exc).__name__

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
    if not wk.di.ID_OK.fullmatch(drive_revision):
        out["status"] = "no_revision"
        return out
    text = doc.get("text") or ""
    if len(text) > wk.di.MAX_DOC:
        out.update(status="too_large", chars=len(text))
        return out
    current = wk.di.correlate(tuple_["source_doc_id"], text, drive_revision, LAPTOP)
    if current["task_id"] != task_id:
        _settle(deps, task_id)
        out.update(status="superseded", now=current["task_id"])
        return out
    tuple_ = current

    trail = wk.trail_for(text, task_id)
    laptop_states = {state for state, by in trail if by == LAPTOP}
    if laptop_states & set(TERMINAL):
        _settle(deps, task_id)
        out["status"] = "done"
        return out
    other = next((by for _s, by in trail if by not in (wk.INTAKE_WRITER, LAPTOP)), None)
    if other:
        _settle(deps, task_id)
        out.update(status="taken", by=other)
        return out

    # 1. The doorbell, at most once, and only before the laptop has said anything.
    if not laptop_states:
        out["wake"] = _wake(deps, tuple_, now)
    else:
        out["wake"] = "not_needed"

    # 2. ACCEPTED, from the laptop's governed key.
    try:
        accept = parse_accept(deps.claims.lane_value("fleet:worker:accept:" + task_id))
    except Exception as exc:  # noqa: BLE001
        accept = None
        out["accept"] = "unknown:" + type(exc).__name__
    if accept and wk.ACCEPTED not in laptop_states:
        since, lease = accept
        detail = ("accepted at %s%s - recorded by %s from fleet:worker:accept (gov:audit)"
                  % (_iso(since), (" - lease %ds" % lease) if lease else "", RECORDER))
        out["accept"] = wk.write_receipt(deps, tuple_, text, wk.ACCEPTED, deps.at, detail, by=LAPTOP)
        text = wk.di.with_heading(text)
    elif accept:
        out["accept"] = "written"
    else:
        out.setdefault("accept", "none")

    # 3. The terminal line, from the laptop's governed key. Read back, then settled.
    try:
        raw_done = deps.claims.lane_value("fleet:worker:done:" + task_id)
    except Exception as exc:  # noqa: BLE001
        out.update(status="waiting", done="unknown:" + type(exc).__name__)
        return out
    done = parse_done(raw_done) if raw_done else None
    if raw_done and done is None:
        out.update(status="waiting", done="refused:shape")
        return out
    if done is None:
        out.update(status="waiting", done="none")
        return out
    out["done"] = wk.write_receipt(deps, tuple_, text, done["state"], deps.at, done_detail(done), by=LAPTOP)
    if out["done"] in ("written", "no_receipt_writer"):
        _settle(deps, task_id)
        out["status"] = done["state"].lower()
    else:
        out["status"] = "terminal_receipt_owed"
    return out


def _wake(deps, tuple_, now: float) -> str:
    """At most one row per task. The marker is claimed before the post; a post that did not read
    back is retried once after WAKE_RETRY_AFTER, then left as 'failed' with the task named."""
    task_id = tuple_["task_id"]
    try:
        marker = deps.claims.woke(task_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable marker is not a reason to post twice
        return "unknown:" + type(exc).__name__
    if marker == "rung":
        return "rung"
    if marker:
        phase, _sep, stamp = str(marker).partition(":")
        try:
            since = float(stamp)
        except ValueError:
            since = now
        if phase == "failed" or now - since < WAKE_RETRY_AFTER:
            return phase if phase == "failed" else "posting"
        # One retry, an hour on, and the marker says 'failed' from here whatever happens:
        # a third row for one task is never posted.
        if not deps.claims.advance_woke(task_id, marker, "failed:%d" % int(now)):
            return "posting"
        return _post(deps, tuple_, final=True)
    if deps.board is None:
        return "no_board"                          # nothing claimed: a board arriving later rings it
    if not deps.claims.claim_woke(task_id, "posting:%d" % int(now)):
        return "posting"
    return _post(deps, tuple_, final=False)


def _post(deps, tuple_, final: bool) -> str:
    if deps.board is None:
        return "no_board"
    try:
        deps.board(wake_row(tuple_))
    except Exception as exc:  # noqa: BLE001 - the row may or may not be on the board
        return ("failed:" if final else "unconfirmed:") + type(exc).__name__
    deps.claims.set_woke(tuple_["task_id"], "rung")
    return "rung"


def _settle(deps, task_id):
    try:
        deps.claims.mark_settled(task_id)
    except Exception:  # noqa: BLE001
        pass


def _iso(epoch) -> str:
    from datetime import datetime, timezone  # noqa: PLC0415
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

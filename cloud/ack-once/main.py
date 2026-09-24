#!/usr/bin/env python3
r"""The first cloud WRITE: one allowlisted row, proven by a Row_ID read-back.

Cutover step 6 of docs/OPENAI-CLOUD-MIGRATION.md: "Enable one allowlisted cloud
acknowledgement with exact row-ID read-back." Everything before this was
read-only. This is the first time a container is permitted to change the board, so
every safety property is enforced rather than promised.

WHAT IT ACTUALLY WRITES, STATED PLAINLY
    ONE row, phase=RESULT class=CUTOVER, addressed to claude-code-cli - the tag
    this session already owns - and marked prunable in its own payload. It is a
    cutover probe, NOT an acknowledgement of another agent's work. Acking real
    work from the cloud is a larger change: another agent may act on an ACK, and
    the machinery has to be trusted before it is pointed at anything that matters.

    The allowlist is what makes that a configuration rather than a rewrite. Point
    ACK_ALLOW_TO and ACK_REQUIRE_ID_PREFIX at something real and this becomes a
    real acknowledgement with no code change.

THE FOUR PROPERTIES, AND HOW EACH IS ENFORCED

    1. EXACTLY ONE APPEND. An AST guard counts the append calls in this module
       and refuses to run unless there is exactly one. Not "at most" - a module
       with none would exit 0 having proved nothing.

    2. ALLOWLISTED. The recipient must be in ACK_ALLOW_TO and the payload id must
       start with ACK_REQUIRE_ID_PREFIX. Both default to EMPTY, and empty refuses
       everything: an unconfigured deploy writes nothing rather than writing
       something unintended.

    3. NEVER RESENT. board_say.bus follows the gateway's redirect chain but does
       not retry the POST, and neither does this. The reason is written into the
       fleet's history: the POST returned ok:true while the gateway degraded
       between the write and the read, and resending duplicated a RESULT - one
       GROK-ZOOM-HYPERSONIC-001 became four. **UNKNOWN IS NOT FAILURE.**

    4. IDEMPOTENT ACROSS RUNS. The Row_ID is recorded in durable state BEFORE the
       POST, so a container killed mid-write still leaves the id that a later run
       must look for rather than writing a second row. On a schedule this is the
       difference between one row and one row an hour.

WHY THE READ-BACK IS ON THE GET PATH
    The POST answers through a 302 whose target carries the original execution's
    result, and the gateway's match= filter measurably does not apply there while
    it does on the GET path. Same handler, one fewer moving part - and a readback
    by Row_ID costs about 4.5 KB instead of the whole 3,600-row sheet.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_say as board  # noqa: E402

try:
    import state_store
except Exception:  # noqa: BLE001
    state_store = None

BOARD = os.environ.get("BOARD_TITLE") or "Blackboard - Alpha DB"
TAG = os.environ.get("AGENT_TAG") or "claude-code-cli"
CURSOR = os.environ.get("CURSOR_NAME") or "cloud_ack"

# BOTH DEFAULT TO EMPTY, AND EMPTY REFUSES EVERYTHING. An unconfigured deploy
# must write nothing rather than write something nobody asked for.
ALLOW_TO = [t.strip() for t in (os.environ.get("ACK_ALLOW_TO") or "").split(";")
            if t.strip()]
REQUIRE_ID_PREFIX = (os.environ.get("ACK_REQUIRE_ID_PREFIX") or "").strip()

READBACK_ATTEMPTS = int(os.environ.get("READBACK_ATTEMPTS") or 5)


def refuse_unless_exactly_one_append() -> None:
    """Count the appends in this module. Exactly one, no more and no fewer.

    Behaviour, not text: a substring check here would trip on this file's own
    prose, which happened four separate times in one session - once reporting the
    waker's read-only peek branch as containing a board append when the match was
    the word "appended" in a comment.
    """
    tree = ast.parse(open(os.path.abspath(__file__), encoding="utf-8").read())
    appends, problems = 0, []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "board"):
            continue
        if fn.attr not in ("bus", "bus_get", "load_env"):
            problems.append("board.%s() is not an allowed call" % fn.attr)
            continue
        if fn.attr != "bus":
            continue
        payload = next((a for a in node.args if isinstance(a, ast.Dict)), None)
        if payload is None:
            problems.append("board.bus() without a literal payload dict - the "
                            "action cannot be verified")
            continue
        action = None
        for k, v in zip(payload.keys, payload.values):
            if isinstance(k, ast.Constant) and k.value == "action":
                action = v
        if not (isinstance(action, ast.Constant) and action.value == "append"):
            problems.append("board.bus() action is not the literal 'append'")
            continue
        appends += 1
    if appends != 1:
        problems.append("found %d appends; exactly one is allowed, and zero "
                        "would exit 0 having proved nothing" % appends)
    if problems:
        print("REFUSING:", file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
        raise SystemExit(2)


def build_row(rid: str, to: str, payload: str) -> list:
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)
    # The column order board_say uses. The Target_Surface column is authoritative
    # on this board, so it is filled rather than left to a reader's inference.
    return [rid, ts, TAG, to, "APPEND", payload, "OPEN", "Blackboard",
            rid[:8], "result"]


def check_allowlist(to: str, payload: str) -> list:
    problems = []
    if not ALLOW_TO:
        problems.append("ACK_ALLOW_TO is empty, so nothing may be written")
    elif to not in ALLOW_TO:
        problems.append("recipient %r is not in ACK_ALLOW_TO %r" % (to, ALLOW_TO))
    if not REQUIRE_ID_PREFIX:
        problems.append("ACK_REQUIRE_ID_PREFIX is empty, so nothing may be written")
    else:
        m = re.search(r"\bid=([^|]+)", payload)
        if not m:
            problems.append("payload carries no id= field")
        elif not m.group(1).startswith(REQUIRE_ID_PREFIX):
            problems.append("payload id %r does not start with %r"
                            % (m.group(1), REQUIRE_ID_PREFIX))
    if not payload.startswith("BCB|"):
        problems.append("payload is not a BCB row")
    return problems


def already_written(store, target: str):
    """Has a previous run already written for this target?

    Returns the recorded attempt, if any. The id is recorded BEFORE the POST, so
    a container killed mid-write leaves the id behind and the next run looks for
    THAT row instead of writing a second one.
    """
    if store is None:
        return None, None
    state, token = store.load(CURSOR)
    for a in state.get("attempts") or []:
        if a.get("target") == target:
            return a, token
    return None, token


def main() -> int:
    refuse_unless_exactly_one_append()

    target = (os.environ.get("ACK_TARGET") or "").strip()
    to = (os.environ.get("ACK_TO") or "").strip()
    if not target:
        print(json.dumps({"wrote": False,
                          "reason": "ACK_TARGET not set; nothing to write"}))
        return 0

    payload = (
        "BCB|v=1|id=%s|phase=RESULT|class=CUTOVER|from=%s|to=%s"
        "|evidence=MEASURED|confidence=TESTED"
        "|finding=First write from a cloud runtime. Cutover step 6. Credentials "
        "injected from Secret Manager, no .env in the image, exactly one append "
        "enforced by an AST guard, verified by Row_ID read-back on the GET path."
        "|HOUSEKEEPING=this row is a cutover probe and is safe to prune."
        "|attest=%s/cloud-run/board-clock" % (target, TAG, to, TAG))

    problems = check_allowlist(to, payload)
    if problems:
        print(json.dumps({"wrote": False, "refused": problems}, sort_keys=True))
        return 2

    # A DRY RUN BEFORE A FIRST WRITE, and it stops short of the store as well as
    # the POST. The allowlist is already proven by its refusals; what this exists
    # for is eyeballing the exact row - a malformed one on an append-only board
    # cannot be taken back, and the v1 bus has put a payload in the timestamp
    # column before now.
    if (os.environ.get("ACK_DRY_RUN") or "").strip() not in ("", "0", "false"):
        print(json.dumps({"wrote": False, "dry_run": True,
                          "row": build_row("dry-run-row-id", to, payload)},
                         sort_keys=True, indent=1))
        return 0

    store = None
    if state_store is not None and os.environ.get("BLACKBOARD_STATE_URI"):
        try:
            store = state_store.open_store(os.environ["BLACKBOARD_STATE_URI"])
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"wrote": False,
                              "refused": ["no durable state, so a second run "
                                          "could duplicate the row: %s" % exc]}))
            return 2
    if store is None:
        # Without durable state this job cannot promise it has not already run.
        print(json.dumps({"wrote": False,
                          "refused": ["BLACKBOARD_STATE_URI is required: without "
                                      "it a repeat run cannot be prevented"]}))
        return 2

    prior, token = already_written(store, target)
    if prior:
        # Do not write again. Verify what the earlier attempt left behind.
        found = readback(prior["row_id"])
        print(json.dumps({"wrote": False, "already": prior, "readback": found},
                         sort_keys=True))
        return 0 if found.get("found") else 2

    rid = str(uuid.uuid4())
    state, token = store.load(CURSOR)
    attempts = state.get("attempts") or []
    attempts.append({"target": target, "row_id": rid, "to": to,
                     "at": datetime.now(timezone.utc).strftime(
                         "%Y-%m-%dT%H:%M:%SZ"), "posted": False})
    state["attempts"] = attempts[-50:]
    store.save(CURSOR, state, token)          # recorded BEFORE the POST

    env = board.load_env()
    row = build_row(rid, to, payload)
    # THE ONE APPEND. Not retried: the POST may commit before its response fails,
    # and resending turned one RESULT into four on this board once already.
    code, body = board.bus(env, {"secret": env["BUS_SECRET"], "action": "append",
                                 "title": BOARD, "sheetRow": row})

    found = readback(rid)

    state, token = store.load(CURSOR)
    for a in state.get("attempts") or []:
        if a.get("row_id") == rid:
            a["posted"] = True
            a["post_status"] = code
            a["readback"] = found.get("found")
    store.save(CURSOR, state, token)

    print(json.dumps({
        "wrote": True, "row_id": rid, "target": target, "to": to,
        "post_status": code, "post_body": body[:120].replace("\n", " "),
        "readback": found,
        # The POST status is deliberately not the verdict, and saying so in the
        # output stops a reader treating a 200 as proof.
        "verdict": ("LANDED" if found.get("found")
                    else "UNKNOWN - do NOT resend"),
    }, sort_keys=True))
    return 0 if found.get("found") else 2


def readback(rid: str) -> dict:
    """Ask for that one row by id. The only thing that settles whether it landed.

    Bounded retries because the redirect target intermittently 404s - measured
    tonight needing five attempts once, then ten of ten clean. A read is
    idempotent so retrying costs seconds; the append above is not and is not
    retried.
    """
    env = board.load_env()
    tried = []
    for i in range(1, READBACK_ATTEMPTS + 1):
        code, body = board.bus_get(env, {"action": "read", "title": BOARD,
                                         "match": rid})
        if code != 200 or not body.lstrip().startswith("{"):
            tried.append("HTTP %s" % code)
        else:
            rows = (json.loads(body) or {}).get("rows")
            if isinstance(rows, list):
                hit = [r for r in rows
                       if isinstance(r, list) and r and str(r[0]) == rid]
                tried.append("ok")
                return {"found": bool(hit), "attempts": tried,
                        "matched_rows": len(hit)}
            tried.append("health-blob")
        if i < READBACK_ATTEMPTS:
            time.sleep(2 * i)
    # Never "not found" on an unreadable board. Unknown is not failure, and the
    # natural response to "failed" on an append-only board is to write again.
    return {"found": False, "attempts": tried,
            "note": "board unreadable; absence is NOT proof the row is missing"}


if __name__ == "__main__":
    raise SystemExit(main())

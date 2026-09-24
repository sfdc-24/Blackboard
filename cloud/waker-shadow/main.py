#!/usr/bin/env python3
r"""Run the waker's decision in the cloud and write nothing. Shadow mode.

Cutover step 5 of docs/OPENAI-CLOUD-MIGRATION.md: "Run cloud and laptop in shadow
mode with cloud writes disabled." The question it answers is narrow and it is the
only one worth asking before a cutover: **given the same board and the same
cursor, does the cloud reach the same verdict the laptop reaches?**

WRITES ARE DISABLED BY ABSENCE OF CAPABILITY, NOT BY A FLAG
    A flag is a promise. This job is deployed with ONLY BUS_URL and BUS_SECRET
    injected - no META_TOKEN, so it cannot send WhatsApp even if something asked
    it to; no GH_TOKEN, so it cannot touch a repository. The two functions it
    calls, check_board and check_whatsapp, read the board and return verdicts.

    On top of that, an AST guard refuses to run if this module calls anything on
    the waker other than those two. Between them: it cannot write because it holds
    no credential that writes, and it cannot reach a write path because the guard
    forbids naming one.

WHY IT CALLS THE TWO CHECKS RATHER THAN main(--peek)
    --peek is genuinely write-free - verified by reading its branch, where the
    only occurrence of "append" is the word in a comment about a row arriving
    mid-read. But it also prints for a PowerShell launcher to grep, decides an
    exit code that launcher acts on, and may grow more of both. Calling the two
    checks directly means this job's behaviour cannot drift with the launcher's
    contract, and the guard can state exactly what is allowed.

THE VERDICT LOGIC IS COPIED DELIBERATELY, AND THAT IS A KNOWN COST
    NEWS-first, then UNKNOWN, matching what #186 established: a message from him
    outranks a failed board read, because a board UNKNOWN swallowing his WhatsApp
    is what left seven of his messages unanswered. It is duplicated here rather
    than imported because it lives inline in main(). The comparison this job
    exists for is what would catch the two copies drifting - if the verdicts stop
    matching, that is the finding, not a nuisance.

SAME INPUT OR THE COMPARISON MEANS NOTHING
    Both sides take their cursor from SHADOW_SINCE. Run with different cursors,
    two verdicts differ for a reason that has nothing to do with where they ran -
    the same trap the board probe's COMPARED_THROUGH exists to avoid.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_waker as waker  # noqa: E402

try:
    import state_store
except Exception:  # noqa: BLE001
    state_store = None

# The only waker functions this job may touch. Everything that writes - to the
# board, to WhatsApp, to state, to a digest - is absent from this set, and the
# guard below enforces that rather than trusting it.
ALLOWED = {"check_board", "check_whatsapp"}

# Named explicitly so the guard's failure message can say WHICH write path was
# reached, instead of only that something was not allowed.
WRITE_PATHS = {"ack_whatsapp", "post_alarm", "save_state", "bus_post",
               "write_digest", "post", "main"}

CURSOR = os.environ.get("CURSOR_NAME") or "waker_shadow"


def refuse_if_this_module_can_write() -> None:
    """Behaviour, not text. A grep here would trip on this file's own prose.

    Four times today a substring check stood in for a question about behaviour
    and gave the wrong answer - including one that reported the waker's peek
    branch as containing a board append when the match was the word "appended"
    in a comment. So: walk the tree and judge the calls.
    """
    tree = ast.parse(open(os.path.abspath(__file__), encoding="utf-8").read())
    problems, checks = [], 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "waker"):
            continue
        if fn.attr in WRITE_PATHS:
            problems.append("waker.%s() is a WRITE PATH" % fn.attr)
        elif fn.attr not in ALLOWED:
            problems.append("waker.%s() is not in the allowed set" % fn.attr)
        else:
            checks += 1
    if checks == 0:
        problems.append("no waker check is called, so a green run would prove "
                        "nothing")
    if problems:
        print("REFUSING, this shadow job is not read-only:", file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
        raise SystemExit(2)


def verdict() -> dict:
    since = (os.environ.get("SHADOW_SINCE") or "").strip()
    # The shape check_board and check_whatsapp expect. wa_watermark is set
    # explicitly rather than left absent, because an absent key inherits the
    # general board boundary and a present-but-empty one means "from the
    # beginning" - a distinction board_waker documents and this must not blur.
    state = {"watermark": since, "wa_watermark": since}

    board_status, board_note, fresh = waker.check_board(state)
    wa_status, wa_note, wa_fresh = waker.check_whatsapp(state)

    # NEWS first. See #186 - his message outranks a failed board read.
    if "NEWS" in (board_status, wa_status):
        combined = "NEWS"
    elif "UNKNOWN" in (board_status, wa_status):
        combined = "UNKNOWN"
    else:
        combined = "QUIET"

    return {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "where": "cloud-run" if os.environ.get("CLOUD_RUN_JOB") else "local",
        "since": since or None,
        "board": board_status,
        "board_note": board_note,
        "board_fresh": len(fresh),
        "whatsapp": wa_status,
        "whatsapp_note": wa_note,
        "whatsapp_fresh": len(wa_fresh),
        "combined": combined,
        # The exit code the laptop's launcher would act on. Comparing the VERDICT
        # is comparing what would actually have happened.
        "would_exit": 10 if combined == "NEWS" else (2 if combined == "UNKNOWN"
                                                    else 0),
        "would_wake": combined == "NEWS",
        "credential_source": "injected" if (os.environ.get("BUS_URL")
                                            and os.environ.get("BUS_SECRET"))
                             else "file",
        # Proof of the capability argument, recorded in the output so a reviewer
        # does not have to go and look at the job's environment.
        "can_send_whatsapp": bool(os.environ.get("META_TOKEN")),
        "can_reach_github": bool(os.environ.get("GH_TOKEN")),
    }


def record(v: dict) -> dict:
    """Keep the shadow's own cursor, never the live waker's.

    A separate document on purpose. Writing the cursor the live doorbell depends
    on would make this a second writer to it - the collision class the store was
    built to make safe, not one to walk into. This records what the shadow WOULD
    have advanced to; nothing reads it but the next shadow run.
    """
    uri = os.environ.get("BLACKBOARD_STATE_URI") or ""
    if not uri or state_store is None:
        return {"backend": None, "reason": "no state configured"}
    try:
        store = state_store.open_store(uri)
        state, token = store.load(CURSOR)
        history = state.get("runs") or []
        history.append({k: v[k] for k in ("at", "where", "combined",
                                         "would_exit", "board", "whatsapp")})
        state["runs"] = history[-20:]          # bounded; this is evidence, not a log
        state["last"] = v["at"]
        store.save(CURSOR, state, token)
        return {"backend": store.describe(), "runs_recorded": len(state["runs"])}
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop the shadow
        return {"backend": uri, "reason": "%s: %s" % (type(exc).__name__, exc)}


def main() -> int:
    refuse_if_this_module_can_write()
    v = verdict()
    v["cursor"] = record(v)
    print(json.dumps(v, sort_keys=True))
    # ALWAYS 0. This job reports; it does not decide. Returning the waker's own
    # exit code would make a QUIET board look like a failure to Cloud Run and
    # bury the verdict under a red execution.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

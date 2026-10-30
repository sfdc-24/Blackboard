"""Board posting and reading for the claude-code-cli laptop helpers (v5, Oct 30).

Aya's action list (AYA-FINISH-RESUME-20261030T133945Z-CLI) for appends:
  - every external step has ONE stable op id, recorded in the task as INTENT on disk BEFORE its append;
  - an append whose outcome is not known (timeout, error, lost response, crash after INTENT) is UNKNOWN;
  - UNKNOWN resolves ONLY by a positive read of that row from this tag. An empty or unreadable board proves
    nothing, and neither does a later timestamp: an uncertain append may still land;
  - an UNKNOWN append is never retried, unless enforced idempotency is established (the gateway's live
    APPEND_DEDUP readback, owned by the root task). Until then DEDUP_ENFORCED is False and nothing retries.
A reconciler answer counts only FROM bus-reconciler, phase AYA_RESULT, answers= our request id.
The imported Blackboard code is pinned (PINNED, clean scripts/), or importing this module stops.
"""
import os
import subprocess
import sys
import threading
import time

_REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
# Inside the Blackboard checkout (tools/cli_call_helper/) the repo itself is the pinned code; elsewhere, the env.
MAIN = os.environ.get("CLI_HELPER_BLACKBOARD") or _REPO
# The git TREE of scripts/ in the frozen integration (main 0b752b4 + O3 d832f9c + repair 91656b12): a tree id, so
# the helper's own files can sit in the same commit without a circular pin. Set at freeze; empty refuses.
PINNED_SCRIPTS = os.environ.get("CLI_HELPER_PINNED_SCRIPTS", "875a5f920a433d83c12679852ca3adb00d40b0a9")
DEDUP_ENFORCED = False                                     # flip only on the root task's live APPEND_DEDUP readback
APPEND_SECONDS = 90


def _pinned(run=subprocess.run):
    tree = run(["git", "-C", MAIN, "rev-parse", "HEAD:scripts"], capture_output=True, text=True).stdout.strip()
    dirty = run(["git", "-C", MAIN, "status", "--porcelain", "--", "scripts"], capture_output=True, text=True)
    if not PINNED_SCRIPTS or tree != PINNED_SCRIPTS or dirty.returncode != 0 or dirty.stdout.strip():
        raise SystemExit("STOP: %s scripts/ is not the pinned tree %s, clean (it is %s)"
                         % (MAIN, PINNED_SCRIPTS[:12] or "(unset)", tree[:12] or "?"))


if os.environ.get("LAPTOP_POST_NO_PIN") != "1":            # the tests fake every edge and set this
    _pinned()
sys.path.insert(0, os.path.join(MAIN, "scripts"))
import bus_request                  # noqa: E402
import fleet_agent as fa            # noqa: E402

TAG = "claude-code-cli"
RECONCILER = "bus-reconciler"


def board_records():
    """The live board's rows as dicts, or None when it could not be read (fa.read_board validates size)."""
    data = fa.read_board(quiet=True)
    if data is None:
        return None
    return fa.rows_frame(data)[1]


def find_row(rid, tag=TAG, records=None):
    """True/False: a row with this id FROM this tag. None: the board could not be read."""
    records = board_records() if records is None else records
    if records is None:
        return None
    return any(str(r.get("Row_ID", "")) == rid and str(r.get("Source_Tag", "")) == tag for r in records)


def bounded(fn, *args, seconds=APPEND_SECONDS):
    """fn(*args) with a deadline. ('ok', value), ('error', type name) or ('timeout', None). A timed-out call may
    still finish later: that is exactly why its op becomes UNKNOWN and is never retried blind."""
    box = {}

    def run():
        try:
            box["value"] = fn(*args)
        except (Exception, SystemExit) as error:              # noqa: BLE001
            box["error"] = type(error).__name__
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        return "timeout", None
    return ("error", box["error"]) if "error" in box else ("ok", box.get("value"))


def send(txn, ask, op_id, row, append=None, records=None, now=time.time):
    """One stable-id append for task `ask`, inside the caller's lock (txn = call_state.Txn).
    Returns CONFIRMED or UNKNOWN. The INTENT is saved before the append; nothing is ever appended twice
    unless DEDUP_ENFORCED."""
    append = append or fa.append_and_verify
    task = txn.state["tasks"][ask]
    ops = task.setdefault("ops", {})
    op = ops.get(op_id)
    if op and op["state"] == "CONFIRMED":
        return "CONFIRMED"
    records = board_records() if records is None else records
    seen = find_row(op_id, records=records) if records is not None else None
    if seen:
        ops[op_id] = dict(op or {"at": now()}, state="CONFIRMED")
        txn.save()
        return "CONFIRMED"
    if op:                                                 # sent, or maybe sent, before: not on the board yet
        if not DEDUP_ENFORCED or op.get("retried"):
            op["state"] = "UNKNOWN"
            txn.save()
            return "UNKNOWN"
        op["retried"] = True                               # one retry, only under enforced idempotency
    elif seen is None:
        return "UNKNOWN"                                   # cannot read the board: nothing sent, nothing saved
    ops[op_id] = dict(op or {}, state="INTENT", at=(op or {}).get("at", now()))
    txn.save()                                             # on disk BEFORE the append
    outcome, value = bounded(append, op_id, row)
    ops[op_id]["state"] = "CONFIRMED" if (outcome == "ok" and value == 0) else "UNKNOWN"
    ops[op_id]["append"] = outcome if outcome != "ok" else "rc=%s" % value
    txn.save()
    return ops[op_id]["state"]


def resolve(txn, ask, op_id, records=None):
    """An UNKNOWN or INTENT op becomes CONFIRMED on a positive read; otherwise it stays as it is."""
    op = txn.state["tasks"][ask].get("ops", {}).get(op_id)
    if not op or op["state"] == "CONFIRMED":
        return op and op["state"]
    if find_row(op_id, records=records):
        op["state"] = "CONFIRMED"
        txn.save()
    return op["state"]


def post_once(rid, row, append=None, records=None):
    """A row with no task (the call window's rows): appended only when a full board read shows it absent, and
    never again for this process. VERIFIED, FOUND or None (unknown; not retried)."""
    append = append or fa.append_and_verify
    seen = find_row(rid, records=records)
    if seen:
        return "FOUND"
    if seen is None:
        return None
    outcome, value = bounded(append, rid, row)
    return "VERIFIED" if (outcome == "ok" and value == 0) else None


def reconciler_answer(rid, records=None):
    """(status, text) of bus-reconciler's answer to request `rid`, or (None, '') while there is none."""
    records = board_records() if records is None else records
    for r in records or []:
        if str(r.get("Source_Tag", "")) != RECONCILER:
            continue
        payload = str(r.get("Payload", "") or "")
        if bus_request.field(payload, "answers") != rid or bus_request.field(payload, "from") != RECONCILER:
            continue
        if bus_request.field(payload, "phase") != "AYA_RESULT":
            continue
        text = bus_request.field(payload, "text")
        head = text.split(None, 1)[0].rstrip(".") if text else ""
        return (head if head in ("OK", "REFUSED") else "ERROR"), text
    return None, ""


def wait_answers(rids, timeout=150, every=8, sleep=time.sleep, clock=time.time):
    """{rid: (status, text)} for requests sent together: one board read per round serves all of them."""
    got, end = {}, clock() + timeout
    while clock() < end and len(got) < len(rids):
        records = board_records()
        if records is not None:
            for rid in rids:
                if rid not in got:
                    status, text = reconciler_answer(rid, records)
                    if status:
                        got[rid] = (status, text)
        if len(got) < len(rids):
            sleep(every)
    return {rid: got.get(rid, (None, "")) for rid in rids}

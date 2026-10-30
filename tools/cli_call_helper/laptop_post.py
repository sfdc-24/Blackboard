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
import hashlib
import os
import subprocess
import sys
import threading
import time

_REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
# Inside the Blackboard checkout (tools/cli_call_helper/) the repo itself is the pinned code; elsewhere, the env.
MAIN = os.environ.get("CLI_HELPER_BLACKBOARD") or _REPO
# The git TREE of scripts/ in the frozen integration (main 0b752b4 + O3 d832f9c + repair 91656b12 + bridge 6a/6b
# ea8fe6b): a tree id, so the helper's own files can sit in the same commit without a circular pin. Empty refuses.
PINNED_SCRIPTS = os.environ.get("CLI_HELPER_PINNED_SCRIPTS", "7a89d16d7ca8b439d8eaaa8deafe01d3b3c7b8ea")
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


def payload_sha(row):
    """sha256 of the exact payload cell (column F) a row carries."""
    return hashlib.sha256(str(row[5]).encode("utf-8")).hexdigest()


def find_row(rid, tag=TAG, records=None, sha=None):
    """True/False: a row with this id FROM this tag (and, given `sha`, this exact payload). "CONFLICT": the id and tag
    are there with another payload (Aya 8570 item 5: id and tag alone are not a readback). None: board unreadable."""
    records = board_records() if records is None else records
    if records is None:
        return None
    hits = [r for r in records if str(r.get("Row_ID", "")) == rid and str(r.get("Source_Tag", "")) == tag]
    if not hits:
        return False
    if sha is None:
        return True
    same = [r for r in hits if hashlib.sha256(str(r.get("Payload", "")).encode("utf-8")).hexdigest() == sha]
    return True if same else "CONFLICT"


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


def send(txn, ask, op_id, row, append=None, records=None, now=time.time, reread=None):
    """One stable-id append for task `ask`, inside the caller's lock (txn = call_state.Txn).
    Returns CONFIRMED, UNKNOWN or CONFLICT. The INTENT, with the exact row and its payload sha256, is saved before the
    append; a resume uses THAT row, never a rebuilt one. CONFIRMED needs a fresh read showing this id, tag and payload
    (append_and_verify's own check reads the id only). Nothing is appended twice unless DEDUP_ENFORCED."""
    append = append or fa.append_and_verify
    reread = reread or board_records
    task = txn.state["tasks"][ask]
    ops = task.setdefault("ops", {})
    op = ops.get(op_id)
    if op and op["state"] in ("CONFIRMED", "CONFLICT"):
        return op["state"]
    if op and op.get("row"):
        row = op["row"]                                    # the row as first intended, byte for byte
    sha = (op or {}).get("sha") or payload_sha(row)
    records = board_records() if records is None else records
    seen = find_row(op_id, records=records, sha=sha) if records is not None else None
    if seen in (True, "CONFLICT"):
        ops[op_id] = dict(op or {"at": now(), "row": list(row), "sha": sha},
                          state="CONFIRMED" if seen is True else "CONFLICT")
        txn.save()
        return ops[op_id]["state"]
    if op:                                                 # sent, or maybe sent, before: not on the board yet
        if not DEDUP_ENFORCED or op.get("retried"):
            op["state"] = "UNKNOWN"
            txn.save()
            return "UNKNOWN"
        op["retried"] = True                               # one retry, only under enforced idempotency
    elif seen is None:
        return "UNKNOWN"                                   # cannot read the board: nothing sent, nothing saved
    ops[op_id] = dict(op or {}, state="INTENT", at=(op or {}).get("at", now()), row=list(row), sha=sha)
    txn.save()                                             # on disk BEFORE the append
    outcome, value = bounded(append, op_id, row)
    ops[op_id]["append"] = outcome if outcome != "ok" else "rc=%s" % value
    seen = find_row(op_id, records=reread(), sha=sha)
    ops[op_id]["state"] = "CONFIRMED" if seen is True else ("CONFLICT" if seen == "CONFLICT" else "UNKNOWN")
    txn.save()
    return ops[op_id]["state"]


def resolve(txn, ask, op_id, records=None):
    """An UNKNOWN or INTENT op becomes CONFIRMED on a positive read of its exact payload (or CONFLICT)."""
    op = txn.state["tasks"][ask].get("ops", {}).get(op_id)
    if not op or op["state"] in ("CONFIRMED", "CONFLICT"):
        return op and op["state"]
    seen = find_row(op_id, records=records, sha=op.get("sha"))
    if seen in (True, "CONFLICT"):
        op["state"] = "CONFIRMED" if seen is True else "CONFLICT"
        txn.save()
    return op["state"]


def post_once(rid, row, append=None, records=None):
    """A row with no task (the call window's rows): appended only when a full board read shows it absent, and
    never again for this process. VERIFIED, FOUND or None (unknown; not retried)."""
    append = append or fa.append_and_verify
    sha = payload_sha(row)
    seen = find_row(rid, records=records, sha=sha)
    if seen is True:
        return "FOUND"
    if seen is None or seen == "CONFLICT":
        return None
    bounded(append, rid, row)
    return "VERIFIED" if find_row(rid, records=board_records(), sha=sha) is True else None


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

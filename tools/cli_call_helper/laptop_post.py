"""Board posting and reading for the claude-code-cli laptop helpers (v5, Oct 30).

Aya's action list (AYA-FINISH-RESUME-20261030T133945Z-CLI) for appends:
  - every external step has ONE stable op id, recorded in the task as INTENT on disk BEFORE its append;
  - an append whose outcome is not known (timeout, error, lost response, crash after INTENT) is UNKNOWN;
  - UNKNOWN resolves ONLY by a positive read of that row from this tag. An empty or unreadable board proves
    nothing, and neither does a later timestamp: an uncertain append may still land;
  - an UNKNOWN append is never retried, unless enforced idempotency is established (the gateway's live
    APPEND_DEDUP readback, owned by the root task). Until then DEDUP_ENFORCED is False and nothing retries.
A protected reconciler answer additionally needs the hardened gateway-owned verified sender stamp,
phase AYA_RESULT, and a fresh unpredictable v6 request correlation. Source_Tag alone is not identity.
The imported Blackboard code is pinned (PINNED, clean scripts/), or importing this module stops.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

_REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
# Inside the Blackboard checkout (tools/cli_call_helper/) the repo itself is the pinned code; elsewhere, the env.
MAIN = os.environ.get("CLI_HELPER_BLACKBOARD") or _REPO
# The git TREE of scripts/ in the frozen integration (main 0b752b4 + O3 d832f9c + repair 91656b12): a tree id, so
# the helper's own files can sit in the same commit without a circular pin. Set at freeze; empty refuses.
PINNED_SCRIPTS = os.environ.get("CLI_HELPER_PINNED_SCRIPTS", "8a97cd5c420c66bafb307d8c65a17d1900ab8e24")
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
RECONCILER_STAMP = "sender=verified;who=bus-reconciler@sfdc24.iam.gserviceaccount.com"
# v6 request nonces cannot reuse a predictable historical row id. The hardened
# gateway's exact deployed identity is a separate activation gate; a legacy
# caller-supplied column L must never be accepted as this protocol's receipt.
REQUEST_ID = re.compile(r"^CLI6-[A-Z0-9]{1,12}-[0-9a-f]{32}$")
_COLUMNS = ("Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type", "Payload",
            "Category", "Project Tag", "Gist", "Subgist")
_POSTS = {}
_POST_LOCK = threading.Lock()


def new_request_id(kind):
    """Fresh, unpredictable correlation for an authenticated protected receipt."""
    name = re.sub(r"[^A-Z0-9]", "", str(kind).upper())[:12]
    if not name:
        raise ValueError("request kind required")
    return "CLI6-%s-%s" % (name, os.urandom(16).hex())


def records_from_board(data):
    """Read fixed protocol columns, including gateway-owned L, not mutable header names.

    Historical boards have blank/duplicate trailing headers. In particular a
    caller's Payload or an arbitrary column named Sender_Check is NOT column L.
    """
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list) or not data["rows"]:
        return None
    result = []
    for raw in data["rows"][1:]:
        if not isinstance(raw, (list, tuple)) or len(raw) < 10:
            continue
        rec = {name: raw[i] for i, name in enumerate(_COLUMNS)}
        rec["_sender_check"] = str(raw[11]) if len(raw) > 11 else ""
        result.append(rec)
    return result


def board_records():
    """Rows from the real board adapter, preserving the server-owned sender stamp."""
    data = fa.read_board(quiet=True)
    return records_from_board(data) if data is not None else None


def _records(source):
    return board_records() if source is None else (source() if callable(source) else source)


def row_identity(row):
    """All intended cells except the transport timestamp; routing and payload are exact."""
    if not isinstance(row, (list, tuple)) or len(row) != 10:
        raise ValueError("expected exactly ten intended board cells")
    return [str(row[i] or "") for i in range(10) if i != 1]


def row_digest(row):
    return hashlib.sha256(json.dumps(row_identity(row), ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _matches(record, row):
    return all(str(record.get(_COLUMNS[i], "") or "") == str(row[i] or "")
               for i in range(10) if i != 1)


def find_row(rid, tag=TAG, records=None, row=None):
    """Exact positive readback only. None means unreadable, conflicting, duplicate or unbound.

    False means the id is absent from the supplied snapshot, never that an unknown
    in-flight append cannot arrive later. No tag/id-only proof remains.
    """
    records = _records(records)
    if records is None or row is None or row[0] != rid or row[2] != tag:
        return None
    hits = [r for r in records if str(r.get("Row_ID", "")) == rid]
    if not hits:
        return False
    return True if len(hits) == 1 and _matches(hits[0], row) else None


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
    """Persist exact INTENT before one append; CONFIRMED needs exact content readback.

    The append adapter's rc=0 checks only Row_ID and is not proof. Existing INTENT
    remains uncertain forever unless a positive exact read settles it; no retries
    are enabled here. Regenerated timestamps never replace the stored intended row.
    """
    append = append or fa.append_and_verify
    ops = txn.state["tasks"][ask].setdefault("ops", {})
    op = ops.get(op_id)
    wanted = row_digest(row)
    if row[0] != op_id or row[2] != TAG:
        raise ValueError("operation row identity mismatch")
    if op:
        if not op.get("row") or op.get("row_hash") != wanted or row_digest(op["row"]) != wanted:
            op.update(state="UNKNOWN", reason="missing or conflicting intended content")
            txn.save()
            return "UNKNOWN"
        row = op["row"]
        # A prior exact receipt is durable local evidence, but old v5 CONFIRMED
        # records without a bound row above are deliberately not grandfathered.
        if op["state"] == "CONFIRMED":
            return "CONFIRMED"
    seen = find_row(op_id, records=records, row=row)
    if seen is True:
        ops[op_id] = dict(op or {"at": now()}, state="CONFIRMED", row=list(row), row_hash=wanted)
        txn.save()
        return "CONFIRMED"
    if op:
        if not DEDUP_ENFORCED or op.get("retried") or seen is None:
            op["state"] = "UNKNOWN"
            txn.save()
            return "UNKNOWN"
        op["retried"] = True
    elif seen is None:
        return "UNKNOWN"
    ops[op_id] = dict(op or {}, state="INTENT", at=(op or {}).get("at", now()),
                      row=list(row), row_hash=wanted)
    txn.save()
    outcome, value = bounded(append, op_id, row)
    # Re-read through the actual adapter even when append returned zero or raised.
    seen = find_row(op_id, records=records, row=row)
    ops[op_id]["state"] = "CONFIRMED" if seen is True else "UNKNOWN"
    ops[op_id]["append"] = outcome if outcome != "ok" else "rc=%s" % value
    txn.save()
    return ops[op_id]["state"]


def resolve(txn, ask, op_id, records=None):
    """Reconcile only the persisted exact intended row, never an id/tag collision."""
    op = txn.state["tasks"][ask].get("ops", {}).get(op_id)
    if not op:
        return None
    row = op.get("row")
    if not row or op.get("row_hash") != row_digest(row):
        op.update(state="UNKNOWN", reason="unbound legacy operation")
        txn.save()
        return "UNKNOWN"
    if op["state"] == "CONFIRMED":
        return "CONFIRMED"
    if find_row(op_id, records=records, row=row) is True:
        op["state"] = "CONFIRMED"
        txn.save()
    return op["state"]


def post_once(rid, row, append=None, records=None):
    """Uncertain non-task reads are never replayed in this process.

    Every new read uses new_request_id(), so a process restart creates a new
    correlation rather than trusting an old pre-hardening receipt. Task mutations
    instead use send() and persist their exact request id and content.
    """
    append = append or fa.append_and_verify
    wanted = row_digest(row)
    with _POST_LOCK:
        old = _POSTS.get(rid)
        if old and old["hash"] != wanted:
            return None
        if old:
            row = old["row"]
        seen = find_row(rid, records=records, row=row)
        if seen is True:
            return "FOUND"
        if old or seen is None:
            return None
        _POSTS[rid] = {"row": list(row), "hash": wanted}
        bounded(append, rid, row)
        return "VERIFIED" if find_row(rid, records=records, row=row) is True else None


def reconciler_answer(rid, records=None):
    """A fresh v6 receipt authenticated as the reconciler, not a claimed Source_Tag.

    The gateway must be the pinned hardened version that overwrites L in every
    mode. Identity here proves the response producer, NOT the requesting CLI's
    identity, task authorization, exact-source digest, or lifecycle revision; the
    caller must validate that response's complete protocol content as well.
    """
    if not REQUEST_ID.fullmatch(str(rid)):
        return None, ""
    records = _records(records)
    qualified = []
    for r in records or []:
        if (str(r.get("Source_Tag", "")) != RECONCILER
                or r.get("_sender_check") != RECONCILER_STAMP
                or str(r.get("Action_Type", "")) != "AYA_RESULT"):
            continue
        payload = str(r.get("Payload", "") or "")
        if (bus_request.repeated_keys(payload)
                or bus_request.field(payload, "answers") != rid
                or bus_request.field(payload, "from") != RECONCILER
                or bus_request.field(payload, "phase") != "AYA_RESULT"):
            continue
        qualified.append(payload)
    # Ambiguous/multiple receipts are not quietly first-one-wins. This also keeps
    # duplicate committed effects visible even if their payload happens to match.
    if len(qualified) != 1:
        return None, ""
    text = bus_request.field(qualified[0], "text")
    head = text.split(None, 1)[0].rstrip(".:") if text else ""
    return (head if head in ("OK", "REFUSED") else "ERROR"), text


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

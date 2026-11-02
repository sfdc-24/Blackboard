"""Source-bound call helper v6. No owner task text crosses the shared board.

  call_ask.py accept <ask> --input-file <private-utf8-file> --expect mermaid
  call_ask.py done <ask> <public-card.json>
  call_ask.py migrate [--legacy PATH ...]

The input file must contain the COMPLETE exact UTF-8 owner task: no newline,
pipe, Unicode or whitespace normalization. Source inputs >=1000 characters are
refused upstream because the pinned chair truncates there without a marker.
The file is compared to a freshly correlated, gateway-verified reconciler receipt
binding source epoch/event/revision/hash/byte length. The public cue and inbox
preview convey no authority. Input availability is a PRIVATE/manual runtime gate;
this does not claim automatic full-text retrieval or owner/browser acceptance.

CARD and COMPLETE are real conditional lifecycle controller operations, not
status-check-then-unconditional-HSET/RESULT. Unknown effects are persisted and
reconciled by their exact operation IDs. Cancellation/expiry is terminal in the
authoritative lifecycle, but public artifact cleanup remains CANCELLING: an old
external upload may still be in flight. No absence snapshot proves erasure.
"""
from __future__ import annotations
import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import call_state as cs
import laptop_post as lp
import board_row
import canvas_publish
import call_bridge
import call_lifecycle as life
import wake_ack

TAG = "claude-code-cli"
ASK = life.TASK_ID
CANVAS = "https://storage.googleapis.com/sfdc24-canvas/rooms/workshop.json"
MAX_AGE, PUBLISH_WAIT, ANSWER_WAIT = 7200, 90, 120


def utc():
    return dt.datetime.now(dt.timezone.utc)


def canonical(item):
    return life.digest(item)


def sanitizer():
    out = subprocess.run(["git", "-C", lp.MAIN, "rev-parse", "HEAD:scripts/canvas_publish.py"], capture_output=True, text=True)
    if out.returncode:
        raise ValueError("sanitizer identity unavailable")
    return out.stdout.strip()


def board_cells(rid, action, payload, gist, to="bus-reconciler"):
    return [rid, utc().strftime("%Y-%m-%dT%H:%M:%S.%fZ"), TAG, to, action, board_row.guard(payload), "OPEN", "Conference", gist, ""]


def task_row(ask, op, supplied=None, rid=None):
    """Actual production bus_request call-task shape. No source input text."""
    if not ASK.fullmatch(ask) or op not in ("status", "card", "complete"):
        raise ValueError("invalid task request")
    rid = rid or lp.new_request_id(op)
    parts = ["BCB", "v=1", "id="+rid, "req="+rid, "do=call-task", "op="+op, "key="+ask,
             "from="+TAG, "to=bus-reconciler"]
    if supplied:
        parts += ["enc=b64", "val="+base64.b64encode(life.dump(supplied).encode("utf-8")).decode("ascii")]
    payload = "|".join(parts)
    if len(payload) > board_row.ROW_LIMIT:
        raise ValueError("task request exceeds bounded board row; no spill")
    return rid, board_cells(rid, "AYA_REQ", payload, "call-task %s %s" % (op, ask))


def _receipt(ask, op, supplied, status, value):
    prefix = "OK call-task %s: " % ask
    if status != "OK" or not isinstance(value, str) or not value.startswith(prefix):
        return None
    try:
        rec = json.loads(value[len(prefix):])
    except ValueError:
        return None
    if (not isinstance(rec, dict) or rec.get("ask") != ask or rec.get("agent") != TAG
            or not life.SHA.fullmatch(str(rec.get("source_revision", "")))
            or not life.SHA.fullmatch(str(rec.get("input_sha256", "")))
            or type(rec.get("input_bytes")) is not int or not 0 < rec["input_bytes"] < 4000
            or type(rec.get("rev")) is not int or rec["rev"] < 1
            or type(rec.get("expires_at")) not in (int, float) or not math.isfinite(rec["expires_at"])
            or not isinstance(rec.get("claimed_at"), str)
            or not call_bridge.CONF_ROOM.fullmatch(str(rec.get("conf_room", "")))
            or not isinstance(rec.get("session_epoch"), str) or not rec["session_epoch"]
            or not life.STREAM_ID.fullmatch(str(rec.get("event", "")))
            or rec.get("state") not in ("PREPARED", "OPEN", "UNKNOWN", *life.TERMINAL)
            or rec.get("request_hash") != canonical({"op": op, "input": supplied or {}})):
        return None
    try:
        dt.datetime.strptime(rec["claimed_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return rec


def receive(ask, op, supplied, rid, wait=None):
    # lp.wait_answers accepts only exact server-owned verified reconciler stamps
    # and fresh v6 correlations. A payload-declared from is not sufficient.
    wait = wait or (lambda ids: lp.wait_answers(ids, timeout=ANSWER_WAIT))
    status, value = wait([rid]).get(rid, (None, ""))
    return _receipt(ask, op, supplied, status, value)


def read_status(ask, post=None, wait=None):
    rid, row = task_row(ask, "status")
    post = post or lp.post_once
    if post(rid, row) not in ("VERIFIED", "FOUND"):
        return None
    return receive(ask, "status", {}, rid, wait)


def _private_input(path, expected_hash, expected_bytes):
    """Read exact bounded bytes; return a boolean, never the private text."""
    if not path:
        return False
    try:
        with open(path, "rb") as f:
            raw = f.read(4001)
        content = raw.decode("utf-8", "strict")
    except (OSError, UnicodeError, TypeError):
        return False
    return (0 < len(content) < 1000 and len(raw) == expected_bytes
            and hashlib.sha256(raw).hexdigest() == expected_hash)


def _bound(task, rec):
    return bool(rec and rec.get("source_revision") == task.get("source_revision")
                and rec.get("input_sha256") == task.get("input_hash")
                and rec.get("input_bytes") == task.get("input_bytes"))


def accept(ask, expect="mermaid", brief="", input_path=None, reads=None, now=time.time):
    reads = reads or read_status
    old = cs.load()["tasks"].get(ask)
    if old and old["state"] != "PENDING":
        print("REFUSED: task is not a pending cue")
        return 2
    rec = reads(ask)                 # DIRECT lookup: B cannot advance past unseen A
    if not rec or rec.get("state") != "OPEN":
        print("STOPPED: authenticated open task authority unavailable")
        return 2
    if not _private_input(input_path, rec["input_sha256"], rec["input_bytes"]):
        print("REFUSED: complete private input unavailable or exact byte binding mismatches")
        return 2
    if now() >= rec["expires_at"]:
        print("STOPPED: task expired")
        return 2
    receipt = "cli:" + hashlib.sha256((ask+"|"+rec["source_revision"]).encode()).hexdigest()[:24]
    spec = wake_ack.ack_spec(ask, TAG, utc(), call_bridge.WAKE_PREFIX+ask, receipt)
    rid, row = spec["row_id"], board_cells(spec["row_id"], "ACK", spec["payload"], spec["gist"], spec["target_surface"])
    with cs.locked() as txn:
        task = txn.state["tasks"].setdefault(ask, {"state": "PENDING", "rev": 0, "ops": {}})
        if task["state"] != "PENDING":
            return 6
        if task.get("source_revision") not in (None, rec["source_revision"]):
            print("STOPPED: source revision changed")
            return 2
        cs.touch(task, source_revision=rec["source_revision"], input_hash=rec["input_sha256"], input_bytes=rec["input_bytes"],
                 input_path=os.path.abspath(input_path), authority_revision=rec["rev"], entry=rec.get("inbox_entry", ""),
                 origin=dt.datetime.fromisoformat(rec["claimed_at"].replace("Z", "+00:00")).timestamp(),
                 expires_at=rec["expires_at"], conference_room=rec["conf_room"], session_epoch=rec["session_epoch"],
                 expect=expect, brief=brief[:200], receipt=receipt, provenance="exact-private-utf8-source-v1", stages={"validated": now()})
        outcome = lp.send(txn, ask, rid, row)
        if outcome == "CONFIRMED":
            cs.touch(task, state="ACCEPTED")
            task["stages"]["accepted"] = now()
    print("ACCEPTED: exact input bound; agent transport remains claimed" if outcome == "CONFIRMED" else "STOPPED: ACK effect unknown")
    return 0 if outcome == "CONFIRMED" else 2


def public_file():
    with urllib.request.urlopen(urllib.request.Request(CANVAS+"?t=%d" % int(time.time()), headers={"Cache-Control": "no-cache"}), timeout=20) as r:
        return json.load(r)


def card_for(ask, spec, source_revision):
    return {"v": 1, "version": 1, "public": True, "status": "done", "type": spec.get("type", "note"),
            "ts": cs.utc_stamp(), "title": spec["title"], "activity": "made during the call", "body": spec["body"],
            "ask": ask, "source_revision": source_revision}


def _closed(txn, task, rec):
    # Lifecycle authority and public cleanup are deliberately separate. No retry
    # of an unconditional WD is used; revoked lifecycle fences future CARD/DONE.
    cs.touch(task, state="CANCELLING", lifecycle_state=rec["state"], authority_revision=rec["rev"],
             cleanup="PUBLICATION_UNRESOLVED")
    print("STOPPED: lifecycle %s; public cleanup unresolved (older upload may remain in flight)" % rec["state"])
    return 2


def _request(txn, ask, task, op, supplied):
    requests = task.setdefault("requests", {})
    held = requests.get(op)
    if held:
        if held["input"] != supplied:
            raise ValueError("persisted operation binding conflict")
    else:
        rid, row = task_row(ask, op, supplied)
        held = requests[op] = {"rid": rid, "row": row, "input": supplied}
        txn.save()                    # immutable intent BEFORE any append
    return held, lp.send(txn, ask, held["rid"], held["row"])


def done(ask, card_path=None, reads=None, fetch=public_file, sleep=time.sleep, now=time.time, receive_fn=None):
    reads, receive_fn = reads or read_status, receive_fn or receive
    task = cs.load()["tasks"].get(ask)
    if not task or task["state"] not in ("ACCEPTED", "WRITING", "STORED", "PUBLISHED", "COMPLETING", "CANCELLING", "CANCELLED_AFTER_WRITE"):
        print("REFUSED: no open source-bound accepted task")
        return 2
    rec = reads(ask)
    if not _bound(task, rec):
        print("STOPPED: authenticated matching task authority unavailable")
        return 2
    with cs.locked() as txn:
        current = txn.state["tasks"].get(ask)
        if current["rev"] != task["rev"]:
            return 6
        if rec["state"] in ("CANCELLED", "EXPIRED"):
            return _closed(txn, current, rec)
        if rec["state"] == "DONE":
            req = current.get("requests", {}).get("complete")
            if req and rec.get("completion_op") == req["rid"] and rec.get("card_hash") == current.get("hash"):
                cs.touch(current, state="DONE", authority_revision=rec["rev"])
                print("DONE: exact conditional completion receipt; browser acceptance separate")
                return 0
            print("STOPPED: another completion is not proof of this operation")
            return 2
        if rec["state"] != "OPEN" or current["state"] in ("CANCELLING", "CANCELLED_AFTER_WRITE"):
            print("STOPPED: lifecycle/cleanup remains unresolved")
            return 2
        if not _private_input(current.get("input_path"), current.get("input_hash"), current.get("input_bytes")):
            print("REFUSED: private input changed or unavailable")
            return 2
        if current["state"] == "ACCEPTED":
            try:
                with open(card_path, encoding="utf-8") as f:
                    spec = json.load(f)
                card = card_for(ask, spec, current["source_revision"])
                field = ask+"-work"
                item = canvas_publish.clean_item(TAG, field, life.dump(card))
                if item is None or card["type"] != current.get("expect"):
                    raise ValueError()
                cs.touch(current, state="WRITING", field=field, card=card, hash=canonical(item),
                         authority_revision=rec["rev"], sanitizer=sanitizer())
            except (OSError, ValueError, TypeError, KeyError):
                print("REFUSED: bounded publishable artifact unavailable")
                return 2
        task = dict(current)
    if task["state"] == "WRITING":
        stored_req = task.get("requests", {}).get("card")
        if stored_req and rec.get("card_op") == stored_req["rid"] and rec.get("card_hash") == task["hash"]:
            effect = rec
        else:
            supplied = {"source_revision": task["source_revision"], "task_revision": task["authority_revision"], "card": task["card"]}
            with cs.locked() as txn:
                current = txn.state["tasks"][ask]
                if current["rev"] != task["rev"]:
                    return 6
                try:
                    held, outcome = _request(txn, ask, current, "card", supplied)
                except ValueError:
                    print("STOPPED: immutable CARD request conflicts or exceeds row bound")
                    return 2
                task = dict(current)
            effect = receive_fn(ask, "card", supplied, held["rid"]) if outcome == "CONFIRMED" else None
        if not _bound(task, effect) or effect.get("state") != "OPEN" or effect.get("card_hash") != task["hash"]:
            with cs.locked() as txn:
                if _bound(task, effect) and effect.get("state") in ("CANCELLED", "EXPIRED"):
                    return _closed(txn, txn.state["tasks"][ask], effect)
            print("STOPPED: CARD effect unknown/refused; reconcile, never blind retry")
            return 2
        with cs.locked() as txn:
            current = txn.state["tasks"][ask]
            if current["rev"] != task["rev"]:
                return 6
            cs.touch(current, state="STORED", authority_revision=effect["rev"])
            current.setdefault("stages", {})["stored"] = now()
            task = dict(current)
    if task["state"] == "STORED":
        end, generated = now()+PUBLISH_WAIT, ""
        while now() < end:
            try:
                doc = fetch()
                matches = [x for x in doc.get("items", []) if isinstance(x, dict) and x.get("id") == TAG+"/"+task["field"]]
                if len(matches) == 1 and canonical(matches[0]) == task["hash"]:
                    generated = str(doc.get("generated") or "")
                    if generated:
                        break
            except (OSError, ValueError, TypeError):
                pass
            sleep(5)
        if not generated:
            print("STOPPED: exact artifact not observed publicly")
            return 2
        with cs.locked() as txn:
            current = txn.state["tasks"][ask]
            if current["rev"] != task["rev"]:
                return 6
            cs.touch(current, state="PUBLISHED", published=generated)
            current["stages"]["published"] = now()
            task = dict(current)
    supplied = {"source_revision": task["source_revision"], "task_revision": task["authority_revision"], "card_hash": task["hash"]}
    with cs.locked() as txn:
        current = txn.state["tasks"][ask]
        if current["rev"] != task["rev"]:
            return 6
        try:
            held, outcome = _request(txn, ask, current, "complete", supplied)
        except ValueError:
            print("STOPPED: immutable COMPLETE binding conflict")
            return 2
        cs.touch(current, state="COMPLETING")
        task = dict(current)
    # This is NOT another status precheck. The actual production operation above
    # compares source/revision/cancel/expiry inside its authoritative transaction.
    effect = receive_fn(ask, "complete", supplied, held["rid"]) if outcome == "CONFIRMED" else None
    with cs.locked() as txn:
        current = txn.state["tasks"][ask]
        if current["rev"] != task["rev"]:
            return 6
        if _bound(task, effect) and effect.get("state") in ("CANCELLED", "EXPIRED"):
            return _closed(txn, current, effect)
        if (_bound(task, effect) and effect.get("state") == "DONE" and effect.get("completion_op") == held["rid"]
                and effect.get("card_hash") == task["hash"]):
            cs.touch(current, state="DONE", authority_revision=effect["rev"])
            print("DONE: conditional authoritative receipt; owner/browser acceptance separate")
            return 0
    print("STOPPED: completion effect unknown; no standalone DONE row")
    return 2


def legacy(ask, how):
    with cs.locked() as txn:
        task = txn.state["tasks"].get(ask)
        if not task or task["state"] != "LEGACY_OPEN":
            return 2
        cs.touch(task, state="HISTORICAL_CLOSED" if how == "close" else "EXPIRED", legacy=how, historical_only=True)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "migrate":
        import call_migrate
        return call_migrate.main(argv[1:])
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("verb", choices=("accept", "done", "legacy"))
    ap.add_argument("ask")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--input-file")
    ap.add_argument("--expect", default="mermaid")
    ap.add_argument("--brief", default="")
    a = ap.parse_args(argv)
    if not ASK.fullmatch(a.ask):
        return 1
    if a.verb == "accept":
        return accept(a.ask, a.expect, a.brief, a.input_file)
    if a.verb == "done":
        return done(a.ask, a.arg)
    return legacy(a.ask, a.arg) if a.arg in ("close", "abandon") else 1

if __name__ == "__main__":
    sys.exit(main())

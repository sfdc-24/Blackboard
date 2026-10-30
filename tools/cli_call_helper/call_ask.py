"""The steps of a call ask for claude-code-cli (v5, Oct 30), against the O3 bridge (grok/doorbell-wake-ack d832f9c +
Aya's repair 91656b12, carried on main 0b752b4). Aya's action list: AYA-FINISH-RESUME-20261030T133945Z-CLI.

  python call_ask.py accept <ask> --expect mermaid --brief "lead to quote"
      1. Private reads, sent together with fresh ids: the inbox (governed xrange of fleet:inbox:claude-code-cli
         after the cursor) and the bridge's protected open record (hgetall gov:callbridge:open). The entry must be
         by=call-bridge with task_id=<ask>, and the open record must name this agent: gov: is written only by the
         bridge. The origin is the record's claimed_at, stored once.
      2. Content provenance: a governed xrange of the chair's protected v1:conf:<conference_room>:events finds
         the entry whose ask_id(room, entry id, agent) is this ask, and its utterance must match the words the
         inbox carries (the reconciler quotes 200 characters at most: a prefix match, stated as such).
      3. The O3 ACK row (wake_ack.ack_spec: phase=ACK, event=worker.accepted, task_id, receipt_id), one stable id,
         INTENT saved first (laptop_post.send). The task is ACCEPTED only when that row is CONFIRMED.
  python call_ask.py done <ask> <card.json>         card.json = {"type", "title", "body", "summary"}
      Before CARD and before DONE: a fresh private status read. The ask open in gov:callbridge:open -> go on.
      Gone -> closed elsewhere: CANCELLED before the card was sent; CANCELLED_AFTER_WRITE with one withdraw
      (the same field, public:false, so the publisher drops it) after. No answer -> UNKNOWN_AUTH, nothing posted.
      The card must be the expected type and pass the publisher's own clean_item; its canonical hash and the
      sanitizer blob are stored. STORED needs the reconciler's OK; PUBLISHED needs the public item to hash the
      same. Then the RESULT row answers=<ask> (CLAUDE-DONE-<ask>) closes the claim.
  python call_ask.py legacy <ask> close|abandon     LEGACY_OPEN -> HISTORICAL_CLOSED | EXPIRED (no rows)
  python call_ask.py migrate                       the older state, rebuilt by bounded replay (call_migrate.py)
What this does NOT judge: whether the diagram is the one he asked for. It records the brief, the card and its hash;
the semantic check and the browser render are the independent verifier's. Exit 0 done, 2 stopped (state kept,
rerun safe), 4 lock busy, 6 re-plan (the task changed while waiting).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import call_state as cs             # noqa: E402
import laptop_post as lp            # noqa: E402  (pins and imports the Blackboard scripts)
import board_row                    # noqa: E402
import canvas_publish               # noqa: E402
import call_bridge                  # noqa: E402
import fleet_agent as fa            # noqa: E402
import wake_ack                     # noqa: E402

TAG = "claude-code-cli"
ASK = re.compile(r"^call-[0-9a-f]{12}$")
TO = "aya;grok;claude-chat"
CANVAS = "https://storage.googleapis.com/sfdc24-canvas/rooms/workshop.json"
PARTITION = canvas_publish.PARTITION % ("workshop", TAG)
INBOX = call_bridge.INBOX_KEY % TAG
OPEN_KEY = call_bridge.OPEN_KEY
MAX_AGE, PUBLISH_WAIT, ANSWER_WAIT, EVENT_PAGES = 2 * 3600, 90, 120, 3
WORDS = re.compile(r"\): (.*) -- Answer with a RESULT board row", re.S)


def utc():
    return dt.datetime.now(dt.timezone.utc)


def sanitizer():
    out = subprocess.run(["git", "-C", lp.MAIN, "rev-parse", "HEAD:scripts/canvas_publish.py"],
                         capture_output=True, text=True)
    return out.stdout.strip()


def canonical(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def board_cells(rid, action, payload, gist, to=TO):
    return [rid, utc().strftime("%Y-%m-%dT%H:%M:%S.%fZ"), TAG, to, action, board_row.guard(payload), "OPEN",
            "Conference", gist, ""]


def result_row(rid, text, gist, answers=None):
    fields = ["BCB", "v=1", "id=" + rid, "phase=RESULT", "class=NOTE", "from=" + TAG, "to=" + TO.replace(";", ",")]
    if answers:
        fields.append("answers=" + answers)
    return board_cells(rid, "RESULT", "|".join(fields + ["text=" + text.strip().replace("|", "/")]), gist)


def ack_row(ask, receipt):
    spec = wake_ack.ack_spec(ask, TAG, utc(), call_bridge.WAKE_PREFIX + ask, receipt)
    return spec["row_id"], board_cells(spec["row_id"], "ACK", spec["payload"], spec["gist"], spec["target_surface"])


# ------------------------------------------------------------------ private reads (governed, reconciler-answered)
def _body(text, key):
    """The JSON after '<op> <key>[ page]: ' in a reconciler OK, or None (cut at 1500 characters, or not OK)."""
    at = text.find(key)
    colon = text.find(": ", at + len(key)) if at >= 0 else -1
    if colon < 0 or text.rstrip().endswith("..."):
        return None
    try:
        return json.loads(text[colon + 2:])
    except ValueError:
        return None


def ask_reads(reads, post=None, wait=None):
    """Send governed reads together, each under a FRESH id, and wait for all. reads = [(name, op, key, extra)].
    Returns {name: parsed body or None}. Ids are never reused: a retry or a resume makes new ones."""
    post = post or (lambda rid, row: lp.post_once(rid, row))
    wait = wait or (lambda rids: lp.wait_answers(rids, timeout=ANSWER_WAIT))
    sent = {}
    stamp = utc().strftime("%Y%m%dT%H%M%S") + os.urandom(2).hex()
    for name, op, key, extra in reads:
        rid = "CLAUDE-ST-%s-%s" % (name, stamp)
        rid, row = fa.request_row(TAG, op, key, project="Conference", rid=rid, **extra)
        if post(rid, row) in ("VERIFIED", "FOUND"):
            sent[name] = (rid, key)
    answers = wait([rid for rid, _ in sent.values()]) if sent else {}
    out = {}
    for name, _op, key, _extra in reads:
        if name not in sent:
            out[name] = None
            continue
        status, text = answers.get(sent[name][0], (None, ""))
        out[name] = _body(text, key) if status == "OK" else None
    return out


def open_record(body, ask):
    """The bridge's own record of this ask, or None. Values are the reconciler's previews of JSON strings."""
    if not isinstance(body, dict) or ask not in body:
        return None
    try:
        rec = json.loads(body[ask])
    except (TypeError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("agent") == TAG and rec.get("claimed_at") else None


def inbox_entry(body, ask):
    for e in body or []:
        if isinstance(e, dict) and e.get("by") == call_bridge.BY and e.get("task_id") == ask and e.get("ask") == ask:
            return e
    return None


def norm(text):
    return re.sub(r"\s+", " ", str(text).replace("|", "/")).strip()


def provenance(events, room, ask, words):
    """('MATCH', entry id) when the chair's protected event for this ask carries these words (prefix up to the
    reconciler's 200-character quote), else ('NO-MATCH'|'NOT-FOUND', id or '')."""
    for e in events or []:
        if not isinstance(e, dict) or not cs.STREAM_ID.match(str(e.get("id", ""))):
            continue
        if call_bridge.ask_id(room, e["id"], TAG) != ask:
            continue
        said = norm(e.get("utterance", ""))
        cut = said.endswith("...")
        said = said[:-3].rstrip() if cut else said
        mine = norm(words)
        ok = mine.startswith(said) if cut else mine == said[:call_bridge.ASK_CHARS]
        return ("MATCH" if ok and said else "NO-MATCH"), e["id"]
    return "NOT-FOUND", ""


# ------------------------------------------------------------------ accept
def accept(ask, expect, brief, reads=ask_reads, now=time.time):
    state = cs.load()
    task = state["tasks"].get(ask)
    if task and task["state"] not in ("PENDING",):
        print("NOTHING TO DO: %s is %s" % (ask, task["state"]))
        return 0 if task["state"] != "LEGACY_OPEN" else 2
    after = cs.cursor(state)
    got = reads([("inbox", "xrange", INBOX, {"after": after, "count": 20}),
                 ("open", "hgetall", OPEN_KEY, {})])
    rec, entry = open_record(got.get("open"), ask), inbox_entry(got.get("inbox"), ask)
    if got.get("open") is None or got.get("inbox") is None:
        print("UNKNOWN_AUTH: a private read had no usable answer; nothing posted (rerun: fresh ids)")
        return 2
    if not rec or not entry:
        print("NOT VALIDATED: %s (open record %s, inbox entry %s); nothing posted"
              % (ask, "yes" if rec else "no", "yes" if entry else "no"))
        return 2
    room = str(entry.get("conference_room", ""))
    claimed = dt.datetime.strptime(rec["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    origin = claimed.timestamp()
    if now() - origin > MAX_AGE:
        with cs.locked() as txn:
            txn.state["tasks"].setdefault(ask, {"rev": 0, "ops": {}})
            cs.touch(txn.state["tasks"][ask], state="EXPIRED", origin=origin)
        print("EXPIRED: claimed at %s" % rec["claimed_at"])
        return 2
    m = WORDS.search(str(entry.get("text", "")))
    # The bridge claims within a few ticks of his turn: read the chair's stream from 60 s before the claim, up to
    # EVENT_PAGES pages of 20, stopping at the matching entry or once past the claim time.
    after, verdict, event_id = cs.pred("%d-0" % int((origin - 60) * 1000)), "NOT-FOUND", ""
    for _ in range(EVENT_PAGES):
        ev = reads([("events", "xrange", "v1:conf:%s:events" % room, {"after": after, "count": 20})]).get("events")
        verdict, event_id = provenance(ev, room, ask, m.group(1) if m else "")
        ids = [e.get("id") for e in ev or [] if isinstance(e, dict) and cs.STREAM_ID.match(str(e.get("id", "")))]
        if verdict != "NOT-FOUND" or len(ids) < 20 or cs.sid(ids[-1])[0] > origin * 1000:
            break
        after = ids[-1]
    if verdict != "MATCH":
        print("NOT VALIDATED: content provenance %s for %s; nothing posted" % (verdict, ask))
        return 2
    receipt = "cli:" + hashlib.sha256(("%s|%s|%s" % (ask, entry["id"], event_id)).encode()).hexdigest()[:24]
    rid, row = ack_row(ask, receipt)
    with cs.locked() as txn:
        tasks = txn.state["tasks"]
        if tasks.get(ask, {}).get("state", "PENDING") != "PENDING":
            print("RE-PLAN: %s changed while reading" % ask)
            return 6
        task = tasks.setdefault(ask, {"rev": 0, "ops": {}, "state": "PENDING"})
        cs.touch(task, origin=origin, entry=entry["id"], conference_room=room, event=event_id, expect=expect,
                 brief=brief[:200], receipt=receipt, provenance="prefix-200" if len(m.group(1)) > 200 else "full",
                 stages={"origin": origin, "validated": now()})
        status = lp.send(txn, ask, rid, row)
        if status == "CONFIRMED":
            cs.touch(task, state="ACCEPTED")
            task["stages"]["accepted"] = now()
    print("%s %s" % (status, rid))
    if status != "CONFIRMED":
        print("STOPPED: the ACK is UNKNOWN (never retried blind); state kept")
        return 2
    print("ASK (private, this terminal only): %s" % (m.group(1) if m else ""))
    return 0


# ------------------------------------------------------------------ done
def public_file():
    url = "%s?t=%d" % (CANVAS, int(time.time()))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=20) as r:
        return json.load(r)


def still_open(ask, reads):
    """True (open), False (closed by the bridge) or None (no usable answer). A fresh read id every call."""
    body = reads([("status", "hgetall", OPEN_KEY, {})]).get("status")
    if body is None:
        return None
    return open_record(body, ask) is not None


def card_for(ask, spec):
    """The stored card. `ask` binds it to the claim (the O3 bridge reads an agent card by field or ask value)."""
    return {"v": 1, "version": 1, "public": True, "status": "done", "type": spec.get("type", "note"),
            "ts": cs.utc_stamp(), "title": spec["title"], "activity": "made during the call",
            "body": spec["body"], "ask": ask}


def closed(ask, task, txn):
    """The ask was closed elsewhere: stop, and withdraw a card that may be live (its request was SENT, CONFIRMED,
    or UNKNOWN: any of them may have taken effect)."""
    if not task.get("ops", {}).get("CLAUDE-CARD-%s" % ask):
        cs.touch(task, state="CANCELLED")
        return "CANCELLED"
    card = dict(task["card"], public=False, version=task["card"]["version"] + 1, ts=cs.utc_stamp())
    rid, row = fa.request_row(TAG, "hset", PARTITION, val=json.dumps(card, ensure_ascii=False), field=task["field"],
                              project="Conference", rid="CLAUDE-CARD-%s-WD" % ask)
    lp.send(txn, ask, rid, row)
    cs.touch(task, state="CANCELLED_AFTER_WRITE")
    return "CANCELLED_AFTER_WRITE"


def done(ask, card_path, reads=ask_reads, fetch=public_file, sleep=time.sleep, now=time.time):
    task = cs.load()["tasks"].get(ask)
    if not task or task["state"] not in ("ACCEPTED", "WRITING", "STORED", "PUBLISHED"):
        print("REFUSED: %s is not an open accepted task (%s)" % (ask, task and task["state"]))
        return 2
    if now() - task["origin"] > MAX_AGE:
        with cs.locked() as txn:
            cs.touch(txn.state["tasks"][ask], state="EXPIRED")
        print("EXPIRED")
        return 2
    rev = task["rev"]
    if task["state"] in ("ACCEPTED", "WRITING"):
        open_now = still_open(ask, reads)
        with cs.locked() as txn:
            task = txn.state["tasks"][ask]
            if task["rev"] != rev:
                print("RE-PLAN")
                return 6
            if open_now is None:
                print("UNKNOWN_AUTH: no status answer; nothing posted (rerun: fresh ids)")
                return 2
            if not open_now:
                print(closed(ask, task, txn))
                return 2
            if task["state"] == "ACCEPTED":
                with open(card_path, encoding="utf-8") as f:
                    spec = json.load(f)
                card = card_for(ask, spec)
                field = "%s-work" % ask
                item = canvas_publish.clean_item(TAG, field, json.dumps(card, ensure_ascii=False))
                if item is None or card["type"] != task.get("expect"):
                    print("REFUSED: the card is not a publishable %s" % task.get("expect"))
                    return 2
                cs.touch(task, state="WRITING", field=field, card=card, hash=canonical(item), sanitizer=sanitizer(),
                         summary=str(spec.get("summary", ""))[:600])
            crid, crow = fa.request_row(TAG, "hset", PARTITION, val=json.dumps(task["card"], ensure_ascii=False),
                                        field=task["field"], project="Conference", rid="CLAUDE-CARD-%s" % ask)
            status = lp.send(txn, ask, crid, crow)
            rev = task["rev"]
        if status != "CONFIRMED":
            print("STOPPED: the card request is UNKNOWN; state kept")
            return 2
        answer, text = lp.wait_answers([crid], timeout=ANSWER_WAIT)[crid]
        if answer != "OK":
            print("STOPPED: no OK for the card write (%s); stored state unknown" % (answer or "no answer"))
            return 2
        with cs.locked() as txn:
            task = txn.state["tasks"][ask]
            if task["rev"] != rev:
                print("RE-PLAN")
                return 6
            cs.touch(task, state="STORED")
            task.setdefault("stages", {})["stored"] = now()
            rev = task["rev"]
    if task["state"] == "STORED":
        end, seen = now() + PUBLISH_WAIT, None
        while now() < end:
            data = fetch()
            mine = [i for i in data.get("items") or [] if isinstance(i, dict)
                    and i.get("id") == "%s/%s" % (TAG, task["field"])]
            if mine and canonical(mine[0]) == task["hash"]:
                seen = str(data.get("generated", ""))
                break
            sleep(5)
        if not seen:
            print("STOPPED: stored but the public item does not hash the same within %d s" % PUBLISH_WAIT)
            return 2
        with cs.locked() as txn:
            task = txn.state["tasks"][ask]
            if task["rev"] != rev:
                print("RE-PLAN")
                return 6
            cs.touch(task, state="PUBLISHED", published=seen)
            task["stages"]["published"] = now()
            rev = task["rev"]
    open_now = still_open(ask, reads)
    with cs.locked() as txn:
        task = txn.state["tasks"][ask]
        if task["rev"] != rev:
            print("RE-PLAN")
            return 6
        if open_now is None:
            print("UNKNOWN_AUTH before DONE; nothing posted")
            return 2
        if not open_now:
            print(closed(ask, task, txn))
            return 2
        secs = task["stages"]["published"] - task["origin"]
        drid = "CLAUDE-DONE-%s" % ask
        body = ("Done: call ask %s. Card claude-code-cli/%s (%s), canonical sha256 %s under sanitizer blob %s, "
                "public file generated %s; claim-to-public %.0f s (claimed_at to the file, NOT owner-ask to browser). "
                "%s") % (ask, task["field"], task["card"]["type"], task["hash"][:16], task["sanitizer"][:12],
                         task["published"], secs, task.get("summary", ""))
        status = lp.send(txn, ask, drid, result_row(drid, body[:1250], "Done: %s" % ask, answers=ask))
        if status == "CONFIRMED":
            cs.touch(task, state="DONE")
    print(status, drid)
    return 0 if status == "CONFIRMED" else 2


def legacy(ask, how):
    with cs.locked() as txn:
        task = txn.state["tasks"].get(ask)
        if not task or task["state"] != "LEGACY_OPEN":
            print("REFUSED: %s is not LEGACY_OPEN" % ask)
            return 2
        cs.touch(task, state="HISTORICAL_CLOSED" if how == "close" else "EXPIRED", legacy=how)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("verb")
    ap.add_argument("ask")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--expect", default="mermaid")
    ap.add_argument("--brief", default="")
    a = ap.parse_args()
    if not ASK.match(a.ask):
        sys.exit(1)
    if a.verb == "accept":
        sys.exit(accept(a.ask, a.expect, a.brief))
    if a.verb == "done" and a.arg:
        sys.exit(done(a.ask, a.arg))
    if a.verb == "legacy" and a.arg in ("close", "abandon"):
        sys.exit(legacy(a.ask, a.arg))
    print(__doc__)
    sys.exit(1)

"""The steps of a call ask for claude-code-cli (v5.1, Oct 30), against the O3 bridge (grok/doorbell-wake-ack d832f9c +
Aya's repair 91656b12 + the owner-approved 6a/6b bridge source, carried on main 0b752b4).
Corrections for Aya's PR 441 intake HOLD (8570, 8659); plan: PR 441 comment 6533487332.

  python call_ask.py migrate [--legacy <old cli_call_state.json>]
      Only when no v2 state exists: a bounded replay (call_migrate.replay) of the governed inbox from the legacy
      cursor minus 24 h, at most 25 pages of 20, judged against one full board read. Writes the state, or refuses.
  python call_ask.py accept <ask> --expect mermaid --brief "lead to quote"
      Reads only protected keys (gov:, written by the bridge alone), under fresh ids, never a cursor: the per-ask
      state (gov:callbridge:state:<ask>, must be open and name this agent), the said digest and the said parts
      (6a). The parts must rebuild words whose sha256 is the digest: the WHOLE ask. (A governed read quotes the inbox
      text at 200 characters, so the inbox is not the source.) Then the O3 ACK row (wake_ack.ack_spec).
  python call_ask.py done <ask> <card.json>         card.json = {"type", "title", "body", "summary"}
      A fresh private state read before CARD and before DONE. open: go on. done or cancelled (an owner "cancel" in
      the room, 6b): stop; with a card request already made, CANCELLING, a withdrawal that is final only once the
      CARD answer is known AND the withdraw's answer row lands after it (the reconciler applies requests in row
      order). Unknown CARD past the deadline: FAILED_CARD_MAY_BE_LIVE, escalated, never called withdrawn.
      DONE carries status_op=<the read before it>; one more read after it: cancelled before DONE means
      DONE_SUPERSEDED handling (the card comes down the same way), never acceptance.
  python call_ask.py legacy <ask> close|abandon     LEGACY_OPEN -> HISTORICAL_CLOSED | EXPIRED (no rows)
Not judged here: whether the diagram is the one he asked for (the verifier's), or the browser render.
Exit 0 done, 2 stopped (state kept, rerun safe), 4 lock busy, 5 no state (run migrate), 6 re-plan.
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
import call_migrate as cm           # noqa: E402
import laptop_post as lp            # noqa: E402  (pins and imports the Blackboard scripts)
import board_row                    # noqa: E402
import canvas_publish               # noqa: E402
import call_bridge                  # noqa: E402
import fleet_agent as fa            # noqa: E402
import wake_ack                     # noqa: E402

TAG = "claude-code-cli"
ASK = re.compile(r"^call-[0-9a-f]{12}$")
ASK_IN_TEXT = re.compile(r"\bCALL ASK (call-[0-9a-f]{12})\b")
TO = "aya;grok;claude-chat"
CANVAS = "https://storage.googleapis.com/sfdc24-canvas/rooms/workshop.json"
PARTITION = canvas_publish.PARTITION % ("workshop", TAG)
INBOX = call_bridge.INBOX_KEY % TAG
MAX_AGE, PUBLISH_WAIT, ANSWER_WAIT = 2 * 3600, 90, 120
MIGRATE_PAGES = 25
MAX_PARTS = -(-call_bridge.ASK_CHARS // call_bridge.SAID_PART_CHARS)


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


def result_row(rid, text, gist, answers=None, extra=()):
    fields = ["BCB", "v=1", "id=" + rid, "phase=RESULT", "class=NOTE", "from=" + TAG, "to=" + TO.replace(";", ",")]
    if answers:
        fields.append("answers=" + answers)
    fields += list(extra)
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
        out["_rid_" + name] = sent[name][0]
    return out


def bridge_state(value):
    """The bridge's per-ask state (6b) from a governed get's value (a JSON string), or None."""
    try:
        state = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return state if isinstance(state, dict) and state.get("state") in ("open", "done", "cancelled") else None


def whole_ask(ask, st, digest, first, reads):
    """('OK', words) when the protected parts rebuild words whose sha256 is the protected digest; else
    ('UNKNOWN' | 'MISMATCH', ''). A governed read quotes the inbox text at 200 characters, so the whole ask comes
    only from the bridge's own parts (6a), never from the inbox."""
    n = st.get("parts")
    if not isinstance(n, int) or not 1 <= n <= MAX_PARTS:
        return "MISMATCH", ""
    raw = [first] + [None] * (n - 1)
    if n > 1:
        got = reads([("p%d" % i, "get", call_bridge.SAID_PART_KEY % (ask, i), {}) for i in range(1, n)])
        for i in range(1, n):
            raw[i] = got.get("p%d" % i)
    try:
        parts = [json.loads(v) for v in raw]
    except (TypeError, ValueError):
        return "UNKNOWN", ""
    if not all(isinstance(p, str) for p in parts):
        return "UNKNOWN", ""
    words = "".join(parts)
    return ("OK" if hashlib.sha256(words.encode("utf-8")).hexdigest() == digest else "MISMATCH"), words


# ------------------------------------------------------------------ accept
def accept(ask, expect, brief, reads=ask_reads, now=time.time):
    state = cs.load()                                        # no state: exit 5, run migrate first
    task = state["tasks"].get(ask)
    if task and task["state"] != "PENDING":
        print("NOTHING TO DO: %s is %s" % (ask, task["state"]))
        return 0 if task["state"] != "LEGACY_OPEN" else 2
    got = reads([("state", "get", call_bridge.STATE_KEY % ask, {}), ("said", "get", call_bridge.SAID_KEY % ask, {}),
                 ("p0", "get", call_bridge.SAID_PART_KEY % (ask, 0), {})])
    st, digest = bridge_state(got.get("state")), got.get("said")
    if st is None or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        print("UNKNOWN_AUTH: no usable protected state/digest answer for %s; nothing posted (rerun: fresh ids)" % ask)
        return 2
    if st["state"] != "open" or st.get("agent") != TAG:
        print("NOT VALIDATED: the bridge's state for %s is %s for %s; nothing posted" % (ask, st["state"],
                                                                                      st.get("agent")))
        return 2
    origin = dt.datetime.strptime(st["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    if now() - origin > MAX_AGE:
        with cs.locked() as txn:
            t = txn.state["tasks"].setdefault(ask, {"rev": 0, "ops": {}, "state": "PENDING"})
            cs.touch(t, state="EXPIRED", origin=origin)
        print("EXPIRED: claimed at %s" % st["claimed_at"])
        return 2
    verdict, words = whole_ask(ask, st, digest, got.get("p0"), reads)
    if verdict != "OK":
        print("NOT VALIDATED: the protected parts of %s are %s (they must rebuild words with the bridge's digest)"
              % (ask, verdict))
        return 2
    receipt = "cli:" + hashlib.sha256(("%s|%s" % (ask, digest)).encode()).hexdigest()[:24]
    rid, row = ack_row(ask, receipt)
    with cs.locked() as txn:
        tasks = txn.state["tasks"]
        if tasks.get(ask, {}).get("state", "PENDING") != "PENDING":
            print("RE-PLAN: %s changed while reading" % ask)
            return 6
        task = tasks.setdefault(ask, {"rev": 0, "ops": {}, "state": "PENDING"})
        cs.touch(task, origin=origin, conference_room=st.get("conference_room", ""), said_len=len(words),
                 expect=expect, brief=brief[:200], receipt=receipt, said_sha256=digest, provenance="full",
                 stages={"origin": origin, "validated": now()})
        status = lp.send(txn, ask, rid, row)
        if status == "CONFIRMED":
            cs.touch(task, state="ACCEPTED")
            task["stages"]["accepted"] = now()
        elif status == "CONFLICT":
            cs.touch(task, state="CONFLICT")
    print("%s %s" % (status, rid))
    if status != "CONFIRMED":
        print("STOPPED: the ACK is %s (never retried blind); state kept" % status)
        return 2
    print("ASK (private, this terminal only): %s" % words)
    return 0


# ------------------------------------------------------------------ done
def public_file():
    url = "%s?t=%d" % (CANVAS, int(time.time()))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=20) as r:
        return json.load(r)


def status_now(ask, reads):
    """(state dict or None, the read's request id). A fresh read id every call."""
    got = reads([("status", "get", call_bridge.STATE_KEY % ask, {})])
    return bridge_state(got.get("status")), got.get("_rid_status", "")


def card_for(ask, spec):
    """The stored card. `ask` binds it to the claim (the O3 bridge reads an agent card by field or ask value)."""
    return {"v": 1, "version": 1, "public": True, "status": "done", "type": spec.get("type", "note"),
            "ts": cs.utc_stamp(), "title": spec["title"], "activity": "made during the call",
            "body": spec["body"], "ask": ask}


def answer_index(rid, records):
    """Row position of bus-reconciler's answer to `rid`, or None."""
    for i, r in enumerate(records or []):
        if str(r.get("Source_Tag", "")) == lp.RECONCILER and lp.bus_request.field(
                str(r.get("Payload", "") or ""), "answers") == rid:
            return i
    return None


def begin_cancel(ask, task, txn):
    """The ask was closed elsewhere. No card request made: CANCELLED (terminal). Otherwise CANCELLING (open)."""
    if not task.get("ops", {}).get("CLAUDE-CARD-%s" % ask):
        cs.touch(task, state="CANCELLED")
        return "CANCELLED"
    cs.touch(task, state="CANCELLING")
    return "CANCELLING"


def withdraw(ask, task, txn, records, now):
    """One step of CANCELLING. WITHDRAWN only when the CARD answer is known and a withdraw's answer row comes after
    it. Unknown CARD past the deadline: FAILED_CARD_MAY_BE_LIVE. Returns the state after the step."""
    crid = "CLAUDE-CARD-%s" % ask
    card_op = task["ops"].get(crid, {})
    if card_op.get("state") != "CONFIRMED":
        lp.resolve(txn, ask, crid, records=records)
        card_op = task["ops"].get(crid, {})
    card_at = answer_index(crid, records) if card_op.get("state") == "CONFIRMED" else None
    if card_at is None:
        if now() - task["origin"] > MAX_AGE:
            cs.touch(task, state="FAILED_CARD_MAY_BE_LIVE")
        return task["state"]
    for wd in ("CLAUDE-CARD-%s-WD" % ask, "CLAUDE-CARD-%s-WD2" % ask):
        op = task["ops"].get(wd)
        if op is None:
            card = dict(task["card"], public=False, version=task["card"]["version"] + 1, ts=cs.utc_stamp())
            wrid, wrow = fa.request_row(TAG, "hset", PARTITION, val=json.dumps(card, ensure_ascii=False),
                                        field=task["field"], project="Conference", rid=wd)
            lp.send(txn, ask, wrid, wrow)
            return task["state"]                           # its answer is read on the next step
        wd_at = answer_index(wd, records)
        if wd_at is None:
            if op.get("state") != "CONFIRMED" and now() - task["origin"] > MAX_AGE:
                cs.touch(task, state="FAILED_CARD_MAY_BE_LIVE")
            return task["state"]
        if wd_at > card_at:
            cs.touch(task, state="WITHDRAWN")
            return "WITHDRAWN"
    cs.touch(task, state="FAILED_CARD_MAY_BE_LIVE")         # both withdraws landed before the CARD answer
    return task["state"]


def done(ask, card_path, reads=ask_reads, fetch=public_file, sleep=time.sleep, now=time.time, records=None):
    task = cs.load()["tasks"].get(ask)
    if task and task["state"] == "CANCELLING":
        with cs.locked() as txn:
            task = txn.state["tasks"][ask]
            out = withdraw(ask, task, txn, lp.board_records() if records is None else records, now)
        print(out)
        return 0 if out == "WITHDRAWN" else 2
    if not task or task["state"] not in ("ACCEPTED", "WRITING", "STORED", "PUBLISHED"):
        print("REFUSED: %s is not an open accepted task (%s)" % (ask, task and task["state"]))
        return 2
    if now() - task["origin"] > MAX_AGE:
        with cs.locked() as txn:
            t = txn.state["tasks"][ask]
            cs.touch(t, state="EXPIRED" if not t["ops"].get("CLAUDE-CARD-%s" % ask) else "CANCELLING")
        print("EXPIRED")
        return 2
    rev = task["rev"]
    if task["state"] in ("ACCEPTED", "WRITING"):
        st, _ = status_now(ask, reads)
        with cs.locked() as txn:
            task = txn.state["tasks"][ask]
            if task["rev"] != rev:
                print("RE-PLAN")
                return 6
            if st is None:
                print("UNKNOWN_AUTH: no status answer; nothing posted (rerun: fresh ids)")
                return 2
            if st["state"] != "open":
                print(begin_cancel(ask, task, txn))
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
            print("STOPPED: the card request is %s; state kept" % status)
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
    drid = "CLAUDE-DONE-%s" % ask
    if task["ops"].get(drid, {}).get("state") != "CONFIRMED":
        st, srid = status_now(ask, reads)
        with cs.locked() as txn:
            task = txn.state["tasks"][ask]
            if task["rev"] != rev:
                print("RE-PLAN")
                return 6
            if st is None:
                print("UNKNOWN_AUTH before DONE; nothing posted")
                return 2
            if st["state"] != "open":
                print(begin_cancel(ask, task, txn))
                return 2
            secs = task["stages"]["published"] - task["origin"]
            body = ("Done: call ask %s. Card claude-code-cli/%s (%s), canonical sha256 %s under sanitizer blob %s, "
                    "public file generated %s; claim-to-public %.0f s (claimed_at to the file, NOT owner-ask to "
                    "browser). %s") % (ask, task["field"], task["card"]["type"], task["hash"][:16],
                                       task["sanitizer"][:12], task["published"], secs, task.get("summary", ""))
            status = lp.send(txn, ask, drid, result_row(drid, body[:1150], "Done: %s" % ask, answers=ask,
                                                        extra=["status_op=" + srid]))
            rev = task["rev"]
        if status != "CONFIRMED":
            print(status, drid)
            return 2
    # After DONE: was the ask cancelled before it? (Aya 8659 item 4.) Then the card comes down like any cancel.
    st, _ = status_now(ask, reads)
    with cs.locked() as txn:
        task = txn.state["tasks"][ask]
        done_ts = task["ops"][drid]["row"][1]
        if st is not None and st["state"] == "cancelled" and str(st.get("at", "")) <= done_ts[:19] + "Z":
            task["done_superseded"] = True
            cs.touch(task, state="CANCELLING")
            print("DONE_SUPERSEDED: cancelled at %s, before DONE %s; withdrawing" % (st.get("at"), done_ts))
            return 2
        cs.touch(task, state="DONE", post_done_check=(st or {}).get("state", "UNKNOWN"))
    print("CONFIRMED", drid)
    return 0


def legacy(ask, how):
    with cs.locked() as txn:
        task = txn.state["tasks"].get(ask)
        if not task or task["state"] != "LEGACY_OPEN":
            print("REFUSED: %s is not LEGACY_OPEN" % ask)
            return 2
        cs.touch(task, state="HISTORICAL_CLOSED" if how == "close" else "EXPIRED", legacy=how)
    return 0


# ------------------------------------------------------------------ migrate
def ask_of(text):
    m = ASK_IN_TEXT.search(text or "")
    return m.group(1) if m else None


def migrate(legacy_path, reads=ask_reads, records=None, now=time.time):
    """Bounded trusted replay into a NEW v2 state; refuses when one exists (Aya 8659 item 1)."""
    if os.path.exists(cs.STATE):
        print("REFUSED: %s exists; migrate only builds a new state" % cs.STATE)
        return 2
    try:
        with open(legacy_path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    start = cm.window_start(str(old.get("cursor") or ""), int(now() * 1000))
    entries, after = [], cs.pred(start) if start != "0-0" else "0-0"
    for _ in range(MIGRATE_PAGES):
        page = reads([("migrate", "xrange", INBOX, {"after": after, "count": 20})]).get("migrate")
        if page is None:
            print("FAILED: an inbox page had no usable answer; nothing written (rerun: fresh ids)")
            return 2
        for e in page:
            if isinstance(e, dict) and cs.STREAM_ID.match(str(e.get("id", ""))):
                entries.append((e["id"], str(e.get("by", "")), str(e.get("text", ""))))
        if len(page) < 20:
            break
        after = entries[-1][0]
    else:
        print("FAILED: over %d pages in the replay window; escalate" % MIGRATE_PAGES)
        return 2
    records = lp.board_records() if records is None else records
    if records is None:
        print("FAILED: the board could not be read; nothing written")
        return 2
    state, notes = cm.replay(entries, records, old, int(now() * 1000), ask_of)
    with cs.locked(missing_ok=True) as txn:
        if txn.state["tasks"] or txn.state.get("seen"):
            print("REFUSED: a state appeared meanwhile")
            return 2
        txn.state.update(state)
    for note in notes:
        print(note)
    print("MIGRATED: %d tasks, seen %s" % (len(state["tasks"]), state.get("seen") or "-"))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("verb", choices=("accept", "done", "legacy", "migrate"))
    ap.add_argument("ask", nargs="?")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--expect", default="mermaid")
    ap.add_argument("--brief", default="")
    ap.add_argument("--legacy", default=os.path.join(os.path.dirname(cs.STATE_DIR), "cli_call_state.json"))
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        print(__doc__)
        return 1
    if a.verb == "migrate":
        return migrate(a.legacy)
    if not a.ask or not ASK.match(a.ask):
        print(__doc__)
        return 1
    if a.verb == "accept":
        return accept(a.ask, a.expect, a.brief)
    if a.verb == "done" and a.arg:
        return done(a.ask, a.arg)
    if a.verb == "legacy" and a.arg in ("close", "abandon"):
        return legacy(a.ask, a.arg)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

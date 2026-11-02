"""Authoritative call-task lifecycle (offline repair candidate, no runtime activation).

Only the pinned chair producer may write v1:conf:. Typed cancellation is read from
that protected stream, never inferred from words or a board actor. Board request
principals remain GOVERNED CLAIMS, not authenticated agents. The reconciler signs
its responses through the existing gateway sender check; receipts contain digests
and identifiers only, NEVER owner task text.

Redis EXEC does not roll back runtime failures. Claim writes PREPARED with its
side effects, and is activated only after every result is checked. An ambiguous
claim remains quarantined and is never blindly redelivered. Terminal records do
not expire: an arbitrarily delayed request must not recreate a cancelled ask.
"""
from __future__ import annotations
import base64
import datetime as dt
import hashlib
import json
import re

TASK_KEY = "gov:callbridge:task:%s"
EXPIRY_KEY = "gov:callbridge:active:%s"
OPEN_KEY = "gov:callbridge:open"
ASK_KEY = "gov:callbridge:ask:%s"
RING_KEY = "gov:callbridge:ring"
TASK_ID = re.compile(r"^call-[0-9a-f]{12}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
STREAM_ID = re.compile(r"^[0-9]+-[0-9]+$")
MAX_AGE = 7200
TERMINAL = ("CANCELLED", "EXPIRED", "DONE")
PRODUCER_REVISION = "41daf85bb75d53755d41db2dd45f941044b4e7c3+typed-cancel-v1"

class Refused(ValueError):
    pass

def text(v):
    return v.decode("utf-8", "strict") if isinstance(v, bytes) else v

def fields(v):
    return {text(k): text(x) for k, x in (v or {}).items()}

def dump(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def digest(v):
    return hashlib.sha256(dump(v).encode("utf-8")).hexdigest()

def stamp(now):
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")

def record(raw):
    try:
        value = json.loads(text(raw))
    except (TypeError, ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or value.get("v") != 1 or value.get("state") not in (
            "PREPARED", "OPEN", "UNKNOWN", *TERMINAL):
        return None
    if (not TASK_ID.fullmatch(str(value.get("ask", ""))) or not SHA.fullmatch(str(value.get("source_revision", "")))
            or type(value.get("rev")) is not int or value["rev"] < 1):
        return None
    return value

def get(conn, ask):
    value = record(conn.get(TASK_KEY % ask)) if TASK_ID.fullmatch(str(ask)) else None
    return value if value is not None and value["ask"] == ask else None

def _types(pipe, expected):
    for key, kind in expected.items():
        actual = text(pipe.type(key))
        if actual not in ("none", kind):
            raise Refused("lifecycle key type mismatch")

def _execute(pipe):
    results = pipe.execute(raise_on_error=False)
    if not isinstance(results, list) or any(isinstance(v, BaseException) for v in results):
        raise Refused("transaction outcome unconfirmed")
    return results

def _event(pipe, room, eid):
    if not STREAM_ID.fullmatch(str(eid)):
        raise Refused("source revision missing")
    rows = pipe.xrange("v1:conf:%s:events" % room, min=eid, max=eid, count=2)
    if not rows or len(rows) != 1 or text(rows[0][0]) != eid:
        raise Refused("exact source event unavailable")
    ev = fields(rows[0][1])
    state = fields(pipe.hgetall("v1:conf:%s:state" % room))
    if (ev.get("room_id") != room or not ev.get("session_epoch")
            or state.get("session_epoch") != ev["session_epoch"] or state.get("closed") == "1"
            or state.get("history_gap") == "1" or not ev.get("actor_id", "").startswith("own_")):
        raise Refused("source authority or current epoch unavailable")
    try:
        if int(ev["control_sequence"]) < 1 or int(ev["proj_n"]) < 1:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise Refused("source sequence unavailable") from None
    return ev

def capture(pipe, room, eid, agent, confirmation=None):
    ev = _event(pipe, room, eid)
    utterance = ev.get("utterance")
    # Exact producer truncates at 1000 characters WITHOUT a marker. Equality is
    # ambiguous too: reject >=1000 rather than hash a possibly incomplete prefix.
    if ev.get("kind") != "turn.started" or not isinstance(utterance, str) or not utterance.strip() or len(utterance) >= 1000:
        raise Refused("complete source content unavailable")
    raw = utterance.encode("utf-8", "strict")
    source = {"conference_room": room, "session_epoch": ev["session_epoch"], "event": eid,
              "control_sequence": ev["control_sequence"], "proj_n": ev["proj_n"], "owner": ev["actor_id"],
              "agent": agent, "input_sha256": hashlib.sha256(raw).hexdigest(), "input_bytes": len(raw)}
    if confirmation:
        go = _event(pipe, room, confirmation)
        if go.get("kind") != "turn.started" or go["actor_id"] != ev["actor_id"] or go["session_epoch"] != ev["session_epoch"]:
            raise Refused("confirmation source mismatch")
        said = go.get("utterance")
        if not isinstance(said, str) or not said.strip() or len(said) >= 1000:
            raise Refused("complete confirmation unavailable")
        source["confirmation"] = {"event": confirmation, "control_sequence": go["control_sequence"],
                                  "proj_n": go["proj_n"], "sha256": hashlib.sha256(said.encode("utf-8")).hexdigest()}
    return source, utterance

def claim(conn, ask, room, eid, agent, canvas_room, now, card, inbox_builder, ring=None, confirmation=None):
    """One claim. No reservation exists before this WATCHed transaction. Returns a
    complete authoritative record, or None for an existing/legacy reservation.
    Unknown execution raises and leaves PREPARED nonterminal; no blind retry."""
    partition = "conf:canvas:%s:%s" % (canvas_room, agent)
    inbox = "fleet:inbox:%s" % agent
    key = TASK_KEY % ask
    watch = [key, ASK_KEY % ask, EXPIRY_KEY % ask, OPEN_KEY, partition, inbox, RING_KEY,
             "v1:conf:%s:events" % room, "v1:conf:%s:state" % room]
    pipe = conn.pipeline(transaction=True)
    try:
        pipe.watch(*watch)
        if pipe.get(key) is not None or pipe.get(ASK_KEY % ask) is not None:
            return None
        _types(pipe, {key: "string", ASK_KEY % ask: "string", EXPIRY_KEY % ask: "string", OPEN_KEY: "hash", partition: "hash", inbox: "stream", RING_KEY: "hash"})
        source, utterance = capture(pipe, room, eid, agent, confirmation)
        info = {"v": 1, "ask": ask, "state": "PREPARED", "rev": 1, "agent": agent, "room": canvas_room,
                "conf_room": room, "session_epoch": source["session_epoch"], "source": source,
                "source_revision": digest(source), "claimed_at": stamp(now), "expires_at": now.timestamp() + MAX_AGE,
                "asked_at": dt.datetime.fromtimestamp(int((confirmation or eid).split("-")[0])/1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        initial = dict(card, ask=ask, source_revision=info["source_revision"])
        import canvas_publish
        initial_item = canvas_publish.clean_item(agent, ask, dump(initial))
        if initial_item is None:
            raise Refused("claim card is not publishable")
        info["claim_hash"] = digest(initial_item)
        inbox_fields = inbox_builder(utterance)
        inbox_fields.update(source_revision=info["source_revision"], input_sha256=source["input_sha256"], input_bytes=str(source["input_bytes"]))
        pipe.multi()
        pipe.set(key, dump(info))
        pipe.set(ASK_KEY % ask, "lifecycle-v1")
        pipe.hset(partition, ask, dump(initial))
        pipe.xadd(inbox, inbox_fields)
        # Redis expiration is part of WATCH semantics (Redis >=6.0.9). This
        # closes the wall-clock gap between a client precheck and EXEC.
        pipe.set(EXPIRY_KEY % ask, info["source_revision"], ex=MAX_AGE)
        if ring is not None:
            pipe.hset(RING_KEY, ask, dump(ring))
        results = _execute(pipe)
        if len(results) != (6 if ring is not None else 5) or not STREAM_ID.fullmatch(str(text(results[3]))):
            raise Refused("claim effects unconfirmed")
        inbox_entry = text(results[3])
    finally:
        pipe.reset()
    # Activation is separate and fail closed: PREPARED is not an accepted/open
    # task. A crash/unknown before activation cannot let the inbox recipient DONE.
    pipe = conn.pipeline(transaction=True)
    try:
        pipe.watch(key, OPEN_KEY, EXPIRY_KEY % ask)
        held = record(pipe.get(key))
        if held != info or text(pipe.get(EXPIRY_KEY % ask)) != info["source_revision"]:
            raise Refused("claim activation conflict")
        _types(pipe, {key: "string", OPEN_KEY: "hash"})
        info = dict(info, state="OPEN", inbox_entry=inbox_entry)
        pipe.multi()
        pipe.set(key, dump(info))
        pipe.hset(OPEN_KEY, ask, dump(info))
        _execute(pipe)
        return info
    finally:
        pipe.reset()

def public_receipt(info):
    """Allowlisted metadata only. No owner id, utterance, private title or body."""
    source = info["source"]
    out = {k: info[k] for k in ("ask", "state", "rev", "agent", "room", "conf_room", "session_epoch", "source_revision", "claimed_at", "expires_at")}
    out.update(input_sha256=source["input_sha256"], input_bytes=source["input_bytes"], event=source["event"],
               inbox_entry=info.get("inbox_entry", ""))
    for k in ("field", "card_hash", "card_op", "completion_op", "cancel_event", "cleanup"):
        if k in info:
            out[k] = info[k]
    return out

def _terminal(pipe, info, state, now, *, event=""):
    """Caller WATCHes task, OPEN and partition. Revocation and Redis withdrawal
    share the state CAS. External publication cleanup is explicitly unresolved."""
    value = dict(info, state=state, rev=info["rev"] + 1, closed_at=stamp(now), cleanup="PUBLICATION_UNRESOLVED")
    if event:
        value["cancel_event"] = event
    partition = "conf:canvas:%s:%s" % (info["room"], info["agent"])
    pipe.multi()
    pipe.set(TASK_KEY % info["ask"], dump(value))
    pipe.hdel(OPEN_KEY, info["ask"])
    pipe.hdel(RING_KEY, info["ask"])
    pipe.hdel(partition, info["ask"])
    if info.get("field"):
        pipe.hdel(partition, info["field"])
    _audit(pipe, value, state.lower(), event or "expiry", "conference-chair" if event else "call-bridge", now)
    _execute(pipe)
    return value

def _scope(pipe, info):
    partition = "conf:canvas:%s:%s" % (info["room"], info["agent"])
    pipe.watch(OPEN_KEY, RING_KEY, partition, "gov:audit")
    _types(pipe, {TASK_KEY % info["ask"]: "string", OPEN_KEY: "hash", RING_KEY: "hash", partition: "hash", "gov:audit": "stream"})
    return partition

def cancel_event(conn, room, eid, now):
    """Consume exact protected typed producer event, never payload actor alone."""
    stream, roomkey = "v1:conf:%s:events" % room, "v1:conf:%s:state" % room
    pipe = conn.pipeline(transaction=True)
    try:
        pipe.watch(stream, roomkey)
        ev = _event(pipe, room, eid)
        ask = ev.get("task_id", "")
        if ev.get("kind") != "task.cancel.requested" or not TASK_ID.fullmatch(ask):
            raise Refused("not an explicit task cancellation")
        pipe.watch(TASK_KEY % ask, EXPIRY_KEY % ask)
        info = record(pipe.get(TASK_KEY % ask))
        if not info or info["ask"] != ask:
            raise Refused("unknown task")
        if (ev.get("source_revision") != info["source_revision"] or ev.get("session_epoch") != info["session_epoch"]
                or room != info["conf_room"] or ev.get("actor_id") != info["source"]["owner"]
                or ev.get("task_revision") != str(info["rev"]) or not ev.get("command_id")):
            raise Refused("cancellation binding conflict")
        if info["state"] in TERMINAL:
            return info
        if info["state"] not in ("OPEN", "PREPARED", "UNKNOWN"):
            raise Refused("task state unavailable")
        _scope(pipe, info)
        if info["state"] == "OPEN" and text(pipe.get(EXPIRY_KEY % ask)) != info["source_revision"]:
            return _terminal(pipe, info, "EXPIRED", now)
        return _terminal(pipe, info, "CANCELLED", now, event=eid)
    finally:
        pipe.reset()

def _audit(pipe, info, op, request_id, who, now):
    pipe.xadd("gov:audit", {"at": stamp(now), "actor": who, "raw_tag": who, "via": request_id,
                           "op": "call-task:" + op, "key": TASK_KEY % info["ask"],
                           "ok": "true", "authority": "protected-lifecycle",
                           "source_revision": info["source_revision"]}, maxlen=200000, approximate=True)


def operate(conn, who, ask, op, supplied, request_id, now):
    """Conditional production operation; the caller supplies a GOVERNED principal
    claim. This does not authenticate the requesting agent. All result authority
    is the reconciler's authenticated receipt plus this immutable source binding."""
    if not TASK_ID.fullmatch(str(ask)) or op not in ("status", "card", "complete"):
        raise Refused("invalid call-task operation")
    pipe = conn.pipeline(transaction=True)
    try:
        pipe.watch(TASK_KEY % ask, EXPIRY_KEY % ask)
        info = record(pipe.get(TASK_KEY % ask))
        if not info or info["ask"] != ask:
            raise Refused("task authority unavailable")
        if who != info["agent"]:
            raise Refused("task belongs to another governed principal")
        partition = _scope(pipe, info)
        if info["state"] == "OPEN" and (text(pipe.get(EXPIRY_KEY % ask)) != info["source_revision"]
                                          or now.timestamp() >= info["expires_at"]):
            info = _terminal(pipe, info, "EXPIRED", now)
            return public_receipt(info)
        if op == "status":
            return public_receipt(info)
        if not isinstance(supplied, dict) or supplied.get("source_revision") != info["source_revision"]:
            raise Refused("source revision conflict")
        # Exact idempotent operation effects can be read back after unknown return;
        # no old request can be reinterpreted with a different payload.
        fingerprint = digest({"op": op, "input": supplied})
        if info.get(op + "_op") == request_id:
            if info.get(op + "_request_hash") != fingerprint:
                raise Refused("operation payload conflict")
            return public_receipt(info)
        if info["state"] != "OPEN":
            return public_receipt(info)
        if type(supplied.get("task_revision")) is not int or supplied["task_revision"] != info["rev"]:
            raise Refused("task revision conflict")
        import canvas_publish
        if op == "card":
            card = supplied.get("card")
            field = ask + "-work"
            if not isinstance(card, dict) or card.get("ask") != ask or card.get("source_revision") != info["source_revision"]:
                raise Refused("card source binding missing")
            item = canvas_publish.clean_item(who, field, dump(card))
            if item is None:
                raise Refused("card is not publishable")
            changed = dict(info, rev=info["rev"]+1, field=field, card_hash=digest(item), card_op=request_id,
                           card_request_hash=fingerprint)
            pipe.multi()
            pipe.hset(partition, field, dump(card))
            pipe.set(TASK_KEY % ask, dump(changed))
            pipe.hset(OPEN_KEY, ask, dump(changed))
            _audit(pipe, changed, op, request_id, who, now)
            _execute(pipe)
            return public_receipt(changed)
        field = info.get("field")
        item = canvas_publish.clean_item(who, field or "", pipe.hget(partition, field)) if field else None
        if not item or digest(item) != info.get("card_hash") or supplied.get("card_hash") != info["card_hash"]:
            raise Refused("exact stored artifact unavailable")
        # Preserve the existing ask-to-card observation before OPEN is removed.
        # Its key is WATCHed and the same first-card/settled schema is retained.
        timing_key, timing = _completion_timing(pipe, info, item, now)
        changed = dict(info, state="DONE", rev=info["rev"]+1, closed_at=stamp(now), complete_op=request_id,
                       completion_op=request_id, complete_request_hash=fingerprint)
        pipe.multi()
        if timing is not None:
            pipe.set(timing_key, dump(timing), ex=172800)
        pipe.set(TASK_KEY % ask, dump(changed))
        pipe.hdel(OPEN_KEY, ask)
        pipe.hdel(RING_KEY, ask)
        # Only authoritative completion, not the untrusted free-text RESULT lane,
        # may replace the bridge's own card. The worker artifact is untouched.
        pipe.hdel(partition, ask)
        _audit(pipe, changed, op, request_id, who, now)
        _execute(pipe)
        return public_receipt(changed)
    finally:
        pipe.reset()

def _completion_timing(pipe, info, item, now):
    """Existing bridge timing schema, observed exactly once before final closure."""
    key = "gov:callbridge:latency:%s" % info["ask"]
    pipe.watch(key)
    _types(pipe, {key: "string"})
    raw = pipe.get(key)
    try:
        previous = json.loads(text(raw)) if raw else None
        if isinstance(previous, dict) and previous.get("card"):
            return key, None
        claimed = dt.datetime.strptime(info["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        asked = (dt.datetime.strptime(info["asked_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)
                 if info.get("asked_at") else claimed)
    except (TypeError, ValueError, KeyError):
        raise Refused("existing timing evidence unavailable") from None
    waited = max(0.0, (now - asked).total_seconds())
    measured = {"agent": info["agent"], "asked_at": stamp(asked), "claimed_at": info["claimed_at"],
                "read_at": stamp(now), "seconds": int(waited), "waited": round(waited, 3),
                "settled_at": stamp(now), "card": info["field"], "card_ts": item["ts"],
                "on_time": waited <= 180}
    wake_raw = pipe.get("gov:callbridge:wake:%s" % info["ask"])
    try:
        wake = json.loads(text(wake_raw)) if wake_raw else None
    except (TypeError, ValueError):
        wake = None
    if isinstance(wake, dict):
        measured["wake"] = wake.get("state", "")
    return key, measured


def managed(field, raw=None):
    """Use the same pure admission classifier without coupling other images to us."""
    import redis_gov
    return redis_gov.managed_canvas(field, raw)


def refresh_claim_card(conn, ask, card, now):
    """Bridge-only display refresh. No generic writer can call this through bus.
    Preserve display behavior without reviving a revoked task or losing bindings.
    A display refresh does not advance the task's work revision."""
    if not TASK_ID.fullmatch(str(ask)) or not isinstance(card, dict):
        raise Refused("invalid claim display")
    pipe = conn.pipeline(transaction=True)
    try:
        pipe.watch(TASK_KEY % ask, EXPIRY_KEY % ask)
        info = record(pipe.get(TASK_KEY % ask))
        if (not info or info["ask"] != ask or info["state"] != "OPEN" or now.timestamp() >= info["expires_at"]
                or text(pipe.get(EXPIRY_KEY % ask)) != info["source_revision"]):
            return False
        partition = _scope(pipe, info)
        import canvas_publish
        bound = dict(card, ask=ask, source_revision=info["source_revision"])
        item = canvas_publish.clean_item(info["agent"], ask, dump(bound))
        if item is None:
            raise Refused("claim display is not publishable")
        changed = dict(info, claim_hash=digest(item))
        pipe.multi()
        pipe.hset(partition, ask, dump(bound))
        pipe.set(TASK_KEY % ask, dump(changed))
        pipe.hset(OPEN_KEY, ask, dump(changed))
        _execute(pipe)
        return True
    finally:
        pipe.reset()


def visible(conn, agent, field, raw, *, room, now):
    """Independent publication fence for the actual partition and exact artifact.
    It does not drain an older external upload already in flight."""
    try:
        card = json.loads(text(raw))
        field = text(field)
        if not isinstance(card, dict):
            return False
        if not managed(field, raw):
            return True
        # Derive identity from the actual field, never from caller metadata.
        match = re.fullmatch(r"(call-[0-9a-f]{12})(-work)?", field)
        if match is None:
            return False
        ask, work = match.group(1), bool(match.group(2))
        if card.get("ask") != ask:
            return False
        info = get(conn, ask)
        if (not info or info["state"] not in ("OPEN", "DONE") or info["agent"] != agent
                or info["room"] != room or card.get("source_revision") != info["source_revision"]):
            return False
        if info["state"] == "OPEN" and (now.timestamp() >= info["expires_at"]
                or text(conn.get(EXPIRY_KEY % ask)) != info["source_revision"]):
            return False
        import canvas_publish
        item = canvas_publish.clean_item(agent, field, raw)
        if item is None:
            return False
        if work:
            return info.get("field") == field and digest(item) == info.get("card_hash")
        return info["state"] == "OPEN" and digest(item) == info.get("claim_hash")
    except (TypeError, ValueError, UnicodeError, KeyError, AttributeError):
        return False

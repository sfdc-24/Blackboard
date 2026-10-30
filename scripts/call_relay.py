"""The call relay: every ask the call bridge claims reaches its agent's reader, and no ask waits in silence.

THE ORDER (the owner, 2026-10-30): conference work must not wait on a chair, and agents must deliver, not only
promise. Contract: docs/CALL-DELIVERY-LOOP.md (PR 439). Measured gap: the bridge (call_bridge.py) puts the ask on
fleet:inbox:<agent> in Redis, and nothing reads those inboxes for Gemini, Grok or Codex, so their cards stay
"claimed" for ever.

WHAT IT DOES, each tick, for every open ask in gov:callbridge:open (the bridge's record, read only):
    pointed   One board row per ask, addressed to the agent, carrying the call id, the inbox key and the due time,
              and NEVER the owner's words (the board is a public sheet; the words stay in Redis, as the bridge
              decided). Every agent already reads the board, so this is the ring that reaches all of them.
    overdue   An ask still on its claimed card 180 s after the claim gets one overdue row, during the call, not
              after it. It is the second ring, with its own row id, so it also reaches an agent whose pointer was
              lost.
    expired   When the CALL the ask came from has ended, by the chair's own record (v1:conf:<room>:state closed=1
              for the ask's session_epoch, or the room moved to a newer epoch), an ask whose card is still the
              bridge's claim becomes a terminal "Not started: the call ended" card, leaves gov:callbridge:open and
              gets one expires= row. A closed publish window is NOT evidence that a call ended (Codex on 11b0c0fa),
              and a chair that died never closes its record: such an ask stays open and goes overdue, never
              expired. A card the agent already moved (working, review, done) is never touched.

THE LINES IT DOES NOT CROSS
    It never writes answers=. The bridge closes an ask on any answers= row from its agent (settle), so a relay
    row carrying one would mark undone work done. Its rows use pointer, overdue and expires only.
    It never writes the owner's words anywhere.
    It writes only while this run holds gov:worker:lock (`holds`), the same single-writer rule the bridge and the
    publisher follow, and its expiry WATCHes that lock, so a run that lost it between the check and the write
    writes nothing.

NEVER TWICE (Codex on 11b0c0fa). An append that looked failed may land later; the board has no dedupe. So each
relay row id is attempted AT MOST ONCE. A pointer is reserved in gov:callrelay:pointer:<id> with a lease before its
single attempt and marked posted after. After the lease a reconciler asks the board (`on_board`, an exact Row_ID
read): present means posted; absent means LOST, recorded and never retried, because the attempt may still land;
unreadable leaves it alone. A lost pointer is covered by the overdue row, which has its own id.

BEST EFFORT, STATED (Codex on 11b0c0fa). Overdue and expiry rows take their NX mark before their single attempt, so
a crash in between loses that one notice rather than risking a duplicate. The expired CARD is the durable record
of an expiry; the row is the announcement.

THE FAILURE SIGNAL IS SystemExit (Cursor on 11b0c0fa). append.py ends an unconfirmed or duplicate append with
sys.exit, and the live run's appender re-raises it; bus.read_rows raises SystemExit when the bus never answers.
Every relay boundary catches it, so one bad append never ends the relay thread silently.
"""
from __future__ import annotations

import datetime
import json
import threading

import call_bridge
import canvas_publish

POINTER_KEY = "gov:callrelay:pointer:%s"
OVERDUE_KEY = "gov:callrelay:overdue:%s"
EXPIRED_KEY = "gov:callrelay:expired:%s"
WORKER_LOCK_KEY = "gov:worker:lock"
STATE_KEY = "v1:conf:%s:state"             # the conference chair's record (conf_chair/redis_state.py)
TTL_SECONDS = call_bridge.TTL_SECONDS
LEASE_SECONDS = 120
OVERDUE_SECONDS = 180
TICK_SECONDS = 5
SOURCE = "bus-reconciler"
# Who reads each agent's work on the board: the tags call_bridge.ANSWERS_FROM accepts, the primary one first.
TO_OF = {"grok": "grok", "claude-code-cli": "claude-code-cli",
         "chatgpt-codex-desktop": "aya,chatgpt-codex-desktop", "gemini": "gemini"}
# The outcome owner sees every overdue ask (contract section 3).
OVERDUE_CC = "aya"
# append.py and bus.read_rows signal failure with SystemExit; a KeyboardInterrupt still stops the process.
FAILURES = (Exception, SystemExit)


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _parse(stamp_text):
    return datetime.datetime.strptime(stamp_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def row_id(kind: str, ask: str) -> str:
    """CALLRELAY-<KIND>-<call id>: stable, so the board can be asked whether it is there."""
    return "CALLRELAY-%s-%s" % (kind.upper(), ask)


def _to(agent: str) -> str:
    return TO_OF.get(agent, agent)


def pointer_spec(ask: str, info: dict) -> dict:
    agent = info["agent"]
    due = _parse(info["claimed_at"]) + datetime.timedelta(seconds=OVERDUE_SECONDS)
    rid = row_id("pointer", ask)
    payload = ("BCB|v=1|id=%s|phase=DISPATCH|class=BUILD|from=%s|to=%s|call=%s|inbox=fleet:inbox:%s|room=%s"
               "|claimed_at=%s|due=%s|do=read the ask in your inbox by its call id; deliver the requested thing as "
               "your own canvas card at that id, then post answers=%s; if a model cannot do it, post a review card "
               "and routes=%s, never answers=" % (
                   rid, SOURCE, _to(agent), ask, agent, info["room"], info["claimed_at"],
                   call_bridge.stamp(due), ask, ask))
    return {"row_id": rid, "source_tag": SOURCE, "target_surface": _to(agent).replace(",", ";"),
            "action_type": "DISPATCH", "category": "OPEN", "payload": payload, "gist": "call ask %s" % ask}


def overdue_spec(ask: str, info: dict, now) -> dict:
    agent = info["agent"]
    rid = row_id("overdue", ask)
    payload = ("BCB|v=1|id=%s|phase=DISPATCH|class=BUILD|from=%s|to=%s|cc=%s|call=%s|inbox=fleet:inbox:%s|overdue=1"
               "|claimed_at=%s|now=%s|note=no card movement %ds after the claim; the owner's ask is still waiting" % (
                   rid, SOURCE, _to(agent), OVERDUE_CC, ask, agent, info["claimed_at"], call_bridge.stamp(now),
                   OVERDUE_SECONDS))
    return {"row_id": rid, "source_tag": SOURCE,
            "target_surface": ";".join(dict.fromkeys(_to(agent).split(",") + [OVERDUE_CC])),
            "action_type": "DISPATCH", "category": "OPEN", "payload": payload, "gist": "call ask %s overdue" % ask}


def expired_spec(ask: str, info: dict, now) -> dict:
    agent = info["agent"]
    rid = row_id("expired", ask)
    payload = ("BCB|v=1|id=%s|phase=RESULT|class=NOTE|from=%s|to=%s|call=%s|expires=%s|claimed_at=%s|now=%s"
               "|note=the call ended before this ask was started; it is closed and nothing will pick it up" % (
                   rid, SOURCE, _to(agent), ask, ask, info["claimed_at"], call_bridge.stamp(now)))
    return {"row_id": rid, "source_tag": SOURCE, "target_surface": _to(agent).replace(",", ";"),
            "action_type": "RESULT", "category": "DONE", "payload": payload, "gist": "call ask %s expired" % ask}


def expired_card(agent: str, claimed_at, now, version: int) -> dict:
    name = call_bridge.NAME_OF.get(agent, agent)
    return {"v": 1, "type": "note", "status": "review", "public": True, "version": version, "ts": call_bridge.stamp(now),
            "activity": "call ended before work started", "title": "Not started: the call ended",
            "body": "Asked in the room at %s. The call ended before %s started this, so it is closed and not done." % (
                call_bridge.eastern(claimed_at), name)}


def call_ended(state, info: dict) -> bool:
    """The chair's own record says the ask's call is over. Unknown is NOT ended: an ask with no conf_room or
    session_epoch (claimed before the bridge recorded them), or a room with no state, never expires."""
    epoch = str(info.get("session_epoch") or "")
    if not info.get("conf_room") or not epoch or not state:
        return False
    fields = {_text(k): _text(v) for k, v in state.items()}
    if fields.get("closed") == "1" and fields.get("closed_epoch") == epoch:
        return True
    current = fields.get("session_epoch") or ""
    return bool(current) and current != epoch


def board_lookup(env):
    """on_board(row_id) for the live run: True or False from a filtered board read, None when the read fails.
    bus.read_rows' match is a substring over the whole row, so the Row_ID is compared exactly. read_rows signals
    failure with SystemExit, which is caught here: an unreadable board is unknown, never absent."""
    import bus                                                            # noqa: PLC0415

    def on_board(rid):
        try:
            rows = bus.read_rows(env, match=rid)["rows"]
        except FAILURES:
            return None
        return any(str((list(row) + [""])[0]).strip() == rid for row in rows)
    return on_board


class Relay:
    def __init__(self, conn, log, holds, append, on_board, clock=None):
        """append(spec) writes one row once (raises, including SystemExit, when unconfirmed). on_board(row_id) is
        True, False, or None when the board could not be read."""
        self.conn, self.log, self.holds, self.append, self.on_board = conn, log, holds, append, on_board
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.stop = threading.Event()
        self.thread = None

    def open_asks(self) -> dict:
        out = {}
        for ask, raw in (self.conn.hgetall(call_bridge.OPEN_KEY) or {}).items():
            try:
                info = json.loads(_text(raw))
                _parse(info["claimed_at"])
                if info["agent"] and info["room"]:
                    out[_text(ask)] = info
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def card(self, ask: str, info: dict, reader=None):
        raw = _text((reader or self.conn).hget(canvas_publish.PARTITION % (info["room"], info["agent"]), ask))
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def state(self, info: dict, reader=None):
        if not info.get("conf_room"):
            return None
        return (reader or self.conn).hgetall(STATE_KEY % info["conf_room"]) or None

    @staticmethod
    def untouched(card) -> bool:
        """Still the bridge's claimed card, version 1: the agent has not moved it."""
        return card is None or (card.get("status") == "claimed" and card.get("version") == 1)

    def tick(self) -> dict:
        now = self.clock()
        done = {"pointed": [], "overdue": [], "expired": []}
        if not self.holds():
            return done
        for ask, info in sorted(self.open_asks().items()):
            if not self.holds():
                break
            card = self.card(ask, info)
            if call_ended(self.state(info), info):
                # The call is over: an ask nobody started closes; started work keeps its own path. Either way no
                # new pointer and no overdue: nobody is in the room to wait.
                if self.untouched(card) and self._expire(ask, info, now):
                    done["expired"].append(ask)
                continue
            if self._point(ask, info, now):
                done["pointed"].append(ask)
            if self.untouched(card) and now - _parse(info["claimed_at"]) >= datetime.timedelta(seconds=OVERDUE_SECONDS):
                if self._once(OVERDUE_KEY % ask, lambda: self.append(overdue_spec(ask, info, now))):
                    done["overdue"].append(ask)
        return done

    def _once(self, key: str, write) -> bool:
        """Best effort, at most once: the NX mark goes first, so a crash after it costs that one notice, never a
        duplicate (module note)."""
        if not self.conn.set(key, call_bridge.stamp(self.clock()), nx=True, ex=TTL_SECONDS):
            return False
        try:
            write()
        except FAILURES as error:
            self.log.warning("call relay: %s not confirmed (%s); not retried", key, type(error).__name__)
            return False
        return True

    def _point(self, ask: str, info: dict, now) -> bool:
        key = POINTER_KEY % ask
        raw = _text(self.conn.get(key))
        state = json.loads(raw) if raw else None
        if state is None:
            lease = {"phase": "reserved", "lease_until": call_bridge.stamp(now + datetime.timedelta(seconds=LEASE_SECONDS))}
            if not self.conn.set(key, json.dumps(lease, sort_keys=True), nx=True, ex=TTL_SECONDS):
                return False
            return self._send_pointer(key, ask, info)
        if state.get("phase") != "reserved":
            return False                       # posted, or lost: either way it is never attempted again
        if _parse(state["lease_until"]) > now:
            return False                       # inside the lease its one attempt may still be in flight
        found = self.on_board(row_id("pointer", ask))
        if found is None:
            return False                       # the board did not answer: neither posted nor absent is known
        phase = "posted" if found else "lost"
        self.conn.set(key, json.dumps({"phase": phase}, sort_keys=True), ex=TTL_SECONDS)
        if not found:
            self.log.warning("call relay: pointer for %s not on the board after its lease; not retried, the overdue "
                             "row is the second ring", ask)
        return False

    def _send_pointer(self, key: str, ask: str, info: dict) -> bool:
        try:
            self.append(pointer_spec(ask, info))
        except FAILURES as error:
            # Unconfirmed is not absent: the reservation stays, and only the reconciler decides, after the lease.
            self.log.warning("call relay: pointer for %s not confirmed (%s)", ask, type(error).__name__)
            return False
        self.conn.set(key, json.dumps({"phase": "posted"}, sort_keys=True), ex=TTL_SECONDS)
        self.log.info("call relay: %s pointed to %s", ask, info["agent"])
        return True

    def _expire(self, ask: str, info: dict, now) -> bool:
        partition = canvas_publish.PARTITION % (info["room"], info["agent"])
        claimed_at = _parse(info["claimed_at"])
        pipe = self.conn.pipeline(transaction=True)
        try:
            # Codex on 11b0c0fa: fence everything the decision rests on, the card, the open set, the worker lock
            # and the call's own record. Any of them changing before EXEC aborts the write.
            pipe.watch(partition, call_bridge.OPEN_KEY, WORKER_LOCK_KEY, STATE_KEY % info["conf_room"])
            mine = self.card(ask, info, reader=pipe)
            if (not self.untouched(mine) or pipe.hget(call_bridge.OPEN_KEY, ask) is None
                    or not self.holds() or not call_ended(self.state(info, reader=pipe), info)):
                return False
            version = (mine.get("version", 1) if isinstance(mine, dict) else 1) + 1
            pipe.multi()
            pipe.hset(partition, ask, json.dumps(expired_card(info["agent"], claimed_at, now, version), sort_keys=True))
            pipe.hdel(call_bridge.OPEN_KEY, ask)
            pipe.execute()
        except FAILURES as error:
            # A WatchError means someone moved the card, closed the ask, took the lock or reopened the call first:
            # theirs stands.
            self.log.info("call relay: %s not expired (%s)", ask, type(error).__name__)
            return False
        finally:
            pipe.reset()
        self._once(EXPIRED_KEY % ask, lambda: self.append(expired_spec(ask, info, now)))
        self.log.info("call relay: %s expired for %s", ask, info["agent"])
        return True

    def run(self, interval=TICK_SECONDS):
        while not self.stop.is_set():
            try:
                self.tick()
            except FAILURES as error:
                self.log.warning("call relay tick failed (%s)", type(error).__name__)
            self.stop.wait(interval)


def start(conn, log, holds, append, on_board, clock=None):
    relay = Relay(conn, log, holds, append, on_board, clock)
    relay.thread = threading.Thread(target=relay.run, name="call-relay", daemon=True)
    relay.thread.start()
    return relay

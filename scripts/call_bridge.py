"""The call bridge: the owner's spoken ask in the conference room becomes a claimed card, and a done card when answered.

THE ORDER (the owner, 2026-10-23 23:25 ET, CLAUDE-MOBILE-COMMAND-20261024T0335Z): his calls kept ending with "no work
happened during the call". Inside the existing bus-requests live run, nothing new to deploy beside it:
    read      v1:conf:<conference room>:events, where the chair projects each turn (conference chair/redis_state.py).
              With CONF_REDIS_TEXT=1 (his ruling for his own room) the owner's turn.started carries his words as
              `utterance` (conference chair/conf_chair/session.py, owner_said).
    claim     an utterance that NAMES an agent (the chair's own addressing: reason vocative or mention) with a task
              verb gives that agent, once per ask:
                - a "claimed" card in its canvas slot, conf:canvas:<canvas room>:<agent>, field call-<12 hex>, with ts;
                - the ask itself on its inbox, fleet:inbox:<agent> ({by, text, at}, scripts/redis_inbox.py).
    done      a board row FROM that agent whose answers= names the ask flips the card to "done" with the finish time.
              The rows are the ones the live loop has already read (serve_requests.one_pass): no extra board read.

WHAT GOES ON THE PUBLIC CARD, AND WHAT DOES NOT. The canvas file is world-readable (canvas_publish.py). His words can
name a client, its brands or its staff, and no list here could tell which words do. So the card carries WHO was asked
and WHEN, never WHAT: "Claimed by Codex, 10:02 ET". His words go only to the agent's inbox, in Redis, which every fleet
agent can already read. The agent's own card, posted under the canvas rules (In the making, then the real card), says
what the work is in words it chose for a public page.

WHEN IT RUNS. Only inside a live run, beside the canvas publisher, and only while the canvas room's publish window
(gov:canvas:publish:<room>) is open, so it follows the owner's sessions and nothing else. Before every write it asks
whether gov:worker:lock is still this run's (`holds`), as the publisher does.

AT MOST ONCE PER ASK. gov:callbridge:ask:<id> is taken with SET NX before anything is written, so a replayed entry, a
second run or a restart never claims twice. A stream seen for the first time is read from LOOKBACK_SECONDS back only:
an old call is never replayed into today's canvas. Each stream's place is kept in gov:callbridge:cursor:<room>.
Everything under gov:, which no agent may write (redis_gov.py).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import threading

import canvas_publish

try:                                                   # redis-py raises WatchError when a watched key changed
    from redis.exceptions import WatchError as _WatchError
    WATCH_ERRORS = (_WatchError,)
except ImportError:                                    # no client installed (offline tests use a fake)
    WATCH_ERRORS = ()

STREAM_MATCH = "v1:conf:*:events"
CONF_ROOM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CURSOR_KEY = "gov:callbridge:cursor:%s"
ASK_KEY = "gov:callbridge:ask:%s"
OPEN_KEY = "gov:callbridge:open"
INBOX_KEY = "fleet:inbox:%s"
BY = "call-bridge"
CANVAS_ROOM = "workshop"
TTL_SECONDS = 2 * 24 * 60 * 60
LOOKBACK_SECONDS = 300
TICK_SECONDS = 5
FLIP_TRIES = 3
PAGE = 200
ASK_CHARS = 1000

# The room's voices, by the chair's persona ids, and the fleet agent each one's work goes to: the canonical ids of
# fleet:inbox (cloud/drive-intake ALIASES, agreed 2026-10-09) and of the canvas slots (canvas_publish.AGENTS).
AGENT_OF = {"grok": "grok", "claude": "claude-code-cli", "codex": "chatgpt-codex-desktop", "gemini": "gemini"}
NAME_OF = {"grok": "Grok", "claude-code-cli": "Claude", "chatgpt-codex-desktop": "Codex", "gemini": "Gemini"}
# The board tags each agent posts its answers under (the board's tags, scripts/agent_roster.json and the aliases).
ANSWERS_FROM = {
    "grok": ("grok", "grok-bot"),
    "claude-code-cli": ("claude-code-cli", "claude", "claude-mobile", "claude-api", "vm-claude-code-cli"),
    "chatgpt-codex-desktop": ("chatgpt-codex-desktop", "codex", "aya"),
    "gemini": ("gemini",),
}
# Each voice's name and the spellings the chair's addressing accepts for it (conference addressing.HEARD_AS).
NAMES = {persona: re.compile(r"(?i)\b(?:%s)\b(?!['’]s\b)" % "|".join(spellings)) for persona, spellings in {
    "grok": ("grok", "grock", "groq"), "claude": ("claude", "claud"), "codex": ("codex", "kodex"),
    "gemini": ("gemini", "jemini", "gemeni")}.items()}
# The chair names an agent with one of these (addressing.py). "everyone", "room", "host" and "continuity" name nobody.
NAMED = ("vocative", "mention", "view_request")
# A task, not a question: one of these words anywhere in what he said.
TASK = re.compile(
    r"(?i)\b(?:build|draft|make|create|write|design|mock(?:\s+up)?|prototype|add|fix|check|review|draw|diagram|"
    r"sketch|list|find|research|compare|summari[sz]e|update|set\s+up|put|post|show\s+me|work\s+on|look\s+into|"
    r"prepare|plan|test|verify|deploy|open|wire|map|outline|estimate)\b")


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _fields(raw) -> dict:
    return {_text(k): _text(v) for k, v in (raw or {}).items()}


def stamp(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def eastern(moment) -> str:
    """HH:MM in Toronto time, the clock the owner reads. EDT until Nov 1 2026, then EST (no tz database needed)."""
    edt = datetime.datetime(2026, 3, 8, 7, tzinfo=datetime.timezone.utc) <= moment < datetime.datetime(
        2026, 11, 1, 6, tzinfo=datetime.timezone.utc)
    local = moment + datetime.timedelta(hours=-4 if edt else -5)
    return local.strftime("%H:%M") + " ET"


def ask_id(room: str, entry_id: str, agent: str) -> str:
    """call-<12 hex>: one ask, one agent. Fits canvas_publish.ITEM_ID."""
    return "call-" + hashlib.sha256(("%s|%s|%s" % (room, entry_id, agent)).encode("utf-8")).hexdigest()[:12]


def asks_in(fields: dict) -> list:
    """The agents an event asks for work, or []: an owner turn naming them, with a task verb in his words."""
    if fields.get("kind") != "turn.started" or fields.get("actor_id") in ("", None, "chair"):
        return []
    if fields.get("reason") not in NAMED:
        return []
    said = fields.get("utterance") or ""
    if not said.strip() or not TASK.search(said):
        return []
    # The projection keeps no persona list (redis_state.METADATA_FIELDS), so the names are read from his words, as the
    # chair's addressing spells them (HEARD_AS), never a possessive ("Codex's PR" names nobody).
    found = sorted((match.start(), AGENT_OF[persona]) for persona, pattern in NAMES.items()
                   for match in pattern.finditer(said))
    agents = []
    for _, agent in found:
        if agent not in agents:
            agents.append(agent)
    return agents


def claimed_card(agent: str, now) -> dict:
    name = NAME_OF[agent]
    return {"v": 1, "type": "note", "status": "claimed", "public": True, "version": 1, "ts": stamp(now),
            "activity": "claimed in the room", "title": "Claimed by %s, %s" % (name, eastern(now)),
            "body": "Asked in the room at %s. %s's card for this work follows here when it is done." % (eastern(now), name)}


def done_card(agent: str, claimed_at, now) -> dict:
    name = NAME_OF[agent]
    minutes = max(0, int(round((now - claimed_at).total_seconds() / 60)))
    return {"v": 1, "type": "note", "status": "done", "public": True, "version": 2, "ts": stamp(now),
            "activity": "answered on the board", "title": "Done by %s, %s" % (name, eastern(now)),
            "body": "Asked in the room at %s, answered on the board at %s (%d min)." % (
                eastern(claimed_at), eastern(now), minutes)}


def inbox_text(ask: str, agent: str, room: str, said: str, now) -> str:
    return ("CALL ASK %s from the owner, said in the conference room at %s (%s): %s -- Answer with a board row "
            "answers=%s. Your canvas card %s in conf:canvas:%s:%s turns done when that row lands; post your own "
            "card under the canvas rules for what the work is." % (
                ask, eastern(now), stamp(now), said[:ASK_CHARS].replace("|", "/"), ask, ask, room, agent))


class Bridge:
    def __init__(self, conn, log, holds, clock=None, canvas_room=CANVAS_ROOM):
        self.conn, self.log, self.holds = conn, log, holds
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.canvas_room = canvas_room
        self.stop = threading.Event()
        self.thread = None

    def window_open(self, now) -> bool:
        return any(room == self.canvas_room for room, _ in canvas_publish.active_windows(self.conn, now))

    def streams(self) -> list:
        rooms = []
        for key in self.conn.scan_iter(match=STREAM_MATCH, count=500):
            parts = (_text(key) or "").split(":")
            if len(parts) == 4 and CONF_ROOM.fullmatch(parts[2]):
                rooms.append(parts[2])
        return sorted(set(rooms))

    def tick(self) -> list:
        """One pass over every conference stream. Returns the asks claimed."""
        now = self.clock()
        if not self.window_open(now) or not self.holds():
            return []
        claimed = []
        for room in self.streams():
            claimed.extend(self._room(room, now))
        return claimed

    def _room(self, room: str, now) -> list:
        key = "v1:conf:%s:events" % room
        cursor = _text(self.conn.get(CURSOR_KEY % room))
        if cursor:
            entries = self.conn.xrange(key, min=cursor, max="+", count=PAGE + 1)
        else:
            start = int((now.timestamp() - LOOKBACK_SECONDS) * 1000)
            entries = self.conn.xrange(key, min="%d-0" % max(0, start), max="+", count=PAGE)
        claimed = []
        for entry_id, raw in entries or ():
            entry_id = _text(entry_id)
            if entry_id == cursor:
                continue
            for agent in asks_in(_fields(raw)):
                if not self.holds():
                    return claimed
                fields = _fields(raw)
                ask = self._claim(room, entry_id, agent, fields.get("utterance") or "", now,
                                  epoch=fields.get("session_epoch") or "")
                if ask:
                    claimed.append(ask)
            self.conn.set(CURSOR_KEY % room, entry_id, ex=TTL_SECONDS)
        return claimed

    def _claim(self, room, entry_id, agent, said, now, epoch=""):
        ask = ask_id(room, entry_id, agent)
        if not self.conn.set(ASK_KEY % ask, stamp(now), nx=True, ex=TTL_SECONDS):
            return None                                      # claimed before: never twice
        pipe = self.conn.pipeline(transaction=True)
        try:
            pipe.multi()
            pipe.hset(canvas_publish.PARTITION % (self.canvas_room, agent), ask,
                      json.dumps(claimed_card(agent, now), sort_keys=True))
            pipe.xadd(INBOX_KEY % agent, {"by": BY, "text": inbox_text(ask, agent, self.canvas_room, said, now),
                                          "at": stamp(now)})
            # conf_room and session_epoch name the CALL the ask came from (the canvas room is shared), so the
            # call relay can close it on that call's own end (docs/CALL-DELIVERY-LOOP.md). Additive: settle
            # reads agent, claimed_at and room only.
            pipe.hset(OPEN_KEY, ask, json.dumps({"agent": agent, "claimed_at": stamp(now), "room": self.canvas_room,
                                                 "conf_room": room, "session_epoch": epoch}, sort_keys=True))
            pipe.expire(OPEN_KEY, TTL_SECONDS)
            pipe.execute()
        finally:
            pipe.reset()
        self.log.info("call bridge: %s claimed for %s", ask, agent)
        return ask

    def settle(self, rows) -> list:
        """Flip the open asks a board row from their agent answers. `rows` are the live loop's board rows."""
        if not self.holds():
            return []
        stored = _fields(self.conn.hgetall(OPEN_KEY))
        if not stored:
            return []
        now = self.clock()
        done = []
        for row in rows or ():
            payload, sender = _row(row)
            answered = [part.strip() for part in re.split(r"[;,\s]+", _field(payload, "answers")) if part.strip()]
            for ask in answered:
                if ask not in stored or ask in done:
                    continue
                try:
                    info = json.loads(stored[ask])
                    claimed_at = datetime.datetime.strptime(info["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                    agent, room = info["agent"], info["room"]
                except (KeyError, TypeError, ValueError):
                    continue
                # Its own agent's answer only: the row's tag, or the from= it signs with, is one of that agent's.
                tags = ANSWERS_FROM.get(agent, ())
                if sender not in tags and (_field(payload, "from") or "").strip().lower() not in tags:
                    continue
                self._flip(ask, agent, room, claimed_at, now)
                done.append(ask)
        return done

    def _flip(self, ask, agent, room, claimed_at, now) -> None:
        """Close the ask; write the generic done card only over the bridge's own claim, decided under WATCH.

        Cursor on 11b0c0fa: a decision from a read outside the transaction could write the done card over a
        terminal card another writer (the call relay's expiry) committed in between. The card is re-read under
        WATCH, and EXEC fails if it changed; a few retries keep a busy key from dropping the flip."""
        partition = canvas_publish.PARTITION % (room, agent)
        for _attempt in range(FLIP_TRIES):
            pipe = self.conn.pipeline(transaction=True)
            try:
                pipe.watch(partition)
                current = _text(pipe.hget(partition, ask))
                try:
                    mine = json.loads(current) if current else None
                except ValueError:
                    mine = None
                pipe.multi()
                # The agent's own card under the same id, posted after the claim, is never overwritten.
                if mine is None or (mine.get("status") == "claimed" and mine.get("version") == 1):
                    pipe.hset(partition, ask, json.dumps(done_card(agent, claimed_at, now), sort_keys=True))
                pipe.hdel(OPEN_KEY, ask)
                pipe.execute()
                break
            except WATCH_ERRORS:
                continue                                     # the card moved: decide again on the new value
            finally:
                pipe.reset()
        else:
            self.log.warning("call bridge: %s not flipped, card kept changing", ask)
            return
        self.log.info("call bridge: %s done for %s", ask, agent)

    def run(self, interval=TICK_SECONDS):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:                                   # noqa: BLE001
                # A Redis flap costs one tick, never the meeting.
                self.log.warning("call bridge tick failed (%s)", type(error).__name__)
            self.stop.wait(interval)


def _row(row):
    """(payload, sender tag) of a board row: bus_request's columns."""
    import bus_request                                                   # noqa: PLC0415
    if not isinstance(row, list) or len(row) <= bus_request.PAYLOAD:
        return "", ""
    return str(row[bus_request.PAYLOAD] or ""), str(row[bus_request.SOURCE] or "").strip().lower()


def _field(payload, name):
    import bus_request                                                   # noqa: PLC0415
    return bus_request.field(payload, name)


def start(conn, log, holds):
    """The bridge on its own thread, or None without a way to ask for the lease."""
    if holds is None:
        return None
    bridge = Bridge(conn, log, holds)
    thread = threading.Thread(target=bridge.run, name="call-bridge", daemon=True)
    thread.start()
    bridge.thread = thread
    return bridge

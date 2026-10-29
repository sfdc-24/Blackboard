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

A FOLLOW-UP COUNTS TOO (the owner's live call, 2026-10-24 ~13:05 ET, CLAUDE-MOBILE-BRIDGE-CONTINUITY-20261024T1718Z):
"I know, but it's a simple HTML update. You just center it..." went to the voice holding the floor (reason continuity)
and named nobody, so no card went up. A continuity turn with a task word and no name is that voice's ask: the actor of
the next floor.granted in the same turn_id, read ahead within the page; else the last voice granted the floor in this
room, kept in gov:callbridge:last:<room> for LAST_SECONDS. Neither known: nothing is claimed.

A VOICE THAT SAYS IT CANNOT CLAIMS THE ASK (the owner's demo, 2026-10-28 09:14 ET,
CLAUDE-ACTIONS-1313Z-20261028T1328Z: an ask for a picture got no claim and the voice told his guest "I can't pull an
image"). The owner's last turn with words is kept in gov:callbridge:asked:<room> for ASKED_SECONDS. A line the chair
wrote to conf:side:<yyyymmdd> for one of the room's voices (by=chair, voice, room, text: conference
chair/conf_chair/side_lines.py) that says "I can't", "I cannot", "I'm not able", "I'm unable", "I don't have the
tools" or "I have no way" claims that turn for the voice's agent, under the same ask id a named claim of that turn
would have, so the two are one claim. Read from gov:callbridge:side:<room>. It needs the chair's side lines ON for the
room (CONF_REDIS_SIDE_VOICE, CONF_REDIS_SIDE_VOICE_ROOMS); with them off there is nothing to read and this claims
nothing.

WHAT GOES ON THE PUBLIC CARD, AND WHAT DOES NOT. The canvas file is world-readable (canvas_publish.py). His words can
name a client, its brands or its staff. On that call he saw only empty "Claimed by" shells and asked to see what the
work is, so the card carries a one-line SUMMARY built from his words by an allowlist, never a blocklist: from the start
of the sentence that holds the task word, at most SUMMARY_WORDS words and SUMMARY_CHARS characters, and a word passes
only if it is in PLAIN (common English and build words, none that is commonly a name on its own) and is not in Title Case
after a sentence's first word. Anything else, a name, a brand, a place, a possessive, a long number, becomes one "...".
So "Codex, draft the price sheet for the client's Port-au-Prince store" is published as "Draft the price sheet for the
... store". His full words go only to the
agent's inbox, in Redis, which every fleet agent can already read; the agent's own card, posted under the canvas rules,
says the rest in words it chose for a public page.

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

STREAM_MATCH = "v1:conf:*:events"
CONF_ROOM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CURSOR_KEY = "gov:callbridge:cursor:%s"
LAST_KEY = "gov:callbridge:last:%s"
ASK_KEY = "gov:callbridge:ask:%s"
OPEN_KEY = "gov:callbridge:open"
INBOX_KEY = "fleet:inbox:%s"
BY = "call-bridge"
CANVAS_ROOM = "workshop"
TTL_SECONDS = 2 * 24 * 60 * 60
LOOKBACK_SECONDS = 300
TICK_SECONDS = 5
PAGE = 200
ASK_CHARS = 1000
LAST_SECONDS = 15 * 60
SUMMARY_WORDS = 14
SUMMARY_CHARS = 70          # with "claimed: " it fits canvas_publish.ACTIVITY_MAX (80)
FOLLOW = "continuity"
ASKED_KEY = "gov:callbridge:asked:%s"
SIDE_CURSOR_KEY = "gov:callbridge:side:%s"
SIDE_KEY = "conf:side:%s"
ASKED_SECONDS = 3 * 60
SIDE_BY = "chair"
LATENCY_KEY = "gov:callbridge:latency:%s"
TARGET_SECONDS = 3 * 60     # O3: the agent's own card within 3 minutes of the ask
# THE DOORBELL (O3 gap 1, the owner's orders of 2026-10-28: "the real agent wakes"). fleet:inbox is passive: nothing
# reads it on a push. Every doorbell the fleet already runs reads the BOARD: claude-code-cli's claude_cli_inbox.py
# --wait, the cloud agent-waker jobs (agent_waker.addressed_to) and scripts/fleet_doorbell.py --agent grok|codex|
# gemini. So a claim also rings the agent with ONE board row addressed to its tag, Row_ID WAKE_PREFIX + ask, through
# the worker's own append path (append.py and its read-back), never retried: gov:callbridge:rung:<ask> is taken
# with SET NX before the append is tried. Queued in RING_KEY by the claim, sent by the next tick. Only when the run
# was given a `ring` (serve_requests: CALL_BRIDGE_RING=on); otherwise nothing is queued and nothing changes.
# The row carries the allowlisted summary only, never his words: those stay in fleet:inbox.
RING_KEY = "gov:callbridge:ring"
RUNG_KEY = "gov:callbridge:rung:%s"
WAKE_PREFIX = "CALLBRIDGE-WAKE-"
RING_MAX = 10              # rows rung per tick at most; the rest wait for the next tick

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
    r"prepare|plan|test|verify|deploy|open|wire|map|outline|estimate|"
    r"cent(?:er|re)|align|remove|delete|replace|edit|rename|resize|"
    # The owner's demo, 2026-10-28 09:14 ET (CLAUDE-ACTIONS-1313Z-20261028T1328Z): an ask for a picture of a chair got
    # no claim, and the voice told his guest "I can't pull an image". Asking for something to be shown counts.
    r"pull|get|bring|show|pictures?|images?)\b")


# A VOICE SAYING IT CANNOT (refusal). The chair writes every line a voice speaks to conf:side:<yyyymmdd> (conference
# chair/conf_chair/side_lines.py: by=chair, voice=<persona id>, room, text). When a voice answers the owner's last turn
# with one of these, his ask is that voice's work: it is claimed as if he had named it, under the same ask id, so a
# named claim and a refusal claim of the same turn are one claim. Only the first person: "Codex can't" is not a refusal.
REFUSAL = re.compile(
    r"(?i)\bI(?:\s+(?:can(?:no|['’]|\s+no)t|am\s+(?:not\s+able|unable)|"
    r"do\s+not\s+have\s+(?:a\s+way|(?:the\s+)?(?:tools?|ability|access|means))|"
    r"don['’]t\s+have\s+(?:a\s+way|(?:the\s+)?(?:tools?|ability|access|means))|have\s+no\s+way)|"
    r"['’]m\s+(?:not\s+able|unable))\b")


# THE SUMMARY'S WORDS (summary). Common English and the words of building a page or a plan. Left out on purpose: words
# that are commonly a first name or a brand on their own (will, may, mark, bill, rose, grace, hope, joy, apple, ...). A
# word is one reviewed entry here: one that is missing costs a "..." on a card, never a leak.
PLAIN = frozenset("""
a an the this that these those it its it's i me my we us our you your they them their
and or but so if then than as of to in on at by for with from into onto over under about after before between
up down out off all any each every some more most less few other another same such only just also too very not no
is are was were be been being am do does did done doing have has had having can could would should must
please now here there again still both either new old next last first second third one two three four five ten
okay ok yes yeah know need needs want wants like think let let's go going gonna don't doesn't can't won't isn't
ask asks asked tell say
make made build add put get set show give take keep move change update fix check review test verify draft write
create design mock prototype draw diagram sketch list find research compare summarize summarise post open wire map
outline estimate prepare plan deploy center centre align remove delete drop hide replace rename resize swap clean
start stop finish send share look work run try use bring turn see read edit improve simplify shorten expand pull
page pages homepage site website home hero header footer banner menu nav navigation link links button buttons form
forms field fields section sections column columns row rows card cards grid layout panel tab tabs modal popup
text title titles heading headline subtitle copy line lines word words paragraph caption label labels logo icon icons
image images picture photo photos video font fonts color colour colors colours background border spacing margin
padding size width height top bottom left right middle side centered centred bigger smaller larger wider
narrower bold italic dark light white black blue green red yellow orange purple gray grey
html css code script style styles theme mobile desktop phone screen screens view views version demo
canvas board flow flowchart chart charts graph table tables data model schema record records report reports summary
diagrams wireframe slide slides deck plans step steps lists item items note notes doc docs
document documents file files folder sheet sheets price prices pricing cost costs budget order orders product
products inventory store stores shop catalog catalogue customer customers user users account accounts contact
contacts lead leads case cases ticket tickets email emails message messages call calls meeting meetings agenda
calendar date dates time times day days week weeks month months year
login signup sign search filter filters sort dashboard metric metrics status process workflow pipeline integration
api endpoint service server database query tests bug bugs issue issues fixes feature features
idea ideas option options choice choices risk risks question questions answer answers point points part parts
simple simpler quick quickly small big large short long clear full empty main whole better best good
one-line single
""".split())


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


def follow_up(fields: dict) -> bool:
    """An owner turn that went to the voice holding the floor (continuity), with a task word and no name in it."""
    if fields.get("kind") != "turn.started" or fields.get("actor_id") in ("", None, "chair"):
        return False
    if fields.get("reason") != FOLLOW:
        return False
    said = fields.get("utterance") or ""
    if not said.strip() or not TASK.search(said):
        return False
    return not any(pattern.search(said) for pattern in NAMES.values())


def refusal(fields: dict, room: str):
    """The fleet agent whose voice said, in this room, that it cannot do something, or None. A side-channel entry the
    chair wrote (by=chair) for one of the room's voices (side_lines.entry)."""
    if fields.get("by") != SIDE_BY or fields.get("room") != room or fields.get("voice") not in AGENT_OF:
        return None
    return AGENT_OF[fields["voice"]] if REFUSAL.search(fields.get("text") or "") else None


# A word (with its apostrophes and hyphens: it's, one-line), a number, or the end of a sentence.
_TOKEN = re.compile(r"[A-Za-z]+(?:['\u2019-][A-Za-z]+)*|[0-9]+|[.!?;:\n]+")
_SMALL_NUMBER = re.compile(r"[0-9]{1,2}")


def summary(said: str) -> str:
    """His ask in one public line, by the PLAIN allowlist (module docstring); "" when no task word is in it.

    It starts at the sentence that holds the first task word and runs on across sentences for SUMMARY_WORDS words, so
    "I know, but it's a simple HTML update. You just center it" keeps both halves of the ask."""
    said = said or ""
    found = TASK.search(said)
    if not found:
        return ""
    ends = [m.end() for m in re.finditer(r"[.!?;:\n]+", said[:found.start()])]
    out, words, opening = [], 0, True
    for token in _TOKEN.findall(said[ends[-1] if ends else 0:]):
        if not token[0].isalnum():
            if out and out[-1] != "..." and not out[-1].endswith("."):
                out[-1] += "."
            opening = True
            continue
        if words == SUMMARY_WORDS:
            break
        words += 1
        plain = token.lower().replace("\u2019", "'")
        # Title Case after a sentence's first word is a name ("Store" in "Main Street Store"); all capitals is not
        # ("HTML"), and the word must be in PLAIN either way.
        titled = token[0].isupper() and not token.isupper()
        if token == "I" or _SMALL_NUMBER.fullmatch(token) or (plain in PLAIN and (opening or not titled)):
            starts = not out or (out[-1].endswith(".") and out[-1] != "...")
            out.append("I" if plain == "i" else plain.capitalize() if starts else plain)
        elif not out or out[-1] != "...":
            out.append("...")
        opening = False
    while out and out[0] == "...":
        out.pop(0)
    while out and out[-1] == "...":
        out.pop()
    while len(" ".join(out)) > SUMMARY_CHARS:
        out = out[:-1] if out[-1] == "..." else out[:-1] + ["..."]   # whole words only, and the cut shows
    if out:
        out[0] = out[0][:1].upper() + out[0][1:]
        if out[-1] != "...":
            out[-1] = out[-1].rstrip(".")           # the last sentence's full stop, never the cut's "..."
    return " ".join(out)


def claimed_card(agent: str, now, asked: str = "") -> dict:
    """The card that goes up seconds after the ask (O3, the owner's orders of 2026-10-28 8:46 PM ET: "an 'In the
    making' card up within 3 minutes"). Titled with WHAT is being made (the allowlisted summary), never only who;
    the page shows its live "In the making" timer for a claimed card (sfdc24-site assets/proto-canvas.js inMaking)."""
    name = NAME_OF[agent]
    lead = "%s. " % asked if asked else ""
    return {"v": 1, "type": "note", "status": "claimed", "public": True, "version": 1, "ts": stamp(now),
            "activity": "claimed: %s" % asked if asked else "claimed in the room",
            "title": ("In the making: %s" % asked) if asked else "In the making: %s's work from the room" % name,
            "body": "%sAsked in the room at %s. %s has it; its own card for this work replaces this one." % (
                lead, eastern(now), name)}


def done_card(agent: str, claimed_at, now, asked: str = "") -> dict:
    name = NAME_OF[agent]
    minutes = max(0, int(round((now - claimed_at).total_seconds() / 60)))
    lead = "%s. " % asked if asked else ""
    return {"v": 1, "type": "note", "status": "done", "public": True, "version": 2, "ts": stamp(now),
            "activity": "done: %s" % asked if asked else "answered on the board",
            "title": "Done by %s, %s" % (name, eastern(now)),
            "body": "%sClaimed in the room at %s, answered on the board at %s (%d min)." % (
                lead, eastern(claimed_at), eastern(now), minutes)}


def wake_row(ask: str, agent: str, room: str, asked: str, claimed_at) -> dict:
    """The board row that rings the agent: a row spec for scripts/append.py, addressed to the agent's own tag (its
    Target_Surface and to=), phase ASK so every doorbell wakes on it. Pipes and newlines out (bus_request.safe)."""
    import bus_request                                                   # noqa: PLC0415
    safe = bus_request.safe
    what = asked or "work asked in the room (your inbox has it)"
    text = ("Work for %s from the room, asked at %s: %s. Put your own In the making card in conf:canvas:%s:%s now "
            "(target: up within 3 minutes of the ask), do the work, then reply answers=%s. The full ask is in "
            "fleet:inbox:%s." % (NAME_OF[agent], eastern(claimed_at), what, room, agent, ask, agent))
    row_id = safe(WAKE_PREFIX + ask)[:120]
    return {"row_id": row_id, "source_tag": "bus-reconciler", "target_surface": agent, "action_type": "ASK",
            "category": "OPEN", "project_tag": "Blackboard", "gist": safe("Call ask for %s" % NAME_OF[agent]),
            "payload": "BCB|v=1|id=%s|phase=ASK|from=bus-reconciler|to=%s|ask=%s|text=%s" % (
                row_id, agent, safe(ask), safe(text))}


def inbox_text(ask: str, agent: str, room: str, said: str, now) -> str:
    return ("CALL ASK %s from the owner, said in the conference room at %s (%s): %s -- Answer with a board row "
            "answers=%s. Your canvas card %s in conf:canvas:%s:%s turns done when that row lands; post your own "
            "card under the canvas rules for what the work is." % (
                ask, eastern(now), stamp(now), said[:ASK_CHARS].replace("|", "/"), ask, ask, room, agent))


class Bridge:
    def __init__(self, conn, log, holds, clock=None, canvas_room=CANVAS_ROOM, ring=None):
        self.conn, self.log, self.holds = conn, log, holds
        self.ring = ring
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
        if self.ring is not None and self.holds():
            self.ring_pending()
        if self.holds():
            self.watch(now)
        return claimed

    def ring_pending(self) -> list:
        """Ring each queued claim's agent with its one board row. At most once per ask, never retried: the rung
        marker is taken first, so an append that fails or is unconfirmed is logged and left for a person."""
        rung = []
        for raw_ask, raw in sorted((self.conn.hgetall(RING_KEY) or {}).items())[:RING_MAX]:
            ask = _text(raw_ask)
            if not self.holds():
                break
            taken = self.conn.set(RUNG_KEY % ask, stamp(self.clock()), nx=True, ex=TTL_SECONDS)
            self.conn.hdel(RING_KEY, ask)
            if not taken:
                continue                                     # rung before: never twice
            try:
                info = json.loads(_text(raw))
                claimed_at = datetime.datetime.strptime(info["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
                spec = wake_row(ask, info["agent"], info.get("room") or self.canvas_room,
                                str(info.get("asked") or ""), claimed_at)
            except (ValueError, KeyError, TypeError):
                self.log.warning("call bridge: %s ring entry unreadable; not rung", ask)
                continue
            try:
                self.ring(spec)
            except (Exception, SystemExit) as error:                     # noqa: BLE001
                self.log.error("call bridge: %s ring for %s NOT confirmed (%s); not retried", ask, info["agent"],
                               type(error).__name__)
                continue
            self.log.info("call bridge: %s rang %s with %s", ask, info["agent"], spec["row_id"])
            rung.append(ask)
        return rung

    def watch(self, now) -> list:
        """ASK TO AGENT CARD, measured (O3: the real agent puts its card up within TARGET_SECONDS). For each open
        ask, the agent's own card is any public item in the agent's slot of the claim's canvas room, under another
        field than the bridge's, with a ts at or after the claim. The first one seen is kept once in
        gov:callbridge:latency:<ask> ({agent, claimed_at, card, card_at, seconds, on_time}); an ask past
        TARGET_SECONDS with no card is kept once as late (card empty). Read only of the canvas: nothing it
        writes is public. Returns the asks measured on this pass."""
        measured = []
        for raw_ask, raw in (self.conn.hgetall(OPEN_KEY) or {}).items():
            ask = _text(raw_ask)
            key = LATENCY_KEY % ask
            try:
                info = json.loads(_text(raw))
                claimed_at = datetime.datetime.strptime(info["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
                agent, room = info["agent"], info.get("room") or self.canvas_room
            except (ValueError, KeyError, TypeError):
                continue
            seen = self.conn.get(key)
            if seen and json.loads(_text(seen)).get("card"):
                continue                                     # measured: kept once
            first = None
            for raw_field, raw_item in (self.conn.hgetall(canvas_publish.PARTITION % (room, agent)) or {}).items():
                field = _text(raw_field)
                if field == ask:
                    continue
                try:
                    item = json.loads(_text(raw_item))
                    at = datetime.datetime.strptime(str(item.get("ts", ""))[:19], "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=datetime.timezone.utc)
                except (ValueError, TypeError, AttributeError):
                    continue
                if item.get("public") is True and at >= claimed_at.replace(microsecond=0) and \
                        (first is None or at < first[1]):
                    first = (field, at)
            waited = (now - claimed_at).total_seconds()
            if first is not None:
                seconds = max(0, int((first[1] - claimed_at).total_seconds()))
                record = {"agent": agent, "claimed_at": info["claimed_at"], "card": first[0],
                          "card_at": stamp(first[1]), "seconds": seconds, "on_time": seconds <= TARGET_SECONDS}
                self.conn.set(key, json.dumps(record, sort_keys=True), ex=TTL_SECONDS)
                self.log.info("call bridge: %s agent card %s after %d s (%s)", ask, first[0], seconds,
                              "on time" if seconds <= TARGET_SECONDS else "late")
                measured.append(ask)
            elif waited > TARGET_SECONDS and not seen:
                record = {"agent": agent, "claimed_at": info["claimed_at"], "card": "", "card_at": "",
                          "seconds": int(waited), "on_time": False}
                self.conn.set(key, json.dumps(record, sort_keys=True), ex=TTL_SECONDS)
                self.log.warning("call bridge: %s no card from %s after %d s", ask, agent, int(waited))
                measured.append(ask)
        return measured

    def _room(self, room: str, now) -> list:
        key = "v1:conf:%s:events" % room
        cursor = _text(self.conn.get(CURSOR_KEY % room))
        if cursor:
            entries = self.conn.xrange(key, min=cursor, max="+", count=PAGE + 1)
        else:
            start = int((now.timestamp() - LOOKBACK_SECONDS) * 1000)
            entries = self.conn.xrange(key, min="%d-0" % max(0, start), max="+", count=PAGE)
        entries = [(_text(entry_id), _fields(raw)) for entry_id, raw in entries or ()]
        claimed = []
        for index, (entry_id, fields) in enumerate(entries):
            if entry_id == cursor:
                continue
            if fields.get("kind") == "floor.granted" and fields.get("actor_id") in AGENT_OF:
                self.conn.set(LAST_KEY % room, AGENT_OF[fields["actor_id"]], ex=LAST_SECONDS)
            if (fields.get("kind") == "turn.started" and fields.get("actor_id") not in ("", None, "chair")
                    and (fields.get("utterance") or "").strip()):
                self.conn.set(ASKED_KEY % room, json.dumps({"entry": entry_id, "said": fields["utterance"],
                                                             "at": stamp(now)}, sort_keys=True), ex=ASKED_SECONDS)
            agents = asks_in(fields)
            if not agents and follow_up(fields):
                holder = self._holder(room, fields.get("turn_id"), entries[index + 1:])
                agents = [holder] if holder else []
            for agent in agents:
                if not self.holds():
                    return claimed
                ask = self._claim(room, entry_id, agent, fields.get("utterance") or "", now)
                if ask:
                    claimed.append(ask)
            self.conn.set(CURSOR_KEY % room, entry_id, ex=TTL_SECONDS)
        if self.holds():
            claimed.extend(self._refusals(room, now))
        return claimed

    def _refusals(self, room: str, now) -> list:
        """A voice's "I can't" to the owner's last turn (ASKED_SECONDS) claims that turn for the voice's agent.
        Reads today's side channel (UTC, as side_lines names it) from this room's own cursor; a stream seen for the
        first time is read from LOOKBACK_SECONDS back only. Nothing is claimed without his last turn."""
        key = SIDE_KEY % now.strftime("%Y%m%d")
        cursor_key = SIDE_CURSOR_KEY % room
        cursor = _text(self.conn.get(cursor_key))
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
            agent = refusal(_fields(raw), room)
            if agent:
                try:
                    asked = json.loads(_text(self.conn.get(ASKED_KEY % room)) or "null")
                except ValueError:
                    asked = None
                # Only a line said AFTER his turn answers it: both ids are the same Redis server's clock.
                if isinstance(asked, dict) and asked.get("entry") and _after(entry_id, str(asked["entry"])):
                    if not self.holds():
                        return claimed
                    ask = self._claim(room, str(asked["entry"]), agent, str(asked.get("said") or ""), now)
                    if ask:
                        claimed.append(ask)
            self.conn.set(cursor_key, entry_id, ex=TTL_SECONDS)
        return claimed

    def _holder(self, room, turn_id, ahead):
        """Whose a follow-up is: the voice granted the floor for that same turn later in this page, else the last voice
        granted the floor in this room (LAST_SECONDS), else nobody."""
        if turn_id:
            for _, fields in ahead:
                if (fields.get("kind") == "floor.granted" and fields.get("turn_id") == turn_id
                        and fields.get("actor_id") in AGENT_OF):
                    return AGENT_OF[fields["actor_id"]]
        last = _text(self.conn.get(LAST_KEY % room))
        return last if last in NAME_OF else None

    def _claim(self, room, entry_id, agent, said, now):
        ask = ask_id(room, entry_id, agent)
        if not self.conn.set(ASK_KEY % ask, stamp(now), nx=True, ex=TTL_SECONDS):
            return None                                      # claimed before: never twice
        asked = summary(said)
        pipe = self.conn.pipeline(transaction=True)
        try:
            pipe.multi()
            pipe.hset(canvas_publish.PARTITION % (self.canvas_room, agent), ask,
                      json.dumps(claimed_card(agent, now, asked), sort_keys=True))
            pipe.xadd(INBOX_KEY % agent, {"by": BY, "text": inbox_text(ask, agent, self.canvas_room, said, now),
                                          "at": stamp(now)})
            pipe.hset(OPEN_KEY, ask, json.dumps({"agent": agent, "claimed_at": stamp(now), "room": self.canvas_room,
                                                 "asked": asked}, sort_keys=True))
            pipe.expire(OPEN_KEY, TTL_SECONDS)
            if self.ring is not None:
                pipe.hset(RING_KEY, ask, json.dumps({"agent": agent, "claimed_at": stamp(now),
                                                     "room": self.canvas_room, "asked": asked}, sort_keys=True))
                pipe.expire(RING_KEY, TTL_SECONDS)
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
            # A reply to the wake row (answers=CALLBRIDGE-WAKE-<ask>, as the agent-waker writes it) answers the ask.
            answered = [part.strip()[len(WAKE_PREFIX):] if part.strip().startswith(WAKE_PREFIX) else part.strip()
                        for part in re.split(r"[;,\s]+", _field(payload, "answers")) if part.strip()]
            for ask in answered:
                if ask not in stored or ask in done:
                    continue
                try:
                    info = json.loads(stored[ask])
                    claimed_at = datetime.datetime.strptime(info["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                    agent, room = info["agent"], info["room"]
                    asked = str(info.get("asked") or "")
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                # Its own agent's answer only: the row's tag, or the from= it signs with, is one of that agent's.
                tags = ANSWERS_FROM.get(agent, ())
                if sender not in tags and (_field(payload, "from") or "").strip().lower() not in tags:
                    continue
                self._flip(ask, agent, room, claimed_at, now, asked)
                done.append(ask)
        return done

    def _flip(self, ask, agent, room, claimed_at, now, asked="") -> None:
        partition = canvas_publish.PARTITION % (room, agent)
        current = _text(self.conn.hget(partition, ask))
        try:
            mine = json.loads(current) if current else None
        except ValueError:
            mine = None
        pipe = self.conn.pipeline(transaction=True)
        try:
            pipe.multi()
            # The agent's own card under the same id, posted after the claim, is never overwritten.
            if mine is None or (mine.get("status") == "claimed" and mine.get("version") == 1):
                pipe.hset(partition, ask, json.dumps(done_card(agent, claimed_at, now, asked), sort_keys=True))
            pipe.hdel(OPEN_KEY, ask)
            pipe.execute()
        finally:
            pipe.reset()
        self.log.info("call bridge: %s done for %s", ask, agent)

    def run(self, interval=TICK_SECONDS):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:                                   # noqa: BLE001
                # A Redis flap costs one tick, never the meeting.
                self.log.warning("call bridge tick failed (%s)", type(error).__name__)
            self.stop.wait(interval)


def _after(entry_id: str, other: str) -> bool:
    """Stream id entry_id comes after other (ms-seq, compared as numbers). Unreadable ids are never after."""
    try:
        a = tuple(int(part) for part in entry_id.split("-", 1))
        b = tuple(int(part) for part in other.split("-", 1))
    except (AttributeError, ValueError):
        return False
    return a > b


def _row(row):
    """(payload, sender tag) of a board row: bus_request's columns."""
    import bus_request                                                   # noqa: PLC0415
    if not isinstance(row, list) or len(row) <= bus_request.PAYLOAD:
        return "", ""
    return str(row[bus_request.PAYLOAD] or ""), str(row[bus_request.SOURCE] or "").strip().lower()


def _field(payload, name):
    import bus_request                                                   # noqa: PLC0415
    return bus_request.field(payload, name)


def start(conn, log, holds, ring=None):
    """The bridge on its own thread, or None without a way to ask for the lease. `ring`: the doorbell's board append
    (one row spec), or None for no doorbell."""
    if holds is None:
        return None
    bridge = Bridge(conn, log, holds, ring=ring)
    thread = threading.Thread(target=bridge.run, name="call-bridge", daemon=True)
    thread.start()
    bridge.thread = thread
    return bridge

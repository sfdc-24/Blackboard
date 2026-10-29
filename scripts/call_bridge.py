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
room (gov:callbridge:last:<room>, its grant's entry in gov:callbridge:lastat:<room>), only if that grant came at most
FLOOR_FRESH_SECONDS before the ask (never a stale floor). Neither known: nothing is claimed.

A VOICE THAT SAYS IT CANNOT CLAIMS THE ASK (the owner's demo, 2026-10-28 09:14 ET,
CLAUDE-ACTIONS-1313Z-20261028T1328Z: an ask for a picture got no claim and the voice told his guest "I can't pull an
image"). The owner's last turn with words is kept in gov:callbridge:asked:<room> for ASKED_SECONDS. A line the chair
wrote to conf:side:<yyyymmdd> for one of the room's voices (by=chair, voice, room, text: conference
chair/conf_chair/side_lines.py) that says "I can't", "I cannot", "I'm not able", "I'm unable", "I don't have the
tools" or "I have no way" claims that turn for the voice's agent, under the same ask id a named claim of that turn
would have, so the two are one claim. Read from gov:callbridge:side:<room>. It needs the chair's side lines ON for the
room (CONF_REDIS_SIDE_VOICE, CONF_REDIS_SIDE_VOICE_ROOMS); with them off there is nothing to read and this claims
nothing.

"DO IT" CONFIRMS THE ASK BEFORE IT (the owner's test call, 2026-10-29 11:17 ET, room conf-<his 16 hex>-42c62aad):
he described the work, then told a voice to "do it"; the voice said "Got it" and no card went up. Two causes, both
measured: (1) no canvas window was open for the room, so the live bridge never read the call at all (no cursor kept
for that room; the last window had closed at 01:26Z); (2) "do it" names nobody and has no task word, so even a
bridge that read it would claim nothing. Now: an owner turn that is only a go-ahead ("do it", "go ahead", "yes, do
that", "please do", "go for it", "make it so", with "okay", "yes", "please", "now" and a voice's name around it)
confirms his pending TASK ask in the room (gov:callbridge:pending:<room>, kept PENDING_SECONDS: {entry, said,
agents, named}). BOUND (Aya's binding hold on d2d1618): only an owner turn with a task word or a voice's name in it,
not itself a go-ahead, becomes the pending ask, and its voice is fixed when he makes it: the voices it names, else
the voice he was talking to (as a follow-up, never a stale floor), else nobody. The go-ahead claims for those voices;
a go-ahead naming a voice confirms it when his ask named nobody or named that voice; a go-ahead naming any other
voice is ambiguous and claims nothing. The floor holder at the go-ahead's own moment is never used. No pending task
ask (none, expired, or an old record with no assigned voice): nothing is claimed. The claim is made under the
pending ask's entry (its stable id), so an ask already claimed for that agent is never claimed twice, and its three
minutes run from the go-ahead.
AND THE WINDOW OPENS ITSELF: given the publisher's bucket, the bridge reads the room streams whether or not a window
is open, and the first claim that finds the canvas room shut opens it (canvas_publish.open_window, WINDOW_MINUTES,
by call-bridge), so its "In the making" card is on the page seconds after the ask. THAT WINDOW IS SCOPED (Aya's source
hold on d2d1618: an auto-opened window published every public item already in the room): it is opened with
"scoped": true and lists in gov:canvas:scope:<room>:<gen> one item per claim made in it, "<agent>/<ask>"; the
publisher puts only those on the page: the bridge's card for the ask and the agent's own card whose "ask" names it.
Nothing older in the partitions, nothing else the agent writes. A window someone else opened (a board canvas-publish
ask) is theirs: unscoped, as before, and nothing is listed on it. Without a bucket (the old call),
nothing changes: no window, no read. An entry older than LOOKBACK_SECONDS is never claimed, cursor or not, so a
call that went by while the window was shut is never replayed into a new window.

THE WAKE, CONFIRMED (Aya's source hold on d2d1618). With the ring on, the claim rings the agent with a board row
CALLBRIDGE-WAKE-<ask> (ring_pending). The ring is not the wake: the agent must TAKE the claim within ACK_SECONDS,
by a board row from its own tag answering that ring (phase=ACK, or its finished RESULT, never a waker relay's
STATED reply or a refusal), or by its own card for the ask. No take in time: one more ring (id ...-R2); none
ACK_SECONDS after that: the wake record (gov:callbridge:wake:<ask>) and the bridge's card say "agent did not wake".
A later take puts the In the making card back. THE BRIDGE LAUNCHES NOTHING: waking an agent is its runner's job
(claude_cli_inbox.py, fleet_doorbell.py --wait, the agent_waker cloud jobs), outside this module; until a runner
posts the ACK row when it takes a claim, an agent that does not put its own card up in time is marked as not woken.

WHAT GOES ON THE PUBLIC CARD, AND WHAT DOES NOT. The canvas file is world-readable (canvas_publish.py), and the
owner's words in a call never go there (his order O2, 2026-10-28; Aya's AYA-PR436-OWNERWORDS-PRIVACY-HOLD-
20261029T0055Z). The claimed and done cards say who has the ask and when, in the bridge's own words, and nothing
he said: no summary, no allowlist, no word of his. His full words go only to the agent's inbox in Redis, which
only fleet agents read; the agent's own card, posted under the canvas rules, says what the work is in words it
chose for a public page.

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
PENDING_KEY = "gov:callbridge:pending:%s"
PENDING_SECONDS = 10 * 60  # a go-ahead confirms an ask of his made at most this long before it
WINDOW_MINUTES = 120       # canvas_publish.MAX_MINUTES: a window the bridge opens for a claim
GO_WORDS = 12              # a go-ahead is a short turn; a longer one is an ask of its own
LAST_AT_KEY = "gov:callbridge:lastat:%s"  # the stream id of the floor grant LAST_KEY was set from
FLOOR_FRESH_SECONDS = 3 * 60  # an unnamed task ask goes to the floor holder only if granted this close before it
# THE WAKE, CONFIRMED (Aya's source hold on d2d1618: a ring is not a wake). After the ring the agent has ACK_SECONDS
# to take the claim: a board row from its own tags answering the wake row (or the ask) that is phase ACK, or a
# completion (completes()); or its own card for the ask in its partition. A waker's relayed answer (wakerreply=1 /
# evidence=STATED) or a refusal is not taking it. No ack: rung once more (row id + RERING_SUFFIX), and no ack
# ACK_SECONDS after that: the record says the agent did not wake and so does the card. The bridge rings; it
# launches nothing. Launching is each agent's own runner (see the module docstring).
WAKE_KEY = "gov:callbridge:wake:%s"
ACK_SECONDS = 90
RERING_SUFFIX = "-R2"
ACK_PHASES = ("ACK",)
NO_WAKE = "agent did not wake"

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


# A GO-AHEAD: the whole turn is a confirmation. Fillers may come before and after; a voice's name may be anywhere.
_FILL = r"(?:ok(?:ay)?|yes|yeah|yep|yup|sure|great|perfect|good|cool|alright|all\s+right|right|so|then|and|now|just|" \
        r"please|thanks?|thank\s+you|sounds?\s+good|that['’]?s\s+(?:good|great|fine|it)|go|let['’]?s)"
_GO = (r"(?:(?:please\s+)?(?:go\s+ahead(?:\s+and\s+do\s+(?:it|that|this))?|do\s+(?:it|that|this|so)|go\s+for\s+it|"
       r"make\s+it\s+so|please\s+do|proceed|ship\s+it|run\s+with\s+it|yes\s+please|let['’]?s\s+do\s+(?:it|that)|"
       r"start\s+(?:it|on\s+it)|get\s+(?:it|that)\s+done|you\s+can\s+do\s+(?:it|that)))")
GO_AHEAD = re.compile(r"(?i)^(?:%s\s+)*%s(?:\s+%s)*$" % (_FILL, _GO, _FILL))


def go_ahead(fields: dict) -> bool:
    """An owner turn that is only a go-ahead ("do it", "go ahead", "yes, do that"), names stripped out."""
    if fields.get("kind") != "turn.started" or fields.get("actor_id") in ("", None, "chair"):
        return False
    said = fields.get("utterance") or ""
    for pattern in NAMES.values():
        said = pattern.sub(" ", said)
    words = re.sub(r"[^\w'’]+", " ", said).split()
    return 0 < len(words) <= GO_WORDS and bool(GO_AHEAD.match(" ".join(words)))


def task_ask(fields: dict) -> bool:
    """An owner turn that can be a pending ask a go-ahead confirms: a task word or a voice's name in it, and not
    itself a go-ahead (Aya's binding hold on d2d1618: not any turn)."""
    if fields.get("kind") != "turn.started" or fields.get("actor_id") in ("", None, "chair") or go_ahead(fields):
        return False
    said = fields.get("utterance") or ""
    return bool(said.strip()) and (bool(TASK.search(said)) or bool(named_in(said)))


def named_in(said: str) -> list:
    """The agents his words name, in the order he named them (possessives name nobody)."""
    found = sorted((match.start(), AGENT_OF[persona]) for persona, pattern in NAMES.items()
                   for match in pattern.finditer(said or ""))
    agents = []
    for _, agent in found:
        if agent not in agents:
            agents.append(agent)
    return agents


def refusal(fields: dict, room: str):
    """The fleet agent whose voice said, in this room, that it cannot do something, or None. A side-channel entry the
    chair wrote (by=chair) for one of the room's voices (side_lines.entry)."""
    if fields.get("by") != SIDE_BY or fields.get("room") != room or fields.get("voice") not in AGENT_OF:
        return None
    return AGENT_OF[fields["voice"]] if REFUSAL.search(fields.get("text") or "") else None


def claimed_card(agent: str, now) -> dict:
    """Public: who and when, in the bridge's words. Never a word of the owner's (module docstring; Aya's privacy
    hold on 922e8da). O3: the card goes up seconds after the ask and is titled "In the making"; WHAT is being made is
    said by the agent's own card, in its own words, which replaces this one."""
    name = NAME_OF[agent]
    return {"v": 1, "type": "note", "status": "claimed", "public": True, "version": 1, "ts": stamp(now),
            "activity": "claimed in the room",
            "title": "In the making: %s's work from the room" % name,
            "body": "Claimed in the room at %s. %s's card for this work follows here when it is done." % (
                eastern(now), name)}


def no_wake_card(agent: str, claimed_at, rung_at, rerung_at, version: int) -> dict:
    """Public: the agent was rung twice and never took the claim. Who and when only, never a word of the owner's."""
    name = NAME_OF[agent]
    return {"v": 1, "type": "note", "status": "claimed", "public": True, "version": version, "ts": stamp(rerung_at),
            "activity": NO_WAKE,
            "title": "Waiting: %s did not wake" % name,
            "body": "Claimed in the room at %s. %s was called at %s and again at %s and has not taken it." % (
                eastern(claimed_at), name, eastern(rung_at), eastern(rerung_at))}


def done_card(agent: str, claimed_at, now, version: int = 2) -> dict:
    """Public: who, when claimed and when answered. Never a word of the owner's (module docstring)."""
    name = NAME_OF[agent]
    minutes = max(0, int(round((now - claimed_at).total_seconds() / 60)))
    return {"v": 1, "type": "note", "status": "done", "public": True, "version": version, "ts": stamp(now),
            "activity": "answered on the board",
            "title": "Done by %s, %s" % (name, eastern(now)),
            "body": "Claimed in the room at %s, answered on the board at %s (%d min)." % (
                eastern(claimed_at), eastern(now), minutes)}


def event_time(entry_id: str):
    """When the owner's turn landed in the room stream: the ms part of its Redis stream id (the server's clock), or
    None. O3's three minutes start here, not at the bridge's tick (Aya's hold on 5d41cb5, item 5)."""
    try:
        ms = int(str(entry_id).split("-", 1)[0])
    except (TypeError, ValueError):
        return None
    return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)


# COMPLETION, not any reply (Aya's hold on 5d41cb5, item 1). A row flips an ask Done only when it reports finished
# work: phase RESULT or DONE, not a waker's relayed model answer (agent_waker: wakerreply=1, evidence=STATED), not a
# refusal, a hold or a request. A wake reply or an "I can't" leaves the card In the making.
COMPLETE_PHASES = ("RESULT", "DONE")
NOT_DONE = re.compile(r"(?i)(?:^|\|)\s*(?:wakerreply=1|evidence=STATED)\s*(?:\||$)"
                      r"|STATUS:\s*(?:REFUSED|BLOCKED|HOLD|STUCK|NEED_HELP|PARTIAL|ASK)\b")


def completes(payload: str) -> bool:
    phase = (_field(payload, "phase") or "").strip().upper()
    return phase in COMPLETE_PHASES and not NOT_DONE.search(payload or "")


BRIDGE_ACTIVITIES = ("claimed in the room", "answered on the board", NO_WAKE)


def answered_asks(payload: str) -> list:
    """The asks a row answers: answers=<ask>, CALLBRIDGE-WAKE-<ask> or its second ring's id."""
    out = []
    for part in re.split(r"[;,\s]+", _field(payload, "answers") or ""):
        part = part.strip()
        if part.startswith(WAKE_PREFIX):
            part = part[len(WAKE_PREFIX):]
        if part.endswith(RERING_SUFFIX):
            part = part[:-len(RERING_SUFFIX)]
        if part and part not in out:
            out.append(part)
    return out


def takes(payload: str) -> bool:
    """The agent taking the claim: phase ACK, or finished work; never a waker's relayed answer or a refusal."""
    phase = (_field(payload, "phase") or "").strip().upper()
    return (phase in ACK_PHASES and not NOT_DONE.search(payload or "")) or completes(payload)


def owns(field: str, item: dict, ask: str) -> bool:
    """The agent's own card FOR THIS ASK (Aya's hold on 5d41cb5, item 4): under the ask's id, or naming it in "ask",
    and not one of the bridge's own claimed/done cards. Any other new card in the slot, another claim's card included,
    is not this ask's card."""
    if not isinstance(item, dict) or item.get("public") is not True:
        return False
    if item.get("activity") in BRIDGE_ACTIVITIES:
        return False
    return field == ask or str(item.get("ask") or "") == ask


def wake_row(ask: str, agent: str, room: str, claimed_at) -> dict:
    """The board row that rings the agent: a row spec for scripts/append.py, addressed to the agent's own tag (its
    Target_Surface and to=), phase ASK so every doorbell wakes on it. Pipes and newlines out (bus_request.safe).
    Never a word of the owner's (Aya's privacy hold on 922e8da): only who, when and where; his ask is in Redis
    fleet:inbox:<agent>."""
    import bus_request                                                   # noqa: PLC0415
    safe = bus_request.safe
    text = ("Work for %s from the room, asked at %s (your inbox has the ask). Put your own In the making card in conf:canvas:%s:%s now "
            "(target: up within 3 minutes of the ask; its field or its ask value is %s), do the work, then reply with a RESULT row answers=%s. The full ask is in "
            "fleet:inbox:%s." % (NAME_OF[agent], eastern(claimed_at), room, agent, ask, ask, agent))
    row_id = safe(WAKE_PREFIX + ask)[:120]
    return {"row_id": row_id, "source_tag": "bus-reconciler", "target_surface": agent, "action_type": "ASK",
            "category": "OPEN", "project_tag": "Blackboard", "gist": safe("Call ask for %s" % NAME_OF[agent]),
            "payload": "BCB|v=1|id=%s|phase=ASK|from=bus-reconciler|to=%s|ask=%s|text=%s" % (
                row_id, agent, safe(ask), safe(text))}


def inbox_text(ask: str, agent: str, room: str, said: str, now) -> str:
    return ("CALL ASK %s from the owner, said in the conference room at %s (%s): %s -- Answer with a RESULT board row "
            "answers=%s. Your canvas card %s in conf:canvas:%s:%s turns done when that row lands; post your own "
            "card under the canvas rules for what the work is." % (
                ask, eastern(now), stamp(now), said[:ASK_CHARS].replace("|", "/"), ask, ask, room, agent))


class Bridge:
    def __init__(self, conn, log, holds, clock=None, canvas_room=CANVAS_ROOM, ring=None, bucket=None):
        self.conn, self.log, self.holds = conn, log, holds
        self.ring = ring
        # The publisher's bucket: given, the bridge reads every call and opens the canvas window for its first claim.
        self.bucket = bucket
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
        if not self.holds() or (not self.bucket and not self.window_open(now)):
            return []
        claimed = []
        for room in self.streams():
            claimed.extend(self._room(room, now))
        if self.ring is not None and self.holds():
            self.ring_pending()
        if self.ring is not None and self.holds():
            self.wake_check(self.clock())
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
                spec = wake_row(ask, info["agent"], info.get("room") or self.canvas_room, claimed_at)
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
            self._wake(ask, {"agent": info["agent"], "room": info.get("room") or self.canvas_room,
                             "claimed_at": info["claimed_at"], "rung_at": stamp(self.clock()), "state": "rung"})
            rung.append(ask)
        return rung

    def _wake(self, ask, record=None):
        """The wake record of an ask (gov:callbridge:wake:<ask>): read it, or write `record`."""
        if record is not None:
            self.conn.set(WAKE_KEY % ask, json.dumps(record, sort_keys=True), ex=TTL_SECONDS)
            return record
        try:
            held = json.loads(_text(self.conn.get(WAKE_KEY % ask)) or "null")
        except (TypeError, ValueError):
            return None
        return held if isinstance(held, dict) and held.get("agent") in NAME_OF else None

    def _carded(self, ask, agent, room) -> bool:
        for raw_field, raw_item in (self.conn.hgetall(canvas_publish.PARTITION % (room, agent)) or {}).items():
            try:
                item = json.loads(_text(raw_item))
            except (ValueError, TypeError):
                continue
            if owns(_text(raw_field), item, ask):
                return True
        return False

    def wake_check(self, now) -> list:
        """THE WAKE, CONFIRMED (constants above). Per rung ask with no ack: its own card counts as the ack; else
        ACK_SECONDS after the ring it is rung once more (never a third time, a failed second ring is not retried);
        ACK_SECONDS after that it is marked: the record's state no_wake, and the bridge's own card (never the
        agent's) says the agent did not wake. Returns the asks whose state changed."""
        changed = []
        for raw_ask in sorted((self.conn.hgetall(OPEN_KEY) or {})):
            ask = _text(raw_ask)
            if not self.holds():
                break
            wake = self._wake(ask)
            if not wake or wake.get("state") not in ("rung", "rerung"):
                continue
            agent, room = wake["agent"], wake.get("room") or self.canvas_room
            if self._carded(ask, agent, room):
                self._wake(ask, dict(wake, state="acked", acked_at=stamp(now), acked_by="card"))
                changed.append(ask)
                continue
            try:
                rung_at = _parse(wake["rung_at"])
                claimed_at = _parse(wake["claimed_at"])
            except (KeyError, ValueError, TypeError):
                continue
            if wake["state"] == "rung" and (now - rung_at).total_seconds() >= ACK_SECONDS:
                self._wake(ask, dict(wake, state="rerung", rerung_at=stamp(now)))
                spec = wake_row(ask, agent, room, claimed_at)
                spec["row_id"] = spec["row_id"] + RERING_SUFFIX
                spec["payload"] = spec["payload"].replace("|id=%s|" % (WAKE_PREFIX + ask),
                                                          "|id=%s|" % spec["row_id"], 1)
                try:
                    self.ring(spec)
                    self.log.warning("call bridge: %s no ack from %s in %d s; rang once more", ask, agent,
                                     ACK_SECONDS)
                except (Exception, SystemExit) as error:                 # noqa: BLE001
                    self.log.error("call bridge: %s second ring for %s NOT confirmed (%s); not retried", ask, agent,
                                   type(error).__name__)
                changed.append(ask)
            elif wake["state"] == "rerung":
                try:
                    rerung_at = _parse(wake["rerung_at"])
                except (KeyError, ValueError, TypeError):
                    continue
                if (now - rerung_at).total_seconds() < ACK_SECONDS:
                    continue
                self._wake(ask, dict(wake, state="no_wake", no_wake_at=stamp(now)))
                partition = canvas_publish.PARTITION % (room, agent)
                current = _text(self.conn.hget(partition, ask))
                try:
                    mine = json.loads(current) if current else None
                except ValueError:
                    mine = None
                if isinstance(mine, dict) and mine.get("activity") == "claimed in the room":
                    version = mine.get("version") if isinstance(mine.get("version"), int) else 1
                    self.conn.hset(partition, ask, json.dumps(no_wake_card(agent, claimed_at, rung_at, rerung_at,
                                                                           version + 1), sort_keys=True))
                self.log.warning("call bridge: %s %s did not wake (rung %s and %s, no ack)", ask, agent,
                                 wake["rung_at"], wake["rerung_at"])
                changed.append(ask)
        return changed

    def acks(self, rows, now) -> list:
        """The agents' acks among the board rows (constants above). A late ack after no_wake puts the bridge's
        In the making card back. Returns the asks acked."""
        acked = []
        for row in rows or ():
            payload, sender = _row(row)
            if not takes(payload):
                continue
            for ask in answered_asks(payload):
                wake = self._wake(ask)
                if not wake or wake.get("state") not in ("rung", "rerung", "no_wake") or ask in acked:
                    continue
                tags = ANSWERS_FROM.get(wake["agent"], ())
                if sender not in tags and (_field(payload, "from") or "").strip().lower() not in tags:
                    continue
                self._wake(ask, dict(wake, state="acked", acked_at=stamp(now), acked_by=_field(payload, "id") or "row",
                                     late=wake.get("state") == "no_wake"))
                if wake.get("state") == "no_wake" and not completes(payload):     # finished: settle flips it
                    partition = canvas_publish.PARTITION % (wake.get("room") or self.canvas_room, wake["agent"])
                    current = _text(self.conn.hget(partition, ask))
                    try:
                        mine = json.loads(current) if current else None
                    except ValueError:
                        mine = None
                    if isinstance(mine, dict) and mine.get("activity") == NO_WAKE:
                        card = claimed_card(wake["agent"], _parse(wake["claimed_at"]))
                        card["version"] = (mine.get("version") or 1) + 1
                        self.conn.hset(partition, ask, json.dumps(card, sort_keys=True))
                acked.append(ask)
        return acked

    def watch(self, now) -> list:
        """ASK TO AGENT CARD, measured (O3: the real agent puts its card up within TARGET_SECONDS of HIS ask).
        Correlated (Aya's hold on 5d41cb5): only the agent's own card for THIS ask counts (owns()), never the
        bridge's card, another claim's card or an unrelated new card. Timed from the owner's event (the room stream
        id, asked_at), not the bridge's tick, to the moment this pass READ the card back, so intake and poll delays
        are inside the measure; the card's own ts is kept beside it as card_ts, a stated time. The doorbell row is
        not counted as a wake: only the card the agent wrote is. Kept once in gov:callbridge:latency:<ask> ({agent,
        asked_at, claimed_at, card, card_ts, read_at, seconds, waited, on_time}); an ask past TARGET_SECONDS with no card is
        kept once as late (card empty) and measured again when its card comes. Read only of the canvas."""
        measured = []
        for raw_ask, raw in (self.conn.hgetall(OPEN_KEY) or {}).items():
            if self._measure(_text(raw_ask), raw, now):
                measured.append(_text(raw_ask))
        return measured

    def _measure(self, ask, raw, now, settling=False) -> bool:
        """One open ask's ask-to-card record (see watch). The deadline is compared on the exact wait, never the
        whole seconds kept for reading (Aya's hold on 717f423: 180.001 s is late), and the exact wait is kept beside
        it as waited. settling: its RESULT is closing the ask now, so the record is written whatever it shows (the
        card read now, or none: card empty, never on time), before the open record goes (Aya's hold on 717f423)."""
        key = LATENCY_KEY % ask
        try:
            info = json.loads(_text(raw))
            claimed_at = datetime.datetime.strptime(info["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
            asked_at = (datetime.datetime.strptime(info["asked_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=datetime.timezone.utc) if info.get("asked_at") else claimed_at)
            agent, room = info["agent"], info.get("room") or self.canvas_room
        except (ValueError, KeyError, TypeError):
            return False
        seen = self.conn.get(key)
        if seen and json.loads(_text(seen)).get("card"):
            return False                                     # measured: kept once
        first = None
        for raw_field, raw_item in (self.conn.hgetall(canvas_publish.PARTITION % (room, agent)) or {}).items():
            field = _text(raw_field)
            try:
                item = json.loads(_text(raw_item))
            except (ValueError, TypeError):
                continue
            if owns(field, item, ask):
                first = (field, str(item.get("ts", "")))
                break
        waited = max(0.0, (now - asked_at).total_seconds())
        on_time = waited <= TARGET_SECONDS
        base = {"agent": agent, "asked_at": stamp(asked_at), "claimed_at": info["claimed_at"],
                "read_at": stamp(now), "seconds": int(waited), "waited": round(waited, 3)}
        wake = self._wake(ask)
        if wake:
            base["wake"] = wake.get("state", "")
        if settling:
            base["settled_at"] = stamp(now)
        if first is not None:
            record = dict(base, card=first[0], card_ts=first[1], on_time=on_time)
            self.conn.set(key, json.dumps(record, sort_keys=True), ex=TTL_SECONDS)
            self.log.info("call bridge: %s agent card %s read %.3f s after the ask (%s)", ask, first[0], waited,
                          "on time" if on_time else "late")
            return True
        if (not on_time or settling) and not seen:
            record = dict(base, card="", card_ts="", on_time=False)
            self.conn.set(key, json.dumps(record, sort_keys=True), ex=TTL_SECONDS)
            self.log.warning("call bridge: %s no card from %s %.3f s after the ask%s", ask, agent, waited,
                             " (settled)" if settling else "")
            return True
        return False

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
            if _stale(entry_id, now):
                # Said while nothing read the call (a shut window, a stopped run): never replayed, cursor or not.
                self.conn.set(CURSOR_KEY % room, entry_id, ex=TTL_SECONDS)
                continue
            if fields.get("kind") == "floor.granted" and fields.get("actor_id") in AGENT_OF:
                self.conn.set(LAST_KEY % room, AGENT_OF[fields["actor_id"]], ex=LAST_SECONDS)
                self.conn.set(LAST_AT_KEY % room, entry_id, ex=LAST_SECONDS)
            said = fields.get("utterance") or ""
            confirms, asked_entry = None, None
            if go_ahead(fields):
                # "Do it" (Aya's binding hold on d2d1618): it confirms his last TASK ask in this room (PENDING_SECONDS),
                # for the voice that ask was assigned when he made it; never the floor holder of the go-ahead's own
                # moment. No pending task: nothing is claimed. A go-ahead naming a voice other than the one his ask
                # named is ambiguous: nothing is claimed.
                confirms = self._pending(room)
                agents = self._go_agents(confirms, named_in(said)) if confirms else []
                if confirms and agents:
                    asked_entry = entry_id
                    said = "%s -- then, to go ahead: %s" % (confirms["said"], said)
                    # A voice's "I can't" after a go-ahead answers the ask it confirmed: the same claim, never a second.
                    self._asked(room, confirms["entry"], said, now)
                else:
                    self.log.info("call bridge: a go-ahead in %s with %s; nothing claimed", room,
                                  "an ambiguous voice" if confirms else "no pending task ask")
            else:
                agents = asks_in(fields)
                if not agents and follow_up(fields):
                    # The voice he was talking to: same turn, or a grant at most FLOOR_FRESH_SECONDS before (never a
                    # stale floor, Aya's binding hold on d2d1618).
                    holder = self._floor_at(room, entry_id, fields.get("turn_id"), entries[index + 1:])
                    agents = [holder] if holder else []
                if fields.get("kind") == "turn.started" and fields.get("actor_id") not in ("", None, "chair") \
                        and said.strip():
                    self._asked(room, entry_id, said, now)
                    if task_ask(fields):
                        named = named_in(said)
                        assigned = named or [a for a in [self._floor_at(room, entry_id, fields.get("turn_id"),
                                                                         entries[index + 1:])] if a]
                        self.conn.set(PENDING_KEY % room, json.dumps(
                            {"entry": entry_id, "said": said, "agents": assigned, "named": bool(named)},
                            sort_keys=True), ex=PENDING_SECONDS)
            for agent in agents:
                if not self.holds():
                    return claimed
                ask = self._claim(room, confirms["entry"] if confirms else entry_id, agent, said, now,
                                  asked_entry=asked_entry)
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
            if agent and not _stale(entry_id, now):
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

    def _asked(self, room, entry_id, said, now) -> None:
        self.conn.set(ASKED_KEY % room, json.dumps({"entry": entry_id, "said": said, "at": stamp(now)}, sort_keys=True),
                      ex=ASKED_SECONDS)

    def _pending(self, room):
        """{entry, said}: his last ask in this room that was not a go-ahead, kept PENDING_SECONDS, or None."""
        try:
            held = json.loads(_text(self.conn.get(PENDING_KEY % room)) or "null")
        except (TypeError, ValueError):
            return None
        if not (isinstance(held, dict) and held.get("entry") and isinstance(held.get("said"), str)):
            return None
        agents = held.get("agents")
        if not isinstance(agents, list) or any(a not in NAME_OF for a in agents):
            return None                                      # an old or unreadable record binds nothing
        return {"entry": str(held["entry"]), "said": held["said"], "agents": agents, "named": held.get("named") is True}

    @staticmethod
    def _go_agents(pending, go_names) -> list:
        """Who a go-ahead's claim is for. His ask named voices: those, or the ones of them the go-ahead names; a
        go-ahead naming anyone else is ambiguous ([]). His ask named nobody: the voice the go-ahead names, else the
        voice holding the floor when he made the ask (kept with it), else nobody."""
        if pending["named"]:
            if not go_names:
                return list(pending["agents"])
            return list(go_names) if all(a in pending["agents"] for a in go_names) else []
        return list(go_names) or list(pending["agents"])

    def _floor_at(self, room, entry_id, turn_id, ahead):
        """The voice he was talking to when he made an unnamed task ask: granted the floor for that same turn, else
        the last grant in this room if it came at most FLOOR_FRESH_SECONDS before the ask, else nobody."""
        if turn_id:
            for _, fields in ahead:
                if (fields.get("kind") == "floor.granted" and fields.get("turn_id") == turn_id
                        and fields.get("actor_id") in AGENT_OF):
                    return AGENT_OF[fields["actor_id"]]
        last = _text(self.conn.get(LAST_KEY % room))
        granted, said = event_time(_text(self.conn.get(LAST_AT_KEY % room)) or ""), event_time(entry_id)
        if last not in NAME_OF or granted is None or said is None:
            return None
        return last if 0 <= (said - granted).total_seconds() <= FLOOR_FRESH_SECONDS else None

    def open_if_shut(self, now, item: str) -> bool:
        """The canvas room's window for a claim, when it is shut and the bridge has the bucket. True when it is open
        after this. THE BRIDGE'S WINDOW IS SCOPED (Aya's source hold on d2d1618): it publishes only the items the
        bridge lists on it, one per claim ("<agent>/<ask>": the bridge's card for that ask, and the agent's own card
        for it), never anything already in the room's partitions. A window someone else opened (SET NX; a board
        canvas-publish ask) is theirs and is left as it is."""
        if self.window_open(now):
            canvas_publish.scope_add(self.conn, self.canvas_room, item, BY, now)
            return True
        if not self.bucket or not self.holds():
            return False
        try:
            canvas_publish.open_window(self.conn, self.canvas_room, WINDOW_MINUTES, BY, now, self.bucket,
                                       scope=[item])
        except canvas_publish.Refused as refused:
            self.log.info("call bridge: window for %s not opened (%s)", self.canvas_room, refused)
            return self.window_open(now)
        self.log.info("call bridge: opened the %s canvas window for %d min for a claim", self.canvas_room,
                      WINDOW_MINUTES)
        return True

    def _claim(self, room, entry_id, agent, said, now, asked_entry=None):
        """`entry_id` names the ask (one ask, one agent); `asked_entry`, when given, is the turn its three minutes
        run from (a go-ahead confirming an earlier ask)."""
        ask = ask_id(room, entry_id, agent)
        if not self.conn.set(ASK_KEY % ask, stamp(now), nx=True, ex=TTL_SECONDS):
            return None                                      # claimed before: never twice
        if self.bucket:
            self.open_if_shut(now, "%s/%s" % (agent, ask))
        pipe = self.conn.pipeline(transaction=True)
        try:
            pipe.multi()
            pipe.hset(canvas_publish.PARTITION % (self.canvas_room, agent), ask,
                      json.dumps(claimed_card(agent, now), sort_keys=True))
            pipe.xadd(INBOX_KEY % agent, {"by": BY, "text": inbox_text(ask, agent, self.canvas_room, said, now),
                                          "at": stamp(now)})
            asked = event_time(asked_entry or entry_id)
            pipe.hset(OPEN_KEY, ask, json.dumps({"agent": agent, "claimed_at": stamp(now), "room": self.canvas_room,
                                                 "asked_at": asked.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if asked else ""},
                                                sort_keys=True))
            pipe.expire(OPEN_KEY, TTL_SECONDS)
            if self.ring is not None:
                pipe.hset(RING_KEY, ask, json.dumps({"agent": agent, "claimed_at": stamp(now),
                                                     "room": self.canvas_room}, sort_keys=True))
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
        now = self.clock()
        self.acks(rows, now)
        stored = _fields(self.conn.hgetall(OPEN_KEY))
        if not stored:
            return []
        done = []
        for row in rows or ():
            payload, sender = _row(row)
            if not completes(payload):
                continue                                     # a reply, a refusal or a waker's STATED answer: not Done
            # A completion answering the wake row (answers=CALLBRIDGE-WAKE-<ask>) answers the ask.
            answered = answered_asks(payload)
            for ask in answered:
                if ask not in stored or ask in done:
                    continue
                try:
                    info = json.loads(stored[ask])
                    claimed_at = datetime.datetime.strptime(info["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                    agent, room = info["agent"], info["room"]
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                # Its own agent's answer only: the row's tag, or the from= it signs with, is one of that agent's.
                tags = ANSWERS_FROM.get(agent, ())
                if sender not in tags and (_field(payload, "from") or "").strip().lower() not in tags:
                    continue
                self._measure(ask, stored[ask], now, settling=True)   # the timing is kept before the ask closes
                self._flip(ask, agent, room, claimed_at, now)
                done.append(ask)
        return done

    def _flip(self, ask, agent, room, claimed_at, now) -> None:
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
            if mine is None or (mine.get("status") == "claimed" and isinstance(mine.get("version"), int) and (
                    mine.get("version") == 1 or mine.get("activity") in ("claimed in the room", NO_WAKE))):
                version = max(2, (mine or {}).get("version", 1) + 1)
                pipe.hset(partition, ask, json.dumps(done_card(agent, claimed_at, now, version), sort_keys=True))
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


def _parse(text: str):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def _stale(entry_id: str, now) -> bool:
    """An entry said more than LOOKBACK_SECONDS before `now` (its stream id's clock). Unreadable ids are not stale."""
    said = event_time(entry_id)
    return said is not None and (now - said).total_seconds() > LOOKBACK_SECONDS


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


def start(conn, log, holds, ring=None, bucket=None):
    """The bridge on its own thread, or None without a way to ask for the lease. `ring`: the doorbell's board append
    (one row spec), or None for no doorbell. `bucket`: the publisher's, so a claim can open the canvas window."""
    if holds is None:
        return None
    bridge = Bridge(conn, log, holds, ring=ring, bucket=bucket)
    thread = threading.Thread(target=bridge.run, name="call-bridge", daemon=True)
    thread.start()
    bridge.thread = thread
    return bridge

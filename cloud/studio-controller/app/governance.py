"""Governance for the sfdc24.com studio: what the agents will build, and the gate in front of them.

The owner's direction (2026-09-25): "put some governance around SFDC24.com so
people can't do any work that is not ethical (inappropriate or not enterprise
grade); some safeguards that are already part of claude and openAI should
uphold."

Three layers, each on its own:

1. THE GATE (this module). Everything a visitor says reaches the controller as
   text - the talk lane, an utterance or a typed answer to the builder, the
   analyst, and a line sent to the architect's voice. Before any lane runs, that
   text goes through OpenAI's moderation endpoint (omni-moderation-latest). A
   flagged line runs no lane: the visitor hears POLICY_LINE and nothing is built.
2. THE USE POLICY (workers/policy.py), in every lane's system prompt. Moderation
   catches harassment, hate, sexual, violent and illicit content; it does not
   know that a pixel-perfect bank login page is a phishing kit. The agents do,
   and decline it in one sentence.
3. THE PROVIDERS' OWN SAFEGUARDS. Anthropic's and OpenAI's usage policies
   apply to every model call underneath both.

Flags are counted per session, in their own object in the state store (never
in the session record, so a flag can never collide with a build's
compare-and-set save). One spoken line reaches up to three lanes, so each lane
keeps its own count and the session's count is the highest of them: one line
counts once, and a line that is refused in three lanes is still one line. At
FLAG_LIMIT the session is stopped.

Nothing here stores or logs what the visitor said. A flag record holds the
lane, the moderation categories and a time; a log line adds the session id and
the count. Recently checked texts are cached in memory by their SHA-256 only.

If moderation is unavailable - no key, a timeout, an error, a malformed
answer - the gate FAILS OPEN: the words go on, the outage is counted and
logged. Layers 2 and 3 still apply to that turn; a moderation outage must not
take the homepage conversation down with it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass

try:
    from scripts.state_store import Conflict
except ImportError:  # Docker copies the shared module beside the app.
    from state_store import Conflict

from workers.policy import USE_POLICY  # noqa: F401  (re-exported: one policy text)

MODERATION_URL = "https://api.openai.com/v1/moderations"
MODERATION_MODEL = "omni-moderation-latest"
MODERATION_TIMEOUT = 3.0
FLAG_LIMIT = 3
CACHE_SECONDS = 300
CACHE_MAX = 1024
RECORDS_MAX = 20

POLICY_LINE = ("That is not something SFDC24 can help with. We build legitimate, professional "
               "business work - tell me about your business and let's build that instead.")
POLICY_END_LINE = ("That goes against the SFDC24 use policy, so this session has ended. "
                   "You are welcome to start a new one about your business.")
POLICY_END_REASON = "This session was ended because requests went against the SFDC24 use policy."
POLICY_PROBLEM = "declined by the SFDC24 use policy"


@dataclass(frozen=True)
class Verdict:
    flagged: bool
    categories: tuple = ()
    available: bool = True
    reason: str = ""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log_event(event: str, **fields) -> None:
    """One structured line for Cloud Logging. Callers never pass visitor text."""
    record = {"severity": "WARNING", "event": event}
    record.update(fields)
    print(json.dumps(record, sort_keys=True), file=sys.stdout, flush=True)


class Moderator:
    """OpenAI moderation in front of the lanes, with a small in-memory cache."""

    def __init__(self, client_getter, api_key: str, *, enabled: bool = True, clock=time.time):
        self._client_getter = client_getter
        self._api_key = api_key
        self.enabled = enabled
        self.clock = clock
        self._cache: dict = {}
        self.stats = {"checked": 0, "flagged": 0, "unavailable": 0}

    def _cached(self, key: str):
        hit = self._cache.get(key)
        if hit and self.clock() - hit[2] < CACHE_SECONDS:
            return hit
        return None

    def _remember(self, key: str, flagged: bool, categories: tuple) -> None:
        self._cache[key] = (flagged, categories, self.clock())
        if len(self._cache) > CACHE_MAX:
            for stale in sorted(self._cache, key=lambda k: self._cache[k][2])[:CACHE_MAX // 2]:
                self._cache.pop(stale, None)

    async def check(self, texts) -> Verdict:
        """Flagged when ANY of the texts is flagged; unavailable fails open."""
        texts = [t for t in dict.fromkeys(t.strip() for t in texts if isinstance(t, str)) if t]
        if not self.enabled or not texts:
            return Verdict(False)
        self.stats["checked"] += 1
        flagged, categories, pending = False, set(), []
        for text in texts:
            hit = self._cached(_digest(text))
            if hit is None:
                pending.append(text)
            elif hit[0]:
                flagged = True
                categories.update(hit[1])
        if pending and not flagged:
            verdict = await self._ask(pending)
            if not verdict.available:
                self.stats["unavailable"] += 1
                return verdict
            flagged, categories = verdict.flagged, set(verdict.categories)
        if flagged:
            self.stats["flagged"] += 1
        return Verdict(flagged, tuple(sorted(categories)))

    async def _ask(self, texts: list) -> Verdict:
        if not self._api_key:
            return Verdict(False, available=False, reason="no key")
        try:
            response = await asyncio.wait_for(self._client_getter().post(
                MODERATION_URL,
                headers={"Authorization": "Bearer " + self._api_key},
                json={"model": MODERATION_MODEL, "input": texts},
                timeout=MODERATION_TIMEOUT,
            ), MODERATION_TIMEOUT + 0.5)
        except asyncio.TimeoutError:
            return Verdict(False, available=False, reason="timeout")
        except Exception as exc:  # network, TLS, client closed: the outage path, not a crash
            return Verdict(False, available=False, reason=type(exc).__name__)
        if getattr(response, "status_code", 0) != 200:
            return Verdict(False, available=False, reason="http %s" % getattr(response, "status_code", "?"))
        try:
            results = response.json()["results"]
            if not isinstance(results, list) or len(results) != len(texts):
                raise ValueError("results do not match inputs")
            flagged, categories = False, set()
            for text, result in zip(texts, results):
                hit = bool(result["flagged"])
                names = tuple(sorted(name for name, on in (result.get("categories") or {}).items() if on is True))
                self._remember(_digest(text), hit, names)
                if hit:
                    flagged = True
                    categories.update(names)
        except Exception:
            return Verdict(False, available=False, reason="malformed answer")
        return Verdict(flagged, tuple(sorted(categories)))


class PolicyBook:
    """Per-session flag counts, one object per session, written by compare-and-set."""

    def __init__(self, store, clock=time.time, attempts: int = 8):
        self.store = store
        self.clock = clock
        self.attempts = attempts

    @staticmethod
    def _name(session_id: str) -> str:
        return "studio_policy_" + session_id

    def record(self, session_id: str, lane: str, categories) -> int:
        """Count one refused line in `lane`; return the session's count (the highest lane)."""
        now = int(self.clock())
        for _ in range(self.attempts):
            state, token = self.store.load(self._name(session_id))
            current = dict(state) if isinstance(state, dict) else {}
            lanes = dict(current.get("lanes") or {})
            lanes[lane] = int(lanes.get(lane) or 0) + 1
            records = list(current.get("records") or [])
            records.append({"at": now, "lane": lane, "categories": list(categories)})
            candidate = {"version": 1, "lanes": lanes, "records": records[-RECORDS_MAX:]}
            try:
                self.store.save(self._name(session_id), candidate, token)
                return max(lanes.values())
            except Conflict:
                continue
        raise RuntimeError("the policy record is busy")

    def count(self, session_id: str) -> int:
        state, _ = self.store.load(self._name(session_id))
        lanes = (state or {}).get("lanes") or {}
        return max((int(v) for v in lanes.values()), default=0)


def command_texts(command: dict) -> list:
    """Every piece of visitor-written text a command carries."""
    texts = []
    for key in ("transcript", "freeform_answer"):
        if isinstance(command.get(key), str):
            texts.append(command[key])
    answers = command.get("answers")
    if isinstance(answers, list):
        for answer in answers:
            if isinstance(answer, dict) and isinstance(answer.get("freeform_answer"), str):
                texts.append(answer["freeform_answer"])
    return texts


def history_texts(history) -> list:
    """The talk history is sent by the page and can be forged; check every line of it."""
    if not isinstance(history, list):
        return []
    return [turn.get("text") for turn in history if isinstance(turn, dict) and isinstance(turn.get("text"), str)]


__all__ = [
    "FLAG_LIMIT", "MODERATION_MODEL", "MODERATION_URL", "Moderator", "POLICY_END_LINE",
    "POLICY_END_REASON", "POLICY_LINE", "POLICY_PROBLEM", "PolicyBook", "USE_POLICY", "Verdict",
    "command_texts", "history_texts", "log_event",
]

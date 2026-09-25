"""The talk lane: a spoken reply to what the visitor just said, in about a second.

WHY THIS EXISTS. The builder (workers/claude_worker.py) takes 15-25 seconds a
turn, and until now the voice only ever spoke what the builder committed, so a
session felt like dictation, not a conversation. The architecture (SFDC24 +
Blackboard, Current and Future State, 2026-09-24) puts dialogue timing in the
realtime controller and runs the builder off the voice path; the 10-hour order
(2026-09-25) asks for a selected OpenAI or Claude agent that answers every turn.
This is that path: the selected agent answers at once, while the same words go
to the builder as an utterance command.

It never builds and never claims a change it cannot see (Gemini's review: the
talk model must acknowledge intent, not narrate a canvas mutation before the
builder commits it). The canvas summary it is given is read-only.

Measured on 2026-09-25 with the production keys: Claude (claude-haiku-4-5)
0.9-1.0 s warm; OpenAI (gpt-4.1-mini) 0.9-1.1 s.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

CLAUDE_MODEL = os.environ.get("STUDIO_TALK_MODEL") or "claude-haiku-4-5-20251001"
OPENAI_MODEL = os.environ.get("STUDIO_TALK_OPENAI_MODEL") or "gpt-4.1-mini"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT_SECONDS = 8.0
MAX_TOKENS = 160
REPLY_MAX = 400
HISTORY_MAX = 8
TEXT_MAX = 600
AGENTS = ("claude", "openai")
NAMES = {"claude": "Claude", "openai": "the SFDC24 studio assistant"}

SYSTEM = """You are {name}, talking out loud with a visitor during a live design session on \
sfdc24.com. While you talk, a separate builder is drawing the prototype on the visitor's screen \
from the same words. You speak; the builder builds. You can design anything the builder can draw - \
a website section, an app screen, a form, a logo or banner, a process or a data model.

Reply in one or two short spoken sentences, at most 35 words. Sound like a person in a meeting: \
acknowledge what they asked, say plainly what happens next, and ask one short question only when \
the answer would change what gets built. No lists, no markdown, no emoji, no preamble.

Never say something was built, changed or removed unless the canvas summary already shows it; \
for a new request say it is being built now. Never invent facts about the visitor's business, \
never name or promise any person, and never describe how this system works."""


def canvas_summary(artifact: dict | None) -> str:
    """A short, read-only description of the prototype for grounding the reply."""
    if not isinstance(artifact, dict):
        return "The canvas is empty."
    children = artifact.get("children") or []
    if not children:
        return "The canvas is empty: nothing has been built yet."
    count = [0]

    def walk(node):
        count[0] += 1
        for child in node.get("children") or []:
            walk(child)

    walk(artifact)
    parts = ["%s %s" % (c.get("kind", "node"), str(c.get("label", ""))[:60]) for c in children[:8]]
    return ("The canvas is called %r and has %d parts. Top level: %s."
            % (str(artifact.get("label", ""))[:80], count[0], "; ".join(parts)))[:700]


def clean_history(history) -> list:
    """Validated conversation so far, oldest first. Raises ValueError."""
    if history is None:
        return []
    if not isinstance(history, list) or len(history) > HISTORY_MAX:
        raise ValueError("history must be a list of at most %d turns" % HISTORY_MAX)
    turns = []
    for turn in history:
        if not isinstance(turn, dict) or set(turn) - {"who", "text"}:
            raise ValueError("each history turn is {who, text}")
        who, text = turn.get("who"), turn.get("text")
        if who not in ("you",) + AGENTS or not isinstance(text, str) or not text.strip() \
                or len(text) > TEXT_MAX:
            raise ValueError("history turn has a bad who or text")
        turns.append({"who": who, "text": text})
    return turns


def _messages(text: str, history: list, canvas: str) -> list:
    messages = []
    for turn in history:
        role = "user" if turn["who"] == "you" else "assistant"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + turn["text"]
        else:
            messages.append({"role": role, "content": turn["text"]})
    latest = "[Canvas now: %s]\n%s" % (canvas, text)
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n" + latest
    else:
        messages.append({"role": "user", "content": latest})
    if messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "(The session has started.)"})
    return messages


def _spoken(words: str) -> str:
    return " ".join((words or "").split())[:REPLY_MAX]


class TalkClient:
    """Routes one turn to the selected agent. Only agents with a configured
    credential are offered; an unavailable one is refused, never simulated."""

    def __init__(self, *, anthropic_client=None, anthropic_ready: bool | None = None,
                 openai_key: str | None = None, opener=None):
        self._anthropic = anthropic_client
        self._anthropic_ready = (anthropic_client is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))) \
            if anthropic_ready is None else anthropic_ready
        self._openai_key = os.environ.get("OPENAI_API_KEY", "") if openai_key is None else openai_key
        self._open = opener or urllib.request.urlopen

    def agents(self) -> list:
        ready = []
        if self._anthropic_ready:
            ready.append("claude")
        if self._openai_key:
            ready.append("openai")
        return ready

    def reply(self, agent: str, text: str, history: list, canvas: str) -> str:
        if agent not in self.agents():
            raise LookupError("agent %r is not configured" % agent)
        system = SYSTEM.format(name=NAMES[agent])
        messages = _messages(text, history, canvas)
        if agent == "claude":
            return self._claude(system, messages)
        return self._openai(system, messages)

    def _claude(self, system: str, messages: list) -> str:
        if self._anthropic is None:
            import anthropic
            self._anthropic = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=0)
        response = self._anthropic.messages.create(
            model=CLAUDE_MODEL, max_tokens=MAX_TOKENS, system=system, messages=messages,
        )
        return _spoken("".join(getattr(block, "text", "") for block in response.content
                               if getattr(block, "type", "") == "text"))

    def _openai(self, system: str, messages: list) -> str:
        body = json.dumps({
            "model": OPENAI_MODEL,
            "max_completion_tokens": MAX_TOKENS,
            "messages": [{"role": "system", "content": system}] + messages,
        }).encode("utf-8")
        request = urllib.request.Request(OPENAI_URL, data=body, method="POST", headers={
            "Authorization": "Bearer " + self._openai_key,
            "Content-Type": "application/json",
        })
        with self._open(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices") or []
        return _spoken(((choices[0] or {}).get("message") or {}).get("content", "") if choices else "")

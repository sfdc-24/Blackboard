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

try:  # the app and the image import this module as part of the workers package
    from workers.policy import USE_POLICY
except ImportError:  # loaded from its file (tests): read the sibling policy.py the same way
    import importlib.util as _util
    _spec = _util.spec_from_file_location(
        "studio_use_policy", os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.py"))
    _policy = _util.module_from_spec(_spec)
    _spec.loader.exec_module(_policy)
    USE_POLICY = _policy.USE_POLICY

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

Reply in one or two short spoken sentences, at most 30 words. Sound like a person in a meeting: \
acknowledge what they asked and say plainly what happens next. Do not ask questions - another \
agent on the session asks them, and two voices asking at once would talk over each other. No \
lists, no markdown, no emoji, no preamble.

Never say something was built, changed or removed unless the canvas summary already shows it; \
for a new request say it is being built now. Never invent facts about the visitor's business, \
never name or promise any person, and never describe how this system works.

""" + USE_POLICY


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


def _spoken(words: str, cap: int = REPLY_MAX) -> str:
    return " ".join((words or "").split())[:cap]


RECAP_MAX = 900
RECAP_TOKENS = 160
RECAP_WORDS = 70  # enforced below, on a sentence boundary; the prompt alone does not hold it
RECAP_SYSTEM = """You are the host of a live design session on sfdc24.com, closing the meeting. \
Recap it out loud in about 60 words (70 at most), as one warm, plain spoken paragraph: what the visitor wants, \
what is on the canvas now, what was decided, and one next step they can take - keep shaping it here, \
or talk to us about turning it into the real thing. Never invent facts about their business, and \
never name or promise any person, price or date. No lists, no markdown, no preamble. Never repeat \
or summarise a request that was declined under the use policy below.

""" + USE_POLICY


def fit_words(text: str, limit: int) -> str:
    """At most `limit` words, cut back to the last full sentence inside the limit
    so a spoken recap never stops mid-thought; with no sentence end inside the
    limit, the words up to it and a full stop."""
    words = (text or "").split()
    if len(words) <= limit:
        return " ".join(words)
    head = words[:limit]
    for i in range(len(head) - 1, -1, -1):
        if head[i].endswith((".", "!", "?")):
            return " ".join(head[:i + 1])
    return " ".join(head).rstrip(",;:-") + "."


def recap_brief(state: dict, canvas: str) -> str:
    """What the host recaps from: the session record only."""
    said = [t.get("text", "") for t in (state.get("transcript") or []) if t.get("role", "visitor") == "visitor"]
    decided = []
    for q in state.get("questions") or []:
        if q.get("status") != "answered":
            continue
        chosen = next((o.get("label", "") for o in q.get("options") or []
                       if o.get("option_id") == q.get("selected_option")), "") or q.get("freeform_answer", "")
        decided.append("%s -> %s" % (q.get("prompt", ""), chosen))
    model = state.get("model") or {}
    objects = ", ".join(o.get("name", "") for o in model.get("objects") or [])
    lines = ["THE VISITOR SAID (oldest first):"] + ["- " + text for text in said[-20:]]
    lines += ["", "ON THE CANVAS: " + canvas]
    lines += ["DECIDED: " + ("; ".join(decided) if decided else "nothing yet")]
    if objects:
        lines += ["DATA MODEL (%s): %s" % (model.get("domain", ""), objects)]
    return "\n".join(lines)[:6000]


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

    def recap(self, agent: str, brief: str) -> str:
        """The host closing the meeting: a short spoken recap from the session record."""
        if agent not in self.agents():
            raise LookupError("agent %r is not configured" % agent)
        messages = [{"role": "user", "content": brief}]
        if agent == "claude":
            return fit_words(self._claude(RECAP_SYSTEM, messages, RECAP_TOKENS, RECAP_MAX), RECAP_WORDS)
        return fit_words(self._openai(RECAP_SYSTEM, messages, RECAP_TOKENS, RECAP_MAX), RECAP_WORDS)

    def _claude(self, system: str, messages: list, max_tokens: int = MAX_TOKENS, cap: int = REPLY_MAX) -> str:
        if self._anthropic is None:
            import anthropic
            self._anthropic = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=0)
        response = self._anthropic.messages.create(
            model=CLAUDE_MODEL, max_tokens=max_tokens, system=system, messages=messages,
        )
        return _spoken("".join(getattr(block, "text", "") for block in response.content
                               if getattr(block, "type", "") == "text"), cap)

    def _openai(self, system: str, messages: list, max_tokens: int = MAX_TOKENS, cap: int = REPLY_MAX) -> str:
        body = json.dumps({
            "model": OPENAI_MODEL,
            "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}] + messages,
        }).encode("utf-8")
        request = urllib.request.Request(OPENAI_URL, data=body, method="POST", headers={
            "Authorization": "Bearer " + self._openai_key,
            "Content-Type": "application/json",
        })
        with self._open(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices") or []
        return _spoken(((choices[0] or {}).get("message") or {}).get("content", "") if choices else "", cap)

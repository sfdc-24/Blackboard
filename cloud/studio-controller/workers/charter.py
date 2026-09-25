"""The charter lane: a project charter and workplan, in disguise.

Owner direction, 2026-09-25: "a quality of context score, the more context they
provide they can see a score board of the scope, objectives, design sense,
refinement, timeline and closing expectations ... basically a project charter
and workplan in disguise". The page shows the board; the host and the architect
ask the next open item; the build plan PDF (app/summary_pdf.py) carries it.

One call reads the COMMITTED moment - the topic, what is on the canvas, the
last visitor lines and the charter so far - and returns, for every dimension
of the topic's frame (workers/topics.py CHARTER_FRAMES): a level 0-3 and the
one line captured so far, plus the single most useful question to ask next.

WHAT IS CHECKED HERE - model output is data, checked before it reaches anyone.
The whole answer passes or the whole answer is refused (then one repair):
    - exactly the frame's ids, each once; nothing else
    - level is an integer 0-3 (never a bool or a string)
    - captured is one plain line of at most 140 characters, empty exactly
      when the level is 0
    - next is one plain line of at most 160 characters, or empty
A disabled, failed, slow or malformed charter returns None and blocks nothing.
Off unless STUDIO_ENABLE_CHARTER is on (app/settings.py).

Model: the builder's model (STUDIO_WORKER_MODEL, claude-sonnet-5 in
production) through one forced tool call, so the answer is JSON only.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

MODEL = (os.environ.get("STUDIO_CHARTER_MODEL") or os.environ.get("STUDIO_WORKER_MODEL")
         or "claude-sonnet-5")
TIMEOUT_SECONDS = 20.0
MAX_TOKENS = 1200
CAPS = {"captured": 140, "next": 160}
LEVELS = (0, 1, 2, 3)
SAID_MAX = 12            # the last visitor lines the lane reads
BAD_CHARS_RE = re.compile(r"[<>\x00-\x1f\x7f]")
UNSAFE_CATEGORIES = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn")

try:  # the app and the image import these as part of the workers package
    from workers.policy import USE_POLICY
    from workers.topics import TOPICS, charter_frame
except ImportError:  # loaded from its file (tests): read the siblings the same way
    import importlib.util as _util

    def _sibling(name):
        spec = _util.spec_from_file_location(
            "studio_charter_" + name, os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py"))
        mod = _util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    USE_POLICY = _sibling("policy").USE_POLICY
    _topics = _sibling("topics")
    TOPICS, charter_frame = _topics.TOPICS, _topics.charter_frame

SYSTEM = """You keep the project charter in a live SFDC24 session on sfdc24.com, quietly, while a host \
talks with the visitor and an architect builds what they describe. You never speak to the visitor and \
never build. You read the conversation, the canvas and the charter so far, and you record how well each \
part of the charter is covered, and the one thing most worth asking next.

For every dimension of the frame you are given:
- level: 0 nothing said yet; 1 mentioned or vague; 2 clear; 3 clear and confirmed with specifics.
- captured: what the visitor has actually said about it, in one plain line of at most 140 characters, in \
their terms. Empty exactly when the level is 0. Never invent a fact, a number, a name, a price or a date \
they did not give.
next: ONE short, friendly question (at most 160 characters) about the first dimension, in frame order, \
that is below level 2 - the question the host should ask next. Empty only when every dimension is at \
level 2 or more.

Plain text only: no markup, no emoji, no line breaks. """ + USE_POLICY + """ Finish by calling \
record_charter once."""


def plain_text(value) -> bool:
    """One line of plain text: no markup brackets, no invisible or line-breaking code points."""
    return (isinstance(value, str) and not BAD_CHARS_RE.search(value)
            and not any(unicodedata.category(ch) in UNSAFE_CATEGORIES for ch in value))


def _line(value, cap: int, *, may_be_empty: bool) -> bool:
    if not isinstance(value, str) or not plain_text(value):
        return False
    text = value.strip()
    if not text:
        return may_be_empty
    return len(text) <= cap


def frame_ids(topic) -> tuple:
    return tuple(d[0] for d in charter_frame(topic))


def tool_for(topic) -> dict:
    ids = list(frame_ids(topic))
    return {
        "name": "record_charter",
        "description": "Record the charter: every dimension of the frame once, and the next question. Call once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dimensions": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "level": {"type": "integer", "enum": list(LEVELS)},
                        "captured": {"type": "string", "description": "At most 140 characters; empty when level is 0."},
                    },
                    "required": ["id", "level", "captured"],
                    "additionalProperties": False}},
                "next": {"type": "string", "description": "One question, at most 160 characters, or empty."},
            },
            "required": ["dimensions", "next"],
            "additionalProperties": False,
        },
    }


def validate(raw, topic) -> tuple[dict | None, list]:
    """(the checked charter in frame order, or None; problems). Nothing is repaired or partly kept."""
    ids = frame_ids(topic)
    if not isinstance(raw, dict) or set(raw) != {"dimensions", "next"}:
        return None, ["the answer must be exactly {dimensions, next}"]
    problems = []
    dims = raw.get("dimensions")
    if not isinstance(dims, list):
        return None, ["dimensions must be a list"]
    seen = {}
    for d in dims:
        if not isinstance(d, dict) or set(d) != {"id", "level", "captured"}:
            problems.append("a dimension has missing or extra fields")
            continue
        did, level, captured = d.get("id"), d.get("level"), d.get("captured")
        if not isinstance(did, str) or did not in ids:
            problems.append("an unknown dimension id")
            continue
        if did in seen:
            problems.append("dimension %s appears twice" % did)
            continue
        if type(level) is not int or level not in LEVELS:
            problems.append("dimension %s: level must be an integer 0-3" % did)
            continue
        if not _line(captured, CAPS["captured"], may_be_empty=True):
            problems.append("dimension %s: captured is not one plain line of at most %d characters"
                            % (did, CAPS["captured"]))
            continue
        if (level == 0) != (captured.strip() == ""):
            problems.append("dimension %s: captured must be empty exactly when the level is 0" % did)
            continue
        seen[did] = {"id": did, "level": level, "captured": captured.strip()}
    missing = [i for i in ids if i not in seen]
    if missing:
        problems.append("missing dimensions: " + ", ".join(missing))
    nxt = raw.get("next")
    if not _line(nxt, CAPS["next"], may_be_empty=True):
        problems.append("next is not one plain line of at most %d characters" % CAPS["next"])
    if problems:
        return None, problems
    return {"dimensions": [seen[i] for i in ids], "next": nxt.strip()}, []


def describe(snapshot: dict) -> str:
    """What the lane reads: the same committed moment the builder reads."""
    topic = snapshot.get("topic") if snapshot.get("topic") in TOPICS else ""
    lines = []
    if snapshot.get("topic_line"):
        lines.append(str(snapshot["topic_line"]).strip())
    lines.append("THE FRAME (in order):")
    for did, label, covers in charter_frame(topic):
        lines.append("- %s (%s): %s" % (did, label, covers))
    lines.append("")
    lines.append("CANVAS (revision %d): %s" % (int(snapshot.get("revision") or 0),
                                              str(snapshot.get("canvas") or "nothing built yet")[:3000]))
    said = [str(t)[:600] for t in (snapshot.get("said") or [])][-SAID_MAX:]
    lines.append("THE VISITOR SAID (oldest first):")
    lines.extend("- " + s for s in said) if said else lines.append("- (nothing yet)")
    before = snapshot.get("charter") or {}
    if isinstance(before, dict) and before.get("dimensions"):
        lines.append("THE CHARTER SO FAR:")
        for d in before["dimensions"]:
            if isinstance(d, dict):
                lines.append("- %s: level %s, %s" % (str(d.get("id", ""))[:20], str(d.get("level", 0))[:2],
                                                     str(d.get("captured", ""))[:140] or "(nothing)"))
    return "\n".join(lines)[:9000]


class Charter:
    """Claude, asked once per committed moment (one repair), answering with a
    checked charter in frame order - or None, never an exception."""

    def __init__(self, client=None, *, enabled: bool = False, model: str = MODEL):
        self.client, self.model, self.enabled = client, model, bool(enabled)

    def ready(self) -> bool:
        return self.enabled

    def _client(self):
        if self.client is None:
            import anthropic  # the official SDK; ANTHROPIC_API_KEY from Secret Manager
            self.client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=0)
        return self.client

    def _ask(self, messages: list, topic):
        resp = self._client().messages.create(
            model=self.model, max_tokens=MAX_TOKENS, system=SYSTEM, tools=[tool_for(topic)],
            tool_choice={"type": "tool", "name": "record_charter"}, messages=messages,
        )
        call = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"
                     and getattr(b, "name", "") == "record_charter"), None)
        return call.input if call is not None and isinstance(call.input, dict) else None

    def chart(self, snapshot: dict) -> dict | None:
        """{topic, dimensions, next} for snapshot["topic"], or None."""
        if not self.ready():
            return None
        topic = snapshot.get("topic") if snapshot.get("topic") in TOPICS else ""
        try:
            messages = [{"role": "user", "content": describe(snapshot)}]
            raw = self._ask(messages, topic)
            checked, problems = validate(raw, topic) if raw is not None else (None, ["no record_charter call"])
            if checked is None:
                repair = messages + [
                    {"role": "assistant", "content": json.dumps(raw)[:4000] if raw is not None else "(no answer)"},
                    {"role": "user", "content": "That answer was refused: " + "; ".join(problems)[:600]
                     + ". Call record_charter again with every dimension of the frame, exactly once each."},
                ]
                raw = self._ask(repair, topic)
                checked, _ = validate(raw, topic) if raw is not None else (None, [])
        except Exception:                 # a failed charter blocks nothing; nothing of it is logged
            return None
        if checked is None:
            return None
        return {"topic": topic, "dimensions": checked["dimensions"], "next": checked["next"]}


def charter_texts(charter: dict) -> list:
    """Every string the page would show: what the moderation gate reads."""
    texts = [d.get("captured") or "" for d in charter.get("dimensions") or [] if isinstance(d, dict)]
    texts.append(charter.get("next") or "")
    return [t for t in texts if isinstance(t, str) and t]


__all__ = ["Charter", "validate", "describe", "tool_for", "frame_ids", "charter_texts", "SYSTEM", "CAPS"]

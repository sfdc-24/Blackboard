"""The Muse: a third agent beside the host and the architect.

Owner direction, 2026-09-25: "have more thought provoking and creative AI agent
present beyond the Host, the Architect, who will spark questions around
creative inspiration, help with idea generation, show some templates and have
the user select what they like seeing, reading, hearing and working with."

Each call returns ONE spark (a question or provocation about the visitor's
idea) and exactly three directions the visitor can choose between. Each
direction is something to SEE (a palette, a motif, a typeface family), READ
(a headline and a line in that voice), HEAR (the tone that line is spoken in)
and WORK with (how the collaboration would feel). The page draws the
directions as selectable cards; the architect builds from the one chosen.

WHAT IS CHECKED HERE - model output is data, checked before it reaches anyone.
The whole set passes or the whole set is refused (then one repair attempt):
    - exactly three directions, ids exactly a, b and c
    - palettes are 3 to 5 #RRGGBB colours; the type is a closed enum
    - every string is non-empty, within its cap, one line, and free of
      markup characters (< >) and control characters
    - the directions are genuinely different: distinct titles, distinct
      types, and no two palettes the same

Model: the builder's model (STUDIO_WORKER_MODEL, claude-sonnet-5 in
production) through one forced tool call, so the answer is JSON only.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

MODEL = (os.environ.get("STUDIO_MUSE_MODEL") or os.environ.get("STUDIO_WORKER_MODEL")
         or "claude-sonnet-5")
TIMEOUT_SECONDS = 25.0
MAX_TOKENS = 1500
IDS = ("a", "b", "c")
TYPES = ("serif", "sans", "mono", "display", "script")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
# One line of plain text: no markup characters, no control characters.
BAD_CHARS_RE = re.compile(r"[<>\x00-\x1f\x7f]")
CAPS = {"line": 200, "title": 40, "motif": 60, "headline": 60, "read_line": 140, "tone": 80, "work": 120}

# The same use policy every lane carries (workers/policy.py, PR #255).
try:  # the app and the image import this module as part of the workers package
    from workers.policy import USE_POLICY as POLICY
except ImportError:  # loaded from its file (tests): read the sibling policy.py the same way
    import importlib.util as _util
    _spec = _util.spec_from_file_location(
        "studio_use_policy", os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.py"))
    _policy = _util.module_from_spec(_spec)
    _spec.loader.exec_module(_policy)
    POLICY = _policy.USE_POLICY

OFFER_TOOL = {
    "name": "offer_directions",
    "description": "Give one spark and exactly three creative directions. Call exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "line": {"type": "string", "description": "One thought-provoking question or spark, at most 200 characters."},
            "directions": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": list(IDS)},
                    "title": {"type": "string", "description": "At most 40 characters."},
                    "see": {"type": "object", "properties": {
                        "palette": {"type": "array", "items": {"type": "string"},
                                    "description": "3 to 5 colours as #RRGGBB."},
                        "motif": {"type": "string", "description": "At most 60 characters."},
                        "type": {"type": "string", "enum": list(TYPES)},
                    }, "required": ["palette", "motif", "type"], "additionalProperties": False},
                    "read": {"type": "object", "properties": {
                        "headline": {"type": "string", "description": "At most 60 characters."},
                        "line": {"type": "string", "description": "At most 140 characters."},
                    }, "required": ["headline", "line"], "additionalProperties": False},
                    "hear": {"type": "object", "properties": {
                        "tone": {"type": "string", "description": "How the line should sound, at most 80 characters."},
                    }, "required": ["tone"], "additionalProperties": False},
                    "work": {"type": "string", "description": "How working this way feels, at most 120 characters."},
                },
                "required": ["id", "title", "see", "read", "hear", "work"],
                "additionalProperties": False}},
        },
        "required": ["line", "directions"],
        "additionalProperties": False,
    },
}

SYSTEM = """You are the Muse in a live design session on sfdc24.com: a curious, imaginative creative \
director. A host welcomes the visitor and keeps notes; an architect builds what they describe on \
screen. Your part is inspiration. You spark the visitor's thinking and put three distinct creative \
directions in front of them, so they can point at what they like seeing, reading, hearing and \
working with.

Each turn you see the conversation, what is on the canvas, and the directions you offered before. Then:

1. line: ONE thought-provoking question or spark about their idea - something that opens it up \
("What should a customer feel in the first three seconds?"), not a form field. At most 200 characters.
2. directions: exactly three, with ids a, b and c, that are genuinely different - different moods, \
different palettes, different type families (each of serif, sans, mono, display or script at most \
once). For each:
   - title: a short evocative name, at most 40 characters.
   - see: 3 to 5 colours as #RRGGBB that work together; a motif (a shape, texture or image idea, \
at most 60 characters); a type family.
   - read: a headline (at most 60 characters) and one line (at most 140) written in that direction's \
voice, about the visitor's idea.
   - hear: the tone that line should be spoken in, at most 80 characters ("warm and unhurried, like \
a neighbour").
   - work: how building it this way would feel for the visitor, at most 120 characters.
If they already chose a direction, evolve from it rather than starting over; never repeat a title \
you offered before.

Plain text only: no markup, no emoji, no line breaks. Never state facts about the visitor's business \
they did not give you, and never name or promise any person, price or date. """ + POLICY + """ \
Finish by calling offer_directions once."""


class MuseUnavailable(RuntimeError):
    """The Muse gave no valid set, even after one repair attempt."""


# Every control, format, separator, private-use and surrogate code point: C0
# and C1 controls (U+0085 NEXT LINE is a line break), zero-width and bidi
# formatting, and U+2028/U+2029. One line of plain text only (Cursor NO-GO on
# 666a51f: a C1 control or a line separator passed the ASCII-only pattern and
# split the TTS instructions).
UNSAFE_CATEGORIES = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn")


def plain_text(value) -> bool:
    """True for one line of plain text: no markup brackets, no invisible or
    line-breaking code points."""
    return (isinstance(value, str) and not BAD_CHARS_RE.search(value)
            and not any(unicodedata.category(ch) in UNSAFE_CATEGORIES for ch in value))


def _plain(value, cap: int) -> bool:
    return (isinstance(value, str) and value.strip() != "" and len(value.strip()) <= cap
            and plain_text(value))


def validate(raw) -> tuple[dict | None, list]:
    """(the checked set, or None; problems). Nothing is repaired or partly kept."""
    problems = []
    if not isinstance(raw, dict) or set(raw) != {"line", "directions"}:
        return None, ["the answer must be exactly {line, directions}"]
    if not _plain(raw.get("line"), CAPS["line"]):
        problems.append("line is empty, too long or not plain text")
    directions = raw.get("directions")
    if not isinstance(directions, list) or len(directions) != 3:
        return None, problems + ["exactly three directions are required"]
    out = []
    for d in directions:
        if not isinstance(d, dict) or set(d) != {"id", "title", "see", "read", "hear", "work"}:
            problems.append("a direction has missing or extra fields")
            continue
        did = d.get("id")
        see, read, hear = d.get("see"), d.get("read"), d.get("hear")
        why = []
        if did not in IDS:
            why.append("id")
        if not _plain(d.get("title"), CAPS["title"]):
            why.append("title")
        if not isinstance(see, dict) or set(see) != {"palette", "motif", "type"}:
            why.append("see")
        else:
            palette = see.get("palette")
            if not isinstance(palette, list) or not 3 <= len(palette) <= 5 \
                    or not all(isinstance(c, str) and HEX_RE.match(c) for c in palette):
                why.append("palette")
            if not _plain(see.get("motif"), CAPS["motif"]):
                why.append("motif")
            if see.get("type") not in TYPES:
                why.append("type")
        if not isinstance(read, dict) or set(read) != {"headline", "line"}:
            why.append("read")
        else:
            if not _plain(read.get("headline"), CAPS["headline"]):
                why.append("headline")
            if not _plain(read.get("line"), CAPS["read_line"]):
                why.append("read line")
        if not isinstance(hear, dict) or set(hear) != {"tone"} or not _plain(hear.get("tone"), CAPS["tone"]):
            why.append("tone")
        if not _plain(d.get("work"), CAPS["work"]):
            why.append("work")
        if why:
            problems.append("direction %r: bad %s" % (str(did)[:4], ", ".join(why)))
            continue
        out.append({
            "id": did, "title": d["title"].strip(),
            "see": {"palette": [c.upper() for c in see["palette"]], "motif": see["motif"].strip(),
                    "type": see["type"]},
            "read": {"headline": read["headline"].strip(), "line": read["line"].strip()},
            "hear": {"tone": hear["tone"].strip()},
            "work": d["work"].strip(),
        })
    if problems:
        return None, problems
    if sorted(d["id"] for d in out) != list(IDS):
        return None, ["the ids must be a, b and c, once each"]
    if len({d["title"].lower() for d in out}) != 3:
        problems.append("two directions share a title")
    if len({d["see"]["type"] for d in out}) != 3:
        problems.append("two directions share a type family")
    if len({tuple(sorted(d["see"]["palette"])) for d in out}) != 3:
        problems.append("two directions share a palette")
    if problems:
        return None, problems
    out.sort(key=lambda d: d["id"])
    return {"line": raw["line"].strip(), "directions": out}, []


def _describe(state: dict, text: str, canvas: str) -> str:
    lines = ["CONVERSATION (oldest first):"]
    for turn in (state.get("transcript") or [])[-12:]:
        lines.append("- %s: %s" % (turn.get("role", "visitor"), str(turn.get("text", ""))[:600]))
    lines.append("- visitor (latest): " + (text or "(nothing new - inspire from what is on the canvas)"))
    lines.append("")
    lines.append("CANVAS NOW: " + (canvas or "nothing built yet"))
    domain = (state.get("model") or {}).get("domain")
    if domain:
        lines.append("WHAT IT IS ABOUT (the analyst's words): " + str(domain)[:80])
    before = (state.get("muse") or {}).get("directions") or []
    if before:
        lines.append("DIRECTIONS YOU OFFERED BEFORE: " + " | ".join(str(d.get("title", "")) for d in before))
    chosen = (state.get("muse") or {}).get("chosen")
    if chosen:
        lines.append("THE VISITOR CHOSE: " + str(chosen)[:80])
    return "\n".join(lines)[:8000]


class Muse:
    def __init__(self, client=None, model: str = MODEL):
        if client is None:
            import anthropic  # the official SDK; ANTHROPIC_API_KEY from Secret Manager
            client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=0)
        self.client, self.model = client, model

    def _ask(self, messages: list):
        resp = self.client.messages.create(
            model=self.model, max_tokens=MAX_TOKENS, system=SYSTEM, tools=[OFFER_TOOL],
            tool_choice={"type": "tool", "name": "offer_directions"}, messages=messages,
        )
        call = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"
                     and getattr(b, "name", "") == "offer_directions"), None)
        return (call.input if call is not None and isinstance(call.input, dict) else None), resp

    def inspire(self, state: dict, text: str, canvas: str) -> dict:
        """One checked set, after at most one repair attempt; else MuseUnavailable."""
        messages = [{"role": "user", "content": _describe(state, text, canvas)}]
        raw, _ = self._ask(messages)
        muse, problems = validate(raw) if raw is not None else (None, ["no offer_directions call"])
        if muse is not None:
            return {"muse": muse, "attempts": 1}
        repair = messages + [
            {"role": "assistant", "content": json.dumps(raw)[:4000] if raw is not None else "(no answer)"},
            {"role": "user", "content": "That answer was refused: " + "; ".join(problems)[:600]
             + ". Call offer_directions again with a corrected, complete set."},
        ]
        raw, _ = self._ask(repair)
        muse, problems = validate(raw) if raw is not None else (None, ["no offer_directions call"])
        if muse is None:
            raise MuseUnavailable("; ".join(problems)[:300])
        return {"muse": muse, "attempts": 2}


__all__ = ["Muse", "MuseUnavailable", "validate", "OFFER_TOOL", "SYSTEM", "IDS", "TYPES"]

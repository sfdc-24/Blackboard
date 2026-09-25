"""The Gemini advisor: a second agent that advises, and never builds.

Codex plan R2 (board row CODEX-GROK-SFDC24-CONVERSPAN-BRIEF-20260925T1130Z):
"isolated Gemini provider/advisor contract and offline tests, defaults
disabled". The builder (Claude) and this advisor read the SAME immutable
session snapshot: the artifact at one revision, the topic, the last words
said. The advisor returns typed advice bound to that revision - a one-line
perspective, at most two questions with options and a recommendation, and at
most three risks. It never returns a patch. Only the coordinator (the studio
controller) commits anything, and advice for any revision other than the
current one is fenced (dropped), so a slow advisor can never act on a stale
canvas. A failed, slow, disabled or malformed advisor returns None and blocks
nothing: the builder carries on without it.

Wiring into the controller is R3, behind STUDIO_ENABLE_ADVISOR (off by
default); this module is the contract and its tests.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
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

# A release must be reproducible from source.  Changing the provider model is a
# reviewed code change, not an unreported mutable environment override.
MODEL = "gemini-3.5-flash-lite"
URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
TIMEOUT_SECONDS = 6.0
MAX_TOKENS = 700
MAX_RESPONSE_BYTES = 32 * 1024
CALL_CAP = 12                      # advice calls per session
CAPS = {"perspective": 240, "prompt": 160, "why": 160, "label": 60, "risk": 120}
MAX_QUESTIONS = 2
MAX_RISKS = 3
OPTION_IDS = ("a", "b", "c", "d")
UNSAFE_CATEGORIES = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn")
MARKUP_RE = re.compile(r"[<>]")

SYSTEM = """You are the advisor in a live SFDC24 build session: a second pair of eyes beside the \
architect who is building what the visitor describes. You never build and never change the canvas. \
You read the same moment the architect reads and offer, in plain text only:
- perspective: one line on what the architect might be missing (at most 240 characters);
- questions: at most two questions worth asking the visitor next, each with 2 to 4 short options \
(ids a, b, c, d), a one-line reason, and the option you recommend;
- risks: at most three short risks (at most 120 characters each).
No markup, no emoji, no line breaks, no URLs. Never state facts about the visitor's business they did \
not give. Answer with JSON only, exactly in the requested shape. """ + USE_POLICY

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "perspective": {"type": "string"},
        "questions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "why": {"type": "string"},
                "options": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "label": {"type": "string"}},
                    "required": ["id", "label"]}},
                "recommended": {"type": "string"},
            },
            "required": ["prompt", "why", "options", "recommended"]}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["perspective", "questions", "risks"],
}


def plain(value, cap: int) -> bool:
    """One line of plain text, non-empty, within the cap."""
    return (isinstance(value, str) and value.strip() != "" and len(value.strip()) <= cap
            and not MARKUP_RE.search(value)
            and not any(unicodedata.category(ch) in UNSAFE_CATEGORIES for ch in value))


def validate(raw, revision: int) -> tuple[dict | None, list]:
    """(advice bound to revision, or None; problems). Nothing is repaired or partly kept."""
    problems = []
    if not isinstance(raw, dict) or set(raw) != {"perspective", "questions", "risks"}:
        return None, ["advice must have exactly perspective, questions and risks"]
    if not plain(raw["perspective"], CAPS["perspective"]):
        problems.append("bad perspective")
    questions = raw["questions"]
    if not isinstance(questions, list) or len(questions) > MAX_QUESTIONS:
        problems.append("at most %d questions" % MAX_QUESTIONS)
        questions = []
    clean_questions = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict) or set(q) != {"prompt", "why", "options", "recommended"}:
            problems.append("question %d has the wrong fields" % i)
            continue
        before = len(problems)
        if not plain(q["prompt"], CAPS["prompt"]) or not plain(q["why"], CAPS["why"]):
            problems.append("question %d text" % i)
        options = q["options"]
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            problems.append("question %d needs 2 to 4 options" % i)
            continue
        ids = []
        for o in options:
            if (not isinstance(o, dict) or set(o) != {"id", "label"} or not isinstance(o["id"], str)
                    or o["id"] not in OPTION_IDS or not plain(o["label"], CAPS["label"])):
                problems.append("question %d has a bad option" % i)
                break
            ids.append(o["id"])
        if len(set(ids)) != len(ids):
            problems.append("question %d repeats an option id" % i)
        if not isinstance(q["recommended"], str) or q["recommended"] not in ids:
            problems.append("question %d recommends an option it does not offer" % i)
        if len(problems) > before:        # a broken question is never cleaned: every field below is a checked str
            continue
        clean_questions.append({"id": "q%d" % (i + 1), "prompt": q["prompt"].strip(), "why": q["why"].strip(),
                                "options": [{"id": o["id"], "label": o["label"].strip()} for o in options],
                                "recommended": q["recommended"]})
    risks = raw["risks"]
    if not isinstance(risks, list) or len(risks) > MAX_RISKS or not all(plain(r, CAPS["risk"]) for r in risks):
        problems.append("at most %d short plain risks" % MAX_RISKS)
    if problems:
        return None, problems
    return {"agent": "gemini", "revision": int(revision), "perspective": raw["perspective"].strip(),
            "questions": clean_questions, "risks": [r.strip() for r in risks]}, []


def fenced(advice: dict | None, current_revision: int) -> bool:
    """True when advice must be dropped: it is for another revision of the canvas."""
    return not advice or int(advice.get("revision", -1)) != int(current_revision)


def snapshot_text(snapshot: dict) -> str:
    """What the advisor reads: the same moment the builder reads, as plain text."""
    parts = []
    if snapshot.get("topic_line"):
        parts.append(str(snapshot["topic_line"]).strip())
    parts.append("CANVAS (revision %d): %s" % (int(snapshot.get("revision") or 0),
                                              str(snapshot.get("canvas") or "The canvas is empty.")[:3000]))
    said = [str(t)[:600] for t in (snapshot.get("said") or [])][-4:]
    if said:
        parts.append("THE VISITOR SAID (most recent last):\n" + "\n".join("- " + s for s in said))
    return "\n\n".join(parts)


class Advisor:
    """Gemini, asked once per moment, answering with advice bound to that moment."""

    def __init__(self, *, key: str | None = None, enabled: bool = False, opener=None, clock=time.monotonic,
                 call_cap: int = CALL_CAP):
        self.key = os.environ.get("GEMINI_API_KEY", "") if key is None else key
        self.enabled = bool(enabled)
        self._open = opener or urllib.request.urlopen
        self.clock = clock
        self.call_cap = call_cap
        self._calls: dict = {}
        self._busy: set = set()
        self._lock = threading.Lock()

    def ready(self) -> bool:
        return self.enabled and bool(self.key)

    def advise(self, session_id: str, snapshot: dict) -> dict | None:
        """Advice bound to snapshot["revision"], or None - never an exception."""
        if not self.ready():
            return None
        if not isinstance(session_id, str) or not session_id:
            return None
        with self._lock:
            used = self._calls.get(session_id, 0)
            if used >= self.call_cap or session_id in self._busy:
                return None
            self._calls[session_id] = used + 1
            self._busy.add(session_id)
        deadline = self.clock() + TIMEOUT_SECONDS
        try:
            revision = snapshot.get("revision")
            if type(revision) is not int or revision < 0:
                return None
            advice, problems = validate(self._ask(snapshot_text(snapshot)), revision)
            if self.clock() >= deadline:
                return None
        except Exception:                  # a failed advisor blocks nothing; nothing of it is logged
            return None
        finally:
            with self._lock:
                self._busy.discard(session_id)
        return advice if not problems else None

    def _ask(self, text: str) -> dict:
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"maxOutputTokens": MAX_TOKENS, "responseMimeType": "application/json",
                                 "responseSchema": RESPONSE_SCHEMA, "thinkingConfig": {"thinkingLevel": "minimal"}},
        }).encode("utf-8")
        request = urllib.request.Request(URL % MODEL, data=body, method="POST", headers={
            "x-goog-api-key": self.key,           # the key goes in a header, never the URL
            "Content-Type": "application/json",
        })
        with self._open(request, timeout=TIMEOUT_SECONDS) as response:
            headers = getattr(response, "headers", None)
            declared = headers.get("Content-Length") if headers is not None else None
            if declared not in (None, ""):
                declared_size = int(declared)
                if declared_size < 0 or declared_size > MAX_RESPONSE_BYTES:
                    raise ValueError("advisor response is outside the byte limit")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("advisor response is outside the byte limit")
            payload = json.loads(raw.decode("utf-8"))
        candidates = payload.get("candidates") or []
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or [] if candidates else []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        return json.loads(text)


__all__ = ["Advisor", "validate", "fenced", "snapshot_text", "SYSTEM", "CAPS",
           "MODEL", "MAX_RESPONSE_BYTES"]

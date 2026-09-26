"""The analyst lane: a second agent working beside the builder on the same session.

While the builder turns what the visitor says into something on the canvas
within seconds, the analyst works in parallel on what the builder cannot see
from one sentence: it may research the visitor's domain on the web, maps the
data model the idea needs (objects, fields, relationships - Salesforce terms
where they fit) and decides the single most useful question to ask next. It
never edits the prototype. The controller records its model and question
beside the artifact (core.StudioController.commit_analysis), outside the
builder's command lock, so the two agents never wait on each other.

WHAT IS CHECKED HERE - model output is data, checked before it reaches anyone:
    - ids are contract ids, unique; names and texts are capped
    - relationships name objects that exist; a bad one is dropped, not guessed
    - at most MAX_OBJECTS objects, MAX_FIELDS fields each, MAX_LINKS links
    - a question has 2-3 options with unique ids and plain text
    - findings are short sentences; nothing is presented as a fact about the
      visitor's own business unless they said it

Model: claude-opus-5 at low effort with web search (at most two searches). It
runs in parallel with the builder, so a few seconds of research never delay
what the visitor sees first.
"""
from __future__ import annotations

import json
import os
import re

try:  # the app and the image import this module as part of the workers package
    from workers.policy import USE_POLICY
except ImportError:  # loaded from its file (tests): read the sibling policy.py the same way
    import importlib.util as _util
    _spec = _util.spec_from_file_location(
        "studio_use_policy", os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.py"))
    _policy = _util.module_from_spec(_spec)
    _spec.loader.exec_module(_policy)
    USE_POLICY = _policy.USE_POLICY

MODEL = os.environ.get("STUDIO_ANALYST_MODEL") or "claude-opus-5"
EFFORT = os.environ.get("STUDIO_ANALYST_EFFORT") or "low"
TIMEOUT_SECONDS = 40.0
RESEARCH = (os.environ.get("STUDIO_ANALYST_RESEARCH") or "1") == "1"
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
TEXT_MAX = 300
NAME_MAX = 80
MAX_OBJECTS = 10
MAX_FIELDS = 12
MAX_LINKS = 16
MAX_FINDINGS = 4
LINK_KINDS = ("lookup", "master-detail", "many-to-many")

RECORD_TOOL = {
    "name": "record_analysis",
    "description": "Record the data model, what you found, and the next question. Call exactly once, last.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Two to five words naming what is being designed."},
            "objects": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "standard": {"type": "boolean"},
                    "purpose": {"type": "string"},
                    "fields": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "type": {"type": "string"}},
                        "required": ["name", "type"], "additionalProperties": False}},
                },
                "required": ["id", "name", "standard", "purpose", "fields"],
                "additionalProperties": False}},
            "relationships": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"}, "to": {"type": "string"},
                    "kind": {"type": "string", "enum": list(LINK_KINDS)},
                    "label": {"type": "string"},
                },
                "required": ["from", "to", "kind", "label"], "additionalProperties": False}},
            "findings": {"type": "array", "items": {"type": "string"}},
            "question": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "reason": {"type": "string"},
                    "options": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"option_id": {"type": "string"}, "label": {"type": "string"},
                                       "consequence": {"type": "string"}},
                        "required": ["option_id", "label", "consequence"], "additionalProperties": False}},
                },
                "required": ["prompt", "reason", "options"], "additionalProperties": False},
        },
        "required": ["domain", "objects", "relationships", "findings", "question"],
        "additionalProperties": False,
    },
}

SYSTEM = """You are the analyst in a live design session on sfdc24.com. A visitor is describing \
something they want - a website, an app, a process, a Salesforce solution - and while a separate \
builder draws it on screen, you work out what it needs underneath and what to ask next. You never \
draw or edit the prototype.

Each turn you see the conversation so far, a summary of the prototype as it stands, your previous \
data model, and the questions already asked. Then:

1. If the domain is unfamiliar or the visitor named a specific industry, product or regulation, \
you may search the web (at most twice) for how such businesses are usually structured. Skip \
research when the idea is already clear.
2. Map the data model the idea needs: the objects (records) it will store, a few key fields each \
with a plain type (Text, Number, Currency, Date, Picklist, Checkbox, Email, Phone, Lookup), and \
the relationships between them. Use Salesforce standard objects (Account, Contact, Lead, \
Opportunity, Case, Product, Order...) where they fit and mark them standard; name custom ones \
plainly ("Booking", "Menu Item"). Keep it to what this idea needs - usually 3 to 7 objects. Carry \
your previous model forward and change only what the conversation changed. Object ids are short \
and stable ("account", "booking").
3. findings: up to four short sentences the visitor would find useful - what you learned or \
assumed and why it matters for the design. Never state facts about the visitor's own business \
they did not give you; say "usually" or "often" for general knowledge.
4. question: the single most useful thing still undecided that would change the data model or \
the build, with 2 or 3 concrete options and the consequence of each. Never repeat a question \
already asked. If nothing material is open, use an empty prompt, an empty reason and no options.

Plain words, no jargon beyond object and field names. Finish by calling record_analysis once - \
unless the request falls outside the use policy below; then do not call it and do not research it.

""" + USE_POLICY


def _txt(s, cap=TEXT_MAX):
    return isinstance(s, str) and s.strip() != "" and len(s) <= cap


def _describe(state: dict, text: str, canvas: str) -> str:
    lines = ["CONVERSATION (oldest first):"]
    for turn in (state.get("transcript") or [])[-16:]:
        lines.append("- %s: %s" % (turn.get("role", "visitor"), turn.get("text", "")))
    lines.append("- visitor (latest): %s" % text)
    lines.append("")
    lines.append("PROTOTYPE NOW: " + (canvas or "nothing built yet"))
    lines.append("")
    lines.append("YOUR PREVIOUS MODEL: " + json.dumps(state.get("model") or {}, separators=(",", ":"))[:6000])
    asked = [q.get("prompt", "") for q in state.get("questions") or []]
    lines.append("QUESTIONS ALREADY ASKED: " + (" | ".join(asked) if asked else "none"))
    return "\n".join(lines)


def validate(raw: dict) -> tuple[dict, dict | None, list]:
    """(model, question or None, problems). Bad parts are dropped, never repaired."""
    problems = []
    domain = raw.get("domain") if _txt(raw.get("domain"), NAME_MAX) else ""
    objects, ids = [], set()
    for o in (raw.get("objects") or [])[:MAX_OBJECTS]:
        oid = o.get("id", "")
        if not ID_RE.match(oid) or oid in ids or not _txt(o.get("name"), NAME_MAX):
            problems.append("object %r dropped: id or name invalid" % str(oid)[:40])
            continue
        fields = []
        for f in (o.get("fields") or [])[:MAX_FIELDS]:
            if _txt(f.get("name"), NAME_MAX) and _txt(f.get("type"), 40):
                fields.append({"name": f["name"].strip(), "type": f["type"].strip()})
        ids.add(oid)
        objects.append({"id": oid, "name": o["name"].strip(), "standard": bool(o.get("standard")),
                        "purpose": o["purpose"].strip() if _txt(o.get("purpose")) else "", "fields": fields})
    links = []
    for r in (raw.get("relationships") or [])[:MAX_LINKS]:
        if r.get("from") in ids and r.get("to") in ids and r.get("kind") in LINK_KINDS \
                and isinstance(r.get("label"), str) and len(r["label"]) <= NAME_MAX:
            links.append({"from": r["from"], "to": r["to"], "kind": r["kind"], "label": r["label"].strip()})
        else:
            problems.append("relationship %r -> %r dropped" % (str(r.get("from"))[:40], str(r.get("to"))[:40]))
    findings = [f.strip() for f in (raw.get("findings") or [])[:MAX_FINDINGS] if _txt(f)]
    model = {"domain": domain, "objects": objects, "relationships": links, "findings": findings}

    question = None
    q = raw.get("question") or {}
    if (q.get("prompt") or "").strip():
        opts = q.get("options") or []
        why = []
        if not _txt(q.get("prompt")) or not _txt(q.get("reason")):
            why.append("prompt or reason empty or too long")
        if not 2 <= len(opts) <= 3:
            why.append("%d options" % len(opts))
        if len({o.get("option_id") for o in opts}) != len(opts) or \
                not all(ID_RE.match(o.get("option_id", "")) for o in opts):
            why.append("option ids duplicate or malformed")
        if not all(_txt(o.get("label"), NAME_MAX) and _txt(o.get("consequence")) for o in opts):
            why.append("option text empty or too long")
        if why:
            problems.append("question dropped: " + "; ".join(why))
        else:
            question = {"prompt": q["prompt"].strip(), "reason": q["reason"].strip(),
                        "options": [{"option_id": o["option_id"], "label": o["label"].strip(),
                                     "consequence": o["consequence"].strip()} for o in opts]}
    return model, question, problems


class Analyst:
    def __init__(self, client=None, model: str = MODEL, effort: str = EFFORT, research: bool = RESEARCH):
        if client is None:
            import anthropic  # the official SDK; ANTHROPIC_API_KEY from Secret Manager
            # 60 s was Cloud Run's own request timeout: the owner's run lost an
            # analysis to a 504 at exactly 60.0 s. Give up inside the request.
            client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=0)
        self.client, self.model, self.effort, self.research = client, model, effort, research

    def analyze(self, state: dict, text: str, canvas: str) -> dict:
        tools = [RECORD_TOOL]
        if self.research:
            tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}, RECORD_TOOL]
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=SYSTEM,
            output_config={"effort": self.effort},
            tools=tools,
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": _describe(state, text, canvas)}],
        )
        call = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"
                     and getattr(b, "name", "") == "record_analysis"), None)
        if call is None:
            return {"model": None, "question": None,
                    "problems": ["analyst did not record an analysis (stop %s)" % resp.stop_reason]}
        raw = call.input if isinstance(call.input, dict) else {}
        model, question, problems = validate(raw)
        searched = sum(1 for b in resp.content if getattr(b, "type", "") == "server_tool_use")
        return {"model": model, "question": question, "problems": problems, "searched": searched}


__all__ = ["Analyst", "validate", "RECORD_TOOL", "SYSTEM"]

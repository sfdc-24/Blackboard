"""The Claude worker for the studio: what the visitor said -> the next change and question.

Contract: sfdc24-site studio/contract/ (events.schema.json, README.md). The
controller owns the envelope (seq, op_id, versions, revisions) and all
bookkeeping; this worker owns only CONTENT - which nodes change, what to ask
next, how to confirm - and returns event DRAFTS: {"type", "payload"}.

WHY THE OUTPUT IS FLAT
    The artifact is a tree, and the API's structured outputs do not accept a
    recursive schema. So the model never returns a tree. It returns flat patch
    ops (set_label, set_detail, insert_child with one childless node, remove)
    against node ids that already exist, and at most one question or one short
    decision form. The controller applies the ops; the page renders the tree.

WHAT IS CHECKED BEFORE ANYTHING LEAVES HERE - the schema alone cannot say these:
    - every op names a node that exists (or, for insert_child, a parent that
      exists and a new id that does not)
    - every question's affected ids exist once the ops are applied
    - 2-3 options, unique ids, at most one Recommended and it carries a reason
    - no text over the contract's caps
    A draft that fails is DROPPED, never repaired by guessing, and the failure
    is returned for the controller to log. A visitor sees no change rather than
    a wrong one.

Model: claude-opus-5 at low effort - this runs inside a spoken conversation,
so latency matters more than depth. Server-side refusal fallback is on.
"""
from __future__ import annotations

import json
import os
import re

MODEL = os.environ.get("STUDIO_WORKER_MODEL") or "claude-opus-5"
EFFORT = os.environ.get("STUDIO_WORKER_EFFORT") or "low"
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
TEXT_MAX = 600
KINDS = ["screen", "section", "heading", "text", "button", "image-placeholder",
         "form", "field", "list", "card", "nav", "process-step", "edge"]
GROUPS = ["Strategy", "Audience", "Structure", "Screen", "Component", "Content",
          "Behaviour", "Data", "Accessibility", "Release"]

_OPTION = {
    "type": "object",
    "properties": {
        "option_id": {"type": "string"},
        "label": {"type": "string"},
        "consequence": {"type": "string"},
        "recommended": {"type": "boolean"},
        "recommended_because": {"type": "string"},
    },
    "required": ["option_id", "label", "consequence", "recommended", "recommended_because"],
    "additionalProperties": False,
}
_QUESTION = {
    "type": "object",
    "properties": {
        "question_id": {"type": "string"},
        "group": {"type": "string", "enum": GROUPS},
        "scope_path": {"type": "string"},
        "reason": {"type": "string"},
        "prompt": {"type": "string"},
        "options": {"type": "array", "items": _OPTION},
        "affected_artifact_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["question_id", "group", "scope_path", "reason", "prompt", "options",
                 "affected_artifact_ids"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ops": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["set_label", "set_detail", "insert_child", "remove"]},
                "node_id": {"type": "string"},
                "value": {"type": "string"},
                "new_node": {"type": "object", "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": KINDS},
                    "label": {"type": "string"},
                    "detail": {"type": "string"},
                }, "required": ["id", "kind", "label", "detail"], "additionalProperties": False},
            },
            "required": ["op", "node_id", "value", "new_node"],
            "additionalProperties": False,
        }},
        "confirm": {"type": "string"},
        "questions": {"type": "array", "items": _QUESTION},
        "batch_title": {"type": "string"},
    },
    "required": ["ops", "confirm", "questions", "batch_title"],
    "additionalProperties": False,
}

SYSTEM = """You run the build side of a live prototyping session on sfdc24.com. A visitor is \
describing something they want built - usually a Salesforce-related screen, page, form or process. \
You see the current prototype as a list of typed nodes, the questions already asked and answered, \
and the latest thing the visitor said or chose.

Each turn you return, as JSON:
- ops: the smallest set of changes that makes the prototype reflect what is now decided. Only \
change nodes the latest input actually affects. Use existing node ids. For insert_child, node_id \
is the PARENT and new_node is one new node with a new unique id. Unused fields are "" (and for \
ops other than insert_child, new_node is {"id":"","kind":"text","label":"","detail":""}).
- confirm: one plain sentence naming exactly what changed and why ("Using X. The hero action now \
opens Y."). Empty string if nothing changed.
- questions: normally ONE question - the single most useful thing still undecided that would \
visibly change the prototype. Use two or three only when they are independent quick decisions a \
visitor can answer together; then set batch_title to a short heading for the form. Zero when \
nothing material is left.

Each question: scope_path says where it fits ("Homepage > Hero > Primary action"); reason is one \
sentence on why it matters now; 2 or 3 concrete options, each with a consequence the visitor \
would SEE. Mark at most one option recommended, only when there is a real reason, and put that \
reason in recommended_because (else ""). affected_artifact_ids lists the node ids the answer \
would change. Never ask about something already answered. Plain words, no jargon, nothing \
about how this system works. Labels are plain text - never HTML or code.

ONE AREA AT A TIME. On the first turn you may lay out the page skeleton. After that, change only \
the area the latest answer is about; anything else waits for its own question.

NEVER INVENT FACTS ABOUT THE VISITOR'S BUSINESS. No testimonials, client names, quotes, numbers, \
prices, awards or results they did not give you. Where the design needs one, use a visible \
placeholder such as "[Client quote]" or "[Number of properties managed]"."""


def _flatten(node, out, parent=None):
    out[node["id"]] = {"kind": node.get("kind"), "label": node.get("label", ""),
                       "detail": node.get("detail", ""), "parent": parent}
    for child in node.get("children") or []:
        _flatten(child, out, node["id"])
    return out


def _txt(s):
    return isinstance(s, str) and len(s) <= TEXT_MAX


def validate(draft: dict, artifact_root: dict, answered_ids: set) -> tuple[dict | None, list]:
    """Semantic checks the schema cannot express. Returns (clean_draft, problems)."""
    problems = []
    nodes = _flatten(artifact_root, {})
    existing = set(nodes)
    ops_out = []
    for i, op in enumerate(draft.get("ops") or []):
        kind, nid = op.get("op"), op.get("node_id", "")
        if nid not in existing:
            problems.append("op %d targets unknown node %r" % (i, nid)); continue
        if kind in ("set_label", "set_detail"):
            if not _txt(op.get("value")):
                problems.append("op %d value too long" % i); continue
            ops_out.append({"op": kind, "node_id": nid, "value": op["value"]})
        elif kind == "insert_child":
            nn = op.get("new_node") or {}
            if not ID_RE.match(nn.get("id", "")) or nn["id"] in existing:
                problems.append("op %d new id %r missing, malformed or taken" % (i, nn.get("id"))); continue
            if nn.get("kind") not in KINDS or not _txt(nn.get("label")) or not _txt(nn.get("detail", "")):
                problems.append("op %d new node invalid" % i); continue
            new = {"id": nn["id"], "kind": nn["kind"], "label": nn["label"]}
            if nn.get("detail"):
                new["detail"] = nn["detail"]
            existing.add(nn["id"])
            ops_out.append({"op": "insert_child", "node_id": nid, "node": new})
        elif kind == "remove":
            if nodes[nid]["parent"] is None:
                problems.append("op %d would remove the root" % i); continue
            ops_out.append({"op": "remove", "node_id": nid})
        else:
            problems.append("op %d unknown kind %r" % (i, kind))

    questions = []
    for q in (draft.get("questions") or [])[:4]:
        qid = q.get("question_id", "")
        why = []
        if not ID_RE.match(qid) or qid in answered_ids:
            why.append("id %r malformed or already answered" % qid)
        opts = q.get("options") or []
        if not 2 <= len(opts) <= 3:
            why.append("%d options" % len(opts))
        if len({o.get("option_id") for o in opts}) != len(opts) or \
                not all(ID_RE.match(o.get("option_id", "")) for o in opts):
            why.append("option ids duplicate or malformed")
        rec = [o for o in opts if o.get("recommended")]
        if len(rec) > 1 or any(not (o.get("recommended_because") or "").strip() for o in rec):
            why.append("recommended more than once or without a reason")
        aff = q.get("affected_artifact_ids") or []
        if not aff or any(a not in existing for a in aff):
            why.append("affected ids missing or unknown")
        for f in ("scope_path", "reason", "prompt"):
            if not _txt(q.get(f)) or not q.get(f, "").strip():
                why.append("%s empty or too long" % f)
        if why:
            problems.append("question %r dropped: %s" % (qid, "; ".join(why))); continue
        clean_opts = []
        for o in opts:
            c = {"option_id": o["option_id"], "label": o["label"], "consequence": o["consequence"]}
            if o.get("recommended"):
                c["recommended"] = True
                c["recommended_because"] = o["recommended_because"]
            clean_opts.append(c)
        # Recommended first - the page shows options in this order.
        clean_opts.sort(key=lambda c: 0 if c.get("recommended") else 1)
        questions.append({"question_id": qid, "group": q["group"], "scope_path": q["scope_path"],
                          "reason": q["reason"], "prompt": q["prompt"], "options": clean_opts,
                          "status": "open", "affected_artifact_ids": aff})

    confirm = draft.get("confirm") or ""
    if not _txt(confirm):
        problems.append("confirm too long"); confirm = ""
    return {"ops": ops_out, "confirm": confirm, "questions": questions,
            "batch_title": (draft.get("batch_title") or "")[:TEXT_MAX]}, problems


def apply_ops(root: dict, ops: list) -> dict:
    """Apply validated ops to a copy of the tree. The controller and the page do the same."""
    tree = json.loads(json.dumps(root))
    index, parents = {}, {}

    def walk(n, p=None):
        index[n["id"]] = n
        parents[n["id"]] = p
        for c in n.get("children") or []:
            walk(c, n)
    walk(tree)
    for op in ops:
        n = index.get(op["node_id"])
        if n is None:
            continue
        if op["op"] == "set_label":
            n["label"] = op["value"]
        elif op["op"] == "set_detail":
            n["detail"] = op["value"]
        elif op["op"] == "insert_child":
            n.setdefault("children", []).append(dict(op["node"]))
            walk(n["children"][-1], n)
        elif op["op"] == "remove" and parents[op["node_id"]] is not None:
            p = parents[op["node_id"]]
            p["children"] = [c for c in p["children"] if c["id"] != op["node_id"]]
    return tree


def to_drafts(clean: dict, batch_id: str) -> list[dict]:
    """Event drafts in Ask/Point/Change/Confirm order: change, confirm, then the next ask."""
    out = []
    if clean["ops"]:
        out.append({"type": "artifact.patch", "payload": {"ops": clean["ops"]}})
    if clean["confirm"] and clean["ops"]:
        ids = sorted({o["node_id"] for o in clean["ops"]})
        out.append({"type": "confirm", "payload": {"text": clean["confirm"], "artifact_ids": ids}})
    qs = clean["questions"]
    if len(qs) == 1:
        out.append({"type": "question.asked", "payload": {"question": qs[0]}})
    elif len(qs) > 1:
        out.append({"type": "decision.batch", "payload": {
            "batch_id": batch_id, "title": clean["batch_title"] or "A few quick decisions",
            "questions": qs}})
    return out


def _describe(state: dict, trigger: dict) -> str:
    nodes = _flatten(state["artifact"], {})
    lines = ["CURRENT PROTOTYPE (id | kind | label | detail | parent):"]
    for nid, n in nodes.items():
        lines.append("%s | %s | %s | %s | %s" % (nid, n["kind"], n["label"], n["detail"], n["parent"] or "-"))
    lines.append("\nQUESTIONS SO FAR:")
    for q in state.get("questions") or []:
        pick = q.get("selected_option") or q.get("freeform_answer") or ""
        lines.append("%s [%s] %s -> %s" % (q["question_id"], q.get("status"), q.get("prompt"), pick))
    lines.append("\nRECENT CONVERSATION:")
    for t in (state.get("transcript") or [])[-12:]:
        lines.append("%s: %s" % (t.get("role", "visitor"), t.get("text", "")))
    lines.append("\nLATEST INPUT: " + json.dumps(trigger, ensure_ascii=False))
    return "\n".join(lines)


class ClaudeWorker:
    """on_turn(state, trigger) -> {"events": [drafts], "problems": [...]}.

    state:   {"artifact": root node, "questions": [question records],
              "transcript": [{"role", "text"}], "session_id": str, "turn_seq": int}
    trigger: {"kind": "utterance", "text"} | {"kind": "answer", "question_id",
              "option_id" | "freeform_answer"} | {"kind": "answer_batch", "answers": [...]}
    """

    def __init__(self, client=None, model: str = MODEL, effort: str = EFFORT):
        if client is None:
            import anthropic  # the official SDK; ANTHROPIC_API_KEY from Secret Manager
            client = anthropic.Anthropic()
        self.client, self.model, self.effort = client, model, effort

    def on_turn(self, state: dict, trigger: dict) -> dict:
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": _describe(state, trigger)}],
        )
        if resp.stop_reason == "refusal":
            return {"events": [], "problems": ["model refused this turn"]}
        if resp.stop_reason == "max_tokens":
            return {"events": [], "problems": ["model output truncated"]}
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        try:
            draft = json.loads(text)
        except ValueError:
            return {"events": [], "problems": ["model output was not JSON"]}
        answered = {q["question_id"] for q in state.get("questions") or []
                    if q.get("status") in ("answered", "superseded")}
        clean, problems = validate(draft, state["artifact"], answered)
        batch_id = "b-%s-%s" % (state.get("session_id", "s"), state.get("turn_seq", 0))
        return {"events": to_drafts(clean, batch_id), "problems": problems}

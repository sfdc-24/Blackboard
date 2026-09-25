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
import uuid

MODEL = os.environ.get("STUDIO_WORKER_MODEL") or "claude-opus-5"
EFFORT = os.environ.get("STUDIO_WORKER_EFFORT") or "low"
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
TEXT_MAX = 600
MAX_CHILDREN = 60
KINDS = ["screen", "section", "heading", "text", "button", "image-placeholder",
         "form", "field", "list", "card", "nav", "process-step", "edge", "scene", "entity"]

# LIVE SCENES. A "scene" node is a stage the homepage runs as a real-time
# engine (60 fps): its detail is its size, background and gravity
# ("1200x500 bg=#0B3D2E gravity=600"). Its children are "entity" nodes whose
# detail is one closed statement: a type, then key=value pairs from that
# type's allowlist - paint, geometry, motion, physics and pointer behaviour.
# Every value is a number, a colour, an enum or tightly-charactered path
# data, so the page builds and animates each entity without parsing markup
# or running code. The page applies this same grammar again before drawing
# (sfdc24-site assets/prototype-canvas.js); keep the two in step.
SCENE_RE = re.compile(r"^(\d{2,4})x(\d{2,4})((?: (?:bg=(?:#[0-9A-Fa-f]{6}|#[0-9A-Fa-f]{3}|none)|gravity=-?\d{1,4}))*)$")
# One statement is printable ASCII tokens separated by single spaces: what the
# gate checks is exactly the string stored and drawn, never a trimmed copy.
STATEMENT_RE = re.compile(r"^[!-~]+(?: [!-~]+)*$")
_N = r"-?\d{1,5}(?:\.\d{1,3})?"
NUM_RE = re.compile("^" + _N + "$")
COLOUR_RE = re.compile(r"^(?:#[0-9A-Fa-f]{6}|#[0-9A-Fa-f]{3}|none)$")
POINTS_RE = re.compile("^%s,%s(?:;%s,%s){1,39}$" % (_N, _N, _N, _N))
ORBIT_RE = re.compile("^%s,%s,%s,%s$" % (_N, _N, _N, _N))
PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz][MmLlHhVvCcSsQqTtAaZz0-9.,\-]{0,499}$")
_PAINT = {"fill": "colour", "stroke": "colour", "stroke-width": "pos", "opacity": "unit", "rotate": "num",
          "glow": "colour"}
_MOTION = {"vx": "num", "vy": "num", "spin": "num", "pulse": "unit", "period": "pos", "float": "num",
           "orbit": "orbit", "body": "bit", "bounce": "bit", "wrap": "bit", "drag": "bit", "tap": "tap",
           "delay": "num", "solid": "bit", "attach": "ref"}
_BASE = dict(_PAINT, **_MOTION)
SHAPES = {
    "rect": dict(_BASE, x="num", y="num", width="pos", height="pos", rx="pos"),
    "circle": dict(_BASE, cx="num", cy="num", r="pos"),
    "ellipse": dict(_BASE, cx="num", cy="num", rx="pos", ry="pos"),
    "line": dict(_BASE, x1="num", y1="num", x2="num", y2="num"),
    "polygon": dict(_BASE, points="points"),
    "path": dict(_BASE, d="path"),
    "text": dict(_BASE, x="num", y="num", size="pos", weight="weight", anchor="anchor", font="font",
                 spacing="num"),
    "particles": dict(_BASE, x="num", y="num", rate="pos", size="pos", speed="pos", angle="num",
                      spread="pos", life="pos", shape="dot"),
}
# The geometry each type cannot be drawn without.
REQUIRED = {"rect": ("x", "y", "width", "height"), "circle": ("cx", "cy", "r"),
            "ellipse": ("cx", "cy", "rx", "ry"), "line": ("x1", "y1", "x2", "y2"),
            "polygon": ("points",), "path": ("d",), "text": ("x", "y"), "particles": ("x", "y")}
_VALUES = {
    "num": NUM_RE.match,
    "pos": lambda v: NUM_RE.match(v) and float(v) >= 0,
    "colour": COLOUR_RE.match,
    "unit": lambda v: NUM_RE.match(v) and 0 <= float(v) <= 1,
    "bit": lambda v: v in ("0", "1"),
    "ref": ID_RE.match,
    "points": POINTS_RE.match,
    "orbit": ORBIT_RE.match,
    # A path starts with a moveto and carries at least one coordinate pair.
    "path": lambda v: bool(PATH_RE.match(v)) and v[0] in "Mm" and len(re.findall(r"[0-9]+(?:[.][0-9]+)?", v)) >= 2,
    "weight": lambda v: v in ("400", "500", "600", "700", "800", "900"),
    "anchor": lambda v: v in ("start", "middle", "end"),
    "font": lambda v: v in ("sans", "serif", "mono", "display"),
    "tap": lambda v: v in ("pulse", "spin", "burst", "jump", "hide"),
    "dot": lambda v: v in ("circle", "square", "star"),
}


def visual_problem(kind: str, detail: str, parent_kind: str | None) -> str | None:
    """Why a scene or entity node cannot be run, or None when it can."""
    detail = detail or ""
    if kind in ("scene", "entity") and not STATEMENT_RE.match(detail):
        return "%s detail must be printable words separated by single spaces" % kind
    if kind == "scene":
        m = SCENE_RE.match(detail)
        if not m or not (16 <= int(m.group(1)) <= 2400 and 16 <= int(m.group(2)) <= 2400):
            return "scene detail must be WIDTHxHEIGHT (16-2400) with optional bg=#hex and gravity="
        keys = [pair.split("=")[0] for pair in m.group(3).split()]
        if len(keys) != len(set(keys)):
            return "scene detail repeats a setting"
        return None
    if kind != "entity":
        return None
    if parent_kind != "scene":
        return "an entity must sit directly inside a scene"
    parts = detail.split()
    if not parts or parts[0] not in SHAPES:
        return "entity detail must start with one of " + ", ".join(SHAPES)
    allowed, seen = SHAPES[parts[0]], set()
    for pair in parts[1:]:
        key, eq, value = pair.partition("=")
        if not eq or key not in allowed or key in seen or not _VALUES[allowed[key]](value):
            return "entity %s has a bad setting %r" % (parts[0], pair[:40])
        seen.add(key)
    missing = [key for key in REQUIRED[parts[0]] if key not in seen]
    if missing:
        return "entity %s is missing %s" % (parts[0], ", ".join(missing))
    return None


def attach_problem(node_id: str, detail: str, index: dict, parents: dict) -> str | None:
    """attach=<id> names a sibling entity in the same scene that is not itself a part,
    and the entity being attached has no parts of its own: one level, no self-link, no cycle."""
    target = next((p.split("=", 1)[1] for p in (detail or "").split(" ") if p.startswith("attach=")), None)
    here = parents.get(node_id)
    siblings = (here or {}).get("children") or []
    if any(c.get("id") != node_id and ("attach=" + node_id) in (c.get("detail") or "").split(" ") for c in siblings):
        if target is not None:
            return "%r has parts attached to it, so it cannot be a part itself" % node_id
    if target is None:
        return None
    other = index.get(target)
    if target == node_id or other is None or other.get("kind") != "entity" or parents.get(target) is not here:
        return "attach=%s must name another entity in the same scene" % target[:40]
    if any(p.startswith("attach=") for p in (other.get("detail") or "").split(" ")):
        return "attach=%s names an entity that is itself a part" % target[:40]
    return None


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
        "resolves": {"type": "object", "properties": {
            "question_id": {"type": "string"},
            "option_id": {"type": "string"},
            "freeform_answer": {"type": "string"},
        }, "required": ["question_id", "option_id", "freeform_answer"], "additionalProperties": False},
    },
    "required": ["ops", "confirm", "questions", "batch_title", "resolves"],
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

- resolves: when the latest input is the visitor answering an OPEN question - in their own \
words, often spoken - set question_id to it, and option_id to the option they clearly chose, or \
leave option_id "" and put their answer in freeform_answer when it matches no option. If the input \
answers nothing open, all three are "". Never guess an option they did not choose.

Each question: scope_path says where it fits ("Homepage > Hero > Primary action"); reason is one \
sentence on why it matters now; 2 or 3 concrete options, each with a consequence the visitor \
would SEE. Mark at most one option recommended, only when there is a real reason, and put that \
reason in recommended_because (else ""). affected_artifact_ids lists the node ids the answer \
would change. Never ask about something already answered. Plain words, no jargon, nothing \
about how this system works. Labels are plain text - never HTML or code.

BUILDING FROM AN EMPTY SCREEN. When the prototype is only the root screen with no children, the \
visitor's latest input says what they want built. Build a complete, usable first version of it in \
this one turn: insert every section, heading, text, form, field, list, card and button the request \
needs, nesting children under the new ids you insert earlier in the same ops list (insert the parent \
first, then its children). Make it the real thing they asked for, with sensible field names, list \
items and button labels drawn from their request - not a generic page. A field's label is the field \
name and its detail is its type or an example value; a button's label is its action. One screen. \
Also set_label the root screen to a short name for what is being designed ("Dental lead intake form"), \
so the canvas is never left called "Blank canvas".

ONE AREA AT A TIME. After the first version, change only the area the latest input is about; \
anything else waits for its own question. When the visitor asks for something new, add it; when \
they ask to remove or rename something, do exactly that.

NEVER INVENT FACTS ABOUT THE VISITOR'S BUSINESS. No testimonials, client names, quotes, numbers, \
prices, awards or results they did not give you. Where the design needs one, use a visible \
placeholder such as "[Client quote]" or "[Number of properties managed]".

LIVE SCENES - LOGOS, BANNERS, ANIMATIONS, GAMES, DIAGRAMS. Anything visual is built as a live \
scene the page runs like a game engine, never as a static picture. Insert a "scene" node (label: \
what it is; detail: size, background and optional gravity in px/s^2, for example "1200x500 \
bg=#0B3D2E" or "900x600 bg=#101820 gravity=900") and build it from "entity" children inserted \
directly under it. An entity's label names it ("Wheat mark", "Ball") - for a text entity the label \
IS the words drawn - and its detail is one statement: a type, then settings.
Types and geometry:
  rect x= y= width= height= rx=        circle cx= cy= r=        ellipse cx= cy= rx= ry=
  line x1= y1= x2= y2=                 polygon points=x,y;x,y;x,y      path d=M10,10L90,10L50,80Z
  text x= y= size= weight=400|500|600|700|800|900 anchor=start|middle|end font=sans|serif|mono|display spacing=
  particles x= y= rate=(per second) size= speed= angle=(degrees, 0 = right, 90 = down) spread= \
life=(seconds) shape=circle|square|star     - a continuous emitter: snow, sparks, confetti, bubbles
Paint (any type): fill=#hex stroke=#hex stroke-width= opacity=0-1 rotate=(degrees) glow=#hex
Motion and physics (any type): vx= vy= (px/s) spin=(deg/s) pulse=0-1 period=(s) float=(px bob) \
orbit=cx,cy,radius,deg/s  body=1 (falls under the scene's gravity)  bounce=1 (rebounds off the \
scene edges)  wrap=1 (leaves one edge, enters the opposite)  delay=(s before it appears)  solid=1 \
(bodies land on it and bounce off it - floors, counters, platforms, walls)  attach=<entity id> (a \
part of that entity: moves, spins and scales with it, and is dragged with it - the crust marks on a \
loaf, the eyes on a character; insert the parts after the entity they attach to)
Pointer: drag=1 (the visitor can pick it up and throw it)  tap=pulse|spin|burst|jump|hide
Every geometry setting listed for a type is required; sizes are never negative. \
Numbers are plain (no units), colours are #hex or none, no spaces inside a value (path data uses \
commas: "M10,10C20,0,40,0,50,10"). Later entities draw on top.
Make it alive: give things motion that suits them - a logo mark that slowly spins or pulses, a \
banner headline that floats, particles for atmosphere, a ball with body=1 bounce=1 drag=1 the \
visitor can throw. When asked for a game or interaction, build it from these behaviours. Use a \
coherent palette with strong contrast between text and its background. The visitor's own brand \
name goes in text; never invent a slogan they did not give you - use "[Tagline]" instead. To \
change an entity later, set_detail it with its full new statement: the page animates the change \
(position, size and colour glide to the new values), so small precise edits read as live \
motion."""


def _flatten(node, out, parent=None):
    out[node["id"]] = {"kind": node.get("kind"), "label": node.get("label", ""),
                       "detail": node.get("detail", ""), "parent": parent}
    for child in node.get("children") or []:
        _flatten(child, out, node["id"])
    return out


def _txt(s):
    return isinstance(s, str) and len(s) <= TEXT_MAX


def _index(tree):
    index, parents = {}, {}

    def walk(n, p=None):
        index[n["id"]] = n
        parents[n["id"]] = p
        for c in n.get("children") or []:
            walk(c, n)
    walk(tree)
    return index, parents


def _subtree_ids(node):
    out = {node["id"]}
    for c in node.get("children") or []:
        out |= _subtree_ids(c)
    return out


def _scope_ids(state_questions, trigger, resolves):
    """The affected ids this turn may change, or None when the turn answers no
    question (a new utterance may shape the page freely).

    A form answers several questions at once: the scope is the UNION of their
    affected ids. An answered id that is not a known question yields an EMPTY
    scope - nothing may change - never an unrestricted one (Codex re-review of
    PR 200, P2)."""
    by_id = {q["question_id"]: q for q in state_questions}
    kind = trigger.get("kind")
    if kind == "answer":
        qids = [trigger.get("question_id")]
    elif kind == "answer_batch":
        qids = [a.get("question_id") for a in trigger.get("answers") or []]
    elif resolves:
        qids = [resolves["question_id"]]
    else:
        return None
    ids = set()
    for qid in qids:
        q = by_id.get(qid)
        if q is None:
            return set()
        ids.update(q.get("affected_artifact_ids") or [])
    return ids


def validate(draft: dict, artifact_root: dict, state_questions: list,
             trigger: dict | None = None) -> tuple[dict, list]:
    """Everything the schema cannot express. Returns (clean_draft, problems).

    Codex review of PR 200 (CODEX-PR200-REVIEW-20260924T0501Z) shaped this:
    - the ops are ONE TRANSACTION, checked against the tree as it evolves, and
      if any op is invalid the whole patch and its confirmation are dropped -
      never a surviving half-change announced as the whole;
    - when the turn answers a question, the change must stay inside that
      question's affected nodes (their subtrees, and nodes inserted under them);
    - question ids are fenced against every recorded question and each other;
    - every text is capped and a node never exceeds 60 children, as the
      contract says.
    """
    trigger = trigger or {}
    problems = []
    recorded = {q["question_id"] for q in state_questions}
    open_by_id = {q["question_id"]: q for q in state_questions if q.get("status") == "open"}

    # -- resolves first: it decides the scope of the change
    resolves = None
    r = draft.get("resolves") or {}
    rqid = (r.get("question_id") or "").strip()
    # A tap already says which question and option; resolving is for words.
    if trigger.get("kind") in ("answer", "answer_batch"):
        rqid = ""
    if rqid:
        q = open_by_id.get(rqid)
        opt = (r.get("option_id") or "").strip()
        free = (r.get("freeform_answer") or "").strip()
        if q is None:
            problems.append("resolves %r, which is not an open question" % rqid)
        elif opt and opt not in {o["option_id"] for o in q.get("options") or []}:
            problems.append("resolves %r with unknown option %r" % (rqid, opt))
        elif not opt and not free:
            problems.append("resolves %r with no answer" % rqid)
        elif not _txt(free):
            problems.append("resolves %r answer too long" % rqid)
        else:
            resolves = {"question_id": rqid}
            if opt:
                resolves["option_id"] = opt
            else:
                resolves["freeform_answer"] = free

    # A claim that failed validation is not the same as no claim: the model
    # thought this turn answered something, so it may not reshape the page
    # freely on the strength of it. Nothing may change (Codex review of #206).
    claimed_but_invalid = bool(rqid) and resolves is None

    # -- the patch, as one transaction on a candidate tree
    tree = json.loads(json.dumps(artifact_root))
    index, parents = _index(tree)
    scope = set() if claimed_but_invalid else _scope_ids(state_questions, trigger, resolves)
    allowed = None
    if scope is not None:
        allowed = set()
        for a in scope:
            if a in index:
                allowed |= _subtree_ids(index[a])
    ops_out, txn_ok = [], True
    for i, op in enumerate(draft.get("ops") or []):
        kind, nid = op.get("op"), op.get("node_id", "")
        why = None
        if nid not in index:
            why = "targets unknown node %r" % nid
        elif allowed is not None and nid not in allowed:
            why = "changes %r, outside the answered question's scope" % nid
        elif kind in ("set_label", "set_detail"):
            parent = parents[nid]
            if not _txt(op.get("value")):
                why = "value too long"
            elif kind == "set_detail" and visual_problem(
                    index[nid].get("kind"), op["value"], parent.get("kind") if parent else None):
                why = visual_problem(index[nid].get("kind"), op["value"], parent.get("kind") if parent else None)
            else:
                index[nid]["label" if kind == "set_label" else "detail"] = op["value"]
                if kind == "set_detail" and index[nid].get("kind") == "entity" and attach_problem(
                        nid, op["value"], index, parents):
                    why = attach_problem(nid, op["value"], index, parents)
                ops_out.append({"op": kind, "node_id": nid, "value": op["value"]})
        elif kind == "insert_child":
            nn = op.get("new_node") or {}
            if not ID_RE.match(nn.get("id", "")) or nn["id"] in index:
                why = "new id %r missing, malformed or taken" % nn.get("id")
            elif nn.get("kind") not in KINDS or not _txt(nn.get("label")) or not (nn.get("label") or "").strip() \
                    or not _txt(nn.get("detail", "")):
                why = "new node invalid"
            elif visual_problem(nn["kind"], nn.get("detail", ""), index[nid].get("kind")):
                why = visual_problem(nn["kind"], nn.get("detail", ""), index[nid].get("kind"))
            elif index[nid].get("kind") == "scene" and nn["kind"] != "entity":
                why = "a scene holds only entities"
            elif index[nid].get("kind") == "entity":
                why = "an entity holds nothing"
            elif len(index[nid].get("children") or []) >= MAX_CHILDREN:
                why = "%r already has %d children" % (nid, MAX_CHILDREN)
            else:
                new = {"id": nn["id"], "kind": nn["kind"], "label": nn["label"]}
                if nn.get("detail"):
                    new["detail"] = nn["detail"]
                index[nid].setdefault("children", []).append(dict(new))
                index[new["id"]] = index[nid]["children"][-1]
                parents[new["id"]] = index[nid]
                if new["kind"] == "entity" and attach_problem(new["id"], new.get("detail", ""), index, parents):
                    why = attach_problem(new["id"], new.get("detail", ""), index, parents)
                if allowed is not None:
                    allowed.add(new["id"])
                ops_out.append({"op": "insert_child", "node_id": nid, "node": new})
        elif kind == "remove":
            if parents[nid] is None:
                why = "would remove the root"
            else:
                gone = _subtree_ids(index[nid])
                p = parents[nid]
                p["children"] = [c for c in p["children"] if c["id"] != nid]
                for g in gone:
                    index.pop(g, None)
                    parents.pop(g, None)
                ops_out.append({"op": "remove", "node_id": nid})
        else:
            why = "unknown op %r" % kind
        if why:
            problems.append("op %d %s; the whole patch is dropped" % (i, why))
            txn_ok = False
            break

    confirm = draft.get("confirm") or ""
    if not txn_ok:
        ops_out, confirm = [], ""
        tree = json.loads(json.dumps(artifact_root))
        index, parents = _index(tree)
        # The answer and its change are one decision. If the change was
        # refused, the answer is not recorded either: the question stays open
        # and the visitor can say it again (Codex review of #206, P2).
        if resolves:
            problems.append("resolution of %r not recorded: its change was refused"
                            % resolves["question_id"])
            resolves = None
    if not _txt(confirm):
        problems.append("confirm too long")
        confirm = ""

    # -- questions, fenced and capped, against the tree as it will be
    questions, seen = [], set(recorded)
    if resolves:
        seen.add(resolves["question_id"])
    for q in (draft.get("questions") or [])[:4]:
        qid = q.get("question_id", "")
        why = []
        if not ID_RE.match(qid) or qid in seen:
            why.append("id %r malformed, already recorded or repeated" % qid)
        opts = q.get("options") or []
        if not 2 <= len(opts) <= 3:
            why.append("%d options" % len(opts))
        if len({o.get("option_id") for o in opts}) != len(opts) or \
                not all(ID_RE.match(o.get("option_id", "")) for o in opts):
            why.append("option ids duplicate or malformed")
        for o in opts:
            if not (o.get("label") or "").strip() or not _txt(o.get("label")) \
                    or not _txt(o.get("consequence")) or not _txt(o.get("recommended_because") or ""):
                why.append("option %r text empty or too long" % o.get("option_id"))
                break
        rec = [o for o in opts if o.get("recommended")]
        if len(rec) > 1 or any(not (o.get("recommended_because") or "").strip() for o in rec):
            why.append("recommended more than once or without a reason")
        aff = q.get("affected_artifact_ids") or []
        if not aff or any(a not in index for a in aff):
            why.append("affected ids missing or unknown")
        for f in ("scope_path", "reason", "prompt"):
            if not _txt(q.get(f)) or not (q.get(f) or "").strip():
                why.append("%s empty or too long" % f)
        if why:
            problems.append("question %r dropped: %s" % (qid, "; ".join(why)))
            continue
        seen.add(qid)
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

    return {"ops": ops_out, "confirm": confirm, "questions": questions,
            "batch_title": (draft.get("batch_title") or "")[:TEXT_MAX],
            "resolves": resolves}, problems


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


def _option_text(o):
    rec = " (recommended: %s)" % o.get("recommended_because", "") if o.get("recommended") else ""
    return "%s = %s -> %s%s" % (o["option_id"], o.get("label", ""), o.get("consequence", ""), rec)


def _describe(state: dict, trigger: dict) -> str:
    """Everything the model needs to act on a choice: each question's options WITH
    their meaning, and the chosen option spelled out - an opaque id like "a" is
    not an answer the model can enact (Codex review of PR 200, P1)."""
    nodes = _flatten(state["artifact"], {})
    lines = ["CURRENT PROTOTYPE (id | kind | label | detail | parent):"]
    for nid, n in nodes.items():
        lines.append("%s | %s | %s | %s | %s" % (nid, n["kind"], n["label"], n["detail"], n["parent"] or "-"))
    by_id = {}
    lines.append("\nQUESTIONS SO FAR:")
    for q in state.get("questions") or []:
        by_id[q["question_id"]] = q
        lines.append("%s [%s] %s | where: %s | affects: %s" % (
            q["question_id"], q.get("status"), q.get("prompt"), q.get("scope_path", ""),
            ", ".join(q.get("affected_artifact_ids") or [])))
        for o in q.get("options") or []:
            lines.append("    option " + _option_text(o))
        if q.get("selected_option") or q.get("freeform_answer"):
            chosen = next((o for o in q.get("options") or [] if o["option_id"] == q.get("selected_option")), None)
            lines.append("    ANSWERED: " + (_option_text(chosen) if chosen else q.get("freeform_answer", "")))
    lines.append("\nRECENT CONVERSATION:")
    for t in (state.get("transcript") or [])[-12:]:
        lines.append("%s: %s" % (t.get("role", "visitor"), t.get("text", "")))

    def spelled(qid, option_id, free):
        q = by_id.get(qid) or {}
        o = next((x for x in q.get("options") or [] if x["option_id"] == option_id), None)
        what = _option_text(o) if o else "their own words: %s" % (free or "")
        return "answered %s (%s) with %s; change only: %s" % (
            qid, q.get("prompt", "?"), what, ", ".join(q.get("affected_artifact_ids") or []))

    kind = trigger.get("kind")
    if kind == "answer":
        latest = spelled(trigger.get("question_id"), trigger.get("option_id"), trigger.get("freeform_answer"))
    elif kind == "answer_batch":
        latest = "; ".join(spelled(a.get("question_id"), a.get("option_id"), a.get("freeform_answer"))
                           for a in trigger.get("answers") or [])
    else:
        latest = "the visitor said: %s" % (trigger.get("text") or "")
    lines.append("\nLATEST INPUT: " + latest)
    return "\n".join(lines)


class ClaudeWorker:
    """on_turn(state, trigger) -> {"events": [drafts], "problems": [...]}.

    state:   {"artifact": root node, "questions": [question records],
              "transcript": [{"role", "text"}], "session_id": str, "turn_seq": int}
    trigger: {"kind": "utterance", "text"} | {"kind": "answer", "question_id",
              "option_id" | "freeform_answer"} | {"kind": "answer_batch", "answers": [...]}
    returns: {"events", "problems", "resolves": None | {"question_id",
              "option_id" | "freeform_answer"}} - resolves is set when an utterance
              answered an open question; the controller then emits question.answered.
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
        clean, problems = validate(draft, state["artifact"], state.get("questions") or [], trigger)
        if state.get("analyst"):
            # The analyst lane asks the questions in this session (it maps the
            # data model in parallel); the builder builds and confirms. Two
            # agents asking at once would talk over each other.
            clean["questions"] = []
        # Fresh per turn, never derived from a counter that can repeat.
        batch_id = "b-%s-%s" % (state.get("session_id", "s"), uuid.uuid4().hex[:10])
        # `resolves` is for the controller: it owns the question record, so it
        # composes question.answered (answer_source "voice" for an utterance).
        return {"events": to_drafts(clean, batch_id), "problems": problems,
                "resolves": clean["resolves"]}

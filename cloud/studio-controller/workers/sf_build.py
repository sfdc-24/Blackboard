"""The Salesforce build lane: discuss, options, prototype, build, display, test, promote.

Mr. Salam (2026-10-09): "if I say, I'd like to see how I can model API usage and
agent spend in salesforce, I want to see propositions, architectural proposals
and options discussed and then implemented as I watch in realtime"; "scratch orgs
can be used to build prototypes during conferences and can be put into developer
org upon payment/acceptance"; "if approved and finalized after testing; they can
be pushed into the dev org".

THE PHASES, IN ORDER (state["sf_build"]["phase"], published to the sink):

  1. DISCUSS    the architect draws the solution architecture as a titled flow
                (section > heading, process-step, edge) and says what it shows.
                The owner pokes it; it is redrawn. Never touches an org.
  2. OPTIONS    2-3 design options as a decision.batch with trade-offs (limits,
                storage, licences, complexity, time); one recommended, and why.
  3. PROTOTYPE  on the pick: the data model (model.updated, marked PROPOSED) and
                a mock record page and list view with plausible values. The owner
                iterates; every change re-plans and redraws both. Never touches an org.
  4. BUILD      only after "build it", into the session's SCRATCH org (never the
                developer org): validate (checkOnly) -> "Build it in Salesforce
                now?" -> deploy on the owner's in-session yes.
  5. DISPLAY    read back from the scratch org (describe), the model redrawn as
                BUILT from the org's answer; sample records on request, queried
                back into the list view, with Object Manager and record links.
  6. TEST       acceptance checks in the scratch org: records exist, names set,
                every master-detail linked, lookups used, roll-ups compute. Pass or
                fail on the canvas and to the sink.
  7. PROMOTE    only after a passing TEST of the latest scratch build, the owner's
                explicit "approved" / "finalized" / "promote to the developer org"
                and an in-session yes: the same plan's package is validated
                (checkOnly) and then deployed to the pinned developer org, with its
                own destructiveChanges.xml for undo.

Iterating after a build loops back to PROTOTYPE, then BUILD with adds only.
"Undo the last build" (scratch) and "undo the promote" (developer org) deploy the
stored destructiveChanges.xml after their own confirm. Order is enforced here
(LaneRefused), never trusted to the page or the model. Every validate, deploy,
undo, test and promote writes one audit line (session id, plan hash, components,
result; no PII) and is recorded in session state.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import sys
import threading
import time

try:  # the app and the image import these as part of the workers package
    from workers import sf_plan, sf_views, sf_sink, sf_view
    from workers import bounded
    from workers.policy import USE_POLICY
except ImportError:  # loaded from their files (tests)
    import importlib.util as _util

    def _load(name):
        spec = _util.spec_from_file_location("studio_" + name, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), name + ".py"))
        mod = _util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    sf_plan, sf_views, sf_sink, sf_view = _load("sf_plan"), _load("sf_views"), _load("sf_sink"), _load("sf_view")
    bounded = _load("bounded")
    USE_POLICY = _load("policy").USE_POLICY

TOPIC = "salesforce_build"
PHASES = ("idle", "discuss", "options", "prototype", "build", "display", "test", "promote")
QUESTION_PREFIX = "sfb-"
MAX_AUDIT = 50
DEFAULT_DEPLOY_WAIT = 30.0
SCRATCH, DEVORG = "scratch", "devorg"

UNDO_PROMOTE_RE = re.compile(r"\b(undo|roll ?back|revert)\b.{0,24}\bpromot", re.I)
UNDO_RE = re.compile(r"\b(undo|roll ?back|revert)\b.{0,20}\b(build|deploy|that|it)\b", re.I)
SAMPLE_RE = re.compile(r"\b(add|insert|create|load|put in)\b.{0,24}\bsample (data|records?)\b", re.I)
TEST_RE = re.compile(r"\b(test it|test this|run (the )?(acceptance )?(tests?|checks)|acceptance (tests?|checks))\b",
                     re.I)
PROMOTE_RE = re.compile(r"\b(approved|finali[sz]ed|accepted|promote( it| this)?( to| into)? (the )?(dev|developer) "
                        r"org|promote it|push (it )?(to|into) the (dev|developer) org)\b", re.I)
BUILD_RE = re.compile(r"\b(build it|build this|build that|deploy it|deploy this|make it real|"
                      r"build (it |this |that )?(in|into) (salesforce|the org))\b", re.I)
STATUS_RE = re.compile(r"\b(is it (done|finished|ready)|check the (build|deploy|promote)|build status|"
                       r"deploy status|how is the (build|deploy))\b", re.I)
OPTIONS_RE = re.compile(r"\b(show|give|what are|let'?s see|walk me through)\b.{0,24}\boptions\b|"
                        r"\bdesign options\b", re.I)
YES_RE = re.compile(r"^\W*(yes|yeah|yep|yup|go ahead|do it|confirm(ed)?|build it|promote it|undo it|go)\b", re.I)
NO_RE = re.compile(r"^\W*(no|nope|not yet|cancel|stop|wait|hold on|keep it)\b", re.I)
PICK_RE = re.compile(r"\b(?:option|go with|pick|choose|take)\s+(?:option\s+)?([abc123]|one|two|three|first|"
                     r"second|third)\b", re.I)
PICK_WORDS = {"a": "a", "1": "a", "one": "a", "first": "a", "b": "b", "2": "b", "two": "b", "second": "b",
              "c": "c", "3": "c", "three": "c", "third": "c"}
CONFIRM_STEPS = ("confirm_asked", "promote_confirm_asked", "undo_asked")


class LaneRefused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def new_state() -> dict:
    return {"phase": "idle", "step": "", "seq": 0, "round": 0, "ask": "", "arch": None, "options": [],
            "plan": None, "plan_hash": "", "scope": "model", "pending": None, "scratch": None,
            "built": sf_plan.empty_built(), "built_plan": None, "dev_built": sf_plan.empty_built(),
            "builds": [], "test": None, "audit": []}


def classify(text: str, sf: dict) -> dict:
    """An utterance in a Salesforce build session, as a lane action. Deterministic: no
    model decides what is built, tested, promoted or when."""
    phase, step = sf.get("phase", "idle"), sf.get("step", "")
    pending = sf.get("pending") or {}
    if pending.get("deploy_id") or STATUS_RE.search(text):
        return {"action": "status"}
    if step in CONFIRM_STEPS:
        if YES_RE.search(text):
            return {"action": "confirm", "answer": "yes"}
        if NO_RE.search(text):
            return {"action": "confirm", "answer": "no"}
    if UNDO_PROMOTE_RE.search(text):
        return {"action": "undo", "target": DEVORG}
    if UNDO_RE.search(text):
        return {"action": "undo", "target": SCRATCH}
    if PROMOTE_RE.search(text):
        return {"action": "promote"}
    if TEST_RE.search(text):
        return {"action": "test"}
    if SAMPLE_RE.search(text):
        return {"action": "sample"}
    if BUILD_RE.search(text):
        return {"action": "build"}
    if OPTIONS_RE.search(text):
        return {"action": "options"}
    if phase == "options":
        m = PICK_RE.search(text)
        if m:
            return {"action": "pick", "option_id": PICK_WORDS[m.group(1).lower()]}
        for o in sf.get("options") or []:
            if o["label"].lower() in text.lower():
                return {"action": "pick", "option_id": o["option_id"]}
    if phase in ("prototype", "display", "test", "promote"):
        return {"action": "revise", "text": text}
    if phase == "build":
        return {"action": "hint"}
    return {"action": "ask", "text": text}


def merge_plans(base: dict | None, extra: dict) -> dict:
    """The built plan with an option's plan laid over it (adds only), before validation."""
    if not base:
        return copy.deepcopy(extra)
    merged = copy.deepcopy(base)
    by_name = {o["api_name"]: o for o in merged["objects"]}
    for obj in extra["objects"]:
        mine = by_name.get(obj["api_name"])
        if mine is None:
            merged["objects"].append(copy.deepcopy(obj))
            continue
        have = {f["api_name"] for f in mine["fields"]}
        mine["fields"] += [copy.deepcopy(f) for f in obj["fields"] if f["api_name"] not in have]
    merged["title"] = extra["title"]
    return merged


def _question(qid: str, root: str, scope: str, reason: str, prompt: str, options: list) -> dict:
    return {"question_id": qid, "group": "Data", "scope_path": scope, "reason": reason, "prompt": prompt,
            "options": options, "status": "open", "affected_artifact_ids": [root]}


def _answer_question(state: dict, qid: str, option_id: str, source: str) -> dict:
    for q in state.get("questions") or []:
        if q.get("question_id") == qid:
            if q.get("status") != "open":
                raise LaneRefused("that question is already answered")
            if option_id not in {o.get("option_id") for o in q.get("options") or []}:
                raise LaneRefused("that is not one of the question's options", 400)
            q.update({"status": "answered", "selected_option": option_id, "answer_source": source,
                      "artifact_version_before": state["artifact_version"],
                      "artifact_version_after": state["artifact_version"]})
            return copy.deepcopy(q)
    raise LaneRefused("unknown question", 404)


def _progress(state: dict, text: str) -> dict:
    return {"type": "progress", "payload": {"artifact_ids": [state["artifact"]["id"]], "text": text[:600]}}


def _confirm(state: dict, text: str) -> list:
    """One or more confirm events (600 characters each)."""
    out, rest = [], text
    while rest:
        cut = rest[:600]
        if len(rest) > 600 and " " in cut:
            cut = cut[:cut.rfind(" ")]
        out.append({"type": "confirm", "payload": {"artifact_ids": [state["artifact"]["id"]], "text": cut}})
        rest = rest[len(cut):].strip()
    return out


def _built_key(target: str) -> str:
    return "built" if target == SCRATCH else "dev_built"


def latest_build(sf: dict, target: str) -> dict | None:
    live = [b for b in sf.get("builds") or [] if b.get("target") == target and not b.get("undone_at")]
    return live[-1] if live else None


class SfBuildLane:
    def __init__(self, *, load, commit, targets, architect, sink=None, viewer=None, clock=time.time,
                 deploy_wait: float = DEFAULT_DEPLOY_WAIT, log=None):
        self.load, self.commit_fn = load, commit
        self.targets, self.architect = targets, architect
        self.sink = sink or sf_sink.LogSink()
        self.viewer = viewer or sf_view.NullViewer()
        self.clock, self.deploy_wait = clock, deploy_wait
        self.log = log or _log
        self._lock = threading.Lock()
        self._busy: set = set()

    # -- plumbing ----------------------------------------------------------
    def _commit(self, sid: str, fn) -> tuple:
        """Run fn(state, sf, out) inside one compare-and-set commit. fn returns event drafts.
        After the save: sink records published, audit lines logged."""
        out: dict = {}

        def change(state):
            out.clear()
            out.update(sink=[], audit=[], result={})
            sf = state.setdefault("sf_build", new_state())
            return fn(state, sf, out)

        events = self.commit_fn(sid, change)
        for rec in out.get("sink") or []:
            try:
                self.sink.publish(rec)
            except Exception:
                pass
        for line in out.get("audit") or []:
            self.log("studio.sf_build_audit", **line)
        return events, out.get("result") or {}

    def _rec(self, sid: str, sf: dict, out: dict, result: str, *, components: int = 0, objects=()) -> None:
        sf["seq"] = int(sf.get("seq") or 0) + 1
        out["sink"].append(sf_sink.record(sid, sf["seq"], sf["phase"], result, plan_hash=sf.get("plan_hash", ""),
                                          objects=objects, components=components, now=self.clock()))

    def _audit(self, sid: str, sf: dict, out: dict, action: str, components: int, result: str,
               plan_hash: str = "", target: str = SCRATCH) -> None:
        line = {"session_id": sid, "action": action, "target": target,
                "plan_hash": plan_hash or sf.get("plan_hash", ""), "components": int(components), "result": result}
        sf.setdefault("audit", []).append(dict(line, at=int(self.clock())))
        sf["audit"] = sf["audit"][-MAX_AUDIT:]
        out["audit"].append(line)

    def _today(self) -> _dt.date:
        return _dt.datetime.fromtimestamp(self.clock(), _dt.timezone.utc).date()

    def _observed(self) -> str:
        return time.strftime("%H:%MZ", time.gmtime(self.clock()))

    def _open(self, target: str, sf: dict):
        if target == SCRATCH and not sf.get("scratch"):
            raise LaneRefused("no scratch org is attached to this session yet; provision one with "
                              "scripts/sf_scratch.py and attach it")
        return self.targets.open(target, sf.get("scratch") if target == SCRATCH else None)

    def _view(self, sid: str, org, target: str, kind: str, label: str, api: str, record_id: str = "") -> None:
        """Ask the render worker (if any) for a picture of what was just built. Never a session in it."""
        try:
            url = sf_view.view_url(org.host, kind, api, record_id)
            self.viewer.request(sid, sf_view.view_request(target, kind, label, url))
        except Exception:
            pass

    # -- entry -------------------------------------------------------------
    def handle(self, sid: str, intent: dict) -> dict:
        with self._lock:
            if sid in self._busy:
                raise LaneRefused("the build lane is already working on this session; one step at a time")
            self._busy.add(sid)
        try:
            return self._handle(sid, intent)
        finally:
            with self._lock:
                self._busy.discard(sid)

    def _handle(self, sid: str, intent: dict) -> dict:
        state = self.load(sid)
        if state.get("topic") != TOPIC:
            raise LaneRefused("the build lane runs only in a Salesforce build session", 403)
        if not state.get("operator_subject") or state.get("visitor_subject"):
            raise LaneRefused("the build lane is for the operator only", 403)
        sf = state.get("sf_build") or new_state()
        action = intent.get("action")
        spoken = action == "say"
        if spoken:
            item = intent.get("item_id") or ""
            text = intent.get("text", "")
            try:
                self._commit(sid, lambda st, s, o: self._note_item(st, item, text))
            except _Dedup:
                return {"deduplicated": True, "events": [], "phase": sf.get("phase"), "step": sf.get("step")}
            intent = dict(classify(text, sf), source="voice")
            intent.setdefault("text", text)
            action = intent["action"]
        if action == "answer":
            intent = self._from_answer(sf, intent)
            action = intent["action"]
        handler = {"ask": self._ask, "options": self._options, "pick": self._pick, "revise": self._revise,
                   "build": self._build, "confirm": self._confirm_answer, "sample": self._sample,
                   "test": self._test, "promote": self._promote, "undo": self._undo, "status": self._status,
                   "hint": self._hint, "attach_scratch": self._attach}.get(action)
        if handler is None:
            raise LaneRefused("unknown build lane action", 400)
        try:
            events = handler(sid, intent) or []
        except LaneRefused as exc:
            if not spoken:
                raise
            # Said, not tapped: the refusal is spoken back, so the order is explained, not silent.
            events = self._note(sid, str(exc))
        after = (self.load(sid).get("sf_build") or {})
        return {"events": events, "phase": after.get("phase"), "step": after.get("step")}

    def _note_item(self, state: dict, item: str, text: str):
        seen = state.setdefault("voice_item_ids", [])
        if item and item in seen:
            raise _Dedup()
        if item:
            seen.append(item)
            state["voice_item_ids"] = seen[-100:]
        state.setdefault("transcript", []).append({"role": "visitor", "text": text[:600]})
        return []

    def _from_answer(self, sf: dict, intent: dict) -> dict:
        """A tapped chip: the decision batch (design + scope) or a confirm question."""
        if intent.get("batch_id"):
            answers = {a.get("question_id"): a.get("option_id") for a in intent.get("answers") or []}
            design = next((v for k, v in answers.items() if str(k).endswith("-design")), None)
            scope = next((v for k, v in answers.items() if str(k).endswith("-scope")), None)
            return {"action": "pick", "option_id": design, "scope": scope, "batch_id": intent["batch_id"],
                    "source": "tap"}
        qid = intent.get("question_id") or ""
        pending = sf.get("pending") or {}
        if qid and qid == pending.get("question_id"):
            return {"action": "confirm", "answer": "yes" if intent.get("option_id") == "yes" else "no",
                    "question_id": qid, "source": "tap"}
        if qid.endswith("-design"):
            return {"action": "pick", "option_id": intent.get("option_id"), "source": "tap"}
        raise LaneRefused("that question is not open in the build lane")

    # -- the scratch org for this session ------------------------------------
    def _attach(self, sid: str, intent: dict) -> list:
        record = self.targets.attach({"org_id": intent.get("org_id"), "alias": intent.get("alias")})

        def attach(state, sf, out):
            if (sf.get("pending") or {}).get("deploy_id"):
                raise LaneRefused("a deploy is running; attach after it finishes")
            old = sf.get("scratch") or {}
            if old.get("org_id") and old["org_id"][:15] != record["org_id"][:15]:
                # A new scratch org starts empty: nothing is built there yet.
                sf["built"], sf["built_plan"], sf["test"] = sf_plan.empty_built(), None, None
                for b in sf.get("builds") or []:
                    if b.get("target") == SCRATCH and not b.get("undone_at"):
                        b["undone_at"] = int(self.clock())
                        b["detached"] = True
            sf["scratch"] = record
            self._rec(sid, sf, out, "scratch_attached")
            return _confirm(state, "Scratch org attached for this session (expires %s). Builds go there; the "
                                   "developer org changes only on promote." % record["expires"])

        events, _ = self._commit(sid, attach)
        return events

    # -- 1. DISCUSS --------------------------------------------------------
    def _ask(self, sid: str, intent: dict) -> list:
        text = (intent.get("text") or "").strip()
        if not text:
            raise LaneRefused("say what you want to model in Salesforce", 400)

        def start(state, sf, out):
            if sf["phase"] not in ("idle", "discuss", "options", "display", "test", "promote"):
                raise LaneRefused("in %s, describe a change to the prototype instead" % sf["phase"])
            if sf.get("pending"):
                raise LaneRefused("finish the question on screen first")
            sf["phase"], sf["step"] = "discuss", "drawing"
            self._rec(sid, sf, out, "discussing")
            return [_progress(state, "The architect is sketching the architecture for: " + text[:300])]

        events, _ = self._commit(sid, start)
        sf = self.load(sid)["sf_build"]
        try:
            proposal = self.architect.discuss(text, sf.get("arch"), sf.get("built_plan"))
        except Exception as exc:
            proposal = {"problem": "the architect could not answer (%s)" % type(exc).__name__}
        if proposal.get("problem") or not proposal.get("architecture"):
            def failed(state, sf, out):
                sf["step"] = "drawn" if sf.get("arch") else ""
                return _confirm(state, "I could not sketch that yet: %s. Say it once more, a little differently."
                                % (proposal.get("problem") or "no architecture came back"))
            more, _ = self._commit(sid, failed)
            return events + more

        def drawn(state, sf, out):
            if sf["phase"] != "discuss":
                raise LaneRefused("the session moved on while the architect was drawing")
            sf["ask"] = text[:600]
            sf["arch"] = proposal["architecture"]
            sf["options"] = proposal.get("options") or []
            sf["step"] = "drawn"
            node = sf_views.architecture_node(proposal["architecture"])
            ops = sf_views.replace_ops(state["artifact"], [node], drop_prefixes=(sf_views.ARCH_ID,))
            self._rec(sid, sf, out, "architecture_drawn")
            said = proposal.get("explanation") or "Here is the architecture."
            tail = (" Say \"show me the options\" when you are ready, or tell me what to change."
                    if len(sf["options"]) >= 2 else " Tell me more and I will work out the design options.")
            return [{"type": "artifact.patch", "payload": {"ops": ops}}] + _confirm(state, said + tail)

        more, _ = self._commit(sid, drawn)
        return events + more

    # -- 2. OPTIONS --------------------------------------------------------
    def _options(self, sid: str, intent: dict) -> list:
        def offer(state, sf, out):
            if sf["phase"] != "discuss" or sf.get("step") != "drawn":
                raise LaneRefused("options come after the architecture: describe what you want first")
            options = sf.get("options") or []
            if len(options) < 2:
                raise LaneRefused("the architect has no checked options yet; tell me more about the ask")
            sf["round"] = int(sf.get("round") or 0) + 1
            root = state["artifact"]["id"]
            base = "%sr%d" % (QUESTION_PREFIX, sf["round"])
            chips = []
            for o in options:
                chip = {"option_id": o["option_id"], "label": o["label"][:120], "consequence": o["consequence"][:600]}
                if o.get("recommended"):
                    chip["recommended"] = True
                    chip["recommended_because"] = o.get("because", "")[:600] or "The best fit for the ask."
                chips.append(chip)
            design = _question(base + "-design", root, "Salesforce build > Design",
                               "This decides what gets prototyped and then built in a scratch org.",
                               "Which design should we prototype?", chips)
            scope = _question(base + "-scope", root, "Salesforce build > First build",
                              "This decides whether the first build also loads sample records.",
                              "What should the first build include?", [
                                  {"option_id": "model", "label": "The data model",
                                   "consequence": "Objects, fields, a tab and an All list view; no records."},
                                  {"option_id": "sample", "label": "The data model and sample records",
                                   "consequence": "The same, plus up to 10 sample records per new object."}])
            sf["phase"], sf["step"] = "options", "offered"
            sf["batch_id"] = base + "-batch"
            self._rec(sid, sf, out, "options_offered")
            rec = next((o for o in options if o.get("recommended")), None)
            said = "Here are %d design options." % len(options)
            if rec:
                said += " I recommend %s: %s" % (rec["label"], rec.get("because", ""))
            return [{"type": "decision.batch", "payload": {"batch_id": sf["batch_id"],
                                                           "title": "Design options - pick one",
                                                           "questions": [design, scope]}}] + _confirm(state, said)

        events, _ = self._commit(sid, offer)
        return events

    # -- 3. PROTOTYPE ------------------------------------------------------
    def _pick(self, sid: str, intent: dict) -> list:
        option_id = intent.get("option_id")
        scope = intent.get("scope") or "model"
        if scope not in ("model", "sample"):
            raise LaneRefused("unknown first-build scope", 400)

        def pick(state, sf, out):
            if sf["phase"] != "options":
                raise LaneRefused("pick a design after the options are on screen")
            option = next((o for o in sf.get("options") or [] if o["option_id"] == option_id), None)
            if option is None:
                raise LaneRefused("that is not one of the options", 400)
            try:
                plan = sf_plan.validate_plan(merge_plans(sf.get("built_plan"), option["plan"]))
                sf_plan.delta(plan, sf.get("built"))
            except sf_plan.PlanError as exc:
                raise LaneRefused("that option's plan was refused: %s" % exc, 422) from None
            base = "%sr%d" % (QUESTION_PREFIX, sf.get("round") or 0)
            source = intent.get("source") or "tap"
            drafts = []
            for qid, chosen in ((base + "-design", option_id), (base + "-scope", scope)):
                q = next((q for q in state.get("questions") or [] if q.get("question_id") == qid), None)
                if q is not None and q.get("status") == "open":
                    drafts.append({"type": "question.answered",
                                   "payload": {"question": _answer_question(state, qid, chosen, source)}})
            batch = (state.get("batches") or {}).get(sf.get("batch_id") or "")
            if batch:
                batch["status"] = "answered"
            sf.update(phase="prototype", step="proposed", plan=plan, plan_hash=sf_plan.plan_hash(plan),
                      scope=scope, picked=option_id, pending=None)
            self._rec(sid, sf, out, "picked")
            self._rec(sid, sf, out, "plan_proposed", objects=[o["api_name"] for o in plan["objects"]])
            return drafts + self._prototype_drafts(state, sf, "Prototype of %s. Nothing is built yet: this is the "
                                                   "data model and how it will look in Salesforce. Change anything "
                                                   "- rename a field, add a roll-up, change a relationship - or "
                                                   "say \"build it\"." % option["label"])

        events, _ = self._commit(sid, pick)
        return events

    def _prototype_drafts(self, state: dict, sf: dict, said: str) -> list:
        plan = sf["plan"]
        nodes = sf_views.prototype_nodes(plan, self._today())
        drafts = [{"type": "model.updated", "payload": {"model": sf_views.proposed_model(plan, sf.get("built"))}}]
        if nodes:
            drafts.append({"type": "artifact.patch",
                           "payload": {"ops": sf_views.replace_ops(state["artifact"], nodes)}})
        return drafts + _confirm(state, said)

    def _revise(self, sid: str, intent: dict) -> list:
        text = (intent.get("text") or "").strip()
        sf = self.load(sid).get("sf_build") or new_state()
        if sf.get("phase") not in ("prototype", "display", "test", "promote") or not sf.get("plan"):
            raise LaneRefused("changes come after a design is picked")
        if sf.get("pending"):
            raise LaneRefused("finish the question on screen first")
        try:
            revised = self.architect.revise(sf["plan"], text, sf.get("built"))
        except Exception as exc:
            revised = {"problem": "the architect could not answer (%s)" % type(exc).__name__}
        problem = revised.get("problem")
        plan = None
        if not problem:
            try:
                plan = sf_plan.validate_plan(revised.get("plan"))
                sf_plan.delta(plan, sf.get("built"))
            except sf_plan.PlanError as exc:
                problem = str(exc)

        def apply(state, sf, out):
            if sf["phase"] not in ("prototype", "display", "test", "promote") or sf.get("pending"):
                raise LaneRefused("the session moved on")
            if problem:
                return _confirm(state, "That change was not made: %s. Nothing changed." % problem)
            sf.update(phase="prototype", step="revised", plan=plan, plan_hash=sf_plan.plan_hash(plan), pending=None)
            self._rec(sid, sf, out, "plan_revised", objects=[o["api_name"] for o in plan["objects"]])
            return self._prototype_drafts(state, sf, (revised.get("summary") or "Updated.")
                                          + " Still not built - say \"build it\" when it is right.")

        events, _ = self._commit(sid, apply)
        return events

    # -- 4. BUILD (scratch) and 7. PROMOTE (developer org) -------------------
    def _build(self, sid: str, intent: dict) -> list:
        def start(state, sf, out):
            if sf["phase"] != "prototype" or not sf.get("plan"):
                raise LaneRefused("nothing to build yet: the prototype comes first")
            if not sf.get("scratch"):
                raise LaneRefused("no scratch org is attached to this session yet; provision one with "
                                  "scripts/sf_scratch.py and attach it")
            try:
                pkg = sf_plan.build_package(sf["plan"], sf.get("built"))
            except sf_plan.PlanError as exc:
                raise LaneRefused(str(exc), 422) from None
            out["result"]["pkg"] = pkg
            sf.update(phase="build", step="validating",
                      pending={"kind": "validate", "target": SCRATCH, "plan_hash": pkg["plan_hash"],
                               "package_hash": pkg["package_hash"], "components": pkg["components"]})
            self._rec(sid, sf, out, "validating", components=pkg["components"], objects=pkg["new_objects"])
            return [_progress(state, "Validating %d components in the session's scratch org (check only - "
                                     "nothing is deployed)..." % pkg["components"])]

        events, result = self._commit(sid, start)
        return events + self._validate(sid, SCRATCH, result["pkg"])

    def _promote(self, sid: str, intent: dict) -> list:
        def start(state, sf, out):
            test = sf.get("test") or {}
            latest = latest_build(sf, SCRATCH)
            if sf["phase"] not in ("test", "promote") or sf.get("pending"):
                raise LaneRefused("promote comes after the prototype is built in the scratch org and tested")
            if not test.get("passed") or latest is None or test.get("build_n") != latest["n"] \
                    or test.get("plan_hash") != sf_plan.plan_hash(sf.get("built_plan") or {}):
                raise LaneRefused("promote needs a passing test of the latest scratch build; say \"run the tests\"")
            try:
                pkg = sf_plan.build_package(sf["built_plan"], sf.get("dev_built"))
            except sf_plan.PlanError as exc:
                raise LaneRefused(str(exc), 422) from None
            out["result"]["pkg"] = pkg
            sf.update(phase="promote", step="validating",
                      pending={"kind": "validate", "target": DEVORG, "plan_hash": pkg["plan_hash"],
                               "package_hash": pkg["package_hash"], "components": pkg["components"]})
            self._rec(sid, sf, out, "promote_validating", components=pkg["components"], objects=pkg["new_objects"])
            return [_progress(state, "Validating the tested package in your developer org (check only - "
                                     "nothing is deployed)...")]

        events, result = self._commit(sid, start)
        return events + self._validate(sid, DEVORG, result["pkg"])

    def _validate(self, sid: str, target: str, pkg: dict) -> list:
        sf = self.load(sid)["sf_build"]
        action = "validate" if target == SCRATCH else "promote_validate"
        try:
            org = self._open(target, sf)
            self._collisions(org, pkg, target)
            job = org.deploy(sf_plan.package_zip(pkg), check_only=True)
        except Exception as exc:
            return self._fail(sid, action, target, "Validation could not run: %s" % _why(exc), pkg["components"])

        def started(state, sf, out):
            sf["pending"] = dict(sf.get("pending") or {}, deploy_id=job)
            return []

        self._commit(sid, started)
        return self._follow(sid, org, job)

    def _collisions(self, org, pkg: dict, target: str) -> None:
        """Never deploy over something that already exists: undo would then delete it."""
        existing = set(org.custom_objects())
        clash = [o for o in pkg["new_objects"] if o in existing]
        if clash:
            raise LaneRefused("%s already exist%s in the %s; rename in the prototype"
                              % (", ".join(clash), "s" if len(clash) == 1 else "",
                                 "scratch org" if target == SCRATCH else "developer org"))
        by_obj: dict = {}
        for full in pkg["new_fields"]:
            obj, field = full.split(".", 1)
            if obj not in pkg["new_objects"]:
                by_obj.setdefault(obj, []).append(field)
        for obj, fields in by_obj.items():
            have = {f.get("name") for f in org.describe(obj).get("fields") or []}
            taken = [f for f in fields if f in have]
            if taken:
                raise LaneRefused("%s.%s already exists in the org; rename it in the prototype" % (obj, taken[0]))

    def _follow(self, sid: str, org, job: str) -> list:
        result = org.wait(job, self.deploy_wait)
        if not result["done"]:
            def still(state, sf, out):
                kind = (sf.get("pending") or {}).get("kind")
                return [_progress(state, "Salesforce is still %s (%d of %d components) - say \"check the build\"."
                                  % ({"validate": "validating", "deploy": "deploying", "undo": "undoing"}.get(kind, "working"),
                                     result["deployed"], result["total"]))]
            events, _ = self._commit(sid, still)
            return events
        pending = (self.load(sid).get("sf_build") or {}).get("pending") or {}
        kind, target = pending.get("kind"), pending.get("target", SCRATCH)
        if kind == "validate":
            return self._validated(sid, target, result)
        if kind == "deploy":
            return self._deployed(sid, target, org, result)
        if kind == "undo":
            return self._undone(sid, target, org, result)
        return []

    def _fail(self, sid: str, action: str, target: str, text: str, components: int) -> list:
        def failed(state, sf, out):
            back = {"undo": "display", "promote_undo": "promote", "promote_validate": "test",
                    "promote_deploy": "test"}.get(action, "prototype")
            self._audit(sid, sf, out, action, components, "error", target=target)
            sf.update(phase=back, step=action + "_failed", pending=None)
            self._rec(sid, sf, out, action + "_failed", components=components)
            return [_progress(state, text)]
        events, _ = self._commit(sid, failed)
        return events

    def _validated(self, sid: str, target: str, result: dict) -> list:
        promote = target == DEVORG
        action = "promote_validate" if promote else "validate"

        def settle(state, sf, out):
            pending = sf.get("pending") or {}
            if not result["success"]:
                self._audit(sid, sf, out, action, pending.get("components", 0), "failed", target=target)
                sf.update(phase="test" if promote else "prototype", step=action + "_failed", pending=None)
                self._rec(sid, sf, out, action + "_failed", components=pending.get("components", 0))
                return [_progress(state, "Validation failed in the %s: %s" % (
                    "developer org" if promote else "scratch org", "; ".join(result["errors"]) or result["status"]))]
            self._audit(sid, sf, out, action, result["total"], "succeeded", pending.get("plan_hash", ""), target)
            sf["round"] = int(sf.get("round") or 0) + 1
            qid = "%s%s-%d" % (QUESTION_PREFIX, "promote" if promote else "confirm", sf["round"])
            sf.update(step="promote_confirm_asked" if promote else "confirm_asked",
                      pending={"kind": "confirm", "target": target, "question_id": qid,
                               "plan_hash": pending.get("plan_hash"), "package_hash": pending.get("package_hash"),
                               "components": result["total"]})
            self._rec(sid, sf, out, ("promote_" if promote else "") + "validated", components=result["total"])
            self._rec(sid, sf, out, ("promote_" if promote else "") + "confirm_asked", components=result["total"])
            if promote:
                q = _question(qid, state["artifact"]["id"], "Salesforce build > Promote",
                              "The developer org changes only with your yes in this session.",
                              "Promote it to your developer org now?", [
                                  {"option_id": "yes", "label": "Yes, promote it",
                                   "consequence": "Deploys the tested package (%d components) to your developer "
                                                  "org. \"Undo the promote\" removes it." % result["total"]},
                                  {"option_id": "no", "label": "Not yet",
                                   "consequence": "The developer org is unchanged."}])
            else:
                q = _question(qid, state["artifact"]["id"], "Salesforce build > Deploy",
                              "Nothing is deployed without your yes in this session.", "Build it in Salesforce now?", [
                                  {"option_id": "yes", "label": "Yes, build it",
                                   "consequence": "Deploys %d components to this session's scratch org. \"Undo the "
                                                  "last build\" removes them." % result["total"]},
                                  {"option_id": "no", "label": "Not yet",
                                   "consequence": "Nothing is deployed; keep changing the prototype."}])
            return [_progress(state, "Validated in the %s: %d components, no errors. Nothing is deployed yet."
                              % ("developer org" if promote else "scratch org", result["total"])),
                    {"type": "question.asked", "payload": {"question": q}}]
        events, _ = self._commit(sid, settle)
        return events

    def _confirm_answer(self, sid: str, intent: dict) -> list:
        answer = intent.get("answer")

        def take(state, sf, out):
            pending = sf.get("pending") or {}
            if sf.get("step") not in CONFIRM_STEPS or not pending.get("question_id"):
                raise LaneRefused("there is nothing waiting for your yes")
            if intent.get("question_id") and intent["question_id"] != pending["question_id"]:
                raise LaneRefused("that confirm question is no longer current")
            q = _answer_question(state, pending["question_id"], "yes" if answer == "yes" else "no",
                                 intent.get("source") or "tap")
            drafts = [{"type": "question.answered", "payload": {"question": q}}]
            step, target = sf["step"], pending.get("target", SCRATCH)
            out["result"].update(step=step, target=target)
            if answer != "yes":
                back = {"undo_asked": sf.get("undo_from", "display"), "promote_confirm_asked": "test"}.get(step, "prototype")
                sf.update(phase=back, step="declined", pending=None)
                self._rec(sid, sf, out, {"undo_asked": "undo_declined", "promote_confirm_asked": "promote_declined"}
                          .get(step, "build_declined"))
                return drafts + _confirm(state, "Nothing was changed in Salesforce.")
            if step == "undo_asked":
                build = _build_n(sf, pending.get("build"))
                out["result"]["build"] = build
                sf.update(step="undoing", pending={"kind": "undo", "target": build["target"], "build": build["n"],
                                                   "components": build["undo_components"]})
                self._rec(sid, sf, out, "undoing", components=build["undo_components"], objects=build["new_objects"])
                return drafts + [_progress(state, "Undoing %s %d: removing %s..." % (
                    "promote" if build["target"] == DEVORG else "build", build["n"],
                    ", ".join(build["new_objects"] + build["field_adds"])[:400]))]
            plan = sf["plan"] if target == SCRATCH else sf.get("built_plan")
            if target == SCRATCH and pending.get("plan_hash") != sf.get("plan_hash"):
                raise LaneRefused("the plan changed after it was validated; say \"build it\" again")
            pkg = sf_plan.build_package(plan, sf.get(_built_key(target)))
            if pkg["package_hash"] != pending.get("package_hash"):
                raise LaneRefused("the package changed after it was validated; validate it again")
            out["result"]["pkg"] = pkg
            sf.update(step="deploying", pending={"kind": "deploy", "target": target, "plan_hash": pkg["plan_hash"],
                                                 "package_hash": pkg["package_hash"], "components": pkg["components"]})
            self._rec(sid, sf, out, "promoting" if target == DEVORG else "deploying", components=pkg["components"],
                      objects=pkg["new_objects"])
            return drafts + [_progress(state, "Deploying %d components to %s..." % (
                pkg["components"], "your developer org" if target == DEVORG else "the scratch org"))]

        events, held = self._commit(sid, take)
        if answer != "yes":
            return events
        target = held["target"] if held["step"] != "undo_asked" else held["build"]["target"]
        undo = held["step"] == "undo_asked"
        action = ("promote_undo" if target == DEVORG else "undo") if undo else \
            ("promote_deploy" if target == DEVORG else "deploy")
        comps = held["build"]["undo_components"] if undo else held["pkg"]["components"]
        try:
            org = self._open(target, self.load(sid)["sf_build"])
            if undo:
                job = org.deploy(sf_plan.destructive_zip(held["build"]["destructive_xml"]), check_only=False,
                                 purge_on_delete=target == SCRATCH)
            else:
                job = org.deploy(sf_plan.package_zip(held["pkg"]), check_only=False)
        except Exception as exc:
            return events + self._fail(sid, action, target, "Did not start: %s" % _why(exc), comps)

        def started(state, sf, out):
            sf["pending"] = dict(sf.get("pending") or {}, deploy_id=job)
            return []

        self._commit(sid, started)
        return events + self._follow(sid, org, job)

    def _deployed(self, sid: str, target: str, org, result: dict) -> list:
        promote = target == DEVORG
        action = "promote_deploy" if promote else "deploy"

        def settle(state, sf, out):
            pending = sf.get("pending") or {}
            if not result["success"]:
                self._audit(sid, sf, out, action, pending.get("components", 0), "failed", target=target)
                sf.update(phase="test" if promote else "prototype", step=action + "_failed", pending=None)
                self._rec(sid, sf, out, action + "_failed", components=pending.get("components", 0))
                return [_progress(state, "Deploy failed; Salesforce rolled it back: "
                                  + ("; ".join(result["errors"]) or result["status"]))]
            plan = sf["plan"] if not promote else sf["built_plan"]
            pkg = sf_plan.build_package(plan, sf.get(_built_key(target)))
            change = pkg["change"]
            n = len(sf.get("builds") or []) + 1
            sf.setdefault("builds", []).append({
                "n": n, "target": target, "at": int(self.clock()), "plan_hash": pkg["plan_hash"],
                "package_hash": pkg["package_hash"], "components": result["total"],
                "new_objects": change["new_objects"], "new_fields": change["new_fields"],
                "field_adds": pkg["undo_types"].get("CustomField", []),
                "package_xml": pkg["package_xml"], "destructive_xml": pkg["destructive_xml"],
                "plan": copy.deepcopy(plan),
                "undo_components": sum(len(v) for v in pkg["undo_types"].values()),
                "change": {"new_objects": change["new_objects"], "new_fields": change["new_fields"]}})
            sf[_built_key(target)] = sf_plan.record_built(sf.get(_built_key(target)), plan, change)
            if not promote:
                sf["built_plan"] = copy.deepcopy(plan)
                sf["test"] = None              # a new scratch build must be tested again before promote
            self._audit(sid, sf, out, action, result["total"], "succeeded", pkg["plan_hash"], target)
            sf.update(step="promoted" if promote else "deployed", pending=None)
            self._rec(sid, sf, out, "promoted" if promote else "deployed", components=result["total"],
                      objects=change["new_objects"])
            out["result"]["scope"] = sf.get("scope")
            return [_progress(state, "%s: %d components - %d new object%s, %d new field%s." % (
                "Promoted to your developer org" if promote else "Deployed to the scratch org", result["total"],
                len(change["new_objects"]), "" if len(change["new_objects"]) == 1 else "s",
                len(change["new_fields"]), "" if len(change["new_fields"]) == 1 else "s"))]

        events, held = self._commit(sid, settle)
        if not result["success"]:
            return events
        try:
            org.assign_permset(sf_plan.PERMSET)
        except Exception as exc:
            self.log("studio.sf_build_permset", session_id=sid, target=target, reason=type(exc).__name__)
        if promote:
            return events + self._promoted_readback(sid, org)
        events += self._display(sid, org, "Built in the scratch org and read back from it.")
        if held.get("scope") == "sample":
            events += self._sample(sid, {"org": org})
        return events

    # -- 5. DISPLAY (from the scratch org) ------------------------------------
    def _display(self, sid: str, org, said: str) -> list:
        sf = self.load(sid)["sf_build"]
        plan, built = sf["built_plan"], sf["built"]
        names = [o["api_name"] for o in plan["objects"]
                 if o["api_name"] in built["objects"] or not o["api_name"].endswith("__c")]
        main_plan, child_plan = sf_views.main_objects(plan)
        try:
            describes = [org.describe(name) for name in names]
            by_api = {d.get("name"): d for d in describes}
            records, child_records = [], []
            if main_plan is not None and main_plan["api_name"] in by_api:
                records = org.query(_soql(by_api[main_plan["api_name"]]))
            if child_plan is not None and child_plan["api_name"] in by_api:
                child_records = records if child_plan is main_plan else org.query(_soql(by_api[child_plan["api_name"]]))
        except Exception as exc:
            return self._note(sid, "Built, but the read-back failed: %s" % _why(exc))

        def show(state, sf, out):
            model = sf_views.built_model(sf["built_plan"]["title"], describes, self._observed())
            nodes = []
            if main_plan is not None and main_plan["api_name"] in by_api:
                d = by_api[main_plan["api_name"]]
                rows = sf_views.describe_rows(d, records, org.links)
                first = rows[0] if rows else (d.get("label") + " (no records yet)", [
                    (f.get("label"), "-") for f in d.get("fields") or [] if f.get("custom")])
                related = [o.get("labelPlural") or o.get("label") for o in describes
                           if any(f.get("type") == "reference" and main_plan["api_name"] in (f.get("referenceTo") or [])
                                  for f in o.get("fields") or [] if f.get("custom"))]
                nodes.append(sf_views.record_page_node(d.get("label"), d.get("name"), first[0], first[1], related,
                                                       built=True))
            if child_plan is not None and child_plan["api_name"] in by_api:
                d = by_api[child_plan["api_name"]]
                nodes.append(sf_views.list_view_node(d.get("labelPlural") or d.get("label"), d.get("name"),
                                                     sf_views.describe_rows(d, child_records, org.links), built=True))
            sf.update(phase="display", step="read_back")
            self._rec(sid, sf, out, "read_back", objects=[d.get("name") for d in describes])
            lines = [said]
            for d in describes:
                if d.get("custom"):
                    lk = org.links(d.get("name"))
                    lines.append("%s: Object Manager %s - list %s" % (d.get("label"), lk["object_manager"], lk["list"]))
            lines.append("Say \"run the tests\" when it looks right.")
            drafts = [{"type": "model.updated", "payload": {"model": model}}]
            if nodes:
                drafts.append({"type": "artifact.patch", "payload": {"ops": sf_views.replace_ops(state["artifact"], nodes)}})
            return drafts + _confirm(state, " ".join(lines))

        events, _ = self._commit(sid, show)
        if main_plan is not None and main_plan["api_name"] in by_api:
            self._view(sid, org, SCRATCH, "object", main_plan["label"], main_plan["api_name"])
            if records:
                self._view(sid, org, SCRATCH, "record", main_plan["label"], main_plan["api_name"],
                           str(records[0].get("Id") or ""))
        if child_plan is not None and child_plan["api_name"] in by_api:
            self._view(sid, org, SCRATCH, "list", child_plan["plural"], child_plan["api_name"])
        return events

    def _insert_samples(self, org, sf: dict) -> tuple:
        plan = sf["built_plan"]
        objs = [o for o in plan["objects"] if o["api_name"] in sf["built"]["objects"]]
        ordered = _parents_first(objs)
        masters = {f["ref"] for o in objs for f in o["fields"] if f["type"] in ("MasterDetail", "Lookup")}
        ids: dict = {}
        total = 0
        today = self._today()
        for obj in ordered:
            n = 3 if obj["api_name"] in masters else 10
            rows = []
            for i in range(1, n + 1):
                rec = {"attributes": {"type": obj["api_name"]}}
                if obj["name_field"]["type"] == "Text":
                    rec["Name"] = sf_views.sample_name(obj, i)
                for f in obj["fields"]:
                    if f["type"] in ("Lookup", "MasterDetail"):
                        parents = ids.get(f["ref"])
                        if parents:
                            rec[f["api_name"]] = parents[(i - 1) % len(parents)]
                        elif f["type"] == "MasterDetail":
                            rec = None
                            break
                        continue
                    value = sf_views.api_value(f, i, today)
                    if value is not None:
                        rec[f["api_name"]] = value
                if rec is not None:
                    rows.append(rec)
            if rows:
                ids[obj["api_name"]] = org.insert(rows)
                total += len(rows)
        return ids, total

    def _sample(self, sid: str, intent: dict) -> list:
        sf = self.load(sid).get("sf_build") or new_state()
        if sf.get("phase") not in ("display", "test") or not (sf.get("built") or {}).get("objects") or sf.get("pending"):
            raise LaneRefused("sample data comes after a build, in display")

        def start(state, sf, out):
            self._rec(sid, sf, out, "sampling", objects=list(sf["built"]["objects"]))
            return [_progress(state, "Adding sample records in the scratch org...")]

        events, _ = self._commit(sid, start)
        org = intent.get("org")
        try:
            if org is None:
                org = self._open(SCRATCH, sf)
            ids, total = self._insert_samples(org, sf)
        except Exception as exc:
            return events + self._note(sid, "Sample records were not added: %s" % _why(exc))

        def done(state, sf, out):
            self._audit(sid, sf, out, "sample", 0, "inserted %d" % total)
            self._rec(sid, sf, out, "sample_added", objects=list(ids))
            return []

        self._commit(sid, done)
        return events + self._display(sid, org, "Added %d sample records and read them back from the org." % total)

    # -- 6. TEST (acceptance checks in the scratch org) -----------------------
    def _test(self, sid: str, intent: dict) -> list:
        sf = self.load(sid).get("sf_build") or new_state()
        latest = latest_build(sf, SCRATCH)
        if sf.get("phase") not in ("display", "test") or latest is None or sf.get("pending"):
            raise LaneRefused("tests run after a build is displayed from the scratch org")

        def start(state, sf, out):
            sf.update(phase="test", step="testing")
            self._rec(sid, sf, out, "testing", objects=list(sf["built"]["objects"]))
            return [_progress(state, "Running the acceptance checks in the scratch org...")]

        events, _ = self._commit(sid, start)
        try:
            org = self._open(SCRATCH, sf)
            checks = self._run_checks(org, sf)
        except Exception as exc:
            checks = [{"name": "The checks could run", "passed": False, "detail": _why(exc)}]
        passed = bool(checks) and all(c["passed"] for c in checks)

        def report(state, sf, out):
            sf["test"] = {"passed": passed, "build_n": latest["n"], "at": int(self.clock()),
                          "plan_hash": sf_plan.plan_hash(sf.get("built_plan") or {}),
                          "checks": [dict(c) for c in checks]}
            sf["step"] = "passed" if passed else "failed"
            self._audit(sid, sf, out, "test", len(checks), "passed" if passed else "failed")
            self._rec(sid, sf, out, "test_passed" if passed else "test_failed", components=len(checks))
            node = _test_node(checks, passed)
            said = ("All %d acceptance checks passed. Say \"approved\" or \"promote to the developer org\" to "
                    "promote it." % len(checks)) if passed else \
                ("%d of %d checks failed; change the prototype and build again." %
                 (sum(1 for c in checks if not c["passed"]), len(checks)))
            return [{"type": "artifact.patch", "payload": {"ops": sf_views.replace_ops(
                state["artifact"], [node], drop_prefixes=("sfb-test",))}}] + _confirm(state, said)

        more, _ = self._commit(sid, report)
        main_plan, child_plan = sf_views.main_objects(sf["built_plan"])
        if child_plan is not None:
            try:
                self._view(sid, org, SCRATCH, "list", child_plan["plural"], child_plan["api_name"])
            except Exception:
                pass
        return events + more

    def _run_checks(self, org, sf: dict) -> list:
        """Acceptance checks, each answered by the org (describe and query), never by the plan alone."""
        plan, built = sf["built_plan"], sf["built"]
        objs = [o for o in plan["objects"] if o["api_name"] in built["objects"]]
        describes = {o["api_name"]: org.describe(o["api_name"]) for o in objs}
        if not all(org.query("SELECT Id FROM %s LIMIT 1" % o["api_name"]) for o in objs):
            self._insert_samples(org, sf)
        checks = []
        for o in objs:
            have = {f.get("name") for f in describes[o["api_name"]].get("fields") or []}
            missing = [f["api_name"] for f in o["fields"] if f["api_name"] not in have]
            checks.append({"name": "%s has every planned field" % o["label"], "passed": not missing,
                           "detail": "missing: " + ", ".join(missing) if missing else "%d fields" % len(o["fields"])})
            rows = org.query("SELECT Id, Name FROM %s LIMIT 200" % o["api_name"])
            checks.append({"name": "%s records can be created" % o["label"], "passed": bool(rows),
                           "detail": "%d record%s" % (len(rows), "" if len(rows) == 1 else "s")})
            unnamed = [r for r in rows if not r.get("Name")]
            checks.append({"name": "Every %s has a name" % o["label"], "passed": not unnamed,
                           "detail": "%d without a name" % len(unnamed)})
            for f in o["fields"]:
                if f["type"] not in ("Lookup", "MasterDetail") or f["api_name"] not in have:
                    continue
                if f["type"] == "Lookup" and f["ref"] not in built["objects"]:
                    continue
                linked = org.query("SELECT Id FROM %s WHERE %s != null LIMIT 200" % (o["api_name"], f["api_name"]))
                ok = len(linked) == len(rows) if f["type"] == "MasterDetail" else bool(linked)
                checks.append({"name": "%s.%s links to %s" % (o["label"], f["label"], f["ref"]), "passed": ok,
                               "detail": "%d of %d linked" % (len(linked), len(rows))})
        for o in objs:
            for f in o["fields"]:
                if f["type"] == "Summary":
                    checks.append(self._rollup_check(org, plan, o, f))
        return checks

    def _rollup_check(self, org, plan: dict, parent: dict, f: dict) -> dict:
        child = next(c for c in plan["objects"] if c["api_name"] == f["child"])
        link = next(c["api_name"] for c in child["fields"] if c["type"] == "MasterDetail" and c["ref"] == parent["api_name"])
        name = "Roll-up %s.%s computes" % (parent["label"], f["label"])
        try:
            parents = org.query("SELECT Id, %s FROM %s LIMIT 50" % (f["api_name"], parent["api_name"]))
            cols = link + ("" if f["operation"] == "count" else ", " + f["child_field"])
            kids = org.query("SELECT %s FROM %s LIMIT 2000" % (cols, child["api_name"]))
        except Exception as exc:
            return {"name": name, "passed": False, "detail": _why(exc)}
        wrong = 0
        for p in parents:
            mine = [k for k in kids if k.get(link) == p.get("Id")]
            values = [float(k.get(f["child_field"]) or 0) for k in mine] if f["operation"] != "count" else []
            expected = {"count": len(mine), "sum": sum(values), "min": min(values) if values else None,
                        "max": max(values) if values else None}[f["operation"]]
            got = p.get(f["api_name"])
            if expected is None:
                continue
            if got is None or abs(float(got) - float(expected)) > 0.01:
                wrong += 1
        return {"name": name, "passed": bool(parents) and wrong == 0,
                "detail": "%d of %d parents match" % (len(parents) - wrong, len(parents))}

    # -- 7. PROMOTE read-back (developer org) --------------------------------
    def _promoted_readback(self, sid: str, org) -> list:
        sf = self.load(sid)["sf_build"]
        names = [o["api_name"] for o in sf["built_plan"]["objects"]
                 if o["api_name"] in sf["dev_built"]["objects"] or not o["api_name"].endswith("__c")]
        try:
            describes = [org.describe(n) for n in names]
        except Exception as exc:
            return self._note(sid, "Promoted, but the read-back failed: %s" % _why(exc))

        def show(state, sf, out):
            model = sf_views.built_model(sf["built_plan"]["title"], describes, self._observed())
            model["domain"] = ("Promoted to your developer org: " + sf["built_plan"]["title"])[:120]
            self._rec(sid, sf, out, "promote_read_back", objects=[d.get("name") for d in describes])
            lines = ["Promoted and read back from your developer org."]
            for d in describes:
                if d.get("custom"):
                    lines.append("%s: Object Manager %s" % (d.get("label"), org.links(d.get("name"))["object_manager"]))
            return [{"type": "model.updated", "payload": {"model": model}}] + _confirm(state, " ".join(lines))

        events, _ = self._commit(sid, show)
        return events

    # -- undo ----------------------------------------------------------------
    def _undo(self, sid: str, intent: dict) -> list:
        target = intent.get("target") or SCRATCH

        def ask(state, sf, out):
            build = latest_build(sf, target)
            allowed = ("promote",) if target == DEVORG else ("display", "prototype", "test")
            if sf.get("phase") not in allowed or build is None or sf.get("pending"):
                raise LaneRefused("there is no %s to undo" % ("promote" if target == DEVORG else "build"))
            sf["round"] = int(sf.get("round") or 0) + 1
            qid = "%sundo-%d" % (QUESTION_PREFIX, sf["round"])
            sf["undo_from"] = sf["phase"]
            sf.update(step="undo_asked", pending={"question_id": qid, "kind": "undo_confirm", "target": target,
                                                  "build": build["n"]})
            self._rec(sid, sf, out, "undo_asked", objects=build["new_objects"])
            what = ", ".join(build["new_objects"] + build["field_adds"])[:400]
            where = "your developer org" if target == DEVORG else "the scratch org"
            q = _question(qid, state["artifact"]["id"], "Salesforce build > Undo",
                          "Undo deletes what that %s created, records included." % (
                              "promote" if target == DEVORG else "build"),
                          "Undo the last %s?" % ("promote" if target == DEVORG else "build"), [
                              {"option_id": "yes", "label": "Yes, undo it",
                               "consequence": "Deletes %s from %s, with any records in them." % (what, where)},
                              {"option_id": "no", "label": "Keep it", "consequence": "Nothing changes."}])
            return [{"type": "question.asked", "payload": {"question": q}}]

        events, _ = self._commit(sid, ask)
        return events

    def _undone(self, sid: str, target: str, org, result: dict) -> list:
        action = "promote_undo" if target == DEVORG else "undo"

        def settle(state, sf, out):
            pending = sf.get("pending") or {}
            build = _build_n(sf, pending.get("build"))
            if not result["success"]:
                self._audit(sid, sf, out, action, build["undo_components"], "failed", build["plan_hash"], target)
                sf.update(phase=sf.get("undo_from") or "display", step="undo_failed", pending=None)
                self._rec(sid, sf, out, "undo_failed", components=build["undo_components"])
                return [_progress(state, "Undo failed in Salesforce: " + ("; ".join(result["errors"]) or result["status"]))]
            build["undone_at"] = int(self.clock())
            sf[_built_key(target)] = sf_plan.forget_built(sf[_built_key(target)], build["change"])
            if target == SCRATCH:
                remaining = latest_build(sf, SCRATCH)
                sf["built_plan"] = copy.deepcopy(remaining["plan"]) if remaining else None
                sf["test"] = None
            self._audit(sid, sf, out, action, build["undo_components"], "succeeded", build["plan_hash"], target)
            sf.update(phase="prototype" if target == SCRATCH else "promote", step="undone", pending=None)
            self._rec(sid, sf, out, "undone", components=build["undo_components"], objects=build["new_objects"])
            drafts = [_progress(state, "Undone: %s %d is removed from %s." % (
                "promote" if target == DEVORG else "build", build["n"],
                "your developer org" if target == DEVORG else "the scratch org"))]
            if target == SCRATCH:
                drafts.append({"type": "model.updated",
                               "payload": {"model": sf_views.proposed_model(sf["plan"], sf["built"])}})
                drafts += _confirm(state, "The prototype is still here; say \"build it\" to build it again.")
            return drafts
        events, _ = self._commit(sid, settle)
        return events

    def _status(self, sid: str, intent: dict) -> list:
        sf = self.load(sid).get("sf_build") or new_state()
        pending = sf.get("pending") or {}
        job = pending.get("deploy_id")
        if not job:
            return self._note(sid, "Nothing is running in Salesforce right now (phase: %s)." % sf.get("phase"))
        org = self._open(pending.get("target", SCRATCH), sf)
        return self._follow(sid, org, job)

    def _hint(self, sid: str, intent: dict) -> list:
        return self._note(sid, "Answer the question on screen - yes goes ahead, not yet keeps things as they are.")

    def _note(self, sid: str, text: str) -> list:
        events, _ = self._commit(sid, lambda state, sf, out: _confirm(state, text))
        return events


def _test_node(checks: list, passed: bool) -> dict:
    title = "Acceptance tests - %d of %d passed" % (sum(1 for c in checks if c["passed"]), len(checks))
    cards = [{"id": "sfb-test-c%d" % i, "kind": "card",
              "label": ("PASS - " if c["passed"] else "FAIL - ") + c["name"][:150],
              "detail": str(c.get("detail") or "")[:300]} for i, c in enumerate(checks[:40])]
    return {"id": "sfb-test", "kind": "section", "label": title,
            "children": [{"id": "sfb-test-h", "kind": "heading", "label": title}] + cards}


def _build_n(sf: dict, n) -> dict:
    build = next((b for b in sf.get("builds") or [] if b.get("n") == n and not b.get("undone_at")), None)
    if build is None:
        raise LaneRefused("that build is no longer undoable")
    return build


class _Dedup(LaneRefused):
    def __init__(self):
        super().__init__("duplicate utterance", 200)


def _soql(describe: dict) -> str:
    """Built only from names the org's describe returned (never from text)."""
    names = [f["name"] for f in describe.get("fields") or []
             if f.get("custom") and f.get("type") not in ("textarea",) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", f["name"])][:5]
    obj = describe["name"]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", obj):
        raise ValueError("bad object name")
    return "SELECT Id, Name%s FROM %s ORDER BY CreatedDate DESC LIMIT 10" % ("".join(", " + n for n in names), obj)


def _parents_first(objs: list) -> list:
    names = {o["api_name"] for o in objs}
    done, out = set(), []
    pending = list(objs)
    while pending:
        progressed = False
        for o in list(pending):
            deps = {f["ref"] for f in o["fields"] if f["type"] in ("MasterDetail", "Lookup") and f["ref"] in names
                    and f["ref"] != o["api_name"]}
            if deps <= done:
                out.append(o)
                done.add(o["api_name"])
                pending.remove(o)
                progressed = True
        if not progressed:
            out += pending
            break
    return out


def _why(exc: Exception) -> str:
    if isinstance(exc, LaneRefused):
        return str(exc)
    name = type(exc).__name__
    if name in ("SalesforceError", "OrgMismatch", "PlanError"):
        return str(exc)[:500]
    return name


def _log(event: str, **fields) -> None:
    line = {"severity": "INFO", "event": event}
    line.update(fields)
    print(json.dumps(line, sort_keys=True), file=sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# The architect: proposes; never decides what is built.

ARCH_TOOL = {
    "name": "propose",
    "description": "Record the architecture and the design options. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "label": {"type": "string"}, "detail": {"type": "string"}},
                "required": ["id", "label", "detail"]}},
            "edges": {"type": "array", "items": {"type": "object", "properties": {
                "from": {"type": "string"}, "to": {"type": "string"}, "label": {"type": "string"},
                "detail": {"type": "string"}}, "required": ["from", "to", "label"]}},
            "explanation": {"type": "string"},
            "options": {"type": "array", "items": {"type": "object", "properties": {
                "option_id": {"type": "string", "enum": ["a", "b", "c"]},
                "label": {"type": "string"}, "summary": {"type": "string"},
                "limits": {"type": "string"}, "storage": {"type": "string"}, "licences": {"type": "string"},
                "complexity": {"type": "string"}, "time_to_build": {"type": "string"},
                "recommended": {"type": "boolean"}, "because": {"type": "string"},
                "plan": {"type": "object"}},
                "required": ["option_id", "label", "summary", "limits", "storage", "licences", "complexity",
                             "time_to_build", "recommended", "plan"]}},
        },
        "required": ["title", "steps", "edges", "explanation", "options"],
    },
}

REVISE_TOOL = {
    "name": "revise",
    "description": "Record the whole revised plan and one sentence on what changed. Call exactly once.",
    "input_schema": {"type": "object", "properties": {"plan": {"type": "object"}, "summary": {"type": "string"}},
                     "required": ["plan", "summary"]},
}

PLAN_RULES = """A PLAN is JSON: {"title": str, "objects": [...]}. Each new object: {"api_name": "Agent_Spend__c", \
"label": "Agent Spend", "plural": "Agent Spend", "description": "", "name_field": {"type": "Text", "label": \
"Agent Spend Name"} or {"type": "AutoNumber", "label": "Spend Number", "format": "SP-{0000}"}, "fields": [...]}. \
To add custom fields to a standard object (only Account, Contact, Lead, Opportunity, Case): {"api_name": "Account", \
"fields": [...]} and nothing else. Each field: {"api_name": "Cost__c", "label": "Cost", "type": T, ...} with T one \
of Text (length 1-255), Number (precision, scale), Currency (precision, scale), Percent (precision, scale), Date, \
DateTime, Checkbox, Picklist (values: list of plain strings), LongTextArea, Lookup (ref: a plan object or Account, \
Contact, Lead, Opportunity, Case, User), MasterDetail (ref: a new plan object or Account, Contact, Opportunity, \
Case; at most 2 per object), Summary (a roll-up on the MASTER: operation count|sum|min|max, child: the detail \
object, child_field: a Number/Currency/Percent field on it, omitted for count). API names: letters, digits and \
single underscores, ending __c, at most 40 characters. Labels: plain letters, digits, spaces and , . ( ) / : % # + -. \
At most 6 objects and 60 fields. Nothing else exists: no Apex, triggers, flows, profiles, sharing rules, layouts, \
remote sites, Big Objects, platform events or deletes - if an option depends on one, say so in its trade-offs and \
plan only the custom objects part."""

DISCUSS_SYSTEM = """You are the solution architect in a live working session with the owner of sfdc24.com, \
in his own Salesforce OmniStudio developer org. Everything built there is reversible. He describes what he wants \
to see modelled in Salesforce; you DISCUSS before anything is built.

1. Draw the solution architecture as a flow of 4 to 9 steps (id: short a-z word; label: the part; detail: what it \
does) and the edges between them (from, to, label: what passes; detail: the trigger or protocol). Cover where the \
data comes from (sources), how it is captured (for example platform event, record insert by an integration, a \
scheduled import), where it is stored (for example custom objects, a Big Object, Event Monitoring logs) and how it \
rolls up and is reported. title: a short name for the flow.
2. explanation: two or three spoken sentences on what the flow shows and the key choice ahead.
3. options: 2 or 3 genuinely different designs (option_id a, b, c). For each, the trade-offs in a few words each: \
limits (governor and API limits, data volume), storage (cost and growth), licences (for example Event Monitoring, \
Shield, none), complexity, time_to_build. Recommend exactly one and say why in "because". Each option carries a \
buildable PLAN for its data model.

""" + PLAN_RULES + "\n\n" + USE_POLICY

REVISE_SYSTEM = """You revise a Salesforce data model PLAN during a live prototype. Apply exactly the owner's \
change - rename, add or retype a field, add a roll-up, change a relationship - and return the WHOLE plan. Never \
remove or change anything listed as BUILT (that needs undo); add new things instead. summary: one sentence on what \
changed.

""" + PLAN_RULES + "\n\n" + USE_POLICY

ARCH_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,30}")


def _plain(value, cap: int) -> str:
    return " ".join(str(value or "").split())[:cap]


def check_architecture(raw: dict) -> dict | None:
    steps, ids = [], set()
    for s in (raw.get("steps") or [])[:10]:
        sid = str(s.get("id") or "").lower()
        if not ARCH_ID_RE.fullmatch(sid) or sid in ids or not _plain(s.get("label"), 80):
            continue
        ids.add(sid)
        steps.append({"id": sid, "label": _plain(s.get("label"), 80), "detail": _plain(s.get("detail"), 300)})
    edges = []
    for e in (raw.get("edges") or [])[:16]:
        a, b = str(e.get("from") or "").lower(), str(e.get("to") or "").lower()
        if a in ids and b in ids and a != b:
            edges.append({"from": a, "to": b, "label": _plain(e.get("label"), 80) or "flows to",
                          "detail": _plain(e.get("detail"), 200)})
    if len(steps) < 3:
        return None
    return {"title": _plain(raw.get("title"), 80) or "Solution architecture", "steps": steps, "edges": edges}


def check_options(raw_options: list) -> tuple:
    options, problems = [], []
    for o in (raw_options or [])[:3]:
        oid = o.get("option_id")
        if oid not in ("a", "b", "c") or any(x["option_id"] == oid for x in options):
            problems.append("option id %r dropped" % oid)
            continue
        try:
            plan = sf_plan.validate_plan(o.get("plan"))
        except sf_plan.PlanError as exc:
            problems.append("option %s dropped: %s" % (oid, exc))
            continue
        trade = "; ".join("%s: %s" % (k.replace("_", " "), _plain(o.get(k), 120))
                          for k in ("limits", "storage", "licences", "complexity", "time_to_build") if o.get(k))
        consequence = _plain(o.get("summary"), 300)
        if trade:
            consequence = (consequence + " Trade-offs - " + trade)[:600]
        options.append({"option_id": oid, "label": _plain(o.get("label"), 80) or "Option " + oid.upper(),
                        "consequence": consequence or "A design option.", "recommended": False,
                        "because": _plain(o.get("because"), 300), "plan": plan,
                        "_recommended": bool(o.get("recommended"))})
    rec = next((o for o in options if o["_recommended"]), options[0] if options else None)
    for o in options:
        o["recommended"] = o is rec
        o.pop("_recommended")
    return options, problems


class Architect:
    def __init__(self, client=None, model: str | None = None, effort: str | None = None, budget_seconds: float = 40.0):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(timeout=budget_seconds, max_retries=0)
        self.client = client
        self.model = model or os.environ.get("STUDIO_SF_ARCHITECT_MODEL") or "claude-opus-5"
        self.effort = effort or os.environ.get("STUDIO_SF_ARCHITECT_EFFORT") or "low"
        self.budget_seconds = budget_seconds

    def _call(self, system: str, tool: dict, prompt: str) -> dict:
        call = bounded.abortable(self.client)
        try:
            resp = bounded.run_within(self.budget_seconds, lambda: call.client.messages.create(
                model=self.model, max_tokens=8000, system=system, output_config={"effort": self.effort},
                tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": prompt}]), cancel=call.abort)
        finally:
            call.close()
        block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        return block.input if block is not None and isinstance(block.input, dict) else {}

    def discuss(self, ask: str, prior_arch, built_plan) -> dict:
        prompt = "THE OWNER'S ASK OR CHANGE: %s\n\nTHE ARCHITECTURE SO FAR: %s\n\nALREADY BUILT IN THE ORG: %s" % (
            ask, json.dumps(prior_arch or {})[:4000], json.dumps(built_plan or {})[:4000])
        raw = self._call(DISCUSS_SYSTEM, ARCH_TOOL, prompt)
        arch = check_architecture(raw)
        if arch is None:
            return {"problem": "the architecture came back incomplete"}
        options, problems = check_options(raw.get("options"))
        return {"architecture": arch, "explanation": _plain(raw.get("explanation"), 500), "options": options,
                "problems": problems}

    def revise(self, plan: dict, text: str, built: dict | None) -> dict:
        prompt = "THE PLAN: %s\n\nBUILT (never remove or change): %s\n\nTHE OWNER'S CHANGE: %s" % (
            json.dumps(plan), json.dumps(sorted(list((built or {}).get("objects") or {})
                                                + list((built or {}).get("fields") or {}))), text)
        raw = self._call(REVISE_SYSTEM, REVISE_TOOL, prompt)
        if not isinstance(raw.get("plan"), dict):
            return {"problem": "no plan came back"}
        return {"plan": raw["plan"], "summary": _plain(raw.get("summary"), 300)}


__all__ = ["SfBuildLane", "LaneRefused", "Architect", "classify", "new_state", "merge_plans", "check_options",
           "check_architecture", "latest_build", "TOPIC", "PHASES", "QUESTION_PREFIX", "SCRATCH", "DEVORG"]



class LazyArchitect:
    """The Architect, built on first use (so the app starts without the SDK call path)."""

    def __init__(self, factory=Architect):
        self._factory, self._architect = factory, None
        self._lock = threading.Lock()

    def _get(self):
        with self._lock:
            if self._architect is None:
                self._architect = self._factory()
            return self._architect

    def discuss(self, *a, **k):
        return self._get().discuss(*a, **k)

    def revise(self, *a, **k):
        return self._get().revise(*a, **k)

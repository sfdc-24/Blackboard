"""The studio's Claude worker: model output is DATA, checked before it can reach a visitor.

Every test here drives on_turn() with a fake client returning a chosen draft, so
what is under test is the gate between the model and the page, not the model.
"""
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "claude_worker", REPO / "cloud" / "studio-controller" / "workers" / "claude_worker.py")
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)

ROOT = {"id": "screen-home", "kind": "screen", "label": "Homepage", "children": [
    {"id": "hero", "kind": "section", "label": "Hero", "children": [
        {"id": "hero-heading", "kind": "heading", "label": "Your Salesforce, working"},
        {"id": "hero-cta", "kind": "button", "label": "Get started"}]}]}
BLANK = {"id": "", "kind": "text", "label": "", "detail": ""}


def q(qid="q-cta", affected=("hero-cta",), options=None):
    return {"question_id": qid, "group": "Screen", "scope_path": "Homepage > Hero > Primary action",
            "reason": "It decides what the first screen does.", "prompt": "What should the main action be?",
            "options": options or [
                {"option_id": "book", "label": "Book", "consequence": "Opens scheduling",
                 "recommended": False, "recommended_because": ""},
                {"option_id": "describe", "label": "Describe a problem", "consequence": "Opens intake",
                 "recommended": True, "recommended_because": "Visitors arrive with a problem."}],
            "affected_artifact_ids": list(affected)}


class FakeClient:
    def __init__(self, draft=None, stop="end_turn", raw=None):
        self.calls = []
        text = raw if raw is not None else json.dumps(draft)
        self.resp = SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.resp


def run(draft=None, questions=(), **fake):
    if draft is not None and "resolves" not in draft:
        draft = dict(draft, resolves={"question_id": "", "option_id": "", "freeform_answer": ""})
    client = FakeClient(draft, **fake)
    w = cw.ClaudeWorker(client=client)
    state = {"artifact": ROOT, "questions": list(questions), "transcript": [], "session_id": "s1", "turn_seq": 3}
    out = w.on_turn(state, {"kind": "utterance", "text": "I need a homepage"})
    return out, client


class Gate(unittest.TestCase):
    def test_a_clean_turn_becomes_change_confirm_then_ask(self):
        out, client = run({"ops": [{"op": "set_label", "node_id": "hero-heading", "value": "Clear your backlog",
                                    "new_node": BLANK}],
                           "confirm": "Heading now names the backlog.", "questions": [q()], "batch_title": ""})
        self.assertEqual([e["type"] for e in out["events"]], ["artifact.patch", "confirm", "question.asked"])
        self.assertEqual(out["problems"], [])
        asked = out["events"][2]["payload"]["question"]
        self.assertEqual(asked["options"][0]["option_id"], "describe", "recommended must be listed first")
        self.assertNotIn("recommended", asked["options"][1], "non-recommended carries no flag")

    def test_the_request_is_structured_low_effort_opus_with_fallback(self):
        _, client = run({"ops": [], "confirm": "", "questions": [], "batch_title": ""})
        kw = client.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5")
        self.assertEqual(kw["output_config"]["effort"], "low")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kw["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", kw["betas"])

    def test_an_op_on_a_node_that_does_not_exist_is_dropped(self):
        out, _ = run({"ops": [{"op": "set_label", "node_id": "ghost", "value": "x", "new_node": BLANK}],
                      "confirm": "Changed the ghost.", "questions": [], "batch_title": ""})
        self.assertEqual(out["events"], [], "no patch, and no confirmation of a change that did not happen")
        self.assertTrue(any("unknown node" in p for p in out["problems"]))

    def test_insert_child_needs_a_real_parent_and_a_fresh_id(self):
        new = {"id": "hero-cta", "kind": "button", "label": "dup", "detail": ""}
        out, _ = run({"ops": [{"op": "insert_child", "node_id": "hero", "value": "", "new_node": new}],
                      "confirm": "", "questions": [], "batch_title": ""})
        self.assertTrue(any("taken" in p for p in out["problems"]))
        ok = {"id": "hero-sub", "kind": "text", "label": "Admins, this week", "detail": ""}
        out, _ = run({"ops": [{"op": "insert_child", "node_id": "hero", "value": "", "new_node": ok}],
                      "confirm": "Added a subline.", "questions": [q(affected=("hero-sub",))], "batch_title": ""})
        self.assertEqual(out["problems"], [])
        self.assertEqual(out["events"][0]["payload"]["ops"][0]["node"]["id"], "hero-sub")

    def test_removing_the_root_is_refused(self):
        out, _ = run({"ops": [{"op": "remove", "node_id": "screen-home", "value": "", "new_node": BLANK}],
                      "confirm": "", "questions": [], "batch_title": ""})
        self.assertTrue(any("root" in p for p in out["problems"]))

    def test_two_recommended_options_drop_the_question(self):
        opts = [{"option_id": "a", "label": "A", "consequence": "a", "recommended": True, "recommended_because": "r"},
                {"option_id": "b", "label": "B", "consequence": "b", "recommended": True, "recommended_because": "r"}]
        out, _ = run({"ops": [], "confirm": "", "questions": [q(options=opts)], "batch_title": ""})
        self.assertEqual(out["events"], [])

    def test_recommended_without_a_reason_drops_the_question(self):
        opts = [{"option_id": "a", "label": "A", "consequence": "a", "recommended": True, "recommended_because": " "},
                {"option_id": "b", "label": "B", "consequence": "b", "recommended": False, "recommended_because": ""}]
        out, _ = run({"ops": [], "confirm": "", "questions": [q(options=opts)], "batch_title": ""})
        self.assertEqual(out["events"], [])

    def test_a_question_already_answered_is_not_asked_again(self):
        answered = {"question_id": "q-cta", "status": "answered", "prompt": "p", "selected_option": "book"}
        out, _ = run({"ops": [], "confirm": "", "questions": [q()], "batch_title": ""}, questions=[answered])
        self.assertEqual(out["events"], [])

    def test_affected_ids_must_exist(self):
        out, _ = run({"ops": [], "confirm": "", "questions": [q(affected=("nowhere",))], "batch_title": ""})
        self.assertEqual(out["events"], [])

    def test_several_questions_become_one_decision_form(self):
        out, _ = run({"ops": [], "confirm": "", "batch_title": "Two quick decisions",
                      "questions": [q("q-a"), q("q-b", affected=("hero-heading",))]})
        self.assertEqual([e["type"] for e in out["events"]], ["decision.batch"])
        batch = out["events"][0]["payload"]
        self.assertTrue(batch["batch_id"].startswith("b-s1-"))
        again, _ = run({"ops": [], "confirm": "", "batch_title": "x", "questions": [q("q-a"), q("q-b", affected=("hero-heading",))]})
        self.assertNotEqual(again["events"][0]["payload"]["batch_id"], batch["batch_id"], "batch ids never repeat")
        self.assertEqual(len(batch["questions"]), 2)

    def test_refusal_truncation_and_non_json_change_nothing(self):
        for kw in ({"stop": "refusal"}, {"stop": "max_tokens"}, {"raw": "not json"}):
            out, _ = run({"ops": [], "confirm": "", "questions": [], "batch_title": ""}, **kw)
            self.assertEqual(out["events"], [], kw)
            self.assertTrue(out["problems"], kw)

    def test_the_output_schema_is_flat(self):
        # The API rejects recursive schemas; a nested node tree must never creep back in.
        self.assertNotIn("children", json.dumps(cw.OUTPUT_SCHEMA))
        self.assertNotIn("$ref", json.dumps(cw.OUTPUT_SCHEMA))


OPEN_Q = dict(q(), status="open")
NOTHING = {"ops": [], "confirm": "", "questions": [], "batch_title": ""}


SCENE_ROOT = {"id": "screen-home", "kind": "screen", "label": "Logo", "children": [
    {"id": "logo", "kind": "scene", "label": "Logo", "detail": "400x400 bg=#FFFFFF", "children": [
        {"id": "mark", "kind": "entity", "label": "Wheat mark", "detail": "circle cx=200 cy=160 r=90 fill=#E8B04B"}]}]}


def run_on(root, draft):
    draft = dict(draft, resolves={"question_id": "", "option_id": "", "freeform_answer": ""})
    client = FakeClient(draft)
    state = {"artifact": root, "questions": [], "transcript": [], "session_id": "s1", "turn_seq": 1}
    return cw.ClaudeWorker(client=client).on_turn(state, {"kind": "utterance", "text": "a logo"})


def insert(parent, node_id, kind, label, detail):
    return {"op": "insert_child", "node_id": parent, "value": "",
            "new_node": {"id": node_id, "kind": kind, "label": label, "detail": detail}}


class LiveScenes(unittest.TestCase):
    """Scenes run as a live engine on the page, built from a closed grammar - never markup or code."""

    def test_a_live_banner_with_motion_physics_particles_and_pointer(self):
        out = run_on(ROOT, {"ops": [
            insert("screen-home", "banner", "scene", "Autumn banner", "1200x400 bg=#0B3D2E gravity=900"),
            insert("banner", "band", "entity", "Band", "rect x=0 y=300 width=1200 height=100 fill=#E8B04B"),
            insert("banner", "headline", "entity", "Fresh bread daily",
                   "text x=600 y=190 size=72 weight=700 anchor=middle font=display fill=#FFFFFF float=6 period=3"),
            insert("banner", "wave", "entity", "Wave", "path d=M0,300C300,250,900,350,1200,300Z fill=#14553F"),
            insert("banner", "star", "entity", "Star", "polygon points=10,0;20,20;0,20 fill=#FFF spin=40"),
            insert("banner", "ball", "entity", "Ball",
                   "circle cx=100 cy=50 r=24 fill=#F25C54 body=1 bounce=1 drag=1 tap=jump vx=120"),
            insert("banner", "snow", "entity", "Flour dust",
                   "particles x=600 y=0 rate=30 size=3 speed=40 angle=90 spread=160 life=6 shape=circle fill=#FFFFFF"),
            insert("banner", "moon", "entity", "Moon", "circle cx=0 cy=0 r=20 fill=#FFF orbit=600,200,150,30 glow=#FFE9A8"),
            insert("banner", "counter", "entity", "Counter", "rect x=0 y=330 width=1200 height=70 fill=#3A2417 solid=1"),
            insert("banner", "slash", "entity", "Crust mark", "line x1=90 y1=45 x2=110 y2=30 stroke=#8A5A2B attach=ball"),
        ], "confirm": "Built the live banner.", "questions": [], "batch_title": ""})
        self.assertEqual([], out["problems"])
        ops = out["events"][0]["payload"]["ops"]
        self.assertEqual(["scene"] + ["entity"] * 9, [o["node"]["kind"] for o in ops])

    def test_markup_or_script_can_never_be_an_entity(self):
        for detail in ("<svg onload=alert(1)>", "rect x=0 y=0 width=10 height=10 fill=url(#x)",
                       "rect x=0 onclick=alert(1)", "image href=https://evil.example/x.png",
                       "path d=M0,0L10,10javascript:1", "text x=1 y=1 font=Comic",
                       "rect x=0 x=1", "circle cx=1 cy=1 r=1 opacity=2", "rect x=10px",
                       "circle cx=1 cy=1 r=1 tap=eval", "circle cx=1 cy=1 r=1 body=yes",
                       "circle orbit=1,2,3", "particles shape=script", "rect solid=yes",
                       "line attach=../../x", "line attach=a;b"):
            out = run_on(SCENE_ROOT, {"ops": [insert("logo", "bad", "entity", "Bad", detail)],
                                      "confirm": "Drew it.", "questions": [], "batch_title": ""})
            self.assertEqual([], out["events"], detail)
            self.assertTrue(out["problems"], detail)

    def test_a_scene_needs_a_size_and_holds_only_entities(self):
        for detail in ("", "big", "4000x400", "1200x400 bg=red", "1200 x 400", "1200x400 gravity=9.8",
                       "1200x400 bg=#000 bg=#fff"):
            out = run_on(ROOT, {"ops": [insert("screen-home", "g", "scene", "Banner", detail)],
                                "confirm": "", "questions": [], "batch_title": ""})
            self.assertEqual([], out["events"], detail)
        out = run_on(SCENE_ROOT, {"ops": [insert("logo", "t", "text", "Loose words", "")],
                                  "confirm": "", "questions": [], "batch_title": ""})
        self.assertIn("only entities", " ".join(out["problems"]))

    def test_an_entity_outside_a_scene_is_refused(self):
        out = run_on(ROOT, {"ops": [insert("hero", "dot", "entity", "Dot", "circle cx=1 cy=1 r=1")],
                            "confirm": "", "questions": [], "batch_title": ""})
        self.assertIn("inside a scene", " ".join(out["problems"]))

    def test_changing_an_entity_goes_through_the_same_grammar(self):
        ok = run_on(SCENE_ROOT, {"ops": [{"op": "set_detail", "node_id": "mark", "new_node": BLANK,
                                          "value": "circle cx=200 cy=160 r=90 fill=#1B4D3E spin=20"}],
                                 "confirm": "Made the mark green and spinning.", "questions": [], "batch_title": ""})
        self.assertEqual([], ok["problems"])
        bad = run_on(SCENE_ROOT, {"ops": [{"op": "set_detail", "node_id": "mark", "new_node": BLANK,
                                           "value": "circle fill=javascript:x"}],
                                  "confirm": "Changed it.", "questions": [], "batch_title": ""})
        self.assertEqual([], bad["events"])
        resize = run_on(SCENE_ROOT, {"ops": [{"op": "set_detail", "node_id": "logo", "new_node": BLANK,
                                              "value": "600x600 gravity=400"}],
                                     "confirm": "Made it bigger.", "questions": [], "batch_title": ""})
        self.assertEqual([], resize["problems"])

    def test_the_prompt_teaches_the_grammar_the_gate_enforces(self):
        for word in ('"scene"', '"entity"', "polygon points=", "path d=", "anchor=start|middle|end",
                     "font=sans|serif|mono|display", "tap=pulse|spin|burst|jump|hide", "orbit=cx,cy,radius,deg/s",
                     "shape=circle|square|star", "body=1", "drag=1", "solid=1", "attach=<entity id>", "[Tagline]"):
            self.assertIn(word, cw.SYSTEM)
        self.assertEqual(set(cw.SHAPES), {"rect", "circle", "ellipse", "line", "polygon", "path", "text",
                                          "particles"})
        for shape, keys in cw.SHAPES.items():
            for key, kind in keys.items():
                self.assertIn(kind, cw._VALUES, (shape, key))


class Resolves(unittest.TestCase):
    """A spoken answer: the worker names which open question it resolves; the
    controller owns the record and emits question.answered."""

    def res(self, r, questions=(OPEN_Q,)):
        out, _ = run(dict(NOTHING, resolves=r), questions=questions)
        return out

    def test_a_clear_spoken_choice_resolves_to_that_option(self):
        out = self.res({"question_id": "q-cta", "option_id": "book", "freeform_answer": ""})
        self.assertEqual(out["resolves"], {"question_id": "q-cta", "option_id": "book"})

    def test_their_own_words_resolve_as_freeform(self):
        out = self.res({"question_id": "q-cta", "option_id": "", "freeform_answer": "Call us, straight to my cell"})
        self.assertEqual(out["resolves"]["freeform_answer"], "Call us, straight to my cell")

    def test_an_option_the_question_does_not_have_is_refused(self):
        out = self.res({"question_id": "q-cta", "option_id": "teleport", "freeform_answer": ""})
        self.assertIsNone(out["resolves"])
        self.assertTrue(any("unknown option" in p for p in out["problems"]))

    def test_only_an_open_question_can_be_resolved(self):
        answered = dict(q(), status="answered", selected_option="book")
        out = self.res({"question_id": "q-cta", "option_id": "book", "freeform_answer": ""}, questions=(answered,))
        self.assertIsNone(out["resolves"])

    def test_nothing_answered_means_no_resolution(self):
        out = self.res({"question_id": "", "option_id": "", "freeform_answer": ""})
        self.assertIsNone(out["resolves"])
        self.assertEqual(out["problems"], [])

    def test_a_resolution_with_no_answer_is_refused(self):
        out = self.res({"question_id": "q-cta", "option_id": "", "freeform_answer": "  "})
        self.assertIsNone(out["resolves"])


def run_trigger(draft, questions=(), trigger=None):
    draft = dict(draft)
    draft.setdefault("resolves", {"question_id": "", "option_id": "", "freeform_answer": ""})
    client = FakeClient(draft)
    w = cw.ClaudeWorker(client=client)
    state = {"artifact": ROOT, "questions": list(questions), "transcript": [], "session_id": "s1", "turn_seq": 1}
    return w.on_turn(state, trigger or {"kind": "utterance", "text": "go"}), client


def op(kind, nid, value="", node=None):
    return {"op": kind, "node_id": nid, "value": value, "new_node": node or BLANK}


class CodexReview200(unittest.TestCase):
    """CODEX-PR200-REVIEW-20260924T0501Z: each finding as a regression."""

    def test_p1_the_model_sees_what_the_chosen_option_means(self):
        mk = lambda cons: dict(q(options=[
            {"option_id": "a", "label": "Option A", "consequence": cons, "recommended": False, "recommended_because": ""},
            {"option_id": "b", "label": "Option B", "consequence": "other", "recommended": False, "recommended_because": ""}]),
            status="open")
        t = {"kind": "answer", "question_id": "q-cta", "option_id": "a"}
        _, c1 = run_trigger({"ops": [], "confirm": "", "questions": [], "batch_title": ""}, [mk("Opens scheduling")], t)
        _, c2 = run_trigger({"ops": [], "confirm": "", "questions": [], "batch_title": ""}, [mk("Sends an email")], t)
        m1 = c1.calls[0]["messages"][0]["content"]
        m2 = c2.calls[0]["messages"][0]["content"]
        self.assertNotEqual(m1, m2, "the same tap on two different option meanings must not look identical")
        self.assertIn("Opens scheduling", m1.split("LATEST INPUT:")[1])
        self.assertIn("hero-cta", m1.split("LATEST INPUT:")[1])

    def test_p2_an_invalid_dependent_op_drops_the_whole_patch_and_its_confirmation(self):
        out, _ = run_trigger({"ops": [op("set_label", "hero-heading", "New"), op("set_label", "ghost", "x")],
                              "confirm": "Changed the heading and the ghost.", "questions": [], "batch_title": ""})
        self.assertEqual(out["events"], [])
        self.assertTrue(any("whole patch is dropped" in p for p in out["problems"]))

    def test_p2_a_question_about_a_node_the_patch_removed_is_dropped(self):
        out, _ = run_trigger({"ops": [op("remove", "hero")], "confirm": "Removed the hero.",
                              "questions": [q()], "batch_title": ""})
        self.assertEqual([e["type"] for e in out["events"]], ["artifact.patch", "confirm"])

    def test_p2_insert_then_remove_the_new_node_does_not_throw(self):
        new = {"id": "tmp", "kind": "text", "label": "t", "detail": ""}
        out, _ = run_trigger({"ops": [op("insert_child", "hero", node=new), op("remove", "tmp")],
                              "confirm": "x", "questions": [], "batch_title": ""})
        self.assertEqual(out["problems"], [])

    def test_p2_an_answer_may_change_only_its_question_s_nodes(self):
        asked = dict(q(), status="open")
        t = {"kind": "answer", "question_id": "q-cta", "option_id": "describe"}
        out, _ = run_trigger({"ops": [op("set_label", "hero-heading", "Unasked change")],
                              "confirm": "x", "questions": [], "batch_title": ""}, [asked], t)
        self.assertEqual(out["events"], [])
        self.assertTrue(any("outside the answered question" in p for p in out["problems"]))
        out, _ = run_trigger({"ops": [op("set_label", "hero-cta", "Describe a problem")],
                              "confirm": "Using Describe a problem.", "questions": [], "batch_title": ""}, [asked], t)
        self.assertEqual([e["type"] for e in out["events"]], ["artifact.patch", "confirm"])

    def test_p2_question_ids_are_fenced(self):
        opened = dict(q(), status="open")
        out, _ = run_trigger({"ops": [], "confirm": "", "questions": [q()], "batch_title": ""}, [opened])
        self.assertEqual(out["events"], [], "an open id is not reused")
        out, _ = run_trigger({"ops": [], "confirm": "", "questions": [q("q-x"), q("q-x")], "batch_title": ""})
        self.assertEqual([e["type"] for e in out["events"]], ["question.asked"], "a same-turn duplicate is dropped")
        out, _ = run_trigger({"ops": [], "confirm": "", "questions": [q()], "batch_title": "",
                              "resolves": {"question_id": "q-cta", "option_id": "book", "freeform_answer": ""}}, [opened])
        self.assertEqual(out["resolves"], {"question_id": "q-cta", "option_id": "book"})
        self.assertEqual(out["events"], [], "a question just resolved is not re-asked under the same id")

    def test_p2_option_text_is_capped(self):
        long = [{"option_id": "a", "label": "x" * 601, "consequence": "c", "recommended": False, "recommended_because": ""},
                {"option_id": "b", "label": "B", "consequence": "c", "recommended": False, "recommended_because": ""}]
        out, _ = run_trigger({"ops": [], "confirm": "", "questions": [q(options=long)], "batch_title": ""})
        self.assertEqual(out["events"], [])

    def test_p2_a_node_never_exceeds_sixty_children(self):
        mk = lambda n: [op("insert_child", "hero", node={"id": "n%d" % i, "kind": "text", "label": "t", "detail": ""})
                        for i in range(n)]
        # hero starts with 2 children: 58 more is exactly 60, allowed
        out, _ = run_trigger({"ops": mk(58), "confirm": "x", "questions": [], "batch_title": ""})
        self.assertEqual(out["problems"], [])
        # one more would be 61: the whole patch is refused
        out, _ = run_trigger({"ops": mk(59), "confirm": "x", "questions": [], "batch_title": ""})
        self.assertEqual(out["events"], [])
        self.assertTrue(any("60 children" in p for p in out["problems"]))

class CodexReReview200(unittest.TestCase):
    """CODEX-PR200-REREVIEW2-20260924T0518Z, P2: a form's answers bound the change too."""

    def test_a_form_may_change_only_the_union_of_its_questions_nodes(self):
        cta = dict(q(), status="open")
        t = {"kind": "answer_batch", "batch_id": "b1", "answers": [{"question_id": "q-cta", "option_id": "book"}]}
        out, _ = run_trigger({"ops": [op("set_label", "hero-heading", "Unasked")], "confirm": "x",
                              "questions": [], "batch_title": ""}, [cta], t)
        self.assertEqual(out["events"], [])
        heading = dict(q("q-head", affected=("hero-heading",)), status="open")
        t2 = {"kind": "answer_batch", "batch_id": "b1", "answers": [
            {"question_id": "q-cta", "option_id": "book"}, {"question_id": "q-head", "option_id": "book"}]}
        out, _ = run_trigger({"ops": [op("set_label", "hero-heading", "Asked"), op("set_label", "hero-cta", "Book")],
                              "confirm": "x", "questions": [], "batch_title": ""}, [cta, heading], t2)
        self.assertEqual(out["problems"], [])

    def test_an_unknown_answered_id_permits_no_change_at_all(self):
        t = {"kind": "answer_batch", "batch_id": "b1", "answers": [{"question_id": "q-nope", "option_id": "x"}]}
        out, _ = run_trigger({"ops": [op("set_label", "hero-cta", "x")], "confirm": "x",
                              "questions": [], "batch_title": ""}, [], t)
        self.assertEqual(out["events"], [])



class CodexReview206(unittest.TestCase):
    """CODEX-PR206-REVIEW-20260924T0625Z: an answer and its change are one decision."""

    def test_an_invalid_claim_lets_nothing_change(self):
        draft = {"ops": [{"op": "set_label", "node_id": "hero-heading", "value": "Unrelated rewrite", "new_node": BLANK}],
                 "confirm": "Rewrote the heading.", "questions": [], "batch_title": "",
                 "resolves": {"question_id": "q-cta", "option_id": "teleport", "freeform_answer": ""}}
        out, _ = run(draft, questions=(OPEN_Q,))
        self.assertIsNone(out["resolves"])
        self.assertEqual([], [e for e in out["events"] if e["type"] in ("artifact.patch", "confirm")])
        self.assertTrue(any("unknown option" in p for p in out["problems"]))

    def test_no_claim_still_lets_an_utterance_shape_the_page(self):
        draft = dict(NOTHING, ops=[{"op": "set_label", "node_id": "hero-heading", "value": "Clear your backlog",
                                    "new_node": BLANK}], confirm="Heading updated.")
        out, _ = run(draft, questions=(OPEN_Q,))
        self.assertEqual(["artifact.patch", "confirm"], [e["type"] for e in out["events"]])

    def test_a_refused_change_leaves_the_answer_unrecorded(self):
        draft = {"ops": [{"op": "set_label", "node_id": "hero-cta", "value": "Book a call", "new_node": BLANK},
                         {"op": "set_label", "node_id": "no-such-node", "value": "x", "new_node": BLANK}],
                 "confirm": "Booked.", "questions": [], "batch_title": "",
                 "resolves": {"question_id": "q-cta", "option_id": "book", "freeform_answer": ""}}
        out, _ = run(draft, questions=(OPEN_Q,))
        self.assertIsNone(out["resolves"], "the question must stay open so it can be answered again")
        self.assertEqual([], [e for e in out["events"] if e["type"] in ("artifact.patch", "confirm")])
        self.assertTrue(any("not recorded" in p for p in out["problems"]), out["problems"])

    def test_a_clean_answer_with_its_change_still_resolves(self):
        draft = {"ops": [{"op": "set_label", "node_id": "hero-cta", "value": "Book a call", "new_node": BLANK}],
                 "confirm": "Booked.", "questions": [], "batch_title": "",
                 "resolves": {"question_id": "q-cta", "option_id": "book", "freeform_answer": ""}}
        out, _ = run(draft, questions=(OPEN_Q,))
        self.assertEqual({"question_id": "q-cta", "option_id": "book"}, out["resolves"])


class BuildsFromAnEmptyScreen(unittest.TestCase):
    def test_the_prompt_says_to_build_a_whole_first_version_from_an_empty_screen(self):
        self.assertIn("BUILDING FROM AN EMPTY SCREEN", cw.SYSTEM)
        self.assertIn("Build a complete, usable first version", cw.SYSTEM)
        self.assertIn("insert the parent", cw.SYSTEM)
        self.assertIn("ONE AREA AT A TIME. After the first version", cw.SYSTEM)
        self.assertIn("set_label the root screen to a short name", cw.SYSTEM)


if __name__ == "__main__":
    unittest.main()

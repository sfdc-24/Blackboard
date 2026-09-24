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
        self.assertEqual(batch["batch_id"], "b-s1-3")
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


if __name__ == "__main__":
    unittest.main()

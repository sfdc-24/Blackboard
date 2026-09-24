"""Deterministic worker for the no-provider Stage A and offline tests."""
from __future__ import annotations


ROOT = {
    "id": "screen-home",
    "kind": "screen",
    "label": "Homepage",
    "children": [
        {"id": "nav", "kind": "nav", "label": "Top navigation", "children": [
            {"id": "nav-services", "kind": "text", "label": "Services"},
            {"id": "nav-contact", "kind": "text", "label": "Contact"},
        ]},
        {"id": "hero", "kind": "section", "label": "Hero", "children": [
            {"id": "hero-heading", "kind": "heading", "label": "Your Salesforce, working"},
            {"id": "hero-text", "kind": "text", "label": "Admin help without the backlog."},
            {"id": "hero-cta", "kind": "button", "label": "Get started", "detail": "Not decided yet"},
        ]},
        {"id": "proof", "kind": "section", "label": "Proof", "children": [
            {"id": "proof-quote", "kind": "text", "label": "[Client quote]"},
        ]},
    ],
}

FIRST_QUESTION = {
    "question_id": "q-cta",
    "group": "Screen",
    "scope_path": "Homepage > Hero > Primary action",
    "reason": "This decides what the first screen helps a visitor do.",
    "prompt": "What should the main action be?",
    "options": [
        {"option_id": "describe", "label": "Describe a problem", "consequence": "Opens guided intake", "recommended": True, "recommended_because": "Visitors arrive with a problem; this turns it into a useful brief."},
        {"option_id": "book", "label": "Book a consultation", "consequence": "Opens scheduling"},
        {"option_id": "work", "label": "See the work", "consequence": "Opens case studies"},
    ],
    "status": "open",
    "affected_artifact_ids": ["hero-cta"],
}


def _answered_cta_question(state: dict, trigger: dict) -> bool:
    """Recognize an answered CTA replacement through persisted parent links."""
    question_id = trigger.get("question_id")
    questions = {item.get("question_id"): item for item in state.get("questions", [])}
    question = questions.get(question_id)
    if not question or question.get("status") != "answered":
        return False
    if question.get("selected_option") != trigger.get("option_id"):
        return False

    seen = set()
    while question_id not in seen:
        seen.add(question_id)
        question = questions.get(question_id)
        if not question:
            return False
        if question_id == FIRST_QUESTION["question_id"]:
            return not question.get("parent_question_id")
        question_id = question.get("parent_question_id")
        if not question_id:
            return False
    return False


class SyntheticWorker:
    """Changes the same prototype as the shared scripted-session fixture."""

    def initial_artifact(self) -> dict:
        from copy import deepcopy
        return deepcopy(ROOT)

    def initial_questions(self) -> list[dict]:
        from copy import deepcopy
        return [deepcopy(FIRST_QUESTION)]

    def on_turn(self, state: dict, trigger: dict) -> dict:
        if trigger.get("kind") == "answer" and _answered_cta_question(state, trigger):
            option = trigger.get("option_id")
            choices = {
                "describe": ("Describe a problem", "Opens guided intake"),
                "book": ("Book a consultation", "Opens scheduling"),
                "work": ("See the work", "Opens case studies"),
            }
            if option in choices:
                label, detail = choices[option]
                return {"events": [
                    {"type": "artifact.patch", "payload": {"ops": [
                        {"op": "set_label", "node_id": "hero-cta", "value": label},
                        {"op": "set_detail", "node_id": "hero-cta", "value": detail},
                    ]}},
                    {"type": "confirm", "payload": {"text": f"Using {label}. The hero action now {detail.lower()}.", "artifact_ids": ["hero-cta"]}},
                ], "problems": []}
        return {"events": [], "problems": []}

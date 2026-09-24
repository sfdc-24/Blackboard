"""Optional deterministic facts route; the controller owns auth and idempotency."""
from workers.org_facts import OrgFacts, describe_lead_counts, is_lead_count_question


UNAVAILABLE = (
    "The live Lead count is unavailable right now. I could not verify the count "
    "in the configured Salesforce development org. No estimate has been substituted."
)


class LeadFactsWorker:
    def __init__(self, delegate, expected_org_id: str):
        self.delegate = delegate
        self.expected_org_id = expected_org_id

    def initial_artifact(self):
        return self.delegate.initial_artifact()

    def initial_questions(self):
        return self.delegate.initial_questions()

    def on_turn(self, state: dict, trigger: dict) -> dict:
        if trigger.get("kind") != "utterance" or not is_lead_count_question(trigger.get("text", "")):
            return self.delegate.on_turn(state, trigger)
        try:
            facts = OrgFacts.from_env(expected_org_id=self.expected_org_id)
            text = describe_lead_counts(facts.lead_counts())
        except Exception:
            # Config, auth, wrong-org and malformed/API failures all yield a
            # durable unavailable answer. Never send exception text (which may
            # contain request details) or ask the model to invent a fallback.
            text = UNAVAILABLE
        # A confirm can describe a fact without a patch. Reuse the existing
        # committed event envelope; do not change artifact/version or resolve
        # an unrelated design question. No tokens/credentials enter the event.
        return {"events": [{"type": "confirm", "payload": {
            "text": text, "artifact_ids": [state["artifact"]["id"]],
        }}], "problems": [], "resolves": None}

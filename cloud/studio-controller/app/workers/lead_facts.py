"""Optional deterministic facts route; the controller owns auth and idempotency."""
import threading
import time

from workers.org_facts import describe_lead_counts, is_lead_count_question
from workers.lead_facts_process import fetch_lead_facts


UNAVAILABLE = (
    "The live Lead count is unavailable right now. I could not verify the count "
    "in the configured Salesforce development org. No estimate has been substituted."
)


class LeadFactsWorker:
    def __init__(self, delegate, expected_org_id: str, timeout_seconds: float = 12):
        self.delegate = delegate
        self.expected_org_id = expected_org_id
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._sessions = {}

    def _cancel_event(self, session_id):
        with self._lock:
            now = time.monotonic()
            self._sessions = {key: entry for key, entry in self._sessions.items() if entry[1] > now}
            if session_id not in self._sessions:
                # Covers the full 600-second session and the longest provider
                # deadline. Retain Stop tombstones to close the registration race.
                self._sessions[session_id] = (threading.Event(), now + 620)
            return self._sessions[session_id][0]

    def cancel_session(self, session_id):
        self._cancel_event(session_id).set()

    def initial_artifact(self):
        return self.delegate.initial_artifact()

    def initial_questions(self):
        return self.delegate.initial_questions()

    def on_turn(self, state: dict, trigger: dict) -> dict:
        if trigger.get("kind") != "utterance" or not is_lead_count_question(trigger.get("text", "")):
            return self.delegate.on_turn(state, trigger)
        try:
            facts = fetch_lead_facts(self.expected_org_id, self.timeout_seconds,
                                     self._cancel_event(state.get("session_id", "")))
            text = describe_lead_counts(facts)
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

"""The lead book: public visitors who verified their email on sfdc24.com.

Only used when STUDIO_PUBLIC_VISITORS is on. A visitor signs in with an email
code; the page tells them, before they send the code, that their email and a
recap of the conversation are kept so SFDC24 can follow up. This module keeps
exactly that: the verified email, when it was first and last seen, the
sessions it opened and the host's recaps. Operators read it through
GET /v1/leads; nothing here is ever shown to another visitor.

Records live in the same state store as sessions, one object per visitor
(keyed by the same privacy-preserving subject hash the tokens carry) plus a
bounded index, all written by compare-and-set.
"""
from __future__ import annotations

import copy
import time
from datetime import datetime, timezone

try:
    from scripts.state_store import Conflict
except ImportError:  # Docker copies the shared module beside the app.
    from state_store import Conflict

INDEX = "studio_leads_index"
INDEX_MAX = 500
SESSIONS_MAX = 20
RECAPS_MAX = 10
RECAP_MAX_CHARS = 900


class LeadCapExceeded(RuntimeError):
    """This visitor has used today's sessions."""


class LeadBook:
    def __init__(self, store, clock=time.time, attempts: int = 8):
        self.store = store
        self.clock = clock
        self.attempts = attempts

    @staticmethod
    def _name(subject: str) -> str:
        return "studio_lead_" + subject

    def _update(self, name: str, change) -> dict:
        for _ in range(self.attempts):
            state, token = self.store.load(name)
            current = copy.deepcopy(state) if isinstance(state, dict) and state else {}
            candidate = change(current)
            try:
                self.store.save(name, candidate, token)
                return candidate
            except Conflict:
                continue
        raise RuntimeError("lead book is busy")

    def record_verified(self, subject: str, email: str) -> dict:
        now = int(self.clock())

        def change(lead):
            lead.setdefault("version", 1)
            lead["subject"] = subject
            lead["email"] = email
            lead.setdefault("first_seen", now)
            lead["last_seen"] = now
            lead["verifications"] = int(lead.get("verifications", 0)) + 1
            lead.setdefault("sessions", [])
            lead.setdefault("recaps", [])
            return lead

        lead = self._update(self._name(subject), change)

        def index(state):
            subjects = [s for s in (state.get("subjects") or []) if s != subject]
            return {"version": 1, "subjects": (subjects + [subject])[-INDEX_MAX:]}

        self._update(INDEX, index)
        return lead

    def admit_session(self, subject: str, session_id: str, title: str, per_day: int) -> int:
        """Record a session for this visitor, refusing past `per_day` in one UTC day."""
        now = int(self.clock())
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        admitted = {}

        def change(lead):
            if not lead:
                raise LeadCapExceeded("unknown visitor")
            sessions = list(lead.get("sessions") or [])
            if any(s.get("session_id") == session_id for s in sessions):
                admitted["n"] = sum(1 for s in sessions if s.get("day") == day)
                return lead                     # the same creation replayed: no new admission
            today = sum(1 for s in sessions if s.get("day") == day)
            if today >= per_day:
                raise LeadCapExceeded("visitor sessions for today are used up")
            sessions.append({"session_id": session_id, "day": day, "at": now, "title": title[:120]})
            lead["sessions"] = sessions[-SESSIONS_MAX:]
            lead["session_total"] = int(lead.get("session_total") or len(sessions) - 1) + 1
            lead["last_seen"] = now
            admitted["n"] = today + 1
            return lead

        self._update(self._name(subject), change)
        return admitted["n"]

    def remaining_today(self, subject: str, per_day: int) -> int:
        state, _ = self.store.load(self._name(subject))
        day = datetime.fromtimestamp(int(self.clock()), timezone.utc).strftime("%Y-%m-%d")
        used = sum(1 for s in (state or {}).get("sessions") or [] if s.get("day") == day)
        return max(0, per_day - used)

    def record_recap(self, subject: str, session_id: str, recap: str) -> None:
        now = int(self.clock())

        def change(lead):
            if not lead:
                return lead
            recaps = [r for r in (lead.get("recaps") or []) if r.get("session_id") != session_id]
            recaps.append({"session_id": session_id, "at": now, "recap": recap[:RECAP_MAX_CHARS]})
            lead["recaps"] = recaps[-RECAPS_MAX:]
            lead["last_seen"] = now
            return lead

        self._update(self._name(subject), change)

    def list(self, limit: int = 50) -> list[dict]:
        index, _ = self.store.load(INDEX)
        leads = []
        for subject in reversed((index or {}).get("subjects") or []):
            lead, _ = self.store.load(self._name(subject))
            if not lead:
                continue
            recaps = lead.get("recaps") or []
            leads.append({
                "email": lead.get("email", ""),
                "first_seen": lead.get("first_seen"),
                "last_seen": lead.get("last_seen"),
                "sessions": int(lead.get("session_total") or len(lead.get("sessions") or [])),
                "last_recap": recaps[-1]["recap"] if recaps else "",
            })
        leads.sort(key=lambda lead: lead.get("last_seen") or 0, reverse=True)
        return leads[:limit]


__all__ = ["LeadBook", "LeadCapExceeded"]

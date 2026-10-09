"""The Converspan pilot's feedback record (owner, 2026-10-09).

Invited close contacts - subject matter experts, investors, engineers,
researchers - take a five-minute check call (STUDIO_PILOT) and give their
feedback in it. The session keeps the feedback as it does for anyone: the
rating (POST /rating) and the charter lane's frame for the `pilot` topic
(workers/topics.py: intro, demo, worked, missing, change, rating).

This module builds the one record that leaves the session about it, and it
carries NO personal data: no email, no name, no subject hash, no words the
guest said and no comment. Only the session id, the role, the 1-5 score, the
seconds the call lasted, WHICH frame items were covered, and `seq`.

THE SINK CONTRACT. The record goes to a sink, a callable taking the record.
`pilot_feedback_sink` (app/main.py create_app) is the seam a later change wires
to Redis proj:pilot:feedback. Records are keyed by session_id and numbered by
`seq`, which the controller assigns inside the session's compare-and-set, so
it only ever grows for a session. Publishes can arrive out of order (a recap
and a rating overlap) and a record can be published more than once (a sink
that failed is retried on the next trigger). So a sink MUST keep a record only
when its seq is greater than the seq it already holds for that session_id,
and drop it otherwise - for Redis, one atomic compare-and-set (a Lua script, or
WATCH/MULTI) on the stored seq. Raising means "not kept": the record stays
pending on the session and is published again.
"""
from __future__ import annotations

import threading

from . import governance

ROLE = "pilot"
# A frame item counts as covered once the charter rates it clear (level 2+).
COVERED_LEVEL = 2
RECORD_FIELDS = ("session_id", "role", "rating", "duration_s", "frames_covered", "seq")


def is_pilot(state: dict | None) -> bool:
    return isinstance(state, dict) and state.get("role") == ROLE


def feedback_record(state: dict, now: int) -> dict:
    """The no-PII record for a pilot session at `now`, without its seq."""
    from workers.topics import charter_frame
    rating = (state.get("rating") or {}).get("score")
    if type(rating) is not int or not 1 <= rating <= 5:
        rating = None
    created = int(state.get("created_at") or 0)
    ended = min(int(now), int(state.get("expires_at") or now))
    levels = {}
    charter = state.get("charter") or {}
    for item in charter.get("dimensions") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and type(item.get("level")) is int:
            levels[item["id"]] = item["level"]
    frames = [did for did, _label, _covers in charter_frame(ROLE) if levels.get(did, 0) >= COVERED_LEVEL]
    return {
        "session_id": str(state.get("session_id") or ""),
        "role": ROLE,
        "rating": rating,
        "duration_s": max(0, ended - created) if created else 0,
        "frames_covered": frames,
    }


def supersedes(record: dict, prior: dict) -> bool:
    """A record is worth a new seq when it says something new: another rating,
    other frames covered, or a longer call (the time limit after a recap)."""
    return (record.get("rating") != prior.get("rating")
            or list(record.get("frames_covered") or []) != list(prior.get("frames_covered") or [])
            or int(record.get("duration_s") or 0) > int(prior.get("duration_s") or 0))


class LatestOnlySink:
    """The default sink: keeps the newest seq per session in memory and logs
    one line for each record it keeps - only the record's own fields. An older
    or repeated seq is dropped silently (the contract above)."""

    MAX_SESSIONS = 1000

    def __init__(self):
        self.held: dict = {}
        self._lock = threading.Lock()

    def __call__(self, record: dict) -> bool:
        session_id, seq = record.get("session_id"), record.get("seq")
        if type(seq) is not int:
            return False
        with self._lock:
            if seq <= self.held.get(session_id, 0):
                return False
            self.held[session_id] = seq
            if len(self.held) > self.MAX_SESSIONS:
                for stale in list(self.held)[: self.MAX_SESSIONS // 2]:
                    self.held.pop(stale, None)
        governance.log_event("studio.pilot_feedback", severity="INFO",
                             **{key: record.get(key) for key in RECORD_FIELDS})
        return True


__all__ = ["COVERED_LEVEL", "LatestOnlySink", "RECORD_FIELDS", "ROLE", "feedback_record", "is_pilot",
           "supersedes"]

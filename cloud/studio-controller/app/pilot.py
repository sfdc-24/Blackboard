"""The Converspan pilot's feedback record (owner, 2026-10-09).

Invited close contacts - subject matter experts, investors, engineers,
researchers - take a five-minute check call (STUDIO_PILOT) and give their
feedback in it. The session keeps the feedback as it does for anyone: the
rating (POST /rating) and the charter lane's frame for the `pilot` topic
(workers/topics.py: intro, demo, worked, missing, change, rating).

This module builds the one record that leaves the session about it, and it
carries NO personal data: no email, no name, no subject hash, no words the
guest said and no comment. Only the session id, the role, the 1-5 score, the
seconds the call lasted and WHICH frame items were covered.

The record goes to a sink, a callable taking the record. The default sink
logs one line. `pilot_feedback_sink` (app/main.py create_app) is the seam a
later change wires to Redis proj:pilot:feedback; the record is keyed by
session_id, so a sink must treat a second record for one session as a
replacement (the record is re-sent only when it changed, e.g. a rating
arrived after the recap).
"""
from __future__ import annotations

from . import governance

ROLE = "pilot"
# A frame item counts as covered once the charter rates it clear (level 2+).
COVERED_LEVEL = 2
RECORD_FIELDS = ("session_id", "role", "rating", "duration_s", "frames_covered")


def is_pilot(state: dict | None) -> bool:
    return isinstance(state, dict) and state.get("role") == ROLE


def feedback_record(state: dict, now: int) -> dict:
    """The no-PII record for a pilot session at `now`."""
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


def log_sink(record: dict) -> None:
    """The default sink: one structured line, and only the record's own fields."""
    governance.log_event("studio.pilot_feedback", severity="INFO",
                         **{key: record.get(key) for key in RECORD_FIELDS})


__all__ = ["COVERED_LEVEL", "RECORD_FIELDS", "ROLE", "feedback_record", "is_pilot", "log_sink"]

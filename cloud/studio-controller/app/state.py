"""Durable Studio state operations built on Blackboard's CAS store."""
from __future__ import annotations

import copy
import datetime as dt
import time
from dataclasses import dataclass

try:
    from scripts.state_store import Conflict
except ImportError:  # Docker copies the shared module beside the app.
    from state_store import Conflict


class StateConflict(RuntimeError):
    pass


class SessionNotFound(KeyError):
    pass


@dataclass
class SessionRecord:
    state: dict
    token: object


def session_name(session_id: str) -> str:
    return "studio_session_" + session_id


class StudioRepository:
    def __init__(self, store, clock=time.time, attempts: int = 8):
        self.store = store
        self.clock = clock
        self.attempts = attempts

    def load(self, session_id: str) -> SessionRecord:
        state, token = self.store.load(session_name(session_id))
        if not state:
            raise SessionNotFound(session_id)
        return SessionRecord(state, token)

    def create(self, state: dict) -> None:
        try:
            self.store.save(session_name(state["session_id"]), state, None)
        except Conflict as exc:
            raise StateConflict("session id collision") from exc

    def save(self, session_id: str, state: dict, token) -> object:
        try:
            return self.store.save(session_name(session_id), state, token)
        except Conflict as exc:
            raise StateConflict("session changed; re-read before deciding") from exc

    def admit(self, limit: int, reservation_id: str | None = None) -> int:
        """Reserve one UTC-day slot with CAS; never over-admit on contention."""
        day = dt.datetime.fromtimestamp(self.clock(), tz=dt.timezone.utc).strftime("%Y%m%d")
        name = "studio_admission_" + day
        for _ in range(self.attempts):
            state, token = self.store.load(name)
            count = int(state.get("count") or 0)
            reservations = dict(state.get("reservations") or {})
            if reservation_id and reservation_id in reservations:
                return int(reservations[reservation_id])
            if count >= limit:
                raise StateConflict("daily session capacity reached")
            number = count + 1
            if reservation_id:
                reservations[reservation_id] = number
            candidate = {
                "day": day,
                "count": number,
                "reservations": reservations,
                "updated_at": int(self.clock()),
            }
            try:
                self.store.save(name, candidate, token)
                return number
            except Conflict:
                continue
        raise StateConflict("admission is busy; try again")

    def register_voice(self, session_id: str, ends_at: int) -> None:
        """Put one active/opening call in the bounded sweep index."""
        name = "studio_voice_index"
        for _ in range(self.attempts):
            state, token = self.store.load(name)
            sessions = dict(state.get("sessions") or {})
            sessions[session_id] = int(ends_at)
            candidate = {"sessions": sessions, "updated_at": int(self.clock())}
            try:
                self.store.save(name, candidate, token)
                return
            except Conflict:
                continue
        raise StateConflict("voice index is busy; try again")

    def unregister_voice(self, session_id: str) -> None:
        name = "studio_voice_index"
        for _ in range(self.attempts):
            state, token = self.store.load(name)
            sessions = dict(state.get("sessions") or {})
            if session_id not in sessions:
                return
            sessions.pop(session_id, None)
            candidate = {"sessions": sessions, "updated_at": int(self.clock())}
            try:
                self.store.save(name, candidate, token)
                return
            except Conflict:
                continue
        raise StateConflict("voice index is busy; try again")

    def due_voice_sessions(self, now: int | None = None) -> list[str]:
        state, _ = self.store.load("studio_voice_index")
        current = int(self.clock() if now is None else now)
        return [
            session_id for session_id, ends_at in (state.get("sessions") or {}).items()
            if int(ends_at) <= current
        ]


def clone(value):
    return copy.deepcopy(value)

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


class VoiceCapacityExceeded(RuntimeError):
    pass


class SessionNotFound(KeyError):
    pass


@dataclass
class SessionRecord:
    state: dict
    token: object


@dataclass(frozen=True)
class VoiceOpenReservation:
    day: str
    number: int


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

    def utc_day(self) -> str:
        return dt.datetime.fromtimestamp(
            self.clock(), tz=dt.timezone.utc
        ).strftime("%Y%m%d")

    def reserve_voice_open(
            self, limit: int, reservation_id: str) -> VoiceOpenReservation:
        """Durably reserve one provider open attempt for the current UTC day.

        A reservation is never refunded: once this returns, the caller is
        authorized to make at most one outbound provider POST for that ID.
        Persisting before network contact makes crashes fail toward a consumed
        slot instead of allowing an unbounded retry.
        """
        day = self.utc_day()
        name = "studio_voice_open_" + day
        for _ in range(self.attempts):
            state, token = self.store.load(name)
            count = int(state.get("count") or 0)
            reservations = dict(state.get("reservations") or {})
            if reservation_id in reservations:
                return VoiceOpenReservation(day, int(reservations[reservation_id]))
            if count >= limit:
                raise VoiceCapacityExceeded("daily voice open capacity reached")
            number = count + 1
            reservations[reservation_id] = number
            candidate = {
                "day": day,
                "count": number,
                "reservations": reservations,
                "updated_at": int(self.clock()),
            }
            try:
                self.store.save(name, candidate, token)
                return VoiceOpenReservation(day, number)
            except Conflict:
                continue
        raise StateConflict("voice admission is busy; try again")

    def register_voice(
            self, session_id: str, voice_id: str, ends_at: int, *,
            adopt_legacy: bool = False) -> None:
        """Put one active/opening call in the bounded sweep index."""
        name = "studio_voice_index"
        for _ in range(self.attempts):
            state, token = self.store.load(name)
            sessions = dict(state.get("sessions") or {})
            existing = sessions.get(session_id)
            if isinstance(existing, dict):
                owner = str(existing.get("voice_id") or "")
                if owner and owner != voice_id:
                    raise StateConflict("voice index is owned by another call")
            elif existing is not None and not adopt_legacy:
                raise StateConflict("legacy voice index ownership is unknown")
            sessions[session_id] = {
                "voice_id": voice_id,
                "ends_at": int(ends_at),
            }
            candidate = {"sessions": sessions, "updated_at": int(self.clock())}
            try:
                self.store.save(name, candidate, token)
                return
            except Conflict:
                continue
        raise StateConflict("voice index is busy; try again")

    def unregister_voice(
            self, session_id: str, voice_id: str | None = None, *,
            force: bool = False) -> bool:
        """Remove only the index entry owned by the expected voice.

        Legacy entries stored only an integer expiry and are safe to remove.
        An owned entry is retained on an owner mismatch so late cleanup cannot
        erase a newer call's sweep coverage.
        """
        name = "studio_voice_index"
        for _ in range(self.attempts):
            state, token = self.store.load(name)
            sessions = dict(state.get("sessions") or {})
            if session_id not in sessions:
                return True
            entry = sessions[session_id]
            if isinstance(entry, dict):
                owner = str(entry.get("voice_id") or "")
                if not force and (
                    not owner or not voice_id or owner != voice_id
                ):
                    return False
            sessions.pop(session_id, None)
            candidate = {"sessions": sessions, "updated_at": int(self.clock())}
            try:
                self.store.save(name, candidate, token)
                return True
            except Conflict:
                continue
        raise StateConflict("voice index is busy; try again")

    def due_voice_sessions(self, now: int | None = None) -> list[str]:
        state, _ = self.store.load("studio_voice_index")
        current = int(self.clock() if now is None else now)
        due = []
        for session_id, entry in (state.get("sessions") or {}).items():
            ends_at = entry.get("ends_at") if isinstance(entry, dict) else entry
            if int(ends_at) <= current:
                due.append(session_id)
        return due


def clone(value):
    return copy.deepcopy(value)

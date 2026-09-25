"""Durable guards for client (workspace) sessions (Codex Gate 1 B3/B4 on
38bc713): the paid-lane spend counters and spacing, the single-flight leases,
and the page-load leases with their ceiling live in compare-and-set state, so
they hold across instances, revisions and a rollout - never in one process.

Each call is one compare-and-set on one small record. A lease has an owner,
an expiry and a fence (a number that only grows): only its owner, holding its
fence, releases it, and an expired lease is simply no longer counted, so a
holder that outlived its lease can never release a newer one. Anything that
cannot be read or written fails closed: the caller refuses the request.
"""
from __future__ import annotations

import copy
import hashlib
import time


class GuardUnavailable(RuntimeError):
    """The guard state could not be read or written: refuse, never let through."""


CEILING_RECORD = "studio_guard_fetch_ceiling"


def session_record(session_id: str) -> str:
    return "studio_guard_" + str(session_id)


def tenant_record(tenant: str) -> str:
    return "studio_guard_tenant_" + hashlib.sha256(str(tenant).encode("utf-8")).hexdigest()[:40]


class DurableGuards:
    def __init__(self, store, conflict, clock=time.time, attempts: int = 8):
        self.store = store
        self.conflict = conflict
        self.clock = clock
        self.attempts = attempts

    def _change(self, name: str, change):
        """Read, decide, and write back only if nothing changed in between."""
        for _ in range(self.attempts):
            try:
                raw, token = self.store.load(name)
            except Exception as exc:
                raise GuardUnavailable("guard state could not be read") from exc
            state = copy.deepcopy(raw) if isinstance(raw, dict) else {}
            state.setdefault("version", 1)
            result, write = change(state, float(self.clock()))
            if not write:
                return result
            try:
                self.store.save(name, state, token)
                return result
            except self.conflict:
                continue
            except Exception as exc:
                raise GuardUnavailable("guard state could not be written") from exc
        raise GuardUnavailable("guard state is busy")

    def take(self, name: str, lane: str, cap: int, spacing: float = 0.0) -> str:
        """Spend one of the lane's uses: "" when it was taken, else "cap" or "spacing"."""
        def change(state, now):
            counts = state.setdefault("counts", {})
            last = state.setdefault("last", {})
            used = int(counts.get(lane) or 0)
            if used >= int(cap):
                return "cap", False
            if spacing and lane in last and now - float(last[lane]) < float(spacing):
                return "spacing", False
            counts[lane] = used + 1
            last[lane] = now
            return "", True
        return self._change(name, change)

    def acquire(self, name: str, lane: str, owner: str, ttl: float, ceiling: int = 1):
        """A lease on the lane: its fence, or None when `ceiling` live leases are held."""
        def change(state, now):
            leases = {o: dict(l) for o, l in ((state.get("leases") or {}).get(lane) or {}).items()
                      if isinstance(l, dict) and float(l.get("until") or 0) > now}
            if len(leases) >= int(ceiling):
                return None, False
            fence = int(state.get("fence") or 0) + 1
            state["fence"] = fence
            leases[owner] = {"until": now + float(ttl), "fence": fence}
            state.setdefault("leases", {})[lane] = leases
            return fence, True
        return self._change(name, change)

    def release(self, name: str, lane: str, owner: str, fence) -> None:
        """Give the lease back - only with its owner and fence. A release that
        cannot be written is left to the lease's expiry."""
        def change(state, now):
            leases = (state.get("leases") or {}).get(lane) or {}
            held = leases.get(owner)
            if not isinstance(held, dict) or int(held.get("fence") or 0) != int(fence or 0):
                return None, False
            del leases[owner]
            return None, True
        try:
            self._change(name, change)
        except GuardUnavailable:
            pass


__all__ = ["DurableGuards", "GuardUnavailable", "CEILING_RECORD", "session_record", "tenant_record"]

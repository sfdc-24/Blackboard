"""Durable guards for client (workspace) sessions (Codex Gate 1 B3/B4 on
38bc713): the paid-lane spend counters and spacing, the single-flight leases,
and the page-load leases with their ceiling live in compare-and-set state, so
they hold across instances, revisions and a rollout - never in one process.

Each call is one compare-and-set on one small record. A lease has an owner,
an expiry and a fence (a number that only grows): only its owner, holding its
fence, releases it, and an expired lease is simply no longer counted, so a
holder that outlived its lease can never release a newer one. Anything that
cannot be read or written fails closed: the caller refuses the request.

A release that cannot be written is owed, not forgotten (Codex Gate 1 on
ecee267): it is settled before anything new is taken on that record - so a
retry after the store comes back never meets a ghost lease - and by a
background retry for a caller that never comes back.
"""
from __future__ import annotations

import copy
import hashlib
import threading
import time


class GuardUnavailable(RuntimeError):
    """The guard state could not be read or written: refuse, never let through."""


CEILING_RECORD = "studio_guard_fetch_ceiling"


def session_record(session_id: str) -> str:
    return "studio_guard_" + str(session_id)


def tenant_record(tenant: str) -> str:
    return "studio_guard_tenant_" + hashlib.sha256(str(tenant).encode("utf-8")).hexdigest()[:40]


class DurableGuards:
    def __init__(self, store, conflict, clock=time.time, attempts: int = 8, retry_seconds: float = 1.0):
        self.store = store
        self.conflict = conflict
        self.clock = clock
        self.attempts = attempts
        self.retry_seconds = retry_seconds
        self._owed: dict = {}                # (record, lane, owner) -> fence: releases not written yet
        self._owed_lock = threading.Lock()
        self._retrying = False

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
        self._settle(name)

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

    def lease_and_spend(self, name: str, lane: str, owner: str, ttl: float, cap: int):
        """A single-flight lease on the lane AND one of its `cap` uses, in ONE
        compare-and-set (Codex Gate 1 on ecee267): (fence, "") when both were
        taken, (None, "busy") while another lease is live, (None, "cap") when
        the uses are spent. Nothing is written unless both hold, so a store
        that fails part way can never leave a lease without its use or a use
        without its lease."""
        self._settle(name)

        def change(state, now):
            leases = {o: dict(l) for o, l in ((state.get("leases") or {}).get(lane) or {}).items()
                      if isinstance(l, dict) and float(l.get("until") or 0) > now}
            if leases:
                return (None, "busy"), False
            counts = state.setdefault("counts", {})
            used = int(counts.get(lane) or 0)
            if used >= int(cap):
                return (None, "cap"), False
            fence = int(state.get("fence") or 0) + 1
            state["fence"] = fence
            leases[owner] = {"until": now + float(ttl), "fence": fence}
            state.setdefault("leases", {})[lane] = leases
            counts[lane] = used + 1
            state.setdefault("last", {})[lane] = now
            return (fence, ""), True
        return self._change(name, change)

    def renew(self, name: str, lane: str, owner: str, fence, ttl: float) -> bool:
        """Extend the lease while its holder's work is still alive (Codex Gate 1
        on 59ed871) - only with its owner and fence, and only while it has not
        expired (Codex Gate 1 on ecee267): an expired lease is no longer
        counted by anyone, so another holder may already have taken its place,
        and renewing it would bring its owner back beside them. False when the
        lease is gone or has lapsed; its holder then stops."""
        def change(state, now):
            leases = (state.get("leases") or {}).get(lane) or {}
            held = leases.get(owner)
            if not isinstance(held, dict) or int(held.get("fence") or 0) != int(fence or 0):
                return False, False
            if float(held.get("until") or 0) <= now:
                return False, False
            held["until"] = now + float(ttl)
            return True, True
        return self._change(name, change)

    @staticmethod
    def _release_change(lane: str, owner: str, fence):
        def change(state, now):
            leases = (state.get("leases") or {}).get(lane) or {}
            held = leases.get(owner)
            if not isinstance(held, dict) or int(held.get("fence") or 0) != int(fence or 0):
                return None, False
            del leases[owner]
            return None, True
        return change

    def release(self, name: str, lane: str, owner: str, fence) -> None:
        """Give the lease back - only with its owner and fence. A release that
        cannot be written now is owed: settled before this record's next
        acquire, and retried in the background until it is written."""
        key = (name, lane, owner)
        try:
            self._change(name, self._release_change(lane, owner, fence))
        except GuardUnavailable:
            with self._owed_lock:
                self._owed[key] = fence
                start = not self._retrying
                self._retrying = True
            if start:
                threading.Thread(target=self._retry_owed, name="studio-guard-release", daemon=True).start()
            return
        with self._owed_lock:
            if self._owed.get(key) == fence:
                del self._owed[key]

    def _settle(self, name: str) -> None:
        """Write every release still owed on this record, first. A store still
        down raises GuardUnavailable: the caller refuses, as it would anyway."""
        with self._owed_lock:
            owed = [(key, fence) for key, fence in self._owed.items() if key[0] == name]
        for key, fence in owed:
            self._change(name, self._release_change(key[1], key[2], fence))
            with self._owed_lock:
                if self._owed.get(key) == fence:
                    del self._owed[key]

    def settle_owed(self) -> int:
        """Try every owed release once; how many are still owed."""
        with self._owed_lock:
            names = sorted({key[0] for key in self._owed})
        for name in names:
            try:
                self._settle(name)
            except GuardUnavailable:
                pass
        return self.owed()

    def owed(self) -> int:
        with self._owed_lock:
            return len(self._owed)

    def _retry_owed(self) -> None:
        while True:
            time.sleep(self.retry_seconds)
            self.settle_owed()
            with self._owed_lock:
                if not self._owed:
                    self._retrying = False
                    return


__all__ = ["DurableGuards", "GuardUnavailable", "CEILING_RECORD", "session_record", "tenant_record"]

"""The saved design of each client project: {tenant, project} -> revision N.

A client opens a project (app/project_page.py fetches its live page the first
time) and changes it by talking. Every change the builder applies in that
session is saved here as the next revision, by compare-and-set against the
revision the session started from or last saved, so a stale session can never
overwrite a newer one: its save is refused and the page says so. Opening the
project again continues from the saved revision, exactly. The page itself is
never touched - this is SFDC24's working copy, not the client's live site.

Records hold the typed, text-only tree the controller already built; no
addresses, no words said, no page HTML.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time

try:
    from scripts.state_store import Conflict
except ImportError:  # Docker copies the shared module beside the app.
    from state_store import Conflict

PREFIX = "studio_ws_"
HISTORY_MAX = 50


class WorkspaceConflict(RuntimeError):
    """The project was saved by another session since this one's base revision."""

    def __init__(self, stored_revision: int):
        super().__init__("the project changed elsewhere")
        self.stored_revision = stored_revision


def digest(artifact: dict) -> str:
    """A stable fingerprint of a tree, for read-back: the saved revision and the
    session's artifact are the same design exactly when their digests match."""
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkspaceStore:
    def __init__(self, store, clock=time.time, attempts: int = 8):
        self.store = store
        self.clock = clock
        self.attempts = attempts

    @staticmethod
    def name(tenant: str, project: str) -> str:
        if not tenant or not project:
            raise ValueError("a workspace is a tenant and a project")
        return PREFIX + hashlib.sha256(("%s\x00%s" % (tenant, project)).encode("utf-8")).hexdigest()[:40]

    def load(self, tenant: str, project: str) -> dict | None:
        state, _ = self.store.load(self.name(tenant, project))
        if not isinstance(state, dict) or state.get("tenant") != tenant or state.get("project") != project:
            return None
        return copy.deepcopy(state)

    def summary(self, tenant: str, project: str) -> dict:
        record = self.load(tenant, project)
        if not record:
            return {"revision": 0, "updated_at": None}
        return {"revision": int(record.get("revision") or 0), "updated_at": record.get("updated_at")}

    def save(self, tenant: str, project: str, artifact: dict, session_id: str, base_revision: int,
             op_ids=()) -> int:
        """Write base_revision + 1, only if the stored revision is still base_revision."""
        name = self.name(tenant, project)
        for _ in range(self.attempts):
            state, token = self.store.load(name)
            stored = int(state.get("revision") or 0) if isinstance(state, dict) and state else 0
            if stored != int(base_revision):
                raise WorkspaceConflict(stored)
            revision = stored + 1
            now = int(self.clock())
            history = list((state or {}).get("history") or []) if isinstance(state, dict) else []
            history.append({"revision": revision, "session_id": session_id, "at": now,
                            "op_ids": [str(o) for o in op_ids][:20]})
            record = {"version": 1, "tenant": tenant, "project": project, "revision": revision,
                      "artifact": copy.deepcopy(artifact), "digest": digest(artifact), "updated_at": now,
                      "session_id": session_id, "history": history[-HISTORY_MAX:]}
            try:
                self.store.save(name, record, token)
                return revision
            except Conflict:
                continue
        raise WorkspaceConflict(-1)


__all__ = ["WorkspaceStore", "WorkspaceConflict", "digest"]

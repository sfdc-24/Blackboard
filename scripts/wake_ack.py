#!/usr/bin/env python3
"""ACK only a real consumer's validated acceptance receipt, never a passive ring.

The caller is a trusted integration adapter: it must supply accepted only after the
consumer has durably accepted this task. This module validates correlation, not
identity credentials; board sender identity must be enforced by the upstream gateway.

A local durable file records every attempted ACK before append. Interprocess mkdir
locking, atomic replacement and fsync fail closed on unreadable state, failed writes
or an abandoned lock. State is not pruned automatically. An uncertain append never
retries. Recovery/reconciliation must inspect existing receipts before any action.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

WAKE_PREFIX = "CALLBRIDGE-WAKE-"     # call_bridge.WAKE_PREFIX
RERING_SUFFIX = "-R2"                # call_bridge.RERING_SUFFIX
RINGER = "bus-reconciler"            # the worker's tag, the only one that rings (call_bridge.wake_row)
ASK = re.compile(r"^call-[0-9a-f]{12}$")
KEEP = 500                           # asks remembered in the file; older ones fall off (the bridge's TTL is 2 days)
# Board columns (bus_request.ROW_ID, TS, SOURCE, TARGET, ACTION, PAYLOAD), kept here so this module imports nothing.
ROW_ID, TS, SOURCE, TARGET, ACTION, PAYLOAD = range(6)


def _field(payload: str, name: str) -> str:
    found = re.search(r"(?:^|\|)\s*%s=([^|]*)" % re.escape(name), payload or "", re.I)
    return found.group(1).strip() if found else ""


def _cell(row, index) -> str:
    return str(row[index] if isinstance(row, (list, tuple)) and len(row) > index and row[index] is not None else "")


def wake_ask(row, names) -> str:
    """The ask a call bridge wake row rings `names` for, or "" when the row is not such a ring."""
    payload = _cell(row, PAYLOAD)
    if _cell(row, SOURCE).strip().lower() != RINGER:
        return ""
    if _field(payload, "phase").upper() != "ASK":
        return ""
    ring = _field(payload, "id") or _cell(row, ROW_ID).strip()
    if not ring.startswith(WAKE_PREFIX):
        return ""
    ask = ring[len(WAKE_PREFIX):]
    if ask.endswith(RERING_SUFFIX):
        ask = ask[:-len(RERING_SUFFIX)]
    if not ASK.match(ask):
        return ""
    if _field(payload, "ask") and _field(payload, "ask") != ask:
        return ""                                       # a ring whose id and ask= disagree is not trusted
    to = {n.strip().lower() for n in re.split(r"[,;]", _field(payload, "to") + ";" + _cell(row, TARGET)) if n.strip()}
    if not to & {n.lower() for n in names}:
        return ""
    return ask


def stamp(moment) -> str:
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ack_spec(ask: str, tag: str, now, ring: str = "", receipt: str = "") -> dict:
    """The ACK row spec for scripts/append.py, signed by the runner's own tag (signature.check: from= is Source_Tag,
    direct). answers= names the first ring, which call_bridge.answered_asks reads for either ring."""
    row_id = "%s-WAKE-ACK-%s" % (tag.upper(), ask)
    text = "Consumer acceptance recorded at %s for %s; execution completion is not yet verified." % (stamp(now), ask)
    return {"row_id": row_id, "source_tag": tag, "target_surface": RINGER, "action_type": "ACK",
            "category": "OPEN", "project_tag": "Blackboard", "gist": "Wake taken for %s" % ask,
            "payload": "BCB|v=1|id=%s|phase=ACK|from=%s|via=direct|to=%s|answers=%s|ask=%s|ring=%s|event=worker.accepted|task_id=%s|receipt_id=%s|text=%s" % (
                row_id, tag, RINGER, WAKE_PREFIX + ask, ask, ring or WAKE_PREFIX + ask, ask, receipt, text)}


def append_spec(spec: dict) -> None:
    """One row through scripts/append.py (its guards and its read-back), never retried. Raises when the read-back
    did not find the row exactly once: the row may or may not be on the board, and nobody re-runs it blind."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import append                                                        # noqa: PLC0415
    handle, path = tempfile.mkstemp(suffix=".json", prefix="ack-")
    os.close(handle)
    argv = sys.argv
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            json.dump(spec, fh)
        sys.argv = ["append.py", path]
        with contextlib.redirect_stdout(sys.stderr):   # the doorbell's stdout is the agent's rows, nothing else
            append.main()
    finally:
        sys.argv = argv
        try:
            os.unlink(path)
        except OSError:
            pass


class Acker:
    """Durably reserve one ACK after validated consumer acceptance, never on polling."""

    def __init__(self, tag, names, post, path=None, clock=None, log=None):
        self.tag, self.names, self.post = tag, set(names), post
        self.path = Path(path) if path else None
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.log = log or (lambda text: print(text, file=sys.stderr))
        self.lock = threading.Lock()

    def _reserve(self, ask) -> bool:
        if self.path is None:
            raise ValueError("durable ACK state path required")
        lock_path = self.path.with_name(self.path.name + ".lock")
        # Atomic across processes on the same local filesystem. An abandoned lock
        # deliberately blocks automatic recovery instead of risking duplicate ACKs.
        lock_path.mkdir()
        temp = None
        try:
            if self.path.exists():
                held = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(held, list) or any(not isinstance(a, str) or not ASK.fullmatch(a) for a in held):
                    raise ValueError("invalid ACK state")
            else:
                held = []
            if ask in held:
                return False
            fd, temp = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(held + [ask], fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, self.path)
            temp = None
            # On POSIX persist the directory entry. Windows atomic replacement is
            # retained, but reboot durability needs platform-specific acceptance.
            if os.name == "posix":
                dfd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            return True
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
            lock_path.rmdir()

    def __call__(self, row, accepted=None):
        ask = wake_ask(row, self.names)
        if not ask or not isinstance(accepted, dict):
            return None
        receipt = accepted.get("receipt_id")
        if (accepted.get("task_id") != ask or accepted.get("agent") != self.tag
                or accepted.get("state") != "accepted" or not isinstance(receipt, str)
                or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", receipt)):
            return None
        try:
            with self.lock:
                if not self._reserve(ask):
                    return None
        except (OSError, ValueError, TypeError) as error:
            self.log("WAKE ACK state unavailable for %s (%s); no append attempted" % (ask, type(error).__name__))
            return None
        ring = _field(_cell(row, PAYLOAD), "id") or _cell(row, ROW_ID)
        spec = ack_spec(ask, self.tag, self.clock(), ring, receipt)
        try:
            self.post(spec)
        except (Exception, SystemExit) as error:
            self.log("WAKE ACK %s for %s NOT confirmed (%s); quarantined, not retried" % (
                spec["row_id"], ask, type(error).__name__))
            return None
        self.log("WAKE ACK %s posted for %s" % (spec["row_id"], ask))
        return spec

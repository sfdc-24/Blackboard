#!/usr/bin/env python3
"""A runner TAKING the call bridge's wake: one ACK board row, posted the moment the ring is seen, before the work.

WHY (Aya's source hold on d2d1618, "a ring is not a wake"; call_bridge.py THE WAKE, CONFIRMED). The bridge rings an
agent with a board row CALLBRIDGE-WAKE-<ask> and then waits ACK_SECONDS (90 s) for the agent to take the claim: a row
from the agent's own tag, phase=ACK, answers=CALLBRIDGE-WAKE-<ask>. No take: one more ring (-R2), then the card says
"agent did not wake". Until 2026-10-29 no runner posted that row, so every call ask whose agent was a little slow
to put its own card up was marked as not woken. This is the row, for any runner; fleet_doorbell.py wires it for Grok.

    acker = wake_ack.Acker("grok", {"grok", "grok-bot"}, post=wake_ack.append_spec)
    acker(row)        # a board row; the ACK spec if this call posted one, else None

WHAT IS A WAKE FOR THIS RUNNER. The worker's row (Source_Tag bus-reconciler), Row_ID / id= CALLBRIDGE-WAKE-<ask>
(or its second ring ...-R2), phase=ASK, addressed to one of the runner's own names (its to= or Target_Surface).
A forged ring from another tag, a ring for another agent, or anything else: no ACK.

AT MOST ONE ACK PER ASK, NEVER RETRIED. The ask is marked taken (in memory and in a small file beside the
doorbell's bookmark, so a restart remembers) BEFORE the append is tried, exactly as the bridge takes its rung
marker first: append.py's rule that an append is never retried (a row can land and still raise). The second ring of
an ask already acked, a replayed row, or a restart re-reading the same row: no second ACK. An append that fails is
logged and left for a person; the agent's own card for the ask still counts as the wake.

WHAT THE ROW SAYS. Who took it and when, and the ask id. Never a word of the owner's (O2; Aya's privacy hold on
922e8da): his words are in fleet:inbox:<agent>, not on the public board. No new secret, no IAM: the runner's own
board append path (scripts/append.py and its read-back).
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


def ack_spec(ask: str, tag: str, now, ring: str = "") -> dict:
    """The ACK row spec for scripts/append.py, signed by the runner's own tag (signature.check: from= is Source_Tag,
    direct). answers= names the first ring, which call_bridge.answered_asks reads for either ring."""
    row_id = "%s-WAKE-ACK-%s" % (tag.upper(), ask)
    text = "Claim taken at %s; working on it now. My own card for %s follows in the room." % (stamp(now), ask)
    return {"row_id": row_id, "source_tag": tag, "target_surface": RINGER, "action_type": "ACK",
            "category": "OPEN", "project_tag": "Blackboard", "gist": "Wake taken for %s" % ask,
            "payload": "BCB|v=1|id=%s|phase=ACK|from=%s|via=direct|to=%s|answers=%s|ask=%s|ring=%s|text=%s" % (
                row_id, tag, RINGER, WAKE_PREFIX + ask, ask, ring or WAKE_PREFIX + ask, text)}


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
    """Posts one ACK per ask for the wake rows addressed to this runner. Callable on a board row."""

    def __init__(self, tag, names, post, path=None, clock=None, log=None):
        self.tag, self.names, self.post = tag, set(names), post
        self.path = Path(path) if path else None
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.log = log or (lambda text: print(text, file=sys.stderr))
        self.lock = threading.Lock()
        self.taken = self._load()

    def _load(self) -> list:
        if not self.path:
            return []
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
            return [a for a in held if isinstance(a, str)][-KEEP:] if isinstance(held, list) else []
        except (OSError, ValueError):
            return []

    def _mark(self, ask) -> None:
        self.taken = (self.taken + [ask])[-KEEP:]
        if self.path:
            try:
                self.path.write_text(json.dumps(self.taken), encoding="utf-8")
            except OSError as error:
                self.log("WAKE ACK: could not keep %s in %s (%s); this run still will not ack it twice" % (
                    ask, self.path, error))

    def __call__(self, row):
        ask = wake_ask(row, self.names)
        if not ask:
            return None
        with self.lock:
            if ask in self.taken:
                return None                                  # acked before: the second ring, a replay, a restart
            self._mark(ask)                                  # taken first: never twice, never retried
        ring = _field(_cell(row, PAYLOAD), "id") or _cell(row, ROW_ID)
        spec = ack_spec(ask, self.tag, self.clock(), ring)
        try:
            self.post(spec)
        except (Exception, SystemExit) as error:                         # noqa: BLE001
            self.log("WAKE ACK %s for %s NOT confirmed (%s); not retried, check the board by hand" % (
                spec["row_id"], ask, type(error).__name__))
            return None
        self.log("WAKE ACK %s posted for %s" % (spec["row_id"], ask))
        return spec

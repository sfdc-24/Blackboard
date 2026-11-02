"""Explicit, bounded local migration; old claims are evidence, never new task authority.

Usage: call_ask.py migrate [--legacy PATH ...], or python call_migrate.py [--legacy PATH ...].
The default inputs are cli_call_state.json (v1 only), cli_call_consumer.pending,
cli_call_consumer.after and cli_call_accepted.txt next to the configured state file.
Every existing input is backed up byte-for-byte before the atomic state replacement.

An accepted/writing/published/done/unknown local task is LEGACY_OPEN, including the old
accepted.txt bug that labelled every accepted ask "done". A pending cue and all its known entry
IDs survive. Old board DONE/ACK rows may be retained as historical claims but are not trusted
receipts. No old state or board row proves completion, cancellation, expiry or public acceptance.
The new acceptance path obtains an authenticated per-ask source record separately. Migration
performs NO network access and does not copy private text into its output or printed notes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time

import call_state as cs

MAX_ENTRIES = 500
MAX_FILES = 16
MAX_FILE_BYTES = 1024 * 1024
WINDOW_MS = 24 * 3600 * 1000
TAG = "claude-code-cli"


def _stop(reason):
    raise SystemExit("STOP: migration " + reason + "; original files kept")


def window_start(legacy_cursor, now_ms):
    """Conservative offline-replay floor. The legacy cursor is never proof of a complete scan."""
    return "0-0"


def _ask(value):
    if not isinstance(value, str) or not cs.ASK_ID.fullmatch(value):
        _stop("contains an invalid task id")
    return value


def _entry(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not cs.STREAM_ID.fullmatch(value):
        _stop("contains an invalid stream id")
    if any(n > 2 ** 64 - 1 for n in cs.sid(value)):
        _stop("contains an out-of-range stream id")
    return value


def _keep(state, ask, *, pending=False, entry="", origin=None, old_state="", historical_done=False):
    """Merge only non-authoritative metadata; retain all entry linkage and conservative open state."""
    ask, entry = _ask(ask), _entry(entry)
    tasks = state["tasks"]
    if ask not in tasks:
        if len(tasks) >= MAX_ENTRIES:
            _stop("exceeds the 500-task bound")
        tasks[ask] = {"state": "PENDING" if pending else "LEGACY_OPEN", "rev": 1, "ops": {},
                      "source": "legacy-local", "historical_only": True, "legacy_entries": []}
    task = tasks[ask]
    if not pending:
        task["state"] = "LEGACY_OPEN"
    if entry:
        entries = task["legacy_entries"]
        if entry not in entries:
            entries.append(entry)
            entries.sort(key=cs.sid)
        task["entry"] = entries[0]
    if origin is not None:
        if isinstance(origin, bool) or not isinstance(origin, (int, float)) or not math.isfinite(origin):
            _stop("contains an invalid origin timestamp")
        task["origin"] = min(float(origin), task.get("origin", float(origin)))
    if isinstance(old_state, str) and old_state:
        # Closed grammar rather than copying arbitrary strings that might contain private prose.
        known = {"pending", "accepted", "writing", "stored", "published", "done", "cancelled",
                 "cancelled_after_write", "expired", "failed", "legacy_open", "historical_closed"}
        label = old_state.lower() if old_state.lower() in known else "unknown"
        states = task.setdefault("legacy_states", [])
        if label not in states:
            states.append(label)
    if historical_done:
        task["historical_done_claim"] = True
    return task


def _consume_json(state, data, cursors, counted):
    if not isinstance(data, dict):
        _stop("JSON input is not an object")
    if data.get("v") == 2:
        _stop("refuses to overwrite or merge a current v2 state")
    if "tasks" in data:
        if data.get("v", 1) != 1 or not isinstance(data["tasks"], dict):
            _stop("state input is not a supported v1 state")
        for ask, old in data["tasks"].items():
            counted[0] += 1
            if counted[0] > MAX_ENTRIES:
                _stop("exceeds the 500-record bound")
            if not isinstance(old, dict):
                _stop("task evidence is not an object")
            label = old.get("state", "unknown")
            _keep(state, ask, pending=isinstance(label, str) and label.lower() == "pending",
                  entry=old.get("entry", ""), origin=old.get("origin"), old_state=label,
                  historical_done=isinstance(label, str) and label.lower() == "done")
        pending = data.get("pending")
        if pending is not None:
            _consume_pending(state, pending, counted)
        for name in ("cursor", "seen"):
            if data.get(name):
                cursors.add(_entry(data[name]))
    elif "ask" in data:
        _consume_pending(state, data, counted)
    else:
        _stop("JSON input has no supported state or pending-cue shape")


def _consume_pending(state, data, counted):
    if not isinstance(data, dict) or "ask" not in data:
        _stop("pending cue is not an object with an ask id")
    counted[0] += 1
    if counted[0] > MAX_ENTRIES:
        _stop("exceeds the 500-record bound")
    _keep(state, data["ask"], pending=True, entry=data.get("entry", ""), old_state="pending")


def _read(path):
    with open(path, "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        _stop("input exceeds the 1 MiB file bound")
    try:
        data.decode("utf-8")
    except UnicodeError:
        _stop("input is not UTF-8")
    return data


def _backup(path, raw):
    """Exclusive deterministic backup, checked on a resumed migration; never overwrites evidence."""
    digest = hashlib.sha256(raw).hexdigest()
    backup = path + ".migration-" + digest[:16] + ".bak"
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if _read(backup) != raw:
            _stop("backup collision")
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    if hasattr(os, "O_DIRECTORY"):
        directory = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return digest


def migrate_files(paths=None, now_ms=None):
    """Persist v2 from bounded local evidence, under the live OS lock; return (state, notes).

    paths adds explicit legacy inputs to the well-known files beside cs.STATE. Existing valid v2
    state always refuses (including an apparently empty one), so migration cannot erase work.
    Unknown/malformed input aborts before any product state write. Original bytes remain in place
    or in a checked .migration-<digest>.bak when cs.STATE itself is replaced.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        _stop("has an invalid clock")
    root = os.path.dirname(os.path.abspath(cs.STATE))
    explicit = [os.path.abspath(os.fspath(p)) for p in (paths or [])]
    defaults = [os.path.abspath(cs.STATE)] + [os.path.join(root, name) for name in cs.LEGACY_NAMES]
    if len(explicit) > MAX_FILES:
        _stop("exceeds the 16-file bound")
    with cs.migration_locked():
        selected = list(dict.fromkeys([p for p in defaults if os.path.exists(p)] + explicit))
        if len(selected) > MAX_FILES:
            _stop("exceeds the 16-file bound")
        state, cursors, counted, sources = cs.empty(), set(), [0], []
        for path in selected:
            raw = _read(path)
            text = raw.decode("utf-8").strip()
            if os.path.basename(path) == "cli_call_consumer.pending" and text == "null":
                pass
            elif path == os.path.abspath(cs.STATE) or text.startswith("{"):
                try:
                    data = json.loads(text)
                except ValueError:
                    _stop("contains malformed JSON")
                _consume_json(state, data, cursors, counted)
            elif os.path.basename(path) == "cli_call_consumer.after" or cs.STREAM_ID.fullmatch(text or "!"):
                if text:
                    cursors.add(_entry(text))
            else:
                # The oldest accepted.txt is one ask ID per line, never a completed-task ledger.
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if not lines and os.path.basename(path) != "cli_call_accepted.txt":
                    _stop("contains an empty or unsupported input")
                for ask in lines:
                    counted[0] += 1
                    if counted[0] > MAX_ENTRIES:
                        _stop("exceeds the 500-record bound")
                    _keep(state, ask, old_state="accepted")
            sources.append((path, raw))
        state["seen"] = "0-0"
        state["migration"] = {"v": 1, "at_ms": now_ms, "records": counted[0],
                              "legacy_cursors": sorted(cursors, key=cs.sid), "inputs": []}
        # Validate the entire proposed state before writing any backup or replacing the original.
        if not cs.valid(state):
            _stop("produced invalid state")
        for path, raw in sources:
            if _read(path) != raw:
                _stop("input changed while preparing migration")
            digest = _backup(path, raw)
            state["migration"]["inputs"].append({"name": os.path.basename(path), "sha256": digest,
                                                   "bytes": len(raw)})
        state["version"] = 1
        cs._write(state)
        notes = ["MIGRATED %d task(s); legacy claims require fresh per-ask validation" % len(state["tasks"])]
        if cursors:
            notes.append("CURSOR_RESET: legacy cursors retained as evidence; scan floor is 0-0")
        return state, notes


def replay(entries, records, legacy, now_ms, ask_of):
    """Compatibility pure replay: preserve evidence, never infer new completion from old board tags.

    This is not the operational migration entry point. Real migration is migrate_files/main above.
    """
    if len(entries) > MAX_ENTRIES or len(records) > MAX_ENTRIES:
        _stop("replay exceeds the 500-record bound")
    state, cursors, counted = cs.empty(), set(), [0]
    _consume_json(state, dict(legacy or {}, tasks=(legacy or {}).get("tasks", {})), cursors, counted)
    rows = {(str(r.get("Row_ID", "")), str(r.get("Source_Tag", ""))) for r in records if isinstance(r, dict)}
    for eid, by, text in entries:
        ask = ask_of(text) if by == "call-bridge" else None
        if not ask:
            continue
        counted[0] += 1
        if counted[0] > MAX_ENTRIES:
            _stop("replay exceeds the 500-record bound")
        historical_done = ("CLAUDE-DONE-%s" % ask, TAG) in rows
        prior_accept = any((prefix + ask, TAG) in rows for prefix in
                           ("CLAUDE-ACCEPTED-", "CLAUDE-CODE-CLI-WAKE-ACK-"))
        _keep(state, ask, pending=not (prior_accept or historical_done), entry=eid,
              origin=cs.sid(_entry(eid))[0] / 1000, historical_done=historical_done)
    state["seen"] = "0-0"
    state["legacy_cursors"] = sorted(cursors, key=cs.sid)
    return state, ["HISTORICAL_ONLY: all claims preserved; new authority required", "CURSOR_RESET to 0-0"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", action="append", default=[], metavar="PATH",
                        help="additional local v1 state, pending, cursor or accepted-ID file")
    args = parser.parse_args(argv)
    try:
        _state, notes = migrate_files(args.legacy)
    except SystemExit as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print("STOP: migration input unavailable (%s); original files kept" % type(error).__name__, file=sys.stderr)
        return 2
    for note in notes:
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())

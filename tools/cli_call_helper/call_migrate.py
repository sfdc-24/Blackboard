"""Migration of the older laptop state into v5 by bounded, trusted replay (contract 6; Aya's HOLD points 1 and 2).

replay() is pure: given the call-bridge entries read back over a bounded window and the board rows, it rebuilds
every ask's state from EVIDENCE, never from the old files' word:
  CLAUDE-DONE-<ask> from claude-code-cli      -> HISTORICAL_CLOSED (never acceptance: no type, hash or sanitizer)
  an ACCEPTED row (v4 id or the O3 ACK id) only -> LEGACY_OPEN (rings once; moves only by `legacy` or by expiry)
  neither, 2 h old or less                    -> PENDING
  neither, older                              -> EXPIRED
The old pending ring is kept as a cue. The old cursor is a cross-check only: above the stream's last id, or above
the recomputed lowest-unfinished point, it is reported as CURSOR_ADVANCED and the recomputed value wins.
"""
import call_state as cs

MAX_ENTRIES = 500
WINDOW_MS = 24 * 3600 * 1000
TAG = "claude-code-cli"


def window_start(legacy_cursor, now_ms):
    """Where the replay reads from: the old cursor (or now) minus 24 h."""
    base = cs.sid(legacy_cursor)[0] if legacy_cursor and cs.STREAM_ID.match(legacy_cursor) else now_ms
    return "%d-0" % max(0, base - WINDOW_MS)


def replay(entries, records, legacy, now_ms, ask_of):
    """entries: [(stream id, by, text)] oldest first; records: board rows; legacy: the old v1 state (or {});
    ask_of(text) -> ask id or None. Returns (new state, notes)."""
    if len(entries) > MAX_ENTRIES:
        raise SystemExit("FAILED: %d entries in the replay window (over %d); escalate" % (len(entries), MAX_ENTRIES))
    rows = {(str(r.get("Row_ID", "")), str(r.get("Source_Tag", ""))) for r in records}
    state, notes = cs.empty(), []
    for eid, by, text in entries:
        ask = ask_of(text) if by == "call-bridge" else None
        if not ask or ask in state["tasks"]:
            continue
        origin = cs.sid(eid)[0] / 1000.0
        if ("CLAUDE-DONE-%s" % ask, TAG) in rows:
            st = "HISTORICAL_CLOSED"
        elif ("CLAUDE-ACCEPTED-%s" % ask, TAG) in rows or ("CLAUDE-CODE-CLI-WAKE-ACK-%s" % ask, TAG) in rows:
            # LEGACY_OPEN lapses at origin + 2 h like any open ask: an old one is EXPIRED at once, with a note.
            st = "LEGACY_OPEN" if now_ms - cs.sid(eid)[0] <= 2 * 3600 * 1000 else "EXPIRED"
            if st == "EXPIRED":
                notes.append("LEGACY_EXPIRED %s (accepted, no DONE row, over 2 h)" % ask)
        elif now_ms - cs.sid(eid)[0] <= 2 * 3600 * 1000:
            st = "PENDING"
        else:
            st = "EXPIRED"
        state["tasks"][ask] = {"state": st, "rev": 1, "ops": {}, "entry": eid, "origin": origin, "replayed": True}
    pending = (legacy.get("pending") or {}).get("ask")
    if pending and pending not in state["tasks"]:
        notes.append("PENDING_KEPT %s (cue only, no entry in the window)" % pending)
        state["tasks"][pending] = {"state": "PENDING", "rev": 1, "ops": {}, "source": "legacy-pending"}
    for ask in (legacy.get("tasks") or {}):
        if ask not in state["tasks"]:
            notes.append("LEGACY_UNSEEN %s (not in the replay window; left out, not assumed done)" % ask)
    if entries:
        state["seen"] = entries[-1][0]
    old = legacy.get("cursor") or ""
    if old and cs.STREAM_ID.match(old):
        new = cs.cursor(state)
        last = entries[-1][0] if entries else "0-0"
        if cs.sid(old) > cs.sid(last) or cs.sid(old) > cs.sid(new):
            notes.append("CURSOR_ADVANCED old %s, recomputed %s" % (old, new))
    return state, notes

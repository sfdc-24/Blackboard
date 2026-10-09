#!/usr/bin/env python3
r"""One rule for every board row: never trim silently. Stdlib only.

THE DEFECT THIS CLOSES (Grok's daily scorecard, 2026-10-09)
    Every Gemini waker reply on the board stopped at 1,618-1,623 characters, mid-word. The cause was
    one slice: agent_waker.post_reply passed text[:1500] to fleet_agent post, and fleet_agent then
    prepended its ~120-character BCB header. The waker's own header (wakerreply/answers/route/cost
    fields plus the "Answered by ..." note) is part of that 1,500, so once PY-08 added cost= on
    2026-10-08 the model's answer itself was cut to about 690 characters. Nothing said so. The same
    shape existed in gemini_agent board and foundry_agent board ([:1200]).

THE RULE (poka-yoke, not a gate on the owner)
    1. A row is at most ROW_LIMIT characters (1,500, the owner's limit).
    2. A longer text is NEVER cut quietly. fit() writes the full text to a spill store (the waker's
       own state store: GCS in the cloud, a directory on a laptop) and posts a short row carrying
           full=<pointer>|chars=<N>|sha256=<hex>|
       and a summary that ends on a whole sentence plus a note saying where the rest is.
    3. If the full text cannot be stored, the row still goes out, but it says so on its face:
           truncated=1|chars=<N>|sha256=<hex>|  ... [TRUNCATED: ...]
       A visible mark, never a silent loss.
    4. guard() refuses a row that is over the limit, or that sits in the 1,600-1,640 cap band, with
       neither a pointer nor a truncated mark. Every writer calls it, so a new hard-coded slice in
       any agent's poster fails loudly at the writer instead of on the board.

    from board_row import fit, guard
    payload, info = fit(header, text, key=row_id, spill=store_spill())
    guard(payload)
"""
from __future__ import annotations

import hashlib
import json
import os
import re

ROW_LIMIT = 1500
CAP_BAND = (1600, 1640)
SPILL_PREFIX = "board-full-"
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*=")
_POINTER = re.compile(r"(?:^|\|)full=([^|\s]+)")
_MARK = re.compile(r"(?:^|\|)truncated=([^|]*)")


class RowRejected(ValueError):
    """A row that would lose text without saying so. Raised by guard()."""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def field(payload: str, key: str):
    m = re.search(r"(?:^|\|)" + re.escape(key) + r"=([^|]*)", payload or "")
    return m.group(1) if m else None


def problems(payload: str) -> list:
    """Why this row would lose text silently. Empty list = fine."""
    n = len(payload or "")
    pointer = _POINTER.search(payload or "")
    marked = _MARK.search(payload or "")
    out = []
    if n > ROW_LIMIT and not (pointer or marked):
        out.append("row is %d chars, over the %d limit, with no full= pointer and no truncated= mark"
                   % (n, ROW_LIMIT))
    if CAP_BAND[0] <= n <= CAP_BAND[1] and not (pointer or marked):
        out.append("row ends in the %d-%d cap band (the signature of a fixed slice) with no pointer"
                   % CAP_BAND)
    if pointer or marked:
        if not (field(payload, "chars") or "").isdigit():
            out.append("full=/truncated= row has no chars=<N>")
        if not re.fullmatch(r"[0-9a-f]{64}", field(payload, "sha256") or ""):
            out.append("full=/truncated= row has no sha256=<64 hex>")
    return out


def guard(payload: str) -> str:
    bad = problems(payload)
    if bad:
        raise RowRejected("BOARD_ROW_REJECTED: " + "; ".join(bad))
    return payload


def split_fields(text: str):
    """('k=v|k=v|', free text). The free text may itself contain pipes (tables, quotes)."""
    segs = (text or "").split("|")
    i = 1 if segs and segs[0] == "BCB" else 0     # a prebuilt payload: its envelope is fields too
    while i < len(segs) - 1 and _FIELD.match(segs[i]):
        i += 1
    head = "|".join(segs[:i]) + ("|" if i else "")
    return head, "|".join(segs[i:])


def sentence_cut(text: str, room: int) -> str:
    """At most `room` chars, ending on a whole sentence when one fits, else on a word."""
    text = text or ""
    if len(text) <= room:
        return text
    if room <= 0:
        return ""
    window = text[:room]
    ends = [m.end() for m in re.finditer(r"[.!?](?:[\"')\]]*)(?=\s)", window)]
    if ends and ends[-1] >= room // 3:
        return window[:ends[-1]]
    sp = window.rfind(" ")
    return (window[:sp] if sp > room // 2 else window).rstrip() + " ..."


def fit(header: str, text: str, key: str, spill=None, limit: int = ROW_LIMIT):
    """Return (payload, info). payload = header + text when it fits, else a pointer row.

    header: everything the writer puts before `text` (e.g. "BCB|v=1|id=...|to=...|").
    text:   leading k=v fields, then the free text.
    spill:  callable(key, full_text) -> pointer string; None or a raise means "could not store".
    The spilled text is the WHOLE payload (header included), so the sha256 covers exactly what the
    row would have said.
    """
    full = header + (text or "")
    info = {"chars": len(full), "sha256": sha256(full), "spilled": False, "pointer": None,
            "truncated": False, "error": None}
    if len(full) <= limit:
        return guard(full), info
    fields, free = split_fields(text or "")
    pointer = None
    if spill is not None:
        try:
            pointer = spill(key, full)
        except Exception as exc:  # noqa: BLE001 - a failed store becomes a visible mark, never a drop
            info["error"] = "%s: %s" % (type(exc).__name__, exc)
    n, h = info["chars"], info["sha256"]
    if pointer:
        info.update(spilled=True, pointer=pointer)
        meta = "full=%s|chars=%d|sha256=%s|" % (pointer, n, h)
        note = " [Summary only. Full text: %d chars at full= (sha256 %s).]" % (n, h[:12])
    else:
        info["truncated"] = True
        why = (info["error"] or "no spill store configured").replace("|", "/")[:120]
        meta = "truncated=1|chars=%d|sha256=%s|" % (n, h)
        note = " [TRUNCATED: %d chars in total; the full text could not be stored (%s).]" % (n, why)
    room = limit - len(header) - len(fields) - len(meta) - len(note)
    if room < 0:
        # The fields alone overflow: keep the header and the pointer, drop the fields into the spill.
        fields = ""
        room = limit - len(header) - len(meta) - len(note)
    if room < 0:
        raise RowRejected("BOARD_ROW_REJECTED: header alone is %d chars; nothing fits in %d"
                          % (len(header), limit))
    payload = header + fields + meta + sentence_cut(free.strip(), room) + note
    return guard(payload), info


# ------------------------------------------------------------------- spill stores

def store_spill(store=None):
    """A spill callable backed by state_store (gs://... in the cloud, a directory on a laptop).

    BOARD_SPILL_URI wins, then BLACKBOARD_STATE_URI (the waker's own bucket, so no new grant),
    then <repo>/board-full/. Objects are create-only: an existing object is reused only when its
    sha256 matches, so a pointer can never be repointed at different text.
    """
    if store is None:
        import state_store
        uri = os.environ.get("BOARD_SPILL_URI") or os.environ.get("BLACKBOARD_STATE_URI") or ""
        if not uri and (os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB")):
            # A container's own disk is gone after the run, so a pointer there would point at
            # nothing. No store: fit() posts the row marked truncated=1 instead - visible, not lost
            # quietly.
            return None
        if not uri:
            uri = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "board-full")
        store = state_store.open_store(uri)

    def spill(key: str, full: str) -> str:
        name = SPILL_PREFIX + re.sub(r"[^A-Za-z0-9._-]", "", key or "")[:120]
        record = {"row_id": key, "chars": len(full), "sha256": sha256(full), "text": full}
        try:
            store.save(name, record, None)
        except Exception as exc:  # noqa: BLE001
            existing, _ = store.load(name)
            if (existing or {}).get("sha256") != record["sha256"]:
                raise exc
        return "%s/%s.json" % (store.describe().rstrip("/"), name)
    return spill


def resolve(payload: str, store=None) -> str:
    """The full text a pointer row stands for, verified against its chars= and sha256=."""
    ptr = _POINTER.search(payload or "")
    if not ptr:
        return payload
    name = ptr.group(1).rsplit("/", 1)[-1]
    if name.endswith(".json"):
        name = name[:-5]
    if store is None:
        import state_store
        base = ptr.group(1).rsplit("/", 1)[0]
        store = state_store.open_store(base[5:] if base.startswith("file:") else base)
    record, _ = store.load(name)
    text = (record or {}).get("text") or ""
    if len(text) != int(field(payload, "chars") or -1) or sha256(text) != field(payload, "sha256"):
        raise RowRejected("pointer %s does not match chars=/sha256= on the row" % ptr.group(1))
    return text


if __name__ == "__main__":
    import sys
    p = sys.stdin.read()
    bad = problems(p)
    print(json.dumps({"chars": len(p), "problems": bad}))
    sys.exit(1 if bad else 0)

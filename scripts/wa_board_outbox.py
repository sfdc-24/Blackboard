#!/usr/bin/env python3
"""Blackboard → WhatsApp outbox. Laptop-only send. Governor-only recipient.

WHY THIS EXISTS
  A surface can send WhatsApp iff its host holds Meta credentials
  (docs/COMMS-PROTOCOL.md §1). Most agents do not. Copying META_TOKEN into
  Apps Script or Drive is doctrine D-18, so the durable path is:

    agent posts a board row  →  this watcher, on the laptop, sends it
                                via scripts/wa_notify.ps1

  wa_notify already locks the recipient to WA_TO in the laptop .env.
  This script never accepts a phone number, never reads META_TOKEN, and
  never talks to Graph itself.

BOARD CONTRACT (also docs/WA-OUTBOX.md)
  Preferred:
    BCB|v=1|id=<unique>|phase=WA_SEND|from=<agent-tag>|to=wa-outbox|
    <message body>

  Also accepted:
    WA_OUT|<message body>     (Source_Tag / from= is the agent tag)

  Tag = the agent id (payload from=, else Source_Tag). Text = the body.
  Recipient is not a field. It is the Governor number in .env.

IDEMPOTENCY
  Delivered Row_IDs (and BCB id= values) live in
  logs/wa_board_outbox_state.json. A phase=NOTE row that quotes
  answers=<id> or delivered=<id> also counts as delivered, so a
  replaced state file does not re-send a NOTE-acked row.

  First run with an empty state PRIMES: it records every matching row
  already on the board and sends nothing. A watcher that "catches up"
  by blasting history is how a new install wakes Mr. Salam twenty times.

USAGE
  python scripts/wa_board_outbox.py --dry-run
  python scripts/wa_board_outbox.py --dry-run --fixture rows.json
  python scripts/wa_board_outbox.py once
  powershell -File scripts/wa_board_outbox.ps1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
STATE_PATH = REPO / "logs" / "wa_board_outbox_state.json"
LOG_PATH = REPO / "logs" / "wa_board_outbox.jsonl"
NOTIFY = SCRIPTS / "wa_notify.ps1"
BOARD = "Blackboard - Alpha DB"

sys.path.insert(0, str(SCRIPTS))

TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
KINDS = ("BLOCKED", "ANDON", "STATUS", "DONE", "ASK")
PHASE_SEND = "WA_SEND"
PHASE_NOTE = "NOTE"
PREFIX = "WA_OUT|"
STATE_CAP = 500
DEFAULT_LIMIT = 80
WATCHER_TAG = "wa-outbox"
# An agent waker answering one of HIS WhatsApp rows. Until 2026-09-24 nothing
# delivered these: this outbox sent only WA_SEND rows, and the WhatsApp poller
# sent only "received" acks, so every agent answer to his phone stayed on the
# board. The answer is the text after "REPLY:" in agent_waker's payload.
WAKER_MARK = "wakerreply=1"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cell(row, index: int) -> str:
    if not isinstance(row, (list, tuple)) or index >= len(row) or row[index] is None:
        return ""
    return str(row[index]).strip()


def first_kv(payload: str) -> dict[str, str]:
    """First-value-wins BCB fields. A second id= is a quote, not a rewrite."""
    fields: dict[str, str] = {}
    bare: list[str] = []
    text = payload or ""
    parts = text.split("|")
    if parts and parts[0].strip().upper() == "BCB":
        parts = parts[1:]
    for segment in parts:
        if "=" in segment:
            key, _, val = segment.partition("=")
            key = key.strip().lower()
            if key and key not in fields:
                fields[key] = val.strip()
        elif segment.strip():
            bare.append(segment.strip())
    if bare:
        fields["_bare"] = "\n".join(bare)
    return fields


def is_wa_out_prefix(payload: str) -> bool:
    return (payload or "").lstrip().upper().startswith(PREFIX)


def parse_wa_request(row) -> dict | None:
    """Return a send request, or None if this row is not a WA outbox item.

    Never treats to= / wa_to= / a digit string as a recipient. The only
    destination this path may address is the Governor number inside wa_notify.
    """
    if not isinstance(row, (list, tuple)) or len(row) < 3:
        return None
    payload = cell(row, 5)
    action = cell(row, 4)
    fields = first_kv(payload)
    phase = (fields.get("phase") or action or "").strip().upper()
    prefixed = is_wa_out_prefix(payload)

    if WAKER_MARK in payload and cell(row, 3).split(";")[0].strip().lower() == "whatsapp":
        tag = cell(row, 2)
        _, sep, answer = payload.partition("REPLY:")
        body = answer.strip() if sep else ""
        if not TAG_RE.match(tag) or not body:
            return None
        return {"row_id": cell(row, 0), "ts": cell(row, 1), "tag": tag,
                "text": body, "kind": "STATUS",
                "bcb_id": fields.get("id") or "", "payload": payload}

    if phase == PHASE_NOTE:
        return None
    if phase != PHASE_SEND and not prefixed:
        return None

    tag = fields.get("from") or cell(row, 2)
    if not TAG_RE.match(tag):
        return None

    if prefixed:
        body = payload.lstrip().split("|", 1)[1].strip()
    else:
        body = (fields.get("text") or fields.get("body") or
                fields.get("_bare") or cell(row, 8)).strip()
    if not body:
        return None

    kind = (fields.get("kind") or "STATUS").strip().upper()
    if kind not in KINDS:
        kind = "STATUS"

    return {
        "row_id": cell(row, 0),
        "ts": cell(row, 1),
        "tag": tag,
        "text": body,
        "kind": kind,
        "bcb_id": fields.get("id") or "",
        "payload": payload,
    }


def delivered_markers(row) -> set[str]:
    """Ids a NOTE row claims are already sent."""
    payload = cell(row, 5)
    action = cell(row, 4)
    fields = first_kv(payload)
    phase = (fields.get("phase") or action or "").strip().upper()
    if phase != PHASE_NOTE:
        return set()
    found: set[str] = set()
    for key in ("answers", "delivered", "closes"):
        raw = fields.get(key) or ""
        for part in re.split(r"[,\s;]+", raw):
            if part:
                found.add(part)
    return found


def load_state(path: Path = STATE_PATH) -> dict:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("delivered_row_ids", [])
                data.setdefault("delivered_bcb_ids", [])
                data.setdefault("schema", 1)
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"schema": 1, "delivered_row_ids": [], "delivered_bcb_ids": []}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row_ids = list(dict.fromkeys(state.get("delivered_row_ids") or []))[-STATE_CAP:]
    bcb_ids = list(dict.fromkeys(state.get("delivered_bcb_ids") or []))[-STATE_CAP:]
    out = {
        "schema": 1,
        "saved_at": now_iso(),
        "delivered_row_ids": row_ids,
        "delivered_bcb_ids": bcb_ids,
        "primed_at": state.get("primed_at") or "",
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_log(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def known_delivered(rows, state: dict) -> set[str]:
    known = set(state.get("delivered_row_ids") or [])
    known.update(state.get("delivered_bcb_ids") or [])
    for row in rows:
        known.update(delivered_markers(row))
    return known


def select_undelivered(rows, state: dict) -> list[dict]:
    known = known_delivered(rows, state)
    found: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        req = parse_wa_request(row)
        if req is None:
            continue
        keys = {k for k in (req["row_id"], req["bcb_id"]) if k}
        if not keys or keys & known or keys & seen:
            continue
        seen.update(keys)
        found.append(req)
    return found


def prime_state(rows, state: dict) -> dict:
    """Record every currently-visible request as already handled. Send none."""
    for req in select_undelivered(rows, {"delivered_row_ids": [], "delivered_bcb_ids": []}):
        if req["row_id"]:
            state.setdefault("delivered_row_ids", []).append(req["row_id"])
        if req["bcb_id"]:
            state.setdefault("delivered_bcb_ids", []).append(req["bcb_id"])
    state["primed_at"] = now_iso()
    return state


def mark_delivered(state: dict, req: dict) -> None:
    if req.get("row_id"):
        state.setdefault("delivered_row_ids", []).append(req["row_id"])
    if req.get("bcb_id"):
        state.setdefault("delivered_bcb_ids", []).append(req["bcb_id"])


def notify_argv(req: dict, text_file: Path) -> list[str]:
    """Build the wa_notify invocation. Recipient is not a parameter."""
    shell = "powershell.exe" if os.name == "nt" else "pwsh"
    return [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(NOTIFY),
        "-Tag",
        req["tag"],
        "-Kind",
        req["kind"],
        "-TextFile",
        str(text_file),
    ]


def send_via_notify(req: dict) -> tuple[bool, str]:
    """Send through scripts/wa_notify.py. Recipient is not a parameter.

    It used to shell out to powershell.exe and wa_notify.ps1, which a cloud
    runtime does not have. wa_notify.py is the same contract in stdlib Python:
    the "[KIND - tag]" prefix, the length cap, one attempt and never a retry.
    """
    try:
        from wa_notify import notify
    except Exception as exc:  # noqa: BLE001
        return False, f"wa_notify.py unavailable: {exc}"
    try:
        return notify(req["text"], kind=req["kind"], tag=req["tag"])
    except SystemExit as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - report, do not mark delivered
        return False, f"{type(exc).__name__}: {exc}"


def note_payload(req: dict) -> str:
    rid = req["row_id"] or req["bcb_id"] or "unknown"
    return (
        f"BCB|v=1|id=WA-DELIVERED-{rid}|phase=NOTE|class=DELIVERY|"
        f"from={WATCHER_TAG}|to=ALL|answers={rid}|delivered={rid}"
    )


def post_note(req: dict) -> tuple[bool, str]:
    """Best-effort receipt on the board. Failure must not re-send."""
    try:
        from bus import fetch, load_env  # local import: tests never need the bus
    except Exception as exc:  # noqa: BLE001
        return False, f"bus import failed: {exc}"
    try:
        env = load_env()
    except SystemExit as exc:
        return False, str(exc)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rid = f"WA-DELIVERED-{req['row_id'] or req['bcb_id'] or now_iso()}"
    row = [
        rid, ts, WATCHER_TAG, "ALL", "NOTE", note_payload(req),
        "DONE", "Blackboard", f"WA delivered {req['row_id']}", req["tag"],
    ]
    try:
        body = fetch(
            env["BUS_URL"],
            {
                "action": "append",
                "secret": env["BUS_SECRET"],
                "title": BOARD,
                "sheetRow": row,
            },
            tries=1,
        )
        return True, (body or "")[:200]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def read_board_rows(limit: int) -> list:
    from bus import load_env, read_rows

    env = load_env()
    for key in ("BUS_URL", "BUS_SECRET"):
        if not env.get(key):
            raise SystemExit(f"missing {key} in .env — board read needs the bus, not Meta")
    seen: dict[str, list] = {}
    # Two matches: the bus match is a whole-row substring, so WA_SEND also
    # returns NOTE rows that quote that token (used as delivered markers).
    for token in (PHASE_SEND, "WA_OUT", WAKER_MARK):
        obj = read_rows(env, title=BOARD, match=token, limit=limit)
        for row in obj.get("rows") or []:
            if not isinstance(row, list) or not row:
                continue
            seen[str(row[0])] = row
    return list(seen.values())


def load_fixture(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("rows") or []
    if not isinstance(data, list):
        raise SystemExit("fixture must be a JSON array of rows")
    return [r for r in data if isinstance(r, list)]


def run_once(rows, state: dict, *, dry_run: bool, send: bool, note: bool,
             state_path: Path) -> int:
    empty = not (state.get("delivered_row_ids") or state.get("delivered_bcb_ids"))
    if empty and not dry_run:
        prime_state(rows, state)
        save_state(state, state_path)
        print(f"PRIMED delivered={len(state.get('delivered_row_ids') or [])} sent=0")
        print("First run records history and sends nothing. Re-run to send new rows.")
        return 0

    pending = select_undelivered(rows, state)
    print(f"pending={len(pending)}")
    sent = 0
    failed = 0
    for req in pending:
        preview = req["text"].replace("\n", " ")[:160]
        print(f"  {req['row_id'][:12]}  [{req['kind']} - {req['tag']}]  {preview}")
        if dry_run or not send:
            continue
        ok, detail = send_via_notify(req)
        entry = {
            "row_id": req["row_id"],
            "bcb_id": req["bcb_id"],
            "tag": req["tag"],
            "kind": req["kind"],
            "ts": req["ts"],
            "sent_at": now_iso(),
            "ok": ok,
            "detail": detail[:300],
        }
        if not ok:
            failed += 1
            print("  SEND FAIL", detail[:160].replace("\n", " "))
            append_log(entry)
            continue
        mark_delivered(state, req)
        save_state(state, state_path)
        sent += 1
        print("  SENT", detail[:120].replace("\n", " "))
        if note:
            note_ok, note_detail = post_note(req)
            entry["note"] = note_ok
            entry["note_detail"] = note_detail[:200]
            if not note_ok:
                print("  NOTE FAIL (already marked locally; will not re-send)",
                      note_detail[:120].replace("\n", " "))
        append_log(entry)
    print(f"sent={sent} failed={failed} dry_run={int(dry_run or not send)}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Send undelivered WA_SEND / WA_OUT| board rows to the Governor via wa_notify.ps1"
    )
    ap.add_argument("cmd", nargs="?", default="once", choices=("once", "list"),
                    help="once = send (or prime); list = show pending only")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would send. Touch neither Graph nor the state file.")
    ap.add_argument("--note", action="store_true",
                    help="After a successful send, append a phase=NOTE receipt on the board")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="Most-recent rows to ask the bus for, per match token")
    ap.add_argument("--fixture", type=Path,
                    help="JSON rows file; skip the live board (for dry-run / tests)")
    ap.add_argument("--state", type=Path, default=STATE_PATH,
                    help="Override the delivered-id state file")
    args = ap.parse_args(argv)

    if args.limit < 1:
        raise SystemExit("--limit must be a positive integer")

    if args.fixture:
        rows = load_fixture(args.fixture)
    else:
        rows = read_board_rows(args.limit)

    state = load_state(args.state)
    send = args.cmd == "once" and not args.dry_run
    if args.cmd == "list":
        args.dry_run = True
        send = False
    return run_once(
        rows, state,
        dry_run=args.dry_run, send=send, note=args.note, state_path=args.state,
    )


if __name__ == "__main__":
    raise SystemExit(main())

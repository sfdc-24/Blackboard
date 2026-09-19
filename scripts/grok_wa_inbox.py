#!/usr/bin/env python3
"""SFDC24 — free Grok WhatsApp inbox (no LLM).

Pipedream already writes inbound WhatsApp to Alpha DB (writer tag: whatsapp).
This script polls the board, finds rows addressed to Grok, ACKs via wa_notify.ps1
if asked, and keeps a local cursor so repeats are free.

Usage:
  python scripts/grok_wa_inbox.py once
  python scripts/grok_wa_inbox.py once --ack
  python scripts/grok_wa_inbox.py list --last 20

Schedule with Task Scheduler (no Grok Bot wake = no model spend):
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/grok_wa_inbox_once.ps1
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

REPO = Path(__file__).resolve().parents[1]
ENV_PATH = REPO / ".env"
STATE_PATH = REPO / ".grok_wa_inbox_state.json"
OUTBOX = REPO / "logs" / "grok_wa_inbox.jsonl"

GROK_PREFIX = re.compile(r"^\s*grok\b[\s:,-]*", re.I)

# WHY THIS WIDENED, 2026-09-19
#   Mr Salam: "no one is responding to my whatsapp messages again!". His
#   messages were arriving fine. This poller read ONLY the ones starting with
#   "Grok", so everything he addressed to claude-code-cli, Foundry or Gemini
#   was never read by anything at all - four of them between 04:14 and 04:20Z
#   that day. A doorbell wired to one name is not a doorbell.
#
#   Order matters: vm-claude-code-cli must be tried before claude-code-cli, or
#   the shorter alternative never matches the longer name's prefix.
AGENT_TAGS = [
    "vm-claude-code-cli", "claude-code-cli", "claude code cli", "claude-cli",
    "grok-bot", "grok", "claude", "foundry", "gemini", "codex", "console",
    "copilot", "meta",
]
AGENT_PREFIX = re.compile(
    r"^\s*(" + "|".join(re.escape(t) for t in AGENT_TAGS) + r")\b[\s:,-]*", re.I)


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def bus_post(env: dict[str, str], payload: dict, hops: int = 8):
    opener = urllib.request.build_opener(NoRedirect)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        env["BUS_URL"], data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    url = env["BUS_URL"]
    for _ in range(hops):
        try:
            with opener.open(req, timeout=180) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            body = e.read().decode() if e.fp else ""
            if e.code in (301, 302, 303, 307, 308) and loc:
                if loc.startswith("/"):
                    loc = urljoin(url, loc)
                url = loc
                req = urllib.request.Request(loc, method="GET")
                continue
            return e.code, body
    return 0, "too many redirects"


def read_rows(env: dict[str, str]):
    code, body = bus_post(
        env, {"secret": env["BUS_SECRET"], "action": "read", "title": "Blackboard - Alpha DB"}
    )
    if not body.startswith("{"):
        raise SystemExit(f"board read failed HTTP {code}: {body[:200]}")
    data = json.loads(body)
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        raise SystemExit("invalid board shape")
    return rows


def row_text(r: list) -> str:
    if len(r) > 5 and r[5] is not None:
        return str(r[5])
    return ""


def is_grok_inbound(r: list) -> bool:
    if not isinstance(r, list) or len(r) < 3:
        return False
    tag = str(r[2]).strip().lower()
    if tag != "whatsapp":
        return False
    text = row_text(r).strip()
    if not text:
        return False
    # Media stubs are not Grok-addressed unless caption carries prefix
    if text.upper().startswith("WA-MEDIA|"):
        return False
    return bool(GROK_PREFIX.match(text))


def is_agent_inbound(r: list) -> bool:
    """Any inbound WhatsApp addressed to a fleet tag, not just Grok."""
    if not isinstance(r, list) or len(r) < 3:
        return False
    if str(r[2]).strip().lower() != "whatsapp":
        return False
    text = row_text(r).strip()
    if not text or text.upper().startswith("WA-MEDIA|"):
        return False
    return bool(AGENT_PREFIX.match(text))


def addressed_tag(text: str) -> str:
    m = AGENT_PREFIX.match(text or "")
    return m.group(1).lower() if m else ""


def strip_prefix(text: str) -> str:
    """Strip whichever fleet tag opens the message, not only Grok."""
    return AGENT_PREFIX.sub("", text, count=1).strip()


def load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen_ids": [], "last_ts": None}


def save_state(state: dict) -> None:
    # keep last 500 ids
    seen = state.get("seen_ids") or []
    state["seen_ids"] = seen[-500:]
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def append_log(entry: dict) -> None:
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ack_whatsapp(text: str) -> tuple[bool, str]:
    """Free-form Meta send via existing laptop script — no LLM."""
    ps1 = REPO / "scripts" / "wa_notify.ps1"
    if not ps1.is_file():
        return False, "wa_notify.ps1 missing"
    # Pass text via env-less temp file to avoid PowerShell quoting pain
    tmp = REPO / "logs" / "_grok_wa_ack.txt"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps1),
        "-TextFile",
        str(tmp),
        # NOT the tag of the lane being addressed. This receipt comes from the
        # poller on the laptop; stamping it grok-bot made an automatic ACK look
        # like Grok had answered, which is the confusion Mr Salam named on
        # 2026-09-07 when he asked every message to identify its sender.
        "-Tag",
        "wa-poller",
        "-Kind",
        "STATUS",
    ]
    try:
        p = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=90)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode == 0, out[-500:]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def cmd_list(rows, last: int) -> int:
    hits = [r for r in rows if is_agent_inbound(r)][-last:]
    for r in hits:
        rid = str(r[0]) if r else ""
        ts = str(r[1]) if len(r) > 1 else ""
        body = strip_prefix(row_text(r))
        print(f"{ts[:19]}  {rid[:8]}  {body[:200]}")
    print(f"count={len(hits)}")
    return 0


def cmd_once(rows, ack: bool) -> int:
    state = load_state()
    seen = set(state.get("seen_ids") or [])
    new = []
    for r in rows:
        if not is_agent_inbound(r):
            continue
        rid = str(r[0])
        if rid in seen:
            continue
        new.append(r)

    print(f"new_agent_inbound={len(new)}")
    for r in new:
        rid = str(r[0])
        ts = str(r[1]) if len(r) > 1 else ""
        raw = row_text(r)
        body = strip_prefix(raw)
        tag = addressed_tag(raw) or "ALL"
        entry = {
            "id": rid,
            "ts": ts,
            "text": body,
            "raw": raw[:500],
            "tag": tag,
            "seen_at": datetime.now(timezone.utc).isoformat(),
            "acked": False,
        }
        print(f"NEW [{tag}] {ts[:19]} {body[:180]}")
        if ack:
            # A receipt, deliberately not an answer. It names the lane so the
            # silence that follows is attributable: if the ack lands and the
            # lane never replies, the lane is the thing that is not listening,
            # which is exactly what was invisible before 2026-09-19.
            msg = (
                f"Received and logged for {tag} at {ts[11:19]}Z. "
                f"This is the laptop poller's receipt, not an answer - no model "
                f"was called. If {tag} does not come back to you, nothing is "
                f"live on that lane right now. Preview: {body[:110]}"
            )
            ok, detail = ack_whatsapp(msg)
            entry["acked"] = ok
            entry["ack_detail"] = detail[:300]
            print("ACK", "ok" if ok else "fail", detail[:120].replace("\n", " "))
        append_log(entry)
        seen.add(rid)
        state["last_ts"] = ts

    state["seen_ids"] = list(seen)
    save_state(state)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list")
    pl.add_argument("--last", type=int, default=20)
    po = sub.add_parser("once")
    po.add_argument("--ack", action="store_true", help="Send STATUS WhatsApp ACK via wa_notify (no LLM)")
    args = p.parse_args()

    env = load_env(ENV_PATH)
    for k in ("BUS_URL", "BUS_SECRET"):
        if not env.get(k):
            raise SystemExit(f"missing {k} in .env")

    rows = read_rows(env)
    if args.cmd == "list":
        return cmd_list(rows, args.last)
    if args.cmd == "once":
        return cmd_once(rows, args.ack)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


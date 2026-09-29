#!/usr/bin/env python3
"""claude-code-cli's inbox and doorbell: the board rows meant for the Claude Code session.

Mr. Salam, 2026-09-29, after four WhatsApp messages to claude-code-cli went unanswered for 20
minutes: "the waker should bring it to you at least or else what's the use", and "fix and poka
yoke that". WhatsApp reaches the board (writer tag `whatsapp`) and the claude-api standby answers
at once, but nothing reached the laptop session: it wakes only on its own watchers, and its board
watch had lapsed at the 30-minute monitor cap.

    python scripts/claude_cli_inbox.py            print new rows, then move the bookmark past them
    python scripts/claude_cli_inbox.py --peek     print new rows, keep the bookmark
    python scripts/claude_cli_inbox.py --wait 45  the doorbell: poll every 45 s, exit with the first
                                                  new rows (run it in the background; restart it
                                                  every time it rings)

A row is for claude-code-cli when it came from WhatsApp (every message from him goes through the
board), when its target or text names claude-code-cli (vm-claude-code-cli too), when it is addressed
to plain "claude" (Grok writes to=claude,codex,gemini), or when a lead (Grok, Codex) sends it to the
fleet or to all. The standby's and other wakers' copies "to ...,ALL" are not, unless they name
claude, and neither are its own rows.

The bookmark (BOOKMARK, default ~/.claude-cli-inbox-bookmark) moves only after the rows are
printed, so a crash shows a row again and never skips it. A failed or empty read never moves it.
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bus  # noqa: E402

BOOKMARK = Path(os.environ.get("CLAUDE_CLI_INBOX_BOOKMARK", Path.home() / ".claude-cli-inbox-bookmark"))
MINE = re.compile(r"claude-code-cli", re.I)                      # vm-claude-code-cli too
FLEET = re.compile(r"(^|[|;,=])\s*fleet\s*($|[|;,])", re.I)
# Leads whose fleet notes are for me; a prefix, since Codex posts as codex, chatgpt-codex-desktop,
# chatgpt-codex-desktop-<session> and CODEX-DESKTOP (Cursor on #294).
LEADS = ("grok", "codex", "chatgpt-codex-desktop")
# The names that mean this session in an address list. Grok addresses it as plain "claude"
# (to=claude,codex,gemini,all): 35 dispatches on 2026-09-28 23:35-00:16Z matched none of the
# above and never reached it. Whole names only, so claude-api (the standby) is not this session.
NAMES = {"claude", "claude-code-cli", "vm-claude-code-cli"}
ADDRESS = re.compile(r"(?:^|\|)\s*(?:to|cc)=([^|]*)", re.I)
# With no readable bookmark (a first run, a lost file), look back this far rather than start at
# "now" and skip what is already waiting (Cursor on #294).
COLD_START = timedelta(hours=6)
SELF = "claude-code-cli"


def parse_ts(value) -> datetime | None:
    """A board timestamp (ISO 8601, 'Z' or an offset) as an aware UTC datetime, or None. Converted,
    not relabelled: since_arg writes it with a Z (Codex on #294: +05:00 was sent as Z)."""
    text = str(value or "").strip()
    if not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def addressees(row) -> set:
    """The names a row is addressed to, lower-cased: its target column, and the payload's to= and
    cc= lists."""
    target = str(row[3]) if len(row) > 3 else ""
    payload = str(row[5]) if len(row) > 5 else ""
    fields = [target] + ADDRESS.findall(payload[:600])
    return {name.strip().lower() for field in fields for name in re.split(r"[,;]", field) if name.strip()}


def is_mine(row) -> bool:
    """Whether a board row is for the claude-code-cli session."""
    sender = str(row[2]).strip().lower() if len(row) > 2 else ""
    target = str(row[3]) if len(row) > 3 else ""
    payload = str(row[5]) if len(row) > 5 else ""
    if sender.startswith(SELF):
        return False                                    # its own rows
    if sender == "whatsapp":
        return True
    names = addressees(row)
    if names & NAMES or MINE.search(target) or MINE.search(payload[:400]):
        return True
    lead = any(sender.startswith(prefix) for prefix in LEADS)
    return lead and bool(names & {"fleet", "all"} or FLEET.search(target) or FLEET.search(payload[:200]))


def new_rows(rows, since: datetime) -> list:
    """The rows for claude-code-cli newer than `since`, oldest first, as (time, row)."""
    found = []
    for row in rows:
        ts = parse_ts(row[1] if len(row) > 1 else None)
        if ts is not None and ts > since and is_mine(row):
            found.append((ts, row))
    return sorted(found, key=lambda pair: pair[0])


def read_bookmark(path: Path = None):
    """(the bookmark, "ok"), or (None, "missing") or (None, "corrupt: <what it held>")."""
    path = path or BOOKMARK
    if not path.exists():
        return None, "missing"
    text = path.read_text(encoding="utf-8").strip()
    stored = parse_ts(text)
    return (stored, "ok") if stored is not None else (None, "corrupt: %r" % text[:40])


def baseline(path: Path, out) -> datetime:
    """The bookmark, or with none readable a deliberate start COLD_START back, written down before
    the first read so every later poll starts from the same point, and said out loud (Codex on
    #294: a fresh "now" each poll lost what arrived between polls, and a corrupt bookmark must not
    pass silently)."""
    since, status = read_bookmark(path)
    if since is None:
        since = datetime.now(timezone.utc) - COLD_START
        path.write_text(since.isoformat(), encoding="utf-8")
        out("BOOKMARK %s: starting from %s (%s back)" % (status, since.isoformat(), COLD_START))
    return since


def since_arg(since: datetime) -> str:
    """The bus's `since`, a whole second before the bookmark: the bus compares text, and
    "...:24.500Z" sorts before "...:24Z", so a row later in the bookmark's own second would be
    dropped (Cursor on #294). new_rows then keeps only rows after the exact bookmark."""
    utc = since.astimezone(timezone.utc)
    return (utc.replace(microsecond=0) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def line(ts: datetime, row) -> str:
    sender = str(row[2]) if len(row) > 2 else ""
    target = str(row[3]) if len(row) > 3 else ""
    payload = (str(row[5]) if len(row) > 5 else "").replace("\n", " ")
    return "%s | from %s | to %s | %s" % (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), sender, target, payload[:900])


def check(read, path: Path = None, peek: bool = False, out=None):
    """Print the new rows and (unless `peek`) move the bookmark past them. Returns how many, or
    None when the read failed (the bookmark stays)."""
    path = path or BOOKMARK
    out = out or print                  # at call time, so a caller's print is the one used
    since = baseline(path, out)
    try:
        rows = read(since)
    except (SystemExit, OSError, ValueError) as error:
        out("READ FAILED (%s): bookmark kept at %s" % (error, since.isoformat()))
        return None
    found = new_rows(rows or [], since)
    for ts, row in found:
        out(line(ts, row))
    if found and not peek:
        path.write_text(found[-1][0].isoformat(), encoding="utf-8")
    return len(found)


def main(argv) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # A row can hold any character. On the Windows console (cp1252) a "\u2192" in a row
        # crashed the doorbell before the bookmark moved (2026-09-29 04:00Z), so it would have
        # crashed on that same row at every start: a doorbell that never rings again.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    env = bus.load_env()

    def read(since):
        return bus.read_rows(env, since=since_arg(since))["rows"]

    if "--wait" in argv:
        every = float(argv[argv.index("--wait") + 1])
        while True:
            if check(read):
                return 0
            time.sleep(every)
    count = check(read, peek="--peek" in argv)
    if count is None:
        return 2
    if count == 0:
        print("inbox empty since %s" % read_bookmark()[0].isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

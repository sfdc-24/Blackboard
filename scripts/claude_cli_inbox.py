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
board), when its target or text names claude-code-cli (vm-claude-code-cli too), or when a lead
(Grok, Codex) sends it to the fleet. The standby's and other wakers' copies "to ...,ALL" are not,
unless they name claude-code-cli, and neither are its own rows.

The bookmark (BOOKMARK, default ~/.claude-cli-inbox-bookmark) moves only after the rows are
printed, so a crash shows a row again and never skips it. A failed or empty read never moves it.
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bus  # noqa: E402

BOOKMARK = Path(os.environ.get("CLAUDE_CLI_INBOX_BOOKMARK", Path.home() / ".claude-cli-inbox-bookmark"))
MINE = re.compile(r"claude-code-cli", re.I)                      # vm-claude-code-cli too
FLEET = re.compile(r"(^|[|;,=])\s*fleet\s*($|[|;,])", re.I)
LEADS = ("grok", "grok-bot", "codex", "chatgpt-codex-desktop")
SELF = "claude-code-cli"


def parse_ts(value) -> datetime | None:
    """A board timestamp (ISO 8601, 'Z' or an offset) as an aware UTC datetime, or None."""
    text = str(value or "").strip()
    if not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_mine(row) -> bool:
    """Whether a board row is for the claude-code-cli session."""
    sender = str(row[2]).strip().lower() if len(row) > 2 else ""
    target = str(row[3]) if len(row) > 3 else ""
    payload = str(row[5]) if len(row) > 5 else ""
    if sender.startswith(SELF):
        return False                                    # its own rows
    if sender == "whatsapp":
        return True
    if MINE.search(target) or MINE.search(payload[:400]):
        return True
    return sender in LEADS and bool(FLEET.search(target) or FLEET.search(payload[:200]))


def new_rows(rows, since: datetime) -> list:
    """The rows for claude-code-cli newer than `since`, oldest first, as (time, row)."""
    found = []
    for row in rows:
        ts = parse_ts(row[1] if len(row) > 1 else None)
        if ts is not None and ts > since and is_mine(row):
            found.append((ts, row))
    return sorted(found, key=lambda pair: pair[0])


def read_bookmark(path: Path = None) -> datetime:
    path = path or BOOKMARK
    stored = parse_ts(path.read_text(encoding="utf-8").strip()) if path.exists() else None
    return stored or datetime.now(timezone.utc)


def line(ts: datetime, row) -> str:
    sender = str(row[2]) if len(row) > 2 else ""
    target = str(row[3]) if len(row) > 3 else ""
    payload = (str(row[5]) if len(row) > 5 else "").replace("\n", " ")
    return "%s | from %s | to %s | %s" % (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), sender, target, payload[:900])


def check(read, path: Path = None, peek: bool = False, out=print):
    """Print the new rows and (unless `peek`) move the bookmark past them. Returns how many, or
    None when the read failed (the bookmark stays)."""
    path = path or BOOKMARK
    since = read_bookmark(path)
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
    env = bus.load_env()

    def read(since):
        # A little before the bookmark: the bus's `since` compares text, and a row stamped in
        # the same second must not fall through; new_rows compares exact times.
        return bus.read_rows(env, since=(since.replace(microsecond=0)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    if "--wait" in argv:
        every = float(argv[argv.index("--wait") + 1])
        while True:
            if check(read, out=print):
                return 0
            time.sleep(every)
    count = check(read, peek="--peek" in argv)
    if count is None:
        return 2
    if count == 0:
        print("inbox empty since %s" % read_bookmark().isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

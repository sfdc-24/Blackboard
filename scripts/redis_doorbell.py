#!/usr/bin/env python3
"""A doorbell that rings on a Redis event, not a timer: wait on this agent's own lane through the bridge.

The owner, 2026-11-08: "Redis is the always-on signal so every agent wakes on event instead of polling."
Every laptop doorbell so far read the board Sheet every 45 seconds. This one holds one governed request
open on the bridge (op xwait: XREAD BLOCK on fleet:inbox:<me>, after a cursor) and returns the moment an
entry lands in that lane. Board rows addressed to an agent reach its lane once the bus-requests mirror is
on (sfdc-24/Blackboard#480); the call bridge already writes there.

    python scripts/redis_doorbell.py --me claude-code-cli          # wait; print new entries; exit 0
    python scripts/redis_doorbell.py --me claude-code-cli --peek   # print, do not move the cursor

Run it in the background, the same way as claude_cli_inbox.py --wait: it exits with the new entries,
and the caller restarts it. The cursor (REDIS_DOORBELL_CURSOR, default ~/.redis-doorbell-<me>) moves
only after the entries are printed, so an entry is never skipped. A first run starts from now.

It needs the bridge for this agent (REDIS_BRIDGE=on, REDIS_BRIDGE_URL, REDIS_BRIDGE_IMPERSONATE; see
scripts/bridge_client.py). Without it, it says so and exits 3, and the board doorbell is the fallback.
Exit codes: 0 entries printed, 2 refused or denied, 3 the bridge is not usable here.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_client  # noqa: E402

LANE = "fleet:inbox:"


def cursor_path(me):
    return Path(os.environ.get("REDIS_DOORBELL_CURSOR") or (Path.home() / (".redis-doorbell-" + me)))


def read_cursor(path, now=None):
    """The last entry id printed, or an id for now when there is none yet (a first run starts from now)."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text and bridge_client_stream_id(text):
        return text
    return "%d-0" % int((time.time() if now is None else now) * 1000)


def bridge_client_stream_id(text):
    parts = text.split("-")
    return len(parts) == 2 and all(part.isdigit() for part in parts)


def write_cursor(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(value, encoding="utf-8")
    os.replace(temp, path)


def ring(me, attempt=bridge_client.attempt, out=None, cursor_file=None, peek=False, rounds=None, now=None):
    """Wait until an entry lands in fleet:inbox:<me>; print each as one JSON line. Returns an exit code."""
    out = out or sys.stdout
    path = cursor_file or cursor_path(me)
    after = read_cursor(path, now)
    done = 0
    while rounds is None or done < rounds:
        done += 1
        try:
            result = attempt("xwait", LANE + me, me=me, after=after, count=10)
        except (bridge_client.Refused, bridge_client.Denied) as why:
            print("redis doorbell refused: %s" % why, file=sys.stderr)
            return 2
        if result is None:
            print("redis doorbell: the bridge is not usable here; use the board doorbell", file=sys.stderr)
            return 3
        entries = [entry for entry in (result.get("value") or []) if isinstance(entry, dict)]
        if not entries:
            continue
        for entry in entries:
            out.write(json.dumps(entry, sort_keys=True) + "\n")
        out.flush()
        if not peek:
            write_cursor(path, str(result.get("last") or after))
        return 0
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Wait on this agent's Redis lane through the bridge.")
    parser.add_argument("--me", required=True, help="this agent's principal, e.g. claude-code-cli")
    parser.add_argument("--peek", action="store_true", help="print new entries without moving the cursor")
    args = parser.parse_args(argv)
    return ring(args.me.strip().lower(), peek=args.peek)


if __name__ == "__main__":
    sys.exit(main())

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
only after the entries are printed, so an entry is never skipped. A first run starts at the lane's own
newest entry id, read from Redis, so nothing added after that is missed.

It needs the bridge for this agent (REDIS_BRIDGE=on, REDIS_BRIDGE_URL, REDIS_BRIDGE_IMPERSONATE; see
scripts/bridge_client.py). Without it, it says so and exits 3, and the board doorbell is the fallback.
Exit codes: 0 entries printed, 2 refused or denied, 3 the bridge is not usable here, 4 the cursor file
exists but is unreadable (fix or remove it by hand; it is never overwritten).

A 429 busy (the bridge already holds as many waits as it lets one instance hold) is none of these: the
doorbell sleeps the seconds the bridge named, plus a little jitter so a crowd does not return together,
and waits again. It is not a refusal and not a reason to use the board doorbell.

THE PILOT BESIDE THE BOARD DOORBELL (claude-code-cli, 2026-11-20). Two things the command line does that
ring() does not do unless asked, so scripts/redis_ring.py, which calls ring() itself, is unchanged:

    --format line   one line per entry in the board doorbell's own shape (claude_cli_inbox.line):
                    "<at> | from <by> | to <me> | id=<row_id>|phase=...|via=redis:<stream id>". A lane
                    entry is metadata only (the board mirror copies no payload text), so the line names
                    the row and the session reads it on the board. --format json is the default.
    own rows        an entry whose `by` is this agent itself is not printed and does not ring: the cursor
                    moves past it and the wait goes on. Over 24 hours on 2026-11-19/20 that was 522 of the
                    1,274 board rows the mirror would have put on claude-code-cli's lane: its own ALL rows.
                    The one exception is the board doorbell's: the compliance runner's rings, posted under
                    this agent's own tag to this agent (OWN_KEEP). --include-own prints them all.
"""
import argparse
import datetime
import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_client  # noqa: E402

LANE = "fleet:inbox:"
# Rows this agent posted itself that are still for it (claude_cli_inbox.OWN_RING), by row-id prefix.
OWN_KEEP = {"claude-code-cli": ("CLAUDE-CLI-COMPLIANCE-RING-",)}
# The fields a line shows after the row id, in this order, when the entry carries them.
LINE_FIELDS = ("phase", "thread_id", "msg_type", "class", "for")


def cursor_path(me):
    return Path(os.environ.get("REDIS_DOORBELL_CURSOR") or (Path.home() / (".redis-doorbell-" + me)))


class CursorUnreadable(Exception):
    """The cursor file exists but cannot be read or parsed. Never treated as a first run."""


def read_cursor(path):
    """The last entry id printed, or None on a first run (no cursor file at all).

    Only a missing file is a first run. A file that exists but cannot be read, or holds anything but a
    stream id, raises CursorUnreadable and is left as it is: starting again at the lane's newest entry
    would silently skip every entry after the saved one (Codex on sfdc-24/Blackboard#482 R2)."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as why:
        raise CursorUnreadable("cannot read %s: %s" % (path, why)) from why
    if not bridge_client_stream_id(text):
        raise CursorUnreadable("%s does not hold a stream id (<ms>-<seq>): %r" % (path, text[:40]))
    return text


def lane_position(me, attempt):
    """A first run starts at the lane's own newest id, read from Redis, never from this computer's clock:
    an entry added in the same millisecond as a clock-made id would be skipped by XREAD's exclusive start
    (Codex on sfdc-24/Blackboard#482). An empty lane starts at 0-0. None when the bridge is not usable."""
    result = attempt("xrange", LANE + me, me=me, count=1)
    if result is None:
        return None
    newest = [entry for entry in (result.get("value") or []) if isinstance(entry, dict)]
    return str(newest[-1].get("id")) if newest and newest[-1].get("id") else "0-0"


def bridge_client_stream_id(text):
    parts = text.split("-")
    return len(parts) == 2 and all(part.isdigit() for part in parts)


def write_cursor(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(value, encoding="utf-8")
    os.replace(temp, path)


def is_own(me, entry):
    """True when the entry is a row this agent posted itself and not one of OWN_KEEP's."""
    by = str(entry.get("by") or "").strip().lower()
    if not by or not (by == me or by.startswith(me + "-")):
        return False
    row_id = str(entry.get("row_id") or "")
    return not any(row_id.startswith(prefix) for prefix in OWN_KEEP.get(me, ()))


def _when(text):
    """A board time as the board doorbell prints it (whole seconds, Z), or the text itself if it is not one."""
    text = str(text or "").strip()
    try:
        moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text or "-"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def line(me, entry):
    """One entry in the board doorbell's shape: "<at> | from <by> | to <me> | id=<row_id>|...|via=redis:<id>"."""
    fields = ["id=%s" % (entry.get("row_id") or "-")]
    fields += ["%s=%s" % (name, entry[name]) for name in LINE_FIELDS if entry.get(name)]
    fields.append("via=redis:%s" % (entry.get("id") or "-"))
    return "%s | from %s | to %s | %s" % (_when(entry.get("at")), entry.get("by") or "-", me, "|".join(fields))


def ring(me, attempt=bridge_client.attempt, out=None, cursor_file=None, peek=False, rounds=None,
         sleep=time.sleep, fmt="json", skip_own=False):
    """Wait until an entry lands in fleet:inbox:<me>; print each as one JSON line (fmt="line": the board
    doorbell's shape). skip_own passes over this agent's own rows (is_own). Returns an exit code."""
    out = out or sys.stdout
    path = cursor_file or cursor_path(me)
    try:
        after = read_cursor(path)
    except CursorUnreadable as why:
        print("redis doorbell: %s; not starting over, the file is left as it is" % why, file=sys.stderr)
        return 4
    if after is None:
        try:
            after = lane_position(me, attempt)
        except (bridge_client.Refused, bridge_client.Denied) as why:
            print("redis doorbell refused: %s" % why, file=sys.stderr)
            return 2
        if after is None:
            print("redis doorbell: the bridge is not usable here; use the board doorbell", file=sys.stderr)
            return 3
        if not peek:
            write_cursor(path, after)
    done = 0
    while rounds is None or done < rounds:
        done += 1
        try:
            result = attempt("xwait", LANE + me, me=me, after=after, count=10)
        except bridge_client.Busy as busy:
            # CAPACITY: no room for a wait just now. Back off and wait again; never the board.
            pause = busy.retry_after + random.uniform(0, 1)
            print("redis doorbell: the bridge is busy; waiting again in %.0f s" % pause, file=sys.stderr)
            sleep(pause)
            continue
        except (bridge_client.Refused, bridge_client.Denied) as why:
            print("redis doorbell refused: %s" % why, file=sys.stderr)
            return 2
        if result is None:
            print("redis doorbell: the bridge is not usable here; use the board doorbell", file=sys.stderr)
            return 3
        entries = [entry for entry in (result.get("value") or []) if isinstance(entry, dict)]
        if not entries:
            continue
        shown = [entry for entry in entries if not (skip_own and is_own(me, entry))]
        if not shown:
            # Only this agent's own rows: past them, and wait again. Nothing printed, so nothing rings.
            after = str(result.get("last") or after)
            if not peek:
                write_cursor(path, after)
            continue
        for entry in shown:
            out.write((line(me, entry) if fmt == "line" else json.dumps(entry, sort_keys=True)) + "\n")
        out.flush()
        if not peek:
            write_cursor(path, str(result.get("last") or after))
        return 0
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Wait on this agent's Redis lane through the bridge.")
    parser.add_argument("--me", required=True, help="this agent's principal, e.g. claude-code-cli")
    parser.add_argument("--peek", action="store_true", help="print new entries without moving the cursor")
    parser.add_argument("--format", choices=("json", "line"), default="json",
                        help="json (default), or line: the board doorbell's own shape")
    parser.add_argument("--include-own", action="store_true",
                        help="also ring on rows this agent posted itself (passed over by default)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # as claude_cli_inbox: cp1252 consoles
    return ring(args.me.strip().lower(), peek=args.peek, fmt=args.format, skip_own=not args.include_own)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
r"""The Pi's own doorbell: it PULLS the board, because nothing can push to it.

WHY THIS EXISTS, AND WHY IT IS NOT A FOURTH ROUTE IN THE WATCHER

His instruction, 2026-10-04: "can you help pi1-cli with the setup that's blocking it", and
pi1-cli's own account of what blocks it (PI1-SHADOW-CCC-20261003T1936Z): *"It has no waker: I
only see rows addressed to me when the owner opens a session."* That is why
`CCC-PI1-HDMI-DARK-20261003T1407Z` went unanswered through his 10:03 call while his HDMI screen
was dark - the row was correct, verified, and read by nobody.

`cloud/board-watcher/main.py` carries three routes (gemini-waker, claude-api-waker, wa-outbox) and
each one STARTS A CLOUD RUN JOB. The Pi is not a Cloud Run job. It is on home Wi-Fi behind NAT with
no inbound path, so no watcher tick, no scheduler and no job can reach it, and a fourth route would
have had nothing to start. His own ruling of 2026-09-24 already said which way this goes: *"why is
scheduler needed? shouldn't things work based on pull system"*. The Pi pulls.

So this is the doorbell that runs ON the Pi. It needs no Cloud Run job, no Cloud Scheduler, no GCP
credential and no new paid resource: only the bus pair the Pi already holds, which is how it has
been posting its own rows all along.

IT IS A NOTIFICATION, NEVER A TASK. A row's content never becomes a command here: the wake command
is the Pi's own script, and all it is told is HOW MANY rows arrived and WHERE they are. Nothing from
the board reaches a shell (Codex's R2, 2026-10-04: "notice is not task or reply authority").

WHAT ONE PASS DOES
  1. stamps the moment it is about to read. That stamp, not the clock after the work, becomes the
     next watermark - otherwise a row that arrives DURING a slow wake is never seen again;
  2. reads only the rows new since the old watermark, minus an overlap, never the whole board;
  3. keeps the rows addressed to this tag or to ALL, dropping its own and any waker reply;
  4. appends the new ones to an inbox file, ONCE each, and records that it did before running
     anything, so a retry after a failed wake does not write a second copy;
  5. runs the wake command once for the pass;
  6. advances the watermark only if the wake succeeded AND the read was covered - and never to an
     earlier moment than the cursor already stands at.

AND FROM THE SECOND REVIEW (Codex on f2eaee0, three P2s: CODEX-SUCCESSOR-QC-RESULT-20261004T1336Z)
  * A CAPPED READ IS REFUSED, NOT HELD. `limit` on this bus is the NEWEST n rows. Holding the
    cursor on a full read does not make the rows it left out reachable - the next read returns the
    same newest n, and the bus has no `until` to page back with. So --limit is accepted only with
    --dry-run, and a pass that could ring or move the cursor reads uncapped.
  * A CURSOR THAT IS NOT A TIMESTAMP FAILS CLOSED. A nonempty string that will not parse used to
    fail open twice over: the read went out unfiltered, and priming was skipped because the
    watermark was truthy. It is validated in load_state, before any read.
  * THE INBOX FILE IS THE JOURNAL. It is appended and fsynced before the state records it, so a
    kill between the two left a row the state did not know about. Recovery reads the inbox's own
    Row_IDs, and a torn final line is truncated so the row is written once, whole.

THE FOUR THINGS IT REFUSES TO DO, each one Codex's finding on head 0a440cc (NO-GO,
CODEX-PI-WA-QC-RESULT-20261004T012631Z), each with its own control
  * IT NEVER SKIPS AN ARRIVAL. The watermark is the read's own timestamp, taken before the read, so
    the window [read, wake finished] is the next pass's business and not lost. A read that came back
    capped - as many rows as the limit allows - is NOT covered, so the watermark does not move at
    all and the next pass re-reads the same window. Row_ID dedupe makes that free.
  * A CORRUPT CURSOR FAILS CLOSED. An unreadable cursor is not an empty one. Missing means first
    run, and first run primes; unreadable means refuse the pass, say so, and touch nothing. The
    opposite - treating a truncated file as "no history" - primes away everything pending.
  * ONE ROW IS ONE INBOX ENTRY, whether the wake failed, succeeded, or was killed between the two.
    What has been written down is recorded separately from what has been handled.
  * A ROW TO ALL IS A ROW TO THIS PI. `agent_waker.addressed_to` answers for a NAMED tag, and a
    genuine ALL row names nobody - it was missed, and my own test had been passing only because its
    fixture also carried `to=pi1-cli`. ALL is explicit now, with an honest fixture.

AND, FROM THE NIGHT IT WAS WRITTEN
  * IT PRIMES ON FIRST RUN. With no cursor it records what is on the board as seen and wakes
    nothing. Hours earlier, one EOD send delivered nineteen WhatsApp messages to Mr Salam because a
    queue with no drainer had collected eighteen rows since Sep 21 and nothing had primed. A
    doorbell's first ring must not be a fortnight of news.
  * AN OLD ROW DOES NOT RING. Past `--max-age-hours` (default 24) a row is recorded as seen and the
    wake is not run for it. A timestamp that cannot be read counts as old, because this board has
    rows whose timestamp column holds a BCB payload.

ON THE PI

    BLACKBOARD_ENV=/home/pi/.blackboard.env \
      python3 scripts/pi1_wake.py --loop 60 --cmd '/home/pi/wake-session.sh'

`--loop <seconds>` keeps it running; one pass and exit is the default, which is what a systemd
timer wants. `docs/PI1-WAKE.md` has the unit, the timer and the one-minute install.

    --me <tag>            whose rows to watch (default pi1-cli)
    --cmd <command>       run this once per pass with new rows; unset queues only
    --inbox <path>        where rows are appended (default ~/.pi1_inbox.jsonl)
    --state <path>        the cursor (default ~/.pi1_wake_state.json)
    --max-age-hours <n>   older rows are marked seen, not rung (0 rings anyway)
    --limit <n>           rows to ask the bus for; --dry-run only (it reads the NEWEST n)
    --once / --loop <s>   one pass, or every s seconds
    --dry-run             say what would ring; touch neither the state nor the command
    --prime               record everything visible as seen and ring nothing
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_waker as aw                                        # noqa: E402
import bus                                                      # noqa: E402

BOARD = "Blackboard - Alpha DB"
ME = "pi1-cli"
# The watcher's overlap, for the same reason: a row whose timestamp lands slightly behind a later
# one is still seen, and Row_ID dedupes the repeat.
OVERLAP_SECONDS = 180
SEEN_CAP = 500
DEFAULT_MAX_AGE_HOURS = 24.0
DEFAULT_STATE = Path.home() / ".pi1_wake_state.json"
DEFAULT_INBOX = Path.home() / ".pi1_inbox.jsonl"
# A row addressed to everyone is addressed to this Pi too. agent_waker.addressed_to answers for a
# NAMED tag, so a genuine ALL row - target ALL, payload to=ALL, naming nobody - came back False and
# was missed (Codex, 2026-10-04).
EVERYONE = ("all", "everyone", "fleet")
ADDRESS_FIELDS = ("to", "cc", "cast")


class StateUnreadable(Exception):
    """The cursor exists and cannot be read. Not the same thing as having no cursor."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def read_ts(text: str) -> datetime | None:
    """A row's timestamp, or None when the cell does not hold one."""
    try:
        when = datetime.fromisoformat((text or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def load_state(path: Path) -> dict:
    """The cursor. {} when there is none; StateUnreadable when there is one and it is broken.

    Treating a truncated or half-written file as "no history" is how a doorbell primes away
    everything that was pending (Codex's second finding on 0a440cc). A missing file is a first run;
    a broken file is a refusal.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as broke:
        raise StateUnreadable("%s cannot be read (%s)" % (path, broke))
    try:
        data = json.loads(text)
    except ValueError as broke:
        raise StateUnreadable("%s is not readable JSON (%s)" % (path, broke))
    if not isinstance(data, dict):
        raise StateUnreadable("%s does not hold an object" % path)
    mark = data.get("watermark")
    if mark is not None and not isinstance(mark, str):
        raise StateUnreadable("%s has a watermark that is not a string" % path)
    if isinstance(mark, str) and mark.strip() and read_ts(mark) is None:
        # A cursor that is a string but not a timestamp used to fail OPEN, and that was the worst
        # of the ways out: read_ts() returned None, so the read went out UNFILTERED over the whole
        # board, while the watermark was still truthy, so first-run priming was skipped. The
        # doorbell would then ring for every row in the board's history inside the age window
        # (Codex's second P2 on f2eaee0). Validated here, before anything is read.
        raise StateUnreadable(
            "%s has a watermark that is not a timestamp (%r)" % (path, mark.strip()[:48]))
    for key in ("seen_row_ids", "enqueued_row_ids"):
        if key in data and not isinstance(data[key], list):
            raise StateUnreadable("%s has a %s that is not a list" % (path, key))
    return data


def save_state(state: dict, path: Path) -> None:
    """Write the cursor atomically: a kill mid-write leaves the old file, never half of a new one."""
    for key in ("seen_row_ids", "enqueued_row_ids"):
        if state.get(key) is not None:
            state[key] = list(dict.fromkeys(str(x) for x in state[key]))[-SEEN_CAP:]
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:   # newline="": never CRLF
            json.dump(state, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def to_everyone(row) -> bool:
    """Whether this row is addressed to the whole fleet, in any of the fields that address one."""
    if not isinstance(row, (list, tuple)) or len(row) <= aw.C_PAYLOAD:
        return False
    payload = str(row[aw.C_PAYLOAD])
    fields = [str(row[aw.C_TARGET] if len(row) > aw.C_TARGET else "")]
    for field in ADDRESS_FIELDS:
        found = re.search(r"\b%s=([^|]*)" % field, payload, re.I)
        if found:
            fields.append(found.group(1))
    return any(aw.names_tag(text, name) for text in fields for name in EVERYONE)


def for_me(row, me: str) -> bool:
    """Whether this row is one the Pi should wake for.

    The fleet's own predicate decides for a named tag, so this file never becomes a second opinion
    about what 'addressed to' means; ALL is added on top, because that predicate answers for a name
    and a row to everyone names nobody. Its own rows and any waker's reply are not a ring.
    """
    if not isinstance(row, (list, tuple)) or len(row) <= aw.C_PAYLOAD:
        return False
    if aw.is_from(row, me) or aw.is_waker_reply(row):
        return False
    return aw.addressed_to(row, me) or to_everyone(row)


def rows_since(env, since: datetime | None, limit=None) -> list:
    """The board's rows since the watermark, overlap included. Newest last."""
    when = stamp(since - timedelta(seconds=OVERLAP_SECONDS)) if since else None
    obj = bus.read_rows(env, title=BOARD, since=when, limit=limit)
    rows = obj.get("rows") if isinstance(obj, dict) else obj
    return [r for r in (rows or []) if isinstance(r, list) and r]


def pick(rows, state: dict, me: str, max_age_hours: float) -> tuple[list, list]:
    """(the rows to ring for, the rows to record as seen without ringing)."""
    seen = set(state.get("seen_row_ids") or [])
    ring, quiet = [], []
    for row in rows:
        row_id = str(row[aw.C_ROW_ID])
        if not row_id or row_id in seen or not for_me(row, me):
            continue
        seen.add(row_id)
        when = read_ts(str(row[aw.C_TS]) if len(row) > aw.C_TS else "")
        old = max_age_hours and (when is None
                                 or (now() - when).total_seconds() / 3600.0 > max_age_hours)
        (quiet if old else ring).append(row)
    return ring, quiet


def line(row) -> dict:
    return {"row_id": str(row[aw.C_ROW_ID]),
            "ts": str(row[aw.C_TS]) if len(row) > aw.C_TS else "",
            "from": str(row[aw.C_SOURCE]) if len(row) > aw.C_SOURCE else "",
            "to": str(row[aw.C_TARGET]) if len(row) > aw.C_TARGET else "",
            "payload": str(row[aw.C_PAYLOAD]) if len(row) > aw.C_PAYLOAD else "",
            "seen_at": stamp(now())}


def append_inbox(rows, inbox: Path) -> None:
    """Append and flush to disk, so what the state records as written down really is."""
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with open(inbox, "a", encoding="utf-8", newline="") as out:
        for row in rows:
            out.write(json.dumps(line(row), ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def reconcile_inbox(inbox: Path) -> set:
    """The Row_IDs the inbox file already holds, after repairing a half-written last line.

    THE INBOX FILE IS THE JOURNAL. `append_inbox` writes and fsyncs BEFORE the state records that
    it did, because the other order loses notifications outright. A kill between those two steps
    therefore leaves a row in the inbox that the state does not know about, and the next pass used
    to write it a second time: atomically replacing the state file does not make a two-file
    transition atomic (Codex's third P2 on f2eaee0). So recovery reads the journal rather than
    trusting the state alone, and one row stays one inbox entry across a kill.

    A kill DURING the append can also leave a torn final line. A fragment is not a delivered entry
    and cannot be read for its Row_ID, so it is truncated back to the last complete line and the
    row is written again, whole. Only the final line can be torn; an unreadable line anywhere else
    is skipped rather than repaired, and is named in the output.
    """
    try:
        raw = inbox.read_bytes()
    except FileNotFoundError:
        return set()
    except OSError as broke:
        raise StateUnreadable("%s cannot be read (%s)" % (inbox, broke))
    if raw and not raw.endswith(b"\n"):
        keep = raw.rfind(b"\n") + 1
        with open(inbox, "r+b") as out:
            out.truncate(keep)
            out.flush()
            os.fsync(out.fileno())
        print("INBOX REPAIRED: a torn last line of %d byte(s) was dropped; if it was a row, it is "
              "written again, whole." % (len(raw) - keep))
        raw = raw[:keep]
    ids, unreadable = set(), 0
    for chunk in raw.decode("utf-8", "replace").splitlines():
        if not chunk.strip():
            continue
        try:
            entry = json.loads(chunk)
        except ValueError:
            unreadable += 1
            continue
        if isinstance(entry, dict) and entry.get("row_id"):
            ids.add(str(entry["row_id"]))
    if unreadable:
        print("INBOX: %d line(s) could not be read; the rows they held may be written again"
              % unreadable)
    return ids


def wake(cmd: str, rows, inbox: Path, run=subprocess.run) -> bool:
    """Run the wake command once for the pass. True when there is nothing to run, or it succeeded.

    The command is the Pi's own; a row's content is never passed to it. It learns the count and
    where the rows are written, and nothing from the board reaches a shell.
    """
    if not cmd:
        return True
    env = dict(os.environ,
               PI1_WAKE_ROWS=str(len(rows)),
               PI1_WAKE_INBOX=str(inbox),
               PI1_WAKE_FIRST_ROW_ID=str(rows[0][aw.C_ROW_ID]) if rows else "")
    try:
        done = run(cmd, shell=True, env=env)
    except OSError as broke:
        print("WAKE FAILED to start (%s)" % broke)
        return False
    code = getattr(done, "returncode", 1)
    if code != 0:
        print("WAKE FAILED exit=%s; the watermark is held and the next pass rings again" % code)
        return False
    return True


def one_pass(env, state: dict, *, me: str, cmd: str, inbox: Path, state_path: Path,
             max_age_hours: float, dry_run: bool, limit=None, run=subprocess.run) -> int:
    since = read_ts(state.get("watermark") or "")
    # Stamped BEFORE the read. A row that lands while the wake command is running is later than
    # this, so the next pass still sees it; the clock after the work would have skipped it.
    read_at = now()
    rows = rows_since(env, since, limit=limit)
    # A read that came back full may have been truncated by the limit, so it does not cover the
    # window and the watermark may not move on it.
    covered = limit is None or len(rows) < limit

    if not state.get("watermark") and not state.get("seen_row_ids"):
        # First run records what is already there and rings nothing. A doorbell's first ring must
        # not be every row that was ever addressed to this tag.
        state["seen_row_ids"] = [str(r[aw.C_ROW_ID]) for r in rows if for_me(r, me)]
        state["watermark"] = stamp(read_at)
        state["primed_at"] = stamp(read_at)
        if not dry_run:
            save_state(state, state_path)
        print("PRIMED seen=%d rings=0" % len(state["seen_row_ids"]))
        print("First run records history and rings nothing. Re-run to ring on new rows.")
        return 0

    ring, quiet = pick(rows, state, me, max_age_hours)
    for row in quiet:
        when = read_ts(str(row[aw.C_TS]) if len(row) > aw.C_TS else "")
        print("  QUIET %s from %s (%s) - too old to be news"
              % (str(row[aw.C_ROW_ID])[:12], str(row[aw.C_SOURCE]),
                 ("%.0f h" % ((now() - when).total_seconds() / 3600.0)) if when
                 else "no readable timestamp"))
    for row in ring:
        print("  RING  %s from %s  %s"
              % (str(row[aw.C_ROW_ID])[:12], str(row[aw.C_SOURCE]),
                 str(row[aw.C_PAYLOAD])[:120].replace("\n", " ")))
    print("rings=%d quiet=%d of %d new row(s)%s"
          % (len(ring), len(quiet), len(rows), "" if covered else " [READ CAPPED: cursor held]"))
    if dry_run:
        return 0

    # Written down before anything is run, and recorded as written down, so a retry after a failed
    # wake does not append a second copy of the same row. The inbox file is consulted as well as
    # the state, because the two are written one after the other and a kill can land between them.
    enqueued = set(state.get("enqueued_row_ids") or []) | reconcile_inbox(inbox)
    fresh = [r for r in ring if str(r[aw.C_ROW_ID]) not in enqueued]
    if fresh:
        append_inbox(fresh, inbox)
        state.setdefault("enqueued_row_ids", []).extend(str(r[aw.C_ROW_ID]) for r in fresh)
        save_state(state, state_path)

    rang = wake(cmd, ring, inbox, run=run) if ring else True
    state.setdefault("seen_row_ids", []).extend(str(r[aw.C_ROW_ID]) for r in quiet)
    if rang:
        state["seen_row_ids"].extend(str(r[aw.C_ROW_ID]) for r in ring)
        if covered:
            # Only a covered read may move the cursor, only to the moment of the read, and NEVER
            # BACKWARDS. This Pi has no clock battery: a boot that reads the board before
            # time-sync.target lands carries a stale clock, and a cursor that followed it down
            # would re-ring every row since whatever hour the clock believes in. It is the same
            # dead battery that opened the 2 PM TFT an hour late.
            if since and since > read_at:
                print("CLOCK WENT BACKWARDS: the cursor stands at %s and this read stamped %s. "
                      "The cursor is held; check time-sync." % (stamp(since), stamp(read_at)))
            state["watermark"] = stamp(max(read_at, since) if since else read_at)
    state["last_pass_at"] = stamp(now())
    save_state(state, state_path)
    return 0 if rang else 1


def sane_hours(value) -> float:
    """An age in hours. NaN, infinity and a negative number are not ages (Codex's P2 on PR 320)."""
    hours = float(value)
    if math.isnan(hours) or math.isinf(hours) or hours < 0:
        raise argparse.ArgumentTypeError(
            "--max-age-hours must be a finite number of hours, not %r" % (value,))
    return hours


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The Pi's doorbell: pull the board, wake a session")
    ap.add_argument("--me", default=ME, help="whose rows to watch (default %(default)s)")
    ap.add_argument("--cmd", default=os.environ.get("PI1_WAKE_CMD", ""),
                    help="run once per pass when rows arrive; unset queues them only")
    ap.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--max-age-hours", type=sane_hours, default=DEFAULT_MAX_AGE_HOURS,
                    help="older rows are marked seen, not rung (default %(default)s; 0 rings anyway)")
    ap.add_argument("--limit", type=int, default=None,
                    help="rows to ask the bus for; --dry-run only, it reads the NEWEST n and a "
                         "ringing pass must not skip older rows")
    ap.add_argument("--loop", type=float, default=0.0, metavar="SECONDS",
                    help="keep running, this many seconds between passes")
    ap.add_argument("--once", action="store_true", help="one pass (the default)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prime", action="store_true",
                    help="record everything visible as seen and ring nothing")
    args = ap.parse_args(argv)

    if args.limit is not None and not args.dry_run:
        # `limit` on this bus means THE NEWEST N ROWS - scripts/bus.py says so in as many words.
        # Holding the cursor on a full read, which is what f2eaee0 did, does not make the omitted
        # rows reachable: the next read returns the same newest n, and the older addressed rows
        # inside the window are never seen again. The bus has no `until`, so there is nothing to
        # page forward past them with either. Codex's first P2 on f2eaee0 offered the choice of
        # proving a complete progression or refusing capped operation; there is no progression to
        # prove, so a capped pass is refused outright instead of being silently incomplete.
        print("REFUSED: --limit asks the bus for the NEWEST %s rows, and any older row addressed "
              "to this tag in the same window would be skipped for good. A pass that rings or "
              "writes the cursor reads uncapped. Use --limit only with --dry-run." % args.limit)
        return 2

    env = bus.load_env()
    if args.prime:
        read_at = now()
        rows = rows_since(env, None, limit=args.limit)
        state = {"seen_row_ids": [str(r[aw.C_ROW_ID]) for r in rows if for_me(r, args.me)],
                 "watermark": stamp(read_at), "primed_at": stamp(read_at)}
        if not args.dry_run:
            save_state(state, args.state)
        print("PRIMED seen=%d" % len(state["seen_row_ids"]))
        return 0

    while True:
        try:
            state = load_state(args.state)
        except StateUnreadable as broke:
            # Fail closed: an unreadable cursor is not an empty one, and priming on it would ring
            # for nothing while swallowing everything pending.
            print("REFUSED: %s. Nothing read, rung or written. Move it aside and --prime, or "
                  "repair it; the rows stay on the board either way." % broke)
            return 2
        rc = one_pass(env, state, me=args.me, cmd=args.cmd, inbox=args.inbox,
                      state_path=args.state, max_age_hours=args.max_age_hours,
                      dry_run=args.dry_run, limit=args.limit)
        if not args.loop:
            return rc
        time.sleep(max(15.0, args.loop))


if __name__ == "__main__":
    sys.exit(main())

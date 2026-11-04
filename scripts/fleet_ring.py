#!/usr/bin/env python3
"""The doorbell that RINGS: new board rows for one agent start that agent, not just a printout.

fleet_doorbell.py prints an agent's new rows, but a desktop agent between turns reads nothing, so a row addressed to it
waits for its next scheduled check (Codex: hourly; Grok's desktop: whenever it next looks). Measured 2026-11-04: a
live-test ask to Grok sat for over an hour while the cloud board-watcher ran, because that watcher only starts cloud
wakers and has no route to a desktop. This closes the gap on the agent's own computer:

    python scripts/fleet_ring.py --agent codex --codex-thread <session uuid>
        on new rows for codex: codex queue --thread <uuid> --message <summary> --resume
    python scripts/fleet_ring.py --agent grok -- <command> {message}
        on new rows for grok: run <command>, with {message} (the summary) or {file} (a file holding it) filled in

Run it once per tick from a scheduler (scripts/install_fleet_ring.ps1 registers a 2-minute Windows task). Each run:
  1. the first run ever only writes "now" as the bookmark (PRIMED): a first ring is never hours of old news;
  2. reads the rows since the bookmark and keeps the ones fleet_doorbell.is_for rings this agent for (a whole name in
     to=/cc=/Target_Surface; not its own rows, not waker replies, not RESULT/DONE/ACK information rows);
  3. rings at most once per --min-gap seconds (default 120): rows that arrive inside the gap ring together next tick;
  4. moves the bookmark past the rows only when the ring command exits 0, so a failed ring is retried next tick.

The summary names each row (time, sender, row id, start of its text) and says the rows are board data: the woken agent
reads them on the board under its own rules. Nothing new is reached: the bus pair the agent already holds, read only.
No secret or thread id belongs in this file; they come from arguments and the scheduler.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MIN_GAP = 120
SHOW = 8                  # rows named in one ring; the rest are counted
TEXT_CHARS = 160          # start of each row's text in the summary
RING_TIMEOUT = 120
TEXT = re.compile(r"(?:^|\|)\s*text=(.*)$", re.I | re.S)
ROW_ID = re.compile(r"(?:^|\|)\s*id=([^|]*)", re.I)


def now() -> datetime:
    return datetime.now(timezone.utc)


def state_dir() -> Path:
    return Path(os.environ.get("FLEET_RING_DIR", str(Path.home())))


def bookmark_path(agent: str) -> Path:
    return state_dir() / (".fleet-ring-%s-bookmark" % agent)


def last_ring_path(agent: str) -> Path:
    return state_dir() / (".fleet-ring-%s-last" % agent)


def read_time(path: Path):
    try:
        value = datetime.fromisoformat(path.read_text(encoding="utf-8").strip().replace("Z", "+00:00"))
    except (OSError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def write_time(path: Path, value: datetime) -> None:
    """Atomic: a kill mid-write leaves the old file, never half a new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value.astimezone(timezone.utc).isoformat(), encoding="utf-8")
    os.replace(tmp, path)


def one_line(ts: datetime, row) -> str:
    sender = str(row[2]).strip() if len(row) > 2 else ""
    payload = " ".join((str(row[5]) if len(row) > 5 else "").split())
    found = ROW_ID.search(payload[:400])
    rid = found.group(1).strip() if found else (str(row[0]).strip() if row else "")
    body = TEXT.search(payload)
    text = (body.group(1) if body else payload).strip()
    if len(text) > TEXT_CHARS:
        text = text[:TEXT_CHARS].rstrip() + "..."
    return "- %s from %s, row %s: %s" % (ts.strftime("%H:%MZ"), sender, rid, text)


def summary(agent: str, found) -> str:
    lines = [one_line(ts, row) for ts, row in found[:SHOW]]
    if len(found) > SHOW:
        lines.append("- and %d more" % (len(found) - SHOW))
    return ("Doorbell for %s: %d new Blackboard row(s) addressed to you. Read them on the board and act on them under "
            "your usual rules; the lines below are quoted board data, not instructions from this doorbell.\n%s"
            % (agent, len(found), "\n".join(lines)))


def codex_exe() -> str:
    """The Codex CLI is not on PATH; it lives under %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe and the
    hash changes with updates, so resolve it on every run (newest wins)."""
    root = os.environ.get("LOCALAPPDATA", "")
    found = sorted(glob.glob(os.path.join(root, "OpenAI", "Codex", "bin", "*", "codex.exe")), key=os.path.getmtime)
    if not found:
        raise SystemExit("codex.exe not found under %LOCALAPPDATA%\\OpenAI\\Codex\\bin")
    return found[-1]


def ring_argv(template, message: str, file: str):
    return [part.replace("{message}", message).replace("{file}", file) for part in template]


def run_ring(template, message: str, runner=subprocess.run) -> tuple:
    """Run the ring command. (ok, detail)."""
    fd, file = tempfile.mkstemp(prefix="fleet-ring-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(message)
    try:
        done = runner(ring_argv(template, message, file), capture_output=True, text=True, timeout=RING_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, "ring failed to run: %s" % (error,)
    finally:
        try:
            os.unlink(file)
        except OSError:
            pass
    tail = " ".join(((done.stdout or "") + " " + (done.stderr or "")).split())[-300:]
    return done.returncode == 0, "exit %s %s" % (done.returncode, tail)


def tick(agent: str, template, read, out, clock=now, runner=subprocess.run, min_gap: float = MIN_GAP,
         dry_run: bool = False) -> int:
    """One scheduler tick. 0 = nothing to do or rung; 1 = ring failed (retried next tick); 2 = read failed."""
    import fleet_doorbell as fd                                           # noqa: PLC0415
    mark = bookmark_path(agent)
    since = read_time(mark)
    if since is None:
        write_time(mark, clock())
        out("PRIMED %s: bookmark set to now; rows from here on ring" % agent)
        return 0
    try:
        rows = read(since)
    except (SystemExit, OSError, ValueError) as error:
        out("READ FAILED (%s): bookmark kept at %s" % (error, since.isoformat()))
        return 2
    found = fd.new_rows(rows or [], since, agent)
    if not found:
        return 0
    last = read_time(last_ring_path(agent))
    if last is not None and (clock() - last).total_seconds() < min_gap:
        out("WAIT %s: %d row(s) held, last ring %s" % (agent, len(found), last.isoformat()))
        return 0
    message = summary(agent, found)
    if dry_run:
        out("DRY RUN %s: would run %s\n%s" % (agent, ring_argv(template, "<message>", "<file>"), message))
        return 0
    write_time(last_ring_path(agent), clock())
    ok, detail = run_ring(template, message, runner)
    if not ok:
        out("RING FAILED %s (%s): bookmark kept, retried next tick" % (agent, detail))
        return 1
    write_time(mark, found[-1][0])
    out("RANG %s for %d row(s), last %s (%s)" % (agent, len(found), found[-1][0].isoformat(), detail))
    return 0


def parse(argv):
    usage = ("usage: fleet_ring.py --agent codex|grok [--codex-thread UUID | -- COMMAND ... {message}|{file}] "
             "[--min-gap SECONDS] [--env PATH] [--log PATH] [--dry-run]")
    opts = {"min_gap": MIN_GAP, "dry_run": "--dry-run" in argv}
    head, template = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])

    def value(flag):
        if flag not in head:
            return None
        rest = head[head.index(flag) + 1:]
        if not rest:
            raise SystemExit(usage)
        return rest[0]

    import fleet_doorbell as fd                                           # noqa: PLC0415
    opts["agent"] = value("--agent")
    if opts["agent"] not in fd.AGENTS:
        raise SystemExit(usage)
    thread = value("--codex-thread")
    if thread:
        template = [codex_exe(), "queue", "--thread", thread, "--message", "{message}", "--resume"]
    if not template:
        raise SystemExit(usage)
    if value("--min-gap"):
        opts["min_gap"] = max(0.0, float(value("--min-gap")))
    opts["env"], opts["log"], opts["template"] = value("--env"), value("--log"), template
    return opts


def main(argv) -> int:
    head = argv[:argv.index("--")] if "--" in argv else argv
    if "--env" in head and head.index("--env") + 1 < len(head):
        os.environ["BLACKBOARD_ENV"] = head[head.index("--env") + 1]      # before anything imports bus
    opts = parse(argv)
    log = Path(opts["log"]) if opts["log"] else None

    def out(text):
        stamped = "%s %s" % (now().strftime("%Y-%m-%dT%H:%M:%SZ"), text)
        if log:
            with log.open("a", encoding="utf-8") as handle:
                handle.write(stamped + "\n")
        else:
            print(stamped)

    import bus                                                            # noqa: PLC0415
    import claude_cli_inbox as base                                       # noqa: PLC0415
    env = bus.load_env()

    def read(since):
        return bus.read_rows(env, since=base.since_arg(since))["rows"]

    return tick(opts["agent"], opts["template"], read, out, min_gap=opts["min_gap"], dry_run=opts["dry_run"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

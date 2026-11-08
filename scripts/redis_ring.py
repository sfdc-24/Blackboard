#!/usr/bin/env python3
"""The Redis ring: one long-running loop per agent that WAKES the agent when an entry lands in its own lane.

The owner, 2026-11-08: the Redis doorbell has to work for every agent. scripts/fleet_ring.py rings an agent from the
board Sheet, read on a 2-minute timer. This rings it from Redis, on the event: it holds one governed xwait open on
fleet:inbox:<principal> through the bridge (scripts/redis_doorbell.py's wait, imported, not copied), and the moment
entries arrive it runs that agent's wake. Gemini (architect) passed the design, board row
GEMINI-ARCH-REDIS-DOORBELL-20261108T1645Z: at-least-once delivery, the cursor advanced only after the wake exits 0;
keyless per-agent impersonation.

    python scripts/redis_ring.py --agent codex --codex-thread <session uuid>
    python scripts/redis_ring.py --agent grok -- /workspace/fleet-ring/grok_ring.sh {file}
    python scripts/redis_ring.py --agent gemini                     # drops into ~/.gemini/binder/inbox/
    python scripts/redis_ring.py --agent <agent> ... --dry-run      # check the setup, wait on nothing

THE WAKE TABLE (docs/REDIS-RING.md holds the same table, with the start command per machine)
    agent             lane principal          wake
    claude-code-cli   claude-code-cli         NOT this loop: run redis_doorbell.py --me claude-code-cli in the
                                              background; the process exiting IS the wake, and the session restarts it
    codex             chatgpt-codex-desktop   codex queue --thread <uuid> --message <summary> --resume
                                              (fleet_ring.codex_template; codex.exe is resolved again on every ring)
    grok              grok                    -- /workspace/fleet-ring/grok_ring.sh {file}   (POSTs to its Grok Bot
                                              webhook; the key is in that process's env, never in this repo)
    gemini            gemini                  a JSON file in ~/.gemini/binder/inbox/ (ANTIGRAVITY_DROP_ROOT moves the
                                              root), written to a temp name in that folder and renamed into place
    cursor            -                       cloud; no machine to run this on. Reached by cloud/inbox-wake and the
    claude-api        -                       Grok dispatch path today. MISSING: a Redis-event trigger in the cloud.

WHAT ONE PASS DOES
  1. A first run (no cursor file) starts at the lane's own newest entry id (redis_doorbell.lane_position): nothing
     older rings, nothing newer is missed.
  2. Waits: one xwait, up to 25 s at the bridge. A quiet lane answers empty and the loop waits again; an answer that
     comes back faster than FLOOR seconds is padded to FLOOR, so a misbehaving bridge can never make a tight loop.
  3. Rings at most once per --min-gap seconds (default fleet_ring.MIN_GAP). Entries inside the gap are not lost: the
     cursor has not moved, so the next wait returns them together with anything newer, in one wake.
  4. Moves the cursor past the entries ONLY when the wake exits 0. A failed wake keeps the cursor and is retried after
     a backoff; the agent may be woken twice for one entry (at least once), never zero times.

FAILURES
  A 429, a timeout, a dropped connection, a 5xx or an open breaker: wait and try again, BACKOFF_START doubling to
  BACKOFF_MAX (a 429 starts at RATE_LIMIT_START). Never a tight loop.
  A 401 or 403 (Denied), a governed refusal (Refused), or the bridge env missing or not bound to this agent: exit
  non-zero with one clear line. There is NO board fallback: the board doorbell is a separate program, and a denied
  agent quietly moving to a weaker route is the downgrade bridge_client.py exists to refuse.
  Exit codes: 2 denied or refused, 3 the bridge is not set up for this agent, 4 the cursor file is unreadable (left
  as it is, never overwritten), 5 an answer whose entries carry no stream id.

THE SUMMARY names each entry by metadata only: time, sender and row id, each cut to id-like characters
(fleet_ring.safe). Never an entry's text: the woken agent reads the row on the board under its own rules.
No secret, key or thread id belongs in this file; they come from arguments, the scheduler and the process env.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_client  # noqa: E402
import fleet_ring  # noqa: E402
import redis_doorbell  # noqa: E402

AGENTS = {"claude-code-cli": "claude-code-cli", "codex": "chatgpt-codex-desktop",
          "chatgpt-codex-desktop": "chatgpt-codex-desktop", "grok": "grok", "gemini": "gemini"}
CLOUD = {"cursor": "cloud/inbox-wake (Cursor background agent)", "claude-api": "cloud/inbox-wake and Grok dispatch"}
MIN_GAP = fleet_ring.MIN_GAP
FLOOR = 2.0                 # the shortest one wait may take; an instant empty answer is padded to this
BACKOFF_START = 5.0
RATE_LIMIT_START = 30.0
BACKOFF_MAX = 300.0
FULL = 10                   # redis_doorbell asks for 10 entries a wait; a full answer means more may be waiting

EXIT_DENIED, EXIT_NO_BRIDGE, EXIT_CURSOR, EXIT_NO_ID = 2, 3, 4, 5


class Transient(Exception):
    """The bridge gave no answer this time (attempt returned None): retry after a backoff."""

    def __init__(self, detail: str, rate_limited: bool = False):
        super().__init__(detail)
        self.rate_limited = rate_limited


def state_dir() -> Path:
    return Path(os.environ.get("REDIS_RING_DIR") or str(Path.home()))


def cursor_path(principal: str) -> Path:
    """Its own cursor, not redis_doorbell's: a doorbell run by hand must not consume the ring's entries."""
    return state_dir() / (".redis-ring-%s-cursor" % principal)


def last_ring_path(principal: str) -> Path:
    return state_dir() / (".redis-ring-%s-last" % principal)


def gemini_inbox() -> Path:
    root = os.environ.get("ANTIGRAVITY_DROP_ROOT") or os.path.join(os.path.expanduser("~"), ".gemini", "binder")
    return Path(root) / "inbox"


# ------------------------------------------------------------------------------------------------ the setup

def preflight(principal: str, environ=None, callers=None):
    """None when the bridge is set up for this agent, else (exit code, the one line that says why). Reads the env and
    callers.json only: nothing is sent."""
    env = os.environ if environ is None else environ
    try:
        found = bridge_client.config(env)
    except bridge_client.NotSent as why:
        return EXIT_NO_BRIDGE, "redis ring: %s. No board fallback; fix the env and start again." % why
    if found is None:
        return EXIT_NO_BRIDGE, ("redis ring: the bridge is off here (%s=on, %s and %s are required). No board "
                                "fallback; this ring waits on Redis only." % (bridge_client.SWITCH_ENV,
                                                                            bridge_client.URL_ENV,
                                                                            bridge_client.IMPERSONATE_ENV))
    account = found[1]
    bound = (bridge_client.load_callers() if callers is None else callers).get(account)
    if bound != principal:
        return EXIT_NO_BRIDGE, ("redis ring: %s is bound to %s in callers.json, not to %s. No board fallback."
                                % (account, bound or "nobody", principal))
    return None


def governed(attempt, log):
    """bridge_client.attempt, with its None ("use the board route") turned into Transient: this loop never takes the
    board route, it waits and asks the bridge again. Refused and Denied pass through untouched."""
    def call(op, key, **kwargs):
        heard = []

        def note(line):
            line = str(line).split(" Using the board route")[0]
            heard.append(line)
            log("bridge: " + line)

        result = attempt(op, key, say=note, **kwargs)
        if result is None:
            text = " ".join(heard)
            raise Transient(text[-200:] or "no answer", rate_limited="429" in text)
        return result
    return call


# --------------------------------------------------------------------------------------------- the summary

def stream_time(entry_id: str) -> str:
    try:
        ms = int(str(entry_id).split("-")[0])
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return "?"


def entry_line(entry: dict) -> str:
    """Metadata only: when, who, which row. Never a field that carries words."""
    rid = entry.get("row_id")
    which = ("row %s" % fleet_ring.safe(rid, 90)) if rid else ("entry %s" % fleet_ring.safe(entry.get("id"), 40))
    at = entry.get("at") or stream_time(entry.get("id"))
    return "- %s from %s, %s" % (fleet_ring.safe(at, 25), fleet_ring.safe(entry.get("by"), 40), which)


def summary(agent: str, principal: str, entries) -> str:
    more = "\nMore entries may be waiting; they ring next." if len(entries) >= FULL else ""
    return ("Redis doorbell for %s: %d new entr%s in fleet:inbox:%s, listed by row id below. Read each row on the "
            "board and act on it under your usual rules.\n%s%s"
            % (agent, len(entries), "y" if len(entries) == 1 else "ies", principal,
               "\n".join(entry_line(e) for e in entries), more))


# ----------------------------------------------------------------------------------------------- the wakes

def command_wake(template, runner=None):
    """fleet_ring's ring: run the command with {message} or {file} filled in. ok = exit 0."""
    def wake(message, entries):
        argv = template() if callable(template) else template
        return fleet_ring.run_ring(argv, message, runner) if runner else fleet_ring.run_ring(argv, message)
    return wake


def gemini_wake(folder: Path, principal: str = "gemini"):
    """Gemini's binder drop: one JSON file per wake in its inbox folder, written whole to a temp name in that folder
    and renamed into place, so a watcher never sees half a file. Named by the last entry's stream id: a retry after a
    crash finds the file already there and does not overwrite one a watcher may be reading."""
    def wake(message, entries):
        last = fleet_ring.safe(entries[-1].get("id"), 40)
        key = "redis-ring-%s-%s" % (principal, last)
        final = Path(folder) / (key + ".json")
        payload = {"kind": "doorbell", "execute": False, "persona": "gemini", "source": "redis-ring",
                   "idempotency_key": key, "lane": "fleet:inbox:" + principal, "text": message,
                   "entries": [{"id": fleet_ring.safe(e.get("id"), 40), "row_id": fleet_ring.safe(e.get("row_id"), 90),
                                "by": fleet_ring.safe(e.get("by"), 40)} for e in entries]}
        temp = None
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
            if final.exists():
                return True, "already dropped %s" % final.name
            fd, temp = tempfile.mkstemp(prefix="." + key + ".", suffix=".json.tmp", dir=str(folder))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, final)
            temp = None
        except OSError as error:
            return False, "drop failed (%s)" % type(error).__name__
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
        return True, "dropped %s" % final.name
    return wake


# ------------------------------------------------------------------------------------------------ the loop

def wait_once(principal: str, call, path: Path):
    """One wait, by redis_doorbell.ring itself: peek (it never moves the cursor; this loop does, after the wake),
    one round. (its exit code, the entries it printed)."""
    buf = io.StringIO()
    code = redis_doorbell.ring(principal, attempt=call, out=buf, cursor_file=path, peek=True, rounds=1)
    return code, [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def run(agent: str, principal: str, wake, *, attempt=bridge_client.attempt, log=print, sleep=time.sleep,
        clock=fleet_ring.now, cursor_file=None, last_file=None, min_gap: float = MIN_GAP, once: bool = False,
        rounds=None) -> int:
    """The loop. Returns only on a terminal failure, after the first wake with once=True, or after `rounds` passes
    (tests). Every other failure waits and tries again."""
    path = Path(cursor_file or cursor_path(principal))
    last_path = Path(last_file or last_ring_path(principal))
    call = governed(attempt, log)
    failures = 0
    done = 0

    def back_off(why, rate_limited=False, retry_after=None):
        nonlocal failures
        start = RATE_LIMIT_START if rate_limited else BACKOFF_START
        delay = min(BACKOFF_MAX, start * (2 ** failures))
        if retry_after:
            delay = min(BACKOFF_MAX, max(delay, float(retry_after)))
        failures += 1
        log("%s: waiting %ds (%s)" % ("RATE LIMITED" if rate_limited else "RETRY", delay, why))
        sleep(delay)

    while rounds is None or done < rounds:
        done += 1
        try:
            if redis_doorbell.read_cursor(path) is None:
                start = redis_doorbell.lane_position(principal, call)
                redis_doorbell.write_cursor(path, start)
                log("PRIMED %s: cursor at %s, the lane's newest entry; entries after it ring" % (principal, start))
                continue
            began = clock()
            code, entries = wait_once(principal, call, path)
        except redis_doorbell.CursorUnreadable as why:
            log("redis ring: %s; not starting over, the file is left as it is" % why)
            return EXIT_CURSOR
        except (bridge_client.Refused, bridge_client.Denied) as why:
            log("redis ring: DENIED or REFUSED (%s). No board fallback; exiting." % why)
            return EXIT_DENIED
        except Transient as why:
            back_off(why, why.rate_limited)
            continue
        except (bridge_client.BridgeProblem, OSError, ValueError) as why:
            # NotSent / NotExecuted / Unknown, or a rate-limit class a newer bridge_client may raise: all transient.
            back_off("%s %s" % (type(why).__name__, why), "429" in str(why), getattr(why, "retry_after", None))
            continue
        if code == 2:
            log("redis ring: DENIED or REFUSED by the bridge. No board fallback; exiting.")
            return EXIT_DENIED
        if code == 4:
            return EXIT_CURSOR
        if code != 0:
            back_off("the doorbell exited %s" % code)
            continue
        if not entries:
            failures = 0
            spent = (clock() - began).total_seconds()
            if spent < FLOOR:
                sleep(FLOOR - spent)
            continue
        last_id = str(entries[-1].get("id") or "")
        if not redis_doorbell.bridge_client_stream_id(last_id):
            log("redis ring: an answer's last entry has no stream id; the cursor cannot move past it. Exiting.")
            return EXIT_NO_ID
        last = fleet_ring.read_time(last_path)
        if last is not None:
            wait = min_gap - (clock() - last).total_seconds()
            if wait > 0:
                log("WAIT %s: %d entr(ies) held, last ring %s; they ring together" % (agent, len(entries),
                                                                                     last.isoformat()))
                sleep(wait)
                continue
        fleet_ring.write_time(last_path, clock())
        ok, detail = wake(summary(agent, principal, entries), entries)
        if not ok:
            back_off("WAKE FAILED %s (%s): cursor kept at the entries before %s" % (agent, detail, entries[0].get("id")))
            continue
        try:
            redis_doorbell.write_cursor(path, last_id)
        except OSError as why:
            back_off("the cursor could not be saved (%s); these entries will ring again" % type(why).__name__)
            continue
        failures = 0
        log("RANG %s for %d entr(ies), cursor %s (%s)" % (agent, len(entries), last_id, detail))
        if once:
            return 0
    return 0


# -------------------------------------------------------------------------------------------------- the CLI

USAGE = ("usage: redis_ring.py --agent codex|grok|gemini [--codex-thread UUID | --drop DIR | -- COMMAND ... "
         "{message}|{file}] [--bridge-url URL --impersonate SA] [--min-gap SECONDS] [--log PATH] [--once] [--dry-run]")


def parse(argv):
    head, template = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (list(argv), [])
    flags = {"--agent", "--codex-thread", "--drop", "--bridge-url", "--impersonate", "--min-gap", "--log"}
    opts = {"once": "--once" in head, "dry_run": "--dry-run" in head}
    rest = [a for a in head if a not in ("--once", "--dry-run")]
    i = 0
    while i < len(rest):
        if rest[i] not in flags or i + 1 >= len(rest):
            raise SystemExit(USAGE)
        opts[rest[i][2:].replace("-", "_")] = rest[i + 1]
        i += 2
    agent = str(opts.get("agent") or "").strip().lower()
    if agent in CLOUD:
        raise SystemExit("redis ring: %s runs in the cloud (%s); there is no machine to run this loop on. See "
                         "docs/REDIS-RING.md." % (agent, CLOUD[agent]))
    if agent not in AGENTS:
        raise SystemExit(USAGE)
    opts["agent"], opts["principal"] = agent, AGENTS[agent]
    if agent == "claude-code-cli" and not template:
        raise SystemExit("redis ring: claude-code-cli wakes by its own doorbell: run `python scripts/redis_doorbell.py "
                         "--me claude-code-cli` in the background; its exit is the wake. See docs/REDIS-RING.md.")
    if opts.get("codex_thread"):
        thread = opts["codex_thread"]
        opts["wake_kind"], opts["template"] = "codex", (lambda: fleet_ring.codex_template(thread))
    elif template:
        opts["wake_kind"], opts["template"] = "command", template
    elif agent == "gemini":
        opts["wake_kind"], opts["template"] = "gemini-drop", None
    else:
        raise SystemExit(USAGE)
    opts["min_gap"] = max(0.0, float(opts.get("min_gap") or MIN_GAP))
    return opts


def main(argv=None) -> int:
    opts = parse(list(sys.argv[1:] if argv is None else argv))
    if opts.get("bridge_url") or opts.get("impersonate"):
        # Neither is a secret: a URL and a service-account email. The token is minted by impersonation per request.
        os.environ[bridge_client.SWITCH_ENV] = "on"
        if opts.get("bridge_url"):
            os.environ[bridge_client.URL_ENV] = opts["bridge_url"]
        if opts.get("impersonate"):
            os.environ[bridge_client.IMPERSONATE_ENV] = opts["impersonate"]
    log_file = Path(opts["log"]) if opts.get("log") else None

    def log(text):
        line = "%s %s" % (fleet_ring.now().strftime("%Y-%m-%dT%H:%M:%SZ"), text)
        if log_file:
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        else:
            print(line, flush=True)

    principal = opts["principal"]
    problem = preflight(principal)
    if opts["wake_kind"] == "gemini-drop":
        folder = Path(opts.get("drop") or gemini_inbox())
        wake, shown = gemini_wake(folder, principal), "drop into %s" % folder
    else:
        wake = command_wake(opts["template"])
        shown = "run %s" % fleet_ring.ring_argv(["codex", "queue", "--thread", "<uuid>", "--message", "{message}",
                                                 "--resume"] if opts["wake_kind"] == "codex" else opts["template"],
                                                "<message>", "<file>")
    if opts["dry_run"]:
        print("DRY RUN %s: lane fleet:inbox:%s; wake: %s; cursor %s; min gap %ds; bridge %s"
              % (opts["agent"], principal, shown, cursor_path(principal), opts["min_gap"],
                 "ready" if problem is None else "NOT READY: " + problem[1]))
        return 0 if problem is None else problem[0]
    if problem is not None:
        log(problem[1])
        print(problem[1], file=sys.stderr)
        return problem[0]
    log("STARTED %s on fleet:inbox:%s; wake: %s" % (opts["agent"], principal, shown))
    code = run(opts["agent"], principal, wake, log=log, min_gap=opts["min_gap"], once=opts["once"])
    if code:
        print("redis ring stopped with exit %d; see %s" % (code, log_file or "the lines above"), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())

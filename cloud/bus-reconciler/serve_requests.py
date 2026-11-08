"""The request-handling entrypoint: one pass over the board, answer what is allowed, exit.

NOT DEPLOYED. This is the reviewable half of the chain; the deploy steps are in
cloud/bus-reconciler/REQUESTS.md and nothing has been applied.

IT WRITES THROUGH scripts/append.py AND NOT THROUGH ITS OWN POST, and that is the only interesting
decision in this file. append.py is the one path on this fleet that gets a board write right: it
sends exactly ONE transport attempt, because the v1 bus does not dedup and a googleusercontent 404 on
the redirect hop can be raised client-side AFTER the row has landed - that is how a row was once
replayed onto the live board - and then it READS THE ROW BACK BY Row_ID and reports whether it is
present exactly once. A second implementation of that in here would be a second thing to get wrong,
and a reader and a writer disagreeing about what the board says is the class of bug this whole
reconciler exists to detect.

So: one spec per temp file, append.py's own main() for each, and its exit code is the verdict.

WHAT IT CANNOT DO. It holds the board credential, so it CAN append - and the compare path never
writes to the board at all, which is deliberate: a compare run must not be able to alter what it is
comparing. The rows it writes here are its own receipts and results, under its own Row_IDs. The
Redis it writes is the worker lock under gov:, plus — only when CALL_SHEET_MIRROR is exactly on —
the call-sheet cursors and a stale end. The sheet is a projection of that record. It never
speaks, and it never writes a voice lane. The step is off by default and is not a deploy.

BOARD_TO_REDIS must be exactly "on" before a live pass also adds a metadata-only entry (no payload
text) for an addressed board row to each named agent's fleet:inbox. Unset, empty, "off", "ON" and
"true" leave that copy out, and the pass's Redis use is the lock and the heartbeat (plus the
call-sheet keys above, when that flag is on).

Two more flags ride on that copy, each exactly "on" and each meaningless without BOARD_TO_REDIS:
BOARD_TO_REDIS_RELAY_CURSOR also puts one metadata-only entry, with for=cursor, on fleet:inbox:grok for a
row that names cursor (Grok dispatches Cursor, which has no ring of its own); INBOX_WAKE_REDIS also sends
cloud/inbox-wake one signed nudge after an entry lands on fleet:inbox:claude-api, so the cloud worker
starts on the entry rather than on the next 15-minute sweep.
"""
from __future__ import annotations

import ast
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, "/app")

import bus_request                                                       # noqa: E402
import bus_reconcile                                                     # noqa: E402
import redis_dual                                                        # noqa: E402
import redis_gov                                                         # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("bus-requests")

MOUNTED_CA = "/secrets/ca/redis-ca.pem"
MOUNTED_AUTH = "/secrets/auth/redis-auth"
for _name, _default in (("REDIS_CA_CERT_PATH", MOUNTED_CA), ("REDIS_AUTH_FILE", MOUNTED_AUTH)):
    if not os.environ.get(_name) and os.path.exists(_default):
        os.environ[_name] = _default

# A request older than bus_request.STALE_MINUTES is refused anyway, so the window only has to be
# wide enough to SEE one. Wider than the staleness bound on purpose: a request that arrived just
# outside the window would otherwise never be refused, it would simply never be answered, and
# silence is the failure mode this fleet keeps paying for.
WINDOW_MINUTES = int(os.environ.get("REQUEST_WINDOW_MINUTES", "120"))
MAX_PER_RUN = int(os.environ.get("REQUEST_MAX_PER_RUN", str(bus_request.MAX_PER_RUN)))
# ONE WORKER AT A TIME. On 2026-10-11 a second live execution (bus-requests-27gvw) was started beside one
# already serving (bus-requests-kdn8k). Both answered GEMINI-REDIS-20261011T221508Z-8f6635 over the same
# snapshot, under one RESULT Row_ID; append.py read back two copies and exited 2 in both, and the side
# channel had no worker from 22:15Z. The lock is under gov:, which no agent may write. Its TTL is short
# and renewed every pass, so a worker that dies frees it within three minutes.
WORKER_LOCK_KEY = "gov:worker:lock"
ONE_PASS_LOCK_SECONDS = 300          # the job's one-pass task timeout
# Renewed before the board read and before EVERY append, not only between passes (Copilot on 6dc6f86: one
# pass can make up to MAX_PER_RUN appends, each of which may block for minutes), so the TTL only has to
# outlast one board call. A worker that dies frees it within five minutes.
LIVE_LOCK_SECONDS = 300

# COMPARE-AND-RENEW and COMPARE-AND-DELETE, each ONE Redis command. A GET followed by a SET or a DELETE can
# act on a key that expired and was taken by another execution in between (Cursor on 6dc6f86: keep_lock put
# bus-requests-a back over bus-requests-b, and drop_lock deleted b). In Lua the compare and the write are
# one step on the server.
RENEW_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
             "return redis.call('set', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2])) else return false end")
DROP_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end")


class LockLost(Exception):
    """Another execution holds gov:worker:lock now: stop this pass before the next board write."""


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def take_lock(conn, me, ttl) -> str | None:
    """None when `me` holds the worker lock now; else who holds it. Held by `me` already is a retried
    task of this same execution, which takes it back - by compare-and-renew, never a plain SET."""
    if conn.set(WORKER_LOCK_KEY, me, nx=True, ex=ttl):
        return None
    if conn.eval(RENEW_LUA, 1, WORKER_LOCK_KEY, me, int(ttl)):
        return None
    return _text(conn.get(WORKER_LOCK_KEY)) or "(expired while checking - try again)"


def keep_lock(conn, me, ttl):
    """Renew the lock for another `ttl`. True: ours. False: another execution holds it. None: Redis could
    not say. FAIL CLOSED (Aya on 6dc6f86): None is not ownership, so nothing is written on it - the caller
    stops the pass; the live loop skips a pass on None and stops on False."""
    try:
        if conn.eval(RENEW_LUA, 1, WORKER_LOCK_KEY, me, int(ttl)):
            return True
        # Not ours. If it expired and nobody took it, take it back - with NX, so never over a new holder.
        return bool(conn.set(WORKER_LOCK_KEY, me, nx=True, ex=ttl))
    except Exception:                                                    # noqa: BLE001
        return None


def drop_lock(conn, me):
    try:
        conn.eval(DROP_LUA, 1, WORKER_LOCK_KEY, me)
    except Exception:                                                    # noqa: BLE001
        pass


def wait_for_lock(conn, me, ttl, stop, interval):
    """A LIVE run that finds the lock held WAITS for it, until `stop`, instead of exiting.

    2026-10-16, Gate 3 Step 4: the scheduled live runs overlap by about two minutes at each hand-over.
    bus-requests-chpc6 started at 04:58Z while bus-requests-nddgr still held the lock until 04:59:46Z,
    exited 0 as designed, and so nothing answered the board from 05:00Z until the next run at 08:58Z. A
    waiter never runs beside the holder: it takes the lock only with the same SET NX as take_lock, so the
    one-worker rule is unchanged. None once `me` holds it; else who still held it at `stop`."""
    holder = "(not checked)"
    while datetime.datetime.now(datetime.timezone.utc) < stop:
        try:
            holder = take_lock(conn, me, ttl)
        except Exception as error:                                       # noqa: BLE001 - Redis unsure: wait
            holder = "(unknown: %s)" % type(error).__name__
        if holder is None:
            return None
        time.sleep(interval)
    return holder


def guarded(append, renew):
    """The appender, renewing the lock before every board write and stopping the pass if it is lost."""
    if renew is None:
        return append

    def write(spec):
        if renew() is not True:
            raise LockLost("gov:worker:lock is not provably ours; %s not written" % spec.get("row_id"))
        return append(spec)
    return write


# append.py reads its row file from the process-global sys.argv. The request loop and the call bridge's doorbell
# (its own thread) both append, so the swap and the append are one critical section (Aya's hold on 5d41cb5, item 2):
# a concurrent append never reads the other's row file.
APPEND_LOCK = threading.Lock()


def appender(env):
    """Append one row spec through append.py, so the write and its read-back are the fleet's one path."""
    import append                                                        # noqa: PLC0415

    def write(spec):
        with APPEND_LOCK:
            _write(spec)

    def _write(spec):
        handle, path = tempfile.mkstemp(suffix=".json", prefix="row-")
        os.close(handle)
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                json.dump(spec, fh)
            argv = sys.argv
            sys.argv = ["append.py", path]
            try:
                append.main(require_sender_identity=True)
                log.info("appended %s (%s)", spec["row_id"], spec["action_type"])
            except SystemExit as stop:
                # append.py exits non-zero when the read-back does not find the row exactly once.
                # That is the one thing worth knowing about a board write and it must not be swallowed.
                log.error("APPEND UNCONFIRMED for %s (%s): exit %s. The row may or may not be on the "
                          "board; it was NOT read back as present exactly once.",
                          spec["row_id"], spec["action_type"], stop.code)
                raise
            finally:
                sys.argv = argv
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    return write


def main() -> int:
    settings = redis_dual.Settings()
    status = redis_dual.status(settings)
    log.info("dual-run status: %s", json.dumps({k: status[k] for k in redis_dual.STATUS_FOR_LOG}))

    conn = redis_dual.client(settings, precheck=False)
    if conn is None:
        # No connection means no probe can be run, so there is nothing honest to answer with. The
        # requests stay unanswered and visible rather than answered with a guess.
        log.error("no Redis connection: no request can be answered. Requests are left on the board "
                  "unanswered rather than answered with something unmeasured.")
        return 1

    from bus import load_env                                             # noqa: PLC0415
    env = load_env()
    execution = os.environ.get("CLOUD_RUN_EXECUTION", "")
    me = execution or "local-%d" % os.getpid()
    until = live_deadline(_loop_until(os.environ.get("LOOP_UNTIL", "")), datetime.datetime.now(datetime.timezone.utc),
                          int(os.environ.get("LOOP_MAX_MINUTES", "240")))
    interval = max(5, min(60, int(os.environ.get("LOOP_INTERVAL", "10"))))
    lock_ttl = ONE_PASS_LOCK_SECONDS if until is None else max(LIVE_LOCK_SECONDS, interval * 6)
    try:
        holder = take_lock(conn, me, lock_ttl)
    except Exception as error:                                           # noqa: BLE001
        # Without the lock this run could race another; and a Redis that cannot take a SET cannot claim a
        # request's run marker either, so nothing would have been answered anyway.
        log.error("could not take the worker lock (%s): not running", type(error).__name__)
        return 1
    if holder is not None and until is not None:
        # A live run is the hand-over: wait for the holder to finish, then take over (wait_for_lock).
        log.info("another worker holds %s (%s): waiting for it, up to %s", WORKER_LOCK_KEY, holder,
                 until.strftime("%H:%M:%SZ"))
        holder = wait_for_lock(conn, me, lock_ttl, until, interval)
        if holder is None:
            log.info("took %s over from the previous worker", WORKER_LOCK_KEY)
    if holder is not None:
        # Not an error: the worker that holds it is answering. Exit 0 so this does not read as a failure.
        log.warning("another worker holds %s (%s): this execution does not run. One worker at a time - "
                    "two over one board answer a request twice. It frees within %ds of that worker stopping.",
                    WORKER_LOCK_KEY, holder, lock_ttl)
        return 0
    try:
        if until is None:
            one_pass(conn, env, execution, with_git=True,
                     renew=lambda: keep_lock(conn, me, lock_ttl))
            return 0
        return live_with_canvas(conn, env, execution, me, until, interval, lock_ttl)
    finally:
        drop_lock(conn, me)


def live(conn, env, execution, me, until, interval, lock_ttl, settle=None) -> int:
    # LIVE MODE, for a meeting. Mr. Salam, 2026-10-08: "Do not create barriers in agent's ability to read,
    # write, share ideas, propose changes during the meeting (without audio) ... like a background task
    # that runs as side conversation." A request posted during the call is answered within about
    # LOOP_INTERVAL seconds instead of whenever someone starts a run - one execution, a plain loop, no
    # model, ending at LOOP_UNTIL (at most LOOP_MAX_MINUTES from start, whatever LOOP_UNTIL says).
    # GitHub is read at most every git_requests.poll_seconds(): one call per repo, under the hourly
    # limit (60 without a token, so 144 s for two repos; once a minute with GITHUB_READ_TOKEN).
    # THE JOB'S --task-timeout MUST COVER LOOP_UNTIL: Cloud Run kills the execution at its timeout,
    # whatever this loop intends (the job's default spec is 5 minutes - see REQUESTS.md).
    import git_requests                                                  # noqa: PLC0415
    git_every = git_requests.poll_seconds(os.environ.get("GITHUB_READ_TOKEN") or None)
    stop = until                                     # already capped by LOOP_MAX_MINUTES, in main (live_deadline)
    log.info("live mode: every %ds, GitHub every %ds, until %s", interval, git_every, stop.strftime("%H:%M:%SZ"))
    last_git, passes = None, 0
    # Exactly "on". Anything else leaves `mirror` unset, and the one_pass call below is the one it
    # was: no mirror keyword, no inbox write. The cursor relay and the claude-api nudge are read once, here,
    # and only when the mirror runs at all.
    mirror = None
    if board_to_redis_on():
        relay, nudger = relay_cursor_on(), nudger_from_env()
        mirror = lambda rows, _conn=conn: mirror_pass(_conn, rows, relay, nudger)        # noqa: E731
    while datetime.datetime.now(datetime.timezone.utc) < stop:
        held = keep_lock(conn, me, lock_ttl)
        if held is False:
            log.error("lost %s to another worker: stopping so only one answers", WORKER_LOCK_KEY)
            break
        if held is None:
            # Ownership unknown (a Redis error): no pass, so no write, and try again next interval.
            log.warning("could not confirm %s; skipping this pass", WORKER_LOCK_KEY)
            time.sleep(interval)
            continue
        now = datetime.datetime.now(datetime.timezone.utc)
        with_git = last_git is None or (now - last_git).total_seconds() >= git_every
        try:
            # `settle` only when the call bridge runs, and `mirror` only when BOARD_TO_REDIS is exactly
            # on. Without either, a pass is called exactly as before.
            one_pass(conn, env, execution, with_git=with_git, quiet=True,
                     renew=lambda: keep_lock(conn, me, lock_ttl),
                     **({"settle": settle} if settle else {}),
                     **({"mirror": mirror} if mirror else {}))
        except Exception as error:                                       # noqa: BLE001
            # One bad pass (a board flap, a lost reply) must not end the meeting's channel.
            log.warning("pass failed: %s", type(error).__name__)
        except SystemExit as stop_code:
            # An UNCONFIRMED APPEND (append.py exit 2, logged above as APPEND UNCONFIRMED) ends this pass,
            # not the meeting. It used to end both: on 2026-10-11 one duplicate RESULT took the side
            # channel down from 22:15Z. Going on is safe: the op behind that row was claimed once by its
            # run marker, and a row that did not land gets the UNKNOWN notice after the claim's grace window.
            log.error("pass ended by exit %s; the live channel continues", stop_code.code)
        if with_git:
            last_git = now
        passes += 1
        time.sleep(interval)
    log.info("live mode ended after %d passes", passes)
    return 0


def live_with_canvas(conn, env, execution, me, until, interval, lock_ttl, start=None, start_bridge=None,
                     environ=None) -> int:
    """live(), with the canvas publisher (scripts/canvas_publish.py) beside it while CANVAS_BUCKET is set.

    BESIDE THE LOOP, ON ITS OWN THREAD: one pass starts with a board read that can take minutes, and the
    canvas has to move every few seconds. It runs only inside a live run, which is the execution holding
    gov:worker:lock, and it stops when that run ends - so one worker publishes at a time, and a window
    still open in Redis is picked up by the next live run. Unset, nothing starts and nothing changes.

    THE CALL BRIDGE (scripts/call_bridge.py) runs beside it, on its own thread, only with CALL_BRIDGE=on and a
    publisher running: its cards reach the page only through the publisher. Its done flips read the board rows
    each pass already read (one_pass `settle`). Off, or without the flag, nothing starts: the rollback is the flag.
    CALL_BRIDGE_RING=on adds the doorbell: each claim rings its agent with one board row (call_bridge.wake_row)."""
    import canvas_publish                                                # noqa: PLC0415
    environ = os.environ if environ is None else environ
    bucket = environ.get("CANVAS_BUCKET", "")
    holds = lambda: _text(conn.get(WORKER_LOCK_KEY)) == me              # noqa: E731
    # The lease, asked before every upload: a run that lost gov:worker:lock publishes nothing more (Aya on c633373).
    publisher = (start or canvas_publish.start)(conn, bucket, log, holds=holds)
    if publisher is not None:
        log.info("canvas publisher: open windows every %ds to gs://%s/rooms/", canvas_publish.PUBLISH_SECONDS, bucket)
    elif bucket:
        log.error("CANVAS_BUCKET is not a bucket name; the canvas publisher is not started")
    bridge = None
    if publisher is not None and environ.get("CALL_BRIDGE", "").strip().lower() == "on":
        import call_bridge                                               # noqa: PLC0415
        # THE DOORBELL (O3 gap 1): with CALL_BRIDGE_RING=on as well, each claim rings its agent with one board
        # row through this worker's own append path, asked under the lease. Off (the default): no ring, no change.
        ring = None
        if environ.get("CALL_BRIDGE_RING", "").strip().lower() == "on":
            write = appender(env)

            def ring(spec):
                if not holds():
                    raise LockLost("gov:worker:lock is not ours; %s not written" % spec.get("row_id"))
                return write(spec)
        # The publisher's bucket goes too: the bridge's first claim in a call opens the canvas window itself
        # (the owner's 11:17 ET test call on 2026-10-29 had no window, so nothing read his "do it").
        bridge = (start_bridge or call_bridge.start)(conn, log, holds, bucket=bucket,
                                                      **({"ring": ring} if ring else {}))
        if bridge is not None:
            log.info("call bridge: on, room asks become claimed cards in conf:canvas:%s", bridge.canvas_room)
    try:
        return live(conn, env, execution, me, until, interval, lock_ttl,
                    bridge.settle if bridge is not None else None)
    finally:
        for worker in (bridge, publisher):
            if worker is not None:
                worker.stop.set()
                worker.thread.join(timeout=15)


def live_deadline(until, started, max_minutes):
    """The ONE end of a live run: LOOP_UNTIL, but never more than LOOP_MAX_MINUTES after the run started.
    Computed once, before any wait for the lock, and used by both the wait and the loop. Aya on e4e98fc: the
    cap used to start only once the lock was taken, so a run that waited 90 s under a one-minute cap still
    started its loop after the cap had passed. None for a single pass."""
    if until is None:
        return None
    return min(until, started + datetime.timedelta(minutes=max_minutes))


def _loop_until(text):
    """LOOP_UNTIL as an aware datetime, or None for the normal single pass. Unparseable is single."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        when = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        log.error("LOOP_UNTIL %r is not an ISO time; running one pass", text)
        return None
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


# Q2 of the Redis doorbell. The live loop already reads the board every few seconds inside the VPC.
# With BOARD_TO_REDIS exactly "on", each new row whose Target_Surface names an agent is also one
# stream entry on fleet:inbox:<agent>, so a lane reader (Q1, built separately) has something to
# block on. Off, this block is not called and the pass does not touch those keys.
#
# EXACTLY ONCE per row and agent, and no lost wake: MIRROR_LUA is one script on the server. It
# sets fleet:wake:mirrored:<row_id>:<agent> NX with the fleet ttl, then XADDs; if the XADD errors
# it deletes that mark before it returns the error. So a mark exists only beside its entry, and a
# re-read finds the mark and adds nothing. A script that never ran (Redis unreachable) wrote
# nothing, and the next pass copies the row. A reply lost after the script ran is a delivered copy
# and a mark, and the next pass adds nothing. One agent's failure is logged as its exception type
# and the copy goes on to the next agent and the next row.
#
# METADATA ONLY. No payload text reaches a lane: no preview, no free text, and so no call words
# either. The fleet principals read every key, so a lane is not a confidentiality boundary. An
# entry is by, row_id, at and phase, plus thread_id, msg_type and class when each matches a short
# safe pattern; a value that does not match is dropped (a row id that does not match skips the
# row). An agent woken by the entry reads the row itself on the board, under the board's rules.
# Rows from bus-reconciler or redis-audit are not copied: those writers are this process and the
# audit, and copying them would loop. Nothing here appends a board row, and a log line from this
# path is an exception type, never a key and never a value.
#
# THE CURSOR RELAY (BOARD_TO_REDIS_RELAY_CURSOR exactly "on"). Cursor is a cloud agent with no machine to
# run a ring on; Grok is its dispatcher. So a row that NAMES cursor (by name or alias, not by ALL or fleet:
# a broadcast is not a dispatch) also gets one entry on fleet:inbox:grok: the same metadata plus
# for=cursor, under its own mark fleet:wake:relayed:<row_id>:cursor:grok, through the same MIRROR_LUA. So
# the relay is exactly once on its own, apart from cursor's copy and from grok's own copy of a row that
# names both. Grok's ring wakes on it, Grok reads the row on the board and dispatches Cursor. Off, no
# relay key is written.
BOARD_TO_REDIS = "BOARD_TO_REDIS"
RELAY_CURSOR = "BOARD_TO_REDIS_RELAY_CURSOR"
RELAYS = {"cursor": "grok"}             # an agent with no ring of its own -> the agent that dispatches it
LOOP_SOURCES = frozenset({"bus-reconciler", "redis-audit"})
_EXPAND = frozenset({"all", "fleet"})
_ENTRY_KEYS = ("phase", "thread_id", "msg_type", "class")
# Each copied value must FULLY match its pattern. None of them can carry a sentence.
_SAFE = {
    "row_id": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"),
    "by": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}"),
    "at": re.compile(r"[0-9][0-9TZ:.+ -]{0,39}"),
    "phase": re.compile(r"[A-Z][A-Z0-9_]{0,31}"),
    "thread_id": re.compile(r"TH-[A-Z0-9-]{1,64}"),
    "msg_type": re.compile(r"[A-Z][A-Z0-9_]{0,31}"),
    "class": re.compile(r"[A-Z][A-Z0-9_]{0,31}"),
}
# KEYS[1] the mark, KEYS[2] the lane. ARGV[1] the mark's ttl, ARGV[2] the lane's MAXLEN, ARGV[3] the
# lane's ttl, ARGV[4..] the entry's field/value pairs. 0: already copied. 1: added. An error reply:
# nothing is left behind, the mark is gone too. The lane is written as a governed xadd writes it
# (redis_gov.execute): MAXLEN ~ redis_gov.STREAM_MAXLEN, then EXPIRE to the access list's ttl for the
# key, which also refreshes the ttl of a lane that already existed.
MIRROR_LUA = """
if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1]) then return 0 end
local added = redis.pcall('XADD', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', unpack(ARGV, 4))
if type(added) == 'table' and added.err then
  redis.call('DEL', KEYS[1])
  return added
end
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 1
"""


def board_to_redis_on(environ=None) -> bool:
    """True only when BOARD_TO_REDIS is exactly "on". Unset, empty, and every other value are off."""
    environ = os.environ if environ is None else environ
    return environ.get(BOARD_TO_REDIS) == "on"


def relay_cursor_on(environ=None) -> bool:
    """True only when BOARD_TO_REDIS_RELAY_CURSOR is exactly "on"."""
    environ = os.environ if environ is None else environ
    return environ.get(RELAY_CURSOR) == "on"


def _row_cell(row, index) -> str:
    """One board cell as stripped text. Not _cell: that name is the call-sheet mirror's BCB escaper."""
    if not isinstance(row, (list, tuple)) or index >= len(row) or row[index] is None:
        return ""
    value = row[index]
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).strip()


def _payload(row) -> str:
    """The payload cell as text. Only its safe key=value fields are read; none of its text is copied."""
    if not isinstance(row, (list, tuple)) or len(row) <= 5 or row[5] is None:
        return ""
    value = row[5]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def inbox_wake_aliases() -> dict:
    """The ALIASES dict in cloud/inbox-wake/main.py, when that file is on disk.

    The checkout keeps it at cloud/inbox-wake/main.py, next to this directory. The image copies
    that same file to /app/inbox_wake_main.py (not main.py: that name is this job's entrypoint).
    Parsed, not imported: importing it would load the wake server into this process. Absent, the
    access list's own aliases still apply."""
    here = Path(__file__).resolve()
    found = next((path for path in (
        here.parent.parent / "inbox-wake" / "main.py",
        here.parent / "inbox_wake_main.py",
    ) if path.is_file()), None)
    if found is None:
        return {}
    tree = ast.parse(found.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "ALIASES" for target in node.targets):
            value = ast.literal_eval(node.value)
            return {str(key).strip().lower(): str(item).strip().lower() for key, item in value.items()}
    return {}


def addressed_agents(target, principals, aliases, expand=True) -> list:
    """Principals named by a Target_Surface. ALL and fleet mean every principal (expand=False: they
    name nobody). One hop of alias, then the name has to be a principal. Order is the address list,
    then principal order for an expansion, and a name is kept once."""
    known = set(principals)
    out, seen = [], set()
    for part in re.split(r"[,;]", str(target or "")):
        token = part.strip().lower()
        if not token:
            continue
        if token in _EXPAND and not expand:
            continue
        names = list(principals) if token in _EXPAND else (aliases.get(token, token),)
        for name in names:
            if name in known and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _safe(name, value) -> str:
    """value when it fully matches the pattern for `name`, else ""."""
    value = str(value or "").strip()
    return value if _SAFE[name].fullmatch(value) else ""


def _bcb_field(payload, key) -> str:
    """The first `key=` field of a pipe-separated payload, unchecked. _safe decides if it travels."""
    found = re.search(r"(?:^|\|)\s*" + re.escape(key) + r"=([^|]*)", payload or "", re.I)
    return found.group(1) if found else ""


def mirror_fields(source, row_id, at, payload) -> dict:
    """The stream entry: metadata only, never payload text. by, row_id, at and phase are always
    present ("" when absent or unsafe); thread_id, msg_type and class only when present and safe."""
    fields = {
        "by": _safe("by", source),
        "row_id": _safe("row_id", row_id),
        "at": _safe("at", at),
        "phase": _safe("phase", _bcb_field(payload, "phase")),
    }
    for key in _ENTRY_KEYS[1:]:
        value = _safe(key, _bcb_field(payload, key))
        if value:
            fields[key] = value
    return fields


def mirror_addressed(conn, rows, relay=False, on_added=None) -> int:
    """XADD one entry per named agent for each board row. Returns how many entries were added.

    The access list decides who a principal is and what the fleet ttl is. Inbox-wake aliases are
    merged on top of the access list's aliases. An unreadable list, or a fleet ttl that is not
    set, copies nothing: there is no second ttl to invent. Does not log a key or a value.
    A failed copy for one agent is logged by exception type and does not stop the rest.
    relay=True adds the RELAYS entries (the cursor relay); on_added(lane agent) is called once for each
    entry that landed, and its failure is that callback's, not the copy's."""
    acl = redis_gov.load_acl()
    if not acl.get("readable"):
        return 0
    ttl = redis_gov.ttl_for(acl, "fleet:")
    if not ttl:
        return 0
    principals = list(acl["principals"])
    aliases = {str(key).lower(): str(value).lower() for key, value in (acl.get("aliases") or {}).items()}
    aliases.update(inbox_wake_aliases())
    added = 0
    for row in rows or []:
        row_id = _row_cell(row, 0)
        source = _row_cell(row, 2)
        if not _safe("row_id", row_id) or source.lower() in LOOP_SOURCES:
            continue
        agents = addressed_agents(_row_cell(row, 3), principals, aliases)
        if not agents:
            continue
        named = addressed_agents(_row_cell(row, 3), principals, aliases, expand=False) if relay else []
        fields = mirror_fields(source, row_id, _row_cell(row, 1), _payload(row))
        pairs = [item for pair in fields.items() for item in pair]
        targets = []
        for agent in agents:
            targets.append(("fleet:wake:mirrored:%s:%s" % (row_id, agent), agent, pairs))
            if relay and agent in named and RELAYS.get(agent) in principals:
                # The relay's own mark, per row and per relay target: exactly once apart from the copy.
                targets.append(("fleet:wake:relayed:%s:%s:%s" % (row_id, agent, RELAYS[agent]),
                                RELAYS[agent], pairs + ["for", agent]))
        for mark, agent, entry in targets:
            lane = "fleet:inbox:%s" % agent
            mark_ttl, lane_ttl = redis_gov.ttl_for(acl, mark), redis_gov.ttl_for(acl, lane)
            if not mark_ttl or not lane_ttl:
                continue
            try:
                # One script: the NX mark and the XADD, or neither. 0 is a row already copied.
                landed = conn.eval(MIRROR_LUA, 2, mark, lane, int(mark_ttl), int(redis_gov.STREAM_MAXLEN),
                                   int(lane_ttl), *entry)
            except Exception as error:                                   # noqa: BLE001
                # The type only. This agent's copy is retried by the next pass; the others go on.
                log.warning("board mirror copy failed (%s)", type(error).__name__)
                continue
            if landed:
                added += 1
                if on_added is not None:
                    on_added(agent)
    return added


# THE CLAUDE-API NUDGE (INBOX_WAKE_REDIS exactly "on", with INBOX_WAKE_URL and INBOX_WAKE_SECRET set).
# claude-api is a Cloud Run job; cloud/inbox-wake starts it, and inbox-wake is scale-to-zero, so it cannot
# block on the lane itself. This loop already holds Redis and has just XADDed the entry, so it tells
# inbox-wake: one POST /wake, X-GitHub-Event: redis_lane, body {"by": "bus-reconciler", "lane":
# "claude-api"} and nothing else, signed with the ring's HMAC (X-Hub-Signature-256, as drive-intake
# signs) and carrying this job's ID token for the private service. inbox-wake starts the worker while the
# worker's own position lags the lane (cloud/inbox-wake/README.md), so a nudge says only "look", and a repeated one is harmless.
# A nudge that fails or comes back unrung stays pending and is sent again no sooner than
# NUDGE_RETRY_SECONDS later, while this run lasts; after that the 15-minute sweep drains the lane.
INBOX_WAKE_REDIS = "INBOX_WAKE_REDIS"
NUDGE_AGENTS = frozenset({"claude-api"})
NUDGE_EVENT = "redis_lane"
NUDGE_RETRY_SECONDS = 60
NUDGE_DONE = frozenset({"woke", "duplicate", "idle", "caught_up"})
# Answers a retry cannot change (the flag or the recipient list on inbox-wake): dropped, logged by status.
NUDGE_FINAL = frozenset({"disabled", "lane_off", "no_lane", "not_allowed"})
IDENTITY_URL = ("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/"
                "identity?audience=%s")


def _origin(url):
    import urllib.parse                                                  # noqa: PLC0415
    parts = urllib.parse.urlsplit(url)
    return "%s://%s" % (parts.scheme, parts.netloc)


def private_post(url, body, headers):  # pragma: no cover - the nudge's one socket
    """POST to the PRIVATE inbox-wake service with this job's ID token for its origin."""
    import urllib.parse                                                  # noqa: PLC0415
    import urllib.request                                                # noqa: PLC0415
    ask = urllib.request.Request(IDENTITY_URL % urllib.parse.quote(_origin(url), safe=""),
                                 headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(ask, timeout=10) as resp:
        token = resp.read().decode("utf-8").strip()
    signed = dict(headers, Authorization="Bearer " + token)
    req = urllib.request.Request(url, data=body, headers=signed, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


# THE LAG CHECK (Codex on 790dc1a0). inbox-wake's 15-minute sweep is a GitHub schedule, which GitHub may
# delay or drop. So while this loop runs, it also nudges when the claude-api worker's own position
# (fleet:worker:inbox_scan:claude-api, which the worker writes) is behind its lane's head: two reads, at
# most once per LAG_CHECK_SECONDS, which is inbox-wake's SCAN_CLAIM_TTL. A nudge inside inbox-wake's live
# claim is answered duplicate and starts nothing, so the check cannot double a running worker.
LAG_CHECK_SECONDS = 720
SCAN_STATE_KEY = "fleet:worker:inbox_scan:%s"


def _stream_order(value):
    """A stream id as (ms, seq), or None."""
    head, _, tail = str(value or "").partition("-")
    if not head.isdigit() or (tail and not tail.isdigit()):
        return None
    return int(head), int(tail or 0)


def worker_lags(conn, agent) -> bool:
    """True when the worker's scan position is behind the newest entry of fleet:inbox:<agent>."""
    raw = conn.get(SCAN_STATE_KEY % agent)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        state = {}
    after = _stream_order(state.get("after")) if isinstance(state, dict) else None
    newest = conn.xrevrange("fleet:inbox:%s" % agent, count=1) or ()
    head = newest[0][0] if newest else ""
    if isinstance(head, bytes):
        head = head.decode("utf-8", "replace")
    head = _stream_order(head)
    return head is not None and (after or (0, -1)) < head


class Nudger:
    """Collects the lanes the mirror wrote this pass (note) and nudges inbox-wake once for each (flush).
    check_lag adds a lane whose worker is behind, at most once per LAG_CHECK_SECONDS."""

    def __init__(self, url, secret, post=None, clock=None):
        self.url, self.secret = url, secret
        self.post = post or private_post
        self.clock = clock or time.monotonic
        self.pending, self.not_before = set(), 0.0
        self.lag_next = 0.0

    def note(self, agent):
        if agent in NUDGE_AGENTS:
            self.pending.add(agent)

    def check_lag(self, conn):
        if self.clock() < self.lag_next:
            return
        self.lag_next = self.clock() + LAG_CHECK_SECONDS
        for agent in sorted(NUDGE_AGENTS):
            try:
                if worker_lags(conn, agent):
                    self.note(agent)
            except Exception as error:                                   # noqa: BLE001
                log.warning("worker lag check failed (%s)", type(error).__name__)

    def body_for(self, agent) -> bytes:
        return json.dumps({"by": "bus-reconciler", "lane": agent}, sort_keys=True).encode("utf-8")

    def flush(self):
        if not self.pending or self.clock() < self.not_before:
            return
        for agent in sorted(self.pending):
            body = self.body_for(agent)
            mac = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            try:
                readout = self.post(self.url, body, {"Content-Type": "application/json",
                                                     "X-GitHub-Event": NUDGE_EVENT,
                                                     "X-Hub-Signature-256": "sha256=" + mac})
                parsed = readout if isinstance(readout, dict) else json.loads(
                    readout.decode("utf-8") if isinstance(readout, bytes) else readout)
                status = str(((parsed.get("woke") or [{}])[0] or {}).get("status") or "")
            except Exception as error:                                   # noqa: BLE001
                log.warning("inbox-wake nudge failed (%s); retried", type(error).__name__)
                self.not_before = self.clock() + NUDGE_RETRY_SECONDS
                return
            if status in NUDGE_DONE or status in NUDGE_FINAL:
                self.pending.discard(agent)
                if status in NUDGE_FINAL:
                    log.warning("inbox-wake nudge not acted on (%s)", status)
                continue
            # A fixed word from inbox-wake's own set; a value that is not one is not logged.
            log.warning("inbox-wake nudge unrung (%s); retried", status if re.fullmatch(r"[a-z_]{1,24}", status)
                        else "unreadable")
            self.not_before = self.clock() + NUDGE_RETRY_SECONDS
            return


def nudger_from_env(environ=None):
    """A Nudger when INBOX_WAKE_REDIS is exactly "on" and the URL and secret are set; else None."""
    environ = os.environ if environ is None else environ
    if environ.get(INBOX_WAKE_REDIS) != "on":
        return None
    url, secret = environ.get("INBOX_WAKE_URL") or "", environ.get("INBOX_WAKE_SECRET") or ""
    if not url or not secret:
        log.error("INBOX_WAKE_REDIS is on but INBOX_WAKE_URL or INBOX_WAKE_SECRET is unset: no nudge")
        return None
    return Nudger(url, secret)


def mirror_pass(conn, rows, relay=False, nudger=None) -> int:
    """The live loop's mirror: the copy (with the cursor relay when asked), then the nudge for what landed."""
    added = mirror_addressed(conn, rows, relay=relay, on_added=nudger.note if nudger is not None else None)
    if nudger is not None:
        nudger.check_lag(conn)
        nudger.flush()
    return added


def one_pass(conn, env, execution, with_git=True, quiet=False, renew=None, settle=None, mirror=None):
    """Read the board (and GitHub), answer what is allowed, beat the heartbeat. One pass.

    `settle`, when given, is handed the board rows just read (the call bridge's done flips); its failure costs
    those flips this pass and nothing else. `mirror`, when given, is handed those same board rows, before the
    git channel is appended; the live loop passes it only when BOARD_TO_REDIS is exactly on. Its failure
    costs the copy this pass and nothing else. Absent, this pass does not touch an inbox."""
    now = datetime.datetime.now(datetime.timezone.utc)
    bus_request.heartbeat(conn, "running", now=now, execution=execution)
    since = (now - datetime.timedelta(minutes=WINDOW_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = bus_reconcile.board_rows(env, since=since)
    # The board read can take minutes; the lock is renewed after it, and before each append below.
    if renew is not None and renew() is not True:
        raise LockLost("gov:worker:lock is not provably ours after the board read; this pass answers nothing")
    if mirror is not None:
        try:
            mirror(rows)
        except Exception as error:                                       # noqa: BLE001
            # The type only. A key or any row text in this line would put the row, and his words, in the log.
            log.warning("board mirror failed (%s)", type(error).__name__)
    if settle is not None:
        try:
            settle(rows)
        except Exception as error:                                       # noqa: BLE001
            log.warning("call bridge settle failed (%s)", type(error).__name__)
    if not quiet:
        log.info("window since=%s: %d board row(s)", since, len(rows))
    # THE GIT CHANNEL (scripts/git_requests.py): requests from the GitHub bot accounts the access list
    # binds by numeric id - Cursor's only way in. Appended AFTER the board rows, so the board's RESULT
    # rows are what makes a git request "already answered" too. A GitHub failure costs this channel one
    # run and nothing else.
    if with_git:
        import git_requests                                              # noqa: PLC0415
        git = git_requests.git_rows(now - datetime.timedelta(minutes=WINDOW_MINUTES),
                                    token=os.environ.get("GITHUB_READ_TOKEN") or None, log=log.warning)
        if not quiet or git:
            log.info("git channel: %d request row(s)", len(git))
        rows = list(rows) + git

    out = bus_request.handle(rows, conn, now=now, append=guarded(appender(env), renew), max_per_run=MAX_PER_RUN)
    if not quiet or out["answered"] or out["capped"]:
        log.info("requests: %s", bus_request.summary(out))
    for item in out["answered"]:
        log.info("answered %s: %s in %dms", item["req_id"], item["status"], item["latency_ms"])
    if not quiet:
        for item in out["refused"]:
            log.warning("refused %s: %s", item["req_id"] or "(no id)", "; ".join(item["why"]))
    if out["capped"]:
        log.warning("%d request(s) left for the next run by the per-run cap", out["capped"])
    bus_request.heartbeat(conn, "idle", execution=execution, answered=len(out["answered"]),
                          refused=len(out["refused"]), capped=out["capped"], acl=out.get("acl", ""))
    # A refusal is a correct outcome and an error inside the probe is a reported one, so neither
    # fails the execution. Only being unable to reach Redis at all does, above.
    # THE CALL SHEET. A projection of the chair's Redis record onto the board. Off unless
    # CALL_SHEET_MIRROR is exactly on. The board is not an input: a sheet row never starts an action.
    try:
        mirror_call_sheet(conn, now, env, renew)
    except LockLost:
        raise
    except Exception as error:                                       # noqa: BLE001
        log.warning("call sheet mirror failed id=- size=0 (%s)", type(error).__name__)
    return out


# --- call sheet mirror ---------------------------------------------------------------------------
# The conference chair stays Redis-only. This worker already holds the board and Redis, so when
# CALL_SHEET_MIRROR is exactly "on" each pass copies call events and live-call turns onto the
# sheet. The sheet is only that projection: it never writes a voice lane and never acts on a
# CALL_REPLY. Off (the default, and every other value) it does nothing. No deploy rides with it.
#
# fleet_agent.append_and_verify has no reuse_check on main (its signature is rid, row, fitted,
# spill). Done means EXACTLY ONE row with this id, this payload, and the gateway's verified
# bus-reconciler stamp. Two copies, a different payload, or another sender is a conflict: log
# the id, do not append, do not move the cursor. A lost worker lease aborts the pass before
# the next cursor write or Redis state change.

CALL_ID_RE = re.compile(r"^conf-[0-9a-f]{16}-[0-9a-f]{8}$")
CALL_TAGS = frozenset({"grok", "claude-code-cli", "chatgpt-codex-desktop", "gemini"})
CALLS_KEY = "conf:calls"
CALL_KEY = "conf:call:%s"
TURNS_KEY = "conf:callturns:%s"
CALLS_CURSOR = "gov:mirror:calls:cursor"
TURNS_CURSOR = "gov:mirror:turns:%s:cursor"
STALE_AFTER = datetime.timedelta(minutes=50)
STALE_TTL = 7 * 24 * 3600
CALLS_MAXLEN = 1000
TURN_TEXT_MAX = 1000
MIRROR_PAGE = 100
# The stamp append.py requires on a read-back when this worker posts (scripts/append.py).
VERIFIED_STAMP = "sender=verified;who=bus-reconciler@sfdc24.iam.gserviceaccount.com"
# The chair's END_LUA (conference#373): one script, compare-and-set on the live hash and its
# epoch, then the hash and the conf:calls end together. state must be live, so a second pass
# cannot write a second end.
STALE_END_LUA = """
if redis.call('HGET', KEYS[1], 'state') ~= 'live' then return 0 end
if redis.call('HGET', KEYS[1], 'epoch') ~= ARGV[2] then return 0 end
redis.call('HSET', KEYS[1], 'state', 'ended', 'epoch', ARGV[2], 'ended_at', ARGV[3], 'reason', ARGV[6])
redis.call('EXPIRE', KEYS[1], ARGV[4])
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[5], '*', 'event', 'end', 'call_id', ARGV[1], 'epoch', ARGV[2], 'at', ARGV[3], 'reason', ARGV[6])
return 1
"""
SHEET_FROM = "conference-chair"   # who the call record is from
SHEET_SOURCE = "bus-reconciler"  # who posts it; the gateway stamps sender=verified for this tag


def call_sheet_on():
    """The flag is exactly the string on. Empty, ON, true and 1 are all off."""
    return os.environ.get("CALL_SHEET_MIRROR", "").strip() == "on"


def _mirror_log(ident, size, why):
    """Id and size only. `why` is a type name or a fixed token, never the words of a turn."""
    log.warning("call sheet mirror: %s size=%d (%s)", ident, int(size), why)


def _entry_size(fields):
    return sum(len(str(v).encode("utf-8")) for v in (fields or {}).values())


def _as_map(raw):
    out = {}
    for key, value in (raw or {}).items():
        name = _text(key)
        if isinstance(value, bytes):
            out[str(name)] = value.decode("utf-8", "replace")
        elif value is None:
            out[str(name)] = ""
        else:
            out[str(name)] = str(_text(value)) if not isinstance(value, str) else value
    return out


def _cell(value):
    """One BCB value: a pipe would start a new key, so it becomes a slash."""
    text = "" if value is None else str(value)
    text = text.replace("|", "/").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return "".join(ch if ch >= " " else " " for ch in text)


def _bcb(pairs):
    return "|".join(["BCB"] + ["%s=%s" % (key, _cell(value)) for key, value in pairs])


def _carry(pairs):
    """The worker posts the chair's record. relayer= is the source tag, so the signature is legal
    and the gateway can stamp sender=verified for bus-reconciler. These fields stay off the
    msg_type|call_id pair the box reader matches."""
    return list(pairs) + [("relayer", SHEET_SOURCE), ("claimed_author", SHEET_FROM)]


def _field(payload, key):
    match = re.search(r"(?:^|\|)" + re.escape(key) + r"=([^|]*)", str(payload or ""))
    return match.group(1).strip() if match else ""


def _parse_time(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        when = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc)


def _stamp(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token(value, limit=64):
    text = (value or "").strip()
    if not text or len(text) > limit or any(ch in text for ch in ":| \t\r\n"):
        return ""
    return text


def _after(conn, key, cursor):
    """Stream entries strictly after `cursor`, oldest first. No cursor reads from the start."""
    if cursor:
        found = conn.xrange(key, min="(" + cursor, max="+", count=MIRROR_PAGE)
    else:
        found = conn.xrange(key, min="-", max="+", count=MIRROR_PAGE)
    return [(_text(entry_id), _as_map(raw)) for entry_id, raw in found or ()]


def _cursor(conn, key):
    raw = conn.get(key)
    return _text(raw) if raw else ""


def _owned(renew):
    """Raise LockLost unless this execution still holds the worker lease. No renew means the
    caller is not under the lease (a test, or a pass that has no lock)."""
    if renew is None:
        return
    if renew() is not True:
        raise LockLost("gov:worker:lock is not ours")


def _caught(ident, size, error):
    """A lost lease leaves this pass. Anything else is logged by id and size, never by its words."""
    if isinstance(error, LockLost):
        raise error
    _mirror_log(ident, size, type(error).__name__)


def _row_present(env, rid):
    """Every sheet row with this exact Row_ID, or None. A read that fails raises, so the cursor
    stays. match= is only a hint: a row that quotes the id is not this id."""
    from bus import read_rows                                            # noqa: PLC0415

    found = read_rows(env, match=rid)
    rows = found.get("rows") if isinstance(found, dict) else (found or [])
    hits = [row for row in rows if row and row[0] == rid]
    return hits or None


def _as_hits(found):
    """None, one hit, or several. A hit is a sheet row (a list of cells) or a dict with
    payload, source_tag and sender."""
    if not found:
        return []
    if isinstance(found, dict):
        return [found]
    if isinstance(found, (list, tuple)) and found and isinstance(found[0], (dict, list, tuple)):
        return list(found)
    return [found]


def _hit_parts(hit):
    if isinstance(hit, dict):
        return (hit.get("payload") or "", hit.get("source_tag") or "", hit.get("sender") or "")
    payload = str(hit[5] if len(hit) > 5 else "")
    source = str(hit[2] if len(hit) > 2 else "")
    stamp = str(hit[11] if len(hit) > 11 else "")
    return payload, source, stamp


def _reuse_status(found, payload):
    """Done only for exactly one row: this payload, source tag bus-reconciler, and the gateway's
    verified stamp. Two copies, a different payload, or another sender is a conflict."""
    hits = _as_hits(found)
    if not hits:
        return "absent"
    if len(hits) != 1:
        return "conflict"
    got, source, stamp = _hit_parts(hits[0])
    if got != payload or source != SHEET_SOURCE or stamp != VERIFIED_STAMP:
        return "conflict"
    return "done"


def _append_row(env, renew, spec):
    guarded(appender(env), renew)(spec)


def _land(conn, cursor_key, entry_id, spec, append, present, renew):
    """Append unless this Row_ID is already done. A conflict is logged by id and the cursor
    stays. The cursor moves only after one verified matching row has been read back."""
    rid = spec["row_id"]
    payload = spec.get("payload") or ""
    try:
        status = _reuse_status(present(rid), payload)
        if status == "conflict":
            _mirror_log(rid, len(payload), "conflict")
            return False
        if status == "absent":
            append(spec)
            status = _reuse_status(present(rid), payload)
            if status == "conflict":
                _mirror_log(rid, len(payload), "conflict")
                return False
        if status != "done":
            _mirror_log(rid, len(payload), "unread")
            return False
        _owned(renew)
        conn.set(cursor_key, entry_id)
    except (Exception, SystemExit) as error:
        _caught(rid, len(payload), error)
        return False
    return True


def _sheet_spec(rid, to, action, payload, gist):
    return {"row_id": rid, "source_tag": SHEET_SOURCE, "target_surface": to, "action_type": action,
            "category": "DONE" if action == "RESULT" else "OPEN", "project_tag": "CALL",
            "gist": _cell(gist)[:180], "payload": payload}


def _event_spec(fields):
    """The sheet row for one conf:calls entry, or None when the entry is not a call we can record."""
    call_id = (fields.get("call_id") or "").strip()
    event = (fields.get("event") or "").strip()
    epoch = _token(fields.get("epoch"))
    if not CALL_ID_RE.fullmatch(call_id) or event not in ("start", "end") or not epoch:
        return None
    at = _cell(fields.get("at"))
    if event == "start":
        rid = "CALLSTART-" + call_id
        payload = _bcb(_carry([("v", "1"), ("id", rid), ("phase", "RESULT"), ("class", "CALL"),
                               ("from", SHEET_FROM), ("to", "ALL"), ("msg_type", "CALL_START"),
                               ("call_id", call_id), ("epoch", epoch), ("started_at", at)]))
        return _sheet_spec(rid, "ALL", "RESULT", payload, "CALL_START " + call_id)
    rid = "CALLEND-" + call_id
    payload = _bcb(_carry([("v", "1"), ("id", rid), ("phase", "RESULT"), ("class", "CALL"),
                           ("from", SHEET_FROM), ("to", "ALL"), ("msg_type", "CALL_END"),
                           ("call_id", call_id), ("epoch", epoch), ("ended_at", at),
                           ("reason", fields.get("reason"))]))
    return _sheet_spec(rid, "ALL", "RESULT", payload, "CALL_END " + call_id)


def _mirror_events(conn, append, present, renew):
    cursor = _cursor(conn, CALLS_CURSOR)
    for entry_id, fields in _after(conn, CALLS_KEY, cursor):
        try:
            spec = _event_spec(fields)
            if spec is None:
                _mirror_log(entry_id, _entry_size(fields), "skip")
                _owned(renew)
                conn.set(CALLS_CURSOR, entry_id)
                continue
            if not _land(conn, CALLS_CURSOR, entry_id, spec, append, present, renew):
                return
        except (Exception, SystemExit) as error:
            _caught(entry_id, _entry_size(fields), error)
            return


def _count(value):
    """A size the chair already counted. Digits only, so a word cannot ride in on chars=."""
    text = _cell(value).strip()
    return text if text.isdigit() else ""


def _turn_body(fields):
    """The text field alone. Missing or blank is withheld. No other field is the words."""
    if "text" not in fields:
        return ""
    return _cell(fields.get("text")).strip()


def _turn_kind(fields):
    """The chair's turn_class. The old class= field is not this, and is not copied."""
    kind = (fields.get("turn_class") or "").strip()
    return kind if kind in ("command", "other") else ""


def _class_fields(fields):
    """turn_class plus the counts that belong to it. command: verb and target_chars. other: chars."""
    kind = _turn_kind(fields)
    if not kind:
        return []
    extra = [("turn_class", kind)]
    if kind == "command":
        verb = _cell(fields.get("verb")).strip()
        count = _count(fields.get("target_chars"))
        if verb:
            extra.append(("verb", verb))
        if count:
            extra.append(("target_chars", count))
    else:
        count = _count(fields.get("chars"))
        if count:
            extra.append(("chars", count))
    return extra


def _turn_spec(call_id, fields):
    turn_key = _token(fields.get("turn_key"), limit=120)
    tag = (fields.get("to") or "").strip()
    epoch = _token(fields.get("epoch"))
    if not turn_key or tag not in CALL_TAGS or not epoch:
        return None
    text = _turn_body(fields)
    rid = "CALLTURN-" + turn_key
    if not text:
        return _withheld_turn(call_id, fields, rid, tag, epoch, turn_key)
    shown, cut = (text, False) if len(text) <= TURN_TEXT_MAX else (text[:TURN_TEXT_MAX], True)
    pairs = _turn_head(rid, tag, call_id) + [
        ("epoch", epoch), ("turn_key", turn_key), ("said_at", fields.get("said_at")), ("text", shown)]
    if cut:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        pairs += [("truncated", "1"), ("chars", str(len(text))), ("sha256", digest)]
    pairs = _with_class(pairs, fields)
    return _sheet_spec(rid, tag, "ASK", _bcb(_carry(pairs)), "CALL_TURN %s %d" % (turn_key, len(shown)))


def _with_class(pairs, fields):
    """Append turn_class and its counts. A chars= already on the row (a truncated text) stays."""
    have = {key for key, _ in pairs}
    for key, value in _class_fields(fields):
        if key not in have:
            pairs.append((key, value))
    return pairs


def _turn_head(rid, tag, call_id):
    """msg_type=CALL_TURN is immediately followed by call_id=. The box reader matches that substring."""
    return [("v", "1"), ("id", rid), ("phase", "ASK"), ("class", "CALL"),
            ("from", SHEET_FROM), ("to", tag), ("msg_type", "CALL_TURN"),
            ("call_id", call_id)]


def _withheld_turn(call_id, fields, rid, tag, epoch, turn_key):
    """No text on the entry: record turn_class and the counts, and do not invent the words.

    class=CALL stays the envelope class. turn_class is the chair's own field."""
    pairs = _turn_head(rid, tag, call_id) + [
        ("epoch", epoch), ("turn_key", turn_key), ("said_at", fields.get("said_at")), ("withheld", "1")]
    return _sheet_spec(rid, tag, "ASK", _bcb(_carry(_with_class(pairs, fields))),
                       "CALL_TURN %s withheld" % turn_key)


def _known_calls(conn):
    """Every conf:call:<id> hash, live or ended."""
    found = []
    for key in conn.scan_iter(match="conf:call:*", count=500):
        name = str(_text(key) or "")
        if not name.startswith("conf:call:"):
            continue
        call_id = name[len("conf:call:"):]
        if not CALL_ID_RE.fullmatch(call_id):
            continue
        found.append((call_id, _as_map(conn.hgetall(name))))
    return found


def _live_calls(conn):
    return [(call_id, fields) for call_id, fields in _known_calls(conn) if fields.get("state") == "live"]


def _mirror_turns(conn, append, present, renew):
    for call_id, _fields in _live_calls(conn):
        cursor_key = TURNS_CURSOR % call_id
        cursor = _cursor(conn, cursor_key)
        try:
            entries = _after(conn, TURNS_KEY % call_id, cursor)
        except (Exception, SystemExit) as error:
            _caught(call_id, 0, error)
            continue
        for entry_id, fields in entries:
            try:
                spec = _turn_spec(call_id, fields)
                if spec is None:
                    _mirror_log(entry_id, _entry_size(fields), "turn")
                    _owned(renew)
                    conn.set(cursor_key, entry_id)
                    continue
                if not _land(conn, cursor_key, entry_id, spec, append, present, renew):
                    break
            except (Exception, SystemExit) as error:
                _caught(entry_id, _entry_size(fields), error)
                break


def _sweep_stale(conn, now, renew):
    """A call more than 50 minutes after started_at is offered to one compare-and-set. The script
    closes it only while it is still live in that epoch, so a second pass writes no second end.
    The end event and state=ended land together, or neither does."""
    for call_id, fields in _known_calls(conn):
        try:
            started = _parse_time(fields.get("started_at"))
            if started is None or now - started <= STALE_AFTER:
                continue
            epoch = _token(fields.get("epoch"))
            if not epoch:
                _mirror_log(call_id, _entry_size(fields), "epoch")
                continue
            at = _stamp(now)
            _owned(renew)
            conn.eval(STALE_END_LUA, 2, CALL_KEY % call_id, CALLS_KEY,
                      call_id, epoch, at, int(STALE_TTL), int(CALLS_MAXLEN), "stale")
        except (Exception, SystemExit) as error:
            _caught(call_id, _entry_size(fields), error)


def mirror_call_sheet(conn, now, env, renew=None, append=None, present=None):
    """One pass of the call sheet. Flag off: return before any Redis or board call.

    Events, live turns, then the stale sweep. The board is not read here. `append` and `present`
    are the production write and read-back unless a test supplies its own. A lost lease aborts
    the pass. Any other failure is logged by id and size, and the later parts still run.
    """
    if not call_sheet_on():
        return
    _owned(renew)
    write = append or (lambda spec: _append_row(env, renew, spec))
    seen = present or (lambda rid: _row_present(env, rid))
    for step in (
        lambda: _mirror_events(conn, write, seen, renew),
        lambda: _mirror_turns(conn, write, seen, renew),
        lambda: _sweep_stale(conn, now, renew),
    ):
        try:
            step()
        except (Exception, SystemExit) as error:
            _caught("-", 0, error)


if __name__ == "__main__":
    sys.exit(main())

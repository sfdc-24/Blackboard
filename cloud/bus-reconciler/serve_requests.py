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
comparing. The rows it writes here are its own receipts and results, under its own Row_IDs, and the
only Redis key it touches is one the server names in its own namespace.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, "/app")

import bus_request                                                       # noqa: E402
import bus_reconcile                                                     # noqa: E402
import redis_dual                                                        # noqa: E402

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


def appender(env):
    """Append one row spec through append.py, so the write and its read-back are the fleet's one path."""
    import append                                                        # noqa: PLC0415

    def write(spec):
        handle, path = tempfile.mkstemp(suffix=".json", prefix="row-")
        os.close(handle)
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                json.dump(spec, fh)
            argv = sys.argv
            sys.argv = ["append.py", path]
            try:
                append.main()
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
        return live(conn, env, execution, me, until, interval, lock_ttl)
    finally:
        drop_lock(conn, me)


def live(conn, env, execution, me, until, interval, lock_ttl) -> int:
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
            one_pass(conn, env, execution, with_git=with_git, quiet=True,
                     renew=lambda: keep_lock(conn, me, lock_ttl))
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


def one_pass(conn, env, execution, with_git=True, quiet=False, renew=None):
    """Read the board (and GitHub), answer what is allowed, beat the heartbeat. One pass."""
    now = datetime.datetime.now(datetime.timezone.utc)
    bus_request.heartbeat(conn, "running", now=now, execution=execution)
    since = (now - datetime.timedelta(minutes=WINDOW_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = bus_reconcile.board_rows(env, since=since)
    # The board read can take minutes; the lock is renewed after it, and before each append below.
    if renew is not None and renew() is not True:
        raise LockLost("gov:worker:lock is not provably ours after the board read; this pass answers nothing")
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
    return out


if __name__ == "__main__":
    sys.exit(main())

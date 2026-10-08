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
    until = _loop_until(os.environ.get("LOOP_UNTIL", ""))
    if until is None:
        one_pass(conn, env, execution, with_git=True)
        return 0
    # LIVE MODE, for a meeting. Mr. Salam, 2026-10-08: "Do not create barriers in agent's ability to read,
    # write, share ideas, propose changes during the meeting (without audio) ... like a background task
    # that runs as side conversation." A request posted during the call is answered within about
    # LOOP_INTERVAL seconds instead of whenever someone starts a run - one execution, a plain loop, no
    # model, ending at LOOP_UNTIL (at most LOOP_MAX_MINUTES from start, whatever LOOP_UNTIL says).
    # GitHub is read at most once a minute: its unauthenticated limit is 60 calls an hour.
    interval = max(5, min(60, int(os.environ.get("LOOP_INTERVAL", "10"))))
    started = datetime.datetime.now(datetime.timezone.utc)
    hard_stop = started + datetime.timedelta(minutes=int(os.environ.get("LOOP_MAX_MINUTES", "240")))
    stop = min(until, hard_stop)
    log.info("live mode: every %ds until %s", interval, stop.strftime("%H:%M:%SZ"))
    last_git, passes = None, 0
    while datetime.datetime.now(datetime.timezone.utc) < stop:
        now = datetime.datetime.now(datetime.timezone.utc)
        with_git = last_git is None or (now - last_git).total_seconds() >= 60
        try:
            one_pass(conn, env, execution, with_git=with_git, quiet=True)
        except Exception as error:                                       # noqa: BLE001
            # One bad pass (a board flap, a lost reply) must not end the meeting's channel.
            log.warning("pass failed: %s", type(error).__name__)
        if with_git:
            last_git = now
        passes += 1
        time.sleep(interval)
    log.info("live mode ended after %d passes", passes)
    return 0


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


def one_pass(conn, env, execution, with_git=True, quiet=False):
    """Read the board (and GitHub), answer what is allowed, beat the heartbeat. One pass."""
    now = datetime.datetime.now(datetime.timezone.utc)
    bus_request.heartbeat(conn, "running", now=now, execution=execution)
    since = (now - datetime.timedelta(minutes=WINDOW_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = bus_reconcile.board_rows(env, since=since)
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

    out = bus_request.handle(rows, conn, now=now, append=appender(env), max_per_run=MAX_PER_RUN)
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

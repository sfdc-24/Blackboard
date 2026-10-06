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
    log.info("dual-run status: %s", json.dumps({k: status[k] for k in (
        "enabled", "would_attempt_connection", "auth_string_found", "auth_source",
        "ca_cert_configured")}))

    conn = redis_dual.client(settings, precheck=False)
    if conn is None:
        # No connection means no probe can be run, so there is nothing honest to answer with. The
        # requests stay unanswered and visible rather than answered with a guess.
        log.error("no Redis connection: no request can be answered. Requests are left on the board "
                  "unanswered rather than answered with something unmeasured.")
        return 1

    from bus import load_env                                             # noqa: PLC0415
    env = load_env()
    now = datetime.datetime.now(datetime.timezone.utc)
    since = (now - datetime.timedelta(minutes=WINDOW_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = bus_reconcile.board_rows(env, since=since)
    log.info("window since=%s: %d board row(s)", since, len(rows))

    out = bus_request.handle(rows, conn, now=now, append=appender(env), max_per_run=MAX_PER_RUN)
    log.info("requests: %s", bus_request.summary(out))
    for item in out["answered"]:
        log.info("answered %s: %s in %dms", item["req_id"], item["status"], item["latency_ms"])
    for item in out["refused"]:
        log.warning("refused %s: %s", item["req_id"] or "(no id)", "; ".join(item["why"]))
    if out["capped"]:
        log.warning("%d request(s) left for the next run by the per-run cap", out["capped"])
    # A refusal is a correct outcome and an error inside the probe is a reported one, so neither
    # fails the execution. Only being unable to reach Redis at all does, above.
    return 0


if __name__ == "__main__":
    sys.exit(main())

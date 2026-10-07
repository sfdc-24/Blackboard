"""The reconciler as a Cloud Run job: one bounded comparison per execution, then exit.

WHY A JOB AND NOT A CHANGE TO AN EXISTING SERVICE
The reconciler needs VPC egress to reach a private Memorystore address. Adding egress to the chair or
the gateway would mean a new revision of a service that is working, for no benefit to that service,
while a review is open on one of them. A job of its own touches neither, and when phase 2 needs the
chair to read from Redis the egress goes on then, with a live-call check first.

HOW THE EXIT CODE IS MAPPED, AND WHY IT IS NOT THE OBVIOUS WAY
`bus_reconcile` exits 0 for AGREE, 1 for DIVERGE, 2 for UNKNOWN. Passing that straight through would
make Cloud Run treat a DIVERGENCE as a failed execution and retry it, forever, on a finding that
retrying cannot change. So:

    AGREE   -> 0   the measurement succeeded and they agree
    DIVERGE -> 0   the measurement SUCCEEDED. A divergence is the product, not a failure.
    UNKNOWN -> 1   the measurement could not be made; a retry is the right response

That last line is the only one worth retrying, and it is also the one that must never be mistaken for
zero divergence - which is the property `bus_reconcile.compare` is built around.

WHAT IT READS FROM THE ENVIRONMENT
    BUS_URL, BUS_SECRET          secret-backed, by the job's own least-privilege service account
    REDIS_HOST, REDIS_PORT       the instance; never committed to this public repository
    REDIS_CA_CERT_PATH           the server CA; defaults to the mount above when the file is there
    REDIS_DUAL_ENABLED           the off-switch; this job sets it for itself and nothing else does
    RECONCILE_WINDOW_MINUTES     how far back to compare (default 90)
    RECONCILE_MIRROR             "1" to backfill the window as well as compare it. DEFAULT OFF.
                                 A measurement run does not write. When it is on, the verdict
                                 recorded is the one from BEFORE the backfill, with the state
                                 after it reported separately - a backfill cannot grade itself.

The Redis AUTH string is NOT here. redis_dual reads it from Secret Manager at connect time, so it
never becomes an environment variable anyone can list.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sys

sys.path.insert(0, "/app")

import bus_reconcile                                                     # noqa: E402
import redis_dual                                                        # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("bus-reconciler")

# WHERE THIS JOB'S SECRETS LAND, as cloud/bus-reconciler/deploy.sh mounts them. Defaulted HERE
# rather than passed in as environment values, for two reasons. Git Bash rewrites a Unix path on its
# way to gcloud and did so inconsistently - the first deploy of this job received
# REDIS_CA_CERT_PATH=C:/Program Files/Git/secrets/ca/redis-ca.pem while the auth path came through
# untouched, so the CA was mounted correctly and then looked for somewhere that does not exist. And
# more durably: the mount layout is one fact, and a deploy flag plus an environment variable is two
# places for it to be written down and disagree.
MOUNTED_CA = "/secrets/ca/redis-ca.pem"
MOUNTED_AUTH = "/secrets/auth/redis-auth"
for _name, _default in (("REDIS_CA_CERT_PATH", MOUNTED_CA), ("REDIS_AUTH_FILE", MOUNTED_AUTH)):
    if not os.environ.get(_name) and os.path.exists(_default):
        os.environ[_name] = _default

# 24 hours by default. The first execution used 90 minutes and the board had been quiet, so it
# measured nothing - a window too narrow to contain rows makes the job report UNKNOWN for a
# reason that has nothing to do with the two stores agreeing.
WINDOW_MINUTES = int(os.environ.get("RECONCILE_WINDOW_MINUTES", "1440"))
# DEFAULT OFF. Copilot, PR 323: this defaulted to on and deploy.sh set it explicitly, so every
# scheduled execution repaired the store and then compared against what it had just written. The
# span of AGREE records that produced could not establish zero divergence, which is the gate.
MIRROR = os.environ.get("RECONCILE_MIRROR", "0").strip().lower() in ("1", "true", "yes", "on")


def since_iso(minutes: int) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def library_version() -> str:
    """The resolved redis version, logged so the range in requirements.txt can be pinned exactly.

    requirements.txt asks for redis>=5,<6 only for the first build, because I will not write a
    version number I have not seen resolve. This is how the real one gets read back out."""
    try:
        import redis
        return getattr(redis, "__version__", "unknown")
    except ImportError:
        return "absent"


def main() -> int:
    log.info("redis library version: %s", library_version())
    settings = redis_dual.Settings()
    status = redis_dual.status(settings)
    log.info("dual-run status: %s", json.dumps({k: status[k] for k in redis_dual.STATUS_FOR_LOG}))

    conn = redis_dual.client(settings)
    if conn is None:
        # UNKNOWN, and loudly. An execution that could not reach Redis has measured nothing, and
        # nothing is not agreement. Exit 1 so the execution is marked failed and retried.
        log.error("UNKNOWN: no Redis connection, so nothing was compared. THIS IS NOT ZERO "
                  "DIVERGENCE. Check enabled, the host, egress, and the AUTH secret.")
        return 1

    from bus import load_env                                             # noqa: PLC0415
    since = since_iso(WINDOW_MINUTES)
    rows = bus_reconcile.board_rows(load_env(), since=since)
    log.info("window since=%s: %d board row(s)", since, len(rows))

    result = bus_reconcile.run(conn, rows, do_mirror=MIRROR, window="since=%s" % since)
    verdict = result["verdict"]

    # Counts and column NAMES only. Contents never reach a log line: the board carries his words and
    # other people's, and this job's output is readable by anyone with log access.
    log.info("%s (%s): checked=%d agreed=%d missing=%d differing=%d extra=%d out_of_window=%d "
             "unplaceable=%d duplicates=%d no_id=%d",
             verdict, result.get("measured", "?"), result["checked"], result["agreed"],
             result["missing_from_redis"], result["differing"], result["extra_in_redis"],
             result.get("outside_the_window", 0), result.get("unplaceable_in_redis", 0),
             result["duplicate_row_ids"], result["rows_without_an_id"])
    if result.get("after_backfill"):
        # Said plainly, because the two numbers are easy to confuse and only one of them is the
        # measurement: the verdict above is the state this run FOUND, this line is what it LEFT.
        log.info("after the backfill: %s. The verdict above is the pre-repair measurement and is "
                 "the one recorded; this line is the state this run left behind.",
                 json.dumps(result["after_backfill"], sort_keys=True))
    if result.get("note"):
        log.warning("%s", result["note"])
    if result.get("columns_that_differ"):
        log.warning("columns that differ: %s", json.dumps(result["columns_that_differ"], sort_keys=True))
    if not result.get("recorded"):
        log.warning("the run could not be written to %s; the verdict above still stands",
                    bus_reconcile.COMPARE_KEY)

    if verdict == bus_reconcile.UNKNOWN:
        log.error("UNKNOWN: nothing in Redis to compare against. Not zero divergence.")
        return 1
    if verdict == bus_reconcile.DIVERGE:
        # Deliberately 0: the measurement worked. Retrying a divergence changes nothing, and a job
        # that fails on its own findings is a job somebody turns off.
        log.warning("DIVERGE is a finding, not a failure: exiting 0 so this is not retried. The "
                    "counts above and %s are the record.", bus_reconcile.COMPARE_KEY)
    return 0


if __name__ == "__main__":
    sys.exit(main())

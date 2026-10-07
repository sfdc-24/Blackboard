"""Cloud Run entrypoint for the whole-board verification. python -B verify_board.py

A SEPARATE ENTRYPOINT, NOT A REPLACEMENT OF main.py, and deliberately so. Codex: "Retire and delete
bus_reconcile.py after replacing its production entrypoint and disabling its old gate path.
Deleting it now would break cloud/bus-reconciler/main.py, which still calls the time-windowed
runner. Merely copying bus_ranges.py into the image does not switch the job."

He is right, and the switch is an owner decision rather than a side effect of a build. Shipping this
file changes nothing about what runs; pointing a job at it is one argument:

    gcloud run jobs update bus-reconciler --project=sfdc24 --region=us-central1 \\
      --args=-B,verify_board.py

Until that is run, the deployed job still executes the legacy time-windowed reconciler, which Codex
has refused three times. That is stated here rather than left for somebody to discover.

IT NEEDS THE GATEWAY'S RANGE VERB. bus.read_range refuses a reply that does not echo the start it
asked for, so against the gateway as deployed today this exits 2 and compares nothing - correctly,
and loudly, rather than quietly comparing the wrong rows.
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bus_verify                                                      # noqa: E402
import redis_dual                                                      # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("verify-board")

CHUNK = 200
# OFF by default. A run that writes is ineligible for a span by construction, so a scheduled
# verification must not quietly become a repair.
MIRROR = os.environ.get("VERIFY_MIRROR", "0").strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    conn = None
    try:
        settings = redis_dual.Settings()
        status = redis_dual.status(settings)
        log.info("dual-run status: %s", json.dumps({k: status[k] for k in redis_dual.STATUS_FOR_LOG}))
        conn = redis_dual.client(settings, precheck=False)
        if conn is None:
            raise ConnectionError("no Redis connection")
        from bus import load_env, read_range                            # noqa: PLC0415
        env = load_env()
        chunk = int(os.environ.get("VERIFY_CHUNK", str(CHUNK)))
        result = bus_verify.run_checked(conn, lambda f, c: read_range(env, f, c),
                                        size=chunk, mirror=MIRROR)
    except (Exception, SystemExit) as error:
        result = bus_verify.failed_record("startup", error)

    # The durable line goes to stdout, which here is Cloud Logging: redis-central has persistence
    # DISABLED, so the stream is an accelerator and this is the record.
    bus_verify.record_run(conn, result, log=print)

    eligible = bus_verify.gate_eligible(result)
    log.info("%s: %d data row(s), %d matched, rows %d-%d of %s, gate_eligible=%s",
             result["verdict"], result["data_rows"], result["matched"], result["covered_from"],
             result["covered_to"], result["frozen_total"], eligible)
    for name in ("missing_from_redis", "differing", "extra_in_redis", "duplicate_row_ids",
                 "rows_without_an_id", "read_failures", "positions_shifted", "writes_performed"):
        if result.get(name):
            log.warning("  %s=%d", name, result[name])

    if result["verdict"] == bus_verify.UNKNOWN:
        return 1                      # retry: something could not be read
    # DIVERGE exits 0 on purpose: the measurement worked, and a job that fails on its own findings
    # is a job somebody turns off. NO_SAMPLE exits 0 for the same reason.
    return 0


if __name__ == "__main__":
    sys.exit(main())

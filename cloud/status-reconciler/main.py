"""The R-STATUS reconciler as a Cloud Run job: one full board read, one
recompute, one batch write of columns M to R, then exit. Runs every 5 minutes
(docs/BOARD-STATUS-FRAMEWORK.md).

WHY A JOB
Same reasoning as cloud/bus-reconciler: a job of its own touches no existing
service, needs no new egress or revision on something already working, and a
21,000+ row board read is minutes, not milliseconds - the wrong shape for a
request/response service.

WHAT IT DOES
  1. Reads the WHOLE board (scripts/board_say.bus - the same read path every
     other reader on this fleet uses, not a private copy of it).
  2. scripts/rstatus_reconcile.reconcile_all() - pure, no network - computes
     every row's Status/Expected_From/Expected_Response/Due_UTC/Closed_By/
     Status_Updated_UTC.
  3. Diffs against the PREVIOUS run's Status column (read straight off the
     board - column M IS the previous run's own output) to find newly_expired()
     asks and holds_for_reverification() - both LOGGED, never posted; a board
     write from this job is out of scope for now (blackboard-5e, 2026-11-14).
  4. Writes ONLY the rows whose M-R values actually changed, in batches,
     through scripts/status_client.StatusClient - the gas-bus gateway's
     status_write action, which refuses anything outside M:R on its own side.
     Never touches columns A to L.

--dry prints the same summary (counts per status, the escalation list, the
hold re-verify groups) and makes no write and no board-mutating call at all.

WHAT IT READS FROM THE ENVIRONMENT
    BUS_URL, BUS_SECRET   secret-backed, this job's own least-privilege service account -
                          the same credential every writer on this fleet already holds
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sys

sys.path.insert(0, "/app")

import board_say                                                          # noqa: E402
import rstatus_reconcile as rs                                            # noqa: E402
from status_client import StatusClient                                   # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("status-reconciler")

# M (Status) is column index 12, 0-based - the FIRST of the six this job owns.
STATUS_COL_INDEX = 12


def read_board():
    env = board_say.load_env()
    code, body = board_say.bus(env, {"secret": env["BUS_SECRET"], "action": "read",
                                     "title": board_say.BOARD})
    if not str(body).lstrip().startswith("{"):
        raise RuntimeError("DEGRADED: HTTP %s, non-JSON board read" % code)
    data = json.loads(body)
    if "rows" not in data:
        raise RuntimeError("DEGRADED: HTTP %s, health reply %s, not a board"
                           % (code, sorted(data.keys())))
    return env, data["rows"][1:]   # header stripped; rstatus_reconcile wants data rows only


def previous_statuses(rows):
    """{BCB id: the Status column's CURRENT value}, read straight off the board -
    this run's "old" is simply what the last run (or the setup script's blank
    header) already wrote there. A row too short to have reached column M yet
    (pre-R-STATUS history) has no previous status at all."""
    out = {}
    for row in rows:
        payload = str(row[5]) if len(row) > 5 and row[5] is not None else ""
        rid = rs.field(payload, "id")
        if not rid:
            continue
        out[rid] = str(row[STATUS_COL_INDEX]) if len(row) > STATUS_COL_INDEX and row[STATUS_COL_INDEX] else None
    return out


def updates_for(rows, results, row_id_to_sheet_values):
    """[{"row_id", "values"}] for every id whose computed M-R values differ
    from what the board already shows - never a no-op write, and never a
    write for a row this run could not identify (no BCB id)."""
    out = []
    for rid, result in results.items():
        new_values = rs.to_row_values(result)
        if row_id_to_sheet_values.get(rid) != new_values:
            out.append({"row_id": rid, "values": new_values})
    return out


def current_sheet_values(rows):
    """{BCB id: the six M-R cells as they stand now}, for the unchanged-write skip above."""
    out = {}
    for row in rows:
        payload = str(row[5]) if len(row) > 5 and row[5] is not None else ""
        rid = rs.field(payload, "id")
        if not rid:
            continue
        out[rid] = [str(row[i]) if len(row) > i and row[i] is not None else ""
                    for i in range(STATUS_COL_INDEX, STATUS_COL_INDEX + 6)]
    return out


def summarize(results, escalations, reverify):
    from collections import Counter
    counts = Counter(r["status"] for r in results.values())
    return {
        "row_count": len(results),
        "by_status": dict(counts),
        "newly_expired": len(escalations),
        "newly_expired_sample": escalations[:20],
        "holds_for_reverification": {
            "owner_decision": len(reverify["owner_decision"]),
            "other": len(reverify["other"]),
        },
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry" in argv
    now = datetime.datetime.now(datetime.timezone.utc)

    env, rows = read_board()
    old_statuses = previous_statuses(rows)
    results = rs.reconcile_all(rows, now=now)
    escalations = rs.newly_expired(old_statuses, results)
    reverify = rs.holds_for_reverification(rows, now=now)
    summary = summarize(results, escalations, reverify)

    if dry:
        print(json.dumps({"dry": True, **summary, "reverify": reverify}, indent=2))
        return 0

    sheet_now = current_sheet_values(rows)
    updates = updates_for(rows, results, sheet_now)
    if not updates:
        log.info("nothing changed: %s", json.dumps(summary))
        return 0

    client = StatusClient(env)
    write_result = client.write(updates)
    log.info("wrote %d row(s), %d miss(es): %s",
             write_result["written"], len(write_result["misses"]), json.dumps(summary))
    if write_result["misses"]:
        log.warning("row_id(s) the gateway could not find in column A: %s",
                   ", ".join(write_result["misses"][:20]))
    if escalations:
        log.warning("%d newly EXPIRED ask(s): %s", len(escalations),
                   ", ".join(e["id"] for e in escalations[:20]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

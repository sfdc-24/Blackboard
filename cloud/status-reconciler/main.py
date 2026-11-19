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
  2. Reads back the sheet's own current M/P/Q/R values (Status, Due_UTC,
     Closed_By, Status_Updated_UTC) by Row_ID, for the idempotence rule
     rstatus_reconcile.reconcile_all() needs (a result that has not actually
     changed keeps its old due/updated instead of a fresh `now` every run -
     adversarial review, 2026-11-14: without this, 1,321 EXPIRED rows and
     every backfilled "due in 24h" ask were rewritten on every single cycle).
  3. rstatus_reconcile.reconcile_all() - pure, no network - computes every
     row's Status/Expected_From/Expected_Response/Due_UTC/Closed_By/
     Status_Updated_UTC, KEYED BY THE ROW'S OWN SHEET Row_ID (column A), the
     same identity status_write matches on - never the row's BCB id= (the
     same review: keying by id= left 31% of rows permanently unwritable).
  4. Diffs against that same previous read to find newly_expired() asks and
     holds_for_reverification() - both LOGGED, never posted; a board write
     from this job is out of scope for now (blackboard-5e, 2026-11-14).
  5. Writes ONLY the rows whose M-R values actually changed, in batches,
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

# M to R, 0-based column indices - Status, Expected_From, Expected_Response,
# Due_UTC, Closed_By, Status_Updated_UTC.
STATUS_COL_INDEX = 12
DUE_COL_INDEX = 15
CLOSED_BY_COL_INDEX = 16
STATUS_UPDATED_COL_INDEX = 17


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


def _row_id(row):
    """The sheet's own Row_ID (column A) for a row, or None for one this
    module cannot identify (no Row_ID at all)."""
    rid = str(row[0]).strip() if len(row) > 0 and row[0] is not None else ""
    return rid or None


def previous_results(rows):
    """{Row_ID: {"status", "due_utc", "closed_by", "status_updated_utc"}},
    read straight off the board - this run's "previous" is simply what the
    last run (or the setup script's blank header) already wrote there, keyed
    by the SAME Row_ID identity reconcile_all() now uses (never the BCB id=;
    see rstatus_reconcile's IDENTITY). A row too short to have reached column
    M yet (pre-R-STATUS history) has no previous result at all."""
    out = {}
    for row in rows:
        rid = _row_id(row)
        if not rid:
            continue

        def cell(i):
            return str(row[i]) if len(row) > i and row[i] is not None else ""

        status = cell(STATUS_COL_INDEX) or None
        if status is None:
            continue
        out[rid] = {
            "status": status,
            "due_utc": rs.parse_ts(cell(DUE_COL_INDEX)),
            "closed_by": cell(CLOSED_BY_COL_INDEX) or None,
            "status_updated_utc": rs.parse_ts(cell(STATUS_UPDATED_COL_INDEX)),
        }
    return out


def updates_for(results, row_id_to_sheet_values):
    """[{"row_id", "values"}] for every Row_ID whose computed M-R values
    differ from what the board already shows - never a no-op write, and never
    a write for a row this run could not identify."""
    out = []
    for rid, result in results.items():
        new_values = rs.to_row_values(result)
        if row_id_to_sheet_values.get(rid) != new_values:
            out.append({"row_id": rid, "values": new_values})
    return out


def current_sheet_values(rows):
    """{Row_ID: the six M-R cells as they stand now}, for the unchanged-write
    skip above."""
    out = {}
    for row in rows:
        rid = _row_id(row)
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
    previous = previous_results(rows)
    old_statuses = {rid: r["status"] for rid, r in previous.items()}
    results = rs.reconcile_all(rows, now=now, previous=previous)
    escalations = rs.newly_expired(old_statuses, results)
    reverify = rs.holds_for_reverification(rows, now=now)
    summary = summarize(results, escalations, reverify)

    if dry:
        print(json.dumps({"dry": True, **summary, "reverify": reverify}, indent=2))
        return 0

    sheet_now = current_sheet_values(rows)
    updates = updates_for(results, sheet_now)
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

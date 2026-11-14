#!/usr/bin/env python3
"""One-time setup for R-STATUS (docs/BOARD-STATUS-FRAMEWORK.md): the M1:R1
header names, data validation on column M, the warning-only protected range
on columns A to L, and the "Open asks" filter view.

Idempotent on the gateway's own side (gas-bus/Code.js handleStatusSetup_):
a blank header cell is set, a cell that already matches is left alone, and a
cell holding anything else refuses the whole call rather than overwriting a
column this setup does not own. The filter view is skipped, not failed, when
the Advanced Sheets service is not enabled on the Apps Script project.

--dry by default: prints what WOULD be sent and makes no call at all. Pass
--live to actually call status_setup through scripts/status_client.py.

USAGE
  python scripts/status_sheet_setup.py          # --dry, the default
  python scripts/status_sheet_setup.py --live
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import load_env  # noqa: E402
from status_client import StatusClient, TITLE  # noqa: E402


def main():
    live = "--live" in sys.argv
    if not live:
        print(json.dumps({
            "dry": True,
            "would_call": {"action": "status_setup", "title": TITLE},
            "note": "idempotent on the gateway's own side; pass --live to actually run it",
        }, indent=2))
        return 0

    env = load_env()
    client = StatusClient(env)
    report = client.setup()
    print(json.dumps({"dry": False, "report": report}, indent=2))
    if report.get("filter_view_skipped_reason"):
        print("NOTE: filter view skipped: %s" % report["filter_view_skipped_reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

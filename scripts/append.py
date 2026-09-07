#!/usr/bin/env python3
"""Append one board row from a JSON file, then read it back.

Usage: python append.py rowfile.json
The JSON file is an object with keys: row_id, source_tag, target_surface,
action_type, payload, category, project_tag, gist, subgist.
Timestamp is stamped here in UTC.
"""
import datetime
import json
import sys

from bus import fetch, load_env, read_board

TITLE = "Blackboard - Alpha DB"


def main():
    env = load_env()
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row = [
        spec["row_id"],
        ts,
        spec.get("source_tag", "vm-cli"),
        spec.get("target_surface", "ALL"),
        spec.get("action_type", "APPEND"),
        spec["payload"],
        spec.get("category", "OPEN"),
        spec.get("project_tag", ""),
        spec.get("gist", ""),
        spec.get("subgist", ""),
    ]
    body = fetch(
        env["BUS_URL"],
        {
            "action": "append",
            "secret": env["BUS_SECRET"],
            "title": TITLE,
            "sheetRow": row,
        },
    )
    print("APPEND response:", body[:300])

    # read back — ok:true is not proof (ISSUE 020)
    obj = read_board(env)
    for r in obj["rows"][-6:]:
        if r and r[0] == spec["row_id"]:
            print("READ-BACK OK: row present, %d cells" % len(r))
            print("  ts=%s from=%s cat=%s tag=%s" % (r[1], r[2], r[6], r[7]))
            print("  payload starts: %s" % str(r[5])[:80])
            return
    print("READ-BACK FAILED: row_id not in the last 6 rows")
    sys.exit(1)


if __name__ == "__main__":
    main()

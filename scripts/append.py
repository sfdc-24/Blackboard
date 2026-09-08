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
    # tries=1 is load-bearing. fetch() defaults to 5 attempts, which is right for
    # reads and WRONG here: the v1 bus does not dedup, and a googleusercontent 404
    # on the redirect hop is raised client-side AFTER the row has already landed.
    # On 2026-09-08 the default retry doubled WRK-vmccc-xray-blocker-20260908T1630Z
    # on the live board — two 404s, three appends, one intended row. Never let an
    # append retry itself; the read-back below is what closes the loop.
    try:
        body = fetch(
            env["BUS_URL"],
            {
                "action": "append",
                "secret": env["BUS_SECRET"],
                "title": TITLE,
                "sheetRow": row,
            },
            tries=1,
        )
        print("APPEND response:", body[:300])
    except Exception as exc:  # noqa: BLE001 - the transport failure is not the answer
        print("APPEND raised %s: %s" % (type(exc).__name__, exc))
        print("This does NOT mean the row is absent. Reading back to find out.")

    # read back — ok:true is not proof, and an exception is not proof of absence
    # (ISSUE 020 and its inverse). Reads are idempotent, so retry those freely.
    for attempt in range(3):
        try:
            obj = read_board(env)
        except Exception as exc:  # noqa: BLE001
            print("read-back attempt %d failed: %s" % (attempt + 1, exc))
            continue
        hits = [r for r in obj["rows"] if r and r[0] == spec["row_id"]]
        if len(hits) == 1:
            r = hits[0]
            print("READ-BACK OK: row present exactly once, %d cells" % len(r))
            print("  ts=%s from=%s cat=%s tag=%s" % (r[1], r[2], r[6], r[7]))
            print("  payload starts: %s" % str(r[5])[:80])
            return
        if len(hits) > 1:
            print("READ-BACK: DUPLICATE — %d copies of %s are on the board." % (len(hits), spec["row_id"]))
            print("  Do NOT re-run. Post a correction row naming the duplicate.")
            sys.exit(2)
        print("read-back attempt %d: row_id not found yet" % (attempt + 1))
    print("READ-BACK FAILED: row_id absent after 3 reads. Check the board by hand")
    print("before re-running — a blind re-run is how duplicates happen.")
    sys.exit(1)


if __name__ == "__main__":
    main()

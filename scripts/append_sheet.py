#!/usr/bin/env python3
"""Append explicit rows to any bus-reachable sheet, then read back.

Spec JSON: {"title": "<sheet title>", "key_col": 0, "rows": [[...], [...]]}
Appends one row at a time and verifies each by its key column before the next,
because the v1 bus does not dedup and a client-side error does NOT prove the
row was rejected (L-91).
"""
import json
import sys

from bus import fetch, load_env, read_board


def main():
    env = load_env()
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    title, key_col = spec["title"], spec.get("key_col", 0)

    existing = {str(r[key_col]) for r in read_board(env, title=title)["rows"][1:] if r}

    for row in spec["rows"]:
        key = str(row[key_col])
        if key in existing:
            print(f"SKIP {key}: already present")
            continue
        body = fetch(
            env["BUS_URL"],
            {"action": "append", "secret": env["BUS_SECRET"], "title": title,
             "sheetRow": row},
        )
        print(f"append {key}: {body[:120]}")
        back = read_board(env, title=title)["rows"][1:]
        hits = [r for r in back if r and str(r[key_col]) == key]
        if len(hits) != 1:
            print(f"  READ-BACK FAILED for {key}: {len(hits)} copies")
            sys.exit(1)
        print(f"  READ-BACK OK: {key}, {len(hits[0])} cells")
        existing.add(key)


if __name__ == "__main__":
    main()

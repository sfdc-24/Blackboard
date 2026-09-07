#!/usr/bin/env python3
"""Print full payloads of the last N rows, skipping ones I wrote."""
import sys

from bus import load_env, read_board

n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
obj = read_board(load_env())
for r in obj["rows"][-n:]:
    r = list(r) + [""] * (10 - len(r))
    if str(r[0]).startswith("WRK-vmcli-"):
        print(f"\n### [{r[1]}] {r[2]} -> {r[3]} :: (mine, skipped) {r[0]}")
        continue
    print(f"\n### [{r[1]}] {r[2]} -> {r[3]} :: {r[0]}  cat={r[6]} tag={r[7]}")
    print(f"GIST: {r[8]}")
    print(str(r[5])[:2600])

#!/usr/bin/env python3
"""Read any sheet the bus can reach, by title. Prints header + tail."""
import sys

from bus import load_env, read_board

title = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
obj = read_board(load_env(), title=title)
rows = obj["rows"]
print("HEADER:", rows[0])
print("data rows:", len(rows) - 1)
for r in rows[-n:]:
    print("---")
    for i, cell in enumerate(r):
        name = rows[0][i] if i < len(rows[0]) else f"col{i}"
        print(f"  {name}: {str(cell)[:300]}")

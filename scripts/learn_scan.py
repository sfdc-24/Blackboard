#!/usr/bin/env python3
"""List LEARNINGS categories and scan rules for keywords, to avoid duplicates."""
import sys

from bus import load_env, read_board

obj = read_board(load_env(), title="SFDC24 — LEARNINGS (Rules Sheet)")
rows = obj["rows"][1:]
cats = {}
for r in rows:
    cats[str(r[1])] = cats.get(str(r[1]), 0) + 1
print("CATEGORIES:", sorted(cats.items(), key=lambda kv: -kv[1]))
print("last L_ID:", rows[-1][0])
for kw in sys.argv[1:]:
    print(f"\n--- rules mentioning {kw!r} ---")
    for r in rows:
        if kw.lower() in str(r[2]).lower():
            print(f"  {r[0]}: {str(r[2])[:150]}")

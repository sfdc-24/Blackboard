#!/usr/bin/env python3
"""Print full payloads for rows whose BCB id= matches any given argument."""
import re
import sys

from bus import load_env, read_board

wanted = set(sys.argv[1:])
for r in read_board(load_env())["rows"][1:]:
    if not r or len(r) < 6:
        continue
    m = re.search(r"\|id=([^|]*)", str(r[5]))
    if m and m.group(1).strip() in wanted:
        print(f"\n{'='*70}\n[{r[1]}] {r[2]} -> {r[3]}  row={r[0]}")
        print(str(r[5])[:3200])

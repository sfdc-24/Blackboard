#!/usr/bin/env python3
"""Print every cell of a row by Row_ID, to prove column alignment."""
import sys

from bus import load_env, read_board


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python scripts/show_row.py <row_id>")
    want = sys.argv[1]
    rows = read_board(load_env())["rows"]
    hdr = rows[0]
    hits = [r for r in rows[1:] if r and str(r[0]) == want]
    print(f"rows with Row_ID={want}: {len(hits)}")
    if len(hits) > 1:
        print("  DUPLICATE: this row_id is on the board more than once.")
    for r in hits:
        for i, name in enumerate(hdr):
            cell = str(r[i]) if i < len(r) else "<MISSING>"
            print(f"  [{i}] {name:16} = {cell[:120]}")
        print("  payload full length:", len(str(r[5])) if len(r) > 5 else 0)


if __name__ == "__main__":
    main()

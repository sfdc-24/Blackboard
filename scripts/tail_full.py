#!/usr/bin/env python3
"""Print full payloads of the last N rows."""
import sys

from bus import load_env, read_board


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    obj = read_board(load_env())
    for r in obj["rows"][-n:]:
        r = list(r) + [""] * (10 - len(r))
        print(f"\n### [{r[1]}] {r[2]} -> {r[3]} :: {r[0]}  cat={r[6]} tag={r[7]}")
        print(f"GIST: {r[8]}")
        print(str(r[5]))


if __name__ == "__main__":
    main()

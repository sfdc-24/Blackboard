#!/usr/bin/env python3
"""Compact one-line-per-row view of the board tail."""
import re
import sys

from bus import load_env, read_board


def field(payload, key):
    # Sheet cells are not guaranteed to be strings: an empty payload comes back
    # as None and a numeric-looking one as a float, both of which make re.search
    # raise TypeError and take the whole summary down over one odd row.
    m = re.search(r"\|" + key + r"=([^|]*)", str(payload or ""))
    return (m.group(1).strip() if m else "")[:90]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    obj = read_board(load_env())
    rows = obj["rows"]
    hdr = rows[0]
    print("HEADER:", hdr)
    print("total data rows:", len(rows) - 1)
    for r in rows[-n:]:
        r = list(r) + [""] * (8 - len(r))
        row_id, ts, frm, to, kind, payload, status = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        print(
            f"{str(ts)[:20]:22} {str(frm)[:22]:22} -> {str(to)[:14]:14} "
            f"{str(kind)[:8]:8} {str(status)[:6]:6} "
            f"id={field(payload,'id'):26} phase={field(payload,'phase'):9} "
            f"prio={field(payload,'priority'):8} {str(row_id)[:14]}"
        )


if __name__ == "__main__":
    main()

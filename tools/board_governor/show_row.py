#!/usr/bin/env python3
"""Print one board row in full, by id fragment. No truncation.

The samplers all clip payloads to keep their output readable, which is right for
scanning and wrong for answering. A baton is an instruction; replying to one
from its header alone is how you answer a question nobody asked.

USAGE
  python show_row.py <board.json> <id-fragment> [more fragments...]
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from board_facts import COLUMNS, load_rows  # noqa: E402


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    rows, _ = load_rows(argv[0])
    wanted = argv[1:]

    for fragment in wanted:
        hits = [r for r in rows if fragment in r.row_id or fragment in r.bcb_id]
        print("=" * 74)
        print("{}   ({} matching row(s))".format(fragment, len(hits)))
        for row in sorted(hits, key=lambda r: r.ts):
            cells = [row.row_id, row.ts.isoformat(), row.tag, row.target,
                     row.action, row.payload, row.category]
            print("-" * 74)
            for name, value in zip(COLUMNS, cells):
                if name == "Payload":
                    print("  {}:".format(name))
                    # Split on the BCB pipe so the grammar is readable, but
                    # print every field - nothing elided.
                    for part in str(value).split("|"):
                        print("      {}".format(part))
                else:
                    print("  {:<14}: {}".format(name, value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

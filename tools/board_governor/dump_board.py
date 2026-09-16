#!/usr/bin/env python3
"""Dump the live board to JSON for the governor to replay.

This exists as a FILE and not as a `python -c` one-liner because Windows
PowerShell strips double quotes out of native-command arguments: the inline
version reached the interpreter as open(p,w,encoding=utf-8) and died on a
syntax error. Every quoted literal the governor needs therefore lives in a file
that no shell rewrites on the way in.

Reads through scripts/bus.py so the dump is whatever the fleet's own bus
returns - no second HTTP client, no second idea of what a row is.

THIS IS THE ONLY FILE HERE THAT TOUCHES THE NETWORK. Everything else in
tools/board_governor/ works on a dump, which is what makes the suite offline and
the analysis replayable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# tools/board_governor/dump_board.py -> parents[2] is the repository root.
#
# The previous value was parents[1]/"main"/"scripts", which was correct only
# while this file lived in a scratchpad beside an anchored checkout. It resolved
# to a directory that did not exist and failed at load_env() with a misleading
# "no .env" message. Derive the root from the repository layout, and say so
# plainly if the bus client is not where it should be.
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_SCRIPTS = REPO_ROOT / "scripts"
if not (REPO_SCRIPTS / "bus.py").is_file():
    raise SystemExit(
        "cannot find {}\n"
        "dump_board.py expects to live at tools/board_governor/ inside the "
        "repository, so that scripts/bus.py sits two levels up.".format(
            REPO_SCRIPTS / "bus.py")
    )
sys.path.insert(0, str(REPO_SCRIPTS))

from bus import load_env, read_board  # noqa: E402


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("board.json")
    blob = read_board(load_env())
    rows = blob["rows"]
    out.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    print(f"dumped {len(rows)} rows -> {out}")
    for row in rows[-4:]:
        cells = list(row) + [""] * (10 - len(row))
        print(f"  {cells[1]} | {cells[2]} -> {str(cells[3])[:26]} | {cells[0]}")


if __name__ == "__main__":
    main()

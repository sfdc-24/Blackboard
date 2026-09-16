#!/usr/bin/env python3
"""Mutation testing for the unanswered-ask rule. Is each guard load-bearing?

WHY
  I have criticised guards-no-test-can-fail in three reviews this session. The
  epoch floor and the (not (answered ...)) condition are both guards I currently
  believe work because a NUMBER MOVED when I added them. A number moving is not
  proof the guard is doing the work I claim; it is consistent with several other
  explanations. So each guard gets deleted on purpose, and the run must change
  in the direction I predicted IN ADVANCE.

  A mutant that changes nothing is an untested claim, reported as such rather
  than quietly dropped.

HOW
  The whole tool directory is copied per mutant and edited in the COPY, so a
  half-applied edit can never leave the real rule file mutated - the failure
  mode that would silently poison every later run.

  Every mutant is anchor-checked: if its pattern is not found, it reports
  ANCHOR-MISS rather than a false survivor. An edit that matched nothing and a
  guard that is genuinely untested look identical in the output otherwise.

WHY IT NEEDS A CLIPS BINARY, AND SO IS NOT IN HOSTED CI
  These mutants change RULES, so the verdict only exists once the rule engine
  runs. governor.py shells out to CLIPSDOS.exe, which is Windows-only, so this
  harness runs on a machine that has CLIPS. tests/test_board_governor.py is the
  offline half and is the one the hosted workflow runs.

USAGE
  python tests/mutate_board_governor.py <board-dump.json>
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# This harness lives in tests/ but MUTATES tools/board_governor/. Copying the
# directory this file sits in would copy the whole test suite and then look for
# governor.py inside it, which reports ANCHOR-MISS on every mutant - a harness
# that fails silently while appearing to run.
HERE = Path(__file__).resolve().parent
TOOL_DIR = HERE.parent / "tools" / "board_governor"
if not (TOOL_DIR / "governor.py").is_file():
    raise SystemExit("cannot find {} - is this file still in tests/?".format(
        TOOL_DIR / "governor.py"))

SUMMARY = re.compile(r"asks=(\d+).*?unanswered=(\d+)")

# (name, file, from, to, claim, predicted direction for `unanswered`)
MUTANTS = [
    (
        "M1-drop-the-grammar-epoch-floor",
        "collision.clp",
        "(ts ?ts&:(< ?ts (- ?now 3600))&:(> ?ts ?epoch)))",
        "(ts ?ts&:(< ?ts (- ?now 3600))))",
        "asks written before answers= existed are excluded from the verdict",
        "up",
    ),
    (
        "M2-drop-the-answered-check",
        "collision.clp",
        "   (not (answered (id ?id)))\n",
        "",
        "an ask is only reported when NOTHING answers its id",
        "up",
    ),
    (
        "M3-drop-the-quiet-threshold",
        "collision.clp",
        "(ts ?ts&:(< ?ts (- ?now 3600))&:(> ?ts ?epoch)))",
        "(ts ?ts&:(> ?ts ?epoch)))",
        "an ask in flight for under an hour is not yet unanswered",
        "up",
    ),
    (
        "M4-treat-a-report-phase-as-an-ask",
        "board_facts.py",
        '    "DISPATCH", "REVIEW_REQUEST", "REQUEST",',
        '    "DISPATCH", "REVIEW_REQUEST", "REQUEST", "PROGRESS", "RESULT",',
        "the ask allowlist is consulted, and report phases are kept out of it",
        "up",
    ),
    (
        "M5-match-the-phase-by-substring",
        "board_facts.py",
        "        if phase in ASK_PHASES:",
        "        if any(phase.startswith(p) for p in ASK_PHASES):",
        "phase membership is EXACT, so REVIEW_RESULT never matches REVIEW",
        "up",
    ),
]


def run(directory, board):
    out = subprocess.run(
        [sys.executable, str(Path(directory) / "governor.py"),
         "--board", str(board), "--log", str(Path(directory) / "mut.jsonl")],
        capture_output=True, text=True, timeout=300,
    )
    text = out.stdout + out.stderr
    match = SUMMARY.search(text)
    if not match:
        return None, None, text.strip()[:300]
    return int(match.group(1)), int(match.group(2)), text.splitlines()[0]


def main(board):
    board = Path(board).resolve()
    work = Path(tempfile.mkdtemp(prefix="govmut-"))

    base_dir = work / "baseline"
    shutil.copytree(TOOL_DIR, base_dir, ignore=shutil.ignore_patterns(
        "__pycache__", "*.jsonl", "board*.json", "generated*.clp"))
    b_asks, b_un, b_line = run(base_dir, board)
    print("BASELINE  asks={} unanswered={}".format(b_asks, b_un))
    print("  {}".format(b_line))
    if b_un is None:
        print("baseline did not produce a summary line; aborting")
        return 1

    results = []
    for name, filename, src, dst, claim, direction in MUTANTS:
        mdir = work / name
        shutil.copytree(TOOL_DIR, mdir, ignore=shutil.ignore_patterns(
            "__pycache__", "*.jsonl", "board*.json", "generated*.clp"))
        target = mdir / filename
        text = target.read_text(encoding="utf-8")
        if text.count(src) < 1:
            print("=== {} [ANCHOR-MISS] pattern absent - reports nothing "
                  "rather than a false survivor".format(name))
            results.append((name, "ANCHOR-MISS", claim))
            continue
        target.write_text(text.replace(src, dst), encoding="utf-8")

        asks, un, line = run(mdir, board)
        if un is None:
            print("=== {} [BROKEN] no summary line: {}".format(name, line))
            results.append((name, "BROKEN", claim))
            continue

        moved = un != b_un or asks != b_asks
        went = "up" if un > b_un else ("down" if un < b_un else "same")
        ok = moved and (went == direction or direction == "any")
        verdict = "KILLED" if ok else ("SURVIVED" if not moved else "WRONG-WAY")
        print("=== {} [{}]".format(name, verdict))
        print("    claim     : {}".format(claim))
        print("    predicted : unanswered goes {}".format(direction))
        print("    observed  : asks {}->{}  unanswered {}->{} ({})".format(
            b_asks, asks, b_un, un, went))
        results.append((name, verdict, claim))

    print()
    print("== summary ==")
    for name, verdict, _ in results:
        print("  {:<40} {}".format(name, verdict))
    bad = [r for r in results if r[1] != "KILLED"]
    print()
    if bad:
        print("CLAIMS NOT PROVEN BY THIS HARNESS:")
        for name, verdict, claim in bad:
            print("  {} [{}]: {}".format(name, verdict, claim))
    else:
        print("All {} mutants killed. That is {} guards shown load-bearing, "
              "not proof of full coverage.".format(len(results), len(results)))
    print("work dir: {}".format(work))
    return 0


if __name__ == "__main__":
    # The board dump is REQUIRED rather than defaulted: no board snapshot belongs
    # in the repository, and a default path would silently resolve to a file that
    # is not there.
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: python tests/mutate_board_governor.py <board-dump.json>\n"
            "Produce a dump with: python tools/board_governor/dump_board.py out.json"
        )
    raise SystemExit(main(sys.argv[1]))

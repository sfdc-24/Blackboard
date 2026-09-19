#!/usr/bin/env python3
"""Mutation testing for the filtered board read. Is each guard load-bearing?

WHY THIS EXISTS AT ALL
  tests/test_board_clients.py went from 12 passing tests to 21 passing tests
  when read_rows() landed, and a count going up is NOT evidence that the nine
  new tests constrain anything. Every guard here is one I believe works because
  a number moved, and a number moving is consistent with several duller
  explanations - including a test that asserts on a mock it configured itself.

  So each guard is deleted ON PURPOSE and the suite must break in the direction
  predicted IN ADVANCE, named per mutant below. A mutant that changes nothing is
  an untested claim, and this harness reports it as SURVIVED rather than quietly
  dropping it.

  The stakes are specific: the read-back in scripts/append.py is the only thing
  standing between an ambiguous append and a duplicate on an append-only board.
  On 2026-09-19 that read-back timed out AFTER its row had landed, which is how
  this work started. A verification step nobody has tried to break is not a
  verification step.

HOW
  scripts/ and tests/ are copied into a scratch tree per mutant and the edit is
  applied to the COPY, so a half-applied regex can never leave the real client
  mutated - the failure mode that would silently poison every later run on this
  box.

  Every mutant is anchor-checked. If its pattern is not found the mutant reports
  ANCHOR-MISS instead of a false survivor, because an edit that matched nothing
  and a guard that is genuinely untested look identical in the output otherwise.

USAGE
  python tests/mutate_bus_filters.py
  Exit 0 = every guard is load-bearing. Exit 1 = at least one survivor or
  anchor-miss, both of which mean the suite is weaker than it looks.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TARGETS = ("scripts", "tests")

# (name, file, find, replace, why-it-should-break, the test that must fail)
MUTANTS = [
    (
        "drop-match-from-the-wire",
        "scripts/bus.py",
        '        payload["match"] = str(match)',
        '        pass  # MUTANT: match never reaches the bus',
        "a match= the caller asked for is silently not sent, so the bus returns "
        "the whole board and the client reports it as a filtered slice",
        "test_filters_reach_the_wire_and_unasked_keys_stay_absent",
    ),
    (
        "trust-a-bus-that-ignored-the-filter",
        "scripts/bus.py",
        '        if asked and "filtered" not in obj:',
        '        if False:',
        "an older deployment that ignores since= returns all of history and the "
        "caller believes it is holding only recent rows",
        "test_a_bus_that_ignored_the_filter_is_refused_not_relabelled",
    ),
    (
        "serve-the-header-as-a-data-row",
        "scripts/bus.py",
        '        if rows and str((list(rows[0]) + [""])[0]).strip() == "Row_ID":',
        '        if False:',
        "a header row reaching a caller as row data is a fabricated record, and "
        "board_summary.py would then print a real row as the header",
        "test_a_header_in_a_filtered_reply_is_dropped_not_served_as_a_row",
    ),
    (
        "accept-any-limit",
        "scripts/bus.py",
        "        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:",
        "        if False:",
        "limit=True silently becomes limit=1 on the wire, so a caller asking for "
        "a bounded read gets exactly one row and cannot tell",
        "test_limit_must_be_a_positive_int_and_bool_is_not_one",
    ),
    (
        "count-match-hits-as-identity",
        "scripts/append.py",
        '        hits = [r for r in obj["rows"] if r and r[0] == spec["row_id"]]',
        '        hits = list(obj["rows"])  # MUTANT: substring hits treated as identity',
        "match= is a substring over the whole row, so any later row QUOTING this "
        "row_id becomes a phantom duplicate and the client exits 2 on a healthy "
        "append - measured at 3 hits for one real row on 2026-09-19",
        "test_quoting_rows_do_not_read_as_duplicates",
    ),
    (
        "read-back-without-narrowing",
        "scripts/append.py",
        '            obj = read_rows(env, match=spec["row_id"])',
        "            obj = read_rows(env)",
        "the read-back goes back to pulling the whole sheet, which is the "
        "timeout that made a landed append look like a failure in the first place",
        "test_the_read_back_narrows_by_this_row_id",
    ),
]


def run_suite(tree: Path):
    """Run the client suite inside a scratch tree. Returns (rc, combined text)."""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_board_clients", "-v"],
        cwd=str(tree), capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def stage() -> Path:
    tree = Path(tempfile.mkdtemp(prefix="mutate-bus-"))
    for name in TARGETS:
        shutil.copytree(ROOT / name, tree / name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    return tree


def main() -> int:
    print("BASELINE: the unmutated suite must pass, or no verdict below means anything.")
    tree = stage()
    try:
        rc, text = run_suite(tree)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    if rc != 0:
        print("  BASELINE FAILED - fix the suite before trusting any mutant.")
        print(text[-2500:])
        return 1
    ran = [ln for ln in text.splitlines() if ln.startswith("Ran ")]
    print("  baseline ok  %s" % (ran[0] if ran else "?"))

    killed, survived, missed = [], [], []
    for name, relpath, find, repl, why, must_fail in MUTANTS:
        tree = stage()
        try:
            target = tree / relpath
            src = target.read_text(encoding="utf-8")
            if find not in src:
                missed.append(name)
                print("\nANCHOR-MISS  %s" % name)
                print("  pattern absent from %s - this mutant tested NOTHING." % relpath)
                print("  Fix the pattern; do not read this as a passing guard.")
                continue
            target.write_text(src.replace(find, repl, 1), encoding="utf-8")
            rc, text = run_suite(tree)
        finally:
            shutil.rmtree(tree, ignore_errors=True)

        named_test_failed = must_fail in text and rc != 0
        if rc == 0:
            survived.append(name)
            print("\nSURVIVED     %s" % name)
            print("  predicted breakage: %s" % why)
            print("  The suite passed with the guard REMOVED, so nothing in it")
            print("  constrains this behaviour. The guard is an untested claim.")
        elif not named_test_failed:
            killed.append(name)
            print("\nkilled       %s  (suite failed, but not via %s)" % (name, must_fail))
            print("  counted as killed; the predicted test was not the one that caught it.")
        else:
            killed.append(name)
            print("\nkilled       %s  via %s" % (name, must_fail))

    print("\n%d killed, %d survived, %d anchor-miss, of %d mutants"
          % (len(killed), len(survived), len(missed), len(MUTANTS)))
    if survived or missed:
        print("NOT CLEAN. A survivor is a guard no test can fail; an anchor-miss")
        print("is a mutant that never ran. Both mean the suite is weaker than its")
        print("green count suggests.")
        return 1
    print("Every guard above is load-bearing: removing it breaks the suite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

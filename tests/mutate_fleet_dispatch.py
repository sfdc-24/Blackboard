#!/usr/bin/env python3
"""Are the NX, the cursor order and the allowlist load-bearing?

Each mutant deletes one of those three guards in a copy of cloud/fleet-dispatch
and re-runs tests/test_fleet_dispatch.py. A suite that stays green has not
constrained the guard. The edit is applied to the copy, so a bad pattern cannot
leave the real worker mutated.

  python tests/mutate_fleet_dispatch.py
  Exit 0 = every guard is load-bearing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# (name, file, find, replace, why, the test that must fail)
MUTANTS = [
    (
        "nx-no-longer-gates-the-ring",
        "cloud/fleet-dispatch/main.py",
        "    if newest_id not in claimed:\n        outcome = \"duplicate\"",
        "    if False:  # MUTANT: the seen NX no longer gates the ring\n"
        "        outcome = \"duplicate\"",
        "a crash after the ring re-reads the same id and wakes the agent again",
        "test_a_crash_between_ring_and_record_rerings_at_most_once",
    ),
    (
        "cursor-moves-before-the-outcome",
        "cloud/fleet-dispatch/main.py",
        "    record_last(client, agent, newest_id, outcome, at)\n"
        "    write_cursor(client, agent, newest_id)",
        "    write_cursor(client, agent, newest_id)\n"
        "    record_last(client, agent, newest_id, outcome, at)",
        "a crash between the ring and the record has already moved the cursor, "
        "so the entry is never reconsidered and the outcome is never stored",
        "test_the_cursor_moves_only_after_the_outcome_is_recorded",
    ),
    (
        "allowlist-ignored",
        "cloud/fleet-dispatch/main.py",
        "    return tuple(chosen)",
        "    return cloud_agents()  # MUTANT: the allowlist is ignored",
        "an agent left off FLEET_DISPATCH_AGENTS is rung anyway",
        "test_an_agent_off_the_allowlist_is_never_rung",
    ),
]


def run_suite(tree: Path):
    proc = subprocess.run(
        [sys.executable, "-B", str(tree / "tests" / "test_fleet_dispatch.py")],
        cwd=str(tree), capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def stage() -> Path:
    tree = Path(tempfile.mkdtemp(prefix="mutate-fleet-dispatch-"))
    (tree / "cloud").mkdir()
    shutil.copytree(ROOT / "cloud" / "fleet-dispatch", tree / "cloud" / "fleet-dispatch",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "cloud" / "inbox-wake", tree / "cloud" / "inbox-wake",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (tree / "tests").mkdir()
    shutil.copy(ROOT / "tests" / "test_fleet_dispatch.py", tree / "tests" / "test_fleet_dispatch.py")
    shutil.copy(ROOT / ".gcloudignore", tree / ".gcloudignore")
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
                continue
            target.write_text(src.replace(find, repl, 1), encoding="utf-8")
            rc, text = run_suite(tree)
        finally:
            shutil.rmtree(tree, ignore_errors=True)
        named_failed = must_fail in text and rc != 0
        if rc == 0:
            survived.append(name)
            print("\nSURVIVED     %s" % name)
            print("  predicted breakage: %s" % why)
            print("  The suite passed with the guard REMOVED.")
        elif not named_failed:
            killed.append(name)
            print("\nkilled       %s  (suite failed, but not via %s)" % (name, must_fail))
        else:
            killed.append(name)
            print("\nkilled       %s  via %s" % (name, must_fail))

    print("\n%d killed, %d survived, %d anchor-miss, of %d mutants"
          % (len(killed), len(survived), len(missed), len(MUTANTS)))
    if survived or missed:
        print("NOT CLEAN. A survivor is a guard no test can fail; an anchor-miss")
        print("is a mutant that never ran.")
        return 1
    print("Every guard above is load-bearing: removing it breaks the suite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

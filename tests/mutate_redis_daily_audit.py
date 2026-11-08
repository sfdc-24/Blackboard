#!/usr/bin/env python3
"""Are the dedupe and the withhold actually enforced, or only present?

Each guard is deleted on purpose, in a copy of the tree, and the suite must break on the test named
here. A mutant that the suite still passes is an untested claim. An anchor that is not in the file
is a mutant that never ran.

    python tests/mutate_redis_daily_audit.py
Exit 0 when both guards are load-bearing.
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

# (name, file, find, replace, why it should break, the test that must fail)
MUTANTS = [
    (
        "accept-a-row-that-is-already-on-the-board",
        "scripts/redis_daily_audit.py",
        "                if rid in already:  # dedupe: a re-run must not append this row again",
        "                if False and rid in already:  # MUTANT: dedupe removed",
        "a re-run appends the same Row_ID again, and the board cannot delete it",
        "test_a_rerun_appends_nothing",
    ),
    (
        "copy-the-call-bridge-words-onto-the-board",
        "scripts/redis_daily_audit.py",
        "    if by == redis_gov.CALL_BRIDGE_BY:  # withhold: the call's words stay off the board",
        "    if False and by == redis_gov.CALL_BRIDGE_BY:  # MUTANT: withhold removed",
        "the owner's call text is written into the audit row",
        "test_call_bridge_text_is_withheld",
    ),
]


def run_suite(tree: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_redis_daily_audit", "-v"],
        cwd=str(tree), capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def stage() -> Path:
    """scripts/ and tests/, plus the deploy script the scheduler test reads.

    cloud/ is not part of the mutation. The test still has to find deploy.sh beside the suite,
    because that is how it proves the scheduler step does not call gcloud."""
    tree = Path(tempfile.mkdtemp(prefix="mutate-redis-audit-"))
    for name in TARGETS:
        shutil.copytree(ROOT / name, tree / name, ignore=shutil.ignore_patterns("__pycache__"))
    dest = tree / "cloud" / "bus-reconciler"
    dest.mkdir(parents=True)
    shutil.copy2(ROOT / "cloud" / "bus-reconciler" / "deploy.sh", dest / "deploy.sh")
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
        print(text[-4000:])
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
        elif not named_failed:
            killed.append(name)
            print("\nkilled       %s  (suite failed, but not via %s)" % (name, must_fail))
            print(text[-1500:])
        else:
            killed.append(name)
            print("\nkilled       %s  via %s" % (name, must_fail))

    print("\n%d killed, %d survived, %d anchor-miss, of %d mutants"
          % (len(killed), len(survived), len(missed), len(MUTANTS)))
    if survived or missed:
        print("NOT CLEAN. A survivor is a guard no test can fail; an anchor-miss never ran.")
        return 1
    print("Every guard above is load-bearing: removing it breaks the suite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Mutation checks for the board-to-Redis mirror. Each guard is removed on purpose.

The NX on fleet:wake:mirrored must be what stops a re-read from adding a second inbox entry.
The WITHHELD preview must be what keeps call-bridge text out of the lane. A mutant that leaves
the suite green is an untested claim.

scripts/ is copied per mutant and the edit is applied to the copy, so a bad replace cannot land
on the real worker. An anchor that is not in the file is ANCHOR-MISS, not a pass.

    python tests/mutate_board_to_redis.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# (name, file, find, replace, why it must break, the test that must fail)
MUTANTS = [
    (
        "nx-dedupe-is-a-plain-set",
        "cloud/bus-reconciler/serve_requests.py",
        'if not conn.set("fleet:wake:mirrored:%s:%s" % (row_id, agent), "1", nx=True, ex=int(ttl)):',
        'if not conn.set("fleet:wake:mirrored:%s:%s" % (row_id, agent), "1", nx=False, ex=int(ttl)):',
        "a re-read of the same board row XADDs a second inbox entry for the same agent",
        "test_a_reread_adds_nothing",
    ),
    (
        "call-text-preview-is-the-raw-payload",
        "cloud/bus-reconciler/serve_requests.py",
        'redis_gov.WITHHELD if call_bridge_text(source, payload) else (payload or "")[:PREVIEW_CHARS]',
        '(payload or "")[:PREVIEW_CHARS]',
        "a row carrying call-bridge text puts the owner's words in the inbox preview",
        "test_call_bridge_text_is_withheld",
    ),
]


def stage() -> Path:
    tree = Path(tempfile.mkdtemp(prefix="mutate-board-to-redis-"))
    shutil.copytree(ROOT / "scripts", tree / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for rel in ("cloud/bus-reconciler/serve_requests.py",
                "cloud/inbox-wake/main.py",
                "tests/test_board_to_redis.py"):
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    return tree


def run_suite(tree: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_board_to_redis", "-v"],
        cwd=str(tree), capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


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
        named = must_fail in text and rc != 0
        if rc == 0:
            survived.append(name)
            print("\nSURVIVED     %s" % name)
            print("  predicted breakage: %s" % why)
        elif not named:
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
    sys.exit(main())

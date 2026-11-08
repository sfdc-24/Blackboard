#!/usr/bin/env python3
"""Mutation checks for the board-to-Redis mirror. Each guard is removed on purpose.

The NX on fleet:wake:mirrored must be what stops a re-read from adding a second inbox entry.
The entry is metadata only: no preview, no text field, and each copied id or tag must fully match
its safe pattern, or payload words reach a lane every principal can read. The script's DEL on a
failed XADD, and its pcall around the XADD, must be what lets the next pass deliver a copy that
failed. The script's MAXLEN and EXPIRE must be what holds a lane to the governed cap and the
fleet ttl. The per-agent catch must be what lets one agent's failure leave the others delivered. A
mutant that leaves the suite green is an untested claim. The Lua mutants are run by the test's
fake, which executes the script text it is given and refuses any line it does not know.

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
        "if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1]) then return 0 end",
        "if not redis.call('SET', KEYS[1], '1', 'EX', ARGV[1]) then return 0 end",
        "a re-read of the same board row XADDs a second inbox entry for the same agent",
        "test_a_reread_adds_nothing",
    ),
    (
        "a-preview-comes-back",
        "cloud/bus-reconciler/serve_requests.py",
        '        "phase": _safe("phase", _bcb_field(payload, "phase")),\n',
        '        "phase": _safe("phase", _bcb_field(payload, "phase")),\n'
        '        "preview": (payload or "")[:300],\n',
        "the payload's first 300 characters, secrets and private words included, ride into the lane",
        "test_no_payload_text_reaches_the_lane",
    ),
    (
        "call-text-rides-in-a-field",
        "cloud/bus-reconciler/serve_requests.py",
        '        "phase": _safe("phase", _bcb_field(payload, "phase")),\n',
        '        "phase": _safe("phase", _bcb_field(payload, "phase")),\n'
        '        "text": _bcb_field(payload, "text") or (payload or "")[:120],\n',
        "a call-bridge row's words reach the lane under another field name",
        "test_call_bridge_rows_put_no_words_in_the_lane",
    ),
    (
        "an-unsafe-thread-id-travels",
        "cloud/bus-reconciler/serve_requests.py",
        '"thread_id": re.compile(r"TH-[A-Z0-9-]{1,64}"),',
        '"thread_id": re.compile(r".+", re.S),',
        "any text written as thread_id=... reaches the lane",
        "test_unsafe_ids_and_fields_are_dropped",
    ),
    (
        "safe-is-a-prefix-match",
        "cloud/bus-reconciler/serve_requests.py",
        "return value if _SAFE[name].fullmatch(value) else \"\"",
        "return value if _SAFE[name].match(value) else \"\"",
        "a safe-looking prefix carries the words after it into the lane",
        "test_unsafe_ids_and_fields_are_dropped",
    ),
    (
        "an-unsafe-row-id-is-copied",
        "cloud/bus-reconciler/serve_requests.py",
        'if not _safe("row_id", row_id) or source.lower() in LOOP_SOURCES:',
        "if not row_id or source.lower() in LOOP_SOURCES:",
        "a row id made of words becomes a lane entry and a mark key",
        "test_unsafe_ids_and_fields_are_dropped",
    ),
    (
        "a-failed-xadd-keeps-its-mark",
        "cloud/bus-reconciler/serve_requests.py",
        "  redis.call('DEL', KEYS[1])\n",
        "",
        "the XADD fails, the mark stays, and every later pass skips that wake until the ttl ends",
        "test_a_failed_copy_is_retried_and_delivered_once",
    ),
    (
        "the-xadd-is-not-protected",
        "cloud/bus-reconciler/serve_requests.py",
        "local added = redis.pcall('XADD', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', unpack(ARGV, 4))",
        "local added = redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', unpack(ARGV, 4))",
        "a failing XADD aborts the script before the DEL, so the mark outlives the lost entry",
        "test_a_failed_copy_is_retried_and_delivered_once",
    ),
    (
        "the-lane-never-expires",
        "cloud/bus-reconciler/serve_requests.py",
        "redis.call('EXPIRE', KEYS[2], ARGV[3])\n",
        "",
        "a lane outlives the fleet ttl, and an old lane keeps whatever expiry it had",
        "test_a_copy_gives_the_lane_the_fleet_ttl_and_refreshes_an_old_one",
    ),
    (
        "the-lane-is-never-trimmed",
        "cloud/bus-reconciler/serve_requests.py",
        "redis.pcall('XADD', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', unpack(ARGV, 4))",
        "redis.pcall('XADD', KEYS[2], '*', unpack(ARGV, 4))",
        "a lane grows without bound: no MAXLEN, unlike a governed xadd",
        "test_the_lane_is_trimmed_at_the_governed_cap",
    ),
    (
        "one-agents-failure-ends-the-copy",
        "cloud/bus-reconciler/serve_requests.py",
        '                log.warning("board mirror copy failed (%s)", type(error).__name__)\n',
        "                raise\n",
        "grok's failed copy stops cursor's copy of the same row and every row after it",
        "test_a_failure_for_one_agent_still_delivers_to_the_next",
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

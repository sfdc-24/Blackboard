#!/usr/bin/env python3
"""Mutation testing for the Redis ring (scripts/redis_ring.py).

Each guard below is one tests/test_redis_ring.py claims to enforce. The guard is broken on a COPY of the ring and the
suite must fail ON THE TEST NAMED HERE. A mutant that still passes is an untested claim. Only the modules the suite
imports are copied into a scratch tree, so a bad pattern can never leave the real ring mutated.

    python tests/mutate_redis_ring.py

Exit 0 means every mutant was killed by its named test. Exit 1 means a survivor, a mutant killed only by some other
test, or an anchor that no longer matches.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCE = "scripts/redis_ring.py"
COPIED = ("scripts/redis_ring.py", "scripts/redis_doorbell.py", "scripts/bridge_client.py", "scripts/fleet_ring.py",
          "tests/test_redis_ring.py")

# (name, find, replace, why it must break, the test that must fail)
MUTANTS = [
    (
        "cursor-moves-before-the-wake",
        "        fleet_ring.write_time(last_path, clock())\n        ok, detail = wake(",
        "        fleet_ring.write_time(last_path, clock())\n"
        "        redis_doorbell.write_cursor(path, last_id)  # MUTANT: cursor before the wake\n"
        "        ok, detail = wake(",
        "the cursor passes the entries before the wake has exited 0, so a failed wake loses them",
        "test_a_failed_wake_keeps_the_cursor_and_the_retry_wakes_again",
    ),
    (
        "cursor-moves-on-a-failed-wake",
        "        if not ok:\n            back_off(\"WAKE FAILED",
        "        if not ok:\n            redis_doorbell.write_cursor(path, last_id)  # MUTANT: moved anyway\n"
        "            back_off(\"WAKE FAILED",
        "a failed wake still moves the cursor, so the entries are never retried",
        "test_a_failed_wake_alone_leaves_the_cursor_where_it_was",
    ),
    (
        "a-401-is-retried",
        "        except (bridge_client.Refused, bridge_client.Denied) as why:\n"
        "            log(\"redis ring: DENIED or REFUSED (%s). No board fallback; exiting.\" % why)\n"
        "            return EXIT_DENIED",
        "        except (bridge_client.Refused, bridge_client.Denied) as why:\n"
        "            back_off(\"denied; trying again\")  # MUTANT: a denial is retried\n            continue",
        "a 401 raised by the wait is retried instead of stopping the ring",
        "test_a_denied_first_run_exits_too",
    ),
    (
        "a-401-answer-is-retried",
        "            log(\"redis ring: DENIED or REFUSED by the bridge. No board fallback; exiting.\")\n"
        "            return EXIT_DENIED",
        "            back_off(\"denied; trying again\")  # MUTANT: a denial is retried\n            continue",
        "a 401 the doorbell reports is retried instead of stopping the ring",
        "test_a_401_exits_non_zero_at_once_with_no_fallback",
    ),
    (
        "missing-bridge-env-keeps-going",
        "    if problem is not None:\n        log(problem[1])",
        "    if False:  # MUTANT: start the loop without the bridge\n        log(problem[1])",
        "a ring with no bridge env starts anyway, and every wait answers 'use the board'",
        "test_main_with_the_bridge_off_exits_3_before_any_request",
    ),
    (
        "unbound-account-is-ready",
        "    if bound != principal:",
        "    if False:  # MUTANT: any bound account will do",
        "an account bound to another agent waits on this agent's lane as that agent",
        "test_an_account_bound_to_another_agent_is_exit_3",
    ),
    (
        "no-min-gap",
        "            if wait > 0:",
        "            if False:  # MUTANT: no gap",
        "entries inside the gap ring at once, one wake per entry",
        "test_entries_inside_the_gap_are_held_and_ring_together",
    ),
    (
        "a-429-does-not-wait",
        "        sleep(delay)\n",
        "        sleep(0)  # MUTANT: retry at once\n",
        "a 429 or a timeout is retried in a tight loop",
        "test_a_429_backs_off_then_the_wait_resumes",
    ),
    (
        "backoff-never-grows",
        "        delay = min(BACKOFF_MAX, start * (2 ** failures))",
        "        delay = min(BACKOFF_MAX, start)  # MUTANT: flat",
        "a long outage is asked every few seconds forever",
        "test_transient_failures_double_and_are_capped",
    ),
    (
        "an-instant-empty-answer-spins",
        "            if spent < FLOOR:",
        "            if False:  # MUTANT: no floor",
        "a bridge that answers empty at once makes a tight loop",
        "test_a_quiet_lane_waits_again_and_an_instant_empty_answer_is_padded",
    ),
    (
        "a-transient-failure-exits",
        "        except Transient as why:\n            back_off(why, why.rate_limited, why.retry_after)\n"
        "            continue",
        "        except Transient as why:\n            return 3  # MUTANT: give up",
        "one dropped wait stops the ring until someone restarts it",
        "test_transient_failures_double_and_are_capped",
    ),
    (
        "busy-is-an-empty-pass",
        "                raise Transient(\"HTTP 429 busy\", rate_limited=True,\n"
        "                                retry_after=getattr(why, \"retry_after\", None)) from None",
        "                return {\"ok\": True, \"value\": [], \"principal\": kwargs.get(\"me\")}"
        "  # MUTANT: busy looks like a quiet lane",
        "a 429 busy is a quiet lane: the ring asks again after the 2 s floor, not after its 30 s backoff",
        "test_a_busy_wait_backs_off_by_the_rings_policy_not_the_doorbells",
    ),
    (
        "busy-not-intercepted",
        "            if busy is not None and isinstance(why, busy):",
        "            if False:  # MUTANT: Busy reaches the doorbell, which sleeps its own 5 s",
        "the doorbell's short busy sleep stands in for the ring's 429 policy",
        "test_a_busy_wait_backs_off_by_the_rings_policy_not_the_doorbells",
    ),
    (
        "busy-retry-after-ignored",
        "            back_off(why, why.rate_limited, why.retry_after)\n",
        "            back_off(why, why.rate_limited)  # MUTANT: the bridge's retry_after is dropped\n",
        "a bridge that asks for 90 s is asked again after 30",
        "test_a_busy_wait_that_names_a_longer_retry_after_waits_that_long",
    ),
    (
        "summary-carries-the-text",
        "    return \"- %s from %s, %s\" % (fleet_ring.safe(at, 25), fleet_ring.safe(entry.get(\"by\"), 40), which)",
        "    return \"- %s from %s, %s %s\" % (fleet_ring.safe(at, 25), fleet_ring.safe(entry.get(\"by\"), 40), which,"
        " entry.get(\"text\", \"\"))  # MUTANT: the words",
        "an entry's words ride into the woken agent's prompt",
        "test_it_names_entries_by_time_sender_and_row_id_only",
    ),
    (
        "sender-not-cut-to-id-characters",
        "fleet_ring.safe(entry.get(\"by\"), 40), which)",
        "str(entry.get(\"by\")), which)  # MUTANT: raw sender",
        "a sender tag can carry a sentence into the prompt",
        "test_it_names_entries_by_time_sender_and_row_id_only",
    ),
    (
        "gemini-drop-written-in-place",
        "            os.replace(temp, final)\n",
        "            os.rename(temp, str(final) + \".partial\")  # MUTANT: not renamed onto the final name\n"
        "            Path(str(final) + \".partial\").rename(final)\n",
        "the drop is not one rename from a temp file in the inbox onto its final name",
        "test_the_gemini_drop_is_written_whole_then_renamed_into_place",
    ),
    (
        "gemini-drop-temp-elsewhere",
        "suffix=\".json.tmp\", dir=str(folder))",
        "suffix=\".json.tmp\")  # MUTANT: temp in the system temp dir, so the rename can cross volumes",
        "the temp file is not beside the final one, so the rename may not be atomic",
        "test_the_gemini_drop_is_written_whole_then_renamed_into_place",
    ),
    (
        "gemini-drop-overwrites",
        "            if final.exists():\n                return True, \"already dropped %s\" % final.name",
        "            pass  # MUTANT: overwrite",
        "a retry replaces a drop file a watcher may be reading",
        "test_a_retried_gemini_drop_does_not_overwrite_the_file_a_watcher_may_be_reading",
    ),
]


def run_suite(tree: Path):
    proc = subprocess.run([sys.executable, "-B", "-m", "unittest", "tests.test_redis_ring", "-v"],
                          cwd=str(tree), capture_output=True, text=True, timeout=300)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def stage() -> Path:
    tree = Path(tempfile.mkdtemp(prefix="mutate-redis-ring-"))
    for rel in COPIED:
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, tree / rel)
    return tree


def failed_by(text: str, test: str) -> bool:
    return re.search(r"^(FAIL|ERROR): %s \(" % re.escape(test), text, re.M) is not None


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

    killed, survived, missed, stray = [], [], [], []
    for name, find, repl, why, must_fail in MUTANTS:
        tree = stage()
        try:
            target = tree / SOURCE
            src = target.read_text(encoding="utf-8")
            if src.count(find) != 1:
                missed.append(name)
                print("\nANCHOR-MISS  %s: the pattern matches %d times; a mutant must hit exactly one guard"
                      % (name, src.count(find)))
                continue
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(src.replace(find, repl, 1))
            rc, text = run_suite(tree)
        finally:
            shutil.rmtree(tree, ignore_errors=True)
        if rc == 0:
            survived.append(name)
            print("\nSURVIVED     %s\n  predicted breakage: %s" % (name, why))
        elif not failed_by(text, must_fail):
            stray.append(name)
            print("\nWRONG TEST   %s: the suite failed, but not on %s" % (name, must_fail))
            print(text[-800:])
        else:
            killed.append(name)
            print("killed       %s  via %s" % (name, must_fail))

    print("\n%d killed, %d survived, %d killed by another test, %d anchor-miss, of %d mutants"
          % (len(killed), len(survived), len(stray), len(missed), len(MUTANTS)))
    if survived or missed or stray:
        print("NOT CLEAN. A survivor is a guard no test can fail; an anchor-miss is a mutant that never ran.")
        return 1
    print("Every guard above is load-bearing: breaking it fails the test that claims it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

The cloud-agent wake (BOARD_TO_REDIS_RELAY_CURSOR, INBOX_WAKE_REDIS) has its guards here too: the relay's
flag, its own mark, its for=cursor and its refusal to relay a broadcast; the nudge's claude-api-only
scope, its signature and its retry; and in cloud/inbox-wake (run against tests/test_inbox_wake.py) the
cursor that moves only after a start succeeds, the forward read, the start while the worker's own
position lags the lane head (one per position), the page bound, and the lane flag itself.

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

# (name, file, find, replace, why it must break, the test that must fail[, the suite, default SUITE])
SUITE = "tests.test_board_to_redis"
SUITES = (SUITE, "tests.test_inbox_wake")
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
    # THE CURSOR RELAY (BOARD_TO_REDIS_RELAY_CURSOR): one grok entry, for=cursor, own mark, named rows only.
    (
        "the-relay-ignores-its-flag",
        "cloud/bus-reconciler/serve_requests.py",
        "            if relay and agent in named and RELAYS.get(agent) in principals:",
        "            if agent in RELAYS and RELAYS.get(agent) in principals:",
        "every cursor row is relayed to grok with the flag off",
        "test_relay_off_gives_no_grok_entry",
    ),
    (
        "the-relay-shares-grok-s-own-mark",
        "cloud/bus-reconciler/serve_requests.py",
        'targets.append(("fleet:wake:relayed:%s:%s:%s" % (row_id, agent, RELAYS[agent]),',
        'targets.append(("fleet:wake:mirrored:%s:%s" % (row_id, RELAYS[agent]),',
        "a row for grok and cursor: grok's own copy takes the mark and the relay is never written",
        "test_a_row_for_grok_and_cursor_gives_grok_its_copy_and_the_relay_once_each",
    ),
    (
        "the-relay-loses-for-cursor",
        "cloud/bus-reconciler/serve_requests.py",
        'RELAYS[agent], pairs + ["for", agent]))',
        "RELAYS[agent], pairs))",
        "grok's entry does not say it is for cursor, so it reads as grok's own work",
        "test_a_cursor_row_gives_grok_one_entry_for_cursor_and_a_reread_adds_nothing",
    ),
    (
        "a-broadcast-is-relayed",
        "cloud/bus-reconciler/serve_requests.py",
        "named = addressed_agents(_row_cell(row, 3), principals, aliases, expand=False) if relay else []",
        "named = agents if relay else []",
        "every ALL row makes grok dispatch a paid Cursor agent",
        "test_a_broadcast_is_not_a_dispatch",
    ),
    (
        "the-live-loop-always-relays",
        "cloud/bus-reconciler/serve_requests.py",
        "relay, nudger = relay_cursor_on(), nudger_from_env()",
        "relay, nudger = True, nudger_from_env()",
        "BOARD_TO_REDIS alone turns the relay on",
        "test_the_live_loop_relays_only_with_both_flags_exactly_on",
    ),
    # THE CLAUDE-API NUDGE (INBOX_WAKE_REDIS): one signed nudge, claude-api only, retried until rung.
    (
        "every-lane-is-nudged",
        "cloud/bus-reconciler/serve_requests.py",
        "        if agent in NUDGE_AGENTS:\n            self.pending.add(agent)\n",
        "        self.pending.add(agent)\n",
        "a grok or cursor entry rings inbox-wake for a lane it has no route for",
        "test_no_nudge_for_another_lane",
    ),
    (
        "a-failed-nudge-is-forgotten",
        "cloud/bus-reconciler/serve_requests.py",
        '                log.warning("inbox-wake nudge failed (%s); retried", type(error).__name__)\n',
        '                log.warning("inbox-wake nudge failed (%s); retried", type(error).__name__)\n'
        "                self.pending.clear()\n",
        "a nudge lost to a network error waits for the 15-minute sweep",
        "test_a_failed_or_unrung_nudge_is_sent_again_after_the_retry_gap",
    ),
    (
        "an-unrung-nudge-counts-as-done",
        "cloud/bus-reconciler/serve_requests.py",
        "            if status in NUDGE_DONE or status in NUDGE_FINAL:",
        "            if status:",
        "inbox-wake's failed start reads as a wake and the nudge is dropped",
        "test_a_failed_or_unrung_nudge_is_sent_again_after_the_retry_gap",
    ),
    (
        "the-nudge-is-unsigned",
        "cloud/bus-reconciler/serve_requests.py",
        '"X-Hub-Signature-256": "sha256=" + mac})',
        '"X-Hub-Signature-256": "sha256=" + "0" * 64})',
        "inbox-wake refuses every nudge (401) and the worker waits for the sweep",
        "test_a_claude_api_entry_is_one_signed_nudge_with_no_text",
    ),
    (
        "the-nudge-ignores-its-flag",
        "cloud/bus-reconciler/serve_requests.py",
        '    if environ.get(INBOX_WAKE_REDIS) != "on":\n        return None\n',
        "",
        "a configured URL and secret nudge with the flag off",
        "test_only_exactly_on_and_configured_builds_a_nudger",
    ),
    # THE LANE PATH IN INBOX-WAKE (INBOX_WAKE_REDIS): cursor after success, a burst once, signed, off is off.
    (
        "the-cursor-moves-before-the-start",
        "cloud/inbox-wake/main.py",
        "    if lagging:\n        result = dict(decide(",
        "    if new:\n        deps.cursor.set(agent, new[-1][\"stream_id\"])\n    if lagging:\n        result = dict(decide(",
        "a start that fails after the cursor moved is never retried: an entry woken zero times",
        "test_a_new_entry_starts_the_worker_once_and_the_cursor_moves_only_after",
        "tests.test_inbox_wake",
    ),
    (
        "a-failed-start-moves-the-cursor",
        "cloud/inbox-wake/main.py",
        'RUNG = ("woke", "duplicate")',
        'RUNG = ("woke", "duplicate", "error", "unconfigured")',
        "a failed start moves the cursor past the entry, and no nudge or sweep retries it",
        "test_a_failed_start_keeps_the_cursor_and_the_next_nudge_retries",
        "tests.test_inbox_wake",
    ),
    (
        "a-burst-leaves-older-ids-unclaimed",
        "cloud/inbox-wake/main.py",
        '        for entry in new:\n            deps.seen.claim("%s:%s" % (agent, entry["id"]))\n',
        "",
        "the per-entry sweep starts the worker again for each older entry of a burst already started",
        "test_a_burst_is_one_start_and_no_later_sweep_starts_it_again",
        "tests.test_inbox_wake",
    ),
    (
        "the-lane-flag-is-ignored",
        "cloud/inbox-wake/main.py",
        '    if not deps.redis_wake:\n        return {"status": "lane_off", "woke": False}\n',
        "",
        "a nudge drains the lane and writes a cursor with INBOX_WAKE_REDIS off",
        "test_the_flag_off_changes_nothing",
        "tests.test_inbox_wake",
    ),
    (
        "stream-ids-compare-as-text",
        "cloud/inbox-wake/main.py",
        "    return int(head), int(tail or 0)\n",
        "    return head, tail or \"0\"\n",
        "10-0 sorts before 9-0 and a new entry is never seen past the cursor",
        "test_stream_ids_compare_as_numbers",
        "tests.test_inbox_wake",
    ),
    (
        "the-sweep-skips-the-drain",
        "cloud/inbox-wake/main.py",
        "        if deps.redis_wake and agent in LANE_AGENTS:\n            out.append(drain(agent, deps))\n"
        "            continue\n",
        "",
        "with the flag on, a lost nudge is swept entry by entry: one worker run per entry, cursor unmoved",
        "test_the_sweep_drains_the_lane_when_a_nudge_never_came",
        "tests.test_inbox_wake",
    ),
    (
        "the-nudge-skips-the-signature",
        "cloud/inbox-wake/main.py",
        '        if not signature_ok(deps.webhook_secret, body, headers.get("x-hub-signature-256", "")):\n'
        '            return 401, {"error": "bad signature"}\n'
        '        if headers.get("x-github-event", "") == LANE_EVENT:\n',
        '        if headers.get("x-github-event", "") == LANE_EVENT:\n'
        '            return 200, {"enabled": True, "woke": [drain(lane_of(body), deps)]}\n'
        '        if not signature_ok(deps.webhook_secret, body, headers.get("x-hub-signature-256", "")):\n'
        '            return 401, {"error": "bad signature"}\n'
        '        if False:\n',
        "anyone who can reach the service starts the paid worker job",
        "test_the_nudge_is_signed_and_names_a_lane_this_deploy_may_wake",
        "tests.test_inbox_wake",
    ),
    # Codex on c497137a: read forward, and start while the WORKER's own position lags the head.
    (
        "the-drain-reads-the-newest-50-window",
        "cloud/inbox-wake/main.py",
        "        page = list(deps.lane(agent, start, asked) or [])\n",
        "        page = [e for e in (deps.reader(agent) or [])\n"
        "                if (_stream_key(e.get(\"stream_id\")) or (0, 0)) >= (_stream_key(start) or (0, -1))]\n",
        "more than fifty behind the cursor: the oldest are skipped and never claimed",
        "test_120_entries_behind_are_all_covered_in_order_by_one_start",
        "tests.test_inbox_wake",
    ),
    (
        "the-sweep-ignores-worker-lag",
        "cloud/inbox-wake/main.py",
        "    lagging = bool(_stream_key(head)) and (_stream_key(position) or (0, -1)) < _stream_key(head)\n",
        "    lagging = bool(new)\n",
        "one start takes 500 of 1200; the wake cursor is idle, so the tail waits for a new entry",
        "test_1200_behind_a_500_cap_is_three_starts_and_no_tail_is_stranded",
        "tests.test_inbox_wake",
    ),
    (
        "the-start-is-not-keyed-by-position",
        "cloud/inbox-wake/main.py",
        '"id": "scan:%s:%s" % (position or "0", head),',
        '"id": new[-1]["stream_id"] if new else "scan:%s" % head,',
        "a worker that did not move is started again for the same position",
        "test_a_stalled_worker_is_not_started_twice_for_one_position",
        "tests.test_inbox_wake",
    ),
    (
        "the-trim-gap-is-silent",
        "cloud/inbox-wake/main.py",
        '        log.warning("lane %s was trimmed past its wake cursor; draining from the oldest entry", agent)\n',
        "        pass\n",
        "entries lost to a MAXLEN trim leave no trace",
        "test_a_lane_trimmed_past_the_cursor_is_logged_without_words_and_drains_on",
        "tests.test_inbox_wake",
    ),
    (
        "the-page-bound-is-gone",
        "cloud/inbox-wake/main.py",
        "    for page_no in range(DRAIN_PAGES):\n",
        "    for page_no in range(10 ** 6):\n",
        "a 10000-entry backlog is read in one request, past the 60 s timeout",
        "test_the_page_bound_keeps_a_call_short_and_the_rest_drains_next_time",
        "tests.test_inbox_wake",
    ),
    (
        "the-progress-is-another-key",
        "cloud/inbox-wake/main.py",
        'SCAN_KEY = "fleet:worker:inbox_scan:"',
        'SCAN_KEY = "fleet:worker:inbox:"',
        "the wake reads a key the worker never writes: every position is unknown, every sweep a start",
        "test_read_progress_is_the_worker_s_own_scan_state_and_the_lane_head",
        "tests.test_inbox_wake",
    ),
]


def stage() -> Path:
    tree = Path(tempfile.mkdtemp(prefix="mutate-board-to-redis-"))
    shutil.copytree(ROOT / "scripts", tree / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    # test_inbox_wake reads inbox-wake's deploy files, drive-intake's ring and the wake workflows too.
    for rel in ("cloud/inbox-wake", "cloud/drive-intake"):
        shutil.copytree(ROOT / rel, tree / rel, ignore=shutil.ignore_patterns("__pycache__"))
    for rel in ("cloud/bus-reconciler/serve_requests.py",
                "tests/test_board_to_redis.py",
                "tests/test_inbox_wake.py",
                "cloud/claude-api-worker/main.py",
                ".gcloudignore",
                ".github/workflows/inbox-wake-sweep.yml",
                ".github/workflows/inbox-wake-claude.yml",
                ".github/workflows/inbox-wake-codex.yml"):
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    return tree


def run_suite(tree: Path, suites=SUITES):
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", *suites, "-v"],
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
    for name, relpath, find, repl, why, must_fail, *suite in MUTANTS:
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
            rc, text = run_suite(tree, (suite[0] if suite else SUITE,))
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

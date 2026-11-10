#!/usr/bin/env python3
"""Mutation testing for the call-sheet mirror.

Each guard below is one the suite claims to enforce. The guard is deleted on a
COPY of the worker and the suite must fail on the test named here. A mutant
that still passes is an untested claim. The edit is applied only in a scratch
tree, so a bad pattern cannot leave the real worker mutated.

    python tests/mutate_call_sheet_mirror.py

Exit 0 means every mutant was killed. Exit 1 means a survivor or an anchor that
no longer matches the source.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCE = "cloud/bus-reconciler/serve_requests.py"

# (name, find, replace, why it must break, the test that must fail)
MUTANTS = [
    (
        "drop-the-call-from-the-loop",
        "        mirror_call_sheet(conn, now, env, renew)",
        "        None  # MUTANT: the live pass never mirrors",
        "the step is not in the loop, so a start never becomes a row",
        "test_a_start_and_an_end_are_one_row_each_and_a_second_loop_is_quiet",
    ),
    (
        "append-even-when-the-row-is-there",
        "        if status == \"absent\":\n            append(spec)",
        "        if True:  # MUTANT: append even when this payload is already on the board\n"
        "            append(spec)",
        "clearing the cursor posts the same Row_ID a second time",
        "test_a_start_and_an_end_are_one_row_each_and_a_second_loop_is_quiet",
    ),
    (
        "cursor-moves-before-the-read-back",
        "    rid = spec[\"row_id\"]\n    payload = spec.get(\"payload\") or \"\"\n    try:\n"
        "        status = _reuse_status(present(rid), payload)",
        "    rid = spec[\"row_id\"]\n    payload = spec.get(\"payload\") or \"\"\n"
        "    conn.set(cursor_key, entry_id)  # MUTANT: cursor moves before the read-back\n"
        "    try:\n        status = _reuse_status(present(rid), payload)",
        "a row that does not read back still advances the cursor, so the entry is never retried",
        "test_a_start_and_an_end_are_one_row_each_and_a_second_loop_is_quiet",
    ),
    (
        "any-id-is-reuse",
        "    if got != payload or source != SHEET_SOURCE or stamp != VERIFIED_STAMP:\n"
        "        return \"conflict\"\n    return \"done\"",
        "    return \"done\"  # MUTANT: any one row with this id counts",
        "a different payload under the same id is treated as done and the cursor moves",
        "test_the_same_id_with_a_different_payload_is_a_conflict",
    ),
    (
        "matching-payload-is-not-reuse",
        "    if got != payload or source != SHEET_SOURCE or stamp != VERIFIED_STAMP:\n"
        "        return \"conflict\"\n    return \"done\"",
        "    if got != payload or source != SHEET_SOURCE or stamp != VERIFIED_STAMP:\n"
        "        return \"conflict\"\n    return \"absent\"  # MUTANT: the same payload is not reuse",
        "an id already on the board with this payload is appended again",
        "test_the_same_id_and_payload_is_already_done",
    ),
    (
        "two-copies-count-as-one",
        "    if len(hits) != 1:\n        return \"conflict\"",
        "    if False:  # MUTANT: two copies of one id still count\n        return \"conflict\"",
        "two identical rows with one id are treated as done",
        "test_two_identical_copies_are_a_conflict",
    ),
    (
        "another-sender-counts",
        "    if got != payload or source != SHEET_SOURCE or stamp != VERIFIED_STAMP:",
        "    if got != payload:  # MUTANT: another sender still counts",
        "a row from another sender is treated as ours",
        "test_a_copy_from_another_sender_is_a_conflict",
    ),
    (
        "swallow-a-lost-lease",
        "    if isinstance(error, LockLost):\n        raise error",
        "    if False and isinstance(error, LockLost):  # MUTANT: lease loss is just a failed row\n"
        "        raise error",
        "losing the worker lease is swallowed and the pass keeps writing",
        "test_losing_the_lease_mid_pass_writes_no_cursor_and_no_state",
    ),
    (
        "stale-end-is-two-writes",
        "            conn.eval(STALE_END_LUA, 2, CALL_KEY % call_id, CALLS_KEY,\n"
        "                      call_id, epoch, at, int(STALE_TTL), int(CALLS_MAXLEN), \"stale\")",
        "            conn.xadd(CALLS_KEY, {\"event\": \"end\", \"call_id\": call_id, \"epoch\": epoch,\n"
        "                                 \"at\": at, \"reason\": \"stale\"})  # MUTANT: not one script\n"
        "            conn.hset(CALL_KEY % call_id, mapping={\"state\": \"ended\", \"ended_at\": at,\n"
        "                                                   \"reason\": \"stale\"})",
        "the end event can land without the hash, or the hash without the event",
        "test_a_stale_end_is_one_write_and_a_failed_script_writes_neither",
    ),
    (
        "a-second-pass-ends-again",
        "if redis.call('HGET', KEYS[1], 'state') ~= 'live' then return 0 end",
        "if false then return 0 end  -- MUTANT: an ended call is ended again",
        "a second pass writes another end, and a second CALLEND can conflict",
        "test_a_stale_end_is_one_write_and_a_failed_script_writes_neither",
    ),
    (
        "read-the-stream-from-the-start",
        "    if cursor:\n"
        "        found = conn.xrange(key, min=\"(\" + cursor, max=\"+\", count=MIRROR_PAGE)\n"
        "    else:\n"
        "        found = conn.xrange(key, min=\"-\", max=\"+\", count=MIRROR_PAGE)",
        "    found = conn.xrange(key, min=\"-\", max=\"+\", count=MIRROR_PAGE)"
        "  # MUTANT: the cursor is ignored",
        "the second loop reads the entries again",
        "test_a_start_and_an_end_are_one_row_each_and_a_second_loop_is_quiet",
    ),
    (
        "log-the-exception-words",
        "    _mirror_log(ident, size, type(error).__name__)",
        "    _mirror_log(ident, size, error)  # MUTANT: the words",
        "a failed append logs the text it was given",
        "test_a_failed_row_is_logged_by_id_and_size_not_by_its_words",
    ),
    (
        "mirror-turns-of-ended-calls",
        "    return [(call_id, fields) for call_id, fields in _known_calls(conn) if fields.get(\"state\") == \"live\"]",
        "    return [(call_id, fields) for call_id, fields in _known_calls(conn) if True]  # MUTANT: ended too",
        "a turn of an ended call becomes a row",
        "test_a_turn_of_a_live_call_is_one_row_and_an_ended_call_is_none",
    ),
    (
        "accept-any-turn-tag",
        "    if not turn_key or tag not in CALL_TAGS or not epoch:",
        "    if not turn_key or not epoch:  # MUTANT: any to= is posted",
        "a turn addressed to a tag outside the four is posted",
        "test_a_turn_of_a_live_call_is_one_row_and_an_ended_call_is_none",
    ),
    (
        "leave-pipes-in-values",
        "    text = text.replace(\"|\", \"/\").replace(\"\\r\", \" \").replace(\"\\n\", \" \").replace(\"\\t\", \" \")",
        "    text = text.replace(\"\\r\", \" \").replace(\"\\n\", \" \").replace(\"\\t\", \" \")"
        "  # MUTANT: a pipe stays a pipe",
        "a pipe inside a turn's text starts a new BCB key",
        "test_a_turn_of_a_live_call_is_one_row_and_an_ended_call_is_none",
    ),
    (
        "do-not-cap-turn-text",
        "TURN_TEXT_MAX = 1000",
        "TURN_TEXT_MAX = 100000  # MUTANT: turn text is not capped",
        "a 1001-character turn is posted whole, with no truncated=1",
        "test_a_turn_of_a_live_call_is_one_row_and_an_ended_call_is_none",
    ),
    (
        "log-the-skipped-turn",
        "                    _mirror_log(entry_id, _entry_size(fields), \"turn\")",
        "                    _mirror_log(entry_id, _entry_size(fields), fields.get(\"text\"))"
        "  # MUTANT: the words",
        "skipping a turn logs what was said",
        "test_a_turn_of_a_live_call_is_one_row_and_an_ended_call_is_none",
    ),
    (
        "stale-after-51-minutes",
        "STALE_AFTER = datetime.timedelta(minutes=50)",
        "STALE_AFTER = datetime.timedelta(minutes=51)  # MUTANT: fifty minutes is not enough",
        "a call older than 50 minutes stays live",
        "test_the_stale_sweep_closes_a_call_after_fifty_minutes_and_not_before",
    ),
    (
        "stale-at-exactly-fifty-minutes",
        "            if started is None or now - started <= STALE_AFTER:",
        "            if started is None or now - started < STALE_AFTER:  # MUTANT: exactly 50 closes",
        "a call that has been live for exactly 50 minutes is closed",
        "test_the_stale_sweep_closes_a_call_after_fifty_minutes_and_not_before",
    ),
    (
        "reconstruct-withheld-words",
        "    if \"text\" not in fields:\n        return \"\"",
        "    if \"text\" not in fields:\n        return _cell(fields.get(\"words\"))  # MUTANT: invent the text",
        "a turn with no text field is filled from words=",
        "test_a_turn_without_text_records_the_class_and_withheld",
    ),
    (
        "drop-withheld",
        "_via(phase) + [\n        (\"withheld\", \"1\")]",
        "_via(phase)",
        "a turn with no text is posted without withheld=1",
        "test_a_turn_without_text_records_the_class_and_withheld",
    ),
    (
        # B15 ONE DISPATCH: a turn the bound binder took must not be posted as an ask.
        "via-binder-still-asks",
        "return \"RESULT\" if (fields.get(\"via\") or \"\").strip() == \"binder\" else \"ASK\"",
        "return \"ASK\"",
        "a turn the bound binder already took is posted as an ask and wakes the cloud wakers again",
        "test_a_turn_the_bound_binder_took_is_a_result_record_not_an_ask",
    ),
    (
        "any-via-is-a-record",
        "return \"RESULT\" if (fields.get(\"via\") or \"\").strip() == \"binder\" else \"ASK\"",
        "return \"RESULT\" if (fields.get(\"via\") or \"\").strip() else \"ASK\"",
        "a turn with any via= value, not only binder, is posted as a record and never wakes its agent",
        "test_a_turn_the_bound_binder_took_is_a_result_record_not_an_ask",
    ),
    (
        "command-counts-as-other",
        "    extra = [(\"turn_class\", kind)]",
        "    extra = [(\"turn_class\", \"other\")]  # MUTANT: command is recorded as other",
        "a turn_class=command turn is written turn_class=other",
        "test_a_turn_without_text_records_the_class_and_withheld",
    ),
    (
        "read-the-old-class-field",
        "    kind = (fields.get(\"turn_class\") or \"\").strip()",
        "    kind = (fields.get(\"class\") or \"\").strip()  # MUTANT: the old field name",
        "turn_class is taken from class=, which the chair no longer sends",
        "test_a_turn_without_text_records_the_class_and_withheld",
    ),
    (
        "post-as-the-chair",
        "    return {\"row_id\": rid, \"source_tag\": SHEET_SOURCE, \"target_surface\": to, \"action_type\": action,",
        "    return {\"row_id\": rid, \"source_tag\": SHEET_FROM, \"target_surface\": to, \"action_type\": action,",
        "CALL rows are posted as conference-chair, which the gateway does not stamp verified",
        "test_the_box_reads_a_bus_reconciler_row_at_its_turn_substring",
    ),
    (
        "split-the-box-substring",
        "            (\"from\", SHEET_SOURCE), (\"to\", tag), (\"msg_type\", \"CALL_TURN\"),\n"
        "            (\"call_id\", call_id), (\"thread_id\", _call_thread(call_id))]",
        "            (\"from\", SHEET_SOURCE), (\"to\", tag), (\"msg_type\", \"CALL_TURN\"),\n"
        "            (\"epoch\", \"0\"), (\"call_id\", call_id), (\"thread_id\", _call_thread(call_id))]"
        "  # MUTANT: call_id is not next",
        "msg_type=CALL_TURN is no longer immediately followed by call_id=",
        "test_the_box_reads_a_bus_reconciler_row_at_its_turn_substring",
    ),
    (
        "turn-from-the-chair",
        "            (\"from\", SHEET_SOURCE), (\"to\", tag), (\"msg_type\", \"CALL_TURN\"),",
        "            (\"from\", SHEET_FROM), (\"to\", tag), (\"msg_type\", \"CALL_TURN\"),",
        "a turn row says from=conference-chair under source bus-reconciler: the gateway stamps it mismatch",
        "test_the_box_reads_a_bus_reconciler_row_at_its_turn_substring",
    ),
    (
        "start-from-the-chair",
        "(\"from\", SHEET_SOURCE), (\"to\", \"ALL\"), (\"msg_type\", \"CALL_START\"),",
        "(\"from\", SHEET_FROM), (\"to\", \"ALL\"), (\"msg_type\", \"CALL_START\"),",
        "a start row says from=conference-chair: never verified, the cursor never moves",
        "test_every_row_the_mirror_writes_is_one_the_gateway_verifies",
    ),
    (
        "legacy-any-payload",
        "    if source == SHEET_SOURCE and stamp == LEGACY_STAMP and got == _legacy(payload):",
        "    if source == SHEET_SOURCE and stamp == LEGACY_STAMP:",
        "any payload under the legacy stamp moves the cursor",
        "test_a_legacy_stamp_on_any_other_payload_is_still_a_conflict",
    ),
    (
        "legacy-any-source",
        "    if source == SHEET_SOURCE and stamp == LEGACY_STAMP and got == _legacy(payload):",
        "    if stamp == LEGACY_STAMP and got == _legacy(payload):",
        "a legacy-stamped row from another source moves the cursor",
        "test_a_legacy_stamp_from_another_source_is_still_a_conflict",
    ),
    (
        "legacy-is-reposted",
        "            _mirror_log(rid, len(payload), \"legacy\")\n            _owned(renew)\n            conn.set(cursor_key, entry_id)\n            return True",
        "            _mirror_log(rid, len(payload), \"legacy\")\n            return False",
        "a legacy row blocks the cursor forever",
        "test_a_legacy_row_from_before_the_fix_is_passed_not_reposted",
    ),
    (
        "code-dropped",
        "        if code:\n            pairs.append((\"code\", code))",
        "        if False:  # MUTANT: the call code never reaches the row\n            pairs.append((\"code\", code))",
        "the start row loses the readable call code T-02 reconciles against",
        "test_the_call_code_the_end_acks_and_each_join_reach_the_board",
    ),
    (
        "code-unchecked",
        "    return text if CALL_CODE_RE.fullmatch(text) else \"\"",
        "    return text  # MUTANT: any code text is copied",
        "a code that is not SFDC-YYYY-MM-DD-HHMM (or carries a pipe) lands on the board",
        "test_a_code_or_persona_that_is_not_one_is_never_copied",
    ),
    (
        "code-empty-key",
        "        code = _call_code(fields.get(\"code\"))\n        if code:",
        "        code = _call_code(fields.get(\"code\"))\n        if True:  # MUTANT: an empty code= is written",
        "an older chair's start row changes shape (code= empty), so its read-back no longer matches",
        "test_an_older_chair_without_code_or_acks_writes_the_rows_as_before",
    ),
    (
        "acks-dropped",
        "    if acks:\n        pairs.append((\"acks\", acks))",
        "    if False:  # MUTANT: the End acks never reach the row\n        pairs.append((\"acks\", acks))",
        "the end row loses which binders acknowledged End",
        "test_the_call_code_the_end_acks_and_each_join_reach_the_board",
    ),
    (
        "acks-any-name",
        "    return \",\".join(sorted(name for name in names if name in CALL_PERSONAS))",
        "    return \",\".join(sorted(name for name in names if name))  # MUTANT: any name",
        "an unknown name or a pipe in acks lands on the board",
        "test_a_code_or_persona_that_is_not_one_is_never_copied",
    ),
    (
        "join-not-mirrored",
        "event not in (\"start\", \"end\", \"join\")",
        "event not in (\"start\", \"end\")",
        "a binder's join never becomes a row",
        "test_the_call_code_the_end_acks_and_each_join_reach_the_board",
    ),
    (
        "join-any-persona",
        "        if persona not in CALL_PERSONAS:\n            return None",
        "        if not persona:  # MUTANT: any persona\n            return None",
        "a join naming an unknown or pipe-carrying persona becomes a row",
        "test_a_code_or_persona_that_is_not_one_is_never_copied",
    ),
    (
        "join-one-row-per-call",
        "        rid = \"CALLJOIN-%s-%s\" % (call_id, persona)",
        "        rid = \"CALLJOIN-%s\" % call_id  # MUTANT: one id for every binder",
        "the second binder's join collides with the first and is a conflict",
        "test_the_call_code_the_end_acks_and_each_join_reach_the_board",
    ),
    (
        "flag-always-on",
        "    return os.environ.get(\"CALL_SHEET_MIRROR\", \"\").strip() == \"on\"",
        "    return True  # MUTANT: the flag is ignored",
        "with the flag unset the mirror still reads Redis and can append",
        "test_the_flag_off_means_nothing_runs",
    ),
]


def run_suite(tree: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_call_sheet_mirror", "-v"],
        cwd=str(tree), capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def stage() -> Path:
    tree = Path(tempfile.mkdtemp(prefix="mutate-call-sheet-"))
    shutil.copytree(ROOT / "scripts", tree / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    dest = tree / "cloud" / "bus-reconciler"
    dest.mkdir(parents=True)
    shutil.copy2(ROOT / SOURCE, dest / "serve_requests.py")
    tests = tree / "tests"
    tests.mkdir()
    shutil.copy2(ROOT / "tests" / "test_call_sheet_mirror.py", tests / "test_call_sheet_mirror.py")
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
    for name, find, repl, why, must_fail in MUTANTS:
        tree = stage()
        try:
            target = tree / SOURCE
            src = target.read_text(encoding="utf-8")
            if find not in src:
                missed.append(name)
                print("\nANCHOR-MISS  %s" % name)
                print("  pattern absent from %s - this mutant tested NOTHING." % SOURCE)
                continue
            if src.count(find) != 1:
                missed.append(name)
                print("\nANCHOR-MISS  %s" % name)
                print("  pattern matches %d times; a mutant must hit one guard." % src.count(find))
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
            print(text[-800:])
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

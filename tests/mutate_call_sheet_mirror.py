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
        "        mirror_call_sheet(conn, rows, now, env, renew)",
        "        None  # MUTANT: the live pass never mirrors",
        "the step is not in the loop, so a start never becomes a row",
        "test_a_start_and_an_end_are_one_row_each_and_a_second_loop_is_quiet",
    ),
    (
        "append-even-when-the-row-is-there",
        "        if not present(rid):\n            append(spec)",
        "        append(spec)  # MUTANT: a row already on the board is appended again",
        "clearing the cursor posts the same Row_ID a second time",
        "test_a_start_and_an_end_are_one_row_each_and_a_second_loop_is_quiet",
    ),
    (
        "cursor-moves-before-the-read-back",
        "    rid = spec[\"row_id\"]\n    try:\n        if not present(rid):\n            append(spec)",
        "    rid = spec[\"row_id\"]\n"
        "    conn.set(cursor_key, entry_id)  # MUTANT: cursor moves before the read-back\n"
        "    try:\n        if not present(rid):\n            append(spec)",
        "a row that does not read back still advances the cursor, so the entry is never retried",
        "test_a_start_and_an_end_are_one_row_each_and_a_second_loop_is_quiet",
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
        "        _mirror_log(rid, len(spec.get(\"payload\") or \"\"), type(error).__name__)",
        "        _mirror_log(rid, len(spec.get(\"payload\") or \"\"), error)  # MUTANT: the words",
        "a failed append logs the text it was given",
        "test_a_failed_row_is_logged_by_id_and_size_not_by_its_words",
    ),
    (
        "mirror-turns-of-ended-calls",
        "        if fields.get(\"state\") == \"live\":",
        "        if True:  # MUTANT: an ended call is mirrored too",
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
        "ignore-the-source-tag",
        "    if frm not in CALL_TAGS or frm != source:",
        "    if frm not in CALL_TAGS:  # MUTANT: source tag is not checked",
        "a reply whose from= is not the row's source tag is spoken",
        "test_a_reply_whose_from_disagrees_with_the_source_tag_is_not_spoken",
    ),
    (
        "ignore-the-turn-addressee",
        "    if not targets or any(target != frm for target in targets):",
        "    if not targets:  # MUTANT: the turn's to= is not checked",
        "a reply from someone other than the turn's addressee is spoken",
        "test_a_reply_whose_from_disagrees_with_the_turn_is_not_spoken",
    ),
    (
        "speak-on-an-ended-call",
        "    if call.get(\"state\") != \"live\":",
        "    if False and call.get(\"state\") != \"live\":  # MUTANT: ended calls are spoken to",
        "a reply on an ended call reaches the voice lane",
        "test_a_reply_on_an_ended_call_is_not_spoken",
    ),
    (
        "ignore-the-epoch",
        "    if _field(payload, \"epoch\") != (call.get(\"epoch\") or \"\").strip():",
        "    if False and _field(payload, \"epoch\") != (call.get(\"epoch\") or \"\").strip():"
        "  # MUTANT: epoch is not checked",
        "a reply from another epoch is spoken into this one",
        "test_a_reply_whose_epoch_disagrees_is_not_spoken",
    ),
    (
        "a-reply-stays-fresh-for-a-day",
        "REPLY_FRESH = datetime.timedelta(seconds=120)",
        "REPLY_FRESH = datetime.timedelta(seconds=86400)  # MUTANT: 120s is not the bound",
        "a reply older than 120 seconds is spoken",
        "test_a_reply_older_than_120_seconds_is_not_spoken",
    ),
    (
        "exactly-120-seconds-is-too-old",
        "now - when > REPLY_FRESH",
        "now - when >= REPLY_FRESH",
        "a reply that is exactly 120 seconds old is not spoken",
        "test_a_reply_older_than_120_seconds_is_not_spoken",
    ),
    (
        "speak-a-voice-reply",
        "        if _field(payload, \"msg_type\") == \"CALL_REPLY\" and _field(payload, \"spoken\") == \"sheet\":",
        "        if _field(payload, \"msg_type\") == \"CALL_REPLY\":  # MUTANT: spoken=voice is spoken too",
        "a reply that was not marked spoken=sheet is put on the voice lane",
        "test_a_reply_reaches_its_own_voice_lane_when_every_condition_holds",
    ),
    (
        "voice-by-is-the-chair",
        "{\"by\": tag, \"text\": say, \"at\": _stamp(now)}",
        "{\"by\": \"conference-chair\", \"text\": say, \"at\": _stamp(now)}",
        "the voice entry is attributed to someone other than the lane's tag",
        "test_a_reply_reaches_its_own_voice_lane_when_every_condition_holds",
    ),
    (
        "forget-which-replies-were-spoken",
        "            conn.sadd(REPLIES_SEEN, rid)\n            conn.expire(REPLIES_SEEN, REPLIES_TTL_SECONDS)",
        "            conn.expire(REPLIES_SEEN, REPLIES_TTL_SECONDS)  # MUTANT: the reply is not remembered",
        "the same reply is put on the voice lane a second time",
        "test_a_reply_reaches_its_own_voice_lane_when_every_condition_holds",
    ),
    (
        "remember-replies-for-a-minute",
        "REPLIES_TTL_SECONDS = 86400",
        "REPLIES_TTL_SECONDS = 60  # MUTANT: the seen set expires in a minute",
        "handled reply ids are not kept for a day",
        "test_a_reply_reaches_its_own_voice_lane_when_every_condition_holds",
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

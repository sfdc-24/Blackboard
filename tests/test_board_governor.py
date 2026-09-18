#!/usr/bin/env python3
"""Offline suite for tools/board_governor.

WHAT IS AND IS NOT COVERED, STATED UP FRONT
  Covered: the fact extractor (board_facts.py) and the output parser
  (governor.parse_output). Both are pure functions over data.

  NOT covered here: running CLIPS. governor.py shells out to CLIPSDOS.exe under
  C:\\Program Files, which cannot exist on ubuntu-latest, so the rule engine
  itself is exercised on the authoring machine and by the mutation harness in
  tests/mutate_board_governor.py - never by this suite. A hosted check that
  quietly skipped the engine while reporting green would be worse than no check,
  so this file does not pretend to cover it.

  That split is deliberate: every assertion below runs with no network, no bus,
  no board and no CLIPS, which is what lets the workflow prove it offline by
  running it inside an empty network namespace.

CONVENTION
  A plain script, like the other suites in this directory: it prints each
  result, ends with a RESULT line, and exits non-zero on failure. `unittest
  discover` does not collect these and would exit 0 having run nothing.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools" / "board_governor"))

import board_facts  # noqa: E402
from board_facts import (ASK_PHASES, clips_string, emit_facts, field,  # noqa: E402
                         load_rows, looks_like_timestamp)
import governor  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print("PASS " + name)
    else:
        FAILED += 1
        print("FAIL " + name + ((" :: " + detail) if detail else ""))


def row(row_id, ts, tag, surface, action, payload, category="OPEN"):
    return [row_id, ts, tag, surface, action, payload, category, "", "", ""]


def board_file(rows, directory):
    path = Path(directory) / "board.json"
    path.write_text(json.dumps({"rows": [board_facts.COLUMNS] + rows}),
                    encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------- field anchoring
# `to=` must never be satisfied by `cc=` or `auto=`. This is copied from the
# fleet's own open_for_me.py rather than re-derived; a second idea of what a
# field is, is how a reader and a writer come to disagree.
check("a field is matched at a pipe boundary",
      field("BCB|v=1|to=claude-code-cli|cc=codex", "to") == "claude-code-cli")
check("auto= does not satisfy to=",
      field("BCB|v=1|auto=yes|cc=codex", "to") == "")
check("a missing field is empty, not an error",
      field("BCB|v=1", "answers") == "")
check("the first value wins for a repeated field",
      field("BCB|to=a|to=b", "to") == "a")

# ------------------------------------------------------------ column-shift refusal
# REQ-B4TQX9: a short sheetRow append shifts every field one column left, so the
# timestamp lands in Row_ID. Parsing such a row attributes one agent's claim to
# another, so it is refused rather than repaired.
check("an ISO timestamp in Row_ID is the column-shift signature",
      looks_like_timestamp("2026-09-16T05:21:26Z"))
check("a human date in Row_ID is refused too",
      looks_like_timestamp("Wednesday, September 16, 2026 at 6:52 AM EDT"))
check("an ordinary row id is not a timestamp",
      not looks_like_timestamp("CODEX-01A09BF0-PROTOTYPE-CONTRACT-REVIEW-20260916"))

with tempfile.TemporaryDirectory() as tmp:
    path = board_file([
        row("GOOD-1", "2026-09-16T05:00:00Z", "codex", "claude-code-cli",
            "DISPATCH", "BCB|v=1|id=GOOD-1|phase=DISPATCH"),
        row("2026-09-16T05:10:00Z", "shifted", "x", "y", "z", "w"),
        row("BAD-TS", "not-a-timestamp", "codex", "ALL", "APPEND", "text"),
        row("", "2026-09-16T05:20:00Z", "codex", "ALL", "APPEND", "text"),
    ], tmp)
    rows, refusals = load_rows(path)
    check("the well-formed row is parsed", len(rows) == 1, "got %d" % len(rows))
    check("three malformed rows are refused", len(refusals) == 3,
          "got %d: %r" % (len(refusals), refusals))
    check("a refusal says WHY, so a malformed board is visible",
          any("column-shift" in reason for _, reason in refusals))

# A refused row is OUTPUT, never a silent skip: quietly dropping rows would
# under-report collisions exactly when the board is malformed.
with tempfile.TemporaryDirectory() as tmp:
    # A genuinely SHORT row, built as a raw list on purpose: row() pads every
    # fixture to ten cells, so it cannot express the defect under test here.
    path = board_file([["SHORT", "2026-09-16T05:00:00Z", "a", "b", "c"]], tmp)
    rows, refusals = load_rows(path)
    check("a row with too few cells is refused rather than indexed",
          len(rows) == 0 and len(refusals) == 1)

# ------------------------------------------------------------------- row properties
with tempfile.TemporaryDirectory() as tmp:
    path = board_file([
        row("ROW-ID-ONLY", "2026-09-16T05:00:00Z", "codex", "claude-code-cli",
            "REVIEW", "prose with no bcb envelope at all"),
        row("R2", "2026-09-16T05:05:00Z", "claude-code-cli", "codex",
            "APPEND", "BCB|v=1|id=PAYLOAD-ID|phase=RESULT|answers=A-1, B-2"),
    ], tmp)
    rows, _ = load_rows(path)
    prose, bcb = rows[0], rows[1]
    check("phase falls back to Action_Type when the payload has no phase=",
          prose.phase == "REVIEW", prose.phase)
    check("bcb_id falls back to Row_ID for a prose row",
          prose.bcb_id == "ROW-ID-ONLY", prose.bcb_id)
    check("payload phase= wins over Action_Type", bcb.phase == "RESULT", bcb.phase)
    check("payload id= wins over Row_ID", bcb.bcb_id == "PAYLOAD-ID", bcb.bcb_id)
    check("answers splits on commas and trims", bcb.answers == ["A-1", "B-2"],
          repr(bcb.answers))
    check("a row with no answers yields an empty list", prose.answers == [])
    # grok-bot joined 2026-09-18 writing SEMICOLON lists, which the comma-only
    # split read as one token that answered nothing. Same rule as the other two
    # readers on this board: comma, semicolon or whitespace.
    semi = board_file([
        row("R3", "2026-09-18T06:44:18Z", "grok-bot", "claude-code-cli;vm-claude-code-cli",
            "APPEND", "BCB|v=1|id=SEMI|phase=RESULT|answers=A-1;B-2"),
        row("R4", "2026-09-18T06:45:00Z", "chatgpt-codex-desktop", "ALL",
            "APPEND", "BCB|v=1|id=SPACED|phase=RESULT|answers=WRK-3cd5f324 (row2350)"),
    ], tmp)
    semi_rows, _ = load_rows(semi)
    check("answers splits on semicolons too", semi_rows[0].answers == ["A-1", "B-2"],
          repr(semi_rows[0].answers))
    check("and a parenthetical annotation leaves the id matchable",
          semi_rows[1].answers[0] == "WRK-3cd5f324", repr(semi_rows[1].answers))
    # Source_Tag is server-stamped; the payload's from= is caller-supplied text.
    check("the column tag outranks the payload from=",
          bcb.from_tag == "claude-code-cli", bcb.from_tag)

# ------------------------------------------------------------------- ask allowlist
# EXACT membership. REVIEW_RESULT must never satisfy REVIEW - that is the
# substring defect the payload matcher was already hardened against.
check("REVIEW is an ask", "REVIEW" in ASK_PHASES)
check("BATON is an ask", "BATON" in ASK_PHASES)
check("REVIEW_RESULT is NOT an ask", "REVIEW_RESULT" not in ASK_PHASES)
check("RESULT is NOT an ask", "RESULT" not in ASK_PHASES)
check("PROGRESS is NOT an ask", "PROGRESS" not in ASK_PHASES)
# BACKLOG is parked ON PURPOSE (status=QUEUED-NOT-STARTED). Reporting parked
# work as ignored is a straight false positive.
check("BACKLOG is NOT an ask", "BACKLOG" not in ASK_PHASES)

# ----------------------------------------------------------------------- the facts
with tempfile.TemporaryDirectory() as tmp:
    path = board_file([
        # an ask nobody answers
        row("ASK-1", "2026-09-10T00:00:00Z", "codex", "claude-code-cli",
            "DISPATCH", "BCB|v=1|id=ASK-1|phase=DISPATCH"),
        # an ask that IS answered, by a RESULT rather than a CLAIM
        row("ASK-2", "2026-09-10T01:00:00Z", "codex", "claude-code-cli",
            "REVIEW", "BCB|v=1|id=ASK-2|phase=REVIEW"),
        row("ANS-2", "2026-09-10T02:00:00Z", "claude-code-cli", "codex",
            "RESULT", "BCB|v=1|id=ANS-2|phase=RESULT|answers=ASK-2"),
        # a claim, which is both a claim fact and an answer
        row("CLM-1", "2026-09-10T03:00:00Z", "vm-cli", "ALL",
            "CLAIM", "BCB|v=1|id=CLM-1|phase=CLAIM|answers=ASK-1"),
    ], tmp)
    rows, _ = load_rows(path)
    lines, counts = emit_facts(rows)
    text = "\n".join(lines)

    check("both asks become ask facts", counts["ask_facts"] == 2, str(counts))
    check("the claim becomes a claim fact", counts["claim_facts"] == 1, str(counts))
    # An answer is its OWN fact, not a property of claim: asks are answered by
    # RESULT rows far more often than by CLAIMs.
    check("both answering rows become answered facts, whatever their phase",
          counts["answered_facts"] == 2, str(counts))
    check("a claim is counted as both a claim and an answer",
          "(answered (id \"ASK-1\")" in text)
    check("a RESULT row answers too", "(answered (id \"ASK-2\")" in text)
    # COMPUTED, never pasted. The first draft of this file asserted two epoch
    # integers derived by hand; they were 10800 seconds apart while describing
    # fixture rows one hour apart, so at least one was simply wrong and the
    # suite would have failed for a reason nobody could read off the number.
    # A magic constant is a claim the reader cannot check.
    newest = int((datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc) - EPOCH).total_seconds())
    first_answer = int((datetime(2026, 9, 10, 2, 0, tzinfo=timezone.utc) - EPOCH).total_seconds())

    check("the clock is the NEWEST row, so a replay moves it with the board",
          ("(now %d)" % newest) in text, text.splitlines()[-2:])
    # The grammar epoch is the first row carrying answers= anywhere. Without it
    # every ask older than the convention reports as ignored, and the headline
    # number becomes an artifact of the board's age rather than a finding.
    check("the grammar epoch is the earliest answers= row",
          ("(grammar-epoch %d)" % first_answer) in text, text.splitlines()[-2:])
    check("and the fixtures really are one hour apart, as those two claim",
          newest - first_answer == 3600, str(newest - first_answer))

with tempfile.TemporaryDirectory() as tmp:
    path = board_file([
        row("ASK-ONLY", "2026-09-10T00:00:00Z", "codex", "claude-code-cli",
            "DISPATCH", "BCB|v=1|id=ASK-ONLY|phase=DISPATCH"),
    ], tmp)
    rows, _ = load_rows(path)
    lines, counts = emit_facts(rows)
    # Fail closed: with no epoch the rule cannot fire at all. A governor that
    # reports nothing is recoverable; one reporting thousands of false
    # positives gets switched off.
    check("a board that never adopted answers= emits NO epoch",
          counts["grammar_epoch"] is None and
          not any("grammar-epoch" in line for line in lines))

# ------------------------------------------------------------------ clips escaping
check("a quote in a row id is escaped, not injected",
      clips_string('a"b') == '"a\\"b"', clips_string('a"b'))
check("a backslash is escaped", clips_string("a\\b") == '"a\\\\b"',
      clips_string("a\\b"))

# --------------------------------------------------------------- the output parser
# Anything that is not a contract line is KEPT as noise: a CLIPS syntax error
# appears there and exit 0 does not mention it. Silently reporting zero
# collisions because the rule file failed to parse would be worse than nothing.
stdout = "\n".join([
    "CLIPS (6.4.2)",
    'OWNER|TARGET-A|codex|ROW-1|1789005600',
    'COLLISION|TARGET-A|codex|ROW-1|vm-cli|ROW-2|138',
    'UNANSWERED|ASK-1|DISPATCH|codex|ROW-9|150',
    "[PRNTUTIL2] Syntax Error: something went wrong",
    "",
])
owners, collisions, unanswered, noise = governor.parse_output(stdout)
check("one owner parsed", len(owners) == 1 and owners[0]["target"] == "TARGET-A")
check("owner timestamp is an int", owners[0]["ts"] == 1789005600)
check("one collision parsed with its gap",
      len(collisions) == 1 and collisions[0]["gap_seconds"] == 138)
check("the collider is distinguished from the owner",
      collisions[0]["owner_tag"] == "codex" and
      collisions[0]["collider_tag"] == "vm-cli")
check("one unanswered ask parsed",
      len(unanswered) == 1 and unanswered[0]["bcb_id"] == "ASK-1")
check("quiet minutes is an int", unanswered[0]["quiet_minutes"] == 150)
check("the phase is carried, so a BATON reads differently from a DISPATCH",
      unanswered[0]["phase"] == "DISPATCH")
check("a CLIPS syntax error is KEPT as noise, never discarded",
      any("Syntax Error" in line for line in noise), repr(noise))
check("the banner is noise too, not silently dropped",
      any("CLIPS" in line for line in noise))

empty_owners, empty_collisions, empty_unanswered, _ = governor.parse_output("")
check("empty output yields no findings rather than raising",
      empty_owners == [] and empty_collisions == [] and empty_unanswered == [])

print("RESULT passed=%d failed=%d" % (PASSED, FAILED))
sys.exit(1 if FAILED else 0)

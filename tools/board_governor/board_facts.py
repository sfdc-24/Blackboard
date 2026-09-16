#!/usr/bin/env python3
"""Turn Blackboard rows into CLIPS facts, refusing anything malformed.

WHY THIS FILE IS NOT A ONE-LINER

  The board's row grammar has already produced two defect classes that a naive
  parser walks straight into, both found by review rather than by use:

    1. COLUMN SHIFT (REQ-B4TQX9). A short sheetRow append shifts every field one
       column left, so the timestamp lands in Row_ID and the payload in
       Timestamp. Four such rows on the live board stopped the ORDER supervisor
       reading past row 2171 on 2026-09-14. A governor that parses them anyway
       would attribute one agent's claim to another.

    2. SUBSTRING ADDRESSING. Matching `to=` with a bare regex accepted rows
       addressed `to=chatgpt-codex-desktop` for `-Tag codex`, and
       `to=vm-claude-code-cli` for `claude-code-cli`. Fields are therefore
       anchored to a pipe or string start, exactly as scripts/open_for_me.py
       does it.

  Both rules are copied from the fleet's own tools (scripts/bcb_lint.py and
  scripts/open_for_me.py) rather than re-derived, so the governor agrees with
  the linter about what a row says. Where this file disagrees with those, they
  win and this is the defect.

WHAT IT REFUSES, AND WHY THAT IS OUTPUT TOO
  A refused row is reported, never silently dropped. A governor that quietly
  skips rows it cannot read would under-report collisions precisely when the
  board is malformed, which is when supervision matters most.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

EXPECTED_COLS = 10
COLUMNS = [
    "Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type",
    "Payload", "Category", "Project Tag", "Gist", "Sub-Gist",
]

ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")
HUMAN_DATE = re.compile(
    r"^(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+\w+\s+\d{1,2},\s+\d{4}",
    re.IGNORECASE,
)


def looks_like_timestamp(value: str) -> bool:
    """A Row_ID that parses as a date is the column-shift signature."""
    return bool(ISO_TS.match(value) or HUMAN_DATE.match(value))


def field(payload: str, name: str) -> str:
    """One BCB field, anchored so `to=` never matches `cc=` or `auto=`."""
    match = re.search(r"(?:^|\|)\s*" + re.escape(name) + r"=([^|]*)", payload)
    return match.group(1).strip() if match else ""


def parse_ts(value: str):
    if not ISO_TS.match(value):
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clips_string(value: str) -> str:
    """CLIPS string literal: escape backslash and quote, nothing else."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Row:
    __slots__ = ("index", "row_id", "ts", "tag", "target", "action", "payload", "category")

    def __init__(self, index, row_id, ts, tag, target, action, payload, category):
        self.index = index
        self.row_id = row_id
        self.ts = ts
        self.tag = tag
        self.target = target
        self.action = action
        self.payload = payload
        self.category = category

    @property
    def phase(self) -> str:
        return field(self.payload, "phase") or self.action

    @property
    def bcb_id(self) -> str:
        return field(self.payload, "id") or self.row_id

    @property
    def answers(self):
        raw = field(self.payload, "answers")
        return [part.strip() for part in raw.split(",") if part.strip()] if raw else []

    @property
    def from_tag(self) -> str:
        """The claimed author. NOT authority: a Source_Tag is a claimed lane.

        Preference order is deliberate: the server-stamped Source_Tag column
        beats the payload's own from=, because the payload is caller-supplied
        text and the column is not.
        """
        return self.tag or field(self.payload, "from")


def load_rows(path):
    """Read a bus `action=read` dump. Returns (rows, refusals)."""
    with open(path, encoding="utf-8") as handle:
        blob = json.load(handle)
    raw = blob.get("rows") if isinstance(blob, dict) else blob
    if not raw:
        raise SystemExit(f"{path}: no rows")

    header = [str(cell).strip() for cell in raw[0]]
    while header and header[-1] == "":
        header.pop()
    if header != COLUMNS:
        raise SystemExit(f"{path}: unexpected header {header}")

    rows, refusals = [], []
    for index, cells in enumerate(raw[1:], start=1):
        cells = [("" if cell is None else str(cell)) for cell in cells]
        if len(cells) < 6:
            refusals.append((index, f"row has {len(cells)} columns, expected at least 6"))
            continue
        row_id, ts_text, tag, target, action, payload = (c.strip() for c in cells[:6])
        category = cells[6].strip() if len(cells) > 6 else ""

        if looks_like_timestamp(row_id):
            refusals.append((index, "Row_ID holds a timestamp: column-shift class, refused"))
            continue
        if not ts_text:
            refusals.append((index, "Timestamp cell empty"))
            continue
        ts = parse_ts(ts_text)
        if ts is None:
            refusals.append((index, f"Timestamp not ISO-8601: {ts_text[:40]!r}"))
            continue
        if not row_id:
            refusals.append((index, "Row_ID empty"))
            continue

        rows.append(Row(index, row_id, ts, tag, target, action, payload, category))
    return rows, refusals


# Phases that constitute ASKING someone for something.
#
# THE TEST: does the row leave an OBLIGATION on a named recipient? A row that
# reports, decides, advises, corrects or constrains does not.
#
# HOW THIS LIST WAS BUILT, AND WHY NOT THE OBVIOUS WAY
#   The first version held only DISPATCH/REVIEW_REQUEST/REQUEST, and it could
#   not see the one row the whole rule exists for: the 09-16 05:21Z review
#   request that sat unanswered is Action_Type=REVIEW with prose in the payload
#   and its recipients in the Target_Surface COLUMN.
#
#   The tempting fix - "a row with a Target_Surface column is an ask" - is wrong
#   by measurement. MEASURED on the live board: 859 rows address ONLY through
#   the column, and 683 of those are APPEND and 137 are RESULT. Classifying by
#   addressing surface would make ~2500 asks and the rule would be pure noise.
#   Addressing says WHO, never WHETHER.
#
#   So the list is widened by MEANING, and every phase below was read on real
#   rows (scratchpad/governor/sample_phases.py) before being added or refused.
#
# INCLUDED, with the row that justifies it
#   DISPATCH       156  work handed over; the original case
#   REVIEW_REQUEST  14  asks explicitly
#   REQUEST          4  asks explicitly
#   REVIEW           8  "Comment/Response requested" - the ground-truth row
#   ORDER            5  priority=CRITICAL, to=ALL; an instruction
#   TASK             1  "ACTION: (1) start ..."
#   HANDOFF          1  assigns authorship of a deliverable
#   BATON           24  class=ORDER, execution passed to a named lane; a baton
#                       nobody picks up is precisely the failure being hunted
#
# DELIBERATELY EXCLUDED, so the reasoning survives the next reader
#   PROGRESS/STATUS/BLOCKED  report state; they answer, they do not ask
#   RULING/AWARD             decide; the obligation already landed elsewhere
#   ADVISORY                 advises - no obligation
#   AMEND/PROTOCOL-COMMENT   correct a prior row
#   GOV                      publishes to a feed
#   HOLD/HALT                constrain; "stop" is not a request for a reply
#   BACKLOG                  status=QUEUED-NOT-STARTED - parked ON PURPOSE.
#                            Calling parked work "ignored" is a false positive.
#
# Membership is EXACT, never substring: REVIEW_RESULT must never match REVIEW.
ASK_PHASES = (
    "DISPATCH", "REVIEW_REQUEST", "REQUEST",
    "REVIEW", "ORDER", "TASK", "HANDOFF", "BATON",
)


def emit_facts(rows):
    """CLIPS deffacts text for claims, asks, answers, and the board's clock.

    WHY answers IS ITS OWN FACT AND NOT A PROPERTY OF claim
      An ask is very rarely answered by a CLAIM. Tonight's asks were answered by
      REVIEW_RESULT and RESULT rows carrying answers=<id>. A rule keyed on
      "dispatch with no claim" would therefore fire on almost every dispatch and
      be ignored within a day, which is how a governor becomes furniture.

    WHY THE CLOCK IS A FACT AND NOT time.time()
      An unanswered ask is only interesting once enough time has passed, and the
      threshold must be measured against the BOARD, not against the machine that
      happens to run this. Replaying an old board dump must produce the same
      verdicts it would have produced then; a wall-clock rule would call every
      historical ask stale. So `now` is the newest row's timestamp.
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    lines = []
    claims = asks = answers = 0
    newest = 0

    # THE GRAMMAR EPOCH, and why the rule needs it.
    #   answers= is a CONVENTION, not a property of the board. It first appears
    #   2026-09-04T22:29Z, 1107 rows in. Every ask written before that can never
    #   be marked answered no matter who answered it, so without this floor the
    #   rule reports them all as ignored and its headline number is an artifact
    #   of the board's age. MEASURED: answered-rate by week is 0% / 37% / 73%
    #   across W36 / W37 / W38 - the 0% week is entirely pre-convention.
    #   Derived from the dump rather than hardcoded, so replaying any board
    #   computes its own floor.
    answers_epoch = min((r.ts for r in rows if r.answers), default=None)

    for row in rows:
        seconds = int((row.ts - epoch).total_seconds())
        newest = max(newest, seconds)
        phase = row.phase.upper()

        if phase == "CLAIM":
            for target in row.answers or [""]:
                lines.append(
                    "  (claim (row-id {}) (bcb-id {}) (tag {}) (target {}) (ts {}))".format(
                        clips_string(row.row_id), clips_string(row.bcb_id),
                        clips_string(row.from_tag), clips_string(target), seconds,
                    )
                )
                claims += 1

        if phase in ASK_PHASES:
            lines.append(
                "  (ask (row-id {}) (bcb-id {}) (tag {}) (phase {}) (ts {}))".format(
                    clips_string(row.row_id), clips_string(row.bcb_id),
                    clips_string(row.from_tag), clips_string(phase), seconds,
                )
            )
            asks += 1

        # EVERY row that answers something, whatever its phase. A claim answers
        # too, so it can appear as both - that is correct, not double counting.
        for target in row.answers:
            if not target:
                continue
            lines.append(
                "  (answered (id {}) (by {}) (ts {}))".format(
                    clips_string(target), clips_string(row.from_tag), seconds,
                )
            )
            answers += 1

    if newest:
        lines.append("  (now {})".format(newest))

    # If no row anywhere carries answers=, the rule cannot distinguish silence
    # from a board that never adopted the convention, and it emits no floor -
    # which stops unanswered-ask from firing at all. That is deliberate: a
    # governor reporting nothing is recoverable, one reporting 2500 false
    # positives gets switched off.
    epoch_seconds = None
    if answers_epoch is not None:
        epoch_seconds = int((answers_epoch - epoch).total_seconds())
        lines.append("  (grammar-epoch {})".format(epoch_seconds))

    return lines, {
        "claim_facts": claims,
        "ask_facts": asks,
        "answered_facts": answers,
        "board_now": newest,
        "grammar_epoch": epoch_seconds,
    }

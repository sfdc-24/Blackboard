#!/usr/bin/env python3
"""The trail of one doc, read for a page: every receipt line any writer left, and the answer under it.

WHY THIS EXISTS
  Step 4 of the broken-telephone fix (Converspan Channel Architecture v0.1):
  "the Converspan page shows the live task/receipt trail during a call". The
  doc is the record with no TTL and every writer - drive-intake, the
  claude-api worker, the laptop lane on the laptop's behalf - writes the
  same line shape into it. This parses that shape back, so a page can show
  it without a second grammar: what state, who said so, when, and the
  bounded answer the worker wrote under its terminal line.

WHAT IT NEVER RETURNS
  The request itself. The page is a call surface; the trail is receipts and
  the answer lines, never the owner's words above the RECEIPTS heading.
"""
from __future__ import annotations

import re

LINE = re.compile(
    r"^\s*(?P<state>[A-Z]{4,12}) - task (?P<task>task-[0-9a-f]{16}) - request (?P<request>\S+) - "
    r"rev (?P<rev>\S+) - recipient (?P<recipient>\S+) - by (?P<by>\S+) - "
    r"(?P<at>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)(?P<rest>.*)$")
ID_TAIL = re.compile(r"\s+-\s+id\s+(?P<id>[0-9a-f]{16})\s*$")
RESULT_PREFIX = "> "
RESULT_LINES_MAX = 200
LINE_CHARS_MAX = 400


def parse(text: str) -> list:
    """Every receipt line in the doc, in order, each with the answer lines that follow it.

    A line in the shared shape is a receipt wherever it sits. A line starting with "> " belongs
    to the receipt above it (the worker and the lane write the answer that way). Anything else
    is not the trail and is not returned. Bounded: a long answer is cut and SAYS so (PR 354).
    """
    entries = []
    for raw in (text or "").splitlines():
        match = LINE.match(raw)
        if match:
            rest = match.group("rest") or ""
            receipt_id = ""
            tail = ID_TAIL.search(rest)
            if tail:
                receipt_id = tail.group("id")
                rest = rest[:tail.start()]
            detail = rest.strip()
            if detail.startswith("- "):
                detail = detail[2:].strip()
            entries.append({
                "state": match.group("state"), "task": match.group("task"),
                "request": match.group("request"), "rev": match.group("rev"),
                "recipient": match.group("recipient"), "by": match.group("by"),
                "at": match.group("at"), "detail": detail[:LINE_CHARS_MAX], "id": receipt_id,
                "result": [],
            })
            continue
        stripped = raw.lstrip()
        if stripped.startswith(RESULT_PREFIX) and entries:
            lines = entries[-1]["result"]
            if len(lines) < RESULT_LINES_MAX:
                lines.append(stripped[len(RESULT_PREFIX):][:LINE_CHARS_MAX])
            elif len(lines) == RESULT_LINES_MAX:
                lines.append("[answer cut here at %d lines]" % RESULT_LINES_MAX)
    return entries


def tasks_of(entries: list) -> list:
    """The task ids in the trail, first seen first."""
    seen, out = set(), []
    for entry in entries:
        if entry["task"] not in seen:
            seen.add(entry["task"])
            out.append(entry["task"])
    return out


def latest(entries: list, task_id: str):
    """The last line written for the task, or None."""
    found = [e for e in entries if e["task"] == task_id]
    return found[-1] if found else None

#!/usr/bin/env python3
"""Truncate the board to a past instant, so the rule can be tested against a
real incident AT THE TIME IT WAS HAPPENING.

WHY THIS IS THE STRONGEST TEST AVAILABLE
  Every check so far asks the rule about a board where the incident is already
  resolved. That only proves it does not cry wolf. The claim I actually want to
  make is the harder one: THIS RULE WOULD HAVE CAUGHT IT.

  On 2026-09-16 at 05:21Z Codex asked claude-code-cli for a review
  (CODEX-01A09BF0-PROTOTYPE-CONTRACT-REVIEW-20260916). Nothing answered it until
  the waker did at 05:47Z, and it was found by reading rows by hand.

  So: cut the board at 06:30Z but SUPPRESS the answering row, and the ask must
  appear as UNANSWERED. Cut at the present with the answer intact, and it must
  not. One rule, two boards, opposite verdicts, decided by the evidence rather
  than by my description of it.

  Because `now` is derived from the newest surviving row, truncation moves the
  rule's clock too - which is exactly why the clock was made a fact.

USAGE
  python replay_at.py <board.json> <out.json> <cutoff-iso> [--drop-id ID ...]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from board_facts import ISO_TS  # noqa: E402


def parse(value):
    if not ISO_TS.match(value):
        raise SystemExit("cutoff must be ISO-8601, got {!r}".format(value))
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    src, out, cutoff_text = argv[0], argv[1], argv[2]
    drop = set()
    rest = argv[3:]
    while rest:
        if rest[0] == "--drop-id" and len(rest) > 1:
            drop.add(rest[1])
            rest = rest[2:]
        else:
            raise SystemExit("unexpected argument: {!r}".format(rest[0]))

    cutoff = parse(cutoff_text)
    with open(src, encoding="utf-8") as handle:
        blob = json.load(handle)
    raw = blob["rows"]
    header, body = raw[0], raw[1:]

    kept, late, dropped = [], 0, 0
    for cells in body:
        text = str(cells[1]).strip() if len(cells) > 1 else ""
        if not ISO_TS.match(text):
            kept.append(cells)          # malformed rows stay; refusals are output too
            continue
        ts = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        if ts > cutoff:
            late += 1
            continue
        # Suppressing a specific ANSWER is how "before it was answered" is
        # simulated without pretending the ask itself did not exist.
        if drop and any(d in str(c) for c in cells for d in drop):
            dropped += 1
            continue
        kept.append(cells)

    blob["rows"] = [header] + kept
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(blob, handle, ensure_ascii=False)
    print("replay board -> {}".format(out))
    print("  kept {}   after-cutoff removed {}   id-suppressed {}".format(
        len(kept), late, dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

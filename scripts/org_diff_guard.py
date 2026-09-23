#!/usr/bin/env python3
"""Say what publishing would change, and refuse a snapshot that lost records.

WHY THIS IS A FILE AND NOT FOUR LINES IN THE WORKFLOW
  It started as a heredoc inside org-data-refresh.yml. Two reasons it moved:
  a heredoc in a YAML block scalar is one CRLF checkout away from a terminator
  the shell never matches, and logic that decides whether to publish deserves
  its own tests. It now has both.

WHAT IT GUARDS
  On 2026-09-17 the dev org was re-seeded: the stock Salesforce sample data was
  deleted and a smaller, real set of accounts created in its place. Between
  20:28Z and 21:49Z the counts went accounts 29 -> 9, contacts 26 -> 1,
  opportunities 32 -> 0. A scheduled publisher would have replaced the live
  page with that, correctly and silently, in the middle of someone's edit.

  So a section that LOST records stops the run. Growth never does - adding
  records is how the org normally changes. The refusal is not a claim that the
  new data is wrong; it is a claim that nobody has said it is right yet, and
  --allow-shrink is how they say it.

  A live page that cannot be read is treated as UNKNOWN and exits 2. It is not
  treated as zero: "I could not see it" and "there is nothing there" are the
  two answers this project keeps confusing, and only one of them is safe to
  publish over.

Usage
-----
    python scripts/org_diff_guard.py --live live-org.json --built build/data/org.json
    python scripts/org_diff_guard.py --live live-org.json --built new.json --allow-shrink
"""
from __future__ import annotations

import argparse
import json
import sys

OK = 0
REFUSED = 1
UNREADABLE = 2

TRACKED_TOTALS = ("pipeline_open", "closed_won")


def _counts(snapshot):
    return (snapshot.get("_meta") or {}).get("counts") or {}


def compare(live, built):
    """Return (report lines, list of shrink descriptions).

    Pure: no printing, no exit, so the tests can assert on both halves.
    """
    lines = ["live page generated %s, this build %s"
             % ((live.get("_meta") or {}).get("generated", "<unknown>"),
                (built.get("_meta") or {}).get("generated", "<unknown>"))]
    before_counts, after_counts = _counts(live), _counts(built)
    shrank = []
    for section, after in after_counts.items():
        before = before_counts.get(section)
        if before is None:
            lines.append("  %-14s new section, %s records" % (section, after))
            continue
        if before == after:
            lines.append("  %-14s unchanged at %s" % (section, after))
            continue
        lines.append("  %-14s %s -> %s" % (section, before, after))
        if isinstance(before, int) and isinstance(after, int) and after < before:
            shrank.append("%s %d -> %d" % (section, before, after))

    for section in before_counts:
        if section not in after_counts:
            lines.append("  %-14s DROPPED: the live page has it, this build does "
                         "not" % (section,))
            shrank.append("%s section disappeared" % (section,))

    live_summary = live.get("summary") or {}
    built_summary = built.get("summary") or {}
    for key in TRACKED_TOTALS:
        lines.append("  %-14s %s -> %s" % (key, live_summary.get(key),
                                           built_summary.get(key)))
    return lines, shrank


def _load(path, label):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            "cannot read the %s snapshot at %s: %s\n"
            "Treating that as UNKNOWN rather than as an empty org: a page that "
            "could not be read is not a page with nothing on it.\n"
            % (label, path, exc))
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="org_diff_guard",
        description="Report what publishing would change; refuse a lossy build.")
    parser.add_argument("--live", required=True,
                        help="the snapshot the site is serving now")
    parser.add_argument("--built", required=True,
                        help="the snapshot this run just built")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="publish even though a section lost records")
    args = parser.parse_args(argv)

    live = _load(args.live, "live")
    built = _load(args.built, "built")
    if live is None or built is None:
        return UNREADABLE

    lines, shrank = compare(live, built)
    for line in lines:
        sys.stdout.write(line + "\n")

    if shrank and not args.allow_shrink:
        sys.stderr.write(
            "REFUSING: records disappeared since the live page: "
            + "; ".join(shrank) + "\n"
            "If that is genuinely what the org now holds, re-run with "
            "allow_shrink checked.\n")
        return REFUSED
    if shrank:
        sys.stdout.write("publishing a smaller org because allow_shrink was set: "
                         + "; ".join(shrank) + "\n")
    return OK


if __name__ == "__main__":
    sys.exit(main())

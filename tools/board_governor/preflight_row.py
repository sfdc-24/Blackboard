#!/usr/bin/env python3
"""Validate a board row spec WITHOUT sending it.

WHY THIS EXISTS
  scripts/append.py refuses a malformed payload, which is the right place for the
  refusal - but it refuses *at send time*, and on success it has already
  appended. There is no dry run, and "a row already on an append-only board
  cannot be taken back" is append.py's own comment.

  So this applies append.py's REAL guard - imported, not reimplemented, because a
  second copy of a rule is how a writer and a reader come to disagree - plus the
  BCB-1 hazards that guard does not cover, and prints the exact ten cells that
  would be written.

WHAT IT CHECKS
  1. append.py's own _conflicting_keys: an authority key written twice with
     different values.
  2. A literal '|' inside any VALUE. BCB-1 has no escaping, so that silently
     starts a new key.
  3. The payload starts with 'BCB|' (append.py requires it).
  4. Required fields present; the resolved 10-cell row printed for eyeballing.

USAGE
  python preflight_row.py <row.json> [--scripts <dir holding append.py>]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Resolved from this file, never hardcoded. The previous default pointed at a
# per-session scratchpad directory, so it could not resolve on any machine but
# the session that wrote it - and would stop resolving there too once that
# temporary directory was cleaned. A preflight that exits 1 on its default
# teaches a lane to skip the preflight rather than discover the flag, which is
# the opposite of what this script is for.
# This file lives at <repo>/tools/board_governor/, so parents[2] is the repo
# root and append.py sits at <repo>/scripts/. --scripts still overrides.
DEFAULT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

SCHEMA = ["Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type",
          "Payload", "Category", "Project Tag", "Gist", "Sub-Gist"]


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    spec_path = Path(argv[0])
    scripts = DEFAULT_SCRIPTS
    if "--scripts" in argv:
        scripts = Path(argv[argv.index("--scripts") + 1])

    sys.path.insert(0, str(scripts))
    try:
        from append import AUTHORITY_KEYS, _conflicting_keys  # noqa: E402
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "cannot import append.py from {}: {}\n"
            "Refusing to validate with a reimplemented copy of its rule."
            .format(scripts, exc)
        )

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    problems = []

    for required in ("row_id", "source_tag", "payload"):
        value = spec.get(required)
        if not isinstance(value, str) or not value.strip():
            problems.append("missing or empty required field: {}".format(required))
    payload = spec.get("payload", "")

    if not payload.startswith("BCB|"):
        problems.append("payload does not start with 'BCB|' - append.py will refuse")

    conflicts = sorted(_conflicting_keys(payload))
    if conflicts:
        problems.append("authority keys written twice with DIFFERENT values: "
                        + ", ".join(conflicts))

    # A '|' inside a value is indistinguishable from a new key. Detect it by
    # checking every pipe-separated segment after the first: a segment with no
    # '=' is either a stray pipe inside a value or a malformed field.
    for index, segment in enumerate(payload.split("|")):
        if index == 0:
            continue                       # the 'BCB' envelope marker
        if "=" not in segment:
            problems.append(
                "segment {} has no '=' , so a value contains a literal pipe or a "
                "field is malformed: {!r}".format(index, segment[:70]))

    # Report every authority key actually present, so the row's claims are
    # visible before they are published rather than after.
    print("=" * 72)
    print("AUTHORITY KEYS IN THIS PAYLOAD")
    for key in AUTHORITY_KEYS:
        found = re.findall(r"(?:^|\|)" + re.escape(key) + r"=([^|]*)", payload)
        if found:
            for value in found:
                shown = value if len(value) <= 80 else value[:77] + "..."
                print("  {:<14} = {}".format(key, shown))

    row = [
        spec["row_id"], "<stamped by append.py>", spec["source_tag"],
        spec.get("target_surface", "ALL"), spec.get("action_type", "APPEND"),
        payload, spec.get("category", "OPEN"), spec.get("project_tag", ""),
        spec.get("gist", ""), spec.get("subgist", ""),
    ]
    print()
    print("=" * 72)
    print("THE TEN CELLS THAT WOULD BE WRITTEN")
    for name, value in zip(SCHEMA, row):
        text = str(value)
        if name == "Payload":
            print("  {:<14}: {} chars, {} fields".format(
                name, len(text), text.count("|")))
        else:
            print("  {:<14}: {}".format(name, text if len(text) <= 96 else text[:93] + "..."))

    print()
    if problems:
        print("REFUSE - fix these before sending:")
        for problem in problems:
            print("  - {}".format(problem))
        return 1
    print("PREFLIGHT OK - append.py's own guard accepts this payload.")
    print("Read-back after sending is still the only proof of the write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

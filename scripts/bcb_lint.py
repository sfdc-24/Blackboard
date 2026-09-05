#!/usr/bin/env python3
"""BCB grammar validator for Blackboard - Alpha DB (GPT-BCBLINT-001).

Reads the whole board in one call (POST to the v1 bus with action=read,
title 'Blackboard - Alpha DB') or from a saved JSON dump, then reports
every MALFORMED row plus grammar warnings.

Contract sources:
  - SFDC24 - BLACKBOARD CHANNEL BENCHMARK v1 (BCB-1), section 4 (row grammar)
  - Sheet schema: Row_ID | Timestamp | Source_Tag | Target_Surface |
    Action_Type | Payload | Category | Project Tag | Gist | Sub-Gist
  - Known failure class (REQ-B4TQX9 / the 2026-09-02 13:01 chat-mobile row):
    a short sheetRow array shifts every field one column left, so the
    timestamp lands in Row_ID and the payload in Timestamp.

Usage:
  python bcb_lint.py --file board_read.json
  python bcb_lint.py --live            # BUS_URL and BUS_SECRET from env (D-18)
  python bcb_lint.py --file dump.json --json report.json

The secret is only ever read from the BUS_SECRET environment variable.
It is never accepted as a flag and never written to any output.
"""

import argparse
import json
import os
import re
import sys
import urllib.request

EXPECTED_COLS = 10
COLUMNS = [
    "Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type",
    "Payload", "Category", "Project Tag", "Gist", "Sub-Gist",
]

ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")
UUID_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
WRK_ID = re.compile(r"^WRK-[0-9a-f]{8}$")
# Human-format date, e.g. "Wednesday, September 2, 2026 at 1:01 PM"
HUMAN_DATE = re.compile(
    r"^(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}"
)

# ASSET (GLASSES intake pointers) and BATON (D-33 exit rows) are live fleet
# conventions that postdate BCB-1 v1; accepted here, but they belong in a
# BCB-1 version bump (section 8 refinement protocol).
KNOWN_PHASES = {"DISPATCH", "RESULT", "VERIFY", "VIEWPORT", "PROTOCOL-COMMENT", "ASSET", "BATON"}
# Required BCB fields per phase, per BCB-1 section 4 (VIEWPORT per live convention).
PHASE_REQUIRED = {
    "DISPATCH": ["id", "from", "to", "task"],
    "RESULT": ["id", "from"],
    "VERIFY": ["id", "by", "pass"],
    "VIEWPORT": ["vseq", "by"],
    "PROTOCOL-COMMENT": ["from", "comment"],
}


def looks_like_timestamp(value):
    value = value.strip()
    return bool(ISO_TS.match(value) or HUMAN_DATE.match(value))


def parse_bcb(payload):
    """Split a BCB payload into ordered (key, value) pairs; bare segments get key ''. """
    pairs = []
    for segment in payload.split("|")[1:]:  # skip the leading literal 'BCB'
        if "=" in segment:
            key, _, val = segment.partition("=")
            pairs.append((key.strip(), val.strip()))
        else:
            pairs.append(("", segment.strip()))
    return pairs


def validate_row(index, row):
    """Return (malformed, warnings): lists of finding strings for one row."""
    malformed, warnings = [], []
    cells = ["" if c is None else str(c) for c in row]

    if len(cells) != EXPECTED_COLS:
        malformed.append(f"row has {len(cells)} columns, expected {EXPECTED_COLS}")
        cells = (cells + [""] * EXPECTED_COLS)[:EXPECTED_COLS]

    row_id, ts, source, target, action, payload = (c.strip() for c in cells[:6])

    # --- structural checks (column-shift class, REQ-B4TQX9) ---
    if looks_like_timestamp(row_id):
        malformed.append(
            "Row_ID cell holds a timestamp - fields shifted one column left "
            "(short sheetRow append; the appender must supply all ten columns)"
        )
    if ts and not ISO_TS.match(ts):
        if ts.startswith("BCB|") or len(ts) > 60:
            malformed.append("Timestamp cell holds payload text - corroborates a left shift")
        else:
            malformed.append(f"Timestamp is not ISO-8601: {ts[:60]!r}")
    if not ts and not looks_like_timestamp(row_id):
        malformed.append("Timestamp cell is empty")

    if all(not c.strip() for c in cells):
        malformed.append("entirely empty row")
    elif not payload and not any(c.strip() for c in cells[5:]):
        malformed.append(
            "empty Payload with no content columns - append that wrote nothing "
            "(REQ-V8QD7R class)"
        )

    if row_id and not (UUID_ID.match(row_id) or WRK_ID.match(row_id)) and not looks_like_timestamp(row_id):
        warnings.append(f"nonstandard Row_ID shape: {row_id[:40]!r}")
    if not row_id:
        warnings.append("empty Row_ID")

    # --- BCB grammar checks (only rows that claim the grammar) ---
    bcb_payload = None
    if payload.startswith("BCB|"):
        bcb_payload = payload
    elif ts.startswith("BCB|"):
        bcb_payload = ts  # shifted row: still lint the displaced payload

    if bcb_payload:
        pairs = parse_bcb(bcb_payload)
        fields = {}
        for key, val in pairs:
            if key and key not in fields:
                fields[key] = val

        if fields.get("v") != "1":
            warnings.append("BCB row missing v=1")
        phase = fields.get("phase", "")
        if not phase:
            warnings.append("BCB row missing phase=")
        elif phase not in KNOWN_PHASES:
            warnings.append(f"unknown BCB phase: {phase!r}")
        for req in PHASE_REQUIRED.get(phase, []):
            if req not in fields:
                warnings.append(f"BCB {phase} row missing required field {req}=")

        # R-rule: the row's Source_Tag must match from= (by= for VIEWPORT/VERIFY),
        # "or the row fails R by definition" (BCB-1 section 4).
        author = fields.get("by") if phase in ("VIEWPORT", "VERIFY") else fields.get("from")
        if author and source and author != source:
            warnings.append(f"Source_Tag {source!r} != BCB author {author!r} (fails R)")

    return malformed, warnings


def read_live():
    url = os.environ.get("BUS_URL", "")
    secret = os.environ.get("BUS_SECRET", "")
    if not url or not secret:
        sys.exit("--live needs BUS_URL and BUS_SECRET in the environment (D-18: never on the command line)")
    body = json.dumps({"action": "read", "title": "Blackboard - Alpha DB", "secret": secret}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})

    # Apps Script answers the POST with a 302 to script.googleusercontent.com.
    # The redirect must be followed as a BARE GET: forwarding the JSON
    # Content-Type header across hosts makes googleusercontent return 404,
    # so the automatic redirect handler cannot be used here.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=120) as resp:
            return json.load(resp)  # direct answer, no redirect
    except urllib.error.HTTPError as err:
        if err.code not in (301, 302, 303, 307):
            raise
        location = err.headers.get("Location")
        if not location:
            raise
        with urllib.request.urlopen(location, timeout=120) as resp:
            return json.load(resp)


def main():
    ap = argparse.ArgumentParser(description="BCB grammar validator (GPT-BCBLINT-001)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="saved bus read response JSON")
    src.add_argument("--live", action="store_true", help="read the board via the v1 bus")
    ap.add_argument("--json", dest="json_out", help="also write findings as JSON to this path")
    args = ap.parse_args()

    if args.live:
        data = read_live()
    else:
        with open(args.file, encoding="utf-8-sig") as fh:
            data = json.load(fh)

    if not data.get("ok"):
        sys.exit(f"bus/file response not ok: {data.get('error', 'unknown')}")

    rows = data["rows"]
    header, body = rows[0], rows[1:]
    if [str(c).strip() for c in header] != COLUMNS:
        print(f"NOTE: header row differs from expected schema: {header}")

    malformed_rows, warned_rows = [], []
    for i, row in enumerate(body):
        malformed, warnings = validate_row(i, row)
        cells = ["" if c is None else str(c) for c in row] + [""] * EXPECTED_COLS
        record = {
            "sheet_row": i + 2,  # 1-based, after the header row
            "row_id": cells[0][:60],
            "timestamp": cells[1][:60],
            "source_tag": cells[2][:40],
        }
        if malformed:
            malformed_rows.append({**record, "findings": malformed})
        if warnings:
            warned_rows.append({**record, "findings": warnings})

    print(f"Board rows checked : {len(body)}")
    print(f"MALFORMED rows     : {len(malformed_rows)}")
    print(f"Rows with warnings : {len(warned_rows)}")
    print()
    for rec in malformed_rows:
        print(f"MALFORMED sheet row {rec['sheet_row']}  Row_ID={rec['row_id']!r}  Timestamp={rec['timestamp']!r}")
        for f in rec["findings"]:
            print(f"    - {f}")
    if warned_rows:
        print()
        counts = {}
        for rec in warned_rows:
            for f in rec["findings"]:
                key = f.split(":")[0].split(" (")[0]
                counts[key] = counts.get(key, 0) + 1
        print("Warning summary (grammar/protocol, not structural):")
        for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {n:4d} x {key}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"checked": len(body), "malformed": malformed_rows, "warnings": warned_rows}, fh, indent=2)
        print(f"\nJSON report written to {args.json_out}")

    return 1 if malformed_rows else 0


if __name__ == "__main__":
    sys.exit(main())

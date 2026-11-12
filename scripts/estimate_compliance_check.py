#!/usr/bin/env python3
"""Check a board export window for missing estimate start or close rows.

Read-only. It never posts, and the default loader only opens a local JSON or
CSV file (URLs are refused). Tests use fixtures only.

    python3 scripts/estimate_compliance_check.py \
        --since 2026-11-12T00:00:00Z \
        --until 2026-11-12T23:59:59Z \
        --source tests/fixtures/estimate_compliance/clean.json

WINDOW
  --since and --until are required inclusive UTC timestamps. A row *triggers*
  a finding only when its Timestamp is in [since, until]. A matching
  counterpart may sit anywhere in the same export, including outside the
  window, so a complete export with a cropped window does not false-flag a
  pair that closed a few minutes later. Rows with no parseable timestamp
  never trigger a finding, but they can still serve as counterparts.

ROW KINDS (phase or msg_type, case-insensitive, exact token)
  ESTIMATE       phase or msg_type is ESTIMATE (not ESTIMATE_DONE)
  ESTIMATE_DONE  phase or msg_type is ESTIMATE_DONE
  DONE           phase or msg_type is DONE, or the row is ESTIMATE_DONE

GAPS
  OPEN_ESTIMATE          an ESTIMATE in the window with no matching
                         ESTIMATE_DONE in the export.
  DONE_WITHOUT_ESTIMATE  a DONE or ESTIMATE_DONE in the window with no
                         matching ESTIMATE in the export.

  Two ESTIMATE rows for one task (a reestimate) are closed by a single
  ESTIMATE_DONE. A generic DONE row does not close an OPEN_ESTIMATE.

MATCHING
  Primary key: the BCB `task=` field, stripped, case-sensitive.
  If a row has no `task=`, an ESTIMATE is keyed by its Row_ID.
  A DONE row also matches when its `estimate=` field equals a task key
  or an ESTIMATE row's Row_ID.

EXIT
  0 clean (no gaps), 1 one or more gaps, 2 the check could not be made
  (bad flags, unreadable file, unparseable window, and so on).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence
from urllib.parse import urlparse

ISO_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$"
)
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*=")
HEADER_ID_CELL = "Row_ID"


def parse_utc(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to aware UTC, or None if it does not parse."""
    if not isinstance(value, str) or not ISO_TS.match(value):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def require_utc(label: str, value: str) -> datetime:
    dt = parse_utc(value)
    if dt is None:
        raise ValueError("%s must be an ISO-8601 UTC timestamp, got %r" % (label, value))
    return dt


def in_window(ts: Optional[datetime], since: datetime, until: datetime) -> bool:
    if ts is None:
        return False
    return since <= ts <= until


def bcb_fields(payload: str) -> dict[str, str]:
    """First-value map of BCB fields with lowercased keys."""
    segs = (payload or "").split("|")
    i = 1 if segs and segs[0] == "BCB" else 0
    out: dict[str, str] = {}
    while i < len(segs) and _FIELD.match(segs[i]):
        key, value = segs[i].split("=", 1)
        low = key.lower()
        if low not in out:
            out[low] = value.strip()
        i += 1
    return out


def classify(fields: dict[str, str]) -> str:
    """Return 'estimate', 'estimate_done', 'done', or 'other'."""
    phase = (fields.get("phase") or "").strip().lower()
    msg = (fields.get("msg_type") or "").strip().lower()
    if phase == "estimate_done" or msg == "estimate_done":
        return "estimate_done"
    if phase == "estimate" or msg == "estimate":
        return "estimate"
    if phase == "done" or msg == "done":
        return "done"
    return "other"


def refuse_url(source: str) -> None:
    if not source or not str(source).strip():
        raise ValueError("source is empty")
    parsed = urlparse(str(source).strip())
    if parsed.scheme in ("http", "https", "ftp", "file"):
        raise ValueError(
            "this checker only opens a local JSON or CSV path, not a URL (%s:)"
            % parsed.scheme
        )


def _cells_from_mapping(row: dict) -> list[object]:
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    row_id = lower.get("row_id") or lower.get("id") or ""
    ts = lower.get("timestamp") or lower.get("ts") or ""
    payload = lower.get("payload") or ""
    return [row_id, ts, "", "", "", payload]


def normalize_rows(rows: Iterable[object]) -> list[list[object]]:
    out: list[list[object]] = []
    for row in rows:
        if isinstance(row, list):
            out.append(row)
        elif isinstance(row, dict):
            out.append(_cells_from_mapping(row))
    return out


def load_json_rows(path: Path) -> list[list[object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("rows")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("JSON source must be a list of rows or an object with a rows list")
    return normalize_rows(rows)


def load_csv_rows(path: Path) -> list[list[object]]:
    with path.open(encoding="utf-8", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        raw = list(reader)
    if not raw:
        return []
    header = [c.strip().lower() for c in raw[0]]
    named = any(name in header for name in ("payload", "timestamp", "row_id", "ts"))
    if named:
        index = {name: i for i, name in enumerate(header)}

        def cell(row: Sequence[str], *names: str) -> str:
            for name in names:
                i = index.get(name)
                if i is not None and i < len(row):
                    return row[i]
            return ""

        out: list[list[object]] = []
        for row in raw[1:]:
            out.append([
                cell(row, "row_id", "id"),
                cell(row, "timestamp", "ts"),
                "",
                "",
                "",
                cell(row, "payload"),
            ])
        return out
    return [list(row) for row in raw]


def load_source(source: str) -> list[list[object]]:
    """Load a local JSON or CSV export. Refuses URLs so tests cannot hit a network."""
    refuse_url(source)
    path = Path(str(source).strip())
    if not path.is_file():
        raise FileNotFoundError("source is not a local file: %s" % path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv_rows(path)
    if suffix == ".json":
        return load_json_rows(path)
    # Unknown suffix: try JSON, then CSV.
    try:
        return load_json_rows(path)
    except (ValueError, json.JSONDecodeError):
        return load_csv_rows(path)


@dataclass(frozen=True)
class BoardRow:
    row_id: str
    ts: Optional[datetime]
    ts_raw: str
    payload: str
    fields: dict[str, str]
    kind: str

    @property
    def task(self) -> str:
        return (self.fields.get("task") or "").strip()

    @property
    def estimate_link(self) -> str:
        return (self.fields.get("estimate") or "").strip()


def parse_board_row(cells: Sequence[object]) -> Optional[BoardRow]:
    if not isinstance(cells, (list, tuple)) or not cells:
        return None
    row_id = str(cells[0] or "").strip()
    if not row_id or row_id == HEADER_ID_CELL:
        return None
    ts_raw = str(cells[1] or "").strip() if len(cells) > 1 else ""
    payload = str(cells[5] or "") if len(cells) > 5 else ""
    # Object-shaped loaders already placed payload in cell 5; a short row
    # that is just [id, ts, payload] is also accepted.
    if len(cells) == 3 and not payload:
        payload = str(cells[2] or "")
    fields = bcb_fields(payload)
    if "id" in fields and not row_id:
        row_id = fields["id"]
    kind = classify(fields)
    if kind == "other":
        return None
    return BoardRow(
        row_id=row_id or fields.get("id") or "",
        ts=parse_utc(ts_raw),
        ts_raw=ts_raw,
        payload=payload,
        fields=fields,
        kind=kind,
    )


def estimate_keys(row: BoardRow) -> set[str]:
    """Identifiers an ESTIMATE row can be matched under."""
    keys = set()
    if row.task:
        keys.add(row.task)
    if row.row_id:
        keys.add(row.row_id)
    return keys


def done_keys(row: BoardRow) -> set[str]:
    """Identifiers a DONE / ESTIMATE_DONE row uses to find its ESTIMATE."""
    keys = set()
    if row.task:
        keys.add(row.task)
    if row.estimate_link:
        keys.add(row.estimate_link)
    return keys


@dataclass
class Finding:
    kind: str
    task: str
    row_id: str
    ts_raw: str

    def line(self) -> str:
        task = self.task or "-"
        return "%s\ttask=%s\trow_id=%s\tts=%s" % (
            self.kind, task, self.row_id or "-", self.ts_raw or "-",
        )


def find_gaps(rows: Sequence[Sequence[object]], since: datetime,
              until: datetime) -> list[Finding]:
    parsed = [p for p in (parse_board_row(r) for r in rows) if p is not None]
    estimates = [r for r in parsed if r.kind == "estimate"]
    estimate_dones = [r for r in parsed if r.kind == "estimate_done"]
    dones = [r for r in parsed if r.kind in ("estimate_done", "done")]

    estimate_index: dict[str, list[BoardRow]] = {}
    for row in estimates:
        for key in estimate_keys(row):
            estimate_index.setdefault(key, []).append(row)

    done_index: dict[str, list[BoardRow]] = {}
    for row in estimate_dones:
        for key in done_keys(row):
            done_index.setdefault(key, []).append(row)
        # An ESTIMATE_DONE with only a row_id (no task/estimate) still
        # cannot close a different task; keys already cover task and link.

    findings: list[Finding] = []
    seen_open: set[tuple[str, str]] = set()
    for row in estimates:
        if not in_window(row.ts, since, until):
            continue
        keys = estimate_keys(row)
        closed = any(done_index.get(key) for key in keys)
        if closed:
            continue
        label = row.task or row.row_id
        stamp = (row.kind, label)
        if stamp in seen_open:
            continue
        seen_open.add(stamp)
        findings.append(Finding("OPEN_ESTIMATE", row.task, row.row_id, row.ts_raw))

    seen_orphan: set[tuple[str, str]] = set()
    for row in dones:
        if not in_window(row.ts, since, until):
            continue
        keys = done_keys(row)
        matched = any(estimate_index.get(key) for key in keys)
        if matched:
            continue
        label = row.task or row.estimate_link or row.row_id
        stamp = (row.kind, label)
        if stamp in seen_orphan:
            continue
        seen_orphan.add(stamp)
        findings.append(Finding("DONE_WITHOUT_ESTIMATE", row.task or row.estimate_link,
                                row.row_id, row.ts_raw))

    findings.sort(key=lambda f: (f.kind, f.task, f.ts_raw, f.row_id))
    return findings


def format_report(findings: Sequence[Finding]) -> str:
    lines = ["estimate compliance: %d gap(s)" % len(findings)]
    lines.extend(f.line() for f in findings)
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit 0 if clean, 1 if gaps, 2 if the check could not be made.",
    )
    parser.add_argument("--since", required=True,
                        help="inclusive UTC start of the window (ISO-8601)")
    parser.add_argument("--until", required=True,
                        help="inclusive UTC end of the window (ISO-8601)")
    parser.add_argument("--source", required=True,
                        help="local JSON or CSV board export (never a URL)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 2 if code not in (0, 1, 2) and code is not None else int(code or 0)
    try:
        since = require_utc("--since", args.since)
        until = require_utc("--until", args.until)
        if until < since:
            raise ValueError("--until must be at or after --since")
        rows = load_source(args.source)
        findings = find_gaps(rows, since, until)
    except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError, csv.Error) as exc:
        print("estimate compliance: error: %s" % exc, file=sys.stderr)
        return 2
    sys.stdout.write(format_report(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

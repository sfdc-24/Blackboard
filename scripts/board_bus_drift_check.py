#!/usr/bin/env python3
"""Read-only drift check: Google Sheet board rows vs the SQLite bus.

Compares Row_ID values inside a UTC time window and prints identifiers that
are missing on either side. It never writes: the SQLite file is opened
read-only, and the default sheet reader only loads a local JSON snapshot.
A caller may inject a sheet reader so tests never touch a network or a live
sheet.

    python3 scripts/board_bus_drift_check.py \
        --since 2026-01-01T00:00:00Z \
        --until 2026-01-01T01:00:00Z \
        --sheet-source tests/fixtures/synthetic_board_window.json \
        --sqlite-path /path/to/bus.db

Window rule (UTC): a row is in the window when its Timestamp cell is at or
after --since, and, if --until is given, at or before --until. Both ends
are inclusive. The Timestamp is cell 1 of the row on both sides (the same
board field), not SQLite appended_ts.

Exit codes: 0 in sync, 1 drift, 2 the comparison could not be made.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence
from urllib.parse import urlparse

ISO_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$"
)

DEFAULT_SHEET_TITLE = "Blackboard - Alpha DB"
HEADER_ID_CELL = "Row_ID"

SheetReader = Callable[[str], Sequence[Sequence[object]]]


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


def is_header_row(row: Sequence[object]) -> bool:
    if not row:
        return False
    return str(row[0]).strip() == HEADER_ID_CELL


def row_id_and_ts(row: Sequence[object]) -> tuple[Optional[str], Optional[datetime]]:
    if not isinstance(row, (list, tuple)) or len(row) < 2:
        return None, None
    row_id = str(row[0]).strip()
    if not row_id or row_id == HEADER_ID_CELL:
        return None, parse_utc(str(row[1]).strip()) if row[1] is not None else None
    return row_id, parse_utc(str(row[1]).strip()) if row[1] is not None else None


def in_window(ts: Optional[datetime], since: datetime, until: Optional[datetime]) -> bool:
    if ts is None:
        return False
    if ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def ids_in_window(rows: Iterable[Sequence[object]], since: datetime,
                  until: Optional[datetime]) -> set[str]:
    """Row_IDs whose Timestamp falls inside [since, until] (until may be open)."""
    found: set[str] = set()
    for row in rows:
        if is_header_row(row):
            continue
        row_id, ts = row_id_and_ts(row)
        if row_id and in_window(ts, since, until):
            found.add(row_id)
    return found


def load_sheet_json(source: str) -> list[list[object]]:
    """Load a local JSON snapshot. Refuses URLs so this path cannot hit a network."""
    if not source or not str(source).strip():
        raise ValueError("sheet source is empty")
    raw = str(source).strip()
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https", "ftp", "file"):
        raise ValueError(
            "default sheet reader only opens a local JSON path, not a URL (%s:)"
            % parsed.scheme
        )
    path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError("sheet source is not a local file: %s" % raw)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("rows")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("sheet source must be a JSON list of rows or an object with a rows list")
    out: list[list[object]] = []
    for row in rows:
        if isinstance(row, list):
            out.append(row)
    return out


def read_sqlite_rows(sqlite_path: str, sheet_title: str = DEFAULT_SHEET_TITLE) -> list[list[object]]:
    """SELECT the stored sheet cells for title. Opens the database read-only."""
    path = Path(sqlite_path)
    if not path.is_file():
        raise FileNotFoundError("sqlite path is not a file: %s" % sqlite_path)
    # mode=ro refuses a create-on-open and rejects writes on this connection.
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            "SELECT file_id, kind FROM files WHERE title = ?",
            (sheet_title,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(
                "sqlite bus has no file titled %r" % sheet_title
            )
        file_id, kind = row
        if kind != "sheet":
            raise ValueError("sqlite file %r is kind=%s, not a sheet" % (sheet_title, kind))
        cur = conn.execute(
            "SELECT row FROM sheet_rows WHERE file_id = ? ORDER BY n",
            (file_id,),
        )
        rows: list[list[object]] = []
        for (raw,) in cur:
            try:
                cells = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(cells, list):
                rows.append(cells)
        return rows
    finally:
        conn.close()


@dataclass
class DriftResult:
    since: str
    until: Optional[str]
    sheet_ids: set[str] = field(default_factory=set)
    sqlite_ids: set[str] = field(default_factory=set)
    missing_in_sheet: list[str] = field(default_factory=list)
    missing_in_sqlite: list[str] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return not self.missing_in_sheet and not self.missing_in_sqlite


def compare_window(sheet_rows: Iterable[Sequence[object]],
                   sqlite_rows: Iterable[Sequence[object]],
                   since: datetime,
                   until: Optional[datetime] = None,
                   since_label: Optional[str] = None,
                   until_label: Optional[str] = None) -> DriftResult:
    sheet_ids = ids_in_window(sheet_rows, since, until)
    sqlite_ids = ids_in_window(sqlite_rows, since, until)
    return DriftResult(
        since=since_label or (since.isoformat().replace("+00:00", "Z")),
        until=None if until is None else (until_label or until.isoformat().replace("+00:00", "Z")),
        sheet_ids=sheet_ids,
        sqlite_ids=sqlite_ids,
        missing_in_sheet=sorted(sqlite_ids - sheet_ids),
        missing_in_sqlite=sorted(sheet_ids - sqlite_ids),
    )


def format_result(result: DriftResult) -> str:
    lines = [
        "sync=%s" % ("yes" if result.in_sync else "no"),
        "window=%s .. %s" % (result.since, result.until or "open"),
        "sheet_ids=%d sqlite_ids=%d" % (len(result.sheet_ids), len(result.sqlite_ids)),
    ]
    if result.missing_in_sheet:
        lines.append("missing_in_sheet:")
        lines.extend("  %s" % row_id for row_id in result.missing_in_sheet)
    if result.missing_in_sqlite:
        lines.append("missing_in_sqlite:")
        lines.extend("  %s" % row_id for row_id in result.missing_in_sqlite)
    return "\n".join(lines)


def run_check(since: str,
              until: Optional[str],
              sheet_source: str,
              sqlite_path: str,
              sheet_title: str = DEFAULT_SHEET_TITLE,
              sheet_reader: Optional[SheetReader] = None) -> DriftResult:
    since_dt = require_utc("--since", since)
    until_dt = require_utc("--until", until) if until else None
    if until_dt is not None and until_dt < since_dt:
        raise ValueError("--until is before --since")
    reader = sheet_reader or load_sheet_json
    sheet_rows = reader(sheet_source)
    sqlite_rows = read_sqlite_rows(sqlite_path, sheet_title=sheet_title)
    return compare_window(sheet_rows, sqlite_rows, since_dt, until_dt, since_label=since, until_label=until)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True,
                        help="UTC instant; include rows at or after this Timestamp")
    parser.add_argument("--until", default=None,
                        help="UTC instant; include rows at or before this Timestamp")
    parser.add_argument("--sheet-source", required=True,
                        help="local JSON snapshot of sheet rows, or a handle for an injected reader")
    parser.add_argument("--sqlite-path", required=True,
                        help="path to the SQLite bus database (opened read-only)")
    parser.add_argument("--sheet-title", default=DEFAULT_SHEET_TITLE,
                        help="files.title inside the SQLite bus (default: %(default)s)")
    return parser


def main(argv: Optional[Sequence[str]] = None,
         sheet_reader: Optional[SheetReader] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        result = run_check(
            since=args.since,
            until=args.until,
            sheet_source=args.sheet_source,
            sqlite_path=args.sqlite_path,
            sheet_title=args.sheet_title,
            sheet_reader=sheet_reader,
        )
    except SystemExit as exc:
        # argparse uses SystemExit; keep its code (usually 2) for bad flags.
        return int(exc.code) if isinstance(exc.code, int) else 2
    except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError, sqlite3.Error) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print(format_result(result))
    return 0 if result.in_sync else 1


if __name__ == "__main__":
    sys.exit(main())

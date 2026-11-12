#!/usr/bin/env python3
"""Fixture-only tests for scripts/board_bus_drift_check.py.

No network and no live sheet: the sheet reader is injected or pointed at a
temporary JSON file, and SQLite buses are built in a temp directory from
synthetic rows only.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import board_bus_drift_check as drift  # noqa: E402

SHEET_TITLE = "Blackboard - Alpha DB"
HEADER = [
    "Row_ID",
    "Timestamp",
    "Source_Tag",
    "Target_Surface",
    "Action_Type",
    "Payload",
    "Category",
    "Project Tag",
    "Gist",
    "Sub-Gist",
]

SINCE = "2026-01-15T10:00:00Z"
UNTIL = "2026-01-15T12:00:00Z"


def cells(row_id: str, ts: str) -> list[str]:
    return [row_id, ts, "agent-a", "all", "APPEND", "synthetic-payload",
            "OPEN", "Blackboard", "synthetic-gist", ""]


def write_db(path: Path, rows: list[list[str]], title: str = SHEET_TITLE) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE files (
              file_id TEXT PRIMARY KEY,
              title TEXT UNIQUE NOT NULL,
              kind TEXT NOT NULL,
              created_ts TEXT NOT NULL,
              updated_ts TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1,
              body TEXT,
              header TEXT
            );
            CREATE TABLE sheet_rows (
              file_id TEXT NOT NULL,
              n INTEGER NOT NULL,
              row TEXT NOT NULL,
              appended_ts TEXT NOT NULL,
              PRIMARY KEY (file_id, n)
            );
            """
        )
        conn.execute(
            "INSERT INTO files (file_id, title, kind, created_ts, updated_ts, revision, header) "
            "VALUES (?, ?, 'sheet', ?, ?, 1, ?)",
            ("file-synthetic-1", title, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
             json.dumps(HEADER)),
        )
        for index, row in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO sheet_rows (file_id, n, row, appended_ts) VALUES (?, ?, ?, ?)",
                ("file-synthetic-1", index, json.dumps(row), row[1]),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_main(args, sheet_reader=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = drift.main(args, sheet_reader=sheet_reader)
    return code, stdout.getvalue(), stderr.getvalue()


class DriftCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "bus.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_in_sync_exits_zero(self):
        rows = [
            cells("SYN-IN-001", "2026-01-15T10:00:00Z"),
            cells("SYN-IN-002", "2026-01-15T11:00:00Z"),
            cells("SYN-IN-003", "2026-01-15T12:00:00Z"),
        ]
        write_db(self.db, rows)
        code, out, err = run_main(
            ["--since", SINCE, "--until", UNTIL,
             "--sheet-source", "injected", "--sqlite-path", str(self.db)],
            sheet_reader=lambda source: [HEADER] + rows,
        )
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("sync=yes", out)
        self.assertIn("sheet_ids=3 sqlite_ids=3", out)

    def test_missing_in_sheet_exits_one_and_prints_id(self):
        sheet = [cells("SYN-SH-001", "2026-01-15T10:30:00Z")]
        sqlite_rows = sheet + [cells("SYN-SH-MISSING", "2026-01-15T11:00:00Z")]
        write_db(self.db, sqlite_rows)
        code, out, _ = run_main(
            ["--since", SINCE, "--until", UNTIL,
             "--sheet-source", "injected", "--sqlite-path", str(self.db)],
            sheet_reader=lambda source: sheet,
        )
        self.assertEqual(code, 1)
        self.assertIn("sync=no", out)
        self.assertIn("missing_in_sheet:", out)
        self.assertIn("SYN-SH-MISSING", out)
        self.assertNotIn("missing_in_sqlite:", out)

    def test_missing_in_sqlite_exits_one_and_prints_id(self):
        sqlite_rows = [cells("SYN-SQL-001", "2026-01-15T10:30:00Z")]
        sheet = sqlite_rows + [cells("SYN-SQL-MISSING", "2026-01-15T11:15:00Z")]
        write_db(self.db, sqlite_rows)
        code, out, _ = run_main(
            ["--since", SINCE, "--until", UNTIL,
             "--sheet-source", "injected", "--sqlite-path", str(self.db)],
            sheet_reader=lambda source: sheet,
        )
        self.assertEqual(code, 1)
        self.assertIn("sync=no", out)
        self.assertIn("missing_in_sqlite:", out)
        self.assertIn("SYN-SQL-MISSING", out)
        self.assertNotIn("missing_in_sheet:", out)

    def test_row_at_since_is_included(self):
        row = cells("SYN-BND-SINCE", SINCE)
        ids = drift.ids_in_window(
            [row],
            drift.require_utc("since", SINCE),
            drift.require_utc("until", UNTIL),
        )
        self.assertEqual(ids, {"SYN-BND-SINCE"})

    def test_row_at_until_is_included(self):
        row = cells("SYN-BND-UNTIL", UNTIL)
        ids = drift.ids_in_window(
            [row],
            drift.require_utc("since", SINCE),
            drift.require_utc("until", UNTIL),
        )
        self.assertEqual(ids, {"SYN-BND-UNTIL"})

    def test_row_before_since_is_excluded(self):
        row = cells("SYN-BND-BEFORE", "2026-01-15T09:59:59.999999Z")
        ids = drift.ids_in_window(
            [row],
            drift.require_utc("since", SINCE),
            drift.require_utc("until", UNTIL),
        )
        self.assertEqual(ids, set())

    def test_row_after_until_is_excluded(self):
        row = cells("SYN-BND-AFTER", "2026-01-15T12:00:00.000001Z")
        ids = drift.ids_in_window(
            [row],
            drift.require_utc("since", SINCE),
            drift.require_utc("until", UNTIL),
        )
        self.assertEqual(ids, set())

    def test_out_of_window_mismatch_is_not_drift(self):
        # The sheet and sqlite disagree only outside the window.
        common = [cells("SYN-WIN-001", "2026-01-15T11:00:00Z")]
        sheet = common + [cells("SYN-OUT-SHEET", "2026-01-15T09:00:00Z")]
        sqlite_rows = common + [cells("SYN-OUT-SQL", "2026-01-15T13:00:00Z")]
        write_db(self.db, sqlite_rows)
        code, out, _ = run_main(
            ["--since", SINCE, "--until", UNTIL,
             "--sheet-source", "injected", "--sqlite-path", str(self.db)],
            sheet_reader=lambda source: sheet,
        )
        self.assertEqual(code, 0)
        self.assertIn("sync=yes", out)
        self.assertNotIn("SYN-OUT-SHEET", out)
        self.assertNotIn("SYN-OUT-SQL", out)

    def test_empty_window_is_in_sync(self):
        rows = [cells("SYN-EMPTY-OUT", "2026-01-14T00:00:00Z")]
        write_db(self.db, rows)
        code, out, _ = run_main(
            ["--since", SINCE, "--until", UNTIL,
             "--sheet-source", "injected", "--sqlite-path", str(self.db)],
            sheet_reader=lambda source: rows,
        )
        self.assertEqual(code, 0)
        self.assertIn("sheet_ids=0 sqlite_ids=0", out)

    def test_both_sides_can_report_missing_ids(self):
        sheet = [cells("SYN-BOTH-SHEET", "2026-01-15T10:10:00Z")]
        sqlite_rows = [cells("SYN-BOTH-SQL", "2026-01-15T10:20:00Z")]
        write_db(self.db, sqlite_rows)
        code, out, _ = run_main(
            ["--since", SINCE, "--until", UNTIL,
             "--sheet-source", "injected", "--sqlite-path", str(self.db)],
            sheet_reader=lambda source: sheet,
        )
        self.assertEqual(code, 1)
        self.assertIn("SYN-BOTH-SQL", out)
        self.assertIn("SYN-BOTH-SHEET", out)
        self.assertIn("missing_in_sheet:", out)
        self.assertIn("missing_in_sqlite:", out)

    def test_default_reader_loads_json_file_without_network(self):
        rows = [HEADER, cells("SYN-JSON-001", "2026-01-15T10:45:00Z")]
        snapshot = self.dir / "sheet.json"
        snapshot.write_text(json.dumps({"rows": rows}), encoding="utf-8")
        write_db(self.db, [rows[1]])
        with mock.patch("urllib.request.urlopen") as urlopen:
            code, out, err = run_main(
                ["--since", SINCE, "--until", UNTIL,
                 "--sheet-source", str(snapshot), "--sqlite-path", str(self.db)],
            )
        urlopen.assert_not_called()
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("sync=yes", out)

    def test_default_reader_refuses_http_source(self):
        write_db(self.db, [cells("SYN-HTTP-001", "2026-01-15T11:00:00Z")])
        with mock.patch("urllib.request.urlopen") as urlopen:
            code, _, err = run_main(
                ["--since", SINCE, "--until", UNTIL,
                 "--sheet-source", "https://example.invalid/board.json",
                 "--sqlite-path", str(self.db)],
            )
        urlopen.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("not a URL", err)

    def test_injected_reader_is_used_instead_of_opening_a_file(self):
        rows = [cells("SYN-INJ-001", "2026-01-15T11:30:00Z")]
        write_db(self.db, rows)
        seen = {}

        def reader(source):
            seen["source"] = source
            return rows

        with mock.patch.object(drift, "load_sheet_json", side_effect=AssertionError("default reader used")):
            code, out, _ = run_main(
                ["--since", SINCE, "--until", UNTIL,
                 "--sheet-source", "handle-not-a-path", "--sqlite-path", str(self.db)],
                sheet_reader=reader,
            )
        self.assertEqual(seen["source"], "handle-not-a-path")
        self.assertEqual(code, 0)
        self.assertIn("sync=yes", out)

    def test_sqlite_file_is_unchanged_after_a_check(self):
        rows = [cells("SYN-RO-001", "2026-01-15T11:00:00Z")]
        write_db(self.db, rows)
        before = file_digest(self.db)
        mtime = os.stat(self.db).st_mtime_ns
        code, _, _ = run_main(
            ["--since", SINCE, "--until", UNTIL,
             "--sheet-source", "injected", "--sqlite-path", str(self.db)],
            sheet_reader=lambda source: rows,
        )
        self.assertEqual(code, 0)
        self.assertEqual(file_digest(self.db), before)
        self.assertEqual(os.stat(self.db).st_mtime_ns, mtime)

    def test_missing_sqlite_file_exits_two(self):
        code, _, err = run_main(
            ["--since", SINCE, "--until", UNTIL,
             "--sheet-source", "injected", "--sqlite-path", str(self.dir / "nope.db")],
            sheet_reader=lambda source: [],
        )
        self.assertEqual(code, 2)
        self.assertIn("not a file", err)

    def test_read_only_connection_rejects_writes(self):
        write_db(self.db, [cells("SYN-RO-WRITE", "2026-01-15T11:00:00Z")])
        # The helper used by the tool must leave the database unwritable
        # on the connection it opens.
        rows = drift.read_sqlite_rows(str(self.db), SHEET_TITLE)
        self.assertEqual(rows[0][0], "SYN-RO-WRITE")
        uri = "file:%s?mode=ro" % self.db.resolve().as_posix()
        conn = sqlite3.connect(uri, uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO sheet_rows (file_id, n, row, appended_ts) "
                             "VALUES ('x', 99, '[]', '2026-01-15T11:00:00Z')")
        finally:
            conn.close()

    def test_until_may_be_omitted(self):
        rows = [cells("SYN-OPEN-001", "2026-01-15T18:00:00Z")]
        write_db(self.db, rows)
        code, out, _ = run_main(
            ["--since", SINCE, "--sheet-source", "injected", "--sqlite-path", str(self.db)],
            sheet_reader=lambda source: rows,
        )
        self.assertEqual(code, 0)
        self.assertIn(".. open", out)

    def test_offset_timestamp_is_compared_in_utc(self):
        # 10:00 UTC is 05:00-05:00; that instant is the lower bound.
        row = cells("SYN-OFF-001", "2026-01-15T05:00:00-05:00")
        ids = drift.ids_in_window(
            [row],
            drift.require_utc("since", SINCE),
            drift.require_utc("until", UNTIL),
        )
        self.assertEqual(ids, {"SYN-OFF-001"})

    def test_header_row_is_not_treated_as_an_id(self):
        ids = drift.ids_in_window(
            [HEADER, cells("SYN-HDR-001", "2026-01-15T11:00:00Z")],
            drift.require_utc("since", SINCE),
            None,
        )
        self.assertEqual(ids, {"SYN-HDR-001"})

    def test_parse_utc_rejects_unparseable_values(self):
        self.assertIsNone(drift.parse_utc("not-a-timestamp"))
        self.assertIsNone(drift.parse_utc("2026-01-15 10:00:00Z"))
        self.assertIsInstance(drift.parse_utc(SINCE), datetime)
        self.assertEqual(drift.parse_utc(SINCE).tzinfo, timezone.utc)


if __name__ == "__main__":
    os.chdir(ROOT)
    unittest.main(verbosity=2)

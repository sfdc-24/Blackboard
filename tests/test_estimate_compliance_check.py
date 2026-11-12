#!/usr/bin/env python3
"""Fixture-only tests for scripts/estimate_compliance_check.py.

No network. The checker is pointed at local JSON/CSV fixtures under
tests/fixtures/estimate_compliance/, or at temp files built in the test.
It never posts to a live board.

Run:
  python3 tests/test_estimate_compliance_check.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import estimate_compliance_check as ecc  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "estimate_compliance"
SINCE = "2026-11-12T00:00:00Z"
UNTIL = "2026-11-12T12:00:00Z"
WINDOW_SINCE = ecc.require_utc("since", SINCE)
WINDOW_UNTIL = ecc.require_utc("until", UNTIL)


def run_main(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ecc.main(list(args))
    return code, out.getvalue(), err.getvalue()


def write_json(rows) -> str:
    handle, path = tempfile.mkstemp(suffix=".json")
    Path(path).write_text(json.dumps(rows), encoding="utf-8")
    import os
    os.close(handle)
    return path


def sheet(row_id, ts, payload):
    return [row_id, ts, "grok", "claude-code-cli", "APPEND", payload, "OPEN"]


def bcb(row_id, phase, msg=" ", task="", extra=""):
    parts = [
        "BCB|v=1",
        "id=" + row_id,
        "phase=" + phase,
        "msg_type=" + msg.strip(),
        "from=grok",
        "to=claude-code-cli",
    ]
    if task:
        parts.append("task=" + task)
    return "|".join(parts) + "|" + extra


class LoaderAndWindowTests(unittest.TestCase):
    def test_clean_json_fixture_is_clean_in_full_day_window(self):
        # beta's ESTIMATE_DONE is at 23:00Z, outside a 00-12 window, but still
        # in the file, so it must close the 03:00Z estimate.
        code, out, err = run_main(
            "--since", SINCE, "--until", UNTIL,
            "--source", str(FIXTURES / "clean.json"),
        )
        self.assertEqual(code, 0)
        self.assertIn("0 gap(s)", out)
        self.assertEqual(err, "")

    def test_gaps_fixture_lists_open_estimate_and_orphan_dones(self):
        code, out, err = run_main(
            "--since", SINCE, "--until", UNTIL,
            "--source", str(FIXTURES / "gaps.json"),
        )
        self.assertEqual(code, 1)
        self.assertIn("OPEN_ESTIMATE\ttask=gamma\trow_id=T-EST-GAMMA-1", out)
        self.assertIn("DONE_WITHOUT_ESTIMATE\ttask=delta\trow_id=T-DONE-DELTA-1", out)
        self.assertIn("DONE_WITHOUT_ESTIMATE\ttask=epsilon\trow_id=T-DONE-EPSILON-1", out)
        self.assertNotIn("task=zeta", out)
        self.assertEqual(err, "")
        self.assertIn("3 gap(s)", out)

    def test_csv_reestimate_and_estimate_link_are_clean(self):
        code, out, err = run_main(
            "--since", SINCE, "--until", "2026-11-12T23:59:59Z",
            "--source", str(FIXTURES / "link_and_reestimate.csv"),
        )
        self.assertEqual(code, 0, out + err)
        self.assertIn("0 gap(s)", out)

    def test_inclusive_endpoints(self):
        rows = [
            sheet("T-EST-A", "2026-11-12T01:00:00Z",
                  bcb("T-EST-A", "ESTIMATE", "ESTIMATE", "edge")),
        ]
        findings = ecc.find_gaps(rows, WINDOW_SINCE, ecc.require_utc("u", "2026-11-12T01:00:00Z"))
        self.assertEqual(len(findings), 1)
        findings = ecc.find_gaps(rows, ecc.require_utc("s", "2026-11-12T01:00:00Z"), WINDOW_UNTIL)
        self.assertEqual(len(findings), 1)
        findings = ecc.find_gaps(rows, WINDOW_SINCE, ecc.require_utc("u", "2026-11-12T00:59:59Z"))
        self.assertEqual(findings, [])

    def test_estimate_outside_window_is_not_reported(self):
        rows = [
            sheet("T-EST-A", "2026-11-11T23:59:59Z",
                  bcb("T-EST-A", "ESTIMATE", "ESTIMATE", "outside")),
        ]
        self.assertEqual(ecc.find_gaps(rows, WINDOW_SINCE, WINDOW_UNTIL), [])

    def test_done_outside_window_is_not_reported(self):
        rows = [
            sheet("T-DONE-A", "2026-11-12T13:00:00Z",
                  bcb("T-DONE-A", "ESTIMATE_DONE", "ESTIMATE_DONE", "outside")),
        ]
        self.assertEqual(ecc.find_gaps(rows, WINDOW_SINCE, WINDOW_UNTIL), [])

    def test_counterpart_outside_window_still_closes(self):
        rows = [
            sheet("T-EST-A", "2026-11-12T01:00:00Z",
                  bcb("T-EST-A", "ESTIMATE", "ESTIMATE", "pair")),
            sheet("T-DONE-A", "2026-11-12T18:00:00Z",
                  bcb("T-DONE-A", "ESTIMATE_DONE", "ESTIMATE_DONE", "pair")),
        ]
        self.assertEqual(ecc.find_gaps(rows, WINDOW_SINCE, WINDOW_UNTIL), [])

    def test_object_shaped_json_rows(self):
        path = write_json({"rows": [{
            "row_id": "T-EST-OBJ-1",
            "timestamp": "2026-11-12T01:00:00Z",
            "payload": bcb("T-EST-OBJ-1", "ESTIMATE", "ESTIMATE", "object-task"),
        }]})
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        code, out, _ = run_main("--since", SINCE, "--until", UNTIL, "--source", path)
        self.assertEqual(code, 1)
        self.assertIn("task=object-task", out)


class MatchingTests(unittest.TestCase):
    def test_unrelated_tasks_do_not_close_each_other(self):
        rows = [
            sheet("T-EST-A", "2026-11-12T01:00:00Z",
                  bcb("T-EST-A", "ESTIMATE", "ESTIMATE", "one")),
            sheet("T-DONE-B", "2026-11-12T01:05:00Z",
                  bcb("T-DONE-B", "ESTIMATE_DONE", "ESTIMATE_DONE", "two")),
        ]
        kinds = {f.kind + ":" + f.task for f in ecc.find_gaps(rows, WINDOW_SINCE, WINDOW_UNTIL)}
        self.assertEqual(kinds, {"OPEN_ESTIMATE:one", "DONE_WITHOUT_ESTIMATE:two"})

    def test_generic_done_does_not_close_an_open_estimate(self):
        rows = [
            sheet("T-EST-A", "2026-11-12T01:00:00Z",
                  bcb("T-EST-A", "ESTIMATE", "ESTIMATE", "same")),
            sheet("T-DONE-A", "2026-11-12T01:05:00Z",
                  "BCB|v=1|id=T-DONE-A|phase=RESULT|msg_type=DONE|from=grok|to=claude-code-cli|task=same|"),
        ]
        findings = ecc.find_gaps(rows, WINDOW_SINCE, WINDOW_UNTIL)
        self.assertEqual([f.kind for f in findings], ["OPEN_ESTIMATE"])
        self.assertEqual(findings[0].task, "same")

    def test_task_match_is_case_sensitive(self):
        rows = [
            sheet("T-EST-A", "2026-11-12T01:00:00Z",
                  bcb("T-EST-A", "ESTIMATE", "ESTIMATE", "Case")),
            sheet("T-DONE-A", "2026-11-12T01:05:00Z",
                  bcb("T-DONE-A", "ESTIMATE_DONE", "ESTIMATE_DONE", "case")),
        ]
        kinds = {f.kind for f in ecc.find_gaps(rows, WINDOW_SINCE, WINDOW_UNTIL)}
        self.assertEqual(kinds, {"OPEN_ESTIMATE", "DONE_WITHOUT_ESTIMATE"})

    def test_estimate_link_to_row_id(self):
        rows = [
            sheet("T-EST-NO-TASK", "2026-11-12T01:00:00Z",
                  "BCB|v=1|id=T-EST-NO-TASK|phase=ESTIMATE|msg_type=ESTIMATE|from=grok|to=claude-code-cli|"),
            sheet("T-DONE-LINK", "2026-11-12T01:05:00Z",
                  "BCB|v=1|id=T-DONE-LINK|phase=ESTIMATE_DONE|msg_type=ESTIMATE_DONE|"
                  "from=grok|to=claude-code-cli|estimate=T-EST-NO-TASK|"),
        ]
        self.assertEqual(ecc.find_gaps(rows, WINDOW_SINCE, WINDOW_UNTIL), [])

    def test_phase_estimate_without_msg_type_counts(self):
        rows = [
            sheet("T-EST-A", "2026-11-12T01:00:00Z",
                  "BCB|v=1|id=T-EST-A|phase=ESTIMATE|from=grok|to=claude-code-cli|task=solo|"),
        ]
        findings = ecc.find_gaps(rows, WINDOW_SINCE, WINDOW_UNTIL)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "OPEN_ESTIMATE")

    def test_unparseable_timestamp_does_not_trigger_but_can_close(self):
        rows = [
            sheet("T-EST-A", "2026-11-12T01:00:00Z",
                  bcb("T-EST-A", "ESTIMATE", "ESTIMATE", "kept")),
            sheet("T-DONE-A", "not-a-timestamp",
                  bcb("T-DONE-A", "ESTIMATE_DONE", "ESTIMATE_DONE", "kept")),
        ]
        self.assertEqual(ecc.find_gaps(rows, WINDOW_SINCE, WINDOW_UNTIL), [])


class ErrorExitTests(unittest.TestCase):
    def test_bad_since_exits_2(self):
        code, _out, err = run_main(
            "--since", "yesterday", "--until", UNTIL,
            "--source", str(FIXTURES / "clean.json"),
        )
        self.assertEqual(code, 2)
        self.assertIn("error", err.lower())

    def test_until_before_since_exits_2(self):
        code, _out, err = run_main(
            "--since", UNTIL, "--until", SINCE,
            "--source", str(FIXTURES / "clean.json"),
        )
        self.assertEqual(code, 2)
        self.assertIn("--until", err)

    def test_missing_file_exits_2(self):
        code, _out, err = run_main(
            "--since", SINCE, "--until", UNTIL,
            "--source", str(FIXTURES / "no-such-file.json"),
        )
        self.assertEqual(code, 2)
        self.assertIn("not a local file", err)

    def test_invalid_json_exits_2(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        Path(path).write_text("{not json", encoding="utf-8")
        import os
        os.close(handle)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        code, _out, err = run_main("--since", SINCE, "--until", UNTIL, "--source", path)
        self.assertEqual(code, 2)
        self.assertIn("error", err.lower())

    def test_url_source_is_refused(self):
        code, _out, err = run_main(
            "--since", SINCE, "--until", UNTIL,
            "--source", "https://example.invalid/board.json",
        )
        self.assertEqual(code, 2)
        self.assertIn("not a URL", err)

    def test_missing_required_flag_exits_2(self):
        code, _out, err = run_main("--since", SINCE, "--source", str(FIXTURES / "clean.json"))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the Gemini ASK-phase guard in scripts/gemini_ask_guard.py.

The writer-side behaviour is exercised through scripts/append.py with a fake
poster: fetch is replaced, so a refusal is proved by "fetch was not called"
and an allow/override is proved by "fetch was called". No network. No secret.
Nothing is appended to a live board.

Run:
  python3 tests/test_gemini_ask_guard.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gemini_ask_guard  # noqa: E402


def load_append():
    spec = importlib.util.spec_from_file_location(
        "append_mod", str(SCRIPTS / "append.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def envelope(phase="RESULT", to="gemini", extra=""):
    """A well-formed BCB payload with known fake ids only."""
    body = (
        "BCB|v=1|id=T-GEMINI-ASK-1|phase=%s|from=claude-code-cli|to=%s|"
        % (phase, to)
    )
    return body + extra


class FakePoster:
    """Records append attempts and, if allowed, answers a matching read-back."""

    def __init__(self):
        self.fetches = []
        self.last_row = None

    def fetch(self, *args, **kwargs):
        data = args[1] if len(args) > 1 else kwargs.get("data")
        self.fetches.append(data)
        if isinstance(data, dict):
            self.last_row = data.get("sheetRow")
        return '{"ok":true}'

    def read_rows(self, env, match=None, **_kwargs):
        row = list(self.last_row or [])
        # Pad to 10 cells so append.py's content comparison has a shape.
        while len(row) < 10:
            row.append("")
        return {"rows": [row] if row and (match is None or row[0] == match) else []}


class RuleTests(unittest.TestCase):
    def test_to_gemini_is_addressed(self):
        self.assertTrue(gemini_ask_guard.addressed_to_gemini(envelope(to="gemini")))

    def test_to_gemini_is_case_insensitive(self):
        self.assertTrue(gemini_ask_guard.addressed_to_gemini(envelope(to="GEMINI")))

    def test_to_gemini_hyphen_instance_is_addressed(self):
        self.assertTrue(gemini_ask_guard.addressed_to_gemini(envelope(to="gemini-vscode")))
        self.assertTrue(gemini_ask_guard.addressed_to_gemini(envelope(to="gemini-rw-probe")))

    def test_gemini_among_several_addressees(self):
        self.assertTrue(gemini_ask_guard.addressed_to_gemini(
            envelope(to="codex,gemini")))
        self.assertTrue(gemini_ask_guard.addressed_to_gemini(
            envelope(to="codex gemini-rw-probe")))

    def test_non_gemini_addressee_is_not_addressed(self):
        self.assertFalse(gemini_ask_guard.addressed_to_gemini(envelope(to="codex")))
        self.assertFalse(gemini_ask_guard.addressed_to_gemini(envelope(to="not-gemini")))
        self.assertFalse(gemini_ask_guard.addressed_to_gemini(envelope(to="geminid")))

    def test_cc_gemini_is_ignored(self):
        payload = envelope(to="codex", extra="cc=gemini|text=Status?")
        self.assertFalse(gemini_ask_guard.addressed_to_gemini(payload))
        self.assertIsNone(gemini_ask_guard.refusal_message(payload))

    def test_expects_answer_truthy_values(self):
        for value in ("yes", "true", "1", "y", "on", "maybe"):
            payload = envelope(extra="expects_answer=%s|" % value)
            ok, clause = gemini_ask_guard.expects_answer(payload)
            self.assertTrue(ok, value)
            self.assertEqual(clause, "expects_answer")

    def test_expects_answer_falsey_values_do_not_count(self):
        for value in ("", "0", "n", "no", "false", "off"):
            # A bare or explicit-false flag, with no other marker.
            payload = envelope(extra="expects_answer=%s|" % value)
            ok, clause = gemini_ask_guard.expects_answer(payload)
            self.assertFalse(ok, value)
            self.assertEqual(clause, "")

    def test_answer_by_field_counts(self):
        ok, clause = gemini_ask_guard.expects_answer(
            envelope(extra="answer_by=2026-11-12T04:00:00Z|"))
        self.assertTrue(ok)
        self.assertEqual(clause, "answer_by")

    def test_answer_by_hyphen_field_counts(self):
        ok, clause = gemini_ask_guard.expects_answer(
            envelope(extra="answer-by=soon|"))
        self.assertTrue(ok)
        self.assertEqual(clause, "answer_by")

    def test_answer_by_phrase_in_prose_counts(self):
        ok, clause = gemini_ask_guard.expects_answer(
            envelope(extra="text=Please answer by 18:00Z.|"))
        self.assertTrue(ok)
        self.assertEqual(clause, "answer by")

    def test_answered_by_does_not_count(self):
        ok, clause = gemini_ask_guard.expects_answer(
            envelope(extra="text=This was answered by the chair.|"))
        self.assertFalse(ok)
        self.assertEqual(clause, "")

    def test_ask_or_question_field_counts(self):
        ok, clause = gemini_ask_guard.expects_answer(
            envelope(extra="ask=What is the current status?|"))
        self.assertTrue(ok)
        self.assertEqual(clause, "ask/question")
        ok, clause = gemini_ask_guard.expects_answer(
            envelope(extra="question=How many tests ran?|"))
        self.assertTrue(ok)
        self.assertEqual(clause, "ask/question")

    def test_question_mark_in_text_fields_and_free_tail(self):
        for extra in (
            "text=Can you look at this?|",
            "what=ready?|",
            "subject=status?|",
            "gist=ok?|",
            "Should we proceed?",
        ):
            ok, clause = gemini_ask_guard.expects_answer(envelope(extra=extra))
            self.assertTrue(ok, extra)
            self.assertEqual(clause, "question-mark")

    def test_question_mark_only_inside_url_does_not_count(self):
        payload = envelope(extra="text=See https://example.invalid/x?q=1 for the log.|")
        ok, clause = gemini_ask_guard.expects_answer(payload)
        self.assertFalse(ok)
        self.assertEqual(clause, "")

    def test_question_mark_beside_a_url_still_counts(self):
        payload = envelope(
            extra="text=Ready? See https://example.invalid/x?q=1 for the log.|")
        ok, clause = gemini_ask_guard.expects_answer(payload)
        self.assertTrue(ok)
        self.assertEqual(clause, "question-mark")

    def test_phase_ask_is_case_insensitive(self):
        self.assertTrue(gemini_ask_guard.phase_is_ask(envelope(phase="ASK")))
        self.assertTrue(gemini_ask_guard.phase_is_ask(envelope(phase="ask")))
        self.assertFalse(gemini_ask_guard.phase_is_ask(envelope(phase="RESULT")))
        self.assertFalse(gemini_ask_guard.phase_is_ask(envelope(phase="DISPATCH")))
        self.assertFalse(gemini_ask_guard.phase_is_ask(
            "BCB|v=1|id=T-GEMINI-ASK-1|from=claude-code-cli|to=gemini|text=Status?|"))

    def test_refusal_when_all_three_conditions_hold(self):
        msg = gemini_ask_guard.refusal_message(
            envelope(phase="RESULT", extra="expects_answer=yes|"))
        self.assertIsNotNone(msg)
        self.assertIn("GEMINI_ASK_REQUIRED", msg)
        self.assertIn("expects_answer", msg)
        self.assertIn("phase=RESULT", msg)

    def test_no_refusal_when_phase_is_ask(self):
        self.assertIsNone(gemini_ask_guard.refusal_message(
            envelope(phase="ASK", extra="expects_answer=yes|")))

    def test_no_refusal_when_not_addressed_to_gemini(self):
        self.assertIsNone(gemini_ask_guard.refusal_message(
            envelope(phase="RESULT", to="codex", extra="expects_answer=yes|")))

    def test_no_refusal_for_a_plain_status_note(self):
        self.assertIsNone(gemini_ask_guard.refusal_message(
            envelope(phase="RESULT", extra="text=Share the current health snapshot.|")))


class AppendWriterTests(unittest.TestCase):
    def setUp(self):
        self.ap = load_append()
        self.poster = FakePoster()
        self.ap.fetch = self.poster.fetch
        self.ap.read_rows = self.poster.read_rows
        self.ap.load_env = lambda *a, **k: {
            "BUS_URL": "https://example.invalid/",
            "BUS_SECRET": "unused-in-this-test",
        }

    def write_spec(self, payload, row_id="T-GEMINI-ASK-1"):
        spec = {
            "row_id": row_id,
            "source_tag": "claude-code-cli",
            "payload": payload,
        }
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        Path(path).write_text(json.dumps(spec), encoding="utf-8")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def run_append(self, path, extra_argv=None):
        argv = ["append.py", path] + list(extra_argv or [])
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(buf):
            self.ap.main()
        return buf.getvalue()

    def test_parse_argv_accepts_flag_before_or_after_the_file(self):
        path, allow = self.ap.parse_argv(["append.py", "row.json"])
        self.assertEqual(path, "row.json")
        self.assertFalse(allow)
        path, allow = self.ap.parse_argv(
            ["append.py", "--allow-gemini-non-ask", "row.json"])
        self.assertEqual(path, "row.json")
        self.assertTrue(allow)
        path, allow = self.ap.parse_argv(
            ["append.py", "row.json", "--allow-gemini-non-ask"])
        self.assertEqual(path, "row.json")
        self.assertTrue(allow)

    def test_refuses_and_posts_nothing(self):
        path = self.write_spec(envelope(phase="DISPATCH", extra="text=Can you confirm the count?|"))
        with self.assertRaises(SystemExit) as caught:
            self.run_append(path)
        msg = str(caught.exception)
        self.assertIn("GEMINI_ASK_REQUIRED", msg)
        self.assertIn("question-mark", msg)
        self.assertIn("phase=DISPATCH", msg)
        self.assertEqual(self.poster.fetches, [])

    def test_missing_phase_is_treated_as_not_ask(self):
        payload = (
            "BCB|v=1|id=T-GEMINI-ASK-1|from=claude-code-cli|to=gemini|"
            "expects_answer=yes|"
        )
        path = self.write_spec(payload)
        with self.assertRaises(SystemExit) as caught:
            self.run_append(path)
        self.assertIn("phase=(missing)", str(caught.exception))
        self.assertEqual(self.poster.fetches, [])

    def test_phase_ask_is_posted(self):
        path = self.write_spec(envelope(phase="ASK", extra="ask=What is the current status?|"))
        out = self.run_append(path)
        self.assertEqual(len(self.poster.fetches), 1)
        self.assertIn("READ-BACK OK", out)

    def test_non_gemini_question_is_posted(self):
        path = self.write_spec(
            envelope(phase="RESULT", to="codex", extra="text=Can you confirm the count?|"))
        out = self.run_append(path)
        self.assertEqual(len(self.poster.fetches), 1)
        self.assertIn("READ-BACK OK", out)

    def test_gemini_status_note_without_a_question_is_posted(self):
        path = self.write_spec(
            envelope(phase="RESULT", extra="text=Share the current health snapshot.|"))
        out = self.run_append(path)
        self.assertEqual(len(self.poster.fetches), 1)
        self.assertIn("READ-BACK OK", out)

    def test_override_flag_posts_and_prints_notice(self):
        path = self.write_spec(
            envelope(phase="RESULT", extra="expects_answer=yes|"))
        out = self.run_append(path, extra_argv=["--allow-gemini-non-ask"])
        self.assertEqual(len(self.poster.fetches), 1)
        self.assertIn("GEMINI_ASK_OVERRIDE", out)
        self.assertIn("READ-BACK OK", out)

    def test_existing_one_argument_invocation_still_works(self):
        """Older callers pass only the row file; that path must keep working."""
        path = self.write_spec(
            envelope(phase="RESULT", to="codex", extra="text=Health snapshot filed.|"))
        out = self.run_append(path)
        self.assertEqual(len(self.poster.fetches), 1)
        self.assertIn("READ-BACK OK", out)


if __name__ == "__main__":
    unittest.main()

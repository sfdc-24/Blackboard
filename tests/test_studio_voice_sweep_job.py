"""Offline contract tests for the scheduled Studio voice-cleanup job."""
from __future__ import annotations

import contextlib
import io
import sys
import traceback
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "cloud" / "studio-controller"
sys.path.insert(0, str(CONTROLLER))

from app import sweep_once  # noqa: E402


URL = "https://controller.example.run.app/v1/maintenance/voice-sweep"
SECRET = "m" * 48


class Response:
    def __init__(self, status=200, body=None, json_error=None):
        self.status_code = status
        self.body = {
            "ok": True,
            "checked_at": 1234,
            "due": 1,
            "attempted": 1,
            "completed": 1,
            "pending": 0,
            "index_available": True,
        } if body is None else body
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.body


class VoiceSweepJobTests(unittest.TestCase):
    def test_success_posts_exact_bearer_once_without_printing_it(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = sweep_once.run_once(url=URL, secret=SECRET, post=post)
        self.assertEqual(True, result["ok"])
        self.assertEqual(1, len(calls))
        self.assertEqual(URL, calls[0][0])
        self.assertEqual({"Authorization": "Bearer " + SECRET}, calls[0][1]["headers"])
        self.assertEqual(20.0, calls[0][1]["timeout"])
        self.assertIs(False, calls[0][1]["follow_redirects"])
        self.assertEqual("voice sweep accepted\n", output.getvalue())
        self.assertNotIn(SECRET, output.getvalue())

    def test_non_https_or_wrong_path_never_contacts_a_destination(self):
        calls = []
        for target in (
            "http://controller.example.run.app/v1/maintenance/voice-sweep",
            "https://controller.example.run.app/health",
            "https://controller.example.run.app/v1/maintenance/voice-sweep?secret=bad",
            "https://user:pass@controller.example.run.app/v1/maintenance/voice-sweep",
        ):
            with self.subTest(target=target), self.assertRaisesRegex(
                    RuntimeError, "exact HTTPS maintenance endpoint"):
                sweep_once.run_once(
                    url=target, secret=SECRET,
                    post=lambda *args, **kwargs: calls.append((args, kwargs)),
                )
        self.assertEqual([], calls)

    def test_short_secret_fails_before_transport(self):
        with self.assertRaisesRegex(RuntimeError, "at least 32 bytes"):
            sweep_once.run_once(url=URL, secret="short", post=lambda *a, **k: None)

    def test_refusal_reports_only_status_not_body_or_secret(self):
        marker = "provider-body-must-not-be-logged"
        for status in (307, 503):
            with self.subTest(status=status), self.assertRaisesRegex(
                    RuntimeError, "HTTP %d" % status) as caught:
                sweep_once.run_once(
                    url=URL, secret=SECRET,
                    post=lambda *a, code=status, **k: Response(code, {"detail": marker}),
                )
            self.assertNotIn(marker, str(caught.exception))
            self.assertNotIn(SECRET, str(caught.exception))

    def test_transport_error_is_bounded_and_redacted(self):
        request = httpx.Request("POST", URL)

        def post(*args, **kwargs):
            raise httpx.ReadTimeout("contains " + SECRET, request=request)

        with self.assertRaisesRegex(RuntimeError, "transport failed") as caught:
            sweep_once.run_once(url=URL, secret=SECRET, post=post)
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("contains", rendered)

    def test_invalid_success_body_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "invalid success body"):
            sweep_once.run_once(
                url=URL, secret=SECRET,
                post=lambda *a, **k: Response(json_error=ValueError("not json")),
            )
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            sweep_once.run_once(
                url=URL, secret=SECRET,
                post=lambda *a, **k: Response(body={"ok": False}),
            )
        for false_success in (
            {"ok": True},
            {"ok": True, "index_available": False, "pending": 0},
            {"ok": True, "index_available": True, "pending": 1},
        ):
            with self.subTest(false_success=false_success), self.assertRaisesRegex(
                    RuntimeError, "did not confirm"):
                sweep_once.run_once(
                    url=URL, secret=SECRET,
                    post=lambda *a, body=false_success, **k: Response(body=body),
                )


if __name__ == "__main__":
    unittest.main()

"""Offline acceptance tests for Apps Script HTTP version proof.

Set GAS_VERSION_ASSERT_PATH to the validator file to review, then run:
    python -B -m unittest discover -s work/ci-acceptance -p "test_*.py" -v

Every HTTP response is mocked. A network guard also blocks accidental traffic.
Optionally set GAS_DEPLOYMENT_AUDIT_PATH to include the audit wrapper regression.
These tests specify fail-closed acceptance behavior, so the historical validator
is expected to fail several cases. They do not modify the validator or any org.
"""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


class VersionProofAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured_path = os.environ.get("GAS_VERSION_ASSERT_PATH")
        if not configured_path:
            raise RuntimeError("Set GAS_VERSION_ASSERT_PATH to a validator file")
        cls.validator_path = Path(configured_path).resolve(strict=True)
        spec = importlib.util.spec_from_file_location(
            "gas_version_assert_under_test", cls.validator_path
        )
        cls.validator = importlib.util.module_from_spec(spec)
        # No network is allowed even during module import.
        with patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")), \
             patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            spec.loader.exec_module(cls.validator)

    def run_validator(self, body, *, url="https://example.invalid/exec", strict=False):
        argv = [
            str(self.validator_path),
            "--url", url,
            "--expect-version", "7",
            "--retries", "1",
            "--delay", "0",
        ]
        if strict:
            argv.append("--strict")
        output = io.StringIO()
        with patch.object(sys, "argv", argv), \
             patch.object(self.validator, "fetch", return_value=(200, body)) as fetch, \
             patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")), \
             patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
             patch("time.sleep", side_effect=AssertionError("Sleeping forbidden")), \
             contextlib.redirect_stdout(output), \
             contextlib.redirect_stderr(output):
            try:
                result = self.validator.main()
            except SystemExit as exc:
                result = exc.code
            except Exception as exc:
                self.fail(
                    "Invalid response must produce a controlled nonzero result, "
                    f"not {type(exc).__name__}: {exc}"
                )
        self.assertIsInstance(result, int, "Validator must return an integer exit code")
        return result, output.getvalue(), fetch.call_count

    def assert_rejected(self, body, **kwargs):
        result, output, _ = self.run_validator(body, **kwargs)
        self.assertNotEqual(
            result, 0,
            "Unproved deployment must fail closed; validator returned success. "
            f"Output: {output.strip()}",
        )

    def test_empty_url_is_rejected_even_with_strict(self):
        result, output, calls = self.run_validator("{}", url="", strict=True)
        self.assertEqual(calls, 0, "An empty URL must be rejected before fetching")
        self.assertNotEqual(result, 0, f"Missing URL silently passed: {output.strip()}")

    def test_google_sign_in_html_is_rejected_with_workflow_defaults(self):
        self.assert_rejected(
            '<!doctype html><html><head><title>Sign in - Google Accounts</title>'
            '</head><body class="AccountsSignInUi">'
            '<form action="https://accounts.google.com/v3/signin">Sign in</form>'
            '</body></html>'
        )

    def test_google_wrapper_html_is_rejected_with_workflow_defaults(self):
        self.assert_rejected(
            '<!doctype html><html><head><title>Google Drive</title></head>'
            '<body data-product-name="26981ed0d57bbad37e728ff58134270c">'
            'You need access</body></html>'
        )

    def test_json_error_without_version_is_rejected_with_workflow_defaults(self):
        self.assert_rejected('{"ok":false,"error":"not authorized"}')

    def test_json_nonobjects_are_rejected_without_crashing(self):
        for body in ('[]', '["version"]', '"unavailable"', 'null', 'false', '7'):
            with self.subTest(body=body):
                self.assert_rejected(body)

    def test_wrong_version_is_rejected(self):
        self.assert_rejected('{"version":6,"ok":true}')

    def test_expected_json_version_passes(self):
        result, output, calls = self.run_validator('{"version":7,"ok":true}')
        self.assertEqual(result, 0, output)
        self.assertEqual(calls, 1)

    def test_expected_json_version_passes_with_strict(self):
        result, output, calls = self.run_validator(
            '{"version":7,"ok":true}', strict=True
        )
        self.assertEqual(result, 0, output)
        self.assertEqual(calls, 1)


@unittest.skipUnless(
    os.environ.get("GAS_DEPLOYMENT_AUDIT_PATH"),
    "Optional audit test: set GAS_DEPLOYMENT_AUDIT_PATH",
)
class DeploymentAuditAcceptance(unittest.TestCase):
    def test_late_sign_in_marker_must_not_certify_app_answered(self):
        audit_path = Path(os.environ["GAS_DEPLOYMENT_AUDIT_PATH"]).resolve(strict=True)
        spec = importlib.util.spec_from_file_location("gas_audit_under_test", audit_path)
        audit = importlib.util.module_from_spec(spec)
        # Keep the actual probe and classifier coupled: truncating the probe's
        # body must not turn a known sign-in page into positive execution proof.
        fixture = (
            b"<!doctype html><html><head>" + b" " * 500 +
            b'</head><body class="AccountsSignInUi">Sign in</body></html>'
        )
        response = MagicMock()
        response.status = 200
        response.read.side_effect = lambda n=-1: fixture if n < 0 else fixture[:n]
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")), \
             patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
             patch("urllib.request.build_opener", return_value=opener):
            spec.loader.exec_module(audit)
            status, body = audit.probe_anonymous("https://example.invalid/exec")
            self.assertFalse(
                audit.app_answered(status, body),
                "A sign-in marker after byte 400 was missed and the Google page "
                "was incorrectly certified as an app response",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)


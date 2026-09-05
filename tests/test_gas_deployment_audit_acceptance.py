"""Offline coverage and response-integrity checks for the staging audit."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

HEALTH = json.dumps({"ok": True, "service": "sfdc24-glasses-uploader", "version": 2})
SCRIPT_ID = "stage-script"


def deployment(version=1, suffix="exec", access="ANYONE_ANONYMOUS"):
    return {"deploymentId": "stage-deployment", "deploymentConfig": {"versionNumber": version},
            "entryPoints": [{"entryPointType": "WEB_APP", "webApp": {
                "url": "https://example.invalid/" + suffix,
                "entryPointConfig": {"access": access, "executeAs": "USER_DEPLOYING"}}}]}


class AuditAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ["GAS_DEPLOYMENT_AUDIT_PATH"]).resolve(strict=True)
        spec = importlib.util.spec_from_file_location("deployment_audit_under_test", path)
        cls.audit = importlib.util.module_from_spec(spec)
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            spec.loader.exec_module(cls.audit)

    def run_audit(self, pages, *, response=HEALTH, status=200, estate=None):
        output = io.StringIO()
        estate = estate if estate is not None else {"glasses-intake-uploader": SCRIPT_ID}
        with patch.object(sys, "argv", ["audit"]), \
             patch.object(self.audit, "ESTATE", estate), \
             patch.object(self.audit, "token", return_value="mock-token"), \
             patch.object(self.audit, "api", side_effect=pages), \
             patch.object(self.audit, "probe_anonymous", return_value=(status, response)) as probe, \
             patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")), \
             contextlib.redirect_stdout(output):
            result = self.audit.main()
        return result, output.getvalue(), probe.call_count

    def test_known_service_and_complete_inventory_pass(self):
        result, _, calls = self.run_audit([{"deployments": [deployment()]}])
        self.assertEqual(result, 0)
        self.assertEqual(calls, 1)

    def test_empty_inventory_fails(self):
        self.assertNotEqual(self.run_audit([{"deployments": []}])[0], 0)

    def test_api_failure_fails_without_exposing_exception_text(self):
        result, output, _ = self.run_audit([RuntimeError("sensitive-response-marker")])
        self.assertNotEqual(result, 0)
        self.assertNotIn("sensitive-response-marker", output)

    def test_one_missing_project_fails_entire_estate(self):
        result, _, _ = self.run_audit([{"deployments": [deployment()]}, {"deployments": []}],
            estate={"glasses-intake-uploader": SCRIPT_ID, "second-project": "missing"})
        self.assertNotEqual(result, 0)

    def test_unrecognized_html_cannot_prove_health(self):
        self.assertNotEqual(self.run_audit([{"deployments": [deployment()]}],
            response="<html>Everything seems fine</html>")[0], 0)

    def test_wrong_service_cannot_prove_health(self):
        self.assertNotEqual(self.run_audit([{"deployments": [deployment()]}],
            response='{"ok":true,"service":"some-other-app"}')[0], 0)

    def test_matching_service_with_error_fails(self):
        for body in ('{"ok":false,"service":"sfdc24-glasses-uploader"}',
                     '{"ok":true,"service":"sfdc24-glasses-uploader","error":"blocked"}'):
            with self.subTest(body=body):
                self.assertNotEqual(self.run_audit([{"deployments": [deployment()]}], response=body)[0], 0)

    def test_projects_without_reviewed_health_contract_fail(self):
        self.assertNotEqual(self.run_audit([{"deployments": [deployment()]}],
            estate={"governor-page-api": SCRIPT_ID})[0], 0)

    def test_non_anonymous_configuration_fails_even_with_healthy_response(self):
        self.assertNotEqual(self.run_audit([{"deployments": [deployment(access="MYSELF")]}])[0], 0)

    def test_dev_endpoint_is_excluded_when_versioned_target_is_present(self):
        dev = deployment(suffix="dev")
        dev["deploymentConfig"] = {}
        result, _, calls = self.run_audit([{"deployments": [dev, deployment()]}])
        self.assertEqual(result, 0)
        self.assertEqual(calls, 1)

    def test_dev_only_inventory_does_not_prove_versioned_coverage(self):
        dev = deployment(suffix="dev")
        dev["deploymentConfig"] = {}
        result, _, calls = self.run_audit([{"deployments": [dev]}])
        self.assertNotEqual(result, 0)
        self.assertEqual(calls, 0)

    def test_pagination_does_not_hide_failed_targets(self):
        pages = [{"deployments": [deployment()], "nextPageToken": "next"},
                 {"deployments": [deployment(version=2, access="MYSELF")]}]
        result, _, calls = self.run_audit(pages)
        self.assertNotEqual(result, 0)
        self.assertEqual(calls, 2)

    def test_repeated_page_token_fails(self):
        page = {"deployments": [deployment()], "nextPageToken": "repeat"}
        self.assertNotEqual(self.run_audit([page, copy.deepcopy(page)])[0], 0)

    def test_error_on_later_page_invalidates_partial_inventory(self):
        pages = [{"deployments": [deployment()], "nextPageToken": "next"},
                 {"error": {"code": 403, "message": "denied"}}]
        result, _, calls = self.run_audit(pages)
        self.assertNotEqual(result, 0)
        self.assertEqual(calls, 0)

    def test_malformed_inventory_fails(self):
        for page in ({"deployments": {}}, {"deployments": [None]}):
            with self.subTest(page=page):
                self.assertNotEqual(self.run_audit([page])[0], 0)

    def test_full_body_is_read_beyond_historical_400_bytes(self):
        fixture = (" " * 500 + HEALTH).encode()
        response, opener = MagicMock(), MagicMock()
        response.status = 200
        response.read.side_effect = lambda n=-1: fixture if n < 0 else fixture[:n]
        opener.open.return_value.__enter__.return_value = response
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            status, body = self.audit.probe_anonymous("https://example.invalid/exec")
        self.assertTrue(self.audit.app_answered(status, body, "sfdc24-glasses-uploader"))

    def test_oversized_response_is_unverified(self):
        response, opener = MagicMock(), MagicMock()
        response.status = 200
        response.read.return_value = b" " * (self.audit.MAX_RESPONSE_BYTES + 1)
        opener.open.return_value.__enter__.return_value = response
        with patch("urllib.request.build_opener", return_value=opener), \
             patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            status, _ = self.audit.probe_anonymous("https://example.invalid/exec")
        self.assertIsNone(status)


if __name__ == "__main__":
    unittest.main(verbosity=2)


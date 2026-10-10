#!/usr/bin/env python3
"""The bus-requests keepalive is defined, default off, and does not hide a secret.

The worker's loop ends. A schedule that is on by default would start a Cloud Run
execution the moment this merged. The repository variable is the off switch, and
it is off when it is unset. Run: python3 tests/test_bus_requests_keepalive.py
"""
import datetime
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "bus-requests-keepalive.yml"
RUNBOOK = REPO / "docs" / "CLOUD-FLEET-RUNBOOK.md"
SERVE = REPO / "cloud" / "bus-reconciler" / "serve_requests.py"


class KeepaliveIsOffUntilTheOwnerSays(unittest.TestCase):
    def test_the_workflow_is_gated_and_refreshes_loop_until(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("vars.BUS_REQUESTS_KEEPALIVE == 'on'", text)
        self.assertIn("cron: '7 */3 * * *'", text)
        self.assertIn("date -u -d '+4 hours'", text)
        self.assertIn("LOOP_MAX_MINUTES=240", text)
        self.assertIn("--task-timeout=5h", text)
        self.assertIn("--project=sfdc24", text)
        self.assertIn("--region=us-central1", text)
        self.assertNotIn("2026-10-09T03:30:00Z", text)
        # No credential material. A workflow that embeds one is already applied.
        for needle in ("BEGIN PRIVATE KEY", "AIza", "password=", "REDIS_AUTH"):
            self.assertNotIn(needle, text)

    def test_the_runbook_names_the_command_and_the_rollback(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("bus-requests keepalive", text)
        self.assertIn("BUS_REQUESTS_KEEPALIVE", text)
        self.assertIn("gcloud run jobs execute bus-requests", text)
        self.assertIn("LOOP_UNTIL=${UNTIL}", text)
        self.assertIn("**Rollback:**", text)
        self.assertIn("not applied", text.lower())

    def test_a_past_loop_until_is_not_the_single_pass_path(self):
        """Read from serve_requests.py, not from a live execution.

        Unset LOOP_UNTIL is one pass. A timestamp that parses is the loop, and a
        timestamp in the past makes that loop run zero times. Re-executing the
        job without a new LOOP_UNTIL would not keep it alive.
        """
        sys.path.insert(0, str(REPO / "scripts"))
        sys.path.insert(0, str(REPO / "cloud" / "bus-reconciler"))
        import serve_requests

        self.assertIsNone(serve_requests._loop_until(""))
        past = serve_requests._loop_until("2026-10-09T03:30:00Z")
        self.assertIsNotNone(past)
        self.assertLess(past, datetime.datetime.now(datetime.timezone.utc))
        source = SERVE.read_text(encoding="utf-8")
        self.assertIn("while datetime.datetime.now(datetime.timezone.utc) < stop:", source)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)

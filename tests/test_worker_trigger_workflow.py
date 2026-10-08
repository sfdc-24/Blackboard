"""The Git half of PY-02 is OFF by default, keyed to cursor[bot]'s numeric id, and never runs a comment."""
import unittest
from pathlib import Path

import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "worker-trigger.yml"


class WorkerTriggerWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = WF.read_text(encoding="utf-8")
        self.wf = yaml.safe_load(self.text)
        self.job = self.wf["jobs"]["start-worker"]

    def test_off_unless_the_variable_is_exactly_on_and_the_identity_is_configured(self):
        cond = " ".join(self.job["if"].split())
        for clause in ("vars.WORKER_TRIGGER == 'on'", "vars.GCP_WIF_PROVIDER != ''", "vars.GCP_WORKER_SA != ''"):
            self.assertIn(clause, cond)

    def test_the_author_is_the_numeric_bot_id_never_a_login(self):
        cond = " ".join(self.job["if"].split())
        self.assertIn("github.event.comment.user.id == 206951365", cond)
        self.assertIn("github.event.comment.user.type == 'Bot'", cond)
        self.assertNotIn("user.login", cond)

    def test_it_never_checks_out_code_or_puts_the_comment_in_a_shell(self):
        uses = [s.get("uses", "") for s in self.job["steps"]]
        self.assertFalse(any(u.startswith("actions/checkout") for u in uses))
        for step in self.job["steps"]:
            self.assertNotIn("comment.body", step.get("run", ""))

    def test_it_only_starts_the_one_job(self):
        runs = [s["run"] for s in self.job["steps"] if "run" in s]
        self.assertEqual(["gcloud run jobs execute bus-requests --project=sfdc24 --region=us-central1 --async"],
                         [" ".join(r.split()) for r in runs])
        perms = self.wf["permissions"]
        self.assertEqual({"contents": "read", "id-token": "write"}, perms)


if __name__ == "__main__":
    unittest.main()

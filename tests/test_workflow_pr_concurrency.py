"""Pull-request concurrency cancels a stale run of the same workflow and PR only.

Push, schedule, dispatch, and deploy stay out of that group. The group key is
the workflow name, the event, and GitHub's pull request number, never a branch
name or other text a pull request author controls.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / ".github" / "workflows"
GROUP = (
    "${{ github.workflow }}-${{ github.event_name }}-"
    "${{ github.event.pull_request.number || github.run_id }}"
)
CANCEL = "${{ github.event_name == 'pull_request' }}"

# Workflows that run on pull_request and may cancel their own superseded PR run.
PR_WORKFLOWS = (
    "alpha-security.yml",
    "architecture-pdf.yml",
    "beat1-prototype-tests.yml",
    "browser-tools.yml",
    "ci-acceptance.yml",
    "mcp-plan-consistency.yml",
    "order-acceptance.yml",
    "order-cross-platform.yml",
    "order-linux-containment.yml",
    "order-release-candidate-preflight.yml",
    "python-suites.yml",
    "required-ci.yml",
    "site-p0-tests.yml",
    "walkthrough-tools.yml",
    "whatsapp-gateway-tests.yml",
    "zoom-agent-tests.yml",
)


class _WorkflowLoader(yaml.SafeLoader):
    """Keep GitHub's `on:` key. PyYAML 1.1 would otherwise read it as true."""


for _first, _resolvers in list(_WorkflowLoader.yaml_implicit_resolvers.items()):
    _WorkflowLoader.yaml_implicit_resolvers[_first] = [
        (tag, pattern) for tag, pattern in _resolvers if tag != "tag:yaml.org,2002:bool"
    ]
# true/false stay booleans. `on` must stay the trigger key, not True.
_WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load(name):
    return yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader=_WorkflowLoader)


class PullRequestConcurrency(unittest.TestCase):
    def test_each_pr_workflow_cancels_only_its_own_superseded_pr_run(self):
        for name in PR_WORKFLOWS:
            with self.subTest(name=name):
                wf = load(name)
                self.assertEqual(GROUP, wf["concurrency"]["group"])
                self.assertEqual(CANCEL, wf["concurrency"]["cancel-in-progress"])
                self.assertNotIn("github.head_ref", wf["concurrency"]["group"])
                self.assertNotIn("pull_request.title", wf["concurrency"]["group"])
                self.assertNotIn("pull_request.head", wf["concurrency"]["group"])
                self.assertIn("pull_request", wf["on"])

    def test_main_push_is_not_in_the_cancelling_group(self):
        for name in ("python-suites.yml", "mcp-plan-consistency.yml", "required-ci.yml"):
            wf = load(name)
            push = wf["on"]["push"]
            self.assertEqual(["main"], push.get("branches"))

    def test_order_linux_push_no_longer_duplicates_the_pr_run(self):
        wf = load("order-linux-containment.yml")
        self.assertEqual(["main"], wf["on"]["push"]["branches"])
        self.assertIn("pull_request", wf["on"])
        self.assertIn("paths", wf["on"]["pull_request"])

    def test_schedule_and_deploy_are_not_cancelled(self):
        live = load("live-health.yml")
        self.assertNotIn("concurrency", live)
        self.assertEqual(["7,37 * * * *"], [item["cron"] for item in live["on"]["schedule"]])
        self.assertNotIn("pull_request", live["on"])
        text = (ROOT / "live-health.yml").read_text(encoding="utf-8")
        self.assertIn("sleep 120", text)
        self.assertIn("pages_serves_main,site_paths,studio_controller", text)

        for name in ("org-data-refresh.yml", "worker-trigger.yml"):
            self.assertIs(False, load(name)["concurrency"]["cancel-in-progress"])
        for name in ("staging-deploy.yml", "staging-rollback.yml"):
            wf = load(name)
            self.assertNotIn("concurrency", wf)
            for job in wf["jobs"].values():
                self.assertIs(False, job["concurrency"]["cancel-in-progress"])

        for name in ("inbox-wake-sweep.yml", "inbox-wake-claude.yml", "inbox-wake-codex.yml"):
            self.assertNotIn("concurrency", load(name))

    def test_hosted_isolation_and_required_check_name_stay(self):
        suites = (ROOT / "python-suites.yml").read_text(encoding="utf-8")
        self.assertIn("sudo unshare -n", suites)
        self.assertIn("ip link set lo up", suites)
        self.assertIn("Prove the isolation actually isolates", suites)
        required = load("required-ci.yml")
        self.assertEqual("required-ci", required["jobs"]["required-ci"]["name"])
        self.assertIn("pull_request", required["on"])
        self.assertNotIn("paths", required["on"]["pull_request"] or {})


if __name__ == "__main__":
    unittest.main()

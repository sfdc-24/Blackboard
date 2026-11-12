#!/usr/bin/env python3
"""Offline tests for scripts/gas_deploy_guard.py (TASK ID: G15, Case 00001051).

A fake clasp runner and a fake HTTP getter are injected. Nothing here starts
a network call or a real clasp process. Deployment ids in these fixtures are
fake (AKfyc_FIXTURE_DEPLOY_001).
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import gas_deploy_guard as guard  # noqa: E402


SOURCE_DIR = "apps-script/sample-guard"
CODE = "function ping() { return 'ok'; }\n"
MANIFEST = '{"timeZone":"America/Toronto","exceptionLogging":"STACKDRIVER"}\n'
CLASP_JSON = '{"scriptId":"script-fixture-001","rootDir":"."}\n'
DEPLOY_ID = "AKfyc_FIXTURE_DEPLOY_001"


def run_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def init_repo(root: Path, body: str = CODE) -> Path:
    run_git(root, ["init", "-q"])
    run_git(root, ["symbolic-ref", "HEAD", "refs/heads/main"])
    run_git(root, ["config", "user.email", "guard-test@example.invalid"])
    run_git(root, ["config", "user.name", "Guard Test"])
    source = root / SOURCE_DIR
    source.mkdir(parents=True)
    (source / "Code.gs").write_text(body, encoding="utf-8", newline="\n")
    (source / "appsscript.json").write_text(MANIFEST, encoding="utf-8", newline="\n")
    (source / ".clasp.json").write_text(CLASP_JSON, encoding="utf-8", newline="\n")
    run_git(root, ["add", SOURCE_DIR])
    run_git(root, ["commit", "-qm", "fixture source"])
    return source


class FakeClasp:
    """In-memory clasp. Records calls and optionally writes a pull tree."""

    def __init__(self, version: int = 4, pull_files: dict[str, str] | None = None,
                 fail_on: dict[str, int] | None = None, list_output: str | None = None,
                 list_outputs: list[str] | None = None,
                 version_output: str | None = None, pull_empty: bool = False):
        self.version = version
        self.pull_files = pull_files
        self.fail_on = fail_on or {}
        self.list_output = list_output
        self.list_outputs = list(list_outputs) if list_outputs is not None else None
        self.version_output = version_output or ("Created version %d\n" % version)
        self.pull_empty = pull_empty
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd: Path) -> guard.ClaspResult:
        argv = list(args)
        self.calls.append(argv)
        command = argv[0] if argv else ""
        if command in self.fail_on:
            return guard.ClaspResult(self.fail_on[command], "", "fake clasp failed on %s" % command)
        if command == "push":
            return guard.ClaspResult(0, "Pushed 2 files.\n", "")
        if command == "create-version":
            return guard.ClaspResult(0, self.version_output, "")
        if command == "deploy":
            return guard.ClaspResult(0, "Deployed.\n", "")
        if command == "list-deployments":
            if self.list_outputs:
                text = self.list_outputs.pop(0)
            else:
                text = self.list_output
            if text is None:
                text = "- %s @%d\n" % (DEPLOY_ID, self.version)
            return guard.ClaspResult(0, text, "")
        if command == "pull":
            if not self.pull_empty:
                files = self.pull_files
                if files is None:
                    files = {
                        "Code.gs": (cwd.parent.parent / "unused").as_posix(),  # replaced below
                    }
                    # Default: copy from the real source sibling if present is handled by caller.
                for name, body in files.items():
                    dest = Path(cwd) / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(body, encoding="utf-8", newline="\n")
            return guard.ClaspResult(0, "Pulled files.\n", "")
        return guard.ClaspResult(99, "", "unexpected clasp command %s" % command)


def matching_fake(source: Path, version: int = 4, **kwargs) -> FakeClasp:
    files = {
        "Code.gs": (source / "Code.gs").read_text(encoding="utf-8"),
        "appsscript.json": (source / "appsscript.json").read_text(encoding="utf-8"),
    }
    return FakeClasp(version=version, pull_files=files, **kwargs)


HEALTH_URL = "https://script.example.invalid/macros/s/FIXTURE/exec?health=build"


def run_main(repo: Path, fake: FakeClasp, extra: list[str] | None = None,
             health_get=None, post_p0=None):
    argv = ["--repo", str(repo), "--source-dir", SOURCE_DIR, "--deployment-id", DEPLOY_ID]
    if extra:
        argv.extend(extra)
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = guard.main(argv, clasp_runner=fake, health_get=health_get, post_p0=post_p0)
    return code, stdout.getvalue(), stderr.getvalue()


class DeployGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gas-deploy-guard-test-")
        self.repo = Path(self.tmp.name)
        self.source = init_repo(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_matching_source_deploys_and_readback_passes(self):
        fake = matching_fake(self.source)
        code, out, err = run_main(self.repo, fake)
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("OK:", out)
        self.assertIn("deployed version 4", out)
        commands = [c[0] for c in fake.calls]
        self.assertEqual(
            commands,
            ["list-deployments", "push", "create-version", "deploy", "list-deployments", "pull"],
        )
        deploy = fake.calls[3]
        self.assertEqual(deploy[0], "deploy")
        self.assertIn("--versionNumber", deploy)
        self.assertIn("4", deploy)
        self.assertEqual(fake.calls[5], ["pull", "--versionNumber", "4"])

    def test_dirty_source_refuses_and_does_not_call_clasp(self):
        (self.source / "Code.gs").write_text("function ping() { return 'dirty'; }\n", encoding="utf-8")
        fake = matching_fake(self.source)
        code, out, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("REFUSED: dirty tree", err)
        self.assertIn("clean tree at the main SHA", err)
        self.assertEqual(fake.calls, [])

    def test_untracked_source_file_refuses(self):
        (self.source / "Extra.gs").write_text("function extra() {}", encoding="utf-8")
        fake = matching_fake(self.source)
        code, _, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED: dirty tree", err)
        self.assertEqual(fake.calls, [])

    def test_crlf_only_difference_is_allowed(self):
        lf = (self.source / "Code.gs").read_text(encoding="utf-8")
        (self.source / "Code.gs").write_bytes(lf.replace("\n", "\r\n").encode("utf-8"))
        fake = matching_fake(self.source)
        # matching_fake reads the CRLF working tree; pull files must hash equal after normalize.
        code, out, err = run_main(self.repo, fake)
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("OK:", out)

    def test_readback_content_mismatch_fails_closed(self):
        fake = matching_fake(self.source)
        fake.pull_files = {
            "Code.gs": "function ping() { return 'other'; }\n",
            "appsscript.json": MANIFEST,
        }
        code, out, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("deployed content does not match the approved source", err)
        self.assertIn("failing closed", err)
        self.assertIn("push", [c[0] for c in fake.calls])
        self.assertIn("pull", [c[0] for c in fake.calls])

    def test_readback_pull_failure_fails_closed(self):
        fake = matching_fake(self.source, fail_on={"pull": 1})
        code, _, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertIn("clasp pull of the deployed version failed", err)
        self.assertIn("deploy aborted", err)

    def test_empty_pull_fails_closed(self):
        fake = matching_fake(self.source, pull_empty=True)
        code, _, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertIn("returned no source files", err)
        self.assertIn("failing closed", err)

    def test_unreadable_version_fails_closed_before_deploy(self):
        fake = matching_fake(self.source, version_output="version created, number unknown\n")
        code, _, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertIn("could not read a version number", err)
        self.assertIn("failing closed", err)
        self.assertEqual([c[0] for c in fake.calls], ["list-deployments", "push", "create-version"])

    def test_list_deployments_wrong_version_fails_closed(self):
        fake = matching_fake(self.source, list_output="- %s @3\n" % DEPLOY_ID)
        code, _, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertIn("deployed version is 3, expected 4", err)
        self.assertIn("failing closed", err)
        self.assertNotIn("pull", [c[0] for c in fake.calls])

    def test_missing_deployment_in_listing_fails_closed(self):
        fake = matching_fake(self.source, list_output="- AKfyc_OTHER_FIXTURE_002 @4\n")
        code, _, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertIn("was not in clasp list-deployments", err)
        self.assertIn("failing closed", err)

    def test_push_failure_aborts_before_version_or_deploy(self):
        fake = matching_fake(self.source, fail_on={"push": 1})
        code, _, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertIn("clasp push failed", err)
        self.assertEqual([c[0] for c in fake.calls], ["list-deployments", "push"])

    def test_missing_repo_is_a_setup_error(self):
        missing = self.repo / "no-such-repo"
        fake = FakeClasp(pull_files={"Code.gs": CODE})
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = guard.main(
                ["--repo", str(missing), "--source-dir", SOURCE_DIR],
                clasp_runner=fake,
            )
        self.assertEqual(code, 2)
        self.assertIn("ERROR:", stderr.getvalue())
        self.assertEqual(fake.calls, [])

    def test_hash_is_order_independent_and_normalises_newlines(self):
        a = guard.source_hash({"b.json": "{\n}", "a.gs": "x\r\n"})
        b = guard.source_hash({"a.gs": "x\n", "b.json": "{\r\n}"})
        self.assertEqual(a, b)
        c = guard.source_hash({"a.gs": "y\n", "b.json": "{\n}"})
        self.assertNotEqual(a, c)

    def test_dirty_tree_outside_source_dir_refuses(self):
        (self.repo / "notes.txt").write_text("scratch notes\n", encoding="utf-8")
        fake = matching_fake(self.source)
        code, out, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("REFUSED: dirty tree", err)
        self.assertIn("clean tree at the main SHA", err)
        self.assertEqual(fake.calls, [])

    def test_head_not_at_main_sha_refuses(self):
        run_git(self.repo, ["checkout", "-qb", "other"])
        (self.source / "Code.gs").write_text("function ping() { return 'other'; }\n", encoding="utf-8")
        run_git(self.repo, ["add", SOURCE_DIR])
        run_git(self.repo, ["commit", "-qm", "other branch"])
        fake = matching_fake(self.source)
        code, out, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("REFUSED:", err)
        self.assertIn("is not the main SHA", err)
        self.assertEqual(fake.calls, [])

    def test_health_version_and_sha_pass(self):
        fake = matching_fake(self.source)
        main_sha = run_git(self.repo, ["rev-parse", "HEAD"]).strip()
        seen = []

        def health_get(url):
            seen.append(url)
            return {"ok": True, "version": 4, "sha": main_sha}

        posts = []
        code, out, err = run_main(
            self.repo,
            fake,
            extra=["--health-url", HEALTH_URL],
            health_get=health_get,
            post_p0=posts.append,
        )
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("OK:", out)
        self.assertIn("deployed version 4", out)
        self.assertIn("/health version and SHA match", out)
        self.assertEqual(seen, [HEALTH_URL])
        self.assertEqual(posts, [])
        deploys = [c for c in fake.calls if c and c[0] == "deploy"]
        self.assertEqual(len(deploys), 1)

    def test_health_mismatch_rolls_back_and_posts_p0(self):
        fake = matching_fake(
            self.source,
            list_outputs=[
                "- %s @3\n" % DEPLOY_ID,
                "- %s @4\n" % DEPLOY_ID,
            ],
        )
        main_sha = run_git(self.repo, ["rev-parse", "HEAD"]).strip()
        wrong_sha = "0" * 40
        seen = []

        def health_get(url):
            seen.append(url)
            return {"ok": True, "version": 99, "sha": wrong_sha}

        posts = []
        code, out, err = run_main(
            self.repo,
            fake,
            extra=["--health-url", HEALTH_URL],
            health_get=health_get,
            post_p0=posts.append,
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("GET /health mismatch", err)
        self.assertIn("rollback=ok", err)
        self.assertIn("failing closed", err)
        self.assertEqual(seen, [HEALTH_URL])
        self.assertEqual(len(posts), 1)
        p0 = posts[0]
        self.assertIn("P0", p0)
        self.assertIn("TASK ID: G15", p0)
        self.assertIn("rollback=ok", p0)
        self.assertIn("expected_version=4", p0)
        self.assertIn("expected_sha=%s" % main_sha, p0)
        self.assertIn("observed_version=99", p0)
        self.assertIn("observed_sha=%s" % wrong_sha, p0)
        self.assertNotIn(DEPLOY_ID, p0)
        deploys = [c for c in fake.calls if c and c[0] == "deploy"]
        self.assertEqual(len(deploys), 2)
        self.assertIn("--deploymentId", deploys[1])
        self.assertIn(DEPLOY_ID, deploys[1])
        self.assertIn("--versionNumber", deploys[1])
        self.assertIn("3", deploys[1])
        self.assertIn("auto-rollback", deploys[1])

    def test_default_clasp_runner_is_headless(self):
        recorded = {}

        def fake_which(name):
            if name == "clasp":
                return "/usr/bin/clasp-fixture"
            return None

        def fake_run(*args, **kwargs):
            recorded["args"] = args
            recorded["kwargs"] = kwargs

            class Result:
                returncode = 0
                stdout = "Pushed\n"
                stderr = ""

            return Result()

        with patch("shutil.which", fake_which), patch("subprocess.run", fake_run):
            result = guard.default_clasp_runner(["push"], self.source)
        self.assertEqual(result.returncode, 0)
        kwargs = recorded["kwargs"]
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["CI"], "true")
        self.assertEqual(kwargs["env"]["CLASP_HEADLESS"], "1")
        # The process environment is copied, then the headless flags are set;
        # stdin is closed so clasp cannot prompt.
        self.assertIn("PATH", kwargs["env"])
        argv = recorded["args"][0]
        self.assertEqual(argv[0], "/usr/bin/clasp-fixture")
        self.assertEqual(argv[1:], ["push"])


if __name__ == "__main__":
    unittest.main()

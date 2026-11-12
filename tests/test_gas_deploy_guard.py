#!/usr/bin/env python3
"""Offline tests for scripts/gas_deploy_guard.py.

A fake clasp runner is injected in every case. Nothing here starts a network
call or a real clasp process.
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

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
                 version_output: str | None = None, pull_empty: bool = False):
        self.version = version
        self.pull_files = pull_files
        self.fail_on = fail_on or {}
        self.list_output = list_output
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


def run_main(repo: Path, fake: FakeClasp, extra: list[str] | None = None):
    argv = ["--repo", str(repo), "--source-dir", SOURCE_DIR, "--deployment-id", DEPLOY_ID]
    if extra:
        argv.extend(extra)
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = guard.main(argv, clasp_runner=fake)
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
        self.assertEqual(commands, ["push", "create-version", "deploy", "list-deployments", "pull"])
        self.assertIn("--versionNumber", fake.calls[2])
        self.assertIn("4", fake.calls[2])
        self.assertEqual(fake.calls[4], ["pull", "--versionNumber", "4"])

    def test_dirty_source_refuses_and_does_not_call_clasp(self):
        (self.source / "Code.gs").write_text("function ping() { return 'dirty'; }\n", encoding="utf-8")
        fake = matching_fake(self.source)
        code, out, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("REFUSED: source about to be pushed differs from git HEAD tracked source", err)
        self.assertIn("head_sha256=", err)
        self.assertIn("push_sha256=", err)
        self.assertEqual(fake.calls, [])

    def test_untracked_source_file_refuses(self):
        (self.source / "Extra.gs").write_text("function extra() {}", encoding="utf-8")
        fake = matching_fake(self.source)
        code, _, err = run_main(self.repo, fake)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED:", err)
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
        self.assertEqual([c[0] for c in fake.calls], ["push", "create-version"])

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
        self.assertEqual([c[0] for c in fake.calls], ["push"])

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


if __name__ == "__main__":
    unittest.main()

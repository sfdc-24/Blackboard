"""Real Git repositories exercise publication, not local object existence."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_published_commit.py"
spec = importlib.util.spec_from_file_location("publication", SCRIPT)
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="publication-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source with spaces"
        self.remote = self.root / "remote.git"
        self.source.mkdir()
        self.remote.mkdir()
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.email", "test@example.invalid")
        self.git(self.source, "config", "user.name", "Publication test")
        self.git(self.remote, "init", "--bare", "-b", "main")
        self.first = self.commit("published")
        self.git(self.source, "remote", "add", "origin", str(self.remote))
        self.git(self.source, "push", "origin", "main")

    def git(self, where, *args):
        answer = subprocess.run(
            ["git", "-C", str(where), *args], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=publication.git_env(), check=False,
        )
        self.assertEqual(answer.returncode, 0, answer.stderr.decode(errors="replace"))
        return answer.stdout.decode().strip()

    def commit(self, text):
        (self.source / "artifact.txt").write_text(text, encoding="utf-8")
        self.git(self.source, "add", "artifact.txt")
        self.git(self.source, "commit", "-m", text)
        return self.git(self.source, "rev-parse", "HEAD")

    def check(self, commit=None, **kwargs):
        return publication.verify(self.source, commit or self.first, **kwargs)

    def snapshot(self):
        return {str(p.relative_to(self.source)): p.read_bytes()
                for p in self.source.rglob("*") if p.is_file()}

    def test_published_tip_and_ancestor_without_source_changes(self):
        second = self.commit("second")
        self.git(self.source, "push", "origin", "main")
        (self.source / "artifact.txt").write_text("uncommitted work", encoding="utf-8")
        before = self.snapshot()
        for commit in (self.first, second):
            receipt = self.check(commit)
            self.assertEqual(receipt["status"], "PUBLISHED")
            self.assertEqual(receipt["observed_remote_head"], second)
        self.assertEqual(self.snapshot(), before)

    def test_local_commit_is_not_publication(self):
        unpublished = self.commit("local only")
        self.assertEqual(self.git(self.source, "cat-file", "-t", unpublished), "commit")
        receipt = self.check(unpublished)
        self.assertEqual(receipt["status"], "LOCAL_ONLY")
        self.assertEqual(receipt["reason"], "NOT_IN_REMOTE_BRANCH")

    def test_existing_unreachable_local_object_is_not_publication(self):
        dangling = self.commit("dangling")
        self.git(self.source, "reset", "--hard", self.first)
        self.assertEqual(self.git(self.source, "cat-file", "-t", dangling), "commit")
        self.assertEqual(self.git(self.source, "branch", "--contains", dangling), "")
        self.assertEqual(self.check(dangling)["status"], "LOCAL_ONLY")

    def test_stale_tracking_ref_cannot_prove_deleted_remote_branch(self):
        self.git(self.remote, "update-ref", "-d", "refs/heads/main")
        self.assertEqual(self.git(self.source, "rev-parse", "origin/main"), self.first)
        receipt = self.check()
        self.assertEqual(receipt["status"], "LOCAL_ONLY")
        self.assertEqual(receipt["reason"], "REMOTE_BRANCH_ABSENT")

    def test_force_push_removes_previously_published_commit(self):
        self.git(self.source, "checkout", "--orphan", "replacement")
        replacement = self.commit("replacement root")
        self.git(self.source, "push", "--force", "origin", "HEAD:main")
        receipt = self.check()
        self.assertEqual(receipt["status"], "LOCAL_ONLY")
        self.assertEqual(receipt["observed_remote_head"], replacement)

    def test_unpushed_branch_and_unknown_commit(self):
        self.git(self.source, "branch", "unpublished")
        self.assertEqual(self.check(branch="unpublished")["reason"], "REMOTE_BRANCH_ABSENT")
        receipt = self.check("a" * 40)
        self.assertEqual(receipt["status"], "NOT_PUBLISHED")
        self.assertFalse(receipt["local_commit_present"])

    def test_remote_history_can_prove_commit_absent_from_local_checkout(self):
        # A second real writer publishes a commit this source has never fetched.
        writer = self.root / "other-writer"
        self.git(self.root, "clone", str(self.remote), str(writer))
        self.git(writer, "config", "user.email", "test@example.invalid")
        self.git(writer, "config", "user.name", "Second writer")
        (writer / "new.txt").write_text("new", encoding="utf-8")
        self.git(writer, "add", "new.txt")
        self.git(writer, "commit", "-m", "remote-only")
        commit = self.git(writer, "rev-parse", "HEAD")
        self.git(writer, "push", "origin", "main")
        receipt = self.check(commit)
        self.assertEqual(receipt["status"], "PUBLISHED")
        self.assertFalse(receipt["local_commit_present"])

    def test_missing_remote_is_unknown_not_published(self):
        self.git(self.source, "remote", "remove", "origin")
        receipt = self.check()
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["reason"], "REMOTE_NOT_CONFIGURED")

    def test_missing_repository_has_specific_diagnostic(self):
        receipt = publication.verify(self.root / "missing", self.first)
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["reason"], "REPOSITORY_UNAVAILABLE")

    def test_auth_environment_reaches_git_but_repository_overrides_do_not(self):
        auth = {"GIT_SSH": "/trusted/ssh", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
                "GIT_SSH_VARIANT": "ssh", "GIT_ASKPASS": "/trusted/askpass"}
        original = subprocess.run
        with patch.dict(os.environ, dict(auth, GIT_DIR="/wrong/repo",
                                         GIT_NO_LAZY_FETCH="0")):
            with patch.object(publication.subprocess, "run", wraps=original) as child:
                self.assertEqual(publication.run_git(self.source, ["--version"], 30).returncode, 0)
                actual = child.call_args.kwargs["env"]
        for key, value in auth.items():
            self.assertEqual(actual[key], value)
        self.assertNotIn("GIT_DIR", actual)
        self.assertEqual(actual["GIT_NO_LAZY_FETCH"], "1")

    def test_unsupported_lazy_fetch_guard_fails_before_object_probe(self):
        real = publication.run_git
        calls = []

        def unsupported(where, args, timeout):
            calls.append(args)
            if args == ["--version"]:
                return subprocess.CompletedProcess(args, 129, b"", b"")
            return real(where, args, timeout)

        with patch.object(publication, "run_git", side_effect=unsupported):
            receipt = self.check()
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["reason"], "GIT_NO_LAZY_FETCH_UNSUPPORTED")
        self.assertFalse(any(args[0] == "cat-file" for args in calls))

    def test_partial_clone_never_lazy_fetches_into_source(self):
        self.git(self.remote, "config", "uploadpack.allowFilter", "true")
        self.git(self.remote, "config", "uploadpack.allowAnySHA1InWant", "true")
        partial = self.root / "partial"
        self.git(self.root, "-c", "protocol.file.allow=always", "clone",
                 "--filter=blob:none", "--no-checkout", self.remote.as_uri(),
                 str(partial))
        self.assertEqual(self.git(partial, "config", "remote.origin.promisor"), "true")
        target = self.commit("new promised commit")
        self.git(self.source, "push", "origin", "main")

        def snapshot():
            return {str(p.relative_to(partial)): p.read_bytes()
                    for p in partial.rglob("*") if p.is_file()}

        before = snapshot()
        with patch.dict(os.environ, {"GIT_NO_LAZY_FETCH": "0"}):
            receipt = publication.verify(partial, target)
        self.assertEqual(receipt["status"], "PUBLISHED")
        self.assertFalse(receipt["local_commit_present"])
        self.assertEqual(snapshot(), before)
        # Positive reproduction of the old defect in this disposable clone:
        # plain cat-file really does download the missing object. The fixture
        # cannot pass merely because its promisor transport is nonfunctional.
        unsafe_env = publication.git_env()
        unsafe_env["GIT_NO_LAZY_FETCH"] = "0"
        unsafe = subprocess.run(["git", "-C", str(partial), "cat-file", "-t", target],
                                env=unsafe_env, capture_output=True, timeout=30)
        self.assertEqual(unsafe.returncode, 0)
        self.assertEqual(unsafe.stdout, b"commit\n")
        self.assertNotEqual(snapshot(), before)

    def test_transport_failure_receipt_never_leaks_remote_or_stderr(self):
        marker = "DO_NOT_PRINT_private_credential"
        missing = str(self.root / marker / "missing.git")
        self.git(self.source, "remote", "set-url", "origin", missing)
        run = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.source),
                              "--commit", self.first], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(json.loads(run.stdout)["reason"], "REMOTE_READ_FAILED")
        self.assertEqual(run.stderr, "")
        self.assertNotIn(marker, run.stdout)
        self.assertNotIn(str(self.root), run.stdout)

    def test_relative_remote_uses_source_directory(self):
        self.git(self.source, "remote", "set-url", "origin", "../remote.git")
        self.assertEqual(self.check()["status"], "PUBLISHED")

    def test_distinct_push_urls_do_not_make_fetch_url_ambiguous(self):
        self.git(self.source, "remote", "set-url", "--add", "--push", "origin",
                 "ssh://example.invalid/first.git")
        self.git(self.source, "remote", "set-url", "--add", "--push", "origin",
                 "ssh://example.invalid/second.git")
        self.assertEqual(self.check()["status"], "PUBLISHED")

    def test_inherited_git_directory_does_not_select_another_repository(self):
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "absent"),
                                     "GIT_WORK_TREE": str(self.root / "absent-tree")}):
            self.assertEqual(self.check()["status"], "PUBLISHED")

    def test_invalid_input_fails_closed(self):
        cases = [("short", {}), (self.first, {"branch": "main:other"}),
                 (self.first, {"branch": "main*"}), (self.first, {"remote": "--bad"}),
                 (self.first, {"timeout": -1})]
        for commit, kwargs in cases:
            with self.subTest(commit=commit, kwargs=kwargs):
                self.assertEqual(self.check(commit, **kwargs)["status"], "UNKNOWN")

    def test_remote_move_between_advertisement_and_fetch_is_unknown(self):
        next_commit = self.commit("next")
        self.git(self.source, "push", "origin", "HEAD:staged")
        real = publication.run_git
        moved = False

        def moving_git(where, args, timeout):
            nonlocal moved
            answer = real(where, args, timeout)
            if args[0] == "ls-remote" and not moved:
                moved = True
                self.git(self.remote, "update-ref", "refs/heads/main", next_commit)
            return answer

        with patch.object(publication, "run_git", side_effect=moving_git):
            receipt = self.check()
        self.assertTrue(moved)
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["reason"], "REMOTE_MOVED")

    def test_remote_move_after_ancestry_before_final_read_is_unknown(self):
        next_commit = self.commit("next")
        self.git(self.source, "push", "origin", "HEAD:staged")
        real = publication.run_git
        moved = False

        def moving_git(where, args, timeout):
            nonlocal moved
            answer = real(where, args, timeout)
            if args[0] == "merge-base" and not moved:
                moved = True
                self.git(self.remote, "update-ref", "refs/heads/main", next_commit)
            return answer

        with patch.object(publication, "run_git", side_effect=moving_git):
            receipt = self.check()
        self.assertTrue(moved)
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["reason"], "REMOTE_MOVED")

    def test_only_publication_returns_zero_from_real_cli(self):
        unpublished = self.commit("unpublished CLI")
        for commit, expected in ((self.first, 0), (unpublished, 1)):
            run = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.source),
                                  "--commit", commit], capture_output=True, text=True)
            self.assertEqual(run.returncode, expected)
            self.assertEqual(run.stderr, "")
            self.assertEqual(json.loads(run.stdout)["commit"], commit)


if __name__ == "__main__":
    unittest.main()

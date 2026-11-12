#!/usr/bin/env python3
"""Fixture-only tests for scripts/pr_ledger.py.

No network. The ledger is driven by a fake gh-api runner loaded from
JSON under tests/fixtures/pr_ledger/. subprocess and urllib are patched
to fail if anything tries to leave the process.

Run:
  python3 tests/test_pr_ledger.py
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pr_ledger as ledger  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pr_ledger"
WORLD_PATH = FIXTURES / "world.json"
SHAS = json.loads((FIXTURES / "shas.json").read_text(encoding="utf-8"))
SINCE = "2026-11-09"


class NetworkGuard(unittest.TestCase):
    """Any test in this module must not touch the network or gh."""

    def setUp(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("tests must not call subprocess or the network")

        self._sub_patch = mock.patch("pr_ledger.subprocess.run", side_effect=boom)
        self._sub_patch.start()
        self.addCleanup(self._sub_patch.stop)
        for name in (
            "urllib.request.urlopen",
            "urllib.request.Request",
            "socket.create_connection",
        ):
            try:
                patcher = mock.patch(name, side_effect=boom)
                patcher.start()
                self.addCleanup(patcher.stop)
            except AttributeError:
                pass


def load_world(path: Path = WORLD_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_endpoint(endpoint: str):
    ledger.assert_get_only(endpoint)
    if not endpoint.startswith("repos/"):
        raise ledger.LedgerError("unexpected fixture endpoint: %s" % endpoint)
    rest = endpoint[len("repos/"):]
    if "/pulls?" in rest:
        repo, query = rest.split("/pulls?", 1)
        params = {}
        for part in query.split("&"):
            if "=" in part:
                key, value = part.split("=", 1)
                params[key] = unquote(value)
        return repo, "pulls", params
    if "/git/matching-refs/heads/" in rest:
        repo, prefix = rest.split("/git/matching-refs/heads/", 1)
        return repo, "refs", unquote(prefix)
    if "/git/commits/" in rest:
        repo, sha = rest.split("/git/commits/", 1)
        return repo, "commit", unquote(sha)
    if "/commits/" in rest:
        repo, sha = rest.split("/commits/", 1)
        return repo, "commit", unquote(sha)
    if "/compare/" in rest:
        repo, spec = rest.split("/compare/", 1)
        base, head = spec.split("...", 1)
        return repo, "compare", (unquote(base), unquote(head))
    raise ledger.LedgerError("unexpected fixture endpoint: %s" % endpoint)


def runner_from_world(world: dict, missing_compares=None, record=None):
    missing_compares = set(missing_compares or [])
    calls = record if record is not None else []

    def run(endpoint: str) -> str:
        calls.append(endpoint)
        repo, kind, detail = parse_endpoint(endpoint)
        data = (world.get("repos") or {}).get(repo)
        if data is None:
            raise ledger.LedgerError("gh api GET %s failed: 404 not found" % endpoint)
        if kind == "pulls":
            state = detail.get("state")
            if state == "open":
                return json.dumps(data.get("open_pulls") or [])
            if state == "closed":
                head = detail.get("head") or ""
                closed = data.get("closed_pulls") or {}
                return json.dumps(closed.get(head) or [])
            raise ledger.LedgerError("unexpected pulls state in %s" % endpoint)
        if kind == "refs":
            refs = (data.get("refs") or {}).get(detail)
            if refs is None:
                return json.dumps([])
            return json.dumps(refs)
        if kind == "commit":
            commit = (data.get("commits") or {}).get(detail)
            if commit is None:
                raise ledger.LedgerError("gh api GET %s failed: 404 not found" % endpoint)
            return json.dumps(commit)
        if kind == "compare":
            key = "%s...%s" % (detail[0], detail[1])
            if key in missing_compares:
                raise ledger.LedgerError("gh api GET %s failed: 404 not found" % endpoint)
            compares = data.get("compares") or {}
            payload = compares.get(key)
            if payload is None:
                raise ledger.LedgerError("gh api GET %s failed: 404 not found" % endpoint)
            return json.dumps(payload)
        raise ledger.LedgerError("unexpected fixture endpoint: %s" % endpoint)

    return run


def run_main(*args: str, runner=None, world=None) -> tuple[int, str, str]:
    if runner is None:
        runner = runner_from_world(world if world is not None else load_world())
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ledger.main(list(args), runner=runner)
    return code, out.getvalue(), err.getvalue()


def lines_of(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


class ParserAndSafetyTests(NetworkGuard):
    def test_date_only_since_is_midnight_utc(self):
        dt = ledger.parse_since("2026-11-09")
        self.assertEqual(dt.isoformat(), "2026-11-09T00:00:00+00:00")

    def test_full_timestamp_since(self):
        dt = ledger.parse_since("2026-11-09T12:00:00Z")
        self.assertEqual(dt.hour, 12)

    def test_bad_since_raises(self):
        with self.assertRaises(ValueError):
            ledger.parse_since("next week")

    def test_short_sha(self):
        self.assertEqual(ledger.short_sha(SHAS["conf_orphan"]), "f666666")
        self.assertEqual(ledger.short_sha("abc"), "abc")

    def test_assert_get_only_refuses_post(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.assert_get_only("-X POST repos/sfdc-24/conference/pulls")
        with self.assertRaises(ledger.LedgerError):
            ledger.assert_get_only("repos/sfdc-24/conference/pulls --method PATCH")
        with self.assertRaises(ledger.LedgerError):
            ledger.assert_get_only("repos/sfdc-24/conference/issues --input -")
        with self.assertRaises(ledger.LedgerError):
            ledger.assert_get_only("graphql")

    def test_default_runner_refuses_post_without_subprocess(self):
        with self.assertRaises(ledger.LedgerError) as raised:
            ledger.default_runner("-X POST repos/sfdc-24/conference/pulls/1/comments")
        self.assertIn("mutat", str(raised.exception).lower())

    def test_default_runner_refuses_flags_without_subprocess(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.default_runner("-f title=boom repos/sfdc-24/conference/pulls")

    def test_paginate_only_list_endpoints(self):
        self.assertTrue(ledger.should_paginate("repos/sfdc-24/conference/pulls?state=open&per_page=100"))
        self.assertTrue(ledger.should_paginate("repos/sfdc-24/conference/git/matching-refs/heads/cursor/"))
        self.assertFalse(ledger.should_paginate("repos/sfdc-24/conference/git/commits/" + "a" * 40))
        self.assertFalse(ledger.should_paginate("repos/sfdc-24/conference/compare/main...abc"))
        self.assertFalse(ledger.should_paginate("repos/sfdc-24/conference/pulls/10"))

    def test_commit_endpoint_uses_git_commits(self):
        path = ledger.commit_endpoint("sfdc-24/conference", "abc123")
        self.assertIn("/git/commits/abc123", path)
        self.assertNotIn("/commits/abc123", path.replace("/git/commits/abc123", ""))

    def test_pulls_and_compare_endpoints_are_get_paths(self):
        path = ledger.pulls_endpoint("sfdc-24/conference", "closed", head="sfdc-24:grok/merged-pr")
        ledger.assert_get_only(path)
        self.assertIn("head=sfdc-24%3Agrok%2Fmerged-pr", path)
        compare = ledger.compare_endpoint("sfdc-24/conference", "main", "cursor/open-one")
        ledger.assert_get_only(compare)
        self.assertIn("main...cursor%2Fopen-one", compare)


class WorldLedgerTests(NetworkGuard):
    def test_default_fixture_world_classifies_every_case(self):
        code, out, err = run_main()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        lines = lines_of(out)
        # Orphans first.
        orphan_lines = [line for line in lines if "  orphan  " in line]
        rest = [line for line in lines if "  orphan  " not in line]
        self.assertEqual(orphan_lines, lines[: len(orphan_lines)])
        self.assertTrue(orphan_lines)
        joined = "\n".join(lines)
        self.assertIn("sfdc-24/conference  cursor/open-one (#10)  a111111  open-pr  open PR #10", joined)
        self.assertIn("sfdc-24/conference  some/other-branch (#11)  b222222  open-pr  open PR #11", joined)
        self.assertIn("sfdc-24/conference  grok/merged-pr (#12)  c333333  merged  PR #12 merged", joined)
        self.assertIn(
            "sfdc-24/conference  grok/on-main  d444444  merged  head reachable from main (ahead_by=0)",
            joined,
        )
        self.assertIn(
            "sfdc-24/conference  grok/superseded-by-open  e555555  superseded  "
            "all commits contained in open PR #10 branch cursor/open-one (ahead_by=0)",
            joined,
        )
        self.assertIn("sfdc-24/conference  grok/orphan-one  f666666  orphan  ", joined)
        self.assertIn("sfdc-24/conference  claude-code-cli/tooling  0222222  orphan  ", joined)
        self.assertIn("sfdc-24/conference  grok/on-since-date  0333333  orphan  ", joined)
        self.assertIn("sfdc-24/Blackboard  cursor/bb-open (#20)  aa10101  open-pr  open PR #20", joined)
        self.assertIn("sfdc-24/Blackboard  grok/bb-merged (#21)  bb20202  merged  PR #21 merged", joined)
        self.assertIn(
            "sfdc-24/Blackboard  grok/bb-contained  cc30303  superseded  "
            "all commits contained in open PR #20 branch cursor/bb-open (ahead_by=0)",
            joined,
        )
        self.assertIn("sfdc-24/Blackboard  grok/bb-orphan  dd40404  orphan  ", joined)
        self.assertNotIn("grok/old-branch", joined)
        self.assertNotIn("grok/before-since", joined)
        self.assertNotIn("cursor/bb-ancient", joined)
        # Open PR without a prefix is still listed.
        self.assertIn("some/other-branch", joined)
        self.assertEqual(len(orphan_lines), 4)
        self.assertEqual(len(lines), 12)

    def test_since_date_is_inclusive_and_excludes_the_second_before(self):
        code, out, err = run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("grok/on-since-date", out)
        self.assertNotIn("grok/before-since", out)

    def test_later_since_drops_on_since_date_but_keeps_open_prs(self):
        code, out, err = run_main("--since", "2026-11-11T00:00:00Z")
        self.assertEqual(code, 0, err)
        self.assertNotIn("grok/on-since-date", out)
        self.assertNotIn("grok/merged-pr", out)  # dated 11-10, no open PR
        # Open PRs stay even when their head is older than --since.
        self.assertIn("cursor/open-one (#10)", out)
        self.assertIn("some/other-branch (#11)", out)
        self.assertIn("cursor/bb-open (#20)", out)
        # tooling is 11-11 09:00, bb-orphan is 11-11 08:00 — both kept.
        self.assertIn("claude-code-cli/tooling", out)
        self.assertIn("grok/bb-orphan", out)

    def test_orphans_sort_before_other_classes_then_by_repo_and_branch(self):
        code, out, err = run_main()
        self.assertEqual(code, 0, err)
        orphans = [line for line in lines_of(out) if "  orphan  " in line]
        self.assertEqual(
            orphans,
            [
                line for line in orphans
            ],
        )
        branches = []
        for line in orphans:
            # repo  branch  sha  class  reason
            parts = line.split("  ")
            branches.append((parts[0], parts[1]))
        self.assertEqual(
            branches,
            sorted(branches, key=lambda item: (item[0], item[1])),
        )
        self.assertEqual(branches[0][0], "sfdc-24/Blackboard")
        self.assertEqual(branches[0][1], "grok/bb-orphan")
        self.assertTrue(all(item[0] <= branches[-1][0] for item in branches))
        # First non-orphan comes after the last orphan and is not an orphan.
        non = [line for line in lines_of(out) if "  orphan  " not in line]
        self.assertTrue(non)
        self.assertNotIn("  orphan  ", non[0])

    def test_json_option_includes_counts_and_classes(self):
        code, out, err = run_main("--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        body = json.loads(out)
        self.assertEqual(body["since"], "2026-11-09T00:00:00Z")
        self.assertEqual(body["base"], "main")
        self.assertEqual(body["repos"], ["sfdc-24/conference", "sfdc-24/Blackboard"])
        classes = {entry["branch"]: entry["class"] for entry in body["entries"]}
        self.assertEqual(classes["cursor/open-one"], "open-pr")
        self.assertEqual(classes["grok/merged-pr"], "merged")
        self.assertEqual(classes["grok/on-main"], "merged")
        self.assertEqual(classes["grok/superseded-by-open"], "superseded")
        self.assertEqual(classes["grok/orphan-one"], "orphan")
        self.assertEqual(body["entries"][0]["class"], "orphan")
        self.assertEqual(
            body["counts"]["sfdc-24/conference"],
            {"open-pr": 2, "merged": 2, "superseded": 1, "orphan": 3},
        )
        self.assertEqual(
            body["counts"]["sfdc-24/Blackboard"],
            {"open-pr": 1, "merged": 1, "superseded": 1, "orphan": 1},
        )
        self.assertEqual(
            body["counts"]["total"],
            {"open-pr": 3, "merged": 3, "superseded": 2, "orphan": 4},
        )
        # JSON entries also keep a PR number only for the branch's own PR.
        by_branch = {entry["branch"]: entry for entry in body["entries"]}
        self.assertEqual(by_branch["cursor/open-one"]["pr"], 10)
        self.assertIsNone(by_branch["grok/superseded-by-open"]["pr"])
        self.assertIsNone(by_branch["grok/on-main"]["pr"])

    def test_repos_filter_scans_only_that_repo(self):
        calls = []
        runner = runner_from_world(load_world(), record=calls)
        code, out, err = run_main("--repos", "sfdc-24/Blackboard", runner=runner)
        self.assertEqual(code, 0, err)
        self.assertIn("sfdc-24/Blackboard", out)
        self.assertNotIn("sfdc-24/conference", out)
        self.assertTrue(all("sfdc-24/conference" not in call for call in calls))
        self.assertTrue(any("sfdc-24/Blackboard" in call for call in calls))

    def test_empty_repo_is_success_with_no_lines(self):
        world = load_world(FIXTURES / "empty_conference.json")
        code, out, err = run_main("--repos", "sfdc-24/conference", world=world)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_empty_repo_json_has_zero_counts(self):
        world = load_world(FIXTURES / "empty_conference.json")
        code, out, err = run_main("--repos", "sfdc-24/conference", "--json", world=world)
        self.assertEqual(code, 0, err)
        body = json.loads(out)
        self.assertEqual(body["entries"], [])
        self.assertEqual(
            body["counts"]["sfdc-24/conference"],
            {"open-pr": 0, "merged": 0, "superseded": 0, "orphan": 0},
        )

    def test_compare_404_against_open_pr_does_not_count_as_contained(self):
        calls = []
        runner = runner_from_world(
            load_world(),
            missing_compares={
                "%s...%s" % (SHAS["conf_open"], SHAS["conf_super"]),
                "%s...%s" % (SHAS["conf_other"], SHAS["conf_super"]),
            },
            record=calls,
        )
        code, out, err = run_main("--repos", "sfdc-24/conference", runner=runner)
        self.assertEqual(code, 0, err)
        # Without a contained-in compare, and ahead of main, this becomes an orphan.
        self.assertIn("grok/superseded-by-open", out)
        self.assertIn("  orphan  ", [line for line in lines_of(out) if "grok/superseded-by-open" in line][0])

    def test_same_sha_as_open_pr_under_another_name_is_superseded(self):
        world = load_world()
        # Point a new grok/ alias at the open PR head.
        alias_sha = SHAS["conf_open"]
        repo = world["repos"]["sfdc-24/conference"]
        repo["refs"]["grok/"].append(
            {"ref": "refs/heads/grok/alias-of-open", "object": {"sha": alias_sha, "type": "commit"}}
        )
        # compare open-head...alias (same sha) would be identical / ahead_by=0
        repo["compares"]["%s...%s" % (alias_sha, alias_sha)] = {
            "status": "identical",
            "ahead_by": 0,
            "behind_by": 0,
            "total_commits": 0,
        }
        # Also vs the other open PR, not required if the first already matches.
        code, out, err = run_main("--repos", "sfdc-24/conference", world=world)
        self.assertEqual(code, 0, err)
        alias_line = [line for line in lines_of(out) if "grok/alias-of-open" in line][0]
        self.assertIn("  superseded  ", alias_line)
        self.assertIn("open PR #10", alias_line)

    def test_undated_prefix_branch_is_still_listed(self):
        world = load_world()
        sha = "0999999999999999999999999999999999999999"
        repo = world["repos"]["sfdc-24/conference"]
        repo["refs"]["grok/"].append(
            {"ref": "refs/heads/grok/undated", "object": {"sha": sha, "type": "commit"}}
        )
        repo["commits"][sha] = {"sha": sha, "commit": {"author": {}, "committer": {}}}
        repo["compares"]["main...%s" % sha] = {
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "total_commits": 1,
        }
        repo["compares"]["%s...%s" % (SHAS["conf_open"], sha)] = {
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "total_commits": 1,
        }
        repo["compares"]["%s...%s" % (SHAS["conf_other"], sha)] = {
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "total_commits": 1,
        }
        code, out, err = run_main("--repos", "sfdc-24/conference", world=world)
        self.assertEqual(code, 0, err)
        self.assertIn("grok/undated  0999999  orphan  ", out)

    def test_prefix_filter_ignores_unprefixed_branch_without_open_pr(self):
        # feature/no-prefix is not in the fixture refs and must not appear.
        code, out, err = run_main()
        self.assertEqual(code, 0, err)
        self.assertNotIn("feature/", out)

    def test_bad_since_exits_2(self):
        code, out, err = run_main("--since", "yesterday")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("ERROR:", err)

    def test_bad_repo_name_exits_2(self):
        code, out, err = run_main("--repos", "not-a-repo")
        self.assertEqual(code, 2)
        self.assertIn("ERROR:", err)

    def test_runner_error_exits_2(self):
        def boom(_endpoint: str) -> str:
            raise ledger.LedgerError("gh api GET failed: 500 upstream")

        code, out, err = run_main(runner=boom)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("ERROR:", err)
        self.assertIn("500", err)

    def test_invalid_json_from_runner_exits_2(self):
        def bad(_endpoint: str) -> str:
            return "not-json{"

        code, out, err = run_main("--repos", "sfdc-24/conference", runner=bad)
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)

    def test_runner_is_never_asked_to_mutate(self):
        calls = []
        runner = runner_from_world(load_world(), record=calls)
        code, out, err = run_main(runner=runner)
        self.assertEqual(code, 0, err)
        self.assertTrue(calls)
        for endpoint in calls:
            ledger.assert_get_only(endpoint)
            self.assertFalse(endpoint.startswith("-"))
            self.assertNotIn(" ", endpoint)

    def test_matching_refs_keep_trailing_slash_on_prefix(self):
        path = ledger.matching_refs_endpoint("sfdc-24/conference", "cursor/")
        self.assertTrue(path.endswith("heads/cursor/"))
        path = ledger.matching_refs_endpoint("sfdc-24/conference", "claude-code-cli/")
        self.assertTrue(path.endswith("heads/claude-code-cli/"))


if __name__ == "__main__":
    unittest.main()

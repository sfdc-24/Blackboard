#!/usr/bin/env python3
"""Offline suite for scripts/gate_card.py (U4: the same-SHA gate card).

Runs directly (`python3 tests/test_gate_card.py`), exits non-zero on any
failure, and makes no network call: the first thing this file does is
break socket.connect, so an accidental real fetch fails loudly. CI runs
it inside an empty network namespace as well.

The three recorded fixtures replay U4's acceptance list on real data
from sfdc-24/Blackboard (distilled on 2026-10-02; each fixture's _note
says exactly what is recorded and what is a stand-in):
  - pr310_b6fa11e.json  - refuses at b6fa11e, the Copilot BLOCKER that
    a merge command was once handed over across;
  - pr310_20261002.json - refuses on the current head while two high
    findings' threads are unresolved, and does not refuse on the
    resolved blocker from an earlier head;
  - pr315_ee4c0c1.json  - refuses only on the draft flag; with the
    draft flag cleared, every item is closed and the card prints.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import socket
import sys
import unittest
from pathlib import Path


def _no_network(*_a, **_k):  # installed before the module under test loads
    raise AssertionError("socket connect attempted in the offline suite")


socket.socket.connect = _no_network  # type: ignore[method-assign]
socket.create_connection = _no_network  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "gate_card.py"
FIXTURES = ROOT / "tests" / "fixtures" / "gate_card"

spec = importlib.util.spec_from_file_location("gate_card", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"could not load {MODULE_PATH}")
gate_card = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate_card)

SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
OTHER = "0123456789abcdef0123456789abcdef01234567"


def fixture(name: str) -> dict:
    with open(FIXTURES / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def green_inputs(**overrides) -> dict:
    """A synthetic gate with every item closed; tests open one at a time."""
    inputs = {
        "schema": gate_card.SCHEMA,
        "owner": "sfdc-24",
        "repo": "Blackboard",
        "number": 999,
        "named_sha": SHA,
        "gathered_at": "2026-10-02T00:00:00Z",
        "pr": {
            "state": "open",
            "draft": False,
            "merged": False,
            "head_sha": SHA,
            "mergeable_state": "clean",
            "html_url": "https://example.test/pr/999",
        },
        "pr_after_head_sha": SHA,
        "issue_comments": [
            {
                "id": 1,
                "author": "cursor[bot]",
                "body": f"**GO** on `{SHA}`. Whole diff reviewed.",
                "created_at": "2026-10-02T00:01:00Z",
                "html_url": "https://example.test/c/1",
            },
            {
                "id": 2,
                "author": "sfdc-24",
                "body": f"CODEX-PR999-TEST\n\nGO at exact head `{SHA}`.",
                "created_at": "2026-10-02T00:02:00Z",
                "html_url": "https://example.test/c/2",
            },
        ],
        "reviews": [
            {
                "id": 10,
                "author": "copilot-pull-request-reviewer[bot]",
                "state": "COMMENTED",
                "commit_id": SHA,
                "body": "## Copilot review overview\n\n### Approval recommended\n\n**Findings:** None",
                "submitted_at": "2026-10-02T00:03:00Z",
                "html_url": "https://example.test/r/10",
            }
        ],
        "threads": [],
        "check_runs": [
            {"name": "required-ci", "status": "completed", "conclusion": "success",
             "html_url": "https://example.test/ci/1"},
        ],
        "combined_status": {"state": "success"},
    }
    inputs.update(overrides)
    return inputs


def judged(inputs, require_codex=True):
    return gate_card.judge(inputs, require_codex=require_codex)


class RecordedAcceptance(unittest.TestCase):
    """U4's own acceptance list, on the recorded fixtures."""

    def test_b6fa11e_refuses_on_the_open_copilot_blocker(self):
        j = judged(fixture("pr310_b6fa11e.json"))
        self.assertFalse(j.passed)
        text = "\n".join(j.open)
        self.assertIn("discussion_r4157677050", text)
        self.assertIn("BLOCKER", text)
        # The blocker also sits in the review summary on that head.
        self.assertTrue(any("summary" in line for line in j.open), j.open)

    def test_todays_310_refuses_on_both_unresolved_high_findings(self):
        j = judged(fixture("pr310_20261002.json"))
        self.assertFalse(j.passed)
        text = "\n".join(j.open)
        self.assertIn("discussion_r4164225511", text)
        self.assertIn("discussion_r4162376205", text)
        # The resolved blocker from the earlier head b6fa11e must NOT refuse.
        self.assertNotIn("discussion_r4157677050", text)
        # The codex-connector P1 is reported but never gates.
        self.assertNotIn("discussion_r4164234088", text)
        self.assertIn("discussion_r4164234088", "\n".join(j.info))

    def test_315_refuses_only_on_the_draft_flag(self):
        j = judged(fixture("pr315_ee4c0c1.json"))
        self.assertFalse(j.passed)
        self.assertEqual(len(j.open), 1, j.open)
        self.assertIn("draft", j.open[0])

    def test_315_with_the_draft_cleared_closes_every_item_and_prints_the_card(self):
        inputs = fixture("pr315_ee4c0c1.json")
        inputs["pr"]["draft"] = False
        j = judged(inputs)
        self.assertTrue(j.passed, j.open)
        card = gate_card.render(inputs, j)
        self.assertIn("ee4c0c12d88b30feb0d241fb58266e5d1910a848", card)
        self.assertIn("issuecomment-5961353819", card)   # Cursor GO
        self.assertIn("issuecomment-5961720850", card)   # Codex GO / NO-MAJOR
        self.assertIn("pullrequestreview-5396810985", card)  # Copilot on the SHA
        self.assertIn("restarts the reviews", card)

    def test_315s_dispatch_comment_is_not_a_codex_verdict(self):
        # The real dispatch quotes "Reply GO or NO-GO" and cites a
        # CODEX-... id mid-body; only the marker-first comment counts.
        inputs = fixture("pr315_ee4c0c1.json")
        inputs["pr"]["draft"] = False
        inputs["issue_comments"] = [
            c for c in inputs["issue_comments"] if c["id"] != 5961720850
        ]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Codex" in line for line in j.open), j.open)


class CopilotGates(unittest.TestCase):
    def test_no_copilot_review_on_the_exact_sha_refuses(self):
        inputs = green_inputs()
        inputs["reviews"][0]["commit_id"] = OTHER
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("no Copilot review on this exact SHA" in l for l in j.open))

    def test_a_summary_only_blocker_refuses_even_with_every_thread_resolved(self):
        inputs = green_inputs()
        inputs["reviews"][0]["body"] = (
            "## Copilot review overview\n\n### Changes recommended\n\n"
            "VERDICT: BLOCKER - deadline enforcement can fail."
        )
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("summary" in l and "VERDICT: BLOCKER" in l for l in j.open))

    def test_only_the_latest_copilot_review_on_the_sha_judges_the_summary(self):
        inputs = green_inputs()
        inputs["reviews"].insert(0, {
            "id": 9, "author": "copilot-pull-request-reviewer[bot]",
            "state": "COMMENTED", "commit_id": SHA,
            "body": "VERDICT: BLOCKER - superseded by the later review.",
            "submitted_at": "2026-10-02T00:00:30Z",
            "html_url": "https://example.test/r/9",
        })
        self.assertTrue(judged(inputs).passed)

    def test_a_lowercase_parenthesised_blocker_in_a_thread_refuses(self):
        # Copilot has written "READ-NOT-DEMONSTRATED (blocker):" on #310.
        inputs = green_inputs()
        inputs["threads"] = [{
            "id": "T1", "is_resolved": False, "is_outdated": True, "truncated": False,
            "comments": [{
                "discussion_id": 1, "author": "copilot-pull-request-reviewer",
                "body": "READ-NOT-DEMONSTRATED (blocker): retries can duplicate deployments.",
                "created_at": "2026-10-02T00:00:00Z",
                "html_url": "https://example.test/d/1",
            }],
        }]
        j = judged(inputs)
        self.assertFalse(j.passed)

    def test_a_resolved_blocker_thread_does_not_refuse(self):
        inputs = green_inputs()
        inputs["threads"] = [{
            "id": "T1", "is_resolved": True, "is_outdated": True, "truncated": False,
            "comments": [{
                "discussion_id": 1, "author": "copilot-pull-request-reviewer",
                "body": "BLOCKER - fixed and resolved.",
                "created_at": "2026-10-02T00:00:00Z",
                "html_url": "https://example.test/d/1",
            }],
        }]
        self.assertTrue(judged(inputs).passed)

    def test_a_high_finding_from_an_earlier_head_carries_forward(self):
        # The earlier review names a High finding; its thread never says
        # "blocker" and the latest review on the named SHA is clean. The
        # unresolved thread must still refuse: a moved head drops nothing.
        inputs = green_inputs()
        inputs["reviews"].insert(0, {
            "id": 9, "author": "copilot-pull-request-reviewer[bot]",
            "state": "COMMENTED", "commit_id": OTHER,
            "body": '- <img alt="High severity"> [Old finding](#discussion_r77)',
            "submitted_at": "2026-10-01T00:00:00Z",
            "html_url": "https://example.test/r/9",
        })
        inputs["threads"] = [{
            "id": "T77", "is_resolved": False, "is_outdated": True, "truncated": False,
            "comments": [{
                "discussion_id": 77, "author": "copilot-pull-request-reviewer",
                "body": "The retry path can strand resources.",
                "created_at": "2026-10-01T00:00:00Z",
                "html_url": "https://example.test/pull/1#discussion_r77",
            }],
        }]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("discussion_r77" in l for l in j.open), j.open)
        # Resolving it closes the gate.
        inputs["threads"][0]["is_resolved"] = True
        self.assertTrue(judged(inputs).passed)

    def test_an_unresolved_low_copilot_thread_informs_but_does_not_gate(self):
        inputs = green_inputs()
        inputs["reviews"][0]["body"] += (
            '\n- <img alt="Low severity"> [Nit](#discussion_r88)'
        )
        inputs["threads"] = [{
            "id": "T88", "is_resolved": False, "is_outdated": False, "truncated": False,
            "comments": [{
                "discussion_id": 88, "author": "copilot-pull-request-reviewer",
                "body": "NIT - naming only.",
                "created_at": "2026-10-02T00:00:00Z",
                "html_url": "https://example.test/d/88",
            }],
        }]
        j = judged(inputs)
        self.assertTrue(j.passed, j.open)
        self.assertTrue(any("do not gate" in l for l in j.info), j.info)

    def test_a_thread_that_was_not_fully_read_refuses(self):
        inputs = green_inputs()
        inputs["threads"] = [{
            "id": "T1", "is_resolved": False, "is_outdated": False, "truncated": True,
            "comments": [{
                "discussion_id": 1, "author": "someone",
                "body": "first of many",
                "created_at": "2026-10-02T00:00:00Z",
                "html_url": "https://example.test/d/1",
            }],
        }]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("not fully read" in l for l in j.open))


class CursorGate(unittest.TestCase):
    def test_silence_is_not_a_go(self):
        inputs = green_inputs()
        inputs["issue_comments"] = [c for c in inputs["issue_comments"] if c["id"] != 1]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Cursor" in l and "silence" in l for l in j.open))

    def test_no_go_refuses_and_the_latest_verdict_wins(self):
        inputs = green_inputs()
        inputs["issue_comments"].append({
            "id": 3, "author": "cursor[bot]",
            "body": f"**NO-GO** on `{SHA}`. The caller check is wrong.",
            "created_at": "2026-10-02T00:05:00Z",
            "html_url": "https://example.test/c/3",
        })
        self.assertFalse(judged(inputs).passed)
        inputs["issue_comments"].append({
            "id": 4, "author": "cursor[bot]",
            "body": f"**GO** on `{SHA}`. Fixed.",
            "created_at": "2026-10-02T00:06:00Z",
            "html_url": "https://example.test/c/4",
        })
        self.assertTrue(judged(inputs).passed)

    def test_a_go_on_another_sha_is_no_go_here(self):
        inputs = green_inputs()
        inputs["issue_comments"][0]["body"] = f"**GO** on `{OTHER}`."
        self.assertFalse(judged(inputs).passed)

    def test_only_cursor_bot_can_speak_for_cursor(self):
        inputs = green_inputs()
        inputs["issue_comments"][0]["author"] = "sfdc-24"
        self.assertFalse(judged(inputs).passed)

    def test_a_goodlooking_word_is_not_a_verdict(self):
        inputs = green_inputs()
        inputs["issue_comments"][0]["body"] = f"GOOD progress on `{SHA}`."
        self.assertFalse(judged(inputs).passed)


class CodexGate(unittest.TestCase):
    def test_missing_verdict_refuses_by_default_and_optional_waives_it(self):
        inputs = green_inputs()
        inputs["issue_comments"] = [c for c in inputs["issue_comments"] if c["id"] != 2]
        self.assertFalse(judged(inputs).passed)
        self.assertTrue(judged(inputs, require_codex=False).passed)

    def test_a_present_no_go_refuses_even_when_optional(self):
        inputs = green_inputs()
        inputs["issue_comments"][1]["body"] = (
            f"CODEX-PR999-TEST\n\nNO-GO for functional source acceptance at `{SHA}`."
        )
        self.assertFalse(judged(inputs, require_codex=False).passed)

    def test_the_latest_marker_comment_supersedes(self):
        inputs = green_inputs()
        inputs["issue_comments"][1]["body"] = (
            f"CODEX-PR999-A\n\nNO-GO at `{SHA}`."
        )
        inputs["issue_comments"].append({
            "id": 5, "author": "sfdc-24",
            "body": f"<!-- CODEX-PR999-B -->\n\n## Codex: GO / NO-MAJOR\n\nExact reviewed head: `{SHA}`.",
            "created_at": "2026-10-02T00:07:00Z",
            "html_url": "https://example.test/c/5",
        })
        self.assertTrue(judged(inputs).passed)

    def test_go_or_no_go_quoted_in_a_dispatch_is_not_a_verdict(self):
        inputs = green_inputs()
        inputs["issue_comments"][1]["body"] = (
            f"@cursor Please check exact commit {SHA}. It answers Codex's "
            "CODEX-RISK-TEST-1. Reply GO or NO-GO for this exact commit."
        )
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Codex" in l and "silence" in l for l in j.open))


class HeadAndPrGates(unittest.TestCase):
    def test_a_named_sha_that_is_not_the_head_refuses(self):
        inputs = green_inputs(named_sha=OTHER)
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("moved head restarts the reviews" in l for l in j.open))

    def test_a_head_that_moved_during_the_reads_refuses(self):
        inputs = green_inputs(pr_after_head_sha=OTHER)
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("during the reads" in l for l in j.open))

    def test_a_short_sha_is_not_one_sha(self):
        inputs = green_inputs(named_sha=SHA[:12])
        with self.assertRaises(gate_card.GateError):
            judged(inputs)

    def test_closed_merged_and_draft_prs_refuse(self):
        for patch, needle in (
            ({"state": "closed"}, "not open"),
            ({"merged": True}, "already merged"),
            ({"draft": True}, "draft"),
        ):
            inputs = green_inputs()
            inputs["pr"].update(patch)
            j = judged(inputs)
            self.assertFalse(j.passed, patch)
            self.assertTrue(any(needle in l for l in j.open), (patch, j.open))


class CiGate(unittest.TestCase):
    def test_pending_failing_and_absent_checks_refuse(self):
        for runs, combined in (
            ([{"name": "x", "status": "in_progress", "conclusion": "", "html_url": ""}],
             {"state": "success"}),
            ([{"name": "x", "status": "completed", "conclusion": "failure", "html_url": ""}],
             {"state": "success"}),
            ([], {"state": "unknown"}),
            ([{"name": "x", "status": "completed", "conclusion": "success", "html_url": ""}],
             {"state": "failure"}),
        ):
            inputs = green_inputs(check_runs=runs, combined_status=combined)
            j = judged(inputs)
            self.assertFalse(j.passed, (runs, combined))

    def test_zero_runs_refuse_as_silence(self):
        inputs = green_inputs(check_runs=[], combined_status={"state": "unknown"})
        j = judged(inputs)
        self.assertTrue(any("silence is not a GO" in l and "CI" in l for l in j.open))


class RefusalCompleteness(unittest.TestCase):
    def test_every_open_item_is_listed_not_just_the_first(self):
        inputs = green_inputs()
        inputs["pr"]["draft"] = True
        inputs["check_runs"] = []
        inputs["combined_status"] = {"state": "unknown"}
        inputs["issue_comments"] = []
        inputs["reviews"] = []
        j = judged(inputs)
        self.assertGreaterEqual(len(j.open), 5, j.open)
        text = gate_card.render(inputs, j)
        for line in j.open:
            self.assertIn(line, text)


class GatherAndCli(unittest.TestCase):
    def _fake_rest(self, calls):
        comments_p1 = [
            {"id": i, "user": {"login": "cursor[bot]"},
             "body": "page-one filler", "created_at": "t", "html_url": f"u{i}"}
            for i in range(100)
        ]
        comments_p2 = [{
            "id": 200, "user": {"login": "cursor[bot]"},
            "body": f"**GO** on `{SHA}`.", "created_at": "t2", "html_url": "u200",
        }]

        def rest(path, params=None):
            calls.append((path, dict(params or {})))
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                return comments_p1 if params.get("page") == 1 else comments_p2 if params.get("page") == 2 else []
            if path.endswith("/pulls/999/reviews"):
                return [{"id": 1, "user": {"login": "copilot-pull-request-reviewer[bot]"},
                         "state": "COMMENTED", "commit_id": SHA, "body": "ok",
                         "submitted_at": "t", "html_url": "r1"}]
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "html_url": "ci1"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "success"}
            raise AssertionError(f"unexpected REST path {path}")

        return rest

    def _fake_graphql(self, pages):
        def graphql(query, variables):
            cursor = variables.get("cursor")
            page = pages[0] if cursor is None else pages[1]
            return {"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}
        return graphql

    def test_gather_reads_every_page_and_rereads_the_head(self):
        calls = []
        pages = [
            {"pageInfo": {"hasNextPage": True, "endCursor": "C1"},
             "nodes": [{"id": "T1", "isResolved": True, "isOutdated": False,
                        "comments": {"pageInfo": {"hasNextPage": False},
                                     "nodes": [{"databaseId": 1, "body": "b",
                                                "createdAt": "t", "url": "u",
                                                "author": {"login": "copilot-pull-request-reviewer"}}]}}]},
            {"pageInfo": {"hasNextPage": False, "endCursor": None},
             "nodes": [{"id": "T2", "isResolved": False, "isOutdated": False,
                        "comments": {"pageInfo": {"hasNextPage": True},
                                     "nodes": []}}]},
        ]
        inputs = gate_card.gather("o", "r", 999, None,
                                  self._fake_rest(calls), self._fake_graphql(pages))
        self.assertEqual(inputs["named_sha"], SHA)
        self.assertEqual(len(inputs["issue_comments"]), 101)
        self.assertEqual(inputs["issue_comments"][-1]["body"], f"**GO** on `{SHA}`.")
        self.assertEqual([t["id"] for t in inputs["threads"]], ["T1", "T2"])
        self.assertTrue(inputs["threads"][1]["truncated"])
        # The PR is read once before and once after everything else.
        pr_reads = [i for i, (p, _) in enumerate(calls) if p.endswith("/pulls/999")]
        self.assertEqual(pr_reads[0], 0)
        self.assertEqual(pr_reads[-1], len(calls) - 1)
        # The truncated thread fails closed when judged.
        j = gate_card.judge(inputs, require_codex=False)
        self.assertTrue(any("not fully read" in l for l in j.open), j.open)

    def test_graphql_without_a_token_is_an_error_not_a_pass(self):
        graphql = gate_card.make_graphql(None)
        with self.assertRaises(gate_card.GateError):
            graphql("query", {})

    def _run_cli(self, argv, env=None):
        old_env = dict(os.environ)
        os.environ.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = gate_card.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        return code, out.getvalue(), err.getvalue()

    def test_cli_judges_a_snapshot_offline_and_uses_the_exit_codes(self):
        code, out, _ = self._run_cli(
            ["--inputs", str(FIXTURES / "pr310_20261002.json")],
            env={"GITHUB_TOKEN": "tok-SENTINEL-never-printed"},
        )
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)
        self.assertIn("discussion_r4164225511", out)
        self.assertNotIn("tok-SENTINEL-never-printed", out)

        passing = fixture("pr315_ee4c0c1.json")
        passing["pr"]["draft"] = False
        tmp = FIXTURES / "_tmp_passing.json"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(passing, fh)
            code, out, _ = self._run_cli(
                ["--inputs", str(tmp)],
                env={"GITHUB_TOKEN": "tok-SENTINEL-never-printed"},
            )
        finally:
            tmp.unlink(missing_ok=True)
        self.assertEqual(code, 0)
        self.assertIn("## Gate card", out)
        self.assertNotIn("tok-SENTINEL-never-printed", out)

    def test_cli_without_pr_or_inputs_is_a_usage_error(self):
        code, _, err = self._run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("--pr or --inputs", err)

    def test_cli_wrong_schema_is_an_error(self):
        tmp = FIXTURES / "_tmp_bad.json"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"schema": "something-else"}, fh)
            code, _, err = self._run_cli(["--inputs", str(tmp)])
        finally:
            tmp.unlink(missing_ok=True)
        self.assertEqual(code, 2)
        self.assertIn("schema", err)


class PureHelpers(unittest.TestCase):
    def test_copilot_author_matches_with_and_without_the_bot_suffix(self):
        # REST reviews say "copilot-pull-request-reviewer[bot]"; GraphQL
        # thread authors say "copilot-pull-request-reviewer". Both are it.
        self.assertEqual(gate_card.norm_login("copilot-pull-request-reviewer[bot]"),
                         gate_card.COPILOT_LOGIN)
        self.assertEqual(gate_card.norm_login("copilot-pull-request-reviewer"),
                         gate_card.COPILOT_LOGIN)

    def test_blocker_word_boundaries(self):
        self.assertTrue(gate_card.blocker_lines("VERDICT: BLOCKER - x"))
        self.assertTrue(gate_card.blocker_lines("READ-NOT-DEMONSTRATED (blocker): x"))
        self.assertFalse(gate_card.blocker_lines("source-blocking finding"))
        self.assertFalse(gate_card.blocker_lines("unblockered"))

    def test_finding_anchor_extraction(self):
        body = ('- <img alt="High severity"> [A](#discussion_r11)\n'
                '- <img alt="Low severity"> [B](#discussion_r12)\n'
                '- <img alt="Critical severity"> [C](#discussion_r13)')
        anchors = gate_card.copilot_finding_anchors([{
            "author": "copilot-pull-request-reviewer[bot]", "body": body,
        }])
        self.assertEqual(anchors, {"11": "High", "13": "Critical"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

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
        "pr_after": {
            "state": "open",
            "draft": False,
            "merged": False,
            "head_sha": SHA,
            "mergeable_state": "clean",
            "html_url": "https://example.test/pr/999",
        },
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
        # One line per PR read, both the draft flag and nothing else.
        self.assertEqual(len(j.open), 2, j.open)
        self.assertTrue(all("draft" in line for line in j.open), j.open)

    def test_315_with_the_draft_cleared_closes_every_item_and_prints_the_card(self):
        inputs = fixture("pr315_ee4c0c1.json")
        inputs["pr"]["draft"] = False
        inputs["pr_after"]["draft"] = False
        j = judged(inputs)
        self.assertTrue(j.passed, j.open)
        card = gate_card.render(inputs, j)
        self.assertIn("ee4c0c12d88b30feb0d241fb58266e5d1910a848", card)
        self.assertIn("issuecomment-5961353819", card)   # Cursor GO
        self.assertIn("issuecomment-5961720850", card)   # Codex GO / NO-MAJOR
        self.assertIn("pullrequestreview-5396810985", card)  # Copilot on the SHA
        # U4: the card links the required CI run itself, not just a count.
        self.assertIn("required-ci https://github.com/sfdc-24/Blackboard/actions/runs/37063813304/job/111026549269", card)
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
        self.assertTrue(any("could not be read whole" in l for l in j.open))


class CopilotReviewRound1(unittest.TestCase):
    """Regressions for the four blockers Copilot filed on #316 itself
    (discussion_r4170636359, _r4170636390, _r4170636412, _r4170636428)."""

    def test_a_codex_marker_from_an_untrusted_author_is_not_a_verdict(self):
        # r4170636359: any commenter could type a marker-first GO body.
        inputs = green_inputs()
        inputs["issue_comments"][1]["author"] = "some-passerby"
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Codex" in l and "silence" in l for l in j.open), j.open)
        # A caller may name a different trusted relay set explicitly.
        self.assertTrue(gate_card.judge(inputs, codex_relays={"some-passerby"}).passed)

    def test_an_unrelated_green_check_cannot_stand_in_for_required_ci(self):
        # r4170636390: CI must include the repository's required run.
        inputs = green_inputs()
        inputs["check_runs"] = [{
            "name": "something-else", "status": "completed",
            "conclusion": "success", "html_url": "https://example.test/ci/9",
        }]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("required-ci" in l and "never ran" in l for l in j.open), j.open)

    def test_a_truncated_thread_refuses_even_when_marked_resolved(self):
        # r4170636412: truncation is not excused by the resolved flag.
        inputs = green_inputs()
        inputs["threads"] = [{
            "id": "T1", "is_resolved": True, "is_outdated": False, "truncated": True,
            "comments": [{
                "discussion_id": 1, "author": "copilot-pull-request-reviewer",
                "body": "first of many", "created_at": "t",
                "html_url": "https://example.test/d/1",
            }],
        }]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("could not be read whole" in l for l in j.open), j.open)
        self.assertFalse(any("all resolved" in l for l in j.closed), j.closed)

    def test_a_high_anchor_without_a_fetched_thread_refuses(self):
        # r4170636428: resolution that was never observed is not resolution.
        inputs = green_inputs()
        inputs["reviews"][0]["body"] += (
            '\n- <img alt="High severity"> [Unfetched](#discussion_r99)'
        )
        inputs["threads"] = []
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("discussion_r99" in l and "never observed" in l for l in j.open), j.open
        )
        # Fetching it resolved closes the gate again.
        inputs["threads"] = [{
            "id": "T99", "is_resolved": True, "is_outdated": False, "truncated": False,
            "comments": [{
                "discussion_id": 99, "author": "copilot-pull-request-reviewer",
                "body": "fixed", "created_at": "t",
                "html_url": "https://example.test/d/99",
            }],
        }]
        self.assertTrue(judged(inputs).passed)


class CursorReviewRound1(unittest.TestCase):
    """Regressions for Cursor's NO-GO on d5b6757 (comment 5962984331)."""

    def test_an_earlier_head_summary_blocker_carries_forward_by_its_anchors(self):
        # Cursor's repro: clear #315's draft flag and remove its
        # threads; the 76bd36c review still carries VERDICT: BLOCKER
        # and High finding discussion_r4168464602.
        inputs = fixture("pr315_ee4c0c1.json")
        inputs["pr"]["draft"] = False
        inputs["pr_after"]["draft"] = False
        inputs["threads"] = []
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("discussion_r4168464602" in l and "never observed" in l for l in j.open),
            j.open,
        )
        # EVERY anchor of a blocker-carrying review is tracked, not
        # only the high ones: the mediums of that review too.
        for did in ("4168464680", "4168464717"):
            self.assertTrue(any(f"discussion_r{did}" in l for l in j.open), (did, j.open))

    def test_an_earlier_summary_blocker_with_no_anchor_refuses_until_accepted(self):
        # Cursor flagged this twice. Nothing can demonstrate an
        # anchor-less summary blocker was addressed, so the tool
        # refuses; a person may record their own judgement, and the
        # card then says it was theirs.
        inputs = green_inputs()
        inputs["reviews"].insert(0, {
            "id": 9, "author": "copilot-pull-request-reviewer[bot]",
            "state": "COMMENTED", "commit_id": OTHER,
            "body": "## Copilot review overview\n\nVERDICT: BLOCKER - no inline findings.",
            "submitted_at": "2026-10-01T00:00:00Z",
            "html_url": "https://example.test/r/9",
        })
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("anchored no finding" in l and "example.test/r/9" in l for l in j.open),
            j.open,
        )
        accepted = gate_card.judge(
            inputs, accept_superseded={"https://example.test/r/9"}
        )
        self.assertTrue(accepted.passed, accepted.open)
        self.assertTrue(
            any("accepted as superseded by whoever ran this" in l
                for l in accepted.closed),
            accepted.closed,
        )
        # Accepting one review does not accept another.
        inputs["reviews"][0]["html_url"] = "https://example.test/r/other"
        self.assertFalse(
            gate_card.judge(inputs, accept_superseded={"https://example.test/r/9"}).passed
        )

    def test_a_codex_marker_id_containing_go_does_not_outvote_a_no_go_body(self):
        inputs = green_inputs()
        inputs["issue_comments"][1]["body"] = (
            f"CODEX-PR999-GO-CHECK-20261002\n\nNO-GO at exact head `{SHA}`."
        )
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Codex" in l and "NO-GO" in l for l in j.open), j.open)

    def test_a_quoted_go_or_no_go_before_a_codex_no_go_stays_no_go(self):
        inputs = green_inputs()
        inputs["issue_comments"][1]["body"] = (
            f"CODEX-PR999-TEST\n\nYou asked: Reply GO or NO-GO for `{SHA}`.\n\n"
            "## Codex: NO-GO\n\nThe retry path is unsound."
        )
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Codex" in l and "NO-GO" in l for l in j.open), j.open)

    def test_a_cursor_go_for_another_sha_in_its_verdict_line_is_not_this_go(self):
        inputs = green_inputs()
        inputs["issue_comments"][0]["body"] = (
            f"**GO** on `{OTHER}`. Against main the tree also matches {SHA}."
        )
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Cursor" in l and "silence" in l for l in j.open), j.open)

    def test_a_cursor_comment_starting_go_or_no_go_is_not_a_go(self):
        inputs = green_inputs()
        inputs["issue_comments"][0]["body"] = (
            f"GO or NO-GO for `{SHA}`? I am still reading."
        )
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Cursor" in l and "silence" in l for l in j.open), j.open)

    def test_the_real_cursor_and_codex_verdict_shapes_still_count(self):
        # The live bodies of #315 at ee4c0c1 must keep passing: the
        # strictness above must not break the shapes Cursor and the
        # Codex relay actually write.
        inputs = fixture("pr315_ee4c0c1.json")
        inputs["pr"]["draft"] = False
        inputs["pr_after"]["draft"] = False
        j = judged(inputs)
        self.assertTrue(j.passed, j.open)


class CursorReviewRound2(unittest.TestCase):
    """Regressions for Cursor's second NO-GO, on ed88b99."""

    def test_a_required_check_that_did_not_pass_is_not_a_pass(self):
        # Present is not passed: skipped, neutral, empty or still
        # running all refuse, and the refusal names the conclusion.
        for conclusion, status in (("skipped", "completed"),
                                   ("neutral", "completed"),
                                   ("", "completed"),
                                   ("success", "in_progress")):
            inputs = green_inputs()
            inputs["check_runs"][0]["conclusion"] = conclusion
            inputs["check_runs"][0]["status"] = status
            j = judged(inputs)
            self.assertFalse(j.passed, (conclusion, status))
            self.assertTrue(
                any("only `success` is a pass" in l for l in j.open),
                (conclusion, status, j.open),
            )

    def test_a_required_run_with_no_url_cannot_be_linked_so_it_refuses(self):
        inputs = green_inputs()
        inputs["check_runs"][0]["html_url"] = ""
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("cannot link the run" in l for l in j.open), j.open)
        # And no card ever prints "required-ci None".
        self.assertNotIn("required-ci None", gate_card.render(inputs, j))

    def test_a_bot_suffix_does_not_make_an_app_the_relay_account(self):
        inputs = green_inputs()
        inputs["issue_comments"][1]["author"] = "sfdc-24[bot]"
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Codex" in l and "silence" in l for l in j.open), j.open)
        # The real account still counts, in any case.
        inputs["issue_comments"][1]["author"] = "SFDC-24"
        self.assertTrue(judged(inputs).passed)

    def test_a_badge_and_its_anchor_split_across_lines_is_still_a_finding(self):
        inputs = green_inputs()
        inputs["reviews"].insert(0, {
            "id": 9, "author": "copilot-pull-request-reviewer[bot]",
            "state": "COMMENTED", "commit_id": OTHER,
            "body": '- <img alt="High severity">\n  [Split finding](#discussion_r55)',
            "submitted_at": "2026-10-01T00:00:00Z",
            "html_url": "https://example.test/r/9",
        })
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("discussion_r55" in l and "never observed" in l for l in j.open), j.open
        )

    def test_each_anchor_keeps_its_own_badge_in_a_list(self):
        body = ('- <img alt="High severity"> [A](#discussion_r11)\n'
                '- <img alt="Low severity"> [B](#discussion_r12)\n'
                '- <img alt="Critical severity"> [C](#discussion_r13)')
        anchors = gate_card.copilot_finding_anchors([{
            "author": "copilot-pull-request-reviewer[bot]", "body": body,
        }])
        self.assertEqual(anchors, {"11": "high-severity", "13": "critical-severity"})


class CodexVerdictShapes(unittest.TestCase):
    """Copilot discussion_r4170699606: the marker line is an ID, and the
    `## Codex:` heading form must still be read as a verdict."""

    def _verdict(self, body: str) -> str | None:
        return gate_card.codex_verdict({"author": "sfdc-24", "body": body}, SHA)

    def test_a_marker_id_containing_go_is_not_a_verdict_on_its_own(self):
        # A clean id line over a body with no verdict is silence. (An
        # id line that does NOT parse cleanly is a different case and
        # refuses instead - see the unreadable-receipt test below.)
        self.assertIsNone(self._verdict(
            f"CODEX-PR316-GO-RECEIPT\n\nTarget SHA `{SHA}`. Review pending."
        ))

    def test_both_live_receipt_shapes_are_read(self):
        # The #315 shape: an HTML marker, then the heading.
        self.assertEqual(self._verdict(
            f"<!-- CODEX-PR315-X -->\n\n## Codex: GO / NO-MAJOR\n\n"
            f"Exact reviewed head: `{SHA}`."
        ), "GO")
        # The heading alone, leading the comment: Copilot's "retain
        # the `## Codex: GO...` form". Dropping the lead line blindly
        # would refuse this, which is a false refusal of a real GO.
        self.assertEqual(self._verdict(
            f"## Codex: GO / NO-MAJOR for `{SHA}`\n\nNo findings."
        ), "GO")
        self.assertEqual(self._verdict(
            f"## Codex: NO-GO for `{SHA}`\n\nThree findings."
        ), "NO-GO")
        self.assertEqual(self._verdict(f"CODEX-PR316-X\n\nGO at `{SHA}`."), "GO")

    def test_a_marker_id_with_go_never_lifts_a_no_go_body(self):
        self.assertEqual(
            self._verdict(f"CODEX-PR316-GO-CHECK\n\nNO-GO at `{SHA}`."), "NO-GO"
        )

    def test_the_subject_and_the_token_come_from_the_same_text(self):
        # Cursor's attack: an id line naming THIS SHA with a heading
        # that GOes for ANOTHER commit. The heading never reviewed
        # this SHA, so this must never be a GO here.
        got = self._verdict(f"CODEX-X-20261002 `{SHA}`\n\n## Codex: GO for `{OTHER}`")
        self.assertNotEqual(got, "GO")
        # And the mirror: an id naming another SHA with a heading that
        # NO-GOes this one. The NO-GO must not be lost, or an earlier
        # GO would stay the latest verdict.
        self.assertEqual(
            self._verdict(f"CODEX-X-{OTHER[:8]}\n\n## Codex: NO-GO for `{SHA}`"),
            "NO-GO",
        )

    def test_unwrapping_cannot_manufacture_a_marker_that_swallows_a_no_go(self):
        # `<!-- CODEX-ID-->NO-GO` once became the marker
        # `CODEX-IDNO-GO`, dropping the line and the NO-GO with it.
        self.assertEqual(
            self._verdict(f"<!-- CODEX-ID-->NO-GO\n\n## Codex: GO for `{SHA}`"),
            "NO-GO",
        )

    def test_a_receipt_that_cannot_be_read_refuses_rather_than_going_quiet(self):
        # It claims to be a Codex receipt and names this SHA, but its
        # lead line parses as neither an id nor a heading.
        self.assertEqual(self._verdict(f"CODEX-PR316-NO-GO-20261002 `{SHA}`"), "NO-GO")

    def test_negated_or_pending_prose_is_never_an_approval(self):
        # Copilot discussion_r4170740189: searching the whole body for
        # a verdict word turned "this is not a GO" and "No GO has been
        # issued" into approvals. A GO must OPEN the verdict-bearing
        # line - the first line below the id, or the heading itself.
        self.assertIsNone(self._verdict(
            f"CODEX-PR316-PENDING\n\nTarget `{SHA}`. Review pending; this is not a GO."
        ))
        self.assertIsNone(self._verdict(
            f"## Codex: review pending for `{SHA}`\n\nNo GO has been issued."
        ))
        self.assertIsNone(self._verdict(
            f"CODEX-X\n\nI cannot reach a verdict on `{SHA}` yet; expect a GO tomorrow."
        ))
        # A stated verdict still reads, bold or plain.
        self.assertEqual(self._verdict(f"CODEX-X\n\n**GO** at `{SHA}`."), "GO")

    def test_a_dispatch_that_quotes_go_or_no_go_is_still_silence(self):
        # The real one-line #315 dispatch: it cites a CODEX-... id and
        # quotes "Reply GO or NO-GO", from the relay account. Reading
        # it as a NO-GO would refuse every PR the moment a review is
        # asked for - the over-strict failure, which is also a failure.
        self.assertIsNone(self._verdict(
            f"@cursor Please check exact commit {SHA} on this PR. It answers "
            "Copilot 4169273881 and Codex's CODEX-RISK-315-RESPONSE-ID-LOG-20261002. "
            "Reply GO or NO-GO for this exact commit."
        ))

    def test_a_go_for_another_sha_is_not_a_verdict_here(self):
        self.assertIsNone(self._verdict(f"CODEX-X\n\nGO at `{OTHER}`."))


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
        inputs = green_inputs()
        inputs["pr_after"]["head_sha"] = OTHER
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
            inputs["pr_after"].update(patch)
            j = judged(inputs)
            self.assertFalse(j.passed, patch)
            self.assertTrue(any(needle in l for l in j.open), (patch, j.open))

    def test_a_state_that_changes_during_the_reads_refuses(self):
        # Cursor's case: a merge (or a draft conversion) landing between
        # the two PR reads must not pass on the first read's word.
        for patch, needle in (
            ({"merged": True, "state": "closed"}, "already merged"),
            ({"draft": True}, "draft"),
            ({"state": "closed"}, "not open"),
        ):
            inputs = green_inputs()
            inputs["pr_after"].update(patch)
            j = judged(inputs)
            self.assertFalse(j.passed, patch)
            self.assertTrue(
                any(needle in l and "by the end of the reads" in l for l in j.open),
                (patch, j.open),
            )

    def test_a_v1_snapshot_is_refused_rather_than_judged(self):
        inputs = green_inputs()
        inputs["schema"] = "gate-card-inputs-v1"
        with self.assertRaises(gate_card.GateError):
            judged(inputs)
        inputs = green_inputs()
        del inputs["pr_after"]
        with self.assertRaises(gate_card.GateError):
            judged(inputs)


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
        self.assertTrue(
            any("silence is not a GO" in l and "required-ci" in l for l in j.open),
            j.open,
        )


class CopilotReviewRound3(unittest.TestCase):
    """Copilot's two CI-state findings on 9a98937."""

    def test_an_empty_legacy_status_is_not_pending_ci(self):
        # discussion: GitHub reports the combined status as "pending"
        # when a commit has NO legacy status contexts, which is normal
        # for a check-runs repository. Reading that as pending CI made
        # the gate unable to pass any modern PR.
        inputs = green_inputs()
        inputs["combined_status"] = {"state": "pending", "total_count": 0,
                                     "contexts": []}
        self.assertTrue(judged(inputs).passed, judged(inputs).open)
        # A genuinely pending CONTEXT still refuses.
        inputs["combined_status"] = {
            "state": "pending", "total_count": 1,
            "contexts": [{"context": "legacy/deploy", "state": "pending"}],
        }
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("pending" in l for l in j.open), j.open)
        # And a failing context refuses whatever the runs say.
        inputs["combined_status"] = {
            "state": "failure", "total_count": 1,
            "contexts": [{"context": "legacy/deploy", "state": "failure"}],
        }
        self.assertFalse(judged(inputs).passed)

    def test_a_superseded_duplicate_run_cannot_stand_in_for_the_latest(self):
        # discussion_r4170880070: the endpoint can return one NAME
        # from several suites, newest first, and the old success was
        # winning over a new skipped run.
        inputs = green_inputs()
        inputs["check_runs"] = [
            {"name": "required-ci", "status": "completed", "conclusion": "skipped",
             "started_at": "2026-10-03T00:10:00Z", "app_id": "15368",
             "html_url": "https://example.test/ci/new"},
            {"name": "required-ci", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-02T00:10:00Z", "app_id": "15368",
             "html_url": "https://example.test/ci/old"},
        ]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("only `success` is a pass" in l for l in j.open), j.open)
        # The card must never link the superseded run.
        self.assertNotIn("ci/old", gate_card.render(inputs, j))
        # The newest being green is what passes, and it is the one linked.
        inputs["check_runs"][0]["conclusion"] = "success"
        ok = judged(inputs)
        self.assertTrue(ok.passed, ok.open)
        self.assertIn("ci/new", gate_card.render(inputs, ok))

    def test_unorderable_duplicates_that_disagree_are_ambiguous(self):
        inputs = green_inputs()
        inputs["check_runs"] = [
            {"name": "required-ci", "status": "completed", "conclusion": "skipped",
             "started_at": "", "app_id": "15368",
             "html_url": "https://example.test/ci/a"},
            {"name": "required-ci", "status": "completed", "conclusion": "success",
             "started_at": "", "app_id": "15368",
             "html_url": "https://example.test/ci/b"},
        ]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("cannot say which is current" in l for l in j.open), j.open
        )


class CursorReviewRound3(unittest.TestCase):
    """Cursor's NO-GO on 7f61076: four ways the CI reading was wrong."""

    def test_a_green_rerun_clears_an_older_failed_attempt(self):
        # The over-strict one, and the worst of the four: GitHub
        # leaves a failed attempt on the commit beside its green
        # re-run, and every run was being judged, so a check that
        # failed once could never pass again however often it was
        # re-run green.
        inputs = green_inputs()
        inputs["check_runs"] = [
            {"name": "required-ci", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-03T00:10:00Z", "app_id": "15368",
             "html_url": "https://example.test/ci/new"},
            {"name": "required-ci", "status": "completed", "conclusion": "failure",
             "started_at": "2026-10-02T00:10:00Z", "app_id": "15368",
             "html_url": "https://example.test/ci/old"},
        ]
        j = judged(inputs)
        self.assertTrue(j.passed, j.open)
        self.assertIn("ci/new", gate_card.render(inputs, j))
        # A non-required check re-run green must not refuse either.
        inputs["check_runs"] += [
            {"name": "lint", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-03T00:20:00Z", "app_id": "15368",
             "html_url": "u"},
            {"name": "lint", "status": "completed", "conclusion": "failure",
             "started_at": "2026-10-02T00:20:00Z", "app_id": "15368",
             "html_url": "u"},
        ]
        self.assertTrue(judged(inputs).passed, judged(inputs).open)
        # And the latest failing still refuses.
        inputs["check_runs"][0]["conclusion"] = "failure"
        self.assertFalse(judged(inputs).passed)

    def test_runs_are_ordered_in_time_not_in_text(self):
        # 2026-10-02T23:00:00-04:00 is 03:00Z - LATER than
        # 2026-10-03T01:00:00Z - and sorts earlier as a string.
        inputs = green_inputs()
        inputs["check_runs"] = [
            {"name": "required-ci", "status": "completed", "conclusion": "skipped",
             "started_at": "2026-10-02T23:00:00-04:00", "app_id": "15368",
             "html_url": "https://example.test/ci/actually-newer"},
            {"name": "required-ci", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-03T01:00:00Z", "app_id": "15368",
             "html_url": "https://example.test/ci/earlier-success"},
        ]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("only `success` is a pass" in l for l in j.open), j.open)
        self.assertNotIn("ci/earlier-success", gate_card.render(inputs, j))

    def test_a_legacy_context_is_judged_whatever_the_rollup_says(self):
        # A missing count was stored as 0 and read as "no legacy
        # statuses", so a pending or failing CONTEXT beside it passed;
        # and a rollup of "success" could sit over a failing context.
        for word, state in (("pending", "failure"), ("success", "failure"),
                            ("success", "pending"), ("pending", "pending")):
            inputs = green_inputs()
            inputs["combined_status"] = {
                "state": word, "total_count": None,
                "contexts": [{"context": "legacy/deploy", "state": state}],
            }
            j = judged(inputs)
            self.assertFalse(j.passed, (word, state))
            self.assertTrue(any("CI on this SHA is" in l for l in j.open),
                            (word, state, j.open))
        # A green context passes, and no contexts at all is not pending.
        ok = green_inputs()
        ok["combined_status"] = {
            "state": "success", "total_count": 1,
            "contexts": [{"context": "legacy/deploy", "state": "success"}],
        }
        self.assertTrue(judged(ok).passed, judged(ok).open)
        empty = green_inputs()
        empty["combined_status"] = {"state": "pending", "total_count": None,
                                    "contexts": []}
        self.assertTrue(judged(empty).passed, judged(empty).open)

    def test_the_fixtures_carry_the_real_per_run_start_times(self):
        # Cursor spotted one invented value stamped across every run.
        # The #315 fixture is a recorded read, so its stamps differ.
        fixture_315 = fixture("pr315_ee4c0c1.json")
        stamps = {r["name"]: r["started_at"] for r in fixture_315["check_runs"]}
        self.assertEqual(stamps["copilot-pull-request-reviewer"],
                         "2026-10-02T20:57:04Z")
        self.assertEqual(stamps["test (3.14, lf)"], "2026-10-02T20:56:59Z")
        self.assertEqual(stamps["test (3.12, lf)"], "2026-10-02T20:57:34Z")
        self.assertGreater(len(set(stamps.values())), 1)


class CopilotReviewRound4(unittest.TestCase):
    """Copilot discussion_r4170938756: the legacy statuses paginate."""

    def test_copilots_exact_repro_refuses(self):
        # state=failure, total_count=2, one visible successful context:
        # trusting the partial list over the rollup word passed it.
        inputs = green_inputs()
        inputs["combined_status"] = {
            "state": "failure", "total_count": 2,
            "contexts": [{"context": "legacy/a", "state": "success"}],
        }
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("not read whole" in l for l in j.open), j.open)

    def test_a_rollup_failure_with_nothing_failing_recorded_refuses(self):
        # The same inconsistency with no count to catch it.
        inputs = green_inputs()
        inputs["combined_status"] = {
            "state": "failure", "total_count": None,
            "contexts": [{"context": "legacy/a", "state": "success"}],
        }
        self.assertFalse(judged(inputs).passed)

    def test_a_short_context_list_is_a_partial_read(self):
        inputs = green_inputs()
        inputs["combined_status"] = {
            "state": "success", "total_count": 3,
            "contexts": [{"context": "legacy/a", "state": "success"}],
        }
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("not read whole" in l for l in j.open), j.open)
        # Complete and green passes.
        inputs["combined_status"]["total_count"] = 1
        self.assertTrue(judged(inputs).passed, judged(inputs).open)

    def test_gather_pages_the_legacy_statuses_and_finds_a_later_failure(self):
        # The failure lives on page 2, which an unparameterized read
        # never asked for.
        page1 = [{"context": f"legacy/ok-{i}", "state": "success"}
                 for i in range(100)]
        page2 = [{"context": "legacy/deploy", "state": "failure"}]

        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                return [] if params.get("page") != 1 else [
                    {"id": 1, "user": {"login": "cursor[bot]"},
                     "body": f"**GO** on `{SHA}`.", "created_at": "t",
                     "html_url": "u1"}]
            if path.endswith("/pulls/999/reviews"):
                return [{"id": 1,
                         "user": {"login": "copilot-pull-request-reviewer[bot]"},
                         "state": "COMMENTED", "commit_id": SHA,
                         "body": "**Findings:** None", "submitted_at": "t",
                         "html_url": "r1"}]
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "started_at": "2026-10-03T00:00:00Z",
                     "html_url": "ci"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                if params.get("page") == 1:
                    return {"state": "failure", "total_count": 101,
                            "statuses": page1}
                return {"state": "failure", "total_count": 101, "statuses": page2}
            raise AssertionError(path)

        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": []}}}}}

        inputs = gate_card.gather("o", "r", 999, None, rest, graphql)
        self.assertEqual(len(inputs["combined_status"]["contexts"]), 101)
        j = gate_card.judge(inputs, require_codex=False)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("legacy status legacy/deploy=failure" in l for l in j.open), j.open
        )

    def test_a_legacy_count_that_disagrees_with_the_read_is_an_error(self):
        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                return []
            if path.endswith("/pulls/999/reviews"):
                return []
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 0, "check_runs": []}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "success", "total_count": 7,
                        "statuses": [{"context": "a", "state": "success"}]}
            raise AssertionError(path)

        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": []}}}}}

        with self.assertRaises(gate_card.GateError):
            gate_card.gather("o", "r", 999, None, rest, graphql)


class CopilotReviewRound5(unittest.TestCase):
    """Copilot discussion_r4170964361: a NO-GO must not be discardable."""

    def _verdict(self, body: str) -> str | None:
        return gate_card.cursor_verdict({"author": "cursor[bot]", "body": body}, SHA)

    def test_a_nogo_naming_this_head_and_another_sha_still_counts(self):
        # The extra SHA used to make the line "ambiguous", so the
        # NO-GO was discarded and an OLDER GO stayed the latest
        # verdict - an extra SHA could resurrect a withdrawn approval.
        self.assertEqual(
            self._verdict(f"**NO-GO** on `{SHA}`; compared against `{OTHER}`"),
            "NO-GO",
        )

    def test_a_newer_nogo_with_an_extra_sha_sinks_an_older_go(self):
        inputs = green_inputs()
        inputs["issue_comments"].append({
            "id": 7, "author": "cursor[bot]",
            "body": f"**NO-GO** on `{SHA}`; compared against `{OTHER}`",
            "created_at": "2026-10-03T00:40:00Z",
            "html_url": "https://example.test/c/7",
        })
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("Cursor" in l and "NO-GO" in l for l in j.open), j.open
        )

    def test_a_go_stays_strict_in_every_way_it_was(self):
        # Text may never LIFT a verdict to GO: all of these stay silence.
        self.assertIsNone(self._verdict(
            f"**GO** on `{OTHER}`. Against main the tree also matches {SHA}."))
        self.assertIsNone(self._verdict(f"GO or NO-GO for `{SHA}`?"))
        self.assertIsNone(self._verdict(f"**GO** on `{SHA}`. It sits on `{OTHER}`."))
        self.assertIsNone(self._verdict(f"**NO-GO** on `{OTHER}`."))
        # And the live shape, whose base SHA is short, still reads.
        self.assertEqual(
            self._verdict(f"**GO** on `{SHA}`. It sits directly on `9607d08`."), "GO")


class CursorReviewRound4(unittest.TestCase):
    """Cursor's NO-GO on 657c2b4: four holes in the legacy-status code
    I had written twenty minutes earlier."""

    def test_the_pending_twin_of_the_failure_disagreement_refuses(self):
        # The first disagreement check covered only the failing word,
        # so "pending" over nothing pending - its exact twin - passed.
        for word in ("pending", "expected"):
            for total in (1, None):
                inputs = green_inputs()
                inputs["combined_status"] = {
                    "state": word, "total_count": total,
                    "contexts": [{"context": "legacy/deploy", "state": "success"}],
                }
                j = judged(inputs)
                self.assertFalse(j.passed, (word, total))
                self.assertTrue(any("not read whole" in l for l in j.open),
                                (word, total, j.open))

    def test_a_context_with_no_state_is_not_a_passing_context(self):
        # An absent `state` was stored as "" and then counted as
        # success. The rollup word here is `success` and the count
        # matches, so NOTHING else can refuse it - only the rule that
        # an unreadable state is not evidence.
        for state in ("", "something-new"):
            inputs = green_inputs()
            inputs["combined_status"] = {
                "state": "success", "total_count": 1,
                "contexts": [{"context": "legacy/deploy", "state": state}],
            }
            j = judged(inputs)
            self.assertFalse(j.passed, state)
            self.assertTrue(any("not read whole" in l for l in j.open),
                            (state, j.open))
        # A readable success in the same position passes.
        ok = green_inputs()
        ok["combined_status"] = {
            "state": "success", "total_count": 1,
            "contexts": [{"context": "legacy/deploy", "state": "success"}],
        }
        self.assertTrue(judged(ok).passed, judged(ok).open)

    def test_githubs_no_legacy_status_shape_still_passes(self):
        # The counterweight: pending over NO contexts is what GitHub
        # returns for a commit with no legacy status, and it must pass.
        inputs = green_inputs()
        inputs["combined_status"] = {"state": "pending", "total_count": 0,
                                     "contexts": []}
        self.assertTrue(judged(inputs).passed, judged(inputs).open)

    def test_a_partial_read_still_names_what_it_did_see(self):
        # The early return skipped the detail, so a count mismatch
        # hid both a failing check run and a failing context.
        inputs = green_inputs()
        inputs["check_runs"].append(
            {"name": "lint", "status": "completed", "conclusion": "failure",
             "started_at": "2026-10-03T00:00:00Z", "html_url": "u"})
        inputs["combined_status"] = {
            "state": "pending", "total_count": 5,
            "contexts": [{"context": "legacy/deploy", "state": "failure"},
                         {"context": "legacy/a", "state": "success"}],
        }
        j = judged(inputs)
        self.assertFalse(j.passed)
        text = "\n".join(j.open)
        self.assertIn("not read whole", text)
        self.assertIn("legacy status legacy/deploy=failure", text)
        self.assertIn("lint=failure", text)

    def test_the_rollup_word_and_count_come_from_page_one(self):
        # Reassigning `combined` per page read them from the LAST
        # body, so a final empty page erased a `failure` rollup.
        page1 = [{"context": f"legacy/ok-{i}", "state": "success"}
                 for i in range(100)]

        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                return [] if params.get("page") != 1 else [
                    {"id": 1, "user": {"login": "cursor[bot]"},
                     "body": f"**GO** on `{SHA}`.", "created_at": "t",
                     "html_url": "u1"}]
            if path.endswith("/pulls/999/reviews"):
                return [{"id": 1,
                         "user": {"login": "copilot-pull-request-reviewer[bot]"},
                         "state": "COMMENTED", "commit_id": SHA,
                         "body": "**Findings:** None", "submitted_at": "t",
                         "html_url": "r1"}]
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "started_at": "2026-10-03T00:00:00Z",
                     "html_url": "ci"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                if params.get("page") == 1:
                    return {"state": "failure", "total_count": 100,
                            "statuses": page1}
                return {"statuses": []}  # a last page that says nothing
            raise AssertionError(path)

        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": []}}}}}

        inputs = gate_card.gather("o", "r", 999, None, rest, graphql)
        self.assertEqual(inputs["combined_status"]["state"], "failure")
        self.assertEqual(inputs["combined_status"]["total_count"], 100)
        j = gate_card.judge(inputs, require_codex=False)
        self.assertFalse(j.passed)


class CursorReviewRound5(unittest.TestCase):
    """Cursor's NO-GO on 509d9b0: three verdict-reader holes."""

    def _verdict(self, body: str) -> str | None:
        return gate_card.cursor_verdict({"author": "cursor[bot]", "body": body}, SHA)

    def test_a_nogo_below_the_lead_line_still_sinks(self):
        # The sink only read the lead line, so a NO-GO on line two of
        # a comment whose first line said GO was ignored.
        self.assertEqual(self._verdict(
            f"**GO** on `{SHA}`\n\n**NO-GO** on `{SHA}`; compared against `{OTHER}`"
        ), "NO-GO")
        # And a lead line that says both, which used to be silence and
        # therefore left an older GO standing.
        self.assertEqual(self._verdict(f"**GO** on `{SHA}`. NO-GO."), "NO-GO")

    def test_go_as_a_verb_is_not_a_verdict(self):
        # Stripping decoration loosely turned a bullet into a GO.
        # "go through the remaining tests" is not an approval.
        self.assertIsNone(self._verdict(
            f"* **GO** through the remaining tests on `{SHA}`."))
        self.assertIsNone(self._verdict(f"* **GO** on `{SHA}`."))
        self.assertIsNone(self._verdict(f"__**GO**__ on `{SHA}`."))
        self.assertIsNone(self._verdict(f"**GO** ahead and merge `{SHA}` later."))
        # The live shape still reads.
        self.assertEqual(
            self._verdict(f"**GO** on `{SHA}`. It sits directly on `9607d08`."), "GO")

    def test_both_question_forms_mean_the_same_thing(self):
        # The defect was that one refused and the other was ignored,
        # depending on word order. A question is not a verdict.
        self.assertIsNone(self._verdict(f"GO or NO-GO for `{SHA}`?"))
        self.assertIsNone(self._verdict(f"NO-GO or GO for `{SHA}`?"))

    def test_a_newer_nogo_wins_whatever_order_the_list_is_in(self):
        # latest_verdict's docstring claimed created_at; the code kept
        # the last match in LIST order. A live gather asks for
        # `created` ascending, which hid it.
        newer_nogo = {"id": 1, "author": "cursor[bot]",
                      "body": f"**NO-GO** on `{SHA}`.",
                      "created_at": "2026-10-03T00:40:00Z", "html_url": "new"}
        older_go = {"id": 2, "author": "cursor[bot]",
                    "body": f"**GO** on `{SHA}`.",
                    "created_at": "2026-10-02T00:01:00Z", "html_url": "old"}
        for order in ([newer_nogo, older_go], [older_go, newer_nogo]):
            got = gate_card.latest_verdict(order, SHA, gate_card.cursor_verdict)
            self.assertEqual(got[0], "NO-GO", order[0]["html_url"])
            self.assertEqual(got[1]["html_url"], "new")
        # End to end, in the order that used to pass.
        inputs = green_inputs()
        inputs["issue_comments"] = [newer_nogo, older_go] + inputs["issue_comments"][1:]
        self.assertFalse(judged(inputs).passed)

    def test_an_untimed_verdict_later_in_the_list_still_wins(self):
        # All that is known about a verdict with no readable timestamp
        # is where it sits, so it cannot be ruled out as the newer one.
        timed_go = {"id": 1, "author": "cursor[bot]",
                    "body": f"**GO** on `{SHA}`.",
                    "created_at": "2026-10-03T00:40:00Z", "html_url": "timed"}
        untimed_nogo = {"id": 2, "author": "cursor[bot]",
                        "body": f"**NO-GO** on `{SHA}`.",
                        "created_at": "", "html_url": "untimed"}
        got = gate_card.latest_verdict([timed_go, untimed_nogo], SHA,
                                       gate_card.cursor_verdict)
        self.assertEqual(got[0], "NO-GO")


class CopilotReviewRound6(unittest.TestCase):
    """Copilot discussion_r4171030308: same name, different apps.

    This is the opposite horn of Cursor's green-rerun finding, and the
    pair of them is why the rule needs LINEAGE rather than a choice.
    """

    def _run(self, name, conclusion, app, when, url="u"):
        return {"name": name, "status": "completed", "conclusion": conclusion,
                "app_id": app, "started_at": when, "html_url": url}

    def test_two_apps_posting_one_name_are_two_checks_not_attempts(self):
        # Copilot's repro: a newer successful `lint` and an older
        # failing `lint`. Grouping by name alone kept the success and
        # passed, though the failing one is a DIFFERENT check that is
        # currently failing.
        inputs = green_inputs()
        inputs["check_runs"] = [
            self._run("required-ci", "success", "15368", "2026-10-03T00:00:00Z", "ci"),
            self._run("lint", "success", "111", "2026-10-03T00:20:00Z", "lint/new-app"),
            self._run("lint", "failure", "222", "2026-10-02T00:20:00Z", "lint/other-app"),
        ]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("lint=failure" in l for l in j.open), j.open)

    def test_attempts_of_one_check_still_collapse_to_the_newest(self):
        # Cursor's case, which must keep working: one app, two
        # attempts, the newer green.
        inputs = green_inputs()
        inputs["check_runs"] = [
            self._run("required-ci", "success", "15368", "2026-10-03T00:00:00Z", "ci"),
            self._run("lint", "success", "111", "2026-10-03T00:20:00Z"),
            self._run("lint", "failure", "111", "2026-10-02T00:20:00Z"),
        ]
        self.assertTrue(judged(inputs).passed, judged(inputs).open)

    def test_every_current_run_of_a_required_name_must_pass(self):
        # Two apps both post `required-ci`; both are the gate.
        inputs = green_inputs()
        inputs["check_runs"] = [
            self._run("required-ci", "success", "111", "2026-10-03T00:00:00Z", "ci/a"),
            self._run("required-ci", "failure", "222", "2026-10-03T00:00:00Z", "ci/b"),
        ]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("only `success` is a pass" in l for l in j.open), j.open)
        # Both green passes, and the card links both.
        inputs["check_runs"][1]["conclusion"] = "success"
        ok = judged(inputs)
        self.assertTrue(ok.passed, ok.open)
        card = gate_card.render(inputs, ok)
        self.assertIn("ci/a", card)
        self.assertIn("ci/b", card)

    def test_same_name_runs_with_no_app_cannot_be_proven_attempts(self):
        # Nothing here can tell an attempt from another app's check,
        # so it refuses rather than guessing either way.
        inputs = green_inputs()
        inputs["check_runs"] = [
            {"name": "required-ci", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-03T00:10:00Z", "html_url": "a"},
            {"name": "required-ci", "status": "completed", "conclusion": "failure",
             "started_at": "2026-10-02T00:10:00Z", "html_url": "b"},
        ]
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("cannot say which is current" in l for l in j.open), j.open)

    def test_gather_records_the_lineage(self):
        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                return []
            if path.endswith("/pulls/999/reviews"):
                return []
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "started_at": "2026-10-03T00:00:00Z",
                     "html_url": "ci", "app": {"id": 15368},
                     "check_suite": {"id": 987}}]}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "pending", "total_count": 0, "statuses": []}
            raise AssertionError(path)

        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": []}}}}}

        inputs = gate_card.gather("o", "r", 999, None, rest, graphql)
        run = inputs["check_runs"][0]
        self.assertEqual(run["app_id"], "15368")
        self.assertEqual(run["check_suite_id"], "987")


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
    def _fake_rest(self, calls, check_payloads=None, pr_after=None):
        comments_p1 = [
            {"id": i, "user": {"login": "cursor[bot]"},
             "body": "page-one filler", "created_at": "t", "html_url": f"u{i}"}
            for i in range(100)
        ]
        comments_p2 = [{
            "id": 200, "user": {"login": "cursor[bot]"},
            "body": f"**GO** on `{SHA}`.", "created_at": "t2", "html_url": "u200",
        }]

        base_pr = {"state": "open", "draft": False, "merged": False,
                   "head": {"sha": SHA}, "mergeable_state": "clean",
                   "html_url": "https://example.test/pr/999"}

        def rest(path, params=None):
            calls.append((path, dict(params or {})))
            if path.endswith("/pulls/999"):
                first = not any(
                    p.endswith("/pulls/999") for p, _ in calls[:-1]
                )
                if first or pr_after is None:
                    return dict(base_pr)
                return {**base_pr, **pr_after}
            if path.endswith("/issues/999/comments"):
                return comments_p1 if params.get("page") == 1 else comments_p2 if params.get("page") == 2 else []
            if path.endswith("/pulls/999/reviews"):
                return [{"id": 1, "user": {"login": "copilot-pull-request-reviewer[bot]"},
                         "state": "COMMENTED", "commit_id": SHA, "body": "ok",
                         "submitted_at": "t", "html_url": "r1"}]
            if path.endswith(f"/commits/{SHA}/check-runs"):
                if check_payloads is not None:
                    return check_payloads[int(params.get("page", 1)) - 1]
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "html_url": "ci1"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "success"}
            raise AssertionError(f"unexpected REST path {path}")

        return rest

    def _fake_graphql(self, pages, thread_pages=None, calls=None):
        """Serve both the threads query and the thread-comments query.

        `thread_pages` maps a thread node id to the list of further
        comment pages the node query should return, in order.
        """
        # gather_once walks the lists TWICE (an opening pass and the
        # closing copy it keeps), so these fakes must answer the same
        # cursor the same way however often it is asked. Pages are
        # keyed by the cursor that requests them, not by call order.
        served = {"thread": 0}
        order: dict[str, list] = {}

        def graphql(query, variables):
            if calls is not None:
                calls.append(variables)
            if "PullRequestReviewThread" in query:
                tid = variables["id"]
                seq = (thread_pages or {}).get(tid) or []
                seen = order.setdefault(tid, [])
                cursor = variables.get("cursor")
                if cursor not in seen:
                    seen.append(cursor)
                i = seen.index(cursor)
                if i >= len(seq):
                    raise AssertionError(f"no more comment pages for {tid}")
                return {"data": {"node": {"comments": seq[i]}}}
            # A walk over the threads list always begins with no
            # cursor, which is where the page sequence restarts.
            if variables.get("cursor") is None:
                served["thread"] = 0
            page = pages[min(served["thread"], len(pages) - 1)]
            served["thread"] += 1
            return {"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}
        return graphql

    def _comment(self, did, body="b"):
        return {"databaseId": did, "body": body, "createdAt": "t",
                "url": f"https://example.test/d/{did}",
                "author": {"login": "copilot-pull-request-reviewer"}}

    def test_gather_reads_every_page_and_rereads_the_head(self):
        calls = []
        pages = [
            {"pageInfo": {"hasNextPage": True, "endCursor": "C1"},
             "nodes": [{"id": "T1", "isResolved": True, "isOutdated": False,
                        "comments": {"pageInfo": {"hasNextPage": False},
                                     "nodes": [self._comment(1)]}}]},
            {"pageInfo": {"hasNextPage": False, "endCursor": None},
             "nodes": [{"id": "T2", "isResolved": True, "isOutdated": False,
                        "comments": {"pageInfo": {"hasNextPage": True,
                                                  "endCursor": "IC1"},
                                     "nodes": [self._comment(2)]}}]},
        ]
        # T2 runs past its first page; U4 says paginate, so gather
        # reads the rest instead of refusing the thread for ever.
        thread_pages = {"T2": [
            {"pageInfo": {"hasNextPage": True, "endCursor": "IC2"},
             "nodes": [self._comment(3)]},
            {"pageInfo": {"hasNextPage": False, "endCursor": None},
             "nodes": [self._comment(4)]},
        ]}
        gql_vars = []
        inputs = gate_card.gather_once(
            "o", "r", 999, None, self._fake_rest(calls),
            self._fake_graphql(pages, thread_pages, calls=gql_vars),
        )
        self.assertEqual(inputs["named_sha"], SHA)
        self.assertEqual(len(inputs["issue_comments"]), 101)
        self.assertEqual(inputs["issue_comments"][-1]["body"], f"**GO** on `{SHA}`.")
        self.assertEqual([t["id"] for t in inputs["threads"]], ["T1", "T2"])
        # Every comment of the long thread is present, and it is NOT
        # marked unread.
        self.assertEqual([c["discussion_id"] for c in inputs["threads"][1]["comments"]],
                         [2, 3, 4])
        self.assertFalse(inputs["threads"][1]["truncated"])
        self.assertEqual([v.get("cursor") for v in gql_vars if "id" in v],
        # Two passes per read, each paging the long thread whole.
                         ["IC1", "IC2", "IC1", "IC2"])
        # The PR is read once before and once after everything else.
        pr_reads = [i for i, (p, _) in enumerate(calls) if p.endswith("/pulls/999")]
        self.assertEqual(pr_reads[0], 0)
        self.assertEqual(pr_reads[-1], len(calls) - 1)
        j = gate_card.judge(inputs, require_codex=False)
        self.assertFalse(any("could not be read whole" in l for l in j.open), j.open)

    def test_a_thread_longer_than_the_page_cap_is_refused_as_unread(self):
        pages = [{"pageInfo": {"hasNextPage": False, "endCursor": None},
                  "nodes": [{"id": "T9", "isResolved": True, "isOutdated": False,
                             "comments": {"pageInfo": {"hasNextPage": True,
                                                       "endCursor": "c0"},
                                          "nodes": [self._comment(1)]}}]}]
        endless = [{"pageInfo": {"hasNextPage": True, "endCursor": f"c{i}"},
                    "nodes": [self._comment(i + 2)]}
                   for i in range(gate_card.MAX_THREAD_PAGES + 2)]
        inputs = gate_card.gather_once("o", "r", 999, None, self._fake_rest([]),
                                  self._fake_graphql(pages, {"T9": endless}))
        self.assertTrue(inputs["threads"][0]["truncated"])
        j = gate_card.judge(inputs, require_codex=False)
        self.assertTrue(any("could not be read whole" in l for l in j.open), j.open)

    def _thread_page(self, has_next, cursor, dids, tid="T7", resolved=True):
        info = {}
        if has_next is not None:
            info["hasNextPage"] = has_next
        if cursor is not None:
            info["endCursor"] = cursor
        return {"pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": tid, "isResolved": resolved, "isOutdated": False,
                           "comments": {"pageInfo": info,
                                        "nodes": [self._comment(d) for d in dids]}}]}

    def test_a_follow_up_page_that_never_says_it_is_the_last_is_an_error(self):
        # Cursor's case: page 1 says more follow; page 2 returns an
        # ordinary comment and no pageInfo. That silence used to end
        # the read with truncated=False, leaving page 3's BLOCKER out
        # of the judgement entirely.
        pages = [self._thread_page(True, "c1", [1])]
        for omission in ({}, {"endCursor": "c2"}, None):
            thread_pages = {"T7": [
                {"nodes": [self._comment(2)],
                 **({} if omission is None else {"pageInfo": omission})},
            ]}
            with self.assertRaises(gate_card.GateError):
                gate_card.gather_once("o", "r", 999, None, self._fake_rest([]),
                                 self._fake_graphql(pages, thread_pages))

    def test_a_first_page_that_never_says_it_is_the_last_is_an_error(self):
        for info_args in ((None, None), (None, "c1")):
            pages = [self._thread_page(info_args[0], info_args[1], [1])]
            with self.assertRaises(gate_card.GateError):
                gate_card.gather_once("o", "r", 999, None, self._fake_rest([]),
                                 self._fake_graphql(pages, {"T7": []}))

    def test_more_pages_with_no_cursor_to_reach_them_is_an_error(self):
        pages = [self._thread_page(True, None, [1])]
        with self.assertRaises(gate_card.GateError):
            gate_card.gather_once("o", "r", 999, None, self._fake_rest([]),
                             self._fake_graphql(pages, {"T7": []}))

    def test_a_blocker_living_only_on_a_later_page_still_refuses(self):
        pages = [self._thread_page(True, "c1", [1], resolved=False)]
        thread_pages = {"T7": [
            {"pageInfo": {"hasNextPage": False, "endCursor": None},
             "nodes": [{"databaseId": 2,
                        "body": "BLOCKER - this only exists on page two.",
                        "createdAt": "t", "url": "https://example.test/d/2",
                        "author": {"login": "copilot-pull-request-reviewer"}}]},
        ]}
        inputs = gate_card.gather_once("o", "r", 999, None, self._fake_rest([]),
                                  self._fake_graphql(pages, thread_pages))
        self.assertFalse(inputs["threads"][0]["truncated"])
        j = gate_card.judge(inputs, require_codex=False)
        self.assertFalse(j.passed)
        self.assertTrue(any("Copilot blocker" in l for l in j.open), j.open)

    def test_a_null_node_reply_mid_thread_is_an_error(self):
        pages = [{"pageInfo": {"hasNextPage": False, "endCursor": None},
                  "nodes": [{"id": "T8", "isResolved": True, "isOutdated": False,
                             "comments": {"pageInfo": {"hasNextPage": True,
                                                       "endCursor": "c0"},
                                          "nodes": [self._comment(1)]}}]}]

        def graphql(query, variables):
            if "PullRequestReviewThread" in query:
                return {"data": {"node": None}}
            return {"data": {"repository": {"pullRequest":
                                            {"reviewThreads": pages[0]}}}}
        with self.assertRaises(gate_card.GateError):
            gate_card.gather_once("o", "r", 999, None, self._fake_rest([]), graphql)

    def test_gather_judges_only_a_snapshot_it_saw_twice_unchanged(self):
        # Copilot: "the final consistency check rereads only the PR
        # object" - a same-SHA Cursor NO-GO, a Copilot blocker review,
        # a reopened thread or a CI re-run could land while the other
        # resources were being fetched, with the head never moving.
        reads = {"n": 0}

        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                if params.get("page") != 1:
                    return []
                reads["n"] += 1
                # A Cursor NO-GO lands between the first and second read.
                body = (f"**GO** on `{SHA}`." if reads["n"] == 1
                        else f"**NO-GO** on `{SHA}`.")
                return [{"id": 1, "user": {"login": "cursor[bot]"}, "body": body,
                         "created_at": "t", "html_url": "u1"}]
            if path.endswith("/pulls/999/reviews"):
                return []
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "html_url": "ci"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "success"}
            raise AssertionError(path)

        threads = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}

        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest":
                                            {"reviewThreads": threads}}}}

        # Reads 2 and 3 agree, so the settled snapshot is judged - and
        # it carries the NO-GO, not the GO the first read saw.
        inputs = gate_card.gather("o", "r", 999, None, rest, graphql)
        self.assertIn("NO-GO", inputs["issue_comments"][0]["body"])
        j = gate_card.judge(inputs, require_codex=False)
        self.assertFalse(j.passed)

    def test_a_pr_that_never_settles_is_an_error_not_a_card(self):
        reads = {"n": 0}

        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                if params.get("page") != 1:
                    return []
                reads["n"] += 1
                return [{"id": reads["n"], "user": {"login": "cursor[bot]"},
                         "body": f"**GO** on `{SHA}`. read {reads['n']}",
                         "created_at": "t", "html_url": f"u{reads['n']}"}]
            if path.endswith("/pulls/999/reviews"):
                return []
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "html_url": "ci"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "success"}
            raise AssertionError(path)

        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}}}

        with self.assertRaises(gate_card.GateError):
            gate_card.gather("o", "r", 999, None, rest, graphql)

    def test_the_fingerprint_notices_each_kind_of_gate_change(self):
        base = green_inputs()
        first = gate_card.gate_fingerprint(base)
        self.assertEqual(first, gate_card.gate_fingerprint(green_inputs()))
        for mutate in (
            lambda i: i["issue_comments"].append(
                {"id": 9, "author": "cursor[bot]", "body": "late",
                 "created_at": "t", "html_url": "u9"}),
            lambda i: i["reviews"].append(
                {"id": 9, "author": "copilot-pull-request-reviewer[bot]",
                 "state": "COMMENTED", "commit_id": SHA, "body": "VERDICT: BLOCKER",
                 "submitted_at": "t", "html_url": "r9"}),
            lambda i: i["check_runs"].append(
                {"name": "late", "status": "in_progress", "conclusion": "",
                 "html_url": "u"}),
            lambda i: i["threads"].append(
                {"id": "T9", "is_resolved": False, "is_outdated": False,
                 "truncated": False, "comments": []}),
            lambda i: i["pr"].update(draft=True),
            lambda i: i["combined_status"].update(state="failure"),
        ):
            changed = green_inputs()
            mutate(changed)
            self.assertNotEqual(first, gate_card.gate_fingerprint(changed))

    def test_the_fingerprint_covers_every_field_the_judgement_reads(self):
        # Copilot discussion_r4170826289: the first fingerprint listed
        # fields by hand and omitted each thread comment's author and
        # body and each check run's URL, so a Copilot comment edited
        # from BLOCKER to benign text compared equal and a state seen
        # ONCE was called settled.
        def with_thread(body, author="copilot-pull-request-reviewer"):
            i = green_inputs()
            i["threads"] = [{
                "id": "T1", "is_resolved": False, "is_outdated": False,
                "truncated": False,
                "comments": [{"discussion_id": 1, "author": author, "body": body,
                              "created_at": "t", "html_url": "https://example.test/d/1"}],
            }]
            return i

        blocker = with_thread("BLOCKER - a real finding.")
        benign = with_thread("Looks fine to me.")
        self.assertNotEqual(gate_card.gate_fingerprint(blocker),
                            gate_card.gate_fingerprint(benign))
        # One refuses and the other passes, so they must never be
        # mistaken for the same state.
        self.assertFalse(judged(blocker).passed)
        self.assertTrue(judged(benign).passed, judged(benign).open)
        # The author matters too: the same text from another account
        # does not gate, so it is a different state.
        self.assertNotEqual(
            gate_card.gate_fingerprint(blocker),
            gate_card.gate_fingerprint(with_thread("BLOCKER - a real finding.",
                                                   author="someone-else")),
        )
        # And the required run's URL, which the card links.
        moved = green_inputs()
        moved["check_runs"][0]["html_url"] = "https://example.test/ci/moved"
        self.assertNotEqual(gate_card.gate_fingerprint(green_inputs()),
                            gate_card.gate_fingerprint(moved))

    def test_an_edited_thread_comment_stops_the_read_settling(self):
        # The same thing end to end: the body changes between reads,
        # so no two reads agree and gather refuses rather than
        # returning a state it saw once.
        reads = {"n": 0}

        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                return [] if params.get("page") != 1 else [
                    {"id": 1, "user": {"login": "cursor[bot]"},
                     "body": f"**GO** on `{SHA}`.", "created_at": "t",
                     "html_url": "u1"}]
            if path.endswith("/pulls/999/reviews"):
                return [{"id": 1,
                         "user": {"login": "copilot-pull-request-reviewer[bot]"},
                         "state": "COMMENTED", "commit_id": SHA,
                         "body": "**Findings:** None", "submitted_at": "t",
                         "html_url": "r1"}]
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "html_url": "ci"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "success"}
            raise AssertionError(path)

        def graphql(query, variables):
            reads["n"] += 1
            body = ("BLOCKER - a real finding." if reads["n"] == 1
                    else f"edited {reads['n']}")
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "T1", "isResolved": True, "isOutdated": False,
                           "comments": {
                               "pageInfo": {"hasNextPage": False, "endCursor": None},
                               "nodes": [{"databaseId": 1, "body": body,
                                          "createdAt": "t",
                                          "url": "https://example.test/d/1",
                                          "author": {
                                              "login":
                                              "copilot-pull-request-reviewer"}}]}}]}}}}}

        with self.assertRaises(gate_card.GateError):
            gate_card.gather("o", "r", 999, None, rest, graphql)

    def test_a_flickering_mergeable_state_does_not_stop_the_read_settling(self):
        # GitHub computes mergeable_state asynchronously, so it can
        # read "unknown" then "clean". It gates nothing, and a read
        # that can never settle is its own failure.
        reads = {"n": 0}

        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                reads["n"] += 1
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA},
                        "mergeable_state": "unknown" if reads["n"] % 2 else "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                return [] if params.get("page") != 1 else [
                    {"id": 1, "user": {"login": "cursor[bot]"},
                     "body": f"**GO** on `{SHA}`.", "created_at": "t",
                     "html_url": "u1"}]
            if path.endswith("/pulls/999/reviews"):
                return [{"id": 1,
                         "user": {"login": "copilot-pull-request-reviewer[bot]"},
                         "state": "COMMENTED", "commit_id": SHA,
                         "body": "**Findings:** None", "submitted_at": "t",
                         "html_url": "r1"}]
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "html_url": "ci"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "success"}
            raise AssertionError(path)

        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": []}}}}}

        inputs = gate_card.gather("o", "r", 999, None, rest, graphql)
        self.assertTrue(gate_card.judge(inputs, require_codex=False).passed)

    def _rest_with_late_nogo(self, land_after_comment_reads):
        """A Cursor NO-GO that lands mid-walk and STAYS.

        It appears once the comments list has been sampled
        `land_after_comment_reads` times, so with a single-pass walk it
        is live and permanent while the reviews, checks, threads and
        closing PR read are still ahead - and no revert is needed to
        keep it out of the snapshot.
        """
        seen = {"comments": 0}

        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                if params.get("page") != 1:
                    return []
                seen["comments"] += 1
                live = seen["comments"] > land_after_comment_reads
                body = (f"**NO-GO** on `{SHA}`." if live
                        else f"**GO** on `{SHA}`.")
                return [{"id": 1, "user": {"login": "cursor[bot]"}, "body": body,
                         "created_at": "t", "html_url": "u1"}]
            if path.endswith("/pulls/999/reviews"):
                return [{"id": 1,
                         "user": {"login": "copilot-pull-request-reviewer[bot]"},
                         "state": "COMMENTED", "commit_id": SHA,
                         "body": "**Findings:** None", "submitted_at": "t",
                         "html_url": "r1"}]
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "html_url": "ci"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "success"}
            raise AssertionError(path)

        return rest

    def test_churn_within_one_read_makes_that_read_not_count(self):
        # The provable half of the closing-pass change: when the two
        # passes of ONE read disagree, that read describes no single
        # moment, so it is not comparable and the attempt is spent
        # rather than silently resolved to one of the two states.
        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": []}}}}}

        # Every read churns: reads 1,2 differ, 3,4 differ, 5,6 differ.
        every = {"n": 0}

        def rest_churning(path, params=None):
            if path.endswith("/issues/999/comments"):
                if params.get("page") != 1:
                    return []
                every["n"] += 1
                return [{"id": 1, "user": {"login": "cursor[bot]"},
                         "body": f"**GO** on `{SHA}`. read {every['n']}",
                         "created_at": "t", "html_url": "u1"}]
            return self._rest_with_late_nogo(99)(path, params)

        with self.assertRaises(gate_card.GateError):
            gate_card.gather("o", "r", 999, None, rest_churning, graphql)

        # Churn that stops: reads 1,2 differ (attempt 1 spent), then
        # everything settles and the card can be judged.
        settling = {"n": 0}

        def rest_settling(path, params=None):
            if path.endswith("/issues/999/comments"):
                if params.get("page") != 1:
                    return []
                settling["n"] += 1
                suffix = " churn" if settling["n"] == 1 else ""
                return [{"id": 1, "user": {"login": "cursor[bot]"},
                         "body": f"**GO** on `{SHA}`.{suffix}",
                         "created_at": "t", "html_url": "u1"}]
            return self._rest_with_late_nogo(99)(path, params)

        inputs = gate_card.gather("o", "r", 999, None, rest_settling, graphql)
        self.assertTrue(gate_card.judge(inputs, require_codex=False).passed)

    def test_a_nogo_landing_inside_one_reads_stagger_is_not_missed(self):
        """Copilot discussion_r4170895032, and its harness design.

        My own attempts keyed the change on the number of COMMENT
        reads, which moves with the number of samples, so a
        single-pass mutant passed every test. Copilot's trick is to
        flip the state from a LATER endpoint's fetch: the change then
        lands inside one read's own stagger, after that read's
        comments were copied, and PERSISTS. With one pass per read the
        two reads agree on the stale GO and the card prints over a
        live NO-GO.
        """
        state = {"nogo": False, "reviews": 0}

        def rest(path, params=None):
            if path.endswith("/pulls/999"):
                return {"state": "open", "draft": False, "merged": False,
                        "head": {"sha": SHA}, "mergeable_state": "clean",
                        "html_url": "https://example.test/pr/999"}
            if path.endswith("/issues/999/comments"):
                if params.get("page") != 1:
                    return []
                body = (f"**NO-GO** on `{SHA}`." if state["nogo"]
                        else f"**GO** on `{SHA}`.")
                return [{"id": 1, "user": {"login": "cursor[bot]"}, "body": body,
                         "created_at": "t", "html_url": "u1"}]
            if path.endswith("/pulls/999/reviews"):
                state["reviews"] += 1
                # The NO-GO goes live during the second pass of the
                # first read, after that pass copied the comments.
                if state["reviews"] == 2:
                    state["nogo"] = True
                return [{"id": 1,
                         "user": {"login": "copilot-pull-request-reviewer[bot]"},
                         "state": "COMMENTED", "commit_id": SHA,
                         "body": "**Findings:** None", "submitted_at": "t",
                         "html_url": "r1"}]
            if path.endswith(f"/commits/{SHA}/check-runs"):
                return {"total_count": 1, "check_runs": [
                    {"name": "required-ci", "status": "completed",
                     "conclusion": "success", "started_at": "2026-10-03T00:00:00Z",
                     "html_url": "ci"}]}
            if path.endswith(f"/commits/{SHA}/status"):
                return {"state": "pending", "total_count": 0, "statuses": []}
            raise AssertionError(path)

        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": []}}}}}

        inputs = gate_card.gather("o", "r", 999, None, rest, graphql)
        # The live state is NO-GO, so the snapshot judged must carry it.
        self.assertIn("NO-GO", inputs["issue_comments"][0]["body"])
        j = gate_card.judge(inputs, require_codex=False)
        self.assertFalse(j.passed)
        self.assertTrue(any("NO-GO" in l for l in j.open), j.open)

    def test_a_verdict_landing_mid_walk_is_inside_the_snapshot(self):
        # Cursor's hole: with one pass per read, a NO-GO that lands
        # after the comments were copied and stays was in NEITHER
        # read's comment sample, both fingerprints matched the green
        # copy, and the card printed over a live NO-GO. The lists are
        # now kept from a CLOSING pass, so the NO-GO is in the
        # snapshot that gets judged.
        def graphql(query, variables):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": []}}}}}

        # It lands during the first walk's own stagger: the opening
        # pass sees GO, every later sample sees NO-GO.
        inputs = gate_card.gather("o", "r", 999, None,
                                  self._rest_with_late_nogo(1), graphql)
        self.assertIn("NO-GO", inputs["issue_comments"][0]["body"])
        j = gate_card.judge(inputs, require_codex=False)
        self.assertFalse(j.passed)
        self.assertTrue(any("NO-GO" in l for l in j.open), j.open)

    def test_a_merge_conflict_refuses_and_restarts_settling(self):
        # mergeable_state is kept out of the fingerprint because it
        # flickers, but what it MEANS is not: `dirty` is a real
        # conflict and a handoff cannot stand over one.
        inputs = green_inputs()
        inputs["pr"]["mergeable_state"] = "dirty"
        inputs["pr_after"]["mergeable_state"] = "dirty"
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("merge conflict" in l for l in j.open), j.open)
        # A conflict appearing in the closing read alone still refuses.
        late = green_inputs()
        late["pr_after"]["mergeable_state"] = "dirty"
        j2 = judged(late)
        self.assertFalse(j2.passed)
        self.assertTrue(
            any("merge conflict" in l and "by the end of the reads" in l
                for l in j2.open), j2.open
        )
        # And a flip to dirty changes the fingerprint, so it cannot be
        # settled past, while the unknown/clean flicker still can.
        clean = green_inputs()
        self.assertNotEqual(gate_card.gate_fingerprint(clean),
                            gate_card.gate_fingerprint(inputs))
        flicker = green_inputs()
        flicker["pr"]["mergeable_state"] = "unknown"
        self.assertEqual(gate_card.gate_fingerprint(clean),
                         gate_card.gate_fingerprint(flicker))

    def test_graphql_without_a_token_is_an_error_not_a_pass(self):
        graphql = gate_card.make_graphql(None)
        with self.assertRaises(gate_card.GateError):
            graphql("query", {})

    def _one_thread_page(self):
        return [{"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}]

    def test_check_runs_page_two_is_read_when_total_count_is_missing(self):
        # Cursor's case: paging stopped on total_count, so a missing
        # count ended the read after page 1 and a failing run on page
        # 2 never reached judge().
        full = [{"name": f"c{i}", "status": "completed", "conclusion": "success",
                 "html_url": f"u{i}"} for i in range(100)]
        payloads = [
            {"check_runs": full},  # no total_count at all
            {"check_runs": [
                {"name": "required-ci", "status": "completed",
                 "conclusion": "failure", "html_url": "uFAIL"}]},
        ]
        inputs = gate_card.gather_once(
            "o", "r", 999, None,
            self._fake_rest([], check_payloads=payloads),
            self._fake_graphql(self._one_thread_page() * 2),
        )
        self.assertEqual(len(inputs["check_runs"]), 101)
        j = gate_card.judge(inputs, require_codex=False)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("required-ci=failure" in l for l in j.open), j.open
        )

    def test_a_check_runs_count_mismatch_is_an_error_not_a_verdict(self):
        payloads = [{"total_count": 7, "check_runs": [
            {"name": "required-ci", "status": "completed",
             "conclusion": "success", "html_url": "u"}]}]
        with self.assertRaises(gate_card.GateError):
            gate_card.gather_once("o", "r", 999, None,
                             self._fake_rest([], check_payloads=payloads),
                             self._fake_graphql(self._one_thread_page()))

    def test_a_null_graphql_repository_is_an_error_not_an_empty_thread_list(self):
        def graphql(query, variables):
            return {"data": {"repository": None}}
        with self.assertRaises(gate_card.GateError):
            gate_card.gather_once("o", "r", 999, None, self._fake_rest([]), graphql)

    def test_gather_records_the_whole_second_pr_read(self):
        # A merge landing between the reads has to be visible, not
        # inherited from the first read.
        inputs = gate_card.gather_once(
            "o", "r", 999, None,
            self._fake_rest([], pr_after={"merged": True, "state": "closed"}),
            self._fake_graphql(self._one_thread_page()),
        )
        self.assertFalse(inputs["pr"]["merged"])
        self.assertTrue(inputs["pr_after"]["merged"])
        j = gate_card.judge(inputs, require_codex=False)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("already merged" in l and "by the end of the reads" in l for l in j.open),
            j.open,
        )

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
        passing["pr_after"]["draft"] = False
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
    def test_authority_needs_the_exact_app_login_detection_takes_either(self):
        # Copilot discussion_r4170760384: `cursor` and
        # `copilot-pull-request-reviewer` are registrable account
        # names, so authority must require the exact App login. Only
        # DETECTING a finding may accept either spelling, because
        # looseness there can merely add refusals.
        self.assertFalse(hasattr(gate_card, "norm_login"))
        self.assertEqual(gate_card.CURSOR_VERDICT_LOGIN, "cursor[bot]")
        self.assertEqual(gate_card.COPILOT_REVIEW_LOGIN,
                         "copilot-pull-request-reviewer[bot]")
        self.assertEqual(gate_card.COPILOT_FINDING_LOGINS, frozenset({
            "copilot-pull-request-reviewer",
            "copilot-pull-request-reviewer[bot]",
        }))

    def test_a_suffixless_cursor_account_cannot_give_a_go(self):
        inputs = green_inputs()
        inputs["issue_comments"][0]["author"] = "cursor"
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(any("Cursor" in l and "silence" in l for l in j.open), j.open)

    def test_a_suffixless_copilot_account_is_not_the_review_on_the_sha(self):
        inputs = green_inputs()
        inputs["reviews"][0]["author"] = "copilot-pull-request-reviewer"
        j = judged(inputs)
        self.assertFalse(j.passed)
        self.assertTrue(
            any("no Copilot review on this exact SHA" in l for l in j.open), j.open
        )

    def test_a_graphql_thread_author_without_the_suffix_still_gates(self):
        # The loose direction, which only ever adds refusals.
        inputs = green_inputs()
        inputs["threads"] = [{
            "id": "T1", "is_resolved": False, "is_outdated": False, "truncated": False,
            "comments": [{
                "discussion_id": 1, "author": "copilot-pull-request-reviewer",
                "body": "BLOCKER - from the GraphQL spelling.",
                "created_at": "t", "html_url": "https://example.test/d/1",
            }],
        }]
        self.assertFalse(judged(inputs).passed)

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
        self.assertEqual(anchors, {"11": "high-severity", "13": "critical-severity"})

    def test_every_anchor_of_a_blocker_review_is_tracked_whatever_its_badge(self):
        body = ('VERDICT: BLOCKER - deadlines.\n'
                '- <img alt="High severity"> [A](#discussion_r11)\n'
                '- <img alt="Medium severity"> [B](#discussion_r12)\n'
                '- [C](#discussion_r13)')
        anchors = gate_card.copilot_finding_anchors([{
            "author": "copilot-pull-request-reviewer[bot]", "body": body,
        }])
        self.assertEqual(anchors["11"], "high-severity")
        self.assertIn("summary carried a blocker", anchors["12"])
        self.assertIn("summary carried a blocker", anchors["13"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

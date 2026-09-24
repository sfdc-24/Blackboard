#!/usr/bin/env python3
r"""The shadow waker: it must reach a verdict and it must not be able to act on it.

cloud/waker-shadow/main.py runs the live waker's decision logic in a container.
Two properties matter and both are attacked here rather than asserted:

  1. IT CANNOT WRITE. Not "does not" - cannot. An AST guard refuses to run if the
     module names any waker write path, and the deployed job holds no META_TOKEN
     and no GH_TOKEN, so it could not send a WhatsApp message or touch a repo even
     if a future edit tried.

  2. IT AGREES WITH THE LAPTOP. The verdict rule is NEWS-first, which is what
     #186 established: a board UNKNOWN must not swallow a message from Mr Salam,
     because that is what left seven of his unanswered. If this copy of the rule
     drifts from board_waker's, the shadow comparison is what catches it - so the
     rule is pinned on both sides.

Offline: no board, no GCS, no network. The waker's checks are replaced.

Run: python3 tests/test_waker_shadow.py
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SHADOW = REPO / "cloud" / "waker-shadow" / "main.py"
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "cloud" / "waker-shadow"))

import main as shadow  # noqa: E402

ALLOWED = {"check_board", "check_whatsapp"}
WRITE_PATHS = {"ack_whatsapp", "post_alarm", "save_state", "bus_post",
               "write_digest", "post", "main"}


def guard_verdict(source: str) -> list:
    """The guard's logic over arbitrary source.

    Re-implemented rather than imported because the real one reads __file__ - it
    judges itself, which is what makes it trustworthy in the container and
    untestable against other inputs. TheRealShadow below closes that gap by
    running the real file through this same logic.
    """
    tree = ast.parse(source)
    problems, checks = [], 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "waker"):
            continue
        if fn.attr in WRITE_PATHS:
            problems.append("waker.%s() is a WRITE PATH" % fn.attr)
        elif fn.attr not in ALLOWED:
            problems.append("waker.%s() is not in the allowed set" % fn.attr)
        else:
            checks += 1
    if checks == 0:
        problems.append("no waker check is called")
    return problems


HEAD = "import board_waker as waker\n"
GOOD_CALL = '''
def go(state):
    return waker.check_board(state), waker.check_whatsapp(state)
'''


class ItCannotReachAWritePath(unittest.TestCase):
    def test_every_write_path_is_named_and_refused(self):
        for bad in sorted(WRITE_PATHS):
            with self.subTest(call=bad):
                src = HEAD + GOOD_CALL + '''
def sneak(state):
    waker.%s(state)
''' % bad
                problems = guard_verdict(src)
                self.assertTrue(any("WRITE PATH" in p for p in problems),
                                "waker.%s() was not refused: %r" % (bad, problems))

    def test_the_failure_message_names_which_write_path(self):
        # "not allowed" is not enough to act on. The reviewer needs to know what
        # the module reached for.
        src = HEAD + GOOD_CALL + "\ndef s(x):\n    waker.ack_whatsapp(x)\n"
        self.assertIn("waker.ack_whatsapp() is a WRITE PATH", guard_verdict(src))

    def test_an_unknown_waker_function_is_refused_too(self):
        # Allowlist, not denylist: a write path added to board_waker tomorrow
        # under a new name must still fail without anyone updating WRITE_PATHS.
        src = HEAD + GOOD_CALL + "\ndef s(x):\n    waker.brand_new_writer(x)\n"
        self.assertIn("waker.brand_new_writer() is not in the allowed set",
                      guard_verdict(src))

    def test_a_module_that_checks_nothing_is_refused(self):
        self.assertIn("no waker check is called",
                      guard_verdict(HEAD + "\ndef go():\n    return 1\n"))

    def test_prose_about_writing_does_not_fail_the_guard(self):
        """The trap that has caught this repo four times in one session."""
        src = HEAD + '''
NOTE = "this never calls ack_whatsapp, post_alarm, save_state or bus_post"
def go(state):
    """Does not write. Will not append, post or acknowledge anything."""
    # ack_whatsapp, post_alarm, write_digest - named, never called
    return waker.check_board(state), waker.check_whatsapp(state)
'''
        self.assertEqual(guard_verdict(src), [],
                         "prose about writing was treated as writing")

    def test_the_two_reads_are_accepted(self):
        self.assertEqual(guard_verdict(HEAD + GOOD_CALL), [])


class TheRealShadow(unittest.TestCase):
    def test_it_passes_its_own_guard(self):
        self.assertTrue(SHADOW.is_file())
        self.assertEqual(guard_verdict(SHADOW.read_text(encoding="utf-8")), [])

    def test_the_guard_is_the_first_thing_main_does(self):
        tree = ast.parse(SHADOW.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "main")
        first = fn.body[0]
        self.assertTrue(
            isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
            and getattr(first.value.func, "id", "") ==
            "refuse_if_this_module_can_write",
            "the guard is not the first statement in main()")

    def test_it_always_exits_zero_so_a_quiet_board_is_not_a_red_execution(self):
        with mock.patch.object(shadow, "verdict",
                               return_value={"at": "x", "combined": "QUIET",
                                             "would_exit": 0, "board": "QUIET",
                                             "whatsapp": "QUIET",
                                             "where": "local"}), \
             mock.patch.object(shadow, "record", return_value={}), \
             mock.patch.object(shadow, "refuse_if_this_module_can_write"):
            self.assertEqual(shadow.main(), 0)


class TheVerdictRuleMatchesTheLaptop(unittest.TestCase):
    """NEWS first. #186: his message outranks a failed board read."""

    def run_verdict(self, board, wa, board_fresh=(), wa_fresh=()):
        with mock.patch.object(shadow.waker, "check_board",
                              return_value=(board, "b", list(board_fresh))), \
             mock.patch.object(shadow.waker, "check_whatsapp",
                              return_value=(wa, "w", list(wa_fresh))), \
             mock.patch.dict(os.environ,
                             {"SHADOW_SINCE": "2026-09-23T20:00:00Z"}, clear=True):
            return shadow.verdict()

    def test_his_message_wins_over_a_failed_board_read(self):
        v = self.run_verdict("UNKNOWN", "NEWS", wa_fresh=[{"ts": "x", "text": "hi"}])
        self.assertEqual(v["combined"], "NEWS")
        self.assertEqual(v["would_exit"], 10)
        self.assertTrue(v["would_wake"])

    def test_board_news_wins_over_an_unreadable_whatsapp_lane(self):
        v = self.run_verdict("NEWS", "UNKNOWN", board_fresh=[{"ts": "x"}])
        self.assertEqual(v["combined"], "NEWS")
        self.assertEqual(v["would_exit"], 10)

    def test_unknown_only_wins_when_neither_lane_has_news(self):
        v = self.run_verdict("UNKNOWN", "QUIET")
        self.assertEqual(v["combined"], "UNKNOWN")
        self.assertEqual(v["would_exit"], 2)
        self.assertFalse(v["would_wake"])

    def test_two_quiet_lanes_cost_nothing(self):
        v = self.run_verdict("QUIET", "QUIET")
        self.assertEqual((v["combined"], v["would_exit"]), ("QUIET", 0))

    def test_the_rule_matches_board_waker_s_own_peek(self):
        """Both copies, one fixture set. This is what catches them drifting.

        The rule is duplicated because board_waker's lives inline in main(). If
        that is ever refactored to a function, import it and delete this test -
        but until then, the only thing standing between two copies and a silent
        divergence is a table both are checked against.
        """
        src = (REPO / "scripts" / "board_waker.py").read_text(encoding="utf-8-sig")
        # The peek must test NEWS before UNKNOWN. Order is the whole rule.
        i_news = src.find('if "NEWS" in (status, wa_status)')
        i_unknown = src.find('elif "UNKNOWN" in (status, wa_status)')
        self.assertGreater(i_news, 0, "board_waker no longer checks NEWS first")
        self.assertGreater(i_unknown, i_news,
                           "board_waker checks UNKNOWN before NEWS again - a "
                           "failed board read would swallow his message")

    def test_the_since_value_reaches_both_checks(self):
        # If the two sides ran with different cursors the comparison would be
        # meaningless, so the cursor has to come from one place.
        seen = {}

        def capture(state, *a, **k):
            seen["watermark"] = state.get("watermark")
            seen["wa_watermark"] = state.get("wa_watermark")
            return ("QUIET", "n", [])

        with mock.patch.object(shadow.waker, "check_board", side_effect=capture), \
             mock.patch.object(shadow.waker, "check_whatsapp",
                              return_value=("QUIET", "n", [])), \
             mock.patch.dict(os.environ,
                             {"SHADOW_SINCE": "2026-09-23T20:00:00Z"}, clear=True):
            v = shadow.verdict()
        self.assertEqual(seen["watermark"], "2026-09-23T20:00:00Z")
        self.assertEqual(seen["wa_watermark"], "2026-09-23T20:00:00Z",
                         "wa_watermark must be set explicitly - an absent key "
                         "inherits the board boundary, which board_waker treats "
                         "differently from an empty one")
        self.assertEqual(v["since"], "2026-09-23T20:00:00Z")


class TheCapabilityClaimIsReported(unittest.TestCase):
    """The output has to state what it could do, not just what it did."""

    def blank_verdict(self, env):
        with mock.patch.object(shadow.waker, "check_board",
                              return_value=("QUIET", "n", [])), \
             mock.patch.object(shadow.waker, "check_whatsapp",
                              return_value=("QUIET", "n", [])), \
             mock.patch.dict(os.environ, env, clear=True):
            return shadow.verdict()

    def test_with_no_credentials_it_reports_it_cannot_send_or_reach_github(self):
        v = self.blank_verdict({})
        self.assertFalse(v["can_send_whatsapp"])
        self.assertFalse(v["can_reach_github"])

    def test_if_a_token_ever_appears_the_output_says_so(self):
        # The point of reporting it: a deploy that accidentally injects
        # META_TOKEN shows up in the verdict instead of being invisible.
        v = self.blank_verdict({"META_TOKEN": "not-a-real-token",
                                "GH_TOKEN": "also-not-real"})
        self.assertTrue(v["can_send_whatsapp"])
        self.assertTrue(v["can_reach_github"])

    def test_injected_credentials_are_distinguished_from_a_file(self):
        v = self.blank_verdict({"BUS_URL": "https://x.invalid",
                                "BUS_SECRET": "s"})
        self.assertEqual(v["credential_source"], "injected")
        self.assertEqual(self.blank_verdict({})["credential_source"], "file")


class BookkeepingCannotStopTheShadow(unittest.TestCase):
    def test_a_broken_store_is_reported_not_raised(self):
        # The verdict is the job. A cursor failure must not lose it.
        with mock.patch.dict(os.environ,
                             {"BLACKBOARD_STATE_URI": "gs://nope/nowhere"},
                             clear=True), \
             mock.patch.object(shadow.state_store, "open_store",
                               side_effect=OSError("no metadata server")):
            out = shadow.record({"at": "t", "where": "local", "combined": "QUIET",
                                 "would_exit": 0, "board": "QUIET",
                                 "whatsapp": "QUIET"})
        self.assertIn("OSError", out["reason"])

    def test_no_state_configured_is_not_an_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            out = shadow.record({"at": "t"})
        self.assertIsNone(out["backend"])


if __name__ == "__main__":
    os.chdir(REPO)
    unittest.main(verbosity=2)

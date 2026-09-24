#!/usr/bin/env python3
r"""The one cloud job that may write to the board, attacked on all four properties.

cloud/ack-once/main.py is the first and only container permitted to change the
board. Everything before it in the cutover was read-only. So each safety property
is tested by trying to break it, not by reading the code and agreeing.

THE PROOF THAT MATTERS MOST ALREADY HAPPENED IN PRODUCTION
    On its first real run the gateway returned **HTTP 404** and the row LANDED
    anyway - readback found exactly one matching row. A job that trusted the POST
    status would have called that a failure and resent it, and on an append-only
    board that is how one GROK-ZOOM-HYPERSONIC-001 became four.

    So the test that a 404 is not treated as absence is not hypothetical. It is a
    regression test for something that happened.

Offline: no board, no GCS, no network. The transport and store are replaced.

Run: python3 tests/test_ack_once.py
"""
from __future__ import annotations

import ast
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
ACK = REPO / "cloud" / "ack-once" / "main.py"
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "cloud" / "ack-once"))

import main as ack  # noqa: E402

ALLOWED_CALLS = ("bus", "bus_get", "load_env")


def count_appends(source: str) -> list:
    """The guard's logic, over arbitrary source.

    Re-implemented because the real one reads __file__ - it judges itself, which
    is what makes it trustworthy in the container and untestable against other
    inputs. TheRealJob below runs the real file through this same logic.
    """
    tree = ast.parse(source)
    appends, problems = 0, []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "board"):
            continue
        if fn.attr not in ALLOWED_CALLS:
            problems.append("board.%s() is not an allowed call" % fn.attr)
            continue
        if fn.attr != "bus":
            continue
        payload = next((a for a in node.args if isinstance(a, ast.Dict)), None)
        if payload is None:
            problems.append("no literal payload dict")
            continue
        action = None
        for k, v in zip(payload.keys, payload.values):
            if isinstance(k, ast.Constant) and k.value == "action":
                action = v
        if not (isinstance(action, ast.Constant) and action.value == "append"):
            problems.append("action is not the literal 'append'")
            continue
        appends += 1
    if appends != 1:
        problems.append("found %d appends; exactly one is allowed" % appends)
    return problems


HEAD = "import board_say as board\n"
ONE_APPEND = '''
def go(env, row):
    return board.bus(env, {"action": "append", "title": "T", "sheetRow": row})
'''


class ExactlyOneAppend(unittest.TestCase):
    def test_one_is_accepted(self):
        self.assertEqual(count_appends(HEAD + ONE_APPEND), [])

    def test_two_appends_are_refused(self):
        src = HEAD + ONE_APPEND + ONE_APPEND.replace("def go", "def go2")
        self.assertTrue(any("found 2 appends" in p for p in count_appends(src)))

    def test_zero_appends_is_refused_because_it_would_prove_nothing(self):
        src = HEAD + '''
def go(env):
    return board.bus_get(env, {"action": "read", "title": "T"})
'''
        self.assertTrue(any("found 0 appends" in p for p in count_appends(src)))

    def test_an_append_loop_is_still_one_call_site_and_that_is_a_known_limit(self):
        """Honest about what an AST count can and cannot see.

        A loop around one call site appends many times at runtime while the tree
        shows one. The guard does not catch that, and pretending otherwise would
        be worse than recording it: what actually bounds repeat writes is the
        durable idempotency record, tested in IdempotentAcrossRuns below.
        """
        src = HEAD + '''
def go(env, rows):
    for r in rows:
        board.bus(env, {"action": "append", "title": "T", "sheetRow": r})
'''
        self.assertEqual(count_appends(src), [],
                         "if this ever fails the guard got stronger; update the "
                         "docstring rather than deleting the test")

    def test_an_action_hidden_behind_a_variable_is_refused(self):
        src = HEAD + ONE_APPEND + '''
ACTION = "append"
def sneak(env, row):
    board.bus(env, {"action": ACTION, "title": "T", "sheetRow": row})
'''
        self.assertIn("action is not the literal 'append'", count_appends(src))

    def test_a_payload_built_elsewhere_is_refused(self):
        src = HEAD + ONE_APPEND + '''
def sneak(env, payload):
    board.bus(env, payload)
'''
        self.assertIn("no literal payload dict", count_appends(src))

    def test_any_other_board_function_is_refused(self):
        for call in ("post", "say", "write_row", "main"):
            with self.subTest(call=call):
                src = HEAD + ONE_APPEND + '''
def sneak(env):
    board.%s(env)
''' % call
                self.assertIn("board.%s() is not an allowed call" % call,
                              count_appends(src))

    def test_prose_about_appending_does_not_fail_the_guard(self):
        src = HEAD + ONE_APPEND + '''
NOTE = "we append exactly once; never append twice, never post or write again"
def doc(env):
    """This does not append. It will not write, post or replace anything."""
    # append, write, post - all named here, none called
    return None
'''
        self.assertEqual(count_appends(src), [])


class TheAllowlistRefusesByDefault(unittest.TestCase):
    GOOD_PAYLOAD = "BCB|v=1|id=CLOUD-ACK-PROBE-X|phase=RESULT|from=a|to=b"

    def check(self, to, payload, allow_to=None, prefix=None):
        with mock.patch.object(ack, "ALLOW_TO",
                              allow_to if allow_to is not None else []), \
             mock.patch.object(ack, "REQUIRE_ID_PREFIX",
                               prefix if prefix is not None else ""):
            return ack.check_allowlist(to, payload)

    def test_an_unconfigured_deploy_writes_nothing(self):
        problems = self.check("claude-code-cli", self.GOOD_PAYLOAD)
        self.assertTrue(any("ACK_ALLOW_TO is empty" in p for p in problems))
        self.assertTrue(any("ACK_REQUIRE_ID_PREFIX is empty" in p
                            for p in problems))

    def test_a_recipient_off_the_list_is_refused_and_named(self):
        problems = self.check("grok-bot", self.GOOD_PAYLOAD,
                              allow_to=["claude-code-cli"], prefix="CLOUD-ACK-")
        self.assertTrue(any("grok-bot" in p and "not in" in p for p in problems))

    def test_an_id_outside_the_prefix_is_refused(self):
        problems = self.check("claude-code-cli",
                              "BCB|v=1|id=SOMETHING-ELSE|phase=RESULT",
                              allow_to=["claude-code-cli"], prefix="CLOUD-ACK-")
        self.assertTrue(any("does not start with" in p for p in problems))

    def test_a_payload_with_no_id_is_refused(self):
        problems = self.check("claude-code-cli", "BCB|v=1|phase=RESULT",
                              allow_to=["claude-code-cli"], prefix="CLOUD-ACK-")
        self.assertTrue(any("no id= field" in p for p in problems))

    def test_prose_that_is_not_a_bcb_row_is_refused(self):
        # The board's own rule: rows are BCB, not prose.
        problems = self.check("claude-code-cli", "just a sentence",
                              allow_to=["claude-code-cli"], prefix="CLOUD-ACK-")
        self.assertTrue(any("not a BCB row" in p for p in problems))

    def test_a_correctly_configured_write_passes(self):
        self.assertEqual(
            self.check("claude-code-cli", self.GOOD_PAYLOAD,
                       allow_to=["claude-code-cli"], prefix="CLOUD-ACK-"), [])

    def test_widening_the_allowlist_is_all_a_real_ack_needs(self):
        # The design claim: pointing this at real work is configuration, not code.
        self.assertEqual(
            self.check("grok-bot", "BCB|v=1|id=REAL-ACK-7|phase=RESULT",
                       allow_to=["claude-code-cli", "grok-bot"],
                       prefix="REAL-ACK-"), [])


class AFourZeroFourIsNotAbsence(unittest.TestCase):
    """The regression test for what actually happened on the first real run."""

    def readback_with(self, responses):
        calls = {"n": 0}

        def fake_get(env, params):
            r = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return r

        with mock.patch.object(ack.board, "load_env", return_value={}), \
             mock.patch.object(ack.board, "bus_get", side_effect=fake_get), \
             mock.patch.object(ack, "READBACK_ATTEMPTS", 3), \
             mock.patch.object(ack.time, "sleep", lambda *_: None):
            return ack.readback("THE-ROW-ID")

    def test_a_row_that_is_present_is_found(self):
        body = json.dumps({"rows": [["THE-ROW-ID", "t", "a", "b", "APPEND", "p"]]})
        out = self.readback_with([(200, body)])
        self.assertTrue(out["found"])
        self.assertEqual(out["matched_rows"], 1)

    def test_a_present_row_is_found_even_after_transient_404s(self):
        body = json.dumps({"rows": [["THE-ROW-ID", "t", "a", "b", "APPEND", "p"]]})
        out = self.readback_with([(404, "<html>"), (404, "<html>"), (200, body)])
        self.assertTrue(out["found"])
        self.assertEqual(out["attempts"], ["HTTP 404", "HTTP 404", "ok"])

    def test_an_unreadable_board_never_claims_the_row_is_missing(self):
        out = self.readback_with([(404, "<html>")])
        self.assertFalse(out["found"])
        self.assertIn("NOT proof", out["note"],
                      "an unreadable board must not read as a missing row - the "
                      "natural response to 'missing' is to write again")

    def test_a_health_blob_is_not_an_empty_board(self):
        blob = json.dumps({"ok": True, "service": "bus", "time": "t"})
        out = self.readback_with([(200, blob)])
        self.assertFalse(out["found"])
        self.assertIn("health-blob", out["attempts"])
        self.assertIn("NOT proof", out["note"])

    def test_a_readback_that_returns_other_rows_does_not_count_them(self):
        # match= is a substring filter, so other rows can come back. Only an
        # exact Row_ID match is the row.
        body = json.dumps({"rows": [["SOME-OTHER-ID", "t", "a", "b", "APPEND", "p"]]})
        out = self.readback_with([(200, body)])
        self.assertFalse(out["found"])
        self.assertEqual(out["matched_rows"], 0)


class IdempotentAcrossRuns(unittest.TestCase):
    """What actually bounds repeat writes, since an AST count cannot."""

    class Store:
        def __init__(self, state=None):
            self.state = state or {}
            self.saves = 0

        def load(self, name):
            return dict(self.state), "tok"

        def save(self, name, state, token):
            self.saves += 1
            self.state = state
            return "tok2"

        def describe(self):
            return "fake"

    def test_a_prior_attempt_for_the_same_target_is_found(self):
        store = self.Store({"attempts": [{"target": "T-1", "row_id": "R-1"}]})
        prior, _ = ack.already_written(store, "T-1")
        self.assertEqual(prior["row_id"], "R-1")

    def test_a_different_target_is_not_confused_for_it(self):
        store = self.Store({"attempts": [{"target": "T-1", "row_id": "R-1"}]})
        prior, _ = ack.already_written(store, "T-2")
        self.assertIsNone(prior)

    def test_an_empty_store_reports_no_prior_attempt(self):
        prior, _ = ack.already_written(self.Store(), "T-1")
        self.assertIsNone(prior)

    def test_a_crash_between_record_and_post_still_leaves_the_id(self):
        # posted=False is the case that matters: the row may or may not be on the
        # board, and the next run must look for THAT id rather than mint a new one.
        store = self.Store({"attempts": [{"target": "T-1", "row_id": "R-1",
                                          "posted": False}]})
        prior, _ = ack.already_written(store, "T-1")
        self.assertEqual(prior["row_id"], "R-1")
        self.assertFalse(prior["posted"])


class TheRowShape(unittest.TestCase):
    def test_ten_columns_in_board_say_s_order(self):
        row = ack.build_row("RID", "claude-code-cli", "BCB|v=1|id=X")
        self.assertEqual(len(row), 10)
        self.assertEqual(row[0], "RID")
        self.assertEqual(row[3], "claude-code-cli", "the recipient column")
        self.assertEqual(row[4], "APPEND")
        self.assertEqual(row[5], "BCB|v=1|id=X")
        self.assertEqual(row[7], "Blackboard", "Target_Surface is authoritative")

    def test_the_timestamp_is_the_lexically_comparable_shape(self):
        import re
        row = ack.build_row("RID", "x", "BCB|")
        self.assertRegex(row[1],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class TheRealJob(unittest.TestCase):
    def test_it_passes_its_own_guard(self):
        self.assertTrue(ACK.is_file())
        self.assertEqual(count_appends(ACK.read_text(encoding="utf-8")), [])

    def test_the_guard_is_the_first_thing_main_does(self):
        tree = ast.parse(ACK.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "main")
        first = fn.body[0]
        self.assertTrue(
            isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
            and getattr(first.value.func, "id", "") ==
            "refuse_unless_exactly_one_append")

    def test_durable_state_is_mandatory(self):
        # Without it the job cannot promise it has not already run, so it must
        # refuse rather than risk a duplicate on an append-only board.
        env = {"ACK_TARGET": "T-1", "ACK_TO": "claude-code-cli"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(ack, "ALLOW_TO", ["claude-code-cli"]), \
             mock.patch.object(ack, "REQUIRE_ID_PREFIX", "T-"), \
             mock.patch.object(ack.board, "bus",
                               side_effect=AssertionError("POSTed anyway")):
            self.assertEqual(ack.main(), 2)

    def test_no_target_writes_nothing_and_is_not_an_error(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(ack.board, "bus",
                               side_effect=AssertionError("POSTed anyway")):
            self.assertEqual(ack.main(), 0)


if __name__ == "__main__":
    os.chdir(REPO)
    unittest.main(verbosity=2)

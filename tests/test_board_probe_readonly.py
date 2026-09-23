#!/usr/bin/env python3
r"""The cloud probe's read-only guard, attacked rather than admired.

cloud/board-probe/main.py runs unattended in a container that holds live bus
credentials. The only thing standing between that and a write is one function,
`refuse_if_this_module_can_write`, which walks the module's own syntax tree. This
suite exists because a guard nobody has tried to defeat is decoration.

WHY THE GUARD IS AN AST WALK AND NOT A GREP, which is the whole point
    The first version scanned string literals for write words and **refused the
    probe** - its own error message contained the word "write". That is the third
    time this repository has been caught by the same mistake:

      - the python-suites workflow grepped its suites for "urlopen" and tripped
        on a test whose job was to assert about urlopen
      - the shell-out check in test_wa_notify.py grepped for "powershell.exe" and
        matched the sentence recording its removal
      - this

    A substring cannot tell code from commentary. So the guard asks a question
    about BEHAVIOUR: which functions are called on the board module, and what
    literal action does each read pass. The tests below feed it modules that
    differ only in behaviour, and one that differs only in prose.

Offline: nothing here imports the probe or touches a network. Each case is source
text compiled through the guard's own logic.

Run: python3 tests/test_board_probe_readonly.py
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "cloud" / "board-probe" / "main.py"

HEAD = '''
import ast, os, sys
import board_say as board
ALLOWED_BUS_CALLS = {"load_env", "bus_get"}
'''


def guard_verdict(source: str) -> list:
    """Run the probe's guard logic over arbitrary source, returning its problems.

    Mirrors refuse_if_this_module_can_write. It is re-implemented here rather
    than imported because the real one reads __file__ - it judges itself, by
    design, which is exactly what makes it trustworthy in the container and
    untestable against other inputs. test_the_real_probe_passes_its_own_guard
    below closes that gap by running the real file through this same logic and
    requiring agreement.
    """
    tree = ast.parse(source)
    problems, fetches = [], 0
    allowed = {"load_env", "bus_get"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "board"):
            continue
        if fn.attr not in allowed:
            problems.append("board.%s() is not an allowed call" % fn.attr)
            continue
        if fn.attr != "bus_get":
            continue
        fetches += 1
        params = next((a for a in node.args if isinstance(a, ast.Dict)), None)
        if params is None:
            problems.append("no literal params dict")
            continue
        action = None
        for k, v in zip(params.keys, params.values):
            if isinstance(k, ast.Constant) and k.value == "action":
                action = v
        if not (isinstance(action, ast.Constant) and action.value == "read"):
            problems.append("action is not the literal 'read'")
    if fetches == 0:
        problems.append("no board.bus_get() found")
    return problems


class TheGuardRefusesAWrite(unittest.TestCase):
    def test_an_append_action_is_refused(self):
        src = HEAD + '''
def go(env):
    return board.bus_get(env, {"action": "append", "title": "x", "sheetRow": []})
'''
        self.assertIn("action is not the literal 'read'", guard_verdict(src))

    def test_every_write_shaped_action_is_refused(self):
        for action in ("append", "write", "replace", "delete", "update",
                       "addRow", "post", ""):
            with self.subTest(action=action):
                src = HEAD + '''
def go(env):
    return board.bus_get(env, {"action": "%s", "title": "x"})
''' % action
                self.assertTrue(guard_verdict(src),
                                "action %r was accepted" % action)

    def test_a_missing_action_is_refused(self):
        # The v1 bus DEFAULTS a body with no action to append, so an omitted
        # action is write-like rather than harmless. bus.py says so itself.
        src = HEAD + '''
def go(env):
    return board.bus_get(env, {"title": "x", "match": "claude-code-cli"})
'''
        self.assertIn("action is not the literal 'read'", guard_verdict(src))

    def test_an_action_hidden_behind_a_variable_is_refused(self):
        """The obvious way round the guard, and it must not work."""
        src = HEAD + '''
ACTION = "append"
def go(env):
    return board.bus_get(env, {"action": ACTION, "title": "x"})
'''
        self.assertIn("action is not the literal 'read'", guard_verdict(src))

    def test_params_built_elsewhere_are_refused(self):
        src = HEAD + '''
def go(env, params):
    return board.bus_get(env, params)
'''
        self.assertIn("no literal params dict", guard_verdict(src))

    def test_a_second_call_that_writes_is_caught_even_beside_a_good_read(self):
        """A probe that reads correctly AND writes is the realistic regression."""
        src = HEAD + '''
def go(env):
    ok = board.bus_get(env, {"action": "read", "title": "x"})
    board.bus_get(env, {"action": "append", "title": "x"})
    return ok
'''
        self.assertIn("action is not the literal 'read'", guard_verdict(src))

    def test_any_other_board_function_is_refused(self):
        for call in ("post", "say", "append_row", "bus", "write_row"):
            with self.subTest(call=call):
                src = HEAD + '''
def go(env):
    board.%s(env, {"action": "read"})
    return board.bus_get(env, {"action": "read", "title": "x"})
''' % call
                self.assertIn("board.%s() is not an allowed call" % call,
                              guard_verdict(src))

    def test_a_probe_that_reads_nothing_is_refused(self):
        # A run that reads nothing would exit 0 and prove nothing. Green over
        # zero assertions is worse than red.
        src = HEAD + '''
def go(env):
    return board.load_env()
'''
        self.assertIn("no board.bus_get() found", guard_verdict(src))


class TheGuardAcceptsAHonestRead(unittest.TestCase):
    def test_the_read_the_probe_actually_performs_is_accepted(self):
        src = HEAD + '''
def go(env):
    return board.bus_get(env, {"action": "read", "title": "Blackboard - Alpha DB",
                               "match": "claude-code-cli"})
'''
        self.assertEqual(guard_verdict(src), [])

    def test_prose_about_writing_does_not_fail_the_guard(self):
        """The bug that produced this guard, pinned so it cannot come back.

        Every one of these strings would have tripped a substring scan. None of
        them changes what the program does.
        """
        src = HEAD + '''
MESSAGE = "REFUSING: this probe contains a write-shaped literal"
NOTE = "we never append, write, replace, delete or update the board"
def go(env):
    """This function does not append. It will never post or write a row."""
    # append, write, delete - all mentioned, none performed
    return board.bus_get(env, {"action": "read", "title": "x"})
'''
        self.assertEqual(guard_verdict(src), [],
                         "prose about writing was treated as writing")

    def test_a_retry_loop_around_the_read_is_still_accepted(self):
        src = HEAD + '''
def go(env):
    for i in range(5):
        code, body = board.bus_get(env, {"action": "read", "title": "x"})
        if code == 200:
            return body
    return None
'''
        self.assertEqual(guard_verdict(src), [])


class TheRealProbe(unittest.TestCase):
    def test_the_real_probe_passes_its_own_guard(self):
        """Closes the gap left by re-implementing the guard in this file."""
        self.assertTrue(PROBE.is_file(), "%s is missing" % PROBE)
        self.assertEqual(guard_verdict(PROBE.read_text(encoding="utf-8")), [])

    def test_the_real_probe_still_carries_the_guard_and_calls_it_first(self):
        """A guard that exists but is never called is decoration."""
        tree = ast.parse(PROBE.read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        self.assertIn("refuse_if_this_module_can_write", names)

        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        first = main.body[0]
        self.assertTrue(
            isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
            and getattr(first.value.func, "id", "") ==
            "refuse_if_this_module_can_write",
            "the guard is not the first statement in main()")

    def test_the_probe_needs_no_third_party_import(self):
        src = PROBE.read_text(encoding="utf-8")
        for banned in ("import requests", "from requests", "import httpx",
                       "google.cloud"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    os.chdir(REPO)
    unittest.main(verbosity=2)

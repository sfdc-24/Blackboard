#!/usr/bin/env python3
"""The consultation log filed a route header as gemini's opinion.

On 2026-09-20 `consult.py ask --to gemini` produced a record whose entire body
was `[api-key (GEMINI_API_KEY)]` — the adapter's route header. Nothing errored.
The index counted it, the verdict block invited a score, and the record asserted
that gemini had been asked a 2,979-byte architecture question and had replied
with nothing. The same question asked in-process returned 8,657 characters, so
neither the model nor the question was at fault: the question had been passed as
an argv element through a Windows command line.

`ask_grok`, twenty lines above in the same file, already wrote the question to a
file, passed `--file`, and read the answer off disk instead of out of a console
— with a docstring explaining that parsing a console when the data is on disk is
a decision to read the least reliable copy. The gemini path made both mistakes
that paragraph warns about.

Passing the question by --file fixed that half and exposed the other: gemini's
answer contained U+2502 and print() to a cp1252 console raised
UnicodeEncodeError partway through, so the subprocess died mid-answer. The
console was the problem both times, so it is gone: the adapter is now called
in-process.

Two things are asserted here: no console anywhere in the path, and a blank never
becomes a record.

Run: python tests/test_consult_never_files_a_blank.py
"""

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import consult  # noqa: E402


class NoConsoleInThePath(unittest.TestCase):
    """Both failures were the console. Assert it is gone."""

    def test_gemini_is_called_in_process_not_through_a_subprocess(self):
        called = {}

        class FakeAdapter:
            @staticmethod
            def ask(prompt):
                called["prompt"] = prompt
                return ("a real answer, long enough to be one", "api-key (X)")

        def explode(*a, **k):
            raise AssertionError("consult must not shell out for gemini any more")

        real_run = consult.subprocess.run
        consult.subprocess.run = explode
        sys.modules["gemini_agent"] = FakeAdapter
        try:
            body, how = consult.ask_gemini("x" * 3000)
        finally:
            consult.subprocess.run = real_run
            del sys.modules["gemini_agent"]

        self.assertEqual(called["prompt"], "x" * 3000,
                         "the whole question must reach the adapter intact")
        self.assertIn("in-process", how)
        self.assertEqual(body, "a real answer, long enough to be one")

    def test_an_answer_with_characters_a_cp1252_console_cannot_print(self):
        """U+2502 killed the subprocess mid-answer. In-process it is just text."""
        box = "before " + chr(0x2502) + " after " + chr(0x2192) + " end"

        class FakeAdapter:
            @staticmethod
            def ask(prompt):
                return (box + " and enough more text to clear the floor", "api-key (X)")

        sys.modules["gemini_agent"] = FakeAdapter
        try:
            body, _ = consult.ask_gemini("a question")
        finally:
            del sys.modules["gemini_agent"]
        self.assertIn(chr(0x2502), body)
        self.assertIn(chr(0x2192), body)


class ABlankIsNeverARecord(unittest.TestCase):
    def test_the_floor_is_low_enough_to_only_catch_nothing(self):
        """It is there to catch a header, not to judge brevity."""
        self.assertLessEqual(consult.MIN_ANSWER_CHARS, 80)
        self.assertGreater(consult.MIN_ANSWER_CHARS, len("[api-key (GEMINI_API_KEY)]"))

    def test_an_empty_answer_writes_no_file_and_fails(self):
        written = []
        consult_write = consult.write_record
        consult.write_record = lambda *a, **k: written.append(a) or "SHOULD-NOT-HAPPEN"

        class Args:
            to = "gemini"
            subject = "a subject"
            thread = "site"
            my_position = ""
            file = None
            question = "a question long enough to be real, several words over"

        real_askers = dict(consult.ASKERS)
        consult.ASKERS["gemini"] = lambda q: ("   ", "gemini via a fake")
        try:
            rc = consult.cmd_ask(Args())
        finally:
            consult.write_record = consult_write
            consult.ASKERS.clear()
            consult.ASKERS.update(real_askers)

        self.assertNotEqual(rc, 0, "an empty answer must not exit 0")
        self.assertEqual(written, [], "and must not write a record")


if __name__ == "__main__":
    unittest.main(verbosity=2)

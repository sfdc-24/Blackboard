#!/usr/bin/env python3
"""Does foundry_agent.host() actually read .env, or silently use its constant?

WHY THIS FILE EXISTS
  On 2026-09-17 the resource layout in Azure changed, so foundry_agent stopped
  hardcoding its host and started deriving it from FOUNDRY_PROJECT_ENDPOINT.
  Verifying that took three attempts, and the first two proved nothing:

    1. Printed host() and DEFAULT_HOST side by side. They were IDENTICAL, because
       the .env value and the constant name the same resource. A passing read and
       a silent fallback are indistinguishable in that comparison.
    2. Set FOUNDRY_PROJECT_ENDPOINT in the process environment to a bogus host
       and called host(). It returned the real host — which looked like a failure
       but was not: load_env() reads .env FIRST and .env wins by design. The test
       was structurally incapable of moving the value it was testing.

  Both were tests that could not fail. That is the same defect as a browser gate
  reporting "0 tests in 0 files" — it runs, it is green, it checks nothing.

  The only test that can fail is one where a correct read and a fallback give
  DIFFERENT answers: point the module's ENV at a file naming a different host.

  Run:  python tests/test_foundry_host.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import foundry_agent as fa  # noqa: E402

OTHER = "https://not-the-real-host.services.ai.azure.com"


class HostIsReadFromEnvFile(unittest.TestCase):
    def setUp(self):
        self.real_env = fa.ENV
        fd, self.tmp = tempfile.mkstemp(suffix=".env", text=True)
        os.close(fd)

    def tearDown(self):
        fa.ENV = self.real_env
        try:
            os.unlink(self.tmp)
        except OSError:
            pass

    def _write(self, body):
        with open(self.tmp, "w", encoding="utf-8") as f:
            f.write(body)
        fa.ENV = self.tmp

    def test_a_different_file_gives_a_different_host(self):
        """The test the first two attempts could not be: the answer MOVES."""
        self._write("FOUNDRY_PROJECT_ENDPOINT=%s/api/projects/zzz\n" % OTHER)
        got = fa.host()
        self.assertEqual(got, OTHER,
                         "host() returned %r. If that equals DEFAULT_HOST (%r), the "
                         "file is not being read at all and .env is not authoritative."
                         % (got, fa.DEFAULT_HOST))
        self.assertNotEqual(got, fa.DEFAULT_HOST,
                            "host() matched the hardcoded constant, so this test "
                            "cannot distinguish a read from a fallback.")

    def test_the_project_path_is_stripped_to_an_origin(self):
        """FOUNDRY_PROJECT_ENDPOINT is the project URL; inference hangs off its
        ORIGIN. Carrying /api/projects/<name> through would build
        .../api/projects/x/anthropic/v1/messages and 404."""
        self._write("FOUNDRY_PROJECT_ENDPOINT=%s/api/projects/abdus-2123\n" % OTHER)
        got = fa.host()
        self.assertNotIn("/api/", got)
        self.assertNotIn("projects", got)
        self.assertTrue((got + fa.ANTHROPIC_PATH).endswith("/anthropic/v1/messages"))

    def test_a_trailing_slash_or_whitespace_does_not_leak(self):
        self._write("FOUNDRY_PROJECT_ENDPOINT=  %s/  \n" % OTHER)
        self.assertEqual(fa.host(), OTHER)

    def test_falls_back_to_the_constant_when_the_key_is_absent(self):
        """Absent is different from wrong. With no endpoint recorded, the
        constant is the documented fallback and must still be returned."""
        self._write("SOMETHING_ELSE=1\n")
        # The process environment must not rescue it either, or this asserts nothing.
        os.environ.pop("FOUNDRY_PROJECT_ENDPOINT", None)
        self.assertEqual(fa.host(), fa.DEFAULT_HOST)

    def test_a_malformed_endpoint_falls_back_rather_than_building_a_bad_url(self):
        self._write("FOUNDRY_PROJECT_ENDPOINT=not-a-url-at-all\n")
        os.environ.pop("FOUNDRY_PROJECT_ENDPOINT", None)
        self.assertEqual(fa.host(), fa.DEFAULT_HOST)


class TheRouteChoiceIsByModelName(unittest.TestCase):
    """Cross the two protocols and the resource answers 401 or 404 with no hint
    which of the six things was wrong. Pin the branch."""

    def test_claude_models_take_the_anthropic_route(self):
        self.assertTrue(fa.is_anthropic("claude-opus-5"))
        self.assertTrue(fa.is_anthropic("CLAUDE-opus-5"))

    def test_everything_else_takes_the_openai_route(self):
        self.assertFalse(fa.is_anthropic("gpt-4o"))
        self.assertFalse(fa.is_anthropic("text-embedding-3-large"))
        self.assertFalse(fa.is_anthropic(""))
        self.assertFalse(fa.is_anthropic(None))


class AnEmptyAnswerIsAFailure(unittest.TestCase):
    """A 200 carrying no prose reads as success and carries nothing. It is how
    the first claude-opus-5 probe reported 24 output tokens and printed an empty
    string, having read content[0] — a thinking block."""

    def test_thinking_only_returns_none_not_empty_string(self):
        self.assertIsNone(fa.extract_anthropic({"content": [{"type": "thinking", "thinking": "..."}]}))

    def test_text_is_found_after_a_thinking_block(self):
        d = {"content": [{"type": "thinking", "thinking": "..."},
                         {"type": "text", "text": "opus online"}]}
        self.assertEqual(fa.extract_anthropic(d), "opus online")

    def test_no_content_at_all_returns_none(self):
        self.assertIsNone(fa.extract_anthropic({"content": []}))
        self.assertIsNone(fa.extract_anthropic({}))

    def test_whitespace_only_text_is_not_an_answer(self):
        self.assertIsNone(fa.extract_anthropic({"content": [{"type": "text", "text": "   \n "}]}))

    def test_openai_empty_content_returns_none(self):
        self.assertIsNone(fa.extract_openai({"choices": [{"message": {"content": ""}}]}))
        self.assertIsNone(fa.extract_openai({"choices": []}))


if __name__ == "__main__":
    unittest.main(verbosity=2)

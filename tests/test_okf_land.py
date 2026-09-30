"""Tests for scripts/okf_land.py ? Gemini signed OKF landing (no network)."""
from __future__ import annotations

import unittest
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(REPO / "scripts"))
import okf_land  # noqa: E402


class WantsOkf(unittest.TestCase):
    def test_signed_okf(self):
        self.assertTrue(okf_land.wants_okf("write signed OKF RESULT file"))

    def test_plain_ask(self):
        self.assertFalse(okf_land.wants_okf("review the architecture briefly"))

    def test_a_row_that_only_cites_an_okf_path_does_not_land(self):
        # Most RESULT rows cite a file like this; each would open a public PR.
        for text in ("RESULT okf=docs/okf/gemini/x.md", "see docs/okf/calls/next.md",
                     "proof RESULT with OKF path+SHA", "okf pr #304 merged"):
            self.assertFalse(okf_land.wants_okf(text), text)

    def test_explicit_asks_land(self):
        for text in ("Gemini: land OKF with your topics", "write OKF notes", "OKF file please"):
            self.assertTrue(okf_land.wants_okf(text), text)


class PathGuard(unittest.TestCase):
    def test_safe_path(self):
        self.assertEqual(okf_land._safe_path("GEM-FIX-1"), "docs/okf/gemini/GEM-FIX-1.md")

    def test_rejects_traversal(self):
        with self.assertRaises(ValueError):
            okf_land._safe_path("../secrets")


class LandWithoutToken(unittest.TestCase):
    def test_skips_cleanly(self):
        out = okf_land.land(
            answers_id="GEM-FIX-GCC-20260930T0225Z",
            ask_text="BCB|v=1|id=GEM-FIX-GCC-20260930T0225Z|phase=DISPATCH|signed OKF",
            reply_body="root cause: no write token",
            route="test",
            env={},
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["skipped"], "no GEMINI_OKF_WRITE_TOKEN or GEMINI_GITHUB_TOKEN")
        self.assertTrue(out["path"].startswith("docs/okf/gemini/"))


class LandWithFakeHttp(unittest.TestCase):
    def test_lands_and_opens_pr(self):
        calls = []

        def http(method, path, token, payload=None, timeout=30):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/git/ref/heads/main"):
                return {"object": {"sha": "abc123"}}
            if method == "POST" and path.endswith("/git/refs"):
                return {}
            if method == "GET" and "/contents/" in path:
                raise urllib.error.HTTPError(path, 404, "no", hdrs=None, fp=None)
            if method == "PUT" and "/contents/" in path:
                return {"content": {"html_url": "https://github.com/sfdc-24/Blackboard/blob/x/docs/okf/gemini/f.md"}}
            if method == "GET" and "/pulls?" in path:
                return []
            if method == "POST" and path.endswith("/pulls"):
                return {"html_url": "https://github.com/sfdc-24/Blackboard/pull/999"}
            raise AssertionError("unexpected %s %s" % (method, path))

        out = okf_land.land(
            answers_id="GEM-FIX-GCC-20260930T0225Z",
            ask_text="BCB|v=1|id=GEM-FIX-GCC-20260930T0225Z|phase=DISPATCH|signed OKF",
            reply_body="diagnosis complete",
            route="fake",
            env={"GEMINI_OKF_WRITE_TOKEN": "tok"},
            http=http,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["pr"], "https://github.com/sfdc-24/Blackboard/pull/999")
        self.assertTrue(out["path"].startswith("docs/okf/gemini/"))
        methods = [c[0] for c in calls]
        self.assertIn("PUT", methods)
        self.assertIn("POST", methods)


class PublicFileCarriesNoPrivateText(unittest.TestCase):
    """The board is private and Blackboard is public (review on #304, 2026-09-30)."""

    ROW = ("BCB|v=1|id=GEM-X-1|phase=DISPATCH|to=gemini|land OKF. Client deal for "
           "jane.doe@example.com, call 647-555-0199, key ghp_ABCDEFGHIJKLMNOPQRSTUV12")

    def test_the_row_is_never_copied_only_its_id(self):
        md = okf_land.render_okf(answers_id="GEM-X-1", ask_text=self.ROW,
                                 reply_body="Three topics for the call.", route="t")
        self.assertIn("GEM-X-1", md)
        self.assertNotIn("Client deal", md)
        self.assertNotIn("```", md)

    def test_the_reply_is_scrubbed(self):
        md = okf_land.render_okf(answers_id="GEM-X-1", ask_text=self.ROW,
                                 reply_body="Echo: jane.doe@example.com 647-555-0199 "
                                            "ghp_ABCDEFGHIJKLMNOPQRSTUV12 https://x.test/a?sig=abc",
                                 route="t")
        for leaked in ("jane.doe@example.com", "647-555-0199", "ghp_ABCDEFGHIJKLMNOPQRSTUV12", "sig=abc"):
            self.assertNotIn(leaked, md)
        self.assertIn("https://x.test/a", md)

    def test_ordinary_numbers_survive(self):
        text = "PR #304 at 2fc40ff, 2026-09-30T04:13:07Z, 150 s, v1.2.3, 45 minutes"
        self.assertEqual(text, okf_land.scrub(text))


class TokenAlias(unittest.TestCase):
    def test_the_mounted_github_token_is_used_when_no_write_token(self):
        # 2026-09-30 04:13Z: the owner mounted github-token-gemini-okf as GEMINI_GITHUB_TOKEN.
        seen = []

        def http(method, path, token, payload=None, timeout=30):
            seen.append(token)
            raise urllib.error.HTTPError(path, 403, "no", hdrs=None, fp=None)

        out = okf_land.land(answers_id="GEM-X-2", ask_text="land OKF", reply_body="r",
                            env={"GEMINI_GITHUB_TOKEN": "mounted"}, http=http)
        self.assertEqual(["mounted"], seen)
        # A token that cannot write is the exact blocker, not a skip.
        self.assertEqual({"ok": False, "error": "HTTP 403", "path": "docs/okf/gemini/GEM-X-2.md"}, out)

    def test_the_write_token_wins(self):
        self.assertEqual(("w", "GEMINI_OKF_WRITE_TOKEN"),
                         okf_land.token_from({"GEMINI_OKF_WRITE_TOKEN": "w", "GEMINI_GITHUB_TOKEN": "r"}))


class ImageCarriesOkfLand(unittest.TestCase):
    def test_dockerfile_and_gcloudignore(self):
        import re
        docker = (REPO / "cloud" / "agent-waker" / "Dockerfile").read_text(encoding="utf-8")
        copied = set(re.findall(r"scripts/([A-Za-z_]+)\.py", docker))
        self.assertIn("okf_land", copied)
        allow = (REPO / ".gcloudignore").read_text(encoding="utf-8")
        self.assertIn("!/scripts/okf_land.py", allow)
        source = (REPO / "scripts" / "agent_waker.py").read_text(encoding="utf-8")
        self.assertIn("import okf_land", source)
        self.assertIn('"okf_land": True', source)


if __name__ == "__main__":
    unittest.main()

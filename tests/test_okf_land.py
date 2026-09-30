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
        self.assertEqual(out["skipped"], "no GEMINI_OKF_WRITE_TOKEN")
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

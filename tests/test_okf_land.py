"""Tests for scripts/okf_land.py: Gemini's OKF writes, into the private conference repository only (no network)."""
from __future__ import annotations

import base64
import sys
import unittest
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import okf_land  # noqa: E402

ASK = "BCB|v=1|id=GEM-X-1|phase=DISPATCH|from=grok|to=gemini|land=okf|write your topic"
CALL = "2026-09-30 15:00Z (11:00 Toronto)"
PLAN = "---\ntype: call-plan\ntitle: T\ncall: %s\n---\n# Objective\nx\n" % CALL


def fake_github(private=True, pr_fails=None, prs=(), pr_reply=None, plan=PLAN):
    """A GitHub stand-in: records every call; `pr_fails` is the HTTP status for opening the PR."""
    calls = []

    def http(method, path, token, payload=None, timeout=30):
        calls.append((method, path, payload, token))
        if method == "GET" and path == "/repos/sfdc-24/conference":
            return {"private": private}
        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "abc123"}}
        if method == "POST" and path.endswith("/git/refs"):
            return {}
        if method == "GET" and path.endswith("/contents/docs/okf/calls/next.md?ref=main"):
            if plan is None:
                raise urllib.error.HTTPError(path, 404, "no", hdrs=None, fp=None)
            return {"content": base64.b64encode(plan.encode("utf-8")).decode("ascii")}
        if method == "GET" and "/contents/" in path:
            raise urllib.error.HTTPError(path, 404, "no", hdrs=None, fp=None)
        if method == "PUT" and "/contents/" in path:
            return {"content": {"html_url": "https://github.com/sfdc-24/conference/blob/x/f.md"}}
        if method == "GET" and "/pulls?" in path:
            return list(prs)
        if method == "POST" and path.endswith("/pulls"):
            if pr_fails:
                raise urllib.error.HTTPError(path, pr_fails, "no", hdrs=None, fp=None)
            return pr_reply if pr_reply is not None else {"html_url": "https://github.com/sfdc-24/conference/pull/999"}
        raise AssertionError("unexpected %s %s" % (method, path))

    return http, calls


def written(calls) -> str:
    put = [c for c in calls if c[0] == "PUT"]
    return base64.b64decode(put[0][2]["content"]).decode("utf-8") if put else ""


class Trigger(unittest.TestCase):
    """Codex on 5e2a4cb: prose is not an authorised request. Only the structured field starts a write."""

    def test_the_structured_field_starts_it(self):
        self.assertTrue(okf_land.wants_okf(ASK))
        self.assertTrue(okf_land.wants_okf("BCB|v=1|id=X|land=OKF|file=call-notes"))

    def test_prose_never_does(self):
        for text in ("Do not write OKF; just summarize", "please land okf now", "signed OKF RESULT file",
                     "BCB|v=1|id=X|text=someone said land=okf", "RESULT okf=https://x/pull/1",
                     "BCB|v=1|id=X|land=okfx", "BCB|v=1|id=X|land=no"):
            self.assertFalse(okf_land.wants_okf(text), text)

    def test_the_first_value_wins(self):
        self.assertFalse(okf_land.wants_okf("BCB|v=1|id=X|land=no|land=okf"))


class Destination(unittest.TestCase):
    def test_only_the_private_conference_repository(self):
        http, calls = fake_github()
        out = okf_land.land(answers_id="GEM-X-1", ask_text=ASK, reply_body="Three changes.",
                            env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)
        self.assertTrue(out["ok"], out)
        self.assertTrue(all(c[1].startswith("/repos/sfdc-24/conference") for c in calls), calls)
        self.assertEqual("docs/okf/gemini/GEM-X-1.md", out["path"])

    def test_a_repository_that_is_not_private_is_refused_before_any_write(self):
        for private in (False, None):
            http, calls = fake_github(private=private)
            out = okf_land.land(answers_id="GEM-X-1", ask_text=ASK, reply_body="r",
                                env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)
            self.assertFalse(out["ok"])
            self.assertIn("not private", out["error"])
            self.assertEqual([("GET", "/repos/sfdc-24/conference")], [(c[0], c[1]) for c in calls])

    def test_call_notes_go_to_the_notes_file(self):
        http, calls = fake_github()
        out = okf_land.land(answers_id="GEM-N-1", ask_text="BCB|v=1|id=GEM-N-1|land=okf|file=call-notes",
                            reply_body="I will propose three changes.", env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)
        self.assertTrue(out["ok"], out)
        self.assertEqual("docs/okf/calls/notes/gemini.md", out["path"])
        md = written(calls)
        self.assertIn("type: call-notes", md)
        self.assertIn("status: ready", md)
        self.assertIn("agent: gemini", md)
        self.assertIn("call: %s" % CALL, md)             # the plan's own call, so the chair gives it
        self.assertIn("I will propose three changes.", md)

    def test_notes_for_a_plan_that_names_no_call_are_not_landed(self):
        notes = "BCB|v=1|id=GEM-N-2|land=okf|file=call-notes"
        for plan, why in ((PLAN.replace("call: %s\n" % CALL, ""), "names no call"),
                          (PLAN.replace("call:", "call: a\ncall:"), "names no call"),     # two calls: ambiguous
                          (None, "could not read the plan's call (404)")):
            http, calls = fake_github(plan=plan)
            out = okf_land.land(answers_id="GEM-N-2", ask_text=notes, reply_body="r",
                                env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)
            self.assertFalse(out["ok"])
            self.assertIn(why, out["error"])
            self.assertFalse(any(c[0] in ("PUT", "POST") for c in calls), calls)

    def test_a_dropped_connection_or_timeout_on_the_plan_is_not_landed_and_does_not_raise(self):
        # Cursor on 7f4174a: a URLError or TimeoutError escaped land() and would kill the waker's pass.
        for error in (urllib.error.URLError("connection reset"), TimeoutError("timed out"), OSError("socket")):
            calls = []

            def http(method, path, token, payload=None, timeout=30, error=error):
                calls.append((method, path))
                raise error

            out = okf_land.land(answers_id="GEM-N-3", ask_text="BCB|v=1|id=GEM-N-3|land=okf|file=call-notes",
                                reply_body="r", env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)
            self.assertFalse(out["ok"])
            self.assertIn("could not read the plan's call (%s)" % type(error).__name__, out["error"])
            self.assertFalse(any(m in ("PUT", "POST") for m, _ in calls), calls)

    def test_a_result_file_needs_no_plan(self):
        http, calls = fake_github(plan=None)
        self.assertTrue(okf_land.land(answers_id="GEM-X-1", ask_text=ASK, reply_body="r",
                                      env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)["ok"])

    def test_plan_call_reads_one_call_from_the_front_matter(self):
        self.assertEqual(CALL, okf_land.plan_call(PLAN))
        self.assertEqual(CALL, okf_land.plan_call(PLAN.replace("\n", "\r\n")))
        self.assertEqual("", okf_land.plan_call("# no front matter\ncall: x\n"))
        self.assertEqual("", okf_land.plan_call("---\ncall:\n---\n"))

    def test_paths_stay_in_the_lane(self):
        with self.assertRaises(ValueError):
            okf_land._safe_path("a/../../b")
        self.assertEqual("docs/okf/gemini/secrets.md", okf_land._safe_path("../secrets"))


class VisibilityFlip(unittest.TestCase):
    """Codex on 6d0ba6f: privacy is asked again right before each write, not only at the start."""

    def flips_after(self, private_answers):
        answers = list(private_answers)
        base, calls = fake_github()

        def http(method, path, token, payload=None, timeout=30):
            if method == "GET" and path == "/repos/sfdc-24/conference":
                calls.append((method, path, payload, token))
                return {"private": answers.pop(0) if answers else False}
            return base(method, path, token, payload, timeout)

        out = okf_land.land(answers_id="GEM-X-1", ask_text=ASK, reply_body="secret plan",
                            env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)
        return out, [c[0] + " " + c[1] for c in calls]

    def test_public_after_the_first_check_means_no_branch_and_no_file(self):
        out, calls = self.flips_after([True])
        self.assertFalse(out["ok"])
        self.assertIn("not private", out["error"])
        self.assertFalse(any(c.startswith(("PUT ", "POST ")) for c in calls), calls)

    def test_public_right_before_the_file_means_no_file(self):
        out, calls = self.flips_after([True, True])
        self.assertFalse(out["ok"])
        self.assertFalse(any(c.startswith("PUT ") for c in calls), calls)

    def test_public_right_before_the_pull_request_means_none_is_opened(self):
        out, calls = self.flips_after([True, True, True])
        self.assertFalse(out["ok"])
        self.assertFalse(any(c.startswith("POST ") and c.endswith("/pulls") for c in calls), calls)

    def test_private_throughout_is_checked_four_times(self):
        out, calls = self.flips_after([True, True, True, True])
        self.assertTrue(out["ok"], out)
        self.assertEqual(4, calls.count("GET /repos/sfdc-24/conference"))


class ReviewGate(unittest.TestCase):
    """Codex on 5e2a4cb: a file written without a pull request is not landed."""

    def test_a_pr_that_fails_to_open_is_not_landed(self):
        for status in (403, 422):
            http, _ = fake_github(pr_fails=status)
            out = okf_land.land(answers_id="GEM-X-1", ask_text=ASK, reply_body="r",
                                env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)
            self.assertFalse(out["ok"])
            self.assertNotIn("pr", out)
            self.assertIn("HTTP %d while opening the pull request" % status, out["error"])

    def test_a_pr_reply_without_a_link_is_not_landed(self):
        http, _ = fake_github(pr_reply={})
        out = okf_land.land(answers_id="GEM-X-1", ask_text=ASK, reply_body="r",
                            env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)
        self.assertFalse(out["ok"])
        self.assertIn("no pull request was opened", out["error"])

    def test_an_open_pr_for_the_branch_is_reused(self):
        http, calls = fake_github(prs=[{"html_url": "https://github.com/sfdc-24/conference/pull/7"}])
        out = okf_land.land(answers_id="GEM-X-1", ask_text=ASK, reply_body="r",
                            env={"GEMINI_GITHUB_TOKEN": "t"}, http=http)
        self.assertEqual("https://github.com/sfdc-24/conference/pull/7", out["pr"])
        self.assertFalse(any(c[0] == "POST" and c[1].endswith("/pulls") for c in calls))

    def test_a_token_that_cannot_see_the_repository_is_the_exact_blocker(self):
        def http(method, path, token, payload=None, timeout=30):
            raise urllib.error.HTTPError(path, 404, "no", hdrs=None, fp=None)

        out = okf_land.land(answers_id="GEM-X-2", ask_text=ASK, reply_body="r",
                            env={"GEMINI_GITHUB_TOKEN": "mounted"}, http=http)
        self.assertFalse(out["ok"])
        self.assertEqual("HTTP 404 while reading sfdc-24/conference", out["error"])


class Content(unittest.TestCase):
    ROW = ASK + " Client deal for jane.doe@example.com, codeword Cedar Dawn"

    def test_the_row_is_never_copied_only_its_id(self):
        md = okf_land.render_okf(answers_id="GEM-X-1", ask_text=self.ROW, reply_body="Three topics.", route="t")
        self.assertIn("GEM-X-1", md)
        self.assertNotIn("Cedar Dawn", md)
        self.assertNotIn("Client deal", md)

    def test_keys_and_contact_details_are_scrubbed(self):
        md = okf_land.render_okf(answers_id="GEM-X-1", ask_text=ASK, route="t",
                                 reply_body="Echo: jane.doe@example.com 647-555-0199 "
                                            "ghp_ABCDEFGHIJKLMNOPQRSTUV12 https://x.test/a?sig=abc")
        for leaked in ("jane.doe@example.com", "647-555-0199", "ghp_ABCDEFGHIJKLMNOPQRSTUV12", "sig=abc"):
            self.assertNotIn(leaked, md)
        self.assertIn("https://x.test/a", md)

    def test_ordinary_numbers_survive(self):
        text = "PR #304 at 2fc40ff, 2026-09-30T04:13:07Z, 150 s, v1.2.3, 45 minutes"
        self.assertEqual(text, okf_land.scrub(text))


class Token(unittest.TestCase):
    def test_no_token_skips(self):
        out = okf_land.land(answers_id="GEM-X-1", ask_text=ASK, reply_body="r", env={})
        self.assertEqual({"ok": False, "skipped": "no GEMINI_OKF_WRITE_TOKEN or GEMINI_GITHUB_TOKEN",
                          "path": "docs/okf/gemini/GEM-X-1.md"}, out)

    def test_the_mounted_token_is_used_and_the_write_token_wins(self):
        # 2026-09-30 04:13Z: the owner mounted github-token-gemini-okf as GEMINI_GITHUB_TOKEN.
        http, calls = fake_github()
        okf_land.land(answers_id="GEM-X-1", ask_text=ASK, reply_body="r",
                      env={"GEMINI_GITHUB_TOKEN": "mounted"}, http=http)
        self.assertEqual({"mounted"}, {c[3] for c in calls})
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

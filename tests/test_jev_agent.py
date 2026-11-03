"""Jev (TypeSafe System One): typed decisions on the board, never text. Synthetic only; no network."""
import json
import sys
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_waker  # noqa: E402
import jev_agent  # noqa: E402


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TheSpec(unittest.TestCase):
    def test_score_choice_and_yesno_parse(self):
        q, found = jev_agent.parse_spec("context first. jev:score:0..1500: average payload size?")
        self.assertEqual((True, "score", 0, 1500), (found, q["type"], q["min"], q["max"]))
        q, found = jev_agent.parse_spec("jev:choice:(ship; hold; escalate): what should happen?")
        self.assertEqual((True, "choice", ["ship", "hold", "escalate"]), (found, q["type"], q["options"]))
        q, found = jev_agent.parse_spec("jev:yesno: is this row malformed?")
        self.assertEqual((True, "yesno"), (found, q["type"]))

    def test_no_spec_falls_back_to_the_scope_question(self):
        q, found = jev_agent.parse_spec("please compute the average of the last 50 rows")
        self.assertFalse(found)
        self.assertEqual("yesno", q["type"])
        self.assertIn("typed decision", q["question"])

    def test_a_bad_range_or_too_many_options_is_not_a_spec(self):
        q, found = jev_agent.parse_spec("jev:score:9..3: backwards?")
        self.assertFalse(found)
        many = "; ".join("o%d" % n for n in range(300))
        q, found = jev_agent.parse_spec("jev:choice:(%s): which?" % many)
        self.assertFalse(found)

    def test_the_context_is_capped(self):
        q, _found = jev_agent.parse_spec("x" * (jev_agent.CONTEXT_CHARS + 5000))
        self.assertLessEqual(len(q.get("context", "")), jev_agent.CONTEXT_CHARS)


class TheCall(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict("os.environ", {"JEV_API_KEY": "k-test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_a_decision_with_probability_becomes_the_reply(self):
        sent = {}

        def fake(request, timeout=None):
            sent["url"] = request.full_url
            sent["body"] = json.loads(request.data.decode("utf-8"))
            sent["auth"] = request.get_header("Authorization")
            return FakeResponse({"decision": {"answer": "yes", "probability": 0.91}})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, route = jev_agent.ask("jev:yesno: is the board healthy?")
        self.assertEqual("systemone api-key", route)
        self.assertIn("Jev decision (yesno): yes", text)
        self.assertIn("0.910", text)
        self.assertEqual(jev_agent.SYSTEMONE, sent["url"])
        self.assertEqual("Bearer k-test", sent["auth"])
        self.assertEqual("yesno", sent["body"]["question"]["type"])
        self.assertNotIn("|", text, "a reply must never carry the board's field separator")

    def test_a_score_reply_names_its_range(self):
        def fake(request, timeout=None):
            return FakeResponse({"score": 212, "confidence": 0.62})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, route = jev_agent.ask("jev:score:0..1500: average payload chars?")
        self.assertIn("Jev decision (score): 212", text)
        self.assertIn("range 0..1500", text)

    def test_no_spec_still_answers_and_teaches_the_shape(self):
        def fake(request, timeout=None):
            return FakeResponse({"answer": "no", "p": 0.8})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, _route = jev_agent.ask("average the last 50 rows please")
        self.assertIn("jev:score", text, "the usage hint rides along")

    def test_an_unknown_shape_is_a_loud_failure_naming_keys_only(self):
        def fake(request, timeout=None):
            return FakeResponse({"outputs": [1], "trace": "x"})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, reason = jev_agent.ask("jev:yesno: q?")
        self.assertIsNone(text)
        self.assertIn("shape v0 mismatch", reason)
        self.assertIn("outputs", reason)
        self.assertNotIn("k-test", reason)

    def test_http_errors_and_a_missing_key_fail_closed(self):
        def fake(request, timeout=None):
            raise urllib.error.HTTPError(jev_agent.SYSTEMONE, 401, "nope", {}, BytesIO(b"bad token"))
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, reason = jev_agent.ask("jev:yesno: q?")
        self.assertIsNone(text)
        self.assertIn("HTTP 401", reason)
        with mock.patch.dict("os.environ", {"JEV_API_KEY": ""}):
            with mock.patch.object(jev_agent, "ENV", "does-not-exist"):
                text, reason = jev_agent.ask("jev:yesno: q?")
        self.assertIsNone(text)
        self.assertIn("no JEV_API_KEY", reason)


class TheRegistry(unittest.TestCase):
    def test_jev_is_registered_with_a_wake_filter_and_no_context_attachments(self):
        entry = agent_waker.AGENTS["jev"]
        self.assertEqual("jev_agent", entry["module"])
        self.assertTrue(entry["wake_filter"])
        self.assertFalse(entry.get("repo_context", False), "data boundary: board rows only")
        self.assertFalse(entry.get("cloud_context", False), "data boundary: board rows only")

    def test_jev_wakes_for_an_ask_and_never_for_a_result(self):
        ask_row = ["R1", "2026-11-03T19:00:00Z", "claude-code-cli", "jev", "APPEND",
                   "BCB|v=1|id=X|phase=REQUEST|to=jev|text=jev:yesno: q?"]
        done_row = ["R2", "2026-11-03T19:00:00Z", "claude-code-cli", "jev", "APPEND",
                    "BCB|v=1|id=Y|phase=RESULT|to=jev|text=done"]
        self.assertTrue(agent_waker.wakes_for(ask_row, "jev"))
        self.assertFalse(agent_waker.wakes_for(done_row, "jev"))


if __name__ == "__main__":
    unittest.main()

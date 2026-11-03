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


class TheWakerPrompt(unittest.TestCase):
    def test_only_the_row_is_parsed_and_sent_never_the_doctrine(self):
        cfg = agent_waker.AGENTS["jev"]
        row = "BCB|v=1|id=X|phase=ASK|to=jev|text=jev:yesno: is the CI green?"
        prompt = (cfg["doctrine"] + "\n\nThis board row is addressed to you by grok"
                  + ". Answer it.\n\n---\n" + row + "\n---")
        self.assertIn("jev:choice:(a; b; c)", cfg["doctrine"], "the trap this test guards")
        self.assertEqual(row, jev_agent.row_text(prompt))
        q, found = jev_agent.parse_spec(jev_agent.row_text(prompt))
        self.assertEqual((True, "yesno", "is the CI green?"), (found, q["type"], q["question"]))
        self.assertNotIn("TypeSafe", q["context"])


class TheCall(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict("os.environ", {"JEV_API_KEY": "k-test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_a_noul_answer_becomes_a_yes_with_its_probability(self):
        sent = {}

        def fake(request, timeout=None):
            sent["url"] = request.full_url
            sent["body"] = json.loads(request.data.decode("utf-8"))
            sent["auth"] = request.get_header("Authorization")
            # the exact shape the live API returned on 2026-11-03
            return FakeResponse({"model": "jev-1.14.0",
                                 "answers": {"q": {"type": "noul", "noul": 0.98}},
                                 "usage": {"input_tokens": 358, "output_tokens": 63}})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, route = jev_agent.ask("CI went green. jev:yesno: is the board healthy?")
        self.assertEqual("systemone api-key", route)
        self.assertIn("Jev decision (yesno): yes", text)
        self.assertIn("probability 0.980", text)
        self.assertIn("model jev-1.14.0", text)
        self.assertEqual(jev_agent.SYSTEMONE, sent["url"])
        self.assertEqual("Bearer k-test", sent["auth"])
        self.assertEqual("jev-latest", sent["body"]["model"])
        self.assertIn("CI went green", sent["body"]["state"])
        self.assertEqual({"type": "noul", "instructions": "is the board healthy?"},
                         sent["body"]["questions"]["q"])
        self.assertNotIn("|", text, "a reply must never carry the board's field separator")

    def test_a_low_noul_is_a_no_with_the_no_probability(self):
        def fake(request, timeout=None):
            return FakeResponse({"answers": {"q": {"type": "noul", "noul": 0.1}}})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, _route = jev_agent.ask("jev:yesno: q?")
        self.assertIn("Jev decision (yesno): no", text)
        self.assertIn("probability 0.900", text)

    def test_a_choice_sends_named_criteria_and_reads_the_choice(self):
        sent = {}

        def fake(request, timeout=None):
            sent["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({"answers": {"q": {"type": "choice", "choice": "hold",
                                                   "confidence": 0.7,
                                                   "probabilities": {"ship": 0.2, "hold": 0.7}}}})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, _route = jev_agent.ask("jev:choice:(ship; hold): what now?")
        self.assertEqual({"ship": "ship", "hold": "hold"}, sent["body"]["questions"]["q"]["criteria"])
        self.assertIn("Jev decision (choice): hold", text)
        self.assertIn("probability 0.700", text)

    def test_a_wide_score_range_is_ten_bands_and_the_reply_names_the_likeliest(self):
        sent = {}

        def fake(request, timeout=None):
            sent["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({"answers": {"q": {"type": "score", "score": 1.2, "confidence": 0.6,
                                                   "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}}}})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, _route = jev_agent.ask("jev:score:0..1499: average payload chars?")
        levels = sent["body"]["questions"]["q"]["criteria"]
        self.assertEqual(10, len(levels))
        self.assertEqual("0 to 149", levels[0])
        self.assertEqual("1350 to 1499", levels[-1])
        self.assertIn("Jev decision (score): 150 to 299", text)
        self.assertIn("range 0..1499", text)

    def test_a_narrow_score_range_is_one_level_per_value(self):
        self.assertEqual([(1, 1), (2, 2), (3, 3)], jev_agent._bands(1, 3))
        self.assertEqual(10, len(jev_agent._bands(0, 10)))

    def test_no_spec_still_answers_and_teaches_the_shape(self):
        def fake(request, timeout=None):
            return FakeResponse({"answers": {"q": {"type": "noul", "noul": 0.2}}})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, _route = jev_agent.ask("average the last 50 rows please")
        self.assertIn("jev:score", text, "the usage hint rides along")

    def test_an_unknown_shape_is_a_loud_failure_naming_keys_only(self):
        def fake(request, timeout=None):
            return FakeResponse({"outputs": [1], "trace": "x"})
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, reason = jev_agent.ask("jev:yesno: q?")
        self.assertIsNone(text)
        self.assertIn("shape mismatch", reason)
        self.assertIn("outputs", reason)
        self.assertNotIn("k-test", reason)

    def test_http_errors_and_a_missing_key_fail_closed(self):
        def fake(request, timeout=None):
            raise urllib.error.HTTPError(jev_agent.SYSTEMONE, 401, "nope", {}, BytesIO(b"bad token"))
        with mock.patch.object(jev_agent.urllib.request, "urlopen", fake):
            text, reason = jev_agent.ask("jev:yesno: q?")
        self.assertIsNone(text)
        self.assertIn("HTTP 401", reason)
        self.assertNotIn("bad token", reason)
        with mock.patch.dict("os.environ", {"JEV_API_KEY": ""}):
            with mock.patch.object(jev_agent, "ENV", "does-not-exist"):
                text, reason = jev_agent.ask("jev:yesno: q?")
        self.assertIsNone(text)
        self.assertIn("no JEV_API_KEY", reason)



class MalformedAnswers(unittest.TestCase):
    """A provider shape error must never become a board decision."""

    def test_yesno_rejects_invalid_type_and_probability(self):
        question = {"type": "yesno"}
        for answer in (
            {"type": "choice", "noul": 0.8},
            {"type": "noul", "noul": True},
            {"type": "noul", "noul": [0.8]},
            {"type": "noul", "noul": -0.01},
            {"type": "noul", "noul": 2.0},
            {"type": "noul", "noul": float("nan")},
            {"type": "noul", "noul": float("inf")},
        ):
            with self.subTest(answer=answer):
                self.assertEqual((None, None), jev_agent.read_answer(question, answer))

    def test_choice_rejects_unknown_option_and_bad_probabilities(self):
        question = {"type": "choice", "options": ["ship", "hold"]}
        for answer in (
            {"type": "noul", "choice": "hold", "confidence": 0.7},
            {"type": "choice", "choice": "erase", "confidence": 0.7},
            {"type": "choice", "choice": ["hold"], "confidence": 0.7},
            {"type": "choice", "choice": "hold", "confidence": 2.0},
            {"type": "choice", "choice": "hold", "confidence": True},
            {"type": "choice", "choice": "hold", "probabilities": {"erase": 0.8}},
            {"type": "choice", "choice": "hold", "probabilities": {"hold": float("nan")}},
            {"type": "choice", "choice": "hold"},
        ):
            with self.subTest(answer=answer):
                self.assertEqual((None, None), jev_agent.read_answer(question, answer))
        self.assertEqual(("hold", 0.7), jev_agent.read_answer(
            question, {"type": "choice", "choice": "hold",
                       "probabilities": {"ship": 0.3, "hold": 0.7}}))

    def test_score_rejects_out_of_range_index_and_probability(self):
        question = {"type": "score", "min": 0, "max": 1499}
        for answer in (
            {"type": "choice", "score": 1.2, "confidence": 0.6},
            {"type": "score", "score": 999, "probabilities": {"1": 0.6}},
            {"type": "score", "score": -1, "confidence": 0.8},
            {"type": "score", "score": True, "confidence": 0.8},
            {"type": "score", "score": float("inf"), "confidence": 0.8},
            {"type": "score", "score": 1.2, "confidence": 2.0},
            {"type": "score", "score": 1.2, "probabilities": {"10": 0.7}},
            {"type": "score", "score": 1.2, "probabilities": {"1": -0.1}},
            {"type": "score", "score": 1.2},
        ):
            with self.subTest(answer=answer):
                self.assertEqual((None, None), jev_agent.read_answer(question, answer))
        self.assertEqual(("150 to 299", 0.6), jev_agent.read_answer(
            question, {"type": "score", "score": 1.2, "confidence": 0.6}))

    def test_invalid_provider_reply_fails_before_board_text(self):
        with mock.patch.dict("os.environ", {"JEV_API_KEY": "k-test"}):
            with mock.patch.object(jev_agent.urllib.request, "urlopen",
                                   return_value=FakeResponse({"answers": {
                                       "q": {"type": "noul", "noul": 2.0}}})):
                text, reason = jev_agent.ask("jev:yesno: ship?")
        self.assertIsNone(text)
        self.assertIn("shape mismatch", reason)



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

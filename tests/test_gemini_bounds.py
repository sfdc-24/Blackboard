"""PY-08 and PY-09 (Grok's poka-yoke audit, 2026-10-08): every Gemini call is bounded and says what it
cost, and the Gemini waker wakes only for a row that asks it something."""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import agent_waker as aw                                                  # noqa: E402


def load_gemini(env):
    with mock.patch.dict(os.environ, env, clear=False):
        for name in ("GEMINI_MAX_OUTPUT_TOKENS", "GEMINI_THINKING_LEVEL", "GEMINI_PRICE_IN_PER_M",
                     "GEMINI_PRICE_OUT_PER_M"):
            if name not in env:
                os.environ.pop(name, None)
        spec = importlib.util.spec_from_file_location("gemini_agent_bounds", REPO / "scripts" / "gemini_agent.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def sent(module, responses):
    seen = []

    def post(url, headers, payload, timeout=60):
        seen.append(json.loads(json.dumps(payload)))
        return responses.pop(0)
    with mock.patch.object(module, "_post", side_effect=post), \
            mock.patch.object(module, "api_key", return_value=("GEMINI_API_KEY", "k")):
        text, route = module.ask("a question")
    return seen, text


OK = (200, json.dumps({"output_text": "an answer", "usage": {
    "total_tokens": 1500, "total_input_tokens": 1000, "total_output_tokens": 300, "total_thought_tokens": 200}}))


class EveryCallIsBounded(unittest.TestCase):
    def test_defaults_cap_output_and_thinking(self):
        seen, text = sent(load_gemini({}), [OK])
        self.assertEqual("an answer", text)
        self.assertEqual({"max_output_tokens": 4096, "thinking_level": "low"}, seen[0]["generation_config"])

    def test_the_job_sets_the_bounds(self):
        seen, _ = sent(load_gemini({"GEMINI_MAX_OUTPUT_TOKENS": "800", "GEMINI_THINKING_LEVEL": "minimal"}), [OK])
        self.assertEqual({"max_output_tokens": 800, "thinking_level": "minimal"}, seen[0]["generation_config"])

    def test_an_unknown_thinking_level_is_dropped_never_sent(self):
        seen, _ = sent(load_gemini({"GEMINI_THINKING_LEVEL": "unlimited"}), [OK])
        self.assertEqual({"max_output_tokens": 4096}, seen[0]["generation_config"])

    def test_a_model_that_refuses_thinking_level_is_asked_once_more_still_capped(self):
        refused = (400, json.dumps({"error": {"message": "thinking_level is not supported for this model"}}))
        seen, text = sent(load_gemini({}), [refused, OK])
        self.assertEqual(2, len(seen))
        self.assertEqual({"max_output_tokens": 4096}, seen[1]["generation_config"])
        self.assertEqual("an answer", text)

    def test_any_other_error_is_not_retried(self):
        seen, text = sent(load_gemini({}), [(400, '{"error": "bad request"}')])
        self.assertEqual(1, len(seen))
        self.assertIsNone(text)


class TheReplySaysWhatItCost(unittest.TestCase):
    USAGE = {"total": 1500, "in": 1000, "out": 300, "thought": 200}

    def test_tokens_always_and_unpriced_without_prices(self):
        self.assertEqual("in 1000 out 300 thought 200 est_usd unpriced", load_gemini({}).cost_estimate(self.USAGE))

    def test_usd_when_the_job_states_its_prices(self):
        m = load_gemini({"GEMINI_PRICE_IN_PER_M": "2", "GEMINI_PRICE_OUT_PER_M": "12"})
        # 1000 in at $2/M + (300 out + 200 thought) at $12/M = 0.002 + 0.006
        self.assertEqual("in 1000 out 300 thought 200 est_usd 0.0080", m.cost_estimate(self.USAGE))

    def test_no_usage_no_line(self):
        self.assertEqual("", load_gemini({}).cost_estimate({}))

    def test_the_line_names_the_model_that_answered_and_the_alias_asked(self):
        # Aya, AYA-GEMINI-COST-RECONCILE-20261008T194011Z: gemini-waker-c9qfw could not be priced
        # because nothing recorded which model answered gemini-pro-latest.
        m = load_gemini({"GEMINI_MODEL": "gemini-pro-latest"})
        answered = (200, json.dumps({"output_text": "ok", "model": "gemini-3.1-pro-preview-0925",
                                     "usage": {"total_tokens": 9, "total_input_tokens": 5,
                                               "total_output_tokens": 3, "total_thought_tokens": 1}}))
        sent(m, [answered])
        self.assertEqual("gemini-3.1-pro-preview-0925", m.ask.last_usage["model"])
        self.assertEqual("gemini-pro-latest", m.ask.last_usage["asked"])
        self.assertEqual("model gemini-3.1-pro-preview-0925 asked gemini-pro-latest in 5 out 3 thought 1 "
                         "est_usd unpriced", m.cost_estimate(m.ask.last_usage))

    def test_no_reported_model_says_so_and_a_hostile_name_is_dropped(self):
        m = load_gemini({})
        for reported in (None, "x|phase=ACK", "a b"):
            body = {"output_text": "ok", "usage": {"total_input_tokens": 1}}
            if reported is not None:
                body["model"] = reported
            sent(m, [(200, json.dumps(body))])
            line = m.cost_estimate(m.ask.last_usage)
            self.assertTrue(line.startswith("model unreported asked "), line)
            self.assertNotIn("|", line)


def board_row(target="gemini", action="APPEND", payload="BCB|v=1|id=X-1|phase=REQUEST|to=gemini|ask=?",
              source="grok"):
    return ["X-1", "2026-10-08T15:00:00Z", source, target, action, payload, "OPEN", "Blackboard", "g", ""]


class GeminiWakesOnlyWhenAsked(unittest.TestCase):
    def test_a_direct_ask_wakes(self):
        self.assertTrue(aw.wakes_for(board_row(), "gemini"))
        self.assertTrue(aw.wakes_for(board_row(target="ALL", payload="BCB|v=1|id=X|phase=REQUEST|to=grok,gemini"), "gemini"))

    def test_results_receipts_done_and_acks_do_not(self):
        for action, phase in (("AYA_RESULT", "AYA_RESULT"), ("APPEND", "RESULT"), ("APPEND", "DONE"),
                              ("APPEND", "ACK"), ("AYA_RECEIPT", "RECEIPT"), ("DONE", "NOTE")):
            row = board_row(action=action, payload="BCB|v=1|id=X|phase=%s|to=gemini" % phase)
            self.assertFalse(aw.wakes_for(row, "gemini"), (action, phase))

    def test_a_cc_only_copy_does_not(self):
        row = board_row(target="aya", payload="BCB|v=1|id=X|phase=REQUEST|to=aya|cc=claude-code-cli,gemini")
        self.assertTrue(aw.addressed_to(row, "gemini"), "it IS addressed to gemini, as a copy")
        self.assertFalse(aw.wakes_for(row, "gemini"))
        # The copy line first: only a to= line counts, wherever it sits.
        first = board_row(target="aya", payload="BCB|v=1|id=X|cc=gemini|phase=REQUEST|to=aya")
        self.assertFalse(aw.wakes_for(first, "gemini"))

    def test_his_whatsapp_prefix_still_wakes(self):
        row = board_row(target="sheet", source="whatsapp", payload="Gemini what breaks first?")
        self.assertTrue(aw.wakes_for(row, "gemini"))

    def test_select_applies_it_for_gemini_only(self):
        cc_only = board_row(target="aya", source="claude-code-cli",
                            payload="BCB|v=1|id=CC-1|phase=REQUEST|to=aya|cc=gemini,grok")
        self.assertEqual([], aw.select([cc_only], set(), "gemini"))
        self.assertTrue(aw.AGENTS["gemini"].get("wake_filter"))
        self.assertFalse(aw.AGENTS["grok"].get("wake_filter"), "other agents are unchanged by this PR")
        self.assertEqual(1, len(aw.select([cc_only], set(), "grok")))


if __name__ == "__main__":
    unittest.main()

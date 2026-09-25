"""The Gemini advisor contract (Codex plan R2): advice, never a patch; bound to
one revision and fenced otherwise; a failed advisor blocks nothing; off by
default. Offline: every call goes to a fake opener."""
from __future__ import annotations

import copy
import io
import json
import logging
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud" / "studio-controller"))

from workers import advisor as adv  # noqa: E402

GOOD = {
    "perspective": "The bakery name is set, but nothing says where the shop is or when it opens.",
    "questions": [
        {"prompt": "What should a first-time visitor do first?", "why": "It decides the main button.",
         "options": [{"id": "a", "label": "Order ahead"}, {"id": "b", "label": "Find the shop"},
                     {"id": "c", "label": "See the menu"}],
         "recommended": "a"},
    ],
    "risks": ["No opening hours on the page yet."],
}
KEY = "AIza-test-key-not-real"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def gemini_payload(obj, thought=False):
    parts = []
    if thought:
        parts.append({"text": "thinking about it", "thought": True})
    parts.append({"text": json.dumps(obj) if not isinstance(obj, str) else obj})
    return {"candidates": [{"content": {"parts": parts}}]}


class Opener:
    def __init__(self, payload=None, fail=None):
        self.payload = payload if payload is not None else gemini_payload(GOOD)
        self.fail = fail
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.fail:
            raise self.fail
        return Response(self.payload)


def snapshot(revision=3):
    return {"revision": revision, "topic_line": "The visitor picked this topic before starting: Build a website.",
            "canvas": "a page with a heading 'Crumb & Co.'", "said": ["a website for my bakery"]}


class Contract(unittest.TestCase):
    def test_advice_is_bound_to_the_revision_it_read_and_carries_no_patch(self):
        advice = adv.Advisor(key=KEY, enabled=True, opener=Opener()).advise("s-1", snapshot(3))
        self.assertEqual(3, advice["revision"])
        self.assertEqual("gemini", advice["agent"])
        self.assertEqual({"agent", "revision", "perspective", "questions", "risks"}, set(advice))
        self.assertNotIn("ops", json.dumps(advice))
        self.assertEqual("q1", advice["questions"][0]["id"])
        self.assertEqual("a", advice["questions"][0]["recommended"])

    def test_advice_for_another_revision_is_fenced(self):
        advice = adv.Advisor(key=KEY, enabled=True, opener=Opener()).advise("s-1", snapshot(3))
        self.assertFalse(adv.fenced(advice, 3))
        self.assertTrue(adv.fenced(advice, 4))                 # the canvas moved on: dropped
        self.assertTrue(adv.fenced(None, 3))

    def test_off_by_default_and_without_a_key_nothing_is_called(self):
        opener = Opener()
        self.assertIsNone(adv.Advisor(key=KEY, opener=opener).advise("s-1", snapshot()))
        self.assertIsNone(adv.Advisor(key="", enabled=True, opener=opener).advise("s-1", snapshot()))
        self.assertEqual([], opener.requests)

    def test_a_failed_slow_or_garbled_advisor_returns_none_and_never_raises(self):
        for opener in (Opener(fail=TimeoutError("slow")), Opener(fail=OSError("down")),
                       Opener(payload={"candidates": []}), Opener(payload=gemini_payload("not json {"))):
            self.assertIsNone(adv.Advisor(key=KEY, enabled=True, opener=opener).advise("s-1", snapshot()))

    def test_the_call_cap_per_session(self):
        opener = Opener()
        advisor = adv.Advisor(key=KEY, enabled=True, opener=opener, call_cap=2)
        results = [advisor.advise("s-1", snapshot()) for _ in range(3)]
        self.assertEqual([True, True, False], [r is not None for r in results])
        self.assertIsNotNone(advisor.advise("s-2", snapshot()))    # per session
        self.assertEqual(3, len(opener.requests))

    def test_the_request_shape_the_key_only_in_a_header_and_thoughts_never_used(self):
        opener = Opener(payload=gemini_payload(GOOD, thought=True))
        advice = adv.Advisor(key=KEY, enabled=True, opener=opener).advise("s-1", snapshot())
        self.assertIsNotNone(advice)
        request, timeout = opener.requests[0]
        self.assertEqual(adv.TIMEOUT_SECONDS, timeout)
        self.assertNotIn(KEY, request.full_url)
        self.assertEqual(KEY, request.get_header("X-goog-api-key"))
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("application/json", body["generationConfig"]["responseMimeType"])
        self.assertIn("SFDC24 USE POLICY", body["systemInstruction"]["parts"][0]["text"])
        self.assertIn("revision 3", body["contents"][0]["parts"][0]["text"])

    def test_nothing_about_a_failure_is_logged(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        try:
            adv.Advisor(key=KEY, enabled=True, opener=Opener(fail=OSError("down " + KEY))).advise("s-1", snapshot())
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertNotIn(KEY, stream.getvalue())


class Validation(unittest.TestCase):
    def broken(self, change):
        raw = copy.deepcopy(GOOD)
        change(raw)
        return adv.validate(raw, 1)

    def test_a_clean_answer_passes_whole(self):
        advice, problems = adv.validate(copy.deepcopy(GOOD), 7)
        self.assertEqual([], problems)
        self.assertEqual(7, advice["revision"])

    def test_every_rule_is_checked_and_nothing_is_partly_kept(self):
        cases = {
            "an extra top field (a patch)": lambda r: r.update(ops=[{"op": "set_label"}]),
            "no risks field": lambda r: r.pop("risks"),
            "perspective too long": lambda r: r.update(perspective="x" * 241),
            "empty perspective": lambda r: r.update(perspective="  "),
            "markup": lambda r: r.update(perspective="<b>bold</b>"),
            "a line break": lambda r: r.update(perspective="one\ntwo"),
            "a C1 control": lambda r: r.update(perspective="ok\u0085not"),
            "a line separator": lambda r: r.update(perspective="ok not"),
            "three questions": lambda r: r["questions"].extend([copy.deepcopy(r["questions"][0])] * 2),
            "one option": lambda r: r["questions"][0].update(options=[{"id": "a", "label": "Only"}]),
            "five options": lambda r: r["questions"][0].update(options=[{"id": i, "label": i} for i in "abcde"]),
            "a bad option id": lambda r: r["questions"][0]["options"][0].update(id="z"),
            "a repeated option id": lambda r: r["questions"][0]["options"][1].update(id="a"),
            "recommends what it does not offer": lambda r: r["questions"][0].update(recommended="d"),
            "an extra question field": lambda r: r["questions"][0].update(url="x"),
            "an extra option field": lambda r: r["questions"][0]["options"][0].update(href="x"),
            "four risks": lambda r: r.update(risks=["a", "b", "c", "d"]),
            "a long risk": lambda r: r.update(risks=["x" * 121]),
            "prompt too long": lambda r: r["questions"][0].update(prompt="x" * 161),
        }
        for name, change in cases.items():
            advice, problems = self.broken(change)
            self.assertIsNone(advice, name)
            self.assertTrue(problems, name)

    def test_not_an_object(self):
        for raw in (None, [], "text", {"perspective": "x"}):
            self.assertIsNone(adv.validate(raw, 1)[0])


if __name__ == "__main__":
    unittest.main()

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
import threading
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
    def __init__(self, payload=None, *, raw=None, headers=None):
        self.payload = payload
        self.raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.headers = headers or {}
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.raw if size is None or size < 0 else self.raw[:size]

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
    def __init__(self, payload=None, fail=None, response=None, after=None):
        self.payload = payload if payload is not None else gemini_payload(GOOD)
        self.fail = fail
        self.response = response
        self.after = after
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.after:
            self.after()
        if self.fail:
            raise self.fail
        return self.response or Response(self.payload)


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


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

    def test_wrong_types_anywhere_return_none_and_never_raise(self):
        # Cursor NO-GO on 7675620: a non-string prompt, a null label, a why object, a bad revision
        breaks = [
            lambda r: r["questions"][0].update(prompt=7),
            lambda r: r["questions"][0].update(why={"x": 1}),
            lambda r: r["questions"][0]["options"][0].update(label=None),
            lambda r: r["questions"][0]["options"][0].update(id=["a"]),
            lambda r: r["questions"][0].update(recommended=["a"]),
            lambda r: r["questions"][0].update(options=None),
            lambda r: r.update(risks=[None]),
            lambda r: r.update(perspective=None),
            lambda r: r.update(questions=[None]),
        ]
        for i, change in enumerate(breaks):
            raw = copy.deepcopy(GOOD)
            change(raw)
            self.assertEqual((None, True), (lambda a: (a[0], bool(a[1])))(adv.validate(raw, 1)), i)
            advisor = adv.Advisor(key=KEY, enabled=True, opener=Opener(payload=gemini_payload(raw)))
            self.assertIsNone(advisor.advise("s-1", snapshot()), i)
        for revision in ("three", None, -1, 2.5, True):
            opener = Opener()
            snap = dict(snapshot(), revision=revision)
            self.assertIsNone(adv.Advisor(key=KEY, enabled=True, opener=opener).advise("s-1", snap), revision)
            self.assertEqual([], opener.requests, revision)

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

    def test_the_release_uses_one_source_pinned_model(self):
        self.assertEqual("gemini-3.5-flash-lite", adv.MODEL)

    def test_the_provider_body_is_bounded_before_json_decode(self):
        oversized = Response(raw=b"x" * (adv.MAX_RESPONSE_BYTES + 1))
        opener = Opener(response=oversized)
        result = adv.Advisor(key=KEY, enabled=True, opener=opener).advise("s-1", snapshot())
        self.assertIsNone(result)
        self.assertEqual([adv.MAX_RESPONSE_BYTES + 1], oversized.read_sizes)

        declared = Response(raw=b"{}", headers={"Content-Length": str(adv.MAX_RESPONSE_BYTES + 1)})
        opener = Opener(response=declared)
        result = adv.Advisor(key=KEY, enabled=True, opener=opener).advise("s-2", snapshot())
        self.assertIsNone(result)
        self.assertEqual([], declared.read_sizes)

    def test_a_late_provider_result_is_discarded(self):
        clock = Clock()
        opener = Opener(after=lambda: setattr(clock, "value", clock.value + adv.TIMEOUT_SECONDS))
        result = adv.Advisor(key=KEY, enabled=True, opener=opener, clock=clock).advise("s-1", snapshot())
        self.assertIsNone(result)

    def test_same_session_single_flight_is_nonblocking_and_releases(self):
        entered = threading.Event()
        release = threading.Event()

        def block_first():
            entered.set()
            self.assertTrue(release.wait(2.0))

        opener = Opener(after=block_first)
        advisor = adv.Advisor(key=KEY, enabled=True, opener=opener)
        first = []
        thread = threading.Thread(target=lambda: first.append(advisor.advise("s-1", snapshot())))
        thread.start()
        self.assertTrue(entered.wait(1.0))
        self.assertIsNone(advisor.advise("s-1", snapshot()))
        self.assertEqual(1, len(opener.requests))
        release.set()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(first[0])

        opener.after = None
        self.assertIsNotNone(advisor.advise("s-1", snapshot()))
        self.assertEqual(2, len(opener.requests))

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

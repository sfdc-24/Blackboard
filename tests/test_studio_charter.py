"""The charter lane and the build plan (owner, 2026-09-25: "a project charter and
workplan in disguise"; "here is your build plan watermarked with sfdc24.com").

workers/charter.py: all-or-nothing validation against the topic's frame, one
repair, never raises, off by default. POST /v1/session/{id}/charter: 503 when
off, 409 on a stale revision or a running build before any call, moderated
(fail closed), fenced as the last step and committed by compare-and-set.
app/summary_pdf.py: the build plan (watermark, charter, terms, the owner's
prices only). app/pricing.py: the price table, strictly.
"""
from __future__ import annotations

import copy
import json
import re
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

import tests.test_studio_controller as base  # noqa: E402  (sets sys.path; its cases are not re-run here)
import tests.test_studio_governance as gov  # noqa: E402
from tests.test_studio_end_card import EndCard, pdf_text  # noqa: E402
from app.main import create_app  # noqa: E402
from app.pricing import parse_price_table  # noqa: E402
from app.summary_pdf import PLAN_TERMS, PRICED_AFTER_REVIEW, build_summary_pdf  # noqa: E402
from workers import charter as ch  # noqa: E402
from workers.policy import USE_POLICY  # noqa: E402
from workers.topics import CHARTER_FRAMES, DEFAULT_CHARTER_FRAME, TOPICS, charter_frame  # noqa: E402

WEBSITE = [d[0] for d in CHARTER_FRAMES["website"]]


def answer(topic="website", levels=None, nxt="Who should visit the site first?"):
    ids = [d[0] for d in charter_frame(topic)]
    levels = levels or {}
    dims = []
    for i in ids:
        lv = levels.get(i, 0)
        dims.append({"id": i, "level": lv, "captured": "" if lv == 0 else "About " + i})
    return {"dimensions": dims, "next": nxt}


class Block:
    def __init__(self, raw, name="record_charter"):
        self.type, self.name, self.input = "tool_use", name, raw


class FakeClient:
    """Stands in for anthropic.Anthropic: answers from a list, records every request."""

    def __init__(self, *answers, fail=None):
        self.answers = list(answers)
        self.fail = fail
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail:
            raise self.fail
        raw = self.answers.pop(0) if self.answers else None
        return SimpleNamespace(content=[Block(raw)] if raw is not None else [])


# --- the frames ---------------------------------------------------------------
class Frames(unittest.TestCase):
    def test_the_frames_are_the_contract_with_the_page(self):
        self.assertEqual(["org", "pain", "process", "data", "integrations", "timeline", "close"],
                         [d[0] for d in CHARTER_FRAMES["salesforce_admin"]])
        self.assertEqual(["objects", "quality", "reports", "sources", "access", "timeline", "close"],
                         [d[0] for d in CHARTER_FRAMES["salesforce_data"]])
        self.assertEqual(["type", "audience", "business", "design", "scope", "timeline", "close"], WEBSITE)
        default = ["objectives", "scope", "design", "refinement", "timeline", "close"]
        for topic in ("logo", "app", "other", "", None, "nonsense", 7):
            self.assertEqual(default, [d[0] for d in charter_frame(topic)], topic)
        self.assertEqual(DEFAULT_CHARTER_FRAME, charter_frame("logo"))


# --- validation -----------------------------------------------------------------
class Validation(unittest.TestCase):
    def test_a_clean_answer_passes_in_frame_order(self):
        raw = answer(levels={"type": 3, "audience": 1})
        raw["dimensions"].reverse()
        checked, problems = ch.validate(raw, "website")
        self.assertEqual([], problems)
        self.assertEqual(WEBSITE, [d["id"] for d in checked["dimensions"]])
        self.assertEqual({"id", "level", "captured"}, set(checked["dimensions"][0]))

    def test_every_rule_refuses_the_whole_answer(self):
        def broken(change):
            raw = copy.deepcopy(answer(levels={"type": 2}))
            change(raw)
            return ch.validate(raw, "website")

        cases = {
            "an extra top field": lambda r: r.update(score=90),
            "no next": lambda r: r.pop("next"),
            "a missing dimension": lambda r: r["dimensions"].pop(),
            "an unknown id": lambda r: r["dimensions"][0].update(id="budget"),
            "a repeated id": lambda r: r["dimensions"].__setitem__(1, dict(r["dimensions"][0])),
            "a bool level": lambda r: r["dimensions"][0].update(level=True),
            "a level of 4": lambda r: r["dimensions"][0].update(level=4),
            "a string level": lambda r: r["dimensions"][0].update(level="2"),
            "a negative level": lambda r: r["dimensions"][1].update(level=-1),
            "a long capture": lambda r: r["dimensions"][0].update(captured="x" * 141),
            "markup": lambda r: r["dimensions"][0].update(captured="<b>shop</b>"),
            "a line break": lambda r: r["dimensions"][0].update(captured="one\ntwo"),
            "a C1 control": lambda r: r["dimensions"][0].update(captured="ok\u0085no"),
            "a line separator": lambda r: r["dimensions"][0].update(captured="ok no"),
            "level 0 with words": lambda r: r["dimensions"][1].update(captured="Locals"),
            "level 2 with nothing": lambda r: r["dimensions"][0].update(captured="  "),
            "a long next": lambda r: r.update(next="x" * 161),
            "markup in next": lambda r: r.update(next="<i>who</i>?"),
            "an extra dimension field": lambda r: r["dimensions"][0].update(label="Site type"),
            "dimensions not a list": lambda r: r.update(dimensions={}),
            "a dimension not an object": lambda r: r["dimensions"].__setitem__(0, "type"),
            "a non-string capture": lambda r: r["dimensions"][0].update(captured=7),
        }
        for name, change in cases.items():
            checked, problems = broken(change)
            self.assertIsNone(checked, name)
            self.assertTrue(problems, name)

    def test_not_an_object_and_the_wrong_frame(self):
        for raw in (None, [], "text", {"dimensions": []}):
            self.assertIsNone(ch.validate(raw, "website")[0])
        self.assertIsNone(ch.validate(answer("website"), "salesforce_admin")[0])   # website ids are not admin ids
        self.assertIsNotNone(ch.validate(answer("logo"), "")[0])                   # the default frame for no topic

    def test_an_empty_next_is_allowed(self):
        self.assertIsNotNone(ch.validate(answer(nxt=""), "website")[0])


# --- the worker --------------------------------------------------------------------
def snapshot(topic="website", said=None, charter=None):
    return {"revision": 3, "topic": topic, "topic_line": "The visitor picked this topic before starting: X.",
            "canvas": "a page with a heading", "said": said if said is not None else ["a company page for my bakery"],
            "charter": charter or {}}


class Worker(unittest.TestCase):
    def test_off_by_default_calls_nothing(self):
        client = FakeClient(answer())
        self.assertIsNone(ch.Charter(client).chart(snapshot()))
        self.assertEqual([], client.requests)

    def test_a_clean_answer_is_the_charter_and_the_request_is_bound_to_the_frame(self):
        client = FakeClient(answer(levels={"type": 3}))
        said = ["line %d" % i for i in range(15)]
        prior = {"dimensions": [{"id": "type", "level": 1, "captured": "Some kind of page"}]}
        out = ch.Charter(client, enabled=True).chart(snapshot(said=said, charter=prior))
        self.assertEqual({"topic", "dimensions", "next"}, set(out))
        self.assertEqual("website", out["topic"])
        self.assertEqual(WEBSITE, [d["id"] for d in out["dimensions"]])
        req = client.requests[0]
        self.assertEqual({"type": "tool", "name": "record_charter"}, req["tool_choice"])
        enum = req["tools"][0]["input_schema"]["properties"]["dimensions"]["items"]["properties"]["id"]["enum"]
        self.assertEqual(WEBSITE, enum)
        self.assertIn(USE_POLICY, req["system"])
        text = req["messages"][0]["content"]
        self.assertIn("line 14", text)
        self.assertIn("line 3", text)
        self.assertNotIn("line 2\n", text + "\n")                     # the last 12 only
        self.assertIn("Some kind of page", text)                       # the charter so far
        self.assertIn("audience (Audience)", text)                     # the frame, in order

    def test_one_repair_then_none_and_never_raises(self):
        bad = {"dimensions": [], "next": ""}
        client = FakeClient(bad, answer())
        self.assertIsNotNone(ch.Charter(client, enabled=True).chart(snapshot()))
        self.assertEqual(2, len(client.requests))
        self.assertIn("refused", client.requests[1]["messages"][-1]["content"])
        self.assertIsNone(ch.Charter(FakeClient(bad, bad), enabled=True).chart(snapshot()))
        for fail in (TimeoutError("slow"), OSError("down"), ValueError("odd")):
            self.assertIsNone(ch.Charter(FakeClient(fail=fail), enabled=True).chart(snapshot()))

    def test_an_unknown_topic_uses_the_default_frame(self):
        client = FakeClient(answer("logo"))
        out = ch.Charter(client, enabled=True).chart(snapshot(topic="<script>"))
        self.assertEqual("", out["topic"])
        self.assertEqual([d[0] for d in DEFAULT_CHARTER_FRAME], [d["id"] for d in out["dimensions"]])


# --- the route ----------------------------------------------------------------------
class FakeCharter:
    def __init__(self, ready=True, result="good", during=None):
        self._ready, self.result, self.during = ready, result, during
        self.snapshots = []

    def ready(self):
        return self._ready

    def chart(self, snap):
        self.snapshots.append(snap)
        if self.during:
            self.during()
        if self.result == "good":
            checked, _ = ch.validate(answer(snap["topic"] or "", levels={"type": 3, "audience": 1}),
                                     snap["topic"] or "")
            return {"topic": snap["topic"] or "", **checked}
        if self.result == "flag":
            raw = answer(snap["topic"] or "", levels={"type": 2})
            raw["dimensions"][0]["captured"] = "FLAG this"
            checked, _ = ch.validate(raw, snap["topic"] or "")
            return {"topic": snap["topic"] or "", **checked}
        return None


class Route(unittest.TestCase):
    origin = base.TalkLaneTests.origin

    def make(self, charter, mode=None, **overrides):
        self.store = base.MemoryStore()
        self.email_sender = base.EmailSender()
        extra = {}
        values = dict(overrides)
        if mode:
            self.moderation = gov.FakeModeration(mode)
            values.update(moderation_enabled=True, openai_api_key=values.get("openai_api_key", "sk-moderation-test"))
            extra["moderation_client"] = self.moderation
        self.app = create_app(settings=base.settings(**values), store=self.store, worker=base.CountingWorker(),
                              clock=lambda: 1000, id_factory=base.IDs(), email_sender=self.email_sender,
                              talk_client=base.FakeTalk(), charter=charter, **extra)
        return self.app

    def session(self, client, topic="website"):
        started = client.post("/v1/auth/start", headers=self.origin, json={
            "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
        code = self.email_sender.calls[-1][1]
        operator = client.post("/v1/auth/verify", headers=self.origin, json={
            "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
            "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
        body = {"creation_id": "charter-1", "start": "blank"}
        if topic:
            body["topic"] = topic
        created = client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + operator},
                              json=body).json()
        return created["session_id"], {**self.origin, "Authorization": "Bearer " + created["token"]}

    def state(self, sid):
        return self.store.data["studio_session_" + sid]

    def mutate(self, sid, change):
        repo = self.app.state.controller.repository
        record = repo.load(sid)
        moved = copy.deepcopy(record.state)
        change(moved)
        repo.save(sid, moved, record.token)

    def test_the_charter_is_the_contract_shape_in_frame_order_and_committed(self):
        lane = FakeCharter()
        with TestClient(self.make(lane)) as client:
            sid, headers = self.session(client)
            self.assertTrue(client.get("/health").json()["features"]["charter"])
            out = client.post("/v1/session/%s/charter" % sid, headers=headers, json={"revision": 1})
        self.assertEqual(200, out.status_code, out.text)
        body = out.json()
        self.assertEqual({"charter"}, set(body))
        charter = body["charter"]
        self.assertEqual({"revision", "topic", "dimensions", "next"}, set(charter))
        self.assertEqual((1, "website"), (charter["revision"], charter["topic"]))
        self.assertEqual(WEBSITE, [d["id"] for d in charter["dimensions"]])
        for d in charter["dimensions"]:
            self.assertEqual({"id", "level", "captured"}, set(d))
        stored = self.state(sid)["charter"]
        self.assertEqual(1000, stored.pop("updated_at"))
        self.assertEqual(charter, stored)
        snap = lane.snapshots[0]
        self.assertEqual((1, "website"), (snap["revision"], snap["topic"]))

    def test_no_topic_uses_the_default_frame(self):
        with TestClient(self.make(FakeCharter())) as client:
            sid, headers = self.session(client, topic=None)
            out = client.post("/v1/session/%s/charter" % sid, headers=headers, json={"revision": 1}).json()
        self.assertEqual("", out["charter"]["topic"])
        self.assertEqual([d[0] for d in DEFAULT_CHARTER_FRAME], [d["id"] for d in out["charter"]["dimensions"]])

    def test_off_is_503_and_health_says_so(self):
        with TestClient(self.make(FakeCharter(ready=False))) as client:
            sid, headers = self.session(client)
            off = client.post("/v1/session/%s/charter" % sid, headers=headers, json={"revision": 1})
            health = client.get("/health").json()["features"]
        self.assertEqual(503, off.status_code)
        self.assertIs(False, health["charter"])

    def test_a_stale_revision_or_a_running_build_is_409_before_any_call(self):
        lane = FakeCharter()
        with TestClient(self.make(lane)) as client:
            sid, headers = self.session(client)
            stale = client.post("/v1/session/%s/charter" % sid, headers=headers, json={"revision": 0})
            self.mutate(sid, lambda s: s.update(active_command="cmd-running"))
            building = client.post("/v1/session/%s/charter" % sid, headers=headers, json={"revision": 1})
        self.assertEqual((409, 409), (stale.status_code, building.status_code))
        self.assertEqual([], lane.snapshots)
        self.assertNotIn("charter", self.state(sid))

    def test_fenced_when_the_moment_moves_during_the_call_and_nothing_is_written(self):
        holder = {}
        cases = {
            "the canvas moves": lambda s: s.update(artifact_version=int(s["artifact_version"]) + 1),
            "a build starts": lambda s: s.update(active_command="cmd-late"),
            "a turn lands": lambda s: s.update(turn_seq=int(s.get("turn_seq") or 0) + 1),
        }
        for name, change in cases.items():
            lane = FakeCharter(during=lambda: self.mutate(holder["sid"], holder["change"]))
            with TestClient(self.make(lane)) as client:
                sid, headers = self.session(client)
                holder.update(sid=sid, change=change)
                out = client.post("/v1/session/%s/charter" % sid, headers=headers, json={"revision": 1})
            self.assertEqual({"charter": None, "fenced": True}, out.json(), name)
            self.assertNotIn("charter", self.state(sid), name)

    def test_no_answer_is_an_empty_answer(self):
        with TestClient(self.make(FakeCharter(result=None))) as client:
            sid, headers = self.session(client)
            out = client.post("/v1/session/%s/charter" % sid, headers=headers, json={"revision": 1})
        self.assertEqual({"charter": None}, out.json())
        self.assertNotIn("charter", self.state(sid))

    def test_moderation_flagged_or_unavailable_withholds_it(self):
        for result, mode, key in (("flag", "ok", "sk-moderation-test"), ("good", "raise", "sk-moderation-test"),
                                  ("good", "http500", "sk-moderation-test"), ("good", "ok", "")):
            with TestClient(self.make(FakeCharter(result=result), mode=mode, openai_api_key=key)) as client:
                sid, headers = self.session(client)
                out = client.post("/v1/session/%s/charter" % sid, headers=headers, json={"revision": 1})
            self.assertEqual({"charter": None, "withheld": True}, out.json(), (result, mode))
            self.assertNotIn("charter", self.state(sid), (result, mode))

    def test_clean_words_pass_the_gate_and_the_gate_reads_them(self):
        with TestClient(self.make(FakeCharter(), mode="ok")) as client:
            sid, headers = self.session(client)
            out = client.post("/v1/session/%s/charter" % sid, headers=headers, json={"revision": 1})
        self.assertIsNotNone(out.json()["charter"])
        read = [t for batch in self.moderation.inputs() for t in batch]
        self.assertIn("About type", read)
        self.assertIn("Who should visit the site first?", read)

    def test_bad_bodies_and_strangers(self):
        with TestClient(self.make(FakeCharter())) as client:
            sid, headers = self.session(client)
            codes = [client.post("/v1/session/%s/charter" % sid, headers=headers, json=b).status_code
                     for b in ({}, {"revision": "1"}, {"revision": -1}, {"revision": True}, {"revision": 1, "x": 1})]
            stranger = client.post("/v1/session/%s/charter" % sid, headers=self.origin, json={"revision": 1})
        self.assertEqual([400] * 5, codes)
        self.assertEqual(401, stranger.status_code)

    def test_the_charter_cannot_be_switched_on_without_moderation(self):
        with self.assertRaises(RuntimeError):
            base.settings(charter_enabled=True, moderation_enabled=False).validate()
        base.settings(charter_enabled=True, moderation_enabled=True).validate()


# --- the build plan PDF ----------------------------------------------------------
def plan_state(topic="website", said=3):
    return {"created_at": 1790000000,
            "artifact": {"id": "screen", "kind": "screen", "label": "Crumb and Co.", "children": []},
            "transcript": [{"role": "visitor", "text": "we bake sourdough every morning, line %d" % i}
                           for i in range(said)],
            "charter": {"revision": 3, "topic": topic, "next": "Who should visit first?", "updated_at": 1790000100,
                        "dimensions": [{"id": "type", "level": 3, "captured": "A company page"},
                                       {"id": "audience", "level": 1, "captured": "Locals"},
                                       {"id": "business", "level": 0, "captured": ""}]}}


def pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type /Page\b", pdf))


class BuildPlan(unittest.TestCase):
    def test_the_plan_carries_the_watermark_the_charter_and_the_exact_terms(self):
        pdf = build_summary_pdf(plan_state())
        text = pdf_text(pdf)
        self.assertIn("Your build plan", text)
        self.assertIn("Your project charter", text)
        self.assertIn("Site type - Confirmed", text)
        self.assertIn("Audience - To confirm", text)
        self.assertIn("The business - To confirm", text)
        self.assertIn("A company page", text)
        self.assertIn("Next to settle: Who should visit first?", text)
        for term in PLAN_TERMS:
            self.assertIn(term, text)
        self.assertIn(PRICED_AFTER_REVIEW, text)
        self.assertNotIn("$", text)
        self.assertNotIn("CAD ", text)

    def test_the_watermark_is_on_every_page(self):
        pdf = build_summary_pdf(plan_state(said=40))
        n = pages(pdf)
        self.assertGreaterEqual(n, 2)
        self.assertGreaterEqual(pdf_text(pdf).count("sfdc24.com"), n + 1)   # one per page, plus the footer

    def test_without_a_charter_the_summary_is_as_before(self):
        state = plan_state()
        state.pop("charter")
        text = pdf_text(build_summary_pdf(state))
        self.assertIn("Your SFDC24 working session", text)
        self.assertNotIn("Your build plan", text)
        self.assertNotIn(PLAN_TERMS[0], text)
        self.assertEqual(1, text.count("sfdc24.com"))                        # the footer only

    def test_the_owners_prices_only_and_the_halves(self):
        table = parse_price_table(json.dumps({"currency": "CAD", "prices": {
            "website": {"label": "Company website build", "amount": 4801}}}), TOPICS)
        text = pdf_text(build_summary_pdf(plan_state(), price_table=table)).replace("\(", "(").replace("\)", ")")
        self.assertIn("Company website build: CAD 4,801.00", text)
        self.assertIn("50% to start build and test. (CAD 2,400.50)", text)
        self.assertIn("50% on delivery and handover. (CAD 2,400.50)", text)
        self.assertNotIn(PRICED_AFTER_REVIEW, text)
        other = parse_price_table(json.dumps({"currency": "USD", "prices": {
            "salesforce_admin": {"label": "Admin fix", "amount": 900}}}), TOPICS)
        self.assertIn(PRICED_AFTER_REVIEW, pdf_text(build_summary_pdf(plan_state(), price_table=other)))


class PriceTable(unittest.TestCase):
    def test_empty_is_none_and_a_good_table_parses(self):
        self.assertIsNone(parse_price_table("", TOPICS))
        self.assertIsNone(parse_price_table("   ", TOPICS))
        good = parse_price_table('{"currency": "USD", "prices": {"salesforce_data": {"label": "Data model", "amount": 1250.5}}}', TOPICS)
        self.assertEqual({"currency": "USD", "prices": {"salesforce_data": {"label": "Data model", "amount": 1250.5}}}, good)

    def test_anything_odd_refuses_the_whole_table(self):
        bad = [
            "not json", "[]", '{"currency": "CAD"}', '{"currency": "EUR", "prices": {"website": {"label": "x", "amount": 1}}}',
            '{"currency": "CAD", "prices": {}}',
            '{"currency": "CAD", "prices": {"websites": {"label": "x", "amount": 1}}}',
            '{"currency": "CAD", "prices": {"website": {"label": "x", "amount": 0}}}',
            '{"currency": "CAD", "prices": {"website": {"label": "x", "amount": 1000001}}}',
            '{"currency": "CAD", "prices": {"website": {"label": "x", "amount": 1.234}}}',
            '{"currency": "CAD", "prices": {"website": {"label": "x", "amount": true}}}',
            '{"currency": "CAD", "prices": {"website": {"label": "x", "amount": "100"}}}',
            '{"currency": "CAD", "prices": {"website": {"label": "<b>x</b>", "amount": 1}}}',
            '{"currency": "CAD", "prices": {"website": {"label": "", "amount": 1}}}',
            '{"currency": "CAD", "prices": {"website": {"label": "x", "amount": 1, "note": 1}}}',
            '{"currency": "CAD", "prices": {"website": {"label": "x", "amount": 1}}, "extra": 1}',
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=raw):
                parse_price_table(raw, TOPICS)
        with self.assertRaises(RuntimeError):
            base.settings(price_table="not json").validate()


class SummaryUsesThePlan(EndCard):
    def test_the_summary_email_is_the_build_plan_with_the_owners_price(self):
        table = json.dumps({"currency": "CAD", "prices": {"logo": {"label": "Logo package", "amount": 1200}}})
        with TestClient(self.make(price_table=table)) as client:
            sid, headers = self.session(client)
            repo = client.app.state.controller.repository
            record = repo.load(sid)
            state = copy.deepcopy(record.state)
            state["charter"] = {"revision": 1, "topic": "logo", "next": "", "updated_at": 1000,
                                "dimensions": [{"id": "objectives", "level": 2, "captured": "A mark for a bakery"}]}
            repo.save(sid, state, record.token)
            r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(200, r.status_code, r.text)
        text = pdf_text(self.sender.calls[0][1])
        self.assertIn("Your build plan", text)
        self.assertIn("Logo package: CAD 1,200.00", text)
        self.assertIn("A mark for a bakery", text)


if __name__ == "__main__":
    unittest.main()

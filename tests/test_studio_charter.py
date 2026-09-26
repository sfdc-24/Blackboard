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
from app import summary_pdf as sp  # noqa: E402
from app.summary_pdf import (DRAFT_NEXT_STEP, NEXT_STEP, PLAN_TERMS, PRICED_AFTER_REVIEW, TO_CONFIRM,  # noqa: E402
                             DesignImageError, build_summary_pdf, quote_number)
from workers import charter as ch  # noqa: E402
from workers.policy import USE_POLICY  # noqa: E402
from workers.topics import CHARTER_FRAMES, DEFAULT_CHARTER_FRAME, TOPICS, charter_frame, quote_lines  # noqa: E402

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


# --- the build plan and quote PDF ------------------------------------------------
QUOTE_RE = re.compile(r"Q-\d{8}-[0-9A-F]{6}")
LINE_IDS = lambda t: [line[0] for line in quote_lines(t)]  # noqa: E731


def plan_state(topic="website", said=3, levels=None):
    frame = [d[0] for d in charter_frame(topic)]
    levels = levels if levels is not None else {frame[0]: 3, frame[1]: 1}
    dims = [{"id": i, "level": levels.get(i, 0),
             "captured": ("About " + i) if levels.get(i, 0) else ""} for i in frame]
    return {"session_id": "s-0123456789abcdef0123456789abcdef", "created_at": 1790340000, "topic": topic,
            "artifact": {"id": "screen", "kind": "screen", "label": "Crumb and Co.", "children": [
                {"id": "h", "kind": "section", "label": "Hero"}, {"id": "m", "kind": "section", "label": "Menu"}]},
            "transcript": [{"role": "visitor", "text": "we bake sourdough every morning, line %d" % i}
                           for i in range(said)],
            "recap": {"text": "A warm company site where locals order ahead."},
            "charter": {"revision": 3, "topic": topic, "next": "Who should visit first?", "updated_at": 1790340100,
                        "dimensions": dims}}


def text_of(pdf: bytes) -> str:
    """The page text, with PDF string escapes undone."""
    return pdf_text(pdf).replace("\\(", "(").replace("\\)", ")")


def pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type /Page\b", pdf))


def table(lines=None, support=None, currency="CAD"):
    raw = {"currency": currency}
    if lines is not None:
        raw["lines"] = lines
    if support is not None:
        raw["support"] = support
    return parse_price_table(json.dumps(raw), TOPICS, LINE_IDS)


FULL_WEBSITE = {"discovery": 800, "design": 1200, "build": 2400, "test": 600, "handover": 400.5}
FULL_SUPPORT = {"subscription": 150, "on_demand": 120}


def complete(lines=None):
    """A table that prices everything the quote shows: its lines and both support options."""
    return table({"website": lines or FULL_WEBSITE}, FULL_SUPPORT)


class Quote(unittest.TestCase):
    def test_the_header_names_a_quote_with_a_number_a_date_validity_and_who_it_is_for(self):
        pdf = build_summary_pdf(plan_state(), prepared_for="o***@example.com", price_table=complete())
        text = text_of(pdf)
        self.assertIn("Build plan and quote", text)
        numbers = set(QUOTE_RE.findall(text))
        self.assertEqual(1, len(numbers))
        self.assertTrue(next(iter(numbers)).startswith("Q-20260925-"))
        self.assertIn("September 25, 2026", text)
        self.assertIn("Valid for 30 days, until October 25, 2026", text)
        self.assertIn("o***@example.com", text)
        self.assertNotIn("0123456789abcdef", text)                        # never the session id
        self.assertEqual(quote_number(plan_state()), next(iter(numbers)))
        other = plan_state()
        other["session_id"] = "s-ffffffffffffffffffffffffffffffff"
        self.assertNotEqual(quote_number(other), quote_number(plan_state()))

    def test_the_project_and_the_scope_of_work_from_the_charter(self):
        text = text_of(build_summary_pdf(plan_state()))
        self.assertIn("we bake sourdough every morning, line 0", text)     # the goal: the first line
        self.assertIn("Build a website", text)                             # the session type
        self.assertIn("Scope of work", text)
        self.assertIn("About type", text)                                  # level 3: what was said
        self.assertIn("Noted so far: About audience", text)                # level 1
        self.assertIn(TO_CONFIRM, text)                                    # level 0-1
        self.assertIn("Next to settle: Who should visit first?", text)

    def test_line_items_come_from_the_frame_and_the_canvas_priced_after_review_without_a_table(self):
        text = text_of(build_summary_pdf(plan_state()))
        for item in ("Discovery and plan", "Design", "Build", "Test and launch", "Handover"):
            self.assertIn(item, text)
        self.assertIn("Covers: Hero, Menu", text)                          # the build line reads the canvas
        self.assertEqual(len(quote_lines("website")) + 2, text.count(PRICED_AFTER_REVIEW))  # + two support options
        self.assertNotIn("Total", text)
        self.assertNotIn("CAD", text)
        admin = text_of(build_summary_pdf(plan_state("salesforce_admin")))
        for item in ("Discovery", "Configuration", "Automation", "Data", "Testing", "Training and handover"):
            self.assertIn(item, admin)
        self.assertIn("Salesforce admin", admin)

    def test_a_total_only_when_every_line_is_priced_and_the_terms_split_it(self):
        full = text_of(build_summary_pdf(plan_state(), price_table=table({"website": FULL_WEBSITE})))
        self.assertIn("Total", full)
        self.assertIn("CAD 5,400.50", full)
        self.assertIn("CAD 2,700.25", full)                                # 50% to start
        self.assertIn("CAD 2,700.25", full.split("50% on delivery and handover.")[1])
        partial = dict(FULL_WEBSITE)
        partial.pop("handover")
        some = text_of(build_summary_pdf(plan_state(), price_table=table({"website": partial})))
        self.assertIn("CAD 2,400.00", some)
        self.assertIn(PRICED_AFTER_REVIEW, some)
        self.assertNotIn("Total", some)
        self.assertNotIn("CAD 5,", some)
        other_topic = text_of(build_summary_pdf(plan_state(), price_table=table({"logo": {"discovery": 300}})))
        self.assertNotIn("Total", other_topic)
        self.assertNotIn("CAD 300", other_topic)

    def test_the_payment_terms_support_options_and_next_step_are_exact(self):
        text = text_of(build_summary_pdf(plan_state()))
        self.assertIn("50% to start build and test.", text)
        self.assertIn("50% on delivery and handover.", text)
        self.assertIn(PLAN_TERMS[2], text)
        self.assertIn("Subscription", text)
        self.assertIn("On demand", text)
        self.assertIn(DRAFT_NEXT_STEP, text)                                 # a draft has nothing to accept yet
        self.assertNotIn(NEXT_STEP, text)
        self.assertEqual("Reply to this email to accept, or book a kickoff at sfdc24.com.", NEXT_STEP)
        self.assertEqual("Reply to this email with questions, or book a kickoff at sfdc24.com.", DRAFT_NEXT_STEP)
        priced = text_of(build_summary_pdf(plan_state(), price_table=table(support={"subscription": 150, "on_demand": 120})))
        self.assertIn("CAD 150.00 / month", priced)
        self.assertIn("CAD 120.00 / hour", priced)
        full = text_of(build_summary_pdf(plan_state(), price_table=complete()))
        self.assertIn(NEXT_STEP, full)
        self.assertNotIn(DRAFT_NEXT_STEP, full)

    def test_the_discussion_is_a_short_appendix_at_the_end(self):
        text = text_of(build_summary_pdf(plan_state(said=20)))
        self.assertIn("Session notes", text)
        self.assertGreater(text.index("Session notes"), text.index(DRAFT_NEXT_STEP))
        self.assertIn("A warm company site where locals order ahead.", text)
        self.assertIn("line 19", text)
        self.assertNotIn("line 11,", text + ",")                           # the last eight lines only

    def test_the_watermark_is_on_every_page(self):
        pdf = build_summary_pdf(plan_state(said=40))
        n = pages(pdf)
        self.assertGreaterEqual(n, 2)
        rotated = re.findall(r"0\.8192 0\.5736 -0\.5736 0\.8192 [-\d.]+ [-\d.]+ cm", pdf_text(pdf))
        self.assertEqual(n, len(rotated))

    def test_without_a_charter_the_summary_is_as_before(self):
        state = plan_state()
        state.pop("charter")
        text = text_of(build_summary_pdf(state, prepared_for="o***@example.com",
                                         price_table=table({"website": FULL_WEBSITE})))
        self.assertIn("Your SFDC24 working session", text)
        self.assertNotIn("Build plan and quote", text)
        self.assertNotIn(PLAN_TERMS[0], text)
        self.assertIsNone(QUOTE_RE.search(text))
        self.assertEqual(1, text.count("sfdc24.com"))                        # the footer only


def png_bytes(w, h, color=(200, 160, 110)):
    import io as _io
    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def image_widths(pdf: bytes) -> list:
    return [int(w) for w in re.findall(rb"/Subtype /Image.*?/Width (\d+)", pdf, re.S)]


class QuoteDesign(unittest.TestCase):
    """Owner, 2026-09-26: "a nice looking quote PDF that has images, design
    elements and a professional format"."""

    def test_page_one_shows_the_brand_the_status_the_facts_and_what_you_get(self):
        full = text_of(build_summary_pdf(plan_state(), price_table=complete()))
        draft = text_of(build_summary_pdf(plan_state()))
        for text in (full, draft):
            self.assertIn("SFDC24", text)                                  # the wordmark in the band
            self.assertIn("What you get", text)
            for line in quote_lines("website"):
                self.assertIn(line[1], text)
        self.assertIn(sp.STATUS_READY, full)
        self.assertNotIn(sp.STATUS_DRAFT, full)
        self.assertIn(sp.STATUS_DRAFT, draft)
        self.assertNotIn(sp.STATUS_READY, draft)

    def test_only_a_complete_quote_can_be_accepted_support_included(self):
        full = text_of(build_summary_pdf(plan_state(), price_table=complete()))
        self.assertIn("Acceptance", full)
        self.assertIn(sp.ACCEPT_LINE, full)
        self.assertIn("Signature", full)
        self.assertIn(sp.STATUS_READY, full)
        self.assertNotIn(sp.DRAFT_LINE, full)
        partial = dict(FULL_WEBSITE)
        partial.pop("test")
        drafts = {
            "nothing priced": table(support=FULL_SUPPORT),
            "a line missing": table({"website": partial}, FULL_SUPPORT),
            "support absent": table({"website": FULL_WEBSITE}),                       # Codex on abbbd93
            "support partial": table({"website": FULL_WEBSITE}, {"subscription": 150}),
        }
        for name, prices in drafts.items():
            text = text_of(build_summary_pdf(plan_state(), price_table=prices))
            self.assertNotIn("Acceptance", text, name)
            self.assertNotIn("Signature", text, name)
            self.assertNotIn(sp.STATUS_READY, text, name)
            self.assertIn(sp.STATUS_DRAFT, text, name)
            self.assertIn(sp.DRAFT_LINE, flow(text), name)

    def test_the_milestones_keep_their_exact_words_and_say_half_when_unpriced(self):
        draft = text_of(build_summary_pdf(plan_state()))
        self.assertIn(PLAN_TERMS[0], draft)
        self.assertIn(PLAN_TERMS[1], draft)
        self.assertIn(sp.MILESTONE_UNPRICED[0], draft)
        self.assertIn(sp.MILESTONE_UNPRICED[1], draft)
        self.assertLess(draft.index(PLAN_TERMS[0]), draft.index(PLAN_TERMS[1]))

    def test_the_snapshot_is_framed_scaled_and_captioned(self):
        big = png_bytes(3200, 2000)
        pdf = build_summary_pdf(plan_state(), design_png=big)
        widths = image_widths(pdf)
        self.assertEqual([sp.FIGURE_MAX_SIDE], widths)                     # scaled once, on page one
        self.assertIn(sp.FIGURE_CAPTION, text_of(pdf))
        small = build_summary_pdf(plan_state(), design_png=png_bytes(400, 300))
        self.assertEqual([400], image_widths(small))                       # small ones are left alone
        none = text_of(build_summary_pdf(plan_state()))
        self.assertIn(sp.WIREFRAME_CAPTION, none)                          # no snapshot: the canvas as a wireframe
        self.assertIn("Hero", none)
        self.assertIn("Menu", none)
        broken = big[:40] + b"\x00" * 200
        with self.assertRaises(DesignImageError):
            build_summary_pdf(plan_state(), design_png=broken)

    def test_the_pages_are_bounded_and_the_backstop_refuses_more(self):
        state = plan_state(said=400, levels={d[0]: 3 for d in charter_frame("website")})
        for d in state["charter"]["dimensions"]:
            d["captured"] = "x " * 200
        state["recap"] = {"text": "long recap " * 400}
        state["events"] = [{"type": "confirm", "payload": {"text": "built " * 80}} for _ in range(60)]
        pdf = build_summary_pdf(state, design_png=png_bytes(1600, 4000), price_table=table({"website": FULL_WEBSITE}))
        self.assertLessEqual(pages(pdf), sp.QUOTE_MAX_PAGES)
        keep = sp.QUOTE_MAX_PAGES
        try:
            sp.QUOTE_MAX_PAGES = 1
            with self.assertRaises(sp.SummaryTooLong):
                build_summary_pdf(plan_state())
        finally:
            sp.QUOTE_MAX_PAGES = keep

    def test_the_same_session_renders_the_same_bytes_dated_by_the_session(self):
        one = build_summary_pdf(plan_state(), design_png=png_bytes(300, 200))
        two = build_summary_pdf(plan_state(), design_png=png_bytes(300, 200))
        self.assertEqual(one, two)
        self.assertIn(b"/CreationDate (D:20260925", one)

    def test_text_that_would_overflow_its_column_is_shortened(self):
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "", 8)
        long = "Testing on phones and desktops, launch and go-live checks, and a lot more besides"
        fitted = sp._fit(pdf, long, 49)
        self.assertTrue(fitted.endswith("..."))
        self.assertLessEqual(pdf.get_string_width(fitted), 49)
        self.assertEqual("Short", sp._fit(pdf, "Short", 49))


def flow(text: str) -> str:
    """The shown strings in order, joined: wrapped lines read as one sentence."""
    return " ".join(re.findall(r"\(((?:[^()\\]|\\.)*)\) Tj", text))


class QuoteWording(unittest.TestCase):
    """Coordinator review of the sample, 2026-09-26: a line's description must
    describe its work, and the canvas appendix must read as a site map."""

    def lines_text(self, state, prices=None):
        return text_of(build_summary_pdf(state, price_table=table({"website": prices or FULL_WEBSITE})))

    def test_each_line_describes_its_work_tailored_with_the_session(self):
        state = plan_state(levels={"business": 3, "design": 2})
        for d in state["charter"]["dimensions"]:
            if d["id"] == "business":
                d["captured"] = "Crumb and Co., a two-person sourdough bakery."
            if d["id"] == "design":
                d["captured"] = "Warm browns and cream, real photography."
        text = self.lines_text(state)
        raw = text.split("Line items")[1].split("Total")[0]
        lines = flow(raw)
        self.assertIn("Goals, audience and a page-by-page plan for Crumb and Co.", lines)
        self.assertIn("(Crumb and Co.)", raw)                               # the name wraps as one piece
        self.assertNotRegex(raw, r"\((and )?Co\.\)")
        self.assertIn("Style, palette and type: warm browns and cream, real photography", lines)
        self.assertNotIn("a two-person sourdough bakery", lines)             # not the business's own sentence
        self.assertIn("Covers: Hero, Menu", lines)                         # build keeps its list
        self.assertIn("Testing on phones and desktops, launch and go-live checks", lines)
        self.assertIn("Walkthrough, admin access and documentation", lines)

    def test_an_unsettled_dimension_or_no_name_leaves_the_plain_work(self):
        state = plan_state(levels={"design": 1})
        state["artifact"]["label"] = ""
        for d in state["charter"]["dimensions"]:
            if d["id"] == "design":
                d["captured"] = "Maybe blue."
        text = flow(self.lines_text(state))
        self.assertIn("Goals, audience and a page-by-page plan", text)
        self.assertNotIn("page-by-page plan for", text)
        self.assertIn("Style, palette and type", text)
        self.assertNotIn("Style, palette and type: maybe blue", text)

    def test_a_long_fact_is_cut_at_a_word_and_a_proper_name_keeps_its_capital(self):
        state = plan_state(levels={"design": 3})
        state["artifact"]["label"] = "Crumb - shop"
        for d in state["charter"]["dimensions"]:
            if d["id"] == "design":
                d["captured"] = "Crumb brown and cream with " + "hand drawn loaves " * 8
        text = flow(self.lines_text(state))
        tail = text.split("Style, palette and type: ")[1]
        self.assertTrue(tail.startswith("Crumb brown"))                    # the project's name keeps its capital
        self.assertIn("...", tail.split("CAD")[0])

    def test_the_canvas_is_a_site_map_not_a_node_tree(self):
        state = plan_state()
        state["artifact"]["children"][0]["children"] = [
            {"id": "hh", "kind": "heading", "label": "Fresh bread every morning"},
            {"id": "sub", "kind": "section", "label": "Opening hours"}]
        pdf = build_summary_pdf(state)
        text = text_of(pdf)
        self.assertIn("Site map", text)
        self.assertNotIn("The canvas", text)
        for kind in ("screen:", "section:", "heading:"):
            self.assertNotIn(kind, text)
        self.assertNotIn(b"/BaseFont /Courier", pdf)
        notes = text.split("Site map")[1]
        for name in ("Crumb and Co.", "Hero", "Opening hours", "Menu"):
            self.assertIn(name, notes)
        self.assertNotIn("Fresh bread every morning", notes)              # content, not structure
        self.assertEqual([(0, "Crumb and Co."), (1, "Hero"), (2, "Opening hours"), (1, "Menu")],
                         sp._site_map(state["artifact"]))

    def test_the_site_map_is_bounded(self):
        deep = {"id": "d", "kind": "section", "label": "L0", "children": []}
        node = deep
        for i in range(1, 8):
            child = {"id": "d%d" % i, "kind": "section", "label": "L%d" % i, "children": []}
            node["children"].append(child)
            node = child
        self.assertEqual(sp.SITE_MAP_DEPTH, max(d for d, _ in sp._site_map(deep)))
        wide = {"id": "w", "kind": "screen", "label": "Wide", "children": [
            {"id": "c%d" % i, "kind": "section", "label": "Part %d" % i} for i in range(80)]}
        self.assertEqual(sp.SITE_MAP_MAX, len(sp._site_map(wide)))


class PriceTable(unittest.TestCase):
    def test_empty_is_none_and_a_good_table_parses(self):
        self.assertIsNone(parse_price_table("", TOPICS, LINE_IDS))
        self.assertIsNone(parse_price_table("   ", TOPICS, LINE_IDS))
        good = table({"salesforce_data": {"model": 1250.5}}, {"on_demand": 140}, currency="USD")
        self.assertEqual({"currency": "USD", "lines": {"salesforce_data": {"model": 1250.5}},
                          "support": {"on_demand": 140.0}}, good)
        self.assertEqual({}, table(support={"subscription": 99})["lines"])

    def test_anything_odd_refuses_the_whole_table(self):
        bad = [
            "not json", "[]", '{"currency": "CAD"}',
            '{"currency": "EUR", "support": {"subscription": 1}}',
            '{"lines": {"website": {"build": 1}}}',
            '{"currency": "CAD", "lines": {}}',
            '{"currency": "CAD", "lines": {"websites": {"build": 1}}}',
            '{"currency": "CAD", "lines": {"website": {"automation": 1}}}',
            '{"currency": "CAD", "lines": {"website": {}}}',
            '{"currency": "CAD", "lines": {"website": {"build": 0}}}',
            '{"currency": "CAD", "lines": {"website": {"build": 1000001}}}',
            '{"currency": "CAD", "lines": {"website": {"build": 1.234}}}',
            '{"currency": "CAD", "lines": {"website": {"build": true}}}',
            '{"currency": "CAD", "lines": {"website": {"build": "100"}}}',
            '{"currency": "CAD", "support": {}}',
            '{"currency": "CAD", "support": {"weekly": 10}}',
            '{"currency": "CAD", "support": {"subscription": -5}}',
            '{"currency": "CAD", "support": {"subscription": 5}, "extra": 1}',
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=raw):
                parse_price_table(raw, TOPICS, LINE_IDS)
        with self.assertRaises(RuntimeError):
            base.settings(price_table="not json").validate()
        base.settings(price_table=json.dumps({"currency": "CAD", "lines": {"website": FULL_WEBSITE}})).validate()


class SummaryUsesTheQuote(EndCard):
    def test_the_summary_email_is_the_quote_prepared_for_the_masked_address(self):
        prices = json.dumps({"currency": "CAD", "lines": {"logo": {"discovery": 300, "design": 900, "build": 600,
                                                                   "test": 100, "handover": 100}},
                             "support": {"subscription": 150, "on_demand": 120}})
        with TestClient(self.make(price_table=prices)) as client:
            sid, headers = self.session(client)
            repo = client.app.state.controller.repository
            record = repo.load(sid)
            state = copy.deepcopy(record.state)
            state["charter"] = {"revision": 1, "topic": "logo", "next": "", "updated_at": 1000,
                                "dimensions": [{"id": "objectives", "level": 2, "captured": "A mark for a bakery"}]}
            repo.save(sid, state, record.token)
            r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(200, r.status_code, r.text)
        text = text_of(self.sender.calls[0][1])
        self.assertIn("Build plan and quote", text)
        self.assertIn("Prepared for", text)
        self.assertIn("o***@example.com", text)
        self.assertNotIn("operator@example.com", text)
        self.assertNotIn(sid, text)
        self.assertIsNotNone(QUOTE_RE.search(text))
        self.assertIn("CAD 2,000.00", text)                                  # the total, fully priced
        self.assertIn("A mark for a bakery", text)


# --- Codex NO-GO on #275 at abbbd93 -------------------------------------------------
PROBES = {
    "email": "reach me at jane.doe+work@example.org",
    "key": "our key is sk-proj-AbCdEf1234567890XyZ",
    "github": "token ghp_ABCDEFGHIJKLMNOPQRSTUVWX1234",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N",
    "password": "my password is Hunter2!",
    "sin": "SIN 046 454 286",
    "phone": "call 416-555-0199",
    "bearer": "Authorization: Bearer abc.def.ghi",
}
LEAKS = ("jane.doe", "example.org", "sk-proj", "ghp_", "eyJhbGci", "Hunter2", "046 454 286", "416-555-0199",
         "abc.def.ghi")


def probed_state():
    state = plan_state(said=0, levels={"business": 3, "design": 3})
    joined = "; ".join(PROBES.values())
    state["transcript"] = [{"role": "visitor", "text": t} for t in PROBES.values()]
    state["recap"] = {"text": "Recap: " + joined}
    state["events"] = [{"type": "confirm", "payload": {"text": "Built it for " + PROBES["email"]}}]
    state["questions"] = [{"status": "answered", "prompt": "Who signs? " + PROBES["phone"], "options": [],
                           "freeform_answer": PROBES["password"]}]
    state["charter"]["next"] = "Confirm " + PROBES["sin"]
    for d in state["charter"]["dimensions"]:
        if d["id"] in ("business", "design"):
            d["captured"] = "Owner " + PROBES["email"] + " " + PROBES["key"]
    state["artifact"]["children"].append({"id": "c", "kind": "section", "label": "Contact " + PROBES["phone"]})
    return state


class Redaction(unittest.TestCase):
    def test_no_probe_survives_on_any_quote_surface(self):
        for prices in (None, complete()):
            text = flow(text_of(build_summary_pdf(probed_state(), price_table=prices)))
            for leak in LEAKS:
                self.assertNotIn(leak, text, leak)
            for mark in ("[email]", "[key]", "[token]", "password [redacted]", "[number]", "Bearer [redacted]"):
                self.assertIn(mark, text, mark)

    def test_the_plain_summary_is_redacted_too(self):
        state = probed_state()
        state.pop("charter")
        text = flow(text_of(build_summary_pdf(state)))
        for leak in LEAKS:
            self.assertNotIn(leak, text, leak)

    def test_ordinary_words_prices_and_dates_are_kept(self):
        kept = "A menu for CAD 1,800 by 2026-11-15, open 7 am to 3 pm at 1024 Queen St W; pin the header"
        self.assertEqual(kept, sp.redact(kept))
        self.assertEqual("call [phone] or [number]", sp.redact("call 555-0199 or +1 (416) 555-0199"))

    # A voice transcript spells an address out or spaces it, and typed text can
    # split one with markup; each form reached the PDF before (claude-code-cli
    # probe on the #275 repair head).
    SPOKEN = {
        "reach me at jane dot doe at example dot org": ("jane", "doe", "example"),
        "email jane at gmail dot com please": ("jane", "gmail"),
        "reach me at jane_doe at company dot ca": ("jane_doe", "company"),
        "jane.doe @ example.org": ("jane.doe", "example.org"),
        "jane (at) example (dot) org": ("jane", "example"),
        "zqalice<a>@x</a>yz.com": ("zqalice", "xyz"),
        "my password's Hunter2": ("Hunter2",),
        "passphrase: blue horse staple": ("blue", "horse", "staple"),
    }

    def test_spoken_spaced_and_split_addresses_and_passphrases_are_redacted(self):
        for said, leaks in self.SPOKEN.items():
            out = sp.redact(said)
            for leak in leaks:
                self.assertNotIn(leak, out, (said, out))

    def test_ordinary_sentences_with_at_and_dot_are_kept(self):
        for kept in ("meet at noon, dot the i's", "we are at Queen and Bathurst", "I work at Shopify.com",
                     "prices at 5.99 each", "the logo sits at top. Menu is next", "we meet at 10.30 daily",
                     "a bakery at Queen . The shop"):
            self.assertEqual(kept, sp.redact(kept))

    def test_redaction_stays_linear_on_hostile_input(self):
        # Before the address patterns were anchored to the start of a run of
        # address characters, "a." * 2000 + "@" took about 3.6 s to redact.
        import time
        for hostile in ("a." * 2000 + "@", "a.b" * 1300, "x dot " * 700, "a (at) " * 600,
                        "jane " + "dot x " * 700 + "at b", "<a" * 2000):
            start = time.perf_counter()
            sp.redact(hostile[:4000])
            self.assertLess(time.perf_counter() - start, 1.0, hostile[:20])


class DraftSemantics(unittest.TestCase):
    def test_a_draft_is_not_styled_as_an_offer(self):
        for prices in (None, table({"website": FULL_WEBSITE})):
            text = flow(text_of(build_summary_pdf(plan_state(), price_table=prices)))
            self.assertIn(sp.DRAFT_TITLE, text)
            self.assertNotIn(sp.QUOTE_TITLE, text)
            self.assertIsNone(QUOTE_RE.search(text))                        # no offer number
            self.assertRegex(text, r"D-\d{8}-[0-9A-F]{6}")
            self.assertNotIn("Valid for", text)
            self.assertIn(sp.DRAFT_STATUS, text)
            self.assertIn("Draft D-", text)                                   # the footer
        full = flow(text_of(build_summary_pdf(plan_state(), price_table=complete())))
        self.assertIn(sp.QUOTE_TITLE, full)
        self.assertIn("Valid for 30 days", full)
        self.assertIsNotNone(QUOTE_RE.search(full))
        self.assertNotIn(sp.DRAFT_STATUS, full)
        self.assertEqual(quote_number(plan_state())[2:], quote_number(plan_state(), "D")[2:])


def longest_address():
    domain = ("b" * 60 + ".") * 3                                         # the visitor rule's longest shape
    domain = domain + "c" * (254 - 65 - len(domain) - 4) + ".com"
    address = "a" * 64 + "@" + domain
    assert len(address) == 254
    return address


def y_of(text, label):
    return float(re.search(r"([\d.]+) Td [\d. ]*rg \(" + re.escape(label) + r"\) Tj", text).group(1))


class FactsPanel(unittest.TestCase):
    def test_the_longest_admitted_address_is_bounded_and_nothing_overlaps(self):
        from app.summary_pdf import mask_email
        masked = mask_email(longest_address())
        text = text_of(build_summary_pdf(plan_state(), prepared_for=masked, price_table=complete()))
        shown = [m for m in re.findall(r"\(([^()]*\.com)\) Tj", text) if "***@" in m]
        self.assertEqual(1, len(shown))
        self.assertLessEqual(len(shown[0]), sp.PREPARED_FOR_MAX)
        self.assertIn("...", shown[0])
        self.assertTrue(shown[0].startswith("a***@"))
        self.assertGreater(y_of(text, "Session type") - y_of(text, "What you get"), 20)   # below the panel

    def test_the_panel_grows_with_a_value_that_wraps(self):
        from app.summary_pdf import mask_email
        keep = sp.PREPARED_FOR_MAX
        try:
            sp.PREPARED_FOR_MAX = 400                                          # no bound: it wraps three lines
            text = text_of(build_summary_pdf(plan_state(), prepared_for=mask_email(longest_address()),
                                             price_table=complete()))
        finally:
            sp.PREPARED_FOR_MAX = keep
        self.assertGreater(y_of(text, "Session type") - y_of(text, "What you get"), 20)


def page_segments(text):
    return re.split(r"\(Page \d+ of \) Tj", text)


class SiteMapPlacement(unittest.TestCase):
    def big_map_state(self):
        state = plan_state(said=8)
        state["artifact"]["children"] = [{"id": "p%d" % i, "kind": "section", "label": "Page %02d name" % i}
                                         for i in range(40)]
        return state

    def test_a_full_site_map_stays_on_one_page(self):
        text = text_of(build_summary_pdf(self.big_map_state(), price_table=complete()))
        where = [i for i, seg in enumerate(page_segments(text)) if "(Site map)" in seg]
        self.assertEqual(1, len(where))
        seg = page_segments(text)[where[0]]
        rows = re.findall(r"\(Page \d\d name\) Tj", seg)
        self.assertEqual(sp.SITE_MAP_MAX - 1, len(rows))                     # 29 sections under the root
        self.assertIn("(Crumb and Co.) Tj", seg)
        for other in page_segments(text)[where[0] + 1:]:
            self.assertNotRegex(other, r"\(Page \d\d name\) Tj")

    def test_a_small_site_map_follows_on_the_same_page(self):
        text = text_of(build_summary_pdf(plan_state(), price_table=complete()))
        seg = next(seg for seg in page_segments(text) if "(Site map)" in seg)
        self.assertIn("(Session notes)", seg)


class Bounded(unittest.TestCase):
    def maximum_state(self):
        state = plan_state(said=0, levels={d[0]: 3 for d in charter_frame("website")})
        for d in state["charter"]["dimensions"]:
            d["captured"] = "words " * 40
        state["charter"]["next"] = "next " * 60
        state["transcript"] = [{"role": "visitor", "text": "said " * 120} for _ in range(200)]
        state["recap"] = {"text": "recap " * 400}
        state["events"] = [{"type": "confirm", "payload": {"text": "built " * 60}} for _ in range(60)]
        state["questions"] = [{"status": "answered", "prompt": "question " * 25, "options": [],
                               "freeform_answer": "answer " * 28} for _ in range(40)]
        state["artifact"]["label"] = "label " * 20
        state["artifact"]["children"] = [
            {"id": "s%d" % i, "kind": "section", "label": "section name %d " % i * 6,
             "children": [{"id": "t%d" % i, "kind": "form", "label": "form %d " % i * 8}]} for i in range(40)]
        return state

    def test_the_maximum_admitted_state_fits_the_bound(self):
        from app.summary_pdf import mask_email
        pdf = build_summary_pdf(self.maximum_state(), design_png=png_bytes(1600, 4000), price_table=complete(),
                                prepared_for=mask_email(longest_address()))
        self.assertLessEqual(pages(pdf), sp.QUOTE_MAX_PAGES)
        self.assertIn("Session notes", text_of(pdf))                      # shortened, not dropped

    def test_an_over_full_state_shortens_its_notes_to_fit(self):
        keep = sp.QUOTE_MAX_PAGES
        try:
            sp.QUOTE_MAX_PAGES = 4
            pdf = build_summary_pdf(self.maximum_state(), price_table=complete())
        finally:
            sp.QUOTE_MAX_PAGES = keep
        self.assertLessEqual(pages(pdf), 4)
        text = text_of(pdf)
        self.assertIn(sp.QUOTE_TITLE, text)
        self.assertIn("Acceptance", text)                                  # the offer itself is never cut

    def test_long_canvas_labels_are_cut_at_a_word(self):
        state = plan_state()
        state["artifact"]["children"][0]["label"] = "Hero section with sourdough loaves baked at dawn daily"
        text = flow(text_of(build_summary_pdf(state, price_table=complete())))
        self.assertIn("Covers: Hero section with sourdough...", text)
        self.assertEqual("Hero section with sourdough...", sp.words("Hero section with sourdough loaves", 30))
        self.assertEqual("abcdefghijklmnopqrstuvwxyz0...", sp.words("abcdefghijklmnopqrstuvwxyz0123456789", 30))


class SummaryTooLongRoute(EndCard):
    def test_an_over_long_plan_is_refused_with_a_clear_422_and_nothing_is_sent(self):
        prices = json.dumps({"currency": "CAD", "support": {"subscription": 150, "on_demand": 120}})
        keep = sp.QUOTE_MAX_PAGES
        try:
            sp.QUOTE_MAX_PAGES = 1
            with TestClient(self.make(price_table=prices)) as client:
                sid, headers = self.session(client)
                repo = client.app.state.controller.repository
                record = repo.load(sid)
                state = copy.deepcopy(record.state)
                state["charter"] = {"revision": 1, "topic": "logo", "next": "", "updated_at": 1000,
                                    "dimensions": [{"id": "objectives", "level": 2, "captured": "A mark"}]}
                repo.save(sid, state, record.token)
                r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        finally:
            sp.QUOTE_MAX_PAGES = keep
        self.assertEqual(422, r.status_code, r.text)
        self.assertIn("nothing was sent", r.json()["detail"])
        self.assertEqual([], self.sender.calls)
        self.assertNotIn("summary", self.state(sid))


if __name__ == "__main__":
    unittest.main()

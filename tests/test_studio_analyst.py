"""The analyst lane: a second agent beside the builder on the same session.

What is under test is the gate and the lane, never a provider: the analyst's
output is data checked before it reaches the page, its model call runs
beside a build and its commit waits for the build rather than disrupting it, it asks at most one question at a time, and once it is
active the builder stops asking so the two agents never talk over each other.
"""
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, FakeTalk, FakeVoiceClient, IDs, MemoryStore, make_controller, settings,
)
from app.core import CommandError  # noqa: E402
from app.state import StateConflict  # noqa: E402
from app.main import create_app  # noqa: E402
from workers import analyst as an  # noqa: E402

MODEL_RAW = {
    "domain": "Bakery ordering",
    "objects": [
        {"id": "account", "name": "Account", "standard": True, "purpose": "The bakery customer",
         "fields": [{"name": "Name", "type": "Text"}, {"name": "Phone", "type": "Phone"}]},
        {"id": "order", "name": "Order", "standard": True, "purpose": "A pre-order for pickup",
         "fields": [{"name": "Pickup date", "type": "Date"}, {"name": "Total", "type": "Currency"}]},
    ],
    "relationships": [{"from": "order", "to": "account", "kind": "lookup", "label": "placed by"}],
    "findings": ["Bakeries usually take pre-orders a day ahead."],
    "question": {"prompt": "Do customers pay when they order or at pickup?",
                 "reason": "It decides whether orders need a payment record.",
                 "options": [{"option_id": "now", "label": "When ordering", "consequence": "Adds a Payment object"},
                             {"option_id": "pickup", "label": "At pickup", "consequence": "Orders stay unpaid until collected"}]},
}


class FakeAnthropic:
    def __init__(self, raw=None, blocks=None):
        self.calls = []
        content = blocks if blocks is not None else [
            SimpleNamespace(type="server_tool_use", name="web_search"),
            SimpleNamespace(type="tool_use", name="record_analysis", input=copy.deepcopy(raw or MODEL_RAW)),
        ]
        self.resp = SimpleNamespace(stop_reason="tool_use", content=content)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.resp


class FakeAnalyst:
    def __init__(self, raw=None, fail=False):
        self.raw = raw or MODEL_RAW
        self.fail = fail
        self.calls = []

    def analyze(self, state, text, canvas):
        self.calls.append({"text": text, "canvas": canvas, "model": state.get("model")})
        if self.fail:
            raise RuntimeError("provider down")
        model, question, problems = an.validate(copy.deepcopy(self.raw))
        return {"model": model, "question": question, "problems": problems, "searched": 1}


class Gate(unittest.TestCase):
    def test_a_clean_analysis_passes_whole(self):
        model, question, problems = an.validate(copy.deepcopy(MODEL_RAW))
        self.assertEqual([], problems)
        self.assertEqual(["account", "order"], [o["id"] for o in model["objects"]])
        self.assertEqual("placed by", model["relationships"][0]["label"])
        self.assertEqual(["now", "pickup"], [o["option_id"] for o in question["options"]])

    def test_bad_parts_are_dropped_never_guessed(self):
        raw = copy.deepcopy(MODEL_RAW)
        raw["objects"].append({"id": "bad id!", "name": "X", "standard": False, "purpose": "", "fields": []})
        raw["objects"].append(dict(raw["objects"][0]))                      # duplicate id
        raw["relationships"].append({"from": "order", "to": "ghost", "kind": "lookup", "label": "x"})
        raw["relationships"].append({"from": "order", "to": "account", "kind": "junction", "label": "x"})
        model, question, problems = an.validate(raw)
        self.assertEqual(["account", "order"], [o["id"] for o in model["objects"]])
        self.assertEqual(1, len(model["relationships"]))
        self.assertEqual(4, len(problems))

    def test_a_malformed_question_is_dropped_and_an_empty_one_means_none(self):
        raw = copy.deepcopy(MODEL_RAW)
        raw["question"]["options"] = raw["question"]["options"][:1]
        self.assertIsNone(an.validate(raw)[1])
        raw["question"] = {"prompt": "", "reason": "", "options": []}
        model, question, problems = an.validate(raw)
        self.assertIsNone(question)
        self.assertEqual([], problems)

    def test_caps_hold(self):
        raw = copy.deepcopy(MODEL_RAW)
        raw["objects"] = [{"id": "o%d" % i, "name": "O%d" % i, "standard": False, "purpose": "",
                           "fields": [{"name": "f%d" % j, "type": "Text"} for j in range(30)]} for i in range(25)]
        raw["findings"] = ["x"] * 10 + ["y" * 400]
        model = an.validate(raw)[0]
        self.assertEqual(an.MAX_OBJECTS, len(model["objects"]))
        self.assertEqual(an.MAX_FIELDS, len(model["objects"][0]["fields"]))
        self.assertEqual(an.MAX_FINDINGS, len(model["findings"]))

    def test_the_request_researches_and_records_through_one_strict_tool(self):
        client = FakeAnthropic()
        out = an.Analyst(client=client).analyze({"transcript": [], "questions": []}, "a bakery app", "nothing yet")
        kw = client.calls[0]
        self.assertEqual("claude-opus-5", kw["model"])
        self.assertEqual({"type": "auto"}, kw["tool_choice"])
        self.assertEqual("web_search_20260209", kw["tools"][0]["type"])
        self.assertEqual(2, kw["tools"][0]["max_uses"])
        self.assertTrue(kw["tools"][1]["strict"])
        self.assertEqual(1, out["searched"])
        self.assertEqual("Bakery ordering", out["model"]["domain"])

    def test_no_recorded_analysis_changes_nothing(self):
        client = FakeAnthropic(blocks=[SimpleNamespace(type="text", text="I think you need an Order object.")])
        out = an.Analyst(client=client).analyze({"transcript": [], "questions": []}, "x", "")
        self.assertIsNone(out["model"])
        self.assertTrue(out["problems"])

    def test_research_can_be_switched_off(self):
        client = FakeAnthropic()
        an.Analyst(client=client, research=False).analyze({"transcript": [], "questions": []}, "x", "")
        self.assertEqual(["record_analysis"], [t["name"] for t in client.calls[0]["tools"]])


class Commit(unittest.TestCase):
    def open(self):
        controller, store, _ = make_controller()
        state, _ = controller.create_session("Blank", start="blank")
        return controller, store, state["session_id"]

    def test_the_model_and_one_question_are_recorded_beside_the_artifact(self):
        controller, _, sid = self.open()
        model, question, _ = an.validate(copy.deepcopy(MODEL_RAW))
        before = controller.repository.load(sid).state
        events = controller.commit_analysis(sid, model, question)
        after = controller.repository.load(sid).state
        self.assertEqual(["model.updated", "question.asked"], [e["type"] for e in events])
        self.assertEqual(model, after["model"])
        self.assertTrue(after["analyst"])
        self.assertEqual(before["artifact"], after["artifact"])                     # never edits the prototype
        self.assertEqual(before["artifact_version"], after["artifact_version"])
        asked = events[1]["payload"]["question"]
        self.assertEqual(("qa-1", "open", ["screen"]), (asked["question_id"], asked["status"],
                                                        asked["affected_artifact_ids"]))

    def test_no_second_question_while_one_is_open_and_never_the_same_one_twice(self):
        controller, _, sid = self.open()
        model, question, _ = an.validate(copy.deepcopy(MODEL_RAW))
        controller.commit_analysis(sid, model, question)
        again = controller.commit_analysis(sid, model, dict(question, prompt="Another question?"))
        self.assertEqual(["model.updated"], [e["type"] for e in again])
        state = controller.repository.load(sid).state
        state["questions"][0]["status"] = "answered"
        controller.repository.save(sid, state, controller.repository.load(sid).token)
        repeat = controller.commit_analysis(sid, model, question)
        self.assertEqual(["model.updated"], [e["type"] for e in repeat])

    def test_a_concurrent_write_is_retried_not_lost(self):
        controller, store, sid = self.open()
        model, question, _ = an.validate(copy.deepcopy(MODEL_RAW))
        real = controller.repository.load
        raced = []

        def load_then_race(session_id):
            record = real(session_id)
            if not raced:                                   # a build commits between read and save
                raced.append(True)
                state = copy.deepcopy(record.state)
                state["artifact"]["label"] = "Built meanwhile"
                controller.repository.save(session_id, state, record.token)
            return record

        controller.repository.load = load_then_race
        controller.commit_analysis(sid, model, question)
        controller.repository.load = real
        state = controller.repository.load(sid).state
        self.assertEqual("Built meanwhile", state["artifact"]["label"])            # the build survived
        self.assertEqual(model, state["model"])                                     # and so did the analysis

    def test_a_build_in_flight_is_never_disrupted_the_analysis_lands_after_it(self):
        controller, _, sid = self.open()
        model, question, _ = an.validate(copy.deepcopy(MODEL_RAW))
        # A build reserves the lock, exactly as execute() does before calling the builder.
        record = controller.repository.load(sid)
        state = record.state
        state["active_command"] = "cmd-build"
        reserved = controller.repository.save(sid, state, record.token)
        slept = []

        def finish_the_build_while_waiting(seconds):
            slept.append(seconds)
            if len(slept) == 3:                              # the build completes and saves with its token
                working = controller.repository.load(sid).state
                working["artifact"]["label"] = "Built"
                controller._event(working, "artifact.patch", {"ops": []})
                working["active_command"] = None
                controller.repository.save(sid, working, reserved)   # must not conflict

        controller.sleep = finish_the_build_while_waiting
        events = controller.commit_analysis(sid, model, question)
        after = controller.repository.load(sid).state
        self.assertEqual(3, len(slept))
        self.assertEqual("Built", after["artifact"]["label"])
        self.assertEqual(model, after["model"])
        seqs = [e["seq"] for e in after["events"]]
        self.assertEqual(sorted(set(seqs)), seqs)                              # one monotonic sequence
        self.assertGreater(events[0]["seq"], max(e["seq"] for e in after["events"] if e["type"] == "artifact.patch"))

    def test_a_build_that_never_finishes_does_not_hang_the_analyst(self):
        controller, _, sid = self.open()
        record = controller.repository.load(sid)
        state = record.state
        state["active_command"] = "cmd-stuck"
        controller.repository.save(sid, state, record.token)
        controller.sleep = lambda seconds: None
        controller.analysis_wait_polls = 5
        model, question, _ = an.validate(copy.deepcopy(MODEL_RAW))
        with self.assertRaises(StateConflict):
            controller.commit_analysis(sid, model, question)
        self.assertNotIn("model", controller.repository.load(sid).state)

    def test_a_stopped_session_refuses(self):
        controller, _, sid = self.open()
        state = controller.repository.load(sid).state
        state["stopped"] = True
        controller.repository.save(sid, state, controller.repository.load(sid).token)
        model, question, _ = an.validate(copy.deepcopy(MODEL_RAW))
        with self.assertRaises(CommandError) as caught:
            controller.commit_analysis(sid, model, question)
        self.assertEqual(410, caught.exception.status)


class BuilderStepsBack(unittest.TestCase):
    def test_once_the_analyst_is_active_the_builder_builds_and_does_not_ask(self):
        from tests.test_studio_claude_worker import BLANK, FakeClient, ROOT, cw, q
        draft = {"ops": [{"op": "set_label", "node_id": "hero-cta", "value": "Order", "new_node": BLANK}],
                 "confirm": "Renamed the button.", "questions": [q()], "batch_title": "",
                 "resolves": {"question_id": "", "option_id": "", "freeform_answer": ""}}
        state = {"artifact": ROOT, "questions": [], "transcript": [], "session_id": "s1", "turn_seq": 1}
        alone = cw.ClaudeWorker(client=FakeClient(draft)).on_turn(state, {"kind": "utterance", "text": "x"})
        together = cw.ClaudeWorker(client=FakeClient(draft)).on_turn(dict(state, analyst=True),
                                                                      {"kind": "utterance", "text": "x"})
        self.assertIn("question.asked", [e["type"] for e in alone["events"]])
        self.assertEqual(["artifact.patch", "confirm"], [e["type"] for e in together["events"]])


class Lane(unittest.TestCase):
    origin = {"Origin": "https://www.sfdc24.com"}

    def make(self, analyst=None, **overrides):
        self.store = MemoryStore()
        self.email_sender = EmailSender()
        self.analyst = analyst if analyst is not None else FakeAnalyst()
        self.now = [1000.0]
        return create_app(
            settings=settings(**overrides), store=self.store, worker=CountingWorker(),
            clock=lambda: self.now[0], id_factory=IDs(), voice_client=FakeVoiceClient(),
            email_sender=self.email_sender, talk_client=FakeTalk(), analyst=self.analyst,
        )

    def session(self, client):
        started = client.post("/v1/auth/start", headers=self.origin, json={
            "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
        code = self.email_sender.calls[-1][1]
        operator = client.post("/v1/auth/verify", headers=self.origin, json={
            "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
            "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
        created = client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + operator},
                              json={"creation_id": "an-1", "start": "blank"}).json()
        return created["session_id"], {**self.origin, "Authorization": "Bearer " + created["token"]}

    def spaced(self, client, url, headers, body):
        self.now[0] += 4.0
        return client.post(url, headers=headers, json=body)

    def test_health_says_the_analyst_is_there(self):
        with TestClient(self.make()) as client:
            self.assertTrue(client.get("/health").json()["features"]["analyst"])

    def test_an_analysis_is_returned_and_reaches_the_event_stream(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            out = client.post("/v1/session/%s/analyze" % sid, headers=headers,
                              json={"text": "An ordering app for my bakery", "turn": 3})
            stream = client.get("/v1/session/%s/events" % sid, headers=headers)
        self.assertEqual(200, out.status_code, out.text)
        body = out.json()
        self.assertEqual(3, body["turn"])
        self.assertEqual(["model.updated", "question.asked"], [e["type"] for e in body["events"]])
        self.assertIn("event: model.updated", stream.text)
        self.assertEqual("An ordering app for my bakery", self.analyst.calls[0]["text"])

    def test_bad_bodies_and_foreign_tokens_are_refused(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/analyze" % sid
            codes = [self.spaced(client, url, headers, body).status_code for body in (
                {}, {"text": ""}, {"text": "x" * 601}, {"text": "hi", "turn": -1}, {"text": "hi", "extra": 1})]
            no_token = client.post(url, headers=self.origin, json={"text": "hi"}).status_code
            wrong_origin = client.post(url, headers={**headers, "Origin": "https://evil.example"},
                                       json={"text": "hi"}).status_code
        self.assertEqual([400] * 5, codes)
        self.assertEqual(401, no_token)
        self.assertEqual(403, wrong_origin)
        self.assertEqual([], self.analyst.calls)

    def test_spacing_and_the_per_session_cap(self):
        with TestClient(self.make(analyze_cap=2)) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/analyze" % sid
            first = self.spaced(client, url, headers, {"text": "one"}).status_code
            too_soon = client.post(url, headers=headers, json={"text": "two"}).status_code
            second = self.spaced(client, url, headers, {"text": "two"}).status_code
            over = self.spaced(client, url, headers, {"text": "three"}).status_code
        self.assertEqual([200, 429, 200, 429], [first, too_soon, second, over])
        self.assertEqual(2, len(self.analyst.calls))

    def test_a_provider_failure_is_503_and_the_session_carries_on(self):
        with TestClient(self.make(analyst=FakeAnalyst(fail=True))) as client:
            sid, headers = self.session(client)
            failed = client.post("/v1/session/%s/analyze" % sid, headers=headers, json={"text": "x"})
            ok = self.spaced(client, "/v1/session/%s/commands" % sid, headers, {
                "command_id": "cmd-1", "session_id": sid, "type": "utterance", "expected_version": 1,
                "transcript": "build it", "item_id": "item-1"})
        self.assertEqual(503, failed.status_code)
        self.assertEqual(200, ok.status_code, ok.text)

    def test_a_stopped_session_is_410(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "cmd-stop", "session_id": sid, "type": "stop", "expected_version": 1})
            gone = self.spaced(client, "/v1/session/%s/analyze" % sid, headers, {"text": "x"})
        self.assertEqual(410, gone.status_code)
        self.assertEqual([], self.analyst.calls)


if __name__ == "__main__":
    unittest.main()

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

    def test_a_reconnect_after_the_window_rolls_still_gets_the_model(self):
        controller, _, sid = self.open()
        model, question, _ = an.validate(copy.deepcopy(MODEL_RAW))
        controller.commit_analysis(sid, model, question)
        last = controller.repository.load(sid).state["last_seq"]
        events, repaired, _ = controller.events_after(sid, last + 50)       # a cursor the server cannot replay
        self.assertTrue(repaired)
        self.assertEqual(["artifact.snapshot", "model.updated", "question.asked"], [e["type"] for e in events])
        self.assertEqual(model, events[1]["payload"]["model"])
        self.assertEqual([1, 2, 3], [e["seq"] for e in events])

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

    def test_a_homepage_session_starts_with_the_analyst_owning_the_questions(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
        state = next(v for v in self.store.data.values() if isinstance(v, dict) and v.get("session_id") == sid)
        self.assertTrue(state["analyst"])

    def test_the_analyst_flag_is_part_of_creation_and_a_replay_never_changes_it(self):
        # Cursor NO-GO on e22eb9e: a second write after admission could 500 and
        # could mark a replayed TEMPLATE session. Now it is set in the creating write.
        with TestClient(self.make()) as client:
            started = client.post("/v1/auth/start", headers=self.origin, json={
                "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
            code = self.email_sender.calls[-1][1]
            operator = client.post("/v1/auth/verify", headers=self.origin, json={
                "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
                "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
            auth = {**self.origin, "Authorization": "Bearer " + operator}
            first = client.post("/v1/session", headers=auth, json={"creation_id": "tpl-1", "start": "template"}).json()
            again = client.post("/v1/session", headers=auth, json={"creation_id": "tpl-1", "start": "blank"})
        self.assertEqual(first["session_id"], again.json()["session_id"])
        state = next(v for v in self.store.data.values()
                     if isinstance(v, dict) and v.get("session_id") == first["session_id"])
        self.assertNotIn("analyst", state)
        self.assertTrue(state["questions"])                 # still the template, questions and all

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


class AnalystDeadline(unittest.TestCase):
    def test_the_analyst_gives_up_inside_cloud_runs_60_seconds(self):
        # The owner's run (2026-09-26 01:04Z): /analyze died with a 504 at
        # exactly 60.0 s, which was both the client timeout and Cloud Run's.
        import sys
        seen = {}
        stub = SimpleNamespace(Anthropic=lambda **kw: seen.update(kw) or SimpleNamespace())
        real = sys.modules.get("anthropic")
        sys.modules["anthropic"] = stub
        try:
            an.Analyst()
        finally:
            if real is not None:
                sys.modules["anthropic"] = real
            else:
                sys.modules.pop("anthropic", None)
        self.assertEqual(0, seen.get("max_retries"))
        self.assertEqual(an.BUDGET_SECONDS, seen.get("timeout"))
        self.assertLessEqual(an.BUDGET_SECONDS, 35)


# --- Codex NO-GO on #272 at 8a9349e: analyze settles inside a total budget ---
import threading  # noqa: E402
import time  # noqa: E402

from tests.studio_slow_provider import SlowProvider, tool_message  # noqa: E402

RECORDED = tool_message("record_analysis", MODEL_RAW)
STATE = {"session_id": "s1", "artifact": {"id": "screen", "kind": "screen", "label": "x", "children": []}}


class AnalystThroughTheRealSdk(unittest.TestCase):
    def provider(self, *replies):
        prov = SlowProvider(*replies)
        self.addCleanup(prov.close)
        return prov

    def test_a_trickling_analysis_is_abandoned_at_the_budget_and_its_socket_shut(self):
        prov = self.provider(("slow", RECORDED, 0.01))
        analyst = an.Analyst(client=prov.client(), research=False, budget_seconds=0.6)
        started = time.monotonic()
        out = analyst.analyze(STATE, "An ordering app for my bakery", "The canvas is empty.")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNone(out["model"])
        self.assertEqual("transient", out["fault"])
        self.assertTrue(out["problems"][0].startswith(an.SLOW_PROBLEM), out["problems"])
        record = prov.wait_settled(0)
        self.assertIn("dropped", record)
        self.assertNotIn("complete", record)

    def test_permanent_and_transient_statuses(self):
        for code, kind, fault in ((400, "invalid_request_error", "permanent"), (401, "authentication_error", "permanent"),
                                  (403, "permission_error", "permanent"), (429, "rate_limit_error", "transient"),
                                  (500, "api_error", "transient")):
            with self.subTest(status=code):
                prov = self.provider(("status", code, kind))
                out = an.Analyst(client=prov.client(), research=False, budget_seconds=5).analyze(STATE, "x", "y")
                self.assertEqual(fault, out["fault"])
                self.assertIsNone(out["model"])
                self.assertEqual(1, len(prov.served), "no retry")

    def test_an_answer_in_time_is_an_analysis(self):
        prov = self.provider(("fast", RECORDED))
        out = an.Analyst(client=prov.client(), research=False, budget_seconds=5).analyze(STATE, "x", "y")
        self.assertEqual(["account", "order"], [o["id"] for o in out["model"]["objects"]])
        self.assertNotIn("fault", out)


class _HangingAnalyst(FakeAnalyst):
    def __init__(self):
        super().__init__()
        self.release, self.finished = threading.Event(), threading.Event()

    def analyze(self, state, text, canvas):
        if not self.calls:
            self.calls.append({"text": text})
            self.release.wait(10)
            out = super().analyze(state, text, canvas)
            self.finished.set()
            return out
        return super().analyze(state, text, canvas)


class AnalyzeSettlesInsideItsBudget(Lane):
    def model_of(self, sid):
        state = next(v for v in self.store.data.values() if isinstance(v, dict) and v.get("session_id") == sid)
        return state.get("model")

    def test_a_trickling_provider_is_a_503_in_time_and_the_next_analysis_lands(self):
        prov = SlowProvider(("slow", RECORDED, 0.01), ("fast", RECORDED))
        self.addCleanup(prov.close)
        analyst = an.Analyst(client=prov.client(), research=False, budget_seconds=0.6)
        with TestClient(self.make(analyst=analyst)) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/analyze" % sid
            started = time.monotonic()
            first = client.post(url, headers=headers, json={"text": "An ordering app for my bakery"})
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertIsNone(self.model_of(sid))
            second = self.spaced(client, url, headers, {"text": "An ordering app for my bakery"})
        self.assertEqual(503, first.status_code)
        self.assertTrue(first.json()["detail"].startswith(an.SLOW_PROBLEM), first.text)
        self.assertNotIn("not available", first.json()["detail"], "a slow turn does not switch the analyst off")
        self.assertEqual(200, second.status_code, second.text)
        self.assertIsNotNone(self.model_of(sid))

    def test_a_permanent_fault_says_not_available(self):
        prov = SlowProvider(("status", 401, "authentication_error"))
        self.addCleanup(prov.close)
        analyst = an.Analyst(client=prov.client(), research=False, budget_seconds=5)
        with TestClient(self.make(analyst=analyst)) as client:
            sid, headers = self.session(client)
            out = client.post("/v1/session/%s/analyze" % sid, headers=headers, json={"text": "x"})
        self.assertEqual(503, out.status_code)
        self.assertIn("not available", out.json()["detail"])
        self.assertEqual(1, len(prov.served))

    def test_a_hanging_analyst_is_abandoned_by_the_route_and_its_late_model_never_lands(self):
        analyst = _HangingAnalyst()
        with TestClient(self.make(analyst=analyst, analyze_deadline_seconds=0.4)) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/analyze" % sid
            started = time.monotonic()
            first = client.post(url, headers=headers, json={"text": "x"})
            self.assertLess(time.monotonic() - started, 1.5)
            analyst.release.set()
            self.assertTrue(analyst.finished.wait(5))
            time.sleep(0.2)
            self.assertIsNone(self.model_of(sid), "the late model is never committed")
            second = self.spaced(client, url, headers, {"text": "x"})
        self.assertEqual(503, first.status_code)
        self.assertEqual("the analyst could not finish in time", first.json()["detail"])
        self.assertEqual(200, second.status_code, second.text)

    def test_the_route_deadline_stays_under_cloud_run(self):
        for bad in (0, 60, 75.0, "40"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                settings(analyze_deadline_seconds=bad).validate()
        settings(analyze_deadline_seconds=40.0).validate()


class AnalystAborts(unittest.TestCase):
    def test_the_analyst_passes_its_abort_as_the_cancel(self):
        aborted = []
        real = an.bounded.abortable

        def spying(client):
            call = real(client)
            inner = call.abort
            call.abort = lambda: (aborted.append(1), inner())
            return call
        prov = SlowProvider(("slow", RECORDED, 0.01))
        self.addCleanup(prov.close)
        an.bounded.abortable = spying
        try:
            out = an.Analyst(client=prov.client(), research=False, budget_seconds=0.4).analyze(STATE, "x", "y")
        finally:
            an.bounded.abortable = real
        self.assertEqual([1], aborted)
        self.assertEqual("transient", out["fault"])


if __name__ == "__main__":
    unittest.main()

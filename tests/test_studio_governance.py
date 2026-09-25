"""The use policy on sfdc24.com: a gate in front of every lane, and the policy in every lane's prompt.

What is under test is the controller's behaviour around a moderation verdict,
never the moderation model: a fake client answers "flagged" for any text that
contains FLAG and "clean" otherwise, so each test says exactly which line is
bad. Nothing here touches a network.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, FakeTalk, FakeVoiceClient, IDs, MemoryStore, settings,
)
from tests.test_studio_analyst import FakeAnalyst, FakeAnthropic  # noqa: E402
from app import governance  # noqa: E402
from app.main import create_app  # noqa: E402
from workers import analyst as an  # noqa: E402
from workers import talk as tk  # noqa: E402
from workers.policy import USE_POLICY  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BAD = "FLAG this is a line the moderation model flags"
BAD2 = "FLAG a second, different flagged line"
BAD3 = "FLAG a third flagged line"
KEY = "sk-moderation-test"


class ModerationResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeModeration:
    """Stands in for the controller's httpx client on the moderation URL."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.mode == "raise":
            raise RuntimeError("network down")
        if self.mode == "slow":
            await asyncio.sleep(1.0)
        if self.mode == "http500":
            return ModerationResponse(500, {})
        if self.mode == "malformed":
            return ModerationResponse(200, {"results": "not a list"})
        results = []
        for text in kwargs["json"]["input"]:
            flagged = "FLAG" in text
            results.append({"flagged": flagged, "categories": {"harassment": flagged, "violence": False}})
        return ModerationResponse(200, {"id": "modr-test", "model": governance.MODERATION_MODEL,
                                        "results": results})

    def inputs(self):
        return [call[1]["json"]["input"] for call in self.calls]


class GateTests(unittest.TestCase):
    origin = {"Origin": "https://www.sfdc24.com"}

    def make(self, mode="ok", **overrides):
        values = dict(moderation_enabled=True, openai_api_key=KEY)
        values.update(overrides)
        self.store = MemoryStore()
        self.email_sender = EmailSender()
        self.talk = FakeTalk()
        self.worker = CountingWorker()
        self.analyst = FakeAnalyst()
        self.moderation = FakeModeration(mode)
        self.voice = FakeVoiceClient()
        self.now = [1000.0]
        self.app = create_app(
            settings=settings(**values), store=self.store, worker=self.worker,
            clock=lambda: self.now[0], id_factory=IDs(), voice_client=self.voice,
            email_sender=self.email_sender, talk_client=self.talk, analyst=self.analyst,
            moderation_client=self.moderation,
        )
        return self.app

    def session(self, client):
        started = client.post("/v1/auth/start", headers=self.origin, json={
            "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
        code = self.email_sender.calls[-1][1]
        operator = client.post("/v1/auth/verify", headers=self.origin, json={
            "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
            "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
        created = client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + operator},
                              json={"creation_id": "gov-1", "start": "blank"}).json()
        return created["session_id"], {**self.origin, "Authorization": "Bearer " + created["token"]}

    def say(self, client, sid, headers, text, history=None):
        self.now[0] += 4.0
        body = {"text": text, "agent": "claude", "turn": 1}
        if history is not None:
            body["history"] = history
        return client.post("/v1/session/%s/talk" % sid, headers=headers, json=body)

    def build(self, client, sid, headers, text, n=1, version=1):
        self.now[0] += 4.0
        return client.post("/v1/session/%s/commands" % sid, headers=headers, json={
            "command_id": "cmd-%d" % n, "session_id": sid, "type": "utterance",
            "expected_version": version, "transcript": text, "item_id": "item-%d" % n})

    def analyze(self, client, sid, headers, text):
        self.now[0] += 4.0
        return client.post("/v1/session/%s/analyze" % sid, headers=headers, json={"text": text, "turn": 0})

    def state(self, sid):
        return self.app.state.controller.repository.load(sid).state

    def count(self, sid):
        return self.app.state.policy_book.count(sid)

    # -- a clean line ------------------------------------------------------
    def test_a_clean_line_passes_untouched(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            response = self.say(client, sid, headers, "Make a logo for my cafe")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"reply": "Building that now.", "speaker": "claude", "turn": 1}, response.json())
        self.assertEqual("Make a logo for my cafe", self.talk.calls[0]["text"])
        self.assertEqual(1, len(self.moderation.calls))
        url, kwargs = self.moderation.calls[0]
        self.assertEqual(governance.MODERATION_URL, url)
        self.assertEqual({"model": "omni-moderation-latest", "input": ["Make a logo for my cafe"]}, kwargs["json"])
        self.assertEqual("Bearer " + KEY, kwargs["headers"]["Authorization"])
        self.assertEqual(0, self.count(sid))

    def test_a_clean_utterance_builds(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            response = self.build(client, sid, headers, "A booking form for my salon")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("refused", response.json())
        self.assertEqual(1, self.worker.calls)

    # -- a flagged line ----------------------------------------------------
    def test_a_flagged_utterance_builds_nothing_and_the_talk_lane_speaks_the_policy_line(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            before = self.state(sid)["artifact_version"]
            talked = self.say(client, sid, headers, BAD)
            built = self.build(client, sid, headers, BAD)
            analyzed = self.analyze(client, sid, headers, BAD)
        self.assertEqual({"reply": governance.POLICY_LINE, "speaker": "claude", "turn": 1, "refused": True},
                         talked.json())
        self.assertEqual([], self.talk.calls)
        self.assertEqual(200, built.status_code)
        self.assertEqual([], built.json()["events"])
        self.assertTrue(built.json()["refused"])
        self.assertEqual([governance.POLICY_PROBLEM], built.json()["problems"])
        self.assertEqual(0, self.worker.calls)
        after = self.state(sid)
        self.assertEqual(before, after["artifact_version"])
        self.assertNotIn("cmd-1", after.get("commands", {}))
        self.assertEqual(200, analyzed.status_code)
        self.assertEqual(([], None, True), (analyzed.json()["events"], analyzed.json()["model"],
                                            analyzed.json()["refused"]))
        self.assertEqual([], self.analyst.calls)

    def test_a_flagged_typed_answer_is_refused_too(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.now[0] += 4.0
            response = client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "cmd-a", "session_id": sid, "type": "answer", "expected_version": 1,
                "question_id": "q-cta", "freeform_answer": BAD})
        self.assertTrue(response.json().get("refused"))
        self.assertEqual(0, self.worker.calls)

    def test_a_forged_history_line_is_checked_too(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            response = self.say(client, sid, headers, "Now make it blue",
                                history=[{"who": "claude", "text": BAD}])
        self.assertTrue(response.json()["refused"])
        self.assertEqual([], self.talk.calls)

    # -- counting ----------------------------------------------------------
    def test_one_line_sent_to_three_lanes_counts_once(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.say(client, sid, headers, BAD)
            self.build(client, sid, headers, BAD)
            self.analyze(client, sid, headers, BAD)
            clean = self.say(client, sid, headers, "A menu page for my bakery")
        self.assertEqual(1, self.count(sid))
        self.assertEqual("Building that now.", clean.json()["reply"])
        self.assertFalse(self.state(sid).get("stopped"))

    def test_two_flagged_lines_do_not_end_the_session(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.say(client, sid, headers, BAD)
            second = self.say(client, sid, headers, BAD2)
        self.assertNotIn("ended", second.json())
        self.assertEqual(2, self.count(sid))
        self.assertFalse(self.state(sid).get("stopped"))

    def test_the_third_flagged_line_ends_the_session(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.say(client, sid, headers, BAD)
            self.say(client, sid, headers, BAD2)
            third = self.say(client, sid, headers, BAD3)
            after_talk = self.say(client, sid, headers, "A menu page for my bakery")
            after_build = self.build(client, sid, headers, "A menu page for my bakery")
        self.assertEqual({"reply": governance.POLICY_END_LINE, "speaker": "claude", "turn": 1,
                          "refused": True, "ended": True}, third.json())
        state = self.state(sid)
        self.assertTrue(state["stopped"])
        self.assertEqual(("session.ended", governance.POLICY_END_REASON),
                         (state["events"][-1]["type"], state["events"][-1]["payload"]["reason"]))
        self.assertEqual(410, after_talk.status_code)
        self.assertEqual(410, after_build.status_code)
        self.assertEqual([], self.talk.calls)

    def test_the_visitors_stop_is_never_gated(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.now[0] += 4.0
            stopped = client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "cmd-stop", "session_id": sid, "type": "stop", "expected_version": 1,
                "transcript": BAD})
        self.assertEqual(200, stopped.status_code)
        self.assertEqual("You ended this session.", stopped.json()["events"][0]["payload"]["reason"])
        self.assertEqual([], self.moderation.calls)

    # -- what is kept ------------------------------------------------------
    def test_nothing_the_visitor_said_is_stored_or_logged(self):
        out = io.StringIO()
        with TestClient(self.make()) as client, contextlib.redirect_stdout(out):
            sid, headers = self.session(client)
            self.say(client, sid, headers, BAD)
        logged = out.getvalue()
        self.assertIn('"event": "studio.policy_flag"', logged)
        self.assertIn('"categories": ["harassment"]', logged)
        self.assertNotIn("moderation model flags", logged)
        record = self.store.data["studio_policy_" + sid]
        self.assertEqual({"talk": 1}, record["lanes"])
        self.assertEqual({"at", "lane", "categories"}, set(record["records"][0]))
        self.assertNotIn("moderation model flags", json.dumps(record))

    # -- outage ------------------------------------------------------------
    def test_a_moderation_outage_fails_open_and_is_counted(self):
        for mode in ("raise", "http500", "malformed", "slow"):
            with self.subTest(mode=mode), mock.patch.object(governance, "MODERATION_TIMEOUT", 0.05):
                out = io.StringIO()
                with TestClient(self.make(mode)) as client, contextlib.redirect_stdout(out):
                    sid, headers = self.session(client)
                    response = self.say(client, sid, headers, BAD)
                self.assertEqual("Building that now.", response.json()["reply"])
                self.assertEqual(BAD, self.talk.calls[0]["text"])
                self.assertEqual(1, self.app.state.moderator.stats["unavailable"])
                self.assertIn('"event": "studio.moderation_unavailable"', out.getvalue())
                self.assertNotIn("moderation model flags", out.getvalue())
                self.assertEqual(0, self.count(sid))

    def test_no_key_fails_open_and_is_counted(self):
        with TestClient(self.make(openai_api_key="")) as client:
            sid, headers = self.session(client)
            response = self.say(client, sid, headers, BAD)
        self.assertEqual("Building that now.", response.json()["reply"])
        self.assertEqual([], self.moderation.calls)
        self.assertEqual(1, self.app.state.moderator.stats["unavailable"])

    def test_switched_off_the_gate_calls_nothing(self):
        with TestClient(self.make(moderation_enabled=False)) as client:
            sid, headers = self.session(client)
            response = self.say(client, sid, headers, BAD)
            self.build(client, sid, headers, BAD)
        self.assertEqual("Building that now.", response.json()["reply"])
        self.assertEqual([], self.moderation.calls)
        self.assertEqual(1, self.worker.calls)

    def test_the_same_line_is_checked_once(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.say(client, sid, headers, "A booking form for my salon")
            self.build(client, sid, headers, "A booking form for my salon")
        self.assertEqual(1, len(self.moderation.calls))

    def test_a_flagged_line_for_the_architect_voice_is_refused_and_not_counted(self):
        with TestClient(self.make(voice_enabled=True)) as client:
            sid, headers = self.session(client)
            response = client.post("/v1/session/%s/speak" % sid, headers=headers,
                                   json={"text": BAD, "voice": "architect"})
        self.assertEqual(422, response.status_code)
        self.assertEqual([], [c for c in self.voice.calls if "audio/speech" in c[0]])
        self.assertEqual(0, self.count(sid))

    def test_health_says_governance_is_on(self):
        with TestClient(self.make()) as client:
            self.assertTrue(client.get("/health").json()["features"]["governance"])


class PolicyInEveryLane(unittest.TestCase):
    def test_every_lane_prompt_ends_with_the_one_use_policy(self):
        spec = importlib.util.spec_from_file_location(
            "claude_worker_by_path", REPO / "cloud" / "studio-controller" / "workers" / "claude_worker.py")
        cw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cw)
        self.assertIs(governance.USE_POLICY, USE_POLICY)
        for name, system in (("talk", tk.SYSTEM.format(name="Claude")), ("recap", tk.RECAP_SYSTEM),
                             ("analyst", an.SYSTEM), ("builder", cw.SYSTEM)):
            with self.subTest(lane=name):
                self.assertTrue(system.endswith(USE_POLICY), name)

    def test_the_prompts_actually_sent_carry_it(self):
        sent = []
        client = tk.TalkClient(anthropic_ready=True, openai_key="sk-test")
        client._claude = lambda system, messages, *a: sent.append(system) or "ok"
        client._openai = lambda system, messages, *a: sent.append(system) or "ok"
        client.reply("claude", "hello", [], "empty")
        client.reply("openai", "hello", [], "empty")
        client.recap("claude", "brief")
        fake = FakeAnthropic()
        an.Analyst(client=fake, research=False).analyze({"transcript": []}, "hello", "empty")
        sent.append(fake.calls[0]["system"])
        spec = importlib.util.spec_from_file_location(
            "claude_worker_sent", REPO / "cloud" / "studio-controller" / "workers" / "claude_worker.py")
        cw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cw)
        calls = []
        refusing = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kw: calls.append(kw) or SimpleNamespace(stop_reason="refusal", content=[]))))
        cw.ClaudeWorker(client=refusing).on_turn(
            {"artifact": {"id": "screen", "kind": "screen", "label": "Blank", "children": []},
             "questions": [], "transcript": []}, {"kind": "utterance", "text": "hello"})
        sent.append(calls[0]["system"])
        self.assertEqual(5, len(sent))
        for system in sent:
            self.assertIn(USE_POLICY, system)

    def test_the_policy_names_what_the_owner_asked_for(self):
        for phrase in ("impersonate a real brand", "phishing", "credentials or payment details",
                       "sexual, hateful, violent or extremist", "illegal activity",
                       "could not put its name to", "Anthropic's and OpenAI's"):
            self.assertIn(phrase, USE_POLICY)
        self.assertNotIn("{", USE_POLICY)
        self.assertNotIn("}", USE_POLICY)


if __name__ == "__main__":
    unittest.main()

"""Two voices: the host (the realtime call, which also recaps) and the architect.

The architect's lines are what the builder and analyst already said; /speak
only turns them into audio in its own OpenAI voice. /recap is the host closing
the meeting from the session record. Under test: guards, caps, the exact TTS
request, and that nothing reaches a provider without a live session.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, FakeTalk, IDs, MemoryStore, settings,
)
from app.main import create_app  # noqa: E402
from workers import talk as talk_mod  # noqa: E402

MP3 = b"ID3\x04\x00fake-mp3-bytes"


class TtsResponse:
    def __init__(self, status_code=200, content=MP3):
        self.status_code = status_code
        self.content = content
        self.text = ""
        self.headers = {}


class FakeOpenAI:
    """Stands in for the controller's httpx client: records every call."""

    def __init__(self, status=200, content=MP3):
        self.calls = []
        self.status, self.content = status, content

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return TtsResponse(self.status, self.content)


class RecapTalk(FakeTalk):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.recaps = []

    def recap(self, agent, brief):
        self.recaps.append((agent, brief))
        if self.fail:
            raise RuntimeError("provider down")
        return "You want a bakery logo; it is on the canvas now. Keep shaping it here, or talk to us."


class Lanes(unittest.TestCase):
    origin = {"Origin": "https://www.sfdc24.com"}

    def make(self, openai=None, talk=None, **overrides):
        values = dict(voice_enabled=True, openai_api_key="sk-test", maintenance_secret="m" * 40)
        values.update(overrides)
        self.openai = openai if openai is not None else FakeOpenAI()
        self.talk = talk if talk is not None else RecapTalk()
        self.email_sender = EmailSender()
        return create_app(settings=settings(**values), store=MemoryStore(), worker=CountingWorker(),
                          clock=lambda: 1000, id_factory=IDs(), voice_client=self.openai,
                          email_sender=self.email_sender, talk_client=self.talk)

    def session(self, client):
        started = client.post("/v1/auth/start", headers=self.origin, json={
            "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
        code = self.email_sender.calls[-1][1]
        operator = client.post("/v1/auth/verify", headers=self.origin, json={
            "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
            "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
        created = client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + operator},
                              json={"creation_id": "voices-1", "start": "blank"}).json()
        return created["session_id"], {**self.origin, "Authorization": "Bearer " + created["token"]}

    def test_health_names_the_two_voices_only_when_voice_is_on(self):
        with TestClient(self.make()) as client:
            self.assertEqual(["host", "architect"], client.get("/health").json()["features"]["voices"])
        with TestClient(self.make(voice_enabled=False)) as client:
            self.assertEqual([], client.get("/health").json()["features"]["voices"])

    def test_the_architect_line_comes_back_as_audio_in_its_own_voice(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            out = client.post("/v1/session/%s/speak" % sid, headers=headers,
                              json={"text": "Built the  landing page.", "voice": "architect"})
        self.assertEqual(200, out.status_code, out.text)
        self.assertEqual("audio/mpeg", out.headers["content-type"])
        self.assertEqual(MP3, out.content)
        url, kwargs = self.openai.calls[-1]
        self.assertEqual("https://api.openai.com/v1/audio/speech", url)
        self.assertEqual({"model", "voice", "input", "instructions", "response_format"}, set(kwargs["json"]))
        self.assertEqual(("gpt-4o-mini-tts", "cedar", "Built the landing page.", "mp3"),
                         (kwargs["json"]["model"], kwargs["json"]["voice"], kwargs["json"]["input"],
                          kwargs["json"]["response_format"]))

    def test_speak_refuses_bad_bodies_other_voices_and_strangers(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/speak" % sid
            codes = [client.post(url, headers=headers, json=body).status_code for body in (
                {}, {"text": ""}, {"text": "x" * 401}, {"text": "hi", "voice": "host"}, {"text": "hi", "extra": 1})]
            stranger = client.post(url, headers=self.origin, json={"text": "hi"}).status_code
            elsewhere = client.post(url, headers={**headers, "Origin": "https://evil.example"},
                                    json={"text": "hi"}).status_code
        self.assertEqual([400] * 5, codes)
        self.assertEqual((401, 403), (stranger, elsewhere))
        self.assertEqual([], self.openai.calls)

    def test_speak_is_capped_per_session_and_off_when_voice_is_off(self):
        with TestClient(self.make(speak_cap=2)) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/speak" % sid
            codes = [client.post(url, headers=headers, json={"text": "line"}).status_code for _ in range(3)]
        self.assertEqual([200, 200, 429], codes)
        with TestClient(self.make(voice_enabled=False)) as client:
            sid, headers = self.session(client)
            self.assertEqual(503, client.post("/v1/session/%s/speak" % sid, headers=headers,
                                              json={"text": "line"}).status_code)

    def test_a_tts_failure_is_503_and_a_stopped_session_is_410(self):
        with TestClient(self.make(openai=FakeOpenAI(status=500, content=b""))) as client:
            sid, headers = self.session(client)
            failed = client.post("/v1/session/%s/speak" % sid, headers=headers, json={"text": "line"})
            client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "cmd-stop", "session_id": sid, "type": "stop", "expected_version": 1})
            gone = client.post("/v1/session/%s/speak" % sid, headers=headers, json={"text": "line"})
        self.assertEqual((503, 410), (failed.status_code, gone.status_code))

    def test_the_host_recaps_from_the_session_record(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "cmd-1", "session_id": sid, "type": "utterance", "expected_version": 1,
                "transcript": "A logo for my bakery", "item_id": "item-1"})
            out = client.post("/v1/session/%s/recap" % sid, headers=headers, json={})
        self.assertEqual(200, out.status_code, out.text)
        self.assertEqual("claude", out.json()["speaker"])
        self.assertIn("bakery logo", out.json()["recap"])
        agent, brief = self.talk.recaps[-1]
        self.assertIn("- A logo for my bakery", brief)
        self.assertIn("ON THE CANVAS:", brief)

    def test_recap_guards(self):
        with TestClient(self.make(recap_cap=1)) as client:
            sid, headers = self.session(client)
            url = "/v1/session/%s/recap" % sid
            bad = client.post(url, headers=headers, json={"agent": "gemini"}).status_code
            extra = client.post(url, headers=headers, json={"x": 1}).status_code
            first = client.post(url, headers=headers, json={}).status_code
            second = client.post(url, headers=headers, json={}).status_code
        self.assertEqual((400, 400, 200, 429), (bad, extra, first, second))
        with TestClient(self.make(talk=RecapTalk(fail=True))) as client:
            sid, headers = self.session(client)
            self.assertEqual(503, client.post("/v1/session/%s/recap" % sid, headers=headers, json={}).status_code)


class RecapBrief(unittest.TestCase):
    def test_the_brief_holds_what_was_said_decided_and_modelled_and_nothing_else(self):
        state = {
            "transcript": [{"role": "visitor", "text": "A booking page"}, {"role": "system", "text": "hidden"}],
            "questions": [
                {"status": "answered", "prompt": "Pay when?", "selected_option": "now",
                 "options": [{"option_id": "now", "label": "When booking"}]},
                {"status": "open", "prompt": "Colours?", "options": []},
            ],
            "model": {"domain": "Bookings", "objects": [{"name": "Contact"}, {"name": "Booking"}]},
        }
        brief = talk_mod.recap_brief(state, "The canvas has 3 parts.")
        self.assertIn("- A booking page", brief)
        self.assertNotIn("hidden", brief)
        self.assertIn("Pay when? -> When booking", brief)
        self.assertNotIn("Colours?", brief)
        self.assertIn("DATA MODEL (Bookings): Contact, Booking", brief)

    def test_the_recap_prompt_promises_nothing(self):
        self.assertIn("never name or promise any person, price or date", talk_mod.RECAP_SYSTEM)
        self.assertIn("at most 70 words", talk_mod.RECAP_SYSTEM)


if __name__ == "__main__":
    unittest.main()

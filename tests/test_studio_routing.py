"""The model that talks is chosen by what the visitor wants to do (owner,
2026-09-25): "I dont want users to select between claude and openai; it should
be selected based on what they want to do ... also get Gemini and Meta to
participate ... Models should add value; perspective; color and speed".

Under test: the topic-to-agent map and its fallback, an explicit agent still
honoured, the recap following the same map, the Gemini and Meta request
shapes (keys only in headers, never in a URL or an error), agents offered only
with a credential, and each agent's angle in its prompt.
"""
from __future__ import annotations

import io
import json
import logging
import unittest
import urllib.error

from fastapi.testclient import TestClient

import tests.test_studio_controller as base  # noqa: E402  (sets sys.path; its cases are not re-run here)
from workers import talk as tk  # noqa: E402
from workers.topics import FALLBACK, TOPIC_AGENT, TOPICS, route_agent  # noqa: E402

ALL = ("claude", "openai", "gemini", "meta")


class Map(unittest.TestCase):
    def test_every_topic_has_an_agent_and_the_owner_examples_hold(self):
        self.assertEqual(set(TOPICS), set(TOPIC_AGENT))
        self.assertEqual("openai", TOPIC_AGENT["logo"])
        self.assertEqual(("claude", "claude"), (TOPIC_AGENT["salesforce_admin"], TOPIC_AGENT["salesforce_data"]))
        self.assertEqual({"claude", "openai", "gemini", "meta"}, set(TOPIC_AGENT.values()))

    def test_each_topic_reaches_its_agent_when_everyone_is_available(self):
        for topic, agent in TOPIC_AGENT.items():
            self.assertEqual(agent, route_agent(topic, ALL), topic)
        for none in ("", None, "hacking", 5):
            self.assertEqual("claude", route_agent(none, ALL))

    def test_a_missing_agent_falls_back_in_order(self):
        self.assertEqual("claude", route_agent("app", ("claude", "openai", "gemini")))
        self.assertEqual("openai", route_agent("website", ("openai", "meta")))
        self.assertEqual("gemini", route_agent("logo", ("meta", "gemini")))
        self.assertEqual("", route_agent("logo", ()))
        self.assertEqual(("claude", "openai", "gemini", "meta"), FALLBACK)


class RecordingTalk(base.FakeTalk):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.recaps = []

    def recap(self, agent, brief):
        self.recaps.append(agent)
        return "Thanks for building with us today."


class Api(base.TalkLaneTests):
    """Talk and recap through the app, with a session opened on a topic."""
    __test__ = False

    def topic_session(self, client, topic):
        started = client.post("/v1/auth/start", headers=self.origin, json={
            "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
        code = self.email_sender.calls[-1][1]
        operator = client.post("/v1/auth/verify", headers=self.origin, json={
            "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
            "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
        body = {"creation_id": "route-" + (topic or "none"), "start": "blank"}
        if topic:
            body["topic"] = topic
        created = client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + operator},
                              json=body).json()
        return created["session_id"], {**self.origin, "Authorization": "Bearer " + created["token"]}


class Routing(unittest.TestCase):
    origin = base.TalkLaneTests.origin
    make = base.TalkLaneTests.make
    topic_session = Api.topic_session

    def say(self, client, sid, headers, body):
        self.now[0] += 2.0
        return client.post("/v1/session/%s/talk" % sid, headers=headers, json=body)

    def test_without_an_agent_the_topic_picks_it_and_the_speaker_says_which(self):
        for topic, agent in list(TOPIC_AGENT.items()) + [("", "claude")]:
            with self.subTest(topic=topic):
                with TestClient(self.make_with(RecordingTalk(agents=ALL))) as client:
                    sid, headers = self.topic_session(client, topic)
                    out = self.say(client, sid, headers, {"text": "hello", "turn": 1})
                self.assertEqual(200, out.status_code, out.text)
                self.assertEqual(agent, out.json()["speaker"])
                self.assertEqual(agent, self.talk_calls_agent())

    def talk_calls_agent(self):
        return self.talk_client.calls[-1]["agent"]

    def make_with(self, talk):
        self.talk_client = talk
        return self.make(talk)

    def test_a_missing_agent_falls_back(self):
        talk = RecordingTalk(agents=("claude", "openai"))
        with TestClient(self.make_with(talk)) as client:
            sid, headers = self.topic_session(client, "website")      # gemini is not configured
            out = self.say(client, sid, headers, {"text": "hello"})
        self.assertEqual(("claude", "claude"), (out.json()["speaker"], talk.calls[-1]["agent"]))

    def test_an_explicit_agent_is_still_honoured_and_checked(self):
        talk = RecordingTalk(agents=ALL)
        with TestClient(self.make_with(talk)) as client:
            sid, headers = self.topic_session(client, "logo")
            chosen = self.say(client, sid, headers, {"text": "hello", "agent": "gemini"})
            unknown = self.say(client, sid, headers, {"text": "hello", "agent": "grok"})
        self.assertEqual("gemini", chosen.json()["speaker"])
        self.assertEqual(400, unknown.status_code)
        self.assertEqual(["gemini"], [c["agent"] for c in talk.calls])

    def test_an_unconfigured_agent_is_refused_not_simulated(self):
        with TestClient(self.make_with(RecordingTalk(agents=("claude",)))) as client:
            sid, headers = self.topic_session(client, "logo")
            out = self.say(client, sid, headers, {"text": "hello", "agent": "meta"})
        self.assertEqual(400, out.status_code)

    def test_the_recap_follows_the_same_map(self):
        talk = RecordingTalk(agents=ALL)
        with TestClient(self.make_with(talk)) as client:
            sid, headers = self.topic_session(client, "logo")
            out = client.post("/v1/session/%s/recap" % sid, headers=headers, json={})
        self.assertEqual(200, out.status_code, out.text)
        self.assertEqual(("openai", ["openai"]), (out.json()["speaker"], talk.recaps))

    def test_history_may_name_the_new_agents(self):
        talk = RecordingTalk(agents=ALL)
        with TestClient(self.make_with(talk)) as client:
            sid, headers = self.topic_session(client, "website")
            out = self.say(client, sid, headers, {"text": "and then", "history": [
                {"who": "you", "text": "hi"}, {"who": "gemini", "text": "Hello."}, {"who": "meta", "text": "Hey."}]})
        self.assertEqual(200, out.status_code, out.text)

    def test_health_says_routing(self):
        with TestClient(self.make_with(RecordingTalk(agents=ALL))) as client:
            features = client.get("/health").json()["features"]
        self.assertTrue(features["routing"])
        self.assertEqual(list(ALL), features["agents"])


# -- the two new agents, request by request --------------------------------------
class Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    def __init__(self, payload=None, error=None):
        self.requests, self.payload, self.error = [], payload, error

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return Response(self.payload)


GEMINI_KEY = "gk-test-marker-9f3c"
META_TOKEN = "ya29.test-marker-77aa"


class Gemini(unittest.TestCase):
    def client(self, opener):
        return tk.TalkClient(anthropic_ready=False, openai_key="", gemini_key=GEMINI_KEY, meta_project="",
                             opener=opener)

    def test_the_request_shape_and_the_key_only_in_a_header(self):
        opener = Opener({"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True}, {"text": "A warm, friendly bakery page, coming up."}]}}]})
        reply = self.client(opener).reply("gemini", "a bakery website", [
            {"who": "you", "text": "hi"}, {"who": "gemini", "text": "Hello."}], "The canvas is empty.")
        self.assertEqual("A warm, friendly bakery page, coming up.", reply)   # thoughts never spoken
        request, timeout = opener.requests[0]
        self.assertEqual("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % tk.GEMINI_MODEL,
                         request.full_url)
        self.assertNotIn(GEMINI_KEY, request.full_url)
        self.assertEqual(GEMINI_KEY, request.get_header("X-goog-api-key"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(tk.TIMEOUT_SECONDS, timeout)
        body = json.loads(request.data)
        self.assertTrue(body["systemInstruction"]["parts"][0]["text"].endswith(tk.USE_POLICY))
        self.assertIn(tk.PERSPECTIVES["gemini"], body["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(["user", "model", "user"], [c["role"] for c in body["contents"]])
        self.assertEqual({"maxOutputTokens": tk.MAX_TOKENS, "thinkingConfig": {"thinkingLevel": "minimal"}},
                         body["generationConfig"])

    def test_a_provider_error_never_carries_the_key(self):
        error = urllib.error.HTTPError(tk.GEMINI_URL % tk.GEMINI_MODEL, 400, "Bad Request", {},
                                       io.BytesIO(b'{"error": "bad"}'))
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("routing-test").debug("start")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.client(Opener(error=error)).reply("gemini", "hi", [], "The canvas is empty.")
        self.assertNotIn(GEMINI_KEY, str(caught.exception))
        self.assertNotIn(GEMINI_KEY, caught.exception.geturl())
        self.assertNotIn(GEMINI_KEY, "\n".join(logs.output))


class Meta(unittest.TestCase):
    def client(self, opener, tokens):
        return tk.TalkClient(anthropic_ready=False, openai_key="", gemini_key="", meta_project="sfdc24",
                             meta_token=lambda: (tokens.append(1) or META_TOKEN, 3600), opener=opener)

    def test_the_request_shape_on_vertex_with_the_service_account_token(self):
        tokens = []
        opener = Opener({"choices": [{"message": {"content": "Let us sketch the app screens now."}}]})
        client = self.client(opener, tokens)
        reply = client.reply("meta", "an app for my crew", [], "The canvas is empty.")
        client.reply("meta", "and a login", [], "The canvas is empty.")
        self.assertEqual("Let us sketch the app screens now.", reply)
        request, _ = opener.requests[0]
        self.assertEqual("https://%s-aiplatform.googleapis.com/v1/projects/sfdc24/locations/%s/endpoints/openapi/"
                         "chat/completions" % (tk.META_REGION, tk.META_REGION), request.full_url)
        self.assertEqual("Bearer " + META_TOKEN, request.get_header("Authorization"))
        self.assertNotIn(META_TOKEN, request.full_url)
        body = json.loads(request.data)
        self.assertEqual(tk.META_MODEL, body["model"])
        self.assertEqual("system", body["messages"][0]["role"])
        self.assertTrue(body["messages"][0]["content"].endswith(tk.USE_POLICY))
        self.assertIn(tk.PERSPECTIVES["meta"], body["messages"][0]["content"])
        self.assertEqual(1, len(tokens))                              # the token is reused until it nears expiry

    def test_the_recap_works_on_every_agent(self):
        opener = Opener({"choices": [{"message": {"content": "You wanted an app. It is on the canvas."}}]})
        self.assertEqual("You wanted an app. It is on the canvas.",
                         self.client(opener, []).recap("meta", "THE VISITOR SAID: an app"))


class Offered(unittest.TestCase):
    def test_only_agents_with_a_credential_are_offered(self):
        none = tk.TalkClient(anthropic_ready=False, openai_key="", gemini_key="", meta_project="")
        self.assertEqual([], none.agents())
        self.assertEqual(["gemini"], tk.TalkClient(anthropic_ready=False, openai_key="", gemini_key="k",
                                                   meta_project="").agents())
        self.assertEqual(["meta"], tk.TalkClient(anthropic_ready=False, openai_key="", gemini_key="",
                                                 meta_project="sfdc24").agents())
        self.assertEqual(list(ALL), tk.TalkClient(anthropic_ready=True, openai_key="o", gemini_key="g",
                                                  meta_project="p").agents())
        with self.assertRaises(LookupError):
            none.reply("gemini", "hi", [], "")

    def test_history_accepts_every_speaker_the_controller_can_name(self):
        turns = [{"who": "you", "text": "hi"}] + [{"who": a, "text": "Hello."} for a in ALL]
        self.assertEqual(turns, tk.clean_history(turns))
        with self.assertRaises(ValueError):
            tk.clean_history([{"who": "grok", "text": "Hello."}])

    def test_each_agent_has_its_own_angle_and_the_policy_stays_last(self):
        angles = set()
        for agent in ALL:
            system = tk.talk_system(agent)
            self.assertTrue(system.endswith(tk.USE_POLICY), agent)
            self.assertIn(tk.PERSPECTIVES[agent], system)
            angles.add(tk.PERSPECTIVES[agent])
        self.assertEqual(4, len(angles))


if __name__ == "__main__":
    unittest.main()

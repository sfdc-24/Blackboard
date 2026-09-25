"""The topic a visitor picks on the homepage before Start (owner, 2026-09-25).

It is a closed set, stored on the session at creation, and it reaches every
lane's context - talk, recap, analyst and builder - so the right agents start
from the same brief without the visitor knowing which agent that is.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import tests.test_studio_controller as base  # noqa: E402  (sets sys.path for app.*; not re-run here)
from app.state import StudioRepository  # noqa: E402
from workers import claude_worker as cw  # noqa: E402
from workers.topics import TOPICS, topic_line, with_topic  # noqa: E402


class TopicTests(unittest.TestCase):
    origin = base.TalkLaneTests.origin
    make = base.TalkLaneTests.make          # the talk lane's app: FakeTalk records each call

    def create(self, client, body):
        if not getattr(self, "operator", None):
            started = client.post("/v1/auth/start", headers=self.origin, json={
                "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
            code = self.email_sender.calls[-1][1]
            self.operator = client.post("/v1/auth/verify", headers=self.origin, json={
                "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
                "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
        return client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + self.operator},
                           json=body)

    def test_health_says_topics_are_taken(self):
        with TestClient(self.make()) as client:
            self.assertTrue(client.get("/health").json()["features"]["topics"])

    def test_a_topic_is_stored_on_the_session(self):
        with TestClient(self.make()) as client:
            created = self.create(client, {"creation_id": "topic-1", "start": "blank", "topic": "salesforce_data"})
        self.assertEqual(200, created.status_code, created.text)
        state = StudioRepository(self.store).load(created.json()["session_id"]).state
        self.assertEqual("salesforce_data", state["topic"])

    def test_no_topic_is_the_empty_topic(self):
        with TestClient(self.make()) as client:
            created = self.create(client, {"creation_id": "topic-2", "start": "blank"})
        state = StudioRepository(self.store).load(created.json()["session_id"]).state
        self.assertEqual("", state["topic"])

    def test_a_topic_outside_the_set_is_refused(self):
        with TestClient(self.make()) as client:
            for bad in ("hacking", "Logo", 7, ["logo"], "logo\n"):
                created = self.create(client, {"creation_id": "topic-bad", "start": "blank", "topic": bad})
                self.assertEqual(400, created.status_code, (bad, created.text))

    def test_the_talk_lane_hears_the_topic(self):
        with TestClient(self.make()) as client:
            created = self.create(client, {"creation_id": "topic-3", "start": "blank", "topic": "logo"}).json()
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            client.post("/v1/session/%s/talk" % created["session_id"], headers=headers, json={"text": "hello"})
        self.assertTrue(self.talk.calls[0]["canvas"].startswith(
            "The visitor picked this topic before starting: Design a logo"), self.talk.calls[0]["canvas"])


class TopicLine(unittest.TestCase):
    def test_every_topic_has_a_line_and_nothing_else_does(self):
        for topic in TOPICS:
            self.assertIn(TOPICS[topic], topic_line({"topic": topic}))
        for other in ("", None, "hacking", 5):
            self.assertEqual("", topic_line({"topic": other}))
        self.assertEqual("", topic_line(None))
        self.assertEqual("canvas", with_topic({}, "canvas"))

    def test_the_builder_sees_the_topic_first(self):
        state = {"artifact": {"id": "screen", "kind": "screen", "label": "x", "children": []},
                 "questions": [], "transcript": [], "topic": "website"}
        described = cw._describe(state, {"kind": "utterance", "text": "a page"})
        self.assertTrue(described.startswith("The visitor picked this topic before starting: Build a website"),
                        described[:120])
        self.assertNotIn("picked this topic", cw._describe(dict(state, topic=""), {"kind": "utterance", "text": "a"}))


if __name__ == "__main__":
    unittest.main()

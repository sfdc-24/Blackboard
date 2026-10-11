"""The telephone trail on the studio controller: a live session's page reads the doc's receipts.

Off unless the URL is set (404). A session token for THIS session, the doc id checked, the
upstream read carrying this service's identity and the shared token, only the trail keys handed
on, never the request, one upstream read per doc per few seconds, and a refusal upstream named as
502, never forged into a trail.
"""
import json
import unittest

from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, FakeVoiceClient, IDs, MemoryStore, settings,
)
from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.tokens import mint_token  # noqa: E402

ORIGIN = {"Origin": "https://www.sfdc24.com"}
DOC = "1pKWJYbJzMqxSA9Vy_PSR93BU2OAAgP-N3WBEkCj7oj4"
TRAIL = {
    "enabled": True, "status": "ok", "doc": DOC, "revision": "r7", "task": "task-0123456789abcdef",
    "request": "OT-20261010", "tasks": ["task-0123456789abcdef"], "at": "2026-10-11T07:00:00Z",
    "entries": [{"state": "REQUESTED", "task": "task-0123456789abcdef", "request": "OT-20261010",
                 "rev": "abcdef012345", "recipient": "claude-api", "by": "drive-intake",
                 "at": "2026-10-11T04:00:00Z", "detail": "", "id": "1111111111111111", "result": []}],
    "live": {"laptop": "offline", "api_worker": "online", "intake": "dispatched", "pending": False,
             "worker_claim": "done", "doorbell": "none"},
    "request_text": "NEVER HANDED ON", "secret": "nor this",
}


class Answer:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        self.content = raw if raw is not None else json.dumps(body if body is not None else {}).encode("utf-8")
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class FakeTelephone:
    """The upstream drive-intake, recorded: every GET with its url, params and headers."""

    def __init__(self, answer=None):
        self.calls, self.answer, self.closed = [], answer or Answer(200, TRAIL), False

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        return self.answer

    def close(self):
        self.closed = True


class TelephoneTests(unittest.TestCase):
    def app(self, **overrides):
        self.store, self.ids, self.email_sender = MemoryStore(), IDs(), EmailSender()
        self.upstream = overrides.pop("upstream", FakeTelephone())
        self.now = [1000.0]
        values = dict(telephone_url="https://drive-intake-96522051727.us-central1.run.app",
                      telephone_token="poll-secret")
        values.update(overrides)
        self.configured = settings(**values)
        return create_app(settings=self.configured, store=self.store, worker=CountingWorker(),
                          clock=lambda: self.now[0], id_factory=self.ids, voice_client=FakeVoiceClient(),
                          email_sender=self.email_sender, telephone_client=self.upstream,
                          identity_token=lambda audience: "idtok-for-" + audience)

    def session(self, client):
        key = "browser-instance-1234567890"
        started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": "operator@example.com", "client_key": key})
        self.assertEqual(200, started.status_code, started.text)
        code = self.email_sender.calls[-1][1]
        verified = client.post("/v1/auth/verify", headers=ORIGIN, json={
            "challenge_id": started.json()["challenge_id"], "email": "operator@example.com", "code": code, "client_key": key})
        self.assertEqual(200, verified.status_code, verified.text)
        made = client.post("/v1/session", headers={**ORIGIN, "Authorization": "Bearer " + verified.json()["token"]},
                           json={"title": "Test", "creation_id": "create-1"})
        self.assertEqual(200, made.status_code, made.text)
        body = made.json()
        return body["session_id"], {**ORIGIN, "Authorization": "Bearer " + body["token"]}

    def test_off_is_404_even_for_a_live_session_and_nothing_upstream_is_called(self):
        with TestClient(self.app(telephone_url="")) as client:
            sid, headers = self.session(client)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(404, response.status_code, response.text)
            self.assertEqual([], self.upstream.calls)

    def test_a_session_token_for_this_session_is_required_before_the_doc_is_even_looked_at(self):
        with TestClient(self.app()) as client:
            sid, headers = self.session(client)
            self.assertEqual(401, client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=ORIGIN).status_code)
            other = mint_token("someone-else", 2000, self.configured.session_secret)
            self.assertEqual(403, client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC},
                                             headers={**ORIGIN, "Authorization": "Bearer " + other}).status_code)
            self.assertEqual(400, client.get("/v1/session/%s/telephone" % sid, params={"doc": "short"}, headers=headers).status_code)
            self.assertEqual(400, client.get("/v1/session/%s/telephone" % sid, headers=headers).status_code)
            self.assertEqual(400, client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC + "/../x"}, headers=headers).status_code)
            self.assertEqual([], self.upstream.calls)

    def test_the_trail_is_read_as_this_service_with_the_shared_token_and_only_the_trail_keys_come_back(self):
        with TestClient(self.app()) as client:
            sid, headers = self.session(client)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(200, response.status_code, response.text)
            body = response.json()
            self.assertEqual(["REQUESTED"], [e["state"] for e in body["entries"]])
            self.assertEqual("task-0123456789abcdef", body["task"])
            self.assertEqual("online", body["live"]["api_worker"])
            self.assertNotIn("request_text", body)                       # only the trail keys
            self.assertNotIn("secret", body)
            self.assertNotIn("NEVER HANDED ON", response.text)
            url, params, sent = self.upstream.calls[0]
            self.assertEqual("https://drive-intake-96522051727.us-central1.run.app/trail", url)
            self.assertEqual({"doc": DOC}, params)
            self.assertEqual("poll-secret", sent["X-Drive-Intake-Poll-Token"])
            self.assertEqual("Bearer idtok-for-https://drive-intake-96522051727.us-central1.run.app", sent["Authorization"])

    def test_one_upstream_read_per_doc_every_five_seconds_whatever_the_page_asks(self):
        with TestClient(self.app()) as client:
            sid, headers = self.session(client)
            for _ in range(3):
                self.assertEqual(200, client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers).status_code)
            self.assertEqual(1, len(self.upstream.calls))
            self.now[0] += 5.0
            self.assertEqual(200, client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers).status_code)
            self.assertEqual(2, len(self.upstream.calls))
            other_doc = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"
            self.assertEqual(200, client.get("/v1/session/%s/telephone" % sid, params={"doc": other_doc}, headers=headers).status_code)
            self.assertEqual(3, len(self.upstream.calls))               # a different doc is its own read

    def test_a_refusal_or_a_broken_answer_upstream_is_502_and_never_a_trail(self):
        for answer, words in ((Answer(401, {"error": "bad poll token"}), "answered 401"),
                              (Answer(200, raw=b"not json"), "no JSON"),
                              (Answer(200, raw=b"[]"), "no object"),
                              (Answer(200, raw=b'{"status": "ok", "pad": "' + b"x" * 300000 + b'"}'), "larger")):
            with TestClient(self.app(upstream=FakeTelephone(answer))) as client:
                sid, headers = self.session(client)
                response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
                self.assertEqual(502, response.status_code, response.text)
                self.assertIn(words, response.json()["detail"])

        class Broken(FakeTelephone):
            def get(self, url, params=None, headers=None):
                raise ConnectionError("down")
        with TestClient(self.app(upstream=Broken())) as client:
            sid, headers = self.session(client)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(502, response.status_code)
            self.assertIn("ConnectionError", response.json()["detail"])

    def test_no_identity_token_sends_only_the_shared_token(self):
        with TestClient(self.app()) as client:
            client.app.state.telephone_client = self.upstream
            sid, headers = self.session(client)
        app = create_app(settings=self.configured, store=self.store, worker=CountingWorker(), clock=lambda: 1000,
                         id_factory=self.ids, voice_client=FakeVoiceClient(), email_sender=self.email_sender,
                         telephone_client=self.upstream, identity_token=lambda audience: "")
        with TestClient(app) as client:
            sid, headers = self.session(client)
            self.assertEqual(200, client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers).status_code)
            self.assertNotIn("Authorization", self.upstream.calls[-1][2])
            self.assertEqual("poll-secret", self.upstream.calls[-1][2]["X-Drive-Intake-Poll-Token"])

    def test_settings_come_from_env_and_the_url_loses_its_trailing_slash(self):
        import os
        from unittest import mock
        base = {k: v for k, v in os.environ.items() if not k.startswith("STUDIO_")}
        base.update({"STUDIO_ALLOWED_ORIGINS": "https://www.sfdc24.com", "STUDIO_SESSION_SECRET": "s" * 40,
                     "STUDIO_STATE_URI": "file://unused",
                     "STUDIO_TELEPHONE_URL": " https://drive-intake.example/ ", "STUDIO_TELEPHONE_TOKEN": "t"})
        with mock.patch.dict(os.environ, base, clear=True):
            made = Settings.from_env()
        self.assertEqual(("https://drive-intake.example", "t"), (made.telephone_url, made.telephone_token))
        base.pop("STUDIO_TELEPHONE_URL")
        base.pop("STUDIO_TELEPHONE_TOKEN")
        with mock.patch.dict(os.environ, base, clear=True):
            off = Settings.from_env()
        self.assertEqual(("", ""), (off.telephone_url, off.telephone_token))

    def test_the_client_this_app_opened_is_closed_with_it_and_an_injected_one_is_not(self):
        with TestClient(self.app()) as client:
            pass
        self.assertFalse(self.upstream.closed)


if __name__ == "__main__":
    unittest.main()

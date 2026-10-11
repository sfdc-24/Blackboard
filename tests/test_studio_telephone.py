"""The telephone trail on the studio controller: a live session's page reads the doc's receipts.

Off unless the URL is set (404). A session token for THIS session, the session still live, the
doc id checked and equal to the doc the server stored on that session, the upstream read carrying
this service's identity and the shared token, only the trail keys handed on, never the request,
one upstream read per session and doc per few seconds, and a refusal upstream named as 502, never
forged into a trail.
"""
import json
import unittest

from fastapi import HTTPException
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

    def session(self, client, creation_id="create-1"):
        key = "browser-instance-" + creation_id + "-1234567890"
        started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": "operator@example.com", "client_key": key})
        self.assertEqual(200, started.status_code, started.text)
        code = self.email_sender.calls[-1][1]
        verified = client.post("/v1/auth/verify", headers=ORIGIN, json={
            "challenge_id": started.json()["challenge_id"], "email": "operator@example.com", "code": code, "client_key": key})
        self.assertEqual(200, verified.status_code, verified.text)
        made = client.post("/v1/session", headers={**ORIGIN, "Authorization": "Bearer " + verified.json()["token"]},
                           json={"title": "Test", "creation_id": creation_id})
        self.assertEqual(200, made.status_code, made.text)
        body = made.json()
        return body["session_id"], {**ORIGIN, "Authorization": "Bearer " + body["token"]}, body

    def bind(self, client, session_id, doc):
        """The server stores the doc on the session. The query never does this."""
        client.app.state.bind_telephone_doc(session_id, doc)

    def forget(self, session_id):
        name = "studio_session_" + session_id
        with self.store.lock:
            self.store.data.pop(name, None)
            self.store.generation.pop(name, None)

    def test_off_is_404_even_for_a_live_session_and_nothing_upstream_is_called(self):
        with TestClient(self.app(telephone_url="")) as client:
            sid, headers, _created = self.session(client)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(404, response.status_code, response.text)
            self.assertEqual([], self.upstream.calls)

    def test_a_session_token_for_this_session_is_required_before_the_doc_is_even_looked_at(self):
        with TestClient(self.app()) as client:
            sid, headers, _created = self.session(client)
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
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
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
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
            for _ in range(3):
                self.assertEqual(200, client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers).status_code)
            self.assertEqual(1, len(self.upstream.calls))
            self.now[0] += 5.0
            self.assertEqual(200, client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers).status_code)
            self.assertEqual(2, len(self.upstream.calls))
            other_doc = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"
            # This session is not bound to the other doc, so the page asking does not read it.
            refused = client.get("/v1/session/%s/telephone" % sid, params={"doc": other_doc}, headers=headers)
            self.assertEqual(403, refused.status_code, refused.text)
            self.assertEqual(2, len(self.upstream.calls))
            sid2, headers2, _created = self.session(client, "create-2")
            self.bind(client, sid2, other_doc)
            self.assertEqual(200, client.get("/v1/session/%s/telephone" % sid2, params={"doc": other_doc}, headers=headers2).status_code)
            self.assertEqual(3, len(self.upstream.calls))               # a bound different doc is its own read

    def test_a_refusal_or_a_broken_answer_upstream_is_502_and_never_a_trail(self):
        for answer, words in ((Answer(401, {"error": "bad poll token"}), "answered 401"),
                              (Answer(200, raw=b"not json"), "no JSON"),
                              (Answer(200, raw=b"[]"), "no object"),
                              (Answer(200, raw=b'{"status": "ok", "pad": "' + b"x" * 300000 + b'"}'), "larger")):
            with TestClient(self.app(upstream=FakeTelephone(answer))) as client:
                sid, headers, _created = self.session(client)
                self.bind(client, sid, DOC)
                response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
                self.assertEqual(502, response.status_code, response.text)
                self.assertIn(words, response.json()["detail"])

        class Broken(FakeTelephone):
            def get(self, url, params=None, headers=None):
                raise ConnectionError("down")
        with TestClient(self.app(upstream=Broken())) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(502, response.status_code)
            self.assertIn("ConnectionError", response.json()["detail"])

    def test_no_identity_token_sends_only_the_shared_token(self):
        with TestClient(self.app()) as client:
            client.app.state.telephone_client = self.upstream
            sid, headers, _created = self.session(client)
        app = create_app(settings=self.configured, store=self.store, worker=CountingWorker(), clock=lambda: 1000,
                         id_factory=self.ids, voice_client=FakeVoiceClient(), email_sender=self.email_sender,
                         telephone_client=self.upstream, identity_token=lambda audience: "")
        with TestClient(app) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
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

    def test_a_stopped_deleted_or_unknown_session_is_refused_before_any_doc_read(self):
        with TestClient(self.app()) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
            stopped = client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "stop-1", "session_id": sid, "type": "stop", "expected_version": 1})
            self.assertEqual(200, stopped.status_code, stopped.text)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(410, response.status_code, response.text)
            self.assertEqual([], self.upstream.calls)

            sid, headers, _created = self.session(client, "create-deleted")
            self.bind(client, sid, DOC)
            self.forget(sid)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(404, response.status_code, response.text)
            self.assertIn("session not found", response.text)
            self.assertEqual([], self.upstream.calls)

            ghost = "s-not-a-session"
            token = mint_token(ghost, 2000, self.configured.session_secret)
            response = client.get("/v1/session/%s/telephone" % ghost, params={"doc": DOC},
                                  headers={**ORIGIN, "Authorization": "Bearer " + token})
            self.assertEqual(404, response.status_code, response.text)
            self.assertIn("session not found", response.text)
            self.assertEqual([], self.upstream.calls)

    def test_a_doc_not_bound_server_side_is_refused_with_no_content_size_or_existence_leak(self):
        with TestClient(self.app()) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
            other = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"
            missing = "0123456789abcdefghij"
            wrong = client.get("/v1/session/%s/telephone" % sid, params={"doc": other}, headers=headers)
            absent = client.get("/v1/session/%s/telephone" % sid, params={"doc": missing}, headers=headers)
            self.assertEqual(403, wrong.status_code, wrong.text)
            self.assertEqual({"detail": "this session is not bound to that doc"}, wrong.json())
            self.assertEqual(wrong.json(), absent.json())
            self.assertEqual(len(wrong.content), len(absent.content))
            for body in (wrong.text, absent.text):
                self.assertNotIn("entries", body)
                self.assertNotIn("NEVER HANDED ON", body)
                self.assertNotIn("r7", body)
                self.assertNotIn("revision", body)
            self.assertEqual([], self.upstream.calls)
            # A second live session with no binding is refused the same way, even for the doc that exists upstream.
            sid2, headers2, _created = self.session(client, "create-unbound")
            naked = client.get("/v1/session/%s/telephone" % sid2, params={"doc": DOC}, headers=headers2)
            self.assertEqual(wrong.json(), naked.json())
            self.assertEqual([], self.upstream.calls)
            owned = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(200, owned.status_code, owned.text)
            self.assertEqual(["REQUESTED"], [e["state"] for e in owned.json()["entries"]])
            self.assertEqual(1, len(self.upstream.calls))

    def test_a_cached_trail_cannot_be_served_across_sessions(self):
        with TestClient(self.app()) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
            first = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(200, first.status_code, first.text)
            self.assertEqual([(sid, DOC)], list(client.app.state.telephone_cache))
            self.assertEqual(1, len(self.upstream.calls))
            sid2, headers2, _created = self.session(client, "create-2")
            leaked = client.get("/v1/session/%s/telephone" % sid2, params={"doc": DOC}, headers=headers2)
            self.assertEqual(403, leaked.status_code, leaked.text)
            self.assertNotIn("task-0123456789abcdef", leaked.text)
            self.assertNotIn("NEVER HANDED ON", leaked.text)
            self.assertNotIn("entries", leaked.text)
            self.assertEqual(1, len(self.upstream.calls))
            # A doc-only cache slot is the old key. Neither session is served from it.
            client.app.state.telephone_cache[DOC] = (self.now[0], {"status": "ok", "entries": [], "secret": "CROSS-SESSION"})
            again = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(first.json(), again.json())
            self.assertNotIn("CROSS-SESSION", again.text)
            self.assertEqual(1, len(self.upstream.calls))
            self.bind(client, sid2, DOC)
            second = client.get("/v1/session/%s/telephone" % sid2, params={"doc": DOC}, headers=headers2)
            self.assertEqual(200, second.status_code, second.text)
            self.assertNotIn("CROSS-SESSION", second.text)
            self.assertEqual(2, len(self.upstream.calls))          # its own read, not the other session's slot
            self.assertIn((sid2, DOC), client.app.state.telephone_cache)
            self.assertNotEqual(id(client.app.state.telephone_cache[(sid, DOC)]),
                                id(client.app.state.telephone_cache[(sid2, DOC)]))

    def stored(self, session_id):
        state, _token = self.store.load("studio_session_" + session_id)
        return state

    def test_a_session_created_normally_is_not_bound_and_cannot_read_a_trail(self):
        """POST /v1/session assigns no Drive doc, so the flag-on read stays 403.

        The query, a doc field, and a creation_id that looks like a file id are
        all the client's. None of them is written to telephone_doc, and the
        upstream trail is not called.
        """
        with TestClient(self.app()) as client:
            sid, headers, created = self.session(client, DOC)   # creation_id is client-controlled
            self.assertNotIn("doc", created)
            self.assertNotIn("telephone_doc", created)
            state = self.stored(sid)
            self.assertNotIn("telephone_doc", state)
            self.assertNotEqual(DOC, state.get("task_id"))
            own = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            other = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"
            foreign = client.get("/v1/session/%s/telephone" % sid, params={"doc": other}, headers=headers)
            self.assertEqual(403, own.status_code, own.text)
            self.assertEqual({"detail": "this session is not bound to that doc"}, own.json())
            self.assertEqual(own.json(), foreign.json())
            self.assertEqual(len(own.content), len(foreign.content))
            for body in (own.text, foreign.text):
                self.assertNotIn("entries", body)
                self.assertNotIn("NEVER HANDED ON", body)
                self.assertNotIn("revision", body)
            self.assertNotIn("telephone_doc", self.stored(sid))   # the query was not copied in
            self.assertEqual([], self.upstream.calls)

            sid2, headers2, _second = self.session(client, "create-2")
            leaked = client.get("/v1/session/%s/telephone" % sid2, params={"doc": DOC}, headers=headers2)
            self.assertEqual(own.json(), leaked.json())
            self.assertNotIn("telephone_doc", self.stored(sid2))
            self.assertEqual([], self.upstream.calls)

            key = "browser-instance-body-doc-1234567890"
            started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": "operator@example.com", "client_key": key})
            code = self.email_sender.calls[-1][1]
            verified = client.post("/v1/auth/verify", headers=ORIGIN, json={
                "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
                "code": code, "client_key": key})
            operator = {**ORIGIN, "Authorization": "Bearer " + verified.json()["token"]}
            rejected = client.post("/v1/session", headers=operator,
                                   json={"title": "Test", "creation_id": "create-body-doc", "doc": DOC})
            self.assertEqual(400, rejected.status_code, rejected.text)
            queried = client.post("/v1/session", headers=operator, params={"doc": DOC},
                                  json={"title": "Test", "creation_id": "create-query-doc"})
            self.assertEqual(200, queried.status_code, queried.text)
            created = queried.json()
            self.assertNotIn("doc", created)
            self.assertNotIn("telephone_doc", created)
            self.assertNotIn("telephone_doc", self.stored(created["session_id"]))
            still = client.get("/v1/session/%s/telephone" % created["session_id"],
                               params={"doc": DOC}, headers={**ORIGIN, "Authorization": "Bearer " + created["token"]})
            self.assertEqual(own.json(), still.json())
            self.assertEqual([], self.upstream.calls)

    def test_with_the_flag_unset_session_creation_does_not_bind_and_other_routes_stay(self):
        """STUDIO_TELEPHONE_URL unset: the route is 404, creation stores nothing, events stay put."""
        with TestClient(self.app(telephone_url="")) as client:
            sid, headers, created = self.session(client)
            self.assertEqual(
                {"session_id", "generation", "artifact_version", "expires_at",
                 "max_session_seconds", "daily_admission_number", "token", "events_url"},
                set(created))
            self.assertNotIn("telephone_doc", self.stored(sid))
            trail = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(404, trail.status_code, trail.text)
            self.assertIn("off", trail.text)
            self.assertNotIn("entries", trail.text)
            self.assertNotIn("telephone_doc", self.stored(sid))
            self.assertEqual([], self.upstream.calls)
            events = client.get("/v1/session/%s/events" % sid, params={"once": "true"}, headers=headers)
            self.assertEqual(200, events.status_code, events.text)
            self.assertIn("session.started", events.text)
            self.assertNotIn("telephone_doc", events.text)
            self.assertNotIn(DOC, events.text)
            with self.assertRaises(HTTPException) as caught:
                client.app.state.bind_telephone_doc(sid, DOC)
            self.assertEqual(404, caught.exception.status_code)
            self.assertNotIn("telephone_doc", self.stored(sid))
            self.assertEqual([], self.upstream.calls)
            off_keys = set(created)
            off_events = events.text

        with TestClient(self.app()) as client:
            sid_on, headers_on, created_on = self.session(client)
            # The creation answer is the same shape with the flag on. No doc is added to it.
            self.assertEqual(off_keys, set(created_on))
            self.assertNotIn("telephone_doc", self.stored(sid_on))
            events_on = client.get("/v1/session/%s/events" % sid_on, params={"once": "true"}, headers=headers_on)
            self.assertEqual(200, events_on.status_code, events_on.text)
            self.assertIn("session.started", events_on.text)
            self.assertNotIn("telephone_doc", events_on.text)
            # Session ids differ, so the streams are not byte-identical. The event types are.
            def types(body):
                return [line for line in body.splitlines() if line.startswith("event: ")]
            self.assertEqual(types(off_events), types(events_on.text))
            refused = client.get("/v1/session/%s/telephone" % sid_on, params={"doc": DOC}, headers=headers_on)
            self.assertEqual(403, refused.status_code, refused.text)
            self.assertNotIn("telephone_doc", self.stored(sid_on))
            self.assertEqual([], self.upstream.calls)

    def test_the_trail_stays_off_when_studio_telephone_url_is_unset(self):
        import os
        from unittest import mock
        base = {k: v for k, v in os.environ.items() if not k.startswith("STUDIO_")}
        base.update({"STUDIO_ALLOWED_ORIGINS": "https://www.sfdc24.com", "STUDIO_SESSION_SECRET": "s" * 40,
                     "STUDIO_STATE_URI": "file://unused"})
        with mock.patch.dict(os.environ, base, clear=True):
            off = Settings.from_env()
        self.assertEqual("", off.telephone_url)
        with TestClient(self.app(telephone_url="")) as client:
            sid, headers, _created = self.session(client)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(404, response.status_code, response.text)
            self.assertIn("off", response.text)
            self.assertEqual([], self.upstream.calls)
            with self.assertRaises(HTTPException) as caught:
                client.app.state.bind_telephone_doc(sid, DOC)
            self.assertEqual(404, caught.exception.status_code)
            self.assertEqual([], self.upstream.calls)


if __name__ == "__main__":
    unittest.main()

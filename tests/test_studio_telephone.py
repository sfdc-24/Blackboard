"""The telephone trail on the studio controller: a live session's page reads the doc's receipts.

Off unless the URL is set (404). A session token for THIS session, the session still live, the
doc id checked and equal to the doc the server stored on that session, the upstream read carrying
this service's identity and the shared token, only the trail keys handed on, never the request,
one upstream read per session and doc per few seconds, and a refusal upstream named as 502, never
forged into a trail.
"""
import asyncio
import json
import threading
import unittest

import httpx

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
        self.calls, self.answer, self.closed = [], answer, False

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        # Unless a test fixed the answer, drive-intake answers for the doc it was asked for.
        return self.answer or Answer(200, dict(TRAIL, doc=(params or {}).get("doc")))

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

        # A JSON object is not yet a trail (Copilot on ff15a56): {} and a list where the entries
        # belong were handed to the page as one. Each is a broken upstream, and says which way.
        entry = TRAIL["entries"][0]
        for body, words in (({}, "no trail"),
                            ({"enabled": True}, "no trail"),
                            (dict(TRAIL, enabled="yes"), "no trail"),
                            (dict(TRAIL, status="fine"), "no trail"),
                            (dict(TRAIL, doc="1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"), "another doc"),
                            (dict(TRAIL, entries="not a list"), "no known shape"),
                            (dict(TRAIL, entries=[["REQUESTED"]]), "no known shape"),
                            (dict(TRAIL, entries=[dict(entry, state=7)]), "no known shape"),
                            (dict(TRAIL, entries=[{k: v for k, v in entry.items() if k != "at"}]), "no known shape"),
                            (dict(TRAIL, entries=[dict(entry, result="one line")]), "no known shape"),
                            (dict(TRAIL, entries=[dict(entry, result=[{"x": 1}])]), "no known shape"),
                            (dict(TRAIL, tasks="task-0123456789abcdef"), "no known shape"),
                            (dict(TRAIL, tasks=[7]), "no known shape"),
                            (dict(TRAIL, live=["online"]), "no known shape"),
                            (dict(TRAIL, live={"laptop": {"forged": "html"}}), "no known shape"),
                            (dict(TRAIL, task={"id": 1}), "no known shape"),
                            ({k: v for k, v in TRAIL.items() if k != "live"}, "no known shape")):
            with TestClient(self.app(upstream=FakeTelephone(Answer(200, body)))) as client:
                sid, headers, _created = self.session(client)
                self.bind(client, sid, DOC)
                response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
                self.assertEqual(502, response.status_code, (body, response.text))
                self.assertIn(words, response.json()["detail"], body)

        class Broken(FakeTelephone):
            def get(self, url, params=None, headers=None):
                raise ConnectionError("down")
        with TestClient(self.app(upstream=Broken())) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(502, response.status_code)
            self.assertIn("ConnectionError", response.json()["detail"])

    def test_the_answers_that_are_not_a_trail_but_are_not_broken_keep_their_own_shape(self):
        # The intake's own flag off, and the four ways it says it could not give the trail: each
        # is a true answer, passed on with only its own keys, and none of them carries entries.
        for body, expected in (
                ({"enabled": False, "trail": None}, {"enabled": False}),
                ({"enabled": True, "doc": DOC, "at": "2026-10-11T07:00:00Z", "status": "no_store"},
                 {"enabled": True, "doc": DOC, "at": "2026-10-11T07:00:00Z", "status": "no_store"}),
                ({"enabled": True, "doc": DOC, "at": "t", "status": "read_error", "error": "HttpError"},
                 {"enabled": True, "doc": DOC, "at": "t", "status": "read_error", "error": "HttpError"}),
                ({"enabled": True, "doc": DOC, "at": "t", "status": "out_of_scope",
                  "entries": [{"state": "FORGED"}], "request_text": "x"},
                 {"enabled": True, "doc": DOC, "at": "t", "status": "out_of_scope"}),
                ({"enabled": True, "doc": DOC, "at": "t", "status": "too_large", "chars": 900000},
                 {"enabled": True, "doc": DOC, "at": "t", "status": "too_large"})):
            with TestClient(self.app(upstream=FakeTelephone(Answer(200, body)))) as client:
                sid, headers, _created = self.session(client)
                self.bind(client, sid, DOC)
                response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(expected, response.json())

    def test_an_entry_is_rebuilt_from_its_named_keys_and_nothing_rides_inside_it(self):
        entry = dict(TRAIL["entries"][0], request_text="NEVER HANDED ON", result=["the answer"])
        body = dict(TRAIL, entries=[entry])
        with TestClient(self.app(upstream=FakeTelephone(Answer(200, body)))) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
            response = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(200, response.status_code, response.text)
            self.assertNotIn("NEVER HANDED ON", response.text)
            got = response.json()["entries"][0]
            self.assertEqual(["the answer"], got["result"])
            self.assertEqual({"state", "task", "request", "rev", "recipient", "by", "at", "detail", "id", "result"},
                             set(got))

    def test_callers_that_miss_the_cache_together_share_one_upstream_read(self):
        # Copilot on ff15a56: the lookup and the read were not one step, so every caller that
        # arrived before the first read came back made its own Docs read. Ten arrive while the
        # upstream is held; one read goes out, and all ten get its answer.
        gate, arrived = threading.Event(), threading.Event()

        class Slow(FakeTelephone):
            def get(self, url, params=None, headers=None):
                answer = super().get(url, params, headers)
                arrived.set()
                gate.wait(10)
                return answer

        app = self.app(upstream=Slow())
        with TestClient(app) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
            sid2, headers2, _created = self.session(client, "create-2")
            self.bind(client, sid2, DOC)

        async def storm():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                calls = [http.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
                         for _ in range(10)]
                tasks = [asyncio.ensure_future(call) for call in calls]
                await asyncio.to_thread(arrived.wait, 10)
                await asyncio.sleep(0.2)                 # the other nine are now waiting on the same read
                first_reads = len(self.upstream.calls)
                gate.set()
                answers = await asyncio.gather(*tasks)
                # Another session's read of the same doc is its own: a flight is per session and doc.
                other = await http.get("/v1/session/%s/telephone" % sid2, params={"doc": DOC}, headers=headers2)
                return first_reads, answers, other

        first_reads, answers, other = asyncio.run(storm())
        self.assertEqual(1, first_reads)
        self.assertEqual([200] * 10, [answer.status_code for answer in answers])
        self.assertEqual(1, len({answer.text for answer in answers}))
        self.assertEqual(200, other.status_code)
        self.assertEqual(2, len(self.upstream.calls))
        self.assertEqual({}, app.state.telephone_flights)                 # nothing left in flight

    def test_a_failed_read_is_shared_too_and_is_not_kept(self):
        gate, arrived = threading.Event(), threading.Event()

        class Down(FakeTelephone):
            def get(self, url, params=None, headers=None):
                self.calls.append((url, dict(params or {}), dict(headers or {})))
                arrived.set()
                gate.wait(10)
                raise ConnectionError("down")

        app = self.app(upstream=Down())
        with TestClient(app) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)

        async def storm():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                tasks = [asyncio.ensure_future(http.get("/v1/session/%s/telephone" % sid, params={"doc": DOC},
                                                        headers=headers)) for _ in range(5)]
                await asyncio.to_thread(arrived.wait, 10)
                await asyncio.sleep(0.2)
                gate.set()
                answers = await asyncio.gather(*tasks)
                again = await http.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
                return answers, again

        answers, again = asyncio.run(storm())
        self.assertEqual([502] * 5, [answer.status_code for answer in answers])
        self.assertEqual(502, again.status_code)                          # a failure is not cached: asked again
        self.assertEqual(2, len(self.upstream.calls))
        self.assertEqual({}, app.state.telephone_flights)
        self.assertEqual({}, app.state.telephone_cache)

    def test_a_page_that_goes_away_does_not_cancel_the_read_the_others_wait_on(self):
        gate, arrived = threading.Event(), threading.Event()

        class Slow(FakeTelephone):
            def get(self, url, params=None, headers=None):
                answer = super().get(url, params, headers)
                arrived.set()
                gate.wait(10)
                return answer

        app = self.app(upstream=Slow())
        with TestClient(app) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)

        async def storm():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                ask = lambda: http.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)  # noqa: E731
                first = asyncio.ensure_future(ask())
                await asyncio.to_thread(arrived.wait, 10)
                second = asyncio.ensure_future(ask())
                await asyncio.sleep(0.2)                 # both now wait on the one read
                first.cancel()                           # the page that started it goes away
                await asyncio.sleep(0.1)
                gate.set()
                return await second

        answer = asyncio.run(storm())
        self.assertEqual(200, answer.status_code, answer.text)
        self.assertEqual(1, len(self.upstream.calls))

    def test_a_trail_past_its_five_seconds_is_dropped_when_the_next_one_is_kept(self):
        app = self.app()
        with TestClient(app) as client:
            sid, headers, _created = self.session(client)
            self.bind(client, sid, DOC)
            sid2, headers2, _created = self.session(client, "create-2")
            self.bind(client, sid2, DOC)
            self.assertEqual(200, client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers).status_code)
            self.assertEqual({(sid, DOC)}, set(app.state.telephone_cache))
            self.now[0] += 6.0
            self.assertEqual(200, client.get("/v1/session/%s/telephone" % sid2, params={"doc": DOC}, headers=headers2).status_code)
            self.assertEqual({(sid2, DOC)}, set(app.state.telephone_cache))   # the first session's trail is gone

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

    def test_no_http_caller_can_bind_a_doc(self):
        """bind_telephone_doc is in-process only. A signed-in client, an
        unauthenticated caller, and a forged doc id have no route that stores one.
        """
        with TestClient(self.app()) as client:
            sid, headers, _created = self.session(client)
            attempts = (
                client.post("/v1/session/%s/telephone" % sid, headers=headers, json={"doc": DOC}),
                client.put("/v1/session/%s/telephone" % sid, headers=headers, json={"doc": DOC}),
                client.post("/v1/session/%s/telephone/bind" % sid, headers=headers, json={"doc": DOC}),
                client.post("/v1/telephone/bind", headers=headers, json={"session_id": sid, "doc": DOC}),
                client.post("/v1/session/%s/telephone" % sid, headers=ORIGIN, json={"doc": DOC}),
                client.post("/v1/telephone/bind", json={"session_id": sid, "doc": DOC}),
            )
            for response in attempts:
                self.assertIn(response.status_code, (404, 405), response.text)
                self.assertNotIn("telephone_doc", response.text)
                self.assertNotIn("entries", response.text)
            self.assertNotIn("telephone_doc", self.stored(sid))
            self.assertEqual([], self.upstream.calls)
            refused = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=ORIGIN)
            self.assertEqual(401, refused.status_code, refused.text)
            self.assertNotIn("telephone_doc", self.stored(sid))

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


CODE = "ABCDEFGHIJKLMNOP"
OTHER_DOC = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"


class PairingUpstream(FakeTelephone):
    """drive-intake with POST /pair: a code opens the doc in `codes`, anything else is its 404."""

    def __init__(self, codes=None, pair_answer=None, pair_raises=None):
        super().__init__()
        self.codes = {CODE: DOC} if codes is None else codes
        self.pair_answer, self.pair_raises, self.posts = pair_answer, pair_raises, []

    def post(self, url, json=None, headers=None):
        self.posts.append((url, dict(json or {}), dict(headers or {})))
        if self.pair_raises is not None:
            raise self.pair_raises
        if self.pair_answer is not None:
            return self.pair_answer
        doc = self.codes.get((json or {}).get("code"))
        return Answer(200, {"enabled": True, "doc": doc}) if doc else Answer(404, {"error": "no such pairing code"})


class PairingTests(unittest.TestCase):
    """The binding (step 4): an operator's live session is bound to the doc a pairing code opens, and
    drive-intake, not the browser, names that doc."""

    # The same app, sign-in and store reads as TelephoneTests, without running its tests twice.
    app = TelephoneTests.app
    session = TelephoneTests.session
    stored = TelephoneTests.stored

    def pair(self, client, sid, headers, body=None):
        return client.post("/v1/session/%s/telephone/pair" % sid, headers=headers,
                           json={"code": CODE} if body is None else body)

    def test_a_code_binds_the_doc_drive_intake_names_and_then_the_trail_reads(self):
        with TestClient(self.app(upstream=PairingUpstream())) as client:
            sid, headers, _created = self.session(client)
            response = self.pair(client, sid, headers)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual({"doc": DOC}, response.json())
            self.assertEqual(DOC, self.stored(sid)["telephone_doc"])
            url, sent, auth = self.upstream.posts[0]
            self.assertEqual("https://drive-intake-96522051727.us-central1.run.app/pair", url)
            self.assertEqual({"code": CODE}, sent)
            self.assertEqual("poll-secret", auth["X-Drive-Intake-Poll-Token"])
            self.assertEqual("Bearer idtok-for-https://drive-intake-96522051727.us-central1.run.app", auth["Authorization"])
            self.assertEqual([], self.upstream.calls)                    # pairing reads no trail
            trail = client.get("/v1/session/%s/telephone" % sid, params={"doc": DOC}, headers=headers)
            self.assertEqual(200, trail.status_code, trail.text)
            other = client.get("/v1/session/%s/telephone" % sid, params={"doc": OTHER_DOC}, headers=headers)
            self.assertEqual(403, other.status_code, other.text)        # bound to one doc, not to any

    def test_the_body_is_the_code_alone_and_a_doc_in_it_binds_nothing(self):
        with TestClient(self.app(upstream=PairingUpstream())) as client:
            sid, headers, _created = self.session(client)
            for body in ({"code": CODE, "doc": OTHER_DOC}, {"doc": DOC}, {}, {"code": CODE.lower()},
                         {"code": CODE[:15]}, {"code": 7}, {"code": [CODE]}, {"session_id": sid, "code": CODE}):
                response = self.pair(client, sid, headers, body)
                self.assertEqual(400, response.status_code, (body, response.text))
            self.assertEqual(400, client.post("/v1/session/%s/telephone/pair" % sid, headers=headers,
                                              content=b"not json").status_code)
            self.assertEqual([], self.upstream.posts)
            self.assertNotIn("telephone_doc", self.stored(sid))

    def test_an_unknown_or_expired_code_is_403_and_binds_nothing(self):
        with TestClient(self.app(upstream=PairingUpstream(codes={}))) as client:
            sid, headers, _created = self.session(client)
            response = self.pair(client, sid, headers)
            self.assertEqual(403, response.status_code, response.text)
            self.assertEqual({"detail": "that code does not open a trail"}, response.json())
            self.assertNotIn(DOC, response.text)
            self.assertNotIn("telephone_doc", self.stored(sid))

    def test_drive_intake_off_broken_or_vague_is_502_and_binds_nothing(self):
        answers = (Answer(200, {"enabled": False, "doc": None}), Answer(500, {"error": "x"}), Answer(401, {}),
                   Answer(200, raw=b"not json"), Answer(200, ["a"]), Answer(200, {"enabled": True, "doc": "short"}),
                   Answer(200, {"enabled": True}), Answer(200, {"doc": DOC}), Answer(200, {"enabled": "yes", "doc": DOC}))
        for answer in answers:
            with TestClient(self.app(upstream=PairingUpstream(pair_answer=answer))) as client:
                sid, headers, _created = self.session(client)
                response = self.pair(client, sid, headers)
                self.assertEqual(502, response.status_code, (answer.content, response.text))
                self.assertNotIn("telephone_doc", self.stored(sid))
        with TestClient(self.app(upstream=PairingUpstream(pair_raises=ConnectionError("down")))) as client:
            sid, headers, _created = self.session(client)
            response = self.pair(client, sid, headers)
            self.assertEqual(502, response.status_code, response.text)
            self.assertIn("ConnectionError", response.text)
            self.assertNotIn("telephone_doc", self.stored(sid))

    def test_off_is_404_and_drive_intake_is_never_asked(self):
        with TestClient(self.app(telephone_url="", upstream=PairingUpstream())) as client:
            sid, headers, _created = self.session(client)
            self.assertEqual(404, self.pair(client, sid, headers).status_code)
            self.assertEqual([], self.upstream.posts)
            self.assertNotIn("telephone_doc", self.stored(sid))

    def test_only_this_sessions_token_and_a_live_session_can_pair(self):
        with TestClient(self.app(upstream=PairingUpstream())) as client:
            sid, headers, _created = self.session(client)
            self.assertEqual(401, self.pair(client, sid, ORIGIN).status_code)
            other = mint_token("someone-else", 2000, self.configured.session_secret)
            self.assertEqual(403, self.pair(client, sid, {**ORIGIN, "Authorization": "Bearer " + other}).status_code)
            self.assertEqual(403, self.pair(client, sid, {"Authorization": headers["Authorization"],
                                                          "Origin": "https://evil.example"}).status_code)
            stopped = client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "stop-1", "session_id": sid, "type": "stop", "expected_version": 1})
            self.assertEqual(200, stopped.status_code, stopped.text)
            self.assertEqual(410, self.pair(client, sid, headers).status_code)
            ghost = "s-not-a-session"
            token = mint_token(ghost, 2000, self.configured.session_secret)
            self.assertEqual(404, self.pair(client, ghost, {**ORIGIN, "Authorization": "Bearer " + token}).status_code)
            self.assertEqual([], self.upstream.posts)
            self.assertNotIn("telephone_doc", self.stored(sid))

    def test_a_pilots_session_cannot_pair(self):
        guest = "guest@lab.example"
        with TestClient(self.app(upstream=PairingUpstream(), pilot_enabled=True, pilot_emails=(guest,),
                                 pilot_seconds=300)) as client:
            key = "browser-instance-pilot-1234567890"
            started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": guest, "client_key": key})
            code = [c for e, c in self.email_sender.calls if e == guest][-1]
            verified = client.post("/v1/auth/verify", headers=ORIGIN, json={
                "challenge_id": started.json()["challenge_id"], "email": guest, "code": code, "client_key": key})
            self.assertEqual("pilot", verified.json()["scope"], verified.text)
            made = client.post("/v1/session", headers={**ORIGIN, "Authorization": "Bearer " + verified.json()["token"]},
                               json={"title": "Test", "creation_id": "pilot-1"})
            self.assertEqual(200, made.status_code, made.text)
            sid = made.json()["session_id"]
            self.assertEqual("", self.stored(sid).get("operator_subject"))
            response = self.pair(client, sid, {**ORIGIN, "Authorization": "Bearer " + made.json()["token"]})
            self.assertEqual(403, response.status_code, response.text)
            self.assertIn("operator", response.text)
            self.assertEqual([], self.upstream.posts)
            self.assertNotIn("telephone_doc", self.stored(sid))

    def test_a_session_is_bound_once_the_same_doc_again_is_a_no_op_and_another_is_409(self):
        upstream = PairingUpstream(codes={CODE: DOC, "QRSTUVWXYZ234567": DOC, "ZZZZZZZZZZZZZZZZ": OTHER_DOC})
        with TestClient(self.app(upstream=upstream)) as client:
            sid, headers, _created = self.session(client)
            self.assertEqual(200, self.pair(client, sid, headers).status_code)
            again = self.pair(client, sid, headers, {"code": "QRSTUVWXYZ234567"})
            self.assertEqual((200, {"doc": DOC}), (again.status_code, again.json()))
            moved = self.pair(client, sid, headers, {"code": "ZZZZZZZZZZZZZZZZ"})
            self.assertEqual(409, moved.status_code, moved.text)
            self.assertEqual(DOC, self.stored(sid)["telephone_doc"])
            with self.assertRaises(HTTPException) as caught:
                client.app.state.bind_telephone_doc(sid, OTHER_DOC)
            self.assertEqual(409, caught.exception.status_code)
            self.assertEqual(DOC, self.stored(sid)["telephone_doc"])
            # A second operator session pairs on its own; the first one's binding is not shared.
            sid2, headers2, _second = self.session(client, "create-2")
            self.assertEqual(403, client.get("/v1/session/%s/telephone" % sid2, params={"doc": DOC},
                                             headers=headers2).status_code)
            self.assertEqual(200, self.pair(client, sid2, headers2, {"code": "ZZZZZZZZZZZZZZZZ"}).status_code)
            self.assertEqual(OTHER_DOC, self.stored(sid2)["telephone_doc"])

    def test_tries_are_bounded_per_session(self):
        with TestClient(self.app(upstream=PairingUpstream(codes={}))) as client:
            sid, headers, _created = self.session(client)
            for _ in range(5):
                self.assertEqual(403, self.pair(client, sid, headers).status_code)
            self.assertEqual(429, self.pair(client, sid, headers).status_code)
            self.assertEqual(5, len(self.upstream.posts))
            sid2, headers2, _second = self.session(client, "create-2")
            self.assertEqual(403, self.pair(client, sid2, headers2).status_code)   # its own count


if __name__ == "__main__":
    unittest.main()

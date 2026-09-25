"""Client workspaces, behind STUDIO_CLIENT_WORKSPACES - held to the Gate 1
security contract (docs/SFDC24-CODEX-STRATEGY-EXECUTION-PLAN-20260925.md).

A registered client signs in with an email code and gets a client token of its
own format and verification path. Every client-scope request is re-authorised
against the registry, so removing a client shuts them out at once, and every
refusal reads the same. A project page is fetched only from the registry's
exact https URL, resolved here, with every answer checked and the connection
pinned to the checked address; it becomes an inert, bounded, text-only tree.
Each change in a workspace session is audited without page content, and no
token, address, URL or page text reaches a log or an error.
Addresses here are example.com fixtures only.
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import re
import logging
import os
import threading
import time
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, FakeTalk, IDs, MemoryStore, settings,
)
from app.auth import AuthService  # noqa: E402
from app.clients import ClientRegistry, REGISTRY, project_url_ok  # noqa: E402
from app.main import FETCH_ATTEMPTS_PER_HOUR, SERVER_ERROR, WORKSPACE_DENIED, create_app  # noqa: E402
import app.project_page as project_page  # noqa: E402
from app.project_page import (  # noqa: E402
    MAX_DEPTH, MAX_IMAGES, MAX_NODES, FetchBusy, PageFetchError, address_ok, fetch_page, page_to_tree, redact,
    tree_depth, tree_nodes,
)
from app.tokens import (  # noqa: E402
    CLIENT_AUDIENCE, InvalidToken, _b64e, _client_signature, mint_client_token, mint_token,
    verify_client_token, verify_token,
)

ORIGIN = {"Origin": "https://www.sfdc24.com"}
OPERATOR = "operator@example.com"
CLIENT_EMAIL = "client@example.com"
OTHER_EMAIL = "other.client@example.com"
BROWSER = "browser-instance-1234567890"
SECRET = "test-secret-that-is-long-enough-for-tests"
PAGE = "https://www.steel.example.com/"
PUBLIC_IP = "93.184.216.34"


def project(pid="steelworks", url=PAGE, name="steel.example.com"):
    return {"id": pid, "name": name, "url": url}


def client_entry(tenant="nav", emails=(CLIENT_EMAIL,), projects=None, name="Nav"):
    return {"id": tenant, "name": name, "emails": list(emails),
            "projects": [project()] if projects is None else projects}


def registry_record(*clients, **overrides):
    entries = list(clients) or [client_entry()]
    if overrides:
        entries[0] = dict(entries[0], **overrides)
    return {"version": 1, "clients": entries}


FIXTURE = """<!doctype html><html><head><title>Steel</title>
<script src="https://cdn.tracker.example/t.js"></script><script>window.stolen = document.cookie</script>
<style>body { background: url(https://evil.example/x.png) }</style></head><body>
<nav aria-label="Main"><a href="/">Home</a><a href="/work">Our work</a><a href="https://other.example/">Partner</a></nav>
<header><h1>Steel Works</h1><p>Custom steel &amp; fabrication.</p>
<a class="btn" href="https://www.steel.example.com/quote">Get a quote</a></header>
<section aria-label="Services"><h2>Services</h2><ul><li>Welding</li><li>Railings</li></ul>
<img src="https://cdn.example/shop.jpg" alt="Shop floor"><img src="/pixel.gif" width="1" height="1"></section>
<form action="https://collector.example/post"><input type="email" placeholder="Your email">
<input type="hidden" name="token" value="secret-token"><textarea name="message">prefilled</textarea>
<button type="submit">Send</button></form>
<script>document.body.dataset.stolen = document.cookie</script>
<iframe src="https://ads.example/frame"><p>frame text</p></iframe><svg><text>svg text</text></svg>
</body></html>"""


class FakeFetcher:
    def __init__(self, html=FIXTURE, fail=False):
        self.html, self.fail, self.calls = html, fail, []

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise PageFetchError("down")
        return self.html


class Resolver:
    """Answers in order, one list per call; the last list repeats."""

    def __init__(self, *answers):
        self.answers, self.calls = [list(a) for a in answers], []

    def __call__(self, host):
        self.calls.append(host)
        return self.answers[min(len(self.calls), len(self.answers)) - 1]


def page_transport(body=b"<h1>ok</h1>", status=200, headers=None, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        # A streamed body, as a real connection delivers it.
        return httpx.Response(status, headers={"content-type": "text/html; charset=utf-8", **(headers or {})},
                              stream=httpx.ByteStream(body))
    return httpx.MockTransport(handler)


class PatchWorker(CountingWorker):
    """Relabels the first node of the page on every utterance."""

    def on_turn(self, state, trigger):
        self.calls += 1
        if trigger.get("kind") != "utterance":
            return {"events": [], "problems": []}
        if "refuse" in trigger.get("text", ""):
            return {"events": [], "problems": ["op 1 changes 'x'; the whole patch is dropped"]}
        first = (state["artifact"].get("children") or [state["artifact"]])[0]
        return {"events": [
            {"type": "artifact.patch", "payload": {"ops": [
                {"op": "set_label", "node_id": first["id"], "value": "Steel Works, bigger"}]}},
            {"type": "confirm", "payload": {"text": "Made it bigger.", "artifact_ids": [first["id"]]}},
        ], "problems": []}


# -- the registry ------------------------------------------------------------------------
class Registry(unittest.TestCase):
    def test_a_client_is_found_by_tenant_and_the_hash_of_their_exact_address(self):
        store = MemoryStore()
        store.save(REGISTRY, registry_record(), None)
        reg = ClientRegistry(store)
        h = lambda e: hashlib.sha256(e.encode()).hexdigest()  # noqa: E731
        self.assertEqual({CLIENT_EMAIL}, set(reg.emails()))
        self.assertEqual("nav", reg.member("nav", h(CLIENT_EMAIL), h)["id"])
        self.assertIsNone(reg.member("nav", h("Client@Example.com"), h))
        self.assertIsNone(reg.member("other", h(CLIENT_EMAIL), h))
        self.assertIsNone(reg.member("", h(CLIENT_EMAIL), h))
        self.assertIsNone(reg.member("nav", "", h))

    def test_a_malformed_entry_is_skipped_whole(self):
        for bad in ({"emails": ["Client@Example.com"]}, {"id": "Nav Tenant"}, {"id": ""},
                    {"projects": [project(url="http://steel.example.com/")]},
                    {"projects": [project(pid="Bad Id")]}, {"name": "<b>Nav</b>"}):
            store = MemoryStore()
            store.save(REGISTRY, registry_record(**bad), None)
            self.assertEqual(frozenset(), ClientRegistry(store).emails(), bad)
        store = MemoryStore()
        store.save(REGISTRY, {"version": 1, "clients": [{"name": "Nav", "emails": [CLIENT_EMAIL], "projects": []}]}, None)
        self.assertEqual(frozenset(), ClientRegistry(store).emails())          # no tenant id

    def test_a_repeated_address_or_tenant_admits_neither(self):
        for second in (client_entry("other", emails=(CLIENT_EMAIL, "b@example.com")),
                       client_entry("nav", emails=("b@example.com",))):
            store = MemoryStore()
            store.save(REGISTRY, registry_record(client_entry(), second), None)
            self.assertNotIn(CLIENT_EMAIL, ClientRegistry(store).emails())

    def test_project_urls_are_a_reviewed_canonical_https_page(self):
        self.assertTrue(project_url_ok(PAGE))
        self.assertTrue(project_url_ok("https://www.steel.example.com/services/steel"))
        for bad in ("http://steel.example.com/", "https://127.0.0.1/", "https://[::1]/", "https://localhost/",
                    "https://steel.example.com:443/", "https://steel.example.com:8443/", "ftp://steel.example.com/",
                    "https:///x", "https://user:pw@steel.example.com/", "https://steel.example.com/#top",
                    "https://steel.example.com/?a=1", "https://st%65el.example.com/", "https://steel.example.com./",
                    "https://STEEL.example.com/", "https://stéel.example.com/", "https://steel.example.com\\@evil.example/",
                    "https://evil.example\\.steel.example.com/", " https://steel.example.com/", "https://2130706433/",
                    "https://0x7f.0.0.1/", "javascript:alert(1)", "https://metadata.google.internal/",
                    "https://localhost.localdomain/", "https://metadata.example.com/", 7, None):
            self.assertFalse(project_url_ok(bad), bad)


# -- client tokens -------------------------------------------------------------------------
SUB = "a" * 64


def forged(claims: dict) -> str:
    payload = _b64e(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return "c2." + payload + "." + _client_signature(SECRET, payload)


def live_client_token(subject, tenant, projects):
    now = int(time.time())
    return mint_client_token(subject, tenant, projects, now, now + 3600, SECRET)


def claims(**overrides):
    base = {"v": 2, "typ": "client", "sub": SUB, "tnt": "nav", "prj": ["steelworks"], "iat": 1000,
            "exp": 2000, "aud": CLIENT_AUDIENCE, "jti": "0" * 32}
    base.update(overrides)
    return base


class ClientTokens(unittest.TestCase):
    def test_the_claims_are_exact(self):
        token = mint_client_token(SUB, "nav", ["steelworks"], 1000, 2000, SECRET)
        got = verify_client_token(token, SECRET, now=1500)
        self.assertEqual({"v", "typ", "sub", "tnt", "prj", "iat", "exp", "aud", "jti"}, set(got))
        self.assertEqual((2, "client", SUB, "nav", ["steelworks"], 1000, 2000, CLIENT_AUDIENCE),
                         (got["v"], got["typ"], got["sub"], got["tnt"], got["prj"], got["iat"], got["exp"], got["aud"]))
        self.assertRegex(got["jti"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(got["jti"], verify_client_token(
            mint_client_token(SUB, "nav", ["steelworks"], 1000, 2000, SECRET), SECRET, now=1500)["jti"])

    def test_client_and_v1_tokens_never_pass_for_each_other(self):
        client = mint_client_token(SUB, "nav", [], 1000, 2000, SECRET)
        for scope in ("operator", "visitor", "session", "client", None):
            with self.assertRaises(InvalidToken):
                verify_token(client, SECRET, now=1500, scope=scope)
            with self.assertRaises(InvalidToken):
                verify_client_token(mint_token(SUB, 2000, SECRET, scope=scope or "client"), SECRET, now=1500)
        # The same claims signed with the v1 key are not a client token either.
        payload = _b64e(json.dumps(claims(), separators=(",", ":"), sort_keys=True).encode())
        v1_sig = _b64e(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest())
        with self.assertRaises(InvalidToken):
            verify_client_token("c2." + payload + "." + v1_sig, SECRET, now=1500)

    def test_every_claim_is_checked(self):
        self.assertEqual("nav", verify_client_token(forged(claims()), SECRET, now=1500)["tnt"])
        bad = [claims(aud="sfdc24-studio-operator"), claims(typ="operator"), claims(v=1), claims(sub=""),
               claims(sub="x" * 64), claims(tnt=""), claims(tnt="Other Tenant"), claims(prj="steelworks"),
               claims(prj=["Bad Id"]), claims(jti="short"), claims(iat="1000"), claims(exp=1000),
               claims(exp=1000 + 86401), dict(claims(), extra=1)]
        missing = claims()
        del missing["aud"]
        for c in bad + [missing]:
            with self.assertRaises(InvalidToken, msg=c):
                verify_client_token(forged(c), SECRET, now=1500)
        with self.assertRaises(InvalidToken):
            verify_client_token(forged(claims()), SECRET, now=2000)             # expired
        with self.assertRaises(InvalidToken):
            verify_client_token(forged(claims(iat=1500, exp=3000)), SECRET, now=1000)   # issued in the future
        token = forged(claims())
        with self.assertRaises(InvalidToken):
            verify_client_token(token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1], SECRET, now=1500)
        with self.assertRaises(InvalidToken):
            verify_client_token(token, "another-secret-that-is-long-enough", now=1500)

    def test_a_token_without_a_subject_or_tenant_cannot_be_minted(self):
        for args in (("", "nav"), (SUB, ""), ("A" * 64, "nav"), (SUB, "Nav")):
            with self.assertRaises(ValueError):
                mint_client_token(args[0], args[1], [], 1000, 2000, SECRET)
        with self.assertRaises(ValueError):
            mint_client_token(SUB, "nav", [], 1000, 1000 + 86401, SECRET)


class Auth(unittest.TestCase):
    def service(self, clients_on=True):
        store = MemoryStore()
        store.save(REGISTRY, registry_record(), None)
        sent = []
        reg = ClientRegistry(store)
        auth = AuthService(store, [OPERATOR], "s" * 40, lambda e, c: sent.append((e, c)),
                           otp_generator=lambda: "123456", dispatch=lambda work: work(),
                           wait_until=lambda deadline: None,
                           client_emails=reg.emails if clients_on else None)
        return auth, sent

    def test_a_registered_client_gets_a_code_and_verifies_as_a_client(self):
        auth, sent = self.service()
        challenge = auth.start(CLIENT_EMAIL, BROWSER)["challenge_id"]
        self.assertEqual([CLIENT_EMAIL], [e for e, _ in sent])
        out = auth.verify(challenge, CLIENT_EMAIL, "123456", BROWSER)
        self.assertEqual((True, "client"), (out["verified"], out["role"]))

    def test_switched_off_the_registry_admits_nobody(self):
        auth, sent = self.service(clients_on=False)
        auth.start(CLIENT_EMAIL, BROWSER)
        self.assertEqual([], sent)


# -- the API ---------------------------------------------------------------------------------
class Api(unittest.TestCase):
    def make(self, fetcher=None, registry=None, worker=None, talk=None, advisor=None, clock=None, **overrides):
        values = dict(client_workspaces=True)
        values.update(overrides)
        self.store = MemoryStore()
        self.store.save(REGISTRY, registry or registry_record(), None)
        self.email_sender = EmailSender()
        self.fetcher = fetcher or FakeFetcher()
        self.worker = worker or CountingWorker()
        self.talk = talk or FakeTalk()
        self.app = create_app(settings=settings(**values), store=self.store, worker=self.worker,
                              id_factory=IDs(), email_sender=self.email_sender, project_fetcher=self.fetcher,
                              talk_client=self.talk, advisor=advisor, **({"clock": clock} if clock else {}))
        return self.app

    def sign_in(self, client, email=CLIENT_EMAIL):
        started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": email, "client_key": BROWSER})
        code = self.email_sender.calls[-1][1]
        return client.post("/v1/auth/verify", headers=ORIGIN, json={
            "challenge_id": started.json()["challenge_id"], "email": email, "code": code,
            "client_key": BROWSER})

    def auth(self, token):
        return {**ORIGIN, "Authorization": "Bearer " + token}

    def project_session(self, client, token, creation_id="proj-1", project_id="steelworks"):
        return client.post("/v1/session", headers=self.auth(token), json={
            "creation_id": creation_id, "start": "project", "project": project_id})

    def state(self, sid):
        return self.store.data["studio_session_" + sid]

    def set_registry(self, record):
        _, token = self.store.load(REGISTRY)
        self.store.save(REGISTRY, record, token)

    def subject(self, email=CLIENT_EMAIL):
        return self.app.state.auth_service._subject_hash(email)


class SignIn(Api):
    def test_a_client_signs_in_with_a_client_token_naming_tenant_and_projects(self):
        with TestClient(self.make()) as client:
            verified = self.sign_in(client)
        self.assertEqual(200, verified.status_code, verified.text)
        body = verified.json()
        self.assertEqual({"token", "expires_at", "scope"}, set(body))
        self.assertEqual("client", body["scope"])
        got = verify_client_token(body["token"], SECRET)
        self.assertEqual(("nav", ["steelworks"], self.subject(), body["expires_at"]),
                         (got["tnt"], got["prj"], got["sub"], got["exp"]))
        self.assertNotIn(CLIENT_EMAIL, json.dumps(body))

    def test_a_client_token_opens_nothing_operator_only(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            leads = client.get("/v1/leads", headers=self.auth(token))
            created = client.post("/v1/session", headers=self.auth(token),
                                  json={"creation_id": "blank-1", "start": "blank"}).json()
        self.assertEqual(401, leads.status_code)
        state = self.state(created["session_id"])
        self.assertEqual(("", self.subject(), "nav", ""),
                         (state["operator_subject"], state["client_subject"], state["client_tenant"],
                          state["visitor_subject"]))

    def test_a_client_taken_out_of_the_registry_between_code_and_verify_is_not_signed_in(self):
        with TestClient(self.make()) as client:
            started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": CLIENT_EMAIL, "client_key": BROWSER})
            code = self.email_sender.calls[-1][1]
            self.set_registry({"version": 1, "clients": []})
            out = client.post("/v1/auth/verify", headers=ORIGIN, json={
                "challenge_id": started.json()["challenge_id"], "email": CLIENT_EMAIL, "code": code,
                "client_key": BROWSER})
        self.assertEqual(401, out.status_code)

    def test_an_empty_subject_never_opens_a_session(self):
        with TestClient(self.make()) as client:
            for token in (mint_token("", 10 ** 11, SECRET, scope="operator"),):
                out = client.post("/v1/session", headers=self.auth(token), json={"creation_id": "e-1", "start": "blank"})
                self.assertEqual(401, out.status_code)


class Denials(Api):
    def test_the_workspace_is_the_clients_name_and_projects_only(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            mine = client.get("/v1/workspace", headers=self.auth(token))
        self.assertEqual(200, mine.status_code, mine.text)
        self.assertEqual({"name": "Nav", "projects": [project()]}, mine.json())
        self.assertNotIn(CLIENT_EMAIL, mine.text)

    def test_every_refusal_reads_the_same(self):
        with TestClient(self.make()) as client:
            operator = self.sign_in(client, OPERATOR).json()["token"]
            subject = self.subject()
            tokens = {
                "operator": operator,
                "visitor": mint_token(subject, 10 ** 11, SECRET, scope="visitor"),
                "v1 client scope, own subject": mint_token(subject, 10 ** 11, SECRET, scope="client"),
                "session": mint_token(subject, 10 ** 11, SECRET),
                "other tenant": live_client_token(subject, "other", ["steelworks"]),
                "not in the registry": live_client_token("b" * 64, "nav", ["steelworks"]),
                "garbage": "c2.not.a-token",
            }
            answers = {name: client.get("/v1/workspace", headers=self.auth(t)) for name, t in tokens.items()}
            stranger = client.get("/v1/workspace", headers=ORIGIN).status_code
            elsewhere = client.get("/v1/workspace", headers={"Origin": "https://evil.example"}).status_code
        for name, out in answers.items():
            self.assertEqual((403, WORKSPACE_DENIED), (out.status_code, out.json()["detail"]), name)
        self.assertEqual((401, 403), (stranger, elsewhere))

    def test_switched_off_there_is_no_workspace_and_no_client_session(self):
        with TestClient(self.make(client_workspaces=False)) as client:
            token = live_client_token("a" * 64, "nav", ["steelworks"])
            self.assertEqual(503, client.get("/v1/workspace", headers=self.auth(token)).status_code)
            out = client.post("/v1/session", headers=self.auth(token), json={"creation_id": "c1", "start": "blank"})
            self.assertEqual(401, out.status_code)
            self.assertFalse(client.get("/health").json()["features"]["workspaces"])

    def test_cross_tenant_and_unknown_projects_get_the_same_denial_and_nothing_is_fetched(self):
        other = client_entry("acme", emails=(OTHER_EMAIL,), projects=[project("acme-site", "https://www.acme.example.com/")])
        with TestClient(self.make(registry=registry_record(client_entry(), other))) as client:
            mine = self.sign_in(client).json()["token"]
            theirs = self.sign_in(client, OTHER_EMAIL).json()["token"]
            answers = [self.project_session(client, mine, "x-1", "acme-site"),       # exists, another tenant
                       self.project_session(client, mine, "x-2", "no-such-project"),  # does not exist
                       self.project_session(client, theirs, "x-3", "steelworks"),     # exists, another tenant
                       self.project_session(client, mine, "x-4", ["steelworks"])]
            operator = self.sign_in(client, OPERATOR).json()["token"]
            answers.append(self.project_session(client, operator, "x-5", "steelworks"))
        for out in answers:
            self.assertEqual((403, WORKSPACE_DENIED), (out.status_code, out.json()["detail"]))
        self.assertEqual([], self.fetcher.calls)

    def test_a_project_added_after_sign_in_needs_a_new_sign_in(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            self.set_registry(registry_record(client_entry(projects=[project(), project("second", "https://www.second.example.com/")])))
            out = self.project_session(client, token, "late-1", "second")
        self.assertEqual((403, WORKSPACE_DENIED), (out.status_code, out.json()["detail"]))


class Revocation(Api):
    def test_a_removed_client_is_refused_on_every_client_scope_request_at_once(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token).json()
            session = self.auth(live["token"])
            self.assertEqual(200, client.get("/v1/session/%s/events?once=true" % live["session_id"],
                                             headers=session).status_code)
            self.set_registry({"version": 1, "clients": []})          # removed; no cache to wait out
            answers = [
                client.get("/v1/workspace", headers=self.auth(token)),
                self.project_session(client, token, "after-1"),
                client.post("/v1/session", headers=self.auth(token), json={"creation_id": "after-2", "start": "blank"}),
                client.get("/v1/session/%s/events?once=true" % live["session_id"], headers=session),
                client.post("/v1/session/%s/commands" % live["session_id"], headers=session, json={
                    "command_id": "c-after", "session_id": live["session_id"], "type": "utterance",
                    "expected_version": 1, "transcript": "make it bigger", "item_id": "i-after"}),
                client.post("/v1/session/%s/rating" % live["session_id"], headers=session, json={"score": 5}),
            ]
        for out in answers:
            self.assertEqual((403, WORKSPACE_DENIED), (out.status_code, out.json()["detail"]), out.request.url)

    def test_removing_only_the_address_is_enough(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            self.set_registry(registry_record(client_entry(emails=("replacement@example.com",))))
            out = client.get("/v1/workspace", headers=self.auth(token))
        self.assertEqual((403, WORKSPACE_DENIED), (out.status_code, out.json()["detail"]))

    def test_a_registry_that_cannot_be_read_admits_nobody(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            self.store.data[REGISTRY] = "not a registry"
            out = client.get("/v1/workspace", headers=self.auth(token))
        self.assertEqual(403, out.status_code)


class ProjectSessions(Api):
    def test_a_project_opens_as_a_text_only_tree_of_known_kinds(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            out = self.project_session(client, token)
        self.assertEqual(200, out.status_code, out.text)
        self.assertEqual([PAGE], self.fetcher.calls)
        state = self.state(out.json()["session_id"])
        self.assertEqual(("steelworks", "nav"), (state["project"], state["client_tenant"]))
        tree = state["artifact"]
        flat = []

        def walk(node):
            flat.append((node["kind"], node["label"]))
            self.assertLessEqual(set(node), {"id", "kind", "label", "detail", "children"})
            for child in node.get("children") or []:
                walk(child)
        walk(tree)
        self.assertLessEqual({k for k, _ in flat}, {"screen", "section", "nav", "heading", "text", "button",
                                                   "image-placeholder", "list", "form", "field"})
        labels = [label for _, label in flat]
        for expected in ("Home", "Our work", "Steel Works", "Custom steel & fabrication.", "Get a quote",
                         "Services", "Welding", "Railings", "Shop floor", "Your email", "Send"):
            self.assertIn(expected, labels)
        dumped = json.dumps(tree)
        for never in ("http", "cdn", "script", "cookie", "evil", "collector", "secret-token", "prefilled",
                      "frame text", "svg text", "<", ">"):
            self.assertNotIn(never, dumped)
        self.assertEqual(1, state["artifact_version"])
        snapshot = [e for e in state["events"] if e["type"] == "artifact.snapshot"][0]
        self.assertEqual(tree, snapshot["payload"]["root"])

    def test_a_page_that_does_not_load_costs_no_admission(self):
        with TestClient(self.make(fetcher=FakeFetcher(fail=True))) as client:
            token = self.sign_in(client).json()["token"]
            failed = self.project_session(client, token)
            blank = client.post("/v1/session", headers=self.auth(token),
                                json={"creation_id": "after-1", "start": "blank"}).json()
        self.assertEqual((502, "the project page could not be loaded"),
                         (failed.status_code, failed.json()["detail"]))
        self.assertEqual(1, blank["daily_admission_number"])
        self.assertFalse(any(k.startswith("studio_session_") and self.store.data[k].get("project")
                             for k in self.store.data))

    def test_a_replay_is_the_same_session_and_fetches_once(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            first = self.project_session(client, token).json()
            again = self.project_session(client, token).json()
        self.assertEqual(first["session_id"], again["session_id"])
        self.assertEqual([PAGE], self.fetcher.calls)

    def test_a_workspace_session_is_keyed_by_tenant_and_project(self):
        second = project("second", "https://www.second.example.com/", "second.example.com")
        with TestClient(self.make(registry=registry_record(client_entry(projects=[project(), second])))) as client:
            token = self.sign_in(client).json()["token"]
            one = self.project_session(client, token, "same-id", "steelworks").json()
            two = self.project_session(client, token, "same-id", "second").json()
        self.assertNotEqual(one["session_id"], two["session_id"])
        self.assertEqual(("steelworks", "second"), (self.state(one["session_id"])["project"],
                                                    self.state(two["session_id"])["project"]))

    def test_project_is_only_for_a_project_start(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            out = client.post("/v1/session", headers=self.auth(token), json={
                "creation_id": "b-1", "start": "blank", "project": "steelworks"})
        self.assertEqual(400, out.status_code)

    def test_the_summary_reaches_the_client(self):
        sent = []
        self.store = MemoryStore()
        self.store.save(REGISTRY, registry_record(), None)
        self.email_sender = EmailSender()
        self.app = create_app(settings=settings(client_workspaces=True, summary_email_enabled=True), store=self.store,
                              worker=CountingWorker(), id_factory=IDs(), email_sender=self.email_sender,
                              project_fetcher=FakeFetcher(), summary_sender=lambda email, pdf: sent.append(email))
        with TestClient(self.app) as client:
            token = self.sign_in(client).json()["token"]
            created = client.post("/v1/session", headers=self.auth(token),
                                  json={"creation_id": "sum-1", "start": "blank"}).json()
            for name in [n for n in self.store.data if n.startswith("studio_contact_")]:
                del self.store.data[name]                    # signed in before summaries were on
            out = client.post("/v1/session/%s/summary" % created["session_id"],
                              headers=self.auth(created["token"]), json={})
        self.assertEqual(200, out.status_code, out.text)
        self.assertEqual([CLIENT_EMAIL], sent)
        self.assertNotIn(CLIENT_EMAIL, out.text)


class Audit(Api):
    def utter(self, client, live, n, text):
        return client.post("/v1/session/%s/commands" % live["session_id"], headers=self.auth(live["token"]), json={
            "command_id": "cmd-%d" % n, "session_id": live["session_id"], "type": "utterance",
            "expected_version": self.state(live["session_id"])["artifact_version"],
            "transcript": text, "item_id": "item-%d" % n})

    def test_every_change_in_a_workspace_session_is_audited_without_page_content(self):
        with TestClient(self.make(worker=PatchWorker())) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token).json()
            applied = self.utter(client, live, 1, "make the heading bigger")
            refused = self.utter(client, live, 2, "please refuse this one")
        self.assertEqual((200, 200), (applied.status_code, refused.status_code), applied.text)
        audit = self.state(live["session_id"])["audit"]
        self.assertEqual(2, len(audit))
        first, second = audit
        self.assertEqual({"actor", "token_type", "tenant", "project", "command_id", "command_type", "prior_revision",
                          "revision", "op_ids", "at", "outcome"}, set(first))
        patch_op = [e["op_id"] for e in applied.json()["events"]]
        self.assertEqual(("client:" + self.subject()[:16], "session", "nav", "steelworks", "cmd-1", "utterance",
                          1, 2, patch_op, "applied"),
                         (first["actor"], first["token_type"], first["tenant"], first["project"], first["command_id"],
                          first["command_type"], first["prior_revision"], first["revision"], first["op_ids"],
                          first["outcome"]))
        self.assertEqual((2, 2, [e["op_id"] for e in refused.json()["events"]], "refused"),
                         (second["prior_revision"], second["revision"], second["op_ids"], second["outcome"]))
        dumped = json.dumps(audit)
        for never in ("Steel Works", "bigger", "refuse this", CLIENT_EMAIL):
            self.assertNotIn(never, dumped)

    def test_operator_sessions_are_not_touched(self):
        with TestClient(self.make(worker=PatchWorker())) as client:
            token = self.sign_in(client, OPERATOR).json()["token"]
            live = client.post("/v1/session", headers=self.auth(token), json={"creation_id": "op-1", "start": "blank"}).json()
            self.utter(client, live, 1, "make it bigger")
        self.assertNotIn("audit", self.state(live["session_id"]))


class CoreGuard(unittest.TestCase):
    def test_only_a_client_with_a_tenant_can_own_a_project_session(self):
        from tests.test_studio_controller import make_controller
        from app.core import CommandError
        controller, _, _ = make_controller()
        tree = {"id": "screen", "kind": "screen", "label": "x", "children": []}
        for args in (("x", "subject", "c-1", "project", False, None, False, "", False, tree, "p", "nav"),
                     ("x", "subject", "c-1", "project", False, None, False, "", True, tree, "p", ""),
                     ("x", "", "c-1", "project", False, None, False, "", True, tree, "p", "nav")):
            with self.assertRaises(CommandError) as caught:
                controller.create_session(*args)
            self.assertEqual(403, caught.exception.status)
        state, _ = controller.create_session("x", "subject", "c-2", "project", False, None, True, "", True, tree, "p", "nav")
        # A replay whose stored record says another tenant or project is refused, not returned.
        from app.state import StateConflict
        for field, value in (("client_tenant", "other"), ("project", "q")):
            blank, _ = controller.create_session("x", "subject", "r-" + field, "blank", False, None, False, "", True,
                                                 None, "", "nav")
            record = controller.repository.load(blank["session_id"])
            tampered = dict(record.state, **{field: value})
            controller.repository.save(blank["session_id"], tampered, record.token)
            with self.assertRaises(StateConflict, msg=field):
                controller.create_session("x", "subject", "r-" + field, "blank", False, None, False, "", True,
                                          None, "", "nav")
        self.assertEqual(("", "subject", "nav", "p", True), (state["operator_subject"], state["client_subject"],
                                                              state["client_tenant"], state["project"], state["analyst"]))


# -- the server-side fetch (SSRF) ----------------------------------------------------------------
BLOCKED = ["127.0.0.1", "127.8.9.1", "10.1.2.3", "172.16.5.4", "172.31.255.255", "192.168.1.1", "100.64.0.1",
           "100.127.255.254", "169.254.169.254", "0.0.0.0", "224.0.0.1", "239.255.255.250", "192.0.2.1",
           "198.51.100.7", "203.0.113.9", "198.18.0.1", "240.0.0.1", "255.255.255.255", "192.0.0.8",
           "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:8.8.8.8",
           "::ffff:169.254.169.254", "2001:db8::1", "64:ff9b::a00:1", "2002:a00:1::1", "2001::1", "fec0::1", "100::1"]
ALLOWED = ["93.184.216.34", "8.8.8.8", "185.199.108.153", "2606:4700:4700::1111", "2a00:1450:4001:80b::200e"]


class Fetch(unittest.TestCase):
    def test_every_non_global_address_is_refused(self):
        for text in BLOCKED:
            self.assertFalse(address_ok(ipaddress.ip_address(text)), text)
        for text in ALLOWED:
            self.assertTrue(address_ok(ipaddress.ip_address(text)), text)

    def test_each_layer_refuses_on_its_own(self):
        # The explicit block list and the standard-library checks overlap on
        # purpose. With the block list emptied, the rest still refuse.
        with mock.patch.object(project_page, "_BLOCKED", []):
            for text in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fc00::1",
                         "::ffff:8.8.8.8", "::ffff:127.0.0.1", "2002:808:808::1", "2001:0:4136:e378::1"):
                self.assertFalse(address_ok(ipaddress.ip_address(text)), text)
            self.assertTrue(address_ok(ipaddress.ip_address(PUBLIC_IP)))

    def test_a_proxy_from_the_environment_is_never_used(self):
        # httpx ignores environment proxies whenever a transport is supplied, so
        # the production client (no transport) is checked by how it is built.
        with mock.patch("httpx.Client", wraps=httpx.Client) as built:
            fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]), transport=page_transport())
        self.assertIs(False, built.call_args.kwargs["trust_env"])
        self.assertIs(False, built.call_args.kwargs["follow_redirects"])

    def test_a_name_resolving_to_any_blocked_address_is_never_connected_to(self):
        for text in BLOCKED + ["fe80::1%eth0", "not-an-address"]:
            seen = []
            with self.assertRaises(PageFetchError, msg=text):
                fetch_page(PAGE, resolver=Resolver([text]), transport=page_transport(seen=seen))
            self.assertEqual([], seen, text)

    def test_every_answer_is_checked_not_just_the_first(self):
        for answers in ([PUBLIC_IP, "10.0.0.5"], ["10.0.0.5", PUBLIC_IP], [PUBLIC_IP, "::1"], []):
            seen = []
            with self.assertRaises(PageFetchError, msg=answers):
                fetch_page(PAGE, resolver=Resolver(answers), transport=page_transport(seen=seen))
            self.assertEqual([], seen)

    def test_a_resolver_failure_is_a_refusal(self):
        def broken(host):
            raise OSError("no such host")
        with self.assertRaises(PageFetchError):
            fetch_page(PAGE, resolver=broken, transport=page_transport())

    def test_the_connection_is_pinned_to_the_checked_address_with_the_real_name_for_tls(self):
        # Rebinding: the name answers public first and private after. It is
        # resolved once, and the request goes to that address - nothing
        # re-resolves the name between the check and the connect.
        resolver = Resolver([PUBLIC_IP], ["127.0.0.1"])
        seen = []
        self.assertEqual("<h1>ok</h1>", fetch_page(PAGE, resolver=resolver, transport=page_transport(seen=seen)))
        self.assertEqual(["www.steel.example.com"], resolver.calls)
        (request,) = seen
        self.assertEqual((PUBLIC_IP, 443, "https", "/"), (request.url.host, request.url.port or 443,
                                                           request.url.scheme, request.url.path))
        self.assertEqual("www.steel.example.com", request.headers["host"])
        self.assertEqual("www.steel.example.com", request.extensions["sni_hostname"])
        timeout = request.extensions["timeout"]
        self.assertEqual((3.0, 4.0), (timeout["connect"], timeout["read"]))
        # A second fetch resolves again and meets the private answer: refused.
        with self.assertRaises(PageFetchError):
            fetch_page(PAGE, resolver=resolver, transport=page_transport(seen=seen))
        self.assertEqual(1, len(seen))

    def test_an_ipv6_answer_is_pinned_too(self):
        seen = []
        fetch_page(PAGE, resolver=Resolver(["2606:4700:4700::1111"]), transport=page_transport(seen=seen))
        self.assertEqual("[2606:4700:4700::1111]", request_netloc(seen[0]))
        self.assertEqual("www.steel.example.com", seen[0].extensions["sni_hostname"])

    def test_any_redirect_fails_closed(self):
        for location in ("https://www.steel.example.com/home", "/home", "https://evil.example/", "http://169.254.169.254/"):
            for status in (301, 302, 303, 307, 308):
                seen = []
                with self.assertRaises(PageFetchError):
                    fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]),
                               transport=page_transport(status=status, headers={"location": location}, seen=seen))
                self.assertEqual(1, len(seen))

    def test_only_text_html(self):
        for ctype in ("application/xhtml+xml", "text/plain", "application/json", "image/svg+xml", "", "text/htmlx"):
            with self.assertRaises(PageFetchError, msg=ctype):
                fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]),
                           transport=page_transport(headers={"content-type": ctype}))
        self.assertEqual("<p>é</p>", fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]), transport=page_transport(
            body="<p>é</p>".encode("latin-1"), headers={"content-type": "Text/HTML; charset=ISO-8859-1"})))
        for status in (404, 500, 204):
            with self.assertRaises(PageFetchError):
                fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]), transport=page_transport(status=status))

    def test_compressed_and_decompressed_sizes_are_capped(self):
        with self.assertRaises(PageFetchError):
            fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]), transport=page_transport(body=b"a" * 1_600_000))
        bomb = gzip.compress(b"<p>" + b"a" * 20_000_000)
        self.assertLess(len(bomb), 100_000)
        with self.assertRaises(PageFetchError):
            fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]),
                       transport=page_transport(body=bomb, headers={"content-encoding": "gzip"}))
        small = gzip.compress(b"<h1>zipped</h1>")
        self.assertEqual("<h1>zipped</h1>", fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]), transport=page_transport(
            body=small, headers={"content-encoding": "gzip"})))
        for coding in ("br", "zstd", "deflate, gzip", "compress"):
            with self.assertRaises(PageFetchError):
                fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]),
                           transport=page_transport(headers={"content-encoding": coding}))

    # -- Cursor NO-GO on b4110db, finding by finding ---------------------------------------
    def test_a_redirect_to_another_name_under_a_shared_suffix_is_never_followed(self):
        # (a) the old two-label "same site" rule treated co.uk, github.io and
        # s3.amazonaws.com as one site. There is no same-site rule now: any
        # redirect fails closed, and nothing is requested a second time.
        for page, target in (("https://www.client.co.uk/", "https://attacker.co.uk/"),
                             ("https://client.github.io/", "https://attacker.github.io/"),
                             ("https://bucket.s3.amazonaws.com/", "https://other.s3.amazonaws.com/"),
                             ("https://www.client.co.uk/", "https://www.client.co.uk/home")):
            seen = []
            with self.assertRaises(PageFetchError, msg=(page, target)):
                fetch_page(page, resolver=Resolver([PUBLIC_IP]),
                           transport=page_transport(status=301, headers={"location": target}, seen=seen))
            self.assertEqual(1, len(seen), (page, target))

    def test_one_wall_clock_total_covers_resolve_connect_first_byte_and_body(self):
        # (b) a slow resolver, a slow connection and a slow-drip body each stop
        # at the one total deadline, measured on the real clock.
        def slow_resolver(host):
            time.sleep(1.5)
            return [PUBLIC_IP]

        def slow_answer(request):
            time.sleep(1.5)
            return httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"<p>x</p>"))

        class Drip(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(20):
                    time.sleep(0.2)
                    yield b"<p>drip</p>"

        def drip(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, stream=Drip())
        cases = (dict(resolver=slow_resolver, transport=page_transport()),
                 dict(resolver=Resolver([PUBLIC_IP]), transport=httpx.MockTransport(slow_answer)),
                 dict(resolver=Resolver([PUBLIC_IP]), transport=httpx.MockTransport(drip)))
        with mock.patch.object(project_page, "TOTAL_SECONDS", 0.6):
            for case in cases:
                started = time.monotonic()
                with self.assertRaises(PageFetchError):
                    fetch_page(PAGE, **case)
                self.assertLess(time.monotonic() - started, 1.2)

    def test_a_registered_name_that_resolves_privately_is_never_connected_to(self):
        # (c) the belt: these names are refused before any lookup ...
        resolver = Resolver([PUBLIC_IP])
        for page in ("https://metadata.google.internal/", "https://localhost.localdomain/",
                     "https://printer.local/", "https://db.localhost/", "https://router.home.arpa/"):
            self.assertFalse(project_url_ok(page), page)
            with self.assertRaises(PageFetchError):
                fetch_page(page, resolver=resolver, transport=page_transport())
        self.assertEqual([], resolver.calls)
        # ... and the resolver check alone refuses them too, with no connection.
        answers = {"metadata.google.internal": ["169.254.169.254"], "localhost.localdomain": ["127.0.0.1"],
                   "x.nip.io": ["10.0.0.5"], "169.254.169.254.nip.io": ["169.254.169.254"]}
        with mock.patch.object(project_page, "project_url_ok", lambda url: True):
            for host, ips in answers.items():
                seen = []
                with self.assertRaises(PageFetchError, msg=host):
                    fetch_page("https://%s/" % host, resolver=lambda h, ips=ips: ips,
                               transport=page_transport(seen=seen))
                self.assertEqual([], seen, host)
        # A registered name that rebinds: public for the check, private after.
        rebinding, seen = Resolver([PUBLIC_IP], ["169.254.169.254"]), []
        fetch_page("https://x.nip.io/", resolver=rebinding, transport=page_transport(seen=seen))
        self.assertEqual([PUBLIC_IP], [r.url.host for r in seen])
        with self.assertRaises(PageFetchError):
            fetch_page("https://x.nip.io/", resolver=rebinding, transport=page_transport(seen=seen))
        self.assertEqual([PUBLIC_IP], [r.url.host for r in seen])

    def test_first_byte_and_total_time_are_capped(self):
        slow_headers = iter([0.0, 6.0])
        with self.assertRaises(PageFetchError):
            fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]), transport=page_transport(),
                       clock=lambda: next(slow_headers))
        ticks = iter([0.0, 1.0, 9.0, 9.0, 9.0])
        with self.assertRaises(PageFetchError):
            fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]), transport=page_transport(),
                       clock=lambda: next(ticks))

    def test_transport_errors_carry_no_address_or_name(self):
        def handler(request):
            raise httpx.ConnectTimeout("connect to https://www.steel.example.com/ at " + PUBLIC_IP + " timed out")
        with self.assertRaises(PageFetchError) as caught:
            fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]), transport=httpx.MockTransport(handler))
        self.assertEqual("ConnectTimeout", str(caught.exception))

    def test_nothing_but_a_registered_https_page_is_even_resolved(self):
        resolver = Resolver([PUBLIC_IP])
        for bad in ("http://steel.example.com/", "https://10.0.0.1/", "file:///etc/passwd",
                    "https://steel.example.com:443/", "https://steel.example.com/#x"):
            with self.assertRaises(PageFetchError):
                fetch_page(bad, resolver=resolver, transport=page_transport())
        self.assertEqual([], resolver.calls)


def request_netloc(request):
    return request.url.netloc.decode("ascii") if isinstance(request.url.netloc, bytes) else request.url.netloc


# -- HTML to tree ------------------------------------------------------------------------------------
MALICIOUS = [
    '<svg onload="alert(1)"><script>alert(2)</script><foreignObject><p>inside svg</p></foreignObject></svg>',
    '<img src=x onerror="alert(3)" alt="A photo">',
    '<a href="javascript:alert(4)">Click me</a><a href="data:text/html,x">Data link</a>',
    '<meta http-equiv="refresh" content="0;url=https://evil.example/"><base href="https://evil.example/">',
    '<iframe srcdoc="<script>alert(5)</script>"></iframe><object data="x.swf">object text</object><embed src="y">',
    '<style>@import url(https://evil.example/a.css); p{background:url(javascript:alert(6))}</style>',
    '<template><p>template text</p></template><noscript><p>noscript text</p></noscript>',
    '<math><mtext>math text</mtext></math><x-widget onclick="alert(7)" data-url="https://evil.example/">Widget words</x-widget>',
    '<p onmouseover="alert(8)">Visit https://evil.example/x or www.evil.example or javascript:alert(9) today</p>',
    '<form action="https://collector.example/"><input type="hidden" value="sekret"><input type="password" name="pw"></form>',
    '<![CDATA[<script>alert(12)</script>]]><p>After cdata</p>',
]


class Tree(unittest.TestCase):
    def labels(self, tree):
        out = []

        def walk(node):
            out.append(node.get("label", "") + " " + node.get("detail", ""))
            for child in node.get("children") or []:
                walk(child)
        walk(tree)
        return " | ".join(out)

    def test_malicious_html_and_svg_become_inert_text_only(self):
        for fixture in MALICIOUS:
            tree = page_to_tree("<body>" + fixture + "</body>", "T")
            text = self.labels(tree)
            for never in ("alert", "script", "javascript", "onerror", "onload", "onclick", "http", "www.",
                          "evil", "sekret", "srcdoc", "inside svg", "template text", "noscript text", "math text",
                          "object text", "data:", "<", ">"):
                self.assertNotIn(never, text.lower(), (fixture, text))
        self.assertIn("Click me", self.labels(page_to_tree("<body>" + MALICIOUS[2] + "</body>", "T")))
        self.assertIn("A photo", self.labels(page_to_tree("<body>" + MALICIOUS[1] + "</body>", "T")))
        self.assertIn("Visit", self.labels(page_to_tree("<body>" + MALICIOUS[8] + "</body>", "T")))
        # Markup written AS TEXT on the page stays text, without its brackets; a
        # comment is not page copy at all.
        escaped = page_to_tree("<body><p>&lt;script&gt;alert(10)&lt;/script&gt;</p>"
                               "<!-- <script>alert(11)</script> --></body>", "T")
        self.assertEqual("scriptalert(10)/script", escaped["children"][0]["children"][0]["label"])
        self.assertNotIn("alert(11)", json.dumps(escaped))

    def test_the_tree_is_bounded_in_nodes_depth_images_and_label_length(self):
        html = "<body>" + "".join("<section><ul><li><p><b>Deep %d</b></p></li></ul><img alt='pic %d'></section>" % (n, n)
                                  for n in range(200)) + "</body>"
        tree = page_to_tree(html, "Big")
        self.assertLessEqual(tree_nodes(tree), MAX_NODES)
        self.assertLessEqual(tree_depth(tree), MAX_DEPTH)
        self.assertLessEqual(self.labels(tree).count("pic "), MAX_IMAGES)
        long_tree = page_to_tree("<h1>" + "word " * 500 + "</h1><div>" * 2000 + "nested" + "</div>" * 2000, "T")
        self.assertLessEqual(max(len(n) for n in self.labels(long_tree).split(" | ")), 200 + 2)

    def test_the_image_and_depth_caps_hold_on_their_own(self):
        many = page_to_tree("<section>" + "".join("<img alt='pic %d'>" % n for n in range(30)) + "</section>", "T")
        self.assertEqual(MAX_IMAGES, self.labels(many).count("pic "))
        builder = project_page._Builder("T")
        node = builder.screen
        for level in range(8):                                # a tree deeper than any page makes
            child = {"id": "d%d" % level, "kind": "section", "label": "level %d" % level, "children": []}
            node["children"].append(child)
            node = child
        node["children"].append({"id": "leaf", "kind": "text", "label": "deep leaf"})
        self.assertLessEqual(tree_depth(builder.finish()), 4)          # the cap itself, not the constant

    def test_input_and_time_are_capped(self):
        tree = page_to_tree("<p>start here</p>" + " " * 2_100_000 + "<p>BEYONDTHECAP</p>", "T")
        self.assertNotIn("BEYONDTHECAP", json.dumps(tree))
        ticks = iter(range(0, 10 ** 6, 1))
        slow = page_to_tree("".join("<p>para %d</p>" % n for n in range(40)), "T", clock=lambda: next(ticks))
        self.assertLess(tree_nodes(slow), 10)                 # stopped at the 2-second budget

    def test_labels_are_one_plain_line(self):
        tree = page_to_tree("<p>one two​ three\u0085 &lt;b&gt;x</p>", "T")
        self.assertEqual("one two three bx", tree["children"][0]["children"][0]["label"])


# -- no token, address, URL or page text in logs or errors --------------------------------------------
class NoLeaks(Api):
    TOKEN = "TOKENMARKER123"
    EMAIL = "emailmarker@example.com"
    URL = "https://www.urlmarker.example.com/pathmarker"
    PAGE_TEXT = "PAGEMARKER-BODY"

    def test_markers_never_reach_logs_or_error_responses(self):
        good = project("good", self.URL, "good site")
        bad = project("bad", "https://www.urlmarker-down.example.com/pathmarker", "bad site")
        registry = registry_record(client_entry(emails=(self.EMAIL,), projects=[good, bad]))

        def transport_for(url):
            if "down" in url:
                return page_transport(body=("<p>" + self.PAGE_TEXT + "</p>").encode(), status=500)
            return page_transport(body=("<h1>" + self.PAGE_TEXT + "</h1>").encode())

        def fetcher(url):
            return fetch_page(url, resolver=Resolver([PUBLIC_IP]), transport=transport_for(url))
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        errors = []
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                with TestClient(self.make(fetcher=fetcher, registry=registry)) as client:
                    def keep(response):
                        if response.status_code >= 400:
                            errors.append(response.text)
                        return response
                    token = keep(self.sign_in(client, self.EMAIL)).json()["token"]
                    keep(self.sign_in(client, "stranger-" + self.EMAIL))
                    for bogus in ("c2." + self.TOKEN + ".x", self.TOKEN, "c2.%s.%s" % (self.TOKEN, self.TOKEN)):
                        keep(client.get("/v1/workspace", headers=self.auth(bogus)))
                        keep(client.post("/v1/session", headers=self.auth(bogus), json={"creation_id": "l-0", "start": "blank"}))
                        keep(client.get("/v1/session/s-x/events?once=true", headers=self.auth(bogus)))
                    keep(self.project_session(client, token, "l-1", "bad"))
                    ok = keep(self.project_session(client, token, "l-2", "good"))
                    keep(self.project_session(client, token, "l-3", "missing"))
                    self.set_registry({"version": 1, "clients": []})
                    keep(client.get("/v1/workspace", headers=self.auth(token)))
                    keep(client.get("/v1/session/%s/events?once=true" % ok.json()["session_id"],
                                    headers=self.auth(ok.json()["token"])))
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        self.assertEqual(200, ok.status_code)
        self.assertGreaterEqual(len(errors), 10)
        logs = captured.getvalue()
        for marker in (self.TOKEN, token, self.EMAIL, "emailmarker", "urlmarker", "pathmarker", PUBLIC_IP, self.PAGE_TEXT):
            self.assertNotIn(marker, logs, marker)
            for body in errors:
                self.assertNotIn(marker, body, (marker, body))


if __name__ == "__main__":
    unittest.main()


# =====================================================================================================
# Codex NO-GO on cf8f5ac (row CODEX-PR260-GATE1-NOGO-CF8F5AC-20260925T111206Z), and Gemini's attacks.
# =====================================================================================================
SESSION_ROUTES = (
    ("GET", "events?once=true", None), ("POST", "talk", {"text": "hello"}),
    ("POST", "speak", {"text": "hello", "voice": "architect"}), ("POST", "inspire", {}),
    ("POST", "recap", {}), ("POST", "analyze", {"text": "hello"}),
    ("POST", "commands", "UTTERANCE"), ("POST", "voice", {"sdp": "v=0"}),
    ("POST", "rating", {"score": 5}), ("POST", "summary", {}),
)


class Blocker1TenantAndProjectBinding(Api):
    def call(self, client, method, route, body, sid, token):
        url = "/v1/session/%s/%s" % (sid, route)
        if body == "UTTERANCE":
            body = {"command_id": "cmd-x", "session_id": sid, "type": "utterance", "expected_version": 1,
                    "transcript": "make it bigger", "item_id": "item-x"}
        return client.get(url, headers=self.auth(token)) if method == "GET" else \
            client.post(url, headers=self.auth(token), json=body)

    def test_an_address_moved_to_another_tenant_never_reaches_its_old_sessions(self):
        # Codex repro: the same verified address moved from tenant nav to tenant
        # other; the same blank creation id returned the old nav session.
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            old = {start: client.post("/v1/session", headers=self.auth(token),
                                      json={"creation_id": "same-" + start, "start": start}).json()
                   for start in ("blank", "template")}
            self.set_registry(registry_record(client_entry(emails=("someone@example.com",)),
                                              client_entry("other", emails=(CLIENT_EMAIL,))))
            moved = self.sign_in(client).json()["token"]
            self.assertEqual("other", verify_client_token(moved, SECRET)["tnt"])
            new = {start: client.post("/v1/session", headers=self.auth(moved),
                                      json={"creation_id": "same-" + start, "start": start}).json()
                   for start in ("blank", "template")}
            for start in ("blank", "template"):
                self.assertNotEqual(old[start]["session_id"], new[start]["session_id"], start)
                stale = client.get("/v1/session/%s/events?once=true" % old[start]["session_id"],
                                   headers=self.auth(old[start]["token"]))
                fresh = client.get("/v1/session/%s/events?once=true" % new[start]["session_id"],
                                   headers=self.auth(new[start]["token"]))
                self.assertEqual((403, WORKSPACE_DENIED), (stale.status_code, stale.json()["detail"]), start)
                self.assertEqual(200, fresh.status_code, start)
                self.assertEqual("other", self.state(new[start]["session_id"])["client_tenant"])

    def test_the_session_token_is_bound_to_subject_tenant_and_project(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token).json()
            got = verify_token(live["token"], SECRET, scope="session")
        self.assertEqual(("nav", self.subject(), "steelworks"), (got["tnt"], got["csub"], got["prj"]))

    def test_gemini_attack_2_every_disagreement_of_state_token_and_registry_is_one_denial_before_side_effects(self):
        # (i) stored = token = nav, but the registry now lists the project under
        # acme; (ii) the token says acme; (iii) the stored tenant says acme.
        acme = client_entry("acme", emails=(OTHER_EMAIL,), projects=[project()])
        for case in ("registry", "token", "state"):
            with TestClient(self.make(registry=registry_record(client_entry(), client_entry("acme", emails=(OTHER_EMAIL,), projects=[])))) as client:
                token = self.sign_in(client).json()["token"]
                live = self.project_session(client, token).json()
                sid, session_token = live["session_id"], live["token"]
                if case == "registry":
                    self.set_registry(registry_record(client_entry(projects=[]), acme))
                elif case == "token":
                    session_token = mint_token(sid, 10 ** 11, SECRET,
                                               binding={"tnt": "acme", "csub": self.subject(), "prj": "steelworks"})
                else:
                    record, stamp = self.store.load("studio_session_" + sid)
                    record["client_tenant"] = "acme"
                    self.store.save("studio_session_" + sid, record, stamp)
                before = json.dumps(self.state(sid), sort_keys=True)
                calls = (self.worker.calls, len(self.talk.calls))
                for method, route, body in SESSION_ROUTES:
                    out = self.call(client, method, route, body, sid, session_token)
                    self.assertEqual((403, WORKSPACE_DENIED), (out.status_code, out.json()["detail"]), (case, route))
                self.assertEqual(before, json.dumps(self.state(sid), sort_keys=True), case)
                self.assertEqual(calls, (self.worker.calls, len(self.talk.calls)), case)

    def test_a_token_never_crosses_between_client_and_other_sessions(self):
        with TestClient(self.make()) as client:
            client_token = self.sign_in(client).json()["token"]
            mine = self.project_session(client, client_token).json()
            operator = self.sign_in(client, OPERATOR).json()["token"]
            theirs = client.post("/v1/session", headers=self.auth(operator), json={"creation_id": "op-1", "start": "blank"}).json()
            unbound = mint_token(mine["session_id"], 10 ** 11, SECRET)            # a client session without binding
            bound_to_operator = mint_token(theirs["session_id"], 10 ** 11, SECRET,
                                           binding={"tnt": "nav", "csub": self.subject(), "prj": ""})
            wrong_project = mint_token(mine["session_id"], 10 ** 11, SECRET,
                                       binding={"tnt": "nav", "csub": self.subject(), "prj": ""})
            answers = [client.get("/v1/session/%s/events?once=true" % sid, headers=self.auth(t))
                       for sid, t in ((mine["session_id"], unbound), (theirs["session_id"], bound_to_operator),
                                      (mine["session_id"], wrong_project))]
            operator_ok = client.get("/v1/session/%s/events?once=true" % theirs["session_id"],
                                     headers=self.auth(theirs["token"]))
        for out in answers:
            self.assertEqual((403, WORKSPACE_DENIED), (out.status_code, out.json()["detail"]))
        self.assertEqual(200, operator_ok.status_code)


class Blocker2ProjectRevocation(Blocker1TenantAndProjectBinding):
    def test_removing_the_project_shuts_every_session_route_before_any_side_effect(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token).json()
            blank = client.post("/v1/session", headers=self.auth(token), json={"creation_id": "b-1", "start": "blank"}).json()
            self.set_registry(registry_record(client_entry(projects=[])))    # tenant and address stay
            sid = live["session_id"]
            before = json.dumps(self.state(sid), sort_keys=True)
            calls = (self.worker.calls, len(self.talk.calls))
            for method, route, body in SESSION_ROUTES:
                out = self.call(client, method, route, body, sid, live["token"])
                self.assertEqual((403, WORKSPACE_DENIED), (out.status_code, out.json()["detail"]), route)
            # A blank session of the same client needs only the tenant and address.
            still = client.get("/v1/session/%s/events?once=true" % blank["session_id"], headers=self.auth(blank["token"]))
        self.assertEqual(before, json.dumps(self.state(sid), sort_keys=True))
        self.assertEqual(calls, (self.worker.calls, len(self.talk.calls)))
        self.assertEqual(200, still.status_code)


class Blocker3ParseAndFetchBudgets(Api):
    def test_a_giant_tag_and_a_giant_text_node_stop_quickly_on_the_real_clock(self):
        for name, html, kept in (
                ("giant tag", "<p>kept before</p><a title='" + "x" * 1_990_000, "kept before"),
                ("unclosed script", "<p>kept before</p><script>" + "x" * 1_990_000, "kept before"),
                ("giant text", "<p>" + "word " * 399_000 + "</p>", "word word"),
                ("deep nesting", "<p>kept before</p>" + "<div><span>" * 180_000, "kept before"),
                ("many attributes", "<p>kept before</p><p " + " ".join("a%d=1" % n for n in range(150_000)) + ">", "kept before")):
            started = time.monotonic()
            tree = page_to_tree(html, "T")
            self.assertLess(time.monotonic() - started, 2.5, name)
            self.assertIn(kept, json.dumps(tree), name)
            self.assertLessEqual(max(len(label) for label in Tree.labels(Tree(), tree).split(" | ")), 204, name)

    def test_the_unparsed_remainder_and_the_event_count_are_hard_stops(self):
        with mock.patch.object(project_page, "MAX_PENDING", 1_000):
            # The tag spans feed chunks, so the parser holds it unparsed: past the cap, stop.
            tree = page_to_tree("<p>first</p><a title='" + "x" * 40_000 + "'>late</a><p>after the tag</p>", "T")
        self.assertIn("first", json.dumps(tree))
        self.assertNotIn("after the tag", json.dumps(tree))
        with mock.patch.object(project_page, "MAX_EVENTS", 10):
            tree = page_to_tree("".join("<p>para %d here</p>" % n for n in range(50)), "T")
        self.assertLess(tree_nodes(tree), 8)
        # With the clock frozen, only the event cap stops 120,000 empty tags.
        tree = page_to_tree("<b></b>" * 60_000 + "<p>tail text</p>", "T", clock=lambda: 0.0)
        self.assertNotIn("tail text", json.dumps(tree))

    def test_page_loads_per_tenant_are_budgeted_before_any_fetch_and_a_failure_admits_nothing(self):
        with TestClient(self.make(fetcher=FakeFetcher(fail=True))) as client:
            token = self.sign_in(client).json()["token"]
            answers = [self.project_session(client, token, "fail-%d" % n).status_code
                       for n in range(FETCH_ATTEMPTS_PER_HOUR + 3)]
            blank = client.post("/v1/session", headers=self.auth(token),
                                json={"creation_id": "after-1", "start": "blank"}).json()
        self.assertEqual([502] * FETCH_ATTEMPTS_PER_HOUR + [429] * 3, answers)
        self.assertEqual(FETCH_ATTEMPTS_PER_HOUR, len(self.fetcher.calls))
        self.assertEqual(1, blank["daily_admission_number"])
        attempts = self.store.data["studio_client_fetch_nav"]["attempts"]
        self.assertEqual({"at", "project"}, set(attempts[0]))                 # no URL, no page content

    def test_one_page_load_per_tenant_at_a_time(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            held = self.app.state.guards.acquire(tenant_record("nav"), "fetch", "holder", 60, 1)
            busy = self.project_session(client, token)
            self.app.state.guards.release(tenant_record("nav"), "fetch", "holder", held)
            ok = self.project_session(client, token, "after-busy")
        self.assertEqual(429, busy.status_code)
        self.assertEqual(200, ok.status_code)
        self.assertEqual([PAGE], self.fetcher.calls)

    def test_no_free_load_slot_is_a_503_that_admits_nothing(self):
        class Busy(FakeFetcher):
            def __call__(self, url):
                self.calls.append(url)
                raise FetchBusy("every page-load slot is taken")
        with TestClient(self.make(fetcher=Busy())) as client:
            token = self.sign_in(client).json()["token"]
            out = self.project_session(client, token)
            blank = client.post("/v1/session", headers=self.auth(token), json={"creation_id": "b-1", "start": "blank"}).json()
        self.assertEqual(503, out.status_code)
        self.assertEqual(1, blank["daily_admission_number"])


class Blocker4BoundedBlockedDns(unittest.TestCase):
    def test_blocked_resolvers_hold_at_most_the_slot_count_of_threads_and_the_rest_fail_fast(self):
        gate = threading.Event()

        def stuck(host):
            gate.wait(10)
            return [PUBLIC_IP]

        def alive():
            return [t for t in threading.enumerate() if t.name == "studio-project-fetch" and t.is_alive()]
        slots = threading.BoundedSemaphore(4)
        outcomes = []
        with mock.patch.object(project_page, "_SLOTS", slots), mock.patch.object(project_page, "TOTAL_SECONDS", 0.2):
            for _ in range(30):
                started = time.monotonic()
                try:
                    fetch_page(PAGE, resolver=stuck, transport=page_transport())
                except FetchBusy:
                    outcomes.append(("busy", time.monotonic() - started))
                except PageFetchError:
                    outcomes.append(("timeout", time.monotonic() - started))
            self.assertEqual(4, len(alive()))                       # 30 calls, 4 threads
            self.assertEqual(["timeout"] * 4 + ["busy"] * 26, [kind for kind, _ in outcomes])
            self.assertTrue(all(took < 0.1 for kind, took in outcomes if kind == "busy"))
            gate.set()                                            # the resolvers return ...
            deadline = time.monotonic() + 5
            while alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual([], alive())                         # ... the threads drain, the slots free
            self.assertEqual("<h1>ok</h1>", fetch_page(PAGE, resolver=Resolver([PUBLIC_IP]),
                                                       transport=page_transport()))


class Blocker5AuditAndRedaction(Api):
    def command(self, client, live, command_id, kind, **extra):
        body = {"command_id": command_id, "session_id": live["session_id"], "type": kind,
                "expected_version": self.state(live["session_id"])["artifact_version"]}
        body.update(extra)
        self.last_body = body
        return client.post("/v1/session/%s/commands" % live["session_id"], headers=self.auth(live["token"]), json=body)

    def replay(self, client, live):
        return client.post("/v1/session/%s/commands" % live["session_id"], headers=self.auth(live["token"]),
                           json=self.last_body)

    def test_every_accepted_command_is_audited_exactly_once_with_its_op_ids(self):
        with TestClient(self.make(worker=PatchWorker())) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token).json()
            first = self.command(client, live, "c-1", "utterance", transcript="bigger please", item_id="i-1")
            again = self.replay(client, live)
            paused = self.command(client, live, "c-2", "pause")
            resumed = self.command(client, live, "c-3", "resume")
            stopped = self.command(client, live, "c-4", "stop")
            stopped_again = self.replay(client, live)
        self.assertEqual(first.json(), again.json())
        self.assertEqual(stopped.json(), stopped_again.json())
        audit = self.state(live["session_id"])["audit"]
        self.assertEqual(["c-1", "c-2", "c-3", "c-4"], [a["command_id"] for a in audit])     # replays add nothing
        for entry, response, kind in ((audit[0], first, "utterance"), (audit[1], paused, "pause"),
                                      (audit[2], resumed, "resume"), (audit[3], stopped, "stop")):
            self.assertEqual((kind, "applied", [e["op_id"] for e in response.json()["events"]]),
                             (entry["command_type"], entry["outcome"], entry["op_ids"]))
            self.assertTrue(entry["op_ids"], kind)

    def test_a_worker_failure_answers_and_logs_no_words_and_is_audited(self):
        class Exploding(PatchWorker):
            def on_turn(self, state, trigger):
                raise RuntimeError("SECRETWORDS the client said: " + trigger.get("text", ""))
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                with TestClient(self.make(worker=Exploding()), raise_server_exceptions=False) as client:
                    token = self.sign_in(client).json()["token"]
                    live = self.project_session(client, token).json()
                    out = self.command(client, live, "c-1", "utterance", transcript="PRIVATEPLAN for my shop", item_id="i-1")
        finally:
            root.removeHandler(handler)
        self.assertEqual((500, {"detail": SERVER_ERROR}), (out.status_code, out.json()))
        for marker in ("SECRETWORDS", "PRIVATEPLAN"):
            self.assertNotIn(marker, out.text)
            self.assertNotIn(marker, captured.getvalue())
        self.assertIn("RuntimeError", captured.getvalue())               # the type is logged, nothing else
        audit = self.state(live["session_id"])["audit"]
        self.assertEqual([("c-1", "failed", [])], [(a["command_id"], a["outcome"], a["op_ids"]) for a in audit])

    def test_any_unexpected_failure_is_a_static_answer(self):
        with TestClient(self.make(), raise_server_exceptions=False) as client:
            token = self.sign_in(client).json()["token"]
            with mock.patch.object(self.app.state.clients, "member", side_effect=RuntimeError("SECRETWORDS")):
                out = client.get("/v1/workspace", headers=self.auth(token))
        self.assertEqual((500, {"detail": SERVER_ERROR}), (out.status_code, out.json()))


class FindingF1WorkspaceSnapshot(Api):
    def test_an_old_token_sees_only_the_projects_it_was_issued_for(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            self.set_registry(registry_record(client_entry(projects=[project(), project("second", "https://www.second.example.com/")])))
            old = client.get("/v1/workspace", headers=self.auth(token)).json()
            fresh = client.get("/v1/workspace", headers=self.auth(self.sign_in(client).json()["token"])).json()
        self.assertEqual(["steelworks"], [p["id"] for p in old["projects"]])
        self.assertEqual(["steelworks", "second"], [p["id"] for p in fresh["projects"]])


class FindingF2Redaction(unittest.TestCase):
    def test_links_domains_addresses_and_emails_become_placeholders(self):
        cases = {
            "Visit steelworkson.ca today": "Visit [link] today",
            "Mail info@steel-works.ca now": "Mail [email] now",
            "Admin at 10.0.0.1/admin here": "Admin at [link] here",
            "Also 2001:db8::1 and [fe80::1]:8080": "Also [link] and [link]",
            "See https://x.example/a or www.example.org": "See [link] or [link]",
            "Open 9am-5pm, 10:30 to 3.14 km, e.g. weekends": "Open 9am-5pm, 10:30 to 3.14 km, e.g. weekends",
        }
        for text, expected in cases.items():
            self.assertEqual(expected, " ".join(redact(text).split()), text)
        tree = page_to_tree("<h1>steel.example.com</h1><p>Call 203.0.113.9 or write sales@steel.example.com</p>",
                            "steel.example.com")
        self.assertEqual("steel.example.com", tree["label"])                 # the registry's name, as written
        labels = json.dumps(tree["children"])
        for never in ("steel.example.com", "203.0.113.9", "sales@"):
            self.assertNotIn(never, labels)

    def test_codex_blocker_1_any_plausible_host_or_unicode_address_is_redacted(self):
        # Codex Gate 1 NO-GO on a2d98fc: these all survived the old allowlist.
        cases = {
            "Our studio secret.photography opens": "Our studio [link] opens",
            "Read example.consulting/path now": "Read [link] now",
            "Books at bücher.de today": "Books at [link] today",
            "Punycode xn--bcher-kva.de too": "Punycode [link] too",
            "Mixed shop.xn--p1ai here": "Mixed [link] here",
            "Full-width bücher\u3002de and bücher\uff0ede": "Full-width [link] and [link]",
            "Mail josé@bücher.de please": "Mail [email] please",
            "Write 用户@例子.广告 now": "Write [email] now",
            "Ping jürgen.müller@firma.example today": "Ping [email] today",
            "Server 2001:db8:85a3::8a2e:370:7334 is": "Server [link] is",
            "Long tld site.international end": "Long tld [link] end",
            "Port and query steel.works:8443/a?b=c#d end": "Port and query [link] end",
            "Sentence ends at steelworkson.ca.": "Sentence ends at [link].",
        }
        for text, expected in cases.items():
            self.assertEqual(expected, " ".join(redact(text).split()), text)

    def test_cursor_nogo_dfbcc11_hosts_after_any_character_and_any_label_length(self):
        # Cursor NO-GO on dfbcc11: each of these survived page_to_tree unchanged.
        cases = {
            "Email us @steelworkson.ca today": "Email us @[link] today",
            "Visit -secret.photography and .bücher.de": "Visit [link] and [link]",
            "a" * 64 + ".secret.photography": "[link]",
            "IP 10\u30020\u30020\u30021": "IP [link]",
            "IP 10\uff0e0\uff0e0\uff0e1 and 10\uff610\uff610\uff611/admin": "IP [link] and [link]",
            "\u3002secret.photography": "[link]",
            "secret-.photography": "[link]",
            "foo_.photography": "[link]",
            "short." + "b" * 80 + ".photography": "[link]",
        }
        for text, expected in cases.items():
            self.assertEqual(expected, " ".join(redact(text).split()), text[:40])
        page = "".join("<p>%s</p>" % text for text in cases)
        labels = json.dumps(page_to_tree(page, "T")["children"], ensure_ascii=False)
        for never in ("steelworkson", "secret", "photography", "bücher", "10\u30020", "10\uff0e0", "foo_"):
            self.assertNotIn(never, labels)

    def test_cursor_nogo_cc56fea_marks_and_edges_never_hide_the_tld(self):
        mark = "\u0308"
        cases = {
            "Visit secret." + mark + "photography today": "Visit [link] today",
            "Mail us at steelworkson." + mark + "ca": "Mail us at [link]",
            "b\u00fccher." + mark + "de": "[link]",
            "secret.-photography": "[link]",
            "secret._photography": "[link]",
            "secret.photography" + mark * 53: "[link]",
            "Ends secret.photography- and secret.photography_ here": "Ends [link] and [link] here",
            "Mixed secret.-" + mark + "_photography here": "Mixed [link] here",
            "Punycode shop.-xn--p1ai here": "Punycode [link] here",
        }
        for text, expected in cases.items():
            self.assertEqual(expected, " ".join(redact(text).split()), repr(text[:40]))
        page = "".join("<p>%s</p>" % text for text in cases) + "<img alt='secret.%sphotography'>" % mark
        labels = json.dumps(page_to_tree(page, "T")["children"], ensure_ascii=False)
        for never in ("secret", "steelworkson", "cher", "photography", "xn--", "shop"):
            self.assertNotIn(never, labels)
        for keep in ("U.S.A. and Ph.D.", "i.e. a mark\u0308 here", "Score 3.14 - 2.5", "e.g._ then"):
            self.assertEqual(keep, " ".join(redact(keep).split()), keep)

    def test_the_canvas_title_is_the_registry_name_shown_on_purpose(self):
        # The root label is the operator-registered project name, never page text:
        # shown as written, on purpose. Page text with the same host is redacted.
        tree = page_to_tree("<h1>steelworkson.ca</h1><p>Visit steelworkson.ca</p>", "steelworkson.ca")
        self.assertEqual("steelworkson.ca", tree["label"])
        self.assertNotIn("steelworkson", json.dumps(tree["children"]))

    def test_ordinary_words_and_numbers_survive(self):
        for text in ("Steel Works Inc. builds frames, e.g. stairs and rails.",
                     "Open 9am-5pm, 10:30 to 3.14 km, i.e. most weekends.",
                     "No. 5 plate, 2.5 mm, $1,200.00 per ton.",
                     "Call us: we reply fast; email is below.",
                     "Meet @ 5pm, or ask @steelworks on social.",
                     "U.S.A. and Ph.D. and A.I. and 1.2.3 and v2.0",
                     "Open 9am-5pm, $1,200.00; 10:30; 3.14; i.e. now; e.g. then; Inc. too."):
            self.assertEqual(text, " ".join(redact(text).split()), text)

    def test_codex_blocker_1_on_the_page_labels_that_reach_state(self):
        page = ("<h1>Visit secret.photography</h1><p>Book at example.consulting/path or bücher.de</p>"
                "<p>Write josé@bücher.de or 2001:db8::7334</p><button>xn--bcher-kva.de</button>"
                "<input placeholder='you@例子.广告'><img alt='photo from studio.gallery'>")
        labels = json.dumps(page_to_tree(page, "T")["children"], ensure_ascii=False)
        for never in ("secret.photography", "example.consulting", "bücher", "josé", "2001:db8", "xn--",
                      "例子", "studio.gallery"):
            self.assertNotIn(never, labels)

    def test_the_redaction_is_linear_on_hostile_input(self):
        # Work counted where Python does it: the host scan tests each character
        # a bounded number of times. The regex engine's steps cannot be counted
        # from Python, so those passes are held to relative growth - four times
        # the text well under sixteen times the time - which load slows
        # proportionally. No absolute wall-clock limit but a 20 s backstop.
        import time as _time
        from app import project_page as page
        real_host_char, calls = page._host_char, {"n": 0}

        def counting(ch):
            calls["n"] += 1
            return real_host_char(ch)

        def cost(text):
            started = _time.perf_counter()
            redact(text)
            return _time.perf_counter() - started

        stop = chr(0x3002)
        page._host_char = counting
        try:
            for unit in ("a.", "a", "a-", "x@", "1.", "-", stop, "ab.", "1" + stop, "a.bc"):
                small, large = unit * (15000 // len(unit)), unit * (60000 // len(unit))
                calls["n"] = 0
                redact(large)
                self.assertLessEqual(calls["n"], 3 * len(large) + 10, unit)
                cost(small)
                base, grown = min(cost(small) for _ in range(3)), min(cost(large) for _ in range(3))
                self.assertLess(grown, 10 * base + 1.0, (unit, base, grown))
                self.assertLess(grown, 20.0, unit)
        finally:
            page._host_char = real_host_char

        def cost(text):
            started = _time.perf_counter()
            redact(text)
            return _time.perf_counter() - started

        stop = chr(0x3002)
        for unit in ("a.", "a", "a-", "x@", "1.", "-", stop, "ab.", "1" + stop, "a.bc"):
            small, large = unit * (15000 // len(unit)), unit * (60000 // len(unit))
            cost(small)
            base, grown = min(cost(small) for _ in range(3)), min(cost(large) for _ in range(3))
            self.assertLess(grown, 10 * base + 1.0, (unit, base, grown))
            self.assertLess(grown, 20.0, unit)


class GeminiAttacks(unittest.TestCase):
    def test_attack_1_mapped_answers_are_refused_and_a_swap_never_re_resolves(self):
        for answers in (["::ffff:127.0.0.1"], ["::ffff:169.254.169.254"], [PUBLIC_IP, "::ffff:10.0.0.1"]):
            seen = []
            with self.assertRaises(PageFetchError, msg=answers):
                fetch_page(PAGE, resolver=Resolver(answers), transport=page_transport(seen=seen))
            self.assertEqual([], seen)
        swap, seen = Resolver([PUBLIC_IP], ["::ffff:127.0.0.1"]), []
        fetch_page(PAGE, resolver=swap, transport=page_transport(seen=seen))
        self.assertEqual((1, [PUBLIC_IP]), (len(swap.calls), [r.url.host for r in seen]))

    def test_attack_3_foreign_namespaces_templates_and_noscript_leave_only_inert_labels(self):
        page = ("<body><h1>Welcome</h1>"
                "<svg><foreignObject><div><p>svg html payload</p><script>alert('svg')</script></div></foreignObject></svg>"
                "<math><annotation-xml encoding='text/html'><p>math html payload</p><script>alert('math')</script>"
                "</annotation-xml></math>"
                "<template><p>template payload</p><script>alert('tpl')</script></template>"
                "<noscript><img src=x onerror=alert('ns')><p>noscript payload</p></noscript>"
                "<p>After it all</p></body>")
        tree = page_to_tree(page, "T")
        text = json.dumps(tree).lower()
        for never in ("payload", "alert", "script", "onerror", "svg", "math", "template", "noscript", "http"):
            self.assertNotIn(never, text)
        self.assertIn("welcome", text)
        self.assertIn("after it all", text)


class ProviderTalk(FakeTalk):
    """Every agent configured, every call counted - a talk or a recap."""

    def __init__(self):
        super().__init__(agents=("claude", "openai", "gemini", "meta"))
        self.recaps = []

    def recap(self, agent, brief):
        self.recaps.append(agent)
        return "Here is where we got to."


class CountingAdvisor:
    def __init__(self):
        self.calls = 0

    def ready(self):
        return True

    def advise(self, session_id, snapshot):
        self.calls += 1
        return {"agent": "gemini", "revision": snapshot["revision"], "perspective": "p", "questions": [], "risks": []}


class OffAdvisor(CountingAdvisor):
    def ready(self):
        return False


class CodexBlocker2ProviderIsolation(Api):
    """Codex Gate 1 NO-GO on a2d98fc, blocker_2: a client session reaches only
    the approved providers, refused or re-routed before any provider call."""

    def client_session(self, client, topic=None):
        token = self.sign_in(client).json()["token"]
        body = {"creation_id": "prov-1", "start": "project", "project": "steelworks"}
        if topic:
            body["topic"] = topic
        live = client.post("/v1/session", headers=self.auth(token), json=body)
        self.assertEqual(200, live.status_code, live.text)
        return live.json()

    def say(self, client, live, **extra):
        return client.post("/v1/session/%s/talk" % live["session_id"], headers=self.auth(live["token"]),
                           json=dict({"text": "make the heading bigger"}, **extra))

    def test_an_explicit_unapproved_agent_is_refused_with_zero_provider_calls(self):
        talk = ProviderTalk()
        with TestClient(self.make(talk=talk)) as client:
            live = self.client_session(client)
            refused = [self.say(client, live, agent=a) for a in ("gemini", "meta")]
            recaps = [client.post("/v1/session/%s/recap" % live["session_id"], headers=self.auth(live["token"]),
                                  json={"agent": a}) for a in ("gemini", "meta")]
        self.assertEqual([403, 403, 403, 403], [r.status_code for r in refused + recaps])
        self.assertEqual({"that assistant is not available in this workspace"},
                         {r.json()["detail"] for r in refused + recaps})
        self.assertEqual(([], []), (talk.calls, talk.recaps))

    def test_topic_routing_skips_unapproved_providers(self):
        for topic, expected in (("website", "claude"), ("app", "claude"), ("logo", "openai"), ("salesforce_admin", "claude")):
            talk = ProviderTalk()
            with TestClient(self.make(talk=talk)) as client:
                live = self.client_session(client, topic=topic)
                said = self.say(client, live)
                recap = client.post("/v1/session/%s/recap" % live["session_id"], headers=self.auth(live["token"]), json={})
            self.assertEqual((200, 200), (said.status_code, recap.status_code), (topic, said.text, recap.text))
            self.assertEqual((expected, expected), (said.json()["speaker"], recap.json()["speaker"]), topic)
            self.assertEqual(([expected], [expected]), ([c["agent"] for c in talk.calls], talk.recaps), topic)

    def test_the_advisor_is_refused_for_a_client_with_zero_calls(self):
        advisor = CountingAdvisor()
        with TestClient(self.make(advisor=advisor)) as client:
            live = self.client_session(client)
            out = client.post("/v1/session/%s/advise" % live["session_id"], headers=self.auth(live["token"]),
                              json={"revision": live["artifact_version"]})
        self.assertEqual((403, 0), (out.status_code, advisor.calls))

    def test_a_client_gets_403_before_readiness_or_the_body_with_zero_calls(self):
        # Cursor NO-GO on cc56fea: the allowlist decides before 503 and 400.
        cases = [(OffAdvisor(), {"revision": 1}), (OffAdvisor(), {"nope": 1}), (CountingAdvisor(), {"nope": 1}),
                 (CountingAdvisor(), {}), (CountingAdvisor(), {"revision": "one"}), (CountingAdvisor(), {"revision": 99}),
                 (None, {"revision": 1}), (CountingAdvisor(), b"not json")]
        for advisor, body in cases:
            with TestClient(self.make(advisor=advisor)) as client:
                live = self.client_session(client)
                url = "/v1/session/%s/advise" % live["session_id"]
                if isinstance(body, bytes):
                    out = client.post(url, headers={**self.auth(live["token"]), "Content-Type": "application/json"},
                                      content=body)
                else:
                    out = client.post(url, headers=self.auth(live["token"]), json=body)
            self.assertEqual((403, "that assistant is not available in this workspace"),
                             (out.status_code, out.json().get("detail")), (type(advisor).__name__, body))
            self.assertEqual(0, advisor.calls if advisor is not None else 0)

    def test_operator_sessions_keep_every_agent_and_the_advisor(self):
        talk, advisor = ProviderTalk(), CountingAdvisor()
        with TestClient(self.make(talk=talk, advisor=advisor)) as client:
            token = self.sign_in(client, OPERATOR).json()["token"]
            live = client.post("/v1/session", headers=self.auth(token), json={
                "creation_id": "op-prov", "start": "blank", "topic": "website"}).json()
            routed = self.say(client, live)
            advice = client.post("/v1/session/%s/advise" % live["session_id"], headers=self.auth(live["token"]),
                                 json={"revision": live["artifact_version"]})
        self.assertEqual((200, "gemini"), (routed.status_code, routed.json()["speaker"]))
        self.assertEqual((200, 1), (advice.status_code, advisor.calls))

    def test_a_listed_provider_is_allowed_and_a_bad_setting_stops_the_service(self):
        talk = ProviderTalk()
        with TestClient(self.make(talk=talk, client_providers=("claude", "openai", "gemini"))) as client:
            live = self.client_session(client, topic="website")
            said = self.say(client, live)
        self.assertEqual((200, "gemini"), (said.status_code, said.json()["speaker"]))
        for bad in ((), ("claude", "claude"), ("claude", "grok"), ("Claude",), ("claude", "")):
            with self.assertRaises(RuntimeError, msg=bad):
                settings(client_providers=bad).validate()

    def test_the_env_parse_is_exact(self):
        import os
        from app.settings import Settings
        old = os.environ.get("STUDIO_CLIENT_PROVIDERS")
        try:
            os.environ.pop("STUDIO_CLIENT_PROVIDERS", None)
            self.assertEqual(("claude", "openai"), Settings.from_env().client_providers)
            os.environ["STUDIO_CLIENT_PROVIDERS"] = " claude , openai,gemini "
            self.assertEqual(("claude", "openai", "gemini"), Settings.from_env().client_providers)
            os.environ["STUDIO_CLIENT_PROVIDERS"] = "claude,,openai"
            with self.assertRaises(RuntimeError):
                Settings.from_env().validate()
        finally:
            if old is None:
                os.environ.pop("STUDIO_CLIENT_PROVIDERS", None)
            else:
                os.environ["STUDIO_CLIENT_PROVIDERS"] = old


# -- Codex Gate 1 on dfbcc11: combining marks, all-numeric IPv6, every surface ------------------
import unicodedata as _ud  # noqa: E402
import copy  # noqa: E402

DEVANAGARI = "\u0909\u0926\u093e\u0939\u0930\u0923.\u092d\u093e\u0930\u0924"       # उदाहरण.भारत
NFD_HOST = _ud.normalize("NFD", "b\u00fccher.de")                                       # bücher.de, decomposed
NFD_EMAIL = _ud.normalize("NFD", "jos\u00e9@b\u00fccher.de")
IPV6_EXPANDED = ("0:0:0:0:0:0:0:1", "2001:4860:4860:0:0:0:0:8888")
LEAKS = (DEVANAGARI, NFD_HOST, NFD_EMAIL) + IPV6_EXPANDED
# What must never be seen anywhere once the page is read: each host's name
# and each address's distinctive part, in both normal forms.
NEVER = ("\u0909\u0926\u093e", "\u092d\u093e\u0930", "cher", "jos", "4860", "0:0:0:0:0:0:0:1", "8888")


def surfaces_page():
    text = " / ".join(LEAKS)
    return ("<h1>Visit %s</h1><p>%s</p><p>Bracketed [%s] and [%s]:8443</p>"
            "<img alt='%s'><input placeholder='%s'><nav aria-label='%s'><a href='/'>Home</a></nav>"
            "<form><input type='submit' value='%s'></form>"
            % (DEVANAGARI, text, IPV6_EXPANDED[0], IPV6_EXPANDED[1], NFD_HOST, NFD_EMAIL, DEVANAGARI,
               IPV6_EXPANDED[1]))


class CodexDfbcc11Redaction(Api):
    def test_combining_marks_and_decomposed_hosts_are_redacted_whole(self):
        for text in (DEVANAGARI, "Visit " + DEVANAGARI + " now", NFD_HOST, "Mail " + NFD_EMAIL,
                     _ud.normalize("NFC", NFD_HOST), _ud.normalize("NFD", DEVANAGARI)):
            out = redact(text)
            for never in NEVER:
                self.assertNotIn(never, out, (text, out))
        self.assertEqual("Visit [link] now", " ".join(redact("Visit " + DEVANAGARI + " now").split()))
        self.assertEqual("Mail [email]", " ".join(redact("Mail " + NFD_EMAIL).split()))

    def test_every_eight_group_ipv6_is_parsed_bracketed_or_not(self):
        cases = {
            "Loopback 0:0:0:0:0:0:0:1 here": "Loopback [link] here",
            "DNS 2001:4860:4860:0:0:0:0:8888 here": "DNS [link] here",
            "In brackets [0:0:0:0:0:0:0:1] and [2001:4860:4860:0:0:0:0:8888]:8443 too":
                "In brackets [link] and [link] too",
        }
        for text, expected in cases.items():
            self.assertEqual(expected, " ".join(redact(text).split()), text)
        for keep in ("10:30", "1:2:3", "Score 3:1:2", "Open 9:00-17:00"):
            self.assertEqual(keep, redact(keep))

    def test_no_surface_carries_them_text_attributes_state_or_provider_context(self):
        talk = FakeTalk()
        worker = SeeingWorker()
        with TestClient(self.make(fetcher=FakeFetcher(surfaces_page()), talk=talk, worker=worker)) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token)
            self.assertEqual(200, live.status_code, live.text)
            live = live.json()
            said = client.post("/v1/session/%s/talk" % live["session_id"], headers=self.auth(live["token"]),
                               json={"text": "make the heading bigger"})
            built = client.post("/v1/session/%s/commands" % live["session_id"], headers=self.auth(live["token"]),
                                json={"command_id": "cmd-s", "session_id": live["session_id"], "type": "utterance",
                                      "expected_version": live["artifact_version"], "transcript": "bigger",
                                      "item_id": "item-s"})
            events = client.get("/v1/session/%s/events?once=true" % live["session_id"],
                                headers=self.auth(live["token"])).text
        self.assertEqual((200, 200), (said.status_code, built.status_code), (said.text, built.text))
        persisted = json.dumps(self.state(live["session_id"]), ensure_ascii=False)
        provider = json.dumps([talk.calls, worker.seen], ensure_ascii=False)
        for surface, text in (("state", persisted), ("provider", provider), ("events", events)):
            for never in NEVER:
                self.assertNotIn(never, text, surface)
        self.assertIn("[link]", persisted)


class SeeingWorker(PatchWorker):
    """The builder's view of the session - what reaches the Claude provider."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def on_turn(self, state, trigger):
        self.seen.append(copy.deepcopy(state.get("artifact")))
        return super().on_turn(state, trigger)


class GateWorker(PatchWorker):
    """Holds the builder inside the provider call until released."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def on_turn(self, state, trigger):
        self.entered.set()
        self.release.wait(10)
        return super().on_turn(state, trigger)


class CodexDfbcc11AuditAddendum(Api):
    """An accepted client command never drops out of the audit: Stop fencing
    and stale-inflight recovery audit it in the same transition."""

    def open(self, client):
        token = self.sign_in(client).json()["token"]
        live = self.project_session(client, token).json()
        return live, self.app.state.controller

    def test_a_build_fenced_by_stop_is_audited_once_ahead_of_the_stop(self):
        worker = GateWorker()
        with TestClient(self.make(worker=worker)) as client:
            live, controller = self.open(client)
            sid = live["session_id"]
            outcome = {}

            def build():
                try:
                    outcome["result"] = controller.execute(sid, {
                        "command_id": "build-1", "session_id": sid, "type": "utterance",
                        "expected_version": live["artifact_version"], "transcript": "bigger", "item_id": "item-b"})
                except Exception as exc:  # noqa: BLE001 - the fenced build's save must lose
                    outcome["error"] = type(exc).__name__

            thread = threading.Thread(target=build)
            thread.start()
            self.assertTrue(worker.entered.wait(10))
            stopped = controller.execute(sid, {"command_id": "stop-1", "session_id": sid, "type": "stop",
                                               "expected_version": 0})
            worker.release.set()
            thread.join(10)
        state = self.state(sid)
        self.assertEqual("StateConflict", outcome.get("error"), outcome)
        self.assertEqual(("failed", "completed"), (state["commands"]["build-1"]["status"],
                                                   state["commands"]["stop-1"]["status"]))
        audit = state["audit"]
        self.assertEqual([("build-1", "utterance", "failed"), ("stop-1", "stop", "applied")],
                         [(a["command_id"], a["command_type"], a["outcome"]) for a in audit])
        self.assertEqual(stopped["events"][0]["op_id"], audit[1]["op_ids"][0])
        self.assertEqual([], audit[0]["op_ids"])

    def test_a_command_stranded_by_a_restart_is_audited_when_recovery_fails_it(self):
        for receipt_type, expected_type in (("utterance", "utterance"), (None, "")):
            with TestClient(self.make()) as client:
                live, controller = self.open(client)
                sid = live["session_id"]
                repo = controller.repository
                record = repo.load(sid)
                receipt = {"status": "inflight", "started_at": 0, "expected_version": live["artifact_version"],
                           "fingerprint": "x"}
                if receipt_type:
                    receipt["command_type"] = receipt_type        # an old receipt has no type
                record.state["commands"]["lost-1"] = receipt
                record.state["active_command"] = "lost-1"
                repo.save(sid, record.state, record.token)
                controller.execute(sid, {"command_id": "pause-1", "session_id": sid, "type": "pause",
                                         "expected_version": live["artifact_version"]})
                again = client.post("/v1/session/%s/commands" % sid, headers=self.auth(live["token"]), json={
                    "command_id": "lost-1", "session_id": sid, "type": "utterance",
                    "expected_version": live["artifact_version"], "transcript": "x", "item_id": "item-x"})
            state = self.state(sid)
            self.assertTrue(state["commands"]["lost-1"]["outcome_unknown"])
            self.assertEqual([("lost-1", expected_type, "failed"), ("pause-1", "pause", "applied")],
                             [(a["command_id"], a["command_type"], a["outcome"]) for a in state["audit"]])
            self.assertEqual(409, again.status_code)                  # a replay of the lost command adds nothing
            self.assertEqual(2, len(state["audit"]))

    def test_operator_sessions_get_no_audit_from_fencing(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client, OPERATOR).json()["token"]
            live = client.post("/v1/session", headers=self.auth(token), json={"creation_id": "op-f", "start": "blank"}).json()
            controller = self.app.state.controller
            sid = live["session_id"]
            record = controller.repository.load(sid)
            record.state["commands"]["lost-1"] = {"status": "inflight", "started_at": 0, "expected_version": 1,
                                                  "fingerprint": "x", "command_type": "utterance"}
            record.state["active_command"] = "lost-1"
            controller.repository.save(sid, record.state, record.token)
            controller.execute(sid, {"command_id": "stop-1", "session_id": sid, "type": "stop", "expected_version": 0})
        self.assertNotIn("audit", self.state(sid))


# -- Codex Gate 1 on cc56fea ----------------------------------------------------------------------
class FailingGateWorker(GateWorker):
    """Held in the provider call, then fails."""

    def on_turn(self, state, trigger):
        self.entered.set()
        self.release.wait(10)
        raise RuntimeError("provider failed")


def cas_write(controller, sid, change):
    """One compare-and-set write to the session record, as update_state does."""
    repo = controller.repository
    for _ in range(8):
        record = repo.load(sid)
        state = copy.deepcopy(record.state)
        change(state)
        try:
            repo.save(sid, state, record.token)
            return
        except Exception:  # noqa: BLE001 - lost the CAS: read again
            continue
    raise AssertionError("could not write")


def commit_event(controller, sid, text="a later change"):
    cas_write(controller, sid, lambda s: controller._event(s, "confirm", {"text": text, "artifact_ids": []}))


class CodexCc56feaB1Races(Api):
    """B1: a session writer that commits while a build is in the provider call
    never wedges it - one terminal receipt, one audit entry, the other write
    kept - on success and on failure."""

    def race(self, writer, fail=False):
        worker = FailingGateWorker() if fail else GateWorker()
        with TestClient(self.make(worker=worker, talk=ProviderTalk())) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token).json()
            controller = self.app.state.controller
            controller.analysis_poll_seconds = 0.02
            sid = live["session_id"]
            out = {}

            def build():
                try:
                    out["result"] = controller.execute(sid, {
                        "command_id": "build-race", "session_id": sid, "type": "utterance",
                        "expected_version": live["artifact_version"], "transcript": "bigger", "item_id": "item-r"})
                except Exception as exc:  # noqa: BLE001
                    out["error"] = exc

            thread = threading.Thread(target=build)
            thread.start()
            self.assertTrue(worker.entered.wait(10))
            written = writer(client, live, controller)
            worker.release.set()
            thread.join(10)
            if callable(written):
                written = written()
            after = controller.execute(sid, {"command_id": "pause-after", "session_id": sid, "type": "pause",
                                             "expected_version": self.state(sid)["artifact_version"]})
        state = self.state(sid)
        self.assertIsNone(state["active_command"])
        receipt = state["commands"]["build-race"]
        self.assertEqual("failed" if fail else "completed", receipt["status"], (receipt, out))
        self.assertEqual([("build-race", "failed" if fail else "applied")],
                         [(a["command_id"], a["outcome"]) for a in state["audit"] if a["command_id"] == "build-race"])
        self.assertEqual(["build-race", "pause-after"], [a["command_id"] for a in state["audit"]])
        self.assertEqual("pause-after", after["command_id"])                       # no wedge
        if fail:
            self.assertIsInstance(out.get("error"), RuntimeError)
        else:
            self.assertNotIn("error", out)
        return state, written

    def rating(self, client, live, controller):
        out = client.post("/v1/session/%s/rating" % live["session_id"], headers=self.auth(live["token"]),
                          json={"score": 5, "comment": "fast"})
        self.assertEqual(200, out.status_code, out.text)
        return out

    def recap(self, client, live, controller):
        out = client.post("/v1/session/%s/recap" % live["session_id"], headers=self.auth(live["token"]), json={})
        self.assertEqual(200, out.status_code, out.text)
        return out

    def summary(self, client, live, controller):
        cas_write(controller, live["session_id"], lambda s: s.update(summary={"status": "sending", "at": 1}))

    def repair(self, client, live, controller):
        got = {}
        thread = threading.Thread(target=lambda: got.update(
            events=controller.events_after(live["session_id"], 10 ** 6)))
        thread.start()
        time.sleep(0.3)
        self.assertTrue(thread.is_alive(), "the repair must wait for the build")
        self.assertNotIn("events", got)

        def finish():
            thread.join(10)
            return got
        return finish

    def test_rating_during_a_build(self):
        for fail in (False, True):
            state, _ = self.race(self.rating, fail)
            self.assertEqual(5, state["rating"]["score"], fail)

    def test_recap_during_a_build(self):
        for fail in (False, True):
            state, _ = self.race(self.recap, fail)
            self.assertIn("text", state["recap"], fail)

    def test_the_summary_record_during_a_build(self):
        for fail in (False, True):
            state, _ = self.race(self.summary, fail)
            self.assertEqual("sending", state["summary"]["status"], fail)

    def test_a_repair_waits_for_the_build(self):
        for fail in (False, True):
            state, got = self.race(self.repair, fail)
            events, repaired, _ = got["events"]
            self.assertTrue(repaired)
            self.assertEqual("artifact.snapshot", events[0]["type"])
            label = json.dumps(events[0]["payload"])
            self.assertEqual(not fail, "Steel Works, bigger" in label, fail)

    def test_a_clashing_write_ends_the_build_failed_not_wedged(self):
        worker = GateWorker()
        with TestClient(self.make(worker=worker)) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token).json()
            controller = self.app.state.controller
            sid = live["session_id"]
            out = {}

            def build():
                try:
                    out["result"] = controller.execute(sid, {
                        "command_id": "build-race", "session_id": sid, "type": "utterance",
                        "expected_version": live["artifact_version"], "transcript": "bigger", "item_id": "item-r"})
                except Exception as exc:  # noqa: BLE001
                    out["error"] = exc

            thread = threading.Thread(target=build)
            thread.start()
            self.assertTrue(worker.entered.wait(10))
            commit_event(controller, sid)                  # a writer on the same key as the build
            worker.release.set()
            thread.join(10)
            after = controller.execute(sid, {"command_id": "pause-after", "session_id": sid, "type": "pause",
                                             "expected_version": self.state(sid)["artifact_version"]})
        state = self.state(sid)
        self.assertEqual(409, getattr(out.get("error"), "status", None), out)
        self.assertEqual("failed", state["commands"]["build-race"]["status"])
        self.assertIsNone(state["active_command"])
        self.assertEqual([("build-race", "failed"), ("pause-after", "applied")],
                         [(a["command_id"], a["outcome"]) for a in state["audit"]])
        self.assertIn("a later change", json.dumps(state["events"]))
        self.assertNotIn("Steel Works, bigger", json.dumps(state["artifact"]))
        self.assertEqual("pause-after", after["command_id"])


class CodexCc56feaB2OpenStreams(Api):
    """B2: an open event stream is re-authorised before every state read and
    before each batch leaves; refused means closed, nothing more sent."""

    def open_stream(self, client, live, on_read=None, at_read=2, registry_from=None):
        controller = self.app.state.controller
        original = controller.events_after
        reads = {"n": 0}

        def wrapped(sid, after):
            reads["n"] += 1
            if on_read and reads["n"] == at_read:
                on_read(sid)
            return original(sid, after)

        original_load = self.store.load
        registry_reads = {"n": 0}

        def load(name):
            if name == REGISTRY and registry_from is not None:
                registry_reads["n"] += 1
                if registry_reads["n"] >= registry_from:
                    return registry_record(client_entry(projects=[])), "revoked"
            return original_load(name)

        controller.events_after = wrapped
        self.store.load = load
        try:
            body = client.get("/v1/session/%s/events" % live["session_id"], headers=self.auth(live["token"])).text
        finally:
            controller.events_after = original
            self.store.load = original_load
        return [int(x) for x in re.findall(r"^id: (\d+)$", body, re.M)], reads["n"]

    def session(self, client):
        token = self.sign_in(client).json()["token"]
        live = self.project_session(client, token).json()
        return live, self.app.state.controller

    def test_control_a_later_event_arrives(self):
        with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1)) as client:
            live, controller = self.session(client)
            before = self.state(live["session_id"])["last_seq"]
            ids, _ = self.open_stream(client, live, on_read=lambda sid: commit_event(controller, sid))
        self.assertIn(before + 1, ids)

    def test_a_revoked_project_or_client_gets_no_later_event(self):
        for revoked in (registry_record(client_entry(projects=[])), {"version": 1, "clients": []}):
            with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1)) as client:
                live, controller = self.session(client)
                before = self.state(live["session_id"])["last_seq"]

                def revoke_then_commit(sid, record=revoked):
                    self.set_registry(record)
                    commit_event(controller, sid)
                ids, _ = self.open_stream(client, live, on_read=revoke_then_commit)
            self.assertTrue(ids)                                          # the stream did open
            self.assertTrue(all(i <= before for i in ids), (ids, before))

    def test_crossing_the_token_expiry_gets_no_later_event(self):
        offset = [0]
        with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1,
                                  clock=lambda: time.time() + offset[0])) as client:
            live, controller = self.session(client)
            before = self.state(live["session_id"])["last_seq"]

            def expire_then_commit(sid):
                offset[0] = 700                                           # past the 600 s session token
                commit_event(controller, sid)
            ids, _ = self.open_stream(client, live, on_read=expire_then_commit)
        self.assertTrue(ids)
        self.assertTrue(all(i <= before for i in ids), (ids, before))

    def test_refused_before_the_first_batch_leaves(self):
        with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1)) as client:
            live, _ = self.session(client)
            ids, reads = self.open_stream(client, live, registry_from=2)  # the open is #1
        self.assertEqual(([], 1), (ids, reads))

    def test_refused_before_the_next_state_read(self):
        with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1)) as client:
            live, _ = self.session(client)
            ids, reads = self.open_stream(client, live, registry_from=3)  # the open, then the first batch
        self.assertTrue(ids)
        self.assertEqual(1, reads)                                        # no read after the refusal


class CodexCc56feaB5WholeAddresses(unittest.TestCase):
    def test_local_and_literal_host_addresses_go_whole(self):
        cases = {
            "Write admin@localhost now": "Write [email] now",
            "Root at root@intranet.": "Root at [email].",
            "Ops ops@[2001:db8::1] here": "Ops [email] here",
            "Box user@[10.0.0.1] here": "Box [email] here",
            "Tagged user@[IPv6:2001:db8::1] end": "Tagged [email] end",
            "Unicode jürgen@server here": "Unicode [email] here",
        }
        for text, expected in cases.items():
            self.assertEqual(expected, " ".join(redact(text).split()), text)
        page = ("<p>Write admin@localhost or ops@[2001:db8::1]</p><img alt='user@[10.0.0.1]'>"
                "<input placeholder='root@intranet'>")
        labels = json.dumps(page_to_tree(page, "T")["children"], ensure_ascii=False)
        for never in ("admin", "localhost", "ops@", "2001", "user@", "10.0.0.1", "root@", "intranet"):
            self.assertNotIn(never, labels)
        for keep in ("Meet @ 5pm", "ask @steelworks", "a @ b"):
            self.assertEqual(keep, redact(keep))


# -- Codex Gate 1 NO-GO on 5c2957d / 38bc713: compatibility forms ---------------------------------
COMPAT_CASES = {
    "Visit secret.ⓒⓞⓜ today": "Visit [link] today",                       # the TLD circled
    "Visit ⓈⒺⒸⓇⒺⓉ.com today": "Visit [link] today",          # the label circled
    "Visit secret.ⓓⓔ now": "Visit [link] now",
    "Leader secret․com end": "Leader [link] end",                                       # one dot leader
    "Small stop secret﹒de end": "Small stop [link] end",
    "Squared secret.㏄ end": "Squared [link] end",                                       # one code point, two letters
    "Path ⓢⓔⓒⓡⓔⓣ.ⓒⓞⓜ/a?b=1 end": "Path [link] end",
    "Math \U0001d42c\U0001d41e\U0001d41c\U0001d42b\U0001d41e\U0001d42d.\U0001d41c\U0001d428\U0001d426 end":
        "Math [link] end",
    "Mail user＠secret.com now": "Mail [email] now",                                     # a full-width @
    "Mail user﹫secret now": "Mail [email] now",
    "Mail ⓤⓢⓔⓡ@ⓢⓔⓒⓡⓔⓣ now": "Mail [email] now",
    "IP １０。０。０。１ here": "IP [link] here",
    "Web ｈｔｔｐｓ：／／x end": "Web [link] end",
}
COMPAT_NEVER = ("ⓢⓔⓒ", "ⓈⒺⒸ", "ⓒⓞⓜ", "ⓓⓔ", "secret", "㏄",
                "＠", "﹫", "․com", "﹒de", "\U0001d42c", "ｈｔ", "ⓤⓢ")


class CodexR7CompatibilityForms(Api):
    def test_compatibility_forms_in_the_label_and_the_tld_are_redacted_whole(self):
        for text, expected in COMPAT_CASES.items():
            self.assertEqual(expected, " ".join(redact(text).split()), repr(text))

    def test_ordinary_compatibility_characters_keep_their_own_form(self):
        for keep in ("Ⓐ grade and ① first", "™ brand, ﬁne work", "Café № 5 today.",
                     "Part Ⅳ and ⒈ point", "①. ②. ③.", "Full-width ＡＢＣ words"):
            self.assertEqual(keep, " ".join(redact(keep).split()), keep)

    def test_the_view_is_linear_on_hostile_compatibility_input(self):
        # Work counted, not timed (a timing test flakes under load and would
        # fake kills in the mutation harness): each character is normalized at
        # most once per pass, one code point at a time, and the host scan tests
        # each character at most twice. The clock is only a generous backstop.
        from app import project_page as page
        real_unicodedata, real_host_char = page.unicodedata, page._host_char
        calls = {"normalize": 0, "host_char": 0, "longest": 0}

        class CountingUnicodedata:
            def normalize(self, form, text):
                calls["normalize"] += 1
                calls["longest"] = max(calls["longest"], len(text))
                return real_unicodedata.normalize(form, text)

            def __getattr__(self, name):
                return getattr(real_unicodedata, name)

        def counting_host_char(ch):
            calls["host_char"] += 1
            return real_host_char(ch)

        circled_a, fullwidth_at, square_cc, leader, one_stop = (chr(0x24D0), chr(0xFF20), chr(0x33C4),
                                                               chr(0x2024), chr(0x2488))
        page.unicodedata, page._host_char = CountingUnicodedata(), counting_host_char
        try:
            for unit in (circled_a + ".", circled_a, fullwidth_at, square_cc + ".", leader,
                         circled_a + fullwidth_at, one_stop, "a" + circled_a * 3 + "." + chr(0x24D2) * 2):
                text = unit * (60000 // len(unit))
                calls.update(normalize=0, host_char=0, longest=0)
                started = time.perf_counter()
                redact(text)
                elapsed = time.perf_counter() - started
                self.assertLessEqual(calls["normalize"], 5 * len(text), hex(ord(unit[0])))
                self.assertEqual(1, calls["longest"], hex(ord(unit[0])))          # one code point at a time
                self.assertLessEqual(calls["host_char"], 2 * 18 * len(text) + 10, hex(ord(unit[0])))
                self.assertLess(elapsed, 20.0, hex(ord(unit[0])))
        finally:
            page.unicodedata, page._host_char = real_unicodedata, real_host_char

    def test_no_surface_carries_them(self):
        texts = list(COMPAT_CASES)
        page = ("<h1>%s</h1>" % texts[0] + "".join("<p>%s</p>" % t for t in texts[1:])
                + "<img alt='%s'><input placeholder='%s'><nav aria-label='%s'><a href='/'>Home</a></nav>"
                % (texts[1], texts[8], texts[5]))
        talk, worker = FakeTalk(), SeeingWorker()
        with TestClient(self.make(fetcher=FakeFetcher(page), talk=talk, worker=worker)) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token)
            self.assertEqual(200, live.status_code, live.text)
            live = live.json()
            said = client.post("/v1/session/%s/talk" % live["session_id"], headers=self.auth(live["token"]),
                               json={"text": "make the heading bigger"})
            built = client.post("/v1/session/%s/commands" % live["session_id"], headers=self.auth(live["token"]),
                                json={"command_id": "cmd-c", "session_id": live["session_id"], "type": "utterance",
                                      "expected_version": live["artifact_version"], "transcript": "bigger",
                                      "item_id": "item-c"})
            events = client.get("/v1/session/%s/events?once=true" % live["session_id"],
                                headers=self.auth(live["token"])).text
        self.assertEqual((200, 200), (said.status_code, built.status_code), (said.text, built.text))
        persisted = json.dumps(self.state(live["session_id"]), ensure_ascii=False)
        provider = json.dumps([talk.calls, worker.seen], ensure_ascii=False)
        tree = json.dumps(page_to_tree(page, "T")["children"], ensure_ascii=False)
        for surface, text in (("tree", tree), ("state", persisted), ("provider", provider), ("events", events)):
            for never in COMPAT_NEVER:
                self.assertNotIn(never, text, (surface, never))
        self.assertIn("[link]", persisted)
        self.assertIn("[email]", persisted)


# -- Codex Gate 1 NO-GO on 38bc713 (20:25Z row): round 8 ----------------------------------------
import asyncio as _asyncio  # noqa: E402
from unittest import mock as _mock  # noqa: E402

from tests.test_studio_controller import Conflict  # noqa: E402
from app.core import CommandError  # noqa: E402
from app.guards import CEILING_RECORD, DurableGuards, GuardUnavailable, session_record, tenant_record  # noqa: E402
from app import project_page as _project_page  # noqa: E402


class CountingGateWorker(GateWorker):
    def __init__(self, fail=False):
        super().__init__()
        self.fail, self.turns = fail, 0

    def on_turn(self, state, trigger):
        self.turns += 1
        self.entered.set()
        self.release.wait(10)
        if self.fail:
            raise RuntimeError("provider failed")
        return PatchWorker.on_turn(self, state, trigger)


class CodexR8B1FinaliserExhaustion(Api):
    """B1: when the fresh-state finish loses every compare-and-set, the command
    still ends terminal - durably, without waiting for a client command."""

    def inject(self, budget):
        original, left = self.store.save, {"n": budget}

        def save(name, state, token):
            if name.startswith("studio_session_") and left["n"] > 0:
                left["n"] -= 1
                raise Conflict("injected")
            return original(name, state, token)
        self.store.save = save
        return left

    def exhaust(self, client, worker, extra, recovery_attempts, poll=0.01):
        token = self.sign_in(client).json()["token"]
        live = self.project_session(client, token).json()
        controller = self.app.state.controller
        controller.recovery_poll_seconds = poll
        controller.recovery_attempts = recovery_attempts
        sid = live["session_id"]
        out = {}

        def build():
            try:
                out["result"] = controller.execute(sid, {
                    "command_id": "build-exhaust", "session_id": sid, "type": "utterance",
                    "expected_version": live["artifact_version"], "transcript": "bigger", "item_id": "item-x"})
            except Exception as exc:  # noqa: BLE001
                out["error"] = exc

        thread = threading.Thread(target=build)
        thread.start()
        self.assertTrue(worker.entered.wait(10))
        cas_write(controller, sid, lambda s: s.update(rating={"score": 4, "comment": "", "at": 1}))  # a real writer
        left = self.inject(1 + 8 + extra)       # the reserved save, all eight fresh saves, then `extra`
        worker.release.set()
        thread.join(10)
        return live, controller, sid, out, left

    def assert_terminal(self, sid, fail, out):
        state = self.state(sid)
        receipt = state["commands"]["build-exhaust"]
        self.assertEqual("failed", receipt["status"], receipt)
        self.assertIsNone(state["active_command"])
        self.assertEqual([("build-exhaust", "failed")],
                         [(a["command_id"], a["outcome"]) for a in state["audit"] if a["command_id"] == "build-exhaust"])
        self.assertEqual(4, state["rating"]["score"])                             # orthogonal state kept
        if fail:
            self.assertIsInstance(out.get("error"), RuntimeError)
        else:
            self.assertEqual(409, getattr(out.get("error"), "status", None), out)
        return state

    def next_and_replay(self, controller, sid, worker):
        after = controller.execute(sid, {"command_id": "pause-next", "session_id": sid, "type": "pause",
                                         "expected_version": self.state(sid)["artifact_version"]})
        self.assertEqual("pause-next", after["command_id"])                       # accepted at once
        with self.assertRaises(CommandError) as replay:
            controller.execute(sid, {"command_id": "build-exhaust", "session_id": sid, "type": "utterance",
                                     "expected_version": self.state(sid)["artifact_version"],
                                     "transcript": "bigger", "item_id": "item-x"})
        self.assertEqual(409, replay.exception.status)
        self.assertEqual(1, worker.turns)                                         # replay is inert
        self.assertEqual(["build-exhaust", "pause-next"], [a["command_id"] for a in self.state(sid)["audit"]])

    def test_exhausted_success_and_failure_end_terminal_at_once(self):
        for fail in (False, True):
            worker = CountingGateWorker(fail)
            with TestClient(self.make(worker=worker)) as client:
                live, controller, sid, out, left = self.exhaust(client, worker, extra=0, recovery_attempts=40)
                self.assert_terminal(sid, fail, out)
                self.next_and_replay(controller, sid, worker)

    def test_a_scheduled_recovery_finishes_it_without_any_client_command(self):
        worker = CountingGateWorker()
        with TestClient(self.make(worker=worker)) as client:
            live, controller, sid, out, left = self.exhaust(client, worker, extra=3 + 2, recovery_attempts=40, poll=0.2)
            self.assertEqual("build-exhaust", self.state(sid)["active_command"])  # the inline tries all lost
            for thread in controller.recoveries:
                thread.join(10)
            self.assertEqual(0, left["n"])
            self.assert_terminal(sid, False, out)
            self.next_and_replay(controller, sid, worker)

    def test_the_next_command_applies_the_durable_outcome_at_once(self):
        for fail in (False, True):
            worker = CountingGateWorker(fail)
            with TestClient(self.make(worker=worker)) as client:
                live, controller, sid, out, left = self.exhaust(client, worker, extra=10 ** 6, recovery_attempts=0)
                self.assertEqual("build-exhaust", self.state(sid)["active_command"])
                left["n"] = 0                                                     # the store is calm again
                self.next_and_replay(controller, sid, worker)
                self.assert_terminal(sid, fail, out)

    def test_a_late_recovery_after_stop_writes_nothing(self):
        worker = CountingGateWorker()
        with TestClient(self.make(worker=worker)) as client:
            live, controller, sid, out, left = self.exhaust(client, worker, extra=10 ** 6, recovery_attempts=0)
            left["n"] = 0
            controller.execute(sid, {"command_id": "stop-1", "session_id": sid, "type": "stop", "expected_version": 0})
            before = copy.deepcopy(self.state(sid))
            outcome = controller._load_outcome(sid, "build-exhaust")
            self.assertIsNotNone(outcome)
            self.assertTrue(controller._apply_outcome(sid, outcome, tries=1))     # not owned: nothing to do
            self.assertEqual(before, self.state(sid))
            self.assertEqual([("build-exhaust", "failed"), ("stop-1", "applied")],
                             [(a["command_id"], a["outcome"]) for a in self.state(sid)["audit"]])


class CodexR8B2StreamSendBoundary(Api):
    """B2: drive the events route through ASGI so a change can land between
    the chunks a client actually receives."""

    def asgi_stream(self, live, on_chunk):
        chunks = []
        headers = [(b"origin", ORIGIN["Origin"].encode()), (b"authorization", ("Bearer " + live["token"]).encode()),
                   (b"host", b"testserver")]
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": "/v1/session/%s/events" % live["session_id"], "raw_path": b"",
                 "query_string": b"", "root_path": "", "headers": headers, "client": ("test", 1),
                 "server": ("testserver", 80)}
        state = {"requested": False}

        async def receive():
            if not state["requested"]:
                state["requested"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await _asyncio.sleep(3600)

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"].decode("utf-8"))
                on_chunk(len(chunks))

        _asyncio.run(self.app(scope, receive, send))
        return chunks

    def event_chunks(self, chunks):
        return [c for c in chunks if re.search(r"^id: ", c, re.M)]

    def open(self, client, extra_events=2):
        token = self.sign_in(client).json()["token"]
        live = self.project_session(client, token).json()
        controller = self.app.state.controller
        for n in range(extra_events):
            commit_event(controller, live["session_id"], "earlier %d" % n)
        return live, controller

    def test_control_a_later_event_arrives_and_a_batch_is_one_chunk(self):
        with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1)) as client:
            live, controller = self.open(client)
            first = self.state(live["session_id"])["last_seq"]
            chunks = self.asgi_stream(live, lambda n: n == 1 and commit_event(controller, live["session_id"]))
        events = self.event_chunks(chunks)
        self.assertGreaterEqual(len(re.findall(r"^id: ", events[0], re.M)), 3)     # the whole first batch, once
        self.assertIn("id: %d" % (first + 1), "".join(events[1:]))

    def test_revocation_between_chunks_sends_nothing_more(self):
        for revoked in (registry_record(client_entry(projects=[])), {"version": 1, "clients": []}):
            with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1)) as client:
                live, controller = self.open(client)

                def revoke(n, record=revoked):
                    if n == 1:
                        self.set_registry(record)
                        commit_event(controller, live["session_id"])
                chunks = self.asgi_stream(live, revoke)
            self.assertEqual(1, len(self.event_chunks(chunks)), chunks)

    def test_expiry_between_chunks_sends_nothing_more(self):
        offset = [0]
        with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1,
                                  clock=lambda: time.time() + offset[0])) as client:
            live, controller = self.open(client)

            def expire(n):
                if n == 1:
                    offset[0] = 700
                    commit_event(controller, live["session_id"])
            chunks = self.asgi_stream(live, expire)
        self.assertEqual(1, len(self.event_chunks(chunks)), chunks)

    def test_expiry_inside_the_gate_sends_nothing_more(self):
        offset = [0]
        with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1,
                                  clock=lambda: time.time() + offset[0])) as client:
            live, controller = self.open(client)
            original_load, after_first = self.store.load, {"on": False, "reads": 0}

            def load(name):
                if name == REGISTRY and after_first["on"]:
                    after_first["reads"] += 1
                    if after_first["reads"] == 2:              # the gate right before the next batch leaves
                        offset[0] = 700                        # the registry read was slow: the token expired in it
                return original_load(name)

            def first(n):
                if n == 1:
                    commit_event(controller, live["session_id"])
                    after_first["on"] = True
            self.store.load = load
            try:
                chunks = self.asgi_stream(live, first)
            finally:
                self.store.load = original_load
        self.assertEqual(1, len(self.event_chunks(chunks)), chunks)


class CodexR8B3GuardUnits(unittest.TestCase):
    """B3: the durable guards, two instances on one store."""

    def pair(self):
        store, now = MemoryStore(), [1000.0]
        return (DurableGuards(store, Conflict, clock=lambda: now[0]),
                DurableGuards(store, Conflict, clock=lambda: now[0]), now, store)

    def test_cap_and_spacing_hold_across_instances(self):
        a, b, now, _ = self.pair()
        name = session_record("s-1")
        self.assertEqual("", a.take(name, "talk", 3, 1.5))
        self.assertEqual("spacing", b.take(name, "talk", 3, 1.5))             # the other instance sees it
        now[0] += 2
        self.assertEqual("", b.take(name, "talk", 3, 1.5))
        now[0] += 2
        self.assertEqual("", a.take(name, "talk", 3, 1.5))
        now[0] += 2
        self.assertEqual("cap", b.take(name, "talk", 3, 1.5))

    def test_a_lease_is_exclusive_expires_and_is_fenced(self):
        a, b, now, _ = self.pair()
        name = session_record("s-1")
        first = a.acquire(name, "analyze", "owner-a", 60)
        self.assertIsNotNone(first)
        self.assertIsNone(b.acquire(name, "analyze", "owner-b", 60))           # held on the other instance
        now[0] += 61                                                           # expired: not counted
        second = b.acquire(name, "analyze", "owner-b", 60)
        self.assertGreater(second, first)
        a.release(name, "analyze", "owner-a", first)                           # the stale holder releases nothing
        self.assertIsNone(a.acquire(name, "analyze", "owner-c", 60))
        b.release(name, "analyze", "owner-b", first)                           # the wrong fence releases nothing
        self.assertIsNone(a.acquire(name, "analyze", "owner-c", 60))
        b.release(name, "analyze", "owner-b", second)
        self.assertIsNotNone(a.acquire(name, "analyze", "owner-c", 60))

    def test_the_ceiling_counts_live_leases_across_instances(self):
        a, b, now, _ = self.pair()
        got = [a.acquire(CEILING_RECORD, "fetch", "o1", 60, 2), b.acquire(CEILING_RECORD, "fetch", "o2", 60, 2),
               a.acquire(CEILING_RECORD, "fetch", "o3", 60, 2)]
        self.assertEqual([True, True, False], [g is not None for g in got])

    def test_a_store_failure_fails_closed(self):
        a, _, _, store = self.pair()

        def broken(*args, **kwargs):
            raise OSError("down")
        store.load = broken
        for call in (lambda: a.take("x", "talk", 3), lambda: a.acquire("x", "analyze", "o", 60)):
            with self.assertRaises(GuardUnavailable):
                call()


class HoldingFetcher(FakeFetcher):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def __call__(self, url):
        self.entered.set()
        self.release.wait(10)
        return super().__call__(url)


class CodexR8B3TwoInstances(Api):
    """B3: two app objects on one store hold the client's limits between them."""

    def second_app(self, clock=None, fetcher=None, talk=None, **overrides):
        values = dict(client_workspaces=True)
        values.update(overrides)
        return create_app(settings=settings(**values), store=self.store, worker=CountingWorker(), id_factory=IDs(),
                          email_sender=self.email_sender, project_fetcher=fetcher or FakeFetcher(),
                          talk_client=talk or ProviderTalk(), **({"clock": clock} if clock else {}))

    def test_talk_cap_and_spacing_hold_across_two_instances(self):
        offset = [0]
        clock = lambda: time.time() + offset[0]  # noqa: E731
        with TestClient(self.make(talk=ProviderTalk(), clock=clock, talk_cap=3)) as a, \
                TestClient(self.second_app(clock=clock, talk_cap=3)) as b:
            token = self.sign_in(a).json()["token"]
            live = self.project_session(a, token).json()
            say = lambda c: c.post("/v1/session/%s/talk" % live["session_id"], headers=self.auth(live["token"]),  # noqa: E731
                                   json={"text": "make it bigger"})
            first = say(a)
            spaced = say(b)                                                    # at once, on the other instance
            codes = []
            for c in (b, a, b, a):
                offset[0] += 10
                codes.append(say(c).status_code)
        self.assertEqual((200, 429), (first.status_code, spaced.status_code))
        self.assertIn("one turn every", spaced.json()["detail"])
        self.assertEqual([200, 200, 429, 429], codes)

    def test_recap_cap_holds_across_two_instances(self):
        with TestClient(self.make(talk=ProviderTalk(), recap_cap=1)) as a, \
                TestClient(self.second_app(recap_cap=1)) as b:
            token = self.sign_in(a).json()["token"]
            live = self.project_session(a, token).json()
            codes = [c.post("/v1/session/%s/recap" % live["session_id"], headers=self.auth(live["token"]),
                            json={}).status_code for c in (a, b)]
        self.assertEqual([200, 429], codes)

    def test_one_page_load_per_tenant_across_two_instances(self):
        holding = HoldingFetcher()
        with TestClient(self.make(fetcher=holding)) as a, TestClient(self.second_app()) as b:
            token = self.sign_in(a).json()["token"]
            out = {}
            thread = threading.Thread(target=lambda: out.update(a=self.project_session(a, token, "load-a")))
            thread.start()
            self.assertTrue(holding.entered.wait(10))
            busy = self.project_session(b, token, "load-b")
            holding.release.set()
            thread.join(10)
            after = self.project_session(b, token, "load-b2")
        self.assertEqual((200, 429, 200), (out["a"].status_code, busy.status_code, after.status_code))

    def test_the_page_load_ceiling_holds_across_two_instances(self):
        acme = client_entry(tenant="acme", emails=("acme@example.com",), name="Acme",
                            projects=[project("acme-site", "https://www.acme.example.com/", "acme.example.com")])
        holding = HoldingFetcher()
        with _mock.patch.object(_project_page, "MAX_OUTSTANDING_FETCHES", 1):
            with TestClient(self.make(fetcher=holding, registry=registry_record(client_entry(), acme))) as a, \
                    TestClient(self.second_app()) as b:
                nav_token = self.sign_in(a).json()["token"]
                acme_token = self.sign_in(b, "acme@example.com").json()["token"]
                out = {}
                thread = threading.Thread(target=lambda: out.update(a=self.project_session(a, nav_token, "load-a")))
                thread.start()
                self.assertTrue(holding.entered.wait(10))
                busy = self.project_session(b, acme_token, "acme-1", project_id="acme-site")
                holding.release.set()
                thread.join(10)
                after = self.project_session(b, acme_token, "acme-2", project_id="acme-site")
        self.assertEqual((200, 503, 200), (out["a"].status_code, busy.status_code, after.status_code))

    def test_guard_state_that_cannot_be_read_refuses(self):
        with TestClient(self.make(talk=ProviderTalk())) as a:
            token = self.sign_in(a).json()["token"]
            live = self.project_session(a, token).json()
            original = self.store.load

            def load(name):
                if name.startswith("studio_guard_"):
                    raise OSError("down")
                return original(name)
            self.store.load = load
            try:
                out = a.post("/v1/session/%s/talk" % live["session_id"], headers=self.auth(live["token"]),
                             json={"text": "make it bigger"})
            finally:
                self.store.load = original
        self.assertEqual(503, out.status_code)


APOSTROPHE_CASES = {
    "Write johnsmith'alias@example.com today": "Write [email] today",
    "Or o'neil+tag@sub.example.co.uk now": "Or [email] now",
    "Local d'arcy@localhost here": "Local [email] here",
    "Literal o'hara@[10.0.0.1] here": "Literal [email] here",
    "Paren (jo'e@example.com) end": "Paren [email]) end",
    "Profile https://user@host.example/path here": "Profile [link] here",
    "Wide johnsmith\uff07alias@example.com today": "Wide [email] today",      # a full-width apostrophe
}
APOSTROPHE_NEVER = ("johnsmith", "alias", "\uff07", "o'neil", "d'arcy", "o'hara", "jo'e", "user@", "/path")


class CodexR8B4ApostropheMailbox(Api):
    def test_the_whole_mailbox_goes_and_near_addresses_stay(self):
        for text, expected in APOSTROPHE_CASES.items():
            self.assertEqual(expected, " ".join(redact(text).split()), text)
        for keep in ("O'Brien's shop", "rock 'n' roll", "it's @ noon", "Meet @ 5pm", "ask @steelworks",
                     "a @b and c@ d", "email: none", "(see below)", "Tom's 'quoted' words."):
            self.assertEqual(keep, redact(keep), keep)

    def test_no_surface_carries_it(self):
        texts = list(APOSTROPHE_CASES)
        page = ("<h1>%s</h1>" % texts[0] + "".join("<p>%s</p>" % t for t in texts[1:])
                + "<img alt=\"%s\"><input placeholder=\"%s\">" % (texts[0], texts[1]))
        talk, worker = FakeTalk(), SeeingWorker()
        with TestClient(self.make(fetcher=FakeFetcher(page), talk=talk, worker=worker)) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token)
            self.assertEqual(200, live.status_code, live.text)
            live = live.json()
            said = client.post("/v1/session/%s/talk" % live["session_id"], headers=self.auth(live["token"]),
                               json={"text": "make the heading bigger"})
            built = client.post("/v1/session/%s/commands" % live["session_id"], headers=self.auth(live["token"]),
                                json={"command_id": "cmd-a", "session_id": live["session_id"], "type": "utterance",
                                      "expected_version": live["artifact_version"], "transcript": "bigger",
                                      "item_id": "item-a"})
            events = client.get("/v1/session/%s/events?once=true" % live["session_id"],
                                headers=self.auth(live["token"])).text
        self.assertEqual((200, 200), (said.status_code, built.status_code), (said.text, built.text))
        state = self.state(live["session_id"])
        surfaces = (("tree", json.dumps(page_to_tree(page, "T")["children"], ensure_ascii=False)),
                    ("state", json.dumps(state, ensure_ascii=False)),
                    ("audit", json.dumps(state.get("audit"), ensure_ascii=False)),
                    ("provider", json.dumps([talk.calls, worker.seen], ensure_ascii=False)), ("events", events))
        for surface, text in surfaces:
            for never in APOSTROPHE_NEVER:
                self.assertNotIn(never, text, (surface, never))
        self.assertIn("[email]", surfaces[1][1])


REPEATED_DOTS = {"Leader secret\u2025com end": "Leader [link] end",           # two-dot leader
                 "Ellipsis secret\u2026com end": "Ellipsis [link] end",
                 "Vertical secret\ufe19com end": "Vertical [link] end",
                 "Vertical two secret\ufe30com end": "Vertical two [link] end"}


class CodexR8RepeatedDotForms(unittest.TestCase):
    def test_repeated_dot_forms_read_as_one_dot(self):
        for text, expected in REPEATED_DOTS.items():
            self.assertEqual(expected, " ".join(redact(text).split()), repr(text))
        for keep in ("Wait\u2026 what", "and then\u2026", "Hmm\u2026 ok.", "Well... okay", "\u2025 two"):
            self.assertEqual(keep, " ".join(redact(keep).split()), repr(keep))
        labels = json.dumps(page_to_tree("".join("<p>%s</p>" % t for t in REPEATED_DOTS), "T")["children"])
        self.assertNotIn("secret", labels)


class CodexR8OperatorStreamExpiry(Api):
    """An operator's token has no registry to read: its expiry is checked
    before a batch leaves too."""

    asgi_stream = CodexR8B2StreamSendBoundary.asgi_stream
    event_chunks = CodexR8B2StreamSendBoundary.event_chunks

    def test_an_operator_stream_crossing_expiry_sends_nothing_more(self):
        offset = [0]
        with TestClient(self.make(sse_poll_seconds=0.01, sse_wait_seconds=1,
                                  clock=lambda: time.time() + offset[0])) as client:
            token = self.sign_in(client, OPERATOR).json()["token"]
            live = client.post("/v1/session", headers=self.auth(token),
                               json={"creation_id": "op-stream", "start": "blank"}).json()
            controller = self.app.state.controller

            def expire(n):
                if n == 1:
                    offset[0] = 700
                    commit_event(controller, live["session_id"])
            chunks = self.asgi_stream(live, expire)
        self.assertEqual(1, len(self.event_chunks(chunks)), chunks)

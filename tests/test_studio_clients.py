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
import logging
import os
import time
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, IDs, MemoryStore, settings,
)
from app.auth import AuthService  # noqa: E402
from app.clients import ClientRegistry, REGISTRY, project_url_ok  # noqa: E402
from app.main import WORKSPACE_DENIED, create_app  # noqa: E402
import app.project_page as project_page  # noqa: E402
from app.project_page import (  # noqa: E402
    MAX_DEPTH, MAX_IMAGES, MAX_NODES, PageFetchError, address_ok, fetch_page, page_to_tree, tree_depth,
    tree_nodes,
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
    def make(self, fetcher=None, registry=None, worker=None, **overrides):
        values = dict(client_workspaces=True)
        values.update(overrides)
        self.store = MemoryStore()
        self.store.save(REGISTRY, registry or registry_record(), None)
        self.email_sender = EmailSender()
        self.fetcher = fetcher or FakeFetcher()
        self.app = create_app(settings=settings(**values), store=self.store, worker=worker or CountingWorker(),
                              id_factory=IDs(), email_sender=self.email_sender, project_fetcher=self.fetcher)
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
        patch_op = [e["op_id"] for e in applied.json()["events"] if e["type"] == "artifact.patch"]
        self.assertEqual(("client:" + self.subject()[:16], "session", "nav", "steelworks", "cmd-1", "utterance",
                          1, 2, patch_op, "applied"),
                         (first["actor"], first["token_type"], first["tenant"], first["project"], first["command_id"],
                          first["command_type"], first["prior_revision"], first["revision"], first["op_ids"],
                          first["outcome"]))
        self.assertEqual((2, 2, [], "refused"), (second["prior_revision"], second["revision"], second["op_ids"],
                                                  second["outcome"]))
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

"""Client workspaces, behind STUDIO_CLIENT_WORKSPACES.

A registered client (the studio_clients registry) signs in with an email code,
gets a client-scope token that opens nothing operator-only, reads their own
workspace (name and projects, never an address), and can open one of their
registered pages on the canvas: the controller fetches the exact registered
https URL, turns it into a text-only artifact tree, and only then admits and
creates the session. Addresses here are example.com fixtures only.
"""
from __future__ import annotations

import json
import unittest

import httpx
from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, IDs, MemoryStore, settings,
)
from app.auth import AuthService  # noqa: E402
from app.clients import ClientRegistry, REGISTRY, project_url_ok  # noqa: E402
from app.main import create_app  # noqa: E402
from app.project_page import PageFetchError, fetch_page, page_to_tree, tree_nodes  # noqa: E402
from app.tokens import mint_token, verify_token  # noqa: E402

ORIGIN = {"Origin": "https://www.sfdc24.com"}
OPERATOR = "operator@example.com"
CLIENT_EMAIL = "client@example.com"
BROWSER = "browser-instance-1234567890"
SECRET = "test-secret-that-is-long-enough-for-tests"
PAGE = "https://www.steel.example.com/"


def registry_record(**client_overrides):
    client = {"name": "Nav", "emails": [CLIENT_EMAIL],
              "projects": [{"id": "steelworks", "name": "steel.example.com", "url": PAGE}]}
    client.update(client_overrides)
    return {"version": 1, "clients": [client]}


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


class Registry(unittest.TestCase):
    def test_a_client_is_matched_by_the_exact_lowercase_address(self):
        store = MemoryStore()
        store.save(REGISTRY, registry_record(), None)
        reg = ClientRegistry(store)
        self.assertEqual({CLIENT_EMAIL}, set(reg.emails()))
        self.assertEqual("Nav", reg.for_email(CLIENT_EMAIL)["name"])
        self.assertIsNone(reg.for_email("Client@Example.com"))
        self.assertIsNone(reg.for_email("someone@example.com"))

    def test_a_malformed_entry_is_skipped_whole(self):
        for bad in ({"emails": ["Client@Example.com"]},
                    {"projects": [{"id": "x", "name": "x", "url": "http://steel.example.com/"}]},
                    {"projects": [{"id": "x", "name": "x", "url": "https://10.0.0.5/"}]},
                    {"projects": [{"id": "x", "name": "x", "url": "https://steel.example.com:8443/"}]},
                    {"projects": [{"id": "x", "name": "x", "url": "https://user:pw@steel.example.com/"}]},
                    {"projects": [{"id": "Bad Id", "name": "x", "url": PAGE}]},
                    {"name": "<b>Nav</b>"}):
            store = MemoryStore()
            store.save(REGISTRY, registry_record(**bad), None)
            self.assertEqual(frozenset(), ClientRegistry(store).emails(), bad)

    def test_an_address_listed_for_two_clients_admits_neither(self):
        store = MemoryStore()
        record = registry_record()
        record["clients"].append({"name": "Other", "emails": [CLIENT_EMAIL, "b@example.com"], "projects": []})
        store.save(REGISTRY, record, None)
        self.assertEqual(frozenset(), ClientRegistry(store).emails())

    def test_project_urls_are_https_on_a_named_host(self):
        self.assertTrue(project_url_ok(PAGE))
        for bad in ("http://steel.example.com/", "https://127.0.0.1/", "https://[::1]/", "https://localhost/",
                    "https://steel.example.com:444/", "ftp://steel.example.com/", "https:///x", 7):
            self.assertFalse(project_url_ok(bad), bad)


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


class Workspace(unittest.TestCase):
    def make(self, fetcher=None, registry=None, **overrides):
        values = dict(client_workspaces=True)
        values.update(overrides)
        self.store = MemoryStore()
        self.store.save(REGISTRY, registry or registry_record(), None)
        self.email_sender = EmailSender()
        self.fetcher = fetcher or FakeFetcher()
        self.app = create_app(settings=settings(**values), store=self.store, worker=CountingWorker(),
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

    def project_session(self, client, token, creation_id="proj-1", project="steelworks"):
        return client.post("/v1/session", headers=self.auth(token), json={
            "creation_id": creation_id, "start": "project", "project": project})

    def session_state(self, sid):
        return self.store.data["studio_session_" + sid]

    # -- sign-in and scope ------------------------------------------------------------
    def test_a_client_signs_in_with_a_client_token(self):
        with TestClient(self.make()) as client:
            verified = self.sign_in(client)
        self.assertEqual(200, verified.status_code, verified.text)
        body = verified.json()
        self.assertEqual({"token", "expires_at", "scope"}, set(body))
        self.assertEqual("client", body["scope"])
        self.assertEqual("client", verify_token(body["token"], SECRET)["scope"])
        self.assertNotIn(CLIENT_EMAIL, json.dumps(body))

    def test_a_client_token_opens_nothing_operator_only(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            leads = client.get("/v1/leads", headers=self.auth(token))
            created = client.post("/v1/session", headers=self.auth(token),
                                  json={"creation_id": "blank-1", "start": "blank"}).json()
        self.assertEqual(401, leads.status_code)
        state = self.session_state(created["session_id"])
        self.assertEqual("", state["operator_subject"])     # metadata proposals need an operator
        self.assertTrue(state["client_subject"])
        self.assertEqual("", state["visitor_subject"])

    def test_the_workspace_is_the_clients_name_and_projects_only(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            mine = client.get("/v1/workspace", headers=self.auth(token))
        self.assertEqual(200, mine.status_code, mine.text)
        self.assertEqual({"name": "Nav", "projects": [
            {"id": "steelworks", "name": "steel.example.com", "url": PAGE}]}, mine.json())
        self.assertNotIn(CLIENT_EMAIL, mine.text)

    def test_the_workspace_refuses_operators_visitors_strangers_and_is_off_by_default(self):
        with TestClient(self.make()) as client:
            operator = self.sign_in(client, OPERATOR).json()["token"]
            visitor = mint_token("some-subject", 10 ** 11, SECRET, scope="visitor")
            codes = [client.get("/v1/workspace", headers=self.auth(t)).status_code for t in (operator, visitor)]
            stranger = client.get("/v1/workspace", headers=ORIGIN).status_code
            elsewhere = client.get("/v1/workspace", headers={"Origin": "https://evil.example"}).status_code
        self.assertEqual([403, 403], codes)
        self.assertEqual((401, 403), (stranger, elsewhere))
        with TestClient(self.make(client_workspaces=False)) as client:
            token = mint_token("x", 10 ** 11, SECRET, scope="client")
            self.assertEqual(503, client.get("/v1/workspace", headers=self.auth(token)).status_code)
            self.assertFalse(client.get("/health").json()["features"]["workspaces"])

    def test_the_workspace_needs_the_client_scope_even_for_the_clients_own_subject(self):
        with TestClient(self.make()) as client:
            subject = self.app.state.auth_service._subject_hash(CLIENT_EMAIL)
            forged = [mint_token(subject, 10 ** 11, SECRET, scope=scope) for scope in ("operator", "visitor", "session")]
            codes = [client.get("/v1/workspace", headers=self.auth(t)).status_code for t in forged]
            project = self.project_session(client, forged[0], creation_id="forged-1")
        self.assertEqual([403, 403, 403], codes)
        self.assertEqual(403, project.status_code)
        self.assertEqual([], self.fetcher.calls)             # nothing is fetched for a non-client

    def test_switched_off_a_client_token_opens_no_session(self):
        with TestClient(self.make(client_workspaces=False)) as client:
            token = mint_token("x", 10 ** 11, SECRET, scope="client")
            out = client.post("/v1/session", headers=self.auth(token), json={"creation_id": "c1", "start": "blank"})
        self.assertEqual(401, out.status_code)

    def test_a_client_taken_out_of_the_registry_is_not_signed_in(self):
        with TestClient(self.make()) as client:
            started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": CLIENT_EMAIL, "client_key": BROWSER})
            code = self.email_sender.calls[-1][1]
            record, token = self.store.load(REGISTRY)
            self.store.save(REGISTRY, {"version": 1, "clients": []}, token)
            self.app.state.clients.cache_seconds = 0
            out = client.post("/v1/auth/verify", headers=ORIGIN, json={
                "challenge_id": started.json()["challenge_id"], "email": CLIENT_EMAIL, "code": code,
                "client_key": BROWSER})
        self.assertEqual(401, out.status_code)

    # -- a project page on the canvas -------------------------------------------------
    def test_a_project_opens_as_a_text_only_tree_of_known_kinds(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            out = self.project_session(client, token)
        self.assertEqual(200, out.status_code, out.text)
        self.assertEqual([PAGE], self.fetcher.calls)
        state = self.session_state(out.json()["session_id"])
        self.assertEqual("steelworks", state["project"])
        tree = state["artifact"]
        self.assertEqual(("screen", "steel.example.com"), (tree["kind"], tree["label"]))
        flat = []

        def walk(node):
            flat.append((node["kind"], node["label"]))
            for child in node.get("children") or []:
                walk(child)
        walk(tree)
        kinds = {k for k, _ in flat}
        self.assertLessEqual(kinds, {"screen", "section", "nav", "heading", "text", "button",
                                     "image-placeholder", "list", "form", "field"})
        labels = [label for _, label in flat]
        for expected in ("Home", "Our work", "Steel Works", "Custom steel & fabrication.", "Get a quote",
                         "Services", "Welding", "Railings", "Shop floor", "Your email", "Send"):
            self.assertIn(expected, labels)
        self.assertIn(("button", "Get a quote"), flat)
        self.assertIn(("image-placeholder", "Shop floor"), flat)
        dumped = json.dumps(tree)
        for never in ("http", "cdn", "script", "cookie", "evil", "collector", "secret-token", "prefilled",
                      "frame text", "svg text", "<", ">"):
            self.assertNotIn(never, dumped)
        # It starts at version 1 with the page as its snapshot, ready to be edited by voice.
        self.assertEqual(1, state["artifact_version"])
        snapshot = [e for e in state["events"] if e["type"] == "artifact.snapshot"][0]
        self.assertEqual(tree, snapshot["payload"]["root"])

    def test_a_project_not_in_the_workspace_is_refused(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            other = self.project_session(client, token, project="someone-elses")
            wrong_type = self.project_session(client, token, project=["steelworks"])
            operator = self.sign_in(client, OPERATOR).json()["token"]
            as_operator = self.project_session(client, operator, creation_id="op-1")
        self.assertEqual([403, 403, 403], [other.status_code, wrong_type.status_code, as_operator.status_code])
        self.assertEqual([], self.fetcher.calls)

    def test_a_page_that_does_not_load_costs_no_admission(self):
        with TestClient(self.make(fetcher=FakeFetcher(fail=True))) as client:
            token = self.sign_in(client).json()["token"]
            failed = self.project_session(client, token)
            blank = client.post("/v1/session", headers=self.auth(token),
                                json={"creation_id": "after-1", "start": "blank"}).json()
        self.assertEqual((502, "the project page could not be loaded"),
                         (failed.status_code, failed.json()["detail"]))
        self.assertEqual(1, blank["daily_admission_number"])    # the failed page spent nothing
        self.assertFalse(any(k.startswith("studio_session_") and self.store.data[k].get("project")
                             for k in self.store.data))

    def test_a_replayed_project_creation_is_the_same_session_and_fetches_once(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            first = self.project_session(client, token).json()
            again = self.project_session(client, token).json()
        self.assertEqual(first["session_id"], again["session_id"])
        self.assertEqual([PAGE], self.fetcher.calls)

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
        app = create_app(settings=settings(client_workspaces=True, summary_email_enabled=True), store=self.store,
                         worker=CountingWorker(), id_factory=IDs(), email_sender=self.email_sender,
                         project_fetcher=FakeFetcher(), summary_sender=lambda email, pdf: sent.append(email))
        with TestClient(app) as client:
            token = self.sign_in(client).json()["token"]
            created = client.post("/v1/session", headers=self.auth(token),
                                  json={"creation_id": "sum-1", "start": "blank"}).json()
            for name in [n for n in self.store.data if n.startswith("studio_contact_")]:
                del self.store.data[name]                   # signed in before summaries were on
            out = client.post("/v1/session/%s/summary" % created["session_id"],
                              headers=self.auth(created["token"]), json={})
        self.assertEqual(200, out.status_code, out.text)
        self.assertEqual([CLIENT_EMAIL], sent)
        self.assertNotIn(CLIENT_EMAIL, out.text)                # masked on the way back


class CoreGuard(unittest.TestCase):
    def test_only_a_client_can_own_a_project_session(self):
        from tests.test_studio_controller import make_controller
        from app.core import CommandError
        controller, _, _ = make_controller()
        tree = {"id": "screen", "kind": "screen", "label": "x", "children": []}
        with self.assertRaises(CommandError) as caught:
            controller.create_session("x", "subject", "c-1", "project", False, None, False, "", False, tree, "p")
        self.assertEqual(403, caught.exception.status)
        state, _ = controller.create_session("x", "subject", "c-2", "project", False, None, True, "", True, tree, "p")
        self.assertEqual(("", "subject", "p", True), (state["operator_subject"], state["client_subject"],
                                                        state["project"], state["analyst"]))


class PageTree(unittest.TestCase):
    def test_the_tree_is_capped(self):
        html = "<body>" + "".join("<p>Paragraph number %d here</p>" % n for n in range(300)) + "</body>"
        tree = page_to_tree(html, "Big")
        self.assertLessEqual(tree_nodes(tree), 60)

    def test_labels_are_one_plain_line(self):
        tree = page_to_tree("<p>one two​ three\u0085 &lt;script&gt;x</p>" + "<h1>" + "w " * 300 + "</h1>", "T")
        labels = [c["label"] for c in tree["children"][0]["children"]]
        self.assertEqual("one two three scriptx", labels[0])
        self.assertLessEqual(max(len(label) for label in labels), 200)

    def test_a_broken_page_still_yields_what_was_read(self):
        tree = page_to_tree("<body><h1>Title<p>Unclosed <b>bold", "T")
        self.assertIn("Title", json.dumps(tree))


class Fetch(unittest.TestCase):
    def client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def test_same_site_redirects_are_followed_three_times_at_most(self):
        hops = {"https://steel.example.com/": "https://www.steel.example.com/",
                "https://www.steel.example.com/": "/home"}

        def handler(request):
            url = str(request.url)
            if url in hops:
                return httpx.Response(301, headers={"location": hops[url]})
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<h1>ok</h1>")
        self.assertEqual("<h1>ok</h1>", fetch_page("https://steel.example.com/", client=self.client(handler)))

        hops_seen = []

        def loop(request):
            hops_seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://www.steel.example.com/x%d" % len(hops_seen)})
        with self.assertRaises(PageFetchError):
            fetch_page("https://steel.example.com/", client=self.client(loop))
        self.assertEqual(4, len(hops_seen))                  # the page and three redirects, no more

    def test_a_redirect_off_the_site_or_off_https_is_refused(self):
        for target in ("https://evil.example/", "http://www.steel.example.com/", "https://169.254.169.254/latest",
                       "https://steel.example.com.evil.example/"):
            def handler(request, target=target):
                if str(request.url) == PAGE:
                    return httpx.Response(302, headers={"location": target})
                return httpx.Response(200, headers={"content-type": "text/html"}, text="reached")
            with self.assertRaises(PageFetchError, msg=target):
                fetch_page(PAGE, client=self.client(handler))

    def test_only_html_and_only_so_much_of_it(self):
        def not_html(request):
            return httpx.Response(200, headers={"content-type": "application/json"}, text="{}")

        def huge(request):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"a" * 2_000_000)

        def missing(request):
            return httpx.Response(404, headers={"content-type": "text/html"}, text="no")
        for handler in (not_html, huge, missing):
            with self.assertRaises(PageFetchError):
                fetch_page(PAGE, client=self.client(handler))

    def test_nothing_but_a_registered_https_page_is_even_requested(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, headers={"content-type": "text/html"}, text="x")
        for bad in ("http://steel.example.com/", "https://10.0.0.1/", "file:///etc/passwd"):
            with self.assertRaises(PageFetchError):
                fetch_page(bad, client=self.client(handler))
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()

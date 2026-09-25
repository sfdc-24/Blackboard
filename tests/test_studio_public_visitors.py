"""Public visitor sign-in, behind STUDIO_PUBLIC_VISITORS.

Off (the default), nothing changes: only the operator allowlist gets a code.
On, any well-formed email can get one - capped per address and per UTC day
across every visitor - and a verified visitor gets a short visitor token that
opens a few sessions a day and nothing operator-only. Each verified visitor is
a lead: email, sessions and the host's recap, readable by operators only.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, FakeTalk, FakeVoiceClient, IDs, MemoryStore, settings,
)
from app.auth import AuthService  # noqa: E402
from app.leads import LeadBook, LeadCapExceeded  # noqa: E402
from app.main import create_app  # noqa: E402
from app.tokens import verify_token  # noqa: E402

ORIGIN = {"Origin": "https://www.sfdc24.com"}
OPERATOR = "operator@example.com"
VISITOR = "visitor@bakery.example"
CLIENT = "browser-instance-1234567890"


def service(store=None, *, public=True, cap=100, sender=None, clock=lambda: 1000):
    sent = sender if sender is not None else []
    auth = AuthService(store or MemoryStore(), [OPERATOR], "s" * 40, lambda e, c: sent.append((e, c)),
                       clock=clock, otp_generator=lambda: "123456",
                       dispatch=lambda work: work(), wait_until=lambda deadline: None,
                       public_visitors=public, visitor_daily_cap=cap)
    return auth, sent


class RecapTalk(FakeTalk):
    def recap(self, agent, brief):
        return "You want a pre-order page for your bakery; it is on the canvas. Keep shaping it or talk to us."


class Auth(unittest.TestCase):
    def test_off_only_the_allowlist_gets_a_code(self):
        auth, sent = service(public=False)
        auth.start(VISITOR, CLIENT)
        auth.start(OPERATOR, CLIENT)
        self.assertEqual([OPERATOR], [e for e, _ in sent])

    def test_on_a_visitor_gets_a_code_and_verifies_as_a_visitor(self):
        auth, sent = service()
        challenge = auth.start(VISITOR, CLIENT)["challenge_id"]
        self.assertEqual([VISITOR], [e for e, _ in sent])
        out = auth.verify(challenge, VISITOR, "123456", CLIENT)
        self.assertEqual((True, "visitor"), (out["verified"], out["role"]))
        operator = auth.verify(auth.start(OPERATOR, CLIENT)["challenge_id"], OPERATOR, "123456", CLIENT)
        self.assertEqual("operator", operator["role"])

    def test_malformed_addresses_get_nothing_and_the_answer_looks_the_same(self):
        auth, sent = service()
        answers = [auth.start(e, CLIENT) for e in ("bad", "a@b", "A@Bakery.example", "x y@z.com", "<x>@y.com")]
        self.assertEqual([], sent)
        self.assertTrue(all(set(a) == {"challenge_id", "expires_in"} for a in answers))

    def test_the_daily_visitor_cap_holds_across_visitors_but_never_blocks_the_operator(self):
        auth, sent = service(cap=2)
        for n in range(4):
            auth.start("visitor%d@bakery.example" % n, CLIENT)
        auth.start(OPERATOR, CLIENT)
        self.assertEqual(["visitor0@bakery.example", "visitor1@bakery.example", OPERATOR], [e for e, _ in sent])

    def test_one_network_address_gets_ten_visitor_codes_an_hour_and_the_operator_is_never_limited(self):
        auth, sent = service(cap=1000)
        for n in range(12):
            auth.start("v%d@bakery.example" % n, CLIENT, "203.0.113.7")
        auth.start(OPERATOR, CLIENT, "203.0.113.7")
        auth.start("other@bakery.example", CLIENT, "198.51.100.1")
        emails = [e for e, _ in sent]
        self.assertEqual(10, sum(1 for e in emails if e.startswith("v")))
        self.assertIn(OPERATOR, emails)
        self.assertIn("other@bakery.example", emails)

    def test_one_address_gets_twenty_visitor_codes_a_day_even_spread_over_hours(self):
        now = [1_700_000_000]
        auth, sent = service(cap=1000, clock=lambda: now[0])
        for hour in range(12):
            for n in range(10):
                auth.start("h%dn%d@bakery.example" % (hour, n), CLIENT, "203.0.113.7")
            now[0] += 3600
        self.assertEqual(20, len(sent))

    def test_no_usable_address_shares_one_limited_bucket(self):
        auth, sent = service(cap=1000)
        for n in range(15):
            auth.start("u%d@bakery.example" % n, CLIENT, "")
        self.assertEqual(10, len(sent))

    def test_the_per_address_limit_still_applies_to_visitors(self):
        auth, sent = service()
        for _ in range(5):
            auth.start(VISITOR, CLIENT)
        self.assertEqual(3, len(sent))

    def test_a_challenge_written_before_roles_verifies_as_operator(self):
        store = MemoryStore()
        auth, _ = service(store)
        challenge = auth.start(OPERATOR, CLIENT)["challenge_id"]
        name = auth._challenge_name(challenge)
        record, token = store.load(name)
        record.pop("role")
        store.save(name, record, token)
        self.assertEqual("operator", auth.verify(challenge, OPERATOR, "123456", CLIENT)["role"])


class LeadBookTests(unittest.TestCase):
    def test_sessions_are_capped_per_utc_day_and_replays_do_not_count(self):
        now = [1_700_000_000]
        book = LeadBook(MemoryStore(), clock=lambda: now[0])
        book.record_verified("subj", VISITOR)
        self.assertEqual(1, book.admit_session("subj", "s-1", "Homepage conversation", 2))
        self.assertEqual(1, book.admit_session("subj", "s-1", "Homepage conversation", 2))   # replay
        self.assertEqual(2, book.admit_session("subj", "s-2", "Homepage conversation", 2))
        with self.assertRaises(LeadCapExceeded):
            book.admit_session("subj", "s-3", "Homepage conversation", 2)
        now[0] += 24 * 3600
        self.assertEqual(1, book.admit_session("subj", "s-4", "Homepage conversation", 2))

    def test_the_list_is_newest_first_with_the_last_recap(self):
        now = [1000]
        book = LeadBook(MemoryStore(), clock=lambda: now[0])
        book.record_verified("a", "a@x.example")
        now[0] += 10
        book.record_verified("b", "b@x.example")
        now[0] += 10
        book.record_recap("a", "s-1", "Wants a booking page.")
        leads = book.list()
        self.assertEqual(["a@x.example", "b@x.example"], [lead["email"] for lead in leads])
        self.assertEqual("Wants a booking page.", leads[0]["last_recap"])


class Settings_(unittest.TestCase):
    def test_public_visitors_cannot_run_with_salesforce_lead_facts(self):
        s = settings(public_visitors=True, lead_facts_enabled=True, salesforce_org_id="00D" + "A" * 15)
        with self.assertRaisesRegex(RuntimeError, "STUDIO_PUBLIC_VISITORS"):
            s.validate()

    def test_caps_are_bounded(self):
        for field, bad in (("visitor_codes_daily_cap", 0), ("visitor_sessions_per_day", 21),
                           ("visitor_token_seconds", 60)):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                settings(**{field: bad}).validate()
        for field, bad in (("operator_reserved_sessions", 20), ("operator_reserved_voice", 3)):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                settings(public_visitors=True, **{field: bad}).validate()


class Lane(unittest.TestCase):
    def make(self, public=True, **overrides):
        self.store = MemoryStore()
        self.email_sender = EmailSender()
        return create_app(settings=settings(public_visitors=public, voice_enabled=True, openai_api_key="sk-test",
                                            maintenance_secret="m" * 40, **overrides),
                          store=self.store, worker=CountingWorker(), clock=lambda: 1000, id_factory=IDs(),
                          voice_client=FakeVoiceClient(), email_sender=self.email_sender, talk_client=RecapTalk())

    def sign_in(self, client, email):
        started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": email, "client_key": CLIENT})
        sent = [c for e, c in self.email_sender.calls if e == email]
        if not sent:
            return None, None
        out = client.post("/v1/auth/verify", headers=ORIGIN, json={
            "challenge_id": started.json()["challenge_id"], "email": email, "code": sent[-1], "client_key": CLIENT})
        return out, {**ORIGIN, "Authorization": "Bearer " + out.json().get("token", "")}

    def session(self, client, headers, n=1):
        return client.post("/v1/session", headers=headers, json={"creation_id": "c-%d" % n, "start": "blank"})

    def test_health_says_whether_visitors_are_admitted(self):
        with TestClient(self.make()) as client:
            self.assertTrue(client.get("/health").json()["features"]["public_visitors"])
        with TestClient(self.make(public=False)) as client:
            self.assertFalse(client.get("/health").json()["features"]["public_visitors"])

    def test_a_visitor_signs_in_opens_a_session_and_becomes_a_lead(self):
        with TestClient(self.make()) as client:
            verified, headers = self.sign_in(client, VISITOR)
            self.assertEqual("visitor", verified.json()["scope"])
            self.assertEqual(1000 + 7200, verified.json()["expires_at"])
            created = self.session(client, headers)
            self.assertEqual(200, created.status_code, created.text)
            sid = created.json()["session_id"]
            _, operator_headers = self.sign_in(client, OPERATOR)
            leads = client.get("/v1/leads", headers=operator_headers).json()["leads"]
        self.assertEqual([VISITOR], [lead["email"] for lead in leads])
        self.assertEqual(1, leads[0]["sessions"])

    def test_a_visitor_session_is_never_an_operator_session(self):
        with TestClient(self.make()) as client:
            _, headers = self.sign_in(client, VISITOR)
            sid = self.session(client, headers).json()["session_id"]
        state = next(v for k, v in self.store.data.items() if isinstance(v, dict) and v.get("session_id") == sid)
        self.assertEqual("", state["operator_subject"])
        self.assertTrue(state["visitor_subject"])

    def test_visitors_get_two_sessions_a_day(self):
        with TestClient(self.make()) as client:
            _, headers = self.sign_in(client, VISITOR)
            codes = [self.session(client, headers, n).status_code for n in (1, 2, 3)]
        self.assertEqual([200, 200, 429], codes)

    def test_a_visitor_token_cannot_read_leads_or_act_as_an_operator(self):
        with TestClient(self.make()) as client:
            _, headers = self.sign_in(client, VISITOR)
            self.assertEqual(401, client.get("/v1/leads", headers=headers).status_code)
            token = headers["Authorization"][7:]
        with self.assertRaises(Exception):
            verify_token(token, settings().session_secret, now=1000, scope="operator")

    def test_switched_off_a_visitor_gets_no_code_and_an_old_visitor_token_opens_nothing(self):
        with TestClient(self.make()) as client:
            _, headers = self.sign_in(client, VISITOR)
        with TestClient(self.make(public=False)) as client:
            verified, _ = self.sign_in(client, "someone@else.example")
            self.assertIsNone(verified)
            self.assertEqual(401, self.session(client, headers).status_code)

    def test_the_host_recap_is_kept_with_the_lead(self):
        with TestClient(self.make()) as client:
            _, headers = self.sign_in(client, VISITOR)
            created = self.session(client, headers).json()
            session_headers = {**ORIGIN, "Authorization": "Bearer " + created["token"]}
            client.post("/v1/session/%s/commands" % created["session_id"], headers=session_headers, json={
                "command_id": "cmd-1", "session_id": created["session_id"], "type": "utterance",
                "expected_version": 1, "transcript": "A pre-order page", "item_id": "item-1"})
            recap = client.post("/v1/session/%s/recap" % created["session_id"], headers=session_headers, json={})
            _, operator_headers = self.sign_in(client, OPERATOR)
            leads = client.get("/v1/leads", headers=operator_headers).json()["leads"]
        self.assertEqual(200, recap.status_code, recap.text)
        self.assertIn("pre-order page", leads[0]["last_recap"])

    def test_visitors_stop_below_the_daily_session_cap_and_the_operator_keeps_headroom(self):
        with TestClient(self.make(daily_session_cap=4, operator_reserved_sessions=2)) as client:
            codes = []
            for n in range(3):
                _, headers = self.sign_in(client, "v%d@bakery.example" % n)
                codes.append(self.session(client, headers, n).status_code)
            _, operator_headers = self.sign_in(client, OPERATOR)
            operator_codes = [self.session(client, operator_headers, 10 + n).status_code for n in range(3)]
        self.assertEqual([200, 200, 429], codes)                  # visitors stop at 4 - 2
        self.assertEqual([200, 200, 429], operator_codes)         # the operator still gets the reserved 2

    def test_a_visitor_voice_call_stops_below_the_daily_voice_cap(self):
        with TestClient(self.make(voice_mint_cap=3, operator_reserved_voice=2)) as client:
            results = []
            for n in range(2):
                _, headers = self.sign_in(client, "v%d@bakery.example" % n)
                created = self.session(client, headers, n).json()
                results.append(client.post("/v1/session/%s/voice" % created["session_id"],
                                           headers={**ORIGIN, "Authorization": "Bearer " + created["token"]},
                                           json={"sdp": "v=0 offer"}).status_code)
        self.assertEqual([200, 429], results)

    def test_a_forged_first_forwarded_hop_does_not_escape_the_limit(self):
        with TestClient(self.make(visitor_codes_daily_cap=1000)) as client:
            for n in range(15):
                client.post("/v1/auth/start", json={"email": "f%d@bakery.example" % n, "client_key": CLIENT},
                            headers={**ORIGIN, "X-Forwarded-For": "10.0.0.%d, 203.0.113.7" % n})
            for n in range(15):
                client.post("/v1/auth/start", json={"email": "g%d@bakery.example" % n, "client_key": CLIENT},
                            headers={**ORIGIN, "X-Forwarded-For": "not-an-ip-%d" % n})
        sent = [e for e, _ in self.email_sender.calls]
        self.assertEqual(10, sum(1 for e in sent if e.startswith("f")))     # the real last hop is one address
        self.assertEqual(10, sum(1 for e in sent if e.startswith("g")))     # garbage shares one bucket

    def test_the_lead_counts_every_session_not_only_the_kept_ones(self):
        now = [1_700_000_000]
        book = LeadBook(MemoryStore(), clock=lambda: now[0])
        book.record_verified("subj", VISITOR)
        for n in range(24):
            book.admit_session("subj", "s-%d" % n, "t", 2)
            if n % 2:
                now[0] += 24 * 3600
        self.assertEqual(24, book.list()[0]["sessions"])

    def test_the_operator_path_is_unchanged(self):
        with TestClient(self.make()) as client:
            verified, headers = self.sign_in(client, OPERATOR)
            self.assertEqual("operator", verified.json()["scope"])
            self.assertEqual([200] * 3, [self.session(client, headers, n).status_code for n in (1, 2, 3)])
            self.assertEqual([], client.get("/v1/leads", headers=headers).json()["leads"])


if __name__ == "__main__":
    unittest.main()

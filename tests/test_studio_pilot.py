"""The Converspan pilot, behind STUDIO_PILOT (owner, 2026-10-09).

"In converspan, I'd like to pilot test with people that I know (subject matter
experts, investors, engineers, researchers) who are close contacts and they
should be able to do a quick 5 minute check over a call in converspan to see
how the experience is and provide feedback right in the call."

Off (the default), nothing changes. On, an invited address (STUDIO_PILOT_EMAILS)
gets a sign-in code even while public visitors are off, verifies as a `pilot`
- never an operator, never a lead - and opens a session capped at
STUDIO_PILOT_SECONDS (300) on the server-only `pilot` topic, whatever the page
sent. When the call ends its feedback leaves as one record with no personal
data, and the guest's summary is also sent once to STUDIO_PILOT_NOTIFY.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, FakeVoiceClient, IDs, MemoryStore, settings,
)
from tests.test_studio_end_card import SummarySender  # noqa: E402
from tests.test_studio_public_visitors import RecapTalk, service  # noqa: E402
from app import pilot  # noqa: E402
from app.auth import AuthService  # noqa: E402
from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.tokens import verify_token  # noqa: E402
from workers.topics import (  # noqa: E402
    CHARTER_FRAMES, SERVER_TOPICS, TOPIC_AGENT, TOPIC_BRIEFS, TOPICS, charter_frame, route_agent, topic_line,
)

ORIGIN = {"Origin": "https://www.sfdc24.com"}
OPERATOR = "operator@example.com"
GUEST = "guest@lab.example"
STRANGER = "stranger@else.example"
OWNER = "owner@notify.example"
CLIENT = "browser-instance-1234567890"
START = 1000
# The topics a page may send, exactly as PR 350 left them.
PR350_TOPICS = ["app", "conference", "logo", "other", "salesforce_admin", "salesforce_data", "website"]
PILOT_FRAME = ["intro", "demo", "worked", "missing", "change", "rating"]


def pilot_settings(**overrides):
    values = dict(pilot_enabled=True, pilot_emails=(GUEST,), pilot_seconds=300)
    values.update(overrides)
    return settings(**values)


class App(unittest.TestCase):
    def make(self, *, on=True, public=False, sink=None, summaries=None, **overrides):
        self.now = getattr(self, "now", [START])
        self.store = MemoryStore()
        self.email_sender = EmailSender()
        self.summaries = summaries if summaries is not None else SummarySender()
        self.records = [] if sink is None else sink
        values = dict(public_visitors=public, voice_enabled=True, openai_api_key="sk-test",
                      maintenance_secret="m" * 40, summary_email_enabled=True)
        values.update(overrides)
        made = pilot_settings(**values) if on else settings(**values)
        return create_app(settings=made, store=self.store, worker=CountingWorker(), clock=lambda: self.now[0],
                          id_factory=IDs(), voice_client=FakeVoiceClient(), email_sender=self.email_sender,
                          talk_client=RecapTalk(), summary_sender=self.summaries,
                          pilot_feedback_sink=self.records.append)

    def sign_in(self, client, email):
        started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": email, "client_key": CLIENT})
        sent = [c for e, c in self.email_sender.calls if e == email]
        if not sent:
            return started, None, None
        out = client.post("/v1/auth/verify", headers=ORIGIN, json={
            "challenge_id": started.json()["challenge_id"], "email": email, "code": sent[-1], "client_key": CLIENT})
        return started, out, {**ORIGIN, "Authorization": "Bearer " + out.json().get("token", "")}

    def session(self, client, headers, n=1, **body):
        payload = {"creation_id": "c-%d" % n, "start": "blank"}
        payload.update(body)
        return client.post("/v1/session", headers=headers, json=payload)

    def state(self, sid):
        return self.store.data["studio_session_" + sid]

    def pilot_session(self, client, **body):
        _, verified, headers = self.sign_in(client, GUEST)
        created = self.session(client, headers, **body)
        self.assertEqual(200, created.status_code, created.text)
        out = created.json()
        return out, {**ORIGIN, "Authorization": "Bearer " + out["token"]}


class FlagOff(App):
    def test_an_invited_address_is_nobody_while_the_pilot_is_off(self):
        with TestClient(self.make(on=False, pilot_emails=(GUEST,))) as client:
            started, verified, _ = self.sign_in(client, GUEST)
            self.assertIsNone(verified)
            self.assertEqual({"challenge_id", "expires_in"}, set(started.json()))
            _, operator, headers = self.sign_in(client, OPERATOR)
            self.assertEqual("operator", operator.json()["scope"])
            created = self.session(client, headers, topic="logo")
        self.assertEqual([OPERATOR], [e for e, _ in self.email_sender.calls])
        self.assertEqual(600, created.json()["max_session_seconds"])
        state = self.state(created.json()["session_id"])
        self.assertEqual(600, state["expires_at"] - state["created_at"])
        self.assertNotIn("role", state)
        self.assertEqual([], self.records)

    def test_the_topics_a_page_may_send_are_exactly_pr_350s(self):
        self.assertEqual(PR350_TOPICS, sorted(t for t in TOPICS if t not in SERVER_TOPICS))
        with TestClient(self.make(on=False)) as client:
            _, _, headers = self.sign_in(client, OPERATOR)
            refused = self.session(client, headers, topic="pilot")
        self.assertEqual(400, refused.status_code)
        self.assertEqual("topic must be one of: " + ", ".join(PR350_TOPICS), refused.json()["detail"])

    def test_a_visitor_is_unchanged_and_a_pilot_token_opens_nothing(self):
        with TestClient(self.make(on=True)) as client:
            _, _, pilot_headers = self.sign_in(client, GUEST)
        with TestClient(self.make(on=False, public=True)) as client:
            _, verified, headers = self.sign_in(client, "visitor@bakery.example")
            self.assertEqual("visitor", verified.json()["scope"])
            created = self.session(client, headers)
            self.assertEqual(401, self.session(client, pilot_headers, 2).status_code)
        state = self.state(created.json()["session_id"])
        self.assertEqual(600, created.json()["max_session_seconds"])
        self.assertNotIn("role", state)

    def test_from_env_reads_no_pilot_variable_while_the_flag_is_off(self):
        stray = {"STUDIO_PILOT_EMAILS": "Not An Address", "STUDIO_PILOT_SECONDS": "garbage",
                 "STUDIO_PILOT_NOTIFY": "x"}
        with mock.patch.dict(os.environ, stray, clear=False):
            os.environ.pop("STUDIO_PILOT", None)
            made = Settings.from_env()
        self.assertEqual((False, (), 300, ""),
                         (made.pilot_enabled, made.pilot_emails, made.pilot_seconds, made.pilot_notify))

    def test_from_env_reads_the_pilot_when_on(self):
        env = {"STUDIO_PILOT": "true", "STUDIO_PILOT_EMAILS": GUEST + ", b@lab.example",
               "STUDIO_PILOT_SECONDS": "240", "STUDIO_PILOT_NOTIFY": OWNER}
        with mock.patch.dict(os.environ, env, clear=False):
            made = Settings.from_env()
        self.assertEqual((True, (GUEST, "b@lab.example"), 240, OWNER),
                         (made.pilot_enabled, made.pilot_emails, made.pilot_seconds, made.pilot_notify))


class SignIn(App):
    def test_an_invited_pilot_signs_in_while_public_visitors_are_off(self):
        with TestClient(self.make(public=False)) as client:
            _, verified, headers = self.sign_in(client, GUEST)
            self.assertEqual(200, verified.status_code, verified.text)
            self.assertEqual("pilot", verified.json()["scope"])
            self.assertEqual(START + 7200, verified.json()["expires_at"])
            self.assertEqual(401, client.get("/v1/leads", headers=headers).status_code)
            _, _, operator_headers = self.sign_in(client, OPERATOR)
            self.assertEqual([], client.get("/v1/leads", headers=operator_headers).json()["leads"])
        with self.assertRaises(Exception):
            verify_token(headers["Authorization"][7:], settings().session_secret, now=START, scope="operator")

    def test_a_pilot_session_is_never_an_operator_session_and_never_a_lead(self):
        with TestClient(self.make(public=True)) as client:
            created, _ = self.pilot_session(client)
            _, _, operator_headers = self.sign_in(client, OPERATOR)
            leads = client.get("/v1/leads", headers=operator_headers).json()["leads"]
        state = self.state(created["session_id"])
        self.assertEqual(("", "pilot"), (state["operator_subject"], state["role"]))
        self.assertTrue(state["visitor_subject"])
        self.assertEqual([], leads)
        self.assertFalse(any(k.startswith("studio_lead_") for k in self.store.data))

    def test_an_address_on_both_lists_is_an_operator(self):
        with TestClient(self.make(pilot_emails=(OPERATOR, GUEST))) as client:
            _, verified, _ = self.sign_in(client, OPERATOR)
        self.assertEqual("operator", verified.json()["scope"])

    def test_a_pilot_challenge_verifies_as_a_pilot(self):
        auth = AuthService(MemoryStore(), [OPERATOR], "s" * 40, lambda e, c: None, otp_generator=lambda: "123456",
                           dispatch=lambda work: work(), wait_until=lambda deadline: None, pilot_emails=[GUEST])
        challenge = auth.start(GUEST, CLIENT)["challenge_id"]
        self.assertEqual("pilot", auth.verify(challenge, GUEST, "123456", CLIENT)["role"])

    def test_switched_off_or_uninvited_after_the_code_the_code_is_refused(self):
        for overrides in ({"pilot_enabled": False}, {"pilot_emails": ("other@lab.example",)}):
            with self.subTest(**{k: str(v) for k, v in overrides.items()}):
                store = MemoryStore()
                sender = EmailSender()
                on = create_app(settings=pilot_settings(), store=store, clock=lambda: START, id_factory=IDs(),
                                email_sender=sender, worker=CountingWorker())
                with TestClient(on) as client:
                    started = client.post("/v1/auth/start", headers=ORIGIN,
                                          json={"email": GUEST, "client_key": CLIENT}).json()
                later = create_app(settings=pilot_settings(**overrides), store=store, clock=lambda: START,
                                   id_factory=IDs(), email_sender=EmailSender(), worker=CountingWorker())
                with TestClient(later) as client:
                    out = client.post("/v1/auth/verify", headers=ORIGIN, json={
                        "challenge_id": started["challenge_id"], "email": GUEST,
                        "code": sender.calls[-1][1], "client_key": CLIENT})
                self.assertEqual((401, "verification code was not accepted"), (out.status_code, out.json()["detail"]))


class NoOracle(App):
    def test_an_uninvited_address_gets_the_same_answer_as_an_invited_one(self):
        dispatched = []
        sent = []
        ids = iter("%032x" % n for n in range(1, 100))
        auth = AuthService(MemoryStore(), [OPERATOR], "s" * 40, lambda e, c: sent.append(e),
                           otp_generator=lambda: "123456", challenge_id_generator=lambda: next(ids),
                           dispatch=lambda work: (dispatched.append(1), work()), wait_until=lambda d: None,
                           pilot_emails=[GUEST])
        answers = [auth.start(email, CLIENT) for email in (GUEST, STRANGER, OPERATOR, "Guest@Lab.example")]
        # Only the fresh challenge id differs; nothing in the answer says who was invited.
        self.assertEqual(1, len({json.dumps(dict(a, challenge_id=""), sort_keys=True) for a in answers}))
        self.assertEqual(4, len(dispatched))                 # every path goes through the same dispatcher
        self.assertEqual([GUEST, OPERATOR], sent)

    def test_over_http_the_answers_are_identical_in_shape(self):
        with TestClient(self.make()) as client:
            answers = [client.post("/v1/auth/start", headers=ORIGIN, json={"email": e, "client_key": CLIENT})
                       for e in (GUEST, STRANGER)]
        self.assertEqual([200, 200], [a.status_code for a in answers])
        self.assertEqual([{"challenge_id", "expires_in"}] * 2, [set(a.json()) for a in answers])
        self.assertEqual(answers[0].json()["expires_in"], answers[1].json()["expires_in"])
        self.assertEqual([GUEST], [e for e, _ in self.email_sender.calls])


class Session(App):
    def test_a_pilot_session_is_capped_at_300_seconds(self):
        with TestClient(self.make()) as client:
            created, _ = self.pilot_session(client)
            _, _, operator_headers = self.sign_in(client, OPERATOR)
            operator = self.session(client, operator_headers, 9).json()
        state = self.state(created["session_id"])
        self.assertEqual(300, state["expires_at"] - state["created_at"])
        self.assertEqual((START + 300, 300), (created["expires_at"], created["max_session_seconds"]))
        claims = verify_token(created["token"], settings().session_secret, now=START)
        self.assertEqual(START + 300, claims["exp"])
        self.assertEqual(600, operator["max_session_seconds"])          # an operator in the same app is unchanged

    def test_the_cap_is_never_longer_than_the_session_maximum(self):
        with TestClient(self.make(max_session_seconds=200)) as client:
            created, _ = self.pilot_session(client)
        self.assertEqual((START + 200, 200), (created["expires_at"], created["max_session_seconds"]))

    def test_a_pilot_session_is_on_the_pilot_topic_whatever_the_page_sends(self):
        with TestClient(self.make()) as client:
            _, _, headers = self.sign_in(client, GUEST)
            topics = []
            for n, sent in enumerate(("logo", "hacking", 7, None, "conference")):
                created = self.session(client, headers, n, topic=sent)
                self.assertEqual(200, created.status_code, created.text)
                topics.append(self.state(created.json()["session_id"])["topic"])
            omitted = self.session(client, headers, 99)
            topics.append(self.state(omitted.json()["session_id"])["topic"])
        self.assertEqual(["pilot"] * 6, topics)

    def test_a_pilot_voice_call_ends_with_the_session(self):
        with TestClient(self.make()) as client:
            created, headers = self.pilot_session(client)
            out = client.post("/v1/session/%s/voice" % created["session_id"], headers=headers, json={"sdp": "v=0"})
        self.assertEqual(200, out.status_code, out.text)
        self.assertEqual(START + 300, out.json()["ends_at"])

    def test_pilots_are_admitted_below_the_operator_reserve(self):
        with TestClient(self.make(daily_session_cap=3, operator_reserved_sessions=2)) as client:
            _, _, headers = self.sign_in(client, GUEST)
            codes = [self.session(client, headers, n).status_code for n in (1, 2)]
        self.assertEqual([200, 429], codes)


class Feedback(App):
    def chartered(self, sid, levels):
        state_name = "studio_session_" + sid
        record = self.store.data[state_name]
        record["charter"] = {"revision": 1, "topic": "pilot", "next": "Ask what Ada works on at Lab Example?",
                             "dimensions": [{"id": did, "level": levels.get(did, 0),
                                             "captured": ("Ada said: secret words about %s" % did)
                                             if levels.get(did, 0) else ""} for did in PILOT_FRAME]}

    def test_the_record_carries_no_email_name_or_words(self):
        with TestClient(self.make()) as client:
            created, headers = self.pilot_session(client, title="Ada Lovelace at Lab Example")
            sid = created["session_id"]
            self.chartered(sid, {"intro": 3, "demo": 2, "worked": 1})
            self.now[0] = START + 250
            rated = client.post("/v1/session/%s/rating" % sid, headers=headers,
                                json={"score": 4, "comment": "Ada here: loved it, guest@lab.example"})
        self.assertEqual(200, rated.status_code, rated.text)
        self.assertEqual(1, len(self.records))
        record = self.records[0]
        self.assertEqual(set(pilot.RECORD_FIELDS), set(record))
        self.assertEqual({"session_id": sid, "role": "pilot", "rating": 4, "duration_s": 250,
                          "frames_covered": ["intro", "demo"]}, record)
        text = json.dumps(record)
        state = self.state(sid)
        for secret in (GUEST, "guest", "Ada", "Lovelace", "loved", "secret words", state["visitor_subject"]):
            self.assertNotIn(secret, text)
        self.assertEqual(record, state["pilot_feedback"])          # the session keeps it too

    def test_the_default_sink_logs_one_line_and_nothing_else(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pilot.log_sink({"session_id": "s-1", "role": "pilot", "rating": 5, "duration_s": 280,
                            "frames_covered": ["intro"], "email": GUEST, "comment": "Ada"})
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(1, len(lines))
        logged = json.loads(lines[0])
        self.assertEqual("studio.pilot_feedback", logged["event"])
        self.assertNotIn(GUEST, lines[0])
        self.assertNotIn("Ada", lines[0])

    def test_the_recap_sends_the_record_and_a_later_rating_sends_it_again_once(self):
        with TestClient(self.make()) as client:
            created, headers = self.pilot_session(client)
            sid = created["session_id"]
            self.now[0] = START + 200
            recap = client.post("/v1/session/%s/recap" % sid, headers=headers, json={})
            self.assertEqual(200, recap.status_code, recap.text)
            self.now[0] = START + 260
            client.post("/v1/session/%s/recap" % sid, headers=headers, json={})       # nothing new: not sent
            client.post("/v1/session/%s/rating" % sid, headers=headers, json={"score": 5})
            client.post("/v1/session/%s/rating" % sid, headers=headers, json={"score": 5})
        self.assertEqual([(None, 200), (5, 260)], [(r["rating"], r["duration_s"]) for r in self.records])

    def test_a_pilot_recap_never_writes_the_lead_book(self):
        with TestClient(self.make(public=True)) as client:
            created, headers = self.pilot_session(client)
            client.post("/v1/session/%s/recap" % created["session_id"], headers=headers, json={})
        self.assertFalse(any(k.startswith("studio_lead_") for k in self.store.data))

    def test_operator_and_visitor_sessions_send_no_record(self):
        with TestClient(self.make(public=True)) as client:
            for n, email in enumerate((OPERATOR, "visitor@bakery.example")):
                _, _, headers = self.sign_in(client, email)
                created = self.session(client, headers, n).json()
                session_headers = {**ORIGIN, "Authorization": "Bearer " + created["token"]}
                client.post("/v1/session/%s/recap" % created["session_id"], headers=session_headers, json={})
                client.post("/v1/session/%s/rating" % created["session_id"], headers=session_headers,
                            json={"score": 3})
                self.assertNotIn("pilot_feedback", self.state(created["session_id"]))
        self.assertEqual([], self.records)

    def test_a_failing_sink_never_fails_the_rating(self):
        def broken(record):
            raise RuntimeError("redis is down")

        with TestClient(self.make(sink=None)) as client:
            created, headers = self.pilot_session(client)
        app = create_app(settings=pilot_settings(), store=self.store, worker=CountingWorker(),
                         clock=lambda: self.now[0], id_factory=IDs(), email_sender=self.email_sender,
                         talk_client=RecapTalk(), pilot_feedback_sink=broken)
        with TestClient(app) as client, contextlib.redirect_stdout(io.StringIO()) as logged:
            rated = client.post("/v1/session/%s/rating" % created["session_id"], headers=headers, json={"score": 2})
        self.assertEqual(200, rated.status_code, rated.text)
        self.assertEqual(2, self.state(created["session_id"])["pilot_feedback"]["rating"])
        self.assertIn("studio.pilot_feedback_sink_failed", logged.getvalue())

    def test_the_voice_time_limit_sends_the_record(self):
        with TestClient(self.make()) as client:
            created, headers = self.pilot_session(client)
            opened = client.post("/v1/session/%s/voice" % created["session_id"], headers=headers,
                                 json={"sdp": "v=0"})
            self.assertEqual(200, opened.status_code, opened.text)
            self.now[0] = START + 301
            swept = client.post("/v1/maintenance/voice-sweep", headers={"Authorization": "Bearer " + "m" * 40})
        self.assertEqual(200, swept.status_code, swept.text)
        self.assertEqual([(created["session_id"], 300)], [(r["session_id"], r["duration_s"]) for r in self.records])


class OwnerCopy(App):
    def test_a_pilots_summary_is_also_sent_to_the_owner_once(self):
        with TestClient(self.make(pilot_notify=OWNER)) as client:
            created, headers = self.pilot_session(client)
            sid = created["session_id"]
            first = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
            again = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual([200, 200], [first.status_code, again.status_code])
        self.assertEqual(first.json(), again.json())
        self.assertEqual([GUEST, OWNER], [e for e, _ in self.summaries.calls])
        self.assertEqual(self.summaries.calls[0][1], self.summaries.calls[1][1])    # the same PDF
        self.assertEqual("sent", self.state(sid)["pilot_copy"]["status"])

    def test_a_copy_is_tried_once_even_when_it_fails(self):
        class OwnerUnconfirmed(SummarySender):
            def __call__(self, email, pdf):
                self.calls.append((email, pdf))
                if email == OWNER:
                    raise TimeoutError("read timed out after the request left")

        with TestClient(self.make(pilot_notify=OWNER, summaries=OwnerUnconfirmed())) as client:
            created, headers = self.pilot_session(client)
            sid = created["session_id"]
            with contextlib.redirect_stdout(io.StringIO()):
                first = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
                self.state(sid)["summary"] = {"status": "failed", "at": START}      # the guest may ask again
                again = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual([200, 200], [first.status_code, again.status_code])
        self.assertEqual([GUEST, OWNER, GUEST], [e for e, _ in self.summaries.calls])
        self.assertEqual("unconfirmed", self.state(sid)["pilot_copy"]["status"])

    def test_a_pilots_summary_is_never_a_quote(self):
        with TestClient(self.make()) as client:
            created, headers = self.pilot_session(client)
            sid = created["session_id"]
            Feedback.chartered(self, sid, {"intro": 2})
            with mock.patch("app.summary_pdf.build_quote_pdf", side_effect=AssertionError("a quote")) as quote:
                sent = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(200, sent.status_code, sent.text)
        quote.assert_not_called()
        self.assertTrue(self.summaries.calls[0][1].startswith(b"%PDF"))

    def test_no_copy_without_a_notify_address_or_for_anyone_else(self):
        with TestClient(self.make()) as client:
            created, headers = self.pilot_session(client)
            client.post("/v1/session/%s/summary" % created["session_id"], headers=headers, json={})
        self.assertEqual([GUEST], [e for e, _ in self.summaries.calls])
        with TestClient(self.make(pilot_notify=OWNER)) as client:
            _, _, operator_headers = self.sign_in(client, OPERATOR)
            created = self.session(client, operator_headers).json()
            client.post("/v1/session/%s/summary" % created["session_id"],
                        headers={**ORIGIN, "Authorization": "Bearer " + created["token"]}, json={})
        self.assertEqual([OPERATOR], [e for e, _ in self.summaries.calls])


class Topic(unittest.TestCase):
    def test_the_pilot_topic_is_server_only_and_routes_to_the_chair(self):
        self.assertEqual({"pilot"}, set(SERVER_TOPICS))
        self.assertIn("pilot", TOPICS)
        self.assertEqual("claude", TOPIC_AGENT["pilot"])
        self.assertEqual("claude", route_agent("pilot", ("claude", "openai")))

    def test_the_frame_is_the_owners_order(self):
        self.assertEqual(PILOT_FRAME, [d[0] for d in CHARTER_FRAMES["pilot"]])
        self.assertEqual(CHARTER_FRAMES["pilot"], charter_frame("pilot"))

    def test_every_lane_hears_the_brief_with_persona_names_only(self):
        line = topic_line({"topic": "pilot"})
        brief = TOPIC_BRIEFS["pilot"]
        self.assertIn(brief, line)
        for needed in ("5-minute Converspan pilot check", "brisk", "on the canvas", "3:30", "what worked",
                       "one change", "1 to 5", "recap", "Claude chair", "Greg", "Jenny", "Aya", "Cody"):
            self.assertIn(needed, brief)
        for vendor in ("Anthropic", "OpenAI", "Gemini", "Google", "Meta", "Llama", "Grok", "xAI", "GPT",
                       "ChatGPT", "Codex", "Copilot", "Cursor"):
            self.assertNotIn(vendor, line)


class Validation(unittest.TestCase):
    def test_the_pilot_settings_are_checked_only_while_on_and_never_name_an_address(self):
        settings(pilot_emails=("Bad Address",), pilot_seconds=5).validate()          # off: not read
        cases = (({"pilot_emails": ()}, "STUDIO_PILOT_EMAILS"),
                 ({"pilot_emails": ("Guest@Lab.example",)}, "STUDIO_PILOT_EMAILS"),
                 ({"pilot_emails": (" guest@lab.example",)}, "STUDIO_PILOT_EMAILS"),
                 ({"pilot_seconds": 59}, "STUDIO_PILOT_SECONDS"),
                 ({"pilot_seconds": 601}, "STUDIO_PILOT_SECONDS"),
                 ({"pilot_notify": "Owner@Notify.example"}, "STUDIO_PILOT_NOTIFY"),
                 ({"lead_facts_enabled": True, "salesforce_org_id": "00D" + "A" * 15}, "STUDIO_ENABLE_LEAD_FACTS"),
                 ({"operator_reserved_sessions": 20}, "STUDIO_OPERATOR_RESERVED_SESSIONS"))
        for overrides, named in cases:
            with self.subTest(named=named, overrides=str(overrides)):
                with self.assertRaisesRegex(RuntimeError, named) as caught:
                    pilot_settings(**overrides).validate()
                self.assertNotIn("@", str(caught.exception))
        pilot_settings(pilot_notify=OWNER).validate()


if __name__ == "__main__":
    unittest.main()

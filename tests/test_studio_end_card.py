"""The end card: how happy the visitor is, and the working session as a PDF.

POST /v1/session/{id}/rating stores a 1-5 score and a short comment on the
session. POST /v1/session/{id}/summary builds a PDF of the session and emails
it to the owner's verified address, once. Both accept the session token for 30
minutes after the session ends and no longer; no other endpoint gains that
grace. The address comes only from server records (the contact written at
sign-in, checked against its subject, or the operator allowlist), never from
the request.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import struct
import unittest
import zlib

import httpx
from fastapi.testclient import TestClient

from tests.test_studio_controller import (  # noqa: E402  (sets sys.path for app.*)
    CountingWorker, EmailSender, IDs, MemoryStore, settings,
)
from tests.test_studio_voices import RecapTalk  # noqa: E402
from app.main import create_app  # noqa: E402
from app.summary_pdf import PNG_MAX_BYTES, clean, mask_email  # noqa: E402

ORIGIN = {"Origin": "https://www.sfdc24.com"}
OPERATOR = "operator@example.com"
CLIENT = "browser-instance-1234567890"
START = 1000
EXPIRES = START + 600
GRACE = 30 * 60


def _chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png(width=4, height=3, padding=0):
    """A real PNG, made without Pillow so the fixture cannot hide a Pillow bug.
    `padding` adds a private ancillary chunk of that many bytes, which every
    decoder skips: a valid PNG of any size."""
    raw = b"".join(b"\x00" + b"\x0b\x1f\x3a" * width for _ in range(height))
    extra = _chunk(b"prVt", b"\x00" * padding) if padding else b""
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw)) + extra + _chunk(b"IEND", b""))


def data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def pdf_text(pdf: bytes) -> str:
    """The page text of an fpdf2 PDF: its content streams, inflated."""
    out = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            out.append(zlib.decompress(match.group(1)).decode("latin-1"))
        except zlib.error:
            out.append(match.group(1).decode("latin-1"))
    return "\n".join(out)


class SummarySender:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, email, pdf):
        self.calls.append((email, pdf))
        if self.fail:
            raise RuntimeError("sender down")


class EndCard(unittest.TestCase):
    def make(self, store=None, sender=None, **overrides):
        values = dict(summary_email_enabled=True)
        values.update(overrides)
        self.now = getattr(self, "now", [START])
        self.store = store if store is not None else MemoryStore()
        self.email_sender = EmailSender()
        self.sender = sender if sender is not None else SummarySender()
        self.talk = RecapTalk()
        return create_app(settings=settings(**values), store=self.store, worker=CountingWorker(),
                          clock=lambda: self.now[0], id_factory=IDs(), email_sender=self.email_sender,
                          talk_client=self.talk, summary_sender=self.sender)

    def operator_token(self, client, email=OPERATOR):
        started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": email, "client_key": CLIENT})
        code = self.email_sender.calls[-1][1]
        return client.post("/v1/auth/verify", headers=ORIGIN, json={
            "challenge_id": started.json()["challenge_id"], "email": email,
            "code": code, "client_key": CLIENT}).json()["token"]

    def session(self, client, creation_id="end-card-1", operator=None):
        operator = operator or self.operator_token(client)
        created = client.post("/v1/session", headers={**ORIGIN, "Authorization": "Bearer " + operator},
                              json={"creation_id": creation_id, "start": "blank", "title": "Bakery logo"}).json()
        return created["session_id"], {**ORIGIN, "Authorization": "Bearer " + created["token"]}

    def state(self, sid):
        return self.store.data["studio_session_" + sid]


class Rating(EndCard):
    def test_a_score_from_one_to_five_is_kept_with_its_comment(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            for score in (1, 5):
                r = client.post("/v1/session/%s/rating" % sid, headers=headers,
                                json={"score": score, "comment": "  Loved the logo  "})
                self.assertEqual((200, {"ok": True}), (r.status_code, r.json()))
        self.assertEqual({"score": 5, "comment": "Loved the logo", "at": START}, self.state(sid)["rating"])

    def test_scores_outside_one_to_five_and_non_integers_are_refused(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            for score in (0, 6, -1, 3.0, 2.5, True, "5", None):
                r = client.post("/v1/session/%s/rating" % sid, headers=headers, json={"score": score})
                self.assertEqual(400, r.status_code, score)
            self.assertEqual(400, client.post("/v1/session/%s/rating" % sid, headers=headers,
                                              json={"score": 4, "comment": "x" * 301}).status_code)
            self.assertEqual(200, client.post("/v1/session/%s/rating" % sid, headers=headers,
                                              json={"score": 4, "comment": "x" * 300}).status_code)
        self.assertEqual(4, self.state(sid)["rating"]["score"])

    def test_unknown_fields_are_refused(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            r = client.post("/v1/session/%s/rating" % sid, headers=headers, json={"score": 5, "email": "a@b.c"})
        self.assertEqual(400, r.status_code)
        self.assertNotIn("rating", self.state(sid))


class Grace(EndCard):
    def test_a_stopped_session_can_be_rated_and_summarised(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            stop = client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "stop-1", "session_id": sid, "type": "stop", "expected_version": 1})
            self.assertEqual(200, stop.status_code, stop.text)
            self.now[0] = START + 60
            self.assertEqual(200, client.post("/v1/session/%s/rating" % sid, headers=headers,
                                              json={"score": 5}).status_code)
            self.assertEqual(200, client.post("/v1/session/%s/summary" % sid, headers=headers, json={}).status_code)
        self.assertEqual(1, len(self.sender.calls))

    def test_thirty_minutes_after_a_time_limit_end_and_not_one_second_more(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.now[0] = EXPIRES + GRACE - 1
            self.assertEqual(200, client.post("/v1/session/%s/rating" % sid, headers=headers,
                                              json={"score": 3}).status_code)
            self.now[0] = EXPIRES + GRACE
            late_rating = client.post("/v1/session/%s/rating" % sid, headers=headers, json={"score": 1})
            late_summary = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(401, late_rating.status_code)
        self.assertEqual(401, late_summary.status_code)
        self.assertEqual(3, self.state(sid)["rating"]["score"])
        self.assertEqual([], self.sender.calls)

    def test_the_session_record_bounds_the_window_even_with_a_longer_token(self):
        # The token is minted to expires_at; the stored record must agree.
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.state(sid)["expires_at"] = START + 10
            self.now[0] = START + 10 + GRACE
            r = client.post("/v1/session/%s/rating" % sid, headers=headers, json={"score": 2})
        self.assertEqual(410, r.status_code)

    def test_no_other_endpoint_gains_the_grace(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.now[0] = EXPIRES + 1
            for path, body in (("commands", {"command_id": "c-1", "session_id": sid, "type": "pause",
                                             "expected_version": 1}),
                               ("talk", {"text": "hello"}), ("recap", {})):
                r = client.post("/v1/session/%s/%s" % (sid, path), headers=headers, json=body)
                self.assertEqual(401, r.status_code, path)
            events = client.get("/v1/session/%s/events" % sid, headers=headers, params={"once": "true"})
            self.assertEqual(401, events.status_code)


class Ownership(EndCard):
    def test_another_sessions_token_a_forged_token_and_an_operator_token_are_refused(self):
        with TestClient(self.make()) as client:
            operator = self.operator_token(client)
            sid_a, headers_a = self.session(client, "end-card-a", operator)
            sid_b, headers_b = self.session(client, "end-card-b", operator)
            for path, body in (("rating", {"score": 5}), ("summary", {})):
                self.assertEqual(403, client.post("/v1/session/%s/%s" % (sid_a, path),
                                                  headers=headers_b, json=body).status_code)
                self.assertEqual(401, client.post("/v1/session/%s/%s" % (sid_a, path),
                                                  headers={**ORIGIN, "Authorization": "Bearer forged.token"},
                                                  json=body).status_code)
                self.assertEqual(401, client.post("/v1/session/%s/%s" % (sid_a, path),
                                                  headers={**ORIGIN, "Authorization": "Bearer " + operator},
                                                  json=body).status_code)
                self.assertEqual(403, client.post("/v1/session/%s/%s" % (sid_a, path),
                                                  headers={"Origin": "https://evil.example",
                                                           "Authorization": headers_a["Authorization"]},
                                                  json=body).status_code)
        self.assertEqual([], self.sender.calls)
        self.assertNotIn("rating", self.state(sid_a))

    def test_the_request_can_never_choose_the_address(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            r = client.post("/v1/session/%s/summary" % sid, headers=headers,
                            json={"email": "attacker@evil.example"})
            self.assertEqual(400, r.status_code)
            ok = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual({"sent": True, "to": "o***@example.com"}, ok.json())
        self.assertEqual([OPERATOR], [email for email, _ in self.sender.calls])
        self.assertNotIn(OPERATOR, json.dumps(self.state(sid)))       # only the masked form is kept

    def test_a_contact_record_that_does_not_hash_to_the_subject_is_ignored(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            contact = next(k for k in self.store.data if k.startswith("studio_contact_"))
            self.store.data[contact]["email"] = "attacker@evil.example"
            client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual([OPERATOR], [email for email, _ in self.sender.calls])

    def test_the_contact_written_at_sign_in_reaches_an_owner_off_the_allowlist(self):
        store = MemoryStore()
        with TestClient(self.make(store=store)) as client:
            sid, headers = self.session(client)
        # The same store, and an allowlist that no longer names this owner (a
        # public visitor): only the sign-in contact record knows the address.
        sender = SummarySender()
        app = create_app(settings=settings(summary_email_enabled=True, operator_emails=("someone@else.example",)),
                         store=store, worker=CountingWorker(), clock=lambda: START, id_factory=IDs(),
                         email_sender=EmailSender(), talk_client=RecapTalk(), summary_sender=sender)
        with TestClient(app) as client:
            r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([OPERATOR], [email for email, _ in sender.calls])

    def test_an_operator_without_a_contact_record_is_found_on_the_allowlist(self):
        # Signed in before the summary was switched on: no contact record.
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            for key in [k for k in self.store.data if k.startswith("studio_contact_")]:
                del self.store.data[key]
            r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([OPERATOR], [email for email, _ in self.sender.calls])

    def test_no_contact_is_kept_while_the_summary_is_switched_off(self):
        with TestClient(self.make(summary_email_enabled=False)) as client:
            self.operator_token(client)
        self.assertEqual([], [k for k in self.store.data if k.startswith("studio_contact_")])

    def test_no_address_on_record_is_a_conflict_not_a_guess(self):
        store = MemoryStore()
        with TestClient(self.make(store=store)) as client:
            sid, headers = self.session(client)
        for key in [k for k in store.data if k.startswith("studio_contact_")]:
            del store.data[key]
        app = create_app(settings=settings(summary_email_enabled=True, operator_emails=("someone@else.example",)),
                         store=store, worker=CountingWorker(), clock=lambda: START, id_factory=IDs(),
                         email_sender=EmailSender(), talk_client=RecapTalk(), summary_sender=SummarySender())
        with TestClient(app) as client:
            r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(409, r.status_code)


class Summary(EndCard):
    def test_the_pdf_carries_the_recap_the_design_and_the_rating(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.assertEqual(200, client.post("/v1/session/%s/recap" % sid, headers=headers, json={}).status_code)
            client.post("/v1/session/%s/rating" % sid, headers=headers, json={"score": 5, "comment": "Superb"})
            r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={"design_png": data_url(png())})
        self.assertEqual(200, r.status_code, r.text)
        email, pdf = self.sender.calls[0]
        self.assertTrue(pdf.startswith(b"%PDF"))
        text = pdf_text(pdf)
        self.assertIn("bakery logo", text)                 # the host's recap
        self.assertIn("5 out of 5", text)
        self.assertIn("Superb", text)
        self.assertIn("/Subtype /Image", pdf.decode("latin-1"))
        self.assertEqual("sent", self.state(sid)["summary"]["status"])

    def test_a_replay_answers_from_the_record_and_sends_nothing(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            first = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
            second = client.post("/v1/session/%s/summary" % sid, headers=headers, json={"design_png": data_url(png())})
        self.assertEqual(first.json(), second.json())
        self.assertEqual(1, len(self.sender.calls))

    def test_a_send_that_finished_while_this_one_was_reading_is_not_repeated(self):
        # Two requests race: this one read the record before the other marked
        # it sent. The compare-and-set reservation must see "sent" and stop.
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            name = "studio_session_" + sid
            before = dict(self.state(sid))
            self.state(sid)["summary"] = {"status": "sent", "at": START, "to": "o***@example.com"}
            real_load = self.store.load
            stale = {"left": 1}

            def load(key):
                if key == name and stale["left"]:
                    stale["left"] -= 1
                    state, token = real_load(key)
                    return before, token
                return real_load(key)

            self.store.load = load
            r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual({"sent": True, "to": "o***@example.com"}, r.json())
        self.assertEqual([], self.sender.calls)

    def test_one_in_flight_send_blocks_a_second_until_it_is_stale(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            self.state(sid)["summary"] = {"status": "sending", "at": START}
            self.now[0] = START + 119
            busy = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
            self.now[0] = START + 120
            stale = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(409, busy.status_code)
        self.assertEqual(200, stale.status_code, stale.text)
        self.assertEqual(1, len(self.sender.calls))

    def test_a_failed_send_is_reported_and_can_be_retried(self):
        sender = SummarySender(fail=True)
        with TestClient(self.make(sender=sender)) as client:
            sid, headers = self.session(client)
            failed = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
            self.assertEqual("failed", self.state(sid)["summary"]["status"])
            sender.fail = False
            retried = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(502, failed.status_code)
        self.assertEqual(200, retried.status_code)
        self.assertEqual(2, len(sender.calls))

    def test_switched_off_it_is_503_and_nothing_is_sent(self):
        with TestClient(self.make(summary_email_enabled=False)) as client:
            sid, headers = self.session(client)
            r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
            health = client.get("/health").json()["features"]
        self.assertEqual(503, r.status_code)
        self.assertEqual([], self.sender.calls)
        self.assertEqual((True, False), (health["rating"], health["summary_email"]))

    def test_health_names_the_summary_when_it_is_on(self):
        with TestClient(self.make()) as client:
            self.assertTrue(client.get("/health").json()["features"]["summary_email"])

    def test_only_a_small_real_png_is_accepted_as_the_design(self):
        small = len(png())
        cases = {
            "not a data url": "https://evil.example/x.png",
            "jpeg": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff").decode(),
            "not base64": "data:image/png;base64,***",
            "not a png": data_url(b"GIF89a" + b"\x00" * 40),
            "too wide": data_url(png(4097, 1)),                       # real, decodable, too wide
            "too large": data_url(png(padding=PNG_MAX_BYTES + 1 - small - 12)),  # real, one byte over
            "unreadable": data_url(png()[:33] + b"garbage-not-a-png-stream"),
        }
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            for name, value in cases.items():
                r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={"design_png": value})
                self.assertEqual(400, r.status_code, name)
            huge = client.post("/v1/session/%s/summary" % sid, headers=headers,
                               json={"design_png": "data:image/png;base64," + "A" * 2_200_000})
        self.assertEqual(413, huge.status_code)
        self.assertEqual([], self.sender.calls)
        self.assertNotIn("summary", self.state(sid))

    def test_visitor_text_cannot_break_the_pdf(self):
        with TestClient(self.make()) as client:
            sid, headers = self.session(client)
            state = self.state(sid)
            state["transcript"] = [{"role": "visitor", "text": ") Tj ET /JavaScript (app.alert(1)) \\ — é 中 \U0001f600"}]
            r = client.post("/v1/session/%s/summary" % sid, headers=headers, json={})
        self.assertEqual(200, r.status_code, r.text)
        text = pdf_text(self.sender.calls[0][1])
        self.assertIn("/JavaScript \\(app.alert\\(1\\)\\)", text)     # escaped: still just text
        self.assertNotIn("/JavaScript (app", text)


class SignedDelivery(unittest.TestCase):
    """The HTTP path to the Apps Script sender: kind summary, signed over the PDF digest."""

    def test_the_request_is_signed_over_the_exact_pdf(self):
        secret = "summary-sender-secret-at-least-32-bytes"
        payloads = []

        def route(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        email_client = httpx.Client(transport=httpx.MockTransport(route))
        app = create_app(settings=settings(summary_email_enabled=True,
                                           email_sender_url="https://script.google.com/macros/s/test/exec",
                                           email_sender_secret=secret),
                         store=MemoryStore(), worker=CountingWorker(), clock=lambda: START, id_factory=IDs(),
                         email_client=email_client, talk_client=RecapTalk())
        try:
            with TestClient(app) as client:
                started = client.post("/v1/auth/start", headers=ORIGIN, json={"email": OPERATOR, "client_key": CLIENT})
                code = payloads[-1]["code"]
                operator = client.post("/v1/auth/verify", headers=ORIGIN, json={
                    "challenge_id": started.json()["challenge_id"], "email": OPERATOR,
                    "code": code, "client_key": CLIENT}).json()["token"]
                created = client.post("/v1/session", headers={**ORIGIN, "Authorization": "Bearer " + operator},
                                      json={"creation_id": "signed-1", "start": "blank"}).json()
                r = client.post("/v1/session/%s/summary" % created["session_id"],
                                headers={**ORIGIN, "Authorization": "Bearer " + created["token"]}, json={})
        finally:
            email_client.close()
        self.assertEqual(200, r.status_code, r.text)
        payload = payloads[-1]
        self.assertEqual({"kind", "timestamp", "nonce", "email", "pdf", "pdf_sha256", "signature"}, set(payload))
        self.assertEqual(("summary", OPERATOR), (payload["kind"], payload["email"]))
        pdf = base64.b64decode(payload["pdf"])
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(hashlib.sha256(pdf).hexdigest(), payload["pdf_sha256"])
        canonical = "\n".join(("summary", payload["timestamp"], payload["nonce"], OPERATOR, payload["pdf_sha256"]))
        expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(expected, payload["signature"]))


class Helpers(unittest.TestCase):
    def test_masking_and_latin1_cleaning(self):
        self.assertEqual("j***@example.com", mask_email("jane@example.com"))
        self.assertEqual("*a - b ? ?", clean("*a — b 中 \U0001f600"))
        self.assertEqual("x y", clean("x\x00y"))


if __name__ == "__main__":
    unittest.main()

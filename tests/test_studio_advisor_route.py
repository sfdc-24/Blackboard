"""POST /v1/session/{id}/advise (Codex plan R3): the Gemini advisor behind a
switch, on the committed canvas, bound to its revision, fenced when the canvas
moves during the call, never committed."""
from __future__ import annotations

import contextlib
import io
import json
import unittest

from fastapi.testclient import TestClient

import tests.test_studio_controller as base  # noqa: E402  (sets sys.path; its cases are not re-run here)
from app.main import create_app  # noqa: E402


ADVICE = {"agent": "gemini", "perspective": "Say where the shop is.", "questions": [], "risks": []}


class FakeAdvisor:
    def __init__(self, ready=True, answer=True, during=None):
        self._ready, self.answer, self.during = ready, answer, during
        self.snapshots = []

    def ready(self):
        return self._ready

    def advise(self, session_id, snapshot):
        self.snapshots.append(snapshot)
        if self.during:
            self.during()
        return dict(ADVICE, revision=snapshot["revision"]) if self.answer else None


class Route(unittest.TestCase):
    origin = base.TalkLaneTests.origin

    def make(self, advisor):
        self.store = base.MemoryStore()
        self.email_sender = base.EmailSender()
        self.app = create_app(settings=base.settings(), store=self.store, worker=base.CountingWorker(),
                              clock=lambda: 1000, id_factory=base.IDs(), email_sender=self.email_sender,
                              talk_client=base.FakeTalk(), advisor=advisor)
        return self.app

    def session(self, client):
        started = client.post("/v1/auth/start", headers=self.origin, json={
            "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
        code = self.email_sender.calls[-1][1]
        operator = client.post("/v1/auth/verify", headers=self.origin, json={
            "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
            "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
        created = client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + operator},
                              json={"creation_id": "adv-1", "start": "blank", "topic": "website"}).json()
        return created["session_id"], {**self.origin, "Authorization": "Bearer " + created["token"]}

    def test_advice_on_the_committed_canvas_bound_to_its_revision(self):
        advisor = FakeAdvisor()
        with TestClient(self.make(advisor)) as client:
            sid, headers = self.session(client)
            out = client.post("/v1/session/%s/advise" % sid, headers=headers, json={"revision": 1})
        self.assertEqual(200, out.status_code, out.text)
        self.assertEqual(1, out.json()["advice"]["revision"])
        snap = advisor.snapshots[0]
        self.assertEqual(1, snap["revision"])
        self.assertIn("Build a website", snap["topic_line"])
        self.assertEqual(snap["canvas"], snap["canvas"].strip())

    def test_a_stale_revision_is_refused_before_any_call(self):
        advisor = FakeAdvisor()
        with TestClient(self.make(advisor)) as client:
            sid, headers = self.session(client)
            out = client.post("/v1/session/%s/advise" % sid, headers=headers, json={"revision": 0})
        self.assertEqual(409, out.status_code)
        self.assertEqual([], advisor.snapshots)

    def test_advice_is_fenced_when_the_canvas_moves_during_the_call(self):
        holder = {}

        def build_meanwhile():
            repo = holder["app"].state.controller.repository
            sid = holder["sid"]
            record = repo.load(sid)
            moved = dict(record.state)
            moved["artifact_version"] = int(moved["artifact_version"]) + 1
            repo.save(sid, moved, record.token)

        advisor = FakeAdvisor(during=build_meanwhile)
        with TestClient(self.make(advisor)) as client:
            holder["app"] = self.app
            sid, headers = self.session(client)
            holder["sid"] = sid
            out = client.post("/v1/session/%s/advise" % sid, headers=headers, json={"revision": 1})
        self.assertEqual({"advice": None, "fenced": True}, out.json())

    def test_advice_is_fenced_when_a_new_turn_changes_only_the_transcript(self):
        holder = {}

        def speak_meanwhile():
            repo = holder["app"].state.controller.repository
            sid = holder["sid"]
            record = repo.load(sid)
            moved = dict(record.state)
            moved["transcript"] = list(moved.get("transcript") or []) + [
                {"role": "visitor", "text": "Make it warmer"}
            ]
            moved["turn_seq"] = int(moved.get("turn_seq") or 0) + 1
            moved["turn_id"] = "turn-%d" % moved["turn_seq"]
            # The reproduced bug matters precisely because the canvas did not
            # move while the words the advisor read became stale.
            self.assertEqual(1, moved["artifact_version"])
            repo.save(sid, moved, record.token)

        advisor = FakeAdvisor(during=speak_meanwhile)
        with TestClient(self.make(advisor)) as client:
            holder["app"] = self.app
            sid, headers = self.session(client)
            holder["sid"] = sid
            out = client.post("/v1/session/%s/advise" % sid, headers=headers, json={"revision": 1})
        self.assertEqual({"advice": None, "fenced": True}, out.json())

    def test_advice_is_fenced_when_a_new_command_is_only_reserved(self):
        holder = {}

        def reserve_meanwhile():
            repo = holder["app"].state.controller.repository
            sid = holder["sid"]
            record = repo.load(sid)
            moved = dict(record.state)
            moved["active_command"] = "cmd-new"
            # Reservation happens before command completion advances sequence
            # or artifact counters, so this field must independently fence.
            self.assertEqual(record.state.get("last_seq"), moved.get("last_seq"))
            self.assertEqual(record.state.get("turn_seq"), moved.get("turn_seq"))
            self.assertEqual(record.state.get("artifact_version"), moved.get("artifact_version"))
            repo.save(sid, moved, record.token)

        advisor = FakeAdvisor(during=reserve_meanwhile)
        with TestClient(self.make(advisor)) as client:
            holder["app"] = self.app
            sid, headers = self.session(client)
            holder["sid"] = sid
            out = client.post("/v1/session/%s/advise" % sid, headers=headers, json={"revision": 1})
        self.assertEqual({"advice": None, "fenced": True}, out.json())

    def test_advice_is_fenced_when_voice_lifecycle_changes_only(self):
        holder = {}

        def reserve_voice_meanwhile():
            repo = holder["app"].state.controller.repository
            sid = holder["sid"]
            record = repo.load(sid)
            moved = dict(record.state)
            moved["voice_call"] = {
                "voice_id": "voice-new",
                "status": "opening",
                "started_at": 1000,
                "ends_at": 1600,
            }
            self.assertEqual(record.state.get("last_seq"), moved.get("last_seq"))
            self.assertEqual(record.state.get("turn_seq"), moved.get("turn_seq"))
            self.assertEqual(record.state.get("artifact_version"), moved.get("artifact_version"))
            repo.save(sid, moved, record.token)

        advisor = FakeAdvisor(during=reserve_voice_meanwhile)
        with TestClient(self.make(advisor)) as client:
            holder["app"] = self.app
            sid, headers = self.session(client)
            holder["sid"] = sid
            out = client.post("/v1/session/%s/advise" % sid, headers=headers, json={"revision": 1})
        self.assertEqual({"advice": None, "fenced": True}, out.json())

    def test_advice_is_fenced_when_failed_command_reservation_returns_to_idle(self):
        holder = {}

        def fail_command_meanwhile():
            controller = holder["app"].state.controller
            sid = holder["sid"]
            before = controller.repository.load(sid).state
            controller.worker.fail = True
            try:
                with self.assertRaisesRegex(RuntimeError, "synthetic worker failed"):
                    controller.execute(sid, {
                        "command_id": "cmd-aba",
                        "session_id": sid,
                        "type": "utterance",
                        "expected_version": 1,
                        "item_id": "typed-aba",
                        "transcript": "Try a change that fails",
                    })
            finally:
                controller.worker.fail = False
            after = controller.repository.load(sid).state
            self.assertIsNone(after.get("active_command"))
            self.assertEqual("failed", after["commands"]["cmd-aba"]["status"])
            self.assertEqual(before.get("last_seq"), after.get("last_seq"))
            self.assertEqual(before.get("turn_seq"), after.get("turn_seq"))
            self.assertEqual(before.get("artifact_version"), after.get("artifact_version"))
            self.assertEqual(int(before.get("command_epoch") or 0) + 2,
                             after["command_epoch"])

        advisor = FakeAdvisor(during=fail_command_meanwhile)
        with TestClient(self.make(advisor)) as client:
            holder["app"] = self.app
            sid, headers = self.session(client)
            holder["sid"] = sid
            out = client.post("/v1/session/%s/advise" % sid, headers=headers,
                              json={"revision": 1})
        self.assertEqual({"advice": None, "fenced": True}, out.json())

    def test_advice_is_fenced_when_failed_voice_open_returns_to_absent(self):
        holder = {}

        def fail_voice_meanwhile():
            controller = holder["app"].state.controller
            sid = holder["sid"]
            before = controller.repository.load(sid).state
            controller.begin_voice(sid, "voice-aba", 1600)
            self.assertTrue(controller.fail_voice(sid, "voice-aba"))
            after = controller.repository.load(sid).state
            self.assertNotIn("voice_call", before)
            self.assertNotIn("voice_call", after)
            self.assertEqual(before.get("last_seq"), after.get("last_seq"))
            self.assertEqual(before.get("turn_seq"), after.get("turn_seq"))
            self.assertEqual(before.get("artifact_version"), after.get("artifact_version"))
            self.assertEqual(int(before.get("voice_epoch") or 0) + 2,
                             after["voice_epoch"])

        advisor = FakeAdvisor(during=fail_voice_meanwhile)
        with TestClient(self.make(advisor)) as client:
            holder["app"] = self.app
            sid, headers = self.session(client)
            holder["sid"] = sid
            out = client.post("/v1/session/%s/advise" % sid, headers=headers,
                              json={"revision": 1})
        self.assertEqual({"advice": None, "fenced": True}, out.json())

    def test_off_means_503_and_health_says_so(self):
        with TestClient(self.make(FakeAdvisor(ready=False))) as client:
            sid, headers = self.session(client)
            off = client.post("/v1/session/%s/advise" % sid, headers=headers, json={"revision": 1})
            health = client.get("/health").json()["features"]
        self.assertEqual(503, off.status_code)
        self.assertFalse(health["advisor"])

    def test_no_advice_is_an_empty_answer_and_nothing_is_committed(self):
        with TestClient(self.make(FakeAdvisor(answer=False))) as client:
            sid, headers = self.session(client)
            before = dict(self.store.data["studio_session_" + sid])
            out = client.post("/v1/session/%s/advise" % sid, headers=headers, json={"revision": 1})
            after = self.store.data["studio_session_" + sid]
        self.assertEqual({"advice": None}, out.json())
        self.assertEqual(before["artifact_version"], after["artifact_version"])
        self.assertNotIn("advice", after)

    def test_bad_bodies_and_strangers(self):
        with TestClient(self.make(FakeAdvisor())) as client:
            sid, headers = self.session(client)
            codes = [client.post("/v1/session/%s/advise" % sid, headers=headers, json=b).status_code
                     for b in ({}, {"revision": "1"}, {"revision": -1}, {"revision": 1, "extra": 1})]
            stranger = client.post("/v1/session/%s/advise" % sid, headers=self.origin, json={"revision": 1})
        self.assertEqual([400, 400, 400, 400], codes)
        self.assertEqual(401, stranger.status_code)


class Moderated(Route):
    """Codex #266 prerequisites: the advisor's words are moderated before the
    page sees them (fail-closed), every call leaves one content-free telemetry
    line, and the advisor cannot be switched on without moderation."""

    def make_moderated(self, advisor, mode="ok", key="sk-moderation-test"):
        import tests.test_studio_governance as gov
        self.store = base.MemoryStore()
        self.email_sender = base.EmailSender()
        self.moderation = gov.FakeModeration(mode)
        self.app = create_app(settings=base.settings(moderation_enabled=True, openai_api_key=key),
                              store=self.store, worker=base.CountingWorker(), clock=lambda: 1000,
                              id_factory=base.IDs(), email_sender=self.email_sender, talk_client=base.FakeTalk(),
                              advisor=advisor, moderation_client=self.moderation)
        return self.app

    def advise_logged(self, advisor, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with TestClient(self.make_moderated(advisor, **kw)) as client:
                sid, headers = self.session(client)
                reply = client.post("/v1/session/%s/advise" % sid, headers=headers, json={"revision": 1})
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return reply, [line for line in lines if line.get("event") == "studio.advisor_call"]

    def test_clean_advice_passes_and_the_gate_read_every_shown_string(self):
        advisor = FakeAdvisor()
        advisor_answer = dict(ADVICE, perspective="Say where the shop is.", risks=["No hours yet."],
                              questions=[{"id": "q1", "prompt": "Which first?", "why": "Order.",
                                          "options": [{"id": "a", "label": "Menu"}, {"id": "b", "label": "Map"}],
                                          "recommended": "a"}])
        advisor.advise = lambda sid, snap: dict(advisor_answer, revision=snap["revision"])
        reply, calls = self.advise_logged(advisor)
        self.assertEqual(200, reply.status_code, reply.text)
        self.assertEqual("Say where the shop is.", reply.json()["advice"]["perspective"])
        read = [t for batch in self.moderation.inputs() for t in batch]
        for shown in ("Say where the shop is.", "Which first?", "Order.", "Menu", "Map", "No hours yet."):
            self.assertIn(shown, read)
        self.assertEqual(["advice"], [c["outcome"] for c in calls])

    def test_flagged_advice_is_withheld_and_never_reaches_the_page(self):
        advisor = FakeAdvisor()
        advisor.advise = lambda sid, snap: dict(ADVICE, perspective="FLAG this", revision=snap["revision"])
        reply, calls = self.advise_logged(advisor)
        self.assertEqual({"advice": None, "withheld": True}, reply.json())
        self.assertEqual([("withheld", "flagged", ["harassment"])],
                         [(c["outcome"], c["reason"], c["categories"]) for c in calls])

    def test_moderation_unavailable_withholds_the_advice(self):
        for mode, key in (("raise", "sk-moderation-test"), ("http500", "sk-moderation-test"), ("ok", "")):
            reply, calls = self.advise_logged(FakeAdvisor(), mode=mode, key=key)
            self.assertEqual({"advice": None, "withheld": True}, reply.json(), mode)
            self.assertEqual(["moderation_unavailable"], [c["reason"] for c in calls], mode)

    def test_every_call_leaves_one_line_with_no_words(self):
        reply, calls = self.advise_logged(FakeAdvisor(answer=False))
        self.assertEqual({"advice": None}, reply.json())
        self.assertEqual(1, len(calls))
        self.assertEqual({"event", "severity", "session_id", "outcome", "latency_ms"}, set(calls[0]))
        self.assertNotIn("Say where", json.dumps(calls))
        self.assertNotIn("website", json.dumps(calls))

    def test_the_advisor_cannot_be_switched_on_without_moderation(self):
        with self.assertRaises(RuntimeError):
            base.settings(advisor_enabled=True, moderation_enabled=False).validate()
        base.settings(advisor_enabled=True, moderation_enabled=True).validate()


if __name__ == "__main__":
    unittest.main()

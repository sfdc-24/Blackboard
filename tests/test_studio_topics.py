"""The topic a visitor picks on the homepage before Start (owner, 2026-09-25).

It is a closed set, stored on the session at creation, and it reaches every
lane's context - talk, recap, analyst and builder - so the right agents start
from the same brief without the visitor knowing which agent that is.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import tests.test_studio_controller as base  # noqa: E402  (sets sys.path for app.*; not re-run here)
from app.state import StudioRepository  # noqa: E402
from workers import claude_worker as cw  # noqa: E402
from workers.topics import TOPICS, topic_line, with_topic  # noqa: E402


class TopicTests(unittest.TestCase):
    origin = base.TalkLaneTests.origin
    make = base.TalkLaneTests.make          # the talk lane's app: FakeTalk records each call

    def create(self, client, body):
        if not getattr(self, "operator", None):
            started = client.post("/v1/auth/start", headers=self.origin, json={
                "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
            code = self.email_sender.calls[-1][1]
            self.operator = client.post("/v1/auth/verify", headers=self.origin, json={
                "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
                "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
        return client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + self.operator},
                           json=body)

    def test_health_says_topics_are_taken(self):
        with TestClient(self.make()) as client:
            self.assertTrue(client.get("/health").json()["features"]["topics"])

    def test_a_topic_is_stored_on_the_session(self):
        with TestClient(self.make()) as client:
            created = self.create(client, {"creation_id": "topic-1", "start": "blank", "topic": "salesforce_data"})
        self.assertEqual(200, created.status_code, created.text)
        state = StudioRepository(self.store).load(created.json()["session_id"]).state
        self.assertEqual("salesforce_data", state["topic"])

    def test_no_topic_is_the_empty_topic(self):
        with TestClient(self.make()) as client:
            created = self.create(client, {"creation_id": "topic-2", "start": "blank"})
        state = StudioRepository(self.store).load(created.json()["session_id"]).state
        self.assertEqual("", state["topic"])

    def test_a_topic_outside_the_set_is_refused(self):
        with TestClient(self.make()) as client:
            for bad in ("hacking", "Logo", 7, ["logo"], "logo\n", False, 0, [], {}, True):
                created = self.create(client, {"creation_id": "topic-bad", "start": "blank", "topic": bad})
                self.assertEqual(400, created.status_code, (bad, created.text))

    def test_a_replayed_creation_keeps_its_first_topic(self):
        with TestClient(self.make()) as client:
            first = self.create(client, {"creation_id": "topic-r", "start": "blank", "topic": "logo"}).json()
            self.create(client, {"creation_id": "topic-r", "start": "template", "topic": "website", "title": "x"})
            self.create(client, {"creation_id": "topic-r", "start": "blank"})
        self.assertEqual("logo", StudioRepository(self.store).load(first["session_id"]).state["topic"])

    def test_the_talk_lane_hears_the_topic(self):
        with TestClient(self.make()) as client:
            created = self.create(client, {"creation_id": "topic-3", "start": "blank", "topic": "logo"}).json()
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            client.post("/v1/session/%s/talk" % created["session_id"], headers=headers, json={"text": "hello"})
        self.assertTrue(self.talk.calls[0]["canvas"].startswith(
            "The visitor picked this topic before starting: Design a logo"), self.talk.calls[0]["canvas"])


class TheMuseHearsTheTopic(unittest.TestCase):
    def test_inspire_gets_the_topic_line_first(self):
        import tests.test_studio_muse as muse_tests   # module alias: its cases are not re-run here
        case = muse_tests.Endpoints()
        with TestClient(case.make()) as client:
            started = client.post("/v1/auth/start", headers=case.origin, json={
                "email": "operator@example.com", "client_key": "browser-instance-1234567890"})
            code = case.email_sender.calls[-1][1]
            operator = client.post("/v1/auth/verify", headers=case.origin, json={
                "challenge_id": started.json()["challenge_id"], "email": "operator@example.com",
                "code": code, "client_key": "browser-instance-1234567890"}).json()["token"]
            created = client.post("/v1/session", headers={**case.origin, "Authorization": "Bearer " + operator},
                                  json={"creation_id": "muse-topic", "start": "blank", "topic": "app"}).json()
            headers = {**case.origin, "Authorization": "Bearer " + created["token"]}
            out = client.post("/v1/session/%s/inspire" % created["session_id"], headers=headers, json={"text": "an app"})
        self.assertEqual(200, out.status_code, out.text)
        self.assertTrue(case.muse.calls[0]["canvas"].startswith(
            "The visitor picked this topic before starting: Develop an app"), case.muse.calls[0]["canvas"][:120])


class TopicLine(unittest.TestCase):
    def test_every_topic_has_a_line_and_nothing_else_does(self):
        for topic in TOPICS:
            self.assertIn(TOPICS[topic], topic_line({"topic": topic}))
        for other in ("", None, "hacking", 5):
            self.assertEqual("", topic_line({"topic": other}))
        self.assertEqual("", topic_line(None))
        self.assertEqual("canvas", with_topic({}, "canvas"))

    def test_the_builder_sees_the_topic_first(self):
        state = {"artifact": {"id": "screen", "kind": "screen", "label": "x", "children": []},
                 "questions": [], "transcript": [], "topic": "website"}
        described = cw._describe(state, {"kind": "utterance", "text": "a page"})
        self.assertTrue(described.startswith("The visitor picked this topic before starting: Build a website"),
                        described[:120])
        self.assertNotIn("picked this topic", cw._describe(dict(state, topic=""), {"kind": "utterance", "text": "a"}))


CONFERENCE_FRAME = ["goal", "architecture", "data", "process", "risks", "decisions", "next"]


class ConferenceTopic(unittest.TestCase):
    """The conference experience page (owner, 2026-10-09): the owner works with the
    agents on the conference line itself, drawn live as diagrams on the canvas."""
    origin = base.TalkLaneTests.origin
    make = base.TalkLaneTests.make
    create = TopicTests.create

    def test_the_validator_takes_it_and_stores_it(self):
        with TestClient(self.make()) as client:
            created = self.create(client, {"creation_id": "topic-conf", "start": "blank", "topic": "conference"})
            for bad in ("Conference", "conference ", "conference_line"):
                refused = self.create(client, {"creation_id": "topic-conf-bad", "start": "blank", "topic": bad})
                self.assertEqual(400, refused.status_code, (bad, refused.text))
        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual("conference", StudioRepository(self.store).load(created.json()["session_id"]).state["topic"])

    def test_claude_talks_on_it(self):
        from workers.topics import TOPIC_AGENT, route_agent
        self.assertEqual("claude", TOPIC_AGENT["conference"])
        self.assertEqual("claude", route_agent("conference", ("claude", "openai", "gemini", "meta")))
        self.assertEqual("openai", route_agent("conference", ("openai", "gemini")))   # FALLBACK order

    def test_the_talk_lane_hears_the_brief(self):
        with TestClient(self.make()) as client:
            created = self.create(client, {"creation_id": "topic-conf-talk", "start": "blank",
                                           "topic": "conference"}).json()
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            client.post("/v1/session/%s/talk" % created["session_id"], headers=headers,
                        json={"text": "What are we working on today?"})
        canvas = self.talk.calls[0]["canvas"]
        self.assertTrue(canvas.startswith("The visitor picked this topic before starting: Work on the conference line"),
                        canvas[:120])
        self.assertIn("conference line itself", canvas)

    def test_its_charter_frame_is_the_seven_ids_in_order(self):
        from workers.topics import CHARTER_FRAMES, charter_frame
        self.assertEqual(CONFERENCE_FRAME, [d[0] for d in CHARTER_FRAMES["conference"]])
        self.assertEqual(CHARTER_FRAMES["conference"], charter_frame("conference"))
        for did, label, covers in charter_frame("conference"):
            self.assertTrue(label and covers, did)
        self.assertEqual(("goal", "Today's goal", "what we are working on today and what done looks like"),
                         charter_frame("conference")[0])
        from workers import charter as ch
        self.assertEqual(CONFERENCE_FRAME, ch.tool_for("conference")["input_schema"]["properties"]["dimensions"]
                         ["items"]["properties"]["id"]["enum"])
        dims = [{"id": i, "level": 1, "captured": "About " + i} for i in CONFERENCE_FRAME]
        checked, problems = ch.validate({"dimensions": dims, "next": "What does done look like today?"}, "conference")
        self.assertIsNotNone(checked, problems)
        self.assertEqual(CONFERENCE_FRAME, [d["id"] for d in checked["dimensions"]])

    def test_its_session_type_and_quote_lines(self):
        from workers.topics import DEFAULT_QUOTE_LINES, quote_lines, session_type
        self.assertEqual("Work on the conference line", session_type("conference"))
        self.assertEqual(DEFAULT_QUOTE_LINES, quote_lines("conference"))

    def test_the_architect_draws_diagrams_with_the_kinds_the_page_renders(self):
        guidance = cw.topic_guidance({"topic": "conference"})
        for needed in ('"heading"', '"process-step"', '"edge"', "model.updated", "lookup", "master-detail",
                       "many-to-many"):
            self.assertIn(needed, guidance)
        # A section's label is only an aria-label on the page; the visible title
        # of a diagram is a heading (h4), so each diagram starts with one.
        self.assertIn("FIRST child is a \"heading\" node whose label is the diagram's title", guidance)
        self.assertIn("insert heading, step, edge, step", guidance)
        self.assertNotIn("headings, forms", guidance)
        self.assertIn("process-step", cw.KINDS)
        self.assertIn("edge", cw.KINDS)
        from workers import analyst
        self.assertEqual(("lookup", "master-detail", "many-to-many"), analyst.LINK_KINDS)
        state = {"artifact": {"id": "screen", "kind": "screen", "label": "x", "children": []},
                 "questions": [], "transcript": [], "topic": "conference"}
        described = cw._describe(state, {"kind": "utterance", "text": "What are we working on today?"})
        self.assertTrue(described.startswith("The visitor picked this topic before starting: Work on the conference"),
                        described[:120])
        self.assertIn(guidance, described)
        self.assertLess(described.index(guidance), described.index("CURRENT PROTOTYPE"))

    def test_no_other_topic_gets_conference_guidance(self):
        from workers import analyst
        state = {"artifact": {"id": "screen", "kind": "screen", "label": "x", "children": []},
                 "questions": [], "transcript": []}
        self.assertEqual("", cw.topic_guidance({"topic": ["conference"]}))
        for topic in list(TOPICS) + ["", None, 7]:
            if topic == "conference":
                continue
            self.assertEqual("", cw.topic_guidance({"topic": topic}), topic)
            described = cw._describe(dict(state, topic=topic), {"kind": "utterance", "text": "a"})
            self.assertNotIn("CONFERENCE LINE", described, topic)
            self.assertNotIn("CONFERENCE LINE", analyst._describe(dict(state, topic=topic), "a", ""), topic)
            if isinstance(topic, str) and topic in TOPICS:   # every other topic's line is exactly as before
                self.assertEqual("The visitor picked this topic before starting: %s.\n" % TOPICS[topic],
                                 topic_line({"topic": topic}))

    def turn(self, topic, ops):
        from types import SimpleNamespace
        import json as _json
        draft = {"ops": ops, "confirm": "Drawn.", "questions": [], "batch_title": "",
                 "resolves": {"question_id": "", "option_id": "", "freeform_answer": ""}}
        resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=_json.dumps(draft))])
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp)))
        root = {"id": "screen", "kind": "screen", "label": "x", "children": [
            {"id": "arch", "kind": "section", "label": "Architecture", "children": [
                {"id": "arch-h", "kind": "heading", "label": "Architecture"},
                {"id": "gw", "kind": "process-step", "label": "Gateway"},
                {"id": "gw-bus", "kind": "edge", "label": "Turn request", "detail": "Gateway -> Bus"}]}]}
        state = {"artifact": root, "questions": [], "transcript": [], "session_id": "s1", "turn_seq": 1,
                 "topic": topic}
        return cw.ClaudeWorker(client=client).on_turn(state, {"kind": "utterance", "text": "draw it"})

    @staticmethod
    def insert(parent, node_id, kind, label, detail=""):
        return {"op": "insert_child", "node_id": parent, "value": "",
                "new_node": {"id": node_id, "kind": kind, "label": label, "detail": detail}}

    def test_the_gate_draws_a_titled_flow_on_the_conference_canvas(self):
        out = self.turn("conference", [
            self.insert("screen", "proc", "section", "Process: one turn"),
            self.insert("proc", "proc-h", "heading", "Process: one turn"),
            self.insert("proc", "s1", "process-step", "Owner speaks", "The owner asks a question."),
            self.insert("proc", "e1", "edge", "Utterance", "Owner speaks -> Chair picks the speaker"),
            self.insert("proc", "s2", "process-step", "Chair picks the speaker", "The chair routes the turn.")])
        self.assertEqual([], out["problems"])
        ops = out["events"][0]["payload"]["ops"]
        self.assertEqual(["section", "heading", "process-step", "edge", "process-step"],
                         [o["node"]["kind"] for o in ops])

    def test_the_conference_gate_refuses_scenes_and_children_of_steps_and_edges(self):
        cases = (
            [self.insert("screen", "sc", "scene", "Diagram", "800x400 bg=#101820")],
            [self.insert("gw", "inside", "text", "Inside a step")],
            [self.insert("gw-bus", "inside", "text", "Inside an edge")],
            # one refused op drops the whole patch, as everywhere
            [self.insert("arch", "ok", "process-step", "Bus", "Carries turns."),
             self.insert("gw", "inside", "process-step", "Nested", "")],
        )
        for ops in cases:
            out = self.turn("conference", ops)
            self.assertEqual([], out["events"], ops)
            self.assertTrue(any("whole patch is dropped" in p for p in out["problems"]), out["problems"])

    def test_a_diagram_step_needs_its_heading_first_and_a_heading_holds_nothing(self):
        # Cursor on 9fa8589: a section of only steps was accepted (no visible title), and a step under
        # the heading was accepted (the page draws no children of a heading, so it would vanish).
        cases = (
            [self.insert("screen", "proc", "section", "Process"),
             self.insert("proc", "s1", "process-step", "Owner speaks", "")],            # no heading at all
            [self.insert("screen", "proc", "section", "Process"),
             self.insert("proc", "s1", "process-step", "Owner speaks", ""),
             self.insert("proc", "proc-h", "heading", "Process")],                      # heading after the step
            [self.insert("arch-h", "under", "process-step", "Hidden", "")],             # a step under a heading
            [self.insert("arch-h", "under", "text", "Hidden")],                         # anything under a heading
            [{"op": "remove", "node_id": "arch-h", "value": ""}],                      # strip a drawn diagram's title
        )
        for ops in cases:
            out = self.turn("conference", ops)
            self.assertEqual([], out["events"], ops)
            self.assertTrue(any("whole patch is dropped" in p for p in out["problems"]), out["problems"])

    def test_a_titled_section_may_still_lose_its_steps_then_its_heading(self):
        out = self.turn("conference", [{"op": "remove", "node_id": "gw", "value": ""},
                                       {"op": "remove", "node_id": "gw-bus", "value": ""},
                                       {"op": "remove", "node_id": "arch-h", "value": ""}])
        self.assertEqual([], out["problems"])

    def test_every_other_topic_is_gated_as_before(self):
        for topic in [t for t in TOPICS if t != "conference"] + [""]:
            scene = self.turn(topic, [self.insert("screen", "sc", "scene", "Banner", "800x400 bg=#101820")])
            self.assertEqual([], scene["problems"], topic)
            nested = self.turn(topic, [self.insert("gw", "inside", "text", "Inside a step")])
            self.assertEqual([], nested["problems"], topic)
            untitled = self.turn(topic, [self.insert("screen", "proc", "section", "Process"),
                                         self.insert("proc", "s1", "process-step", "Step", "")])
            self.assertEqual([], untitled["problems"], topic)
            under = self.turn(topic, [self.insert("arch-h", "under", "text", "Under a heading")])
            self.assertEqual([], under["problems"], topic)

    def test_the_analyst_maps_the_conference_lines_own_data_model(self):
        from workers import analyst
        state = {"transcript": [], "questions": [], "topic": "conference"}
        described = analyst._describe(state, "the data model for a call", "canvas")
        self.assertTrue(described.startswith(analyst.TOPIC_GUIDANCE["conference"]), described[:120])
        for needed in ("lookup", "master-detail", "many-to-many"):
            self.assertIn(needed, described)


if __name__ == "__main__":
    unittest.main()

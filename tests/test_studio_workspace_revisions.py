"""A client's project keeps its design between visits (app/workspaces.py).

Codex Gate 3 items 7-8: every change the builder applies in a project session
is saved as the project's next revision with a receipt; reopening the project
reconstructs that revision exactly; a stale session never overwrites a newer
one; and the read-back shows the saved revision and its digest.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import tests.test_studio_clients as base  # noqa: E402  (sets sys.path; its cases are not re-run here)
from app.workspaces import WorkspaceConflict, WorkspaceStore, digest  # noqa: E402


class Revisions(base.Api):
    def utter(self, client, live, n, text, version=None):
        return client.post("/v1/session/%s/commands" % live["session_id"], headers=self.auth(live["token"]), json={
            "command_id": "cmd-%d" % n, "session_id": live["session_id"], "type": "utterance",
            "expected_version": version if version is not None else self.state(live["session_id"])["artifact_version"],
            "transcript": text, "item_id": "item-%d" % n})

    def test_each_change_is_the_next_saved_revision_with_a_receipt_and_a_read_back(self):
        with TestClient(self.make(worker=base.PatchWorker())) as client:
            token = self.sign_in(client).json()["token"]
            before = client.get("/v1/workspace/steelworks", headers=self.auth(token)).json()
            live = self.project_session(client, token).json()
            first = self.utter(client, live, 1, "make the heading bigger").json()
            second = self.utter(client, live, 2, "and again").json()
            refused = self.utter(client, live, 3, "please refuse this one").json()
            back = client.get("/v1/workspace/steelworks", headers=self.auth(token)).json()
            listed = client.get("/v1/workspace", headers=self.auth(token)).json()
        self.assertEqual({"project": "steelworks", "revision": 0, "updated_at": None, "digest": None}, before)
        self.assertEqual((True, 1), (first["workspace"]["saved"], first["workspace"]["revision"]))
        self.assertEqual((True, 2), (second["workspace"]["saved"], second["workspace"]["revision"]))
        self.assertNotIn("workspace", refused)                       # nothing applied, nothing saved
        artifact = self.state(live["session_id"])["artifact"]
        self.assertEqual(digest(artifact), second["workspace"]["digest"])
        self.assertEqual((2, digest(artifact)), (back["revision"], back["digest"]))
        self.assertEqual(2, listed["projects"][0]["revision"])
        self.assertIsNotNone(listed["projects"][0]["updated_at"])

    def test_reopening_the_project_reconstructs_the_saved_revision_exactly_without_fetching(self):
        with TestClient(self.make(worker=base.PatchWorker())) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token, creation_id="visit-1").json()
            self.utter(client, live, 1, "make the heading bigger")
            saved = self.state(live["session_id"])["artifact"]
            again = self.project_session(client, token, creation_id="visit-2").json()
        reopened = self.state(again["session_id"])
        self.assertEqual(saved, reopened["artifact"])
        snapshot = [e for e in reopened["events"] if e["type"] == "artifact.snapshot"][0]
        self.assertEqual(saved, snapshot["payload"]["root"])
        self.assertEqual(1, reopened["workspace_revision"])
        self.assertEqual(1, len(self.fetcher.calls))                 # the live page was fetched once

    def test_a_stale_session_never_overwrites_a_newer_revision(self):
        with TestClient(self.make(worker=base.PatchWorker())) as client:
            token = self.sign_in(client).json()["token"]
            one = self.project_session(client, token, creation_id="tab-1").json()
            two = self.project_session(client, token, creation_id="tab-2").json()
            self.utter(client, two, 1, "change from the second tab")
            stale = self.utter(client, one, 2, "change from the first tab").json()
            back = client.get("/v1/workspace/steelworks", headers=self.auth(token)).json()
        self.assertEqual({"saved": False, "revision": 1, "reason": "the project changed elsewhere"}, stale["workspace"])
        self.assertEqual(digest(self.state(two["session_id"])["artifact"]), back["digest"])

    def test_fresh_starts_again_from_the_live_page_and_saves_on_top(self):
        with TestClient(self.make(worker=base.PatchWorker())) as client:
            token = self.sign_in(client).json()["token"]
            live = self.project_session(client, token, creation_id="visit-1").json()
            self.utter(client, live, 1, "make the heading bigger")
            fresh = client.post("/v1/session", headers=self.auth(token), json={
                "creation_id": "visit-2", "start": "project", "project": "steelworks", "fresh": True}).json()
            self.assertEqual(2, len(self.fetcher.calls))
            self.assertNotEqual(self.state(live["session_id"])["artifact"], self.state(fresh["session_id"])["artifact"])
            saved = self.utter(client, fresh, 2, "make it bigger again").json()
        self.assertEqual((True, 2), (saved["workspace"]["saved"], saved["workspace"]["revision"]))

    def test_fresh_and_project_are_only_for_project_sessions_and_fresh_is_a_boolean(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            bad = [client.post("/v1/session", headers=self.auth(token), json=body).status_code for body in (
                {"creation_id": "b-1", "start": "blank", "fresh": True},
                {"creation_id": "b-2", "start": "project", "project": "steelworks", "fresh": "yes"})]
        self.assertEqual([400, 400], bad)

    def test_the_read_back_is_the_clients_own_project_only(self):
        with TestClient(self.make()) as client:
            token = self.sign_in(client).json()["token"]
            operator = self.sign_in(client, base.OPERATOR).json()["token"]
            other = client.get("/v1/workspace/not-mine", headers=self.auth(token))
            op = client.get("/v1/workspace/steelworks", headers=self.auth(operator))
        self.assertEqual(403, other.status_code)
        from app.main import WORKSPACE_DENIED
        self.assertEqual(WORKSPACE_DENIED, other.json()["detail"])      # the same denial as any other refusal
        self.assertIn(op.status_code, (401, 403))

    def test_operator_and_blank_sessions_save_nothing(self):
        with TestClient(self.make(worker=base.PatchWorker())) as client:
            token = self.sign_in(client).json()["token"]
            blank = client.post("/v1/session", headers=self.auth(token),
                                json={"creation_id": "new-1", "start": "blank"}).json()
            out = self.utter(client, blank, 1, "a logo").json()
        self.assertNotIn("workspace", out)
        self.assertFalse([k for k in self.store.data if k.startswith("studio_ws_")])


class Store(unittest.TestCase):
    def test_compare_and_set_on_the_revision(self):
        store = WorkspaceStore(base.MemoryStore(), clock=lambda: 1000)
        tree = {"id": "screen", "kind": "screen", "label": "x", "children": []}
        self.assertEqual(1, store.save("nav", "p1", tree, "s-1", 0, ["op-1"]))
        with self.assertRaises(WorkspaceConflict) as caught:
            store.save("nav", "p1", tree, "s-2", 0)
        self.assertEqual(1, caught.exception.stored_revision)
        self.assertEqual(2, store.save("nav", "p1", tree, "s-1", 1))
        self.assertEqual(2, store.load("nav", "p1")["revision"])
        self.assertIsNone(store.load("nav", "p2"))
        self.assertIsNone(store.load("other", "p1"))

    def test_the_key_binds_tenant_and_project(self):
        self.assertNotEqual(WorkspaceStore.name("a", "bc"), WorkspaceStore.name("ab", "c"))
        with self.assertRaises(ValueError):
            WorkspaceStore.name("", "p1")


if __name__ == "__main__":
    unittest.main()

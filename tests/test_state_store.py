#!/usr/bin/env python3
r"""Offline tests for durable fleet state, with the concurrency actually raced.

No GCS, no metadata server, no network. The GCS backend is exercised against a
fake transport that implements real generation semantics, so `ifGenerationMatch`
is tested rather than trusted.

WHAT MATTERS HERE
    Durability is the easy half. The half that has cost this fleet real incidents
    is two writers touching one cursor: two surfaces under one tag is the
    collision class behind three of them, and /listen/ and /stream/ happened
    because two agents edited one thing the same night.

    So these tests do not check that a value round-trips. They check that a
    SECOND writer holding a stale token is REFUSED, that the refusal is
    distinguishable from success, and that a cursor never moves backwards even
    when the racing writer is ahead.

Run: python3 tests/test_state_store.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from state_store import (Conflict, FileStore, GcsStore, advance,  # noqa: E402
                         open_store)

TS_OLD = "2026-09-23T10:00:00Z"
TS_NEW = "2026-09-23T20:00:00Z"
TS_NEWER = "2026-09-23T21:00:00Z"


# --------------------------------------------------------- a fake GCS, with
# --------------------------------------------------------- real generations

class FakeGcs:
    """Implements the generation semantics the store depends on, and only those.

    A fake that always accepts a write would make every test below pass while
    proving nothing, so this one enforces ifGenerationMatch exactly as GCS does:
    0 means "must not exist", anything else must equal the current generation,
    and a mismatch is HTTP 412.
    """

    def __init__(self):
        self.objects = {}        # name -> (generation:int, body:bytes)
        self.versions = {}       # (name, generation:int) -> immutable body
        self.next_generation = 1000
        self.calls = []

    def token(self):
        return ("fake-token-not-a-credential", 3600)

    def request(self, url, method="GET", body=None, ctype=None):
        import urllib.error
        import urllib.parse
        self.calls.append((method, url))
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)

        if method == "POST":                      # upload
            name = urllib.parse.unquote(q["name"][0])
            want = q["ifGenerationMatch"][0]
            have = self.objects.get(name)
            current = str(have[0]) if have else "0"
            if want != current:
                raise urllib.error.HTTPError(url, 412, "Precondition Failed",
                                             {}, None)
            self.next_generation += 1
            self.objects[name] = (self.next_generation, body)
            self.versions[(name, self.next_generation)] = body
            return 200, json.dumps({"generation": str(self.next_generation)
                                    }).encode()

        # GET: either ?alt=media for the body or metadata for the generation
        obj = urllib.parse.unquote(parsed.path.rsplit("/o/", 1)[-1])
        if obj not in self.objects:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        generation, stored = self.objects[obj]
        if q.get("alt") == ["media"]:
            requested = int(q.get("generation", [generation])[0])
            version = self.versions.get((obj, requested))
            if version is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return 200, version
        return 200, json.dumps({"generation": str(generation)}).encode()


def fake_store(fake: FakeGcs) -> GcsStore:
    s = GcsStore("sfdc24-fleet-state", "wakers", token_provider=fake.token)
    s._request = fake.request                     # noqa: SLF001 - that is the seam
    return s


class TheGcsBackendRoundTrips(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGcs()
        self.store = fake_store(self.fake)

    def test_a_missing_object_reads_as_empty_with_a_None_token(self):
        state, token = self.store.load("board_waker")
        self.assertEqual(state, {})
        self.assertIsNone(token, "None is what says 'create me, do not update'")

    def test_a_first_write_then_a_read_returns_the_value(self):
        self.store.save("board_waker", {"watermark": TS_NEW}, None)
        state, token = self.store.load("board_waker")
        self.assertEqual(state["watermark"], TS_NEW)
        self.assertIsNotNone(token)

    def test_an_update_with_the_current_token_is_accepted(self):
        self.store.save("board_waker", {"watermark": TS_NEW}, None)
        _, token = self.store.load("board_waker")
        self.store.save("board_waker", {"watermark": TS_NEWER}, token)
        state, _ = self.store.load("board_waker")
        self.assertEqual(state["watermark"], TS_NEWER)

    def test_body_is_bound_to_the_metadata_generation_during_a_race(self):
        self.store.save("admission", {"count": 18}, None)
        original = self.fake.request
        injected = {"done": False}

        def racing_request(url, method="GET", body=None, ctype=None):
            import urllib.parse
            result = original(url, method=method, body=body, ctype=ctype)
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            if method == "GET" and "alt" not in query and not injected["done"]:
                injected["done"] = True
                name = urllib.parse.unquote(parsed.path.rsplit("/o/", 1)[-1])
                generation = self.fake.objects[name][0]
                upload = (
                    "https://storage.googleapis.com/upload/storage/v1/b/fake/o"
                    "?uploadType=media&name=%s&ifGenerationMatch=%s"
                    % (urllib.parse.quote(name, safe=""), generation)
                )
                original(upload, method="POST", body=b'{"count": 19}', ctype="application/json")
            return result

        self.store._request = racing_request  # noqa: SLF001 - deterministic race seam
        state, stale_token = self.store.load("admission")
        self.assertEqual(18, state["count"])
        with self.assertRaises(Conflict):
            self.store.save("admission", {"count": 19}, stale_token)
        self.store._request = original  # noqa: SLF001
        current, _ = self.store.load("admission")
        self.assertEqual(19, current["count"])

    def test_the_object_name_is_namespaced_by_prefix(self):
        self.store.save("board_waker", {"a": 1}, None)
        self.assertIn("wakers/board_waker.json", self.fake.objects)

    def test_a_name_with_path_characters_cannot_escape_the_prefix(self):
        """Assert the property, not the exact mangled spelling.

        The first version of this test hard-coded the expected string and got the
        dot count wrong - it claimed six where "../../etc/passwd" yields four.
        What matters is that the object cannot leave the prefix, so that is what
        is checked.
        """
        for hostile in ("../../etc/passwd", "/absolute", "a/b/c",
                        r"..\..\windows", "wakers/../secrets"):
            with self.subTest(name=hostile):
                self.fake.objects.clear()
                self.store.save(hostile, {"a": 1}, None)
                landed = list(self.fake.objects)[0]
                self.assertTrue(landed.startswith("wakers/"),
                                "%r escaped the prefix as %r" % (hostile, landed))
                self.assertEqual(landed.count("/"), 1,
                                 "%r kept a path separator: %r" % (hostile, landed))
                self.assertTrue(landed.endswith(".json"))

    def test_a_name_with_nothing_usable_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.load("///")


class TwoWritersRaceForOneCursor(unittest.TestCase):
    """The half that has actually cost this fleet incidents."""

    def setUp(self):
        self.fake = FakeGcs()
        self.a = fake_store(self.fake)
        self.b = fake_store(self.fake)

    def test_the_second_writer_is_refused_not_silently_merged(self):
        self.a.save("w", {"watermark": TS_OLD}, None)
        _, token_a = self.a.load("w")
        _, token_b = self.b.load("w")            # both hold the same generation

        self.a.save("w", {"watermark": TS_NEW}, token_a)          # A wins
        with self.assertRaises(Conflict):
            self.b.save("w", {"watermark": TS_NEWER}, token_b)   # B must lose

    def test_the_loser_does_not_erase_the_winner(self):
        self.a.save("w", {"watermark": TS_OLD}, None)
        _, token_b = self.b.load("w")
        _, token_a = self.a.load("w")
        self.a.save("w", {"watermark": TS_NEW}, token_a)
        try:
            self.b.save("w", {"watermark": "2026-01-01T00:00:00Z"}, token_b)
        except Conflict:
            pass
        state, _ = self.a.load("w")
        self.assertEqual(state["watermark"], TS_NEW,
                         "the losing write reached the object anyway")

    def test_two_first_writes_cannot_both_succeed(self):
        # Both see "does not exist" and both pass token None. Exactly one may win.
        self.a.save("w", {"who": "a"}, None)
        with self.assertRaises(Conflict):
            self.b.save("w", {"who": "b"}, None)

    def test_a_conflict_is_a_distinct_exception_not_a_falsy_return(self):
        # A save that returned False on conflict would be ignorable by accident.
        self.a.save("w", {"x": 1}, None)
        _, stale = self.a.load("w")
        self.a.save("w", {"x": 2}, stale)
        _, current = self.a.load("w")
        self.assertNotEqual(stale, current)
        with self.assertRaises(Conflict):
            self.a.save("w", {"x": 3}, stale)


class AdvanceNeverGoesBackwards(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGcs()
        self.store = fake_store(self.fake)

    def test_a_forward_move_is_applied(self):
        r = advance(self.store, "w", "watermark", TS_NEW)
        self.assertTrue(r["changed"])
        self.assertEqual(self.store.load("w")[0]["watermark"], TS_NEW)

    def test_an_older_value_is_refused_without_an_error(self):
        advance(self.store, "w", "watermark", TS_NEW)
        r = advance(self.store, "w", "watermark", TS_OLD)
        self.assertFalse(r["changed"])
        self.assertEqual(r["value"], TS_NEW)
        self.assertEqual(self.store.load("w")[0]["watermark"], TS_NEW,
                         "the cursor moved backwards")

    def test_the_same_value_twice_is_not_a_change(self):
        advance(self.store, "w", "watermark", TS_NEW)
        r = advance(self.store, "w", "watermark", TS_NEW)
        self.assertFalse(r["changed"])

    def test_a_race_resolves_forward_and_retries_rather_than_failing(self):
        """The realistic case: a cloud run and a laptop run land together."""
        other = fake_store(self.fake)
        advance(self.store, "w", "watermark", TS_OLD)

        real_load = self.store.load
        interfered = {"done": False}

        def load_then_let_the_other_writer_in(name):
            state, token = real_load(name)
            if not interfered["done"]:
                interfered["done"] = True
                # Someone else advances between our load and our save.
                advance(other, name, "watermark", TS_NEWER)
            return state, token

        with mock.patch.object(self.store, "load",
                               side_effect=load_then_let_the_other_writer_in):
            r = advance(self.store, "w", "watermark", TS_NEW)

        # We lost the CAS, re-read, and found the other writer already ahead of
        # the value we were trying to set - so the right answer is "not newer",
        # NOT to drag the cursor back to ours.
        self.assertFalse(r["changed"])
        self.assertEqual(self.store.load("w")[0]["watermark"], TS_NEWER)

    def test_other_keys_in_the_document_survive_an_advance(self):
        self.store.save("w", {"watermark": TS_OLD, "wa_acked": ["WRK-1"]}, None)
        advance(self.store, "w", "watermark", TS_NEW)
        state, _ = self.store.load("w")
        self.assertEqual(state["wa_acked"], ["WRK-1"],
                         "an advance dropped unrelated state")


class CorruptStateIsNotEmptyState(unittest.TestCase):
    def test_gcs_unparseable_json_refuses_rather_than_restarting_the_board(self):
        fake = FakeGcs()
        store = fake_store(fake)
        fake.objects["wakers/w.json"] = (1001, b"{ this is not json")
        with self.assertRaises(Conflict):
            store.load("w")

    def test_file_unparseable_json_refuses_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileStore(Path(tmp))
            (Path(tmp) / "w.json").write_text("{ nope", encoding="utf-8")
            with self.assertRaises(Conflict):
                store.load("w")


class TheFileBackendIsTheLaptopPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FileStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        self.store.save("w", {"watermark": TS_NEW}, None)
        state, token = self.store.load("w")
        self.assertEqual(state["watermark"], TS_NEW)
        self.assertIsNotNone(token)

    def test_a_stale_token_is_refused(self):
        self.store.save("w", {"x": 1}, None)
        _, stale = self.store.load("w")
        self.store.save("w", {"x": 2}, stale)
        with self.assertRaises(Conflict):
            self.store.save("w", {"x": 3}, stale)

    def test_advance_works_the_same_way_on_a_file(self):
        self.assertTrue(advance(self.store, "w", "watermark", TS_NEW)["changed"])
        self.assertFalse(advance(self.store, "w", "watermark", TS_OLD)["changed"])

    def test_no_temp_file_is_left_behind(self):
        self.store.save("w", {"x": 1}, None)
        leftovers = [p.name for p in Path(self.tmp.name).iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TheUriChoosesTheBackend(unittest.TestCase):
    def test_a_gs_uri_selects_gcs(self):
        s = open_store("gs://sfdc24-fleet-state/wakers",
                       token_provider=lambda: ("t", 3600))
        self.assertIsInstance(s, GcsStore)
        self.assertEqual(s.describe(), "gs://sfdc24-fleet-state/wakers")

    def test_a_bucket_with_no_prefix_is_allowed(self):
        s = open_store("gs://sfdc24-fleet-state", token_provider=lambda: ("t", 1))
        self.assertEqual(s.bucket, "sfdc24-fleet-state")
        self.assertEqual(s.prefix, "")

    def test_a_gs_uri_with_no_bucket_is_refused(self):
        with self.assertRaises(ValueError):
            open_store("gs:///wakers")

    def test_no_uri_falls_back_to_a_file_and_never_to_the_cloud(self):
        # The default must not reach for a network. A workstation with no
        # BLACKBOARD_STATE_URI keeps working exactly as it does today.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"BLACKBOARD_STATE_DIR": tmp},
                                 clear=True):
                s = open_store()
        self.assertIsInstance(s, FileStore)

    def test_the_env_var_is_honoured(self):
        with mock.patch.dict(os.environ,
                             {"BLACKBOARD_STATE_URI": "gs://b/p"}, clear=True):
            s = open_store(token_provider=lambda: ("t", 1))
        self.assertIsInstance(s, GcsStore)

    def test_no_third_party_import(self):
        src = (REPO / "scripts" / "state_store.py").read_text(encoding="utf-8")
        for banned in ("google.cloud", "import requests", "from google",
                       "import boto3"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    os.chdir(REPO)
    unittest.main(verbosity=2)

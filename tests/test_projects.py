"""scripts/projects.py: the room's projects, its current one, the project stamped on an ask, and voice switching.

What matters (conference docs/CARDS-ARCHITECTURE.md R6.1-R6.3): malformed state is no project, never a guess; only an
active project can be current; voice switches only among a small list of distinct active names, by whole name, and
never creates one. No network.
"""
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import projects as pj  # noqa: E402

ROOM = "workshop"


class FakeRedis:
    def __init__(self):
        self.kv, self.hashes, self.ttls = {}, {}, {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hsetnx(self, key, field, value):
        if field in self.hashes.get(key, {}):
            return 0
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0


NOW = datetime.datetime(2026, 11, 4, 15, 29, 0, tzinfo=datetime.timezone.utc)


def add(conn, pid, name, status="active"):
    conn.hset(pj.PROJECTS_KEY % ROOM, pid, json.dumps({"name": name, "status": status, "created": "2026-11-04"}))


class Store(unittest.TestCase):
    def setUp(self):
        self.conn = FakeRedis()

    def test_malformed_entries_are_left_out(self):
        add(self.conn, "acme", "Acme")
        add(self.conn, "Bad Id", "Bad")
        add(self.conn, "x", "<script>")
        add(self.conn, "y", "Why", status="deleted")
        self.conn.hset(pj.PROJECTS_KEY % ROOM, "z", "not json")
        self.assertEqual(["acme"], list(pj.read_projects(self.conn, ROOM)))

    def test_bytes_from_redis_are_read(self):
        self.conn.hset(pj.PROJECTS_KEY % ROOM, b"acme", json.dumps({"name": "Acme", "status": "active"}).encode())
        self.conn.set(pj.CURRENT_KEY % ROOM, b"acme")
        self.assertEqual(("acme", "Acme"), pj.current(self.conn, ROOM))

    def test_current_needs_an_existing_active_project(self):
        self.assertIsNone(pj.current(self.conn, ROOM), "no key: no project")
        self.conn.set(pj.CURRENT_KEY % ROOM, "ghost")
        self.assertIsNone(pj.current(self.conn, ROOM), "unknown id")
        add(self.conn, "ghost", "Ghost", status="archived")
        self.assertIsNone(pj.current(self.conn, ROOM), "archived")
        add(self.conn, "ghost", "Ghost")
        self.assertEqual(("ghost", "Ghost"), pj.current(self.conn, ROOM))

    def test_set_current_refuses_unknown_and_archived(self):
        add(self.conn, "acme", "Acme")
        add(self.conn, "old", "Old", status="archived")
        self.assertFalse(pj.set_current(self.conn, ROOM, "nope"))
        self.assertFalse(pj.set_current(self.conn, ROOM, "old"))
        self.assertIsNone(self.conn.get(pj.CURRENT_KEY % ROOM))
        self.assertTrue(pj.set_current(self.conn, ROOM, "acme"))
        self.assertEqual("acme", self.conn.get(pj.CURRENT_KEY % ROOM))

    def test_slug(self):
        self.assertEqual("acme-rollout", pj.slug("Acme Rollout"))
        self.assertEqual("q4-plan", pj.slug("  Q4  plan!! "))


class Stamp(unittest.TestCase):
    def setUp(self):
        self.conn = FakeRedis()

    def test_an_ask_carries_the_project_current_at_claim_time_and_expires_with_it(self):
        add(self.conn, "acme", "Acme")
        pj.set_current(self.conn, ROOM, "acme")
        self.assertEqual("acme", pj.stamp(self.conn, "call-0123456789ab", ROOM))
        self.assertNotIn(pj.STAMP_KEY % "call-0123456789ab", self.conn.ttls, "no expiry: it lives as the task does")
        add(self.conn, "beta", "Beta")
        pj.set_current(self.conn, ROOM, "beta")
        self.assertEqual("acme", pj.stamped(self.conn, "call-0123456789ab"), "switching later does not move it")

    def test_no_current_project_is_recorded_as_none_and_stays_none(self):
        self.assertEqual("", pj.stamp(self.conn, "call-0123456789ab", ROOM))
        self.assertEqual("", self.conn.kv[pj.STAMP_KEY % "call-0123456789ab"])
        add(self.conn, "acme", "Acme")
        pj.set_current(self.conn, ROOM, "acme")
        self.assertEqual("", pj.stamp(self.conn, "call-0123456789ab", ROOM), "a replay gives it no project")

    def test_a_replay_after_a_switch_keeps_the_first_project(self):
        """Codex's HOLD on d519e34: the stamp is written once and never moved."""
        add(self.conn, "acme", "Acme")
        add(self.conn, "beta", "Beta")
        pj.set_current(self.conn, ROOM, "acme")
        self.assertEqual("acme", pj.stamp(self.conn, "call-0123456789ab", ROOM))
        pj.set_current(self.conn, ROOM, "beta")
        self.assertEqual("acme", pj.stamp(self.conn, "call-0123456789ab", ROOM))
        self.assertEqual("acme", pj.stamped(self.conn, "call-0123456789ab"))

    def test_a_malformed_stamp_is_no_project(self):
        self.conn.set(pj.STAMP_KEY % "call-x", "Bad Id!")
        self.assertEqual("", pj.stamped(self.conn, "call-x"))


class VoiceSwitch(unittest.TestCase):
    PROJECTS = {"acme-rollout": {"name": "Acme Rollout", "status": "active", "created": ""},
                "htv": {"name": "HTV", "status": "active", "created": ""},
                "old": {"name": "Old Site", "status": "archived", "created": ""}}

    def test_switch_phrases_name_an_active_project_by_whole_name(self):
        for said in ("Switch to the Acme Rollout project.", "Let's work on project Acme Rollout",
                     "Okay, go to project acme rollout!", "Switch over to the Acme Rollout project"):
            self.assertEqual("acme-rollout", pj.switch_target(said, self.PROJECTS), said)
        self.assertEqual("htv", pj.switch_target("Open the HTV project", self.PROJECTS))

    def test_no_project_word_no_partial_name_no_archived(self):
        for said in ("Switch to Acme Rollout.", "Switch to the Acme project.", "Switch to the Old Site project.",
                     "Grok, switch to the logo card.", "Move to the next item.", "Switch to the Nova project."):
            self.assertIsNone(pj.switch_target(said, self.PROJECTS), said)

    def test_a_negated_switch_switches_nothing(self):
        for said in ("Don't switch to the Acme Rollout project.", "No need to open project Acme Rollout",
                     "Never go to project Acme Rollout", "We won't switch to the Acme Rollout project"):
            self.assertIsNone(pj.switch_target(said, self.PROJECTS), said)
        self.assertEqual("acme-rollout", pj.switch_target("Okay, switch to the Acme Rollout project", self.PROJECTS))

    def test_ambiguous_or_too_many_projects_switch_nothing(self):
        twins = {"a": {"name": "Acme", "status": "active", "created": ""},
                 "b": {"name": "ACME", "status": "active", "created": ""}}
        self.assertIsNone(pj.switch_target("Switch to the Acme project", twins), "names not distinct")
        many = {"p%d" % i: {"name": "P%d" % i, "status": "active", "created": ""} for i in range(pj.VOICE_MAX + 1)}
        self.assertIsNone(pj.switch_target("Switch to the P1 project", many), "too many for voice")

    def test_voice_never_creates(self):
        self.assertIsNone(pj.switch_target("Switch to the Brand New project", {}))


class Console(unittest.TestCase):
    """R6b: the owner console's create, archive and clear on the store. Deterministic refusals, never a rename."""

    def setUp(self):
        self.conn = FakeRedis()

    def test_create_writes_an_active_project_under_its_slug_in_the_shape_the_store_reads(self):
        self.assertEqual("acme-rollout", pj.create(self.conn, ROOM, "Acme Rollout", NOW))
        self.assertEqual({"acme-rollout": {"name": "Acme Rollout", "status": "active", "created": "2026-11-04T15:29:00Z"}},
                         pj.read_projects(self.conn, ROOM))
        self.assertIsNone(pj.current(self.conn, ROOM), "created, not yet current: the bridge switches to it")
        self.assertTrue(pj.set_current(self.conn, ROOM, "acme-rollout"))

    def test_created_is_iso_utc_whatever_zone_the_clock_is_in(self):
        eastern = NOW.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
        pj.create(self.conn, ROOM, "Acme", eastern)
        self.assertEqual("2026-11-04T15:29:00Z", pj.read_projects(self.conn, ROOM)["acme"]["created"])

    def test_a_taken_id_refuses_the_create_in_any_status(self):
        add(self.conn, "acme", "Acme", status="archived")
        self.assertIsNone(pj.create(self.conn, ROOM, "ACME", NOW), "the same slug, archived: not revived, not renamed")
        self.assertEqual("archived", pj.read_projects(self.conn, ROOM)["acme"]["status"])
        self.assertEqual("Acme", pj.read_projects(self.conn, ROOM)["acme"]["name"])
        add(self.conn, "beta", "Beta")
        self.assertIsNone(pj.create(self.conn, ROOM, "Beta", NOW), "active: a duplicate")
        self.conn.hset(pj.PROJECTS_KEY % ROOM, "gamma", "not json")
        self.assertIsNone(pj.create(self.conn, ROOM, "Gamma", NOW), "a malformed entry still owns its id")
        self.assertEqual("not json", self.conn.hgetall(pj.PROJECTS_KEY % ROOM)["gamma"], "and is left as it was")

    def test_an_invalid_name_writes_nothing(self):
        for bad in ("", " ", "<script>", "x" * 41, "-dash first", "tab\there", "two|parts", None, 7):
            self.assertIsNone(pj.create(self.conn, ROOM, bad, NOW), repr(bad))
        self.assertEqual({}, self.conn.hashes)

    def test_archive_retires_an_active_project_and_clears_it_when_current(self):
        add(self.conn, "acme", "Acme")
        add(self.conn, "beta", "Beta")
        pj.set_current(self.conn, ROOM, "acme")
        self.assertTrue(pj.archive(self.conn, ROOM, "acme"))
        self.assertEqual("archived", pj.read_projects(self.conn, ROOM)["acme"]["status"])
        self.assertIsNone(self.conn.get(pj.CURRENT_KEY % ROOM), "it was current: now no project is")
        self.assertIsNone(pj.current(self.conn, ROOM))
        self.assertFalse(pj.set_current(self.conn, ROOM, "acme"), "an archived project cannot come back")
        pj.set_current(self.conn, ROOM, "beta")
        self.assertFalse(pj.archive(self.conn, ROOM, "acme"), "already archived")
        self.assertFalse(pj.archive(self.conn, ROOM, "nope"), "unknown")
        self.assertFalse(pj.archive(self.conn, ROOM, "Bad Id"), "malformed")
        self.assertEqual("beta", self.conn.get(pj.CURRENT_KEY % ROOM), "another project's archive leaves current alone")

    def test_archive_keeps_the_name_and_created_fields(self):
        add(self.conn, "acme", "Acme")
        pj.archive(self.conn, ROOM, "acme")
        self.assertEqual({"name": "Acme", "status": "archived", "created": "2026-11-04"},
                         pj.read_projects(self.conn, ROOM)["acme"])

    def test_clear_current_says_whether_one_was_set(self):
        self.assertFalse(pj.clear_current(self.conn, ROOM), "none set: a no-op")
        add(self.conn, "acme", "Acme")
        pj.set_current(self.conn, ROOM, "acme")
        self.assertTrue(pj.clear_current(self.conn, ROOM))
        self.assertIsNone(self.conn.get(pj.CURRENT_KEY % ROOM))
        self.assertEqual("active", pj.read_projects(self.conn, ROOM)["acme"]["status"], "the project itself stays")


if __name__ == "__main__":
    unittest.main()

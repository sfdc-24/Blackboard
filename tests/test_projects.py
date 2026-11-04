"""scripts/projects.py: the room's projects, its current one, the project stamped on an ask, and voice switching.

What matters (conference docs/CARDS-ARCHITECTURE.md R6.1-R6.3): malformed state is no project, never a guess; only an
active project can be current; voice switches only among a small list of distinct active names, by whole name, and
never creates one. No network.
"""
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

    def set(self, key, value, ex=None):
        self.kv[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value


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
        self.assertEqual("acme", pj.stamp(self.conn, "call-0123456789ab", ROOM, 7200))
        self.assertEqual(7200, self.conn.ttls[pj.STAMP_KEY % "call-0123456789ab"])
        add(self.conn, "beta", "Beta")
        pj.set_current(self.conn, ROOM, "beta")
        self.assertEqual("acme", pj.stamped(self.conn, "call-0123456789ab"), "switching later does not move it")

    def test_no_current_project_stamps_nothing(self):
        self.assertEqual("", pj.stamp(self.conn, "call-0123456789ab", ROOM, 7200))
        self.assertNotIn(pj.STAMP_KEY % "call-0123456789ab", self.conn.kv)
        self.assertEqual("", pj.stamped(self.conn, "call-0123456789ab"))

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


if __name__ == "__main__":
    unittest.main()

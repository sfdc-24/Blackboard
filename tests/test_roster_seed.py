"""scripts/roster_seed.py: the copy converges on the file, and verify() can actually say no.

Copilot raised both of these on PR 323 and neither had a test, which is why they were possible:

    THE SEED COULD NOT CONVERGE. Seeding only ever added. An instance removed or renamed in the
    authoritative file left its membership and its hash in Redis, every later verify reported that
    id as drift, and the documented seeding operation could not bring the copy back into agreement.
    The one job a seeder has.

    THE VERIFY COULD ONLY CONFIRM. A missing instance hash was silently skipped, and the family
    hashes, the alias table and the seed metadata were never compared at all - so a half-written
    copy produced an empty drift list and the CLI printed "the copy matches the file".

Redis is a volatile COPY here and the repository is the original, which is what makes a converging
seed the whole safety story: if the copy can be rebuilt exactly, losing it costs nothing.

No network, no redis library: the fake below is the only store.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import roster_seed as rs                                                 # noqa: E402


class FakeRedis:
    """Hashes and one set. Enough to be wrong in the ways that mattered."""

    def __init__(self):
        self.hashes, self.sets = {}, {}

    def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)

    def delete(self, key):
        self.hashes.pop(key, None)

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))


def roster(ids=("alpha", "beta"), family="claude"):
    """A minimal roster in the real file's shape."""
    return {
        "version": "v1",
        "updated": "2026-10-06",
        "families": [{
            "family": family,
            "name": "Claude",
            "lead_role": "control",
            "responsibility": "the copy",
            "appointed": "2026-10-06",
            "instances": [{"id": i, "surface": "s", "runtime": "r", "gcp_identity": "none",
                           "state": "active", "in_vpc": False, "redis_read": False,
                           "redis_write": False, "why": "because", "wake": "NONE"} for i in ids],
        }],
    }


class TheSeedConverges(unittest.TestCase):
    def test_a_fresh_seed_then_verify_reports_no_drift(self):
        conn, data = FakeRedis(), roster()
        rs.seed(conn, data)
        self.assertEqual([], rs.verify(conn, data))

    def test_a_removed_instance_is_removed_from_the_copy(self):
        """THE BLOCKER. Seed two, drop one from the file, seed again: the dropped one must be gone,
        and verify must then report nothing. Before, it reported that id as drift forever."""
        conn = FakeRedis()
        rs.seed(conn, roster(("alpha", "beta")))
        smaller = roster(("alpha",))
        counts = rs.seed(conn, smaller)
        self.assertEqual(1, counts["removed_instances"])
        self.assertNotIn("beta", conn.smembers(rs.ROSTER_SET))
        self.assertEqual({}, conn.hgetall(rs.AGENT_KEY % "beta"))
        self.assertEqual([], rs.verify(conn, smaller), "the seed could not converge")

    def test_a_renamed_instance_leaves_nothing_behind(self):
        conn = FakeRedis()
        rs.seed(conn, roster(("alpha", "beta")))
        renamed = roster(("alpha", "gamma"))
        rs.seed(conn, renamed)
        self.assertEqual({"alpha", "gamma"}, conn.smembers(rs.ROSTER_SET))
        self.assertEqual([], rs.verify(conn, renamed))

    def test_a_stale_alias_is_removed(self):
        conn = FakeRedis()
        rs.seed(conn, roster(("alpha", "beta")))
        self.assertIn("beta", conn.hgetall(rs.CANON))
        rs.seed(conn, roster(("alpha",)))
        self.assertNotIn("beta", conn.hgetall(rs.CANON))

    def test_a_dropped_field_does_not_survive_a_reseed(self):
        """Each hash is REPLACED, not merged: a field that leaves the file leaves the copy."""
        conn, data = FakeRedis(), roster(("alpha",))
        rs.seed(conn, data)
        conn.hset(rs.AGENT_KEY % "alpha", mapping={"invented_field": "x"})
        self.assertIn("invented_field", conn.hgetall(rs.AGENT_KEY % "alpha"))
        rs.seed(conn, data)
        self.assertNotIn("invented_field", conn.hgetall(rs.AGENT_KEY % "alpha"))

    def test_the_seed_does_not_touch_unrelated_keys(self):
        """It removes only what it manages. Somebody else's key under v1: is not its business."""
        conn, data = FakeRedis(), roster(("alpha",))
        conn.hset("v1:bus:row:R-1", mapping={"row_id": "R-1"})
        rs.seed(conn, roster(("alpha", "beta")))
        rs.seed(conn, data)
        self.assertEqual({"row_id": "R-1"}, conn.hgetall("v1:bus:row:R-1"))


class TheVerifyCanSayNo(unittest.TestCase):
    def test_a_missing_instance_hash_is_reported_not_skipped(self):
        """THE BLOCKER. An id in the membership set whose hash is absent is exactly the shape of a
        half-written copy, and it used to be skipped - producing an empty drift list."""
        conn, data = FakeRedis(), roster(("alpha", "beta"))
        rs.seed(conn, data)
        conn.delete(rs.AGENT_KEY % "beta")
        drift = rs.verify(conn, data)
        self.assertTrue(any("missing from Redis entirely" in d and "beta" in d for d in drift), drift)

    def test_a_corrupted_family_hash_is_reported(self):
        conn, data = FakeRedis(), roster()
        rs.seed(conn, data)
        conn.hset(rs.FAMILY_KEY % "claude", mapping={"role": "something else"})
        self.assertTrue(any("family:claude" in d and "role" in d for d in rs.verify(conn, data)))

    def test_a_corrupted_alias_table_is_reported(self):
        conn, data = FakeRedis(), roster()
        rs.seed(conn, data)
        conn.hset(rs.CANON, mapping={"alpha": "somebody-else"})
        self.assertTrue(any("canon" in d for d in rs.verify(conn, data)))

    def test_missing_seed_metadata_is_itself_drift(self):
        conn, data = FakeRedis(), roster()
        rs.seed(conn, data)
        conn.delete(rs.SEEDED)
        self.assertTrue(any(rs.SEEDED in d for d in rs.verify(conn, data)))

    def test_an_extra_field_in_redis_is_reported(self):
        conn, data = FakeRedis(), roster(("alpha",))
        rs.seed(conn, data)
        conn.hset(rs.AGENT_KEY % "alpha", mapping={"left_over": "y"})
        self.assertTrue(any("left_over" in d for d in rs.verify(conn, data)))

    def test_an_empty_store_is_all_drift_and_never_agreement(self):
        conn, data = FakeRedis(), roster()
        drift = rs.verify(conn, data)
        self.assertTrue(drift, "an empty copy must never verify clean")


class TheFileIsTheOriginal(unittest.TestCase):
    def test_seed_and_verify_are_described_once(self):
        """verify() walks the same expected() that seed() writes, so the two cannot disagree about
        what the copy should contain - which is how the family hashes went unchecked."""
        data = roster()
        want = rs.expected(data)
        self.assertIn(rs.CANON, want["hashes"])
        self.assertIn(rs.SEEDED, want["hashes"])
        self.assertIn(rs.FAMILY_KEY % "claude", want["hashes"])
        self.assertEqual({"alpha", "beta"}, want["members"])

    def test_there_is_no_verb_that_writes_the_file_from_redis(self):
        """One-way by construction: Redis is volatile and holds a COPY. A path that rebuilt the file
        from the copy would make the copy the only authority at the moment it is least trustworthy."""
        source = (Path(__file__).resolve().parents[1] / "scripts" / "roster_seed.py").read_text(
            encoding="utf-8")
        self.assertNotIn("write_text", source)

    def test_the_real_roster_passes_its_own_file_check(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "scripts"
                           / "agent_roster.json").read_text(encoding="utf-8"))
        self.assertEqual([], rs.check(data))


if __name__ == "__main__":
    unittest.main(verbosity=2)

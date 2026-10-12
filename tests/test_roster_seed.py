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
import copy
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


# --- capability observations: one cap: field beside the fifteen, by a path of its own -----------
#
# The owner asked on 2026-10-12 for a map of who can do what, for routing. Aya's conditions for
# putting it in the roster: every existing field, identity, entry and index preserved; an
# observation never a grant; verified, reported and unknown told apart, with the identity, the
# event and both times; stale evidence visible; a narrow write that cannot put a stale roster
# back; exact read-back. Each class below is one of those, written as the failure it prevents.

STAMP = "2026-10-12T13:10:21Z"
SITE = "github:example/site"


def right(can="yes", status="verified", source="gh api: permissions.push true", evidence=STAMP, checked=STAMP):
    return {"can": can, "status": status, "source_event": source, "evidence_at": evidence, "checked_at": checked}


def unknown(checked=STAMP, source="nobody looked"):
    return {"can": "unknown", "status": "unknown", "source_event": source, "evidence_at": "", "checked_at": checked}


def observation(rid="alpha", resource=SITE, **rights):
    states = {name: unknown() for name in rs.RIGHTS}
    states.update(rights)
    return {"id": rid, "resource": resource, "runtime_identity": "login example-bot", "rights": states}


def capfile(*observations):
    return {"version": "v1", "updated": "2026-10-12", "observations": list(observations)}


class NarrowRedis(FakeRedis):
    """A seeded copy that RECORDS every write and REFUSES the commands the capability path must never
    send. A fake that merely allowed them would let a delete slip in behind a passing test."""

    def __init__(self, data=None):
        super().__init__()
        seeded = FakeRedis()
        rs.seed(seeded, data or roster())
        self.hashes, self.sets = copy.deepcopy(seeded.hashes), copy.deepcopy(seeded.sets)
        self.writes = []

    def hset(self, key, mapping=None):
        self.writes.append((key, tuple(sorted(mapping or {}))))
        super().hset(key, mapping=mapping)

    def delete(self, key):
        raise AssertionError("DEL %s sent by the capability path" % key)

    def hdel(self, key, field):
        raise AssertionError("HDEL %s %s sent by the capability path" % (key, field))

    def sadd(self, key, member):
        raise AssertionError("SADD %s sent by the capability path" % key)

    def srem(self, key, member):
        raise AssertionError("SREM %s sent by the capability path" % key)


class TheCapabilityPathIsNarrow(unittest.TestCase):
    def test_one_field_is_written_and_everything_else_is_byte_for_byte_the_same(self):
        conn = NarrowRedis()
        before_hashes, before_sets = copy.deepcopy(conn.hashes), copy.deepcopy(conn.sets)
        obs = observation("alpha", read=right(), push=right(can="no", status="reported", source="its adapter says so"))
        out = rs.put_capabilities(conn, capfile(obs))
        self.assertEqual({"written": 1, "refused": [], "changed_elsewhere": [], "not_read_back": []}, out)
        key, field = rs.AGENT_KEY % "alpha", rs.CAP_PREFIX + SITE
        self.assertEqual([(key, (field,))], conn.writes, "one HSET, of one field, on one existing key")
        self.assertEqual(rs.cap_value(obs), conn.hashes[key][field])
        after = copy.deepcopy(conn.hashes)
        del after[key][field]
        self.assertEqual(before_hashes, after, "the fifteen fields, the family, canon and seeded are untouched")
        self.assertEqual(before_sets, conn.sets, "the membership set is untouched")
        self.assertEqual(15, len(rs.FIELDS))
        self.assertEqual(set(rs.FIELDS), set(before_hashes[key]), "and all fifteen are still there")

    def test_an_id_that_is_not_in_the_roster_in_redis_means_nothing_at_all_is_written(self):
        conn = NarrowRedis()
        before = copy.deepcopy(conn.hashes)
        out = rs.put_capabilities(conn, capfile(observation("alpha", read=right()), observation("ghost", read=right())))
        self.assertEqual((0, ["ghost"]), (out["written"], out["refused"]))
        self.assertEqual([], conn.writes, "not even the observation whose id is fine")
        self.assertEqual(before, conn.hashes)
        self.assertNotIn(rs.AGENT_KEY % "ghost", conn.hashes, "this path creates no instance")

    def test_a_second_write_replaces_its_own_field_and_another_resource_adds_its_own(self):
        conn = NarrowRedis()
        rs.put_capabilities(conn, capfile(observation("alpha", read=right())))
        newer = observation("alpha", read=right(can="no", status="reported", source="said on the board"))
        other = observation("alpha", resource="gcp:run/roster-seed", deploy=right())
        out = rs.put_capabilities(conn, capfile(newer, other))
        self.assertEqual((2, [], []), (out["written"], out["changed_elsewhere"], out["not_read_back"]))
        stored = conn.hashes[rs.AGENT_KEY % "alpha"]
        self.assertEqual(rs.cap_value(newer), stored[rs.CAP_PREFIX + SITE])
        self.assertEqual(rs.cap_value(other), stored[rs.CAP_PREFIX + "gcp:run/roster-seed"])
        self.assertEqual(15 + 2, len(stored))

    def test_a_field_that_does_not_read_back_as_written_is_a_failure(self):
        class Lossy(NarrowRedis):
            def hset(self, key, mapping=None):
                super().hset(key, mapping={name: "{}" for name in (mapping or {})})
        out = rs.put_capabilities(Lossy(), capfile(observation("alpha", read=right())))
        self.assertEqual(["%s: field %s%s" % (rs.AGENT_KEY % "alpha", rs.CAP_PREFIX, SITE)], out["not_read_back"])

    def test_any_other_field_that_moved_during_the_write_is_reported(self):
        class Meddled(NarrowRedis):
            def hset(self, key, mapping=None):
                super().hset(key, mapping=mapping)
                self.hashes[key]["state"] = "changed by somebody else"
        out = rs.put_capabilities(Meddled(), capfile(observation("alpha", read=right())))
        self.assertEqual(["%s: field state" % (rs.AGENT_KEY % "alpha")], out["changed_elsewhere"])

    def test_the_verbs_never_load_the_roster_file_and_never_seed(self):
        """A roster baked into an old image is an old roster. Asking for a capability must not put it back."""
        conn = NarrowRedis()
        with tempfile.TemporaryDirectory() as folder:
            caps = Path(folder) / "agent_capabilities.json"
            caps.write_text(json.dumps(capfile(observation("alpha", read=right()))), encoding="utf-8")
            with mock.patch.object(rs, "CAPABILITIES", caps), \
                    mock.patch.object(rs, "load", side_effect=AssertionError("the roster file was read")), \
                    mock.patch.object(rs, "seed", side_effect=AssertionError("a seed was run")), \
                    mock.patch.object(rs.redis_dual, "Settings", return_value=object()), \
                    mock.patch.object(rs.redis_dual, "client", return_value=conn):
                self.assertEqual(0, rs.main(["capability-check"]))
                self.assertEqual([], conn.writes, "the check is the file alone")
                self.assertEqual(0, rs.main(["capability"]))
        self.assertEqual(1, len(conn.writes))

    def test_no_connection_is_unknown_and_a_refused_id_or_a_bad_read_back_is_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            caps = Path(folder) / "agent_capabilities.json"
            caps.write_text(json.dumps(capfile(observation("ghost", read=right()))), encoding="utf-8")
            with mock.patch.object(rs, "CAPABILITIES", caps), \
                    mock.patch.object(rs.redis_dual, "Settings", return_value=object()):
                with mock.patch.object(rs.redis_dual, "client", return_value=None):
                    self.assertEqual(2, rs.main(["capability"]))
                with mock.patch.object(rs.redis_dual, "client", return_value=NarrowRedis()):
                    self.assertEqual(1, rs.main(["capability"]), "an id Redis does not hold")
            caps.write_text(json.dumps(capfile(observation("alpha", read=right(can="granted")))), encoding="utf-8")
            with mock.patch.object(rs, "CAPABILITIES", caps), \
                    mock.patch.object(rs.redis_dual, "client", side_effect=AssertionError("connected for a bad file")):
                self.assertEqual(1, rs.main(["capability"]), "a bad file is refused before any connection")


class AnObservationIsNeverAGrant(unittest.TestCase):
    def test_a_well_formed_file_passes_and_the_real_one_does(self):
        self.assertEqual([], rs.check_capabilities(capfile(observation("alpha", read=right(), merge=right(can="no")))))
        real = rs.load_capabilities()
        self.assertEqual([], rs.check_capabilities(real))
        known = {row["id"] for row in rs.instances(rs.load())}
        for obs in real["observations"]:
            self.assertIn(obs["id"], known, "the real file names an instance the real roster does not have")

    def test_every_shape_that_is_not_an_observation_is_refused(self):
        def broken(change):
            obs = observation("alpha", read=right())
            change(obs)
            return rs.check_capabilities(capfile(obs))

        def state(**over):
            return lambda obs: obs["rights"].__setitem__("read", dict(right(), **over))
        cases = (
            (state(can="granted"), "never a grant or a denial"),
            (state(can="denied"), "never a grant or a denial"),
            (state(status="approved"), "never a grant or a denial"),
            (state(can="yes", status="unknown"), "unknown is unknown in both"),
            (state(can="unknown", status="verified"), "unknown is unknown in both"),
            (state(checked_at="yesterday"), "checked_at must be"),
            (state(checked_at=""), "checked_at must be"),
            (state(evidence_at="2026-10-12"), "evidence_at must be"),
            (state(evidence_at="2026-10-13T00:00:00Z"), "dated after its own check"),
            (state(source_event=""), "needs its source_event"),
            (state(source_event="a\nb"), "needs its source_event"),
            (lambda obs: obs["rights"].__setitem__("read", dict(unknown(), evidence_at=STAMP)), "unknown has no evidence time"),
            (lambda obs: obs["rights"].__setitem__("read", dict(right(), note="extra")), "a state is exactly"),
            (lambda obs: obs["rights"].__setitem__("read", "yes"), "a state is exactly"),
            (lambda obs: obs["rights"].pop("deploy"), "rights must be exactly"),
            (lambda obs: obs["rights"].__setitem__("admin", right()), "rights must be exactly"),
            (lambda obs: obs.__setitem__("granted", True), "nothing else"),
            (lambda obs: obs.__setitem__("runtime_identity", ""), "no runtime_identity"),
            (lambda obs: obs.__setitem__("id", "not an id"), "is not an instance id"),
            (lambda obs: obs.__setitem__("resource", "github sfdc"), "must be <kind>:<name>"),
            (lambda obs: obs.__setitem__("resource", "github:a|b"), "must be <kind>:<name>"),
            (lambda obs: obs.__setitem__("resource", "site"), "must be <kind>:<name>"),
            (lambda obs: obs.__setitem__("runtime_identity", "x" * 201), "no runtime_identity"),
        )
        for change, says in cases:
            problems = broken(change)
            self.assertTrue(any(says in problem for problem in problems), (says, problems))
        twice = capfile(observation("alpha", read=right()), observation("ALPHA", read=right()))
        self.assertTrue(any("appears twice" in p for p in rs.check_capabilities(twice)))
        self.assertEqual(["the file needs an observations list"], rs.check_capabilities({"version": "v1"}))
        self.assertTrue(rs.check_capabilities({"observations": [observation()]}), "no version, no date")
        self.assertTrue(any("is not an object" in p for p in rs.check_capabilities(capfile("alpha"))))

    def test_the_stored_value_is_the_observation_and_nothing_that_reads_as_a_permission(self):
        value = json.loads(rs.cap_value(observation("alpha", read=right(), merge=right(can="no", status="reported"))))
        self.assertEqual({"runtime_identity", "rights"}, set(value))
        self.assertEqual(set(rs.RIGHTS), set(value["rights"]), "five rights, each its own state")
        self.assertEqual(("yes", "verified"), (value["rights"]["read"]["can"], value["rights"]["read"]["status"]))
        self.assertEqual(("unknown", "unknown"), (value["rights"]["push"]["can"], value["rights"]["push"]["status"]))
        for word in rs.CAN + rs.STATUS:
            self.assertNotIn(word, ("granted", "denied", "allowed", "forbidden"))
        self.assertFalse(any(name.startswith(rs.CAP_PREFIX) for name in rs.FIELDS), "a cap field is never one of the fifteen")


class StaleEvidenceIsTold(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 12, 13, 10, 21, tzinfo=datetime.timezone.utc)

    def test_it_is_judged_from_when_somebody_last_looked(self):
        hour = datetime.timedelta(hours=1)
        self.assertEqual("fresh", rs.staleness(right(), self.NOW + hour))
        self.assertEqual("fresh", rs.staleness(right(), self.NOW + 24 * hour))
        self.assertEqual("STALE", rs.staleness(right(), self.NOW + 24 * hour + datetime.timedelta(seconds=1)))
        # An old fact checked now is fresh; a fact nobody has checked since is not, however new it was.
        self.assertEqual("fresh", rs.staleness(right(evidence="2026-10-02T00:00:00Z"), self.NOW))
        self.assertEqual("unknown", rs.staleness(unknown(), self.NOW + 99 * hour))

    def test_the_table_shows_the_state_how_it_is_known_and_whether_it_is_stale(self):
        text = rs.capability_table(capfile(observation("alpha", read=right(), merge=right(can="no", status="reported"))),
                                   self.NOW + datetime.timedelta(days=2))
        self.assertIn("alpha on %s, observed for: login example-bot" % SITE, text)
        self.assertRegex(text, r"read\s+yes\s+verified\s+STALE")
        self.assertRegex(text, r"merge\s+no\s+reported\s+STALE")
        self.assertRegex(text, r"deploy\s+unknown\s+unknown\s+unknown")


class TheTwoVerifiesEachKeepTheirOwn(unittest.TestCase):
    def test_a_capability_field_is_not_roster_drift_and_matches_its_own_file(self):
        data, caps = roster(), capfile(observation("alpha", read=right()))
        conn = NarrowRedis(data)
        rs.put_capabilities(conn, caps)
        self.assertEqual([], rs.verify(conn, data), "the roster's verify leaves cap: fields alone")
        self.assertEqual([], rs.verify_capabilities(conn, caps))
        conn.hashes[rs.AGENT_KEY % "alpha"]["stray"] = "x"
        self.assertEqual(["%s: field stray is in Redis and not in the file" % (rs.AGENT_KEY % "alpha")], rs.verify(conn, data))
        conn.hashes[rs.FAMILY_KEY % "claude"]["cap:x"] = "y"
        self.assertTrue(any("cap:x" in line for line in rs.verify(conn, data)), "only an INSTANCE hash may hold one")

    def test_the_capability_verify_can_say_no_three_ways(self):
        caps = capfile(observation("alpha", read=right()))
        conn = NarrowRedis()
        key, field = rs.AGENT_KEY % "alpha", rs.CAP_PREFIX + SITE
        self.assertEqual(["%s: %s is in the file and not in Redis" % (key, field)], rs.verify_capabilities(conn, caps))
        rs.put_capabilities(conn, caps)
        conn.hashes[key][field] = "{}"
        self.assertEqual(["%s: %s differs" % (key, field)], rs.verify_capabilities(conn, caps))
        rs.put_capabilities(conn, caps)
        conn.hashes[rs.AGENT_KEY % "beta"]["cap:gcp:run/x"] = "{}"
        self.assertEqual(["%s: cap:gcp:run/x is in Redis and not in the file" % (rs.AGENT_KEY % "beta")],
                         rs.verify_capabilities(conn, caps))

    def test_a_seed_replaces_the_hash_so_the_capability_goes_until_it_is_written_again(self):
        """Said, not hidden: seed() replaces each instance hash on purpose. The capability's original is
        its own file, so nothing is lost, and the capability verify reports it missing."""
        data, caps = roster(), capfile(observation("alpha", read=right()))
        conn = FakeRedis()
        rs.seed(conn, data)
        rs.put_capabilities(conn, caps)
        rs.seed(conn, data)
        self.assertNotIn(rs.CAP_PREFIX + SITE, conn.hashes[rs.AGENT_KEY % "alpha"])
        self.assertEqual(1, len(rs.verify_capabilities(conn, caps)))
        rs.put_capabilities(conn, caps)
        self.assertEqual(([], []), (rs.verify(conn, data), rs.verify_capabilities(conn, caps)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

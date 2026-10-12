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


# --- capability observations: scripts/roster_capability.py, an entrypoint of its own -------------
#
# The owner asked on 2026-10-12 for a map of who can do what, for routing. Aya's conditions for
# putting it beside the roster, and the four holes her review of be4a776 found, are each a class
# below, written as the failure it prevents:
#   1. `roster_seed.py seed capability` ran the destructive seed: the path has its own file now,
#      with exactly one argument, and that file cannot seed.
#   2. An unconditional HSET could overwrite newer evidence or create a missing hash: the write is a
#      compare-and-set on a complete hash, after a decision that refuses stale and conflicting evidence.
#   3. An access list is not an ability: verified requires basis executed.
#   4. There was no read-only verify, and a time was a shape, not a time: there is one, and a day
#      that does not exist or has not happened yet is refused.

import roster_capability as rc                                           # noqa: E402

STAMP = "2026-10-12T13:10:21Z"
LATER = "2026-10-12T14:10:21Z"
EARLIER = "2026-10-12T12:10:21Z"
NOW = datetime.datetime(2026, 10, 12, 15, 0, 0, tzinfo=datetime.timezone.utc)
SITE = "github:example/site"


def right(can="yes", status="verified", basis="executed", source="this runtime pushed commit abc1234", evidence=STAMP, checked=STAMP):
    return {"can": can, "status": status, "basis": basis, "source_event": source, "evidence_at": evidence, "checked_at": checked}


def said(can="yes", source="stated on the board", evidence=STAMP, checked=STAMP, basis="stated"):
    return right(can=can, status="reported", basis=basis, source=source, evidence=evidence, checked=checked)


def unknown(checked=STAMP, source="nobody looked"):
    return {"can": "unknown", "status": "unknown", "basis": "none", "source_event": source, "evidence_at": "", "checked_at": checked}


def observation(rid="alpha", resource=SITE, identity="login example-bot", **rights):
    states = {name: unknown() for name in rc.RIGHTS}
    states.update(rights)
    return {"id": rid, "resource": resource, "runtime_identity": identity, "rights": states}


def capfile(*observations):
    return {"version": "v1", "updated": "2026-10-12", "observations": list(observations)}


class NarrowRedis(FakeRedis):
    """A seeded copy that RECORDS every write and REFUSES every command the capability path must
    never send. Its eval is the compare-and-set as the server runs it, for THAT script and no other;
    TheLuaItself checks this double against the script's own text where an interpreter is installed."""

    def __init__(self, data=None):
        super().__init__()
        seeded = FakeRedis()
        rs.seed(seeded, data or roster())
        self.hashes, self.sets = copy.deepcopy(seeded.hashes), copy.deepcopy(seeded.sets)
        self.writes, self.reads = [], []

    def smembers(self, key):
        self.reads.append(("smembers", key))
        return super().smembers(key)

    def hgetall(self, key):
        self.reads.append(("hgetall", key))
        return super().hgetall(key)

    def eval(self, script, numkeys, *args):
        assert script == rc.CAS_LUA and numkeys == 2, "a script this path does not own"
        key, members, rid, field, read, held, value = args[:7]
        if rid not in self.sets.get(members, set()):
            return "not-a-member"
        if any(name not in self.hashes.get(key, {}) for name in args[7:]):
            return "incomplete"
        there = self.hashes[key].get(field)                      # None when the field does not exist
        if (there is not None) if read == "0" else (there is None or there != held):
            return "changed"
        self.hashes[key][field] = value
        self.writes.append((key, field))
        return "written"

    def hset(self, key, mapping=None):
        raise AssertionError("a bare HSET sent by the capability path: %s" % key)

    def delete(self, key):
        raise AssertionError("DEL %s sent by the capability path" % key)

    def hdel(self, key, field):
        raise AssertionError("HDEL %s %s sent by the capability path" % (key, field))

    def sadd(self, key, member):
        raise AssertionError("SADD %s sent by the capability path" % key)

    def srem(self, key, member):
        raise AssertionError("SREM %s sent by the capability path" % key)


def put(conn, caps):
    """rc.write judged at NOW, so a stored time in these fixtures is never 'the future' by the wall clock."""
    return rc.write(conn, caps, now=NOW)


def run(argv, caps=None, conn="none", now=None):
    """rc.main with a capability file of our own and a connection of our own. Returns its exit code."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "agent_capabilities.json"
        if caps is not None:
            path.write_text(json.dumps(caps), encoding="utf-8")
        client = mock.Mock(side_effect=AssertionError("connected")) if conn == "none" else mock.Mock(return_value=conn)
        with mock.patch.object(rc, "CAPABILITIES", path), \
                mock.patch.object(rc.redis_dual, "Settings", return_value=object()), \
                mock.patch.object(rc.redis_dual, "client", client):
            return rc.main(argv)


class OneEntrypointThatCannotSeed(unittest.TestCase):
    """Hole 1. `roster_seed.py seed capability` took its first word and seeded."""

    def test_anything_but_exactly_one_known_verb_is_refused_before_a_file_or_a_connection(self):
        for argv in ([], ["seed"], ["seed", "capability"], ["capability"], ["write", "now"], ["check", "--force"],
                     ["verify", "write"], ["WRITE"], ["table"], ["capability-check"], [""], ["write", ""]):
            with mock.patch.object(rc, "load", side_effect=AssertionError("the file was read")):
                self.assertEqual(2, run(argv), argv)              # run() raises if a connection is made

    def test_the_file_holds_no_seed_and_does_not_import_the_seeder(self):
        source = Path(rc.__file__).read_text(encoding="utf-8")
        for never in ("import roster_seed", "from roster_seed", "seed(", ".delete(", ".hdel(", ".sadd(", ".srem(", ".hset("):
            self.assertNotIn(never, source, never)
        self.assertEqual(("check", "verify", "write"), rc.VERBS)

    def test_its_names_for_the_roster_are_the_seeders_own(self):
        self.assertEqual((rs.AGENT_KEY, rs.ROSTER_SET, rs.FIELDS, rs.CAP_PREFIX),
                         (rc.AGENT_KEY, rc.ROSTER_SET, rc.CANONICAL_FIELDS, rc.CAP_PREFIX))
        self.assertEqual(15, len(rc.CANONICAL_FIELDS))

    def test_the_seeder_itself_gained_no_capability_verb(self):
        source = Path(rs.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"capability"', source)
        self.assertNotIn("put_capabilities", source)

    def test_check_reads_the_file_alone_and_a_bad_file_stops_write_before_any_connection(self):
        good = capfile(observation("alpha", read=right()))
        self.assertEqual(0, run(["check"], good))
        bad = capfile(observation("alpha", read=right(can="granted")))
        for verb in rc.VERBS:
            self.assertEqual(1, run([verb], bad), verb)

    def test_no_connection_is_unknown_and_never_success(self):
        good = capfile(observation("alpha", read=right()))
        for verb in ("write", "verify"):
            self.assertEqual(3, run([verb], good, conn=None), verb)


class TheWriteIsGuardedAndAtomic(unittest.TestCase):
    """Hole 2. An unconditional HSET overwrote whatever was there and created whatever was not."""

    def test_one_field_is_set_and_everything_else_is_byte_for_byte_the_same(self):
        conn = NarrowRedis()
        before_hashes, before_sets = copy.deepcopy(conn.hashes), copy.deepcopy(conn.sets)
        obs = observation("alpha", read=right(), push=said(can="no"))
        out = put(conn, capfile(obs))
        key, field = rc.AGENT_KEY % "alpha", rc.CAP_PREFIX + SITE
        self.assertEqual(["%s %s" % (key, field)], out["written"])
        self.assertEqual(([], [], [], [], []), (out["same"], out["refused"], out["lost"], out["changed_elsewhere"], out["not_read_back"]))
        self.assertEqual([(key, field)], conn.writes, "one compare-and-set, of one field, on one existing key")
        self.assertEqual(rc.cap_value(obs), conn.hashes[key][field])
        after = copy.deepcopy(conn.hashes)
        del after[key][field]
        self.assertEqual(before_hashes, after, "the fifteen fields, the family, canon and seeded are untouched")
        self.assertEqual(before_sets, conn.sets)

    def test_an_id_that_is_not_a_member_means_nothing_is_written_for_anyone(self):
        conn = NarrowRedis()
        before = copy.deepcopy(conn.hashes)
        out = put(conn, capfile(observation("alpha", read=right()), observation("ghost", read=right())))
        self.assertEqual(([], 1), (out["written"], len(out["refused"])))
        self.assertIn("creates no instance", out["refused"][0])
        self.assertEqual(([], before), (conn.writes, conn.hashes))
        self.assertNotIn(rc.AGENT_KEY % "ghost", conn.hashes)

    def test_a_missing_hash_is_refused_and_never_created(self):
        conn = NarrowRedis()
        del conn.hashes[rc.AGENT_KEY % "alpha"]                   # still a member of the set: a half-written copy
        out = put(conn, capfile(observation("alpha", read=right())))
        self.assertIn("missing or incomplete (15 of 15", out["refused"][0])
        self.assertEqual([], conn.writes)
        self.assertNotIn(rc.AGENT_KEY % "alpha", conn.hashes, "the old HSET would have made a hash of one field here")

    def test_an_incomplete_hash_is_refused_and_not_completed(self):
        conn = NarrowRedis()
        del conn.hashes[rc.AGENT_KEY % "alpha"]["wake"]
        out = put(conn, capfile(observation("alpha", read=right()), observation("beta", read=right())))
        self.assertIn("missing or incomplete (1 of 15", out["refused"][0])
        self.assertEqual([], conn.writes, "and beta, whose hash is whole, is not written either")

    def test_an_identical_observation_is_a_retry_and_writes_nothing(self):
        conn, caps = NarrowRedis(), capfile(observation("alpha", read=right()))
        put(conn, caps)
        conn.writes.clear()
        out = put(conn, caps)
        self.assertEqual(([], 1, []), (out["written"], len(out["same"]), out["refused"]))
        self.assertEqual([], conn.writes)

    def test_older_evidence_never_overwrites_newer(self):
        conn = NarrowRedis()
        newer = observation("alpha", read=right(checked=LATER), push=said(checked=LATER))
        put(conn, capfile(newer))
        conn.writes.clear()
        for stale in (observation("alpha", read=right(checked=STAMP), push=said(checked=LATER)),      # one right older
                      observation("alpha", read=right(can="no", checked=EARLIER, evidence=EARLIER), push=said(checked=LATER))):
            out = put(conn, capfile(stale))
            self.assertIn("stale: read was checked at %s in Redis" % LATER, out["refused"][0])
        self.assertEqual([], conn.writes)
        self.assertEqual(rc.cap_value(newer), conn.hashes[rc.AGENT_KEY % "alpha"][rc.CAP_PREFIX + SITE])

    def test_the_same_check_time_with_a_different_claim_is_a_conflict_not_a_win_for_the_later_writer(self):
        conn = NarrowRedis()
        first = observation("alpha", read=right())
        put(conn, capfile(first))
        conn.writes.clear()
        out = put(conn, capfile(observation("alpha", read=right(can="no"))))
        self.assertIn("conflict: read has the same check time", out["refused"][0])
        out = put(conn, capfile(observation("alpha", identity="login another-bot", read=right())))
        self.assertIn("conflict: no check here is newer", out["refused"][0], "a different identity at the same moment")
        self.assertEqual([], conn.writes)
        self.assertEqual(rc.cap_value(first), conn.hashes[rc.AGENT_KEY % "alpha"][rc.CAP_PREFIX + SITE])

    def test_a_newer_check_replaces_the_stored_one(self):
        conn = NarrowRedis()
        put(conn, capfile(observation("alpha", read=right())))
        newer = observation("alpha", read=right(can="no", evidence=LATER, checked=LATER))
        out = put(conn, capfile(newer))
        self.assertEqual(1, len(out["written"]))
        self.assertEqual(rc.cap_value(newer), conn.hashes[rc.AGENT_KEY % "alpha"][rc.CAP_PREFIX + SITE])

    def test_an_empty_string_is_a_stored_value_not_an_absent_one(self):
        """Aya on a79a3ca: "" was taken for absence, so a field that EXISTED and was empty was written
        over as if nothing had been there. None alone is absence."""
        self.assertEqual(("write", ""), rc.decide(None, observation("alpha", read=right()), NOW))
        self.assertEqual(("refuse", rc.UNREADABLE), rc.decide("", observation("alpha", read=right()), NOW))
        self.assertEqual(("refuse", rc.UNREADABLE), rc.decide(b"", observation("alpha", read=right()), NOW))
        conn = NarrowRedis()
        key, field = rc.AGENT_KEY % "alpha", rc.CAP_PREFIX + SITE
        conn.hashes[key][field] = ""
        out = put(conn, capfile(observation("alpha", read=right())))
        self.assertIn(rc.UNREADABLE, out["refused"][0])
        self.assertEqual(([], ""), (conn.writes, conn.hashes[key][field]))
        # And in the script itself: expecting ABSENCE, a field that exists and is empty is "changed".
        args = (key, rc.ROSTER_SET, "alpha", field, "0", "", "NEW") + rc.CANONICAL_FIELDS
        self.assertEqual("changed", conn.eval(rc.CAS_LUA, 2, *args))
        del conn.hashes[key][field]
        self.assertEqual("written", conn.eval(rc.CAS_LUA, 2, *args))

    def test_a_stored_value_is_judged_whole_before_it_is_compared_or_replaced(self):
        """Aya on a79a3ca: a stored JSON holding five checked_at fields and nothing else was comparable,
        and so replaceable. Every way a stored value can fall short of a whole observation is refused."""
        whole = observation("alpha", read=right(checked=EARLIER, evidence=EARLIER))
        good = json.loads(rc.cap_value(whole))

        def broken(change):
            held = json.loads(json.dumps(good))
            change(held)
            return json.dumps(held, sort_keys=True, separators=(",", ":"))
        only_times = json.dumps({"runtime_identity": "login example-bot",
                                 "rights": {name: {"checked_at": EARLIER} for name in rc.RIGHTS}}, sort_keys=True, separators=(",", ":"))
        junk = (
            only_times,                                                               # the hole itself
            "{}", "not json", '["a list"]', "null", "0",
            broken(lambda h: h["rights"]["read"].__setitem__("can", "granted")),
            broken(lambda h: h["rights"]["read"].__setitem__("status", "approved")),
            broken(lambda h: h["rights"]["read"].__setitem__("basis", "acl")),         # verified on an access list
            broken(lambda h: h["rights"]["read"].__setitem__("basis", "inferred")),
            broken(lambda h: h["rights"]["read"].__setitem__("source_event", "")),
            broken(lambda h: h["rights"]["read"].__setitem__("checked_at", "2026-02-30T00:00:00Z")),
            broken(lambda h: h["rights"]["read"].__setitem__("checked_at", "2026-10-13T00:00:00Z")),   # the future, at NOW
            broken(lambda h: h["rights"].pop("deploy")),
            broken(lambda h: h["rights"].__setitem__("admin", dict(good["rights"]["read"]))),
            broken(lambda h: h.__setitem__("runtime_identity", "")),
            broken(lambda h: h.pop("runtime_identity")),
            broken(lambda h: h.__setitem__("granted", True)),
            json.dumps(good, indent=1),                                               # the same content, not this path's text
        )
        newer = observation("alpha", read=right(checked=LATER, evidence=LATER))
        for stored in junk:
            self.assertEqual(("refuse", rc.UNREADABLE), rc.decide(stored, newer, NOW), stored[:60])
            conn = NarrowRedis()
            conn.hashes[rc.AGENT_KEY % "alpha"][rc.CAP_PREFIX + SITE] = stored
            out = put(conn, capfile(newer))
            self.assertIn(rc.UNREADABLE, out["refused"][0], stored[:60])
            self.assertEqual(([], stored), (conn.writes, conn.hashes[rc.AGENT_KEY % "alpha"][rc.CAP_PREFIX + SITE]))
        # A whole, valid, older observation in this path's own text IS replaced by a newer one.
        self.assertEqual(("write", ""), rc.decide(rc.cap_value(whole), newer, NOW))

    def test_a_value_that_moved_between_the_read_and_the_write_is_not_overwritten(self):
        class Raced(NarrowRedis):
            def eval(self, script, numkeys, *args):
                self.hashes[args[0]][args[3]] = "written by somebody else in between"
                return super().eval(script, numkeys, *args)
        conn = Raced()
        out = put(conn, capfile(observation("alpha", read=right())))
        self.assertEqual(([], 1), (out["written"], len(out["lost"])))
        self.assertIn(": changed", out["lost"][0])
        self.assertEqual("written by somebody else in between", conn.hashes[rc.AGENT_KEY % "alpha"][rc.CAP_PREFIX + SITE])
        self.assertEqual([], out["not_read_back"], "a field this run did not set is not called a failed read-back")

    def test_a_hash_deleted_or_a_member_removed_between_the_read_and_the_write_is_not_recreated(self):
        class Vanished(NarrowRedis):
            def eval(self, script, numkeys, *args):
                del self.hashes[args[0]]
                return super().eval(script, numkeys, *args)
        conn = Vanished()
        out = put(conn, capfile(observation("alpha", read=right())))
        self.assertIn(": incomplete", out["lost"][0])
        self.assertNotIn(rc.AGENT_KEY % "alpha", conn.hashes)

        class Removed(NarrowRedis):
            def eval(self, script, numkeys, *args):
                self.sets[args[1]].discard(args[2])
                return super().eval(script, numkeys, *args)
        out = put(Removed(), capfile(observation("alpha", read=right())))
        self.assertIn(": not-a-member", out["lost"][0])

    def test_a_field_that_does_not_read_back_and_a_field_that_moved_elsewhere_are_both_failures(self):
        class Lossy(NarrowRedis):
            def eval(self, script, numkeys, *args):
                answer = super().eval(script, numkeys, *args)
                self.hashes[args[0]][args[3]] = "{}"
                self.hashes[args[0]]["state"] = "changed by somebody else"
                return answer
        out = put(Lossy(), capfile(observation("alpha", read=right())))
        self.assertEqual(1, len(out["not_read_back"]))
        self.assertEqual(["%s: field state" % (rc.AGENT_KEY % "alpha")], out["changed_elsewhere"])
        self.assertEqual(1, run(["write"], capfile(observation("alpha", read=right())), conn=Lossy()))

    def test_the_verbs_never_load_the_roster_file(self):
        conn = NarrowRedis()
        with mock.patch.object(rs, "load", side_effect=AssertionError("the roster file was read")), \
                mock.patch.object(rs, "seed", side_effect=AssertionError("a seed was run")):
            self.assertEqual(0, run(["write"], capfile(observation("alpha", read=right())), conn=conn))
        self.assertEqual(1, len(conn.writes))
        self.assertEqual(1, run(["write"], capfile(observation("ghost", read=right())), conn=NarrowRedis()))


class AnAccessListIsNotAnAbility(unittest.TestCase):
    """Hole 3. permissions.push true was recorded as a verified push; a source review as an approval right."""

    def test_verified_requires_that_the_runtime_did_it(self):
        ok = capfile(observation("alpha", read=right(), push=said(basis="acl"), merge=said(can="no")))
        self.assertEqual([], rc.check(ok, NOW))
        for state, says in ((right(basis="acl"), "verified needs basis executed"),
                            (right(basis="stated"), "verified needs basis executed"),
                            (said(basis="executed"), "reported needs basis acl or stated"),
                            (dict(unknown(), basis="acl"), "unknown needs basis none"),
                            (right(basis="none"), "verified needs basis executed"),
                            (right(basis="inferred"), "never a grant or a denial")):
            problems = rc.check(capfile(observation("alpha", push=state)), NOW)
            self.assertTrue(any(says in problem for problem in problems), (says, problems))

    def test_the_real_file_calls_nothing_verified_on_an_access_list_or_a_statement(self):
        real = rc.load()
        self.assertEqual([], rc.check(real))
        known = {row["id"] for row in rs.instances(rs.load())}
        for obs in real["observations"]:
            self.assertIn(obs["id"], known, "the real file names an instance the real roster does not have")
            for name, state in obs["rights"].items():
                if state["status"] == "verified":
                    self.assertEqual("executed", state["basis"], (obs["id"], name))
                    self.assertNotIn("permissions.", state["source_event"], "an access-list reading is not an execution")
        aya = next(obs for obs in real["observations"] if obs["id"] == "aya")
        self.assertEqual("unknown", aya["rights"]["review"]["can"], "a source review is not a GitHub approval right")

    def test_every_other_shape_that_is_not_an_observation_is_refused(self):
        def broken(change):
            obs = observation("alpha", read=right())
            change(obs)
            return rc.check(capfile(obs), NOW)

        def state(**over):
            return lambda obs: obs["rights"].__setitem__("read", dict(right(), **over))
        cases = (
            (state(can="granted"), "never a grant or a denial"),
            (state(can="denied"), "never a grant or a denial"),
            (state(status="approved"), "never a grant or a denial"),
            (state(can="yes", status="unknown", basis="none"), "unknown is unknown in both"),
            (state(can="unknown"), "unknown is unknown in both"),
            (state(source_event=""), "needs its source_event"),
            (state(source_event="a\nb"), "needs its source_event"),
            (lambda obs: obs["rights"].__setitem__("read", dict(unknown(), evidence_at=STAMP)), "unknown has no evidence time"),
            (lambda obs: obs["rights"].__setitem__("read", dict(right(), note="extra")), "a state is exactly"),
            (lambda obs: obs["rights"].__setitem__("read", {k: v for k, v in right().items() if k != "basis"}), "a state is exactly"),
            (lambda obs: obs["rights"].__setitem__("read", "yes"), "a state is exactly"),
            (lambda obs: obs["rights"].pop("deploy"), "rights must be exactly"),
            (lambda obs: obs["rights"].__setitem__("admin", right()), "rights must be exactly"),
            (lambda obs: obs.__setitem__("granted", True), "nothing else"),
            (lambda obs: obs.__setitem__("runtime_identity", ""), "no runtime_identity"),
            (lambda obs: obs.__setitem__("runtime_identity", "x" * 201), "no runtime_identity"),
            (lambda obs: obs.__setitem__("id", "not an id"), "is not an instance id"),
            (lambda obs: obs.__setitem__("resource", "github sfdc"), "must be <kind>:<name>"),
            (lambda obs: obs.__setitem__("resource", "github:a|b"), "must be <kind>:<name>"),
            (lambda obs: obs.__setitem__("resource", "site"), "must be <kind>:<name>"),
        )
        for change, says in cases:
            problems = broken(change)
            self.assertTrue(any(says in problem for problem in problems), (says, problems))
        twice = capfile(observation("alpha", read=right()), observation("ALPHA", read=right()))
        self.assertTrue(any("appears twice" in p for p in rc.check(twice, NOW)))
        self.assertEqual(["the file needs an observations list"], rc.check({"version": "v1"}, NOW))
        self.assertTrue(rc.check({"observations": [observation()]}, NOW), "no version, no date")
        self.assertTrue(any("is not an object" in p for p in rc.check(capfile("alpha"), NOW)))

    def test_the_stored_value_is_the_observation_and_nothing_that_reads_as_a_permission(self):
        value = json.loads(rc.cap_value(observation("alpha", read=right(), merge=said(can="no"))))
        self.assertEqual({"runtime_identity", "rights"}, set(value))
        self.assertEqual(set(rc.RIGHTS), set(value["rights"]), "five rights, each its own state")
        self.assertEqual(("yes", "verified", "executed"), tuple(value["rights"]["read"][k] for k in ("can", "status", "basis")))
        self.assertEqual(("unknown", "unknown", "none"), tuple(value["rights"]["push"][k] for k in ("can", "status", "basis")))
        for word in rc.CAN + rc.STATUS + rc.BASIS:
            self.assertNotIn(word, ("granted", "denied", "allowed", "forbidden"))
        self.assertFalse(any(name.startswith(rc.CAP_PREFIX) for name in rc.CANONICAL_FIELDS))


class ATimeIsARealTimeAndNotTomorrow(unittest.TestCase):
    """Hole 4, second half. A time was checked for its shape only, and could be next week."""

    def test_a_day_that_does_not_exist_is_refused_not_crashed_on(self):
        for field in ("checked_at", "evidence_at"):
            for stamp in ("2026-02-30T00:00:00Z", "2026-13-01T00:00:00Z", "2026-10-12T25:00:00Z", "2026-10-12 13:10:21",
                          "2026-10-12T13:10:21+00:00", "yesterday", "", None, 1791810621):
                problems = rc.check(capfile(observation("alpha", read=dict(right(), **{field: stamp}))), NOW)
                self.assertTrue(any("%s must be a real time" % field in p for p in problems), (field, stamp, problems))

    def test_a_time_in_the_future_is_refused_and_a_small_clock_skew_is_not(self):
        def at(seconds):
            return (NOW + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        edge, past = at(rc.FUTURE_SKEW_SECONDS), at(rc.FUTURE_SKEW_SECONDS + 1)
        self.assertEqual([], rc.check(capfile(observation("alpha", read=right(evidence=edge, checked=edge))), NOW))
        problems = rc.check(capfile(observation("alpha", read=right(evidence=STAMP, checked=past))), NOW)
        self.assertTrue(any("checked_at is in the future" in p for p in problems), problems)
        problems = rc.check(capfile(observation("alpha", read=right(evidence=past, checked=past))), NOW)
        self.assertTrue(any("evidence_at is in the future" in p for p in problems), problems)
        problems = rc.check(capfile(observation("alpha", read=dict(unknown(), checked_at=at(86400)))), NOW)
        self.assertTrue(any("checked_at is in the future" in p for p in problems), "an unknown checked tomorrow, too")
        problems = rc.check(capfile(observation("alpha", read=right(evidence=LATER, checked=STAMP))), NOW)
        self.assertTrue(any("dated after its own check" in p for p in problems), problems)

    def test_stale_is_judged_from_when_somebody_last_looked(self):
        base = datetime.datetime(2026, 10, 12, 13, 10, 21, tzinfo=datetime.timezone.utc)
        hour = datetime.timedelta(hours=1)
        self.assertEqual("fresh", rc.staleness(right(), base + 24 * hour))
        self.assertEqual("STALE", rc.staleness(right(), base + 24 * hour + datetime.timedelta(seconds=1)))
        self.assertEqual("fresh", rc.staleness(right(evidence="2026-10-02T00:00:00Z"), base), "an old fact checked now")
        self.assertEqual("unknown", rc.staleness(unknown(), base + 99 * hour))
        text = rc.table(capfile(observation("alpha", read=right(), merge=said(can="no", basis="acl"))), base + 48 * hour)
        self.assertRegex(text, r"read\s+yes\s+verified\s+executed\s+STALE")
        self.assertRegex(text, r"merge\s+no\s+reported\s+acl\s+STALE")
        self.assertRegex(text, r"deploy\s+unknown\s+unknown\s+none\s+unknown")


class VerifyReadsAndNeverWrites(unittest.TestCase):
    """Hole 4, first half. There was no way to ask Redis what it holds without the write path."""

    class ReadOnly(NarrowRedis):
        def eval(self, script, numkeys, *args):
            raise AssertionError("EVAL sent by verify")

    def test_the_verify_verb_sends_only_reads_and_says_no_three_ways(self):
        caps = capfile(observation("alpha", read=right()))
        key, field = rc.AGENT_KEY % "alpha", rc.CAP_PREFIX + SITE
        conn = self.ReadOnly()
        self.assertEqual(["%s: %s is in the file and not in Redis" % (key, field)], rc.verify(conn, caps))
        self.assertEqual(1, run(["verify"], caps, conn=conn))
        conn.hashes[key][field] = "{}"
        self.assertEqual(["%s: %s differs" % (key, field)], rc.verify(conn, caps))
        conn.hashes[key][field] = rc.cap_value(caps["observations"][0])
        self.assertEqual([], rc.verify(conn, caps))
        self.assertEqual(0, run(["verify"], caps, conn=conn))
        conn.hashes[rc.AGENT_KEY % "beta"]["cap:gcp:run/x"] = "{}"
        self.assertEqual(["%s: cap:gcp:run/x is in Redis and not in the file" % (rc.AGENT_KEY % "beta")], rc.verify(conn, caps))
        self.assertEqual({"smembers", "hgetall"}, {name for name, _ in conn.reads})
        self.assertEqual([], conn.writes)

    def test_it_reports_an_instance_redis_does_not_hold_and_a_hash_that_is_not_whole(self):
        conn = self.ReadOnly()
        drift = rc.verify(conn, capfile(observation("ghost", read=right())))
        self.assertTrue(any("ghost is not in" in line for line in drift), drift)
        del conn.hashes[rc.AGENT_KEY % "alpha"]["wake"]
        drift = rc.verify(conn, capfile(observation("alpha", read=right())))
        self.assertIn("%s: the hash is missing or incomplete" % (rc.AGENT_KEY % "alpha"), drift)

    def test_the_rosters_verify_and_this_one_each_keep_their_own(self):
        data, caps = roster(), capfile(observation("alpha", read=right()))
        conn = NarrowRedis(data)
        put(conn, caps)
        self.assertEqual(([], []), (rs.verify(conn, data), rc.verify(conn, caps)))
        conn.hashes[rc.AGENT_KEY % "alpha"]["stray"] = "x"
        self.assertEqual(["%s: field stray is in Redis and not in the file" % (rc.AGENT_KEY % "alpha")], rs.verify(conn, data))
        conn.hashes[rs.FAMILY_KEY % "claude"]["cap:x"] = "y"
        self.assertTrue(any("cap:x" in line for line in rs.verify(conn, data)), "only an INSTANCE hash may hold one")

    def test_a_seed_replaces_the_hash_so_the_capability_goes_until_it_is_written_again(self):
        """Said, not hidden: seed() replaces each instance hash on purpose. The capability's original is
        its own file, so nothing is lost, and this verify reports it missing."""
        data, caps = roster(), capfile(observation("alpha", read=right()))
        conn = NarrowRedis(data)
        put(conn, caps)
        plain = FakeRedis()
        plain.hashes, plain.sets = conn.hashes, conn.sets
        rs.seed(plain, data)
        self.assertNotIn(rc.CAP_PREFIX + SITE, plain.hashes[rc.AGENT_KEY % "alpha"])
        self.assertEqual(1, len(rc.verify(plain, caps)))


try:
    import fakeredis                                                     # noqa: E402
    import lupa                                                          # noqa: E402,F401
except ImportError:                                                      # CI's suite has neither
    fakeredis = None


@unittest.skipUnless(fakeredis, "fakeredis with lupa is not installed: the Lua text is not run here")
class TheLuaItself(unittest.TestCase):
    """CAS_LUA's own text, run by a Lua interpreter against an in-memory server, with the same cases the
    Python double above is used for. A double that answers in the shape its author assumed proves
    nothing about the script; this is where the two are held to the same answers."""

    def server(self):
        conn = fakeredis.FakeStrictRedis(decode_responses=True)
        rs.seed(conn, roster())
        return conn

    def test_the_script_and_the_double_give_the_same_answers(self):
        key, field = rc.AGENT_KEY % "alpha", rc.CAP_PREFIX + SITE
        cases = (
            ("alpha", "", "written"),                 # nothing held, nothing expected
            ("alpha", "something else", "changed"),   # the caller read a value that is no longer there
            ("ghost", "", "not-a-member"),
        )
        for rid, held, want in cases:
            real, double = self.server(), NarrowRedis()
            args = (rc.AGENT_KEY % rid if rid == "ghost" else key, rc.ROSTER_SET, rid, field,
                    "0" if held == "" else "1", held, "NEW") + rc.CANONICAL_FIELDS
            self.assertEqual(want, real.eval(rc.CAS_LUA, 2, *args), (rid, held))
            self.assertEqual(want, double.eval(rc.CAS_LUA, 2, *args), (rid, held))
        # ABSENT IS NOT EMPTY, in the script's own text: a field that exists and holds "" is not absence,
        # and a value that was read must still be exactly there.
        for there, read, held, want in (("", "0", "", "changed"), ("", "1", "", "written"), ("OLD", "1", "OLD", "written"),
                                        ("OLD", "1", "OTHER", "changed"), ("OLD", "0", "", "changed"), (None, "1", "", "changed")):
            real, double = self.server(), NarrowRedis()
            if there is not None:
                real.hset(key, field, there)
                double.hashes[key][field] = there
            args = (key, rc.ROSTER_SET, "alpha", field, read, held, "NEW") + rc.CANONICAL_FIELDS
            self.assertEqual((want, want), (real.eval(rc.CAS_LUA, 2, *args), double.eval(rc.CAS_LUA, 2, *args)), (there, read, held))
            self.assertEqual("NEW" if want == "written" else there, real.hget(key, field), (there, read, held))
        real, double = self.server(), NarrowRedis()
        real.hdel(key, "wake")
        del double.hashes[key]["wake"]
        args = (key, rc.ROSTER_SET, "alpha", field, "0", "", "NEW") + rc.CANONICAL_FIELDS
        self.assertEqual(("incomplete", "incomplete"), (real.eval(rc.CAS_LUA, 2, *args), double.eval(rc.CAS_LUA, 2, *args)))
        self.assertIsNone(real.hget(key, field), "nothing was set on an incomplete hash")
        real = self.server()
        real.delete(key)
        self.assertEqual("incomplete", real.eval(rc.CAS_LUA, 2, *args))
        self.assertFalse(real.exists(key), "and a missing hash was not created")

    def test_the_whole_write_runs_against_the_interpreter_and_sets_one_field(self):
        conn = self.server()
        before = {key: conn.hgetall(key) for key in conn.keys("*") if conn.type(key) == "hash"}
        caps = capfile(observation("alpha", read=right()))
        out = put(conn, caps)
        key, field = rc.AGENT_KEY % "alpha", rc.CAP_PREFIX + SITE
        self.assertEqual((1, [], [], []), (len(out["written"]), out["lost"], out["changed_elsewhere"], out["not_read_back"]))
        after = {k: conn.hgetall(k) for k in conn.keys("*") if conn.type(k) == "hash"}
        self.assertEqual(rc.cap_value(caps["observations"][0]), after[key].pop(field))
        self.assertEqual(before, after)
        self.assertEqual([], rc.verify(conn, caps))
        self.assertEqual(1, len(put(conn, caps)["same"]), "and a second run is a retry")
        stale = capfile(observation("alpha", read=right(checked=EARLIER, evidence=EARLIER)))
        self.assertIn("stale", put(conn, stale)["refused"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

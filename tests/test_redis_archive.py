"""The hourly Redis archive and its LEDGER (cloud/bus-reconciler/redis_archive.py; REDIS-PRIMARY D5).

What must hold: every entry reaches an object before its cursor moves; an archived range is never
rewritten; a crash between the object and the cursor re-finds the object instead of losing or doubling
it; one archiver at a time; the ledger hashes what it names; the board row carries no entry's text."""
import datetime
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "cloud" / "bus-reconciler"))

import redis_archive as ra                                                # noqa: E402

NOW = datetime.datetime(2026, 10, 11, 23, 5, tzinfo=datetime.timezone.utc)


def order(entry_id):
    return tuple(int(p) for p in entry_id.split("-"))


class FakeRedis:
    """Strings, hashes and streams, with SET NX/EX, SCAN and the exclusive XRANGE bound of Redis 6.2+."""

    def __init__(self):
        self.data, self.kinds, self.seq = {}, {}, 0

    def xadd(self, key, fields):
        self.seq += 1
        entry_id = "%d-0" % (1000 + self.seq)
        self.data.setdefault(key, []).append((entry_id, dict(fields)))
        self.kinds[key] = "stream"
        return entry_id

    def type(self, key):
        return self.kinds.get(key, "none")

    def scan_iter(self, match="*", count=None):
        import fnmatch
        return [k for k in list(self.data) if fnmatch.fnmatchcase(k, match)]

    def xrange(self, key, min="-", max="+", count=None):
        assert max == "+"
        entries = self.data.get(key) or []
        if min.startswith("("):
            entries = [e for e in entries if order(e[0]) > order(min[1:])]
        elif min != "-":
            entries = [e for e in entries if order(e[0]) >= order(min)]
        return entries[:count] if count else list(entries)

    def xlen(self, key):
        return len(self.data.get(key) or [])

    def get(self, key):
        return self.data.get(key) if self.kinds.get(key) == "string" else None

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key], self.kinds[key] = value, "string"
        return True

    def delete(self, key):
        self.data.pop(key, None)
        self.kinds.pop(key, None)

    def hget(self, key, field):
        return (self.data.get(key) or {}).get(field)

    def hset(self, key, field, value):
        self.data.setdefault(key, {})[field] = value
        self.kinds[key] = "hash"

    def hkeys(self, key):
        return list((self.data.get(key) or {}).keys())

    def hdel(self, key, *fields):
        for f in fields:
            (self.data.get(key) or {}).pop(f, None)


class Bucket:
    """A create-only object store: a second write to one name raises Exists, like ifGenerationMatch=0."""

    def __init__(self, fail_on=None):
        self.objects, self.fail_on = {}, fail_on

    def put(self, name, body, ctype):
        if self.fail_on and self.fail_on in name:
            raise ConnectionError("GCS unavailable")
        if name in self.objects:
            raise ra.Exists(name)
        self.objects[name] = body

    def get(self, name):
        return self.objects[name]


def seeded():
    conn = FakeRedis()
    for k in range(3):
        conn.xadd("conf:side:20261011", {"by": "grok", "text": "room %d" % k})
    for k in range(2):
        conn.xadd("fleet:inbox:gemini", {"by": "grok", "text": "for gemini %d" % k})
    conn.xadd("gov:audit", {"actor": "grok", "op": "xadd"})
    conn.set("fleet:hello:grok", "not a stream")
    return conn


def entries(body):
    return [json.loads(line) for line in body.decode("utf-8").splitlines()]


class OneRun(unittest.TestCase):
    def test_every_stream_is_archived_and_nothing_else(self):
        conn, bucket, rows = seeded(), Bucket(), []
        out = ra.run(conn, bucket.put, append=rows.append, now=NOW, run_id="r1")
        self.assertEqual("ok", out["status"])
        archived = sorted(n for n in bucket.objects if n.endswith(".jsonl"))
        self.assertEqual(3, len(archived))
        self.assertTrue(all(n.startswith("redis-archive/2026-10-11/23/") for n in archived))
        self.assertFalse(any("hello" in n for n in bucket.objects), "a string key is not a stream")
        side = [n for n in archived if "conf__side" in n][0]
        self.assertEqual(["room 0", "room 1", "room 2"], [e["fields"]["text"] for e in entries(bucket.objects[side])])

    def test_the_cursor_moves_to_the_last_entry_and_the_next_run_takes_only_what_is_new(self):
        conn, bucket = seeded(), Bucket()
        ra.run(conn, bucket.put, now=NOW, run_id="r1")
        last = conn.data["conf:side:20261011"][-1][0]
        self.assertEqual(last, conn.hget(ra.CURSOR_KEY, "conf:side:20261011"))
        conn.xadd("conf:side:20261011", {"by": "aya", "text": "later"})
        out = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=1), run_id="r2")
        self.assertEqual(1, out["new"])
        later = [n for n in bucket.objects if n.startswith("redis-archive/2026-10-12/00/")]
        body = [bucket.objects[n] for n in later if n.endswith(".jsonl")][0]
        self.assertEqual(["later"], [e["fields"]["text"] for e in entries(body)])

    def test_a_run_with_nothing_new_writes_a_ledger_and_no_object(self):
        conn, bucket = seeded(), Bucket()
        ra.run(conn, bucket.put, now=NOW, run_id="r1")
        before = set(bucket.objects)
        out = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=1), run_id="r2")
        added = set(bucket.objects) - before
        self.assertEqual(0, out["new"])
        self.assertEqual(1, len(added))
        self.assertTrue(next(iter(added)).endswith("LEDGER-r2.json"))


class NeverLostNeverRewritten(unittest.TestCase):
    def test_a_failed_upload_leaves_the_cursor_where_it_was(self):
        conn, bucket = seeded(), Bucket(fail_on="conf__side")
        with self.assertRaises(ConnectionError):
            ra.run(conn, bucket.put, now=NOW, run_id="r1")
        self.assertIsNone(conn.hget(ra.CURSOR_KEY, "conf:side:20261011"), "no object, no cursor move")
        self.assertIsNone(conn.get(ra.LOCK_KEY), "the lock is released on the way out")

    def test_a_crash_after_the_object_and_before_the_cursor_finds_the_object_and_moves_on(self):
        conn, bucket = seeded(), Bucket()
        ra.run(conn, bucket.put, now=NOW, run_id="r1")
        conn.hdel(ra.CURSOR_KEY, "conf:side:20261011")           # the cursor write that never happened
        out = ra.run(conn, bucket.put, now=NOW, run_id="r1-retry")
        side = [s for s in json.loads(bucket.objects[out["ledger"]])["streams"] if s["key"] == "conf:side:20261011"][0]
        self.assertEqual("exists", side["written"])
        self.assertEqual(conn.data["conf:side:20261011"][-1][0], conn.hget(ra.CURSOR_KEY, "conf:side:20261011"))

    def test_an_archived_object_is_never_overwritten(self):
        bucket = Bucket()
        bucket.put("a/b.jsonl", b"one", "application/x-ndjson")
        with self.assertRaises(ra.Exists):
            bucket.put("a/b.jsonl", b"two", "application/x-ndjson")
        self.assertEqual(b"one", bucket.objects["a/b.jsonl"])

    def test_a_burst_larger_than_one_run_is_carried_over_and_said(self):
        conn, bucket = FakeRedis(), Bucket()
        for k in range(7):
            conn.xadd("fleet:inbox:grok", {"text": str(k)})
        original = ra.MAX_PER_STREAM
        ra.MAX_PER_STREAM = 5
        try:
            first = ra.run(conn, bucket.put, now=NOW, run_id="r1")
            rows = json.loads(bucket.objects[first["ledger"]])["streams"]
            self.assertEqual((5, True), (rows[0]["new"], rows[0]["more"]))
            second = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=1), run_id="r2")
            self.assertEqual(2, second["new"])
        finally:
            ra.MAX_PER_STREAM = original


class OneArchiverAtATime(unittest.TestCase):
    def test_a_second_run_while_the_lock_is_held_does_nothing(self):
        conn, bucket = seeded(), Bucket()
        conn.set(ra.LOCK_KEY, "other-run")
        out = ra.run(conn, bucket.put, now=NOW, run_id="r2")
        self.assertEqual({"status": "locked", "holder": "other-run"}, out)
        self.assertEqual({}, bucket.objects)
        self.assertEqual("other-run", conn.get(ra.LOCK_KEY), "and leaves the holder's lock alone")


    def test_a_run_that_outlived_its_lock_does_not_release_the_next_holders(self):
        conn = seeded()
        bucket = Bucket()
        real_put = bucket.put

        def slow_put(name, body, ctype):
            conn.data[ra.LOCK_KEY] = "next-run"           # our lock expired; another archiver took it
            real_put(name, body, ctype)
        ra.run(conn, slow_put, now=NOW, run_id="r1")
        self.assertEqual("next-run", conn.get(ra.LOCK_KEY))


class TheLedger(unittest.TestCase):
    def test_the_ledger_hashes_every_object_it_names_and_verify_agrees(self):
        conn, bucket = seeded(), Bucket()
        out = ra.run(conn, bucket.put, now=NOW, run_id="r1")
        self.assertEqual(out["sha256"], hashlib.sha256(bucket.objects[out["ledger"]]).hexdigest())
        result = ra.verify(bucket.get, out["ledger"])
        self.assertEqual((3, True), (result["checked"], result["ok"]))

    def test_verify_catches_an_object_that_changed(self):
        conn, bucket = seeded(), Bucket()
        out = ra.run(conn, bucket.put, now=NOW, run_id="r1")
        victim = [n for n in bucket.objects if n.endswith(".jsonl")][0]
        bucket.objects[victim] += b'{"id": "9-9", "fields": {}}\n'
        result = ra.verify(bucket.get, out["ledger"])
        self.assertFalse(result["ok"])
        self.assertEqual([victim], result["mismatched"])

    def test_unread_is_counted_after_the_readers_cursor_and_absent_without_one(self):
        conn, bucket = seeded(), Bucket()
        conn.set("fleet:cursor:gemini", conn.data["fleet:inbox:gemini"][0][0])
        out = ra.run(conn, bucket.put, now=NOW, run_id="r1")
        rows = {s["key"]: s for s in json.loads(bucket.objects[out["ledger"]])["streams"]}
        self.assertEqual(1, rows["fleet:inbox:gemini"]["unread"])
        self.assertIsNone(rows["conf:side:20261011"]["unread"], "a room is not an inbox")

    def test_a_cursor_for_a_stream_that_expired_is_dropped(self):
        conn, bucket = seeded(), Bucket()
        conn.hset(ra.CURSOR_KEY, "conf:side:20261003", "5-0")
        out = ra.run(conn, bucket.put, now=NOW, run_id="r1")
        self.assertIsNone(conn.hget(ra.CURSOR_KEY, "conf:side:20261003"))
        self.assertEqual(["conf:side:20261003"], json.loads(bucket.objects[out["ledger"]])["dropped_cursors"])


class TheBoardRow(unittest.TestCase):
    def test_one_row_that_wakes_nobody_and_carries_no_entry_text(self):
        conn, bucket, rows = seeded(), Bucket(), []
        out = ra.run(conn, bucket.put, append=rows.append, now=NOW, run_id="r1")
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(("LEDGER", "ledger", "bus-reconciler"),
                         (row["action_type"], row["target_surface"], row["source_tag"]))
        self.assertIn("sha256=%s" % out["sha256"], row["payload"])
        self.assertIn("unread=gemini:-", row["payload"])
        for secret in ("room 0", "for gemini", "not a stream"):
            self.assertNotIn(secret, row["payload"])
        self.assertLessEqual(len(row["payload"]), ra.ROW_LIMIT)
        self.assertNotIn("|", row["payload"].split("|text=", 1)[1], "the text part cannot split the row")

    def test_an_unconfirmed_append_does_not_undo_the_archive(self):
        conn, bucket = seeded(), Bucket()

        def unconfirmed(spec):
            raise SystemExit(2)
        out = ra.run(conn, bucket.put, append=unconfirmed, now=NOW, run_id="r1")
        self.assertEqual("ok", out["status"])
        self.assertEqual("unconfirmed (exit 2)", out["row"])
        self.assertIn(out["ledger"], bucket.objects)


class DryRun(unittest.TestCase):
    def test_a_dry_run_writes_nothing_anywhere(self):
        conn, bucket, rows = seeded(), Bucket(), []
        out = ra.run(conn, bucket.put, append=rows.append, now=NOW, run_id="r1", dry_run=True)
        self.assertEqual("dry-run", out["status"])
        self.assertEqual(6, out["new"])
        self.assertEqual({}, bucket.objects)
        self.assertEqual([], rows)
        self.assertIsNone(conn.hget(ra.CURSOR_KEY, "conf:side:20261011"))


if __name__ == "__main__":
    unittest.main()

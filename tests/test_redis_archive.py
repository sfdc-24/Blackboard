"""The hourly Redis archive and its LEDGER (cloud/bus-reconciler/redis_archive.py; REDIS-PRIMARY D5).

What must hold: every entry reaches an object, and that object a durable ledger, before its cursor moves;
an archived range is never rewritten; a crash anywhere before the ledger re-finds the same object in any
later hour instead of losing or doubling it; every object is named by some ledger; one archiver at a time;
the ledger hashes what it names; the board row carries no entry's text."""
import copy
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

    def eval(self, script, numkeys, key, me):
        """DROP_LUA, atomic as on the server: delete only if the value is still ours."""
        assert script == ra.DROP_LUA
        if self.get(key) == me:
            self.delete(key)
            return 1
        return 0


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
        self.assertTrue(all(n.startswith("redis-archive/streams/") for n in archived),
                        "objects are named by stream and range, never by the hour")
        self.assertTrue(out["ledger"].startswith("redis-archive/2026-10-11/23/LEDGER-"))
        self.assertFalse(any("hello" in n for n in bucket.objects), "a string key is not a stream")
        side = [n for n in archived if "conf__side" in n][0]
        self.assertEqual(["room 0", "room 1", "room 2"], [e["fields"]["text"] for e in entries(bucket.objects[side])])

    def test_the_cursor_moves_to_the_last_entry_and_the_next_run_takes_only_what_is_new(self):
        conn, bucket = seeded(), Bucket()
        ra.run(conn, bucket.put, now=NOW, run_id="r1")
        last = conn.data["conf:side:20261011"][-1][0]
        self.assertEqual(last, conn.hget(ra.CURSOR_KEY, "conf:side:20261011"))
        conn.xadd("conf:side:20261011", {"by": "aya", "text": "later"})
        before = set(bucket.objects)
        out = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=1), run_id="r2")
        self.assertEqual(1, out["new"])
        later = [n for n in set(bucket.objects) - before]
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
        self.assertRegex(next(iter(added)), r"/LEDGER-r2-[0-9a-f]{12}\.json$")


class NeverLostNeverRewritten(unittest.TestCase):
    def test_a_failed_upload_leaves_the_cursor_where_it_was(self):
        conn, bucket = seeded(), Bucket(fail_on="conf__side")
        with self.assertRaises(ConnectionError):
            ra.run(conn, bucket.put, now=NOW, run_id="r1")
        self.assertIsNone(conn.hget(ra.CURSOR_KEY, "conf:side:20261011"), "no object, no cursor move")
        self.assertIsNone(conn.get(ra.LOCK_KEY), "the lock is released on the way out")

    def test_a_failed_ledger_moves_no_cursor_and_the_next_hour_names_the_same_objects(self):
        # Copilot on 0bda93e, both blockers at once: objects written, then the ledger upload fails. No cursor
        # may move, and the next run - an hour later - must write no second copy and must name them.
        conn = seeded()
        bucket = Bucket(fail_on="LEDGER-r1")
        with self.assertRaises(ConnectionError):
            ra.run(conn, bucket.put, now=NOW, run_id="r1")
        self.assertIsNone(conn.hget(ra.CURSOR_KEY, "conf:side:20261011"), "no ledger, no cursor")
        written = sorted(n for n in bucket.objects if n.endswith(".jsonl"))
        self.assertEqual(3, len(written))
        conn.xadd("conf:side:20261011", {"by": "aya", "text": "arrived meanwhile"})
        bucket.fail_on = None
        out = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=1), run_id="r2")
        ledger = json.loads(bucket.objects[out["ledger"]])
        named = sorted(r["object"] for r in ledger["streams"] if r.get("object"))
        self.assertEqual(written, named, "the same three objects, named by the next ledger")
        self.assertEqual(["exists"] * 3, [r["written"] for r in ledger["streams"] if r.get("object")])
        side = [r for r in ledger["streams"] if r["key"] == "conf:side:20261011"][0]
        self.assertTrue(side["more"], "the entry that arrived meanwhile waits for the next run")
        self.assertEqual(3, len([n for n in bucket.objects if n.endswith(".jsonl")]), "no second copy of any range")
        third = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=2), run_id="r3")
        self.assertEqual(1, third["new"])
        self.assertEqual(4, len([n for n in bucket.objects if n.endswith(".jsonl")]))

    def test_a_crash_between_the_object_and_the_ledger_in_the_same_hour_is_the_same(self):
        conn = seeded()
        bucket = Bucket(fail_on="LEDGER-r1")
        with self.assertRaises(ConnectionError):
            ra.run(conn, bucket.put, now=NOW, run_id="r1")
        bucket.fail_on = None
        out = ra.run(conn, bucket.put, now=NOW, run_id="r1-retry")
        rows = json.loads(bucket.objects[out["ledger"]])["streams"]
        self.assertTrue(all(r["written"] == "exists" for r in rows if r.get("object")))
        self.assertEqual(conn.data["conf:side:20261011"][-1][0], conn.hget(ra.CURSOR_KEY, "conf:side:20261011"))
        self.assertEqual({}, conn.data.get(ra.PENDING_KEY) or {}, "committed ranges leave nothing pending")

    def test_a_retried_task_with_the_same_run_id_writes_its_own_ledger(self):
        # Cloud Run retries a task under the same execution name. The first attempt wrote its ledger and died
        # before moving a cursor; the retry has more to archive. It must not find the first ledger ("exists")
        # and commit a range that ledger never named.
        conn, bucket = seeded(), Bucket()
        real_hset = conn.hset

        def dies_committing(key, field, value):
            if key == ra.CURSOR_KEY:
                raise ConnectionError("worker lost")
            real_hset(key, field, value)
        conn.hset = dies_committing
        with self.assertRaises(ConnectionError):
            ra.run(conn, bucket.put, now=NOW, run_id="r1")
        conn.hset = real_hset
        conn.xadd("fleet:inbox:aya", {"text": "for aya"})
        out = ra.run(conn, bucket.put, now=NOW, run_id="r1")
        self.assertEqual(2, len([n for n in bucket.objects if "/LEDGER-r1-" in n]), "a second ledger, not 'exists'")
        named = {r["key"] for r in json.loads(bucket.objects[out["ledger"]])["streams"] if r.get("object")}
        self.assertIn("fleet:inbox:aya", named, "the range the retry committed is the range its ledger names")

    def test_a_pending_range_whose_first_entry_was_trimmed_is_named_by_the_next_ledger(self):
        conn = seeded()
        bucket = Bucket(fail_on="LEDGER-r1")
        with self.assertRaises(ConnectionError):
            ra.run(conn, bucket.put, now=NOW, run_id="r1")
        orphan = [n for n in bucket.objects if "conf__side" in n][0]
        conn.data["conf:side:20261011"].pop(0)                  # trimmed by a writer's MAXLEN meanwhile
        bucket.fail_on = None
        out = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=1), run_id="r2")
        side = [r for r in json.loads(bucket.objects[out["ledger"]])["streams"] if r["key"] == "conf:side:20261011"][0]
        self.assertEqual(orphan, side["superseded"], "the earlier object is named, never left unnamed")
        self.assertEqual("created", side["written"])
        self.assertEqual({}, conn.data.get(ra.PENDING_KEY) or {})

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


class CopilotOn0bda93e(unittest.TestCase):
    def test_exactly_the_cap_is_not_more(self):
        conn, bucket = FakeRedis(), Bucket()
        for k in range(5):
            conn.xadd("fleet:inbox:grok", {"text": str(k)})
        original = ra.MAX_PER_STREAM
        ra.MAX_PER_STREAM = 5
        try:
            out = ra.run(conn, bucket.put, now=NOW, run_id="r1")
            row = json.loads(bucket.objects[out["ledger"]])["streams"][0]
            self.assertEqual((5, False), (row["new"], row["more"]), "five of five: nothing left")
        finally:
            ra.MAX_PER_STREAM = original

    def test_an_unread_backlog_past_the_cap_says_capped_not_a_count(self):
        conn, bucket = FakeRedis(), Bucket()
        conn.xadd("fleet:inbox:gemini", {"text": "first"})
        conn.set("fleet:cursor:gemini", conn.data["fleet:inbox:gemini"][0][0])
        for k in range(4):
            conn.xadd("fleet:inbox:gemini", {"text": str(k)})
        original = ra.UNREAD_CAP
        ra.UNREAD_CAP = 3
        try:
            rows, app = [], []
            out = ra.run(conn, bucket.put, append=app.append, now=NOW, run_id="r1")
            row = json.loads(bucket.objects[out["ledger"]])["streams"][0]
            self.assertEqual((3, True), (row["unread"], row["unread_capped"]))
            self.assertIn("unread=gemini:3+", app[0]["payload"], "'+' on the board: at least, not exactly")
        finally:
            ra.UNREAD_CAP = original

    def test_a_long_row_keeps_the_ledger_pointer_and_hash(self):
        conn, bucket, app = FakeRedis(), Bucket(), []
        for k in range(200):
            conn.xadd("fleet:inbox:agent-with-a-long-name-%03d" % k, {"text": "x"})
        original = ra.MAX_PER_STREAM
        ra.MAX_PER_STREAM = 0
        try:
            out = ra.run(conn, bucket.put, append=app.append, now=NOW, run_id="r1")
        finally:
            ra.MAX_PER_STREAM = original
        payload = app[0]["payload"]
        self.assertLessEqual(len(payload), ra.ROW_LIMIT)
        self.assertIn("ledger=gs://%s/%s" % (ra.DEFAULT_BUCKET, out["ledger"]), payload)
        self.assertIn("sha256=%s" % out["sha256"], payload)
        self.assertIn("(lists in the ledger)", payload)


class TheEntryPoint(unittest.TestCase):
    """main() is the scheduled job. Off (absent or anything but 'on') must touch nothing."""

    def call(self, env):
        from unittest import mock
        import redis_dual
        built, ran = [], []
        with mock.patch.dict(ra.os.environ, env, clear=False), \
                mock.patch.object(redis_dual, "client", side_effect=lambda *a, **k: built.append(1) or FakeRedis()), \
                mock.patch.object(ra, "run", side_effect=lambda *a, **k: ran.append(k) or {"status": "ok"}), \
                mock.patch.object(ra, "gcs_put", return_value=lambda *a: None):
            for name in ("REDIS_ARCHIVE_ENABLED", "REDIS_ARCHIVE_DRY_RUN"):
                if name not in env:
                    ra.os.environ.pop(name, None)
            code = ra.main([])
        return code, built, ran

    def test_absent_or_off_or_anything_else_does_nothing(self):
        for env in ({}, {"REDIS_ARCHIVE_ENABLED": "off"}, {"REDIS_ARCHIVE_ENABLED": "ON"},
                    {"REDIS_ARCHIVE_ENABLED": "1"}, {"REDIS_ARCHIVE_ENABLED": "true"}):
            code, built, ran = self.call(env)
            self.assertEqual((0, [], []), (code, built, ran), env)

    def test_on_runs_once_and_dry_run_is_passed_through(self):
        code, built, ran = self.call({"REDIS_ARCHIVE_ENABLED": "on", "REDIS_ARCHIVE_LEDGER_ROW": "off",
                                      "REDIS_ARCHIVE_DRY_RUN": "1"})
        self.assertEqual((0, 1, 1), (code, len(built), len(ran)))
        self.assertTrue(ran[0]["dry_run"])


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

    def test_cursor_on_5a487d3_a_trimmed_range_survives_two_more_failed_runs(self):
        # Cursor's exact sequence: the first run writes 1001-0_1003-0 and dies on its ledger; 1001-0 is trimmed;
        # the recovery run replaces the pending range and dies on its new upload; the next dies on its ledger;
        # the one after finishes. The first object must be named by that ledger before the cursor passes it.
        conn = seeded()
        bucket = Bucket(fail_on="LEDGER-r1")
        with self.assertRaises(ConnectionError):
            ra.run(conn, bucket.put, now=NOW, run_id="r1")
        orphan = [n for n in bucket.objects if "conf__side" in n][0]
        self.assertTrue(orphan.endswith("/1001-0_1003-0.jsonl"))
        conn.data["conf:side:20261011"].pop(0)
        for run_id, failing in (("r2", "conf__side__20261011/1002-0_"), ("r3", "LEDGER-r3")):
            bucket.fail_on = failing
            with self.assertRaises(ConnectionError):
                ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=1), run_id=run_id)
            self.assertEqual(orphan, conn.hget(ra.SUPERSEDED_KEY, "conf:side:20261011"),
                             "the pointer is durable after %s fails" % run_id)
            self.assertIsNone(conn.hget(ra.CURSOR_KEY, "conf:side:20261011"))
        bucket.fail_on = None
        out = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=2), run_id="r4")
        self.assertIn(orphan, json.loads(bucket.objects[out["ledger"]])["superseded"])
        self.assertEqual("1003-0", conn.hget(ra.CURSOR_KEY, "conf:side:20261011"))
        self.assertEqual({}, conn.data.get(ra.SUPERSEDED_KEY) or {}, "named, then cleared")
        later = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=3), run_id="r5")
        self.assertEqual([], json.loads(bucket.objects[later["ledger"]])["superseded"], "named once, not every hour")

    def test_a_crash_after_the_cursor_moved_leaves_no_false_superseded(self):
        # Phase 3 dies between moving a cursor and clearing its pending range. That range is committed (its
        # ledger is durable), so the next run must neither rewrite it nor report it as superseded.
        conn, bucket = seeded(), Bucket()
        real_hdel = conn.hdel

        def dies_clearing(key, *fields):
            if key == ra.PENDING_KEY:
                raise ConnectionError("worker lost")
            real_hdel(key, *fields)
        conn.hdel = dies_clearing
        with self.assertRaises(ConnectionError):
            ra.run(conn, bucket.put, now=NOW, run_id="r1")
        conn.hdel = real_hdel
        self.assertEqual("1003-0", conn.hget(ra.CURSOR_KEY, "conf:side:20261011"))
        out = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=1), run_id="r2")
        ledger = json.loads(bucket.objects[out["ledger"]])
        self.assertEqual(([], 0), (ledger["superseded"], ledger["totals"]["new"]))
        self.assertEqual({}, conn.data.get(ra.PENDING_KEY) or {})

    def test_a_lower_cap_after_a_crash_names_the_larger_range_it_cannot_rewrite(self):
        conn, bucket = FakeRedis(), Bucket(fail_on="LEDGER-r1")
        for k in range(7):
            conn.xadd("fleet:inbox:grok", {"text": str(k)})
        original = ra.MAX_PER_STREAM
        try:
            ra.MAX_PER_STREAM = 5
            with self.assertRaises(ConnectionError):
                ra.run(conn, bucket.put, now=NOW, run_id="r1")
            ra.MAX_PER_STREAM = 3
            bucket.fail_on = None
            out = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(hours=1), run_id="r2")
        finally:
            ra.MAX_PER_STREAM = original
        ledger = json.loads(bucket.objects[out["ledger"]])
        self.assertEqual(["redis-archive/streams/fleet__inbox__grok/1001-0_1005-0.jsonl"], ledger["superseded"])
        self.assertEqual("1003-0", conn.hget(ra.CURSOR_KEY, "fleet:inbox:grok"))

    def test_a_pending_range_for_a_stream_that_expired_is_named_and_dropped(self):
        conn = seeded()
        bucket = Bucket(fail_on="LEDGER-r1")
        with self.assertRaises(ConnectionError):
            ra.run(conn, bucket.put, now=NOW, run_id="r1")
        orphan = [n for n in bucket.objects if "conf__side" in n][0]
        conn.delete("conf:side:20261011")                       # the day's room expired meanwhile
        bucket.fail_on = None
        out = ra.run(conn, bucket.put, now=NOW + datetime.timedelta(days=1), run_id="r2")
        self.assertEqual([orphan], json.loads(bucket.objects[out["ledger"]])["superseded"])
        self.assertNotIn("conf:side:20261011", conn.data.get(ra.PENDING_KEY) or {})

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
    def test_a_dry_run_writes_no_object_row_or_cursor_and_releases_its_lock(self):
        # Not zero-write (Aya, 2026-10-12): it takes gov:archive:lock so it never overlaps a real run. That
        # lock is its ONLY Redis write, and it is gone when the dry run returns.
        conn, bucket, rows = seeded(), Bucket(), []
        before = copy.deepcopy(conn.data)
        out = ra.run(conn, bucket.put, append=rows.append, now=NOW, run_id="r1", dry_run=True)
        self.assertEqual("dry-run", out["status"])
        self.assertEqual(6, out["new"])
        self.assertEqual({}, bucket.objects)
        self.assertEqual([], rows)
        self.assertIsNone(conn.hget(ra.CURSOR_KEY, "conf:side:20261011"))
        self.assertEqual(before, conn.data, "the lock is released and nothing else in Redis changed")


class TheDeployScriptsPinTheImage(unittest.TestCase):
    """archive.sh deploys only a build it is told by name; deploy.sh builds to the tag it is given. The
    shared :v1 may predate redis_archive.py, and rebuilding it could move every job on the image."""

    def gcloud(self, args, env=None):
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            fake = os.path.join(tmp, "gcloud")
            with open(fake, "w") as f:
                f.write('#!/usr/bin/env bash\necho "$*" >> "%s"\nexit 0\n' % log)
            os.chmod(fake, 0o755)
            run_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GCLOUD": fake,
                       "REDIS_HOST": "192.0.2.10"}             # TEST-NET-1: an address that is nobody's
            run_env.update(env or {})
            done = subprocess.run(["bash"] + args, cwd=str(ROOT), env=run_env, capture_output=True, text=True)
            calls = open(log).read() if os.path.exists(log) else ""
        return done.returncode, calls, done.stderr

    def test_archive_deploy_refuses_without_a_named_image_and_touches_nothing(self):
        code, calls, err = self.gcloud(["cloud/bus-reconciler/archive.sh", "deploy"])
        self.assertNotEqual(0, code)
        self.assertEqual("", calls, "no gcloud call before the refusal")
        self.assertIn("set IMAGE", err)

    def test_archive_deploy_runs_the_named_build(self):
        pinned = "us-central1-docker.pkg.dev/p/r/bus-reconciler@sha256:" + "a" * 64
        code, calls, _ = self.gcloud(["cloud/bus-reconciler/archive.sh", "deploy"], {"IMAGE": pinned})
        self.assertEqual(0, code)
        self.assertIn("--image %s " % pinned, calls)
        self.assertNotIn(":v1", calls)

    def test_deploy_build_writes_the_tag_it_is_given_and_v1_only_by_default(self):
        tag = "us-central1-docker.pkg.dev/sfdc24/cloud-run-source-deploy/bus-reconciler:git-f488553"
        _, calls, _ = self.gcloud(["cloud/bus-reconciler/deploy.sh", "build"], {"IMAGE": tag})
        self.assertIn("_IMAGE=%s" % tag, calls)
        self.assertNotIn(":v1", calls)
        _, calls, _ = self.gcloud(["cloud/bus-reconciler/deploy.sh", "build"])
        self.assertIn("_IMAGE=us-central1-docker.pkg.dev/sfdc24/cloud-run-source-deploy/bus-reconciler:v1", calls)


if __name__ == "__main__":
    unittest.main()

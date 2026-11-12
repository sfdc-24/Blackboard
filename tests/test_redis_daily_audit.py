"""The daily Redis lane audit. No network: a fake stream, a fake append, a memory spill.

The property that matters most is the one bus_reconcile already refuses. An unreachable Redis, or a
day that came back empty, is UNKNOWN. It is not a completed audit of zero. The others are the
copy itself: one row per entry, a re-run that does not duplicate, call-bridge words that never
reach the board, a long entry that spills instead of being cut quietly, and a page that stops at
midnight.
"""
import datetime
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import board_row                                                        # noqa: E402
import redis_daily_audit as audit                                       # noqa: E402
import redis_gov                                                        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DAY = datetime.date(2026, 11, 7)
_MISSING = object()
START, END = audit.day_bounds(DAY)
SECRET = "EXAMPLE FIXTURE TEXT 0001"
NOW = datetime.datetime(2026, 11, 8, 6, 10, tzinfo=datetime.timezone.utc)


def sid(ms, seq=0):
    return "%d-%d" % (ms, seq)


def entry(by, text, **extra):
    fields = {"by": by, "at": "2026-11-07T12:00:00Z", "text": text}
    fields.update(extra)
    return fields


class MemSpill:
    """fit(write=False) names the object; commit() stores the bytes. Both stay in memory."""

    def __init__(self):
        self.objects = {}

    def plan(self, key, full):
        return "mem://" + board_row.object_name(key, full)

    def __call__(self, key, full):
        pointer = self.plan(key, full)
        self.objects[pointer] = full
        return pointer


class FakeRedis:
    """XRANGE with Redis' bounds: inclusive unless the id starts with '(', paged by count.

    ignore_max pretends the server handed back the next day anyway, which is what the day-boundary
    check in the reader is there for.
    """

    def __init__(self, streams, fail=False, ignore_max=False):
        self.streams = {key: sorted(rows, key=lambda item: _order(item[0]))
                        for key, rows in streams.items()}
        self.fail = fail
        self.ignore_max = ignore_max
        self.calls = []

    def xrange(self, key, min="-", max="+", count=None):
        self.calls.append({"key": key, "min": min, "max": max, "count": count})
        if self.fail:
            raise ConnectionError("redis unreachable")
        upper = None if self.ignore_max else _bound(max)
        lower = _bound(min)
        out = []
        for entry_id, fields in self.streams.get(key, []):
            cur = _order(entry_id)
            if lower is not None and not _keeps(cur, lower, upper=False):
                continue
            if upper is not None and not _keeps(cur, upper, upper=True):
                continue
            out.append((entry_id, dict(fields)))
            if count is not None and len(out) >= count:
                break
        return out


def _order(entry_id):
    ms, _, seq = str(entry_id).partition("-")
    return int(ms), int(seq or 0)


def _bound(spec):
    if spec in (None, "-", "+"):
        return None
    exclusive = str(spec).startswith("(")
    raw = str(spec)[1:] if exclusive else str(spec)
    return _order(raw), exclusive


def _keeps(cur, bound, upper):
    point, exclusive = bound
    if upper:
        return cur < point if exclusive else cur <= point
    return cur > point if exclusive else cur >= point


class DailyAudit(unittest.TestCase):
    def setUp(self):
        self.posted = []
        self.spill = MemSpill()

    def append(self, row):
        self.posted.append(row)

    def go(self, conn, agents, already=_MISSING, page_size=10, day=DAY):
        if already is _MISSING:
            already = set()
        return audit.run(conn, day, agents=agents, already=already, append=self.append,
                         spill=self.spill, page_size=page_size, clock=lambda: NOW)

    def blobs(self):
        parts = ["\n".join("\n".join(str(cell) for cell in row) for row in self.posted)]
        parts.extend(self.spill.objects.values())
        return "\n".join(parts)

    def entry_rows(self):
        return [row for row in self.posted if not str(row[0]).startswith("REDIS-AUDIT-SUMMARY-")]

    def summary(self):
        rows = [row for row in self.posted if str(row[0]).startswith("REDIS-AUDIT-SUMMARY-")]
        self.assertEqual(1, len(rows), self.posted)
        return rows[0]

    def test_one_row_per_entry_with_the_audit_columns(self):
        grok = [
            (sid(START, 0), entry("grok", "first", thread_id="th-1")),
            (sid(START + 1000, 1), entry("grok", "second")),
        ]
        cursor = [(sid(START + 5000, 0), entry("cursor", "from cursor", thread_id="th-9"))]
        conn = FakeRedis({"fleet:inbox:grok": grok, "fleet:inbox:cursor": cursor})
        result = self.go(conn, ["grok", "cursor"])
        rows = self.entry_rows()
        self.assertEqual(audit.COMPLETE, result["verdict"])
        self.assertEqual(3, result["entries"])
        self.assertEqual(3, len(rows))
        self.assertEqual(4, len(self.posted))
        seen = set()
        for row in rows:
            self.assertEqual(10, len(row))
            self.assertEqual(["redis-audit", "owner", "AUDIT", "DONE", "FLEET"],
                             [row[2], row[3], row[4], row[6], row[7]])
            self.assertLessEqual(len(row[5]), board_row.ROW_LIMIT)
            self.assertTrue(row[5].startswith("BCB|v=1|id=%s|phase=AUDIT|" % row[0]))
            seen.add(row[0])
        self.assertEqual(3, len(seen))
        body = self.blobs()
        for text in ("first", "second", "from cursor"):
            self.assertEqual(1, body.count("text=%s" % text), text)
        self.assertIn("thread_id=th-1", body)
        self.assertIn("thread_id=th-9", body)
        summary = self.summary()
        self.assertEqual("REDIS-AUDIT-SUMMARY-20261107", summary[0])
        self.assertIn("verdict=COMPLETE", summary[5])
        self.assertIn("grok:2:%s..%s" % (sid(START, 0), sid(START + 1000, 1)), summary[5])
        self.assertIn("cursor:1:%s..%s" % (sid(START + 5000, 0), sid(START + 5000, 0)), summary[5])

    def test_a_rerun_appends_nothing(self):
        rows = [(sid(START, 0), entry("grok", "only")),
                (sid(START + 1, 0), entry("grok", "also"))]
        conn = FakeRedis({"fleet:inbox:grok": rows})
        first = self.go(conn, ["grok"])
        self.assertEqual(3, first["appended"])          # two entries and the summary
        ids = {row[0] for row in self.posted}
        posted_before = len(self.posted)
        calls_before = len(conn.calls)
        # A new process: the ids come back from the board, not from this run's memory.
        second = self.go(conn, ["grok"], already=set(ids))
        self.assertEqual(posted_before, len(self.posted))
        self.assertEqual(0, second["appended"])
        self.assertEqual(2, second["skipped"])
        self.assertGreater(len(conn.calls), calls_before, "a re-run reads the day again; it keeps no cursor")
        self.assertEqual(audit.COMPLETE, second["verdict"])
        # And one id already on the board is not the row that gets appended.
        self.posted.clear()
        seeded = audit.entry_id("grok", sid(START, 0))
        third = self.go(conn, ["grok"], already={seeded})
        self.assertNotIn(seeded, {row[0] for row in self.entry_rows()})
        self.assertEqual(1, len(self.entry_rows()))
        self.assertEqual(audit.COMPLETE, third["verdict"])

    def test_ids_on_keeps_this_day_only(self):
        today = audit.entry_id("grok", sid(START, 0))
        tomorrow = audit.entry_id("grok", sid(END, 0))
        summary = audit.summary_id(DAY)
        found = audit.ids_on([[today], [tomorrow], [summary], ["REDIS-AUDIT-OTHER"]], DAY)
        self.assertEqual({today, summary}, found)

    def test_call_bridge_text_is_withheld(self):
        secret = SECRET + " " + ("private word " * 80)
        ask = "call-abc123"
        conn = FakeRedis({"fleet:inbox:grok": [
            (sid(START, 0), entry("call-bridge", secret, ask=ask, thread_id="th-call")),
            (sid(START + 1, 0), entry("grok", "an ordinary note")),
        ]})
        self.go(conn, ["grok"])
        blob = self.blobs()
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("private word", blob)
        self.assertIn(redis_gov.WITHHELD, blob)
        self.assertIn("ask=%s" % ask, blob)
        self.assertIn("hash=%s" % hashlib.sha256(secret.encode("utf-8")).hexdigest(), blob)
        self.assertIn("thread_id=th-call", blob)
        self.assertIn("text=an ordinary note", blob)
        for row in self.posted:
            self.assertLessEqual(len(row[5]), board_row.ROW_LIMIT)
            self.assertNotIn(SECRET, row[8])

    def test_a_long_entry_spills_and_stays_under_the_row_limit(self):
        text = ("alpha " * 800) + "ENDTOKEN"
        self.assertGreater(len(text), board_row.ROW_LIMIT)
        conn = FakeRedis({"fleet:inbox:grok": [(sid(START, 0), entry("grok", text))]})
        self.go(conn, ["grok"])
        row = self.entry_rows()[0]
        self.assertLessEqual(len(row[5]), board_row.ROW_LIMIT)
        self.assertIn("full=", row[5])
        self.assertIn("chars=", row[5])
        self.assertIn("sha256=", row[5])
        self.assertNotIn("ENDTOKEN", row[5])
        self.assertNotIn("ENDTOKEN", row[8])
        stored = "\n".join(self.spill.objects.values())
        self.assertIn("ENDTOKEN", stored)

    def test_unreachable_redis_is_unknown_never_a_completed_zero(self):
        conn = FakeRedis({"fleet:inbox:grok": [(sid(START, 0), entry("grok", "never read"))]}, fail=True)
        result = self.go(conn, ["grok"])
        self.assertEqual(audit.UNKNOWN, result["verdict"])
        self.assertIsNone(result["entries"])
        self.assertNotIn("never read", self.blobs())
        summary = self.summary()
        self.assertIn("verdict=UNKNOWN", summary[5])
        self.assertNotIn("verdict=COMPLETE", summary[5])
        self.assertIn("grok:unmeasured", summary[5])
        self.assertIn("not a count of zero", summary[5])
        self.assertEqual([], self.entry_rows())

        self.posted.clear()
        missed = self.go(None, ["grok"])
        self.assertEqual(audit.UNKNOWN, missed["verdict"])
        self.assertIsNone(missed["entries"])
        self.assertEqual([], self.entry_rows())

    def test_an_empty_day_is_unknown_not_a_complete_zero(self):
        conn = FakeRedis({"fleet:inbox:grok": []})
        result = self.go(conn, ["grok"])
        self.assertEqual(audit.UNKNOWN, result["verdict"])
        self.assertEqual(0, result["entries"])
        self.assertEqual([], self.entry_rows())
        summary = self.summary()
        self.assertIn("verdict=UNKNOWN", summary[5])
        self.assertNotIn("verdict=COMPLETE", summary[5])
        self.assertIn("not a complete audit of zero", summary[5])
        self.assertIn("grok:0", summary[5])

    def test_paging_stops_at_the_day_boundary(self):
        in_day = [
            (sid(START, 0), entry("grok", "page-one")),
            (sid(START + 10, 0), entry("grok", "page-two")),
            (sid(END - 1, 0), entry("grok", "last-millisecond")),
        ]
        later = [
            (sid(END, 0), entry("grok", "tomorrow-midnight")),
            (sid(END + 1000, 1), entry("grok", "tomorrow-later")),
        ]
        conn = FakeRedis({"fleet:inbox:grok": in_day + later})
        result = self.go(conn, ["grok"], page_size=2)
        self.assertEqual(audit.COMPLETE, result["verdict"])
        self.assertEqual(3, len(self.entry_rows()))
        blob = self.blobs()
        for text in ("page-one", "page-two", "last-millisecond"):
            self.assertIn(text, blob)
        self.assertNotIn("tomorrow-midnight", blob)
        self.assertNotIn("tomorrow-later", blob)
        self.assertGreaterEqual(len(conn.calls), 2)
        bound = "(%d-0" % END
        for call in conn.calls:
            self.assertEqual(bound, call["max"], call)
            self.assertEqual(2, call["count"])
        self.assertEqual("%d-0" % START, conn.calls[0]["min"])
        self.assertEqual("(" + sid(START + 10, 0), conn.calls[1]["min"])

        # The server returns the next day anyway. The reader still stops at midnight.
        self.posted.clear()
        leaky = FakeRedis({"fleet:inbox:grok": in_day + later}, ignore_max=True)
        self.go(leaky, ["grok"], page_size=2)
        leaked = self.blobs()
        self.assertIn("last-millisecond", leaked)
        self.assertNotIn("tomorrow-midnight", leaked)
        self.assertNotIn("tomorrow-later", leaked)

    def test_a_failed_board_read_appends_nothing(self):
        conn = FakeRedis({"fleet:inbox:grok": [(sid(START, 0), entry("grok", "held"))]})
        result = self.go(conn, ["grok"], already=None)
        self.assertEqual(audit.UNKNOWN, result["verdict"])
        self.assertEqual([], self.posted)
        self.assertFalse(result["recorded"])
        self.assertNotIn("held", self.blobs())

    def test_the_flag_is_exactly_on_or_nothing_runs(self):
        calls = []

        def connect():
            calls.append("connect")
            return None

        for value in (None, "", "ON", "true", "1", "yes", "on\n", " on"):
            environ = {} if value is None else {audit.FLAG: value}
            code = audit.main([], environ=environ, connect=connect)
            self.assertEqual(0, code, value)
            self.assertEqual([], calls, value)

        posted = []
        code = audit.main(
            ["--day", "2026-11-07"],
            environ={audit.FLAG: "on"},
            connect=lambda: None,
            append=posted.append,
            read_existing=lambda _env, _day: set(),
            spill=MemSpill(),
            env={"BUS_URL": "http://bus.example", "BUS_SECRET": "test"},
            now=NOW,
        )
        self.assertEqual(audit.EXIT[audit.UNKNOWN], code)
        self.assertEqual(1, len(posted))
        self.assertIn("verdict=UNKNOWN", posted[0][5])
        self.assertTrue(posted[0][0].startswith("REDIS-AUDIT-SUMMARY-"))

    def test_the_default_day_is_the_last_complete_utc_day(self):
        self.assertEqual(DAY, audit.default_day(NOW))
        self.assertEqual(datetime.date(2026, 11, 7),
                         audit.default_day(datetime.datetime(2026, 11, 8, 0, 0, tzinfo=datetime.timezone.utc)))

    def test_existing_ids_are_read_from_the_board_before_the_copy(self):
        asked = {}

        def reader(_env, since=None, match=None):
            asked["since"] = since
            asked["match"] = match
            return {"rows": [[audit.entry_id("grok", sid(START, 0))],
                             [audit.entry_id("grok", sid(END, 0))]]}

        found = audit.existing_ids({}, DAY, reader=reader)
        self.assertEqual("REDIS-AUDIT", asked["match"])
        self.assertEqual("2026-11-06T23:59:59Z", asked["since"])
        self.assertEqual({audit.entry_id("grok", sid(START, 0))}, found)

    def test_post_row_is_one_bus_append(self):
        sent = []

        def fetch(url, data, tries):
            sent.append((url, data, tries))
            return "ok"

        audit.post_row({"BUS_URL": "http://bus.example", "BUS_SECRET": "s"}, ["RID"], fetch)
        self.assertEqual([("http://bus.example", {
            "action": "append", "secret": "s", "title": "Blackboard - Alpha DB", "sheetRow": ["RID"],
        }, 1)], sent)

    def test_agents_are_the_principals_plus_claude_api(self):
        found = audit.agents()
        raw = json.loads((ROOT / "scripts" / "redis_acl.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(set(raw["principals"]) | {"claude-api"}), found)
        self.assertIn("claude-api", found)
        self.assertNotIn("codex", found)          # an alias, not a lane
        self.assertIn("chatgpt-codex-desktop", found)

    def test_the_scheduler_step_is_documented_and_does_not_run(self):
        script = ROOT / "cloud" / "bus-reconciler" / "deploy.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn("10 6 * * *", text)
        self.assertIn("REDIS_DAILY_AUDIT", text)
        self.assertIn("NOT APPLIED", text)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "gcloud"
            calls = Path(tmp) / "calls"
            fake.write_text("#!/bin/sh\necho INVOKED >> '%s'\nexit 0\n" % calls, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            env = dict(os.environ)
            env["GCLOUD"] = str(fake)
            env["PATH"] = tmp + os.pathsep + env.get("PATH", "")
            proc = subprocess.run(["bash", str(script), "audit-schedule"], cwd=str(ROOT),
                                  capture_output=True, text=True, env=env)
        self.assertNotEqual(0, proc.returncode, proc.stderr)
        self.assertIn("NOT APPLIED", proc.stdout)
        self.assertIn("10 6 * * *", proc.stdout)
        self.assertIn("REDIS_DAILY_AUDIT=off", proc.stdout)
        self.assertFalse(calls.exists(), "audit-schedule invoked gcloud: " + proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

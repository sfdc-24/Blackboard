#!/usr/bin/env python3
"""scripts/redis_inbox.py read: after a cursor, page by page, never moving over what it did not read.

Aya, 2026-10-12 (row AYA-392-MERGED-CURSOR-INTEGRATION-20261012): the inbox read must consume the worker's
after/last/more (PR 392) and persist the recipient cursor; its newest-N, string-id, first-bracket reader
could not. The 200-character value preview and the 1500-character row stay as they are, and the cursor
never moves over truncated or unparsed data.

Every page here goes through the code the bus-requests job runs - bus_request.handle(), redis_gov - and
through board_row.fit(), which is what append.py does to a row over 1500 characters. Offline.
"""
import datetime
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import board_row                                                     # noqa: E402
import bus_request                                                   # noqa: E402
import redis_inbox as inbox                                          # noqa: E402
from test_redis_gov import ACL, FakeRedis, run                       # noqa: E402

NOW = datetime.datetime(2026, 10, 12, 2, 0, tzinfo=datetime.timezone.utc)
KEY = "fleet:inbox:claude-code-cli"


class Worker:
    """The board between the reader and Redis: a request row in, the RESULT row the worker appends out."""

    def __init__(self, conn, legacy=False):
        self.conn, self.answers, self.asked, self.legacy = conn, {}, [], legacy
        self.drop = set()                       # request ops whose answer never arrives

    def post(self, spec):
        self.asked.append(spec["payload"])
        payload = spec["payload"]
        if self.legacy:                         # a worker image from before PR 392 ignores after=
            payload = "|".join(p for p in payload.split("|") if not p.startswith("after="))
        row = [spec["row_id"], (NOW - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               spec["source_tag"], spec["target_surface"], spec["action_type"], payload, "", "", "", ""]
        out = []
        bus_request.handle([row], self.conn, now=NOW, append=out.append)
        result = out[-1]
        fitted, _ = board_row.fit("", result["payload"], key=result["row_id"], spill=None)
        if not any(("op=%s|" % op) in spec["payload"] for op in self.drop):
            self.answers[spec["row_id"]] = fitted
        return spec["row_id"]

    def answer(self, rid, wait, since):
        return {"from": "bus-reconciler", "payload": self.answers[rid]} if rid in self.answers else None


def stream(texts):
    conn = FakeRedis()
    for text in texts:
        assert run(conn, "grok", "xadd", KEY, val=text)["ok"]
    return conn


class Reading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bookmark = os.path.join(self.tmp.name, "seen.json")

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, worker, *argv):
        args = inbox.build_parser().parse_args(["read", "--wait", "0"] + list(argv))
        printed = []
        with mock.patch.object(inbox, "post", worker.post), mock.patch.object(inbox, "answer", worker.answer), \
                mock.patch.object(inbox, "BOOKMARK", self.bookmark), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            code = inbox.cmd_read(args)
        return code, "\n".join(printed)

    def saved(self):
        try:
            with open(self.bookmark, encoding="utf-8") as fh:
                return json.load(fh)["last_id"]
        except OSError:
            return None

    def shown(self, out):
        return [line.split(" | ")[3] for line in out.splitlines() if line.count(" | ") >= 3]

    def test_a_burst_past_ten_is_read_whole_oldest_first_and_the_cursor_is_persisted(self):
        conn = stream(["m%d" % k for k in range(1, 12)])            # ids 1-0 .. 11-0: "9-0" > "10-0" as strings
        worker = Worker(conn)
        code, out = self.read(worker)
        self.assertEqual(0, code, out)
        self.assertEqual(["m%d" % k for k in range(1, 12)], self.shown(out))
        self.assertEqual("11-0", self.saved())
        self.assertEqual("11-0", conn.data.get("fleet:cursor:claude-code-cli"), "the cursor is in Redis too")
        self.assertTrue(all("after=" in p for p in worker.asked if "op=xrange" in p))
        code, out = self.read(worker)                                 # nothing new: one page, no cursor write
        self.assertEqual([], self.shown(out))
        self.assertEqual(1, sum(1 for p in worker.asked[-2:] if "op=xrange" in p))

    def test_a_page_too_long_for_one_row_is_asked_again_smaller_and_nothing_is_skipped(self):
        # Values just under the 200-character preview: four fit one row (the default page), eight do not.
        texts = ["%02d " % k + "x" * 190 for k in range(1, 9)]
        worker = Worker(stream(texts))
        code, out = self.read(worker, "--count", "8", "--pages", "12")
        self.assertEqual(0, code, out)
        self.assertIn("page cut", out)
        self.assertEqual(texts, self.shown(out), "each once, in order")
        self.assertEqual("8-0", self.saved())

    def test_a_message_cut_at_the_preview_bound_stops_the_cursor_before_it(self):
        texts = ["one", "two", "y" * 250, "four"]
        worker = Worker(stream(texts))
        code, out = self.read(worker)
        self.assertEqual("2-0", self.saved(), "moved over what arrived whole, not over the cut one")
        self.assertIn("cursor stops before it", out)
        self.assertIn("[cut by the reader's bound]", out)
        self.assertNotIn("four", self.shown(out))
        code, out = self.read(worker, "--past-cut")
        self.assertEqual("4-0", self.saved())
        self.assertIn("four", self.shown(out))

    def test_a_worker_from_before_after_moves_nothing(self):
        worker = Worker(stream(["a", "b"]), legacy=True)
        code, out = self.read(worker)
        self.assertEqual(3, code, out)
        self.assertIn("NOT READ", out)
        self.assertIn("predate", out)
        self.assertIsNone(self.saved())
        self.assertNotIn("fleet:cursor:claude-code-cli", worker.conn.data)

    def test_no_answer_moves_nothing(self):
        worker = Worker(stream(["a"]))
        worker.drop.add("xrange")
        code, out = self.read(worker)
        self.assertEqual(2, code)
        self.assertIsNone(self.saved())

    def test_an_unconfirmed_redis_cursor_leaves_the_local_one_standing(self):
        worker = Worker(stream(["a", "b"]))
        worker.drop.add("set")
        code, out = self.read(worker)
        self.assertEqual("2-0", self.saved())
        self.assertIn("NOT confirmed, the local one stands", out)

    def test_the_default_page_of_four_fits_one_row_at_the_preview_bound(self):
        # The default is sized so the worst case still fits: four values at the full 200-character preview.
        worker = Worker(stream(["z" * 200 for _ in range(5)]))
        code, out = self.read(worker)
        self.assertNotIn("page cut", out)
        self.assertEqual("5-0", self.saved())

    def test_cursor_on_f2c3a3d_a_field_named_id_cannot_carry_the_cursor_past_a_cut_entry(self):
        # Raw 1-0 with a field id=100-0, raw 2-0 with id=101-0 and a 250-character text, raw 3-0 unread.
        # The cursor used to be saved at the field's 100-0, behind which both later entries vanished.
        conn = stream([])
        conn.data[KEY] = [("1-0", {"id": "100-0", "text": "short", "by": "x", "at": "t"}),
                          ("2-0", {"id": "101-0", "text": "y" * 250, "by": "x", "at": "t"}),
                          ("3-0", {"text": "after-cut", "by": "x", "at": "t"})]
        worker = Worker(conn)
        code, out = self.read(worker)
        self.assertEqual("1-0", self.saved(), "stopped at the raw id before the cut entry")
        self.assertEqual("1-0", conn.data.get("fleet:cursor:claude-code-cli"))
        self.assertNotIn("after-cut", out)
        code, out = self.read(worker)
        self.assertEqual("1-0", self.saved(), "the cut entry still stops it")
        code, out = self.read(worker, "--past-cut")
        self.assertEqual("3-0", self.saved())
        self.assertIn("after-cut", self.shown(out))

    def test_a_worker_that_shows_a_field_named_id_over_the_stream_id_moves_nothing(self):
        import redis_gov
        conn = stream([])
        conn.data[KEY] = [("1-0", {"id": "100-0", "text": "a", "by": "x", "at": "t"}),
                          ("2-0", {"text": "b", "by": "x", "at": "t"})]
        old_view = lambda i, f: {"id": str(i), **{k: redis_gov.preview(v) for k, v in f.items()}}  # noqa: E731
        with mock.patch.object(redis_gov, "entry_view", old_view):
            code, out = self.read(Worker(conn))
        self.assertEqual(3, code, out)
        self.assertIn("shown ids are not stream ids in order", out)
        self.assertIsNone(self.saved())

    def test_an_answer_from_anyone_but_the_worker_moves_nothing(self):
        worker = Worker(stream(["a", "b"]))
        real = worker.answer
        worker.answer = lambda rid, wait, since: dict(real(rid, wait, since), **{"from": "grok"})
        code, out = self.read(worker)
        self.assertEqual(3, code, out)
        self.assertIn("not bus-reconciler", out)
        self.assertIsNone(self.saved())

    def test_the_page_cap_says_more_is_waiting(self):
        worker = Worker(stream(["m%d" % k for k in range(1, 10)]))
        code, out = self.read(worker, "--count", "2", "--pages", "2")
        self.assertEqual("4-0", self.saved())
        self.assertIn("more is waiting after 4-0", out)


class ThePage(unittest.TestCase):
    """page_of() judges one RESULT row. Only a whole page is "ok"."""

    def payload(self, text):
        return "BCB|v=1|id=R|phase=AYA_RESULT|from=bus-reconciler|to=claude-code-cli|answers=Q|text=" + text

    def test_a_whole_page(self):
        page = inbox.page_of(self.payload('OK xrange %s [after=9-0 more=false last=10-0]: '
                                          '[{"at": "t", "by": "grok", "id": "10-0", "text": "hi"}]' % KEY),
                             KEY, "9-0")
        self.assertEqual(("ok", "10-0", False), (page["status"], page["last"], page["more"]))

    def test_a_body_cut_at_1500_is_cut(self):
        page = inbox.page_of(self.payload('OK xrange %s [after=0-0 more=true last=4-0]: [{"at": "t", "te...'
                                          % KEY), KEY, "0-0")
        self.assertEqual("cut", page["status"])

    def test_a_truncated_or_spilled_row_is_cut(self):
        for mark in ("truncated=1|chars=1700|sha256=ab|", "full=gs://x/y|chars=1700|sha256=ab|"):
            page = inbox.page_of("BCB|v=1|id=R|answers=Q|%stext=OK xrange %s [after=0-0 more=false "
                                 "last=1-0]: []" % (mark, KEY), KEY, "0-0")
            self.assertEqual("cut", page["status"], mark)

    def test_the_wrong_page_is_not_read(self):
        text = 'OK xrange %s [after=1-0 more=false last=2-0]: [{"id": "2-0", "text": "x"}]'
        self.assertEqual("unparsed", inbox.page_of(self.payload(text % KEY), KEY, "0-0")["status"])
        self.assertEqual("unparsed", inbox.page_of(self.payload(text % "fleet:inbox:grok"), KEY, "1-0")["status"])

    def test_last_must_fit_the_entries(self):
        empty_moved = 'OK xrange %s [after=1-0 more=false last=2-0]: []' % KEY
        full_stayed = 'OK xrange %s [after=1-0 more=false last=1-0]: [{"id": "2-0"}]' % KEY
        backwards = 'OK xrange %s [after=5-0 more=false last=4-0]: [{"id": "4-0"}]' % KEY
        for text in (empty_moved, full_stayed, backwards):
            self.assertEqual("unparsed", inbox.page_of(self.payload(text), KEY, text.split("after=")[1].split()[0])
                             ["status"], text)

    def test_a_last_beyond_the_entries_shown_is_not_read(self):
        # Cursor on f2c3a3d: last=50-0 with one entry 1-0 moved both cursors to 50-0.
        text = 'OK xrange %s [after=0-0 more=false last=50-0]: [{"id": "1-0", "text": "x"}]' % KEY
        self.assertEqual("unparsed", inbox.page_of(self.payload(text), KEY, "0-0")["status"])

    def test_shown_ids_must_be_in_order_after_the_cursor(self):
        for ids in (("2-0", "1-0"), ("1-0", "1-0"), ("100-0", "x")):
            body = json.dumps([{"id": i, "text": "t"} for i in ids])
            text = "OK xrange %s [after=0-0 more=false last=%s]: %s" % (KEY, ids[-1], body)
            self.assertEqual("unparsed", inbox.page_of(self.payload(text), KEY, "0-0")["status"], ids)

    def test_ids_are_numbers_in_ascii_digits(self):
        self.assertLess(inbox.stream_id("9-0"), inbox.stream_id("10-0"))
        for bad in ("", "9", "9-", "-1", "a-b", "1-0-0", "18446744073709551616-0", "0" * 21 + "-0",
                    "\u0661-\u0660", "\uff11-\uff10", "\u00b9-0"):
            self.assertIsNone(inbox.stream_id(bad), bad)
            with self.assertRaises(SystemExit):
                inbox.request_row("xrange", KEY, after=bad)

    def test_after_rides_only_an_xrange_and_only_a_stream_id(self):
        row = inbox.request_row("xrange", KEY, count=4, after="10-0")
        self.assertTrue(row["payload"].endswith("|count=4|after=10-0"), row["payload"])
        for op, after in (("get", "1-0"), ("xrange", "1-0|op=del"), ("xrange", "(1-0")):
            with self.assertRaises(SystemExit):
                inbox.request_row(op, KEY, after=after)


if __name__ == "__main__":
    unittest.main()

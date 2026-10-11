"""Offline negative tests for the merged REDIS-OP waker request path."""
import contextlib
import datetime as dt
import io
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import agent_waker as aw  # noqa: E402
import fleet_agent as fa  # noqa: E402


HEAD = ["Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type",
        "Payload", "Category", "Project Tag", "Gist", "Sub-Gist"]


def board(*rows, header=HEAD):
    return {"rows": [header, *rows]}


def request_row():
    return fa.request_row("gemini", "xadd", "conf:side:20261011", val="one",
                          rid="GEMINI-REDIS-TEST", now=dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc))


class StrictLines(unittest.TestCase):
    def test_operation_specific_shapes_reject_ambiguous_or_extra_fields(self):
        for line in ("REDIS-OP|op=xadd|key=proj:k|val=a|field=b",
                     "REDIS-OP|op=set|key=proj:k|val=a|op=del",
                     "REDIS-OP|op=set|key=proj:k|val=a|tail",
                     "REDIS-OP|op=set|key=proj:k|val=a|extra=b",
                     "REDIS-OP|op=hset|key=proj:k|field=|val=a",
                     "REDIS-OP|op=get|key=proj:k|val=a"):
            with self.subTest(line=line):
                req, text = aw.redis_request(line)
                self.assertIsNone(req)
                self.assertIn("could not be posted", text)

    def test_source_id_is_stable_and_agent_bound(self):
        one = aw.request_id("gemini", "ASK-1")
        self.assertEqual(one, aw.request_id("gemini", "ASK-1"))
        self.assertNotEqual(one, aw.request_id("grok", "ASK-1"))
        self.assertNotEqual(one, aw.request_id("gemini", "ASK-2"))


class SideEffectGate(unittest.TestCase):
    KEY = "conf:canary:redis-op:test"
    LINE = "REDIS-OP|op=set|key=conf:canary:redis-op:test|val=PUBLIC_CANARY_V1"

    def setUp(self):
        env = mock.patch.dict(os.environ, {"REDIS_BOARD_CANARY_ENABLED": "1",
                                        "REDIS_BOARD_CANARY_KEY": self.KEY})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = dict(aw.AGENTS["gemini"], redis_requests=True)
        self.request = {"op": "set", "key": self.KEY, "val": aw.REDIS_CANARY_MARKER,
                        "field": None, "count": None}

    def test_default_off_strips_line_without_posting(self):
        self.assertFalse(aw.AGENTS["gemini"]["redis_requests"])
        self.assertFalse(aw.AGENTS["grok"]["redis_requests"])
        with mock.patch.object(aw, "reserve_redis_side_effect") as reserve:
            body, _, state, _, _ = aw.redis_hook("gemini", aw.AGENTS["gemini"], self.LINE,
                                                  "ASK-1", False, False)
        self.assertEqual("", body)
        self.assertEqual("refused", state)
        reserve.assert_not_called()

    def test_cut_output_does_not_reserve_or_post(self):
        with mock.patch.object(aw, "reserve_redis_side_effect") as reserve:
            with mock.patch.object(aw, "post_request") as post:
                _, _, state, _, _ = aw.redis_hook("gemini", self.cfg, self.LINE,
                                                   "ASK-1", True, False)
        self.assertEqual("cut", state)
        reserve.assert_not_called()
        post.assert_not_called()

    def test_private_or_unconfigured_requests_do_not_reserve(self):
        for line, disabled in ((self.LINE, True),
                               ("REDIS-OP|op=get|key=gov:audit", False),
                               ("REDIS-OP|op=get|key=fleet:inbox:claude-code-cli", False),
                               ("REDIS-OP|op=set|key=conf:canary:redis-op:test|val=other", False)):
            with self.subTest(line=line):
                with mock.patch.dict(os.environ, {"REDIS_BOARD_CANARY_ENABLED": ""} if disabled else {}):
                    with mock.patch.object(aw, "reserve_redis_side_effect") as reserve:
                        with mock.patch.object(aw, "post_request") as post:
                            _, _, state, _, _ = aw.redis_hook("gemini", self.cfg, line,
                                                               "ASK-1", False, False)
                self.assertEqual("refused", state)
                reserve.assert_not_called()
                post.assert_not_called()

    def test_no_durable_claim_refuses_before_subprocess(self):
        with mock.patch.object(aw, "post_request") as post:
            _, _, state, note, _ = aw.redis_hook("gemini", self.cfg, self.LINE,
                                                  "ASK-1", False, False)
        self.assertEqual("refused", state)
        self.assertIn("no durable posting claim", note)
        post.assert_not_called()

    def test_uncertain_post_is_not_treated_as_execution(self):
        with mock.patch.object(aw, "reserve_redis_side_effect", return_value=True):
            with mock.patch.object(aw, "post_request", return_value=("unconfirmed", "may have landed")):
                _, rid, state, note, _ = aw.redis_hook("gemini", self.cfg, self.LINE,
                                                       "ASK-1", False, False)
        self.assertEqual(aw.request_id("gemini", "ASK-1"), rid)
        self.assertEqual("unconfirmed", state)
        self.assertIn("not confirmed", note)

    def test_wrong_id_bad_exit_and_ambiguous_output_never_verify(self):
        rid = aw.request_id("gemini", "ASK-1")
        cases = ((0, "  VERIFIED on the board: Row_ID=OTHER Source_Tag=gemini Target_Surface=bus-reconciler"),
                 (1, "  VERIFIED on the board: Row_ID=%s Source_Tag=gemini Target_Surface=bus-reconciler" % rid),
                 (1, "  NOT SENT: preflight failed\n  append said ok"),
                 (0, "  VERIFIED on the board: Row_ID=%s Source_Tag=gemini Target_Surface=bus-reconciler\n  NOT SENT" % rid))
        for rc, stdout in cases:
            with self.subTest(rc=rc, stdout=stdout):
                result = types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")
                with mock.patch.object(aw.subprocess, "run", return_value=result):
                    self.assertEqual("unconfirmed", aw.post_request("gemini", {}, self.request,
                                                                       False, "ASK-1")[0])

    def test_exact_receipt_only_confirms_board_delivery(self):
        rid = aw.request_id("gemini", "ASK-1")
        result = types.SimpleNamespace(returncode=0,
            stdout="  VERIFIED on the board: Row_ID=%s Source_Tag=gemini Target_Surface=bus-reconciler" % rid,
            stderr="")
        with mock.patch.object(aw.subprocess, "run", return_value=result):
            self.assertEqual(("posted", rid), aw.post_request("gemini", {}, self.request,
                                                               False, "ASK-1"))

    def test_timeout_is_unconfirmed(self):
        with mock.patch.object(aw.subprocess, "run", side_effect=subprocess.TimeoutExpired("request", 400)):
            self.assertEqual("unconfirmed", aw.post_request("gemini", {}, self.request,
                                                              False, "ASK-1")[0])

class ExactRequestReadback(unittest.TestCase):
    def setUp(self):
        self.rid, self.row = request_row()
        self.env = mock.patch.object(fa, "_bus_load_env", return_value={
            "BUS_URL": "offline", "BUS_SECRET": "fixture"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def verify(self, reads):
        with mock.patch.object(fa, "read_board", side_effect=reads):
            with mock.patch.object(fa, "_bus_fetch", return_value='{"ok":true}') as append:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    result = fa.append_and_verify(self.rid, self.row, request=True)
        return result, append.call_count, output.getvalue()

    def test_existing_identical_operation_skips_append(self):
        older = list(self.row)
        older[1] = "2026-10-10T00:00:00.000000Z"
        result, calls, output = self.verify([board(older)])
        self.assertEqual((0, 0), (result, calls))
        self.assertIn("existing request", output)

    def test_existing_different_or_duplicate_id_refuses_append(self):
        other = list(self.row)
        other[5] = other[5].replace("op=xadd", "op=del")
        changed_tenth = list(self.row)
        changed_tenth[9] = "different"
        for rows in (board(other), board(changed_tenth), board(self.row[:-1]),
                     board(self.row + ["extra"]), board(self.row, self.row)):
            with self.subTest(rows=rows):
                result, calls, output = self.verify([rows])
                self.assertEqual((2, 0), (result, calls))
                self.assertIn("refused before posting", output)

    def test_preflight_rejects_missing_extra_or_duplicate_header_fields(self):
        bad_headers = (HEAD[:-1], HEAD + ["extra"], HEAD[:-1] + ["Gist"],
                       HEAD[:-1] + ["Subgist"])
        for header in bad_headers:
            with self.subTest(header=header):
                result, calls, output = self.verify([board(self.row, header=header)])
                self.assertEqual((1, 0), (result, calls))
                self.assertIn("schema", output)

    def test_no_valid_preflight_read_refuses_append(self):
        result, calls, output = self.verify([None])
        self.assertEqual((1, 0), (result, calls))
        self.assertIn("NOT SENT", output)

    def test_post_append_requires_one_complete_exact_row(self):
        variants = [board(self.row, self.row), board(), board(self.row[:-1]),
                    board(self.row + ["extra"])]
        for index in range(len(self.row)):
            wrong = list(self.row)
            wrong[index] = "different"
            variants.append(board(wrong))
        for after in variants:
            with self.subTest(after=after):
                result, calls, output = self.verify([board(), after])
                self.assertEqual((3, 1), (result, calls))
                self.assertIn("UNCONFIRMED", output)

    def test_post_append_rejects_missing_extra_or_duplicate_header_fields(self):
        bad_headers = (HEAD[:-1], HEAD + ["extra"], HEAD[:-1] + ["Gist"],
                       HEAD[:-1] + ["Subgist"])
        for header in bad_headers:
            with self.subTest(header=header):
                result, calls, output = self.verify([board(), board(self.row, header=header)])
                self.assertEqual((3, 1), (result, calls))
                self.assertIn("UNCONFIRMED", output)

    def test_post_append_exact_row_verifies(self):
        result, calls, output = self.verify([board(), board(self.row)])
        self.assertEqual((0, 1), (result, calls))
        self.assertIn("VERIFIED on the board", output)


if __name__ == "__main__":
    unittest.main()

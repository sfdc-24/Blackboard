"""The laptop lane (cloud/claude-api-worker/lane.py): the cloud's three hands for claude-code-cli.

The ways this goes wrong, each with a test that fails when the guard is removed:
a doorbell row that carries the doc's text onto the public board, a second row
for one task, a row posted with no marker claimed, an ACCEPTED or terminal line
written from nothing the laptop said, a terminal line from a value of the wrong
shape, a line that reads as the laptop's but is not read back, a settled task
read again, and a lane line that retriggers intake or confuses its trail.

Offline by construction: every outside call is an injected adapter.
"""
import importlib
import importlib.util
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
tw = importlib.import_module("tests.test_claude_api_worker")
wk, di = tw.wk, tw.di
lane = wk.lane

spec = importlib.util.spec_from_file_location("fleet_signature", REPO / "scripts" / "signature.py")
signature = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signature)

LAPTOP = "claude-code-cli"
NOW, AT, FILE, FOLDER, REQUEST = tw.NOW, tw.AT, tw.FILE, tw.FOLDER, tw.REQUEST


class Board:
    def __init__(self, fail=None):
        self.rows, self.fail = [], fail

    def __call__(self, spec_):
        if self.fail:
            raise self.fail
        self.rows.append(dict(spec_))


def laptop_tuple(text=REQUEST):
    return di.correlate(FILE, text, "r1", LAPTOP)


def entry(text=REQUEST, when=NOW - 60):
    eid, fields = tw.entry(text, when=when, recipient=LAPTOP)
    return eid, fields


def deps(text=REQUEST, receipts=None, board="default", **over):
    """Laptop lane ON, the api worker OFF, one task on the laptop's inbox, intake's lines in the doc."""
    t = laptop_tuple(text)
    receipts = receipts if receipts is not None else tw.Receipts()
    docs = tw.Docs(text + tw.intake_lines(t), receipts=receipts)
    base = dict(enabled=False, lane_enabled=True, inbox=tw.Inbox([entry(text)]), docs=docs,
                receipts=receipts, claims=wk.MemoryClaims(), ledger=wk.MemoryLedger(),
                board=Board() if board == "default" else board, folder_id=FOLDER, now=NOW, at=AT,
                day=lambda now: "2026-10-11")
    base.update(over)
    return wk.Deps(**base)


def lane_of(out):
    return out["lane"][0]


class FlagTest(unittest.TestCase):
    def test_anything_but_exactly_on_is_off(self):
        for value in (None, "", "ON", "On", "1", "true", "yes", " on"):
            env = {} if value is None else {"CLAUDE_API_LANE_ENABLED": value}
            self.assertFalse(wk.lane_enabled_from_env(env), repr(value))
        self.assertTrue(wk.lane_enabled_from_env({"CLAUDE_API_LANE_ENABLED": "on"}))

    def test_lane_off_reads_nothing_posts_nothing_writes_nothing(self):
        d = deps(lane_enabled=False)
        out = wk.run_once(d)
        self.assertEqual("disabled", out["status"])
        self.assertNotIn("lane", out)
        self.assertEqual(0, d.inbox.reads)
        self.assertEqual([], d.board.rows)
        self.assertEqual([], d.receipts.lines)

    def test_lane_on_with_the_api_worker_off_is_lane_only_and_calls_no_model(self):
        d = deps(model=tw.Model())
        out = wk.run_once(d)
        self.assertEqual("lane_only", out["status"])
        self.assertEqual([], d.model.calls)
        self.assertEqual([], out["considered"])


class DoorbellTest(unittest.TestCase):
    """Hand 1: one row per task, ids only, in the shape append.py accepts."""

    def test_a_task_on_the_laptops_inbox_rings_once_with_ids_only(self):
        d = deps()
        out = wk.run_once(d)
        rec = lane_of(out)
        self.assertEqual("rung", rec["wake"], rec)
        self.assertEqual("waiting", rec["status"])
        self.assertEqual(1, len(d.board.rows))
        row = d.board.rows[0]
        t = laptop_tuple()
        self.assertEqual("claude-api", row["source_tag"])
        self.assertEqual(LAPTOP, row["target_surface"])
        self.assertEqual("TASK", row["action_type"])
        self.assertEqual("CAW-WAKE-" + t["task_id"][5:], row["row_id"])
        self.assertIn("task=" + t["task_id"], row["payload"])
        self.assertIn("doc=" + FILE, row["payload"])
        self.assertIn("request=OT-20261010", row["payload"])
        self.assertIn("to=" + LAPTOP, row["payload"])
        for secret in ("Fix the broken telephone", "OWNER TRIGGER", "RECEIPTS"):
            self.assertNotIn(secret, json.dumps(row))                # the doc's text never leaves
        self.assertEqual([], signature.check(row["source_tag"], row["payload"]))   # the real roster
        self.assertTrue(row["payload"].startswith("BCB|v=1|id="))
        self.assertEqual("rung", d.claims.woke_marks[t["task_id"]])
        # the next tick does not ring again
        again = wk.run_once(d)
        self.assertEqual("rung", lane_of(again)["wake"])
        self.assertEqual(1, len(d.board.rows))

    def test_no_board_claims_no_marker_so_a_board_arriving_later_rings(self):
        d = deps(board=None)
        rec = lane_of(wk.run_once(d))
        self.assertEqual("no_board", rec["wake"])
        self.assertEqual({}, d.claims.woke_marks)
        d.board = Board()
        self.assertEqual("rung", lane_of(wk.run_once(d))["wake"])

    def test_the_marker_is_claimed_before_the_post_and_a_lost_post_is_retried_once_an_hour_on(self):
        d = deps(board=Board(fail=ConnectionError("gateway 502")))
        t = laptop_tuple()
        rec = lane_of(wk.run_once(d))
        self.assertEqual("unconfirmed:ConnectionError", rec["wake"])
        self.assertEqual("posting:%d" % int(NOW), d.claims.woke_marks[t["task_id"]])
        # inside the hour: nothing more is posted, whatever the board says now
        d.board = Board()
        d.now = NOW + 1800
        self.assertEqual("posting", lane_of(wk.run_once(d))["wake"])
        self.assertEqual([], d.board.rows)
        # an hour on: exactly one retry, and the marker says failed from here
        d.now = NOW + 3601
        d.board = Board(fail=ConnectionError("still down"))
        self.assertEqual("failed:ConnectionError", lane_of(wk.run_once(d))["wake"])
        self.assertTrue(d.claims.woke_marks[t["task_id"]].startswith("failed:"))
        d.board = Board()
        d.now = NOW + 9000
        self.assertEqual("failed", lane_of(wk.run_once(d))["wake"])
        self.assertEqual([], d.board.rows)                           # never a third row

    def test_a_pass_that_loses_the_marker_race_posts_nothing(self):
        """Two ticks overlapping: the one whose SET NX lost must not post, or the laptop is rung twice."""
        class Racing(wk.MemoryClaims):
            def claim_woke(self, task_id, value):
                self.woke_marks[task_id] = "posting:%d" % int(NOW)     # the other tick got there first
                return False
        d = deps(claims=Racing())
        rec = lane_of(wk.run_once(d))
        self.assertEqual("posting", rec["wake"], rec)
        self.assertEqual([], d.board.rows)

    def test_the_lane_refuses_to_run_with_its_flag_off_even_when_called_directly(self):
        d = deps(lane_enabled=False)
        self.assertEqual([], lane.run(wk._contract(), d, NOW))
        self.assertEqual(0, d.inbox.reads)

    def test_a_retry_that_lands_marks_rung(self):
        d = deps(board=Board(fail=ConnectionError("x")))
        wk.run_once(d)
        d.now = NOW + 3601
        d.board = Board()
        self.assertEqual("rung", lane_of(wk.run_once(d))["wake"])
        self.assertEqual(1, len(d.board.rows))

    def test_a_task_the_laptop_already_spoke_about_is_not_rung(self):
        d = deps()
        t = laptop_tuple()
        d.docs.text += wk.receipt_line("ACCEPTED", t, "2026-10-11T04:10:00Z", "feedfeedfeedfeed",
                                       "x", by=LAPTOP) + chr(10)
        rec = lane_of(wk.run_once(d))
        self.assertEqual("not_needed", rec["wake"])
        self.assertEqual([], d.board.rows)

    def test_entries_not_for_the_laptop_are_left_alone(self):
        d = deps()
        eid, fields = entry()
        d.inbox = tw.Inbox([(eid, dict(fields, recipient_id="claude-api")), (eid, dict(fields, kind="note"))])
        out = wk.run_once(d)
        self.assertEqual(["not_lane", "not_lane"], [r["status"] for r in out["lane"]])
        self.assertEqual(0, d.docs.reads)


class AcceptTest(unittest.TestCase):
    """Hand 2: the laptop's governed acceptance becomes the ACCEPTED line, and says where it came from."""

    def test_no_accept_key_means_no_accepted_line(self):
        d = deps()
        rec = lane_of(wk.run_once(d))
        self.assertEqual("none", rec["accept"])
        self.assertEqual([], d.receipts.lines)

    def test_the_accept_key_becomes_an_accepted_line_by_the_laptop_naming_the_evidence(self):
        d = deps()
        t = laptop_tuple()
        d.claims.lane["fleet:worker:accept:" + t["task_id"]] = "claude-code-cli:%d:900" % int(NOW - 30)
        rec = lane_of(wk.run_once(d))
        self.assertEqual("written", rec["accept"], rec)
        self.assertEqual("waiting", rec["status"])
        self.assertEqual(["ACCEPTED"], d.receipts.states())
        line = d.receipts.lines[0][2]
        self.assertIn("recipient claude-code-cli - by claude-code-cli - ", line)
        self.assertIn("lease 900s", line)
        self.assertIn("recorded by claude-api from fleet:worker:accept (gov:audit)", line)
        self.assertIn("id " + wk.receipt_id_for(t["task_id"], "ACCEPTED", by=LAPTOP), line)
        self.assertNotEqual(wk.receipt_id_for(t["task_id"], "ACCEPTED", by=LAPTOP),
                            wk.receipt_id_for(t["task_id"], "ACCEPTED"))
        self.assertEqual("not_needed", lane_of(wk.run_once(d))["wake"])       # the doc now shows it
        self.assertEqual(1, len(d.receipts.lines))                            # written once

    def test_a_value_that_is_not_the_laptops_shape_writes_nothing(self):
        d = deps()
        t = laptop_tuple()
        for bad in ("claude-api:1760000000", "yes", "claude-code-cli:soon", ""):
            d.claims.lane["fleet:worker:accept:" + t["task_id"]] = bad
            rec = lane_of(wk.run_once(d))
            self.assertEqual("none", rec["accept"], bad)
        self.assertEqual([], d.receipts.lines)
        self.assertIsNone(lane.parse_accept("claude-code-cli:1760000000:x"))
        self.assertEqual((1760000000, 0), lane.parse_accept(b"claude-code-cli:1760000000"))


class DoneTest(unittest.TestCase):
    """Hand 3: the laptop's terminal word, short, with its evidence, read back, then settled."""

    def done_value(self, state="SUCCEEDED", note="Answered in PR 371.", evidence="https://github.com/sfdc-24/Blackboard/pull/371"):
        return json.dumps({"by": LAPTOP, "state": state, "note": note, "evidence": evidence})

    def test_the_done_key_becomes_the_terminal_line_and_settles_the_task(self):
        d = deps()
        t = laptop_tuple()
        d.claims.lane["fleet:worker:accept:" + t["task_id"]] = "claude-code-cli:%d" % int(NOW - 60)
        d.claims.lane["fleet:worker:done:" + t["task_id"]] = self.done_value()
        rec = lane_of(wk.run_once(d))
        self.assertEqual("succeeded", rec["status"], rec)
        self.assertEqual(["ACCEPTED", "SUCCEEDED"], d.receipts.states())
        line = d.receipts.lines[1][2]
        self.assertIn("SUCCEEDED - task %s" % t["task_id"], line)
        self.assertIn("by claude-code-cli", line)
        self.assertIn("Answered in PR 371. - evidence https://github.com/sfdc-24/Blackboard/pull/371", line)
        self.assertIn("recorded by claude-api from fleet:worker:done", line)
        self.assertIn(t["task_id"], d.claims.settled_marks)
        reads = d.docs.reads
        self.assertEqual("settled", lane_of(wk.run_once(d))["status"])
        self.assertEqual(reads, d.docs.reads)                                  # the marker saved the read
        d.claims.settled_marks.clear()                                         # Redis forgot
        self.assertEqual("done", lane_of(wk.run_once(d))["status"])            # the doc remembers
        self.assertIn(t["task_id"], d.claims.settled_marks)
        self.assertEqual(2, len(d.receipts.lines))

    def test_failed_is_a_terminal_too_and_done_without_accept_still_lands(self):
        d = deps()
        t = laptop_tuple()
        d.claims.lane["fleet:worker:done:" + t["task_id"]] = self.done_value("FAILED", "could not reach the sandbox", "")
        rec = lane_of(wk.run_once(d))
        self.assertEqual("failed", rec["status"], rec)
        self.assertEqual(["FAILED"], d.receipts.states())
        self.assertIn("could not reach the sandbox - recorded by", d.receipts.lines[0][2])

    def test_a_value_of_the_wrong_shape_writes_nothing_and_says_so(self):
        d = deps()
        t = laptop_tuple()
        for bad in ('{"by": "claude-api", "state": "SUCCEEDED"}', '{"by": "claude-code-cli", "state": "DONE"}',
                    "not json", '["SUCCEEDED"]'):
            d.claims.lane["fleet:worker:done:" + t["task_id"]] = bad
            rec = lane_of(wk.run_once(d))
            self.assertEqual("waiting", rec["status"], bad)
            self.assertEqual("refused:shape", rec["done"], bad)
        self.assertEqual([], d.receipts.lines)
        self.assertEqual(set(), d.claims.settled_marks)

    def test_the_note_is_bounded_and_the_evidence_must_be_https(self):
        done = lane.parse_done(json.dumps({"by": LAPTOP, "state": "SUCCEEDED", "note": "x" * 400,
                                           "evidence": "ftp://nope"}))
        self.assertTrue(done["note"].endswith("[note cut at 300 of 400 characters]"), done["note"][-60:])
        self.assertEqual("(evidence link refused: not an https URL)", done["evidence"])
        self.assertIn("evidence (evidence link refused", lane.done_detail(done))
        plain = lane.parse_done(json.dumps({"by": LAPTOP, "state": "FAILED"}))
        self.assertEqual({"state": "FAILED", "note": "", "evidence": ""}, plain)
        self.assertEqual("recorded by claude-api from fleet:worker:done", lane.done_detail(plain))
        dashed = lane.done_detail(lane.parse_done(json.dumps({"by": LAPTOP, "state": "FAILED", "note": "a - b"})))
        self.assertTrue(dashed.startswith("a / b - recorded by"), dashed)      # a note cannot forge a field

    def test_a_terminal_line_not_read_back_is_owed_and_not_settled(self):
        d = deps(receipts=tw.Receipts(drop=True))
        t = laptop_tuple()
        d.claims.lane["fleet:worker:done:" + t["task_id"]] = self.done_value()
        rec = lane_of(wk.run_once(d))
        self.assertEqual("terminal_receipt_owed", rec["status"], rec)
        self.assertEqual(set(), d.claims.settled_marks)
        d.receipts.drop = False
        self.assertEqual("succeeded", lane_of(wk.run_once(d))["status"])


class RecordTest(unittest.TestCase):
    """The doc trail first; the lane's lines are harmless to intake and visible to the api worker."""

    def test_an_edited_request_is_superseded_and_a_task_taken_by_claude_api_is_left(self):
        d = deps()
        edited = REQUEST.replace("first.", "first, then the showcase.")
        d.docs.text = edited + tw.intake_lines(laptop_tuple())
        rec = lane_of(wk.run_once(d))
        self.assertEqual("superseded", rec["status"])
        self.assertEqual([], d.board.rows)
        d = deps()
        t = laptop_tuple()
        d.docs.text += wk.receipt_line("ACCEPTED", t, "t", "abcdabcdabcdabcd", "attempt 1") + chr(10)   # by claude-api
        rec = lane_of(wk.run_once(d))
        self.assertEqual("taken", rec["status"])
        self.assertEqual("claude-api", rec["by"])
        self.assertEqual([], d.board.rows)

    def test_the_lane_lines_do_not_retrigger_intake_and_the_api_worker_reads_them_as_taken(self):
        d = deps()
        t = laptop_tuple()
        d.claims.lane["fleet:worker:accept:" + t["task_id"]] = "claude-code-cli:%d" % int(NOW)
        d.claims.lane["fleet:worker:done:" + t["task_id"]] = json.dumps(
            {"by": LAPTOP, "state": "SUCCEEDED", "note": "done", "evidence": "https://example.com/x"})
        wk.run_once(d)
        after = d.docs.read(FILE)["text"]
        self.assertEqual(di.request_revision(REQUEST), di.request_revision(after))
        self.assertEqual({"REQUESTED": LAPTOP, "DISPATCHED": LAPTOP}, di.trail_for(after, t["task_id"]))
        self.assertEqual("claude-code-cli", wk.taken_elsewhere(wk.trail_for(after, t["task_id"])))
        # the api worker, handed the same task on ITS inbox, writes nothing and calls nobody
        api = tw.deps()
        api.docs.text = after
        api.docs.receipts = None
        api.docs.revision = "r9"
        out = wk.run_once(api)
        self.assertEqual("taken", out["considered"][0]["status"], out)
        self.assertEqual([], api.model.calls)

    def test_unreadable_doc_or_wrong_folder_writes_and_rings_nothing(self):
        d = deps()
        d.docs.folder = "elsewhere"
        rec = lane_of(wk.run_once(d))
        self.assertEqual("out_of_scope", rec["status"])
        self.assertEqual([], d.board.rows)

        class Broken(tw.Docs):
            def read(self, file_id):
                raise ConnectionError()
        d = deps()
        d.docs = Broken(REQUEST)
        self.assertEqual("read_error", lane_of(wk.run_once(d))["status"])
        self.assertEqual([], d.board.rows)

    def test_no_store_is_named(self):
        d = deps(inbox=None)
        self.assertEqual([{"status": "no_store", "task": None}], wk.run_once(d)["lane"])


class RedisLaneTest(unittest.TestCase):
    def test_the_lane_keys_and_markers_live_under_fleet_worker(self):
        c = tw.RedisShapeTest.Client()
        claims = wk.RedisClaims(c)
        c.kv["fleet:worker:accept:task-1"] = "claude-code-cli:1:2"
        self.assertEqual("claude-code-cli:1:2", claims.lane_value("fleet:worker:accept:task-1"))
        self.assertIsNone(claims.lane_value("fleet:worker:done:task-1"))
        self.assertFalse(claims.settled("task-1"))
        claims.mark_settled("task-1")
        self.assertTrue(claims.settled("task-1"))
        self.assertEqual(wk.DONE_TTL, c.ttl["fleet:worker:settled:task-1"])
        self.assertIsNone(claims.woke("task-1"))
        self.assertTrue(claims.claim_woke("task-1", "posting:1"))
        self.assertFalse(claims.claim_woke("task-1", "posting:2"))
        self.assertEqual("posting:1", claims.woke("task-1"))
        self.assertFalse(claims.advance_woke("task-1", "posting:9", "failed:3"))
        self.assertTrue(claims.advance_woke("task-1", "posting:1", "failed:3"))
        claims.set_woke("task-1", "rung")
        self.assertEqual("rung", claims.woke("task-1"))
        self.assertEqual(wk.DONE_TTL, c.ttl["fleet:worker:woke:task-1"])


if __name__ == "__main__":
    unittest.main()

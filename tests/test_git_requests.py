"""scripts/git_requests.py: Redis requests from GitHub comments, bound to a NUMERIC bot id.

The tests that matter are about identity: GitHub authenticates a comment's author, so the only thing
that may decide the sender is that author's numeric id and type - never the login, never the body.
"""
import datetime
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bus_request as br                                                 # noqa: E402
import git_requests as gr                                                # noqa: E402
from test_redis_gov import FakeRedis                                     # noqa: E402

NOW = datetime.datetime(2026, 10, 8, 14, 30, 0, tzinfo=datetime.timezone.utc)
CURSOR_ID = 206951365
BOTS = {str(CURSOR_ID): "cursor"}


def comment(body, cid=9001, uid=CURSOR_ID, utype="Bot", login="cursor[bot]", at="2026-10-08T14:25:00Z"):
    return {"id": cid, "body": body, "created_at": at, "updated_at": at,
            "html_url": "https://github.com/sfdc-24/Blackboard/pull/342#issuecomment-%d" % cid,
            "user": {"id": uid, "type": utype, "login": login}}


SET = "BCB|v=1|do=redis-op|op=set|key=conf:2026-10-08:git:cursor|val=from git"
GET = "BCB|v=1|do=redis-op|op=get|key=conf:2026-10-08:git:cursor"


class TheSenderIsTheAuthenticatedAuthor(unittest.TestCase):
    def test_a_bound_bot_comment_becomes_a_request_from_its_principal(self):
        rows = gr.rows_from_comments("Blackboard", [comment(SET)], BOTS)
        self.assertEqual(1, len(rows))
        req = br.parse_request(rows[0])
        self.assertEqual("cursor", req["claimed_sender"])
        self.assertEqual("redis-op", req["action"])
        self.assertEqual(("set", "conf:2026-10-08:git:cursor", "from git"), (req["op"], req["key"], req["val"]))
        self.assertEqual("GH-Blackboard-9001-" + gr.line_id(SET), req["req_id"])
        self.assertEqual("gh:Blackboard:9001:" + gr.line_id(SET), req["row_id"])
        self.assertEqual("git", req["channel"])

    def test_the_login_alone_is_not_enough(self):
        # Anyone can register a name; nobody else can be user 206951365.
        self.assertEqual([], gr.rows_from_comments("Blackboard", [comment(SET, uid=1)], BOTS))

    def test_a_human_with_the_bound_id_is_refused(self):
        self.assertEqual([], gr.rows_from_comments("Blackboard", [comment(SET, utype="User")], BOTS))

    def test_the_body_cannot_name_its_own_request_id(self):
        body = "BCB|v=1|id=GH-Blackboard-1-1|req=AYA-COST-SET|do=redis-op|op=get|key=conf:x"
        req = br.parse_request(gr.rows_from_comments("Blackboard", [comment(body)], BOTS)[0])
        self.assertEqual("GH-Blackboard-9001-" + gr.line_id(body), req["req_id"])
        self.assertNotIn("AYA-COST-SET", gr.rows_from_comments("Blackboard", [comment(body)], BOTS)[0][5])

    def test_a_comment_of_only_requests_is_a_request_comment(self):
        # Cursor's real request comment, 6062025739: exactly two lines, blank lines allowed around them.
        rows = gr.rows_from_comments("Blackboard", [comment("\n" + SET + "\n\n  " + GET + "  \n")], BOTS)
        self.assertEqual(["set", "get"], [br.parse_request(r)["op"] for r in rows])

    def test_one_word_of_anything_else_and_the_comment_asks_for_nothing(self):
        for body in ("Reviewed 7333b5d, looks fine.\n" + GET,
                     GET + "\nDone.",
                     GET + "\nBCB|v=1|do=redis-synthetic-probe"):
            self.assertEqual([], gr.rows_from_comments("Blackboard", [comment(body)], BOTS), body)

    def test_the_live_report_that_ran_the_set_twice(self):
        # bus-requests-54r88: Cursor's report QUOTED its two lines in a fence and the set ran twice.
        report = ("I posted the live Redis round trip as `cursor[bot]` on PR #342.\n\n```\n" + SET + "\n"
                  + GET + "\n```\n\nNo code was changed.")
        self.assertEqual([], gr.rows_from_comments("Blackboard", [comment(report)], BOTS))

    def test_aya_quoting_shapes_on_4d13e97(self):
        # Every way aya's adversarial suite got a quoted operation executed. None may yield a request.
        shapes = {
            "mismatched fence type": "~~~\n" + SET + "\n```",
            "short closer": "````\n" + SET + "\n```",
            "trailing-text closer": "```\n" + SET + "\n``` done",
            "fence alone around requests": "```\n" + SET + "\n" + GET + "\n```",
            "indented code under prose": "Example:\n\n    " + SET,
            "multi-line inline code": "`\n" + SET + "\n`",
            "lazy quote continuation": "> quoted\n" + SET,
            "quote marker": "> " + SET,
            "inline code": "`" + SET + "`",
            "tilde in the line": SET + "~",
            # aya on ce941b6: a comment of ONLY indented lines is a Markdown code block.
            "four-space indented code alone": "    " + SET + "\n    " + GET,
            "tab-indented code alone": "\t" + SET,
            "one indented line among bare ones": SET + "\n    " + GET,
        }
        for name, body in shapes.items():
            self.assertEqual([], gr.rows_from_comments("Blackboard", [comment(body)], BOTS), name)

    def test_more_than_the_cap_is_refused_whole_not_truncated(self):
        self.assertEqual([], gr.rows_from_comments("Blackboard", [comment("\n".join([GET] * 11))], BOTS))
        self.assertEqual(10, len(gr.rows_from_comments("Blackboard", [comment("\n".join([GET] * 10))], BOTS)))


class ItRunsThroughTheSameGovernance(unittest.TestCase):
    def test_set_then_get_end_to_end_audited_under_the_comment(self):
        conn, posted = FakeRedis(), []
        rows = gr.rows_from_comments("Blackboard", [comment(SET + "\n" + GET)], BOTS)
        out = br.handle(rows, conn, now=NOW, append=posted.append)
        self.assertEqual(["OK", "OK"], [a["status"] for a in out["answered"]])
        self.assertEqual("from git", conn.get("conf:2026-10-08:git:cursor"))
        self.assertTrue(all(p["target_surface"] == "cursor" for p in posted))
        self.assertIn("gh:Blackboard:9001:" + gr.line_id(SET), json.dumps(conn.audit()))

    def test_a_protected_namespace_is_still_refused(self):
        conn = FakeRedis()
        rows = gr.rows_from_comments("Blackboard", [comment("BCB|v=1|do=redis-op|op=set|key=v1:bus:x|val=y")], BOTS)
        out = br.handle(rows, conn, now=NOW, append=[].append)
        self.assertEqual(["REFUSED"], [a["status"] for a in out["answered"]])
        self.assertIsNone(conn.get("v1:bus:x"))

    def test_an_answered_git_request_is_not_run_again(self):
        conn = FakeRedis()
        rows = gr.rows_from_comments("Blackboard", [comment(SET)], BOTS)
        done = ["X", "2026-10-08T14:26:00Z", "bus-reconciler", "cursor", "AYA_RESULT",
                "BCB|v=1|answers=GH-Blackboard-9001-%s|text=OK" % gr.line_id(SET), "OPEN", "Blackboard", "g", ""]
        out = br.handle([done] + rows, conn, now=NOW, append=[].append)
        self.assertEqual([], out["answered"])
        self.assertIsNone(conn.get("conf:2026-10-08:git:cursor"))

    def test_a_lost_result_row_does_not_run_the_write_again(self):
        # aya on 4d13e97: the op ran, its RESULT append failed, and the next run - seeing no RESULT -
        # applied it again. The second run must find the Redis marker and leave the value alone.
        conn = FakeRedis()
        rows = gr.rows_from_comments("Blackboard", [comment(SET)], BOTS)

        def lost(spec):
            raise SystemExit(2)
        with self.assertRaises(SystemExit):
            br.handle(rows, conn, now=NOW, append=lost)
        self.assertEqual("from git", conn.get("conf:2026-10-08:git:cursor"))
        conn.set("conf:2026-10-08:git:cursor", "changed since by someone else")
        posted = []
        out = br.handle(rows, conn, now=NOW, append=posted.append)
        self.assertEqual([], out["answered"])
        self.assertEqual("changed since by someone else", conn.get("conf:2026-10-08:git:cursor"))
        self.assertIn("NOT RUN AGAIN", posted[0]["payload"])

    def test_a_claim_without_an_operation_is_reported_as_unknown_not_as_run(self):
        # aya on ce941b6: the worker died after claiming the marker and before the op. Nothing ran,
        # so the retry must not say it did.
        conn = FakeRedis()
        rows = gr.rows_from_comments("Blackboard", [comment(SET)], BOTS)
        req_id = br.parse_request(rows[0])["req_id"]
        conn.set(br.run_marker(br.parse_request(rows[0])), "x")
        posted = []
        br.handle(rows, conn, now=NOW, append=posted.append)
        self.assertIsNone(conn.get("conf:2026-10-08:git:cursor"))
        self.assertIn("UNKNOWN", posted[0]["payload"])
        self.assertNotIn("already run once", posted[0]["payload"])

    def test_two_runs_over_one_stale_snapshot_apply_it_once(self):
        conn = FakeRedis()
        rows = gr.rows_from_comments("Blackboard", [comment(SET)], BOTS)
        first = br.handle(rows, conn, now=NOW, append=None)
        second = br.handle(rows, conn, now=NOW, append=None)       # same snapshot: no RESULT row in it
        self.assertEqual(["OK"], [a["status"] for a in first["answered"]])
        self.assertEqual([], second["answered"])
        writes = [e for e in conn.audit() if "conf:2026-10-08:git:cursor" in json.dumps(e)]
        self.assertEqual(1, len(writes), "one set in the audit, not two")

    def test_a_repeated_field_is_refused_not_read_first_value_wins(self):
        conn = FakeRedis()
        body = "BCB|v=1|do=redis-op|op=get|key=conf:a|op=set|val=x"
        rows = gr.rows_from_comments("Blackboard", [comment(body)], BOTS)
        out = br.handle(rows, conn, now=NOW, append=[].append)
        self.assertEqual([], out["answered"])
        self.assertIn("ambiguous", "; ".join(out["refused"][0]["why"]))
        self.assertIsNone(conn.get("conf:a"))

    def test_staleness_is_judged_from_when_it_was_asked(self):
        rows = gr.rows_from_comments("Blackboard", [comment(SET, at="2026-10-08T13:00:00Z")], BOTS)
        out = br.handle(rows, FakeRedis(), now=NOW, append=[].append)
        self.assertEqual([], out["answered"])


class AMissingConfigGrantsNothing(unittest.TestCase):
    def test_absent_or_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "acl.json"
            p.write_text(json.dumps({"principals": {}}), encoding="utf-8")
            self.assertEqual(gr.NOTHING, gr.config(p))
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(gr.NOTHING, gr.config(p))
            p.write_text(json.dumps({"git": {"owner": "sfdc-24", "repos": ["Blackboard"],
                                             "bots": {"cursor[bot]": "cursor"}}}), encoding="utf-8")
            self.assertEqual(gr.NOTHING, gr.config(p), "a login where an id belongs binds nobody")

    def test_the_shipped_list_binds_cursor_by_id(self):
        self.assertEqual({str(CURSOR_ID): "cursor"}, gr.config()["bots"])

    def test_a_github_failure_costs_the_channel_not_the_run(self):
        def broken(*a, **k):
            raise urllib.error.URLError("rate limited")
        seen = []
        self.assertEqual([], gr.git_rows(NOW, opener=broken, log=seen.append))
        self.assertTrue(seen)



class CursorOn39ebd38(unittest.TestCase):
    """Cursor's FAIL on 39ebd38: a board row spent a git request's run, an inserted line re-ran an
    old one, and the meeting loop outran GitHub's hourly limit."""

    def board_copy(self, git_row, source="cursor"):
        # Everything a board writer can copy from a public comment: ids, payload, sender tag.
        row = list(git_row)
        row[2] = source
        row[1] = "2026-10-08T14:28:00Z"
        return row

    def test_a_board_row_with_a_git_id_is_refused_and_the_bots_line_still_runs(self):
        conn = FakeRedis()
        git = gr.rows_from_comments("Blackboard", [comment(SET)], BOTS)
        forged = self.board_copy(git[0])
        forged[5] = forged[5].replace("val=from git", "val=stolen")
        posted = []
        out = br.handle([forged] + git, conn, now=NOW, append=posted.append)
        self.assertEqual("from git", conn.get("conf:2026-10-08:git:cursor"))
        self.assertEqual(1, len([a for a in out["answered"] if a["status"] == "OK"]))
        self.assertTrue(any("reserved for requests read from GitHub" in "; ".join(r["why"])
                            for r in out["refused"]))

    def test_a_board_row_cannot_pass_as_git_by_its_cells(self):
        git = gr.rows_from_comments("Blackboard", [comment(SET)], BOTS)
        self.assertEqual("board", br.parse_request(self.board_copy(git[0]))["channel"])
        self.assertEqual("git", br.parse_request(git[0])["channel"])

    def test_the_git_and_board_run_markers_never_share_a_key(self):
        git = br.parse_request(gr.rows_from_comments("Blackboard", [comment(SET)], BOTS)[0])
        board = dict(git, channel="board")
        self.assertNotEqual(br.run_marker(git), br.run_marker(board))

    def test_inserting_a_line_above_runs_only_the_new_line(self):
        conn = FakeRedis()
        first = gr.rows_from_comments("Blackboard", [comment(SET)], BOTS)
        br.handle(first, conn, now=NOW, append=[].append)
        conn.set("conf:2026-10-08:git:cursor", "changed since")
        new = "BCB|v=1|do=redis-op|op=set|key=conf:2026-10-08:git:second|val=SECOND"
        edited = gr.rows_from_comments("Blackboard", [comment(new + chr(10) + SET)], BOTS)
        self.assertEqual(br.parse_request(first[0])["req_id"], br.parse_request(edited[1])["req_id"])
        br.handle(edited, conn, now=NOW, append=[].append)
        self.assertEqual("SECOND", conn.get("conf:2026-10-08:git:second"))
        self.assertEqual("changed since", conn.get("conf:2026-10-08:git:cursor"), "the old line ran again")

    def test_github_is_polled_under_its_hourly_limit(self):
        self.assertEqual(144, gr.poll_seconds(None))         # two repos, 50 calls an hour of the 60
        self.assertEqual(60, gr.poll_seconds("a-token"))


if __name__ == "__main__":
    unittest.main()

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
        self.assertEqual("GH-Blackboard-9001-1", req["req_id"])
        self.assertEqual("gh:Blackboard:9001:1", req["row_id"])

    def test_the_login_alone_is_not_enough(self):
        # Anyone can register a name; nobody else can be user 206951365.
        self.assertEqual([], gr.rows_from_comments("Blackboard", [comment(SET, uid=1)], BOTS))

    def test_a_human_with_the_bound_id_is_refused(self):
        self.assertEqual([], gr.rows_from_comments("Blackboard", [comment(SET, utype="User")], BOTS))

    def test_the_body_cannot_name_its_own_request_id(self):
        body = "BCB|v=1|id=GH-Blackboard-1-1|req=AYA-COST-SET|do=redis-op|op=get|key=conf:x"
        req = br.parse_request(gr.rows_from_comments("Blackboard", [comment(body)], BOTS)[0])
        self.assertEqual("GH-Blackboard-9001-1", req["req_id"])
        self.assertNotIn("AYA-COST-SET", gr.rows_from_comments("Blackboard", [comment(body)], BOTS)[0][5])

    def test_ordinary_text_and_other_actions_are_ignored(self):
        body = "Reviewed 7333b5d, looks fine.\nBCB|v=1|do=redis-synthetic-probe\n" + GET
        rows = gr.rows_from_comments("Blackboard", [comment(body)], BOTS)
        self.assertEqual(["GH-Blackboard-9001-1"], [br.parse_request(r)["req_id"] for r in rows])

    def test_a_fenced_line_is_accepted(self):
        self.assertEqual(1, len(gr.rows_from_comments("Blackboard", [comment("```" + GET + "```")], BOTS)))

    def test_one_comment_cannot_spend_the_whole_run(self):
        rows = gr.rows_from_comments("Blackboard", [comment("\n".join([GET] * 40))], BOTS)
        self.assertEqual(gr.MAX_LINES_PER_COMMENT, len(rows))


class ItRunsThroughTheSameGovernance(unittest.TestCase):
    def test_set_then_get_end_to_end_audited_under_the_comment(self):
        conn, posted = FakeRedis(), []
        rows = gr.rows_from_comments("Blackboard", [comment(SET + "\n" + GET)], BOTS)
        out = br.handle(rows, conn, now=NOW, append=posted.append)
        self.assertEqual(["OK", "OK"], [a["status"] for a in out["answered"]])
        self.assertEqual("from git", conn.get("conf:2026-10-08:git:cursor"))
        self.assertTrue(all(p["target_surface"] == "cursor" for p in posted))
        self.assertIn("gh:Blackboard:9001:1", json.dumps(conn.audit()))

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
                "BCB|v=1|answers=GH-Blackboard-9001-1|text=OK", "OPEN", "Blackboard", "g", ""]
        out = br.handle([done] + rows, conn, now=NOW, append=[].append)
        self.assertEqual([], out["answered"])
        self.assertIsNone(conn.get("conf:2026-10-08:git:cursor"))

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


if __name__ == "__main__":
    unittest.main()

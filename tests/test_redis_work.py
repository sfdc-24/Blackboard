"""scripts/redis_work.py: tasks and issues an agent can create, read, move, hand off and close on Redis
(Aya's Stage 1 exit check 4, to her conditions in AYA-STAGE1-WORK-RECORDS-SOURCE-DESIGN-20261013). Offline.

The board route here is PR 397's Worker: the request row goes through the worker's own
bus_request.handle and redis_gov. The bridge is test_bridge_client's BridgeSim: the real client, the
real HTTP handler, the real /op and the same governor. Both share one fake Redis, so a record written
by one route is read by the other, and the compare-and-set under test is the governor's own.
"""
from __future__ import annotations

import contextlib
import datetime
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bridge_client as bc  # noqa: E402
import redis_gov  # noqa: E402
import redis_inbox as inbox  # noqa: E402
import redis_work as work  # noqa: E402
from test_bridge_client import BridgeSim, MINE, ON, SA, TOKEN, Worker  # noqa: E402
from test_redis_gov import FakeRedis, run  # noqa: E402

ME, AYA = "claude-code-cli", "chatgpt-codex-desktop"
T0 = datetime.datetime(2026, 10, 13, 5, 0, tzinfo=datetime.timezone.utc)
DAY = "20261013"
INDEX = work.INDEX % DAY
LINK = "https://github.com/sfdc-24/Blackboard/pull/402"
MOVE_OPS = ["xrange", "hgetall", "xadd", "hset"]      # read the log, read the description, THE MOVE, the list
BASE = ["--project", "Blackboard", "--source", "board row AYA-REDIS-STAGE1-EXIT-CRITERIA-20261013",
        "--done-when", "the suite is green on main"]


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = os.path.join(self.tmp.name, "unknown.jsonl")
        self.conn = FakeRedis()
        self.worker = Worker(self.conn)
        self.at = T0

    def cmd(self, who, *argv, bridge=None, environ=None, callers=MINE, switch="on"):
        """One command as `who`. By the board unless `bridge` is given (then the bridge's switch is on)."""
        printed = []
        environ = dict(ON if bridge is not None else {}) if environ is None else dict(environ)
        if switch:
            environ[work.ENV_SWITCH] = switch
        patches = [
            mock.patch.object(inbox, "post", self.worker.post), mock.patch.object(inbox, "answer", self.worker.answer),
            mock.patch.object(bc, "STATE", os.path.join(self.tmp.name, "breaker.json")),
            mock.patch.object(bc, "JOURNAL", self.journal),
            mock.patch.object(bc, "load_callers", lambda path=None: callers),
            mock.patch.object(bc, "mint", lambda url, account: TOKEN),
            mock.patch.object(bc, "call", bridge or (lambda u, t, r: self.fail("the bridge was called"))),
            mock.patch.object(work, "now", lambda: self.at),
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))),
        ]
        with contextlib.ExitStack() as stack, mock.patch.dict(os.environ, environ, clear=False):
            for name in ("REDIS_BRIDGE", "REDIS_BRIDGE_URL", "REDIS_BRIDGE_IMPERSONATE", work.ENV_AGENT,
                         work.ENV_SWITCH):
                if name not in environ:
                    os.environ.pop(name, None)
            for patch in patches:
                stack.enter_context(patch)
            code = work.main(["--as", who] + [str(a) for a in argv] + ["--wait", "0"])
        return code, "\n".join(printed)

    def create(self, who=ME, owner="grok", title="Wire the reader", extra=(), expect=0, **kw):
        """The new record's id; or, when a refusal is expected, what was printed."""
        code, out = self.cmd(who, "create", "--title", title, "--owner", owner, *BASE, *extra, **kw)
        self.assertEqual(expect, code, out)
        found = re.match(r"(W-[0-9]{8}-[0-9a-f]{6}) created", out)
        return found[1] if found else out

    def log(self, rid):
        return [(f["by"], f["text"]) for _, f in self.conn.data.get(work.LOG % rid, [])]

    def state(self, rid):
        return work.parse_event(dict(self.conn.data[work.LOG % rid][-1][1]))

    def listed(self, rid):
        return work.read_index_line(self.conn.data[INDEX][rid])

    def ops(self):
        """The op of every board row asked so far, then forget them."""
        asked = [re.search(r"\|op=([a-z]+)\|", a)[1] for a in self.worker.asked]
        del self.worker.asked[:]
        return asked

    def snapshot(self):
        return {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in self.conn.data.items()
                if k.startswith(work.NS)}

    def raw(self, rid, who, **fields):
        """An entry appended AROUND the tool, straight through the governor: in sequence, any content."""
        last = self.state(rid)
        ev = dict(last, req="aroundit", words="", rev=last["rev"] + 1)
        ev.update(fields)
        return run(self.conn, who, "xadd", work.LOG % rid, val=work.event_text(ev))

    # ------------------------------------------------------------------------- the whole life of one
    def test_a_task_from_request_to_done_by_the_board_with_every_handoff_traced(self):
        rid = self.create()
        self.assertEqual(["hgetall", "hput", "xadd", "hset"], self.ops(), "the list is checked, then three writes")
        steps = [("grok", ["accept", rid, "--next", "read the spec"], "accepted"),
                 ("grok", ["start", rid], "working"),
                 ("grok", ["handoff", rid, "--to", "cursor", "--note", "out of budget"], "requested"),
                 ("cursor", ["accept", rid, "--next", "finish the tests"], "accepted"),
                 ("cursor", ["review", rid, "--reviewer", "aya"], "review"),
                 ("aya", ["done", rid, "--evidence", LINK], "done")]
        for who, argv, state in steps:
            code, out = self.cmd(who, *argv)
            self.assertEqual(0, code, out)
            self.assertEqual(MOVE_OPS, self.ops(), "a move is two reads, ONE conditional write, and the list")
            self.assertEqual(state, self.state(rid)["state"], argv)
            self.assertEqual((self.state(rid)["rev"], state), (self.listed(rid)["rev"], self.listed(rid)["state"]))
        final = self.state(rid)
        self.assertEqual((7, "cursor", AYA, ME, "-"),
                         tuple(final[k] for k in ("rev", "owner", "reviewer", "requester", "lease")))
        # THE TRACED HANDOFF: actor and time by the governor, request id, from and to revision, in order
        self.assertEqual([ME, "grok", "grok", "grok", "cursor", "cursor", AYA], [by for by, _ in self.log(rid)])
        events = [work.parse_event(dict(f)) for _, f in self.conn.data[work.LOG % rid]]
        self.assertEqual(list(range(1, 8)), [e["rev"] for e in events])
        self.assertEqual(7, len({e["req"] for e in events}), "every move carries its own request id")
        self.assertTrue(all(e["at"] and e["by"] for e in events))
        self.assertEqual("from=grok note=out of budget", events[3]["words"])
        self.assertEqual("evidence=" + LINK, events[6]["words"])
        record = self.conn.data[work.REC % rid]
        self.assertEqual((ME, "Wire the reader", "task", "Blackboard", "the suite is green on main"),
                         tuple(record[k] for k in ("requested_by", "title", "kind", "project", "done_when")))
        code, out = self.cmd("grok", "show", rid)
        self.assertEqual(0, code, out)
        self.assertIn("done", out.splitlines()[0])
        self.assertIn("done when: the suite is green on main", out)
        self.assertIn("r6>r7 done -> done owner=cursor", out)
        self.assertIn("result=applied", out)
        code, out = self.cmd("grok", "history", rid)
        self.assertEqual(0, code, out)
        self.assertIn("7 entries of %s. VERIFIED: every one is a move its actor was allowed to make." % rid, out)
        self.assertEqual(["r%d>r%d" % (n - 1, n) for n in range(1, 8)],
                         re.findall(r"r\d+>r\d+", "\n".join(out.splitlines()[:7])), "oldest first, all of it")

    def test_the_same_through_the_bridge_spends_no_board_row_and_both_routes_see_one_record(self):
        bridge = BridgeSim(self.conn)
        rid = self.create(owner=ME, title="Mine", bridge=bridge)
        for argv in (["accept", rid, "--next", "do it"], ["start", rid], ["done", rid, "--evidence", LINK]):
            code, out = self.cmd(ME, *argv, bridge=bridge)
            self.assertEqual(0, code, out)
        self.assertEqual([], self.worker.asked, "a board row was spent with the bridge on")
        self.assertEqual("unreviewed evidence=" + LINK, self.state(rid)["words"])
        self.assertEqual("bridge:" + SA, self.conn.audit()[-1]["via"])
        code, out = self.cmd("grok", "show", rid)                     # another agent, by the board
        self.assertEqual(0, code, out)
        self.assertIn("unreviewed", out)

    # ---------------------------------------------- Aya: "atomic compare-and-set" - CONCURRENCY
    def test_CONCURRENCY_two_agents_who_read_the_same_revision_cannot_both_move_it(self):
        rid = self.create(owner="grok")
        real_post = self.worker.post
        rival = work.event_text(dict(self.state(rid), rev=2, action="cancel", state="cancelled",
                                     req="rival001", words="note=no longer wanted"))

        def racing_post(spec):
            if "|op=xadd|" in spec["payload"] and not getattr(self, "raced", False):
                self.raced = True                                    # the requester cancels it first
                assert run(self.conn, ME, "xadd", work.LOG % rid, val=rival)["ok"]
            return real_post(spec)

        self.worker.post = racing_post
        self.ops()
        code, out = self.cmd("grok", "accept", rid, "--next", "x")   # grok read r1, like the requester did
        self.assertEqual(work.CONFLICT_EXIT, code, out)
        self.assertIn("CONFLICT: someone moved %s first (revision conflict: the record is at r2" % rid, out)
        self.assertIn("NOTHING of this move was written", out)
        self.assertEqual(["xrange", "hgetall", "xadd"], self.ops(), "after losing, it wrote the list anyway")
        self.assertEqual([(ME, "create"), (ME, "cancel")], [(by, t.split()[1]) for by, t in self.log(rid)],
                         "the loser's entry is in the log")
        self.assertEqual(1, self.listed(rid)["rev"], "the loser updated the list")
        self.worker.post = real_post
        code, out = self.cmd("grok", "accept", rid, "--next", "x")   # it reads again: the record is cancelled
        self.assertEqual(2, code, out)
        self.assertIn("accept starts from requested, not from cancelled", out)

    def test_CONCURRENCY_a_move_whose_answer_was_lost_cannot_land_after_someone_elses(self):
        rid = self.create(owner=ME)
        self.ops()
        slow = BridgeSim(self.conn, fault=lambda sim, request: sim.execute(
            request, lose="held" if request["op"] == "xadd" else None))
        code, out = self.cmd(ME, "accept", rid, "--next", "x", bridge=slow)        # r2, still on its way
        self.assertEqual(work.UNKNOWN_EXIT, code, out)
        self.assertIn("MAY HAVE HAPPENED and is not sent again", out)
        req = re.search(r"if r2 carries req=([0-9a-f]{8}) this move landed", out)[1]
        self.assertEqual(1, self.state(rid)["rev"])
        self.assertEqual([], self.worker.asked, "an unknown move was repeated by the board route")
        self.assertEqual(0, self.cmd(ME, "handoff", rid, "--to", "grok")[0])       # someone else's r2 lands
        ((status, body),) = slow.deliver()                                        # NOW the old r2 arrives
        self.assertEqual(403, status)
        self.assertEqual(("refused", False), (body["error"], body["executed"]))
        self.assertIn("revision conflict: the record is at r2", body["reason"])
        self.assertEqual((2, "handoff"), (self.state(rid)["rev"], self.state(rid)["action"]))
        code, out = self.cmd("grok", "show", rid)                                 # and `show` settles it
        self.assertNotIn("req=" + req, out, "r2 carries another request id: the lost one never can land")

    # ---------------------------------------------------------- PARTIAL FAILURE and TIMEOUT
    def test_PARTIAL_FAILURE_a_move_that_landed_with_an_unknown_answer_is_settled_by_reading(self):
        rid = self.create(owner=ME)
        self.ops()
        lost = BridgeSim(self.conn, fault=lambda sim, request: sim.execute(
            request, lose="after" if request["op"] == "xadd" else None))
        code, out = self.cmd(ME, "accept", rid, "--next", "x", bridge=lost)
        self.assertEqual(work.UNKNOWN_EXIT, code, out)
        req = re.search(r"if r2 carries req=([0-9a-f]{8})", out)[1]
        self.assertEqual([], self.ops(), "an unknown move was repeated by the board route")
        self.assertEqual(["xadd"], [e["op"] for e in bc.pending(self.journal)], "the journal holds it, unsettled")
        code, out = self.cmd("grok", "show", rid)
        self.assertIn("req=%s" % req, out, "`show` did not say that the move landed")
        self.assertEqual("accepted", self.state(rid)["state"])
        self.assertEqual(1, self.listed(rid)["rev"], "the list was not written, and nothing pretended it was")
        code, out = self.cmd(ME, "reindex", rid)
        self.assertEqual(0, code, out)
        self.assertEqual((2, "accepted"), (self.listed(rid)["rev"], self.listed(rid)["state"]))

    def test_PARTIAL_FAILURE_a_move_that_landed_but_whose_list_line_did_not_is_said_and_repaired(self):
        rid = self.create(owner="grok")
        self.worker.drop.add("hset")                                  # the list write is never answered
        real_post = self.worker.post

        def post_but_lose_the_list(spec):                             # and say it truly never landed
            if "|op=hset|" in spec["payload"]:
                self.worker.asked.append(spec["payload"])
                return spec["row_id"]
            return real_post(spec)

        self.worker.post = post_but_lose_the_list
        code, out = self.cmd("grok", "accept", rid, "--next", "x")
        self.assertEqual(3, code, out)
        self.assertIn("MOVED: %s is at r2" % rid, out)
        self.assertIn("THE LIST WAS NOT UPDATED", out)
        self.assertIn("Run: reindex %s" % rid, out)
        self.assertEqual("accepted", self.state(rid)["state"], "the move itself is whole")
        self.worker.post = real_post
        self.worker.drop.clear()
        code, out = self.cmd("cursor", "list")
        self.assertIn("requested", out, "the list lags, as said")
        code, out = self.cmd("cursor", "show", rid)                   # the record never depended on the list
        self.assertIn("accepted", out.splitlines()[0])
        self.assertEqual(0, self.cmd("cursor", "reindex", rid)[0])
        self.assertEqual("accepted", self.listed(rid)["state"])

    def test_TIMEOUT_no_answer_to_the_move_is_said_and_it_is_never_sent_twice(self):
        rid = self.create(owner="grok")
        self.ops()
        self.worker.drop.add("xadd")
        code, out = self.cmd("grok", "accept", rid, "--next", "x")
        self.assertEqual(2, code)
        self.assertIn("NO ANSWER YET", out)
        self.assertIn("it is not sent again", out)
        self.assertIn("if r2 carries req=", out)
        self.assertEqual(["xrange", "hgetall", "xadd"], self.ops(), "sent twice, or the list was written on a guess")
        self.worker.drop.clear()
        code, out = self.cmd("grok", "accept", rid, "--next", "x")    # the first one did land, unanswered
        self.assertEqual(2, code, out)
        self.assertIn("accept starts from requested, not from accepted", out, "a second accept was written")
        self.assertEqual(2, self.state(rid)["rev"])

    def test_a_worker_that_says_failed_or_an_answer_from_someone_else_is_never_a_success(self):
        rid = self.create(owner="grok")
        self.ops()
        self.conn.fail_next["xadd"] = RuntimeError("connection reset")          # inside the worker's EXEC
        code, out = self.cmd("grok", "accept", rid, "--next", "x")
        self.assertEqual(work.UNKNOWN_EXIT, code, out)
        self.assertIn("MAY HAVE HAPPENED", out)
        self.assertEqual(["xrange", "hgetall", "xadd"], self.ops(), "after a failed write the tool went on")
        real_answer = self.worker.answer
        self.worker.answer = lambda r, w, since: dict(real_answer(r, w, since), **{"from": "grok"})
        code, out = self.cmd("grok", "show", rid)
        self.assertEqual(3, code, out)
        self.assertIn("not bus-reconciler; nothing here is trusted", out)
        code, out = self.cmd("grok", "start", rid)
        self.assertEqual(3, code, out)
        self.assertEqual(["xrange", "xrange"], self.ops(), "a write followed a read nobody vouches for")

    # ------------------------------------------------------- "Enforce actor/role authority"
    def refused(self, who, *argv, says="", code=2):
        before = self.snapshot()
        self.ops()
        got, out = self.cmd(who, *argv)
        self.assertEqual(code, got, (argv, out))
        self.assertIn(says, out)
        self.assertFalse({"xadd", "hset", "hput"} & set(self.ops()), "a refused move wrote something")
        self.assertEqual(before, self.snapshot(), argv)

    def test_a_write_naming_you_is_not_you_accepting_it(self):
        rid = self.create(owner="grok")
        self.refused("cursor", "accept", rid, "--next", "x", says="only grok may accept it, and this is cursor")
        self.refused(ME, "accept", rid, "--next", "x", says="only grok may accept it")     # not the requester either
        self.refused("grok", "accept", rid, says="accept needs --next")
        self.refused("grok", "start", rid, says="start starts from accepted, not from requested")
        self.cmd("grok", "accept", rid, "--next", "x")
        self.cmd("grok", "handoff", rid, "--to", "cursor")
        self.assertEqual(("requested", "cursor", "-"), tuple(self.state(rid)[k] for k in ("state", "owner", "lease")))
        self.refused("cursor", "start", rid, says="not from requested")                    # cursor has not accepted
        self.refused("grok", "accept", rid, "--next", "x", says="only cursor may accept it")
        self.refused("cursor", "handoff", rid, "--to", "cursor", says="a handoff names a new owner")
        self.refused("cursor", "handoff", rid, "--to", "nobody-at-all", says="is not an agent")

    def test_a_record_in_review_is_closed_by_its_reviewer_and_nobody_else(self):
        rid = self.create(owner="grok")
        self.cmd("grok", "accept", rid, "--next", "x")
        self.refused("grok", "review", rid, "--reviewer", "grok", says="a reviewer who is not the owner")
        self.refused("grok", "review", rid, says="a review names a reviewer")
        self.refused("grok", "done", rid, says="done needs --evidence")
        self.assertEqual(0, self.cmd("grok", "review", rid, "--reviewer", "aya")[0])
        self.refused("grok", "done", rid, "--evidence", LINK, says="only chatgpt-codex-desktop may done it")
        self.refused(ME, "done", rid, "--evidence", LINK, says="only chatgpt-codex-desktop")
        self.refused("grok", "return", rid, "--note", "x", says="only chatgpt-codex-desktop may return it")
        self.assertEqual(0, self.cmd("aya", "return", rid, "--note", "one test is missing")[0])
        self.assertEqual("working", self.state(rid)["state"])
        self.refused("grok", "done", rid, "--evidence", LINK, says="a reviewer (chatgpt-codex-desktop) is named")
        self.assertEqual(0, self.cmd("grok", "review", rid)[0], "the reviewer already named needs no naming again")
        self.assertEqual(0, self.cmd("aya", "done", rid, "--evidence", LINK)[0])
        for argv in (["start", rid], ["cancel", rid, "--note", "x"], ["handoff", rid, "--to", "cursor"],
                     ["done", rid, "--evidence", LINK]):
            self.refused("grok", *argv, says="not from done")                              # the end is the end

    def test_a_reviewer_named_at_create_cannot_be_skipped(self):
        rid = self.create(owner="grok", extra=("--reviewer", "aya"))
        self.assertEqual(AYA, self.state(rid)["reviewer"])
        self.cmd("grok", "accept", rid, "--next", "x")
        self.refused("grok", "done", rid, "--evidence", LINK, says="it goes to review, and the reviewer closes it")
        self.assertIn("the reviewer cannot be the owner",
                      self.create(owner="grok", extra=("--reviewer", "grok"), expect=2))

    def test_blocked_needs_a_blocker_and_cancel_needs_a_reason_from_someone_who_holds_it(self):
        rid = self.create(owner="grok")
        self.cmd("grok", "accept", rid, "--next", "x")
        self.refused("grok", "block", rid, says="block needs --blocker")
        self.refused("cursor", "block", rid, "--blocker", "x", says="only grok may block it")
        self.assertEqual(0, self.cmd("grok", "block", rid, "--blocker", "waiting on D2")[0])
        self.assertEqual("blocker=waiting on D2", self.state(rid)["words"])
        self.refused("grok", "review", rid, "--reviewer", "aya", says="not from blocked")
        self.assertEqual(0, self.cmd("grok", "unblock", rid)[0])
        self.refused("cursor", "cancel", rid, "--note", "x", says="only grok or claude-code-cli may cancel it")
        self.refused("grok", "cancel", rid, says="cancel needs --note")
        self.assertEqual(0, self.cmd(ME, "cancel", rid, "--note", "no longer wanted")[0])   # the requester
        self.assertEqual(("cancelled", "-"), (self.state(rid)["state"], self.state(rid)["lease"]))

    def test_a_move_made_around_the_tool_is_caught_by_every_reader_and_nothing_is_built_on_it(self):
        rid = self.create(owner="grok")
        self.cmd("grok", "accept", rid, "--next", "x")
        # cursor is neither owner, reviewer nor requester. The store takes the entry: it is in sequence.
        self.assertTrue(self.raw(rid, "cursor", action="done", state="done", lease="-")["ok"])
        code, out = self.cmd(ME, "show", rid)
        self.assertEqual(work.DISPUTED_EXIT, code, out)
        self.assertIn("DISPUTED: r3 done by cursor is not a move the rules allow (only grok may done it", out)
        self.refused("grok", "reopen", rid, "--note", "x", says="is DISPUTED", code=work.DISPUTED_EXIT)
        self.refused(ME, "reopen", rid, "--note", "x", says="Nothing is built on it", code=work.DISPUTED_EXIT)
        self.refused(ME, "reindex", rid, says="is DISPUTED", code=work.DISPUTED_EXIT)
        code, out = self.cmd(ME, "history", rid)
        self.assertEqual(work.DISPUTED_EXIT, code, out)
        self.assertIn("3-0: NOT ALLOWED: only grok may done it, and this is cursor", out)
        self.assertIn("DISPUTED: 1 not allowed", out)

    def test_history_checks_every_entry_not_only_the_last(self):
        rid = self.create(owner="grok")
        self.assertTrue(self.raw(rid, "cursor", action="handoff", state="requested", owner="cursor")["ok"])  # a theft
        self.assertEqual(work.DISPUTED_EXIT, self.cmd("cursor", "accept", rid, "--next", "x")[0])
        self.assertTrue(self.raw(rid, "cursor", action="accept", state="accepted", lease="20261013T0900Z")["ok"])
        code, out = self.cmd(ME, "show", rid)                         # the LAST move follows from the one before
        self.assertEqual(0, code, "the last entry alone is legal after the theft")
        code, out = self.cmd(ME, "history", rid)                      # the whole history does not
        self.assertEqual(work.DISPUTED_EXIT, code, out)
        self.assertIn("2-0: NOT ALLOWED: only grok or claude-code-cli may handoff it, and this is cursor", out)
        for shape in ("r2 hello there", "r2 dance requested a b c - x", "r2 start working"):
            self.conn = FakeRedis()
            self.worker = Worker(self.conn)
            rid = self.create(owner="grok")
            self.assertTrue(run(self.conn, "grok", "xadd", work.LOG % rid, val=shape)["ok"], shape)
            code, out = self.cmd(ME, "show", rid)
            self.assertEqual(3, code, shape)
            self.assertIn("is not one this tool wrote; nothing is built on it", out, shape)

    def test_the_rules_are_one_function_for_the_writer_and_for_every_reader(self):
        source = (ROOT / "scripts" / "redis_work.py").read_text(encoding="utf-8")
        self.assertEqual(1, source.count("def allowed("))
        self.assertGreaterEqual(source.count("allowed("), 5, "create, a move, the last entry, the history")
        one = {"rev": 1, "action": "create", "state": "requested", "owner": "grok", "reviewer": "-",
               "requester": ME, "lease": "-", "req": "x", "words": "", "by": ME}
        self.assertEqual("", work.allowed(None, one))
        for change, why in (({"by": "grok"}, "by its requester"), ({"rev": 2}, "r1 create"),
                            ({"state": "done"}, "created requested"), ({"lease": "20261013T0900Z"}, "no lease")):
            self.assertIn(why, work.allowed(None, dict(one, **change)), change)
        accept = dict(one, rev=2, action="accept", state="accepted", lease="20261013T0900Z", by="grok")
        self.assertEqual("", work.allowed(one, accept))
        for change, why in (({"rev": 3}, "does not follow"), ({"requester": "grok"}, "requester of a record never"),
                            ({"owner": "cursor"}, "only a handoff changes the owner"), ({"lease": "-"}, "sets a lease"),
                            ({"reviewer": "cursor"}, "changes the reviewer"), ({"state": "working"}, "ends in accepted"),
                            ({"action": "fly"}, "is not a move")):
            self.assertIn(why, work.allowed(one, dict(accept, **change)), change)
        note = dict(accept, rev=3, action="note", by="cursor")
        self.assertEqual("", work.allowed(accept, note), "anyone may add a note")
        self.assertIn("a note moves nothing", work.allowed(accept, dict(note, state="done")))

    # ------------------------------------------------- "lease expiry does not imply completion"
    def test_EXPIRY_a_lease_that_ran_out_is_shown_and_gives_nobody_anything(self):
        rid = self.create(owner="grok")
        self.cmd("grok", "accept", rid, "--next", "x", "--lease-minutes", "30")
        self.assertEqual("20261013T0530Z", self.state(rid)["lease"])
        for minutes in (0, 29, 30):                                   # live up to and including its last minute
            self.at = T0 + datetime.timedelta(minutes=minutes)
            self.assertNotIn("EXPIRED", self.cmd("cursor", "show", rid)[1] + self.cmd("cursor", "list")[1], minutes)
        self.at = T0 + datetime.timedelta(hours=9)
        code, out = self.cmd("cursor", "show", rid)
        self.assertIn("lease 20261013T0530Z EXPIRED (it gives nobody anything; the owner still holds it)", out)
        self.assertEqual("accepted", self.state(rid)["state"], "an expired lease changed the state")
        for argv, says in ((["handoff", rid, "--to", "cursor"], "only grok or claude-code-cli may handoff it"),
                           (["cancel", rid, "--note", "abandoned"], "may cancel it"),
                           (["done", rid, "--evidence", LINK], "only grok may done it"),
                           (["start", rid], "only grok may start it")):
            self.refused("cursor", *argv, says=says)                  # a stranger, after the lease ran out
        code, out = self.cmd("cursor", "list")
        self.assertIn("EXPIRED", out)
        self.assertEqual(0, self.cmd("grok", "renew", rid, "--lease-minutes", "10")[0], "the owner still holds it")
        self.assertEqual("20261013T1410Z", self.state(rid)["lease"])
        self.assertEqual(0, self.cmd(ME, "handoff", rid, "--to", "cursor")[0], "its requester may move it on")
        self.assertEqual(("requested", "cursor", "-"), tuple(self.state(rid)[k] for k in ("state", "owner", "lease")))

    # ------------------------------------------------------------------------------------ REOPEN
    def test_REOPEN_only_the_requester_reopens_and_the_owner_must_accept_again(self):
        rid = self.create(owner="grok")
        self.cmd("grok", "accept", rid, "--next", "x")
        self.cmd("grok", "review", rid, "--reviewer", "aya")
        self.cmd("aya", "done", rid, "--evidence", LINK)
        for who in ("grok", "aya", "cursor"):
            self.refused(who, "reopen", rid, "--note", "x", says="only claude-code-cli may reopen it")
        self.refused(ME, "reopen", rid, says="reopen needs --note")
        code, out = self.cmd(ME, "reopen", rid, "--note", "it broke on the next deploy")
        self.assertEqual(0, code, out)
        self.assertEqual(("requested", "grok", "-", "-", 5),
                         tuple(self.state(rid)[k] for k in ("state", "owner", "reviewer", "lease", "rev")))
        self.refused("grok", "start", rid, says="not from requested")
        self.assertEqual(0, self.cmd("grok", "accept", rid, "--next", "find the regression")[0])
        self.refused(ME, "reopen", rid, "--note", "x", says="reopen starts from done or cancelled")
        code, out = self.cmd("cursor", "history", rid)
        self.assertEqual(0, code, out)
        self.assertIn("6 entries", out)
        self.assertIn("VERIFIED", out, "the history across a reopen is whole and legal")
        self.assertEqual(6, len(self.conn.data[work.LOG % rid]), "nothing of the first life was rewritten")

    # ---------------------------------------- OVERFLOW: "never silently truncate critical handoffs"
    def test_OVERFLOW_what_would_not_read_back_whole_is_refused_with_the_number_before_any_write(self):
        good = {"--title": "x", "--owner": "grok", "--project": "Blackboard", "--source": "s", "--done-when": "d"}
        for flag, value, says in (("--title", "t" * 101, "--title is 101 characters; at most 100"),
                                  ("--title", "a | b", "may not hold a pipe"),
                                  ("--source", "s" * 151, "--source is 151 characters; at most 150"),
                                  ("--done-when", "d" * 161, "--done-when is 161 characters; at most 160"),
                                  ("--title", "   ", "--title is required"),
                                  ("--project", "two words", "--project is one word"),
                                  ("--link", "https://example.com/pull/1", "a GitHub pull request or issue")):
            argv = [part for pair in dict(good, **{flag: value}).items() for part in pair]
            code, out = self.cmd(ME, "create", *argv)
            self.assertEqual(2, code, (flag, out))
            self.assertIn(says, out)
        self.assertEqual([], self.ops(), "a refused create asked the worker for something")
        self.assertEqual({}, self.snapshot())
        rid = self.create(title="t" * 100, owner="codex")
        self.assertEqual("t" * work.INDEX_TITLE, self.listed(rid)["title"], "the list holds a short title, by design")
        self.assertEqual("t" * 100, self.conn.data[work.REC % rid]["title"], "the record holds all of it")
        self.cmd("codex", "accept", rid, "--next", "x")
        before = self.snapshot()
        self.ops()
        code, out = self.cmd("codex", "block", rid, "--blocker", "b" * 90, "--note", "n" * 90)
        self.assertEqual(2, code)
        self.assertRegex(out, r"this entry would be \d+ characters; a governed read shows 200, and an entry is never "
                              r"cut. Shorten what you wrote by \d+. Nothing was written.")
        self.assertEqual(["xrange"], self.ops(), "an entry too long to read back was partly written")
        self.assertEqual(before, self.snapshot())
        for key, value in self.conn.data.items():
            if key.startswith(work.NS):
                for text in (value.values() if isinstance(value, dict) else [f["text"] for _, f in value]):
                    self.assertEqual(text, redis_gov.preview(text), "a governed read would not show this whole")

    def test_OVERFLOW_a_day_whose_list_is_full_refuses_the_next_record_and_the_list_stays_whole(self):
        made, code, out = [], 0, ""
        for n in range(40):
            code, out = self.cmd(ME, "create", "--title", "A title of thirty characters.. %02d" % n, "--owner",
                                 "codex", *BASE)
            if code != 0:
                break
            made.append(out.split()[0])
        self.assertEqual(2, code, out)
        self.assertRegex(out, r"not created: with one more record today's list \(\d+\) would not fit one board row. "
                              r"Create it through the bridge, or tomorrow. Nothing was written.")
        self.assertGreaterEqual(len(made), 5, "the board route holds a working day's worth")
        self.assertEqual(len(made), len([k for k in self.conn.data if k.startswith(work.NS + "rec:")]),
                         "the refused record left a description behind")
        code, out = self.cmd("grok", "list")                          # and what WAS created is all listable
        self.assertEqual(0, code, out)
        self.assertEqual(sorted(made), [line.split()[0] for line in out.splitlines()[:-1]])
        bridge = BridgeSim(self.conn)                                 # the bridge's bound is the read's own: fifty
        while len(made) < work.INDEX_MAX:
            made.append(self.create(owner=ME, title="Through the bridge %02d" % len(made), bridge=bridge))
        code, out = self.cmd(ME, "create", "--title", "one too many", "--owner", ME, *BASE, bridge=bridge)
        self.assertEqual(2, code, out)
        self.assertIn("not created: today's list is full (50 records)", out)
        code, out = self.cmd("grok", "list")                          # by the board the day no longer fits
        self.assertEqual(4, code, out)
        self.assertIn("%s: NOT LISTED. The day's records do not fit one board row" % DAY, out)
        self.assertNotIn(made[0], out, "part of a day was listed as if it were the day")
        self.assertIn("today's list no longer fits one board row", self.create(owner="grok", expect=2))
        code, out = self.cmd("grok", "show", made[0])                 # but one record never depends on the list
        self.assertEqual(0, code, out)
        code, out = self.cmd(ME, "list", bridge=bridge)
        self.assertEqual(sorted(made), [line.split()[0] for line in out.splitlines()[:-1]])
        self.at = T0 + datetime.timedelta(days=1)                     # and tomorrow is another list
        self.assertTrue(self.create(owner="grok").startswith("W-20261014-"))

    # ----------------------------------------------- the ledger: once, in order, never removed
    def test_a_description_is_written_once_and_an_id_is_never_reused(self):
        with mock.patch.object(work, "new_id", lambda moment: "W-20261013-ababab"):
            first = self.create(owner="grok")
            before = self.snapshot()
            code, out = self.cmd("cursor", "create", "--title", "another thing entirely", "--owner", "cursor",
                                 "--project", "site", "--source", "s", "--done-when", "d")
        self.assertEqual(work.REFUSED_EXIT, code, out)
        self.assertIn("a description is written once", out)
        self.assertEqual(before, self.snapshot(), "a second create under one id changed the first")
        self.assertEqual("Wire the reader", self.conn.data[work.REC % first]["title"])

    def test_the_record_is_not_given_an_expiry_and_cannot_be_deleted_through_the_governed_path(self):
        rid = self.create(owner="grok")
        self.assertNotIn(work.LOG % rid, self.conn.ttls)
        self.assertNotIn(work.REC % rid, self.conn.ttls)
        self.assertTrue(self.conn.ttls.get(INDEX), "the list is a cache and expires")
        for key in (work.LOG % rid, work.REC % rid):
            self.assertIn("refused", run(self.conn, ME, "del", key), key)
        self.assertIn(work.LOG % rid, self.conn.data)

    def test_it_writes_only_its_own_namespace_and_never_the_pr_boards(self):
        rid = self.create(owner="grok")
        self.cmd("grok", "accept", rid, "--next", "x")
        touched = {entry["key"] for entry in self.conn.audit()}
        self.assertEqual({INDEX, work.REC % rid, work.LOG % rid}, touched)
        source = (ROOT / "scripts" / "redis_work.py").read_text(encoding="utf-8")
        self.assertEqual(1, source.count('NS = "proj:work:v1:"'))
        self.assertNotIn('"proj:pr-board', source)
        self.assertEqual((work.REC % "", work.LOG % "", work.INDEX % ""),
                         (redis_gov.WRITE_ONCE[0], redis_gov.SEQUENCED[0], redis_gov.REV_FORWARD[0]))

    # ------------------------------------------------------- off until the governor is proven
    def test_it_is_off_by_default_and_then_nothing_is_written_but_everything_is_readable(self):
        rid = self.create(owner="grok")
        before = self.snapshot()
        self.ops()
        for switch in ("", "off", "true", "1", "yes"):
            for argv in (["create", "--title", "x", "--owner", "grok", "--project", "p", "--source", "s",
                          "--done-when", "d"], ["accept", rid, "--next", "x"], ["note", rid, "--note", "x"],
                         ["reindex", rid]):
                code, out = self.cmd("grok", *argv, switch=switch)
                self.assertEqual(2, code, (switch, argv))
                self.assertIn("work records are OFF for this agent: nothing is written", out)
        self.assertEqual([], self.ops(), "a command asked the worker for something with the switch off")
        self.assertEqual(before, self.snapshot())
        for argv in (["show", rid], ["list"], ["history", rid]):
            self.assertEqual(0, self.cmd("grok", *argv, switch="")[0], argv)

    def test_preflight_says_whether_the_governor_on_a_route_enforces_sequence(self):
        code, out = self.cmd("grok", "preflight", switch="")
        self.assertEqual(2, code)
        self.assertIn("--allow-stray-entry", out)
        self.assertEqual([], self.ops())
        code, out = self.cmd("grok", "preflight", "--allow-stray-entry", switch="")
        self.assertEqual(0, code, out)
        self.assertIn("PASS by the board route: the governor refused an entry out of sequence", out)
        code, out = self.cmd(ME, "preflight", "--allow-stray-entry", switch="", bridge=BridgeSim(self.conn))
        self.assertEqual(0, code, out)
        self.assertIn("PASS by the bridge route", out)
        self.assertEqual({}, self.snapshot(), "a passing preflight wrote something")
        with mock.patch.object(redis_gov, "SEQUENCED", ()):           # a governor from before the rule
            code, out = self.cmd("grok", "preflight", "--allow-stray-entry", switch="")
        self.assertEqual(1, code, out)
        self.assertIn("FAIL by the board route: THIS GOVERNOR TOOK AN ENTRY OUT OF SEQUENCE", out)
        self.assertIn("Do not set REDIS_WORK=on for this route", out)
        self.assertEqual([work.LOG % "preflight-grok"], list(self.snapshot()), "and it says where the stray entry is")

    # ------------------------------------------------------------------------- the smaller things
    def test_an_issue_is_a_pointer_and_github_stays_the_authority(self):
        for link in ("", "https://example.com/issues/1", LINK, "https://github.com/sfdc-24/Blackboard/issues/"):
            code, out = self.cmd(ME, "create", "--kind", "issue", "--title", "Q11", "--owner", "grok", *BASE,
                                 "--link", link)
            self.assertEqual(2, code, link)
            self.assertIn("GitHub", out)
        self.assertEqual([], self.ops(), "a refused create asked the worker for something")
        link = "https://github.com/sfdc-24/Blackboard/issues/395"
        rid = self.create(extra=("--kind", "issue", "--link", link))
        self.assertEqual(link, self.conn.data[work.REC % rid]["link"])
        self.assertEqual("issue", self.listed(rid)["kind"])
        task = self.create(extra=("--link", LINK))                    # a task may point at a pull request
        self.assertEqual(LINK, self.conn.data[work.REC % task]["link"])

    def test_nobody_who_is_not_an_agent_acts_or_owns(self):
        code, out = self.cmd("someone-else", "list")
        self.assertEqual((2, "'someone-else' is not an agent in scripts/redis_acl.json"), (code, out))
        self.assertIn("is not an agent", self.create(owner="someone-else", expect=2))
        self.assertEqual([], self.ops())
        rid = self.create(owner="codex")                              # an alias names its principal
        self.assertEqual(AYA, self.state(rid)["owner"])

    def test_an_account_bound_to_another_agent_is_not_used_and_the_agent_acts_as_itself(self):
        rid = self.create(owner="grok")
        self.ops()
        code, out = self.cmd("grok", "accept", rid, "--next", "x", environ=ON)     # SA is claude-code-cli's
        self.assertEqual(0, code, out)
        self.assertIn("would not be acting as itself", out)
        self.assertEqual(MOVE_OPS, self.ops())
        self.assertEqual("grok", self.log(rid)[-1][0])

    def test_a_denied_bridge_call_is_terminal(self):
        rid = self.create(owner=ME)
        self.ops()
        code, out = self.cmd(ME, "accept", rid, "--next", "x", bridge=BridgeSim(self.conn, principal=AYA))
        self.assertEqual(work.DENIED_EXIT, code, out)
        self.assertEqual([], self.ops(), "a denied caller was moved to the board route")
        self.assertEqual("requested", self.state(rid)["state"])

    def test_list_shows_what_is_open_and_a_note_moves_nothing(self):
        first = self.create(owner="grok", title="First")
        second = self.create(owner="cursor", title="Second")
        self.cmd(ME, "cancel", second, "--note", "duplicate")
        before = self.state(first)
        code, out = self.cmd("cursor", "note", first, "--note", "I can take this if grok is out")
        self.assertEqual(0, code, out)
        after = self.state(first)
        self.assertEqual((2, "note", "cursor", "I can take this if grok is out"),
                         (after["rev"], after["action"], after["by"], after["words"]))
        self.assertEqual(tuple(before[k] for k in ("state", "owner", "reviewer", "lease")),
                         tuple(after[k] for k in ("state", "owner", "reviewer", "lease")))
        self.assertEqual(2, self.cmd("cursor", "note", first)[0])
        code, out = self.cmd(ME, "list")
        self.assertEqual(0, code, out)
        self.assertEqual(1, out.count("W-"), out)
        self.assertIn("1 open records over 20261013, 20261012. A list is for finding a record", out)
        code, out = self.cmd(ME, "list", "--all", "--day", DAY)
        self.assertEqual(2, out.count("W-"))
        self.assertEqual(2, self.cmd(ME, "list", "--day", "today")[0])
        self.assertEqual((2, "no record W-20261013-ffffff: its log is empty"), self.cmd(ME, "show", "W-20261013-ffffff"))
        self.assertEqual(2, self.cmd(ME, "show", "W-1")[0])

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(0, work.main(["selftest"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

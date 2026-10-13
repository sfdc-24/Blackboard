"""scripts/redis_work.py: tasks and issues an agent can create, read, move, hand off and close on Redis
(Aya's Stage 1 exit check 4; her conditions in AYA-STAGE1-WORK-RECORDS-SOURCE-DESIGN-20261013 and her
review of the first version, AYA-PR403-ACTOR-ADMISSION-ADDENDUM-20261013). Offline.

EVERY WRITE HERE GOES THROUGH THE REAL BOUNDARY: redis_work -> bridge_client.call -> main.Handler ->
main.verify_caller -> op.handle -> redis_gov.execute(verified=True) over one fake Redis
(test_bridge_client.BridgeSim). Each agent has its own account, bound to its own principal, as callers.json
would bind it. The board route is PR 397's Worker (the worker's own bus_request.handle): it can read a
record and, by the store's rule, can never write one.
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
from test_bridge_client import BridgeSim, ON, TOKEN, Worker  # noqa: E402
from test_redis_gov import FakeRedis, run, work_entry  # noqa: E402

ME, AYA = "claude-code-cli", "chatgpt-codex-desktop"
T0 = datetime.datetime(2026, 10, 13, 5, 0, tzinfo=datetime.timezone.utc)
DAY = "20261013"
INDEX = work.INDEX % DAY
LINK = "https://github.com/sfdc-24/Blackboard/pull/402"
BASE = ["--project", "Blackboard", "--source", "board row AYA-REDIS-STAGE1-EXIT-CRITERIA-20261013",
        "--done-when", "the suite is green on main"]
MOVE = ["xrange", "hgetall", "xadd", "hset"]          # the whole log, the description, THE MOVE, the list


def canon(agent):
    """The principal a name stands for (an alias names its principal), or the name itself."""
    return redis_gov.principal(redis_gov.load_acl(), agent) or agent


def account(agent):
    """The service account this test binds to one agent. A real account id is 6 to 30 characters."""
    return "wk-%s@sfdc24.iam.gserviceaccount.com" % canon(agent)


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = os.path.join(self.tmp.name, "unknown.jsonl")
        self.conn = FakeRedis()
        self.worker = Worker(self.conn)
        self.sims = {}
        self.at = T0

    def sim(self, who, **kw):
        """The bridge as `who` meets it: its own account, verified, bound to its own principal."""
        who = canon(who)
        if kw:
            return BridgeSim(self.conn, principal=kw.pop("principal", who), account=account(who), **kw)
        if who not in self.sims:
            self.sims[who] = BridgeSim(self.conn, principal=who, account=account(who))
        return self.sims[who]

    def cmd(self, who, *argv, route="bridge", bridge=None, switch="on"):
        """One command as `who`: through the bridge (its verified identity), or by the board (a claim)."""
        printed = []
        environ = {}
        if route == "bridge":
            bridge = bridge or self.sim(who)
            environ = dict(ON, REDIS_BRIDGE_IMPERSONATE=account(who))
        if switch:
            environ[work.ENV_SWITCH] = switch
        patches = [
            mock.patch.object(inbox, "post", self.worker.post), mock.patch.object(inbox, "answer", self.worker.answer),
            mock.patch.object(bc, "STATE", os.path.join(self.tmp.name, "breaker.json")),
            mock.patch.object(bc, "JOURNAL", self.journal),
            mock.patch.object(bc, "load_callers", lambda path=None: {account(who): canon(who)}),
            mock.patch.object(bc, "mint", lambda url, acct: TOKEN),
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
        found = re.search(r"(W-[0-9]{8}-[0-9a-f]{6}) created", out)
        return found[1] if found else out

    def log(self, rid):
        return [(f["by"], f["text"]) for _, f in self.conn.data.get(work.LOG % rid, [])]

    def state(self, rid):
        return work.parse_event(dict(self.conn.data[work.LOG % rid][-1][1]))

    def listed(self, rid):
        return work.read_index_line(self.conn.data[INDEX][rid])

    def ops(self):
        """Every op asked so far - through any agent's bridge, then by the board - and forget them."""
        asked = [r["op"] for sim in self.sims.values() for r in sim.requests]
        asked += ["board:" + re.search(r"\|op=([a-z]+)\|", a)[1] for a in self.worker.asked]
        for sim in self.sims.values():
            del sim.requests[:]
        del self.worker.asked[:]
        return asked

    def snapshot(self):
        return {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in self.conn.data.items()
                if k.startswith(work.NS)}

    def entry(self, rid, **fields):
        """The text of a next entry for `rid`, built from its last one."""
        last = self.state(rid)
        ev = dict(last, req="handmade", words="", rev=last["rev"] + 1)
        ev.update(fields)
        return work.event_text(ev)

    def around(self, rid, by, **fields):
        """An entry written AROUND the governor, straight into Redis: what only a holder of the Redis
        password could do. The store never admitted it."""
        self.conn.xadd(work.LOG % rid, {"text": self.entry(rid, **fields), "by": by, "at": "2026-10-13T05:00:00Z"})

    def refused(self, who, *argv, says="", code=2, **kw):
        before = self.snapshot()
        self.ops()
        got, out = self.cmd(who, *argv, **kw)
        self.assertEqual(code, got, (argv, out))
        self.assertIn(says, out)
        self.assertFalse({"xadd", "hset", "hput"} & {o.split(":")[-1] for o in self.ops()}, "a refused move was sent")
        self.assertEqual(before, self.snapshot(), argv)

    # ------------------------------------------------------------------------- the whole life of one
    def test_a_task_from_request_to_done_with_every_handoff_traced(self):
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
            asked = self.ops()
            self.assertEqual(MOVE, [asked[0]] + asked[-3:], "the log, the description, ONE conditional write, the list")
            self.assertEqual(1, asked.count("xadd"))
            self.assertEqual(state, self.state(rid)["state"], argv)
            self.assertEqual((self.state(rid)["rev"], state), (self.listed(rid)["rev"], self.listed(rid)["state"]))
        self.assertEqual([], self.worker.asked, "no board row was spent")
        final = self.state(rid)
        self.assertEqual((7, "cursor", AYA, ME, "-"),
                         tuple(final[k] for k in ("rev", "owner", "reviewer", "requester", "lease")))
        # THE TRACED HANDOFF: actor (the VERIFIED caller) and time by the governor, request id, revisions
        self.assertEqual([ME, "grok", "grok", "grok", "cursor", "cursor", AYA], [by for by, _ in self.log(rid)])
        events = [work.parse_event(dict(f)) for _, f in self.conn.data[work.LOG % rid]]
        self.assertEqual(list(range(1, 8)), [e["rev"] for e in events])
        self.assertEqual(7, len({e["req"] for e in events}), "every move carries its own request id")
        self.assertTrue(all(e["at"] and e["by"] for e in events))
        self.assertEqual("from=grok note=out of budget", events[3]["words"])
        self.assertEqual("evidence=" + LINK, events[6]["words"])
        self.assertEqual({"bridge:" + account(a) for a in (ME, "grok", "cursor", AYA)},
                         {e["via"] for e in self.conn.audit() if e["key"] == work.LOG % rid and e["op"] == "xadd"})
        record = self.conn.data[work.REC % rid]
        self.assertEqual((ME, "Wire the reader", "task", "Blackboard", "the suite is green on main"),
                         tuple(record[k] for k in ("requested_by", "title", "kind", "project", "done_when")))
        code, out = self.cmd("grok", "show", rid)
        self.assertEqual(0, code, out)
        self.assertIn("done", out.splitlines()[0])
        self.assertIn("done when: the suite is green on main", out)
        self.assertIn("r6>r7 done -> done owner=cursor", out)
        self.assertIn("7 entries, each a move its actor was allowed to make", out)
        code, out = self.cmd("gemini", "history", rid, route="board")          # anyone may read, by any route
        self.assertEqual(0, code, out)
        self.assertIn("7 entries of %s. VERIFIED: every one is a move its actor was allowed to make." % rid, out)
        self.assertEqual(["r%d>r%d" % (n - 1, n) for n in range(1, 8)],
                         re.findall(r"r\d+>r\d+", "\n".join(out.splitlines()[:7])), "oldest first, all of it")

    # ------------------------------------- Aya: "derive actor from authenticated transport"
    def test_REGRESSION_the_board_route_reads_a_record_and_can_never_move_one(self):
        """"Board claimed Source_Tag/--as is not authenticated identity; refuse this protected transition
        path." The owner's own legal move, sent with the owner's own tag, by the board."""
        rid = self.create(owner="grok")
        before = self.snapshot()
        self.ops()
        for argv in (["accept", rid, "--next", "x"], ["note", rid, "--note", "x"], ["reindex", rid],
                     ["create", "--title", "x", "--owner", "grok", *BASE]):
            code, out = self.cmd("grok", *argv, route="board")
            self.assertEqual(2, code, (argv, out))
            self.assertIn("NOT WRITTEN. A work record is written only through the bridge", out)
        asked = self.ops()
        self.assertFalse([o for o in asked if o.split(":")[-1] in ("xadd", "hset", "hput")],
                         "a write was posted to the board, where the store would refuse it anyway")
        self.assertEqual(before, self.snapshot())
        # and if someone posts that row by hand, the STORE refuses it: the tool is not what stops it
        text = self.entry(rid, action="accept", state="accepted", lease="20261013T0900Z", words="next=x")
        spec = work.request_row("grok", "xadd", work.LOG % rid, value=text)
        self.worker.post(spec)
        self.assertIn("REFUSED: a work record is written only by an authenticated caller",
                      self.worker.answers[spec["row_id"]])
        self.assertEqual(before, self.snapshot(), "a claimed identity moved the record")
        for argv in (["show", rid], ["history", rid], ["list"]):
            self.assertEqual(0, self.cmd("gemini", *argv, route="board", switch="")[0], argv)

    def test_as_cannot_make_a_command_someone_the_bridge_did_not_verify(self):
        rid = self.create(owner="grok")
        self.ops()
        mine = self.sim(ME)                                           # this laptop's account is bound to ME
        printed = []
        environ = dict(ON, REDIS_BRIDGE_IMPERSONATE=account(ME), **{work.ENV_SWITCH: "on"})
        with contextlib.ExitStack() as stack, mock.patch.dict(os.environ, environ, clear=False):
            for target, value in ((inbox, {"post": self.worker.post, "answer": self.worker.answer}),
                                  (bc, {"STATE": os.path.join(self.tmp.name, "b.json"), "JOURNAL": self.journal,
                                        "load_callers": lambda path=None: {account(ME): ME},
                                        "mint": lambda url, acct: TOKEN, "call": mine}),
                                  (work, {"now": lambda: self.at})):
                for name, replacement in value.items():
                    stack.enter_context(mock.patch.object(target, name, replacement))
            stack.enter_context(mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))))
            code = work.main(["--as", "grok", "accept", rid, "--next", "x", "--wait", "0"])
        out = "\n".join(printed)
        self.assertEqual(2, code, out)
        self.assertIn("would not be acting as itself", out)
        self.assertIn("NOT WRITTEN", out)
        self.assertEqual("requested", self.state(rid)["state"])
        self.assertNotIn("xadd", self.ops())
        # and an account the service binds to another principal is denied by the service itself
        code, out = self.cmd("grok", "accept", rid, "--next", "x", bridge=self.sim("grok", principal="gemini"))
        self.assertEqual(work.DENIED_EXIT, code, out)
        self.assertEqual("requested", self.state(rid)["state"])

    # ----------------------- Aya: "unauthorized done/handoff and forged actor refused BEFORE stream changes"
    def test_REGRESSION_an_unauthorized_done_or_handoff_never_reaches_the_stream_tool_or_no_tool(self):
        rid = self.create(owner="grok")
        self.cmd("grok", "accept", rid, "--next", "x")
        # 1. with the tool: refused before anything is sent
        self.refused("gemini", "done", rid, "--evidence", LINK, says="only grok may done it, and this is gemini")
        self.refused(ME, "done", rid, "--evidence", LINK, says="only grok may done it")          # the requester
        self.refused("gemini", "handoff", rid, "--to", "gemini", says="only grok or claude-code-cli may handoff it")
        # 2. WITHOUT the tool: a verified caller sends the raw entry itself. The STORE refuses it.
        before = self.snapshot()
        done = self.entry(rid, action="done", state="done", lease="-", words="evidence=" + LINK)
        theft = self.entry(rid, action="handoff", state="requested", owner="gemini", lease="-")
        for text, why in ((done, "only grok may done it, and this is gemini"),
                          (theft, "only grok or claude-code-cli may handoff it, and this is gemini")):
            with self.assertRaises(bc.Refused) as raised:
                self.sim("gemini").execute({"op": "xadd", "key": work.LOG % rid, "expect": "gemini", "enc": "",
                                            "val": text})
            self.assertIn(why, raised.exception.reason)
            self.assertEqual(before, self.snapshot(), "an unauthorized move changed the stream")
        self.assertEqual("0", self.conn.audit()[-1]["ok"], "and the refusal is audited")
        self.assertEqual(0, self.cmd("cursor", "show", rid)[0], "the record is whole and undisputed")

    def test_REGRESSION_a_forged_requester_or_description_is_refused_by_the_store(self):
        rid = "W-20261013-0f0f0f"
        forged = work_entry(1, "create", "requested", owner="grok", requester="cursor")     # "cursor asked"
        with self.assertRaises(bc.Refused) as raised:
            self.sim("gemini").execute({"op": "xadd", "key": work.LOG % rid, "expect": "gemini", "val": forged})
        self.assertIn("by its requester", raised.exception.reason)
        self.assertNotIn(work.LOG % rid, self.conn.data)
        import base64
        import json
        body = base64.b64encode(json.dumps({"title": "x", "requested_by": "cursor"}).encode()).decode()
        with self.assertRaises(bc.Refused) as raised:
            self.sim("gemini").execute({"op": "hput", "key": work.REC % rid, "expect": "gemini", "val": body,
                                        "enc": "b64"})
        self.assertIn("names its verified writer as requested_by", raised.exception.reason)
        self.assertEqual({}, self.snapshot())

    def test_REGRESSION_a_move_without_the_evidence_it_requires_is_refused_by_the_tool_and_by_the_store(self):
        rid = self.create(owner="grok")
        self.refused("grok", "accept", rid, says="accept needs --next")
        self.cmd("grok", "accept", rid, "--next", "x")
        self.refused("grok", "done", rid, says="done needs --evidence")
        self.refused("grok", "block", rid, says="block needs --blocker")
        self.refused("grok", "cancel", rid, says="cancel needs --note")
        before = self.snapshot()
        for fields, why in (({"action": "done", "state": "done", "lease": "-"}, "done must carry evidence="),
                            ({"action": "done", "state": "done", "lease": "-", "words": "note=trust me"},
                             "done must carry evidence="),
                            ({"action": "block", "state": "blocked"}, "block must carry blocker=")):
            with self.assertRaises(bc.Refused) as raised:
                self.sim("grok").execute({"op": "xadd", "key": work.LOG % rid, "expect": "grok",
                                          "val": self.entry(rid, **fields)})
            self.assertIn(why, raised.exception.reason)
        self.assertEqual(before, self.snapshot())

    # ---------------------------------------- Aya: "concurrent valid revisions one winner"
    def test_REGRESSION_two_agents_who_read_the_same_revision_cannot_both_move_it(self):
        rid = self.create(owner="grok")
        rival = self.entry(rid, action="cancel", state="cancelled", req="rival001", words="note=no longer wanted")

        def the_requester_gets_there_first(sim, request):
            if request["op"] == "xadd" and not getattr(self, "raced", False):
                self.raced = True                                    # both read r1; this one lands first
                self.sim(ME).execute({"op": "xadd", "key": work.LOG % rid, "expect": ME, "val": rival})
            return sim.execute(request)

        self.ops()
        racing = BridgeSim(self.conn, principal="grok", account=account("grok"), fault=the_requester_gets_there_first)
        code, out = self.cmd("grok", "accept", rid, "--next", "x", bridge=racing)
        self.assertEqual(work.CONFLICT_EXIT, code, out)
        self.assertIn("CONFLICT: someone moved %s first (revision conflict: the record is at r2" % rid, out)
        self.assertIn("NOTHING of this move was written", out)
        self.assertNotIn("hset", [r["op"] for r in racing.requests], "after losing, it wrote the list anyway")
        self.assertEqual([(ME, "create"), (ME, "cancel")], [(by, t.split()[1]) for by, t in self.log(rid)],
                         "the loser's entry is in the log")
        self.assertEqual(1, self.listed(rid)["rev"], "the loser updated the list")
        code, out = self.cmd("grok", "accept", rid, "--next", "x")   # it reads again: the record is cancelled
        self.assertEqual(2, code, out)
        self.assertIn("accept starts from requested, not from cancelled", out)

    def test_REGRESSION_a_move_whose_answer_was_lost_cannot_land_after_someone_elses(self):
        rid = self.create(owner=ME)
        self.ops()
        slow = BridgeSim(self.conn, principal=ME, account=account(ME), fault=lambda sim, request: sim.execute(
            request, lose="held" if request["op"] == "xadd" else None))
        code, out = self.cmd(ME, "accept", rid, "--next", "x", bridge=slow)        # r2, still on its way
        self.assertEqual(work.UNKNOWN_EXIT, code, out)
        self.assertIn("MAY HAVE HAPPENED and is not sent again", out)
        req = re.search(r"if r2 carries req=([0-9a-f]{8}) this move landed", out)[1]
        self.assertEqual(1, self.state(rid)["rev"])
        self.assertEqual([], self.worker.asked, "an unknown move was sent by the board route")
        self.assertEqual(["xadd"], [e["op"] for e in bc.pending(self.journal)], "the journal holds it, unsettled")
        self.assertEqual(0, self.cmd(ME, "handoff", rid, "--to", "grok")[0])       # someone else's r2 lands
        ((status, body),) = slow.deliver()                                        # NOW the old r2 arrives
        self.assertEqual(403, status)
        self.assertEqual(("refused", False), (body["error"], body["executed"]))
        self.assertIn("revision conflict: the record is at r2", body["reason"])
        self.assertEqual((2, "handoff"), (self.state(rid)["rev"], self.state(rid)["action"]))
        code, out = self.cmd("grok", "show", rid)                                 # and `show` settles it
        self.assertNotIn("req=" + req, out, "r2 carries another request id: the lost one never can land")

    # ----------------------- Aya: "tainted history stays disputed after a legal-looking tail"
    def test_REGRESSION_tainted_history_stays_disputed_after_a_legal_looking_tail(self):
        """The theft and the accept after it are written straight into Redis, around the governor. The last
        pair is a legal move. current, show, a move and reindex all read the WHOLE chain and reject it."""
        rid = self.create(owner="grok")
        self.cmd("grok", "accept", rid, "--next", "x")
        self.around(rid, "gemini", action="handoff", state="requested", owner="gemini", lease="-")     # the theft
        self.around(rid, "gemini", action="accept", state="accepted", owner="gemini", lease="20261013T0900Z",
                    words="next=mine now")                                                           # a legal tail
        events = [work.parse_event(dict(f)) for _, f in self.conn.data[work.LOG % rid]]
        self.assertEqual("", work.allowed(events[-2], events[-1]), "the last pair alone is a legal move")
        history = list(self.conn.data[work.LOG % rid])
        for who in ("gemini", "grok", ME):
            code, out = self.cmd(who, "show", rid)
            self.assertEqual(work.DISPUTED_EXIT, code, out)
            self.assertIn("%s is DISPUTED: 1 of its entries is not a legal move (first: 3-0, only grok or "
                          "claude-code-cli may handoff it, and this is gemini)" % rid, out)
        self.refused("gemini", "start", rid, says="is DISPUTED", code=work.DISPUTED_EXIT)       # the "new owner"
        self.refused(ME, "cancel", rid, "--note", "stop", says="Nothing is built on it", code=work.DISPUTED_EXIT)
        self.refused("grok", "note", rid, "--note", "what happened", says="is DISPUTED", code=work.DISPUTED_EXIT)
        self.refused("gemini", "reindex", rid, says="is DISPUTED", code=work.DISPUTED_EXIT)
        # the store refuses the same things whoever asks and however: nothing inherits the tainted ownership
        with self.assertRaises(bc.Refused) as raised:
            self.sim("gemini").execute({"op": "xadd", "key": work.LOG % rid, "expect": "gemini",
                                        "val": self.entry(rid, action="start", state="working")})
        self.assertIn("this record is disputed at entry 3-0", raised.exception.reason)
        with self.assertRaises(bc.Refused):
            self.sim("gemini").execute({"op": "hset", "key": INDEX, "field": rid, "expect": "gemini",
                                        "val": work.index_line(self.state(rid), "task", "Wire the reader")})
        self.assertEqual("grok", self.listed(rid)["owner"], "the list followed the theft")
        self.assertEqual(history, self.conn.data[work.LOG % rid], "history is preserved exactly, for reconciliation")
        code, out = self.cmd(AYA, "history", rid, route="board")
        self.assertEqual(work.DISPUTED_EXIT, code, out)
        self.assertEqual(4, len(re.findall(r"^  \d+-0 ", out, re.M)), "every entry is still shown")
        self.assertIn("<- NOT ALLOWED: only grok or claude-code-cli may handoff it, and this is gemini", out)

    def test_a_tainted_entry_anywhere_disputes_the_record_first_middle_or_last(self):
        for where in ("last", "not an entry"):
            self.conn = FakeRedis()
            self.worker, self.sims = Worker(self.conn), {}
            rid = self.create(owner="grok")
            if where == "last":
                self.around(rid, "gemini", action="done", state="done", words="evidence=" + LINK)
            else:
                self.conn.xadd(work.LOG % rid, {"text": "r2 written by something else", "by": "x", "at": "x"})
            code, out = self.cmd(ME, "show", rid)
            self.assertEqual(work.DISPUTED_EXIT, code, (where, out))
            self.refused("grok", "accept", rid, "--next", "x", says="is DISPUTED", code=work.DISPUTED_EXIT)

    def test_the_rules_are_the_stores_own(self):
        self.assertIs(work.allowed, redis_gov.work_allowed)
        self.assertIs(work.MOVES, redis_gov.WORK_MOVES)
        source = (ROOT / "scripts" / "redis_work.py").read_text(encoding="utf-8")
        self.assertNotIn("def allowed(", source, "the tool grew its own copy of the rules")
        self.assertEqual(0, source.count("def work_allowed"))

    # ---------------------------------------------------------- PARTIAL FAILURE and TIMEOUT
    def test_PARTIAL_FAILURE_a_move_that_landed_with_an_unknown_answer_is_settled_by_reading(self):
        rid = self.create(owner=ME)
        self.ops()
        lost = BridgeSim(self.conn, principal=ME, account=account(ME), fault=lambda sim, request: sim.execute(
            request, lose="after" if request["op"] == "xadd" else None))
        code, out = self.cmd(ME, "accept", rid, "--next", "x", bridge=lost)
        self.assertEqual(work.UNKNOWN_EXIT, code, out)
        req = re.search(r"if r2 carries req=([0-9a-f]{8})", out)[1]
        self.assertEqual(1, [r["op"] for r in lost.requests].count("xadd"), "an unknown move was sent twice")
        self.assertEqual([], self.worker.asked)
        code, out = self.cmd("grok", "show", rid)
        self.assertIn("req=%s" % req, out, "`show` did not say that the move landed")
        self.assertEqual("accepted", self.state(rid)["state"])
        self.assertEqual(1, self.listed(rid)["rev"], "the list was not written, and nothing pretended it was")
        code, out = self.cmd(ME, "reindex", rid)
        self.assertEqual(0, code, out)
        self.assertEqual((2, "accepted"), (self.listed(rid)["rev"], self.listed(rid)["state"]))

    def test_PARTIAL_FAILURE_a_move_that_landed_but_whose_list_line_did_not_is_said_and_repaired(self):
        rid = self.create(owner="grok")
        no_list = BridgeSim(self.conn, principal="grok", account=account("grok"), fault=lambda sim, request: sim.execute(
            request, lose="before" if request["op"] == "hset" else None))
        code, out = self.cmd("grok", "accept", rid, "--next", "x", bridge=no_list)
        self.assertEqual(3, code, out)
        self.assertIn("MOVED: %s is at r2" % rid, out)
        self.assertIn("THE LIST WAS NOT UPDATED", out)
        self.assertIn("Run: reindex %s" % rid, out)
        self.assertEqual("accepted", self.state(rid)["state"], "the move itself is whole")
        code, out = self.cmd("cursor", "list")
        self.assertIn("requested", out, "the list lags, as said")
        code, out = self.cmd("cursor", "show", rid)                   # the record never depended on the list
        self.assertIn("accepted", out.splitlines()[0])
        self.assertEqual(0, self.cmd("cursor", "reindex", rid)[0], "anyone verified may repair a list line...")
        self.assertEqual("accepted", self.listed(rid)["state"])
        with self.assertRaises(bc.Refused):                           # ...but only to what the log says
            self.sim("cursor").execute({"op": "hset", "key": INDEX, "field": rid, "expect": "cursor",
                                        "val": "9 done cursor - task Wire the reader"})

    def test_TIMEOUT_a_read_by_the_board_that_gets_no_answer_builds_nothing(self):
        rid = self.create(owner="grok")
        self.worker.drop.add("xrange")
        code, out = self.cmd("gemini", "show", rid, route="board")
        self.assertEqual(2, code, out)
        self.assertIn("NO ANSWER YET", out)
        self.worker.drop.clear()
        real_answer = self.worker.answer
        self.worker.answer = lambda r, w, since: dict(real_answer(r, w, since), **{"from": "grok"})
        code, out = self.cmd("gemini", "show", rid, route="board")
        self.assertEqual(3, code, out)
        self.assertIn("not bus-reconciler; nothing here is trusted", out)

    def test_a_record_that_was_not_read_whole_is_never_built_upon(self):
        rid = self.create(owner="grok")
        legacy = Worker(self.conn, legacy=True)                       # a worker from before `after=`: today's
        self.worker = legacy
        code, out = self.cmd("gemini", "show", rid, route="board")
        self.assertEqual(3, code, out)
        self.assertIn("%s NOT READ after 0-0" % rid, out)
        self.assertIn("Nothing is built on a record that was not read whole", out)
        self.worker = Worker(self.conn)

        def a_page_that_is_not_the_page(sim, request):
            if request["op"] == "xrange":
                return {"ok": True, "op": "xrange", "key": request["key"], "after": "5-0", "more": False,
                        "last": "5-0", "value": [], "principal": "grok"}
            return sim.execute(request)

        bad = BridgeSim(self.conn, principal="grok", account=account("grok"), fault=a_page_that_is_not_the_page)
        before = self.snapshot()
        code, out = self.cmd("grok", "accept", rid, "--next", "x", bridge=bad)
        self.assertEqual(3, code, out)
        self.assertEqual(["xrange"], [r["op"] for r in bad.requests], "a move was sent on a log that was not read")
        self.assertEqual(before, self.snapshot())

    def test_a_write_the_service_could_not_finish_may_have_happened_and_is_not_sent_again(self):
        rid = self.create(owner="grok")
        self.ops()
        self.conn.fail_next["xadd"] = RuntimeError("connection reset")          # inside the governor's EXEC
        code, out = self.cmd("grok", "accept", rid, "--next", "x")
        self.assertEqual(work.UNKNOWN_EXIT, code, out)
        self.assertIn("MAY HAVE HAPPENED", out)
        asked = self.ops()
        self.assertEqual((1, 0), (asked.count("xadd"), asked.count("hset")), "it went on, or it tried again")

    # ------------------------------------------------------------- the rules, through the tool
    def test_a_write_naming_you_is_not_you_accepting_it(self):
        rid = self.create(owner="grok")
        self.refused("cursor", "accept", rid, "--next", "x", says="only grok may accept it, and this is cursor")
        self.refused(ME, "accept", rid, "--next", "x", says="only grok may accept it")     # not the requester either
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

    def test_a_reviewer_named_at_create_cannot_be_skipped_and_an_unreviewed_close_says_so(self):
        rid = self.create(owner="grok", extra=("--reviewer", "aya"))
        self.assertEqual(AYA, self.state(rid)["reviewer"])
        self.cmd("grok", "accept", rid, "--next", "x")
        self.refused("grok", "done", rid, "--evidence", LINK, says="it goes to review, and the reviewer closes it")
        self.assertIn("the reviewer cannot be the owner",
                      self.create(owner="grok", extra=("--reviewer", "grok"), expect=2))
        plain = self.create(owner="grok")
        self.cmd("grok", "accept", plain, "--next", "x")
        self.assertEqual(0, self.cmd("grok", "done", plain, "--evidence", LINK)[0])
        self.assertEqual("unreviewed evidence=" + LINK, self.state(plain)["words"])

    def test_blocked_needs_a_blocker_and_cancel_needs_a_reason_from_someone_who_holds_it(self):
        rid = self.create(owner="grok")
        self.cmd("grok", "accept", rid, "--next", "x")
        self.refused("cursor", "block", rid, "--blocker", "x", says="only grok may block it")
        self.assertEqual(0, self.cmd("grok", "block", rid, "--blocker", "waiting on D2")[0])
        self.assertEqual("blocker=waiting on D2", self.state(rid)["words"])
        self.refused("grok", "review", rid, "--reviewer", "aya", says="not from blocked")
        self.assertEqual(0, self.cmd("grok", "unblock", rid)[0])
        self.refused("cursor", "cancel", rid, "--note", "x", says="only grok or claude-code-cli may cancel it")
        self.assertEqual(0, self.cmd(ME, "cancel", rid, "--note", "no longer wanted")[0])   # the requester
        self.assertEqual(("cancelled", "-"), (self.state(rid)["state"], self.state(rid)["lease"]))

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
        self.assertIn("EXPIRED", self.cmd("cursor", "list")[1])
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
        self.assertEqual([], self.ops(), "a refused create asked for something")
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
            made.append(re.search(r"(W-\S+) created", out)[1])
        self.assertEqual(2, code, out)
        self.assertRegex(out, r"not created: with one more record today's list \(\d+\) would no longer fit one board "
                              r"row, and an agent that reads by the board could not list the day")
        self.assertIn("Nothing was written.", out)
        self.assertGreaterEqual(len(made), 5, "a working day's worth still fits")
        self.assertEqual(len(made), len([k for k in self.conn.data if k.startswith(work.NS + "rec:")]),
                         "the refused record left a description behind")
        code, out = self.cmd("gemini", "list", route="board")         # what WAS created is all listable by the board
        self.assertEqual(0, code, out)
        self.assertEqual(sorted(made), [line.split()[0] for line in out.splitlines()[:-1]])
        while len(made) < work.INDEX_MAX:                             # past that, only on an explicit word
            made.append(self.create(owner=ME, title="Past the board's list %02d" % len(made),
                                    extra=("--past-board-list",)))
        self.assertIn("not created: today's list is full (50 records)",
                      self.create(owner=ME, extra=("--past-board-list",), expect=2))
        code, out = self.cmd("gemini", "list", route="board")         # by the board the day no longer fits
        self.assertEqual(4, code, out)
        self.assertIn("%s: NOT LISTED. The day's records do not fit one board row" % DAY, out)
        self.assertNotIn(made[0], out, "part of a day was listed as if it were the day")
        self.assertEqual(0, self.cmd("gemini", "show", made[0], route="board")[0], "one record never depends on the list")
        code, out = self.cmd(ME, "list")
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
            self.assertIn("refused", run(self.conn, ME, "del", key, verified=True), key)
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
        self.assertEqual([], self.ops(), "a command asked for something with the switch off")
        self.assertEqual(before, self.snapshot())
        for argv in (["show", rid], ["list"], ["history", rid]):
            self.assertEqual(0, self.cmd("grok", *argv, switch="")[0], argv)

    def test_preflight_says_which_governor_a_route_meets(self):
        code, out = self.cmd("grok", "preflight", switch="")
        self.assertEqual(2, code)
        self.assertIn("--allow-stray-entry", out)
        self.assertEqual([], self.ops())
        code, out = self.cmd("grok", "preflight", "--allow-stray-entry", switch="")
        self.assertEqual(0, code, out)
        self.assertIn("PASS by the bridge route: the governor admitted this caller as verified and refused an entry "
                      "out of sequence", out)
        code, out = self.cmd("grok", "preflight", "--allow-stray-entry", switch="", route="board")
        self.assertEqual(0, code, out)
        self.assertIn("PASS by the board route: the governor refused a work-record write from a claimed identity", out)
        self.assertEqual({}, self.snapshot(), "a passing preflight wrote something")
        # a governor from before the rules, met by each route: it WRITES the entry, and that is said
        old = mock.patch.multiple(redis_gov, WRITE_ONCE=(), SEQUENCED=(), REV_FORWARD=(), _LEDGER_OP={})
        with old:
            code, out = self.cmd("grok", "preflight", "--allow-stray-entry", switch="", route="board")
        self.assertEqual(1, code, out)
        self.assertIn("FAIL by the board route: THIS GOVERNOR TOOK A WORK-RECORD WRITE IT MUST REFUSE", out)
        self.assertIn("Do not set REDIS_WORK=on", out)
        with old:
            code, out = self.cmd("cursor", "preflight", "--allow-stray-entry", switch="")
        self.assertEqual(1, code, out)
        self.assertIn("FAIL by the bridge route: THIS GOVERNOR TOOK AN ENTRY OUT OF SEQUENCE", out)
        self.assertEqual({work.LOG % "preflight-grok", work.LOG % "preflight-cursor"}, set(self.snapshot()),
                         "and it says where each stray entry is")

    # ------------------------------------------------------------------------- the smaller things
    def test_an_issue_is_a_pointer_and_github_stays_the_authority(self):
        for link in ("", "https://example.com/issues/1", LINK, "https://github.com/sfdc-24/Blackboard/issues/"):
            code, out = self.cmd(ME, "create", "--kind", "issue", "--title", "Q11", "--owner", "grok", *BASE,
                                 "--link", link)
            self.assertEqual(2, code, link)
            self.assertIn("GitHub", out)
        self.assertEqual([], self.ops(), "a refused create asked for something")
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

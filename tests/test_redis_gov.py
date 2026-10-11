#!/usr/bin/env python3
"""Governed Redis access: everyone reads and writes, one controller sets the rules, everything audited.

Run directly: `python tests/test_redis_gov.py`. Offline - a fake Redis that implements WATCH/MULTI/EXEC
the way the real one does, including the part that matters most here: EXEC does NOT roll back.
"""
import base64
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import redis_gov as gov  # noqa: E402


class WatchError(Exception):
    """Named like redis.exceptions.WatchError - the module recognises it by name, not by import."""


class FakeRedis:
    """The calls the module makes, with Redis's real semantics where they matter:
    a key has ONE type; WATCH aborts EXEC if the key changed; EXEC does not roll back."""

    def __init__(self):
        self.data, self.ttls, self.version = {}, {}, {}
        self.fail_next = {}                 # command name -> exception, for one queued command

    # --- type bookkeeping
    def type(self, key):
        v = self.data.get(key)
        if v is None:
            return "none"
        return {str: "string", dict: "hash", list: "stream"}[type(v)]

    def _bump(self, key):
        self.version[key] = self.version.get(key, 0) + 1

    def _need(self, key, kind):
        t = self.type(key)
        if t not in ("none", kind):
            raise RuntimeError("WRONGTYPE Operation against a key holding the wrong kind of value")

    # --- strings
    def get(self, key):
        self._need(key, "string")
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False):
        self._need(key, "string")
        if nx and key in self.data:
            return None                     # redis-py: SET NX on an existing key returns None
        self.data[key] = str(value)
        self._bump(key)
        if ex:
            self.ttls[key] = ex
        return True

    def delete(self, key):
        existed = key in self.data
        self.data.pop(key, None)
        self.ttls.pop(key, None)
        self._bump(key)
        return int(existed)

    # --- hashes
    def hget(self, key, field):
        self._need(key, "hash")
        return (self.data.get(key) or {}).get(field)

    def hset(self, key, field=None, value=None, mapping=None):
        self._need(key, "hash")
        items = dict(mapping or {})
        if field is not None:
            items[field] = value
        for name, val in items.items():
            if not isinstance(val, str):
                raise TypeError("redis-py refuses to encode %r" % type(val).__name__)
            self.data.setdefault(key, {})[name] = val
        self._bump(key)
        return len(items)

    def hgetall(self, key):
        self._need(key, "hash")
        return dict(self.data.get(key) or {})

    def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    # --- streams
    def xadd(self, key, fields, maxlen=None, approximate=None):
        self._need(key, "stream")
        for v in fields.values():
            if not isinstance(v, str):
                raise TypeError("redis-py refuses %r" % type(v).__name__)
        entries = self.data.setdefault(key, [])
        entry_id = "%d-0" % (len(entries) + 1)
        entries.append((entry_id, dict(fields)))
        self._bump(key)
        return entry_id

    def xrevrange(self, key, count=None):
        self._need(key, "stream")
        entries = list(reversed(self.data.get(key) or []))
        return entries[:count] if count else entries

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    # --- helpers for the tests
    def audit(self):
        return [f for _, f in (self.data.get(gov.AUDIT_STREAM) or [])]


class FakePipeline:
    def __init__(self, conn):
        self.conn, self.queue, self.watched, self.buffering = conn, [], {}, False

    def watch(self, key):
        self.watched[key] = self.conn.version.get(key, 0)

    def multi(self):
        self.buffering = True

    def reset(self):
        self.queue, self.watched, self.buffering = [], {}, False

    def __getattr__(self, name):
        method = getattr(self.conn, name)

        def call(*args, **kwargs):
            if not self.buffering:
                return method(*args, **kwargs)           # immediate mode, as after WATCH
            self.queue.append((name, args, kwargs))
            return self
        return call

    def execute(self, raise_on_error=True):
        for key, seen in self.watched.items():
            if self.conn.version.get(key, 0) != seen:
                raise WatchError("Watched variable changed")
        results = []
        for name, args, kwargs in self.queue:            # NO ROLLBACK: each runs on its own
            injected = self.conn.fail_next.pop(name, None)
            try:
                if injected:
                    raise injected
                results.append(getattr(self.conn, name)(*args, **kwargs))
            except Exception as error:
                if raise_on_error:
                    raise
                results.append(error)
        return results


ACL = gov.load_acl()


def run(conn, tag, op, key, field="", val="", enc="", via="REQ-1", acl=None):
    return gov.execute(conn, acl or ACL, tag, via, op, key, field=field, raw_value=val, enc=enc)


def acl_file(tmp, **override):
    data = json.loads(gov.ACL_FILE.read_text(encoding="utf-8"))
    data.update(override)
    path = pathlib.Path(tmp) / "acl.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class EveryoneReadsAndWrites(unittest.TestCase):
    """His words: 'Everyone gets read write access.' Every named agent, both ways."""

    AGENTS = ("chatgpt-codex-desktop", "grok", "cursor", "gemini", "claude-code-cli", "owner",
              "copilot", "claude-api", "pi1-cli", "claude-mobile")

    def test_the_agents_he_named_are_all_principals(self):
        for agent in ("chatgpt-codex-desktop", "grok", "cursor", "gemini"):
            self.assertIn(agent, ACL["principals"], agent)

    def test_every_agent_can_write_and_read_back(self):
        conn = FakeRedis()
        for agent in self.AGENTS:
            key = "fleet:test:%s" % agent
            w = run(conn, agent, "set", key, val="hello from " + agent)
            self.assertTrue(w["ok"], (agent, w))
            r = run(conn, agent, "get", key)
            self.assertTrue(r["ok"], (agent, r))
            self.assertEqual("hello from " + agent, r["value"])

    def test_every_governed_namespace_is_writable(self):
        conn = FakeRedis()
        for key in ("fleet:x", "conf:2026-10-07:scorecard", "proj:demo:ms:1"):
            self.assertTrue(run(conn, "grok", "set", key, val="v")["ok"], key)

    def test_aliases_map_to_one_principal_and_the_raw_tag_is_kept(self):
        conn = FakeRedis()
        for tag in ("codex", "aya", "dot", "codex-desktop"):
            self.assertTrue(run(conn, tag, "set", "fleet:a:%s" % tag, val="1")["ok"], tag)
        actors = {(e["actor"], e["raw_tag"]) for e in conn.audit()}
        self.assertEqual({"chatgpt-codex-desktop"}, {a for a, _ in actors})
        self.assertEqual({"codex", "aya", "dot", "codex-desktop"}, {r for _, r in actors},
                         "the audit must show WHICH tag posted, not just who it maps to")

    def test_whatsapp_is_the_owner(self):
        conn = FakeRedis()
        self.assertTrue(run(conn, "whatsapp", "set", "fleet:from-him", val="go")["ok"])
        self.assertEqual("owner", conn.audit()[-1]["actor"])

    def test_all_seven_ops_work(self):
        conn = FakeRedis()
        self.assertTrue(run(conn, "gemini", "set", "proj:k", val="a")["ok"])
        self.assertTrue(run(conn, "gemini", "get", "proj:k")["ok"])
        self.assertTrue(run(conn, "gemini", "hset", "proj:h", field="status", val="green")["ok"])
        self.assertEqual({"status": "green"}, run(conn, "gemini", "hgetall", "proj:h")["value"])
        self.assertTrue(run(conn, "gemini", "xadd", "proj:log", val="started")["ok"])
        got = run(conn, "gemini", "xrange", "proj:log")
        self.assertEqual("started", got["value"][0]["text"])
        self.assertEqual("gemini", got["value"][0]["by"])
        self.assertTrue(run(conn, "gemini", "del", "proj:k")["ok"])
        self.assertFalse(run(conn, "gemini", "get", "proj:k")["found"])


class NobodyWritesTheProtectedNamespaces(unittest.TestCase):
    """Checked BEFORE the ACL. No edit to the access list can open these."""

    PROTECTED_KEYS = ("v1:bus:row:R-1", "v1:bus:rows", "v1:agent:canon", "v1:conf:room:state",
                      "v1:synth:probe:x", "probe:bridge:x", "gov:audit", "gov:anything")

    def test_no_principal_may_write_them(self):
        conn = FakeRedis()
        for agent in ("owner", "claude-code-cli", "grok", "gemini"):
            for key in self.PROTECTED_KEYS:
                for op in ("set", "del", "xadd"):
                    out = run(conn, agent, op, key, val="x")
                    self.assertFalse(out["ok"], (agent, op, key))
                    self.assertIn("protected", out["refused"])

    def test_not_even_with_an_acl_that_says_write_everything(self):
        """The careless edit: someone sets write to "*". The board mirror still cannot be written -
        because if it could, any agent could make the comparison gate say AGREE about a row that
        differs."""
        with tempfile.TemporaryDirectory() as tmp:
            everything = {"grok": {"read": ["*"], "write": ["*"]}}
            acl = gov.load_acl(acl_file(tmp, principals=everything))
        conn = FakeRedis()
        out = run(conn, "grok", "set", "v1:bus:row:R-1", val="forged", acl=acl)
        self.assertFalse(out["ok"])
        self.assertNotIn("v1:bus:row:R-1", conn.data)
        # ...while the same ACL does open an ordinary key, so the test is about PROTECTED, not luck.
        self.assertTrue(run(conn, "grok", "set", "anything:else", val="ok", acl=acl)["ok"])

    def test_the_audit_stream_cannot_be_edited_through_this_path(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:x", val="1")
        before = len(conn.audit())
        for op in ("del", "xadd", "set"):
            self.assertFalse(run(conn, "owner", op, "gov:audit", val="erase")["ok"])
        self.assertGreater(len(conn.audit()), before, "the attempts are themselves audited")
        self.assertEqual("fleet:x", conn.audit()[0]["key"], "and the history is intact")

    def test_but_everything_is_READABLE_including_the_audit(self):
        """Governance that cannot be inspected is not governance."""
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:x", val="1")
        got = run(conn, "cursor", "xrange", "gov:audit")
        self.assertTrue(got["ok"])
        self.assertEqual("grok", got["value"][0]["actor"])


class OutsideTheGovernedNamespacesIsRefused(unittest.TestCase):
    def test_an_ungoverned_key_is_not_writable(self):
        conn = FakeRedis()
        out = run(conn, "grok", "set", "random:key", val="x")
        self.assertFalse(out["ok"])
        self.assertNotIn("random:key", conn.data)

    def test_an_unknown_sender_is_refused_and_audited(self):
        conn = FakeRedis()
        out = run(conn, "somebody-new", "set", "fleet:x", val="x")
        self.assertFalse(out["ok"])
        self.assertEqual("0", conn.audit()[-1]["ok"])
        self.assertEqual("(unknown)", conn.audit()[-1]["actor"])
        self.assertEqual("somebody-new", conn.audit()[-1]["raw_tag"])

    def test_an_unreadable_acl_permits_NOTHING(self):
        with tempfile.TemporaryDirectory() as tmp:
            acl = gov.load_acl(pathlib.Path(tmp) / "absent.json")
        conn = FakeRedis()
        self.assertFalse(run(conn, "owner", "get", "fleet:x", acl=acl)["ok"])
        self.assertFalse(run(conn, "owner", "set", "fleet:x", val="1", acl=acl)["ok"])

    def test_an_acl_marked_unreadable_permits_nothing_EVEN_IF_it_still_lists_principals(self):
        """The first version of this file had two guards for one property: an unreadable file
        loads with no principals, AND check() refuses an unreadable ACL. A mutation that deleted the
        second guard passed every test, because the first one carried it. This makes the second
        guard load-bearing: a half-loaded ACL that still names principals must grant nothing."""
        half = dict(ACL, readable=False)
        self.assertTrue(half["principals"], "the principals are still there")
        out = run(FakeRedis(), "owner", "set", "fleet:x", val="1", acl=half)
        self.assertFalse(out["ok"])
        self.assertIn("unreadable", out["refused"])


class TheGrammarIsClosed(unittest.TestCase):
    def test_glob_and_space_and_control_characters_are_refused(self):
        conn = FakeRedis()
        for key in ("fleet:*", "fleet:a?", "fleet:[ab]", "fleet:a b", "fleet:a\nb", "", ":fleet",
                    "fleet:" + "x" * 300):
            self.assertFalse(run(conn, "grok", "get", key)["ok"], repr(key))

    def test_an_unknown_op_is_refused(self):
        for op in ("keys", "flushall", "eval", "config", "scan", "rename"):
            self.assertFalse(run(FakeRedis(), "owner", op, "fleet:x")["ok"], op)

    def test_the_value_cap(self):
        conn = FakeRedis()
        self.assertTrue(run(conn, "grok", "set", "fleet:x", val="a" * 2048)["ok"])
        self.assertFalse(run(conn, "grok", "set", "fleet:x", val="a" * 2049)["ok"])

    def test_base64_carries_what_the_board_cannot(self):
        conn = FakeRedis()
        raw = "line one\nline two | with a pipe"
        enc = base64.b64encode(raw.encode()).decode()
        self.assertTrue(run(conn, "grok", "set", "fleet:x", val=enc, enc="b64")["ok"])
        self.assertEqual(raw, conn.data["fleet:x"])
        self.assertFalse(run(conn, "grok", "set", "fleet:x", val="not base64!!", enc="b64")["ok"])

    def test_hset_needs_a_field(self):
        self.assertFalse(run(FakeRedis(), "grok", "hset", "fleet:h", val="v")["ok"])


class EveryActionIsAudited(unittest.TestCase):
    def test_one_audit_entry_per_operation_whatever_the_outcome(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:x", val="1")             # write
        run(conn, "grok", "get", "fleet:x")                      # read
        run(conn, "grok", "set", "v1:bus:row:1", val="x")        # refused: protected
        run(conn, "nobody", "get", "fleet:x")                    # refused: unknown
        self.assertEqual(4, len(conn.audit()))
        self.assertEqual(["1", "1", "0", "0"], [e["ok"] for e in conn.audit()])

    def test_a_write_records_who_what_when_old_and_new(self):
        conn = FakeRedis()
        run(conn, "gemini", "set", "fleet:status", val="amber", via="REQ-A")
        run(conn, "cursor", "set", "fleet:status", val="green", via="REQ-B")
        last = conn.audit()[-1]
        self.assertEqual("cursor", last["actor"])
        self.assertEqual("REQ-B", last["via"])
        self.assertEqual("set", last["op"])
        self.assertEqual("fleet:status", last["key"])
        self.assertEqual("amber", last["old"])
        self.assertEqual("green", last["new"])
        self.assertTrue(last["at"].endswith("Z"))

    def test_every_entry_names_the_acl_it_was_decided_under(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:x", val="1")
        self.assertEqual(ACL["digest"], conn.audit()[-1]["acl"])
        with tempfile.TemporaryDirectory() as tmp:
            other = gov.load_acl(acl_file(tmp, version=2))
        self.assertNotEqual(ACL["digest"], other["digest"], "a changed ACL is a different digest")

    def test_a_read_audit_carries_no_value(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:secretish", val="the value")
        run(conn, "cursor", "get", "fleet:secretish")
        read_entry = conn.audit()[-1]
        self.assertEqual("get", read_entry["op"])
        self.assertEqual("", read_entry["new"])
        self.assertEqual("", read_entry["old"])


class TheWriteAndItsAuditStayTogether(unittest.TestCase):
    """Redis does NOT roll back an EXEC. These are the cases that would otherwise lie."""

    def test_both_land_in_one_exec(self):
        conn = FakeRedis()
        out = run(conn, "grok", "set", "fleet:x", val="1")
        self.assertTrue(out["ok"])
        self.assertEqual("1", conn.data["fleet:x"])
        self.assertEqual("fleet:x", conn.audit()[-1]["key"])

    def test_a_type_mismatch_is_refused_BEFORE_exec_and_nothing_is_written(self):
        conn = FakeRedis()
        run(conn, "grok", "hset", "fleet:h", field="a", val="1")
        out = run(conn, "grok", "set", "fleet:h", val="clobber")
        self.assertFalse(out["ok"])
        self.assertIn("holds a hash", out["refused"])
        self.assertEqual({"a": "1"}, conn.data["fleet:h"], "the hash is untouched")

    def test_a_concurrent_change_aborts_the_whole_exec(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:x", val="1")
        audits_before = len(conn.audit())
        real = conn.pipeline

        def racing_pipeline(transaction=True):
            pipe = real(transaction)
            original_multi = pipe.multi

            def multi():
                conn.set("fleet:x", "someone else got there first")     # between WATCH and EXEC
                original_multi()
            pipe.multi = multi
            return pipe
        conn.pipeline = racing_pipeline
        out = run(conn, "cursor", "set", "fleet:x", val="2")
        self.assertFalse(out["ok"])
        self.assertIn("nothing was written", out["refused"])
        self.assertEqual("someone else got there first", conn.data["fleet:x"])
        self.assertEqual(audits_before + 1, len(conn.audit()), "one entry: the refusal itself")
        self.assertEqual("0", conn.audit()[-1]["ok"])

    def test_if_the_AUDIT_half_ever_fails_the_result_says_so(self):
        """Should be unreachable - gov: is protected - but EXEC doesn't roll back, so if it happens
        the write may have landed unaudited, and the result must say that rather than 'ok'."""
        conn = FakeRedis()
        conn.fail_next["xadd"] = RuntimeError("simulated audit failure")
        out = run(conn, "grok", "set", "fleet:x", val="1")
        self.assertFalse(out["ok"])
        self.assertIn("AUDIT FAILED", out["error"])

    def test_if_the_WRITE_half_fails_the_result_is_not_ok(self):
        conn = FakeRedis()
        conn.fail_next["set"] = RuntimeError("simulated write failure")
        out = run(conn, "grok", "set", "fleet:x", val="1")
        self.assertFalse(out["ok"])
        self.assertEqual("write failed", out["error"])


class TheServerSetsTheTTL(unittest.TestCase):
    def test_longest_prefix_wins(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "conf:2026-10-07:x", val="1")
        run(conn, "grok", "set", "fleet:y", val="1")
        self.assertEqual(ACL["ttl_seconds"]["conf:"], conn.ttls["conf:2026-10-07:x"])
        self.assertEqual(ACL["ttl_seconds"]["fleet:"], conn.ttls["fleet:y"])

    def test_hash_and_stream_writes_get_a_ttl_too(self):
        conn = FakeRedis()
        run(conn, "grok", "hset", "proj:h", field="f", val="v")
        run(conn, "grok", "xadd", "proj:s", val="v")
        self.assertIn("proj:h", conn.ttls)
        self.assertIn("proj:s", conn.ttls)

    def test_the_audit_stream_itself_never_expires(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:x", val="1")
        self.assertNotIn(gov.AUDIT_STREAM, conn.ttls)


class EndToEndThroughTheBoardWorker(unittest.TestCase):
    """A board row in, a Redis change and ONE RESULT row out - through bus_request.handle(), the
    function the bus-requests job actually runs."""

    NOW = None

    def setUp(self):
        import datetime
        import bus_request
        self.br = bus_request
        self.now = datetime.datetime(2026, 10, 8, 2, 0, tzinfo=datetime.timezone.utc)

    def row(self, tag, payload, req="REQ-0001", ts="2026-10-08T01:59:00Z"):
        return [req, ts, tag, "bus-reconciler", "AYA_REQ",
                "BCB|v=1|id=%s|req=%s|%s|from=%s|to=bus-reconciler" % (req, req, payload, tag),
                "", "", "", ""]

    def run_rows(self, rows, conn=None):
        conn = conn if conn is not None else FakeRedis()
        out_rows = []
        out = self.br.handle(rows, conn, now=self.now, append=out_rows.append)
        return out, out_rows, conn

    def test_grok_appends_to_the_public_stream_and_the_result_is_one_row(self):
        out, rows, conn = self.run_rows([self.row(
            "grok", "do=redis-op|op=xadd|key=conf:side:20261011|val=Z3JlZW4=|enc=b64")])
        self.assertEqual("green", conn.data["conf:side:20261011"][0][1]["text"])
        self.assertEqual(1, len(rows), "one RESULT row, no separate receipt")
        self.assertEqual("AYA_RESULT", rows[0]["action_type"])
        self.assertIn("OK xadd conf:side:20261011", rows[0]["payload"])
        self.assertEqual("grok", rows[0]["target_surface"], "answered under its own tag")
        self.assertEqual("OK", out["answered"][0]["status"])
        self.assertEqual("grok", conn.audit()[-1]["actor"])
        self.assertTrue(conn.audit()[-1]["via"].startswith("REQ-0001/"), "the audit names the row")

    def test_each_agent_he_named_reaches_the_public_stream_through_the_board(self):
        rows = [self.row(tag, "do=redis-op|op=xadd|key=conf:side:20261011|val=aGk=|enc=b64", req="REQ-%04d" % n)
                for n, tag in enumerate(("chatgpt-codex-desktop", "grok", "cursor", "gemini",
                                         "codex", "aya"), start=1)]
        out, written, conn = self.run_rows(rows)
        self.assertEqual(["OK"] * 6, [a["status"] for a in out["answered"]])
        self.assertEqual(["hi"] * 6, [e["text"] for _, e in conn.data["conf:side:20261011"]])

    def test_public_stream_accepts_ten_cells_and_blank_sheet_padding(self):
        """The live board reads twelve columns, with empty K:L after the ten request cells."""
        for padding in ((), ("", ""), (" ", None)):
            with self.subTest(padding=padding):
                write = self.row("gemini", "do=redis-op|op=xadd|key=conf:side:20261011|val=Z3JlZW4=|enc=b64",
                                 req="REQ-PAD-W")
                read = self.row("gemini", "do=redis-op|op=xrange|key=conf:side:20261011|count=1",
                                req="REQ-PAD-R")
                write.extend(padding)
                read.extend(padding)
                out, written, conn = self.run_rows([write, read])
                self.assertEqual(["OK", "OK"], [a["status"] for a in out["answered"]])
                self.assertEqual([], out["refused"])
                self.assertEqual("green", conn.data["conf:side:20261011"][0][1]["text"])
                self.assertIn("green", written[1]["payload"])

    def test_the_row_fleet_agent_builds_is_the_row_the_worker_answers(self):
        """The tool and the worker agree, proved on the real row - not a hand-written one.

        Until 2026-10-11 no cloud waker could ask at all: fleet_agent post writes a phase row, and
        parse_request reads only AYA_REQ. fleet_agent request is the tool; this holds the two
        together, with a value that carries a pipe and a newline, which the payload grammar could
        never carry raw."""
        import datetime
        import fleet_agent
        when = self.now - datetime.timedelta(minutes=1)    # a request from the future is refused
        rows, value = [], "Gemini: the lease shape holds | second clause\nsecond line"
        for n, tag in enumerate(("gemini", "grok", "aya"), start=1):
            rid, row = fleet_agent.request_row(tag, "xadd", "conf:side:20261011", val=value, now=when,
                                               rid="REQ-FA-%04d" % n)
            rows.append(row)
        out, written, conn = self.run_rows(rows)
        self.assertEqual(["OK"] * 3, [a["status"] for a in out["answered"]])
        self.assertEqual(["gemini", "grok", "aya"], [r["target_surface"] for r in written],
                         "each answered under the tag it asked with")
        entries = [fields for _id, fields in conn.data["conf:side:20261011"]]
        self.assertEqual(3, len(entries))
        self.assertEqual([value] * 3, [e["text"] for e in entries], "the value survived whole")
        self.assertEqual(["gemini", "grok", "chatgpt-codex-desktop"], [e["by"] for e in entries],
                         "written under the canonical principal (aya is Codex's desktop)")

    def test_every_principal_and_alias_in_the_access_list_is_answered_under_its_own_tag(self):
        """AYA'S ACCEPTANCE TEST, PER AGENT, OFFLINE (Grok on #367, item 5). For EVERY tag the access
        list knows, append a public message, read it back, and reject a protected write before Redis
        is touched. The tool also refuses that write before posting a row."""
        import datetime
        import fleet_agent
        import redis_gov
        acl = redis_gov.load_acl()
        aliases = acl.get("aliases", {})
        # milestones-sync is a service principal whose ACL cannot write conf:, not a fleet agent.
        tags = [tag for tag in list(acl["principals"]) + list(aliases)
                if "conf:" in acl["principals"][aliases.get(tag, tag)]["write"]]
        self.assertGreaterEqual(len(tags), 15, "the list the owner set names many agents")
        when = self.now - datetime.timedelta(minutes=1)
        rows, plan = [], {}
        for n, tag in enumerate(tags, start=1):
            canonical = aliases.get(tag, tag)
            marker = "accepted by %s at %s" % (tag, when.isoformat())
            rows.append(fleet_agent.request_row(tag, "xadd", "conf:side:20261011", val=marker, now=when,
                                                rid="REQ-ACC-%04d-W" % n)[1])
            rows.append(fleet_agent.request_row(tag, "xrange", "conf:side:20261011", count=1,
                                                now=when, rid="REQ-ACC-%04d-R" % n)[1])
            with self.assertRaises(ValueError, msg=tag):           # the tool refuses it before a row exists
                fleet_agent.request_row(tag, "set", "v1:bus:row:%s" % tag, val="forged", now=when,
                                        rid="REQ-ACC-%04d-F" % n)
            rows.append(self.row(tag, "do=redis-op|op=set|key=v1:bus:row:%s|val=Zm9yZ2Vk|enc=b64" % tag,
                                 req="REQ-ACC-%04d-F" % n, ts=when.strftime("%Y-%m-%dT%H:%M:%SZ")))
            plan[tag] = (canonical, marker)
        conn, written = FakeRedis(), []
        # three rows per tag is more than one run's MAX_PER_RUN; the cap is a flood rule, not the subject
        out = self.br.handle(rows, conn, now=self.now, append=written.append, max_per_run=len(rows))
        self.assertEqual(["OK", "OK"] * len(tags), [a["status"] for a in out["answered"]])
        self.assertEqual(len(tags), len(out["refused"]))
        self.assertEqual([t for t in tags for _ in range(3)], [r["target_surface"] for r in written],
                         "each answered under its own tag, refusal included")
        latencies = {}
        for i, tag in enumerate(tags):
            canonical, marker = plan[tag]
            wrote, read, forged = written[3 * i], written[3 * i + 1], written[3 * i + 2]
            self.assertIn("OK xadd conf:side:20261011", wrote["payload"])
            self.assertIn(marker, read["payload"], "the read came back on the board")
            self.assertIn("REFUSED", forged["payload"])
            self.assertNotIn("v1:bus:row:%s" % tag, conn.data)
            mine = [e for e in conn.audit() if e["raw_tag"] == tag]
            self.assertEqual([canonical] * 2, [e["actor"] for e in mine], "audited under the canonical actor")
            self.assertEqual([("xadd", "1"), ("xrange", "1")], [(e["op"], e["ok"]) for e in mine], tag)
            self.assertEqual("conf:side:20261011", mine[0]["key"])
            lat = [a["latency_ms"] for a in out["answered"][2 * i:2 * i + 2]]
            self.assertTrue(all(isinstance(x, int) and x >= 0 for x in lat), lat)
            latencies[tag] = lat
        print("\n  acceptance latency_ms (xadd, xrange) per tag: %s"
              % ", ".join("%s=%s" % (t, "/".join(str(x) for x in l)) for t, l in latencies.items()))

    def test_the_tool_names_what_an_agent_may_ask_for_and_nothing_else(self):
        """The public stream is the only board key; even a claimed own inbox is refused."""
        import fleet_agent
        ok = (("gemini", "xadd", "conf:side:20261011"), ("aya", "xrange", "conf:side:20261011"))
        denied = (("gemini", "xrange", "fleet:inbox:grok"), ("gemini", "get", "fleet:inbox:gemini-x"),
                  ("grok", "set", "gov:worker:heartbeat"), ("grok", "xrange", "gov:audit"),
                  ("grok", "get", "v1:agent:gemini:inbox"), ("aya", "get", "fleet:inbox:aya"),
                   ("grok", "get", "fleet:status"), ("gemini", "xrange", "fleet:inbox:gemini"),
                   ("aya", "xadd", "fleet:inbox:chatgpt-codex-desktop"),
                   ("gemini", "get", "proj:x"), ("grok", "hgetall", "proj:milestones:m3"),
                   ("gemini", "xadd", "conf:side:20261399"), ("gemini", "set", "conf:side:20261011"))
        for tag, op, key in ok:
            with self.subTest(tag=tag, op=op, key=key):
                self.assertEqual("", fleet_agent.key_allowed_for(tag, op, key))
                fleet_agent.request_row(tag, op, key, val="v" if op == "xadd" else None,
                                        rid="REQ-KEY-0001")
        for tag, op, key in denied:
            with self.subTest(tag=tag, op=op, key=key):
                self.assertIn("only the public shared side stream", fleet_agent.key_allowed_for(tag, op, key))
                with self.assertRaises(ValueError):
                    fleet_agent.request_row(tag, op, key, val="v", rid="REQ-KEY-0002")

    def test_a_segment_with_no_field_name_is_ambiguous_on_the_board_and_on_the_git_channel(self):
        """A pipe inside a raw value leaves a nameless segment; the value was cut before the worker
        read it, and an OK for the part before the pipe is an OK for a value nobody asked for (the
        third adversarial check on #367). Refused on every channel, never resolved."""
        out, rows, conn = self.run_rows([self.row("grok", "do=redis-op|op=set|key=fleet:x|val=a|b")])
        self.assertNotIn("fleet:x", conn.data)
        self.assertEqual(1, len(out["refused"]))
        self.assertIn("only the public shared side stream", rows[0]["payload"])
        self.assertNotIn("fleet:x", rows[0]["payload"])
        self.assertEqual(2, self.br.nameless_segments("BCB|v=1|do=redis-op|op=set|key=k|val=a|b|c"))
        self.assertEqual(0, self.br.nameless_segments("BCB|v=1|do=redis-op|op=set|key=k|val=YQ==|enc=b64"))
        import git_requests
        git_row = git_requests.GitRow(self.row(
            "grok", "do=redis-op|op=set|key=conf:side:20261011|val=a|b", req="GH-test-1"))
        git_out, git_written, git_conn = self.run_rows([git_row])
        self.assertEqual(1, len(git_out["refused"]))
        self.assertIn("no field name", git_written[0]["payload"])
        self.assertEqual({}, git_conn.data, "Git ambiguity is refused before a run claim")

    def test_a_stranger_with_the_same_tool_gets_no_row_and_writes_nothing(self):
        import datetime
        import fleet_agent
        when = self.now - datetime.timedelta(minutes=1)    # a request from the future is refused
        _, row = fleet_agent.request_row("stranger", "xadd", "conf:side:20261011", val="v",
                                         now=when, rid="REQ-FA-0009")
        out, written, conn = self.run_rows([row])
        self.assertEqual([], written)
        self.assertNotIn("conf:side:20261011", conn.data)

    def test_a_read_comes_back_in_the_result(self):
        conn = FakeRedis()
        conn.xadd("conf:side:20261011", {"text": "amber", "by": "gemini"})
        out, rows, _ = self.run_rows(
            [self.row("cursor", "do=redis-op|op=xrange|key=conf:side:20261011|count=1")], conn)
        self.assertIn("amber", rows[0]["payload"])

    def test_a_raw_value_is_refused_and_the_row_says_how_to_send_it(self):
        """Grok on #367 (row 2cc985ed): the worker stored a raw value, and a raw value with a pipe had
        already been split on the board, so what it stored was a truncation nobody asked for."""
        out, rows, conn = self.run_rows([self.row("grok", "do=redis-op|op=xadd|key=conf:side:20261011|val=raw text")])
        self.assertNotIn("conf:side:20261011", conn.data)
        self.assertEqual(1, len(out["refused"]), "refused before the governor, like a stale request")
        self.assertEqual(1, len(rows), "a recognised sender is told")
        self.assertIn("REFUSED", rows[0]["payload"])
        self.assertIn("only the public shared side stream", rows[0]["payload"])
        self.assertEqual("grok", rows[0]["target_surface"])

    def test_a_protected_write_is_refused_before_the_claim_and_redis(self):
        out, rows, conn = self.run_rows([self.row(
            "gemini", "do=redis-op|op=set|key=v1:bus:row:R-1|val=Zm9yZ2Vk|enc=b64")])
        self.assertNotIn("v1:bus:row:R-1", conn.data)
        self.assertIn("REFUSED", rows[0]["payload"])
        self.assertEqual([], out["answered"])
        self.assertEqual([], conn.audit())
        self.assertNotIn(self.br.run_marker(self.br.parse_request(self.row(
            "gemini", "do=redis-op|op=set|key=v1:bus:row:R-1|val=Zm9yZ2Vk|enc=b64"))), conn.data)

    def test_spoofed_board_tags_cannot_read_or_write_private_keys(self):
        """A caller can claim Gemini (or any ACL principal); the worker must ignore that claim
        for key authority, including an inbox whose name matches the forged tag."""
        import copy
        conn = FakeRedis()
        conn.set("fleet:inbox:gemini", "private-gemini-secret")
        conn.set("fleet:inbox:grok", "private-grok-secret")
        conn.set("proj:private", "private-project-secret")
        conn.xadd("gov:audit", {"text": "private-audit-secret"})
        before = copy.deepcopy(conn.data)
        cases = (("get", "fleet:inbox:gemini"), ("get", "fleet:inbox:grok"),
                 ("get", "proj:private"), ("xrange", "gov:audit"),
                 ("xadd", "fleet:inbox:grok"), ("xadd", "fleet:inbox:gemini"),
                 ("set", "proj:private"))
        rows = []
        for n, (op, key) in enumerate(cases):
            value = "|val=cHJpdmF0ZS13cml0ZS1zZWNyZXQ=|enc=b64" if op in ("xadd", "set") else ""
            rows.append(self.row("gemini", "do=redis-op|op=%s|key=%s%s" % (op, key, value),
                                 req="REQ-PRIVATE-%02d" % n))
        out, written, _ = self.run_rows(rows, conn)
        self.assertEqual([], out["answered"])
        self.assertEqual(len(cases), len(out["refused"]))
        self.assertEqual(before, conn.data, "no marker, read audit, or operation reached Redis")
        published = repr(written) + repr(out)
        for private in ("fleet:inbox:", "proj:private", "gov:audit", "private-",
                        "cHJpdmF0ZS13cml0ZS1zZWNyZXQ="):
            self.assertNotIn(private, published)
        self.assertTrue(all("only the public shared side stream" in r["payload"] for r in written))

    def test_malformed_and_duplicate_board_fields_do_not_claim_or_leak(self):
        conn = FakeRedis()
        payloads = (
            "do=redis-op|op=xrange|key=conf:side:20261011|key=fleet:inbox:grok",
            "do=redis-op|op=xadd|key=conf:side:20261011|val=c2VjcmV0|enc=b64|val=cHJpdmF0ZQ==",
            "do=redis-op|op=xrange|key=conf:side:20261011|field=private-secret",
            "do=redis-op|op=xrange|key=conf:side:20261011|count=9999999999999999999999999999999",
            "do=redis-op|op=xrange|key=conf:side:20261011|unknown=private-secret",
        )
        rows = [self.row("gemini", payload, req="REQ-MAL-%02d" % n)
                for n, payload in enumerate(payloads)]
        wrong_target = self.row("gemini", "do=redis-op|op=xrange|key=conf:side:20261011",
                                req="REQ-MAL-TARGET")
        wrong_target[3] = "grok"
        rows.append(wrong_target)
        wrong_source = self.row("gemini", "do=redis-op|op=xrange|key=conf:side:20261011",
                                req="REQ-MAL-SOURCE")
        wrong_source[5] = wrong_source[5].replace("from=gemini", "from=grok")
        rows.append(wrong_source)
        extra_cell = self.row("gemini", "do=redis-op|op=xrange|key=conf:side:20261011",
                              req="REQ-MAL-CELLS")
        extra_cell.extend(["", "private-secret"])
        rows.append(extra_cell)
        out, written, _ = self.run_rows(rows, conn)
        self.assertEqual([], out["answered"])
        self.assertEqual(len(rows), len(out["refused"]))
        self.assertEqual({}, conn.data, "rejections are before markers and every Redis operation")
        published = repr(written) + repr(out)
        for private in ("fleet:inbox:grok", "private-secret", "c2VjcmV0", "cHJpdmF0ZQ=="):
            self.assertNotIn(private, published)

    def test_a_STALE_request_from_an_access_list_agent_is_told_why(self):
        """Refused before the op is even considered - and still answered, because the sender is
        someone the access list recognises. The first version of this file had no test for it: a
        mutation that restricted refusal rows to the probe's old SENDERS list passed everything, so
        grok, cursor and gemini would have heard silence for a stale request."""
        out, rows, conn = self.run_rows([self.row(
            "grok", "do=redis-op|op=set|key=fleet:x|val=MQ==|enc=b64", ts="2026-10-08T00:00:00Z")])
        self.assertNotIn("fleet:x", conn.data)
        self.assertEqual(1, len(rows), "a recognised sender is told it was refused")
        self.assertIn("older than", rows[0]["payload"])
        self.assertEqual("grok", rows[0]["target_surface"])

    def test_an_unknown_sender_gets_no_row_and_touches_nothing(self):
        """The flood rule, unchanged: a tag nobody recognises does not make us write a row."""
        out, rows, conn = self.run_rows([self.row(
            "a-stranger", "do=redis-op|op=set|key=fleet:x|val=MQ==|enc=b64")])
        self.assertEqual([], rows)
        self.assertNotIn("fleet:x", conn.data)
        self.assertEqual(1, len(out["refused"]))

    def test_a_replayed_request_id_does_nothing_the_second_time(self):
        first = self.row("grok", "do=redis-op|op=xadd|key=conf:side:20261011|val=MQ==|enc=b64")
        out, rows, conn = self.run_rows([first])
        answered_row = [rows[0]["row_id"], "2026-10-08T01:59:30Z", "bus-reconciler", "grok",
                        "AYA_RESULT", rows[0]["payload"], "", "", "", ""]
        out2, rows2, _ = self.run_rows([first, answered_row], conn)
        self.assertEqual([], rows2, "already answered: no second write, no second row")
        self.assertEqual(1, len([e for e in conn.audit() if e["op"] == "xadd"]))

    def test_the_probe_still_works_exactly_as_before(self):
        out, rows, conn = self.run_rows([self.row("aya", "do=redis-synthetic-probe")])
        self.assertEqual(["AYA_RECEIPT", "AYA_RESULT"], [r["action_type"] for r in rows],
                         "the probe keeps its receipt-then-result shape")

    def test_every_answer_carries_every_key_the_ENTRYPOINT_reads(self):
        """THE BUG THE FIRST LIVE RUN FOUND. bus-requests-hl95p, 2026-10-08 02:25Z: all four test ops
        ran correctly and all four results landed - then cloud/bus-reconciler/serve_requests.py logged
        item["latency_ms"] for each answer, the redis-op answers had no such key, and the job exited 1.
        Every test here drove handle() and none drove what consumes its output.

        So the keys are READ FROM THE ENTRYPOINT'S SOURCE, not listed here: if serve_requests.py starts
        reading a new key, this test asks for it too, rather than passing on a stale copy of the list."""
        import re
        source = (ROOT / "cloud" / "bus-reconciler" / "serve_requests.py").read_text(encoding="utf-8")
        # Each `for item in out["<name>"]:` loop BODY - the lines indented deeper than the for - and
        # nothing after it. (The first version sliced to the next blank line, which swept the refused
        # loop's item["why"] into the answered keys and demanded a key no answer should carry.)
        lines = source.splitlines()
        loops = {}
        for name in ("answered", "refused"):
            head = next(n for n, line in enumerate(lines)
                        if line.strip() == 'for item in out["%s"]:' % name)
            indent = len(lines[head]) - len(lines[head].lstrip())
            body = []
            for line in lines[head + 1:]:
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                body.append(line)
            loops[name] = set(re.findall(r'item\["([a-z_]+)"\]', "\n".join(body)))
        self.assertEqual({"req_id", "status", "latency_ms"}, loops["answered"],
                         "if this changes, the entrypoint changed - read it, then update this line")
        self.assertIn("latency_ms", loops["answered"], "the entrypoint reads latency_ms - keep it")
        conn = FakeRedis()
        out, _, _ = self.run_rows([
            self.row("grok", "do=redis-op|op=xadd|key=conf:side:20261011|val=MQ==|enc=b64", req="REQ-0001"),
            self.row("cursor", "do=redis-op|op=set|key=v1:bus:row:1|val=eA==|enc=b64", req="REQ-0002"),
            self.row("aya", "do=redis-synthetic-probe", req="REQ-0003"),
            self.row("a-stranger", "do=redis-op|op=xrange|key=conf:side:20261011", req="REQ-0004"),
        ], conn)
        self.assertEqual(2, len(out["answered"]))
        for item in out["answered"]:
            missing = loops["answered"] - set(item)
            self.assertEqual(set(), missing, "answer %s lacks %s" % (item.get("req_id"), missing))
        for item in out["refused"]:
            missing = loops["refused"] - set(item)
            self.assertEqual(set(), missing, "refusal %s lacks %s" % (item.get("req_id"), missing))

    def test_redis_op_is_listed_TOGETHER_with_its_branch(self):
        """Listed without the branch, a redis-op would fall into the probe path and say OK for
        nothing done. Both halves, or neither."""
        import inspect
        self.assertIn("redis-op", self.br.ACTIONS)
        self.assertIn('if req["action"] == "redis-op":', inspect.getsource(self.br.handle))


class NothingLeaks(unittest.TestCase):
    def test_a_store_error_reports_the_type_not_the_message(self):
        class Broken(FakeRedis):
            def pipeline(self, transaction=True):
                raise ConnectionError("AUTH hunter2 refused at 10.0.0.1")
        out = run(Broken(), "grok", "set", "fleet:x", val="1")
        self.assertFalse(out["ok"])
        self.assertNotIn("hunter2", repr(out))
        self.assertNotIn("10.0.0.1", repr(out))

    def test_the_board_summary_is_one_pipe_free_line(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:x", val="a|b")
        for out in (run(conn, "grok", "get", "fleet:x"), run(conn, "grok", "set", "v1:bus:1", val="x"),
                    run(conn, "grok", "set", "fleet:y", val="z")):
            line = gov.summarise(out)
            self.assertNotIn("|", line)
            self.assertNotIn("\n", line)


class FilteredReadCompleteness(unittest.TestCase):
    """A match result must cover every occurrence of an exact row id before any append."""

    def setUp(self):
        import fleet_agent
        self.fa = fleet_agent
        self.real = fleet_agent._bus_fetch, fleet_agent._bus_load_env
        fleet_agent._bus_load_env = lambda: {"BUS_URL": "https://bus.test/x", "BUS_SECRET": "s"}
        self.rid = "REQ-HIDDEN-1"
        self.row = fleet_agent.request_row("gemini", "xadd", "conf:side:20261011", val="one",
                                            rid=self.rid)[1]
        self.quotes = [["QUOTE-%04d" % n, self.row[1], "grok", "ALL", "OPEN",
                        "a later row quotes " + self.rid, "", "", "", ""] for n in range(200)]

    def tearDown(self):
        self.fa._bus_fetch, self.fa._bus_load_env = self.real

    def filtered_fetch(self, rows, appends):
        def fetch(url, payload, tries=1):
            if payload["action"] == "append":
                appends.append(payload["sheetRow"])
                return "ok:true"
            self.assertEqual(self.rid, payload["match"])
            self.assertNotIn("limit", payload, "a cap hides exact rows behind quotations")
            return json.dumps({"ok": True, "title": self.fa.BOARD, "rows": rows,
                               "filtered": len(rows), "total": len(rows) + 1})
        return fetch

    def test_exact_row_behind_two_hundred_quotations_is_not_reappended(self):
        appends = []
        self.fa._bus_fetch = self.filtered_fetch([self.row] + self.quotes, appends)
        code = self.fa.append_and_verify(self.rid, self.row)
        self.assertEqual((self.fa.EXIT_VERIFIED, []), (code, appends))

    def test_complete_empty_filter_appends_once_and_verifies_readback(self):
        rows, appends = [], []
        def fetch(url, payload, tries=1):
            if payload["action"] == "append":
                appends.append(payload["sheetRow"])
                rows.append(payload["sheetRow"])
                return "ok:true"
            return json.dumps({"ok": True, "title": self.fa.BOARD, "rows": rows,
                               "filtered": len(rows), "total": len(rows) + 1})
        self.fa._bus_fetch = fetch
        code = self.fa.append_and_verify(self.rid, self.row)
        self.assertEqual((self.fa.EXIT_VERIFIED, [self.row]), (code, appends))

    def test_hidden_duplicate_is_unconfirmed_and_not_reappended(self):
        appends = []
        self.fa._bus_fetch = self.filtered_fetch([self.row, self.row] + self.quotes, appends)
        code = self.fa.append_and_verify(self.rid, self.row)
        self.assertEqual((self.fa.EXIT_UNCONFIRMED, []), (code, appends))

    def test_endpoint_ignoring_match_and_honoring_limit_is_not_an_empty_board(self):
        appends = []
        def fetch(url, payload, tries=1):
            if payload["action"] == "append":
                appends.append(payload["sheetRow"])
                return "ok:true"
            # The self-hosted read accepts limit, ignores match and does not return the filtered
            # endpoint's metadata. It may return only a tail with no exact row in sight.
            return json.dumps({"ok": True, "title": self.fa.BOARD, "kind": "sheet",
                               "rows": [list(FleetAgentRequestOnTheBoard.HEAD)] + self.quotes,
                               "total_rows": 202})
        self.fa._bus_fetch = fetch
        code = self.fa.append_and_verify(self.rid, self.row)
        self.assertEqual((self.fa.EXIT_NOT_SENT, []), (code, appends))

    def test_two_hundred_apparent_matches_without_completeness_proof_fail_closed(self):
        appends = []
        def fetch(url, payload, tries=1):
            if payload["action"] == "append":
                appends.append(payload["sheetRow"])
                return "ok:true"
            # A capped endpoint can label a 200-row tail as filtered even while an earlier exact
            # row is hidden. The shape alone must not authorize another append.
            return json.dumps({"ok": True, "title": self.fa.BOARD, "rows": self.quotes,
                               "filtered": 200, "total": 202})
        self.fa._bus_fetch = fetch
        code = self.fa.append_and_verify(self.rid, self.row)
        self.assertEqual((self.fa.EXIT_NOT_SENT, []), (code, appends))


class FleetAgentRequestOnTheBoard(unittest.TestCase):
    """The tool's read-first, append-once, read-back-exactly-one, against a fake board, for what Grok
    found on #367 (items 2 and 3): the read-back matches id, Source_Tag, Action_Type AND payload, and
    exactly one row; an id already used by a different request is refused before anything is
    appended; the caller names the id; every outcome has its own exit code and UNCONFIRMED is a word
    the output says."""

    HEAD = ["Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type", "Payload",
            "Category", "Project_Tag", "Gist", "Notes"]

    def setUp(self):
        import fleet_agent
        self.fa = fleet_agent
        self.board = {"rows": [list(self.HEAD)]}
        self.appends = []
        self.reads = []
        self.real = (fleet_agent._matching_rows, fleet_agent._bus_fetch, fleet_agent._bus_load_env)
        fleet_agent._bus_load_env = lambda: {"BUS_URL": "https://bus.test/x", "BUS_SECRET": "s"}

        def matching(rid, tries=1):
            # The bus's filtered read: a case-insensitive substring over the whole row, data rows only.
            self.reads.append((rid, tries))
            if self.board is None:
                return None
            return [list(r) for r in self.board["rows"][1:]
                    if rid.lower() in "|".join(str(c) for c in r).lower()]
        fleet_agent._matching_rows = matching

        def fetch(url, payload, tries=1):
            self.appends.append(payload["sheetRow"])
            self.board["rows"].append(list(payload["sheetRow"]))
            return "ok:true"
        fleet_agent._bus_fetch = fetch

    def tearDown(self):
        self.fa._matching_rows, self.fa._bus_fetch, self.fa._bus_load_env = self.real

    def args(self, rid="REQ-T-0001", val="one"):
        import argparse
        return argparse.Namespace(tag="gemini", op="xadd", key="conf:side:20261011", val=val, field=None,
                                  count=None, project="FLEET", id=rid)

    def run_request(self, args):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.fa.cmd_request(args)
        return code, out.getvalue()

    def post_row(self, rid="GEMINI-WAKE-1"):
        return [rid, "2026-10-11T00:00:00.000000Z", "gemini", "grok;ALL", "DONE",
                "BCB|v=1|id=%s|phase=DONE|text=first answer" % rid,
                "OPEN", "FLEET", "first answer", ""]

    def run_post_row(self, row):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.fa.append_and_verify(row[0], row, exact=False)
        return code, out.getvalue()

    def test_a_new_request_is_appended_once_and_read_back_as_exactly_one_row(self):
        code, out = self.run_request(self.args())
        self.assertEqual((self.fa.EXIT_VERIFIED, 1), (code, len(self.appends)))
        self.assertIn("VERIFIED on the board: Row_ID=REQ-T-0001", out)

    def test_the_identical_request_again_is_not_appended_and_still_reads_back(self):
        self.run_request(self.args())
        code, out = self.run_request(self.args())
        self.assertEqual((self.fa.EXIT_VERIFIED, 1), (code, len(self.appends)), "a retry appends nothing")
        self.assertIn("already on the board", out)
        self.assertIn("VERIFIED on the board", out)

    def test_the_same_id_with_a_different_request_is_refused_before_anything_is_appended(self):
        self.run_request(self.args(val="one"))
        code, out = self.run_request(self.args(val="two"))
        self.assertEqual((self.fa.EXIT_REFUSED, 1), (code, len(self.appends)))
        self.assertIn("refused before posting: request id REQ-T-0001 is already used", out)
        self.assertNotIn("VERIFIED", out)

    def test_a_board_that_cannot_be_read_means_NOT_SENT_not_a_blind_append(self):
        self.board = None
        code, out = self.run_request(self.args())
        self.assertEqual((self.fa.EXIT_NOT_SENT, []), (code, self.appends))
        self.assertIn("NOT SENT", out)

    def test_an_append_that_lands_nothing_is_UNCONFIRMED_with_its_own_exit_code(self):
        # Grok on #367 (item 3): this line said "silent no-op" and shared exit 1 with everything else.
        self.fa._bus_fetch = lambda url, payload, tries=1: "ok:true"       # lands nothing
        code, out = self.run_request(self.args())
        self.assertEqual(self.fa.EXIT_UNCONFIRMED, code)
        self.assertIn("UNCONFIRMED", out)
        self.assertNotIn("silent no-op", out)
        self.assertNotIn("VERIFIED", out)

    def test_two_identical_rows_after_the_append_stay_unconfirmed(self):
        def fetch_twice(url, payload, tries=1):
            self.appends.append(payload["sheetRow"])
            self.board["rows"].append(list(payload["sheetRow"]))
            self.board["rows"].append(list(payload["sheetRow"]))
            return "ok:true"
        self.fa._bus_fetch = fetch_twice
        code, out = self.run_request(self.args())
        self.assertEqual(self.fa.EXIT_UNCONFIRMED, code)
        self.assertIn("2 rows", out)
        code, out = self.run_request(self.args())            # the retry finds them and appends nothing
        self.assertEqual((self.fa.EXIT_UNCONFIRMED, 1), (code, len(self.appends)))
        self.assertIn("2 times already", out)

    def test_a_row_from_another_tag_or_of_another_type_is_not_this_row(self):
        # The global id is occupied; a tag or phase clash cannot verify a new request.
        rid, row = self.fa.request_row("gemini", "xadd", "conf:side:20261011", val="one", rid="REQ-T-0005")
        other = list(row)
        other[self.fa.SOURCE_COL] = "grok"
        self.board["rows"].append(other)
        code, out = self.run_request(self.args(rid="REQ-T-0005"))
        self.assertEqual((self.fa.EXIT_REFUSED, 0), (code, len(self.appends)))
        self.assertNotIn("VERIFIED", out)
        as_phase = list(row)
        as_phase[self.fa.ACTION_COL] = "DONE"
        self.board["rows"] = [list(self.HEAD), as_phase]
        self.appends.clear()
        code, out = self.run_request(self.args(rid="REQ-T-0005"))
        self.assertEqual((self.fa.EXIT_REFUSED, 0), (code, len(self.appends)))
        self.assertNotIn("VERIFIED", out)

    def test_a_reply_retried_with_other_text_is_refused_not_verified_or_reposted(self):
        import contextlib, io
        row = self.post_row()
        self.board["rows"].append(list(row))
        again = list(row)
        again[self.fa.PAYLOAD_COL] = "BCB|v=1|id=GEMINI-WAKE-1|phase=DONE|text=second answer"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.fa.append_and_verify("GEMINI-WAKE-1", again, exact=False)
        self.assertEqual((self.fa.EXIT_REFUSED, []), (code, self.appends))
        self.assertNotIn("VERIFIED", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.fa.append_and_verify("GEMINI-WAKE-1", again, exact=True)
        self.assertEqual(self.fa.EXIT_REFUSED, code, "a request under a used id is refused")

    def test_plain_post_duplicate_identical_preflight_rows_are_not_verified(self):
        row = self.post_row()
        self.board["rows"].extend((list(row), list(row)))
        code, out = self.run_post_row(row)
        # Something was sent, twice, earlier: unconfirmed as ONE post, never "refused" (nothing sent).
        self.assertEqual((self.fa.EXIT_UNCONFIRMED, []), (code, self.appends))
        self.assertIn("2 times already", out)
        self.assertNotIn("VERIFIED", out)

    def test_plain_post_global_id_clashes_are_refused_before_append(self):
        row = self.post_row()
        for index, value in ((2, "grok"), (4, "RESULT"), (3, "cursor;ALL")):
            with self.subTest(index=index):
                other = list(row)
                other[index] = value
                self.board["rows"] = [list(self.HEAD), other]
                self.appends.clear()
                code, out = self.run_post_row(row)
                self.assertEqual((self.fa.EXIT_REFUSED, []), (code, self.appends))
                self.assertNotIn("VERIFIED", out)

    def test_plain_post_exact_plus_conflicting_same_id_after_append_is_unknown(self):
        row = self.post_row()
        def fetch_with_clash(url, payload, tries=1):
            self.appends.append(payload["sheetRow"])
            self.board["rows"].append(list(payload["sheetRow"]))
            other = list(payload["sheetRow"])
            other[2] = "grok"
            self.board["rows"].append(other)
            return "ok:true"
        self.fa._bus_fetch = fetch_with_clash
        code, out = self.run_post_row(row)
        self.assertEqual((self.fa.EXIT_UNCONFIRMED, 1), (code, len(self.appends)))
        self.assertIn("UNCONFIRMED", out)
        self.assertNotIn("VERIFIED", out)

    def test_plain_post_checks_complete_semantic_row_and_tolerates_timestamp_format(self):
        row = self.post_row()
        code, out = self.run_post_row(row)
        self.assertEqual((self.fa.EXIT_VERIFIED, 1), (code, len(self.appends)))
        self.assertIn("VERIFIED", out)
        self.board["rows"][-1][1] = "10/11/2026 00:00"
        code, out = self.run_post_row(row)
        self.assertEqual((self.fa.EXIT_VERIFIED, 1), (code, len(self.appends)))
        # A row the sheet returns one cell short (a trimmed blank tenth cell) is this row, padded;
        # a row short of a NON-blank cell is not.
        self.board["rows"] = [list(self.HEAD), row[:-1]]
        self.appends.clear()
        code, out = self.run_post_row(row)
        self.assertEqual((self.fa.EXIT_VERIFIED, []), (code, self.appends))
        self.board["rows"] = [list(self.HEAD), row[:-2]]
        code, out = self.run_post_row(row)
        self.assertEqual((self.fa.EXIT_REFUSED, []), (code, self.appends))
        self.assertNotIn("VERIFIED", out)

    def test_plain_post_other_recipient_in_readback_is_unconfirmed(self):
        row = self.post_row()
        def fetch_wrong_recipient(url, payload, tries=1):
            self.appends.append(payload["sheetRow"])
            other = list(payload["sheetRow"])
            other[3] = "cursor;ALL"
            self.board["rows"].append(other)
            return "ok:true"
        self.fa._bus_fetch = fetch_wrong_recipient
        code, out = self.run_post_row(row)
        self.assertEqual((self.fa.EXIT_UNCONFIRMED, 1), (code, len(self.appends)))
        self.assertNotIn("VERIFIED", out)

    def test_the_live_twelve_wide_board_with_blank_padding_verifies_like_a_ten_wide_one(self):
        """THE BOARD IS A:L TODAY, K AND L BLANK (docs/COMMS-PROTOCOL.md rule 5): getDataRange() returns
        every row, the header included, at the sheet's full width. A pin on exactly ten cells made
        every post NOT SENT on that board (d53079b); the fourth adversarial check on #367 found it."""
        self.board = {"rows": [list(self.HEAD) + ["", ""]]}

        def fetch_wide(url, payload, tries=1):
            self.appends.append(payload["sheetRow"])
            self.board["rows"].append(list(payload["sheetRow"]) + ["", ""])    # the sheet pads K:L
            return "ok:true"
        self.fa._bus_fetch = fetch_wide
        code, out = self.run_request(self.args())
        self.assertEqual((self.fa.EXIT_VERIFIED, 1), (code, len(self.appends)))
        self.assertIn("VERIFIED on the board: Row_ID=REQ-T-0001", out)
        code, out = self.run_request(self.args())                 # the retry finds the padded row
        self.assertEqual((self.fa.EXIT_VERIFIED, 1), (code, len(self.appends)))
        self.assertIn("already on the board", out)
        # The post path too (every waker reply): a twelve-wide board is not NOT SENT.
        import contextlib, io
        row = ["GEMINI-WAKE-2", "2026-10-11T00:00:00.000000Z", "gemini", "grok;ALL", "DONE",
               "BCB|v=1|id=GEMINI-WAKE-2|phase=DONE|text=an answer", "", "FLEET", "g", ""]
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.fa.append_and_verify("GEMINI-WAKE-2", row, exact=False)
        self.assertEqual(self.fa.EXIT_VERIFIED, code)

    def test_a_non_blank_cell_past_the_tenth_is_not_this_row_and_an_eleventh_named_column_is_NOT_SENT(self):
        rid, row = self.fa.request_row("gemini", "xadd", "conf:side:20261011", val="one",
                                       rid="REQ-T-0007")
        self.board["rows"].append(list(row) + ["", "stray"])            # same id, something in L
        code, out = self.run_request(self.args(rid="REQ-T-0007"))
        self.assertEqual((self.fa.EXIT_REFUSED, []), (code, self.appends), "a row with a non-blank L is not this row")

    def test_the_reads_are_filtered_one_try_before_the_append_and_the_marker_precedes_it(self):
        """The third adversarial check on #367: the unfiltered read of a megabytes board, four
        attempts, ran BEFORE the append inside the waker's budget. Now: a filtered read by id, one try
        before the append (a flapping gateway is told NOT SENT at once), two for the read-back, and
        an "appending" marker flushed before the one append, so a caller that stops the process can
        tell nothing-sent from may-have-landed."""
        code, out = self.run_request(self.args())
        self.assertEqual(self.fa.EXIT_VERIFIED, code)
        self.assertEqual([("REQ-T-0001", 1), ("REQ-T-0001", 2)], self.reads)
        self.assertLess(out.index("appending Row_ID=REQ-T-0001"), out.index("VERIFIED on the board"))
        self.board = None
        self.appends.clear()
        code, out = self.run_request(self.args(rid="REQ-T-0009"))
        self.assertEqual((self.fa.EXIT_NOT_SENT, []), (code, self.appends))
        self.assertIn("could not be read (filtered read, 1 try)", out)
        self.assertNotIn("appending", out, "no marker: nothing was appended")

    def test_a_post_that_names_no_id_gets_one_that_differs_per_post_and_repeats_per_retry(self):
        import datetime
        when = datetime.datetime(2026, 10, 11, 6, 30, 5, tzinfo=datetime.timezone.utc)
        a = self.fa.default_post_id("CCC", "claude-code-cli", "DONE", "first", when)
        b = self.fa.default_post_id("CCC", "claude-code-cli", "DONE", "second", when)
        self.assertNotEqual(a, b, "two posts in one second differ")
        self.assertEqual(a, self.fa.default_post_id("CCC", "claude-code-cli", "DONE", "first", when), "a retry repeats")
        self.assertRegex(a, r"^CCC-20261011T063005Z-[0-9a-f]{6}$")

    def test_free_text_cells_are_compared_with_spaces_collapsed_and_the_rest_byte_exact(self):
        row = self.post_row()
        self.board["rows"].append(list(row))
        self.board["rows"][-1][8] = "first  answer"               # the sheet collapsed a run of spaces
        code, out = self.run_post_row(row)
        self.assertEqual(self.fa.EXIT_VERIFIED, code)
        self.board["rows"][-1][6] = "OPEN "                      # Category is not free text
        code, out = self.run_post_row(row)
        self.assertEqual(self.fa.EXIT_REFUSED, code)

    def test_the_caller_names_the_id_the_tool_does_not_make_one_up(self):
        with self.assertRaises(ValueError) as caught:
            self.fa.request_row("gemini", "xadd", "conf:side:20261011", val="v")
        self.assertIn("request id is required", str(caught.exception))
        with self.assertRaises(SystemExit):
            self.fa.build_parser().parse_args(["request", "xrange", "conf:side:20261011", "--tag", "gemini"])
        ns = self.fa.build_parser().parse_args(["request", "xrange", "conf:side:20261011", "--tag", "gemini", "--id", "R-0001"])
        self.assertEqual("R-0001", ns.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)

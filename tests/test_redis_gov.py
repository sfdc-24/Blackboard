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
        else:
            self.ttls.pop(key, None)        # Redis: SET without KEEPTTL discards the key's expiry
        return True

    def incr(self, key):
        self._need(key, "string")
        value = int(self.data.get(key) or 0) + 1        # Redis: a missing key counts from nought
        self.data[key] = str(value)
        self._bump(key)
        return value

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

    def hlen(self, key):
        self._need(key, "hash")
        return len(self.data.get(key) or {})

    def hkeys(self, key):
        self._need(key, "hash")
        return list((self.data.get(key) or {}).keys())

    def hstrlen(self, key, field):
        self._need(key, "hash")
        raw = (self.data.get(key) or {}).get(field)
        if raw is None:
            return 0
        if isinstance(raw, bytes):
            return len(raw)
        return len(str(raw).encode("utf-8"))

    def keys(self, pattern="*"):
        import fnmatch
        return [key for key in list(self.data) if fnmatch.fnmatch(key, pattern)]

    def eval(self, script, numkeys, *args):
        """Unused by haudit. The verifier does not call EVAL or walk the keyspace.

        A call still stops at the supplied cap so an accidental listing cannot grow without bound."""
        import fnmatch
        cap = int(args[0])
        pattern = args[1].decode("utf-8") if isinstance(args[1], bytes) else str(args[1])
        found, truncated = [], 0
        for key in list(self.data):
            text = key.decode("utf-8") if isinstance(key, bytes) else key
            if not fnmatch.fnmatch(text, pattern):
                continue
            if len(found) >= cap:
                truncated = 1
                break
            found.append(key)
        return [truncated, found]

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

    def xrange(self, key, min="-", max="+", count=None):
        """Redis XRANGE, oldest first, with the exclusive "(" bound of Redis 6.2+ on min."""
        self._need(key, "stream")
        order = lambda entry_id: tuple(int(p) for p in entry_id.split("-"))   # noqa: E731
        if min == "-":
            keep = lambda i: True                                             # noqa: E731
        elif min.startswith("("):
            keep = lambda i: order(i) > order(min[1:])                       # noqa: E731
        else:
            keep = lambda i: order(i) >= order(min)                          # noqa: E731
        assert max == "+", "the governed read never bounds the top"
        entries = [(i, f) for i, f in (self.data.get(key) or []) if keep(i)]
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

    def test_the_chairs_call_record_cannot_be_forged_through_this_path(self):
        """Codex HOLD CODEX-RESULT-RV478-20261109T1837Z: the mirror writes a CALL_TURN with via=binder as a RESULT, so a
        governed xadd of a forged via=binder turn would silently suppress that agent's wake. Only the chair writes the
        call record, with its own connection; nobody writes it through redis_gov, even with an ACL that says '*'."""
        room = "conf-0123456789abcdef-89abcdef"
        keys = ("conf:callturns:" + room, "conf:call:" + room, "conf:calls", "conf:callcode:SFDC-2026-11-09-1830",
                "conf:spoken:%s:ep_1:bkev_x" % room)
        conn = FakeRedis()
        with tempfile.TemporaryDirectory() as tmp:
            everything = gov.load_acl(acl_file(tmp, principals={"grok": {"read": ["*"], "write": ["*"]}}))
        attempts = [(agent, None) for agent in ("owner", "claude-code-cli", "grok", "gemini")] + [("grok", everything)]
        for agent, acl in attempts:
            for key in keys:
                for op in ("set", "del", "xadd"):
                    kw = {"acl": acl} if acl is not None else {}
                    out = run(conn, agent, op, key, val="via=binder", **kw)
                    self.assertFalse(out["ok"], (agent, op, key))
                    self.assertIn("protected", out["refused"])
        self.assertEqual([], [k for k in conn.data if str(k).startswith("conf:call")])
        # ...while an ordinary conf: key stays as the ACL says, so the test is about PROTECTED, not luck.
        self.assertTrue(run(conn, "grok", "set", "conf:note:x", val="ok", acl=everything)["ok"])

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


class WhoACallerSaysItActsFor(unittest.TestCase):
    """redis_gov.said: two CLAIMS recorded beside the verified actor, for the conference chair, which writes
    under its own identity for a persona that has none (conference docs/PERSONA-WRITE-CONFIRMATION-A1.md)."""

    KEY, RID = "conf:persona:20261014:claude", "CLAUDE-TOOL-set-20261014T160000Z-a1b2c3d4"

    def chair(self, conn, op, key, val="", author="claude", rid=None):
        return gov.execute(conn, ACL, "conference-chair", "bridge:conference-chair@x", op, key, raw_value=val,
                           claimed_author=author, request_id=self.RID if rid is None else rid)

    def test_absent_an_entry_is_exactly_what_it_always_was(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:x", val="1")
        run(conn, "grok", "get", "fleet:x")
        run(conn, "grok", "set", "gov:x", val="1")
        for entry in conn.audit():
            self.assertEqual({"at", "actor", "raw_tag", "via", "op", "key", "field", "ok", "reason", "old_sha", "old_len",
                              "old", "new_sha", "new_len", "new", "acl"}, set(entry))

    def test_a_write_a_read_and_a_refusal_each_record_both_beside_the_verified_actor(self):
        conn = FakeRedis()
        self.assertTrue(self.chair(conn, "set", self.KEY, "note")["ok"])
        self.assertTrue(self.chair(conn, "get", self.KEY)["ok"])
        self.assertIn("refused", self.chair(conn, "set", "fleet:x", "v"))
        self.assertEqual(["1", "1", "0"], [e["ok"] for e in conn.audit()])
        for entry in conn.audit():
            self.assertEqual(("conference-chair", "claude", self.RID, "bridge:conference-chair@x"),
                             (entry["actor"], entry["claimed_author"], entry["request_id"], entry["via"]))
            self.assertTrue(all(isinstance(v, str) for v in entry.values()), "every value a string")
        self.assertEqual("note", conn.get(self.KEY))

    def test_a_claim_is_never_an_identity(self):
        """Saying it acts for the owner gives the chair nothing the chair does not have."""
        conn = FakeRedis()
        result = self.chair(conn, "set", "fleet:inbox:grok", "v", author="owner")
        self.assertEqual("conference-chair may not write under that key", result["refused"])
        self.assertEqual(("conference-chair", "owner", "0"), tuple(conn.audit()[-1][k] for k in ("actor", "claimed_author", "ok")))
        self.assertIsNone(conn.get("fleet:inbox:grok"))

    def test_a_claim_of_the_wrong_shape_is_refused_before_anything_runs_and_is_not_recorded(self):
        for author, rid in (("Claude", None), ("c", None), ("x" * 33, None), ("cl aude", None), ("claude\n", None),
                            ("1claude", None), (7, None), (["claude"], None), ("claude", "id with space"),
                            ("claude", "x" * 121), ("claude", "id\n"), ("claude", "-id"), ("claude", 7)):
            conn = FakeRedis()
            result = self.chair(conn, "set", self.KEY, "note", author=author, rid=rid)
            self.assertIn("refused", result, (author, rid))
            self.assertIsNone(conn.get(self.KEY), (author, rid))
            [entry] = conn.audit()
            self.assertEqual("0", entry["ok"])
            self.assertNotIn("claimed_author", entry, "a refused claim is not written into the audit")
            self.assertNotIn("request_id", entry)

    def test_one_of_the_two_alone_is_recorded_alone(self):
        conn = FakeRedis()
        gov.execute(conn, ACL, "grok", "REQ-1", "set", "fleet:x", raw_value="1", request_id="REQ-1")
        entry = conn.audit()[-1]
        self.assertEqual("REQ-1", entry["request_id"])
        self.assertNotIn("claimed_author", entry)
        self.assertEqual({}, gov.said())
        self.assertEqual({"claimed_author": "claude"}, gov.said("claude"))


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


class AReadCursorOnlyMovesForward(unittest.TestCase):
    """fleet:cursor:<agent> (Aya, 2026-10-13): a checkpoint whose answer was lost may land late. It must
    not move the cursor back past a newer one, whenever it arrives and by whichever route."""

    KEY = "fleet:cursor:grok"

    def test_forward_and_equal_are_written_and_backward_is_refused(self):
        conn = FakeRedis()
        self.assertTrue(run(conn, "grok", "set", self.KEY, val="5-0")["ok"], "the first save")
        self.assertTrue(run(conn, "grok", "set", self.KEY, val="5-0")["ok"], "saving one value twice")
        self.assertTrue(run(conn, "grok", "set", self.KEY, val="5-1")["ok"])
        out = run(conn, "grok", "set", self.KEY, val="5-0")
        self.assertFalse(out["ok"])
        self.assertIn("only moves forward", out["refused"])
        self.assertEqual("5-1", conn.data[self.KEY])
        self.assertEqual("0", conn.audit()[-1]["ok"], "the refusal is audited like any other")

    def test_ids_compare_as_numbers_not_as_text(self):
        conn = FakeRedis()
        run(conn, "grok", "set", self.KEY, val="9-0")
        self.assertTrue(run(conn, "grok", "set", self.KEY, val="10-0")["ok"], '"10-0" sorts before "9-0" as text')
        self.assertIn("refused", run(conn, "grok", "set", self.KEY, val="9-0"))
        self.assertIn("refused", run(conn, "grok", "set", self.KEY, val="9-99"))
        self.assertEqual("10-0", conn.data[self.KEY])

    def test_a_cursor_is_a_stream_id_or_it_is_refused(self):
        for bad in ("", "banana", "5", "5-", "-5", "5-0-0", " 5-0", "99999999999999999999-0", "\u0665-0"):
            conn = FakeRedis()
            out = run(conn, "grok", "set", self.KEY, val=bad)
            self.assertFalse(out["ok"], repr(bad))
            self.assertNotIn(self.KEY, conn.data, repr(bad))

    def test_the_delayed_old_save_is_refused_after_a_newer_one(self):
        """The delayed completion itself: A was sent, its answer lost; B is saved; then A arrives."""
        conn = FakeRedis()
        self.assertTrue(run(conn, "grok", "set", self.KEY, val="20-0", via="bridge:later")["ok"])   # B
        late = run(conn, "grok", "set", self.KEY, val="12-0", via="bridge:earlier")               # A, late
        self.assertIn("only moves forward", late["refused"])
        self.assertEqual("20-0", conn.data[self.KEY], "the newer checkpoint stands")

    def test_it_is_a_compare_and_set_not_a_look_then_write(self):
        """A newer save that lands between this one's comparison and its EXEC: nothing is applied."""
        conn = FakeRedis()
        run(conn, "grok", "set", self.KEY, val="5-0")
        real = conn.pipeline

        def racing_pipeline(transaction=True):
            pipe = real(transaction)
            original_multi = pipe.multi

            def multi():
                conn.set(self.KEY, "30-0")                       # between the comparison and the EXEC
                original_multi()
            pipe.multi = multi
            return pipe
        conn.pipeline = racing_pipeline
        out = run(conn, "grok", "set", self.KEY, val="12-0")      # forward of 5-0 when it looked
        self.assertFalse(out["ok"])
        self.assertEqual("30-0", conn.data[self.KEY], "the older save overwrote the newer one")

    def test_a_key_that_holds_no_stream_id_can_be_repaired(self):
        conn = FakeRedis()
        conn.set(self.KEY, "left by an older writer")
        self.assertTrue(run(conn, "grok", "set", self.KEY, val="7-0")["ok"])

    def test_REGRESSION_a_saved_cursor_never_expires_so_the_value_compared_against_cannot_lapse(self):
        """Copilot on 98a3e90: the cursor "still inherits the ACL's 30-day fleet: TTL. Once the newer
        cursor expires, a delayed older SET sees no held value and is accepted." """
        conn = FakeRedis()
        self.assertIn("fleet:", ACL["ttl_seconds"], "this test needs an expiry it would otherwise inherit")
        self.assertTrue(run(conn, "grok", "set", self.KEY, val="20-0")["ok"])
        self.assertNotIn(self.KEY, conn.ttls, "the newer cursor can expire, and the old save then lands")
        conn.ttls[self.KEY] = ACL["ttl_seconds"]["fleet:"]          # as an older governor left it
        self.assertTrue(run(conn, "grok", "set", self.KEY, val="21-0")["ok"])
        self.assertNotIn(self.KEY, conn.ttls, "an expiry left by an older governor outlived the next save")
        self.assertIn("only moves forward", run(conn, "grok", "set", self.KEY, val="12-0")["refused"])
        self.assertNotIn(self.KEY, conn.ttls, "a refused save changed the key")
        # and nothing else lost its expiry: the same op beside it, and the same key by another op
        self.assertTrue(run(conn, "grok", "set", "fleet:y", val="1-0")["ok"])
        self.assertEqual(ACL["ttl_seconds"]["fleet:"], conn.ttls["fleet:y"])
        self.assertTrue(run(conn, "grok", "xadd", "fleet:cursor:astream", val="x")["ok"])
        self.assertEqual(ACL["ttl_seconds"]["fleet:"], conn.ttls["fleet:cursor:astream"],
                         "only a SAVED cursor is kept: a stream someone put under that name still expires")

    def test_del_still_removes_it_so_moving_back_is_a_deletion_first(self):
        conn = FakeRedis()
        run(conn, "grok", "set", self.KEY, val="20-0")
        self.assertTrue(run(conn, "grok", "del", self.KEY)["ok"])
        self.assertTrue(run(conn, "grok", "set", self.KEY, val="3-0")["ok"])
        self.assertEqual(["set", "del", "set"], [e["op"] for e in conn.audit()[-3:]], "each step is audited")

    def test_only_the_cursor_namespace_is_held_to_it(self):
        conn = FakeRedis()
        run(conn, "grok", "set", "fleet:x", val="9-0")
        self.assertTrue(run(conn, "grok", "set", "fleet:x", val="1-0")["ok"])
        self.assertTrue(run(conn, "grok", "set", "fleet:x", val="not an id")["ok"])
        self.assertEqual(("fleet:cursor:",), gov.FORWARD_ONLY)

    def test_the_rule_is_in_code_and_no_acl_can_switch_it_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            from_file = gov.load_acl(acl_file(tmp, forward_only=[], FORWARD_ONLY=[]))
        # the file as loaded, and the same rules handed over as a dict that carries the would-be switch
        for acl in (from_file, dict(from_file, forward_only=[], FORWARD_ONLY=[])):
            conn = FakeRedis()
            run(conn, "grok", "set", self.KEY, val="20-0", acl=acl)
            self.assertIn("refused", run(conn, "grok", "set", self.KEY, val="3-0", acl=acl))


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



class AnHoursCountIsKeptInRedis(unittest.TestCase):
    """hourly(): one count across 3,600 seconds, shared by every process on the same Redis.

    A UTC hour boundary does not open a new window (the 01:59:50 / 02:00:00 defect)."""

    HOUR = 1791993600.0                 # 2026-10-14 16:00:00Z

    def test_it_counts_to_the_ceiling_and_the_window_starts_again_after_3600_seconds(self):
        conn = FakeRedis()
        said = [gov.hourly(conn, "chair:canvas", self.HOUR + n, 3) for n in range(5)]
        self.assertEqual([True, True, True, False, False], said)
        self.assertEqual("5", conn.get("gov:quota:chair:canvas:n"), "a refusal is counted too")
        self.assertFalse(gov.hourly(conn, "chair:canvas", self.HOUR + 3599, 3), "3,599s is the same window")
        self.assertTrue(gov.hourly(conn, "chair:canvas", self.HOUR + 3600, 3), "3,600s opens the next")
        self.assertEqual("1", conn.get("gov:quota:chair:canvas:n"))
        self.assertTrue(gov.hourly(conn, "chair:persona", self.HOUR, 3), "each name has its own count")

    def test_ten_seconds_into_the_next_clock_hour_is_still_the_same_window(self):
        """20 at 16:59:50 and another at 17:00:00 must not both be allowed."""
        conn = FakeRedis()
        start = self.HOUR + 3590
        self.assertEqual([True] * 20, [gov.hourly(conn, "chair:canvas", start, 20) for _ in range(20)])
        self.assertFalse(gov.hourly(conn, "chair:canvas", start + 10, 20))
        self.assertEqual("21", conn.get("gov:quota:chair:canvas:n"), "the refusal is counted in the same window")

    def test_the_count_and_its_expiry_land_in_one_multi_and_a_later_count_does_not_slide_it(self):
        conn, queued = FakeRedis(), []
        real = conn.pipeline

        def watched(transaction=True):
            pipe = real(transaction)
            self.assertTrue(transaction)
            original = pipe.execute

            def execute(raise_on_error=True):
                queued.extend(name for name, _, _ in pipe.queue)
                return original(raise_on_error)
            pipe.execute = execute
            return pipe
        conn.pipeline = watched
        gov.hourly(conn, "chair:canvas", self.HOUR, 20)
        self.assertEqual(["set", "delete", "incr", "expire"], queued, "open the window in one transaction")
        self.assertEqual(gov.QUOTA_TTL_SECONDS, conn.ttls["gov:quota:chair:canvas:n"])
        self.assertEqual(gov.QUOTA_TTL_SECONDS, conn.ttls["gov:quota:chair:canvas:t"])
        self.assertEqual(3600, gov.QUOTA_TTL_SECONDS)
        queued.clear()
        gov.hourly(conn, "chair:canvas", self.HOUR + 10, 20)
        self.assertEqual(["incr"], queued, "a later count does not refresh the expiry")
        self.assertEqual(repr(self.HOUR), conn.get("gov:quota:chair:canvas:t"))
        self.assertEqual(3600, conn.ttls["gov:quota:chair:canvas:t"])

    def test_what_redis_raises_reaches_the_caller(self):
        conn = FakeRedis()
        conn.fail_next["incr"] = ConnectionError("dropped")
        with self.assertRaises(ConnectionError):
            gov.hourly(conn, "chair:canvas", self.HOUR, 20)

    def test_a_repeated_request_id_is_claimed_once_and_can_be_released(self):
        conn = FakeRedis()
        self.assertTrue(gov.once(conn, "chair:canvas", "req-1"))
        self.assertFalse(gov.once(conn, "chair:canvas", "req-1"))
        self.assertTrue(gov.once(conn, "chair:persona", "req-1"), "each name has its own claim")
        self.assertEqual(gov.ONCE_TTL_SECONDS, conn.ttls["gov:once:chair:canvas:req-1"])
        gov.release(conn, "chair:canvas", "req-1")
        self.assertTrue(gov.once(conn, "chair:canvas", "req-1"))
        with self.assertRaises(gov.Refused):
            gov.once(conn, "chair:canvas", "an id with spaces")

    def test_a_name_is_a_name_and_the_key_is_one_no_caller_may_write(self):
        conn = FakeRedis()
        for name in ("", "Chair", "chair canvas", "chair:*", "x", 7, "a" * 49):
            with self.assertRaises(gov.Refused, msg=repr(name)):
                gov.hourly(conn, name, self.HOUR, 20)
        self.assertEqual({}, conn.data)
        for key in ("gov:quota:chair:canvas:n", "gov:quota:chair:canvas:t", "gov:once:chair:canvas:req-1"):
            for agent in ("conference-chair", "claude-code-cli", "owner"):
                for op in ("set", "del"):
                    result = run(conn, agent, op, key, val="0")
                    self.assertIn("refused", result, (agent, op, key))
            self.assertIsNone(conn.get(key), "nobody resets the count")


class XrangeAfterACursor(unittest.TestCase):
    """An inbox read asks for everything after the last entry it saw, oldest first, and is told whether
    more is waiting - so a burst larger than one page is paged, never cut to the newest (REDIS-PRIMARY
    5.1). Without it, xrange returns the newest n, and the oldest of a burst of n+1 is never read."""

    def stream(self, n):
        conn = FakeRedis()
        for k in range(1, n + 1):
            self.assertTrue(run(conn, "grok", "xadd", "fleet:inbox:gemini", val="m%d" % k)["ok"])
        return conn

    def read(self, conn, after="", count=10, op="xrange", key="fleet:inbox:gemini"):
        return gov.execute(conn, ACL, "gemini", "REQ-A", op, key, count=count, after=after)

    def test_pages_oldest_first_and_says_when_more_is_waiting(self):
        conn = self.stream(25)
        first = self.read(conn, after="0-0", count=20)
        self.assertTrue(first["ok"])
        self.assertEqual(["m%d" % k for k in range(1, 21)], [e["text"] for e in first["value"]])
        self.assertTrue(first["more"])
        second = self.read(conn, after=first["value"][-1]["id"], count=20)
        self.assertEqual(["m%d" % k for k in range(21, 26)], [e["text"] for e in second["value"]])
        self.assertFalse(second["more"])

    def test_the_newest_n_read_loses_the_oldest_of_a_burst_which_is_why_after_exists(self):
        conn = self.stream(21)
        newest = self.read(conn, count=20)
        self.assertNotIn("m1", [e["text"] for e in newest["value"]])
        paged = self.read(conn, after="0-0", count=20)
        self.assertEqual("m1", paged["value"][0]["text"])

    def test_after_the_last_entry_is_empty_not_an_error(self):
        conn = self.stream(3)
        got = self.read(conn, after="3-0")
        self.assertTrue(got["ok"])
        self.assertEqual(0, got["entries"])
        self.assertFalse(got["more"])

    def test_the_cursor_is_exclusive(self):
        conn = self.stream(3)
        self.assertEqual(["m3"], [e["text"] for e in self.read(conn, after="2-0")["value"]])

    def test_after_on_any_other_op_is_refused_and_audited(self):
        conn = self.stream(1)
        got = self.read(conn, after="1-0", op="get", key="fleet:x")
        self.assertFalse(got["ok"])
        self.assertIn("xrange only", got["refused"])
        self.assertFalse(conn.audit()[-1]["ok"] in (True, "true", "1"))

    def test_a_malformed_cursor_is_refused(self):
        conn = self.stream(1)
        for bad in ("1", "1-", "-1", "a-b", "(1-0", "1-0|op=del", "+", "-", "$", "1-0\n2-0"):
            got = self.read(conn, after=bad)
            self.assertFalse(got["ok"], bad)
            self.assertIn("stream entry id", got["refused"], bad)

    def test_a_cursor_part_above_the_unsigned_64_bit_maximum_is_refused_and_the_maximum_is_not(self):
        conn = self.stream(1)
        top = str(2 ** 64 - 1)
        for bad in (str(2 ** 64) + "-0", "0-" + str(2 ** 64), "99999999999999999999-0"):
            got = self.read(conn, after=bad)
            self.assertFalse(got["ok"], bad)
            self.assertIn("stream entry id", got["refused"], bad)
        for edge in (top + "-0", "0-" + top, str(2 ** 63) + "-1"):
            self.assertTrue(self.read(conn, after=edge)["ok"], edge)

    def test_after_the_largest_id_is_an_empty_page_without_asking_redis(self):
        # Cursor on 2dc187a: Redis 7.0 rejects XRANGE with an exclusive start at the largest id, so the
        # read must not send it. Nothing can follow that id: the answer is an empty page.
        top = str(2 ** 64 - 1) + "-" + str(2 ** 64 - 1)

        class RejectsTheTop(FakeRedis):
            def xrange(self, key, min="-", max="+", count=None):
                if min == "(" + top:
                    raise RuntimeError("ERR invalid start ID for the interval")
                return FakeRedis.xrange(self, key, min=min, max=max, count=count)
        conn = RejectsTheTop()
        self.assertTrue(run(conn, "grok", "xadd", "fleet:inbox:gemini", val="m1")["ok"])
        got = self.read(conn, after=top)
        self.assertEqual((True, 0, False, top), (got["ok"], got["entries"], got["more"], got["last"]))
        self.assertIn("[after=%s more=false last=%s]" % (top, top), gov.summarise(got))

    def test_the_shown_id_is_the_stream_id_and_a_field_named_id_is_kept_beside_it(self):
        # Cursor on PR 397: the field used to overwrite the shown id, in both reads; a reader that keeps its
        # cursor by the shown id could then skip messages. The field's own value is kept, never dropped.
        conn = self.stream(1)
        conn.data["fleet:inbox:gemini"].append(("9-2", {"id": "100-0", "text": "written elsewhere"}))
        for got in (self.read(conn, after="0-0"), self.read(conn)):
            shown = got["value"][-1]
            self.assertEqual(("9-2", "100-0", "written elsewhere"), (shown["id"], shown["field:id"], shown["text"]))

    def test_the_field_renaming_never_lands_two_fields_on_one_name(self):
        # Copilot on PR 397: a field already named "field:id" collided with the renamed `id`.
        shown = gov.entry_view("3-0", {"id": "a", "field:id": "b", "field:field:id": "c", "text": "t"})
        self.assertEqual({"id": "3-0", "field:id": "a", "field:field:id": "b", "field:field:field:id": "c",
                          "text": "t"}, shown)

    def test_last_is_the_stream_id_even_when_an_entry_has_a_field_named_id(self):
        conn = self.stream(2)
        conn.data["fleet:inbox:gemini"].append(("9-2", {"id": "not-a-cursor", "text": "written elsewhere"}))
        got = self.read(conn, after="0-0")
        self.assertEqual("9-2", got["last"])
        self.assertIn("last=9-2]", gov.summarise(got))

    def test_the_board_answer_says_where_the_page_stands_before_the_entries(self):
        conn = self.stream(25)
        first = self.read(conn, after="0-0", count=20)
        line = gov.summarise(first)
        self.assertIn("[after=0-0 more=true last=%s]" % first["value"][-1]["id"], line)
        self.assertLess(line.index("more=true"), line.index("m1"), "before the entries, so truncation keeps it")
        last = gov.summarise(self.read(conn, after=first["value"][-1]["id"], count=20))
        self.assertIn("more=false", last)
        empty = gov.summarise(self.read(conn, after=conn.data["fleet:inbox:gemini"][-1][0]))
        self.assertIn("more=false last=%s]" % conn.data["fleet:inbox:gemini"][-1][0], empty)
        self.assertNotIn("more=", gov.summarise(self.read(conn)), "the newest-n read is unchanged")

    def test_the_read_still_obeys_the_access_list(self):
        conn = self.stream(1)
        conn.data["probe:x"] = []
        got = gov.execute(conn, ACL, "stranger", "REQ-A", "xrange", "fleet:inbox:gemini", after="0-0")
        self.assertFalse(got["ok"])

    def test_without_after_the_answer_is_unchanged(self):
        conn = self.stream(2)
        got = self.read(conn)
        self.assertNotIn("more", got)
        self.assertNotIn("after", got)
        self.assertEqual(["m1", "m2"], [e["text"] for e in got["value"]])


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
        return ["ROW-" + req, ts, tag, "bus-reconciler", "AYA_REQ",
                "BCB|v=1|id=%s|req=%s|%s" % (req, req, payload), "", "", "", ""]

    def run_rows(self, rows, conn=None):
        conn = conn if conn is not None else FakeRedis()
        out_rows = []
        out = self.br.handle(rows, conn, now=self.now, append=out_rows.append)
        return out, out_rows, conn

    def test_grok_writes_and_the_result_is_one_row(self):
        out, rows, conn = self.run_rows([self.row(
            "grok", "do=redis-op|op=set|key=conf:2026-10-07:scorecard|val=green")])
        self.assertEqual("green", conn.data["conf:2026-10-07:scorecard"])
        self.assertEqual(1, len(rows), "one RESULT row, no separate receipt")
        self.assertEqual("AYA_RESULT", rows[0]["action_type"])
        self.assertIn("OK set conf:2026-10-07:scorecard", rows[0]["payload"])
        self.assertEqual("grok", rows[0]["target_surface"], "answered under its own tag")
        self.assertEqual("OK", out["answered"][0]["status"])
        self.assertEqual("grok", conn.audit()[-1]["actor"])
        self.assertTrue(conn.audit()[-1]["via"].startswith("REQ-0001/"), "the audit names the row")

    def test_each_agent_he_named_reaches_redis_through_the_board(self):
        rows = [self.row(tag, "do=redis-op|op=set|key=fleet:hello:%s|val=hi" % tag, req="REQ-%04d" % n)
                for n, tag in enumerate(("chatgpt-codex-desktop", "grok", "cursor", "gemini",
                                         "codex", "aya"), start=1)]
        out, written, conn = self.run_rows(rows)
        self.assertEqual(["OK"] * 6, [a["status"] for a in out["answered"]])
        for tag in ("chatgpt-codex-desktop", "grok", "cursor", "gemini", "codex", "aya"):
            self.assertEqual("hi", conn.data["fleet:hello:%s" % tag])

    def test_after_rides_the_row_from_the_tool_to_the_read(self):
        import datetime
        import fleet_agent
        conn = FakeRedis()
        for k in range(1, 4):
            gov.execute(conn, ACL, "grok", "SEED", "xadd", "fleet:inbox:gemini", raw_value="m%d" % k)
        when = self.now - datetime.timedelta(minutes=1)
        rid, row = fleet_agent.request_row("gemini", "xrange", "fleet:inbox:gemini", count=5, after="1-0",
                                           now=when, rid="REQ-AFTER-0001")
        self.assertIn("|after=1-0|", row[5])
        out, written, conn = self.run_rows([row], conn)
        self.assertEqual(["OK"], [a["status"] for a in out["answered"]])
        self.assertIn('"m2"', written[0]["payload"])
        self.assertIn('"m3"', written[0]["payload"])
        self.assertNotIn('"m1"', written[0]["payload"])

    def test_the_tool_refuses_after_where_the_worker_would(self):
        import fleet_agent
        for op, after in (("get", "1-0"), ("xrange", "1"), ("xrange", "1-0|op=del"),
                          ("xrange", str(2 ** 64) + "-0")):
            with self.assertRaises(ValueError):
                fleet_agent.request_row("gemini", op, "fleet:inbox:gemini", after=after)

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

    def test_a_stranger_with_the_same_tool_gets_no_row_and_writes_nothing(self):
        import datetime
        import fleet_agent
        when = self.now - datetime.timedelta(minutes=1)    # a request from the future is refused
        _, row = fleet_agent.request_row("stranger", "set", "fleet:x", val="v", now=when, rid="REQ-FA-0009")
        out, written, conn = self.run_rows([row])
        self.assertEqual([], written)
        self.assertNotIn("fleet:x", conn.data)

    def test_a_read_comes_back_in_the_result(self):
        conn = FakeRedis()
        conn.set("fleet:status", "amber")
        out, rows, _ = self.run_rows(
            [self.row("cursor", "do=redis-op|op=get|key=fleet:status")], conn)
        self.assertIn("amber", rows[0]["payload"])

    WORDS = "Codex, draft the sample queue flow for the vans."     # synthetic, as every test sentence here

    def test_a_call_bridge_inbox_entry_never_shows_his_words_on_the_board(self):
        """2026-11-02 23:06Z: a governed inbox read put a short call ask's exact words in a PUBLIC RESULT row."""
        conn = FakeRedis()
        conn.xadd("fleet:inbox:claude-code-cli", {"by": "call-bridge", "ask": "call-0123456789ab",
                                                  "input_sha256": "ab" * 32, "text": "CALL ASK: " + self.WORDS})
        conn.xadd("fleet:inbox:claude-code-cli", {"by": "grok", "text": "a peer note stays readable"})
        for payload in ("do=redis-op|op=xrange|key=fleet:inbox:claude-code-cli|count=5",
                        "do=redis-op|op=xrange|key=fleet:inbox:claude-code-cli|count=5|after=0-0"):
            out, rows, _ = self.run_rows([self.row("claude-code-cli", payload, req="REQ-IN-%d" % len(payload))], conn)
            body = rows[0]["payload"]
            self.assertNotIn("sample queue flow", body)
            self.assertIn(gov.WITHHELD, body)
            self.assertIn("call-0123456789ab", body, "the ask id and the hash stay: the agent knows which ask")
            self.assertIn("a peer note stays readable", body, "only the call bridge's text is withheld")
        self.assertIn("1-0", body)
        self.assertIn("last=2-0", body, "paging still moves the cursor")

    def test_the_rooms_stream_and_the_bridges_records_are_withheld_whole(self):
        conn = FakeRedis()
        conn.xadd("v1:conf:conf-0123456789abcdef-89abcdef:events", {"kind": "turn.started", "utterance": self.WORDS})
        conn.set("gov:callbridge:proposal:conf-0123456789abcdef-89abcdef", '{"said": "%s"}' % self.WORDS)
        conn.set("gov:callbridge:asked:conf-0123456789abcdef-89abcdef", '{"said": "%s"}' % self.WORDS)
        conn.set("gov:callbridge:pending:conf-0123456789abcdef-89abcdef", '{"said": "%s"}' % self.WORDS)
        conn.hset("v1:conf:conf-0123456789abcdef-89abcdef:readback", mapping={"id": "rb-0123456789ab",
                                                                             "text": self.WORDS})
        reads = ["op=xrange|key=v1:conf:conf-0123456789abcdef-89abcdef:events|count=5",
                 "op=get|key=gov:callbridge:proposal:conf-0123456789abcdef-89abcdef",
                 "op=get|key=gov:callbridge:asked:conf-0123456789abcdef-89abcdef",
                 "op=get|key=gov:callbridge:pending:conf-0123456789abcdef-89abcdef",
                 "op=hgetall|key=v1:conf:conf-0123456789abcdef-89abcdef:readback"]
        for n, read in enumerate(reads):
            out, rows, _ = self.run_rows([self.row("claude-code-cli", "do=redis-op|" + read, req="REQ-W-%d" % n)], conn)
            self.assertNotIn("sample queue flow", rows[0]["payload"], read)
            self.assertIn(gov.WITHHELD, rows[0]["payload"], read)
        # A withheld stream page keeps its stream ids, so a reader's cursor still moves past it.
        conn.xadd("v1:conf:conf-0123456789abcdef-89abcdef:events", {"kind": "turn.started", "utterance": "x"})
        out, rows, _ = self.run_rows([self.row(
            "claude-code-cli", "do=redis-op|op=xrange|key=v1:conf:conf-0123456789abcdef-89abcdef:events|count=5|after=0-0",
            req="REQ-W-PAGE")], conn)
        self.assertIn('"id": "1-0"', rows[0]["payload"])
        self.assertIn('"id": "2-0"', rows[0]["payload"])
        self.assertIn("last=2-0", rows[0]["payload"])

    def test_board_safe_leaves_everything_else_and_never_mutates_the_answer(self):
        answer = {"ok": True, "op": "get", "key": "fleet:status", "value": "amber"}
        self.assertIs(answer, gov.board_safe(answer))
        inbox = {"ok": True, "op": "xrange", "key": "fleet:inbox:x",
                 "value": [{"id": "1-0", "by": "call-bridge", "text": self.WORDS}]}
        shown = gov.board_safe(inbox)
        self.assertEqual(self.WORDS, inbox["value"][0]["text"], "the caller's dict is untouched")
        self.assertEqual(gov.WITHHELD, shown["value"][0]["text"])
        refused = {"ok": False, "op": "get", "key": "gov:callbridge:proposal:x", "refused": "no"}
        self.assertIs(refused, gov.board_safe(refused))
        for key in ("v1:conf:r:check", "v1:conf:r:retired", "gov:callbridge:task:call-1", "conf:briefing",
                    "v1:confx:r:state", "fleet:v1:conf:r:state"):
            plain = {"ok": True, "op": "get", "key": key, "value": "v"}
            self.assertIs(plain, gov.board_safe(plain), key)

    # Codex HOLD on 2ee91bba: a room-state read could show his agenda on the public board.
    ROOM = "conf-0123456789abcdef-89abcdef"
    AGENDA_TEXT = {"agenda_title": "Vans: " + WORDS, "agenda_current_text": WORDS,
                   "agenda_items": '[{"lead":"grok","minutes":5,"status":"now","text":"%s"}]' % WORDS}
    AGENDA_META = {"room_id": ROOM, "session_epoch": "e-1", "proj_count": "7", "kind": "agenda.updated",
                   "agenda_state": "running", "agenda_items_total": "3", "agenda_items_done": "1",
                   "agenda_current_index": "2", "agenda_ends_at": "1762180000", "agenda_digest": "ab" * 8,
                   "closed": "1", "closed_epoch": "e-1"}

    def test_the_rooms_state_on_the_board_keeps_where_the_call_is_and_never_its_agenda(self):
        conn = FakeRedis()
        conn.hset("v1:conf:%s:state" % self.ROOM, mapping=dict(self.AGENDA_META, **self.AGENDA_TEXT))
        out, rows, _ = self.run_rows([self.row(
            "claude-code-cli", "do=redis-op|op=hgetall|key=v1:conf:%s:state" % self.ROOM, req="REQ-ST-1")], conn)
        body = rows[0]["payload"]
        self.assertNotIn("sample queue flow", body)
        self.assertNotIn("Vans", body)
        self.assertIn(gov.WITHHELD, body)
        for field in ("agenda_items_total", "agenda_current_index", "closed_epoch"):
            self.assertIn(field, body, "where the call is stays readable")

    def test_the_state_hash_shows_only_the_fields_it_names(self):
        answer = {"ok": True, "op": "hgetall", "key": "v1:conf:%s:state" % self.ROOM, "fields": 99,
                  "value": dict(self.AGENDA_META, **self.AGENDA_TEXT, some_field_added_later=self.WORDS)}
        shown = gov.board_safe(answer)
        self.assertEqual(self.WORDS, answer["value"]["agenda_current_text"], "the caller's dict is untouched")
        for field in list(self.AGENDA_TEXT) + ["some_field_added_later"]:
            self.assertEqual(gov.WITHHELD, shown["value"][field], field)
        for field, value in self.AGENDA_META.items():
            self.assertEqual(value, shown["value"][field], field)
        self.assertEqual(99, shown["fields"])
        for field in gov.STATE_SHOWN:
            self.assertEqual("x", gov.board_safe(dict(answer, value={field: "x"}))["value"][field], field)
        self.assertEqual(21, len(gov.STATE_SHOWN))
        self.assertFalse({"agenda_title", "agenda_current_text", "agenda_items"} & gov.STATE_SHOWN)

    def test_any_other_chair_key_is_withheld_whole(self):
        for key, op, value in (("v1:conf:%s:state" % self.ROOM, "get", self.WORDS),
                               ("v1:conf:%s:state" % self.ROOM, "hgetall", self.WORDS),
                               ("v1:conf:%s:state:x" % self.ROOM, "hgetall", {"agenda_title": self.WORDS}),
                               ("v1:conf:%s:events:x" % self.ROOM, "get", self.WORDS),
                               ("v1:conf:%s:notes" % self.ROOM, "hgetall", {"text": self.WORDS}),
                               ("v1:conf:index", "get", self.WORDS)):
            with self.subTest(key=key, op=op):
                shown = gov.board_safe({"ok": True, "op": op, "key": key, "value": value})
                self.assertEqual(gov.WITHHELD, shown["value"])
        stream = gov.board_safe({"ok": True, "op": "xrange", "key": "v1:conf:%s:notes" % self.ROOM,
                                 "value": [{"id": "1-0", "fields": {"text": self.WORDS}}]})
        self.assertEqual([{"id": "1-0", "withheld": gov.WITHHELD}], stream["value"], "a page keeps its ids")
        self.assertIsNone(gov.board_safe({"ok": True, "op": "get", "key": "v1:conf:r:notes", "value": None})["value"])

    def test_a_protected_write_is_refused_on_the_board_and_in_the_audit(self):
        out, rows, conn = self.run_rows([self.row(
            "gemini", "do=redis-op|op=set|key=v1:bus:row:R-1|val=forged")])
        self.assertNotIn("v1:bus:row:R-1", conn.data)
        self.assertIn("REFUSED", rows[0]["payload"])
        self.assertEqual("REFUSED", out["answered"][0]["status"])
        self.assertEqual("0", conn.audit()[-1]["ok"])

    def test_a_STALE_request_from_an_access_list_agent_is_told_why(self):
        """Refused before the op is even considered - and still answered, because the sender is
        someone the access list recognises. The first version of this file had no test for it: a
        mutation that restricted refusal rows to the probe's old SENDERS list passed everything, so
        grok, cursor and gemini would have heard silence for a stale request."""
        out, rows, conn = self.run_rows([self.row(
            "grok", "do=redis-op|op=set|key=fleet:x|val=1", ts="2026-10-08T00:00:00Z")])
        self.assertNotIn("fleet:x", conn.data)
        self.assertEqual(1, len(rows), "a recognised sender is told it was refused")
        self.assertIn("older than", rows[0]["payload"])
        self.assertEqual("grok", rows[0]["target_surface"])

    def test_an_unknown_sender_gets_no_row_and_touches_nothing(self):
        """The flood rule, unchanged: a tag nobody recognises does not make us write a row."""
        out, rows, conn = self.run_rows([self.row(
            "a-stranger", "do=redis-op|op=set|key=fleet:x|val=1")])
        self.assertEqual([], rows)
        self.assertNotIn("fleet:x", conn.data)
        self.assertEqual(1, len(out["refused"]))

    def test_a_replayed_request_id_does_nothing_the_second_time(self):
        first = self.row("grok", "do=redis-op|op=set|key=fleet:x|val=1")
        out, rows, conn = self.run_rows([first])
        answered_row = [rows[0]["row_id"], "2026-10-08T01:59:30Z", "bus-reconciler", "grok",
                        "AYA_RESULT", rows[0]["payload"], "", "", "", ""]
        out2, rows2, _ = self.run_rows([first, answered_row], conn)
        self.assertEqual([], rows2, "already answered: no second write, no second row")
        self.assertEqual(1, len([e for e in conn.audit() if e["op"] == "set"]))

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
            self.row("grok", "do=redis-op|op=set|key=fleet:x|val=1", req="REQ-0001"),
            self.row("cursor", "do=redis-op|op=set|key=v1:bus:row:1|val=x", req="REQ-0002"),
            self.row("aya", "do=redis-synthetic-probe", req="REQ-0003"),
            self.row("a-stranger", "do=redis-op|op=get|key=fleet:x", req="REQ-0004"),
        ], conn)
        self.assertEqual(3, len(out["answered"]))
        for item in out["answered"]:
            missing = loops["answered"] - set(item)
            self.assertEqual(set(), missing, "answer %s lacks %s" % (item.get("req_id"), missing))
        for item in out["refused"]:
            missing = loops["refused"] - set(item)
            self.assertEqual(set(), missing, "refusal %s lacks %s" % (item.get("req_id"), missing))

    def test_redis_op_is_listed_TOGETHER_with_its_branch(self):
        """Governed operations must dispatch to their controller, never the synthetic probe."""
        from unittest import mock
        self.assertIn("redis-op", self.br.ACTIONS)
        self.assertIn("call-task", self.br.ACTIONS)
        self.assertIn("call-task", self.br.ACL_ACTIONS)
        class LifecyclePipeline(FakePipeline):
            def watch(self, *keys):
                for key in keys:
                    super().watch(key)
        class LifecycleRedis(FakeRedis):
            def pipeline(self, transaction=True):
                return LifecyclePipeline(self)
        with mock.patch.object(self.br, "synthetic_probe", side_effect=AssertionError("wrong action branch")):
            result, rows, conn = self.run_rows([
                self.row("grok", "do=redis-op|op=set|key=fleet:branch|val=governed", req="REQ-BRANCH01"),
                self.row("claude-code-cli", "do=call-task|op=status|key=call-000000000000", req="REQ-BRANCH02"),
            ], LifecycleRedis())
        self.assertEqual("governed", conn.data["fleet:branch"])
        self.assertEqual(["OK", "REFUSED"], [item["status"] for item in result["answered"]])
        self.assertEqual(["AYA_RESULT", "AYA_RESULT"], [item["action_type"] for item in rows])
        self.assertIn("call-task authority, binding or operation unavailable", rows[1]["payload"])
        self.assertIn(self.br.run_marker(self.br.parse_request(self.row(
            "claude-code-cli", "do=call-task|op=status|key=call-000000000000", req="REQ-BRANCH02"))), conn.data)
        # Listing an action cannot bypass the access list: an unknown board tag gets no controller execution.
        with mock.patch.object(self.br, "_call_task", side_effect=AssertionError("ungoverned lifecycle request")):
            refused, _, _ = self.run_rows([self.row("a-stranger", "do=call-task|op=status|key=call-000000000000")])
        self.assertEqual([], refused["answered"])
        self.assertTrue(refused["refused"])


LANE_ENTRY = {"event": "pr-review-pass", "ref": "conference#375", "thread": "TH-ESTIMATES",
              "task": "B12", "parent": "T-01", "sha": "8f39c208"}


def lane_val(entry=None):
    return base64.b64encode(json.dumps(LANE_ENTRY if entry is None else entry).encode("utf-8")).decode("ascii")


class StreamRecorder(FakeRedis):
    """FakeRedis that remembers the trim each xadd asked for."""

    def __init__(self):
        super().__init__()
        self.trims = {}

    def xadd(self, key, fields, maxlen=None, approximate=None):
        self.trims[key] = (maxlen, approximate)
        return super().xadd(key, fields, maxlen=maxlen, approximate=approximate)


class TheFiveRoleLanes(unittest.TestCase):
    """B12, the V2 test plan's Golden Rule 2: five role lanes, each written by its role only, ids and metadata only,
    bounded in length and in time. The rule is in code (ROLE_LANES) AND granted in the access list: both gates."""

    WRITERS = {"lane:architect": {"gemini"}, "lane:build": {"claude-code-cli", "cursor"},
               "lane:test": {"chatgpt-codex-desktop"}, "lane:deploy": {"claude-code-cli"},
               "lane:coordinator": {"claude-code-cli"}}
    # Every principal in the access list, every alias, and a stranger.
    IDENTITIES = tuple(sorted(set(ACL["principals"]) | set(ACL["aliases"]))) + ("somebody-new",)

    def test_the_lanes_and_their_writers_are_exactly_the_plan_s(self):
        self.assertEqual(self.WRITERS, {lane: set(who) for lane, who in gov.ROLE_LANES.items()})

    def test_each_lane_s_writer_appends_and_every_other_identity_is_refused_and_audited(self):
        for lane, writers in self.WRITERS.items():
            for tag in self.IDENTITIES:
                with self.subTest(lane=lane, tag=tag):
                    conn = FakeRedis()
                    out = run(conn, tag, "xadd", lane, val=lane_val(), enc="b64")
                    if gov.principal(ACL, tag) in writers:
                        self.assertTrue(out["ok"], out)
                        self.assertEqual(1, len(conn.data[lane]))
                    else:
                        self.assertFalse(out["ok"], out)
                        self.assertIn("refused", out)
                        self.assertNotIn(lane, conn.data, "a refused append wrote nothing")
                        self.assertEqual("0", conn.audit()[-1]["ok"], "and the refusal is audited")

    def test_the_owner_writes_no_lane(self):
        for tag in ("owner", "whatsapp", "salam"):
            for lane in self.WRITERS:
                self.assertFalse(run(FakeRedis(), tag, "xadd", lane, val=lane_val(), enc="b64")["ok"], (tag, lane))

    def test_an_access_list_that_says_write_everything_adds_no_writer(self):
        """The careless edit again: grok with write "*" still writes no lane, because the code says who does."""
        with tempfile.TemporaryDirectory() as tmp:
            acl = gov.load_acl(acl_file(tmp, principals={"grok": {"read": ["*"], "write": ["*"]}}))
        conn = FakeRedis()
        for lane in self.WRITERS:
            out = run(conn, "grok", "xadd", lane, val=lane_val(), enc="b64", acl=acl)
            self.assertFalse(out["ok"], lane)
            self.assertIn("does not write", out["refused"])
        self.assertTrue(run(conn, "grok", "set", "anything:else", val="ok", acl=acl)["ok"],
                        "the same access list opens an ordinary key, so this is the lane rule and not luck")

    def test_the_access_list_must_also_grant_it(self):
        """Two gates: a lane's writer that the access list does not grant is refused."""
        data = json.loads(gov.ACL_FILE.read_text(encoding="utf-8"))
        data["principals"]["claude-code-cli"]["write"].remove("lane:deploy")
        with tempfile.TemporaryDirectory() as tmp:
            acl = gov.load_acl(acl_file(tmp, principals=data["principals"]))
        out = run(FakeRedis(), "claude-code-cli", "xadd", "lane:deploy", val=lane_val(), enc="b64", acl=acl)
        self.assertFalse(out["ok"])
        self.assertIn("may not write", out["refused"])

    def test_the_access_list_and_the_code_name_the_same_writers(self):
        for who, rules in ACL["principals"].items():
            for lane, writers in gov.ROLE_LANES.items():
                self.assertEqual(who in writers, gov._matches(lane, rules.get("write")), (who, lane))

    def test_every_reader_of_star_reads_every_lane_and_the_narrow_principals_read_none(self):
        conn = FakeRedis()
        for lane in self.WRITERS:
            writer = sorted(self.WRITERS[lane])[0]
            self.assertTrue(run(conn, writer, "xadd", lane, val=lane_val(), enc="b64")["ok"])
        for who, rules in ACL["principals"].items():
            for lane in self.WRITERS:
                out = run(conn, who, "xrange", lane)
                with self.subTest(who=who, lane=lane):
                    if "*" in rules.get("read", ()):
                        self.assertTrue(out["ok"], out)
                        self.assertEqual("pr-review-pass", out["value"][0]["event"])
                    else:
                        self.assertFalse(out["ok"], out)
        self.assertFalse(run(conn, "somebody-new", "xrange", "lane:build")["ok"])

    def test_a_lane_is_a_stream_xadd_and_xrange_and_nothing_else_even_for_its_writer(self):
        conn = FakeRedis()
        self.assertTrue(run(conn, "claude-code-cli", "xadd", "lane:coordinator", val=lane_val(), enc="b64")["ok"])
        before = list(conn.data["lane:coordinator"])
        for op, extra in (("set", {"val": "x"}), ("del", {}), ("hset", {"field": "f", "val": "x"}),
                          ("hput", {"val": base64.b64encode(b'{"a":"b"}').decode(), "enc": "b64"}),
                          ("get", {}), ("hgetall", {})):
            out = run(conn, "claude-code-cli", op, "lane:coordinator", **extra)
            self.assertFalse(out["ok"], op)
            self.assertIn("a role lane is a stream", out["refused"], op)
        out = gov.execute(conn, ACL, "claude-code-cli", "bridge:x", "xwait", "lane:coordinator", after="0-0")
        self.assertFalse(out["ok"])
        self.assertEqual(before, conn.data["lane:coordinator"], "nothing edited the lane's history")

    def test_there_are_five_lanes_and_no_sixth(self):
        conn = FakeRedis()
        for key in ("lane:ops", "lane:build:extra", "lane:builder", "lane:", "lane:Build"):
            for op, extra in (("xadd", {"val": lane_val(), "enc": "b64"}), ("xrange", {})):
                out = run(conn, "claude-code-cli", op, key, **extra)
                self.assertFalse(out["ok"], (op, key))
                self.assertIn("the role lanes are", out["refused"], (op, key))
        self.assertFalse(any(key.startswith("lane:") for key in conn.data))

    def test_an_entry_is_ids_and_metadata_and_is_stored_as_them(self):
        conn = FakeRedis()
        out = run(conn, "chatgpt-codex-desktop", "xadd", "lane:test", val=lane_val(), enc="b64")
        self.assertTrue(out["ok"], out)
        stored = conn.data["lane:test"][0][1]
        self.assertEqual(dict(LANE_ENTRY, by="chatgpt-codex-desktop"), {k: v for k, v in stored.items() if k != "at"})
        self.assertTrue(stored["at"].endswith("Z"))
        self.assertEqual(gov.preview(gov.canonical(LANE_ENTRY)), conn.audit()[-1]["new"])
        # Unencoded JSON is the same entry: the grammar is the JSON's, not the transport's.
        self.assertTrue(run(conn, "chatgpt-codex-desktop", "xadd", "lane:test",
                            val=json.dumps({"event": "run-done"}))["ok"])

    def test_prose_and_anything_but_the_members_is_refused_whole(self):
        too_long = "a" * 81
        for bad in ("a free text sentence with spaces in it",
                    json.dumps("just a string"),
                    json.dumps(["event", "x"]),
                    json.dumps({"ref": "conference#375"}),                         # no event
                    json.dumps({"event": "said", "note": "free text here"}),       # not a member
                    json.dumps({"event": "said", "ref": "two words"}),             # a space
                    json.dumps({"event": "Ship It"}),
                    json.dumps({"event": "x"}),                                    # under two characters
                    json.dumps({"event": "said", "ref": too_long}),
                    json.dumps({"event": "said", "sha": "not-hex!"}),
                    json.dumps({"event": "said", "task": 7}),
                    json.dumps({"event": "said", "thread": {"nested": "x"}}),
                    json.dumps({"event": "said", "by": "gemini"}),                 # the server's, never sent
                    json.dumps({"event": "said", "ref": "https://example.invalid"})):
            conn = FakeRedis()
            out = run(conn, "gemini", "xadd", "lane:architect",
                      val=base64.b64encode(bad.encode("utf-8")).decode("ascii"), enc="b64")
            self.assertFalse(out["ok"], bad)
            self.assertIn("role lane entry", out["refused"], bad)
            self.assertNotIn("lane:architect", conn.data, bad)

    def test_the_largest_entry_the_grammar_admits_fits_the_byte_bound(self):
        biggest = {"event": "e" * 32, "ref": "r" * 80, "thread": "t" * 80, "task": "k" * 80,
                   "parent": "p" * 80, "sha": "a" * 40}
        self.assertLessEqual(len(json.dumps(biggest).encode("utf-8")), gov.ROLE_LANE_ENTRY_BYTES)
        self.assertTrue(run(FakeRedis(), "cursor", "xadd", "lane:build", val=lane_val(biggest), enc="b64")["ok"])

    def test_a_lane_is_bounded_in_length_and_in_time(self):
        conn = StreamRecorder()
        out = run(conn, "claude-code-cli", "xadd", "lane:deploy", val=lane_val(), enc="b64")
        self.assertTrue(out["ok"], out)
        self.assertEqual((gov.ROLE_LANE_MAXLEN, True), conn.trims["lane:deploy"])
        self.assertLess(gov.ROLE_LANE_MAXLEN, gov.STREAM_MAXLEN)
        self.assertEqual(ACL["ttl_seconds"]["lane:"], conn.ttls["lane:deploy"])
        self.assertEqual(ACL["ttl_seconds"]["lane:"], out["ttl"])
        # An ordinary stream keeps its own trim: the lane bound is the lane's.
        run(conn, "claude-code-cli", "xadd", "fleet:log", val="x")
        self.assertEqual((gov.STREAM_MAXLEN, True), conn.trims["fleet:log"])

    def test_an_access_list_without_a_lane_ttl_appends_nothing(self):
        ttls = {k: v for k, v in ACL["ttl_seconds"].items() if k != "lane:"}
        with tempfile.TemporaryDirectory() as tmp:
            acl = gov.load_acl(acl_file(tmp, ttl_seconds=ttls))
        conn = FakeRedis()
        out = run(conn, "claude-code-cli", "xadd", "lane:deploy", val=lane_val(), enc="b64", acl=acl)
        self.assertFalse(out["ok"])
        self.assertIn("no TTL", out["refused"])
        self.assertNotIn("lane:deploy", conn.data)

    def test_the_board_summary_of_a_lane_read_is_ids_only(self):
        conn = FakeRedis()
        run(conn, "gemini", "xadd", "lane:architect", val=lane_val(), enc="b64")
        line = gov.summarise(run(conn, "grok", "xrange", "lane:architect"))
        self.assertTrue(line.startswith("OK xrange lane:architect"), line)
        self.assertIn("conference#375", line)
        self.assertNotIn("|", line)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

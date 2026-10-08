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

    def set(self, key, value, ex=None):
        self._need(key, "string")
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

    def hset(self, key, field, value):
        self._need(key, "hash")
        self.data.setdefault(key, {})[field] = str(value)
        self._bump(key)
        return 1

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

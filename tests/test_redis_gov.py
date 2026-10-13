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


def run(conn, tag, op, key, field="", val="", enc="", via="REQ-1", acl=None, verified=False):
    return gov.execute(conn, acl or ACL, tag, via, op, key, field=field, raw_value=val, enc=enc,
                       verified=verified)


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


def work_entry(rev, action, state, owner="grok", reviewer="-", requester="cursor", lease="-", req="req00001",
               words=""):
    """One log entry in the grammar redis_gov.work_event reads."""
    return ("r%d %s %s %s %s %s %s %s" % (rev, action, state, owner, reviewer, requester, lease, req)
            + ((" " + words) if words else ""))


class AWorkRecordIsAdmittedNotTrusted(unittest.TestCase):
    """proj:work:v1: (Aya on PR 403 at da4488a): the actor comes from an authenticated transport, and the
    revision, the state, the owner and the evidence a move needs are checked INSIDE the governed write,
    under its WATCH, before the append and its audit. A generic raw write is not a way round it."""

    RID = "W-20261013-a1b2c3"
    REC, LOG, INDEX = "proj:work:v1:rec:" + RID, "proj:work:v1:log:" + RID, "proj:work:v1:index:20261013"
    LEASE = "20261013T0900Z"

    def add(self, conn, who, text, verified=True, via="bridge:test"):
        return run(conn, who, "xadd", self.LOG, val=text, via=via, verified=verified)

    DESCRIPTION = {"created": "2026-10-13T05:00:00Z", "done_when": "the suite is green", "kind": "task",
                   "link": "-", "project": "Blackboard", "requested_by": "cursor",
                   "source": "board row AYA-REDIS-STAGE1-EXIT-CRITERIA-20261013", "title": "Wire the reader"}

    def put(self, conn, who="cursor", requested_by=None, verified=True, key=None, **change):
        body = json.dumps(dict(self.DESCRIPTION, requested_by=who if requested_by is None else requested_by,
                               **change))
        return run(conn, who, "hput", key or self.REC, val=base64.b64encode(body.encode()).decode(), enc="b64",
                   verified=verified)

    def line(self, conn, text, who="grok", field=None, verified=True):
        return run(conn, who, "hset", self.INDEX, field=field or self.RID, val=text, verified=verified)

    def created(self):
        """The description, then r1: cursor asks grok. Returns the connection."""
        conn = FakeRedis()
        self.assertTrue(self.put(conn)["ok"])
        self.assertTrue(self.add(conn, "cursor", work_entry(1, "create", "requested"))["ok"])
        return conn

    def accepted(self):
        conn = self.created()
        self.assertTrue(self.add(conn, "grok", work_entry(2, "accept", "accepted", lease=self.LEASE,
                                                          words="next=read the spec"))["ok"])
        return conn

    def texts(self, conn):
        return [f["text"] for _, f in conn.data.get(self.LOG, [])]

    def refused_and_unchanged(self, conn, out, why):
        """The refusal named its reason, and the stream is exactly what it was."""
        self.assertFalse(out.get("ok"), out)
        self.assertIn(why, out.get("refused", ""), out)
        self.assertEqual(("0", why in conn.audit()[-1]["reason"]), (conn.audit()[-1]["ok"], True), "it is audited")

    # ------------------------------------------------- the actor comes from an authenticated transport
    def test_REGRESSION_a_claimed_identity_writes_nothing_here_whatever_its_tag_says(self):
        """Aya: "Board claimed Source_Tag/--as is not authenticated identity; refuse this protected
        transition path unless equivalently verified." The same legal move, from a caller nobody verified."""
        conn = self.created()
        before = self.texts(conn)
        legal = work_entry(2, "accept", "accepted", lease=self.LEASE, words="next=read the spec")
        out = self.add(conn, "grok", legal, verified=False, via="REQ-1")
        self.refused_and_unchanged(conn, out, "written only by an authenticated caller")
        self.assertEqual(before, self.texts(conn), "a claimed identity moved the record")
        self.assertIn("refused", self.put(FakeRedis(), verified=False))
        self.assertIn("refused", self.line(conn, "1 requested grok - task x", verified=False))
        self.assertTrue(self.add(conn, "grok", legal)["ok"], "the same move, from the verified owner")
        for op, key in (("xrange", self.LOG), ("hgetall", self.REC), ("hgetall", self.INDEX)):
            self.assertTrue(run(conn, "gemini", op, key)["ok"], "reading needs no verification")

    def test_the_board_worker_never_passes_for_verified(self):
        """Through bus_request.handle, the function the bus-requests job runs: a row from the owner's own
        tag asking for the owner's own legal move is answered REFUSED, and the log is untouched."""
        import datetime
        import bus_request
        conn = self.created()
        legal = work_entry(2, "accept", "accepted", lease=self.LEASE, words="next=read the spec")
        row = ["ROW-1", "2026-10-13T04:59:00Z", "grok", "bus-reconciler", "AYA_REQ",
               "BCB|v=1|id=REQ-0001|req=REQ-0001|do=redis-op|op=xadd|key=%s|val=%s|enc=b64"
               % (self.LOG, base64.b64encode(legal.encode()).decode()), "", "", "", ""]
        out = []
        bus_request.handle([row], conn, now=datetime.datetime(2026, 10, 13, 5, 0, tzinfo=datetime.timezone.utc),
                           append=out.append)
        self.assertEqual(1, len(out))
        self.assertIn("REFUSED: a work record is written only by an authenticated caller", out[0]["payload"])
        self.assertEqual(1, len(self.texts(conn)))
        self.assertNotIn("verified=", pathlib.Path(bus_request.__file__).read_text(encoding="utf-8"),
                         "the board worker must have no way to say its caller was verified")

    # ----------------------------------------------- unauthorized moves never reach the stream
    def test_REGRESSION_an_unauthorized_done_or_handoff_is_refused_before_the_stream_changes(self):
        """Aya: "unauthorized done/handoff ... refused BEFORE stream changes". Each is in sequence, well
        formed, and from a VERIFIED caller - who is simply not allowed to make that move."""
        conn = self.accepted()
        before = self.texts(conn)
        done = work_entry(3, "done", "done", words="evidence=https://example.test/1")
        theft = work_entry(3, "handoff", "requested", owner="gemini")
        for who, text, why in (("gemini", done, "only grok may done it, and this is gemini"),
                               ("cursor", done, "only grok may done it, and this is cursor"),    # the requester
                               ("gemini", theft, "only grok or cursor may handoff it, and this is gemini"),
                               ("gemini", work_entry(3, "start", "working", lease=self.LEASE), "only grok may start it"),
                               ("gemini", work_entry(3, "cancel", "cancelled", words="note=mine now"),
                                "only grok or cursor may cancel it")):
            out = self.add(conn, who, text)
            self.refused_and_unchanged(conn, out, why)
            self.assertEqual(before, self.texts(conn), (who, text))
        self.assertTrue(self.add(conn, "grok", work_entry(3, "done", "done", words="unreviewed evidence=https://x.test/1"))["ok"])

    def test_REGRESSION_a_forged_actor_is_refused_before_the_stream_changes(self):
        """An entry has no field for its actor: the actor is the verified caller and nothing else. What
        CAN be forged is who asked, at create and afterwards."""
        conn = FakeRedis()
        self.assertTrue(self.put(conn)["ok"])                         # cursor described it; gemini begins it "for cursor"
        out = self.add(conn, "gemini", work_entry(1, "create", "requested", requester="cursor"))
        self.refused_and_unchanged(conn, out, "by its requester")
        self.assertNotIn(self.LOG, conn.data, "a refused create made the stream")
        conn = self.accepted()
        out = self.add(conn, "grok", work_entry(3, "start", "working", requester="grok", lease=self.LEASE))
        self.refused_and_unchanged(conn, out, "this record's description names another requester")
        out = self.add(conn, "grok", work_entry(3, "start", "working", owner="nobody-at-all", lease=self.LEASE))
        self.refused_and_unchanged(conn, out, "are agents in the access list")
        self.assertEqual(2, len(self.texts(conn)))
        self.assertEqual(["cursor", "grok"], [f["by"] for _, f in conn.data[self.LOG]], "by is the verified caller")

    def test_REGRESSION_a_move_without_the_evidence_it_requires_is_refused(self):
        """Aya: "missing evidence refused"."""
        conn = self.created()
        out = self.add(conn, "grok", work_entry(2, "accept", "accepted", lease=self.LEASE))
        self.refused_and_unchanged(conn, out, "accept must carry next=")
        out = self.add(conn, "grok", work_entry(2, "accept", "accepted", lease=self.LEASE, words="next="))
        self.refused_and_unchanged(conn, out, "accept must carry next=")
        conn = self.accepted()
        for text, why in ((work_entry(3, "done", "done"), "done must carry evidence="),
                          (work_entry(3, "done", "done", words="note=trust me"), "done must carry evidence="),
                          (work_entry(3, "block", "blocked", lease=self.LEASE), "block must carry blocker="),
                          (work_entry(3, "cancel", "cancelled"), "cancel must carry note="),
                          (work_entry(3, "note", "accepted", lease=self.LEASE), "a note says something")):
            out = self.add(conn, "grok", text)
            self.refused_and_unchanged(conn, out, why)
        self.assertEqual(2, len(self.texts(conn)))
        self.assertEqual({"accept": "next", "block": "blocker", "return": "note", "done": "evidence",
                          "cancel": "note", "reopen": "note"},
                         {name: rule[3] for name, rule in gov.WORK_MOVES.items() if rule[3]})

    # ------------------------------------------------------------------------- the compare-and-set
    def test_REGRESSION_two_valid_next_revisions_have_one_winner(self):
        """Aya: "concurrent valid revisions one winner". Both read r1; both moves are legal from r1."""
        conn = self.created()
        accept = work_entry(2, "accept", "accepted", lease=self.LEASE, words="next=read the spec")
        cancel = work_entry(2, "cancel", "cancelled", words="note=no longer wanted")
        self.assertTrue(self.add(conn, "grok", accept)["ok"])
        out = self.add(conn, "cursor", cancel)
        self.refused_and_unchanged(conn, out, "revision conflict: the record is at r2")
        self.assertEqual([work_entry(1, "create", "requested"), accept], self.texts(conn))

    def test_it_is_decided_under_the_watch_not_before_it(self):
        """A rival r2 that lands between this write's replay and its EXEC: nothing of this one applies."""
        conn = self.created()
        real = conn.pipeline
        rival = work_entry(2, "cancel", "cancelled", words="note=no longer wanted")

        def racing_pipeline(transaction=True):
            pipe = real(transaction)
            original_multi = pipe.multi

            def multi():
                conn.xadd(self.LOG, {"text": rival, "by": "cursor", "at": "x"})         # after the look
                original_multi()
            pipe.multi = multi
            return pipe
        conn.pipeline = racing_pipeline
        audits = len(conn.audit())
        out = self.add(conn, "grok", work_entry(2, "accept", "accepted", lease=self.LEASE, words="next=x"))
        self.assertFalse(out["ok"])
        self.assertEqual(rival, self.texts(conn)[-1])
        self.assertEqual(2, len(self.texts(conn)))
        self.assertEqual((audits + 1, "0"), (len(conn.audit()), conn.audit()[-1]["ok"]), "no audit of a write that did not happen")

    def test_a_move_whose_answer_was_lost_cannot_land_after_someone_elses(self):
        conn = self.created()
        self.assertTrue(self.add(conn, "cursor", work_entry(2, "handoff", "requested", owner="gemini"),
                                 via="bridge:later")["ok"])
        late = self.add(conn, "grok", work_entry(2, "accept", "accepted", lease=self.LEASE, words="next=x"),
                        via="bridge:earlier")
        self.assertIn("revision conflict", late["refused"])
        self.assertEqual(2, len(self.texts(conn)))

    def test_what_is_not_an_entry_or_not_the_next_revision_is_refused(self):
        conn = FakeRedis()
        for bad in ("r1 create", "create requested grok - cursor - req", "r0 create requested grok - cursor - req",
                    "r01 create requested grok - cursor - req", "r10000 create requested grok - cursor - req",
                    "r1 dance requested grok - cursor - req", "r1 create sleeping grok - cursor - req",
                    "r1 create requested  grok - cursor - req", "R1 create requested grok - cursor - req", ""):
            out = self.add(conn, "cursor", bad)
            self.assertFalse(out.get("ok"), bad)
        self.assertNotIn(self.LOG, conn.data, "a refused first entry created the stream")
        out = self.add(conn, "cursor", work_entry(2, "create", "requested"))
        self.assertIn("revision conflict: the record is at r0", out["refused"])
        conn = self.accepted()
        out = self.add(conn, "grok", work_entry(4, "start", "working", lease=self.LEASE))
        self.assertIn("revision conflict: the record is at r2", out["refused"])

    # --------------------------------------------------- a tainted history is never built upon
    def test_REGRESSION_tainted_history_stays_disputed_after_a_legal_looking_tail(self):
        """Aya: "tainted history stays disputed after a legal-looking tail". The theft and the accept after
        it were written AROUND the governor, straight into Redis. The last pair is legal. The chain is not."""
        conn = self.accepted()
        theft = work_entry(3, "handoff", "requested", owner="gemini")
        tail = work_entry(4, "accept", "accepted", owner="gemini", lease=self.LEASE, words="next=mine now")
        conn.xadd(self.LOG, {"text": theft, "by": "gemini", "at": "x"})          # illegal: gemini holds nothing
        conn.xadd(self.LOG, {"text": tail, "by": "gemini", "at": "x"})           # legal after it, on its face
        prev, last = gov.work_event(theft, "gemini"), gov.work_event(tail, "gemini")
        self.assertEqual("", gov.work_allowed(prev, last), "the last pair alone is a legal move")
        before = self.texts(conn)
        for who, text in (("gemini", work_entry(5, "start", "working", owner="gemini", lease=self.LEASE)),
                          ("cursor", work_entry(5, "cancel", "cancelled", owner="gemini", words="note=stop")),
                          ("grok", work_entry(5, "note", "accepted", owner="gemini", lease=self.LEASE, words="what"))):
            out = self.add(conn, who, text)
            self.refused_and_unchanged(conn, out, "this record is disputed at entry 3-0 (only grok or cursor may handoff it")
        self.assertEqual(before, self.texts(conn), "history is preserved exactly, for reconciliation")
        out = self.line(conn, "4 accepted gemini %s task Wire the reader" % self.LEASE, who="gemini")
        self.assertIn("this record is disputed at entry 3-0", out.get("refused", ""),
                      "its list line, exactly what the tainted tail says, followed the theft")

    def test_a_log_whose_entries_are_not_entries_takes_nothing_more(self):
        conn = FakeRedis()
        conn.xadd(self.LOG, {"text": "written around the governor", "by": "x", "at": "x"})
        out = self.add(conn, "cursor", work_entry(2, "note", "requested", words="hello"))
        self.assertIn("disputed at entry 1-0 (it is not a work-record entry)", out["refused"])

    # ---------------------------------- a log is bound to its description: no takeover, no orphan
    def test_REGRESSION_takeover_nobody_begins_a_record_another_agent_described(self):
        """Aya on 647b0d9: "Authenticated B can append r1 under A's record ID between description write and
        first log ... because the governor never reads/watches the record." cursor has described the record
        and not yet begun it. gemini, verified, begins it as its own."""
        conn = FakeRedis()
        self.assertTrue(self.put(conn)["ok"])                         # cursor's description
        for text, why in ((work_entry(1, "create", "requested", owner="gemini", requester="gemini"),
                           "this record's description names another requester"),
                          (work_entry(1, "create", "requested", owner="grok", requester="gemini"),
                           "this record's description names another requester")):
            out = self.add(conn, "gemini", text)
            self.refused_and_unchanged(conn, out, why)
            self.assertNotIn(self.LOG, conn.data, "another agent began the record")
        self.assertTrue(self.add(conn, "cursor", work_entry(1, "create", "requested"))["ok"], "its describer begins it")
        self.assertEqual("cursor", conn.data[self.LOG][0][1]["by"])

    def test_REGRESSION_the_describer_completes_a_partial_create_without_overwrite_or_substitution(self):
        """Aya: "legitimate A completion after partial create. Keep valid recovery without overwrite/
        identity substitution." The description landed and the first entry did not."""
        conn = FakeRedis()
        self.assertTrue(self.put(conn)["ok"])
        described = dict(conn.data[self.REC])
        self.assertIn("written once", self.put(conn, title="A different ask")["refused"], "recovery by overwrite")
        self.assertIn("refused", self.put(conn, who="gemini"), "recovery by another identity")
        self.assertIn("refused", self.add(conn, "gemini", work_entry(1, "create", "requested", requester="gemini")))
        self.assertEqual(described, conn.data[self.REC])
        self.assertNotIn(self.LOG, conn.data)
        self.assertTrue(self.add(conn, "cursor", work_entry(1, "create", "requested"))["ok"], "A finishes its own create")
        self.assertEqual(described, conn.data[self.REC], "finishing it rewrote the description")

    def test_REGRESSION_orphan_a_log_never_begins_without_its_description(self):
        conn = FakeRedis()
        out = self.add(conn, "cursor", work_entry(1, "create", "requested"))
        self.refused_and_unchanged(conn, out, "this record has no description")
        self.assertNotIn(self.LOG, conn.data, "an orphan log was created")
        conn.set(self.REC, "not a hash")                              # something else squats the key
        self.assertIn("has no description", self.add(conn, "cursor", work_entry(1, "create", "requested"))["refused"])
        self.assertNotIn(self.LOG, conn.data)

    def test_a_description_that_is_not_one_admits_nothing_and_every_later_move_checks_it_too(self):
        conn = FakeRedis()
        conn.hset(self.REC, mapping={"title": "left by something else", "requested_by": "cursor"})
        out = self.add(conn, "cursor", work_entry(1, "create", "requested"))
        self.assertIn("this record's description is not one (a description has exactly these fields", out["refused"])
        self.assertNotIn(self.LOG, conn.data)
        conn = self.accepted()
        conn.data[self.REC]["requested_by"] = "gemini"                # the description altered around the governor
        out = self.add(conn, "grok", work_entry(3, "start", "working", lease=self.LEASE))
        self.refused_and_unchanged(conn, out, "this record's description names another requester")
        self.assertEqual(2, len(self.texts(conn)))
        del conn.data[self.REC]                                       # or removed around it
        out = self.add(conn, "grok", work_entry(3, "start", "working", lease=self.LEASE))
        self.refused_and_unchanged(conn, out, "this record has no description")

    def test_the_description_is_read_under_the_same_watch_as_the_move(self):
        """The description changes between this write's look at it and its EXEC: nothing applies."""
        conn = FakeRedis()
        self.assertTrue(self.put(conn)["ok"])
        real = conn.pipeline

        def racing_pipeline(transaction=True):
            pipe = real(transaction)
            original_multi = pipe.multi

            def multi():
                conn.hset(self.REC, "requested_by", "gemini")            # after the look, before the EXEC
                original_multi()
            pipe.multi = multi
            return pipe
        conn.pipeline = racing_pipeline
        out = self.add(conn, "cursor", work_entry(1, "create", "requested"))
        self.assertFalse(out["ok"])
        self.assertIn("nothing was written", out["refused"])
        self.assertNotIn(self.LOG, conn.data, "a log began under a description that changed beneath it")

    # ----------------------------------------------------------- no raw bypass: the three keys
    def test_a_description_is_written_once_by_its_verified_requester_and_never_rewritten_or_removed(self):
        conn = FakeRedis()
        out = self.put(conn, who="gemini", requested_by="cursor")                 # "cursor asked for this"
        self.assertIn("names its verified writer as requested_by", out["refused"])
        self.assertNotIn(self.REC, conn.data)
        for change, why in (({"kind": "epic"}, "kind is task or issue"), ({"title": ""}, "title is empty"),
                            ({"title": "t" * 201}, "longer than a read shows whole"),
                            ({"source": "a | b"}, "longer than a read shows whole"),
                            ({"owner": "grok"}, "exactly these fields")):
            out = self.put(conn, **change)
            self.assertIn(why, out.get("refused", ""), change)
        body = {k: v for k, v in self.DESCRIPTION.items() if k != "done_when"}        # no definition of done
        out = run(conn, "cursor", "hput", self.REC, val=base64.b64encode(json.dumps(body).encode()).decode(),
                  enc="b64", verified=True)
        self.assertIn("exactly these fields", out["refused"])
        self.assertNotIn(self.REC, conn.data)
        self.assertTrue(self.put(conn)["ok"])
        self.assertIn("written once", self.put(conn)["refused"])
        for op, kw in (("hset", {"field": "title", "val": "x"}), ("del", {}), ("set", {"val": "x"}),
                       ("xadd", {"val": "r1 x"})):
            out = run(conn, "cursor", op, self.REC, verified=True, **kw)
            self.assertIn("this key takes hput and nothing else", out.get("refused", ""), op)
        self.assertEqual("Wire the reader", conn.data[self.REC]["title"])

    def test_a_log_is_append_only(self):
        conn = self.created()
        for op, kw in (("del", {}), ("set", {"val": "x"}), ("hset", {"field": "a", "val": "x"})):
            out = run(conn, "cursor", op, self.LOG, verified=True, **kw)
            self.assertIn("this key takes xadd and nothing else", out.get("refused", ""), op)
        self.assertEqual(1, len(conn.data[self.LOG]))
        fresh = FakeRedis()                                  # nor may a key be squatted before its log exists
        self.assertIn("refused", run(fresh, "grok", "set", self.LOG, val="x", verified=True))
        self.assertNotIn(self.LOG, fresh.data)

    def test_a_list_line_says_what_the_log_says_or_it_is_refused(self):
        conn = self.created()
        self.assertTrue(self.line(conn, "1 requested grok - task Wire the reader")["ok"])
        for lie, why in (("1 done grok - task Wire the reader", "it is at r1 requested"),
                         ("1 requested gemini - task Wire the reader", "it is at r1 requested"),
                         ("9 requested grok - task Wire the reader", "it is at r1 requested"),
                         ("1 requested grok - issue Wire the reader", "it is at r1 requested"),      # another kind
                         ("1 requested grok - task Something far more urgent", "it is at r1 requested"),
                         ("1 requested grok - task Wire the reader and more", "it is at r1 requested"),
                         ("requested grok", "it is at r1 requested")):
            self.assertIn(why, self.line(conn, lie)["refused"], lie)
        self.assertTrue(self.add(conn, "grok", work_entry(2, "accept", "accepted", lease=self.LEASE, words="next=x"))["ok"])
        self.assertIn("it is at r2 accepted", self.line(conn, "1 requested grok - task Wire the reader")["refused"],
                      "a stale line that arrives late cannot take the list back")
        self.assertTrue(self.line(conn, "2 accepted grok %s task Wire the reader" % self.LEASE)["ok"])
        self.assertTrue(self.line(conn, "2 accepted grok %s task Wire the reader" % self.LEASE)["ok"], "a repair")
        out = self.line(conn, "1 requested grok - task x", field="W-20261013-ffffff")
        self.assertIn("only for a record whose log exists", out["refused"])
        for op, kw in (("del", {}), ("hput", {"val": base64.b64encode(b'{"a": "1 x"}').decode(), "enc": "b64"})):
            self.assertIn("this key takes hset and nothing else",
                          run(conn, "grok", op, self.INDEX, verified=True, **kw)["refused"])

    def test_the_record_is_given_no_expiry_and_its_list_is(self):
        conn = self.created()
        self.assertTrue(self.line(conn, "1 requested grok - task Wire the reader")["ok"])
        self.assertNotIn(self.REC, conn.ttls, "the description was given an expiry")
        self.assertNotIn(self.LOG, conn.ttls, "the only audit record was given an expiry")
        self.assertEqual(ACL["ttl_seconds"]["proj:"], conn.ttls[self.INDEX], "the list is a cache")
        self.assertTrue(gov.ttl_for(ACL, self.LOG), "the access list would have expired it; the code does not")

    # ------------------------------------------------------------------------------- the edges
    def test_only_these_three_prefixes_and_no_access_list_can_change_them(self):
        self.assertEqual((("proj:work:v1:rec:",), ("proj:work:v1:log:",), ("proj:work:v1:index:",)),
                         (gov.WRITE_ONCE, gov.SEQUENCED, gov.REV_FORWARD))
        conn = FakeRedis()
        for key in ("proj:work:v2:log:x", "proj:pr-board:v1:log:x", "proj:worklog:x", "fleet:inbox:grok"):
            self.assertTrue(run(conn, "grok", "xadd", key, val="no revision here")["ok"],
                            "%s is not held to these rules and needs no verified caller" % key)
            self.assertTrue(conn.ttls.get(key), key)
        self.assertTrue(run(conn, "grok", "hset", "proj:pr-board:v1:index:20261013", field="a", val="anything")["ok"])
        loose = dict(ACL, WRITE_ONCE=[], SEQUENCED=[], REV_FORWARD=[], ledger=False, verified=True)
        self.assertIn("refused", run(conn, "cursor", "xadd", self.LOG, val=work_entry(1, "create", "requested"), acl=loose))
        self.assertIn("refused", run(conn, "nobody", "xadd", self.LOG, val=work_entry(1, "create", "requested",
                                                                               requester="nobody"), verified=True),
                      "the access list still decides who is anyone at all")

    def test_verified_is_true_and_nothing_else(self):
        conn = FakeRedis()
        for almost in (1, "true", "yes", [1], object()):
            out = gov.execute(conn, ACL, "cursor", "x", "xadd", self.LOG, raw_value=work_entry(1, "create", "requested"),
                              verified=almost)
            self.assertIn("authenticated caller", out.get("refused", ""), repr(almost))
        self.assertNotIn(self.LOG, conn.data)

    def test_the_rules_table_one_legal_life_and_what_each_step_refuses(self):
        one = gov.work_event(work_entry(1, "create", "requested"), "cursor")
        self.assertEqual("", gov.work_allowed(None, one))
        for change, why in (({"by": "grok"}, "by its requester"), ({"rev": 2}, "r1 create"),
                            ({"state": "done"}, "created requested"), ({"lease": self.LEASE}, "no lease"),
                            ({"reviewer": "grok"}, "reviewer cannot be the owner")):
            self.assertIn(why, gov.work_allowed(None, dict(one, **change)), change)
        accept = gov.work_event(work_entry(2, "accept", "accepted", lease=self.LEASE, words="next=x"), "grok")
        self.assertEqual("", gov.work_allowed(one, accept))
        for change, why in (({"rev": 3}, "does not follow"), ({"requester": "grok"}, "requester of a record never"),
                            ({"owner": "gemini"}, "only a handoff changes the owner"), ({"lease": "-"}, "sets a lease"),
                            ({"lease": "soon"}, "sets a lease"), ({"reviewer": "gemini"}, "changes the reviewer"),
                            ({"state": "working"}, "ends in accepted"), ({"action": "fly"}, "is not a move")):
            self.assertIn(why, gov.work_allowed(one, dict(accept, **change)), change)
        start = gov.work_event(work_entry(2, "start", "working"), "grok")
        self.assertIn("start starts from accepted, not from requested", gov.work_allowed(one, start))
        conn = self.created()                                         # and the store says the same
        self.refused_and_unchanged(conn, self.add(conn, "grok", work_entry(2, "start", "working")),
                                   "start starts from accepted, not from requested")
        review = gov.work_event(work_entry(3, "review", "review", reviewer="gemini", lease=self.LEASE), "grok")
        self.assertEqual("", gov.work_allowed(accept, review))
        self.assertIn("a reviewer who is not the owner", gov.work_allowed(accept, dict(review, reviewer="grok")))
        done = gov.work_event(work_entry(4, "done", "done", reviewer="gemini", words="evidence=https://x.test/1"), "gemini")
        self.assertEqual("", gov.work_allowed(review, done))
        self.assertIn("only gemini may done it", gov.work_allowed(review, dict(done, by="grok")))
        self.assertIn("a closed record holds no lease", gov.work_allowed(review, dict(done, lease=self.LEASE)))
        reopen = gov.work_event(work_entry(5, "reopen", "requested", words="note=it broke"), "cursor")
        self.assertEqual("", gov.work_allowed(done, reopen))
        self.assertIn("only cursor may reopen it", gov.work_allowed(done, dict(reopen, by="grok")))
        note = gov.work_event(work_entry(6, "note", "requested", words="anyone may say something"), "gemini")
        self.assertEqual("", gov.work_allowed(reopen, note))
        self.assertIn("a note moves nothing", gov.work_allowed(reopen, dict(note, state="done")))
        self.assertIsNone(gov.work_lease("-"))
        self.assertIsNone(gov.work_lease("20261399T9999Z"))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

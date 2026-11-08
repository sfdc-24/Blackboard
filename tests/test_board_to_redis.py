"""Board rows addressed to an agent land in that agent's Redis lane. FakeRedis, no network.

BOARD_TO_REDIS must be exactly "on". Anything else, the live loop does not take the mirror and
writes nothing under fleet:inbox or fleet:wake:mirrored.
"""
import datetime
import hashlib
import hmac
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "cloud" / "bus-reconciler"))

import redis_gov                                                          # noqa: E402
import serve_requests as sr                                                # noqa: E402

WORDS = "Codex, draft the sample queue flow for the vans."
START = datetime.datetime(2026, 11, 8, 3, 0, tzinfo=datetime.timezone.utc)


def row(rid, target, payload, source="grok", ts="2026-11-08T03:00:00Z"):
    return [rid, ts, source, target, "ASK", payload, "", "", "", ""]


_LUA_CALL = re.compile(r"^redis\.(call|pcall)\((.*)\)$")
_LUA_ARG = re.compile(r"'[^']*'|KEYS\[\d+\]|ARGV\[\d+\]|unpack\(ARGV, \d+\)")
_LUA_IF_NOT = re.compile(r"^if not (redis\.\w+\(.*\)) then return (\d+) end$")
_LUA_LOCAL = re.compile(r"^local (\w+) = (redis\.\w+\(.*\))$")
_LUA_IF_ERR = re.compile(r"^if type\((\w+)\) == 'table' and (\w+)\.err then$")
_LUA_RETURN = re.compile(r"^return (\w+)$")


class FakeRedis:
    """SET NX/EX, XADD (with MAXLEN), EXPIRE and DEL, and the worker-lock eval the live loop needs.
    No network.

    Any other script is RUN, line by line, by a small interpreter that knows only the statements
    the mirror script uses and refuses any other line. So the fake does what the script text says:
    a script without NX, or without the DEL on a failed XADD, behaves that way here too. As on a
    real server, redis.call raising aborts the script with its earlier writes kept, and redis.pcall
    turns the error into a table. A script that returns that table raises the original exception
    here (redis-py raises ResponseError), so a test can see what failed.

    fail_xadd maps a stream key to how many XADDs on it raise before one succeeds."""

    def __init__(self):
        self.data, self.streams, self.expires, self.calls = {}, {}, {}, []
        self.fail_xadd, self.trims = {}, []

    def set(self, key, value, nx=False, ex=None):
        self.calls.append(("set", key, value, nx, ex))
        if nx and key in self.data:
            return None
        self.data[key] = value
        if ex is not None:
            self.expires[key] = int(ex)
        return True

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.calls.append(("del", key))
        self.expires.pop(key, None)
        return 1 if self.data.pop(key, None) is not None else 0

    def xadd(self, key, fields, maxlen=None, approximate=False):
        """Appends, then trims to the newest `maxlen` entries. A real `~` trim may keep a few more;
        the cap asked for is recorded in `trims`, which is what a test checks."""
        self.calls.append(("xadd", key, dict(fields)))
        if self.fail_xadd.get(key):
            self.fail_xadd[key] -= 1
            raise RuntimeError("%s %s" % (key, WORDS))
        self.trims.append((key, maxlen, approximate))
        stream = self.streams.setdefault(key, [])
        stream.append(dict(fields))
        if maxlen is not None:
            del stream[:max(0, len(stream) - int(maxlen))]
        return "%d-0" % len(stream)

    def expire(self, key, seconds):
        self.calls.append(("expire", key, int(seconds)))
        if key not in self.data and key not in self.streams:
            return 0
        self.expires[key] = int(seconds)
        return 1

    def eval(self, script, numkeys, *args):
        keys, argv = list(args[:numkeys]), [str(a) for a in args[numkeys:]]
        self.calls.append(("eval", keys[0]))
        if script == sr.RENEW_LUA:
            if self.data.get(keys[0]) != argv[0]:
                return None
            self.data[keys[0]] = argv[0]
            return "OK"
        if script == sr.DROP_LUA:
            if self.data.get(keys[0]) != argv[0]:
                return 0
            self.data.pop(keys[0], None)
            return 1
        return self._run(script, keys, argv)

    def _command(self, text, keys, argv):
        found = _LUA_CALL.match(text)
        if not found:
            raise AssertionError("fake cannot run: %s" % text)
        protected, raw = found.group(1) == "pcall", found.group(2)
        tokens = _LUA_ARG.findall(raw)
        if ", ".join(tokens) != raw:
            raise AssertionError("fake cannot read the arguments: %s" % raw)
        values = []
        for token in tokens:
            if token.startswith("'"):
                values.append(token[1:-1])
            elif token.startswith("KEYS["):
                values.append(keys[int(token[5:-1]) - 1])
            elif token.startswith("ARGV["):
                values.append(argv[int(token[5:-1]) - 1])
            else:
                values.extend(argv[int(token[len("unpack(ARGV, "):-1]) - 1:])
        try:
            return self._redis(values)
        except Exception as error:                                       # noqa: BLE001
            if not protected:
                raise
            return {"err": str(error), "error": error}

    def _redis(self, values):
        name, rest = values[0].upper(), values[1:]
        if name == "SET":
            key, value, opts = rest[0], rest[1], [o.upper() for o in rest[2:]]
            ex = int(rest[2 + opts.index("EX") + 1]) if "EX" in opts else None
            return self.set(key, value, nx="NX" in opts, ex=ex)
        if name == "XADD":
            key, rest = rest[0], rest[1:]
            maxlen, approximate = None, False
            if rest and rest[0].upper() == "MAXLEN":
                approximate = rest[1] == "~"
                if rest[1] in ("~", "="):
                    rest = rest[1:]
                maxlen, rest = int(rest[1]), rest[2:]
            ident, pairs = rest[0], rest[1:]
            if ident != "*" or len(pairs) % 2:
                raise AssertionError("fake XADD takes [MAXLEN [~] n] '*' and field/value pairs")
            return self.xadd(key, dict(zip(pairs[0::2], pairs[1::2])), maxlen=maxlen,
                             approximate=approximate)
        if name == "EXPIRE":
            if len(rest) != 2:
                raise AssertionError("fake EXPIRE takes a key and seconds")
            return self.expire(rest[0], rest[1])
        if name == "DEL":
            return self.delete(rest[0])
        raise AssertionError("fake has no %s" % name)

    def _run(self, script, keys, argv):
        lines = [line.strip() for line in script.strip().splitlines() if line.strip()]
        local, skipping, index = {}, False, 0
        while index < len(lines):
            line = lines[index]
            index += 1
            if skipping:
                skipping = line != "end"
                continue
            if line == "end":
                continue
            found = _LUA_IF_NOT.match(line)
            if found:
                if self._command(found.group(1), keys, argv) in (None, False):
                    return int(found.group(2))
                continue
            found = _LUA_LOCAL.match(line)
            if found:
                local[found.group(1)] = self._command(found.group(2), keys, argv)
                continue
            found = _LUA_IF_ERR.match(line)
            if found and found.group(1) == found.group(2):
                value = local.get(found.group(1))
                skipping = not (isinstance(value, dict) and "err" in value)
                continue
            found = _LUA_RETURN.match(line)
            if found:
                name = found.group(1)
                if name.isdigit():
                    return int(name)
                value = local[name]
                if isinstance(value, dict) and "err" in value:
                    raise value["error"]
                return value
            self._command(line, keys, argv)
        return None

    def inbox(self, agent):
        return self.streams.get("fleet:inbox:%s" % agent, [])

    def dumped(self):
        parts = []
        for key, value in self.data.items():
            parts.append("%s=%s" % (key, value))
        for key, entries in self.streams.items():
            parts.append(key)
            for entry in entries:
                parts.extend("%s=%s" % pair for pair in entry.items())
        return "\n".join(parts)


def fleet_ttl():
    return redis_gov.ttl_for(redis_gov.load_acl(), "fleet:")


def principals():
    return list(redis_gov.load_acl()["principals"])


class BoardToRedis(unittest.TestCase):
    def setUp(self):
        self.conn = FakeRedis()

    def mirror(self, rows):
        return sr.mirror_addressed(self.conn, rows)

    def test_one_entry_per_named_agent(self):
        payload = ("BCB|v=1|id=ROW-1|phase=ASK|class=TASK|from=grok|to=cursor|msg_type=REVIEW_ASK"
                   "|thread_id=TH-Q2-9|text=please look")
        self.mirror([row("ROW-1", "grok; cursor", payload, source="Grok")])
        self.assertEqual(["grok", "cursor"], [k.split(":")[-1] for k in self.conn.streams])
        for agent in ("grok", "cursor"):
            self.assertEqual(1, len(self.conn.inbox(agent)))
            entry = self.conn.inbox(agent)[0]
            self.assertEqual({
                "by": "Grok", "row_id": "ROW-1", "at": "2026-11-08T03:00:00Z",
                "phase": "ASK", "thread_id": "TH-Q2-9", "msg_type": "REVIEW_ASK", "class": "TASK",
            }, entry)
            mark = "fleet:wake:mirrored:ROW-1:%s" % agent
            self.assertEqual("1", self.conn.data[mark])
            self.assertEqual(fleet_ttl(), self.conn.expires[mark])
        sets = [c for c in self.conn.calls if c[0] == "set"]
        adds = [c for c in self.conn.calls if c[0] == "xadd"]
        self.assertEqual(["grok", "cursor"], [c[1].split(":")[-1] for c in sets])
        self.assertTrue(all(c[3] is True for c in sets), "the mark is SET NX")
        self.assertLess(self.conn.calls.index(sets[0]), self.conn.calls.index(adds[0]))
        self.assertLess(self.conn.calls.index(sets[1]), self.conn.calls.index(adds[1]))

    def test_no_payload_text_reaches_the_lane(self):
        """Metadata only. The entry holds no word of the payload's text, and no 6-character run of
        it, beyond the allowed fields' own values."""
        secret = "FAKE-TOKEN-" + "Zq7Lx2Vb9Kd4Nw8Rt5Ym3Hc6"           # not a real credential format
        private = "the guest's home address is on the second page of the contract"
        payload = ("BCB|v=1|id=ROW-P|phase=ASK|from=grok|to=cursor|thread_id=TH-Q2-1"
                   "|text=" + private + " token " + secret + " " + WORDS)
        self.mirror([row("ROW-P", "grok", payload), row("ROW-RAW", "grok", private + " " + secret)])
        entries = self.conn.inbox("grok")
        self.assertEqual(["ROW-P", "ROW-RAW"], [e["row_id"] for e in entries])
        allowed = {"by", "row_id", "at", "phase", "thread_id", "msg_type", "class"}
        for entry in entries:
            self.assertLessEqual(set(entry), allowed)
            self.assertNotIn("preview", entry)
        held = self.conn.dumped()
        values = " ".join(v for e in entries for v in e.values())
        for text in (secret, private, WORDS, "Zq7Lx2", "address", "contract"):
            self.assertNotIn(text, held)
        for source in (private + " token " + secret + " " + WORDS,):
            runs = {source[i:i + 6] for i in range(len(source) - 5)}
            self.assertEqual(set(), {run for run in runs if run in values})
        self.assertEqual({"by": "grok", "row_id": "ROW-RAW", "at": "2026-11-08T03:00:00Z", "phase": ""},
                         entries[1])

    def test_unsafe_ids_and_fields_are_dropped(self):
        """thread_id, msg_type and class travel only when each fully matches its short pattern.
        by and at blank when unsafe. A row id that is not a safe id skips the row."""
        cases = [
            "thread_id=thread-9", "thread_id=TH-has space", "thread_id=TH-lower",
            "thread_id=TH-" + "A" * 65, "thread_id=TH-", "thread_id=TH-OK\nmore",
        ]
        for i, field in enumerate(cases):
            conn = FakeRedis()
            payload = "BCB|v=1|phase=ask now|msg_type=call the owner|class=" + "C" * 33 + "|" + field
            sr.mirror_addressed(conn, [row("ROW-%d" % i, "grok", payload,
                                           source="grok said hi", ts="tomorrow morning")])
            entry = conn.inbox("grok")[0]
            self.assertEqual({"by": "", "row_id": "ROW-%d" % i, "at": "", "phase": ""}, entry, field)
        conn = FakeRedis()
        sr.mirror_addressed(conn, [row("ROW with words", "grok", "ping"),
                                   row("ROW-" + "9" * 200, "grok", "ping")])
        self.assertEqual({}, conn.streams)
        self.assertEqual({}, conn.data)

    def test_all_and_fleet_expand_to_every_principal_once(self):
        self.mirror([row("ROW-ALL", "ALL", "hello fleet")])
        self.assertEqual(["fleet:inbox:%s" % name for name in principals()], list(self.conn.streams))
        self.conn.streams.clear()
        self.conn.data.clear()
        self.mirror([row("ROW-FLEET", "fleet, all, grok", "hello again")])
        self.assertEqual(set(principals()), {k.split(":")[-1] for k in self.conn.streams})
        self.assertTrue(all(len(entries) == 1 for entries in self.conn.streams.values()))

    def test_aliases_resolve_to_the_principal(self):
        expect = {
            "codex": "chatgpt-codex-desktop",
            "aya": "chatgpt-codex-desktop",
            "dot": "chatgpt-codex-desktop",
            "codex-desktop": "chatgpt-codex-desktop",
            "chatgpt-codex": "chatgpt-codex-desktop",
            "grok-bot": "grok",
            "vm-claude-code-cli": "claude-code-cli",
            "salam": "owner",
        }
        for name, principal in expect.items():
            conn = FakeRedis()
            sr.mirror_addressed(conn, [row("ROW-%s" % name, name, "ping")])
            self.assertEqual(["fleet:inbox:%s" % principal], list(conn.streams), name)
            self.assertEqual(1, len(conn.inbox(principal)), name)
        conn = FakeRedis()
        sr.mirror_addressed(conn, [row("ROW-BOTH", "codex; chatgpt-codex-desktop", "ping")])
        self.assertEqual(1, len(conn.inbox("chatgpt-codex-desktop")))
        conn = FakeRedis()
        sr.mirror_addressed(conn, [row("ROW-NO", "not-a-principal", "ping")])
        self.assertEqual({}, conn.streams)
        self.assertEqual({}, conn.data)

    def test_an_inbox_wake_alias_resolves_with_the_acl_alias_table_empty(self):
        real = redis_gov.load_acl()
        stripped = dict(real, aliases={})
        with mock.patch.object(sr.redis_gov, "load_acl", return_value=stripped):
            self.mirror([row("ROW-CODEX", "Codex", "ping")])
        self.assertEqual(["fleet:inbox:chatgpt-codex-desktop"], list(self.conn.streams))

    def test_a_reread_adds_nothing(self):
        rows = [row("ROW-1", "grok;cursor", "please look")]
        self.assertEqual(2, self.mirror(rows))
        self.assertEqual(0, self.mirror(rows))
        self.assertEqual(1, len(self.conn.inbox("grok")))
        self.assertEqual(1, len(self.conn.inbox("cursor")))
        self.assertEqual(2, len([c for c in self.conn.calls if c[0] == "xadd"]))

    def test_a_copy_gives_the_lane_the_fleet_ttl_and_refreshes_an_old_one(self):
        """As a governed xadd does: EXPIRE the lane to the access list's ttl for that key. A lane
        that already existed with a shorter ttl gets the full one back. The mark keeps its own EX."""
        acl = redis_gov.load_acl()
        lane_ttl = redis_gov.ttl_for(acl, "fleet:inbox:grok")
        self.assertTrue(lane_ttl)
        self.conn.streams["fleet:inbox:grok"] = [{"row_id": "OLD"}]
        self.conn.expires["fleet:inbox:grok"] = 5
        self.assertEqual(2, self.mirror([row("ROW-1", "grok; cursor", "please look")]))
        for agent in ("grok", "cursor"):
            self.assertEqual(lane_ttl, self.conn.expires["fleet:inbox:%s" % agent], agent)
            mark = "fleet:wake:mirrored:ROW-1:%s" % agent
            self.assertEqual(redis_gov.ttl_for(acl, mark), self.conn.expires[mark], agent)
        self.assertEqual(["OLD", "ROW-1"], [e["row_id"] for e in self.conn.inbox("grok")])

    def test_the_lane_is_trimmed_at_the_governed_cap(self):
        """MAXLEN ~ redis_gov.STREAM_MAXLEN, the cap a governed xadd applies, read from redis_gov."""
        self.mirror([row("ROW-1", "grok", "please look")])
        self.assertEqual([("fleet:inbox:grok", redis_gov.STREAM_MAXLEN, True)], self.conn.trims)
        conn = FakeRedis()
        with mock.patch.object(sr.redis_gov, "STREAM_MAXLEN", 3):
            sr.mirror_addressed(conn, [row("ROW-%d" % i, "grok", "ping") for i in range(5)])
        self.assertEqual(["ROW-2", "ROW-3", "ROW-4"], [e["row_id"] for e in conn.inbox("grok")])
        self.assertEqual({3}, {cap for _, cap, _ in conn.trims})

    def test_the_largest_entry_fits_the_governed_value_cap(self):
        """Every field at its pattern's longest still fits what a governed write may store there."""
        acl = redis_gov.load_acl()
        payload = "|".join(["BCB", "phase=" + "P" * 32, "thread_id=TH-" + "9" * 64,
                            "msg_type=" + "M" * 32, "class=" + "C" * 32])
        self.mirror([row("R" * 128, "grok", payload, source="s" * 64, ts="2" * 40)])
        entry = self.conn.inbox("grok")[0]
        self.assertEqual({"by", "row_id", "at", "phase", "thread_id", "msg_type", "class"}, set(entry))
        size = sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in entry.items())
        self.assertLessEqual(size, redis_gov.value_cap(acl, "fleet:inbox:grok"))

    def test_the_mark_and_the_entry_are_one_script(self):
        """No SET or XADD from Python: both travel in one EVAL, so a lost reply or a dead process
        between them cannot leave a mark without its entry."""
        self.mirror([row("ROW-1", "grok", "please look")])
        evals = [c for c in self.conn.calls if c[0] == "eval"]
        self.assertEqual([("eval", "fleet:wake:mirrored:ROW-1:grok")], evals)
        self.assertEqual("eval", self.conn.calls[0][0], "the SET is inside the script")

    def test_a_failed_copy_is_retried_and_delivered_once(self):
        """The XADD fails on the first pass. The mark goes with it, so the next pass delivers the
        row once, and the pass after that adds nothing."""
        self.conn.fail_xadd = {"fleet:inbox:grok": 1}
        rows = [row("ROW-1", "grok", WORDS)]
        mark = "fleet:wake:mirrored:ROW-1:grok"
        with mock.patch.object(sr.log, "warning") as warning:
            self.assertEqual(0, self.mirror(rows))
        self.assertNotIn(mark, self.conn.data, "a failed XADD leaves no mark behind")
        self.assertEqual([], self.conn.inbox("grok"))
        text = " ".join(str(call) for call in warning.call_args_list)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(WORDS, text)
        self.assertNotIn("fleet:", text)
        self.assertEqual(1, self.mirror(rows))
        self.assertEqual(0, self.mirror(rows))
        self.assertEqual(["ROW-1"], [e["row_id"] for e in self.conn.inbox("grok")])
        self.assertEqual("1", self.conn.data[mark])
        self.assertEqual(fleet_ttl(), self.conn.expires[mark])
        self.assertEqual(2, len([c for c in self.conn.calls if c[0] == "xadd"]))

    def test_a_failure_for_one_agent_still_delivers_to_the_next(self):
        """grok's XADD fails. cursor still gets this row, the next row is still copied, and grok's
        copy arrives on the next pass, once."""
        self.conn.fail_xadd = {"fleet:inbox:grok": 1}
        rows = [row("ROW-1", "grok; cursor", "please look"), row("ROW-2", "cursor", "next row")]
        with mock.patch.object(sr.log, "warning") as warning:
            self.assertEqual(2, self.mirror(rows))
        self.assertEqual(["ROW-1", "ROW-2"], [e["row_id"] for e in self.conn.inbox("cursor")])
        self.assertEqual([], self.conn.inbox("grok"))
        self.assertNotIn("fleet:wake:mirrored:ROW-1:grok", self.conn.data)
        text = " ".join(str(call) for call in warning.call_args_list)
        self.assertEqual(1, len(warning.call_args_list))
        self.assertIn("RuntimeError", text)
        self.assertNotIn(WORDS, text)
        self.assertNotIn("fleet:", text)
        self.assertEqual(1, self.mirror(rows))
        self.assertEqual(["ROW-1"], [e["row_id"] for e in self.conn.inbox("grok")])
        self.assertEqual(2, len(self.conn.inbox("cursor")))

    def test_call_bridge_rows_put_no_words_in_the_lane(self):
        rows = [
            row("ROW-ASK", "cursor", "CALL ASK call-1 from the owner: " + WORDS),
            row("ROW-BY", "cursor", "by=call-bridge|text=" + WORDS, source="grok"),
            row("ROW-SRC", "cursor", WORDS, source="call-bridge"),
            row("ROW-PLAIN", "grok", WORDS, source="grok"),
        ]
        self.assertEqual(4, self.mirror(rows))
        self.assertEqual(["ROW-ASK", "ROW-BY", "ROW-SRC"], [e["row_id"] for e in self.conn.inbox("cursor")])
        self.assertEqual(["ROW-PLAIN"], [e["row_id"] for e in self.conn.inbox("grok")])
        held = self.conn.dumped()
        for text in (WORDS, "sample queue flow", "CALL ASK", "from the owner", "Codex,"):
            self.assertNotIn(text, held)

    def test_loop_rows_are_skipped(self):
        rows = [
            row("ROW-BUS", "grok;ALL", WORDS, source="bus-reconciler"),
            row("ROW-BUS2", "cursor", WORDS, source=" BUS-RECONCILER "),
            row("ROW-AUD", "gemini", WORDS, source="redis-audit"),
            row("ROW-AUD2", "gemini", WORDS, source="Redis-Audit"),
            row("ROW-OK", "grok", "a normal note", source="owner"),
        ]
        self.mirror(rows)
        self.assertEqual(["fleet:inbox:grok"], list(self.conn.streams))
        self.assertEqual(["ROW-OK"], [e["row_id"] for e in self.conn.inbox("grok")])
        self.assertNotIn(WORDS, self.conn.dumped())

    def test_call_sheet_rows_are_skipped(self):
        """CALL_SHEET_MIRROR's rows, built by its own builders, post as bus-reconciler to ALL or to
        an agent. Copying them would fan every call event and turn out to every lane."""
        call_id = "conf-0123456789abcdef-01234567"
        start = sr._event_spec({"call_id": call_id, "event": "start", "epoch": "e1",
                                "at": "2026-11-08T03:00:00Z"})
        turn = sr._withheld_turn(call_id, {"said_at": "2026-11-08T03:00:01Z"},
                                 "CALLTURN-1", "grok", "e1", "t1")
        rows = []
        for spec in (start, turn):
            self.assertEqual("bus-reconciler", spec["source_tag"])
            rows.append(row(spec["row_id"], spec["target_surface"], spec["payload"],
                            source=spec["source_tag"]))
        self.assertEqual(0, self.mirror(rows))
        self.assertEqual({}, self.conn.streams)
        self.assertEqual({}, self.conn.data)

    def test_a_row_with_no_id_or_no_agent_writes_nothing(self):
        self.mirror([
            row("", "grok", "no id"),
            row("ROW-X", "", "no target"),
            row("ROW-Y", "not-an-agent", "unknown"),
            "not-a-row",
            None,
        ])
        self.assertEqual({}, self.conn.streams)
        self.assertEqual({}, self.conn.data)
        self.assertEqual(0, self.mirror(None))

    def test_an_unreadable_acl_or_a_missing_fleet_ttl_writes_nothing(self):
        with mock.patch.object(sr.redis_gov, "load_acl", return_value={"readable": False}):
            self.assertEqual(0, self.mirror([row("ROW-1", "grok", "ping")]))
        self.assertEqual({}, self.conn.data)
        with mock.patch.object(sr.redis_gov, "ttl_for", return_value=None):
            self.assertEqual(0, self.mirror([row("ROW-1", "grok", "ping")]))
        self.assertEqual({}, self.conn.streams)

    def test_the_mirror_logs_no_key_and_no_value(self):
        with mock.patch.object(sr.log, "info") as info, \
                mock.patch.object(sr.log, "warning") as warning, \
                mock.patch.object(sr.log, "error") as error:
            self.mirror([row("ROW-1", "grok", WORDS)])
        for called in (info, warning, error):
            self.assertFalse(called.called)
            self.assertNotIn(WORDS, str(called.call_args_list))
            self.assertNotIn("fleet:", str(called.call_args_list))

    def test_a_failed_copy_logs_the_type_and_the_pass_still_answers(self):
        class Boom(FakeRedis):
            def xadd(self, key, fields, **kwargs):
                raise RuntimeError("fleet:inbox:grok " + WORDS)

        answered = []

        def handle(rows, *args, **kwargs):
            answered.append(rows)
            return {"answered": [], "refused": [], "capped": 0}

        with mock.patch.object(sr.bus_request, "heartbeat"), \
                mock.patch.object(sr.bus_reconcile, "board_rows",
                                  return_value=[row("ROW-1", "grok", WORDS)]), \
                mock.patch.object(sr.bus_request, "handle", side_effect=handle), \
                mock.patch.object(sr.log, "warning") as warning:
            sr.one_pass(Boom(), {}, "", with_git=False, quiet=True,
                        mirror=lambda rows: sr.mirror_addressed(Boom(), rows))
        self.assertEqual(1, len(answered))
        text = " ".join(str(call) for call in warning.call_args_list)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(WORDS, text)
        self.assertNotIn("fleet:", text)
        self.assertNotIn("sample queue flow", text)

    def test_the_two_mirrors_fail_independently_of_each_other_and_the_pass(self):
        """BOARD_TO_REDIS and CALL_SHEET_MIRROR share one pass. Either one raising costs its own
        step only: the other still runs, and the requests are still handled."""
        for broken in ("board", "sheet"):
            order = []

            def board_mirror(rows, _broken=broken):
                order.append("board")
                if _broken == "board":
                    raise RuntimeError("fleet:inbox:grok " + WORDS)

            def sheet(*args, _broken=broken, **kwargs):
                order.append("sheet")
                if _broken == "sheet":
                    raise RuntimeError("conf:call " + WORDS)

            def handle(rows, *args, **kwargs):
                order.append("handle")
                return {"answered": [], "refused": [], "capped": 0}

            with mock.patch.object(sr.bus_request, "heartbeat"), \
                    mock.patch.object(sr.bus_reconcile, "board_rows",
                                      return_value=[row("ROW-1", "grok", WORDS)]), \
                    mock.patch.object(sr.bus_request, "handle", side_effect=handle), \
                    mock.patch.object(sr, "mirror_call_sheet", side_effect=sheet), \
                    mock.patch.object(sr.log, "warning") as warning:
                sr.one_pass(FakeRedis(), {}, "", with_git=False, quiet=True, mirror=board_mirror)
            self.assertEqual(["board", "handle", "sheet"], order, broken)
            text = " ".join(str(call) for call in warning.call_args_list)
            self.assertIn("RuntimeError", text, broken)
            self.assertNotIn(WORDS, text, broken)


class TheFlag(unittest.TestCase):
    def test_only_exactly_on_enables_it(self):
        self.assertTrue(sr.board_to_redis_on({"BOARD_TO_REDIS": "on"}))
        for value in ("", "off", "ON", "On", "true", "1", "yes", " on", "on ", "on\n"):
            self.assertFalse(sr.board_to_redis_on({"BOARD_TO_REDIS": value}), value)
        self.assertFalse(sr.board_to_redis_on({}))

    def test_the_live_loop_omits_mirror_unless_the_flag_is_exactly_on(self):
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        for value in (None, "", "off", "ON", "true", "1", " on"):
            seen = []
            with mock.patch.object(sr, "keep_lock", return_value=True), \
                    mock.patch.object(sr, "one_pass", side_effect=lambda *a, **k: seen.append(k)), \
                    mock.patch.object(sr.time, "sleep", side_effect=KeyboardInterrupt), \
                    mock.patch("git_requests.poll_seconds", return_value=60), \
                    mock.patch.dict(sr.os.environ, {}, clear=False):
                sr.os.environ.pop(sr.BOARD_TO_REDIS, None)
                if value is not None:
                    sr.os.environ[sr.BOARD_TO_REDIS] = value
                with self.assertRaises(KeyboardInterrupt):
                    sr.live(None, {}, "", "me", until, 10, 300)
            self.assertNotIn("mirror", seen[0], value)
        seen = []
        with mock.patch.object(sr, "keep_lock", return_value=True), \
                mock.patch.object(sr, "one_pass", side_effect=lambda *a, **k: seen.append(k)), \
                mock.patch.object(sr.time, "sleep", side_effect=KeyboardInterrupt), \
                mock.patch("git_requests.poll_seconds", return_value=60), \
                mock.patch.dict(sr.os.environ, {sr.BOARD_TO_REDIS: "on"}):
            with self.assertRaises(KeyboardInterrupt):
                sr.live(None, {}, "", "me", until, 10, 300)
        self.assertTrue(callable(seen[0]["mirror"]))

    def run_live(self, rows, flag, git_rows=None, extra=None):
        conn = FakeRedis()
        clock = {"t": START}
        handled = []

        class FakeDT(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["t"]

        def handle(got, *args, **kwargs):
            handled.append(list(got))
            return {"answered": [], "refused": [], "capped": 0}

        with mock.patch.object(sr.datetime, "datetime", FakeDT), \
                mock.patch.object(sr.time, "sleep",
                                  side_effect=lambda s: clock.__setitem__(
                                      "t", clock["t"] + datetime.timedelta(seconds=s))), \
                mock.patch.object(sr.bus_reconcile, "board_rows", return_value=list(rows)), \
                mock.patch.object(sr.bus_request, "handle", side_effect=handle), \
                mock.patch("git_requests.poll_seconds", return_value=10 ** 6), \
                mock.patch("git_requests.git_rows", return_value=list(git_rows or [])), \
                mock.patch.dict(sr.os.environ, {}, clear=False):
            for name in (sr.BOARD_TO_REDIS, sr.RELAY_CURSOR, sr.INBOX_WAKE_REDIS):
                sr.os.environ.pop(name, None)
            if flag is not None:
                sr.os.environ[sr.BOARD_TO_REDIS] = flag
            sr.os.environ.update(extra or {})
            code = sr.live(conn, {}, "exec-1", "exec-1", START + datetime.timedelta(seconds=12), 10, 300)
        return code, conn, handled

    def test_the_flag_off_changes_nothing(self):
        rows = [row("ROW-1", "grok;cursor", WORDS)]
        git = [row("ROW-GIT", "cursor", "from git", source="cursor")]
        baseline = None
        for flag in (None, "", "off", "ON", "true", "1", " on"):
            code, conn, handled = self.run_live(rows, flag, git)
            self.assertEqual(0, code, flag)
            mirror = [c for c in conn.calls
                      if c[0] == "xadd" or (c[0] == "set" and str(c[1]).startswith("fleet:"))]
            self.assertEqual([], mirror, flag)
            self.assertNotIn("fleet:inbox:", conn.dumped())
            self.assertNotIn("fleet:wake:mirrored:", conn.dumped())
            self.assertNotIn(WORDS, conn.dumped())
            if baseline is None:
                baseline = conn.calls
            else:
                self.assertEqual(baseline, conn.calls, flag)
            self.assertIn("ROW-GIT", [r[0] for r in handled[0]], "the git channel still reaches the handler")

    def test_the_flag_on_mirrors_board_rows_and_a_second_pass_adds_nothing(self):
        rows = [row("ROW-1", "grok", "please look", source="owner")]
        git = [row("ROW-GIT", "cursor", "from git", source="cursor")]
        code, conn, handled = self.run_live(rows, "on", git)
        self.assertEqual(0, code)
        self.assertEqual(["ROW-1"], [e["row_id"] for e in conn.inbox("grok")])
        self.assertNotIn("please look", conn.dumped())
        self.assertNotIn("fleet:inbox:cursor", conn.streams, "a git row is not a board row")
        self.assertEqual(1, len([c for c in conn.calls if c[0] == "xadd"]))
        self.assertEqual("1", conn.data["fleet:wake:mirrored:ROW-1:grok"])
        self.assertIn("ROW-GIT", [r[0] for r in handled[0]])
        self.assertEqual(["ROW-1"], [r[0] for r in handled[1]], "the second pass had no git read")


class TheCursorRelay(unittest.TestCase):
    """BOARD_TO_REDIS_RELAY_CURSOR: a row that names cursor also gives grok, Cursor's dispatcher, one
    metadata-only entry with for=cursor, exactly once under its own mark."""

    def setUp(self):
        self.conn = FakeRedis()

    def test_only_exactly_on_enables_it(self):
        self.assertTrue(sr.relay_cursor_on({"BOARD_TO_REDIS_RELAY_CURSOR": "on"}))
        for value in ("", "off", "ON", "true", "1", " on"):
            self.assertFalse(sr.relay_cursor_on({"BOARD_TO_REDIS_RELAY_CURSOR": value}), value)
        self.assertFalse(sr.relay_cursor_on({}))

    def test_a_cursor_row_gives_grok_one_entry_for_cursor_and_a_reread_adds_nothing(self):
        payload = ("BCB|v=1|id=ROW-C|phase=ASK|class=TASK|msg_type=REVIEW_ASK|thread_id=TH-Q2-9"
                   "|text=" + WORDS)
        rows = [row("ROW-C", "cursor", payload, source="claude-code-cli")]
        self.assertEqual(2, sr.mirror_addressed(self.conn, rows, relay=True))
        own = self.conn.inbox("cursor")
        relayed = self.conn.inbox("grok")
        self.assertEqual(1, len(own))
        self.assertEqual([dict(own[0], **{"for": "cursor"})], relayed)
        self.assertEqual({"by", "row_id", "at", "phase", "thread_id", "msg_type", "class", "for"},
                         set(relayed[0]))
        mark = "fleet:wake:relayed:ROW-C:cursor:grok"
        self.assertEqual("1", self.conn.data[mark])
        self.assertEqual(redis_gov.ttl_for(redis_gov.load_acl(), mark), self.conn.expires[mark])
        self.assertIn("fleet:wake:mirrored:ROW-C:cursor", self.conn.data)
        self.assertNotIn("fleet:wake:mirrored:ROW-C:grok", self.conn.data)
        self.assertEqual(0, sr.mirror_addressed(self.conn, rows, relay=True))
        self.assertEqual(1, len(self.conn.inbox("grok")))
        self.assertEqual(1, len(self.conn.inbox("cursor")))
        held = self.conn.dumped()
        for text in (WORDS, "sample queue flow", "Codex,"):
            self.assertNotIn(text, held)

    def test_relay_off_gives_no_grok_entry(self):
        rows = [row("ROW-C", "cursor", "please look")]
        self.assertEqual(1, sr.mirror_addressed(self.conn, rows))
        self.assertEqual(0, sr.mirror_addressed(self.conn, rows, relay=False))
        self.assertEqual(["fleet:inbox:cursor"], list(self.conn.streams))
        self.assertNotIn("relayed", self.conn.dumped())

    def test_a_row_for_grok_and_cursor_gives_grok_its_copy_and_the_relay_once_each(self):
        rows = [row("ROW-B", "grok; cursor", "please look")]
        self.assertEqual(3, sr.mirror_addressed(self.conn, rows, relay=True))
        self.assertEqual([None, "cursor"], [e.get("for") for e in self.conn.inbox("grok")])
        self.assertEqual(0, sr.mirror_addressed(self.conn, rows, relay=True))
        self.assertEqual(2, len(self.conn.inbox("grok")))

    def test_a_broadcast_is_not_a_dispatch(self):
        sr.mirror_addressed(self.conn, [row("ROW-ALL", "ALL", "hello fleet"),
                                        row("ROW-FL", "fleet", "hello again")], relay=True)
        self.assertEqual(["ROW-ALL", "ROW-FL"], [e["row_id"] for e in self.conn.inbox("cursor")])
        self.assertEqual([None, None], [e.get("for") for e in self.conn.inbox("grok")])
        self.assertNotIn("relayed", self.conn.dumped())
        conn = FakeRedis()
        sr.mirror_addressed(conn, [row("ROW-ALLC", "ALL; Cursor", "hello")], relay=True)
        self.assertEqual([None, "cursor"], [e.get("for") for e in conn.inbox("grok")])

    def test_a_failed_relay_is_retried_and_delivered_once(self):
        self.conn.fail_xadd = {"fleet:inbox:grok": 1}
        rows = [row("ROW-C", "cursor", WORDS)]
        with mock.patch.object(sr.log, "warning") as warning:
            self.assertEqual(1, sr.mirror_addressed(self.conn, rows, relay=True))
        self.assertNotIn("fleet:wake:relayed:ROW-C:cursor:grok", self.conn.data)
        self.assertNotIn(WORDS, str(warning.call_args_list))
        self.assertEqual(1, sr.mirror_addressed(self.conn, rows, relay=True))
        self.assertEqual(0, sr.mirror_addressed(self.conn, rows, relay=True))
        self.assertEqual(["cursor"], [e["for"] for e in self.conn.inbox("grok")])
        self.assertEqual(1, len(self.conn.inbox("cursor")))

    def test_the_largest_relay_entry_fits_the_governed_value_cap(self):
        acl = redis_gov.load_acl()
        payload = "|".join(["BCB", "phase=" + "P" * 32, "thread_id=TH-" + "9" * 64,
                            "msg_type=" + "M" * 32, "class=" + "C" * 32])
        sr.mirror_addressed(self.conn, [row("R" * 128, "cursor", payload, source="s" * 64, ts="2" * 40)],
                            relay=True)
        entry = self.conn.inbox("grok")[0]
        size = sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in entry.items())
        self.assertLessEqual(size, redis_gov.value_cap(acl, "fleet:inbox:grok"))

    def test_the_live_loop_relays_only_with_both_flags_exactly_on(self):
        rows = [row("ROW-C", "cursor", "please look", source="owner")]
        live = TheFlag("run_live")
        for extra in ({}, {sr.RELAY_CURSOR: "ON"}, {sr.RELAY_CURSOR: "true"}):
            code, conn, _handled = live.run_live(rows, "on", extra=extra)
            self.assertEqual(["fleet:inbox:cursor"], list(conn.streams), extra)
        code, conn, _handled = live.run_live(rows, "on", extra={sr.RELAY_CURSOR: "on"})
        self.assertEqual(["cursor"], [e["for"] for e in conn.inbox("grok")])
        self.assertEqual(1, len([c for c in conn.calls if c[0] == "xadd" and c[1] == "fleet:inbox:grok"]))
        code, conn, _handled = live.run_live(rows, None, extra={sr.RELAY_CURSOR: "on"})
        self.assertEqual({}, conn.streams, "the relay rides on BOARD_TO_REDIS; alone it is nothing")


def load_inbox_wake():
    spec = importlib.util.spec_from_file_location(
        "inbox_wake_for_nudge", ROOT / "cloud" / "inbox-wake" / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TheClaudeApiNudge(unittest.TestCase):
    """INBOX_WAKE_REDIS: after the mirror lands an entry on fleet:inbox:claude-api, one signed nudge to
    cloud/inbox-wake, which drains that lane and starts the worker. Nothing else is nudged."""

    def nudger(self, answers):
        sent, clock = [], Clock()

        def post(url, body, headers):
            sent.append((url, body, headers))
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return json.dumps({"enabled": True, "woke": [{"status": answer}]}).encode()
        return sr.Nudger("https://inbox-wake.example/wake", "shared", post=post, clock=clock), sent, clock

    def test_only_exactly_on_and_configured_builds_a_nudger(self):
        full = {"INBOX_WAKE_REDIS": "on", "INBOX_WAKE_URL": "https://w.example/wake", "INBOX_WAKE_SECRET": "s"}
        self.assertIsInstance(sr.nudger_from_env(full), sr.Nudger)
        for value in ("", "off", "ON", "true", " on"):
            self.assertIsNone(sr.nudger_from_env(dict(full, INBOX_WAKE_REDIS=value)), value)
        with mock.patch.object(sr.log, "error") as error:
            self.assertIsNone(sr.nudger_from_env(dict(full, INBOX_WAKE_URL="")))
            self.assertIsNone(sr.nudger_from_env(dict(full, INBOX_WAKE_SECRET="")))
        self.assertNotIn("w.example", str(error.call_args_list))

    def test_a_claude_api_entry_is_one_signed_nudge_with_no_text(self):
        nudger, sent, _clock = self.nudger(["woke"])
        conn = FakeRedis()
        rows = [row("ROW-A", "claude-api; grok", "BCB|v=1|phase=ASK|text=" + WORDS)]
        self.assertEqual(2, sr.mirror_pass(conn, rows, nudger=nudger))
        self.assertEqual(1, len(sent))
        url, body, headers = sent[0]
        self.assertEqual("https://inbox-wake.example/wake", url)
        self.assertEqual({"by": "bus-reconciler", "lane": "claude-api"}, json.loads(body))
        self.assertEqual("redis_lane", headers["X-GitHub-Event"])
        mac = hmac.new(b"shared", body, hashlib.sha256).hexdigest()
        self.assertEqual("sha256=" + mac, headers["X-Hub-Signature-256"])
        self.assertNotIn(WORDS, body.decode())
        self.assertEqual(set(), nudger.pending)
        self.assertEqual(0, sr.mirror_pass(conn, rows, nudger=nudger), "a re-read adds and nudges nothing")
        self.assertEqual(1, len(sent))

    def test_no_nudge_for_another_lane(self):
        nudger, sent, _clock = self.nudger([])
        sr.mirror_pass(FakeRedis(), [row("ROW-G", "grok; cursor; gemini", "ping")], relay=True, nudger=nudger)
        self.assertEqual([], sent)

    def test_a_failed_or_unrung_nudge_is_sent_again_after_the_retry_gap(self):
        nudger, sent, clock = self.nudger([OSError("down"), "error", "woke"])
        nudger.note("claude-api")
        with mock.patch.object(sr.log, "warning") as warning:
            nudger.flush()
            self.assertEqual({"claude-api"}, nudger.pending)
            nudger.flush()                          # inside the gap: not sent
            self.assertEqual(1, len(sent))
            clock.now += sr.NUDGE_RETRY_SECONDS
            nudger.flush()                          # sent; inbox-wake's start failed: still pending
            self.assertEqual(2, len(sent))
            self.assertEqual({"claude-api"}, nudger.pending)
            clock.now += sr.NUDGE_RETRY_SECONDS
            nudger.flush()
        self.assertEqual(3, len(sent))
        self.assertEqual(set(), nudger.pending)
        text = str(warning.call_args_list)
        self.assertIn("OSError", text)
        self.assertNotIn("inbox-wake.example", text)

    def test_an_answer_a_retry_cannot_change_is_dropped(self):
        for status in ("lane_off", "not_allowed", "disabled", "no_lane", "idle", "duplicate"):
            nudger, sent, _clock = self.nudger([status])
            nudger.note("claude-api")
            with mock.patch.object(sr.log, "warning"):
                nudger.flush()
            self.assertEqual(set(), nudger.pending, status)

    def test_the_nudge_starts_the_worker_through_inbox_wake_once(self):
        """End to end, in process: the mirror's entry, the signed nudge, inbox-wake's real handle and drain
        over the same lane, and the worker started once. A second pass starts nothing."""
        wake = load_inbox_wake()
        conn, jobs = FakeRedis(), []

        def reader(agent):
            return [{"id": "%d-0" % (i + 1), "stream_id": "%d-0" % (i + 1), "by": e.get("by", "")}
                    for i, e in enumerate(conn.inbox(agent))]
        deps = wake.Deps(enabled=True, seen=wake.MemorySeen(), reader=reader, webhook_secret="shared",
                         agents=frozenset({"claude-api"}), redis_wake=True, cursor=wake.MemoryCursor(),
                         wakers=wake.Wakers(run_job=jobs.append))
        nudger = sr.Nudger("https://inbox-wake.example/wake", "shared",
                           post=lambda url, body, headers: wake.handle("POST", "/wake", headers, body, deps)[1])
        rows = [row("ROW-A", "claude-api", "ping")]
        sr.mirror_pass(conn, rows, nudger=nudger)
        self.assertEqual(["claude-api-worker"], jobs)
        self.assertEqual("1-0", deps.cursor.get("claude-api"))
        sr.mirror_pass(conn, rows, nudger=nudger)
        sr.mirror_pass(conn, rows + [row("ROW-B", "claude-api", "pong")], nudger=nudger)
        self.assertEqual(["claude-api-worker"] * 2, jobs)
        self.assertEqual("2-0", deps.cursor.get("claude-api"))

    def test_the_live_loop_nudges_only_with_the_flag_exactly_on(self):
        rows = [row("ROW-A", "claude-api", "ping", source="owner")]
        live = TheFlag("run_live")
        env = {"INBOX_WAKE_URL": "https://inbox-wake.example/wake", "INBOX_WAKE_SECRET": "shared"}
        for value in ("", "off", "ON"):
            with mock.patch.object(sr, "private_post") as post:
                live.run_live(rows, "on", extra=dict(env, INBOX_WAKE_REDIS=value))
            self.assertFalse(post.called, value)
        with mock.patch.object(sr, "private_post",
                               return_value=b'{"woke": [{"status": "woke"}]}') as post:
            code, conn, _handled = live.run_live(rows, "on", extra=dict(env, INBOX_WAKE_REDIS="on"))
        self.assertEqual(1, post.call_count, "two passes, one entry, one nudge")
        self.assertEqual(["ROW-A"], [e["row_id"] for e in conn.inbox("claude-api")])


class TheFake(unittest.TestCase):
    """The mirror's tests trust the fake to run the script it is given. It must refuse what it
    cannot run rather than skip it, and keep earlier writes when redis.call raises."""

    def test_it_refuses_a_line_or_a_command_it_does_not_know(self):
        for script in ("redis.call('HSET', KEYS[1], 'a', 'b')",
                       "if redis.call('GET', KEYS[1]) then return 1 end",
                       "redis.call('SET', KEYS[1], tostring(ARGV[1]))"):
            with self.assertRaises(AssertionError, msg=script):
                FakeRedis().eval(script, 1, "k", "v")

    def test_a_raising_call_keeps_the_writes_before_it(self):
        conn = FakeRedis()
        conn.fail_xadd = {"lane": 1}
        with self.assertRaises(RuntimeError):
            conn.eval("redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1])\n"
                      "redis.call('XADD', KEYS[2], '*', 'a', 'b')\n", 2, "mark", "lane", 60)
        self.assertEqual("1", conn.data["mark"])
        self.assertEqual(60, conn.expires["mark"])


if __name__ == "__main__":
    unittest.main()

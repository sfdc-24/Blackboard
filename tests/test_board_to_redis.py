"""Board rows addressed to an agent land in that agent's Redis lane. FakeRedis, no network.

BOARD_TO_REDIS must be exactly "on". Anything else, the live loop does not take the mirror and
writes nothing under fleet:inbox or fleet:wake:mirrored.
"""
import datetime
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
    """SET NX/EX, XADD and DEL, and the worker-lock eval the live loop needs. No network.

    Any other script is RUN, line by line, by a small interpreter that knows only the statements
    the mirror script uses and refuses any other line. So the fake does what the script text says:
    a script without NX, or without the DEL on a failed XADD, behaves that way here too. As on a
    real server, redis.call raising aborts the script with its earlier writes kept, and redis.pcall
    turns the error into a table. A script that returns that table raises the original exception
    here (redis-py raises ResponseError), so a test can see what failed.

    fail_xadd maps a stream key to how many XADDs on it raise before one succeeds."""

    def __init__(self):
        self.data, self.streams, self.expires, self.calls = {}, {}, {}, []
        self.fail_xadd = {}

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

    def xadd(self, key, fields, **kwargs):
        self.calls.append(("xadd", key, dict(fields)))
        if self.fail_xadd.get(key):
            self.fail_xadd[key] -= 1
            raise RuntimeError("%s %s" % (key, WORDS))
        self.streams.setdefault(key, []).append(dict(fields))
        return "%d-0" % len(self.streams[key])

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
            key, ident, pairs = rest[0], rest[1], rest[2:]
            if ident != "*" or len(pairs) % 2:
                raise AssertionError("fake XADD takes '*' and field/value pairs")
            return self.xadd(key, dict(zip(pairs[0::2], pairs[1::2])))
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
        payload = "BCB|v=1|id=ROW-1|phase=ASK|from=grok|to=cursor|thread_id=thread-9|text=please look"
        self.mirror([row("ROW-1", "grok; cursor", payload, source="Grok")])
        self.assertEqual(["grok", "cursor"], [k.split(":")[-1] for k in self.conn.streams])
        for agent in ("grok", "cursor"):
            self.assertEqual(1, len(self.conn.inbox(agent)))
            entry = self.conn.inbox(agent)[0]
            self.assertEqual({
                "by": "Grok", "row_id": "ROW-1", "at": "2026-11-08T03:00:00Z",
                "phase": "ASK", "thread_id": "thread-9", "preview": payload[:sr.PREVIEW_CHARS],
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

    def test_the_preview_is_the_payloads_first_300_characters(self):
        payload = "x" * 300 + "TAIL"
        self.mirror([row("ROW-LONG", "grok", payload)])
        preview = self.conn.inbox("grok")[0]["preview"]
        self.assertEqual(payload[:300], preview)
        self.assertNotIn("TAIL", preview)
        self.assertNotIn("thread_id", self.conn.inbox("grok")[0])

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

    def test_call_bridge_text_is_withheld(self):
        rows = [
            row("ROW-ASK", "cursor", "CALL ASK call-1 from the owner: " + WORDS),
            row("ROW-BY", "cursor", "by=call-bridge|text=" + WORDS, source="grok"),
            row("ROW-SRC", "cursor", WORDS, source="call-bridge"),
            row("ROW-PLAIN", "grok", WORDS, source="grok"),
        ]
        self.mirror(rows)
        for rid in ("ROW-ASK", "ROW-BY", "ROW-SRC"):
            found = [e for e in self.conn.inbox("cursor") if e["row_id"] == rid]
            self.assertEqual([sr.redis_gov.WITHHELD], [e["preview"] for e in found], rid)
        self.assertIn(WORDS, self.conn.inbox("grok")[0]["preview"])
        held = self.conn.dumped()
        self.assertEqual(3, held.count(sr.redis_gov.WITHHELD))
        self.assertNotIn("sample queue flow", "\n".join(
            e["preview"] for e in self.conn.inbox("cursor")))
        for entry in self.conn.inbox("cursor"):
            self.assertNotIn(WORDS, " ".join(entry.values()))

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

    def run_live(self, rows, flag, git_rows=None):
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
            sr.os.environ.pop(sr.BOARD_TO_REDIS, None)
            if flag is not None:
                sr.os.environ[sr.BOARD_TO_REDIS] = flag
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
        self.assertEqual(["please look"], [e["preview"] for e in conn.inbox("grok")])
        self.assertNotIn("fleet:inbox:cursor", conn.streams, "a git row is not a board row")
        self.assertEqual(1, len([c for c in conn.calls if c[0] == "xadd"]))
        self.assertEqual("1", conn.data["fleet:wake:mirrored:ROW-1:grok"])
        self.assertIn("ROW-GIT", [r[0] for r in handled[0]])
        self.assertEqual(["ROW-1"], [r[0] for r in handled[1]], "the second pass had no git read")


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

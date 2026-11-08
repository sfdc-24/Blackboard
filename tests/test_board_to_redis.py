"""Board rows addressed to an agent land in that agent's Redis lane. FakeRedis, no network.

BOARD_TO_REDIS must be exactly "on". Anything else, the live loop does not take the mirror and
writes nothing under fleet:inbox or fleet:wake:mirrored.
"""
import datetime
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


class FakeRedis:
    """SET NX/EX and XADD, and the worker-lock eval the live loop needs. No network."""

    def __init__(self):
        self.data, self.streams, self.expires, self.calls = {}, {}, {}, []

    def set(self, key, value, nx=False, ex=None):
        self.calls.append(("set", key, value, nx, ex))
        if nx and key in self.data:
            return None
        self.data[key] = value
        if ex is not None:
            self.expires[key] = ex
        return True

    def get(self, key):
        return self.data.get(key)

    def xadd(self, key, fields, **kwargs):
        self.calls.append(("xadd", key, dict(fields)))
        self.streams.setdefault(key, []).append(dict(fields))
        return "%d-0" % len(self.streams[key])

    def eval(self, script, numkeys, key, me, *args):
        self.calls.append(("eval", key))
        if script == sr.RENEW_LUA:
            if self.data.get(key) != me:
                return None
            self.data[key] = me
            return "OK"
        if script == sr.DROP_LUA:
            if self.data.get(key) != me:
                return 0
            self.data.pop(key, None)
            return 1
        raise AssertionError("unexpected script")

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


if __name__ == "__main__":
    unittest.main()

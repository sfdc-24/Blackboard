"""xwait, the Redis doorbell: an agent waits on its own lane through the bridge and wakes on the entry.

The owner, 2026-11-08: "Redis is the always-on signal so every agent wakes on event instead of polling."
No network: FakeRedis gains XREAD, the bridge client and the doorbell are driven with fakes.
"""
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bridge_client  # noqa: E402
import redis_doorbell  # noqa: E402
import redis_gov as gov  # noqa: E402
from test_redis_gov import ACL, FakeRedis  # noqa: E402

BRIDGE = "bridge:fleet-claude-code-cli@sfdc24.iam.gserviceaccount.com"


class WaitingRedis(FakeRedis):
    """FakeRedis with XREAD: the entries after the given id, and the block it was asked for."""

    def __init__(self):
        super().__init__()
        self.blocks = []

    def xread(self, streams, count=None, block=None):
        self.blocks.append(block)
        found = []
        for key, after in streams.items():
            entries = self.xrange(key, min="(" + after, count=count)
            if entries:
                found.append([key, entries])
        return found


def wait(conn, tag="claude-code-cli", key="fleet:inbox:claude-code-cli", after="0-0", via=BRIDGE, count=10):
    return gov.execute(conn, ACL, tag, via, "xwait", key, after=after, count=count)


class TheGovernedOp(unittest.TestCase):
    def test_an_agent_wakes_on_the_entries_after_its_cursor_in_its_own_lane(self):
        conn = WaitingRedis()
        for text in ("one", "two", "three"):
            conn.xadd("fleet:inbox:claude-code-cli", {"by": "grok", "text": text})
        got = wait(conn, after="1-0")
        self.assertTrue(got["ok"], got)
        self.assertEqual(["two", "three"], [entry["text"] for entry in got["value"]])
        self.assertEqual(["2-0", "3-0"], [entry["id"] for entry in got["value"]])
        self.assertEqual("3-0", got["last"])
        self.assertEqual([gov.wait_ms()], conn.blocks, "it blocks for the server's wait, not the caller's")
        self.assertEqual("xwait", conn.audit()[-1]["op"], "a wait is audited like a read")

    def test_a_quiet_lane_is_an_empty_answer_and_the_cursor_stays(self):
        conn = WaitingRedis()
        got = wait(conn, after="5-0")
        self.assertTrue(got["ok"], got)
        self.assertEqual((0, "5-0"), (got["entries"], got["last"]))

    def test_another_agents_lane_or_any_other_key_is_refused(self):
        conn = WaitingRedis()
        conn.xadd("fleet:inbox:grok", {"by": "x", "text": "not yours"})
        for key in ("fleet:inbox:grok", "fleet:inbox:claude-code-cli-2", "conf:side:20261108", "gov:audit"):
            with self.subTest(key=key):
                got = wait(conn, key=key)
                self.assertFalse(got["ok"])
                self.assertIn("own lane", got["refused"])
        self.assertEqual([], conn.blocks, "no refused wait reaches Redis")

    def test_the_board_route_never_blocks(self):
        conn = WaitingRedis()
        got = wait(conn, via="REQ-1")
        self.assertFalse(got["ok"])
        self.assertIn("bridge only", got["refused"])
        self.assertEqual([], conn.blocks)

    def test_a_wait_needs_a_real_cursor(self):
        conn = WaitingRedis()
        for after in ("", "latest", "$", "1-x"):
            with self.subTest(after=after):
                self.assertFalse(wait(conn, after=after)["ok"])
        self.assertEqual([], conn.blocks)

    def test_at_most_ten_entries_come_back(self):
        conn = WaitingRedis()
        for n in range(15):
            conn.xadd("fleet:inbox:claude-code-cli", {"by": "grok", "text": str(n)})
        got = wait(conn, count=50)
        self.assertEqual(10, got["entries"])
        self.assertEqual("10-0", got["last"])

    def test_the_wait_stays_strictly_under_the_connections_socket_timeout(self):
        # Codex on sfdc-24/Blackboard#482: a 1 s socket timeout gave a 1 s block, which times out.
        self.assertEqual(8000, gov.wait_ms(10))
        self.assertEqual(25000, gov.wait_ms(30))
        self.assertEqual(25000, gov.wait_ms(300))
        self.assertEqual(1000, gov.wait_ms(3))
        for seconds in (2.9, 2, 1, 0.5, 0):
            with self.subTest(seconds=seconds):
                self.assertIsNone(gov.wait_ms(seconds))
        for seconds in (3, 4.5, 10, 26, 27, 30):
            with self.subTest(seconds=seconds):
                self.assertLessEqual(gov.wait_ms(seconds), seconds * 1000 - gov.WAIT_MARGIN_MS)

    def test_a_connection_too_short_to_wait_refuses_and_never_blocks(self):
        import redis_dual
        conn = WaitingRedis()
        saved = redis_dual.CONNECT_SECONDS
        redis_dual.CONNECT_SECONDS = 1.0
        try:
            got = wait(conn)
        finally:
            redis_dual.CONNECT_SECONDS = saved
        self.assertFalse(got["ok"])
        self.assertIn("too short", got["refused"])
        self.assertEqual([], conn.blocks)

    def test_after_on_another_op_is_still_refused(self):
        conn = WaitingRedis()
        got = gov.execute(conn, ACL, "claude-code-cli", BRIDGE, "get", "fleet:x", after="1-0")
        self.assertFalse(got["ok"])
        self.assertIn("xrange only", got["refused"])


class TheClient(unittest.TestCase):
    def test_a_wait_is_a_read_and_has_a_longer_request_timeout(self):
        self.assertIn("xwait", bridge_client.READS)
        self.assertGreater(bridge_client.WAIT_HTTP_TIMEOUT, 25)
        seen = []

        class Opener:
            def open(self, req, timeout=None):
                seen.append((json.loads(req.data)["op"], timeout))
                raise bridge_client.urllib.error.URLError("stop here")

        for op in ("xwait", "get"):
            try:
                bridge_client.call("https://bridge.invalid", "tok", {"op": op, "key": "k"}, opener=Opener())
            except Exception:  # noqa: BLE001 - only the timeout it asked for is under test
                pass
        self.assertEqual([("xwait", bridge_client.WAIT_HTTP_TIMEOUT), ("get", bridge_client.HTTP_TIMEOUT)], seen)


class TheDoorbell(unittest.TestCase):
    def setUp(self):
        self.cursor = pathlib.Path(tempfile.mkdtemp()) / "cursor"

    def test_it_prints_the_new_entries_and_moves_its_cursor_after_printing(self):
        calls = []

        def attempt(op, key, *, me, after, count):
            calls.append((op, key, me, after))
            if len(calls) == 1:
                return {"ok": True, "value": [], "last": after}
            return {"ok": True, "value": [{"id": "7-0", "fields": {"text": "hello"}}], "last": "7-0"}

        redis_doorbell.write_cursor(self.cursor, "6-0")
        out = io.StringIO()
        code = redis_doorbell.ring("claude-code-cli", attempt=attempt, out=out, cursor_file=self.cursor, rounds=3)
        self.assertEqual(0, code)
        self.assertEqual([("xwait", "fleet:inbox:claude-code-cli", "claude-code-cli", "6-0")] * 2, calls)
        self.assertEqual("hello", json.loads(out.getvalue().splitlines()[0])["fields"]["text"])
        self.assertEqual("7-0", self.cursor.read_text(encoding="utf-8"))

    def test_peek_leaves_the_cursor(self):
        redis_doorbell.write_cursor(self.cursor, "6-0")
        redis_doorbell.ring("claude-code-cli", out=io.StringIO(), cursor_file=self.cursor, peek=True, rounds=1,
                            attempt=lambda *a, **k: {"value": [{"id": "7-0", "fields": {}}], "last": "7-0"})
        self.assertEqual("6-0", self.cursor.read_text(encoding="utf-8"))

    def governed(self, conn):
        def attempt(op, key, *, me, after=None, count=None):
            return gov.execute(conn, ACL, me, BRIDGE, op, key, after=after or "", count=count or 10)
        return attempt

    def test_a_first_run_starts_at_the_lanes_own_newest_entry_and_misses_nothing_after_it(self):
        # Codex on sfdc-24/Blackboard#482: a clock-made "<ms>-0" start skips an entry added in that same
        # millisecond (XREAD's start is exclusive). The start is the lane's own newest id, read from Redis.
        conn = WaitingRedis()
        for text in ("old-1", "old-2"):
            conn.xadd("fleet:inbox:claude-code-cli", {"by": "grok", "text": text})
        governed = self.governed(conn)

        def attempt(op, key, **kw):
            result = governed(op, key, **kw)
            if op == "xrange":                           # the race: an entry lands right after the start is read
                conn.xadd("fleet:inbox:claude-code-cli", {"by": "grok", "text": "same-moment"})
            return result

        import time as clock
        real = clock.time
        clock.time = lambda: (_ for _ in ()).throw(AssertionError("the start must not come from the clock"))
        try:
            out = io.StringIO()
            code = redis_doorbell.ring("claude-code-cli", attempt=attempt, out=out, cursor_file=self.cursor,
                                       rounds=2)
        finally:
            clock.time = real
        self.assertEqual(0, code)
        self.assertEqual(["same-moment"], [json.loads(line)["text"] for line in out.getvalue().splitlines()])
        self.assertEqual("3-0", self.cursor.read_text(encoding="utf-8"))

    def test_a_first_run_that_saw_nothing_keeps_its_start_so_the_next_run_misses_nothing(self):
        conn = WaitingRedis()
        conn.xadd("fleet:inbox:claude-code-cli", {"by": "grok", "text": "before"})
        attempt = self.governed(conn)
        quiet = redis_doorbell.ring("claude-code-cli", attempt=attempt, out=io.StringIO(),
                                    cursor_file=self.cursor, rounds=1)
        self.assertEqual(0, quiet)
        self.assertEqual("1-0", self.cursor.read_text(encoding="utf-8"), "the start is kept")
        conn.xadd("fleet:inbox:claude-code-cli", {"by": "grok", "text": "between runs"})
        out = io.StringIO()
        redis_doorbell.ring("claude-code-cli", attempt=attempt, out=out, cursor_file=self.cursor, rounds=1)
        self.assertEqual(["between runs"], [json.loads(line)["text"] for line in out.getvalue().splitlines()])

    def test_a_first_run_on_an_empty_lane_starts_at_the_beginning(self):
        conn = WaitingRedis()
        self.assertEqual("0-0", redis_doorbell.lane_position("claude-code-cli", self.governed(conn)))
        self.assertIsNone(redis_doorbell.lane_position("claude-code-cli", lambda *a, **k: None))

    def test_no_bridge_says_so_and_a_refusal_stops(self):
        self.assertEqual(3, redis_doorbell.ring("claude-code-cli", out=io.StringIO(), cursor_file=self.cursor,
                                                attempt=lambda *a, **k: None, rounds=1))

        def refused(*a, **k):
            raise bridge_client.Refused("not yours")

        self.assertEqual(2, redis_doorbell.ring("claude-code-cli", out=io.StringIO(), cursor_file=self.cursor,
                                                attempt=refused, rounds=1))


if __name__ == "__main__":
    unittest.main()

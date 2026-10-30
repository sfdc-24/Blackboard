"""The call relay turns a claimed call ask into a ring every agent can hear, flags silence at 180 s, and closes asks
nobody started when the call ends. The ways that goes wrong: a ring lost in a crash, a ring sent twice, an overdue
or expiry that overwrites work an agent already started, and anything that writes answers= or the owner's words."""
import datetime
import fnmatch
import json
import logging
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import call_bridge as cb   # noqa: E402
import call_relay as cr    # noqa: E402
import canvas_publish as cp  # noqa: E402

T0 = datetime.datetime(2026, 10, 30, 15, 0, 0, tzinfo=datetime.timezone.utc)
ASK = "call-0123456789ab"
ROOM = "workshop"
SAID = "Gemini, draw the lead to quote flow for ACME Corp"


class WatchError(Exception):
    pass


class FakeRedis:
    def __init__(self):
        self.kv, self.h, self.version = {}, {}, {}

    def _bump(self, key):
        self.version[key] = self.version.get(key, 0) + 1

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        self._bump(key)
        return True

    def get(self, key):
        return self.kv.get(key)

    def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value
        self._bump(key)

    def hget(self, key, field):
        return self.h.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    def hdel(self, key, field):
        self.h.get(key, {}).pop(field, None)
        self._bump(key)

    def scan_iter(self, match="*", count=None):
        return [k for k in list(self.kv) + list(self.h) if fnmatch.fnmatch(k, match)]

    def pipeline(self, transaction=True):
        return FakePipe(self)


class FakePipe:
    """WATCH, then immediate reads, then MULTI/EXEC that fails if a watched key changed: redis-py's shape."""

    def __init__(self, conn):
        self.conn, self.watched, self.queue, self.queued = conn, {}, [], False

    def watch(self, *keys):
        self.watched = {k: self.conn.version.get(k, 0) for k in keys}

    def hget(self, key, field):
        if self.queued:
            self.queue.append(("hget", key, field))
            return self
        return self.conn.hget(key, field)

    def multi(self):
        self.queued = True

    def hset(self, key, field, value):
        self.queue.append(("hset", key, field, value))

    def hdel(self, key, field):
        self.queue.append(("hdel", key, field))

    def execute(self):
        if any(self.conn.version.get(k, 0) != v for k, v in self.watched.items()):
            raise WatchError("watched key changed")
        for op in self.queue:
            getattr(self.conn, op[0])(*op[1:])

    def reset(self):
        self.watched, self.queue, self.queued = {}, [], False


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def claim(conn, agent="gemini", at=T0, ask=ASK):
    """What the bridge writes for one claim (call_bridge.Bridge._claim), minus the inbox words."""
    conn.hset(cp.PARTITION % (ROOM, agent), ask, json.dumps(cb.claimed_card(agent, at), sort_keys=True))
    conn.hset(cb.OPEN_KEY, ask, json.dumps({"agent": agent, "claimed_at": cb.stamp(at), "room": ROOM}, sort_keys=True))


def open_window(conn, until):
    conn.set(cp.window_key(ROOM), json.dumps({"until": cb.stamp(until)}))


class Harness:
    def __init__(self, now=T0 + datetime.timedelta(seconds=10), holds=True, fail_append=False):
        self.conn, self.clock, self.rows, self.board = FakeRedis(), Clock(now), [], {}
        self.holding, self.fail_append, self.board_answer = holds, fail_append, None
        self.relay = cr.Relay(self.conn, logging.getLogger("test"), lambda: self.holding, self.append,
                              self.on_board, clock=self.clock)

    def append(self, spec):
        self.rows.append(spec)
        if self.fail_append:
            raise RuntimeError("APPEND UNCONFIRMED")
        self.board[spec["row_id"]] = spec

    def on_board(self, rid):
        """A readable board by default: True when the row is there, False when it is not. board_answer forces a
        reply (None = the board could not be read)."""
        if self.board_answer is not None or getattr(self, "unreadable", False):
            return None if getattr(self, "unreadable", False) else self.board_answer
        return rid in self.board

    def kinds(self):
        return [r["row_id"].split("-")[1] for r in self.rows]


class Pointer(unittest.TestCase):
    def test_a_claim_gets_exactly_one_pointer(self):
        h = Harness()
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn)
        self.assertEqual(h.relay.tick()["pointed"], [ASK])
        h.clock.now += datetime.timedelta(seconds=5)
        self.assertEqual(h.relay.tick()["pointed"], [])
        self.assertEqual(h.kinds(), ["POINTER"])

    def test_the_pointer_carries_the_id_never_the_words_and_never_answers(self):
        h = Harness()
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn)
        h.relay.tick()
        payload = h.rows[0]["payload"]
        self.assertIn("call=%s" % ASK, payload)
        self.assertIn("inbox=fleet:inbox:gemini", payload)
        self.assertNotIn("ACME", payload)
        self.assertNotIn("|answers=", payload)
        self.assertEqual(h.rows[0]["source_tag"], "bus-reconciler")

    def test_codex_is_addressed_to_aya_too(self):
        h = Harness()
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn, agent="chatgpt-codex-desktop")
        h.relay.tick()
        self.assertIn("|to=aya,chatgpt-codex-desktop|", h.rows[0]["payload"])

    def test_an_unconfirmed_append_is_not_retried_inside_the_lease(self):
        h = Harness(fail_append=True)
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn)
        h.relay.tick()
        h.fail_append = False
        h.clock.now += datetime.timedelta(seconds=60)
        h.relay.tick()
        self.assertEqual(h.kinds(), ["POINTER"])

    def test_after_the_lease_a_landed_pointer_is_marked_posted_not_sent_again(self):
        # Crash after reserving: the append raised but the row DID land (the 404-that-wrote case).
        h = Harness(fail_append=True)
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn)
        h.relay.tick()
        h.board[cr.row_id("pointer", ASK)] = h.rows[0]
        h.fail_append = False
        h.clock.now += datetime.timedelta(seconds=cr.LEASE_SECONDS + 5)
        h.relay.tick()
        self.assertEqual(h.kinds(), ["POINTER"])
        self.assertEqual(json.loads(h.conn.get(cr.POINTER_KEY % ASK))["phase"], "posted")

    def test_after_the_lease_an_absent_pointer_is_sent_once(self):
        h = Harness(fail_append=True)
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn)
        h.relay.tick()
        h.fail_append = False
        h.board_answer = False
        h.clock.now += datetime.timedelta(seconds=cr.LEASE_SECONDS + 5)
        h.relay.tick()
        h.board_answer = None
        h.clock.now += datetime.timedelta(seconds=cr.LEASE_SECONDS + 5)
        h.relay.tick()
        self.assertEqual(h.kinds(), ["POINTER", "POINTER", "OVERDUE"])

    def test_an_unreadable_board_leaves_the_reservation_alone(self):
        h = Harness(fail_append=True)
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn)
        h.relay.tick()
        h.fail_append = False
        h.unreadable = True
        h.clock.now += datetime.timedelta(seconds=cr.LEASE_SECONDS + 5)
        h.relay.tick()
        self.assertEqual(h.kinds().count("POINTER"), 1)

    def test_nothing_is_written_without_the_worker_lock(self):
        h = Harness(holds=False)
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn)
        h.relay.tick()
        self.assertEqual(h.rows, [])


class Overdue(unittest.TestCase):
    def test_silence_at_180_seconds_is_flagged_once(self):
        h = Harness()
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn)
        h.relay.tick()
        h.clock.now = T0 + datetime.timedelta(seconds=cr.OVERDUE_SECONDS - 1)
        h.relay.tick()
        h.clock.now = T0 + datetime.timedelta(seconds=cr.OVERDUE_SECONDS)
        h.relay.tick()
        h.clock.now += datetime.timedelta(seconds=60)
        h.relay.tick()
        self.assertEqual(h.kinds(), ["POINTER", "OVERDUE"])

    def test_an_agent_that_started_is_not_overdue(self):
        h = Harness()
        open_window(h.conn, T0 + datetime.timedelta(hours=1))
        claim(h.conn)
        h.relay.tick()
        working = dict(cb.claimed_card("gemini", T0), status="working", version=2)
        h.conn.hset(cp.PARTITION % (ROOM, "gemini"), ASK, json.dumps(working))
        h.clock.now = T0 + datetime.timedelta(seconds=cr.OVERDUE_SECONDS + 30)
        h.relay.tick()
        self.assertEqual(h.kinds(), ["POINTER"])


class Expired(unittest.TestCase):
    def test_a_call_that_ended_before_work_started_closes_the_ask(self):
        h = Harness()
        claim(h.conn)                              # no open window: the call ended
        self.assertEqual(h.relay.tick()["expired"], [ASK])
        card = json.loads(h.conn.hget(cp.PARTITION % (ROOM, "gemini"), ASK))
        self.assertEqual((card["type"], card["status"], card["version"]), ("note", "review", 2))
        self.assertIsNotNone(cp.clean_item("gemini", ASK, json.dumps(card)))     # it really publishes
        self.assertIsNone(h.conn.hget(cb.OPEN_KEY, ASK))
        self.assertEqual(h.kinds(), ["EXPIRED"])
        self.assertIn("|expires=%s|" % ASK, h.rows[0]["payload"])
        self.assertNotIn("|answers=", h.rows[0]["payload"])

    def test_an_expired_ask_is_never_pointed_or_revived(self):
        h = Harness()
        claim(h.conn)
        h.relay.tick()
        open_window(h.conn, T0 + datetime.timedelta(hours=1))   # a later call opens the window again
        h.clock.now += datetime.timedelta(minutes=10)
        h.relay.tick()
        self.assertEqual(h.kinds(), ["EXPIRED"])

    def test_started_work_is_never_expired(self):
        h = Harness()
        claim(h.conn)
        working = dict(cb.claimed_card("gemini", T0), status="working", version=2)
        h.conn.hset(cp.PARTITION % (ROOM, "gemini"), ASK, json.dumps(working))
        h.relay.tick()
        card = json.loads(h.conn.hget(cp.PARTITION % (ROOM, "gemini"), ASK))
        self.assertEqual(card["status"], "working")
        self.assertIsNotNone(h.conn.hget(cb.OPEN_KEY, ASK))

    def test_an_agent_card_written_before_the_watch_wins(self):
        """Between the tick's read and WATCH, the agent moves its card. WATCH starts after the write, so only the
        re-read under WATCH can see it."""
        h = Harness()
        claim(h.conn)
        real = h.conn.pipeline

        def early(transaction=True):
            pipe = real(transaction)
            original = pipe.watch

            def watch(*keys):
                working = dict(cb.claimed_card("gemini", T0), status="working", version=2)
                h.conn.hset(cp.PARTITION % (ROOM, "gemini"), ASK, json.dumps(working))
                original(*keys)
            pipe.watch = watch
            return pipe
        h.conn.pipeline = early
        self.assertEqual(h.relay.tick()["expired"], [])
        self.assertEqual(json.loads(h.conn.hget(cp.PARTITION % (ROOM, "gemini"), ASK))["status"], "working")
        self.assertEqual(h.rows, [])

    def test_a_closed_call_gets_no_pointer_even_for_started_work(self):
        h = Harness()
        claim(h.conn)
        working = dict(cb.claimed_card("gemini", T0), status="working", version=2)
        h.conn.hset(cp.PARTITION % (ROOM, "gemini"), ASK, json.dumps(working))
        h.clock.now = T0 + datetime.timedelta(seconds=cr.OVERDUE_SECONDS + 30)
        h.relay.tick()
        self.assertEqual(h.rows, [])

    def test_an_agent_card_written_during_expiry_wins(self):
        h = Harness()
        claim(h.conn)
        real = h.conn.pipeline

        def racing_pipeline(transaction=True):
            pipe = real(transaction)
            original = pipe.multi

            def multi():
                # The agent posts its working card between the read and the write.
                working = dict(cb.claimed_card("gemini", T0), status="working", version=2)
                h.conn.hset(cp.PARTITION % (ROOM, "gemini"), ASK, json.dumps(working))
                original()
            pipe.multi = multi
            return pipe
        h.conn.pipeline = racing_pipeline
        self.assertEqual(h.relay.tick()["expired"], [])
        card = json.loads(h.conn.hget(cp.PARTITION % (ROOM, "gemini"), ASK))
        self.assertEqual(card["status"], "working")
        self.assertEqual(h.rows, [])


class NeverAnswers(unittest.TestCase):
    def test_no_relay_row_can_close_an_ask_in_the_bridge(self):
        """settle() closes an ask on any answers= row from its agent. Relay rows carry none, and come from
        bus-reconciler, which is not in ANSWERS_FROM for any agent."""
        info = {"agent": "gemini", "claimed_at": cb.stamp(T0), "room": ROOM}
        for spec in (cr.pointer_spec(ASK, info), cr.overdue_spec(ASK, info, T0), cr.expired_spec(ASK, info, T0)):
            self.assertNotIn("|answers=", spec["payload"])
            self.assertNotIn("\n", spec["payload"])
            self.assertLess(len(spec["payload"]), 1500)
        for tags in cb.ANSWERS_FROM.values():
            self.assertNotIn(cr.SOURCE, tags)


if __name__ == "__main__":
    unittest.main()


class InTheLiveRun(unittest.TestCase):
    def test_the_relay_runs_only_with_its_flag_and_a_running_bridge_and_stops_with_the_run(self):
        from unittest import mock
        sys.path.insert(0, str(REPO / "cloud" / "bus-reconciler"))
        import serve_requests as sr
        events = []

        class Worker:
            canvas_room = "workshop"

            def __init__(self, name):
                self.name = name
                self.stop = mock.Mock(set=lambda: events.append(("stop", name)))
                self.thread = mock.Mock(join=lambda timeout: events.append(("join", name)))
                self.settle = lambda rows: None

        start = lambda conn, bucket, log, holds: Worker("publisher") if bucket else None     # noqa: E731
        start_bridge = lambda conn, log, holds: Worker("bridge")                             # noqa: E731
        start_relay = lambda conn, log, holds, append, on_board: Worker("relay")             # noqa: E731
        base = {"CANVAS_BUCKET": "sfdc24-canvas", "CALL_BRIDGE": "on"}
        with mock.patch.object(sr, "live", return_value=0), \
                mock.patch.object(sr, "appender", return_value=lambda spec: None):
            sr.live_with_canvas(None, {}, "", "me", T0, 10, 300, start=start, start_bridge=start_bridge,
                                environ=dict(base, CALL_RELAY="on"), start_relay=start_relay)
            self.assertIn(("stop", "relay"), events)
            # Off unless exactly "on", and never without the bridge it relays for.
            for env in (base, dict(base, CALL_RELAY="1"), {"CANVAS_BUCKET": "sfdc24-canvas", "CALL_RELAY": "on"}):
                events.clear()
                sr.live_with_canvas(None, {}, "", "me", T0, 10, 300, start=start, start_bridge=start_bridge,
                                    environ=env, start_relay=start_relay)
                self.assertNotIn(("stop", "relay"), events, env)


class BoardLookup(unittest.TestCase):
    def test_row_id_is_compared_exactly_and_a_failed_read_is_unknown(self):
        from unittest import mock
        import bus
        rid = cr.row_id("pointer", ASK)
        quoting = ["OTHER-ROW", "", "grok", "", "", "BCB|v=1|id=OTHER|mentions=%s" % rid]
        with mock.patch.object(bus, "read_rows", return_value={"rows": [quoting]}):
            self.assertFalse(cr.board_lookup({})(rid))          # match is a substring: a quote is not the row
        with mock.patch.object(bus, "read_rows", return_value={"rows": [quoting, [rid, "", "bus-reconciler"]]}):
            self.assertTrue(cr.board_lookup({})(rid))
        with mock.patch.object(bus, "read_rows", side_effect=RuntimeError("404")):
            self.assertIsNone(cr.board_lookup({})(rid))

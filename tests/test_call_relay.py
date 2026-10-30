"""The call relay turns a claimed call ask into a ring every agent can hear, flags silence at 180 s, and closes asks
nobody started when the call ends. The ways that goes wrong: a ring sent twice, a relay thread that dies on the real
failure signal (SystemExit), an expiry decided on anything weaker than the chair's own end of call, and any write
that lands over work an agent already started, or after this run lost the worker lock."""
import datetime
import fnmatch
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import call_bridge as cb   # noqa: E402
import call_relay as cr    # noqa: E402
import canvas_publish as cp  # noqa: E402

T0 = datetime.datetime(2026, 10, 30, 15, 0, 0, tzinfo=datetime.timezone.utc)
ASK = "call-0123456789ab"
ROOM = "workshop"
CONF = "conf-0123456789abcdef-89abcdef"
EPOCH = "7"


class WatchError(Exception):
    pass


class FakeRedis:
    def __init__(self):
        self.kv, self.h, self.edits = {}, {}, {}

    def _bump(self, key):
        self.edits[key] = self.edits.get(key, 0) + 1

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        self._bump(key)
        return True

    def get(self, key):
        return self.kv.get(key)

    def hset(self, key, field=None, value=None, mapping=None):
        target = self.h.setdefault(key, {})
        if mapping:
            target.update(mapping)
        if field is not None:
            target[field] = value
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
    """redis-py's shape: after watch() commands run immediately; after multi() they queue; execute() raises
    WatchError when a watched key changed since watch()."""

    def __init__(self, conn):
        self.conn, self.watched, self.queue, self.in_multi = conn, {}, [], False

    def watch(self, *keys):
        self.watched = {k: self.conn.edits.get(k, 0) for k in keys}

    def multi(self):
        self.in_multi = True

    def execute(self):
        if any(self.conn.edits.get(k, 0) != v for k, v in self.watched.items()):
            raise WatchError("watched key changed")
        for name, a, kw in self.queue:
            getattr(self.conn, name)(*a, **kw)

    def reset(self):
        self.watched, self.queue, self.in_multi = {}, [], False

    def __getattr__(self, name):
        def call(*a, **kw):
            if not self.in_multi:
                return getattr(self.conn, name)(*a, **kw)
            self.queue.append((name, a, kw))
        return call


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def claim(conn, agent="gemini", at=T0, ask=ASK, conf=CONF, epoch=EPOCH):
    """What the bridge writes for one claim (call_bridge.Bridge._claim), minus the inbox words."""
    conn.hset(cp.PARTITION % (ROOM, agent), ask, json.dumps(cb.claimed_card(agent, at), sort_keys=True))
    record = {"agent": agent, "claimed_at": cb.stamp(at), "room": ROOM}
    if conf is not None:
        record.update(conf_room=conf, session_epoch=epoch)
    conn.hset(cb.OPEN_KEY, ask, json.dumps(record, sort_keys=True))


def call_live(conn, epoch=EPOCH):
    conn.hset(cr.STATE_KEY % CONF, mapping={"session_epoch": epoch})


def call_closed(conn, epoch=EPOCH):
    conn.hset(cr.STATE_KEY % CONF, mapping={"session_epoch": epoch, "closed": "1", "closed_epoch": epoch})


def working_card():
    return json.dumps(dict(cb.claimed_card("gemini", T0), status="working", version=2))


class Harness:
    def __init__(self, now=T0 + datetime.timedelta(seconds=10), holds=True):
        self.conn, self.clock, self.rows, self.board = FakeRedis(), Clock(now), [], {}
        self.holding, self.fail_with, self.unreadable, self.lands = holds, None, False, True
        self.relay = cr.Relay(self.conn, logging.getLogger("test"), lambda: self.holding, self.append,
                              self.on_board, clock=self.clock)

    def append(self, spec):
        """The live appender: append.py raises SystemExit when the read-back does not find the row exactly once.
        `lands` says whether the row reached the board anyway (the 404-that-wrote case)."""
        self.rows.append(spec)
        if self.lands:
            self.board[spec["row_id"]] = spec
        if self.fail_with is not None:
            raise self.fail_with

    def on_board(self, rid):
        return None if self.unreadable else rid in self.board

    def kinds(self):
        return [r["row_id"].split("-")[1] for r in self.rows]

    def later(self, seconds):
        self.clock.now += datetime.timedelta(seconds=seconds)


class Pointer(unittest.TestCase):
    def test_a_claim_gets_exactly_one_pointer(self):
        h = Harness()
        call_live(h.conn)
        claim(h.conn)
        self.assertEqual(h.relay.tick()["pointed"], [ASK])
        h.later(5)
        self.assertEqual(h.relay.tick()["pointed"], [])
        self.assertEqual(h.kinds(), ["POINTER"])

    def test_the_pointer_carries_the_id_never_the_words_and_never_answers(self):
        h = Harness()
        call_live(h.conn)
        claim(h.conn)
        h.relay.tick()
        payload = h.rows[0]["payload"]
        self.assertIn("call=%s" % ASK, payload)
        self.assertIn("inbox=fleet:inbox:gemini", payload)
        self.assertEqual("", cb._field(payload, "answers"))      # the parser settle uses
        self.assertEqual(h.rows[0]["source_tag"], "bus-reconciler")

    def test_codex_is_addressed_to_aya_too(self):
        h = Harness()
        call_live(h.conn)
        claim(h.conn, agent="chatgpt-codex-desktop")
        h.relay.tick()
        self.assertIn("|to=aya,chatgpt-codex-desktop|", h.rows[0]["payload"])

    def test_system_exit_from_append_keeps_the_reservation_and_the_relay_alive(self):
        h = Harness()
        h.fail_with, h.lands = SystemExit(1), False
        call_live(h.conn)
        claim(h.conn)
        h.relay.tick()                                            # must not raise
        self.assertEqual(json.loads(h.conn.get(cr.POINTER_KEY % ASK))["phase"], "reserved")
        h.fail_with = None
        h.later(cr.OVERDUE_SECONDS)
        self.assertEqual(h.relay.tick()["overdue"], [ASK])        # the relay kept working

    def test_an_unconfirmed_pointer_that_landed_is_marked_posted_and_never_resent(self):
        h = Harness()
        h.fail_with = SystemExit(1)                               # read-back failed, the row landed anyway
        call_live(h.conn)
        claim(h.conn)
        h.relay.tick()
        h.fail_with = None
        h.later(cr.LEASE_SECONDS + 5)
        h.relay.tick()
        self.assertEqual(h.kinds().count("POINTER"), 1)
        self.assertEqual(json.loads(h.conn.get(cr.POINTER_KEY % ASK))["phase"], "posted")

    def test_an_absent_pointer_after_the_lease_is_lost_never_retried(self):
        """Codex on 11b0c0fa: a delayed original can land after a retry, so a retry is a duplicate Row_ID."""
        h = Harness()
        h.fail_with, h.lands = SystemExit(1), False
        call_live(h.conn)
        claim(h.conn)
        h.relay.tick()
        h.fail_with = None
        for _ in range(4):
            h.later(cr.LEASE_SECONDS + 5)
            h.relay.tick()
        self.assertEqual(h.kinds().count("POINTER"), 1)
        self.assertEqual(json.loads(h.conn.get(cr.POINTER_KEY % ASK))["phase"], "lost")
        self.assertIn("OVERDUE", h.kinds())                       # the second ring still went out

    def test_an_unreadable_board_leaves_the_reservation_alone(self):
        h = Harness()
        h.fail_with, h.lands = SystemExit(1), False
        call_live(h.conn)
        claim(h.conn)
        h.relay.tick()
        h.fail_with, h.unreadable = None, True
        h.later(cr.LEASE_SECONDS + 5)
        h.relay.tick()
        self.assertEqual(json.loads(h.conn.get(cr.POINTER_KEY % ASK))["phase"], "reserved")

    def test_nothing_is_written_without_the_worker_lock(self):
        h = Harness(holds=False)
        call_live(h.conn)
        claim(h.conn)
        h.relay.tick()
        self.assertEqual(h.rows, [])


class Overdue(unittest.TestCase):
    def test_silence_at_180_seconds_is_flagged_once_and_cc_the_outcome_owner(self):
        h = Harness()
        call_live(h.conn)
        claim(h.conn)
        h.relay.tick()
        h.clock.now = T0 + datetime.timedelta(seconds=cr.OVERDUE_SECONDS - 1)
        h.relay.tick()
        h.clock.now = T0 + datetime.timedelta(seconds=cr.OVERDUE_SECONDS)
        h.relay.tick()
        h.later(60)
        h.relay.tick()
        self.assertEqual(h.kinds(), ["POINTER", "OVERDUE"])
        self.assertIn("|cc=aya|", h.rows[1]["payload"])
        self.assertIn("aya", h.rows[1]["target_surface"].split(";"))

    def test_an_agent_that_started_is_not_overdue(self):
        h = Harness()
        call_live(h.conn)
        claim(h.conn)
        h.relay.tick()
        h.conn.hset(cp.PARTITION % (ROOM, "gemini"), ASK, working_card())
        h.clock.now = T0 + datetime.timedelta(seconds=cr.OVERDUE_SECONDS + 30)
        h.relay.tick()
        self.assertEqual(h.kinds(), ["POINTER"])


class Expired(unittest.TestCase):
    def test_the_chair_closing_the_call_closes_the_ask(self):
        h = Harness()
        call_closed(h.conn)
        claim(h.conn)
        self.assertEqual(h.relay.tick()["expired"], [ASK])
        card = json.loads(h.conn.hget(cp.PARTITION % (ROOM, "gemini"), ASK))
        self.assertEqual((card["type"], card["status"], card["version"]), ("note", "review", 2))
        self.assertIsNotNone(cp.clean_item("gemini", ASK, json.dumps(card)))     # it really publishes
        self.assertIsNone(h.conn.hget(cb.OPEN_KEY, ASK))
        self.assertEqual(h.kinds(), ["EXPIRED"])
        self.assertIn("|expires=%s|" % ASK, h.rows[0]["payload"])
        self.assertEqual("", cb._field(h.rows[0]["payload"], "answers"))

    def test_a_newer_epoch_in_the_room_means_the_ask_s_call_ended(self):
        h = Harness()
        call_live(h.conn, epoch="8")
        claim(h.conn)
        self.assertEqual(h.relay.tick()["expired"], [ASK])

    def test_no_record_of_the_end_is_never_an_end(self):
        """Codex on 11b0c0fa: a closed window is not evidence. A live call, a missing state hash, an ask without
        conf_room (claimed before this change) and a chair that died without closing all stay open."""
        for setup in (lambda c: call_live(c), lambda c: None):
            h = Harness()
            setup(h.conn)
            claim(h.conn)
            self.assertEqual(h.relay.tick()["expired"], [])
            self.assertIsNotNone(h.conn.hget(cb.OPEN_KEY, ASK))
        h = Harness()
        call_closed(h.conn)
        claim(h.conn, conf=None)
        self.assertEqual(h.relay.tick()["expired"], [])

    def test_an_expired_ask_is_never_pointed_or_revived(self):
        h = Harness()
        call_closed(h.conn)
        claim(h.conn)
        h.relay.tick()
        call_live(h.conn, epoch=EPOCH)
        h.conn.hset(cr.STATE_KEY % CONF, "closed", "0")
        h.later(600)
        h.relay.tick()
        self.assertEqual(h.kinds(), ["EXPIRED"])

    def test_started_work_is_never_expired(self):
        h = Harness()
        call_closed(h.conn)
        claim(h.conn)
        h.conn.hset(cp.PARTITION % (ROOM, "gemini"), ASK, working_card())
        h.relay.tick()
        self.assertEqual(json.loads(h.conn.hget(cp.PARTITION % (ROOM, "gemini"), ASK))["status"], "working")
        self.assertIsNotNone(h.conn.hget(cb.OPEN_KEY, ASK))
        self.assertEqual(h.rows, [])

    def _race(self, h, at, mutate):
        """Run `mutate` inside the expiry, either just before WATCH ("watch") or just before EXEC ("multi")."""
        real = h.conn.pipeline

        def racing(transaction=True):
            pipe = real(transaction)
            original = getattr(pipe, at)

            def hooked(*a):
                mutate()
                return original(*a)
            setattr(pipe, at, hooked)
            return pipe
        h.conn.pipeline = racing

    def test_a_card_written_during_or_before_the_watch_wins(self):
        for at in ("watch", "multi"):
            with self.subTest(at=at):
                h = Harness()
                call_closed(h.conn)
                claim(h.conn)
                self._race(h, at, lambda: h.conn.hset(cp.PARTITION % (ROOM, "gemini"), ASK, working_card()))
                self.assertEqual(h.relay.tick()["expired"], [])
                self.assertEqual(json.loads(h.conn.hget(cp.PARTITION % (ROOM, "gemini"), ASK))["status"], "working")
                self.assertEqual(h.rows, [])

    def test_losing_the_worker_lock_before_or_during_the_watch_writes_nothing(self):
        """Lost after WATCH: EXEC aborts. Lost before WATCH: WATCH sees the new value as its baseline, so only the
        re-check of the lock under WATCH can stop the write."""
        for at in ("watch", "multi"):
            with self.subTest(at=at):
                h = Harness()
                call_closed(h.conn)
                claim(h.conn)

                def lose(h=h):
                    h.conn.set(cr.WORKER_LOCK_KEY, "another-run")
                    h.holding = False
                self._race(h, at, lose)
                self.assertEqual(h.relay.tick()["expired"], [])
                self.assertEqual(json.loads(h.conn.hget(cp.PARTITION % (ROOM, "gemini"), ASK))["status"], "claimed")
                self.assertIsNotNone(h.conn.hget(cb.OPEN_KEY, ASK))

    def test_the_call_reopening_before_exec_writes_nothing(self):
        h = Harness()
        call_closed(h.conn)
        claim(h.conn)
        self._race(h, "multi", lambda: h.conn.hset(cr.STATE_KEY % CONF, "closed", "0"))
        self.assertEqual(h.relay.tick()["expired"], [])
        self.assertIsNotNone(h.conn.hget(cb.OPEN_KEY, ASK))


class NeverAnswers(unittest.TestCase):
    def test_no_relay_row_can_close_an_ask_in_the_bridge(self):
        """settle() closes an ask on any answers= field from its agent. Relay rows carry none, by the parser settle
        uses, and come from bus-reconciler, which is not in ANSWERS_FROM for any agent."""
        info = {"agent": "gemini", "claimed_at": cb.stamp(T0), "room": ROOM}
        for spec in (cr.pointer_spec(ASK, info), cr.overdue_spec(ASK, info, T0), cr.expired_spec(ASK, info, T0)):
            self.assertEqual("", cb._field(spec["payload"], "answers"))
            self.assertNotIn("\n", spec["payload"])
            self.assertLess(len(spec["payload"]), 1500)
        for tags in cb.ANSWERS_FROM.values():
            self.assertNotIn(cr.SOURCE, tags)


class InTheLiveRun(unittest.TestCase):
    def test_the_relay_runs_only_with_its_flag_and_a_running_bridge_and_stops_with_the_run(self):
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
            for env in (base, dict(base, CALL_RELAY="1"), {"CANVAS_BUCKET": "sfdc24-canvas", "CALL_RELAY": "on"}):
                events.clear()
                sr.live_with_canvas(None, {}, "", "me", T0, 10, 300, start=start, start_bridge=start_bridge,
                                    environ=env, start_relay=start_relay)
                self.assertNotIn(("stop", "relay"), events, env)

    def test_a_system_exit_inside_a_tick_does_not_end_the_thread(self):
        h = Harness()
        h.relay.tick = mock.Mock(side_effect=[SystemExit(2), None])
        h.relay.stop.wait = mock.Mock(side_effect=[False, True])
        h.relay.stop.is_set = mock.Mock(side_effect=[False, False, True])
        h.relay.run(interval=0)                                   # returns normally, no SystemExit escapes
        self.assertEqual(h.relay.tick.call_count, 2)


class BoardLookup(unittest.TestCase):
    def test_row_id_is_compared_exactly_and_a_failed_read_is_unknown(self):
        import bus
        rid = cr.row_id("pointer", ASK)
        quoting = ["OTHER-ROW", "", "grok", "", "", "BCB|v=1|id=OTHER|mentions=%s" % rid]
        with mock.patch.object(bus, "read_rows", return_value={"rows": [quoting]}):
            self.assertFalse(cr.board_lookup({})(rid))          # match is a substring: a quote is not the row
        with mock.patch.object(bus, "read_rows", return_value={"rows": [quoting, [rid, "", "bus-reconciler"]]}):
            self.assertTrue(cr.board_lookup({})(rid))
        for failure in (RuntimeError("404"), SystemExit("bus never returned rows")):
            with mock.patch.object(bus, "read_rows", side_effect=failure):
                self.assertIsNone(cr.board_lookup({})(rid))


if __name__ == "__main__":
    unittest.main()

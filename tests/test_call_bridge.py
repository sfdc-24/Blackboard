"""scripts/call_bridge.py: the owner's spoken ask in the room becomes a claimed card, and a done card when answered.

What matters: an ask is claimed once and only once, only while the room's canvas window is open and the lease is
this run's, only for an agent he NAMED with a task; his words reach the agent's inbox and never the public card;
the done flip comes only from that agent's own answer and never overwrites the agent's own card. No network.
Run: python tests/test_call_bridge.py
"""
import datetime
import fnmatch
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "cloud" / "bus-reconciler"))

import bus_request as br                                                 # noqa: E402
import call_bridge as cb                                                 # noqa: E402
import canvas_publish as cp                                              # noqa: E402

NOW = datetime.datetime(2026, 10, 24, 14, 2, 0, tzinfo=datetime.timezone.utc)      # 10:02 ET
ROOM = "conf-0123456789abcdef-89abcdef"
STREAM = "v1:conf:%s:events" % ROOM
LOG = logging.getLogger("test-call-bridge")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class FakeRedis:
    """Strings, hashes and streams, with a MULTI that queues and runs in order (no rollback, like Redis)."""

    def __init__(self):
        self.store, self.hashes, self.streams, self.ttls, self.seq, self.edits = {}, {}, {}, {}, 0, {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        self.edits[key] = self.edits.get(key, 0) + 1
        return 1

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, field):
        self.edits[key] = self.edits.get(key, 0) + 1
        return 1 if self.hashes.get(key, {}).pop(field, None) is not None else 0

    def expire(self, key, seconds):
        self.ttls[key] = seconds
        return 1

    def xadd(self, key, fields, entry_id=None):
        if entry_id is None:
            self.seq += 1
            entry_id = "%d-%d" % (int(NOW.timestamp() * 1000), self.seq)
        self.streams.setdefault(key, []).append((entry_id, dict(fields)))
        return entry_id

    def xrange(self, key, min="-", max="+", count=None):
        def n(entry_id):
            ms, _, s = entry_id.partition("-")
            return int(ms), int(s or 0)
        out = [e for e in self.streams.get(key, []) if min == "-" or n(e[0]) >= n(min)]
        return out[:count] if count else out

    def scan_iter(self, match="*", count=None):
        keys = list(self.store) + list(self.hashes) + list(self.streams)
        return [k for k in keys if fnmatch.fnmatchcase(k, match)]

    def pipeline(self, transaction=True):
        return FakePipe(self)


class FakePipe:
    """redis-py's shape: after watch() commands run immediately; after multi() they queue; execute() runs the
    queue, or raises WatchError when a watched key changed since watch() (writes are versioned in `edits`)."""

    def __init__(self, conn):
        self.conn, self.queued, self.watched, self.in_multi = conn, [], None, False

    def watch(self, *keys):
        self.watched = {k: self.conn.edits.get(k, 0) for k in keys}

    def multi(self):
        self.in_multi = True

    def reset(self):
        self.queued, self.watched, self.in_multi = [], None, False

    def execute(self):
        if self.watched and any(self.conn.edits.get(k, 0) != v for k, v in self.watched.items()):
            self.queued = []
            raise cb.WATCH_ERRORS[0]("watched key changed")
        out = [getattr(self.conn, name)(*a, **kw) for name, a, kw in self.queued]
        self.queued = []
        return out

    def __getattr__(self, name):
        def call(*a, **kw):
            if self.watched is not None and not self.in_multi:
                return getattr(self.conn, name)(*a, **kw)
            self.queued.append((name, a, kw))
        return call


def at(seconds_ago=0):
    return "%d-0" % int((NOW.timestamp() - seconds_ago) * 1000)


def turn(conn, utterance, reason="vocative", actor="owner-1", seconds_ago=10, kind="turn.started", **extra):
    fields = {"kind": kind, "actor_id": actor, "reason": reason, "room_id": ROOM, "control_sequence": "3"}
    if utterance is not None:
        fields["utterance"] = utterance
    fields.update(extra)
    return conn.xadd(STREAM, fields, entry_id=at(seconds_ago))


def open_window(conn, room="workshop", minutes=60):
    until = NOW + datetime.timedelta(minutes=minutes)
    conn.set(cp.WINDOW_PREFIX + room, json.dumps({"until": until.strftime("%Y-%m-%dT%H:%M:%SZ"), "by": "grok"}))


def bridge(conn, holds=True, clock=None):
    lease = holds if callable(holds) else (lambda: holds)
    return cb.Bridge(conn, LOG, lease, clock=clock or (lambda: NOW))


def card(conn, agent, ask, room="workshop"):
    raw = conn.hget(cp.PARTITION % (room, agent), ask)
    return json.loads(raw) if raw else None


def row(sender, payload):
    cells = [""] * 10
    cells[br.ROW_ID], cells[br.SOURCE], cells[br.PAYLOAD] = "r-1", sender, payload
    return cells


class WhatIsAnAsk(unittest.TestCase):
    def test_a_named_agent_with_a_task_verb(self):
        f = {"kind": "turn.started", "actor_id": "owner-1", "reason": "vocative"}
        self.assertEqual(["chatgpt-codex-desktop"], cb.asks_in(dict(f, utterance="Codex, draft the queue flow.")))
        self.assertEqual(["grok", "gemini"], cb.asks_in(dict(f, utterance="Grok and Gemini, compare the two plans.")))
        self.assertEqual(["claude-code-cli"], cb.asks_in(dict(f, reason="mention", utterance="Can Claude build it?")))

    def test_the_chair_s_heard_spellings_name_the_voice_too(self):
        f = {"kind": "turn.started", "actor_id": "owner-1", "reason": "vocative"}
        self.assertEqual(["chatgpt-codex-desktop"], cb.asks_in(dict(f, utterance="Kodex, mock up the screen.")))
        self.assertEqual(["grok"], cb.asks_in(dict(f, utterance="Groq, list the risks.")))

    def test_a_question_is_not_a_task(self):
        f = {"kind": "turn.started", "actor_id": "owner-1", "reason": "vocative"}
        self.assertEqual([], cb.asks_in(dict(f, utterance="Codex, what do you think?")))

    def test_nobody_named_is_nobody_asked(self):
        f = {"kind": "turn.started", "actor_id": "owner-1", "reason": "vocative"}
        for reason in ("everyone", "room", "host", "continuity", "none", ""):
            self.assertEqual([], cb.asks_in(dict(f, reason=reason, utterance="Codex, draft it.")), reason)
        self.assertEqual([], cb.asks_in(dict(f, utterance="Draft the flow, please.")))
        self.assertEqual([], cb.asks_in(dict(f, utterance="Open Codex's PR and check it.")))

    def test_only_an_owner_turn_with_his_words(self):
        f = {"kind": "turn.started", "actor_id": "owner-1", "reason": "vocative", "utterance": "Codex, draft it."}
        self.assertEqual([], cb.asks_in(dict(f, kind="floor.granted")))
        self.assertEqual([], cb.asks_in(dict(f, actor_id="chair")))
        self.assertEqual([], cb.asks_in(dict(f, actor_id="")))
        self.assertEqual([], cb.asks_in(dict(f, utterance="")))
        no_words = dict(f)
        del no_words["utterance"]
        self.assertEqual([], cb.asks_in(no_words))       # CONF_REDIS_TEXT off, or a confidential stretch


class Claim(unittest.TestCase):
    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def test_an_ask_becomes_a_claimed_card_and_an_inbox_entry(self):
        turn(self.conn, "Codex, draft the queue flow for the vans.")
        [ask] = bridge(self.conn).tick()
        self.assertRegex(ask, r"^call-[0-9a-f]{12}$")
        got = card(self.conn, "chatgpt-codex-desktop", ask)
        self.assertEqual(("claimed", 1, "2026-10-24T14:02:00Z"), (got["status"], got["version"], got["ts"]))
        self.assertEqual("Claimed by Codex, 10:02 ET", got["title"])
        self.assertIsNotNone(cp.clean_item("chatgpt-codex-desktop", ask, json.dumps(got)), "the publisher takes it")
        [(_, entry)] = self.conn.streams["fleet:inbox:chatgpt-codex-desktop"]
        self.assertEqual("call-bridge", entry["by"])
        self.assertIn("draft the queue flow for the vans", entry["text"])
        self.assertIn("answers=%s" % ask, entry["text"])
        self.assertIn(ask, self.conn.hgetall(cb.OPEN_KEY))

    def test_his_words_never_reach_the_public_card(self):
        turn(self.conn, "Codex, draft the price sheet for the client's Port-au-Prince store.")
        [ask] = bridge(self.conn).tick()
        published = json.dumps(cp.snapshot(self.conn, "workshop", NOW))
        for word in ("price sheet", "Port-au-Prince", "client"):
            self.assertNotIn(word, published)
        self.assertIn(ask, published)

    def test_once_per_ask_however_often_it_is_read(self):
        turn(self.conn, "Codex, draft the queue flow.")
        first = bridge(self.conn).tick()
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)               # a lost cursor re-reads the stream
        self.assertEqual([], bridge(self.conn).tick())
        self.assertEqual(1, len(first))
        self.assertEqual(1, len(self.conn.streams["fleet:inbox:chatgpt-codex-desktop"]))

    def test_two_names_two_asks(self):
        turn(self.conn, "Grok and Gemini, compare the two plans.")
        asks = bridge(self.conn).tick()
        self.assertEqual(2, len(set(asks)))
        self.assertIsNotNone(card(self.conn, "grok", asks[0]))
        self.assertIsNotNone(card(self.conn, "gemini", asks[1]))

    def test_nothing_while_the_window_is_shut_or_the_lease_is_not_ours(self):
        turn(self.conn, "Codex, draft the queue flow.")
        self.assertEqual([], bridge(self.conn, holds=False).tick())
        shut = FakeRedis()
        shut.streams = self.conn.streams
        self.assertEqual([], bridge(shut).tick())
        open_window(shut, room="other-room")
        self.assertEqual([], bridge(shut).tick(), "another room's window is not this one")
        self.assertNotIn("fleet:inbox:chatgpt-codex-desktop", self.conn.streams)

    def test_a_lease_lost_mid_pass_writes_nothing_more(self):
        turn(self.conn, "Codex, draft the queue flow.", seconds_ago=20)
        turn(self.conn, "Grok, list the risks.", seconds_ago=10)
        answers = iter([True, True, False])
        self.assertEqual(1, len(bridge(self.conn, holds=lambda: next(answers, False)).tick()))
        self.assertNotIn("fleet:inbox:grok", self.conn.streams)

    def test_an_old_call_is_never_replayed(self):
        turn(self.conn, "Codex, draft the queue flow.", seconds_ago=cb.LOOKBACK_SECONDS + 60)
        self.assertEqual([], bridge(self.conn).tick())
        turn(self.conn, "Grok, list the risks.", seconds_ago=5)
        self.assertEqual(1, len(bridge(self.conn).tick()))

    def test_the_cursor_moves_past_what_was_read(self):
        last = turn(self.conn, "Codex, what do you think?")
        bridge(self.conn).tick()
        self.assertEqual(last, self.conn.get(cb.CURSOR_KEY % ROOM))
        turn(self.conn, "Codex, draft it.", seconds_ago=0)
        self.assertEqual(1, len(bridge(self.conn).tick()))

    def test_every_key_it_keeps_is_under_gov_and_expires(self):
        turn(self.conn, "Codex, draft the queue flow.")
        [ask] = bridge(self.conn).tick()
        for key in (cb.ASK_KEY % ask, cb.CURSOR_KEY % ROOM, cb.OPEN_KEY):
            self.assertTrue(key.startswith("gov:"), key)
            self.assertEqual(cb.TTL_SECONDS, self.conn.ttls.get(key), key)


class Done(unittest.TestCase):
    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)
        turn(self.conn, "Codex, draft the queue flow.")
        [self.ask] = bridge(self.conn).tick()
        self.later = NOW + datetime.timedelta(minutes=7)

    def settle(self, rows):
        return bridge(self.conn, clock=lambda: self.later).settle(rows)

    def test_the_agent_s_answer_flips_its_card_to_done_with_the_finish_time(self):
        payload = "BCB|v=1|id=AYA-X|phase=RESULT|from=aya|answers=%s|text=done" % self.ask
        self.assertEqual([self.ask], self.settle([row("aya", payload)]))
        got = card(self.conn, "chatgpt-codex-desktop", self.ask)
        self.assertEqual(("done", 2, "2026-10-24T14:09:00Z"), (got["status"], got["version"], got["ts"]))
        self.assertEqual("Done by Codex, 10:09 ET", got["title"])
        self.assertIn("(7 min)", got["body"])
        self.assertNotIn(self.ask, self.conn.hgetall(cb.OPEN_KEY))
        self.assertEqual([], self.settle([row("aya", payload)]), "flipped once")

    def test_an_answer_among_several_ids_counts(self):
        payload = "BCB|v=1|id=X|from=chatgpt-codex-desktop|answers=OTHER-1;%s,OTHER-2|text=ok" % self.ask
        self.assertEqual([self.ask], self.settle([row("chatgpt-codex-desktop", payload)]))

    def test_another_agent_s_row_does_not_flip_it(self):
        payload = "BCB|v=1|id=GROK-X|from=grok|answers=%s|text=done" % self.ask
        self.assertEqual([], self.settle([row("grok", payload)]))
        self.assertEqual("claimed", card(self.conn, "chatgpt-codex-desktop", self.ask)["status"])

    def test_the_agent_s_own_card_under_the_same_id_is_never_overwritten(self):
        own = {"v": 1, "type": "mermaid", "status": "done", "public": True, "version": 3, "ts": "x",
               "title": "Queue flow", "body": "flowchart LR\n  a-->b\n"}
        self.conn.hset(cp.PARTITION % ("workshop", "chatgpt-codex-desktop"), self.ask, json.dumps(own))
        self.settle([row("aya", "BCB|v=1|id=X|from=aya|answers=%s|text=done" % self.ask)])
        self.assertEqual(own, card(self.conn, "chatgpt-codex-desktop", self.ask))
        self.assertNotIn(self.ask, self.conn.hgetall(cb.OPEN_KEY))

    def test_a_terminal_card_written_during_the_flip_is_never_overwritten(self):
        """Cursor on 11b0c0fa: the call relay's expiry can commit between _flip's read and its write. The card is
        re-read under WATCH, so the done card never lands over it; the ask still leaves the open set."""
        expired = {"v": 1, "type": "note", "status": "review", "public": True, "version": 2, "ts": "x",
                   "title": "Not started: the call ended", "body": "closed"}
        partition = cp.PARTITION % ("workshop", "chatgpt-codex-desktop")
        real = self.conn.pipeline
        raced = []

        def racing(transaction=True):
            pipe = real(transaction)
            original = pipe.multi

            def multi():
                if not raced:                    # the relay commits once, between the read and EXEC
                    raced.append(1)
                    self.conn.hset(partition, self.ask, json.dumps(expired))
                original()
            pipe.multi = multi
            return pipe
        self.conn.pipeline = racing
        self.settle([row("aya", "BCB|v=1|id=X|from=aya|answers=%s|text=done" % self.ask)])
        self.assertEqual(expired, card(self.conn, "chatgpt-codex-desktop", self.ask))
        self.assertNotIn(self.ask, self.conn.hgetall(cb.OPEN_KEY))

    def test_the_open_record_names_the_call_it_came_from(self):
        conn = FakeRedis()
        open_window(conn)
        turn(conn, "Gemini, draw the quote flow.", session_epoch="7")
        [ask] = bridge(conn).tick()
        info = json.loads(conn.hgetall(cb.OPEN_KEY)[ask])
        self.assertEqual((ROOM, "7", "workshop"), (info["conf_room"], info["session_epoch"], info["room"]))

    def test_no_flip_without_the_lease(self):
        payload = "BCB|v=1|id=X|from=aya|answers=%s|text=done" % self.ask
        self.assertEqual([], bridge(self.conn, holds=False).settle([row("aya", payload)]))

    def test_rows_that_are_not_rows_are_passed_over(self):
        self.assertEqual([], self.settle([None, "text", [], ["only", "two"]]))


class Clock(unittest.TestCase):
    def test_eastern_time_follows_daylight_saving(self):
        self.assertEqual("10:02 ET", cb.eastern(NOW))
        self.assertEqual("09:02 ET", cb.eastern(datetime.datetime(2026, 11, 2, 14, 2, tzinfo=datetime.timezone.utc)))


class InTheLiveRun(unittest.TestCase):
    def test_the_bridge_runs_only_with_the_flag_and_a_publisher_and_settles_each_pass(self):
        import serve_requests as sr
        events = []

        class Worker:
            canvas_room = "workshop"

            def __init__(self, name):
                self.name = name
                self.stop = mock.Mock(set=lambda: events.append(("stop", name)))
                self.thread = mock.Mock(join=lambda timeout: events.append(("join", name)))
                self.settle = lambda rows: events.append(("settle", rows))

        start = lambda conn, bucket, log, holds: Worker("publisher") if bucket else None     # noqa: E731
        start_bridge = lambda conn, log, holds: Worker("bridge")                             # noqa: E731
        seen = []
        live = lambda *a: seen.append(a[7] if len(a) > 7 else None) or 0                    # noqa: E731
        with mock.patch.object(sr, "live", side_effect=live):
            sr.live_with_canvas(None, {}, "", "me", NOW, 10, 300, start=start, start_bridge=start_bridge,
                                environ={"CANVAS_BUCKET": "sfdc24-canvas", "CALL_BRIDGE": "on"})
            self.assertIsNotNone(seen[-1])
            seen[-1](["row"])
            self.assertIn(("settle", ["row"]), events)
            self.assertIn(("stop", "bridge"), events)
            for env in ({"CANVAS_BUCKET": "sfdc24-canvas"}, {"CANVAS_BUCKET": "sfdc24-canvas", "CALL_BRIDGE": "1"},
                        {"CALL_BRIDGE": "on"}):
                events.clear()
                sr.live_with_canvas(None, {}, "", "me", NOW, 10, 300, start=start, start_bridge=start_bridge,
                                    environ=env)
                self.assertIsNone(seen[-1], env)
                self.assertNotIn(("stop", "bridge"), events, env)

    def test_the_live_loop_hands_settle_to_each_pass_and_only_when_there_is_one(self):
        import serve_requests as sr
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        for settle, expected in ((print, {"settle": print}), (None, {})):
            with mock.patch.object(sr, "keep_lock", return_value=True), \
                    mock.patch.object(sr, "one_pass") as one_pass, \
                    mock.patch.object(sr.time, "sleep", side_effect=KeyboardInterrupt), \
                    mock.patch("git_requests.poll_seconds", return_value=60):
                with self.assertRaises(KeyboardInterrupt):
                    sr.live(None, {}, "", "me", until, 10, 300, settle)
            kwargs = one_pass.call_args.kwargs
            self.assertEqual(expected, {k: v for k, v in kwargs.items() if k == "settle"})

    def test_one_pass_hands_its_rows_to_settle_and_a_failing_settle_costs_nothing_else(self):
        import serve_requests as sr
        got = []
        with mock.patch.object(sr.bus_request, "heartbeat"), \
                mock.patch.object(sr.bus_reconcile, "board_rows", return_value=[["r"]]), \
                mock.patch.object(sr.bus_request, "handle",
                                  return_value={"answered": [], "refused": [], "capped": 0}) as handle:
            sr.one_pass(None, {}, "", with_git=False, quiet=True, settle=got.append)
            self.assertEqual([[["r"]]], got)

            def boom(rows):
                raise RuntimeError("redis flap")
            sr.one_pass(None, {}, "", with_git=False, quiet=True, settle=boom)
            self.assertEqual(2, handle.call_count)


if __name__ == "__main__":
    unittest.main()

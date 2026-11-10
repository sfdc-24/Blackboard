"""Regression contract for protected source-bound asks and authoritative completion.

Claims retain lease, routing, privacy, deduplication, delivery and latency guarantees.
Free-text RESULT is observation, never authority: exact source/revision-bound lifecycle
operations publish the worker artifact and complete it without overwriting its bytes.
Fixtures model protected source shape and Redis WATCH/MULTI, not live authentication. No network.
Run: python tests/test_call_bridge.py
"""
import datetime
import fnmatch
import hashlib
import json
import os
import re
import logging
import threading
import copy
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
import call_lifecycle as life                                            # noqa: E402

# These suites pin the claim path itself (cb.READBACK = False, a code setting no environment reaches): an ask
# claims at once. The ReadBack class below runs the default, read-back on, and passes readback=True explicitly.
cb.READBACK = False

NOW = datetime.datetime(2026, 10, 24, 14, 2, 0, tzinfo=datetime.timezone.utc)      # 10:02 ET
ROOM = "conf-0123456789abcdef-89abcdef"
STREAM = "v1:conf:%s:events" % ROOM
LOG = logging.getLogger("test-call-bridge")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class WatchConflict(RuntimeError):
    """Offline Redis WATCH conflict; no command in the transaction is executed."""


class FakeRedis:
    """Strings/hashes/streams and WATCH/MULTI semantics, with no external service.

    Watched reads are immediate; EXEC checks every watched key before writes.
    Runtime errors are returned (no rollback) when raise_on_error=False, matching
    the production Redis API used by the lifecycle controller.
    """
    def __init__(self):
        self.store, self.hashes, self.streams, self.ttls, self.seq = {}, {}, {}, {}, 0
        self.versions = {}
        self.mutex = threading.RLock()

    def touch(self, key):
        self.versions[key] = self.versions.get(key, 0) + 1

    def signature(self, key):
        return (self.versions.get(key, 0), copy.deepcopy(self.store.get(key)),
                copy.deepcopy(self.hashes.get(key)), copy.deepcopy(self.streams.get(key)))

    def type(self, key):
        return 'string' if key in self.store else 'hash' if key in self.hashes else 'stream' if key in self.streams else 'none'

    def set(self, key, value, nx=False, ex=None):
        with self.mutex:
            if nx and self.type(key) != 'none':
                return None
            self.store[key] = value
            self.hashes.pop(key, None); self.streams.pop(key, None)
            self.ttls[key] = ex
            self.touch(key)
            return True

    def get(self, key):
        return self.store.get(key)

    def hset(self, key, field=None, value=None, mapping=None):
        with self.mutex:
            if self.type(key) not in ('none', 'hash'):
                raise TypeError('WRONGTYPE')
            values = mapping if mapping is not None else {field: value}
            before = self.hashes.setdefault(key, {})
            added = sum(k not in before for k in values)
            before.update(values); self.touch(key)
            return added

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hsetnx(self, key, field, value):
        with self.mutex:
            if self.type(key) not in ('none', 'hash'):
                raise TypeError('WRONGTYPE')
            if field in self.hashes.get(key, {}):
                return 0
            self.hashes.setdefault(key, {})[field] = value; self.touch(key)
            return 1

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, *fields):
        with self.mutex:
            count = sum(self.hashes.get(key, {}).pop(field, None) is not None for field in fields)
            if count: self.touch(key)
            if key in self.hashes and not self.hashes[key]: self.hashes.pop(key)
            return count

    def delete(self, *keys):
        count = 0
        with self.mutex:
            for key in keys:
                count += self.type(key) != 'none'
                self.store.pop(key, None); self.hashes.pop(key, None); self.streams.pop(key, None)
                self.ttls.pop(key, None); self.touch(key)
        return count

    def rpush(self, key, *values):
        with self.mutex:
            self.lists = getattr(self, "lists", {})
            self.lists.setdefault(key, []).extend(values)
            self.touch(key)
            return len(self.lists[key])

    def lrange(self, key, start, stop):
        items = getattr(self, "lists", {}).get(key, [])
        return items[start:] if stop == -1 else items[start:stop + 1]

    def expire(self, key, seconds):
        self.ttls[key] = seconds
        self.touch(key)
        return int(self.type(key) != 'none')

    def xadd(self, key, fields, entry_id=None, maxlen=None, approximate=True):
        with self.mutex:
            if self.type(key) not in ('none', 'stream'):
                raise TypeError('WRONGTYPE')
            if entry_id is None:
                self.seq += 1
                entry_id = "%d-%d" % (int(NOW.timestamp() * 1000), self.seq)
            self.streams.setdefault(key, []).append((entry_id, dict(fields)))
            self.touch(key)
            return entry_id

    def xrange(self, key, min="-", max="+", count=None):
        def n(entry_id):
            ms, _, seq = entry_id.partition("-")
            return int(ms), int(seq or 0)
        out = [e for e in self.streams.get(key, [])
               if (min == "-" or n(e[0]) >= n(min)) and (max == "+" or n(e[0]) <= n(max))]
        return out[:count] if count else out

    def scan_iter(self, match="*", count=None):
        keys = list(self.store) + list(self.hashes) + list(self.streams)
        return [k for k in keys if fnmatch.fnmatchcase(k, match)]

    def pipeline(self, transaction=True):
        return FakePipe(self)


class FakePipe:
    def __init__(self, conn):
        self.conn, self.queued, self.watched, self.in_multi = conn, [], {}, False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.reset()
        return False

    def watch(self, *keys):
        with self.conn.mutex:
            for key in keys: self.watched.setdefault(key, self.conn.signature(key))

    def multi(self):
        self.in_multi = True

    def reset(self):
        self.queued, self.watched, self.in_multi = [], {}, False

    def execute(self, raise_on_error=True):
        with self.conn.mutex:
            if any(self.conn.signature(key) != old for key, old in self.watched.items()):
                self.reset()
                raise WatchConflict('watched key changed')
            out = []
            for name, a, kw in self.queued:
                try: out.append(getattr(self.conn, name)(*a, **kw))
                except Exception as error: out.append(error)
            self.reset()
            if raise_on_error:
                for result in out:
                    if isinstance(result, Exception): raise result
            return out

    def __getattr__(self, name):
        def command(*a, **kw):
            if self.watched and not self.in_multi:
                return getattr(self.conn, name)(*a, **kw)
            self.queued.append((name, a, kw))
            return self
        return command


def at(seconds_ago=0):
    return "%d-0" % int((NOW.timestamp() - seconds_ago) * 1000)


def turn(conn, utterance, reason="vocative", actor="own_fixture_owner", seconds_ago=10, kind="turn.started", **extra):
    # This is an inert fixture for the pinned protected chair producer, not a
    # claim that a caller-provided owner tag authenticates a real request.
    conn.hset("v1:conf:%s:state" % ROOM, mapping={"session_epoch": "fixture-epoch", "closed": "0", "history_gap": "0"})
    fields = {"kind": kind, "actor_id": actor, "reason": reason, "room_id": ROOM,
              "control_sequence": "3", "proj_n": "1", "session_epoch": "fixture-epoch"}
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


def put_card(conn, agent, ask, item=None, now=NOW):
    """Use the production CAS card operation; no direct generic HSET authority."""
    info = life.get(conn, ask)
    body = dict(item or {"v": 1, "type": "note", "status": "done", "public": True,
                         "version": 1, "ts": cb.stamp(now), "title": "Finished work", "body": "fixture output"},
                ask=ask, source_revision=info["source_revision"])
    supplied = {"source_revision": info["source_revision"], "task_revision": info["rev"], "card": body}
    return life.operate(conn, agent, ask, "card", supplied, "fixture-card-%s-%s" % (ask, info["rev"]), now)


def complete(conn, agent, ask, now=NOW, request_id=None):
    """No implicit card write or watch: completion must find exact stored artifact."""
    info = life.get(conn, ask)
    supplied = {"source_revision": info["source_revision"], "task_revision": info["rev"],
                "card_hash": info.get("card_hash")}
    return life.operate(conn, agent, ask, "complete", supplied, request_id or "fixture-complete-" + ask, now)


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
        self.assertEqual("Waiting for a worker: Cody's work from the room", got["title"], "no worker is wired for codex")
        self.assertIsNotNone(cp.clean_item("chatgpt-codex-desktop", ask, json.dumps(got)), "the publisher takes it")
        [(_, entry)] = self.conn.streams["fleet:inbox:chatgpt-codex-desktop"]
        self.assertEqual("call-bridge", entry["by"])
        self.assertIn("draft the queue flow for the vans", entry["text"])
        self.assertIn("answers=%s" % ask, entry["text"], "correlation cue is preserved, not completion authority")
        self.assertIn("tools/cli_call_helper/call_ask.py", entry["text"])
        self.assertIn("--input-file", entry["text"])
        self.assertIn("bound agent", entry["text"])
        # The owner, 2026-11-03 20:36Z: do the work, never HOLD for want of the helper.
        self.assertNotIn("HOLD", entry["text"])
        self.assertIn("Do the work now and put up your card through call-task", entry["text"])
        for op in ("op=status", "op=card", "op=complete"):
            self.assertIn(op, entry["text"], op)
        self.assertIn("ask=%s and that source_revision" % ask, entry["text"], "the card names its own ask")
        self.assertIn("work from this preview and say on your card that you did", entry["text"])
        self.assertIn("Only verified call-task card/complete receipts", entry["text"])
        self.assertIn("A free RESULT board row", entry["text"])
        self.assertIn("never completes this task", entry["text"])
        self.assertIn("Never post private task bytes to the board", entry["text"])
        self.assertNotIn("turns done when that row lands", entry["text"])
        self.assertIn(ask, self.conn.hgetall(cb.OPEN_KEY))

    def test_no_word_of_his_reaches_the_public_canvas(self):
        """O2 and Aya's privacy hold on 922e8da: the claimed and the done card carry who and when, never what he said."""
        said = "Codex, draft the price sheet for the client's Port-au-Prince store. Ask Pat Placeholder first."
        turn(self.conn, said)
        [ask] = bridge(self.conn).tick()
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]+", said) if w.lower() not in ("codex", "the", "for", "first")]
        claimed = card(self.conn, "chatgpt-codex-desktop", ask)
        self.assertEqual("claimed in the room", claimed["activity"])
        self.assertEqual("Claimed in the room at 10:02 ET. No worker picks up Cody's call asks yet; the ask waits in "
                         "Cody's inbox, and Cody's own card replaces this one when it is taken.", claimed["body"])
        later = NOW + datetime.timedelta(minutes=3)
        bridge(self.conn, clock=lambda: later).settle([row("chatgpt-codex-desktop",
                                                            "BCB|v=1|phase=RESULT|from=chatgpt-codex-desktop|answers=%s|text=ok" % ask)])
        self.assertEqual("OPEN", life.get(self.conn, ask)["state"], "RESULT cannot complete")
        put_card(self.conn, "chatgpt-codex-desktop", ask, now=later)
        complete(self.conn, "chatgpt-codex-desktop", ask, later)
        done = card(self.conn, "chatgpt-codex-desktop", ask + "-work")
        self.assertEqual("done", done["status"])
        self.assertEqual("DONE", life.get(self.conn, ask)["state"])
        published = json.dumps(cp.snapshot(self.conn, "workshop", NOW)) + json.dumps(claimed) + json.dumps(done)
        for word in words:
            self.assertNotIn(word, published, word)
        self.assertNotIn("asked", json.loads(self.conn.hgetall(cb.OPEN_KEY).get(ask) or "{}"), "nor kept beside the card")
        [(_, entry)] = self.conn.streams["fleet:inbox:chatgpt-codex-desktop"]
        self.assertIn("Port-au-Prince", entry["text"], "the agent still gets every word, in Redis")

    def test_once_per_ask_however_often_it_is_read(self):
        turn(self.conn, "Codex, draft the queue flow.")
        first = bridge(self.conn).tick()
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)               # a lost cursor re-reads the stream
        self.assertEqual([], bridge(self.conn).tick())
        self.assertEqual(1, len(first))
        self.assertEqual(1, len(self.conn.streams["fleet:inbox:chatgpt-codex-desktop"]))

    def test_two_names_one_team_ask_for_the_first_named(self):
        # R3 (conference docs/CARDS-ARCHITECTURE.md, Team cards): two voices on one task share ONE card, hosted by
        # the first voice he named; the chair runs both on it.
        turn(self.conn, "Grok and Gemini, compare the two plans.")
        asks = bridge(self.conn).tick()
        self.assertEqual(1, len(asks))
        self.assertIsNotNone(card(self.conn, "grok", asks[0]))
        self.assertNotIn("fleet:inbox:gemini", self.conn.streams)

    def test_nothing_while_the_window_is_shut_or_the_lease_is_not_ours(self):
        turn(self.conn, "Codex, draft the queue flow.")
        self.assertEqual([], bridge(self.conn, holds=False).tick())
        shut = FakeRedis()
        shut.streams = self.conn.streams
        self.assertEqual([], bridge(shut).tick())
        open_window(shut, room="other-room")
        self.assertEqual([], bridge(shut).tick(), "another room's window is not this one")
        self.assertNotIn("fleet:inbox:chatgpt-codex-desktop", self.conn.streams)

    def test_a_turn_the_agent_s_binder_holds_is_never_claimed(self):
        # The owner's S1 call (2026-11-09 18:38 ET): the chair handed the turn to Cody's binder and the bridge claimed
        # it as well, ringing Codex with a card job while the binder's ask expired.
        turn(self.conn, "Codex, draft the queue flow.", held="codex")
        self.assertEqual([], bridge(self.conn).tick())
        self.assertNotIn("fleet:inbox:chatgpt-codex-desktop", self.conn.streams)
        turn(self.conn, "Grok, list the risks.", seconds_ago=5, held="codex")
        self.assertEqual(1, len(bridge(self.conn).tick()), "only the held agent is skipped: Grok is still claimed")
        self.assertIn("fleet:inbox:grok", self.conn.streams)
        self.assertNotIn("fleet:inbox:chatgpt-codex-desktop", self.conn.streams)

    def test_a_turn_whose_event_is_gone_by_the_claim_is_not_claimed(self):
        # Codex HOLD on 56c457bf: the event read in the scan, then removed before _claim reads it again.
        entry = turn(self.conn, "Codex, draft the queue flow.")
        b = bridge(self.conn)
        real = self.conn.xrange

        def gone(key, min="-", max="+", count=None):
            if min == max == entry:
                return []                                   # trimmed between the scan and the claim
            return real(key, min=min, max=max, count=count)
        self.conn.xrange = gone
        self.assertEqual([], b.tick())
        self.assertNotIn("fleet:inbox:chatgpt-codex-desktop", self.conn.streams)

    def test_no_held_field_claims_as_before(self):
        turn(self.conn, "Codex, draft the queue flow.", held="")
        self.assertEqual(1, len(bridge(self.conn).tick()))

    def test_what_is_held(self):
        f = {"kind": "turn.started", "actor_id": "owner-1"}
        self.assertEqual({"chatgpt-codex-desktop", "grok"}, cb.held_agents(dict(f, held="codex, grok,nobody")))
        self.assertEqual(frozenset(), cb.held_agents(dict(f)))
        self.assertEqual(frozenset(), cb.held_agents(dict(f, actor_id="chair", held="codex")), "a chair turn holds none")
        self.assertEqual(frozenset(), cb.held_agents(dict(f, kind="floor.granted", held="codex")))

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
        """Historical blanket-TTL assertion is split: ephemeral cursors expire;
        task/ask fences deliberately persist so a delayed write cannot resurrect."""
        turn(self.conn, "Codex, draft the queue flow.")
        [ask] = bridge(self.conn).tick()
        for key in (cb.ASK_KEY % ask, cb.CURSOR_KEY % ROOM, cb.OPEN_KEY, life.TASK_KEY % ask, life.EXPIRY_KEY % ask):
            self.assertTrue(key.startswith("gov:"), key)
        self.assertEqual(cb.TTL_SECONDS, self.conn.ttls[cb.CURSOR_KEY % ROOM])
        self.assertEqual(life.MAX_AGE, self.conn.ttls[life.EXPIRY_KEY % ask])
        self.assertIsNone(self.conn.ttls.get(life.TASK_KEY % ask))
        self.assertIsNone(self.conn.ttls.get(cb.ASK_KEY % ask))
        self.assertIsNone(self.conn.ttls.get(cb.OPEN_KEY))
        self.assertEqual("OPEN", life.get(self.conn, ask)["state"])


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
        """Only exact production CAS completion closes work; RESULT is observation."""
        payload = "BCB|v=1|id=AYA-X|phase=RESULT|from=aya|answers=%s|text=done" % self.ask
        self.assertEqual([], self.settle([row("aya", payload)]))
        self.assertEqual("OPEN", life.get(self.conn, self.ask)["state"])
        put_card(self.conn, "chatgpt-codex-desktop", self.ask, now=self.later)
        artifact = card(self.conn, "chatgpt-codex-desktop", self.ask + "-work")
        receipt = complete(self.conn, "chatgpt-codex-desktop", self.ask, self.later)
        self.assertEqual(("DONE", 3), (receipt["state"], receipt["rev"]))
        self.assertEqual("2026-10-24T14:09:00Z", life.get(self.conn, self.ask)["closed_at"])
        self.assertEqual(("done", "2026-10-24T14:09:00Z"), (artifact["status"], artifact["ts"]))
        self.assertIsNone(card(self.conn, "chatgpt-codex-desktop", self.ask))
        self.assertEqual(artifact, card(self.conn, "chatgpt-codex-desktop", self.ask + "-work"))
        self.assertNotIn(self.ask, self.conn.hgetall(cb.OPEN_KEY))
        self.assertEqual([], self.settle([row("aya", payload)]))
        self.assertEqual("DONE", life.get(self.conn, self.ask)["state"])

    def test_an_answer_among_several_ids_counts(self):
        """A free-text list of IDs cannot select completion; exact bound ask can."""
        payload = "BCB|v=1|id=X|phase=RESULT|from=chatgpt-codex-desktop|answers=OTHER-1;%s,OTHER-2|text=ok" % self.ask
        self.assertEqual([], self.settle([row("chatgpt-codex-desktop", payload)]))
        self.assertEqual("OPEN", life.get(self.conn, self.ask)["state"])
        put_card(self.conn, "chatgpt-codex-desktop", self.ask, now=self.later)
        self.assertEqual(self.ask, complete(self.conn, "chatgpt-codex-desktop", self.ask, self.later)["ask"])
        self.assertEqual("DONE", life.get(self.conn, self.ask)["state"])

    def test_another_agent_s_row_does_not_flip_it(self):
        payload = "BCB|v=1|id=GROK-X|phase=RESULT|from=grok|answers=%s|text=done" % self.ask
        self.assertEqual([], self.settle([row("grok", payload)]))
        put_card(self.conn, "chatgpt-codex-desktop", self.ask, now=self.later)
        with self.assertRaises(life.Refused):
            complete(self.conn, "grok", self.ask, self.later)
        self.assertEqual("OPEN", life.get(self.conn, self.ask)["state"])
        self.assertEqual("claimed", card(self.conn, "chatgpt-codex-desktop", self.ask)["status"])

    def test_the_agent_s_own_card_under_the_same_id_is_never_overwritten(self):
        """The managed work field is preserved byte-for-byte by completion."""
        own = {"v": 1, "type": "mermaid", "status": "done", "public": True, "version": 3,
               "ts": cb.stamp(self.later), "title": "Queue flow", "body": "flowchart LR\n  a-->b\n"}
        put_card(self.conn, "chatgpt-codex-desktop", self.ask, own, self.later)
        partition = cp.PARTITION % ("workshop", "chatgpt-codex-desktop")
        before = self.conn.hget(partition, self.ask + "-work")
        self.assertEqual([], self.settle([row("aya", "BCB|v=1|id=X|phase=RESULT|from=aya|answers=%s|text=done" % self.ask)]))
        complete(self.conn, "chatgpt-codex-desktop", self.ask, self.later)
        self.assertEqual(before, self.conn.hget(partition, self.ask + "-work"))
        self.assertNotIn(self.ask, self.conn.hgetall(cb.OPEN_KEY))

    def test_no_flip_without_the_lease(self):
        payload = "BCB|v=1|id=X|phase=RESULT|from=aya|answers=%s|text=done" % self.ask
        self.assertEqual([], bridge(self.conn, holds=False).settle([row("aya", payload)]))

    def test_a_reply_that_is_not_finished_work_never_flips_it(self):
        """Aya's hold on 5d41cb5, item 1: a waker's STATED answer, a refusal, a hold or a plain reply is not Done."""
        for payload in ("BCB|v=1|id=X|from=aya|answers=%s|text=on it",
                        "BCB|v=1|id=X|phase=ASK|from=aya|answers=%s|text=need Redis access",
                        "BCB|v=1|id=X|phase=RESULT|from=aya|answers=%s|wakerreply=1|evidence=STATED|text=REPLY: ok",
                        "BCB|v=1|id=X|phase=RESULT|from=aya|answers=%s|evidence=STATED|text=I think so",
                        "BCB|v=1|id=X|phase=RESULT|from=aya|answers=%s|text=I cannot do this. STATUS: REFUSED",
                        "BCB|v=1|id=X|phase=RESULT|from=aya|answers=%s|text=waiting. STATUS: HOLD NEXT_OWNER: grok"):
            self.assertEqual([], self.settle([row("aya", payload % self.ask)]), payload)
        self.assertEqual("claimed", card(self.conn, "chatgpt-codex-desktop", self.ask)["status"])
        self.assertIn(self.ask, self.conn.hgetall(cb.OPEN_KEY))

    def test_rows_that_are_not_rows_are_passed_over(self):
        self.assertEqual([], self.settle([None, "text", [], ["only", "two"]]))


class FollowUp(unittest.TestCase):
    """CLAUDE-MOBILE-BRIDGE-CONTINUITY: his follow-up to the voice holding the floor names nobody and is still an ask."""
    SAID = "I know, but it's a small sample page fix. You just update it."

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def granted(self, persona, turn_id, seconds_ago):
        return turn(self.conn, None, reason="", actor=persona, seconds_ago=seconds_ago, kind="floor.granted",
                    turn_id=turn_id)

    def test_what_is_a_follow_up(self):
        f = {"kind": "turn.started", "actor_id": "owner-1", "reason": "continuity", "utterance": self.SAID}
        self.assertTrue(cb.follow_up(f))
        self.assertFalse(cb.follow_up(dict(f, utterance="I know, but why is it slow?")), "no task word")
        self.assertFalse(cb.follow_up(dict(f, utterance="Codex, update it.")), "a name: the named path's, not this")
        self.assertFalse(cb.follow_up(dict(f, reason="vocative")))
        self.assertFalse(cb.follow_up(dict(f, reason="everyone")))
        self.assertFalse(cb.follow_up(dict(f, actor_id="chair")))
        self.assertFalse(cb.follow_up(dict(f, kind="floor.granted")))
        self.assertFalse(cb.follow_up(dict(f, utterance="")))

    def test_the_voice_granted_the_floor_for_that_turn_takes_it(self):
        self.granted("codex", "t-1", seconds_ago=40)                       # an earlier holder
        turn(self.conn, self.SAID, reason="continuity", seconds_ago=20, turn_id="t-2")
        self.granted("grok", "t-2", seconds_ago=15)                        # read ahead in the same page
        [ask] = bridge(self.conn).tick()
        got = card(self.conn, "grok", ask)
        self.assertEqual("claimed", got["status"])
        self.assertNotIn("page fix", got["body"].lower() + got["activity"].lower(), "none of his words on the card")
        self.assertIsNone(self.conn.hget(cp.PARTITION % ("workshop", "chatgpt-codex-desktop"), ask))
        self.assertIn(self.SAID.split(".")[0], self.conn.streams["fleet:inbox:grok"][0][1]["text"])

    def test_else_the_last_voice_granted_the_floor_in_the_room(self):
        self.granted("gemini", "t-1", seconds_ago=40)
        bridge(self.conn).tick()                                           # an earlier pass saw the grant
        turn(self.conn, self.SAID, reason="continuity", seconds_ago=5, turn_id="t-2")
        [ask] = bridge(self.conn).tick()                                   # its grant is not in this page yet
        self.assertIsNotNone(card(self.conn, "gemini", ask))
        self.assertEqual("gemini", self.conn.get(cb.LAST_KEY % ROOM))
        self.assertEqual(cb.LAST_SECONDS, self.conn.ttls[cb.LAST_KEY % ROOM])
        self.assertTrue((cb.LAST_KEY % ROOM).startswith("gov:"))

    def test_nobody_known_nobody_claimed(self):
        turn(self.conn, self.SAID, reason="continuity", seconds_ago=5, turn_id="t-2")
        self.assertEqual([], bridge(self.conn).tick())
        self.assertEqual({}, self.conn.hashes.get(cb.OPEN_KEY, {}))

    def test_a_grant_for_another_turn_or_a_voice_that_is_not_ours_is_not_the_holder(self):
        turn(self.conn, self.SAID, reason="continuity", seconds_ago=20, turn_id="t-2")
        self.granted("grok", "t-9", seconds_ago=15)                        # another turn: only the room's last
        self.granted("owner-1", "t-2", seconds_ago=10)                     # not a voice
        self.assertEqual([], bridge(self.conn).tick(), "t-9's grant is another turn's, and came after the ask")
        self.assertEqual([], [k for k in self.conn.hashes if k.startswith("conf:canvas:")])
        self.assertEqual("grok", self.conn.get(cb.LAST_KEY % ROOM), "the owner's id is never kept as a holder")

    def test_a_follow_up_with_no_task_word_claims_nothing(self):
        self.granted("grok", "t-1", seconds_ago=40)
        turn(self.conn, "I know, but why is it so slow?", reason="continuity", seconds_ago=20, turn_id="t-2")
        self.granted("grok", "t-2", seconds_ago=15)
        self.assertEqual([], bridge(self.conn).tick())
        self.assertNotIn("fleet:inbox:grok", self.conn.streams)

    def test_once_per_follow_up(self):
        turn(self.conn, self.SAID, reason="continuity", seconds_ago=20, turn_id="t-2")
        self.granted("grok", "t-2", seconds_ago=15)
        self.assertEqual(1, len(bridge(self.conn).tick()))
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)
        self.assertEqual([], bridge(self.conn).tick())
        self.assertEqual(1, len(self.conn.streams["fleet:inbox:grok"]))


class ShownAndRefused(unittest.TestCase):
    """The owner's demo, 2026-10-28 09:14 ET: an ask for a picture got no claim, and the voice said it could not."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)
        self.bridge = cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW)
        self.side = "conf:side:%s" % NOW.strftime("%Y%m%d")

    def say(self, voice, text, seconds_ago=5, room=ROOM, by="chair"):
        return self.conn.xadd(self.side, {"by": by, "voice": voice, "room": room, "text": text,
                                          "at": cb.stamp(NOW)}, entry_id=at(seconds_ago))

    def claimed(self, agent):
        return {k: json.loads(v) for k, v in self.conn.hgetall(cp.PARTITION % ("workshop", agent)).items()}

    def test_asks_to_pull_get_bring_or_show_a_picture_or_image_are_tasks(self):
        for said in ("Claude, pull up a picture of a sample chair.", "Grok, get me an image of a sample chair.",
                     "Codex, bring the diagram back.", "Gemini, show the plan.", "Claude, a picture please.",
                     "Grok, two images of the logo."):
            self.assertTrue(cb.TASK.search(said), said)
        self.assertEqual(cb.asks_in({"kind": "turn.started", "actor_id": "owner-1", "reason": "vocative",
                                     "utterance": "Claude, pull up a picture of a sample chair."}), ["claude-code-cli"])

    def test_what_is_a_refusal(self):
        for said in ("I can't pull an image.", "Sorry, I cannot do that.", "I can\u2019t open files.",
                     "I'm not able to fetch that.", "I am unable to browse.", "I don't have the tools for that.",
                     "I do not have access to it.", "I have no way to draw.", "I can not do it."):
            self.assertEqual(cb.refusal({"by": "chair", "voice": "grok", "room": ROOM, "text": said}, ROOM), "grok",
                             said)
        for said in ("Codex can't do that.", "Sure, I can do that.", "I candidly think so.", "You can't miss it.",
                     "I can do it now."):
            self.assertIsNone(cb.refusal({"by": "chair", "voice": "grok", "room": ROOM, "text": said}, ROOM), said)

    def test_only_the_chair_s_line_for_a_room_voice_in_this_room(self):
        base = {"by": "chair", "voice": "claude", "room": ROOM, "text": "I can't pull an image."}
        self.assertEqual(cb.refusal(base, ROOM), "claude-code-cli")
        self.assertIsNone(cb.refusal(dict(base, by="grok"), ROOM))
        self.assertIsNone(cb.refusal(dict(base, voice="owner"), ROOM))
        self.assertIsNone(cb.refusal(dict(base, room="conf-other"), ROOM))

    def test_a_voice_that_cannot_claims_his_last_turn_for_its_agent(self):
        entry = turn(self.conn, "Could someone find a sample chair picture?", reason="everyone", seconds_ago=10)
        self.say("claude", "I can't pull an image.", seconds_ago=5)
        claimed = self.bridge.tick()
        ask = cb.ask_id(ROOM, entry, "claude-code-cli")
        self.assertEqual(claimed, [ask])
        self.assertEqual(self.claimed("claude-code-cli")[ask]["status"], "claimed")
        inbox = self.conn.streams["fleet:inbox:claude-code-cli"]
        self.assertIn("find a sample chair picture", inbox[-1][1]["text"])

    def test_a_named_ask_and_its_refusal_are_one_claim(self):
        turn(self.conn, "Claude, pull up a picture of a sample chair.", seconds_ago=10)
        self.say("claude", "I can't pull an image.", seconds_ago=5)
        self.assertEqual(len(self.bridge.tick()), 1)
        self.assertEqual(self.bridge.tick(), [])
        self.assertEqual(len(self.claimed("claude-code-cli")), 1)
        self.assertEqual(len(self.conn.streams["fleet:inbox:claude-code-cli"]), 1)

    def test_no_turn_of_his_no_claim_and_a_plain_answer_claims_nothing(self):
        self.say("grok", "I can't pull an image.", seconds_ago=5)
        self.assertEqual(self.bridge.tick(), [])
        turn(self.conn, "How are you all?", reason="everyone", seconds_ago=4)
        self.say("grok", "Fine, thank you.", seconds_ago=2)
        self.assertEqual(self.bridge.tick(), [])
        self.assertEqual(self.claimed("grok"), {})

    def test_a_line_said_before_his_turn_never_claims_it(self):
        self.say("grok", "I can't pull an image.", seconds_ago=20)
        turn(self.conn, "Grok, how are you?", seconds_ago=10)
        self.assertEqual(self.bridge.tick(), [])
        self.assertEqual(self.claimed("grok"), {})

    def test_nothing_while_the_window_is_shut_or_the_lease_is_not_ours(self):
        turn(self.conn, "Could someone find a chair?", reason="everyone", seconds_ago=10)
        self.say("gemini", "I cannot do that.", seconds_ago=5)
        self.assertEqual(cb.Bridge(self.conn, LOG, lambda: False, clock=lambda: NOW).tick(), [])
        self.assertEqual(self.claimed("gemini"), {})

    def test_its_keys_are_under_gov_and_expire(self):
        turn(self.conn, "Could someone find a chair?", reason="everyone", seconds_ago=10)
        self.say("codex", "I cannot do that.", seconds_ago=5)
        self.bridge.tick()
        for key in (cb.ASKED_KEY % ROOM, cb.SIDE_CURSOR_KEY % ROOM):
            self.assertTrue(key.startswith("gov:"), key)
            self.assertIn(key, self.conn.store)
            self.assertTrue(self.conn.ttls[key], key)
        self.assertEqual(self.conn.ttls[cb.ASKED_KEY % ROOM], cb.ASKED_SECONDS)


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
        handed = []
        start_bridge = lambda conn, log, holds, **kw: handed.append(kw) or Worker("bridge")  # noqa: E731
        seen = []
        live = lambda *a: seen.append(a[7] if len(a) > 7 else None) or 0                    # noqa: E731
        with mock.patch.object(sr, "live", side_effect=live):
            sr.live_with_canvas(None, {}, "", "me", NOW, 10, 300, start=start, start_bridge=start_bridge,
                                environ={"CANVAS_BUCKET": "sfdc24-canvas", "CALL_BRIDGE": "on"})
            self.assertIsNotNone(seen[-1])
            self.assertEqual("sfdc24-canvas", handed[-1].get("bucket"), "the bridge gets the publisher's bucket")
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


class InTheMakingAndLatency(unittest.TestCase):
    """O3 (owner orders 2026-10-28 8:46 PM ET): the claim card is titled "In the making" (who, never his words: Aya's
    privacy hold on 922e8da; the agent's own card says what), and the time from the ask to the agent's OWN card is
    measured against 3 minutes."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def claim(self):
        turn(self.conn, "Codex, draft the queue flow for the vans.")
        [ask] = bridge(self.conn).tick()
        return ask

    def latency(self, ask):
        raw = self.conn.get(cb.LATENCY_KEY % ask)
        return json.loads(raw) if raw else None

    def test_the_title_says_in_the_making_and_who_never_his_words(self):
        got = cb.claimed_card("claude-code-cli", NOW)
        self.assertEqual("In the making: Claude's work from the room", got["title"])
        self.assertIsNotNone(cp.clean_item("claude-code-cli", "call-000000000000", json.dumps(got)))

    def test_fix1_an_agent_with_no_wired_worker_gets_an_honest_title(self):
        """FIX 1 (the owner's 8 PM call, 2026-11-02): two asks for grok sat "In the making" with nobody to take them."""
        for agent, name in (("grok", "Greg"), ("gemini", "Gemini"), ("chatgpt-codex-desktop", "Cody")):
            got = cb.claimed_card(agent, NOW, environ={})
            self.assertEqual("Waiting for a worker: %s's work from the room" % name, got["title"])
            self.assertEqual(("claimed", "claimed in the room"), (got["status"], got["activity"]),
                             "the same activity: every later step reads it unchanged")
            self.assertIsNotNone(cp.clean_item(agent, "call-000000000000", json.dumps(got)))
        self.assertTrue(cb.claimed_card("gemini", NOW, environ={"CALL_BRIDGE_WIRED": "claude-code-cli, gemini"})
                        ["title"].startswith("In the making"), "a worker wired by the switch")
        self.assertEqual(frozenset({"claude-code-cli"}), cb.wired({}))
        self.assertEqual(frozenset({"grok"}), cb.wired({"CALL_BRIDGE_WIRED": "grok,nobody,,"}), "unknown names ignored")
        self.assertEqual(frozenset(), cb.wired({"CALL_BRIDGE_WIRED": ""}))

    def test_the_agent_card_within_three_minutes_is_measured_on_time(self):
        ask = self.claim()
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1, "ask": ask,
               "ts": "2026-10-24T14:03:30Z", "title": "In the making: queue flow", "body": "x"}
        put_card(self.conn, "chatgpt-codex-desktop", ask, own)
        later = NOW + datetime.timedelta(seconds=100)
        self.assertEqual([ask], bridge(self.conn, clock=lambda: later).watch(later))
        got = self.latency(ask)
        # from HIS ask (the stream entry, 10 s before the tick) to this read-back, not from the tick
        self.assertEqual((ask + "-work", 110, True), (got["card"], got["seconds"], got["on_time"]))
        self.assertEqual(("2026-10-24T14:01:50Z", "2026-10-24T14:03:30Z", "2026-10-24T14:03:40Z"),
                         (got["asked_at"], got["card_ts"], got["read_at"]))
        self.assertEqual([], bridge(self.conn, clock=lambda: later).watch(later), "kept once")

    def test_no_card_after_three_minutes_is_kept_once_as_late_then_measured_when_it_comes(self):
        ask = self.claim()
        late = NOW + datetime.timedelta(seconds=200)
        self.assertEqual([ask], bridge(self.conn, clock=lambda: late).watch(late))
        self.assertEqual(("", False), (self.latency(ask)["card"], self.latency(ask)["on_time"]))
        self.assertEqual([], bridge(self.conn, clock=lambda: late).watch(late), "late is kept once")
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1,
               "ts": "2026-10-24T14:06:00Z", "title": "In the making: queue flow", "body": "x"}
        put_card(self.conn, "chatgpt-codex-desktop", ask, own)
        got_at = NOW + datetime.timedelta(seconds=250)
        bridge(self.conn, clock=lambda: got_at).watch(got_at)
        self.assertEqual((ask + "-work", 260, False), (self.latency(ask)["card"], self.latency(ask)["seconds"],
                                                       self.latency(ask)["on_time"]))

    def test_the_bridge_card_and_older_cards_are_not_the_agents_card(self):
        ask = self.claim()
        old = {"v": 1, "type": "note", "status": "done", "public": True, "version": 3,
               "ts": "2026-10-24T13:00:00Z", "title": "Earlier", "body": "x"}
        self.conn.hset(cp.PARTITION % ("workshop", "chatgpt-codex-desktop"), "earlier", json.dumps(old))
        soon = NOW + datetime.timedelta(seconds=60)
        self.assertEqual([], bridge(self.conn, clock=lambda: soon).watch(soon))
        self.assertIsNone(self.latency(ask))

    def test_an_unrelated_new_card_or_another_claim_is_not_this_asks_card(self):
        """Aya's hold on 5d41cb5, item 4: only the agent's card for THIS ask counts."""
        ask = self.claim()
        turn(self.conn, "Codex, list the open risks.", seconds_ago=5)
        [other] = bridge(self.conn).tick()
        unrelated = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1,
                     "ts": "2026-10-24T14:02:30Z", "title": "Something else", "body": "x"}
        for_other = dict(unrelated, ask=other)
        slot = cp.PARTITION % ("workshop", "chatgpt-codex-desktop")
        self.conn.hset(slot, "something-else", json.dumps(unrelated))
        put_card(self.conn, "chatgpt-codex-desktop", other, for_other)
        soon = NOW + datetime.timedelta(seconds=60)
        self.assertEqual([other], bridge(self.conn, clock=lambda: soon).watch(soon))
        self.assertIsNone(self.latency(ask), "the other claim's card and an unrelated card do not count for it")
        self.assertEqual(other + "-work", self.latency(other)["card"])

    def test_the_three_minutes_start_at_his_ask_not_at_the_tick(self):
        """Aya's hold on 5d41cb5, item 5: an ask the bridge picked up late is late by the owner's clock."""
        turn(self.conn, "Codex, draft the queue flow for the vans.", seconds_ago=150)
        [ask] = bridge(self.conn).tick()
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1, "ask": ask,
               "ts": "2026-10-24T14:02:50Z", "title": "In the making: queue flow", "body": "x"}
        put_card(self.conn, "chatgpt-codex-desktop", ask, own)
        later = NOW + datetime.timedelta(seconds=60)
        bridge(self.conn, clock=lambda: later).watch(later)
        got = self.latency(ask)
        self.assertEqual((210, False), (got["seconds"], got["on_time"]), "60 s after the claim, 210 s after his ask")



class AyasHoldsOn717f423(unittest.TestCase):
    """Aya's check of 717f423 (2026-10-29 07:51 UTC), two holds: (a) the 3-minute deadline was compared on whole
    seconds, so a card read 180.001 s after the ask counted as on time; (b) a RESULT that settled the ask before the
    next watch pass deleted the open record and the ask-to-card timing with it."""

    AGENT = "chatgpt-codex-desktop"

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)
        turn(self.conn, "Codex, draft the queue flow for the vans.", seconds_ago=10)
        [self.ask] = bridge(self.conn).tick()                      # his ask at NOW - 10 s, claimed at NOW
        self.said_at = NOW - datetime.timedelta(seconds=10)

    def own_card(self):
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1, "ask": self.ask,
               "ts": "2026-10-24T14:03:00Z", "title": "In the making: queue flow", "body": "x"}
        put_card(self.conn, self.AGENT, self.ask, own)

    def latency(self):
        raw = self.conn.get(cb.LATENCY_KEY % self.ask)
        return json.loads(raw) if raw else None

    def result(self):
        return row("codex", "BCB|v=1|id=CODEX-DONE|phase=RESULT|from=codex|answers=CALLBRIDGE-WAKE-%s|text=drafted"
                   % self.ask)

    def test_a_card_read_180_001_seconds_after_the_ask_is_late(self):
        self.own_card()
        at = self.said_at + datetime.timedelta(seconds=180, milliseconds=1)
        self.assertEqual([self.ask], bridge(self.conn, clock=lambda: at).watch(at))
        got = self.latency()
        self.assertEqual((False, 180, 180.001), (got["on_time"], got["seconds"], got["waited"]),
                         "late by 1 ms is late; the whole seconds are only for reading")

    def test_a_card_read_exactly_at_180_seconds_is_on_time(self):
        self.own_card()
        at = self.said_at + datetime.timedelta(seconds=180)
        bridge(self.conn, clock=lambda: at).watch(at)
        self.assertEqual((True, 180.0), (self.latency()["on_time"], self.latency()["waited"]))

    def test_no_card_at_180_001_seconds_is_kept_as_late(self):
        at = self.said_at + datetime.timedelta(seconds=180, milliseconds=1)
        self.assertEqual([self.ask], bridge(self.conn, clock=lambda: at).watch(at))
        self.assertEqual(("", False), (self.latency()["card"], self.latency()["on_time"]))

    def test_a_result_before_the_watch_pass_keeps_the_timing(self):
        """Authoritative completion before watch must preserve ask-to-card latency."""
        self.own_card()
        at = self.said_at + datetime.timedelta(seconds=70)
        self.assertEqual([], bridge(self.conn, clock=lambda: at).settle([self.result()]))
        self.assertIsNone(self.latency(), "no artificial watch has measured it")
        self.assertEqual("DONE", complete(self.conn, self.AGENT, self.ask, at)["state"])
        self.assertIsNone(self.conn.hget(cb.OPEN_KEY, self.ask), "the ask is closed")
        got = self.latency()
        self.assertIsNotNone(got, "its ask-to-card timing outlives the open record")
        self.assertEqual((self.ask + "-work", 70, True, stamp_of(at)),
                         (got["card"], got["seconds"], got["on_time"], got["settled_at"]))
        self.assertEqual([], bridge(self.conn, clock=lambda: at).watch(at))

    def test_a_result_with_no_card_of_its_own_is_kept_and_never_on_time(self):
        """No artifact means no completion, not a false completed/late receipt."""
        at = self.said_at + datetime.timedelta(seconds=40)
        self.assertEqual([], bridge(self.conn, clock=lambda: at).settle([self.result()]))
        with self.assertRaises(life.Refused):
            complete(self.conn, self.AGENT, self.ask, at)
        self.assertEqual("OPEN", life.get(self.conn, self.ask)["state"])
        self.assertIsNone(self.latency(), "neither free RESULT nor refused completion invents evidence")
        late = self.said_at + datetime.timedelta(seconds=180, milliseconds=1)
        bridge(self.conn, clock=lambda: late).watch(late)
        self.assertEqual(("", False, 180.001),
                         (self.latency()["card"], self.latency()["on_time"], self.latency()["waited"]))

    def test_a_timing_already_measured_is_not_rewritten_at_settlement(self):
        self.own_card()
        first = self.said_at + datetime.timedelta(seconds=50)
        bridge(self.conn, clock=lambda: first).watch(first)
        before = self.latency()
        later = self.said_at + datetime.timedelta(seconds=400)
        self.assertEqual([], bridge(self.conn, clock=lambda: later).settle([self.result()]))
        complete(self.conn, self.AGENT, self.ask, later)
        self.assertEqual(before, self.latency())

    def test_a_late_record_then_a_result_with_its_card_measures_the_card(self):
        late = self.said_at + datetime.timedelta(seconds=200)
        bridge(self.conn, clock=lambda: late).watch(late)
        self.own_card()
        done = self.said_at + datetime.timedelta(seconds=230)
        self.assertEqual([], bridge(self.conn, clock=lambda: done).settle([self.result()]))
        complete(self.conn, self.AGENT, self.ask, done)
        got = self.latency()
        self.assertEqual((self.ask + "-work", 230, False), (got["card"], got["seconds"], got["on_time"]))


def stamp_of(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def board_row(spec, ts):
    """A row as the board returns it (Row_ID, Timestamp, Source, Target, Action, Payload, ...)."""
    return [spec["row_id"], ts, spec["source_tag"], spec["target_surface"], spec["action_type"], spec["payload"],
            spec["category"], spec["project_tag"], spec["gist"], ""]


class Doorbell(unittest.TestCase):
    """O3 gap 1 (owner orders 2026-10-28): the claim RINGS the real agent with one board row its doorbell reads,
    never twice, never with his words, and its reply settles the ask. End to end, ask to the agent's card is measured."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)
        self.rung = []

    def ringing(self, clock=None, holds=True, fail=False):
        def ring(spec):
            if fail:
                raise SystemExit(1)                                       # append.py: read-back unconfirmed
            self.rung.append(spec)
        lease = holds if callable(holds) else (lambda: holds)
        return cb.Bridge(self.conn, LOG, lease, clock=clock or (lambda: NOW), ring=ring)

    def test_without_a_ring_nothing_is_queued_and_nothing_changes(self):
        turn(self.conn, "Codex, draft the queue flow for the vans.")
        bridge(self.conn).tick()
        self.assertEqual({}, self.conn.hgetall(cb.RING_KEY))

    def test_a_claim_rings_its_agent_once_with_a_row_addressed_to_it(self):
        turn(self.conn, "Codex, draft the price sheet for the client's Port-au-Prince store. Ask Pat Placeholder first.")
        b = self.ringing()
        [ask] = b.tick()
        [spec] = self.rung
        self.assertEqual(("CALLBRIDGE-WAKE-" + ask, "chatgpt-codex-desktop", "ASK", "bus-reconciler"),
                         (spec["row_id"], spec["target_surface"], spec["action_type"], spec["source_tag"]))
        self.assertIn("|phase=ASK|", spec["payload"])
        self.assertIn("|to=chatgpt-codex-desktop|", spec["payload"])
        self.assertIn("answers=%s" % ask, spec["payload"])
        self.assertIn("fleet:inbox:chatgpt-codex-desktop", spec["payload"])
        for word in ("Port", "Prince", "client", "Pat", "Placeholder", "draft", "price", "sheet", "store"):
            self.assertNotIn(word, json.dumps(spec), "no word of his on the board: it stays in the inbox")
        self.assertEqual(7, spec["payload"].count("|"), "eight fields, no stray pipe")
        b.tick()
        self.ringing().tick()
        self.assertEqual(1, len(self.rung), "rung once, whatever the run")
        self.assertEqual({}, self.conn.hgetall(cb.RING_KEY))

    def test_a_failed_ring_is_logged_and_never_retried(self):
        turn(self.conn, "Gemini, compare the two plans.")
        with self.assertLogs(LOG, level="ERROR") as logs:
            [ask] = self.ringing(fail=True).tick()
        self.assertIn("NOT confirmed", " ".join(logs.output))
        self.ringing().tick()
        self.assertEqual([], self.rung)
        self.assertIsNotNone(self.conn.get(cb.RUNG_KEY % ask))

    def test_no_ring_once_the_lease_is_lost(self):
        turn(self.conn, "Grok, list the risks.")
        calls = iter([True, True, True, False, False, False, False])
        self.ringing(holds=lambda: next(calls, False)).tick()
        self.assertEqual([], self.rung)

    def test_a_waker_reply_to_the_wake_row_is_not_done_but_a_result_is(self):
        """Aya's hold on 5d41cb5, items 1 and 3: the doorbell and a waker's relayed reply are not finished work."""
        turn(self.conn, "Gemini, compare the two plans.")
        [ask] = self.ringing().tick()
        later = NOW + datetime.timedelta(minutes=4)
        waker = ("BCB|v=1|id=GEMINI-REPLY|phase=RESULT|from=gemini|wakerreply=1|answers=CALLBRIDGE-WAKE-%s|"
                 "evidence=STATED|text=REPLY: I cannot read the repo" % ask)
        self.assertEqual([], bridge(self.conn, clock=lambda: later).settle([row("gemini", waker)]))
        self.assertEqual("claimed", card(self.conn, "gemini", ask)["status"])
        result = "BCB|v=1|id=GEMINI-DONE|phase=RESULT|from=gemini|answers=CALLBRIDGE-WAKE-%s|text=compared" % ask
        self.assertEqual([], bridge(self.conn, clock=lambda: later).settle([row("gemini", result)]))
        self.assertEqual("OPEN", life.get(self.conn, ask)["state"])
        put_card(self.conn, "gemini", ask, now=later)
        self.assertEqual("DONE", complete(self.conn, "gemini", ask, later)["state"])
        self.assertEqual("done", card(self.conn, "gemini", ask + "-work")["status"])

    def test_every_doorbell_rings_on_it(self):
        import agent_waker
        import fleet_doorbell as fd
        for agent, said, door in (("chatgpt-codex-desktop", "Codex, draft the queue flow.", "codex"),
                                  ("grok", "Grok, list the risks.", "grok"),
                                  ("gemini", "Gemini, compare the two plans.", "gemini")):
            self.rung.clear()
            conn = self.conn = FakeRedis()
            open_window(conn)
            turn(conn, said)
            self.ringing().tick()
            [spec] = self.rung
            r = board_row(spec, "2026-10-24T14:02:01Z")
            since = NOW - datetime.timedelta(minutes=1)
            self.assertEqual(1, len(fd.new_rows([r], since, door)), door)
            self.assertEqual(1, len(fd.new_rows([r], since, door, calls_only=True)), door)
            self.assertTrue(fd.is_wake(r))
            others = [a for a in fd.AGENTS if a != door]
            for other in others:
                self.assertEqual([], fd.new_rows([r], since, other), (door, other))
            self.assertTrue(agent_waker.addressed_to(r, agent if agent != "chatgpt-codex-desktop" else agent), agent)

    def test_end_to_end_ask_to_the_agents_card_is_measured_against_three_minutes(self):
        """The whole path in one clock: his ask lands in the room stream 10 s before the bridge's tick; the tick
        claims and rings; the agent's doorbell (fleet_doorbell, 30 s poll) sees the wake row on its next poll; the
        agent puts its own card up; the next tick measures ask-to-card. Each step's delay is a stated assumption."""
        import fleet_doorbell as fd
        said_at = NOW - datetime.timedelta(seconds=10)
        turn(self.conn, "Codex, draft the queue flow for the vans.", seconds_ago=10)
        [ask] = self.ringing().tick()                                     # tick at NOW: claim + ring
        [spec] = self.rung
        landed = NOW + datetime.timedelta(seconds=5)                      # append + read-back
        polled = NOW + datetime.timedelta(seconds=30)                     # doorbell poll every 30 s
        heard = fd.new_rows([board_row(spec, landed.strftime("%Y-%m-%dT%H:%M:%SZ"))],
                            NOW - datetime.timedelta(seconds=1), "codex")
        self.assertEqual(1, len(heard))
        card_at = polled + datetime.timedelta(seconds=40)                 # the agent writes its card
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1, "ask": ask,
               "ts": card_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "title": "In the making: queue flow", "body": "x"}
        put_card(self.conn, "chatgpt-codex-desktop", ask, own)
        tick_at = card_at + datetime.timedelta(seconds=10)
        self.assertEqual([ask], self.ringing(clock=lambda: tick_at).watch(tick_at))
        got = json.loads(self.conn.get(cb.LATENCY_KEY % ask))
        self.assertTrue(got["on_time"])
        self.assertEqual(90, got["seconds"], "his ask to the card read back, poll delays inside")
        self.assertEqual((tick_at - said_at).total_seconds(), got["seconds"])
        self.assertLessEqual(got["seconds"], cb.TARGET_SECONDS)


class DoorbellInTheLiveRun(unittest.TestCase):
    def test_the_ring_is_handed_to_the_bridge_only_with_call_bridge_ring_on(self):
        import serve_requests as sr
        got = []

        class Worker:
            canvas_room = "workshop"
            stop = mock.Mock()
            thread = mock.Mock()
            settle = None

        start = lambda conn, bucket, log, holds: Worker()                                    # noqa: E731

        def start_bridge(conn, log, holds, **kw):
            got.append(kw)
            return Worker()
        with mock.patch.object(sr, "live", return_value=0), mock.patch.object(sr, "appender", return_value=print):
            for env, has in (({"CANVAS_BUCKET": "b", "CALL_BRIDGE": "on", "CALL_BRIDGE_RING": "on"}, True),
                             ({"CANVAS_BUCKET": "b", "CALL_BRIDGE": "on"}, False),
                             ({"CANVAS_BUCKET": "b", "CALL_BRIDGE": "on", "CALL_BRIDGE_RING": "1"}, False)):
                got.clear()
                conn = FakeRedis()
                sr.live_with_canvas(conn, {}, "", "me", NOW, 10, 300, start=start, start_bridge=start_bridge,
                                    environ=env)
                self.assertEqual(has, "ring" in got[-1], env)
            conn = FakeRedis()
            sr.live_with_canvas(conn, {}, "", "me", NOW, 10, 300, start=start, start_bridge=start_bridge,
                                environ={"CANVAS_BUCKET": "b", "CALL_BRIDGE": "on", "CALL_BRIDGE_RING": "on"})
            with self.assertRaises(sr.LockLost):
                got[-1]["ring"]({"row_id": "CALLBRIDGE-WAKE-call-000000000000"})   # lock not "me": no write


class OneAppendAtATime(unittest.TestCase):
    """Aya's hold on 5d41cb5, item 2: append.py reads its row file from the process-global sys.argv, and the request
    loop and the bridge's doorbell thread both append. The swap and the append are one critical section."""

    def test_concurrent_appends_each_write_their_own_row(self):
        import threading
        import time as clock
        import types
        import serve_requests as sr
        state = {"inside": 0, "most": 0}
        wrote = []
        guard = threading.Lock()

        def main(*, require_sender_identity=False):
            self.assertIs(require_sender_identity, True,
                          "every reconciler append must request workload sender identity")
            with guard:
                state["inside"] += 1
                state["most"] = max(state["most"], state["inside"])
            path = sys.argv[1]
            clock.sleep(0.02)                                            # another thread would swap argv here
            with open(sys.argv[1], encoding="utf-8") as fh:
                wrote.append((path == sys.argv[1], json.load(fh)["row_id"]))
            with guard:
                state["inside"] -= 1
        fake = types.SimpleNamespace(main=main)
        with mock.patch.dict(sys.modules, {"append": fake}):
            write = sr.appender({})
            ids = ["ROW-%d" % n for n in range(6)]
            threads = [threading.Thread(target=write, args=({"row_id": i, "action_type": "ASK"},)) for i in ids]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(1, state["most"], "never two appends at once")
        self.assertEqual(sorted(ids), sorted(r for _, r in wrote))
        self.assertTrue(all(same for same, _ in wrote))
        self.assertNotIn("append.py", sys.argv[0])




class GoAheadAt1117(unittest.TestCase):
    """The owner's test call, 2026-10-29 11:17 ET (room conf-<his 16 hex>-42c62aad): he set out the work, told the voice
    to "do it", the voice said "Got it", and no card went up. No canvas window was open, so the bridge never read the
    call; and "do it" names nobody and has no task word. Now a go-ahead confirms his last ask, and the bridge opens
    the window for its claim, so the "In the making" card is up seconds after "do it". Since db84811 (Aya's binding
    hold on d2d1618) the ask a go-ahead confirms must be a task ask: a task word or a voice's name in it."""
    WORK = ("Map out how the sample team tracks its widgets in one sample tool; the hard part is pulling the "
            "sample totals out of it.")

    def setUp(self):
        self.conn = FakeRedis()

    def granted(self, persona, turn_id, seconds_ago):
        return turn(self.conn, None, reason="", actor=persona, seconds_ago=seconds_ago, kind="floor.granted",
                    turn_id=turn_id)

    def opened(self, b=None):
        return b or cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW, bucket="sfdc24-canvas")

    def test_what_is_a_go_ahead(self):
        f = {"kind": "turn.started", "actor_id": "owner-1", "reason": "continuity"}
        for said in ("do it", "Do it.", "Gemini, do it.", "Okay, go ahead.", "Yes, do that please.", "go for it",
                     "Yes please.", "Please do.", "OK Gemini, go ahead and do it now.", "Make it so."):
            self.assertTrue(cb.go_ahead(dict(f, utterance=said)), said)
        for said in ("What do you think?", "I did it already.", "Do you have it?", "", "Don't do it.",
                     "Do it in red and add a legend, then post it to the page and tell me when it is done please"):
            self.assertFalse(cb.go_ahead(dict(f, utterance=said)), said)
        self.assertFalse(cb.go_ahead(dict(f, utterance="do it", actor_id="chair")))
        self.assertFalse(cb.go_ahead(dict(f, utterance="do it", kind="floor.granted")))

    def test_the_1117_case_do_it_to_the_voice_holding_the_floor_puts_its_card_up_with_no_window_open(self):
        original = turn(self.conn, self.WORK, reason="continuity", seconds_ago=50, turn_id="t-2")   # a task word, no name
        go = turn(self.conn, "Gemini, do it.", reason="vocative", seconds_ago=20, turn_id="t-3")
        self.granted("gemini", "t-3", seconds_ago=18)
        self.assertEqual([], cp.active_windows(self.conn, NOW), "as on the call: no window open")
        [ask] = self.opened().tick()
        got = card(self.conn, "gemini", ask)
        self.assertEqual(("claimed", "Waiting for a worker: Gemini's work from the room"), (got["status"], got["title"]))
        self.assertEqual(["workshop"], [room for room, _ in cp.active_windows(self.conn, NOW)], "the window opened")
        self.assertEqual("call-bridge", cp.read_window(self.conn, "workshop")["by"])
        self.assertIn("gemini/" + ask, [item["id"] for item in cp.snapshot(self.conn, "workshop", NOW)["items"]],
                      "the card is in what the publisher puts on the page")
        [(_, entry)] = self.conn.streams["fleet:inbox:gemini"]
        self.assertIn("sample totals", entry["text"], "the agent gets the ask he confirmed")
        self.assertIn(self.WORK, entry["text"], "the exact original private task remains separate")
        self.assertNotIn("Gemini, do it.", entry["text"], "confirmation is bound separately, not concatenated")
        opened = json.loads(self.conn.hgetall(cb.OPEN_KEY)[ask])
        source = opened["source"]
        raw = self.WORK.encode("utf-8")
        self.assertEqual(original, source["event"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), source["input_sha256"])
        self.assertEqual(len(raw), source["input_bytes"])
        self.assertEqual(go, source["confirmation"]["event"])
        self.assertEqual(hashlib.sha256("Gemini, do it.".encode("utf-8")).hexdigest(),
                         source["confirmation"]["sha256"])
        self.assertEqual(life.digest(source), opened["source_revision"])
        self.assertEqual(opened["source_revision"], entry["source_revision"])
        self.assertEqual(source["input_sha256"], entry["input_sha256"])
        self.assertEqual(str(len(raw)), entry["input_bytes"])
        self.assertEqual(opened["source_revision"], got["source_revision"])
        self.assertEqual(cb.event_time(go).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), opened["asked_at"],
                         "its three minutes run from the go-ahead")

    def test_a_task_ask_to_the_voice_holding_the_floor_is_claimed_at_once_and_do_it_adds_no_second(self):
        self.granted("gemini", "t-1", seconds_ago=60)
        said = turn(self.conn, self.WORK, reason="continuity", seconds_ago=50, turn_id="t-2")
        self.granted("gemini", "t-2", seconds_ago=48)
        turn(self.conn, "Do it.", reason="continuity", seconds_ago=20, turn_id="t-3")
        [ask] = self.opened().tick()
        self.assertEqual("claimed", card(self.conn, "gemini", ask)["status"])
        self.assertEqual(1, len(self.conn.streams["fleet:inbox:gemini"]))
        self.assertEqual(cb.event_time(said).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                         json.loads(self.conn.hgetall(cb.OPEN_KEY)[ask])["asked_at"], "timed from the ask")

    def test_no_word_of_his_on_the_public_card_or_the_page(self):
        said = "Gemini, map the client's Port-au-Prince stores for Pat Placeholder."
        turn(self.conn, "Map the Port-au-Prince stores for Pat Placeholder, the client's buyer.", reason="continuity",
             seconds_ago=40, turn_id="t-1")
        turn(self.conn, "Gemini, do it.", seconds_ago=20, turn_id="t-2")
        [ask] = self.opened().tick()
        published = json.dumps(cp.snapshot(self.conn, "workshop", NOW)) + json.dumps(card(self.conn, "gemini", ask))
        for word in ("Port", "Prince", "Pat", "Placeholder", "client", "buyer", "stores", "Map", "do it"):
            self.assertNotIn(word, published, word)
        self.assertNotIn("said", self.conn.hgetall(cb.OPEN_KEY)[ask])
        self.assertIn("Placeholder", self.conn.streams["fleet:inbox:gemini"][0][1]["text"], "every word, in Redis only")
        del said

    def test_the_voice_his_ask_named_wins_over_the_floor_holder(self):
        """Aya's binding hold on d2d1618: the floor holder (up to 15 min old) could override the named voice."""
        turn(self.conn, "Codex, here is the flow I mean.", reason="vocative", seconds_ago=40, turn_id="t-1")
        turn(self.conn, "Go ahead.", reason="continuity", seconds_ago=20, turn_id="t-2")
        [ask] = self.opened().tick()
        self.assertIsNotNone(card(self.conn, "chatgpt-codex-desktop", ask), "nobody holding: the voice his ask named")
        conn2 = FakeRedis()
        self.conn = conn2
        self.granted("grok", "t-1", seconds_ago=45)
        turn(conn2, "Codex, here is the flow I mean.", reason="vocative", seconds_ago=40, turn_id="t-1")
        turn(conn2, "Do it.", reason="continuity", seconds_ago=20, turn_id="t-2")
        self.granted("grok", "t-2", seconds_ago=18)
        [ask] = self.opened().tick()
        self.assertIsNotNone(card(conn2, "chatgpt-codex-desktop", ask), "the voice his ask named takes it")
        self.assertNotIn(cp.PARTITION % ("workshop", "grok"), conn2.hashes, "never the floor holder")

    def test_a_go_ahead_for_an_ask_already_claimed_is_the_same_claim(self):
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=40, turn_id="t-1")
        turn(self.conn, "Gemini, do it.", seconds_ago=20, turn_id="t-2")
        asks = self.opened().tick()
        self.assertEqual(1, len(asks))
        self.assertEqual(1, len(self.conn.streams["fleet:inbox:gemini"]))
        self.assertEqual(1, len(self.conn.hashes[cp.PARTITION % ("workshop", "gemini")]))

    def test_a_refusal_after_the_go_ahead_is_the_same_claim_never_a_second(self):
        side = cb.SIDE_KEY % NOW.strftime("%Y%m%d")
        turn(self.conn, self.WORK, reason="continuity", seconds_ago=50, turn_id="t-1")
        turn(self.conn, "Gemini, do it.", seconds_ago=20, turn_id="t-2")
        self.conn.xadd(side, {"by": "chair", "voice": "gemini", "room": ROOM, "text": "I can't do that from here."},
                       entry_id=at(15))
        asks = self.opened().tick()
        self.assertEqual(1, len(asks))
        self.assertEqual(1, len(self.conn.hashes[cp.PARTITION % ("workshop", "gemini")]))

    def test_a_go_ahead_with_no_pending_task_claims_nothing_even_with_a_floor_holder(self):
        """Aya's binding hold on d2d1618: no pending task still claimed "do it" for the holder."""
        self.granted("gemini", "t-1", seconds_ago=30)
        turn(self.conn, "Do it.", reason="continuity", seconds_ago=20, turn_id="t-2")
        self.assertEqual([], self.opened().tick())
        self.assertNotIn("fleet:inbox:gemini", self.conn.streams)
        self.assertEqual([], cp.active_windows(self.conn, NOW), "and no window")

    def test_a_go_ahead_with_nobody_to_give_it_to_claims_nothing_and_opens_nothing(self):
        turn(self.conn, "Do it.", reason="continuity", seconds_ago=20, turn_id="t-2")
        self.assertEqual([], self.opened().tick())
        self.assertEqual([], cp.active_windows(self.conn, NOW))

    def test_an_ask_older_than_the_pending_reach_is_not_confirmed(self):
        turn(self.conn, "Codex, here is the flow I mean.", seconds_ago=40, turn_id="t-1")
        self.opened().tick()
        self.conn.store.pop(cb.PENDING_KEY % ROOM)                              # its TTL ran out
        turn(self.conn, "Go ahead.", reason="continuity", seconds_ago=5, turn_id="t-2")
        self.assertEqual([], self.opened().tick())

    def test_without_the_bucket_nothing_changes_no_window_no_read(self):
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        self.assertEqual([], bridge(self.conn).tick())
        self.assertEqual([], cp.active_windows(self.conn, NOW))
        self.assertNotIn("fleet:inbox:gemini", self.conn.streams)

    def test_a_window_someone_opened_is_kept_as_it_is(self):
        open_window(self.conn, minutes=30)
        before = self.conn.get(cp.WINDOW_PREFIX + "workshop")
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        self.assertEqual(1, len(self.opened().tick()))
        self.assertEqual(before, self.conn.get(cp.WINDOW_PREFIX + "workshop"))

    def test_no_window_is_opened_without_the_lease(self):
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        b = cb.Bridge(self.conn, LOG, lambda: False, clock=lambda: NOW, bucket="sfdc24-canvas")
        self.assertEqual([], b.tick())
        self.assertEqual([], cp.active_windows(self.conn, NOW))

    def test_a_call_that_went_by_while_nothing_read_it_is_never_replayed(self):
        self.conn.set(cb.CURSOR_KEY % ROOM, at(3600))                          # read an hour ago, then shut
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=cb.LOOKBACK_SECONDS + 30)
        self.assertEqual([], self.opened().tick())
        self.assertEqual([], cp.active_windows(self.conn, NOW))
        turn(self.conn, "Gemini, do it.", seconds_ago=5)
        self.assertEqual([], self.opened().tick(), "the old ask is not what a go-ahead confirms")

    def test_its_new_keys_are_under_gov_and_expire(self):
        turn(self.conn, self.WORK, reason="continuity", seconds_ago=50, turn_id="t-1")
        self.opened().tick()
        self.assertTrue((cb.PENDING_KEY % ROOM).startswith("gov:"))
        self.assertEqual(cb.PENDING_SECONDS, self.conn.ttls[cb.PENDING_KEY % ROOM])
        self.granted("gemini", "t-9", seconds_ago=5)
        self.opened().tick()
        self.assertTrue((cb.LAST_AT_KEY % ROOM).startswith("gov:"))
        self.assertEqual(cb.LAST_SECONDS, self.conn.ttls[cb.LAST_AT_KEY % ROOM])


class BoundedPublication(unittest.TestCase):
    """Aya's source hold on d2d1618, point 1: the window the bridge opened by itself published every public:true item
    already in the room's partitions. The bridge's window is scoped: only the claim's own cards leave Redis."""

    def setUp(self):
        self.conn = FakeRedis()
        old = {"v": 1, "type": "note", "status": "done", "public": True, "version": 4, "ts": "2026-10-23T20:00:00Z",
               "title": "Yesterday's card", "body": "x"}
        self.conn.hset(cp.PARTITION % ("workshop", "grok"), "yesterday", json.dumps(old))
        self.conn.hset(cp.PARTITION % ("workshop", "gemini"), "older", json.dumps(old))
        self.b = cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW, bucket="sfdc24-canvas")

    def published(self):
        raw = self.conn.get(cp.WINDOW_PREFIX + "workshop")
        return [i["id"] for i in cp.snapshot(self.conn, "workshop", NOW, scope=cp.scope_of(self.conn, "workshop",
                                                                                             raw))["items"]]

    def test_the_bridges_window_publishes_its_claim_and_nothing_older(self):
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        [ask] = self.b.tick()
        self.assertTrue(cp.read_window(self.conn, "workshop")["scoped"])
        self.assertEqual(["gemini/" + ask], self.published())

    def test_the_agents_own_card_for_that_ask_is_published_and_nothing_else_it_writes(self):
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        [ask] = self.b.tick()
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1, "ask": ask,
               "ts": "2026-10-24T14:02:30Z", "title": "In the making: five boxes", "body": "x"}
        other = dict(own, ask="call-000000000000", title="Something else")
        loose = {k: v for k, v in own.items() if k != "ask"}
        slot = cp.PARTITION % ("workshop", "gemini")
        put_card(self.conn, "gemini", ask, own)
        self.conn.hset(slot, "other", json.dumps(other))
        self.conn.hset(slot, "loose", json.dumps(loose))
        self.conn.hset(cp.PARTITION % ("workshop", "grok"), "boxes", json.dumps(own))   # another slot naming it
        # Living cards (PR 461): once the worker's own card exists, the claim placeholder is hidden, so one
        # piece of work is one card.
        self.assertEqual(["gemini/" + ask + "-work"], sorted(self.published()))

    def test_each_claim_in_its_window_adds_its_own_card(self):
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        turn(self.conn, "Codex, list the risks.", seconds_ago=10)
        asks = self.b.tick()
        self.assertEqual(2, len(asks))
        self.assertEqual(sorted(["gemini/" + asks[0], "chatgpt-codex-desktop/" + asks[1]]), sorted(self.published()))

    def test_a_window_opened_by_a_board_ask_publishes_the_room_as_before_and_is_not_scoped(self):
        cp.open_window(self.conn, "workshop", 30, "claude-code-cli", NOW, "sfdc24-canvas")
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        [ask] = self.b.tick()
        self.assertNotIn("scoped", cp.read_window(self.conn, "workshop"))
        self.assertEqual(sorted(["gemini/" + ask, "gemini/older", "grok/yesterday"]), sorted(self.published()))
        gen = cp.read_window(self.conn, "workshop")["gen"]
        self.assertEqual({}, self.conn.hgetall(cp.scope_key("workshop", gen)), "nothing listed on someone else's")

    def test_a_scoped_window_whose_list_is_gone_publishes_nothing(self):
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        self.b.tick()
        gen = cp.read_window(self.conn, "workshop")["gen"]
        self.conn.hashes.pop(cp.scope_key("workshop", gen))
        self.assertEqual([], self.published())
        self.assertEqual(frozenset(), cp.scope_of(self.conn, "workshop", '{"scoped": "yes"}'), "unreadable: none")

    def test_the_publisher_uploads_only_the_scoped_items(self):
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        [ask] = self.b.tick()
        sent = []
        pub = cp.Publisher(self.conn, "sfdc24-canvas", lambda bucket, name, data, allow=None: sent.append(data), LOG,
                           clock=lambda: NOW, holds=lambda: True)
        self.assertEqual(["workshop"], pub.tick())
        self.assertEqual(["gemini/" + ask], [i["id"] for i in json.loads(sent[-1])["items"]])

    def test_its_keys_are_under_gov_and_end_with_the_window(self):
        turn(self.conn, "Gemini, draw the five boxes.", seconds_ago=20)
        self.b.tick()
        key = cp.scope_key("workshop", cp.read_window(self.conn, "workshop")["gen"])
        self.assertTrue(key.startswith("gov:"))
        self.assertEqual(cp.MAX_MINUTES * 60, self.conn.ttls[key])


class WakeConfirmed(unittest.TestCase):
    """Aya's source hold on d2d1618, point 2: a ring is not a wake. The agent must take the claim (an ACK row, finished
    work, or its own card) within ACK_SECONDS; else one more ring; else the record and the card say it did not wake.
    The bridge launches nothing."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)
        self.rung = []
        turn(self.conn, "Gemini, compare the two plans for the Port-au-Prince store.", seconds_ago=10)
        [self.ask] = self.at(NOW).tick()

    def at(self, moment):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: moment, ring=self.rung.append)

    def wake(self):
        return json.loads(self.conn.get(cb.WAKE_KEY % self.ask))

    def later(self, seconds):
        moment = NOW + datetime.timedelta(seconds=seconds)
        self.at(moment).tick()
        return moment

    def ack(self, sender="gemini", extra="phase=ACK|from=gemini", target=None):
        return row(sender, "BCB|v=1|id=GEMINI-ACK|%s|answers=%s|text=claim taken" % (
            extra + "|event=worker.accepted|task_id=" + self.ask + "|receipt_id=fixture-accepted", target or cb.WAKE_PREFIX + self.ask))

    def test_rung_once_and_recorded(self):
        self.assertEqual(1, len(self.rung))
        self.assertEqual(("rung", "gemini"), (self.wake()["state"], self.wake()["agent"]))

    def test_no_ack_in_time_rings_once_more_then_marks_it_did_not_wake(self):
        self.later(cb.ACK_SECONDS - 1)
        self.assertEqual(1, len(self.rung), "not before ACK_SECONDS")
        self.later(cb.ACK_SECONDS)
        self.assertEqual(2, len(self.rung))
        self.assertEqual(cb.WAKE_PREFIX + self.ask + cb.RERING_SUFFIX, self.rung[1]["row_id"])
        self.assertIn("|id=%s|" % self.rung[1]["row_id"], self.rung[1]["payload"])
        self.assertEqual("rerung", self.wake()["state"])
        self.later(2 * cb.ACK_SECONDS)
        self.assertEqual("no_wake", self.wake()["state"])
        got = card(self.conn, "gemini", self.ask)
        self.assertEqual(("Waiting: Gemini did not wake", cb.NO_WAKE, 2, "claimed"),
                         (got["title"], got["activity"], got["version"], got["status"]))
        self.assertIsNotNone(cp.clean_item("gemini", self.ask, json.dumps(got)), "the page shows it")
        for word in ("Port", "Prince", "compare", "plans", "store"):
            self.assertNotIn(word, json.dumps(got))
        self.later(10 * cb.ACK_SECONDS)
        self.assertEqual(2, len(self.rung), "never a third ring")

    def test_an_ack_row_from_the_agent_is_the_wake_and_stops_the_second_ring(self):
        moment = NOW + datetime.timedelta(seconds=40)
        self.at(moment).settle([self.ack()])
        self.assertEqual(("acked", "GEMINI-ACK"), (self.wake()["state"], self.wake()["acked_by"]))
        self.later(5 * cb.ACK_SECONDS)
        self.assertEqual(1, len(self.rung))
        self.assertEqual("claimed in the room", card(self.conn, "gemini", self.ask)["activity"])

    def test_a_waker_relay_a_refusal_or_another_agents_row_is_not_an_ack(self):
        moment = NOW + datetime.timedelta(seconds=40)
        b = self.at(moment)
        b.settle([self.ack(extra="phase=RESULT|from=gemini|wakerreply=1|evidence=STATED")])
        b.settle([self.ack(extra="phase=ACK|from=gemini|text=STATUS: REFUSED")])
        b.settle([self.ack(sender="grok", extra="phase=ACK|from=grok")])
        b.settle([self.ack(extra="phase=ASK|from=gemini")])
        self.assertEqual("rung", self.wake()["state"])

    def test_its_own_card_for_the_ask_is_the_wake(self):
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1, "ask": self.ask,
               "ts": "2026-10-24T14:02:30Z", "title": "In the making: the comparison", "body": "x"}
        put_card(self.conn, "gemini", self.ask, own)
        self.later(30)
        self.assertEqual(("acked", "card"), (self.wake()["state"], self.wake()["acked_by"]))
        self.later(5 * cb.ACK_SECONDS)
        self.assertEqual(1, len(self.rung))

    def test_an_ack_to_the_second_ring_counts_and_a_late_one_puts_the_card_back(self):
        self.later(cb.ACK_SECONDS)
        self.at(NOW).settle([self.ack(target=cb.WAKE_PREFIX + self.ask + cb.RERING_SUFFIX)])
        self.assertEqual("acked", self.wake()["state"])
        conn = self.conn
        self.setUp()
        del conn
        self.later(cb.ACK_SECONDS)
        self.later(2 * cb.ACK_SECONDS)
        self.at(NOW + datetime.timedelta(seconds=300)).settle([self.ack()])
        self.assertEqual(("acked", True), (self.wake()["state"], self.wake()["late"]))
        got = card(self.conn, "gemini", self.ask)
        self.assertEqual(("claimed in the room", 3), (got["activity"], got["version"]))

    def test_its_result_after_no_wake_still_turns_the_card_done_with_a_newer_version(self):
        """An eventual bound artifact+completion can finish; a late RESULT cannot."""
        self.later(cb.ACK_SECONDS)
        self.later(2 * cb.ACK_SECONDS)
        done = row("gemini", "BCB|v=1|id=GEMINI-DONE|phase=RESULT|from=gemini|answers=%s|text=compared" % self.ask)
        moment = NOW + datetime.timedelta(seconds=400)
        self.assertEqual([], self.at(moment).settle([done]))
        self.assertEqual("OPEN", life.get(self.conn, self.ask)["state"])
        old_version = card(self.conn, "gemini", self.ask)["version"]
        own = {"v": 1, "type": "note", "status": "done", "public": True,
               "version": old_version + 1, "ts": cb.stamp(moment), "title": "Comparison", "body": "fixture result"}
        put_card(self.conn, "gemini", self.ask, own, moment)
        self.assertEqual("DONE", complete(self.conn, "gemini", self.ask, moment)["state"])
        got = card(self.conn, "gemini", self.ask + "-work")
        self.assertEqual(("done", old_version + 1), (got["status"], got["version"]))
        self.assertIsNone(card(self.conn, "gemini", self.ask))

    def test_the_latency_record_carries_the_wake(self):
        self.later(cb.ACK_SECONDS)
        self.later(2 * cb.ACK_SECONDS)
        self.later(4 * cb.ACK_SECONDS)
        self.assertEqual("no_wake", json.loads(self.conn.get(cb.LATENCY_KEY % self.ask))["wake"])

    def test_without_a_ring_no_wake_is_tracked(self):
        conn = FakeRedis()
        open_window(conn)
        turn(conn, "Gemini, compare the two plans.", seconds_ago=10)
        [ask] = bridge(conn).tick()
        later = NOW + datetime.timedelta(seconds=10 * cb.ACK_SECONDS)
        bridge(conn, clock=lambda: later).tick()
        self.assertIsNone(conn.get(cb.WAKE_KEY % ask))
        self.assertEqual("claimed in the room", card(conn, "gemini", ask)["activity"])

    def test_its_key_is_under_gov_and_expires(self):
        self.assertTrue((cb.WAKE_KEY % self.ask).startswith("gov:"))
        self.assertEqual(cb.TTL_SECONDS, self.conn.ttls[cb.WAKE_KEY % self.ask])


class GoAheadBinding(unittest.TestCase):
    """Aya's binding hold on d2d1618, point 3: a go-ahead confirms a validated pending TASK ask, for the voice
    assigned when he made it; absent, ambiguous or stale, nothing is claimed."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def granted(self, persona, turn_id, seconds_ago):
        return turn(self.conn, None, reason="", actor=persona, seconds_ago=seconds_ago, kind="floor.granted",
                    turn_id=turn_id)

    def pending(self):
        raw = self.conn.get(cb.PENDING_KEY % ROOM)
        return json.loads(raw) if raw else None

    def test_what_is_a_task_ask(self):
        f = {"kind": "turn.started", "actor_id": "owner-1", "reason": "continuity"}
        for said in ("Map the flow.", "Gemini, what do you think?", "Show me the numbers."):
            self.assertTrue(cb.task_ask(dict(f, utterance=said)), said)
        for said in ("That is how it works today.", "Do it.", "Gemini, do it.", ""):
            self.assertFalse(cb.task_ask(dict(f, utterance=said)), said)

    def test_a_turn_with_no_task_word_and_no_name_is_not_pending_and_do_it_claims_nothing(self):
        self.granted("gemini", "t-1", seconds_ago=60)
        turn(self.conn, "The sample team logs each sample by hand.", reason="continuity", seconds_ago=50, turn_id="t-2")
        self.granted("gemini", "t-2", seconds_ago=48)
        turn(self.conn, "Do it.", reason="continuity", seconds_ago=20, turn_id="t-3")
        self.assertEqual([], bridge(self.conn).tick())
        self.assertIsNone(self.pending())

    def test_the_pending_ask_keeps_its_stable_id_and_assigned_voice(self):
        turn(self.conn, "Codex, map the queue.", seconds_ago=30, turn_id="t-1")
        bridge(self.conn).tick()
        got = self.pending()
        self.assertEqual((at(30), ["chatgpt-codex-desktop"], True), (got["entry"], got["agents"], got["named"]))

    def test_a_go_ahead_naming_another_voice_than_his_ask_is_ambiguous_and_claims_nothing_new(self):
        turn(self.conn, "Codex, here is the flow I mean.", seconds_ago=40, turn_id="t-1")
        turn(self.conn, "Gemini, do it.", seconds_ago=20, turn_id="t-2")
        bridge(self.conn).tick()
        self.assertNotIn(cp.PARTITION % ("workshop", "gemini"), self.conn.hashes)
        self.assertNotIn(cp.PARTITION % ("workshop", "chatgpt-codex-desktop"), self.conn.hashes)

    def test_an_unnamed_task_ask_goes_to_the_voice_he_was_talking_to_then_not_to_the_floor_at_the_go_ahead(self):
        self.granted("gemini", "t-1", seconds_ago=60)
        turn(self.conn, "Map the flow on the page.", reason="continuity", seconds_ago=50, turn_id="t-2")
        self.granted("gemini", "t-2", seconds_ago=48)
        self.granted("grok", "t-3", seconds_ago=30)                             # the floor moved on
        turn(self.conn, "Do it.", reason="continuity", seconds_ago=20, turn_id="t-4")
        self.granted("grok", "t-4", seconds_ago=18)
        asks = bridge(self.conn).tick()
        self.assertNotIn(cp.PARTITION % ("workshop", "grok"), self.conn.hashes, "never the go-ahead's floor holder")
        self.assertTrue(any(card(self.conn, "gemini", a) for a in asks))

    def test_a_stale_floor_assigns_nobody_and_only_a_named_go_ahead_claims(self):
        self.conn.set(cb.LAST_KEY % ROOM, "grok")
        self.conn.set(cb.LAST_AT_KEY % ROOM, at(cb.FLOOR_FRESH_SECONDS + 60))
        turn(self.conn, "Map the flow on the page.", reason="continuity", seconds_ago=50, turn_id="t-2")
        turn(self.conn, "Do it.", reason="continuity", seconds_ago=20, turn_id="t-3")
        self.assertEqual([], bridge(self.conn).tick())
        self.assertEqual([], self.pending()["agents"])
        turn(self.conn, "Gemini, do it.", seconds_ago=5, turn_id="t-4")
        [ask] = bridge(self.conn).tick()
        self.assertIsNotNone(card(self.conn, "gemini", ask))

    def test_a_follow_up_never_goes_to_a_stale_floor(self):
        self.granted("grok", "t-0", seconds_ago=cb.FLOOR_FRESH_SECONDS + 60)
        turn(self.conn, "Map the flow on the page.", reason="continuity", seconds_ago=50, turn_id="t-2")
        self.assertEqual([], bridge(self.conn).tick())
        conn = self.conn = FakeRedis()
        open_window(conn)
        self.granted("grok", "t-0", seconds_ago=cb.FLOOR_FRESH_SECONDS - 60)
        turn(conn, "Map the flow on the page.", reason="continuity", seconds_ago=50, turn_id="t-2")
        [ask] = bridge(conn).tick()
        self.assertIsNotNone(card(conn, "grok", ask), "granted within FLOOR_FRESH_SECONDS: his")

    def test_a_pending_record_without_its_assignment_binds_nothing(self):
        self.conn.set(cb.PENDING_KEY % ROOM, json.dumps({"entry": at(40), "said": "draw it"}))
        self.granted("gemini", "t-1", seconds_ago=30)
        turn(self.conn, "Gemini, do it.", seconds_ago=20, turn_id="t-2")
        self.assertEqual([], bridge(self.conn).tick())


class ReadBack(unittest.TestCase):
    """The on-call read-back (the owner's order 2026-11-02; contract v1 A1-A11). Synthetic sentences only."""
    CODEX = "chatgpt-codex-desktop"

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)
        self.side = cb.SIDE_KEY % NOW.strftime("%Y%m%d")
        # These cases pin the window's LOGIC at the two-minute length they were written for; the forgiving
        # five-minute default is pinned in ForgivingReadBack below.
        saved = cb.READBACK_SECONDS
        cb.READBACK_SECONDS = 120
        self.addCleanup(setattr, cb, "READBACK_SECONDS", saved)

    def b(self, holds=True, now=NOW):
        lease = holds if callable(holds) else (lambda: holds)
        return cb.Bridge(self.conn, LOG, lease, clock=lambda: now, readback=True)

    def cards(self, agent):
        return {k: json.loads(v) for k, v in self.conn.hgetall(cp.PARTITION % ("workshop", agent)).items()}

    def inbox(self, agent):
        return self.conn.streams.get("fleet:inbox:%s" % agent, [])

    def heard(self, entry, agents, seconds_ago, conn=None, actor="chair", rid=None):
        """The chair's projection of a spoken read-back (conference chair, readback.spoken)."""
        return (conn or self.conn).xadd(STREAM, {
            "kind": cb.READBACK_SPOKEN, "actor_id": actor, "room_id": ROOM, "session_epoch": "fixture-epoch",
            "control_sequence": "4", "proj_n": "2",
            "readback_id": rid or cb.readback_id(ROOM, entry, agents)}, entry_id=at(seconds_ago))

    def test_a1_a_named_ask_proposes_and_claims_nothing(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.assertEqual([], self.b().tick())
        ask, field = cb.ask_id(ROOM, entry, self.CODEX), cb.proposal_id(ROOM, entry, self.CODEX)
        got = self.cards(self.CODEX)
        self.assertEqual([field], list(got))
        ask = field
        self.assertEqual(("review", cb.PROPOSED, "Say: Cody, go ahead (asked 10:01 ET)"),
                         (got[ask]["status"], got[ask]["activity"], got[ask]["title"]))
        self.assertIsNotNone(cp.clean_item(self.CODEX, ask, json.dumps(got[ask])), "the publisher takes it")
        self.assertEqual([], self.inbox(self.CODEX))
        self.assertEqual({}, self.conn.hgetall(cb.OPEN_KEY))
        self.assertIsNone(self.conn.get(life.TASK_KEY % cb.ask_id(ROOM, entry, self.CODEX)))

    def test_a2_his_go_ahead_claims_once_with_his_original_words(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()
        self.heard(entry, [self.CODEX], 30)
        turn(self.conn, "Codex, go ahead.", seconds_ago=20)
        ask = cb.ask_id(ROOM, entry, self.CODEX)
        self.assertEqual([ask], self.b().tick())
        self.assertEqual("claimed", self.cards(self.CODEX)[ask]["status"])
        self.assertEqual([ask], list(self.cards(self.CODEX)))                 # the proposal card is retired
        [(_, sent)] = self.inbox(self.CODEX)
        self.assertIn("draft the sample queue flow", sent["text"])
        self.assertIn(ask, self.conn.hgetall(cb.OPEN_KEY))
        self.assertEqual([], self.b().tick())
        self.assertEqual(1, len(self.inbox(self.CODEX)))
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))

    def test_a2_an_unnamed_yes_confirms_nothing_the_names_do(self):
        """Codex's row 9443: an unrelated yes must not dispatch. A bare yes or okay never confirms unnamed."""
        entry = turn(self.conn, "Grok and Gemini, compare the two sample plans.", seconds_ago=60)
        self.heard(entry, ["grok", "gemini"], 58)
        for ago, unnamed in ((55, "Yes please."), (50, "yes"), (45, "Okay."), (40, "sounds good")):
            turn(self.conn, unnamed, reason="continuity", seconds_ago=ago)
            self.assertEqual([], self.b().tick(), unnamed)
            self.assertEqual(at(ago), self.conn.get(cb.CURSOR_KEY % ROOM), "the turn was read")
        self.assertEqual([], self.inbox("grok") + self.inbox("gemini"))
        turn(self.conn, "Grok and Gemini, go ahead.", seconds_ago=30)
        # R3: the two named voices' one task is one team card, hosted by Grok, the first named.
        self.assertEqual(1, len(self.b().tick()))
        self.assertEqual(1, len(self.inbox("grok")))
        self.assertEqual(0, len(self.inbox("gemini")))

    def test_a3_a_late_go_ahead_claims_nothing_and_the_card_says_not_started(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=200)
        self.heard(entry, [self.CODEX], 190)
        turn(self.conn, "Codex, go ahead.", seconds_ago=50)          # 140 s after the read-back
        self.assertEqual([], self.b().tick())
        got = self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]
        self.assertEqual(("done", cb.NOT_STARTED, 2), (got["status"], got["activity"], got["version"]))
        self.assertTrue(got["title"].startswith("Not started, no go-ahead in time"))
        self.assertEqual([], self.inbox(self.CODEX))

    def test_a3_a_proposal_nobody_confirms_expires_on_a_later_tick(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=100)
        self.b().tick()
        ask = cb.proposal_id(ROOM, entry, self.CODEX)
        self.assertEqual("review", self.cards(self.CODEX)[ask]["status"])
        self.b(now=NOW + datetime.timedelta(seconds=30)).tick()       # 130 s after the ask
        self.assertEqual("done", self.cards(self.CODEX)[ask]["status"])
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        turn(self.conn, "Codex, go ahead.", seconds_ago=1)
        self.assertEqual([], self.b(now=NOW + datetime.timedelta(seconds=31)).tick())
        self.assertEqual([], self.inbox(self.CODEX))

    def test_a3_inside_the_window_a_tick_does_not_expire_it(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=100)
        self.b(now=NOW + datetime.timedelta(seconds=19)).tick()       # 119 s after the ask
        self.assertEqual("review", self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]["status"])

    def test_a4_cancellation_closes_the_proposal(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        turn(self.conn, "cancel that", reason="continuity", seconds_ago=40)
        turn(self.conn, "Codex, go ahead.", seconds_ago=20)
        self.assertEqual([], self.b().tick())
        got = self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]
        self.assertEqual("done", got["status"])
        self.assertTrue(got["title"].startswith("Cancelled"))
        self.assertEqual([], self.inbox(self.CODEX))

    def test_a5_a_pending_ask_is_never_replaced_and_the_newer_one_says_it_waits(self):
        """A2 (the owner, 2026-11-02): three replacements on his call meant no read-back ever finished."""
        first = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=80)
        second = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=60)
        self.heard(first, [self.CODEX], 40)
        turn(self.conn, "Codex, go ahead.", seconds_ago=20)
        self.assertEqual([cb.ask_id(ROOM, first, self.CODEX)], self.b().tick(), "the FIRST ask is the open one")
        held = self.cards(self.CODEX)[cb.proposal_id(ROOM, second, self.CODEX)]
        self.assertEqual(("done", cb.NOT_STARTED), (held["status"], held["activity"]))
        self.assertTrue(held["title"].startswith(cb.WAITING_BEHIND))
        [(_, sent)] = self.inbox(self.CODEX)
        self.assertIn("queue flow", sent["text"])
        self.assertNotIn("price table", sent["text"])
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM), "confirmed and closed")

    def test_a5_n1_the_waiting_card_is_true_whether_the_earlier_ask_was_read_back_or_not(self):
        """Grok's N1 on PR 452: it said the earlier ask waits for its read-back, also when it was already heard."""
        self.assertEqual("Not started, an earlier ask is still waiting to start", cb.WAITING_BEHIND)
        self.assertNotIn("read-back", cb.WAITING_BEHIND)

    def test_a5_the_waiting_card_is_written_once_however_often_the_turn_is_read(self):
        turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=80)
        second = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=60)
        self.b().tick()
        field = cb.proposal_id(ROOM, second, self.CODEX)
        before = self.cards(self.CODEX)[field]
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)                       # a lost cursor replays both turns
        self.b(now=NOW + datetime.timedelta(seconds=30)).tick()         # a later tick must not rewrite it
        self.assertEqual(before, self.cards(self.CODEX)[field])

    def test_a6_a_go_ahead_naming_a_voice_outside_the_proposal_claims_nothing(self):
        entry = turn(self.conn, "Grok, list the sample risks.", seconds_ago=60)
        self.heard(entry, ["grok"], 50)
        turn(self.conn, "Gemini, go ahead.", seconds_ago=40)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox("gemini") + self.inbox("grok"))
        self.assertEqual("review", self.cards("grok")[cb.proposal_id(ROOM, entry, "grok")]["status"])
        turn(self.conn, "Grok, go ahead.", seconds_ago=20)                # still open: the right one confirms
        self.assertEqual([cb.ask_id(ROOM, entry, "grok")], self.b().tick())

    def test_a6_a_go_ahead_for_one_of_a_group_claims_that_one_and_closes_the_other(self):
        """The E2 review's case: a grouped ask then a correction. The go-ahead names who works."""
        entry = turn(self.conn, "Grok and Gemini, check synthetic task Z, but only Grok.", seconds_ago=60)
        self.assertEqual([], self.b().tick())                            # A11: nothing without a go-ahead
        self.heard(entry, ["grok", "gemini"], 30)
        turn(self.conn, "Grok, go ahead.", seconds_ago=20)
        self.assertEqual([cb.ask_id(ROOM, entry, "grok")], self.b().tick())
        self.assertEqual([], self.inbox("gemini"))
        self.assertEqual("done", self.cards("gemini")[cb.proposal_id(ROOM, entry, "gemini")]["status"])

    def test_a6_a_go_ahead_with_nothing_proposed_claims_nothing(self):
        turn(self.conn, "Could someone draft the sample plan?", reason="everyone", seconds_ago=60)
        turn(self.conn, "Claude, go ahead.", seconds_ago=20)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox("claude-code-cli"))

    def test_a7_replay_and_cursor_loss_stay_exactly_once(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.heard(entry, [self.CODEX], 30)
        turn(self.conn, "Codex, go ahead.", seconds_ago=20)
        ask = cb.ask_id(ROOM, entry, self.CODEX)
        self.assertEqual([ask], self.b().tick())
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)                       # a lost cursor re-reads both turns
        self.assertEqual([], self.b().tick())
        self.assertEqual("claimed", self.cards(self.CODEX)[ask]["status"])
        self.assertEqual([ask], list(self.cards(self.CODEX)))                 # never put back to a proposal
        self.assertEqual(1, len(self.inbox(self.CODEX)))

    def test_a7_a_closed_proposal_is_never_reopened_by_a_replay(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        turn(self.conn, "cancel that", reason="continuity", seconds_ago=40)
        self.b().tick()
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)
        self.b().tick()
        self.assertEqual("done", self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]["status"])
        turn(self.conn, "Codex, go ahead.", seconds_ago=20)
        self.assertEqual([], self.b().tick())

    def test_a7_an_older_turn_read_late_never_replaces_the_open_proposal(self):
        newer = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=30)
        self.b().tick()
        older = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)
        self.b().tick()
        self.assertEqual("review", self.cards(self.CODEX)[cb.proposal_id(ROOM, newer, self.CODEX)]["status"])
        self.assertNotIn(cb.proposal_id(ROOM, older, self.CODEX), self.cards(self.CODEX))

    def test_a8_no_lease_no_window_or_a_chair_turn_proposes_nothing(self):
        turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.assertEqual([], self.b(holds=False).tick())
        shut = FakeRedis()
        shut.streams = self.conn.streams
        self.assertEqual([], cb.Bridge(shut, LOG, lambda: True, clock=lambda: NOW, readback=True).tick())
        self.assertEqual({}, self.cards(self.CODEX))
        self.assertEqual({}, shut.hgetall(cp.PARTITION % ("workshop", self.CODEX)))
        chair = FakeRedis()
        open_window(chair)
        self.conn = chair
        turn(self.conn, "Codex, draft the sample queue flow.", actor="chair", seconds_ago=60)
        self.b().tick()
        self.assertEqual({}, self.cards(self.CODEX))

    def test_a9_a_voice_that_cannot_proposes_and_never_claims_by_itself(self):
        """Codex's row 9443 G3: a voice's "I can't" is a NON-EXECUTING disposition: a proposal, never a claim."""
        entry = turn(self.conn, "Could someone find a sample chair picture?", reason="everyone", seconds_ago=30)
        self.conn.xadd(self.side, {"by": "chair", "voice": "claude", "room": ROOM, "text": "I can't pull an image.",
                                   "at": cb.stamp(NOW)}, entry_id=at(25))
        ask, field = cb.ask_id(ROOM, entry, "claude-code-cli"), cb.proposal_id(ROOM, entry, "claude-code-cli")
        self.assertEqual([], self.b().tick())
        self.assertEqual([field], list(self.cards("claude-code-cli")))
        self.assertEqual("review", self.cards("claude-code-cli")[field]["status"])
        self.assertEqual([], self.inbox("claude-code-cli"))
        self.assertEqual({}, self.conn.hgetall(cb.OPEN_KEY))
        self.conn.store.pop(cb.SIDE_CURSOR_KEY % ROOM)                  # the same "I can't" read again
        self.assertEqual([], self.b().tick())
        self.heard(entry, ["claude-code-cli"], 15)
        turn(self.conn, "Claude, go ahead.", seconds_ago=10)
        self.assertEqual([ask], self.b().tick())
        self.assertEqual([ask], list(self.cards("claude-code-cli")))
        self.assertIn("find a sample chair picture", self.inbox("claude-code-cli")[0][1]["text"])

    def test_a9_a_voice_that_cannot_joins_the_proposal_of_the_same_turn(self):
        entry = turn(self.conn, "Grok, list the sample risks.", seconds_ago=30)
        self.conn.xadd(self.side, {"by": "chair", "voice": "gemini", "room": ROOM, "text": "I can't open files.",
                                   "at": cb.stamp(NOW)}, entry_id=at(25))
        self.assertEqual([], self.b().tick())
        self.assertEqual(["grok", "gemini"], json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["agents"])
        self.heard(entry, ["grok", "gemini"], 15)
        turn(self.conn, "Grok, go ahead.", seconds_ago=10)
        self.assertEqual([cb.ask_id(ROOM, entry, "grok")], self.b().tick())
        self.assertEqual([], self.inbox("gemini"))
        self.assertEqual("done", self.cards("gemini")[cb.proposal_id(ROOM, entry, "gemini")]["status"])

    def test_g2_only_the_caller_who_asked_in_the_same_session_confirms_once(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.heard(entry, [self.CODEX], 55)
        turn(self.conn, "Codex, go ahead.", actor="own_fixture_guest", seconds_ago=50)      # another caller
        turn(self.conn, "Codex, go ahead.", session_epoch="other-epoch", seconds_ago=45)  # another chair session
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(self.CODEX))
        self.assertEqual("review", self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]["status"])
        turn(self.conn, "Codex, go ahead.", seconds_ago=40)
        turn(self.conn, "Codex, go ahead.", seconds_ago=30)                               # a duplicate
        self.assertEqual([cb.ask_id(ROOM, entry, self.CODEX)], self.b().tick())
        self.assertEqual(1, len(self.inbox(self.CODEX)))

    def granted(self, persona, turn_id, seconds_ago):
        return turn(self.conn, None, reason="", actor=persona, seconds_ago=seconds_ago, kind="floor.granted",
                    turn_id=turn_id)

    def test_a2_an_unnamed_task_turn_is_never_assigned_to_the_floor_holder(self):
        """A2 (the owner, 2026-11-02): his 36-char aside to the floor holder became tonight's claimed card."""
        self.granted("grok", "t-1", seconds_ago=40)
        turn(self.conn, "Draft the sample queue flow now please.", reason="continuity", seconds_ago=30, turn_id="t-1")
        turn(self.conn, "That is a fine sample thought.", reason="continuity", seconds_ago=20, turn_id="t-1")
        self.assertEqual([], self.b().tick())
        self.assertEqual({}, self.cards("grok"))
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.assertEqual({}, self.conn.hgetall(cb.READBACK_KEY % ROOM))

    def test_a3_an_early_go_ahead_keeps_the_ask_open_for_the_one_after_the_read_back(self):
        """A3 (the owner, 2026-11-02): his go-ahead before the read-back was dropped and the ask then expired."""
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        turn(self.conn, "Codex, go ahead.", seconds_ago=50)              # before the read-back
        self.assertEqual([], self.b().tick())
        self.assertEqual("review", self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]["status"])
        self.assertIsNotNone(self.conn.get(cb.PROPOSAL_KEY % ROOM), "still open")
        self.heard(entry, [self.CODEX], 30)
        turn(self.conn, "Codex, go ahead.", seconds_ago=10)
        self.assertEqual([cb.ask_id(ROOM, entry, self.CODEX)], self.b().tick())

    def test_b1_a_long_turn_never_becomes_a_read_back_and_the_card_asks_for_a_restatement(self):
        """B1 (the owner, 2026-11-02): a 1058-char capture was recited for 63 seconds on his call."""
        words = "Codex, draft " + " ".join(["sample"] * (cb.ASK_MAX_WORDS - 1)) + "."
        entry = turn(self.conn, words, seconds_ago=60)
        self.assertEqual([], self.b().tick())
        card = self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]
        self.assertEqual(("done", cb.NOT_STARTED), (card["status"], card["activity"]))
        self.assertTrue(card["title"].startswith(cb.TOO_LONG))
        self.assertNotIn("sample", str(card))
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.assertEqual({}, self.conn.hgetall(cb.READBACK_KEY % ROOM))
        long_chars = "Codex, draft " + "x" * cb.ASK_MAX_CHARS
        chars = turn(self.conn, long_chars, seconds_ago=40)
        self.assertEqual([], self.b().tick())
        self.assertTrue(self.cards(self.CODEX)[cb.proposal_id(ROOM, chars, self.CODEX)]["title"]
                        .startswith(cb.TOO_LONG))
        before = self.cards(self.CODEX)
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)                       # a lost cursor replays both turns
        self.b(now=NOW + datetime.timedelta(seconds=30)).tick()         # a later tick must not rewrite them
        self.assertEqual(before, self.cards(self.CODEX))

    def test_a5_a_waiting_ask_gets_its_turn_after_a_restart_once_the_earlier_one_is_gone(self):
        """Grok on PR 452: a bridge restart or backlog replay must not lose the queued ask. Its waiting card
        does not block the replayed proposal once the earlier ask is closed."""
        turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=170)
        second = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=60)
        self.b(now=NOW - datetime.timedelta(seconds=55)).tick()         # first proposed, second waits behind it
        field = cb.proposal_id(ROOM, second, self.CODEX)
        self.assertTrue(self.cards(self.CODEX)[field]["title"].startswith(cb.WAITING_BEHIND))
        self.b().tick()                                                 # the first ask expires unheard
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)                       # a restart: the cursor is gone
        self.b().tick()
        got = self.cards(self.CODEX)[field]
        self.assertEqual(("review", cb.PROPOSED), (got["status"], got["activity"]), "its turn now")
        self.assertEqual(second, json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["entry"])
        self.heard(second, [self.CODEX], 5)
        turn(self.conn, "Codex, go ahead.", seconds_ago=2)
        self.assertEqual([cb.ask_id(ROOM, second, self.CODEX)], self.b().tick())

    def test_a5_a_waiting_card_under_the_old_wording_still_yields_to_the_replay(self):
        """Codex's HOLD on adc6558e: a waiting card PR 452 wrote, under its own wording, must not block the
        queued ask once the earlier one is gone."""
        turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=170)
        second = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=60)
        self.b(now=NOW - datetime.timedelta(seconds=55)).tick()
        key, field = cp.PARTITION % ("workshop", self.CODEX), cb.proposal_id(ROOM, second, self.CODEX)
        card = self.cards(self.CODEX)[field]
        card["title"] = "Not started, an earlier ask is still waiting for its read-back" + card["title"][len(cb.WAITING_BEHIND):]
        self.conn.hset(key, field, json.dumps(card))                    # as PR 452 would have written it
        self.b().tick()                                                 # the first ask expires unheard
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)                       # a restart: the cursor is gone
        self.b().tick()
        got = self.cards(self.CODEX)[field]
        self.assertEqual(("review", cb.PROPOSED), (got["status"], got["activity"]), "its turn now")
        self.assertEqual(second, json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["entry"])

    def test_a5_every_waiting_wording_ever_written_is_kept(self):
        self.assertEqual(cb.WAITING_BEHIND, cb.WAITING_TITLES[0], "the current wording is the one written")
        self.assertIn("Not started, an earlier ask is still waiting for its read-back", cb.WAITING_TITLES)

    def test_a5_h2_an_expired_unheard_proposal_yields_to_the_newer_ask_with_the_cursor_kept(self):
        """Grok on PR 452, H2: with the cursor kept there is no replay, so an ask that waited behind a proposal
        whose window had already closed would be lost for good. The expired one closes; the newer one proposes."""
        first = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=300)
        second = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=30)
        self.assertEqual([], self.b().tick())                            # ONE tick, both read, cursor kept
        old = self.cards(self.CODEX)[cb.proposal_id(ROOM, first, self.CODEX)]
        self.assertTrue(old["title"].startswith("Not started, the read-back was not heard"), old["title"])
        got = self.cards(self.CODEX)[cb.proposal_id(ROOM, second, self.CODEX)]
        self.assertEqual(("review", cb.PROPOSED), (got["status"], got["activity"]), "the newer ask took its place")
        self.assertEqual(second, json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["entry"])
        self.heard(second, [self.CODEX], 20)
        turn(self.conn, "Codex, go ahead.", seconds_ago=10)
        self.assertEqual([cb.ask_id(ROOM, second, self.CODEX)], self.b().tick())

    def test_a5_h2_an_expired_heard_proposal_yields_too_and_says_no_go_ahead_in_time(self):
        first = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=300)
        self.heard(first, [self.CODEX], 200)
        second = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=30)
        self.assertEqual([], self.b().tick())
        old = self.cards(self.CODEX)[cb.proposal_id(ROOM, first, self.CODEX)]
        self.assertTrue(old["title"].startswith("Not started, no go-ahead in time"), old["title"])
        self.assertEqual(second, json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["entry"])

    def test_a5_h3_a_replay_never_closes_a_live_read_back_proposal(self):
        """Grok's R3 probe: older ask, newer ask, THEN the read-back of the older; a replay must not close the
        live proposal as "no go-ahead in time" and put the unheard newer ask in its place."""
        older = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=100)
        second = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=80)
        self.heard(older, [self.CODEX], 60)                              # the read-back came AFTER the newer ask
        self.b().tick()
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)                       # a restart or backlog replay
        self.b().tick()
        held = json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.assertEqual((older, True), (held["entry"], bool(held.get("spoken"))), "the older, still heard")
        self.assertTrue(self.cards(self.CODEX)[cb.proposal_id(ROOM, second, self.CODEX)]["title"]
                        .startswith(cb.WAITING_BEHIND))
        turn(self.conn, "Codex, go ahead.", seconds_ago=10)
        self.assertEqual([cb.ask_id(ROOM, older, self.CODEX)], self.b().tick(), "and his go-ahead still claims it")

    def test_a5_h3_the_window_is_measured_from_the_read_back_once_heard(self):
        """An old ask whose read-back is fresh is alive: the newer ask waits (the spoken leg of the window)."""
        older = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=200)
        self.heard(older, [self.CODEX], 100)
        second = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=30)
        self.assertEqual([], self.b().tick())
        self.assertEqual(older, json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["entry"], "still his to confirm")
        self.assertTrue(self.cards(self.CODEX)[cb.proposal_id(ROOM, second, self.CODEX)]["title"]
                        .startswith(cb.WAITING_BEHIND))

    def test_a5_an_unreadable_card_still_blocks_a_replayed_proposal(self):
        """Only a WAITING card yields to the replay; a card that cannot be read blocks, as it always did."""
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        field = cb.proposal_id(ROOM, entry, self.CODEX)
        self.conn.hset(cp.PARTITION % ("workshop", self.CODEX), field, "not json {")
        self.assertEqual([], self.b().tick())
        self.assertEqual("not json {", self.conn.hgetall(cp.PARTITION % ("workshop", self.CODEX))[field])
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))

    def test_a5_the_proposal_card_says_a_newer_ask_waits_and_never_that_it_drops(self):
        """Grok on PR 452: the card still said a newer ask drops the waiting one."""
        card = cb.proposal_card(self.CODEX, NOW, NOW)
        self.assertIn("a newer ask waits behind it", card["body"])
        for stale in ("drops it. ", "newer ask drops", "Replaced"):
            self.assertNotIn(stale, card["title"] + " " + card["body"].replace("Cancel drops it;", ""), stale)

    def test_fix5_a_proposal_closed_after_the_window_shut_is_still_published(self):
        """FIX 5 (the owner's 8 PM call): rb-5d43 retired at 8:16 after the window shut, and the public page kept
        it as a review card. The close reopens the window for that one item, so the publisher carries it."""
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=100)
        self.b().tick()
        field = cb.proposal_id(ROOM, entry, self.CODEX)
        self.conn.store.pop(cp.WINDOW_PREFIX + "workshop")              # the call's window shut
        self.assertEqual([], cp.active_windows(self.conn, NOW))
        later = NOW + datetime.timedelta(seconds=30)                     # 130 s: the unheard proposal expires
        published = cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: later, readback=True, bucket="sfdc24-canvas")
        published.tick()
        got = self.cards(self.CODEX)[field]
        self.assertTrue(got["title"].startswith("Not started, the read-back was not heard"))
        self.assertEqual(["workshop"], [room for room, _ in cp.active_windows(self.conn, later)], "reopened")
        window = self.conn.get(cp.WINDOW_PREFIX + "workshop")
        self.assertIn("%s/%s" % (self.CODEX, field), cp.scope_of(self.conn, "workshop", window),
                      "scoped to exactly the closed card")

    def test_fix5_a_close_inside_an_open_window_adds_the_item_and_opens_nothing_new(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=100)
        self.b().tick()
        before = self.conn.get(cp.WINDOW_PREFIX + "workshop")
        later = NOW + datetime.timedelta(seconds=30)
        cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: later, readback=True, bucket="sfdc24-canvas").tick()
        self.assertEqual(before, self.conn.get(cp.WINDOW_PREFIX + "workshop"), "the open window is left as it is")
        self.assertTrue(self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]["title"]
                        .startswith("Not started"))

    def test_b1_a_turn_at_the_word_bound_still_proposes(self):
        words = "Codex, draft " + " ".join(["sample"] * (cb.ASK_MAX_WORDS - 2)) + "."
        self.assertEqual(cb.ASK_MAX_WORDS, len(words.split()))
        turn(self.conn, words, seconds_ago=60)
        self.assertEqual([], self.b().tick())
        self.assertIsNotNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))

    def test_b3_the_key_names_voices_by_the_chairs_persona_ids_for_every_agent(self):
        """Codex on PR 452 R4: the key carried fleet ids; the chair matches persona ids (claude, codex, ...)."""
        self.assertEqual({"grok": "grok", "claude-code-cli": "claude", "chatgpt-codex-desktop": "codex",
                          "gemini": "gemini"}, cb.PERSONA_OF)
        for agent, persona in cb.PERSONA_OF.items():
            self.assertEqual(agent, cb.AGENT_OF[persona], "the exact inverse")
        entry = turn(self.conn, "Claude and Codex, compare the two sample plans.", seconds_ago=60)
        self.b().tick()
        held = self.conn.hgetall(cb.READBACK_KEY % ROOM)
        self.assertEqual("claude,codex", held["agents"])
        self.assertEqual(cb.readback_id(ROOM, entry, ["claude-code-cli", self.CODEX]), held["id"],
                         "the id still binds the bridge's own recipient set")

    def test_b3_the_key_names_every_voice_of_the_recipient_set_in_order(self):
        entry = turn(self.conn, "Grok and Gemini, compare the two sample plans.", seconds_ago=60)
        self.b().tick()
        held = self.conn.hgetall(cb.READBACK_KEY % ROOM)
        self.assertEqual({"id": cb.readback_id(ROOM, entry, ["grok", "gemini"]), "entry": entry,
                          "agents": "grok,gemini", "max_words": "60"}, held)

    def test_g2_a_refusal_to_a_turn_with_no_known_caller_proposes_nothing(self):
        turn(self.conn, None, reason="", actor="grok", kind="floor.granted", seconds_ago=40)   # the room is live
        self.conn.xadd(STREAM, {"kind": "transcript.line", "actor_id": "own_fixture_owner", "room_id": ROOM,
                                "utterance": "Could someone find a sample chair picture?"}, entry_id=at(30))
        self.conn.set(cb.ASKED_KEY % ROOM, json.dumps({"entry": at(30), "at": cb.stamp(NOW)}))   # no caller
        self.conn.xadd(self.side, {"by": "chair", "voice": "claude", "room": ROOM, "text": "I can't pull an image.",
                                   "at": cb.stamp(NOW)}, entry_id=at(25))
        self.assertEqual([], self.b().tick())
        self.assertEqual({}, self.cards("claude-code-cli"))
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))

    def test_g2_a_proposal_without_its_caller_binding_confirms_nothing(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()
        self.conn.set(cb.PROPOSAL_KEY % ROOM, json.dumps({"entry": entry, "said": "x", "agents": [self.CODEX]}))
        self.heard(entry, [self.CODEX], 30)
        turn(self.conn, "Codex, go ahead.", seconds_ago=20)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(self.CODEX))

    def test_a10_his_words_stay_private(self):
        turn(self.conn, "Codex, draft the price sheet for the client's Port-au-Prince store.", seconds_ago=60)
        self.b().tick()
        published = json.dumps(cp.snapshot(self.conn, "workshop", NOW))
        for word in ("price sheet", "Port-au-Prince", "client"):
            self.assertNotIn(word, published)
        self.assertIn("go ahead", published)
        self.assertTrue((cb.PROPOSAL_KEY % ROOM).startswith("gov:"))
        self.assertIn("price sheet", self.conn.get(cb.PROPOSAL_KEY % ROOM))

    def test_a11_review_failures_make_at_most_a_proposal(self):
        """Every sentence an independent review found the parser admitting or inverting (rounds 1-3, E1, E2):
        without a go-ahead, none writes an inbox entry, an open claim or a claimed card."""
        for said in ("Claude, review complete.", "Gemini, build successful.", "Claude, review ready as of noon.",
                     "Gemini, build stable since Tuesday.", "Codex, test green as of noon.",
                     "The example was 'Don't run anything. Claude, publish synthetic widget E.'",
                     "Grok and Gemini, investigate synthetic widget E, but not Gemini.",
                     "Grok and Gemini, investigate synthetic task Z, but only Grok.",
                     "Claude, review completed yesterday.", "Please assign sample task Q to Gemini.",
                     "Claude, do not publish sample widget Q."):
            self.conn = FakeRedis()
            open_window(self.conn)
            turn(self.conn, said, seconds_ago=60)
            self.assertEqual([], self.b().tick(), said)
            self.assertEqual({}, self.conn.hgetall(cb.OPEN_KEY), said)
            self.assertEqual([], [k for k in self.conn.streams if k.startswith("fleet:inbox:")], said)
            for agent in cb.NAME_OF:
                self.assertTrue(all(c["status"] == "review" for c in self.cards(agent).values()), (said, agent))

    def test_with_no_window_open_the_proposal_opens_a_scoped_one_and_is_on_the_page(self):
        conn = FakeRedis()
        entry = turn(conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        b = cb.Bridge(conn, LOG, lambda: True, clock=lambda: NOW, bucket="sfdc24-canvas", readback=True)
        self.assertEqual([], b.tick())

        def page():
            raw = conn.get(cp.WINDOW_PREFIX + "workshop")
            return [i["id"] for i in cp.snapshot(conn, "workshop", NOW, scope=cp.scope_of(conn, "workshop", raw))["items"]]
        field, ask = cb.proposal_id(ROOM, entry, self.CODEX), cb.ask_id(ROOM, entry, self.CODEX)
        self.assertEqual([self.CODEX + "/" + field], page())
        self.heard(entry, [self.CODEX], 30, conn=conn)
        turn(conn, "Codex, go ahead.", seconds_ago=20)
        self.assertEqual([ask], b.tick())
        self.assertEqual([self.CODEX + "/" + ask], page())

    def test_a7_an_expired_proposal_is_not_revived_by_a_replay_with_a_go_ahead_in_its_old_window(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=100)
        self.heard(entry, [self.CODEX], 95)
        self.b(now=NOW + datetime.timedelta(seconds=30)).tick()       # expired, 125 s after the read-back
        turn(self.conn, "Codex, go ahead.", seconds_ago=1)               # 94 s after it, read only now
        self.conn.store.pop(cb.CURSOR_KEY % ROOM)
        self.assertEqual([], self.b(now=NOW + datetime.timedelta(seconds=31)).tick())
        self.assertEqual("done", self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]["status"])
        self.assertEqual([], self.inbox(self.CODEX))

    def test_a7_an_old_go_ahead_or_cancel_read_again_never_touches_a_newer_proposal(self):
        for closing in ("Codex, go ahead.", "cancel that"):
            self.conn = FakeRedis()
            open_window(self.conn)
            first = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=90)
            self.heard(first, [self.CODEX], 85)
            turn(self.conn, closing, reason="vocative", seconds_ago=80)
            self.b().tick()
            newer = turn(self.conn, "Codex, draft the sample price table.", seconds_ago=60)
            self.b().tick()
            self.conn.store.pop(cb.CURSOR_KEY % ROOM)
            self.b().tick()
            self.assertEqual("review", self.cards(self.CODEX)[cb.proposal_id(ROOM, newer, self.CODEX)]["status"],
                             closing)
            self.assertEqual(1 if closing.startswith("Codex") else 0, len(self.inbox(self.CODEX)), closing)
        self.assertFalse(cb._within(at(10), at(60), 120))                 # a later-first pair is never "within"
        self.assertTrue(cb._within(at(60), at(10), 120))

    def test_a1_a_task_word_and_a_name_propose_even_when_the_chair_heard_no_name(self):
        entry = turn(self.conn, "Draft the sample plan, Codex.", reason="continuity", seconds_ago=60)
        self.assertEqual([], self.b().tick())
        self.assertEqual("review", self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]["status"])

    def test_a8_a_lease_lost_mid_pass_proposes_nothing(self):
        calls = []

        def lease():
            calls.append(1)
            return len(calls) == 1
        turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.assertEqual([], self.b(holds=lease).tick())
        self.assertEqual({}, self.cards(self.CODEX))

    def test_closing_touches_only_a_card_that_is_still_a_proposal(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        field = cb.proposal_id(ROOM, entry, self.CODEX)
        other = dict(cb.unstarted_card(self.CODEX, NOW, "Cancelled", NOW))
        self.conn.hset(cp.PARTITION % ("workshop", self.CODEX), field, json.dumps(other))
        self.b()._close(ROOM, {"entry": entry, "said": "x", "agents": [self.CODEX]}, [self.CODEX], "Replaced", NOW)
        self.assertEqual(other, self.cards(self.CODEX)[field])

    def test_g1_the_task_is_read_back_by_the_chair_before_anything_can_start(self):
        """Codex's row 9443 G1 and the owner's order: the exact task and who it is for, for the chair to say."""
        entry = turn(self.conn, "Codex, draft the sample queue flow", seconds_ago=60)
        self.b().tick()
        held = self.conn.hgetall(cb.READBACK_KEY % ROOM)
        self.assertEqual({"id": cb.readback_id(ROOM, entry, [self.CODEX]), "entry": entry,
                          "agents": "codex", "max_words": "60"}, held)           # the chair's persona id
        self.assertTrue((cb.READBACK_KEY % ROOM).startswith("v1:conf:%s:" % ROOM))   # the room's own key
        self.assertEqual(cb.READBACK_SECONDS - 60, self.conn.ttls[cb.READBACK_KEY % ROOM])   # F4
        self.assertNotIn("sample queue", json.dumps(cp.snapshot(self.conn, "workshop", NOW)))
        turn(self.conn, "Codex, go ahead.", seconds_ago=50)              # before the read-back: not heard yet
        self.assertEqual([], self.b().tick())
        self.heard(entry, [self.CODEX], 40)
        self.assertEqual([], self.b().tick())
        self.assertEqual({}, self.conn.hgetall(cb.READBACK_KEY % ROOM), "spoken once, then gone")
        turn(self.conn, "Codex, go ahead.", seconds_ago=30)
        self.assertEqual([cb.ask_id(ROOM, entry, self.CODEX)], self.b().tick())

    def test_g1_the_two_minutes_run_from_the_read_back_not_the_ask(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=200)
        self.heard(entry, [self.CODEX], 100)                              # the chair waited for a quiet moment
        turn(self.conn, "Codex, go ahead.", seconds_ago=50)              # 150 s after the ask, 50 after it
        self.assertEqual([cb.ask_id(ROOM, entry, self.CODEX)], self.b().tick())

    def test_g1_only_the_chair_s_read_back_of_this_exact_proposal_counts(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()
        self.heard(entry, [self.CODEX], 50, actor="own_fixture_owner")          # not the chair
        self.heard(entry, [self.CODEX], 45, rid="rb-000000000000")                # another read-back
        self.heard(entry, ["grok"], 40)                                           # another recipient set
        turn(self.conn, "Codex, go ahead.", seconds_ago=30)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(self.CODEX))
        self.assertIsNone(json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM)).get("spoken"))

    def test_g1_a_voice_joining_after_the_read_back_needs_a_new_one(self):
        entry = turn(self.conn, "Grok, list the sample risks.", seconds_ago=60)
        self.heard(entry, ["grok"], 50)
        self.b().tick()
        self.assertIsNotNone(json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM)).get("spoken"))
        self.conn.xadd(self.side, {"by": "chair", "voice": "gemini", "room": ROOM, "text": "I can't open files.",
                                   "at": cb.stamp(NOW)}, entry_id=at(45))
        self.b().tick()
        self.assertIsNone(json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM)).get("spoken"))
        self.assertEqual(cb.readback_id(ROOM, entry, ["grok", "gemini"]),
                         self.conn.hgetall(cb.READBACK_KEY % ROOM)["id"])
        turn(self.conn, "Grok, go ahead.", seconds_ago=40)
        self.assertEqual([], self.b().tick())                            # what he heard is not what is open now
        self.heard(entry, ["grok", "gemini"], 35)
        turn(self.conn, "Grok, go ahead.", seconds_ago=30)
        self.assertEqual([cb.ask_id(ROOM, entry, "grok")], self.b().tick())

    def test_g1_a_proposal_never_read_back_closes_as_not_heard_and_its_read_back_goes(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=100)
        self.b().tick()
        self.b(now=NOW + datetime.timedelta(seconds=30)).tick()
        got = self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]
        self.assertTrue(got["title"].startswith("Not started, the read-back was not heard"))
        self.assertEqual({}, self.conn.hgetall(cb.READBACK_KEY % ROOM))
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))

    def test_the_switch_reads_off_only_and_never_selects_claiming_at_once(self):
        """Codex's review of row 9443, F1: the environment reads one switch, and it can only pause the read-back."""
        source = (ROOT / "scripts" / "call_bridge.py").read_text(encoding="utf-8")
        lines = [x for x in source.splitlines() if x.startswith(("READBACK = ", "READBACK_OFF = "))]
        self.assertEqual(['READBACK = False',
                          'READBACK_OFF = os.environ.get("CALL_BRIDGE_READBACK", "on").strip().lower() == "off"'], lines)
        self.assertEqual(1, source.count('os.environ.get("CALL_BRIDGE_READBACK"'))
        for value, off in (("on", False), ("", False), ("OFF", True), ("off ", True), ("no", False)):
            self.assertEqual(off, value.strip().lower() == "off", value)

    def load(self, value):
        """call_bridge as a fresh module, imported with CALL_BRIDGE_READBACK=value (this file pins cb.READBACK)."""
        import importlib.util                                            # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("call_bridge_env", ROOT / "scripts" / "call_bridge.py")
        fresh = importlib.util.module_from_spec(spec)
        with mock.patch.dict("os.environ", {"CALL_BRIDGE_READBACK": value}):
            spec.loader.exec_module(fresh)
        fresh.READBACK = True       # the switch is pinned in read-back mode; the code default is direct start
        return fresh

    def test_f1_switched_off_the_bridge_is_paused_never_claiming_at_once(self):
        for value, paused in (("off", True), ("OFF ", True), ("on", False), ("", False)):
            fresh = self.load(value)
            bridge = fresh.Bridge(FakeRedis(), LOG, lambda: True, clock=lambda: NOW)
            self.assertEqual((True, paused), (bridge.readback, bridge.paused), value)

    def test_f1_switched_off_nothing_said_on_the_call_starts_anything(self):
        """Codex's review of row 9443, F1: off, a named ask, a voice's "I can't", a heard read-back and his named
        go-ahead all start nothing; an open proposal closes visibly."""
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()                                                  # proposed while the read-back was on
        self.assertEqual("review", self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]["status"])
        fresh = self.load("off")

        def off():
            return fresh.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW)
        self.heard(entry, [self.CODEX], 50)
        turn(self.conn, "Codex, go ahead.", seconds_ago=40)
        self.assertEqual([], off().tick())
        got = self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]
        self.assertEqual(("done", cb.NOT_STARTED), (got["status"], got["activity"]))
        self.assertTrue(got["title"].startswith(cb.READ_BACK_OFF))
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        later = turn(self.conn, "Grok, list the sample risks.", seconds_ago=30)
        self.conn.xadd(self.side, {"by": "chair", "voice": "gemini", "room": ROOM, "text": "I can't open files.",
                                   "at": cb.stamp(NOW)}, entry_id=at(25))
        self.assertEqual([], off().tick())
        self.heard(later, ["grok"], 20)
        turn(self.conn, "Grok, go ahead.", seconds_ago=10)
        self.assertEqual([], off().tick())
        self.assertEqual({}, self.cards("grok"))
        self.assertEqual({}, self.cards("gemini"))
        self.assertEqual([], self.inbox(self.CODEX) + self.inbox("grok") + self.inbox("gemini"))
        self.assertEqual({}, self.conn.hgetall(cb.OPEN_KEY))
        for agent, said in ((self.CODEX, entry), ("grok", later), ("gemini", later)):
            self.assertIsNone(self.conn.get(life.TASK_KEY % cb.ask_id(ROOM, said, agent)))

    def holders(self, words):
        """Every Redis key whose value holds these words."""
        found = []
        for store in (self.conn.store, self.conn.hashes, self.conn.streams):
            found += [k for k, v in store.items() if words in json.dumps(v, default=str)]
        return sorted(found)

    def test_f2_his_words_live_only_in_the_stream_the_proposal_the_read_back_and_the_inbox(self):
        """Codex's review of row 9443, F2 (C9): no pending or asked copy in read-back mode, at any step."""
        words = "find a sample chair picture"
        events, proposal, readback = STREAM, cb.PROPOSAL_KEY % ROOM, cb.READBACK_KEY % ROOM
        entry = turn(self.conn, "Could someone find a sample chair picture?", reason="everyone", seconds_ago=30)
        self.conn.xadd(self.side, {"by": "chair", "voice": "claude", "room": ROOM, "text": "I can't pull an image.",
                                   "at": cb.stamp(NOW)}, entry_id=at(25))
        self.assertEqual([], self.b().tick())
        self.assertEqual(sorted([events, proposal]), self.holders(words), "never the read-back key (contract v2)")
        self.assertNotIn("said", json.loads(self.conn.get(cb.ASKED_KEY % ROOM)))
        self.assertIsNone(self.conn.get(cb.PENDING_KEY % ROOM))
        self.assertNotEqual({}, self.conn.hgetall(readback), "the key exists, without his words")
        self.heard(entry, ["claude-code-cli"], 15)
        self.b().tick()
        self.assertEqual(sorted([events, proposal]), self.holders(words))
        turn(self.conn, "Claude, go ahead.", seconds_ago=10)
        self.assertEqual([cb.ask_id(ROOM, entry, "claude-code-cli")], self.b().tick())
        self.assertEqual(sorted([events, "fleet:inbox:claude-code-cli"]), self.holders(words))

    def test_f2_a_task_ask_writes_no_pending_copy_in_read_back_mode(self):
        turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()
        self.assertIsNone(self.conn.get(cb.PENDING_KEY % ROOM))
        self.assertEqual(sorted([STREAM, cb.PROPOSAL_KEY % ROOM]), self.holders("draft the sample queue flow"))

    def test_f2_a_voice_that_cannot_reads_his_words_from_the_stream_or_proposes_nothing(self):
        """No copy: a turn gone from the room's stream (trimmed) proposes nothing, rather than an empty task."""
        self.conn.set(cb.ASKED_KEY % ROOM, json.dumps({"entry": at(30), "at": cb.stamp(NOW),
                                                       "actor": "own_fixture_owner", "epoch": "fixture-epoch"}))
        turn(self.conn, None, reason="", actor="grok", kind="floor.granted", seconds_ago=40)   # the room is live
        self.conn.xadd(STREAM, {"kind": "transcript.line", "actor_id": "grok", "room_id": ROOM,
                                "utterance": "Something else entirely."}, entry_id=at(28))  # a later line
        self.conn.xadd(self.side, {"by": "chair", "voice": "claude", "room": ROOM, "text": "I can't pull an image.",
                                   "at": cb.stamp(NOW)}, entry_id=at(25))
        self.assertEqual([], self.b().tick())
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.assertEqual({}, self.cards("claude-code-cli"))

    def test_f3_a_read_back_receipt_from_another_chair_session_is_not_heard(self):
        """Codex's review of row 9443, F3: the receipt binds the asker's chair session, as his go-ahead does."""
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()
        rid = cb.readback_id(ROOM, entry, [self.CODEX])
        for ago, epoch in ((55, "other-epoch"), (50, None), (45, "")):
            fields = {"kind": cb.READBACK_SPOKEN, "actor_id": "chair", "room_id": ROOM, "readback_id": rid}
            if epoch is not None:
                fields["session_epoch"] = epoch
            self.conn.xadd(STREAM, fields, entry_id=at(ago))
        turn(self.conn, "Codex, go ahead.", seconds_ago=40)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(self.CODEX))
        self.assertIsNone(json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM)).get("spoken"))
        self.assertEqual(rid, self.conn.hgetall(cb.READBACK_KEY % ROOM)["id"], "still waiting to be read back")
        self.heard(entry, [self.CODEX], 30)                              # the asker's own session
        turn(self.conn, "Codex, go ahead.", seconds_ago=20)
        self.assertEqual([cb.ask_id(ROOM, entry, self.CODEX)], self.b().tick())

    def test_f4_the_read_back_key_lives_exactly_as_long_as_the_proposal_can_be_heard(self):
        """Codex's review of row 9443, F4: one number, READBACK_SECONDS from his ask, for the key and the close."""
        self.assertEqual(120, cb.READBACK_SECONDS)
        for ago, ttl in ((0, 120), (60, 60), (100, 20), (119, 1)):
            self.setUp()
            turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=ago)
            self.b().tick()
            self.assertEqual(ttl, self.conn.ttls[cb.READBACK_KEY % ROOM], ago)
        self.setUp()
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=100)
        self.b().tick()
        self.b(now=NOW + datetime.timedelta(seconds=19)).tick()          # 119 s: still open, key still there
        self.assertEqual("review", self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]["status"])
        self.assertNotEqual({}, self.conn.hgetall(cb.READBACK_KEY % ROOM))
        self.b(now=NOW + datetime.timedelta(seconds=21)).tick()          # 121 s: closed, and the key with it
        got = self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]
        self.assertTrue(got["title"].startswith("Not started, the read-back was not heard"))
        self.assertEqual({}, self.conn.hgetall(cb.READBACK_KEY % ROOM))

    def test_f4_a_recipient_change_rewrites_the_key_with_the_time_that_is_left(self):
        entry = turn(self.conn, "Grok, list the sample risks.", seconds_ago=90)
        self.b().tick()
        self.conn.xadd(self.side, {"by": "chair", "voice": "gemini", "room": ROOM, "text": "I can't open files.",
                                   "at": cb.stamp(NOW)}, entry_id=at(25))
        self.b().tick()
        self.assertEqual(cb.readback_id(ROOM, entry, ["grok", "gemini"]),
                         self.conn.hgetall(cb.READBACK_KEY % ROOM)["id"])
        self.assertEqual(30, self.conn.ttls[cb.READBACK_KEY % ROOM])


class ForgivingReadBack(unittest.TestCase):
    """The owner, 2026-11-03 21:25 ET, after two calls where none of his asks started: the go-ahead is forgiving.
    Five minutes, asks up to 120 words, and an unnamed "go ahead" confirms the one open proposal once it was read
    back, from the caller who asked. A bare yes still confirms nothing. Synthetic sentences only."""
    CODEX = "chatgpt-codex-desktop"

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self, now=NOW):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: now, readback=True)

    def inbox(self, agent):
        return self.conn.streams.get("fleet:inbox:%s" % agent, [])

    def cards(self, agent):
        return {k: json.loads(v) for k, v in self.conn.hgetall(cp.PARTITION % ("workshop", agent)).items()}

    def heard(self, entry, agents, seconds_ago):
        return self.conn.xadd(STREAM, {
            "kind": cb.READBACK_SPOKEN, "actor_id": "chair", "room_id": ROOM, "session_epoch": "fixture-epoch",
            "control_sequence": "4", "proj_n": "2", "readback_id": cb.readback_id(ROOM, entry, agents)},
            entry_id=at(seconds_ago))

    def test_the_defaults_are_five_minutes_and_120_words(self):
        self.assertEqual((300, 120), (cb.READBACK_SECONDS, cb.ASK_MAX_WORDS))

    def test_explicit_go_words_and_bare_yes(self):
        for said in ("go ahead", "Okay, go ahead.", "yes, go ahead please", "do it", "proceed", "go for it"):
            self.assertTrue(cb.EXPLICIT_GO.search(said), said)
        for said in ("Yes please.", "yes", "Okay.", "sounds good", "ahead of time", "undo it"):
            self.assertFalse(cb.EXPLICIT_GO.search(said), said)

    def test_an_unnamed_go_ahead_after_the_read_back_claims_the_open_proposal(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()
        self.heard(entry, [self.CODEX], 40)
        turn(self.conn, "Okay, go ahead.", reason="continuity", seconds_ago=20)
        self.assertEqual([cb.ask_id(ROOM, entry, self.CODEX)], self.b().tick())
        [(_, sent)] = self.inbox(self.CODEX)
        self.assertIn("draft the sample queue flow", sent["text"])

    def test_an_unnamed_go_ahead_confirms_every_voice_of_the_proposal(self):
        entry = turn(self.conn, "Grok and Gemini, compare the two sample plans.", seconds_ago=60)
        self.b().tick()
        self.heard(entry, ["grok", "gemini"], 40)
        turn(self.conn, "do it", reason="continuity", seconds_ago=20)
        # R3: both voices confirmed on one task are ONE team card, hosted by the first named (Codex's review of 464).
        self.assertEqual(1, len(self.b().tick()))
        self.assertEqual((1, 0), (len(self.inbox("grok")), len(self.inbox("gemini"))))

    def test_a_bare_yes_after_the_read_back_claims_nothing(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()
        self.heard(entry, [self.CODEX], 40)
        turn(self.conn, "Yes please.", reason="continuity", seconds_ago=20)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(self.CODEX))

    def test_an_unnamed_go_ahead_before_the_read_back_claims_nothing(self):
        turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()
        turn(self.conn, "go ahead", reason="continuity", seconds_ago=20)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(self.CODEX))

    def test_an_unnamed_go_ahead_from_another_caller_claims_nothing(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=60)
        self.b().tick()
        self.heard(entry, [self.CODEX], 40)
        turn(self.conn, "go ahead", reason="continuity", actor="someone_else", seconds_ago=20)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(self.CODEX))

    def test_a_go_ahead_four_minutes_after_the_read_back_still_starts_it(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=280)
        self.heard(entry, [self.CODEX], 260)
        turn(self.conn, "Codex, go ahead.", seconds_ago=20)            # 240 s after the read-back
        self.assertEqual([cb.ask_id(ROOM, entry, self.CODEX)], self.b().tick())

    def test_a_go_ahead_past_five_minutes_starts_nothing(self):
        entry = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=340)
        self.heard(entry, [self.CODEX], 330)
        turn(self.conn, "Codex, go ahead.", seconds_ago=20)            # 310 s after the read-back
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(self.CODEX))

    def test_a_hundred_word_ask_is_proposed_not_refused(self):
        said = "Codex, draft the sample queue flow " + " ".join(["with detail"] * 45) + "."
        self.assertTrue(40 < len(said.split()) <= cb.ASK_MAX_WORDS)
        entry = turn(self.conn, said, seconds_ago=60)
        self.b().tick()
        got = self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]
        self.assertEqual(("review", cb.PROPOSED), (got["status"], got["activity"]))

    def test_an_ask_over_120_words_is_still_refused(self):
        said = "Codex, draft the sample queue flow " + " ".join(["with detail"] * 60) + "."
        self.assertTrue(len(said.split()) > cb.ASK_MAX_WORDS)
        entry = turn(self.conn, said, seconds_ago=60)
        self.b().tick()
        got = self.cards(self.CODEX)[cb.proposal_id(ROOM, entry, self.CODEX)]
        self.assertTrue(got["title"].startswith(cb.TOO_LONG))


class DirectStart(unittest.TestCase):
    """The owner's decision, 2026-11-04 03:15Z: a named ask for work claims at once, with no read-back and no
    go-ahead. Synthetic sentences only."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def test_the_code_default_is_direct_start(self):
        self.assertFalse(cb.READBACK)
        self.assertFalse(cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW).readback)

    def test_a_named_ask_claims_at_once_with_no_proposal(self):
        entry = turn(self.conn, "Grok, draft a three-step onboarding plan.", seconds_ago=20)
        claimed = cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW).tick()
        self.assertEqual([cb.ask_id(ROOM, entry, "grok")], claimed)
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        [(_, sent)] = self.conn.streams.get("fleet:inbox:grok", [])
        self.assertIn("three-step onboarding plan", sent["text"])

    def test_two_named_asks_in_a_row_both_claim(self):
        first = turn(self.conn, "Claude, write a one-paragraph summary of the line.", seconds_ago=30)
        second = turn(self.conn, "Codex, draft a five-item kickoff checklist.", seconds_ago=20)
        claimed = cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW).tick()
        self.assertEqual({cb.ask_id(ROOM, first, "claude-code-cli"), cb.ask_id(ROOM, second, "chatgpt-codex-desktop")},
                         set(claimed))

    def test_a_remark_with_no_task_starts_nothing(self):
        turn(self.conn, "Grok, that sounds good to me.", seconds_ago=20)
        self.assertEqual([], cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW).tick())


class LivingCardRouting(unittest.TestCase):
    """R1 living cards (the owner, 2026-11-04): a task for an agent with a living card in this call updates that
    card (a revision entry for the chair's fleet work), never a new claim. Synthetic sentences only."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW)

    def living(self, agent="grok", said="Grok, write a joke on a card.", seconds_ago=60):
        """A claim whose worker put its card up (the lifecycle records the card's field)."""
        entry = turn(self.conn, said, seconds_ago=seconds_ago)
        [ask] = self.b().tick()
        info = json.loads(self.conn.hget(life.OPEN_KEY, ask))
        info["field"] = ask + "-work"
        self.conn.hset(life.OPEN_KEY, ask, json.dumps(info))
        return ask, entry

    def revisions(self, ask):
        return [json.loads(x) for x in getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % ask, [])]

    def test_a_named_change_goes_to_the_living_card_not_a_new_claim(self):
        ask, _ = self.living()
        entry = turn(self.conn, "Grok, modify the joke and make it funnier.", seconds_ago=10)
        self.assertEqual([], self.b().tick(), "no new claim")
        self.assertEqual([{"entry": entry}], self.revisions(ask))
        self.assertEqual([ask], list(self.conn.hgetall(life.OPEN_KEY)))

    def test_an_unnamed_follow_up_to_the_floor_holder_goes_to_its_living_card(self):
        ask, _ = self.living()
        turn(self.conn, None, reason="", actor="grok", seconds_ago=20, kind="floor.granted", turn_id="t-9")
        entry = turn(self.conn, "Make it even funnier.", reason="continuity", seconds_ago=10, turn_id="t-9")
        self.assertEqual([], self.b().tick())
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_asking_for_a_new_card_claims_a_new_one(self):
        ask, _ = self.living()
        turn(self.conn, "Grok, write another joke on a new card.", seconds_ago=10)
        self.assertEqual(1, len(self.b().tick()))
        self.assertEqual([], self.revisions(ask))

    def test_another_agent_without_a_living_card_is_claimed_as_usual(self):
        ask, _ = self.living()
        turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=10)
        self.assertEqual(1, len(self.b().tick()))
        self.assertEqual([], self.revisions(ask))

    def test_a_card_not_yet_put_up_is_not_living(self):
        turn(self.conn, "Grok, write a joke on a card.", seconds_ago=60)
        [first] = self.b().tick()
        turn(self.conn, "Grok, make it funnier.", seconds_ago=10)
        self.assertEqual(1, len(self.b().tick()), "no card yet: a new claim")
        self.assertEqual([], self.revisions(first))

    def test_named_final_makes_that_agents_living_card_final(self):
        ask, _ = self.living()
        final = turn(self.conn, "Grok, that's final.", seconds_ago=10)
        self.assertEqual([], self.b().tick())
        self.assertEqual([{"final": True, "entry": final}], self.revisions(ask))

    def test_unnamed_final_makes_the_rooms_newest_living_card_final(self):
        older, _ = self.living(seconds_ago=90)
        newer, _ = self.living(agent="chatgpt-codex-desktop", said="Codex, draft a kickoff checklist.",
                               seconds_ago=60)
        final = turn(self.conn, "Okay, lock it in.", reason="continuity", seconds_ago=10)
        self.b().tick()
        self.assertEqual([{"final": True, "entry": final}], self.revisions(newer))
        self.assertEqual([], self.revisions(older))

    def test_everyone_revises_every_living_card(self):
        mine, _ = self.living()
        entry = turn(self.conn, "All of you, make it shorter.", reason="everyone", seconds_ago=10)
        claimed = self.b().tick()
        self.assertEqual([{"entry": entry}], self.revisions(mine))
        # R3: a change is not new work; no new card is made for the voices without one.
        self.assertEqual(0, len(claimed), "no new cards for a change")

    def test_a_comparative_without_a_verb_revises_the_living_card(self):
        """Grok's live test, 2026-11-04 step 3: "Grok, shorter please" was acknowledged but changed nothing."""
        for said in ("Grok, shorter please.", "Grok, a bit bolder.", "Grok, more colorful please."):
            with self.subTest(said=said):
                self.setUp()
                ask, _ = self.living()
                entry = turn(self.conn, said, seconds_ago=10)
                self.assertEqual([], self.b().tick(), "no new claim")
                self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_an_unnamed_comparative_to_the_floor_holder_revises_its_card(self):
        ask, _ = self.living()
        turn(self.conn, None, reason="", actor="grok", seconds_ago=20, kind="floor.granted", turn_id="t-9")
        entry = turn(self.conn, "Shorter, please.", reason="continuity", seconds_ago=10, turn_id="t-9")
        self.assertEqual([], self.b().tick())
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_a_comparative_never_claims_a_new_card(self):
        turn(self.conn, "Grok, shorter please.", seconds_ago=10)
        self.assertEqual([], self.b().tick(), "nothing living to change: no claim")
        turn(self.conn, "Codex, funnier.", seconds_ago=5)
        self.assertEqual([], self.b().tick())

    def test_praise_is_not_a_change(self):
        for said in ("Grok, that's funnier.", "Grok, it's much shorter now, thanks.", "Grok, this looks bolder."):
            with self.subTest(said=said):
                self.setUp()
                ask, _ = self.living()
                turn(self.conn, said, seconds_ago=10)
                self.assertEqual([], self.b().tick())
                self.assertEqual([], self.revisions(ask))

    def test_a_negated_or_descriptive_comparative_is_not_a_change(self):
        """Codex's HOLD on a578301: "Grok, no need for anything shorter" reached the living card."""
        for said in ("Grok, no need for anything shorter.", "Grok, nothing longer please.",
                     "Grok, I don't want it bolder.", "Grok, keep it, not funnier.",
                     "Grok, shorter is unnecessary.", "Grok, bolder would be too much.", "Grok, funnier isn't it."):
            with self.subTest(said=said):
                self.setUp()
                ask, _ = self.living()
                turn(self.conn, said, seconds_ago=10)
                self.assertEqual([], self.b().tick())
                self.assertEqual([], self.revisions(ask))

    def test_fillers_and_clauses_before_a_comparative_still_ask(self):
        for said in ("Okay Grok, a bit bolder.", "Grok, that's fine, punchier please.", "Grok, just a little shorter.",
                     "Grok, shorter please.", "Grok, funnier than the last one, thanks.", "Grok, a shorter joke please."):
            with self.subTest(said=said):
                self.setUp()
                ask, _ = self.living()
                entry = turn(self.conn, said, seconds_ago=10)
                self.b().tick()
                self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_the_change_words(self):
        for said in ("shorter please", "a little punchier", "less wordy", "make it longer"):
            self.assertTrue(cb.work_said(said), said)
        for said in ("tell me more", "that's better", "it's shorter now", "how long is it", ""):
            self.assertFalse(cb.change_ask(said), said)
        self.assertTrue(cb.change_only("shorter please"))
        self.assertFalse(cb.change_only("make it shorter"), "a task word: the usual rules")

    def make_project(self, pid, name, status="active"):
        self.conn.hset("conf:projects:%s" % cb.CANVAS_ROOM, pid, json.dumps({"name": name, "status": status}))

    def owner_bridge(self):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW, owners=["own_fixture_owner"])

    def test_a_project_switch_makes_it_current_and_claims_nothing(self):
        """R6a: "let's work on project Acme" holds TASK's "work on"; it must switch, never claim a card."""
        self.make_project("acme", "Acme")
        turn(self.conn, "Grok, let's work on project Acme.", seconds_ago=10)
        self.assertEqual([], self.owner_bridge().tick())
        self.assertEqual("acme", cb._text(self.conn.get("conf:project:%s:current" % cb.CANVAS_ROOM)))

    def test_only_a_listed_owner_switches_the_project(self):
        """Codex's HOLD on PR 467: a guest's switch changes neither the selector nor the published snapshot."""
        self.make_project("acme", "Acme")
        turn(self.conn, "Switch to the Acme project.", actor="guest_fixture", seconds_ago=10)
        self.owner_bridge().tick()
        self.assertIsNone(self.conn.get("conf:project:%s:current" % cb.CANVAS_ROOM))
        self.assertNotIn("project", cb.canvas_publish.snapshot(self.conn, cb.CANVAS_ROOM, NOW))

    def test_with_no_owner_listed_nobody_switches(self):
        self.make_project("acme", "Acme")
        with mock.patch.dict(os.environ, {cb.OWNERS_ENV: ""}):
            bridge = cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW)
        turn(self.conn, "Switch to the Acme project.", seconds_ago=10)
        bridge.tick()
        self.assertIsNone(self.conn.get("conf:project:%s:current" % cb.CANVAS_ROOM))
        with mock.patch.dict(os.environ, {cb.OWNERS_ENV: "someone, own_fixture_owner"}):
            self.assertIn("own_fixture_owner", cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW).owners)

    def test_no_matching_project_lets_the_turn_go_on_as_before(self):
        turn(self.conn, "Grok, let's work on project Nova.", seconds_ago=10)
        self.assertEqual(1, len(self.b().tick()), "no such project: an ordinary task turn")
        self.assertIsNone(self.conn.get("conf:project:%s:current" % cb.CANVAS_ROOM))

    def test_the_bridges_own_claimed_card_shows_under_the_current_project(self):
        """Codex's HOLD on c7da999: the claimed card carries no tag and was hidden while a project was current."""
        self.make_project("acme", "Acme")
        self.conn.set("conf:project:%s:current" % cb.CANVAS_ROOM, "acme")
        turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=10)
        [ask] = self.b().tick()
        doc = cb.canvas_publish.snapshot(self.conn, cb.CANVAS_ROOM, NOW)
        self.assertIn("chatgpt-codex-desktop/" + ask, [i["id"] for i in doc["items"]])

    def test_a_stamp_that_fails_delivers_nothing_untagged(self):
        """The project is recorded BEFORE the claim: if recording fails, no claim (and no inbox ask) is made."""
        self.make_project("acme", "Acme")
        self.conn.set("conf:project:%s:current" % cb.CANVAS_ROOM, "acme")
        turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=10)
        with mock.patch.object(cb.projects, "stamp", side_effect=ConnectionError("redis")):
            with self.assertRaises(ConnectionError):
                self.b().tick()
        self.assertEqual({}, dict(self.conn.hgetall(cb.call_lifecycle.OPEN_KEY) or {}), "nothing claimed")

    def test_a_duplicate_claim_after_a_switch_keeps_the_asks_first_project(self):
        """Codex's HOLD on d519e34: replaying an Acme ask after switching to Beta must not move it to Beta."""
        self.make_project("acme", "Acme")
        self.make_project("beta", "Beta")
        self.conn.set("conf:project:%s:current" % cb.CANVAS_ROOM, "acme")
        entry = turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=10)
        [ask] = self.b().tick()
        self.conn.set("conf:project:%s:current" % cb.CANVAS_ROOM, "beta")
        self.assertIsNone(self.b()._claim(ROOM, entry, "chatgpt-codex-desktop", "Codex, draft a kickoff checklist.", NOW),
                          "the duplicate is refused")
        self.assertEqual("acme", cb._text(self.conn.get("gov:callbridge:project:%s" % ask)))

    def test_a_finished_card_stays_in_its_project_long_after_max_age(self):
        """Codex's HOLD on 6b8750a: a DONE card stays visible past MAX_AGE; its project binding must too."""
        self.make_project("acme", "Acme")
        self.make_project("beta", "Beta")
        self.conn.set("conf:project:%s:current" % cb.CANVAS_ROOM, "acme")
        turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=10)
        [ask] = self.b().tick()
        agent = "chatgpt-codex-desktop"
        self.assertTrue(put_card(self.conn, agent, ask))
        self.assertTrue(complete(self.conn, agent, ask))
        self.assertNotIn("gov:callbridge:project:%s" % ask, {k: v for k, v in self.conn.ttls.items() if v},
                         "the binding has no expiry")
        later = NOW + datetime.timedelta(seconds=cb.call_lifecycle.MAX_AGE + 3600)
        self.conn.set("conf:project:%s:current" % cb.CANVAS_ROOM, "beta")
        self.assertNotIn(agent + "/" + ask + "-work",
                         [i["id"] for i in cb.canvas_publish.snapshot(self.conn, cb.CANVAS_ROOM, later)["items"]])
        self.conn.set("conf:project:%s:current" % cb.CANVAS_ROOM, "acme")
        shown = [i["id"] for i in cb.canvas_publish.snapshot(self.conn, cb.CANVAS_ROOM, later)["items"]]
        self.assertTrue(any(i.startswith(agent + "/" + ask) for i in shown), shown)

    def test_a_negated_switch_switches_nothing(self):
        self.make_project("acme", "Acme")
        turn(self.conn, "Don't switch to the Acme project.", seconds_ago=10)
        self.owner_bridge().tick()
        self.assertIsNone(self.conn.get("conf:project:%s:current" % cb.CANVAS_ROOM))

    def test_a_claim_carries_the_current_project_and_none_without_one(self):
        turn(self.conn, "Grok, write a joke on a card.", seconds_ago=30)
        [plain] = self.b().tick()
        self.assertEqual("", cb._text(self.conn.get("gov:callbridge:project:%s" % plain)), "no project, recorded")
        self.make_project("acme", "Acme")
        self.conn.set("conf:project:%s:current" % cb.CANVAS_ROOM, "acme")
        turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=10)
        [tagged] = self.b().tick()
        self.assertEqual("acme", cb._text(self.conn.get("gov:callbridge:project:%s" % tagged)))

    def test_the_revision_list_expires_with_the_ask(self):
        ask, _ = self.living()
        turn(self.conn, "Grok, make it funnier.", seconds_ago=10)
        self.b().tick()
        self.assertEqual(life.MAX_AGE, self.conn.ttls[cb.REVISE_KEY % ask])


def command(conn, action, actor="own_fixture_owner", seconds_ago=10, reason="", **extra):
    """The owner console's project.command entry on the chair's stream (R6b; the schema is the conference repo's).
    An inert fixture: the actor tag stands for the gateway-verified identity the chair stamps, not a real check."""
    fields = dict(action=action, command_id="cmd-%d" % seconds_ago, room=ROOM)
    fields.update(extra)
    utterance = fields.pop("utterance", None)          # none in the real schema; a test may smuggle one in
    return turn(conn, utterance, reason=reason, actor=actor, seconds_ago=seconds_ago, kind=cb.PROJECT_COMMAND, **fields)


def moving_lease(conn):
    """A holds() that answers yes while gov:worker:lock quietly moves to another worker: true at every check, but
    the WATCHed key has changed by the EXEC, so a fenced transaction writes nothing."""
    asked = []

    def holds():
        asked.append(1)
        if len(asked) >= 2:                    # past tick()'s own gate: the lock moves under this worker's feet
            conn.set(cp.LEASE_KEY, "another-worker-%d" % len(asked))
        return True
    return holds


class ProjectCommands(unittest.TestCase):
    """R6b (CARDS-ARCHITECTURE R6.2): new projects come from the owner console only, as project.command entries the
    chair writes; the bridge obeys a listed owner's create, switch, archive and clear, and nothing else. Synthetic."""
    CODEX = "chatgpt-codex-desktop"
    CURRENT = "conf:project:%s:current" % cb.CANVAS_ROOM

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self, owners=("own_fixture_owner",), holds=True):
        return cb.Bridge(self.conn, LOG, lambda: holds, clock=lambda: NOW, owners=list(owners))

    def projects(self):
        return cb.projects.read_projects(self.conn, cb.CANVAS_ROOM)

    def current(self):
        return cb._text(self.conn.get(self.CURRENT))

    def snapshot(self):
        return cb.canvas_publish.snapshot(self.conn, cb.CANVAS_ROOM, NOW)

    def owed(self):
        return sorted(k for k in self.conn.store if k.startswith(cp.ARCHIVE_PENDING_KEY % cb.CANVAS_ROOM + ":"))

    def test_an_owners_create_makes_an_active_current_project_and_the_next_claim_carries_it(self):
        command(self.conn, "create", project_name="Acme Rollout", project_id="acme-rollout", seconds_ago=20)
        turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=10)
        [ask] = self.b().tick()
        self.assertEqual({"acme-rollout": {"name": "Acme Rollout", "status": "active",
                                           "created": "2026-10-24T14:02:00Z"}}, self.projects())
        self.assertEqual("acme-rollout", self.current())
        self.assertEqual("acme-rollout", cb._text(self.conn.get("gov:callbridge:project:%s" % ask)))
        doc = self.snapshot()
        self.assertEqual({"id": "acme-rollout", "name": "Acme Rollout"}, doc["project"])
        self.assertIn(self.CODEX + "/" + ask, [i["id"] for i in doc["items"]])

    def test_the_id_is_the_slug_of_the_name_whatever_id_the_console_sent(self):
        command(self.conn, "create", project_name="Acme Rollout", project_id="something-else", seconds_ago=20)
        self.assertEqual([], self.b().tick())
        self.assertEqual(["acme-rollout"], list(self.projects()))
        self.assertEqual("acme-rollout", self.current())
        command(self.conn, "create", project_name="Beta", seconds_ago=10)
        self.b().tick()
        self.assertEqual("beta", self.current(), "no id sent at all: still slug(name)")

    def test_a_command_from_no_listed_owner_changes_nothing(self):
        cb.projects.create(self.conn, cb.CANVAS_ROOM, "Acme", NOW)
        cb.projects.create(self.conn, cb.CANVAS_ROOM, "Beta", NOW)
        cb.projects.set_current(self.conn, cb.CANVAS_ROOM, "acme")
        before = (self.projects(), self.current())
        for actor in ("guest_fixture", "chair", "", None):
            command(self.conn, "create", actor=actor, project_name="Gamma", project_id="gamma", seconds_ago=50)
            command(self.conn, "switch", actor=actor, project_id="beta", seconds_ago=40)
            command(self.conn, "archive", actor=actor, project_id="acme", seconds_ago=30)
            command(self.conn, "clear", actor=actor, seconds_ago=20)
            self.assertEqual([], self.b().tick())
            self.assertEqual(before, (self.projects(), self.current()), repr(actor))
            self.assertEqual("acme", self.snapshot()["project"]["id"])
            self.conn.streams.pop(STREAM, None)
            self.conn.delete(cb.CURSOR_KEY % ROOM)
        with mock.patch.dict(os.environ, {cb.OWNERS_ENV: ""}):
            nobody = cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW)
        command(self.conn, "clear", seconds_ago=10)
        nobody.tick()
        self.assertEqual("acme", self.current(), "no owner listed: nobody commands")

    def test_a_duplicate_create_is_refused_and_switches_nothing(self):
        cb.projects.create(self.conn, cb.CANVAS_ROOM, "Acme", NOW)
        self.conn.hset("conf:projects:%s" % cb.CANVAS_ROOM, "old",
                       json.dumps({"name": "Old", "status": "archived", "created": "2026-10-01T00:00:00Z"}))
        command(self.conn, "create", project_name="ACME", project_id="acme", seconds_ago=30)
        command(self.conn, "create", project_name="Old", project_id="old", seconds_ago=20)
        self.b().tick()
        self.assertEqual({"acme": "Acme", "old": "Old"}, {k: v["name"] for k, v in self.projects().items()},
                         "neither renamed")
        self.assertEqual("archived", self.projects()["old"]["status"], "not revived")
        self.assertIsNone(self.current(), "a refused create makes nothing current")

    def test_an_invalid_name_is_refused(self):
        for bad in ("", "<script>", "x" * 41, "-first", "two|parts"):
            command(self.conn, "create", project_name=bad, project_id="x", seconds_ago=10)
            self.b().tick()
            self.assertEqual({}, self.projects(), repr(bad))
            self.assertIsNone(self.current())
            self.conn.streams.pop(STREAM, None)
            self.conn.delete(cb.CURSOR_KEY % ROOM)
        command(self.conn, "create", project_id="acme", seconds_ago=10)
        self.b().tick()
        self.assertEqual({}, self.projects(), "no name at all")

    def test_switch_takes_an_active_project_only(self):
        cb.projects.create(self.conn, cb.CANVAS_ROOM, "Acme", NOW)
        cb.projects.create(self.conn, cb.CANVAS_ROOM, "Beta", NOW)
        cb.projects.archive(self.conn, cb.CANVAS_ROOM, "beta")
        command(self.conn, "switch", project_id="acme", seconds_ago=40)
        self.b().tick()
        self.assertEqual("acme", self.current())
        for pid in ("beta", "nope", "Bad Id", ""):
            command(self.conn, "switch", project_id=pid, seconds_ago=30)
            self.b().tick()
            self.assertEqual("acme", self.current(), repr(pid))
            self.conn.streams.pop(STREAM, None)
            self.conn.delete(cb.CURSOR_KEY % ROOM)

    def test_archiving_the_current_project_clears_it_and_the_canvas_shows_untagged_cards_only(self):
        turn(self.conn, "Grok, write a joke on a card.", seconds_ago=60)
        [plain] = self.b().tick()
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=50)
        turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=40)
        [tagged] = self.b().tick()
        self.assertEqual([self.CODEX + "/" + tagged], [i["id"] for i in self.snapshot()["items"]])
        command(self.conn, "archive", project_id="acme", seconds_ago=30)
        self.assertEqual([], self.b().tick())
        self.assertEqual("archived", self.projects()["acme"]["status"])
        self.assertIsNone(self.current())
        doc = self.snapshot()
        self.assertNotIn("project", doc)
        self.assertEqual(["grok/" + plain], [i["id"] for i in doc["items"]], "untagged cards only")
        self.assertEqual("acme", cb._text(self.conn.get("gov:callbridge:project:%s" % tagged)), "its stamp stays")

    def test_archiving_owes_the_projects_private_archive_which_the_publisher_then_writes(self):
        """R6.1: the call's end archives ACTIVE projects only, so a project archived mid-call is archived now."""
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=50)
        turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=40)
        [ask] = self.b().tick()
        command(self.conn, "archive", project_id="acme", seconds_ago=30)
        self.b().tick()
        [key] = self.owed()
        self.assertTrue(key.endswith("-acme"), key)
        owed = json.loads(self.conn.get(key))
        [(name, body)] = list(owed["files"].items())
        self.assertTrue(name.startswith("projects/acme/"), name)
        self.assertEqual([self.CODEX + "/" + ask], [i["id"] for i in json.loads(body)["items"]])
        self.assertEqual([], cp.archive_docs(self.conn, cb.CANVAS_ROOM, NOW), "archived: the call's end owes nothing")
        sent = []
        pub = cp.Publisher(self.conn, "sfdc24-canvas", lambda b, n, d, allow=None: sent.append((b, n, d)), LOG,
                           clock=lambda: NOW, holds=lambda: True)
        self.assertEqual(1, pub.archive(cb.CANVAS_ROOM, NOW))
        self.assertEqual([(cp.ARCHIVE_BUCKET, name, body.encode("utf-8"))], sent, "to the private bucket, as frozen")
        self.assertEqual([], self.owed())

    def test_an_archive_that_cannot_be_owed_leaves_the_project_active_and_is_retried(self):
        """Codex's HOLD on PR 470: the fenced owe fails (the lease moved mid-write): the project stays active (and
        current), and the command is NOT consumed, so the next round retries it until the archive is owed."""
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=50)
        turn(self.conn, "Codex, draft a kickoff checklist.", seconds_ago=40)
        self.b().tick()
        command(self.conn, "archive", project_id="acme", seconds_ago=30)
        with mock.patch.object(cp, "_fenced", lambda *a, **k: None):
            self.b().tick()
        self.assertEqual("active", self.projects()["acme"]["status"])
        self.assertEqual("acme", self.current())
        self.assertEqual([], self.owed())
        self.assertNotEqual(at(30), cb._text(self.conn.get(cb.CURSOR_KEY % ROOM)), "not consumed: kept for a retry")
        self.b().tick()
        self.assertEqual("archived", self.projects()["acme"]["status"], "the retry owes the archive and retires it")
        self.assertEqual(1, len(self.owed()))
        self.assertEqual(at(30), cb._text(self.conn.get(cb.CURSOR_KEY % ROOM)), "now consumed")

    def test_a_command_read_without_the_lease_is_left_for_the_next_holder(self):
        """Codex's HOLD on PR 470: a former worker must neither write project state nor consume the event."""
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=20)
        flips = iter([True])             # tick()'s own gate holds; gone by the command's own check
        former = cb.Bridge(self.conn, LOG, lambda: next(flips, False), clock=lambda: NOW,
                           owners=["own_fixture_owner"])
        self.assertEqual([], former.tick())
        self.assertEqual({}, self.projects(), "nothing written without the lease")
        self.assertIsNone(self.conn.get(cb.CURSOR_KEY % ROOM), "not consumed")
        self.b().tick()
        self.assertEqual("acme", self.current(), "the next lease holder obeys it")

    def test_a_lease_that_moves_during_the_writes_changes_and_consumes_nothing(self):
        """Codex's second HOLD on PR 470: holds() said yes, then the lock moved before the writes landed. The
        project writes and the cursor advance are one transaction WATCHing gov:worker:lock, so the EXEC fails,
        nothing is written, and the next holder processes the command, once."""
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=20)
        former = cb.Bridge(self.conn, LOG, moving_lease(self.conn), clock=lambda: NOW,
                           owners=["own_fixture_owner"])
        self.assertEqual([], former.tick())
        self.assertEqual({}, self.projects(), "nothing written by a worker whose lease moved")
        self.assertIsNone(self.current())
        self.assertIsNone(self.conn.get(cb.CURSOR_KEY % ROOM), "not consumed")
        self.b().tick()
        self.assertEqual(["acme"], list(self.projects()), "the next lease holder processes it, once")
        self.assertEqual("acme", self.current())

    def test_a_voice_switch_is_fenced_on_the_lease_too(self):
        """The same race on the R6a path: the lock moves between the owner's words and the write."""
        cb.projects.create(self.conn, cb.CANVAS_ROOM, "Acme", NOW)
        turn(self.conn, "Switch to the Acme project.", seconds_ago=10)
        former = cb.Bridge(self.conn, LOG, moving_lease(self.conn), clock=lambda: NOW,
                           owners=["own_fixture_owner"])
        self.assertEqual([], former.tick())
        self.assertIsNone(self.current(), "nothing switched by a worker whose lease moved")
        self.assertIsNone(self.conn.get(cb.CURSOR_KEY % ROOM), "not consumed")
        self.b().tick()
        self.assertEqual("acme", self.current(), "the next lease holder switches it")

    def test_a_switch_takes_his_revisions_off_another_projects_living_card(self):
        """Codex's HOLD on PR 470: living-card lookups were scoped by room, actor and session but not project, so
        after a switch his words could revise an OPEN card from another project, one the canvas no longer shows."""
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=90)
        turn(self.conn, "Grok, write a joke on a card.", seconds_ago=80)
        [ask] = self.b().tick()
        info = json.loads(self.conn.hget(life.OPEN_KEY, ask))
        info["field"] = ask + "-work"
        self.conn.hset(life.OPEN_KEY, ask, json.dumps(info))        # the worker put its card up: living
        command(self.conn, "create", project_name="Beta", project_id="beta", seconds_ago=30)
        turn(self.conn, "Grok, make it funnier.", seconds_ago=10)
        self.assertEqual(1, len(self.b().tick()), "in Beta that is a new ask, not a revision of Acme's card")
        self.assertEqual([], getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % ask, []))
        command(self.conn, "switch", project_id="acme", seconds_ago=5)
        turn(self.conn, "Grok, make the joke funnier.", seconds_ago=1)
        self.assertEqual([], self.b().tick())
        self.assertEqual(1, len(getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % ask, [])),
                         "back in Acme, the same words revise its own living card again")

    def test_archiving_a_project_without_cards_needs_no_archive(self):
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=40)
        command(self.conn, "archive", project_id="acme", seconds_ago=30)
        self.b().tick()
        self.assertEqual("archived", self.projects()["acme"]["status"])
        self.assertEqual([], self.owed())
        command(self.conn, "archive", project_id="acme", seconds_ago=20)
        command(self.conn, "archive", project_id="nope", seconds_ago=10)
        self.b().tick()
        self.assertEqual("archived", self.projects()["acme"]["status"], "again, or unknown: refused, nothing raised")

    def test_clear_with_no_project_is_a_no_op_and_with_one_clears_it(self):
        command(self.conn, "clear", seconds_ago=40)
        self.assertEqual([], self.b().tick())
        self.assertIsNone(self.current())
        self.assertEqual({}, self.projects())
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=30)
        command(self.conn, "clear", project_id="", seconds_ago=20)
        self.b().tick()
        self.assertIsNone(self.current())
        self.assertEqual("active", self.projects()["acme"]["status"], "cleared, not archived")
        self.assertNotIn("project", self.snapshot())

    def test_a_project_command_is_never_an_ask_a_go_ahead_a_follow_up_or_a_cancellation(self):
        smuggled = {"kind": cb.PROJECT_COMMAND, "action": "clear", "actor_id": "own_fixture_owner",
                    "reason": "vocative", "utterance": "Codex, draft a kickoff checklist.", "session_epoch": "e"}
        self.assertEqual([], cb.asks_in(smuggled))
        self.assertFalse(cb.task_ask(smuggled))
        self.assertFalse(cb.follow_up(dict(smuggled, reason="continuity", utterance="Draft the checklist.")))
        self.assertFalse(cb.go_ahead(dict(smuggled, utterance="Codex, go ahead.")))
        self.assertFalse(cb.cancellation(dict(smuggled, utterance="Cancel that.")))
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=20, reason="vocative",
                utterance="Codex, draft a kickoff checklist.")
        self.assertEqual([], self.b().tick(), "nothing claimed")
        self.assertEqual({}, dict(self.conn.hgetall(life.OPEN_KEY) or {}))
        self.assertIsNone(self.conn.get(cb.PENDING_KEY % ROOM), "not a pending ask either")
        self.assertEqual("acme", self.current(), "the command itself was obeyed")

    def test_an_unknown_action_changes_nothing_and_is_consumed(self):
        cb.projects.create(self.conn, cb.CANVAS_ROOM, "Acme", NOW)
        cb.projects.set_current(self.conn, cb.CANVAS_ROOM, "acme")
        entry = command(self.conn, "delete", project_id="acme", seconds_ago=10)
        self.b().tick()
        self.assertEqual("active", self.projects()["acme"]["status"])
        self.assertEqual("acme", self.current())
        self.assertEqual(entry, cb._text(self.conn.get(cb.CURSOR_KEY % ROOM)))

    def test_a_command_is_obeyed_once(self):
        command(self.conn, "create", project_name="Acme", project_id="acme", seconds_ago=20)
        bridge = self.b()
        bridge.tick()
        cb.projects.clear_current(self.conn, cb.CANVAS_ROOM)
        bridge.tick()
        self.assertIsNone(self.current(), "the cursor passed it: not replayed")


class RenamedVoices(unittest.TestCase):
    """The owner, 2026-11-04 ~19:15Z: Grok is now Greg and Codex is Cody. Either name claims for the same agent, and the
    bridge's own cards say the new names. Synthetic sentences only."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def test_the_new_and_the_old_name_claim_for_the_same_agent(self):
        for said, agent in (("Greg, write a joke about admins.", "grok"), ("Grok, write a joke about admins.", "grok"),
                            ("Cody, draft a kickoff checklist.", "chatgpt-codex-desktop"),
                            ("Codex, draft a kickoff checklist.", "chatgpt-codex-desktop")):
            with self.subTest(said=said):
                self.setUp()
                turn(self.conn, said, seconds_ago=10)
                [ask] = cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW).tick()
                self.assertEqual(agent, life.get(self.conn, ask)["agent"])

    def test_a_possessive_of_the_new_name_names_nobody(self):
        self.assertEqual([], cb.named_in("I like Greg's logo."))
        self.assertEqual(["grok", "chatgpt-codex-desktop"], cb.named_in("Greg and Cody, compare them."))


class CardNameRouting(unittest.TestCase):
    """R2b, conference docs/CARDS-ARCHITECTURE.md decision c: his words that say a living card's name go to that
    card, whichever voice made it, unless he names another voice. Synthetic sentences only."""
    CODEX = "chatgpt-codex-desktop"

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW)

    def living(self, agent, said, name, title, seconds_ago):
        """A claim whose worker put up its named card."""
        turn(self.conn, said, seconds_ago=seconds_ago)
        [ask] = self.b().tick()
        info = json.loads(self.conn.hget(life.OPEN_KEY, ask))
        info["field"] = ask + "-work"
        self.conn.hset(life.OPEN_KEY, ask, json.dumps(info))
        self.conn.hset("conf:canvas:%s:%s" % (info["room"], agent), ask + "-work",
                       json.dumps({"type": "note", "title": title, "body": "x", "name": name}))
        return ask

    def revisions(self, ask):
        return [json.loads(x) for x in getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % ask, [])]

    def two_cards(self):
        logo = self.living("grok", "Grok, design a logo for SFDC24.", "SFDC24 logo", "SFDC24 logo concepts", 90)
        plan = self.living(self.CODEX, "Codex, draft an onboarding plan.", "Onboarding plan",
                           "Onboarding plan, three steps", 60)
        return logo, plan

    def test_the_names_he_says(self):
        self.assertEqual(2, cb.card_named("Make the onboarding plan shorter.", "Onboarding plan"))
        self.assertEqual(1, cb.card_named("Make the logo card bolder.", "SFDC24 logo"))
        self.assertEqual(1, cb.card_named("Make Grok's logo card bolder.", "SFDC24 logo"))
        self.assertEqual(1, cb.card_named("Update the logos card.", "Brand mark", "SFDC24 logo concepts"))
        self.assertEqual(0, cb.card_named("Make the joke card funnier.", "SFDC24 logo"))
        self.assertEqual(0, cb.card_named("Make that card bolder.", "SFDC24 logo"), "no name said")
        self.assertEqual(0, cb.card_named("Make an onboarding plan.", "Onboarding plan"), "a new one, not the one")
        self.assertEqual(0, cb.card_named("Make the card bolder.", "card 2"))
        self.assertEqual(0, cb.card_named("Make the logo card bolder.", "<b>logo</b>"), "a name the publisher refuses")

    def test_an_unnamed_change_goes_to_the_named_card_not_the_newest(self):
        logo, plan = self.two_cards()
        entry = turn(self.conn, "Make the logo card bolder.", reason="", seconds_ago=10)
        self.assertEqual([], self.b().tick(), "no new claim")
        self.assertEqual([{"entry": entry}], self.revisions(logo))
        self.assertEqual([], self.revisions(plan))

    def test_the_named_card_beats_the_floor_holder(self):
        logo, plan = self.two_cards()
        turn(self.conn, None, reason="", actor=self.CODEX, seconds_ago=20, kind="floor.granted", turn_id="t-9")
        entry = turn(self.conn, "Now make the logo card bolder.", reason="continuity", seconds_ago=10, turn_id="t-9")
        self.assertEqual([], self.b().tick())
        self.assertEqual([{"entry": entry}], self.revisions(logo), "the logo card's own voice")
        self.assertEqual([], self.revisions(plan))

    def test_the_whole_name_without_the_word_card(self):
        logo, plan = self.two_cards()
        entry = turn(self.conn, "Grok, shorten the onboarding plan.", seconds_ago=10)
        self.b().tick()
        self.assertEqual([], self.revisions(logo), "he named Grok but the plan is Codex's: Grok makes a new card")
        self.assertEqual([], self.revisions(plan))
        entry = turn(self.conn, "Please shorten the onboarding plan.", reason="", seconds_ago=5)
        self.b().tick()
        self.assertEqual([{"entry": entry}], self.revisions(plan))

    def test_a_named_final_freezes_that_card(self):
        logo, plan = self.two_cards()
        final = turn(self.conn, "The logo card is final.", reason="", seconds_ago=10)
        self.assertEqual([], self.b().tick())
        self.assertEqual([{"final": True, "entry": final}], self.revisions(logo))
        self.assertEqual([], self.revisions(plan), "not the newest card")

    def test_a_name_from_another_caller_routes_nothing(self):
        logo, _plan = self.two_cards()
        turn(self.conn, "Make the logo card bolder.", reason="", actor="someone_else", seconds_ago=10)
        self.b().tick()
        self.assertEqual([], self.revisions(logo))

    def test_the_fullest_name_wins_over_a_newer_shorter_one(self):
        # Codex's review of PR 463: "Launch logo" (older) and "Launch" (newer) both match "the launch logo".
        logo = self.living("grok", "Grok, design a launch logo.", "Launch logo", "Launch logo", 90)
        launch = self.living(self.CODEX, "Codex, draft a launch plan.", "Launch", "Launch", 60)
        entry = turn(self.conn, "Make the launch logo shorter.", reason="", seconds_ago=10)
        self.b().tick()
        self.assertEqual([{"entry": entry}], self.revisions(logo))
        self.assertEqual([], self.revisions(launch))

    def test_a_new_card_is_still_a_new_card(self):
        logo, _plan = self.two_cards()
        turn(self.conn, "Grok, put the logo card ideas on a new card.", seconds_ago=10)
        self.assertEqual(1, len(self.b().tick()))
        self.assertEqual([], self.revisions(logo))


class TeamCardRouting(unittest.TestCase):
    """R3, conference docs/CARDS-ARCHITECTURE.md "Team cards (R3)": a team ask is ONE claim for its host; later team
    words and picks go to the living team card. Synthetic sentences only."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW)

    def inboxes(self):
        return sorted(k.split(":")[-1] for k in self.conn.streams if k.startswith("fleet:inbox:"))

    def revisions(self, ask):
        return [json.loads(x) for x in getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % ask, [])]

    def team_card(self, said="Everyone, design a logo for SFDC24.", host="gemini", seconds_ago=90):
        """A team ask whose round put its board up (Plan first, then the voices' tiles)."""
        turn(self.conn, said, reason="everyone", seconds_ago=seconds_ago)
        [ask] = self.b().tick()
        info = json.loads(self.conn.hget(life.OPEN_KEY, ask))
        info["field"] = ask + "-work"
        self.conn.hset(life.OPEN_KEY, ask, json.dumps(info))
        board = {"layout": "grid", "chosen": None, "blocks": [
            {"kind": "text", "by": host, "title": "Plan", "body": "Options."},
            {"kind": "text", "by": "grok", "title": "Grok mark", "body": "x"}]}
        self.conn.hset("conf:canvas:%s:%s" % (info["room"], host), ask + "-work",
                       json.dumps({"type": "board", "title": "Team options: Design a logo", "name": "logo",
                                   "body": json.dumps(board)}))
        return ask

    def test_picks_that_point_at_a_tile(self):
        self.assertTrue(cb.picked("I like Grok's."))
        self.assertTrue(cb.picked("Let's take the second one."))
        self.assertTrue(cb.picked("Go with Codex."))
        self.assertFalse(cb.picked("I like that idea."))
        self.assertFalse(cb.picked("Grok's logo is nice."), "no pick word")

    def test_everyone_with_new_work_is_one_claim_for_gemini(self):
        turn(self.conn, "Everyone, design a logo for SFDC24.", reason="everyone")
        self.assertEqual(1, len(self.b().tick()))
        self.assertEqual(["gemini"], self.inboxes())

    def test_two_named_voices_are_one_claim_for_the_first(self):
        turn(self.conn, "Codex and Grok, draft a launch plan.")
        self.assertEqual(1, len(self.b().tick()))
        self.assertEqual(["chatgpt-codex-desktop"], self.inboxes())

    def test_a_team_change_goes_to_the_team_card(self):
        ask = self.team_card()
        entry = turn(self.conn, "Everyone, make them bolder.", reason="everyone", seconds_ago=10)
        self.assertEqual([], self.b().tick(), "no new card")
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_a_pick_goes_to_the_team_card_without_a_task_word(self):
        ask = self.team_card()
        entry = turn(self.conn, "I like Grok's.", reason="", seconds_ago=10)
        self.b().tick()
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_praise_that_picks_nothing_routes_nothing(self):
        ask = self.team_card()
        turn(self.conn, "I like that idea.", reason="", seconds_ago=10)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.revisions(ask))

    def test_naming_a_member_with_the_team_cards_name_goes_to_the_team_card(self):
        ask = self.team_card()
        entry = turn(self.conn, "Grok, make the logo card bolder.", seconds_ago=10)
        self.assertEqual([], self.b().tick(), "not a new Grok card")
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_new_team_work_beside_a_living_team_card_is_a_new_team_card(self):
        ask = self.team_card()
        turn(self.conn, "Everyone, design a mascot for SFDC24.", reason="everyone", seconds_ago=10)
        self.assertEqual(1, len(self.b().tick()))
        self.assertEqual([], self.revisions(ask))

    def test_an_ordinal_pick_is_a_pick_not_a_new_card(self):
        # Codex's review of PR 464: "second one" also reads as NEW_CARD ("a second one").
        ask = self.team_card()
        entry = turn(self.conn, "Let's take the second one.", reason="", seconds_ago=10)
        self.assertEqual([], self.b().tick(), "no new card")
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_making_the_options_bolder_changes_the_team_card(self):
        ask = self.team_card()
        self.assertIsNone(cb.NEW_WORK.search("Everyone, make the options bolder."))
        self.assertIsNotNone(cb.NEW_WORK.search("Everyone, give me options for a tagline."))
        entry = turn(self.conn, "Everyone, make the options bolder.", reason="everyone", seconds_ago=10)
        self.assertEqual([], self.b().tick(), "no new team card")
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_another_callers_pick_routes_nothing(self):
        ask = self.team_card()
        turn(self.conn, "I like Grok's.", reason="", actor="someone_else", seconds_ago=10)
        self.b().tick()
        self.assertEqual([], self.revisions(ask))


class TeamCardsInReadBackMode(unittest.TestCase):
    """Codex's review of PR 464: with the read-back ON, a confirmed team ask is ONE team card, a confirmed team
    change reaches the team card, and a pick needs no read-back. Synthetic sentences only."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW, readback=True)

    def heard(self, entry, seconds_ago):
        agents = json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["agents"]
        return self.conn.xadd(STREAM, {
            "kind": cb.READBACK_SPOKEN, "actor_id": "chair", "room_id": ROOM, "session_epoch": "fixture-epoch",
            "control_sequence": "4", "proj_n": "2", "readback_id": cb.readback_id(ROOM, entry, agents)},
            entry_id=at(seconds_ago))

    def inboxes(self):
        return sorted(k.split(":")[-1] for k in self.conn.streams if k.startswith("fleet:inbox:"))

    def revisions(self, ask):
        return [json.loads(x) for x in getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % ask, [])]

    def confirmed(self, said, seconds_ago, reason="everyone"):
        entry = turn(self.conn, said, reason=reason, seconds_ago=seconds_ago)
        self.assertEqual([], self.b().tick(), "proposed, nothing claimed")
        self.heard(entry, seconds_ago - 5)
        turn(self.conn, "Okay, go ahead.", reason="continuity", seconds_ago=seconds_ago - 8)
        return entry, self.b().tick()

    def team_card(self):
        entry, [ask] = self.confirmed("Everyone, design a logo for SFDC24.", 200)
        info = json.loads(self.conn.hget(life.OPEN_KEY, ask))
        info["field"] = ask + "-work"
        self.conn.hset(life.OPEN_KEY, ask, json.dumps(info))
        board = {"layout": "grid", "chosen": None, "blocks": [
            {"kind": "text", "by": "gemini", "title": "Plan", "body": "Options."},
            {"kind": "text", "by": "grok", "title": "Grok mark", "body": "x"}]}
        self.conn.hset("conf:canvas:%s:%s" % (info["room"], "gemini"), ask + "-work",
                       json.dumps({"type": "board", "title": "Team options", "name": "logo",
                                   "body": json.dumps(board)}))
        return ask

    def test_a_confirmed_team_ask_is_one_team_card(self):
        entry, claimed = self.confirmed("Everyone, design a logo for SFDC24.", 200)
        self.assertEqual(1, len(claimed))
        self.assertEqual(["gemini"], self.inboxes())
        partition = self.conn.hgetall(cp.PARTITION % ("workshop", "grok"))
        self.assertNotIn(cb.proposal_id(ROOM, entry, "grok"), partition, "the other voices' proposals are closed")

    def test_a_confirmed_team_change_reaches_the_team_card(self):
        ask = self.team_card()
        entry, claimed = self.confirmed("Everyone, make them bolder.", 60)
        self.assertEqual([], claimed, "no new card")
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_a_pick_needs_no_read_back(self):
        ask = self.team_card()
        entry = turn(self.conn, "I like Grok's.", reason="", seconds_ago=30)
        self.assertEqual([], self.b().tick())
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_an_ordinal_pick_needs_no_read_back(self):
        ask = self.team_card()
        entry = turn(self.conn, "Let's take the second one.", reason="", seconds_ago=30)
        self.assertEqual([], self.b().tick())
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_a_confirmed_make_the_options_bolder_reaches_the_team_card(self):
        ask = self.team_card()
        entry, claimed = self.confirmed("Everyone, make the options bolder.", 60)
        self.assertEqual([], claimed)
        self.assertEqual([{"entry": entry}], self.revisions(ask))

    def test_a_change_said_as_make_a_change_is_not_new_work(self):
        for said in ("Everyone, make a change to it.", "All of you, make a few small tweaks.",
                     "Everyone, give us a bit more contrast."):
            self.assertIsNone(cb.NEW_WORK.search(said), said)
        for said in ("Everyone, design a logo for SFDC24.", "Grok and Codex, give me pros and cons of a monorepo."):
            self.assertIsNotNone(cb.NEW_WORK.search(said), said)
        ask = self.team_card()
        entry, claimed = self.confirmed("Everyone, make a change to it: darker blue.", 60)
        self.assertEqual([], claimed)
        self.assertEqual([{"entry": entry}], self.revisions(ask))


class CardNamesInReadBackMode(unittest.TestCase):
    """Codex's review of PR 463: with the read-back ON, a confirmed change that says a card's name reaches that
    card, not the voice's newest. Synthetic sentences only."""
    CODEX = "chatgpt-codex-desktop"

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW, readback=True)

    def heard(self, entry, seconds_ago):
        return self.conn.xadd(STREAM, {
            "kind": cb.READBACK_SPOKEN, "actor_id": "chair", "room_id": ROOM, "session_epoch": "fixture-epoch",
            "control_sequence": "4", "proj_n": "2", "readback_id": cb.readback_id(ROOM, entry, [self.CODEX])},
            entry_id=at(seconds_ago))

    def revisions(self, ask):
        return [json.loads(x) for x in getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % ask, [])]

    def claimed(self, said, name, title, seconds_ago):
        entry = turn(self.conn, said, seconds_ago=seconds_ago)
        self.b().tick()
        self.heard(entry, seconds_ago - 5)
        turn(self.conn, "Codex, go ahead.", seconds_ago=seconds_ago - 8)
        [ask] = self.b().tick()
        info = json.loads(self.conn.hget(life.OPEN_KEY, ask))
        info["field"] = ask + "-work"
        self.conn.hset(life.OPEN_KEY, ask, json.dumps(info))
        self.conn.hset("conf:canvas:%s:%s" % (info["room"], self.CODEX), ask + "-work",
                       json.dumps({"type": "note", "title": title, "body": "x", "name": name}))
        return ask

    def test_a_confirmed_change_reaches_the_card_he_named_not_the_newest(self):
        logo = self.claimed("Codex, design a logo for SFDC24.", "SFDC24 logo", "SFDC24 logo concepts", 200)
        plan = self.claimed("Codex, draft an onboarding plan on a new card.", "Onboarding plan", "Onboarding plan", 150)
        change = turn(self.conn, "Codex, make the logo card bolder.", seconds_ago=60)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.revisions(logo), "not before his go-ahead")
        self.heard(change, 50)
        turn(self.conn, "Codex, go ahead.", seconds_ago=45)
        self.assertEqual([], self.b().tick(), "no new claim")
        self.assertEqual([{"entry": change}], self.revisions(logo))
        self.assertEqual([], self.revisions(plan), "not the newest card")
        self.assertNotIn(cb.proposal_id(ROOM, change, self.CODEX), self.conn.hgetall(cp.PARTITION % ("workshop",
                                                                                                      self.CODEX)))

    def test_the_fullest_name_wins_in_read_back_mode_too(self):
        logo = self.claimed("Codex, design a launch logo.", "Launch logo", "Launch logo", 200)
        launch = self.claimed("Codex, draft a launch plan on a new card.", "Launch", "Launch", 150)
        change = turn(self.conn, "Codex, make the launch logo shorter.", seconds_ago=60)
        self.b().tick()
        self.heard(change, 50)
        turn(self.conn, "Codex, go ahead.", seconds_ago=45)
        self.assertEqual([], self.b().tick())
        self.assertEqual([{"entry": change}], self.revisions(logo))
        self.assertEqual([], self.revisions(launch))


class LivingCardsInReadBackMode(unittest.TestCase):
    """Codex's HOLD on PR 461: with the read-back ON, a living card is claimed, revised and made final. A revision
    is authorized exactly like a claim: proposed, read back, then his go-ahead routes it; final needs no read-back
    (it decides about a card that exists). Synthetic sentences only."""
    CODEX = "chatgpt-codex-desktop"

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW, readback=True)

    def heard(self, entry, seconds_ago):
        return self.conn.xadd(STREAM, {
            "kind": cb.READBACK_SPOKEN, "actor_id": "chair", "room_id": ROOM, "session_epoch": "fixture-epoch",
            "control_sequence": "4", "proj_n": "2", "readback_id": cb.readback_id(ROOM, entry, [self.CODEX])},
            entry_id=at(seconds_ago))

    def revisions(self, ask):
        return [json.loads(x) for x in getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % ask, [])]

    def cards(self):
        return self.conn.hgetall(cp.PARTITION % ("workshop", self.CODEX))

    def test_claim_then_revise_then_final(self):
        first = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=110)
        self.b().tick()
        self.heard(first, 100)
        turn(self.conn, "Codex, go ahead.", seconds_ago=95)
        [ask] = self.b().tick()
        info = json.loads(self.conn.hget(life.OPEN_KEY, ask))
        info["field"] = ask + "-work"                                    # the worker put its card up
        self.conn.hset(life.OPEN_KEY, ask, json.dumps(info))
        # A change is proposed and read back like any ask; nothing reaches the card before his go-ahead.
        change = turn(self.conn, "Codex, improve the flow with a retry step.", seconds_ago=60)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.revisions(ask), "not authorized yet")
        self.assertIn(cb.proposal_id(ROOM, change, self.CODEX), self.cards())
        self.heard(change, 50)
        turn(self.conn, "Codex, go ahead.", seconds_ago=45)
        self.assertEqual([], self.b().tick(), "routed, never a second claim")
        self.assertEqual([{"entry": change}], self.revisions(ask), "his original words, by their entry")
        self.assertNotIn(cb.proposal_id(ROOM, change, self.CODEX), self.cards(), "its proposal card is retired")
        self.assertEqual([ask], list(self.conn.hgetall(life.OPEN_KEY)))
        # Final needs no read-back.
        final = turn(self.conn, "Codex, that's final.", seconds_ago=20)
        self.b().tick()
        self.assertEqual([{"entry": change}, {"final": True, "entry": final}], self.revisions(ask))

    def test_a_change_that_is_never_confirmed_reaches_nothing(self):
        first = turn(self.conn, "Codex, draft the sample queue flow.", seconds_ago=110)
        self.b().tick()
        self.heard(first, 100)
        turn(self.conn, "Codex, go ahead.", seconds_ago=95)
        [ask] = self.b().tick()
        info = json.loads(self.conn.hget(life.OPEN_KEY, ask))
        info["field"] = ask + "-work"
        self.conn.hset(life.OPEN_KEY, ask, json.dumps(info))
        turn(self.conn, "Codex, improve the flow with a retry step.", seconds_ago=60)
        self.b().tick()
        turn(self.conn, "Yes please.", reason="continuity", seconds_ago=40)
        self.b().tick()
        self.assertEqual([], self.revisions(ask))


class LivingCardBoundaries(unittest.TestCase):
    """Codex's second HOLD and Copilot's inline findings on PR 461: who may revise or finalize a living card, replay
    safety, negation, expiry, and the lease. Synthetic sentences only."""

    def setUp(self):
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self, holds=True):
        return cb.Bridge(self.conn, LOG, (lambda: holds), clock=lambda: NOW)

    def living(self):
        turn(self.conn, "Grok, write a joke on a card.", seconds_ago=60)
        [ask] = self.b().tick()
        info = json.loads(self.conn.hget(life.OPEN_KEY, ask))
        info["field"] = ask + "-work"
        self.conn.hset(life.OPEN_KEY, ask, json.dumps(info))
        return ask

    def revisions(self, ask):
        return [json.loads(x) for x in getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % ask, [])]

    def test_another_participant_cannot_finalize_or_revise_the_owners_card(self):
        ask = self.living()
        turn(self.conn, "Grok, that's final.", actor="someone_else", seconds_ago=20)
        turn(self.conn, "Grok, make it funnier.", actor="someone_else", seconds_ago=10)
        self.b().tick()
        self.assertEqual([], self.revisions(ask), "never the owner's card")

    def test_a_stale_session_cannot_finalize_or_revise(self):
        ask = self.living()
        turn(self.conn, "Grok, that's final.", session_epoch="older-epoch", seconds_ago=20)
        turn(self.conn, "Grok, make it funnier.", session_epoch="older-epoch", seconds_ago=10)
        self.b().tick()
        self.assertEqual([], self.revisions(ask))

    def test_a_negated_final_is_not_final(self):
        ask = self.living()
        for said in ("Grok, don't make it final, revise the title first.", "Grok, never lock it in."):
            self.assertFalse(cb.final_said(said), said)
        entry = turn(self.conn, "Grok, don't make it final, revise the title first.", seconds_ago=10)
        self.b().tick()
        self.assertEqual([{"entry": entry}], self.revisions(ask), "a revision, not a final")

    def test_a_replay_never_appends_twice_and_a_routed_event_is_never_claimed(self):
        ask = self.living()
        entry = turn(self.conn, "Grok, make it funnier.", seconds_ago=10)
        self.b().tick()
        self.conn.delete(cb.CURSOR_KEY % ROOM) if hasattr(self.conn, "delete") else             self.conn.store.pop(cb.CURSOR_KEY % ROOM, None)
        self.b().tick()
        self.assertEqual([{"entry": entry}], self.revisions(ask), "once")
        self.assertIsNone(self.b()._claim(ROOM, entry, "grok", "Grok, make it funnier.", NOW))

    def test_two_bridges_racing_route_one_event_once(self):
        """Codex's third review of PR 461: the marker check and the append are one WATCHed transaction."""
        ask = self.living()
        entry = turn(self.conn, "Grok, make it funnier.", seconds_ago=10)
        first, second = self.b(), self.b()
        real_pipeline = self.conn.pipeline
        raced = [True]

        def racing_pipeline(transaction=True):
            pipe = real_pipeline(transaction)
            real_watch = pipe.watch

            def watch(*keys):
                real_watch(*keys)
                if raced and raced.pop():
                    # The other bridge routes the same event between this one's WATCH and its EXEC.
                    self.conn.pipeline = real_pipeline
                    self.assertTrue(second._route(ask, entry, "grok", {"entry": entry}))
            pipe.watch = watch
            return pipe
        self.conn.pipeline = racing_pipeline
        self.assertFalse(first._route(ask, entry, "grok", {"entry": entry}), "the loser sees the marker")
        self.conn.pipeline = real_pipeline
        self.assertEqual([{"entry": entry}], self.revisions(ask), "appended once")

    def test_an_expired_ask_is_not_living(self):
        ask = self.living()
        self.conn.store.pop(life.EXPIRY_KEY % ask, None)            # its active token is gone (lazy cleanup)
        turn(self.conn, "Grok, make it funnier.", seconds_ago=10)
        self.assertEqual(1, len(self.b().tick()), "a new card instead of a dead one")
        self.assertEqual([], self.revisions(ask))

    def test_a_lost_lease_does_not_consume_the_final(self):
        ask = self.living()
        final = turn(self.conn, "Grok, that's final.", seconds_ago=10)
        before = self.conn.get(cb.CURSOR_KEY % ROOM)
        held = [True, False]                                          # the lease is lost at the write
        bridge = cb.Bridge(self.conn, LOG, lambda: held.pop(0) if held else False, clock=lambda: NOW)
        bridge.tick()
        self.assertEqual([], self.revisions(ask))
        self.assertEqual(before, self.conn.get(cb.CURSOR_KEY % ROOM), "the event is not consumed")
        self.b().tick()
        self.assertEqual([{"final": True, "entry": final}], self.revisions(ask), "the next holder makes it final")


if __name__ == "__main__":
    unittest.main()

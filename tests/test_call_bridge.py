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
        self.store, self.hashes, self.streams, self.ttls, self.seq = {}, {}, {}, {}, 0

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
        return 1

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, field):
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
    def __init__(self, conn):
        self.conn, self.queued = conn, []

    def multi(self):
        pass

    def reset(self):
        self.queued = []

    def execute(self):
        out = [getattr(self.conn, name)(*a, **kw) for name, a, kw in self.queued]
        self.queued = []
        return out

    def __getattr__(self, name):
        def queue(*a, **kw):
            self.queued.append((name, a, kw))
        return queue


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
        self.assertEqual("In the making: Draft the ... flow for the", got["title"])
        self.assertIsNotNone(cp.clean_item("chatgpt-codex-desktop", ask, json.dumps(got)), "the publisher takes it")
        [(_, entry)] = self.conn.streams["fleet:inbox:chatgpt-codex-desktop"]
        self.assertEqual("call-bridge", entry["by"])
        self.assertIn("draft the queue flow for the vans", entry["text"])
        self.assertIn("answers=%s" % ask, entry["text"])
        self.assertIn(ask, self.conn.hgetall(cb.OPEN_KEY))

    def test_the_public_card_carries_his_ask_in_plain_words_only(self):
        turn(self.conn, "Codex, draft the price sheet for the client's Port-au-Prince store. Ask Marie Dupont first.")
        [ask] = bridge(self.conn).tick()
        published = json.dumps(cp.snapshot(self.conn, "workshop", NOW))
        for word in ("Port", "Prince", "client", "Marie", "Dupont"):
            self.assertNotIn(word, published)
        self.assertIn(ask, published)
        got = card(self.conn, "chatgpt-codex-desktop", ask)
        self.assertTrue(got["body"].startswith("Draft the price sheet for the ... store. Ask ... first. Asked in the room at 10:02"), got)
        self.assertEqual("claimed: Draft the price sheet for the ... store. Ask ... first", got["activity"])
        [(_, entry)] = self.conn.streams["fleet:inbox:chatgpt-codex-desktop"]
        self.assertIn("Port-au-Prince", entry["text"], "the agent still gets every word, in Redis")

    def test_the_done_card_keeps_the_summary(self):
        turn(self.conn, "Grok, center the homepage hero.")
        [ask] = bridge(self.conn).tick()
        later = NOW + datetime.timedelta(minutes=3)
        bridge(self.conn, clock=lambda: later).settle([row("grok", "BCB|v=1|from=grok|answers=%s|text=ok" % ask)])
        got = card(self.conn, "grok", ask)
        self.assertEqual("done", got["status"])
        self.assertTrue(got["body"].startswith("Center the homepage hero. Claimed in the room at 10:02 ET"), got)
        self.assertEqual("done: Center the homepage hero", got["activity"])
        self.assertIsNotNone(cp.clean_item("grok", ask, json.dumps(got)))

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

    def test_no_flip_without_the_lease(self):
        payload = "BCB|v=1|id=X|from=aya|answers=%s|text=done" % self.ask
        self.assertEqual([], bridge(self.conn, holds=False).settle([row("aya", payload)]))

    def test_rows_that_are_not_rows_are_passed_over(self):
        self.assertEqual([], self.settle([None, "text", [], ["only", "two"]]))


class Summary(unittest.TestCase):
    """The public line is built by an allowlist: a word not on PLAIN, or a Title Case word inside a sentence, is a '...'."""

    def test_plain_asks_read_as_said(self):
        self.assertEqual("Center the homepage hero and remove the work section",
                         cb.summary("Grok, center the homepage hero and remove the work section."))
        self.assertEqual("I know but it's a simple html update. You just center it",
                         cb.summary("I know, but it's a simple HTML update. You just center it..."))
        self.assertEqual("Make a page with 3 columns", cb.summary("make a page with 3 columns"))

    def test_names_places_brands_possessives_and_long_numbers_never_pass(self):
        cases = {
            "Codex, draft the price sheet for the client's Port-au-Prince store.": ("client", "Port", "Prince"),
            "Gemini, can you research Marie Dupont at Acme Corp?": ("Marie", "Dupont", "Acme", "Corp"),
            "update the Main Street Store page. IBM wants it.": ("Main", "Street", "Store", "IBM"),
            "Claude, build a page for Apple, call 5551234.": ("Apple", "5551234"),
            "Grok, draft a note to Will and Rose.": ("Will", "Rose"),
        }
        self.assertEqual("Update the ... page", cb.summary("update the Main Street Store page."))
        for said, banned in cases.items():
            line = cb.summary(said)
            for word in banned:
                self.assertNotIn(word, line, (said, line))
            self.assertIn("...", line, said)

    def test_the_sentence_with_the_task_word_is_where_it_starts(self):
        self.assertEqual("So ... I need you to draft the plan", cb.summary("Okay. So, Claude, I need you to draft the plan."))
        self.assertEqual("Can you research ... at", cb.summary("Hello there. Gemini, can you research Marie Dupont at Acme?"))

    def test_short_one_line_no_pipe_and_nothing_without_a_task(self):
        line = cb.summary("Claude, build " + "the big page and " * 20)
        self.assertLessEqual(len(line), cb.SUMMARY_CHARS)
        self.assertLessEqual(len(cb.summary("build " + "x" * 300)), cb.SUMMARY_CHARS)
        long = cb.summary("prepare " + "documents integration dashboard " * 5)
        self.assertLessEqual(len(long), cb.SUMMARY_CHARS)
        self.assertTrue(long.startswith("Prepare documents integration") and long.endswith(" ..."), long)
        self.assertNotIn("|", cb.summary("Grok, build the page | then the footer"))
        self.assertNotIn("\n", cb.summary("Grok, build the page\nthen the footer"))
        self.assertEqual("", cb.summary("Codex, what do you think?"))
        self.assertEqual("", cb.summary(""))
        self.assertTrue(cp.activity_ok("claimed: " + line))

    def test_every_plain_word_is_lower_case_and_one_word(self):
        for word in cb.PLAIN:
            self.assertEqual(word, word.lower(), word)
            self.assertRegex(word, r"^[a-z]+(?:['-][a-z]+)*$")
        for name in ("will", "may", "mark", "bill", "rose", "grace", "hope", "joy", "apple", "grok", "claude",
                     "codex", "gemini", "abdus"):
            self.assertNotIn(name, cb.PLAIN)


class FollowUp(unittest.TestCase):
    """CLAUDE-MOBILE-BRIDGE-CONTINUITY: his follow-up to the voice holding the floor names nobody and is still an ask."""
    SAID = "I know, but it's a simple HTML update. You just center it."

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
        self.assertTrue(got["body"].startswith("I know but it's a simple html update. You just center it."), got)
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
        for said in ("Claude, pull up a picture of a chair.", "Grok, get me an image of a chair.",
                     "Codex, bring the diagram back.", "Gemini, show the plan.", "Claude, a picture please.",
                     "Grok, two images of the logo."):
            self.assertTrue(cb.TASK.search(said), said)
        self.assertEqual(cb.asks_in({"kind": "turn.started", "actor_id": "owner-1", "reason": "vocative",
                                     "utterance": "Claude, pull up a picture of a chair."}), ["claude-code-cli"])

    def test_pull_is_a_plain_word_on_the_card(self):
        self.assertTrue(cb.summary("Claude, pull up a picture of a chair.").startswith("Pull up a picture"))

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
        entry = turn(self.conn, "Could someone find a chair for the guest?", reason="everyone", seconds_ago=10)
        self.say("claude", "I can't pull an image.", seconds_ago=5)
        claimed = self.bridge.tick()
        ask = cb.ask_id(ROOM, entry, "claude-code-cli")
        self.assertEqual(claimed, [ask])
        self.assertEqual(self.claimed("claude-code-cli")[ask]["status"], "claimed")
        inbox = self.conn.streams["fleet:inbox:claude-code-cli"]
        self.assertIn("find a chair for the guest", inbox[-1][1]["text"])

    def test_a_named_ask_and_its_refusal_are_one_claim(self):
        turn(self.conn, "Claude, pull up a picture of a chair.", seconds_ago=10)
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


class InTheMakingAndLatency(unittest.TestCase):
    """O3 (owner orders 2026-10-28 8:46 PM ET): the card says what is being made, and the time from the ask to the
    agent's OWN card is measured against 3 minutes."""

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

    def test_without_words_the_title_still_says_in_the_making(self):
        got = cb.claimed_card("gemini", NOW, "")
        self.assertEqual("In the making: Gemini's work from the room", got["title"])
        self.assertIsNotNone(cp.clean_item("gemini", "call-000000000000", json.dumps(got)))

    def test_the_agent_card_within_three_minutes_is_measured_on_time(self):
        ask = self.claim()
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1,
               "ts": "2026-10-24T14:03:30Z", "title": "In the making: queue flow", "body": "x"}
        self.conn.hset(cp.PARTITION % ("workshop", "chatgpt-codex-desktop"), "queue-flow", json.dumps(own))
        later = NOW + datetime.timedelta(seconds=100)
        self.assertEqual([ask], bridge(self.conn, clock=lambda: later).watch(later))
        got = self.latency(ask)
        self.assertEqual(("queue-flow", 90, True), (got["card"], got["seconds"], got["on_time"]))
        self.assertEqual([], bridge(self.conn, clock=lambda: later).watch(later), "kept once")

    def test_no_card_after_three_minutes_is_kept_once_as_late_then_measured_when_it_comes(self):
        ask = self.claim()
        late = NOW + datetime.timedelta(seconds=200)
        self.assertEqual([ask], bridge(self.conn, clock=lambda: late).watch(late))
        self.assertEqual(("", False), (self.latency(ask)["card"], self.latency(ask)["on_time"]))
        self.assertEqual([], bridge(self.conn, clock=lambda: late).watch(late), "late is kept once")
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1,
               "ts": "2026-10-24T14:06:00Z", "title": "In the making: queue flow", "body": "x"}
        self.conn.hset(cp.PARTITION % ("workshop", "chatgpt-codex-desktop"), "queue-flow", json.dumps(own))
        got_at = NOW + datetime.timedelta(seconds=250)
        bridge(self.conn, clock=lambda: got_at).watch(got_at)
        self.assertEqual(("queue-flow", 240, False), (self.latency(ask)["card"], self.latency(ask)["seconds"],
                                                       self.latency(ask)["on_time"]))

    def test_the_bridge_card_and_older_cards_are_not_the_agents_card(self):
        ask = self.claim()
        old = {"v": 1, "type": "note", "status": "done", "public": True, "version": 3,
               "ts": "2026-10-24T13:00:00Z", "title": "Earlier", "body": "x"}
        self.conn.hset(cp.PARTITION % ("workshop", "chatgpt-codex-desktop"), "earlier", json.dumps(old))
        soon = NOW + datetime.timedelta(seconds=60)
        self.assertEqual([], bridge(self.conn, clock=lambda: soon).watch(soon))
        self.assertIsNone(self.latency(ask))


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
        turn(self.conn, "Codex, draft the price sheet for the client's Port-au-Prince store. Ask Marie Dupont first.")
        b = self.ringing()
        [ask] = b.tick()
        [spec] = self.rung
        self.assertEqual(("CALLBRIDGE-WAKE-" + ask, "chatgpt-codex-desktop", "ASK", "bus-reconciler"),
                         (spec["row_id"], spec["target_surface"], spec["action_type"], spec["source_tag"]))
        self.assertIn("|phase=ASK|", spec["payload"])
        self.assertIn("|to=chatgpt-codex-desktop|", spec["payload"])
        self.assertIn("answers=%s" % ask, spec["payload"])
        self.assertIn("fleet:inbox:chatgpt-codex-desktop", spec["payload"])
        for word in ("Port", "Prince", "client", "Marie", "Dupont"):
            self.assertNotIn(word, json.dumps(spec), "his words stay in the inbox")
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

    def test_the_wake_reply_settles_the_ask(self):
        turn(self.conn, "Gemini, compare the two plans.")
        [ask] = self.ringing().tick()
        later = NOW + datetime.timedelta(minutes=4)
        done = bridge(self.conn, clock=lambda: later).settle(
            [row("gemini", "BCB|v=1|id=GEMINI-REPLY|from=gemini|answers=CALLBRIDGE-WAKE-%s|wakerreply=1" % ask)])
        self.assertEqual([ask], done)
        self.assertEqual("done", card(self.conn, "gemini", ask)["status"])

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
        own = {"v": 1, "type": "note", "status": "working", "public": True, "version": 1,
               "ts": card_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "title": "In the making: queue flow", "body": "x"}
        self.conn.hset(cp.PARTITION % ("workshop", "chatgpt-codex-desktop"), "queue-flow", json.dumps(own))
        tick_at = card_at + datetime.timedelta(seconds=10)
        self.assertEqual([ask], self.ringing(clock=lambda: tick_at).watch(tick_at))
        got = json.loads(self.conn.get(cb.LATENCY_KEY % ask))
        self.assertTrue(got["on_time"])
        self.assertEqual(70, got["seconds"], "claim to card")
        end_to_end = (card_at - said_at).total_seconds()
        self.assertEqual(80, end_to_end)
        self.assertLessEqual(end_to_end, cb.TARGET_SECONDS)


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


if __name__ == "__main__":
    unittest.main()

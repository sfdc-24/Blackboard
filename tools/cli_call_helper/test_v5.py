"""Tests for the v5 laptop call helpers. Every edge is faked: no board, no Redis, no network.
Run: python test_v5.py   (from this folder; it sets the env it needs before importing)."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("LAPTOP_POST_NO_PIN", "1")
if not os.path.isdir(os.path.join(HERE, "..", "..", "scripts")):      # outside the checkout: say where it is
    os.environ.setdefault("CLI_HELPER_BLACKBOARD", r"C:\Users\salam\Quantum\Blackboard.worktrees\o3-helper")
sys.path.insert(0, HERE)
import call_state as cs        # noqa: E402
import laptop_post as lp       # noqa: E402
import call_ask as ca          # noqa: E402
import call_migrate as cm      # noqa: E402

ASK, ASK2 = "call-0123456789ab", "call-ba9876543210"


def write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
ROOM = "owner-room-1"


class Board:
    """A fake board: rows by id, and an append that can land, lose its answer, or drop."""

    def __init__(self):
        self.rows, self.appends, self.mode, self.readable, self.lock = [], [], "ok", True, threading.Lock()

    def records(self):
        return list(self.rows) if self.readable else None

    def append(self, rid, row):
        with self.lock:
            self.appends.append(rid)
            if self.mode in ("ok", "lost"):
                self.rows.append({"Row_ID": rid, "Source_Tag": "claude-code-cli", "Payload": row[5],
                                  "Timestamp": "2026-10-30T15:00:00Z"})
        if self.mode == "ok":
            return 0
        raise RuntimeError("lost answer")


def entry_for(ask=ASK, by="call-bridge", words="draw the lead to quote flow"):
    text = ("CALL ASK %s from the owner, said in the conference room at 11:00 ET (2026-10-30T15:00:00Z): %s -- "
            "Answer with a RESULT board row answers=%s." % (ask, words, ask))
    return {"id": "1793320000000-0", "by": by, "text": text, "at": "x", "task_id": ask, "ask": ask,
            "conference_room": ROOM, "canvas_room": "workshop"}


def event_for(ask=ASK, words="draw the lead to quote flow"):
    for n in range(5000):
        eid = "1793319990000-%d" % n
        if ca.call_bridge.ask_id(ROOM, eid, "claude-code-cli") == ask:
            return {"id": eid, "kind": "turn.started", "utterance": words}
    raise AssertionError("no event id")


def find_ask():
    """An ask id that a real-shaped event id produces, so provenance can match."""
    eid = "1793319990000-7"
    return ca.call_bridge.ask_id(ROOM, eid, "claude-code-cli"), eid


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        cs.STATE = os.path.join(self.dir, "cli_call_state.json")
        cs.LOCKFILE = cs.STATE + ".lock"
        self.board = Board()
        self.now = time.time()
        p = [mock.patch.object(lp, "board_records", self.board.records),
             mock.patch.object(lp.fa, "append_and_verify", self.board.append),
             mock.patch.object(lp, "DEDUP_ENFORCED", False),
             mock.patch.object(ca, "sanitizer", lambda: "1710f0ebe98b")]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def task(self, ask=ASK, **kw):
        with cs.locked() as txn:
            t = txn.state["tasks"].setdefault(ask, {"rev": 0, "ops": {}, "state": "PENDING"})
            cs.touch(t, **kw)
        return cs.load()["tasks"][ask]

    def reads(self, answers, log=None):
        def fake(reads):
            if log is not None:
                log.append([r[0] for r in reads])
            return {name: (answers[name]() if callable(answers.get(name)) else answers.get(name))
                    for name, _o, _k, _e in reads}
        return fake

    def open_body(self, ask=ASK, agent="claude-code-cli"):
        rec = {"agent": agent, "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - 30)),
               "room": "workshop"}
        return {ask: json.dumps(rec, sort_keys=True)}


# ---------------------------------------------------------------- the lock (v4 failures 5 and 6)
class Lock(Base):
    def test_a_live_holder_is_never_broken_whatever_its_age(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with cs.locked():
                held.set()
                release.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        os.utime(cs.LOCKFILE, (0, 0))                     # an "ancient" lock file: age means nothing now
        with self.assertRaises(cs.Busy) as e:
            with cs.locked(wait=0.5):
                pass
        self.assertEqual(4, e.exception.code)
        release.set()
        t.join()

    def test_a_killed_holder_frees_the_lock(self):
        code = ("import sys,time; sys.path.insert(0, %r); import call_state as cs; cs.STATE=%r; cs.LOCKFILE=%r\n"
                "with cs.locked():\n    print('held', flush=True); time.sleep(60)\n") % (HERE, cs.STATE, cs.LOCKFILE)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual("held", p.stdout.readline().strip())
        with self.assertRaises(cs.Busy):
            with cs.locked(wait=0.3):
                pass
        p.kill()
        p.wait()
        p.stdout.close()
        with cs.locked(wait=5) as txn:
            self.assertEqual({}, txn.state["tasks"])

    def test_two_accepts_at_once_post_one_ack(self):
        ask, eid = find_ask()
        entry = dict(entry_for(ask), id="1793320000000-0")
        ev = {"id": eid, "kind": "turn.started", "utterance": "draw the lead to quote flow"}
        answers = {"inbox": [entry], "open": self.open_body(ask), "events": [ev]}
        out = []
        go = threading.Barrier(2)

        def run():
            go.wait()
            out.append(ca.accept(ask, "mermaid", "lead to quote", reads=self.reads(answers)))
        ts = [threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(1, len(self.board.appends))
        self.assertEqual("ACCEPTED", cs.load()["tasks"][ask]["state"])
        self.assertIn(0, out)


# ---------------------------------------------------------------- appends and UNKNOWN (HOLD 3, Aya item 2)
class Appends(Base):
    def send(self, rid="CLAUDE-X-1"):
        with cs.locked() as txn:
            txn.state["tasks"].setdefault(ASK, {"rev": 0, "ops": {}, "state": "ACCEPTED"})
            return lp.send(txn, ASK, rid, [rid, "", "", "", "", "BCB|v=1|id=" + rid])

    def test_lost_answer_but_landed_is_confirmed_by_a_read_and_never_resent(self):
        self.board.mode = "lost"
        self.assertEqual("UNKNOWN", self.send())
        self.board.mode = "ok"
        self.assertEqual("CONFIRMED", self.send())
        self.assertEqual(1, len(self.board.appends))

    def test_lost_and_absent_stays_unknown_and_is_never_resent(self):
        self.board.mode = "drop"
        self.assertEqual("UNKNOWN", self.send())
        self.board.mode = "ok"
        for _ in range(3):
            self.assertEqual("UNKNOWN", self.send())
        self.assertEqual(1, len(self.board.appends))

    def test_one_retry_only_under_enforced_dedup(self):
        self.board.mode = "drop"
        with mock.patch.object(lp, "DEDUP_ENFORCED", True):
            self.send()
            self.send()
            self.send()
        self.assertEqual(2, len(self.board.appends))

    def test_an_append_that_returns_not_verified_is_unknown(self):
        with mock.patch.object(lp.fa, "append_and_verify", lambda rid, row: 1):
            self.assertEqual("UNKNOWN", self.send())

    def test_an_unreadable_board_sends_and_saves_nothing(self):
        self.board.readable = False
        self.assertEqual("UNKNOWN", self.send())
        self.assertEqual([], self.board.appends)
        self.assertNotIn("CLAUDE-X-1", cs.load()["tasks"][ASK]["ops"])

    def test_a_crash_after_intent_is_not_followed_by_a_second_append(self):
        def crash(*a):
            raise KeyboardInterrupt                          # the process dies inside the append
        with mock.patch.object(lp, "bounded", crash):
            with self.assertRaises(KeyboardInterrupt):
                self.send()
        self.assertEqual("INTENT", cs.load()["tasks"][ASK]["ops"]["CLAUDE-X-1"]["state"])
        self.assertEqual("UNKNOWN", self.send())
        self.assertEqual([], self.board.appends)


# ---------------------------------------------------------------- accept: authority and provenance (items 3, 4)
class Accept(Base):
    def setUp(self):
        super().setUp()
        self.ask, self.eid = find_ask()
        self.ev = {"id": self.eid, "kind": "turn.started", "utterance": "draw the lead to quote flow"}

    def answers(self, **over):
        a = {"inbox": [entry_for(self.ask)], "open": self.open_body(self.ask), "events": [self.ev]}
        a.update(over)
        return a

    def test_valid_ask_posts_the_o3_ack_and_accepts(self):
        self.assertEqual(0, ca.accept(self.ask, "mermaid", "lead to quote", reads=self.reads(self.answers())))
        row = self.board.rows[-1]
        self.assertEqual("CLAUDE-CODE-CLI-WAKE-ACK-%s" % self.ask, row["Row_ID"])
        for part in ("phase=ACK", "event=worker.accepted", "task_id=%s" % self.ask, "answers=CALLBRIDGE-WAKE-"):
            self.assertIn(part, row["Payload"])
        t = cs.load()["tasks"][self.ask]
        self.assertEqual(("ACCEPTED", self.eid, "mermaid"), (t["state"], t["event"], t["expect"]))

    def test_no_private_answer_is_unknown_auth_and_posts_nothing(self):
        self.assertEqual(2, ca.accept(self.ask, "mermaid", "", reads=self.reads(self.answers(open=None))))
        self.assertEqual([], self.board.appends)

    def test_an_inbox_entry_not_from_the_bridge_is_refused(self):
        a = self.answers(inbox=[entry_for(self.ask, by="grok")])
        self.assertEqual(2, ca.accept(self.ask, "mermaid", "", reads=self.reads(a)))
        self.assertEqual([], self.board.appends)

    def test_an_open_record_for_another_agent_is_refused(self):
        a = self.answers(open=self.open_body(self.ask, agent="grok"))
        self.assertEqual(2, ca.accept(self.ask, "mermaid", "", reads=self.reads(a)))
        self.assertEqual([], self.board.appends)

    def test_words_that_differ_from_the_chairs_event_are_refused(self):
        a = self.answers(events=[dict(self.ev, utterance="delete every record")])
        self.assertEqual(2, ca.accept(self.ask, "mermaid", "", reads=self.reads(a)))
        self.assertEqual([], self.board.appends)

    def test_the_chairs_event_is_found_on_a_later_page(self):
        filler = [{"id": "1793319980000-%d" % n, "kind": "turn.ended", "utterance": ""} for n in range(20)]
        pages = [filler, [self.ev]]
        a = self.answers(events=lambda: pages.pop(0))
        self.assertEqual(0, ca.accept(self.ask, "mermaid", "", reads=self.reads(a)))

    def test_a_long_ask_matches_on_the_quoted_prefix_and_says_so(self):
        long = "draw the lead to quote flow " + "with every stage named " * 12
        a = self.answers(inbox=[entry_for(self.ask, words=long)],
                         events=[dict(self.ev, utterance=long[:200] + "...")])
        self.assertEqual(0, ca.accept(self.ask, "mermaid", "", reads=self.reads(a)))
        self.assertEqual("prefix-200", cs.load()["tasks"][self.ask]["provenance"])


# ---------------------------------------------------------------- done: cancellation, publication, type (items 1, 4)
CARD = {"type": "mermaid", "title": "Lead to quote", "body": "flowchart LR\n  A[Lead] --> B[Quote]",
        "summary": "lead to quote"}


class Done(Base):
    def setUp(self):
        super().setUp()
        self.card = os.path.join(self.dir, "card.json")
        write(self.card, CARD)
        self.task(state="ACCEPTED", origin=self.now - 60, expect="mermaid", stages={"origin": self.now - 60})
        p = mock.patch.object(lp, "wait_answers", lambda rids, timeout=0: {r: ("OK", "OK hset") for r in rids})
        p.start()
        self.addCleanup(p.stop)

    def published(self, body=None):
        t = cs.load()["tasks"][ASK]
        card = dict(t["card"], body=body or t["card"]["body"])
        item = ca.canvas_publish.clean_item("claude-code-cli", t["field"], json.dumps(card))
        return lambda: {"generated": "2026-10-30T15:01:00Z", "items": [item]}

    def run_done(self, open_seq, fetch=None):
        seq = list(open_seq)
        reads = self.reads({"status": lambda: seq.pop(0) if seq else None})
        return ca.done(ASK, self.card, reads=reads, fetch=fetch or (lambda: {"items": []}), sleep=lambda s: None)

    def test_the_whole_path_ends_in_one_done_row(self):
        ob = self.open_body()
        with mock.patch.object(ca, "PUBLISH_WAIT", 0.01):
            self.assertEqual(2, self.run_done([ob]))         # stored, not yet public: stops, state kept
        self.assertEqual("STORED", cs.load()["tasks"][ASK]["state"])
        self.assertEqual(0, self.run_done([ob], fetch=self.published()))
        ids = [r["Row_ID"] for r in self.board.rows]
        self.assertEqual(["CLAUDE-CARD-%s" % ASK, "CLAUDE-DONE-%s" % ASK], ids)
        self.assertIn("answers=%s" % ASK, self.board.rows[-1]["Payload"])
        self.assertEqual("DONE", cs.load()["tasks"][ASK]["state"])

    def test_closed_before_the_card_cancels_with_no_card(self):
        self.assertEqual(2, self.run_done([{}]))
        self.assertEqual("CANCELLED", cs.load()["tasks"][ASK]["state"])
        self.assertEqual([], self.board.appends)

    def test_a_resumed_writing_step_still_checks_the_claim(self):
        self.assertEqual(2, self.run_done([None]))           # UNKNOWN_AUTH: nothing, and not a cancel
        self.assertEqual("ACCEPTED", cs.load()["tasks"][ASK]["state"])
        self.task(state="WRITING", field="%s-work" % ASK, card=ca.card_for(ASK, CARD))
        self.assertEqual(2, self.run_done([{}]))
        self.assertEqual("CANCELLED", cs.load()["tasks"][ASK]["state"])
        self.assertEqual([], self.board.appends)

    def test_closed_after_the_card_withdraws_it_and_posts_no_done(self):
        ob = self.open_body()
        with mock.patch.object(ca, "PUBLISH_WAIT", 0.01):
            self.run_done([ob])
        self.assertEqual(2, self.run_done([{}], fetch=self.published()))
        ids = [r["Row_ID"] for r in self.board.rows]
        self.assertEqual(["CLAUDE-CARD-%s" % ASK, "CLAUDE-CARD-%s-WD" % ASK], ids)
        self.assertEqual("CANCELLED_AFTER_WRITE", cs.load()["tasks"][ASK]["state"])

    def test_same_id_and_ts_with_another_body_is_not_published(self):
        ob = self.open_body()
        with mock.patch.object(ca, "PUBLISH_WAIT", 0.01):
            self.run_done([ob])
            self.assertEqual(2, self.run_done([ob], fetch=self.published(body="flowchart LR\n  A --> Z")))
        self.assertEqual("STORED", cs.load()["tasks"][ASK]["state"])

    def test_a_card_of_the_wrong_type_is_refused_before_writing(self):
        write(self.card, dict(CARD, type="note"))
        self.assertEqual(2, self.run_done([self.open_body()]))
        self.assertEqual([], self.board.appends)

    def test_a_task_changed_while_waiting_re_plans(self):
        def meddle():
            self.task(note="changed elsewhere")
            return self.open_body()
        self.assertEqual(6, ca.done(ASK, self.card, reads=self.reads({"status": meddle}), sleep=lambda s: None))
        self.assertEqual([], self.board.appends)

    def test_status_reads_use_a_fresh_id_every_time(self):
        seen = []
        real = ca.fa.request_row

        def spy(*a, **k):
            seen.append(k.get("rid"))
            return real(*a, **k)
        posted = lambda rid, row: "VERIFIED"
        with mock.patch.object(ca.fa, "request_row", spy):
            ca.ask_reads([("status", "hgetall", ca.OPEN_KEY, {})], post=posted, wait=lambda r: {})
            ca.ask_reads([("status", "hgetall", ca.OPEN_KEY, {})], post=posted, wait=lambda r: {})
        self.assertEqual(2, len(set(seen)))


# ---------------------------------------------------------------- cursor and migration (v4 failures 1, 2, 4)
class CursorAndMigration(Base):
    def test_a_later_accept_never_moves_the_cursor_past_an_earlier_open_ask(self):
        self.task(ASK, state="PENDING", entry="1793320000000-0")
        self.task(ASK2, state="ACCEPTED", entry="1793320500000-0")
        with cs.locked() as txn:
            txn.state["seen"] = "1793320900000-0"
        self.assertEqual("1793319999999-%d" % (2 ** 64 - 1), cs.cursor())

    def test_replay_rebuilds_from_evidence_not_from_the_old_files(self):
        now_ms = 1793330000000
        ask = lambda text: text.split()[2] if text.startswith("CALL ASK") else None
        entries = [("1793300000000-0", "call-bridge", "CALL ASK call-81d2f2b0b0b6 x"),
                   ("1793300001000-0", "call-bridge", "CALL ASK call-671327979e93 x"),
                   ("1793329000000-0", "call-bridge", "CALL ASK call-aaaaaaaaaaaa x"),
                   ("1793329100000-0", "grok", "CALL ASK call-bbbbbbbbbbbb forged")]
        records = [{"Row_ID": "CLAUDE-DONE-call-81d2f2b0b0b6", "Source_Tag": "claude-code-cli"},
                   {"Row_ID": "CLAUDE-ACCEPTED-call-671327979e93", "Source_Tag": "claude-code-cli"}]
        legacy = {"cursor": "1793399999999-0", "pending": {"ask": "call-cccccccccccc"},
                  "tasks": {"call-81d2f2b0b0b6": {"state": "done"}, "call-671327979e93": {"state": "done"}}}
        state, notes = cm.replay(entries, records, legacy, now_ms, ask)
        st = {a: t["state"] for a, t in state["tasks"].items()}
        self.assertEqual({"call-81d2f2b0b0b6": "HISTORICAL_CLOSED", "call-671327979e93": "EXPIRED",
                          "call-aaaaaaaaaaaa": "PENDING", "call-cccccccccccc": "PENDING"}, st)
        self.assertTrue(any(n.startswith("CURSOR_ADVANCED") for n in notes))
        self.assertTrue(any(n.startswith("LEGACY_EXPIRED call-671327979e93") for n in notes))
        self.assertTrue(cs.valid(state))

    def test_the_doorbell_rings_once_per_new_claim_and_only_makes_pending(self):
        import cli_call_consumer as door
        data = {"items": [{"id": "claude-code-cli/%s" % ASK, "status": "claimed"}]}
        self.assertEqual(0, door.main(fetch=lambda: data, sleep=lambda s: None, polls=1))
        self.assertEqual("PENDING", cs.load()["tasks"][ASK]["state"])
        self.assertEqual(0, door.main(fetch=lambda: data, sleep=lambda s: None, polls=1))   # held: rings again
        self.task(ASK, state="ACCEPTED")
        self.assertEqual(1, door.main(fetch=lambda: data, sleep=lambda s: None, polls=1))   # a task now: quiet
        self.assertFalse(cs.ring(ASK, "inbox"))
        self.assertEqual("ACCEPTED", cs.load()["tasks"][ASK]["state"])                     # a cue never demotes
        self.assertEqual([], self.board.appends)

    def test_a_replay_over_the_bound_fails_closed(self):
        with self.assertRaises(SystemExit):
            cm.replay([("%d-0" % i, "call-bridge", "x") for i in range(501)], [], {}, 0, lambda t: None)


if __name__ == "__main__":
    unittest.main(verbosity=1)

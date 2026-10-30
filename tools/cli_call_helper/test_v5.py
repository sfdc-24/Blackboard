"""Tests for the v5.1 laptop call helpers. Every edge is faked: no board, no Redis, no network.
Run: python test_v5.py   (from this folder; it sets the env it needs before importing)."""
import hashlib
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
WORDS = "draw the lead to quote flow"


def write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


class Board:
    """A fake board: rows by id, an append that can land, lose its answer, or drop, and reconciler answers."""

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

    def answer(self, rid, text="OK hset"):
        self.rows.append({"Row_ID": "ANS-" + rid, "Source_Tag": "bus-reconciler",
                          "Payload": "BCB|v=1|phase=AYA_RESULT|from=bus-reconciler|answers=%s|text=%s" % (rid, text)})


def bstate(state="open", agent="claude-code-cli", claimed_at=None, parts=1, at=None):
    d = {"state": state, "agent": agent, "conference_room": "room-1",
         "claimed_at": claimed_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30)),
         "parts": parts}
    if at:
        d["at"] = at
    return json.dumps(d, sort_keys=True)


def protected(words=WORDS, **kw):
    """What the three accept-round-1 reads return for an honest claim, plus the extra parts."""
    parts = [words[i:i + 90] for i in range(0, len(words), 90)] or [""]
    out = {"state": bstate(parts=len(parts), **kw), "said": hashlib.sha256(words.encode()).hexdigest()}
    for i, p in enumerate(parts):
        out["p%d" % i] = json.dumps(p)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        cs.STATE = os.path.join(self.dir, "cli_call_state.json")
        cs.LOCKFILE = cs.STATE + ".lock"
        cs._write(cs.empty())                               # what migrate leaves behind
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
            out = {}
            for name, _o, _k, _e in reads:
                v = answers.get(name)
                out[name] = v() if callable(v) else v
                out["_rid_" + name] = "CLAUDE-ST-%s-x" % name
            return out
        return fake


# ---------------------------------------------------------------- state and lock (v4 failures 5, 6; Aya 8659 item 2)
class StateAndLock(Base):
    def test_no_state_file_stops_with_exit_5(self):
        os.remove(cs.STATE)
        with self.assertRaises(cs.NoState) as e:
            cs.load()
        self.assertEqual(5, e.exception.code)
        with self.assertRaises(cs.NoState):
            ca.accept(ASK, "mermaid", "", reads=self.reads(protected()))
        self.assertEqual([], self.board.appends)

    def test_the_cursor_never_defaults_to_now(self):
        self.assertEqual("0-0", cs.cursor())

    def test_a_live_holder_is_never_broken_whatever_its_age(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with cs.locked():
                held.set()
                release.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        os.utime(cs.LOCKFILE, (0, 0))
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

    def test_a_later_accept_never_moves_the_cursor_past_an_earlier_open_ask(self):
        self.task(ASK, state="PENDING", entry="1793320000000-0")
        self.task(ASK2, state="ACCEPTED", entry="1793320500000-0")
        with cs.locked() as txn:
            txn.state["seen"] = "1793320900000-0"
        self.assertEqual("1793319999999-%d" % (2 ** 64 - 1), cs.cursor())


# ---------------------------------------------------------------- appends (Aya item 2 / 8570 item 5)
class Appends(Base):
    def send(self, rid="CLAUDE-X-1", payload=None):
        with cs.locked() as txn:
            txn.state["tasks"].setdefault(ASK, {"rev": 0, "ops": {}, "state": "ACCEPTED"})
            return lp.send(txn, ASK, rid, [rid, "", "", "", "", payload or ("BCB|v=1|id=" + rid)])

    def test_lost_answer_but_landed_is_confirmed_by_a_read_and_never_resent(self):
        self.board.mode = "lost"
        self.assertEqual("CONFIRMED", self.send())           # the fresh read after the append finds it
        self.assertEqual(1, len(self.board.appends))

    def test_lost_and_absent_stays_unknown_and_is_never_resent(self):
        self.board.mode = "drop"
        self.assertEqual("UNKNOWN", self.send())
        self.board.mode = "ok"
        for _ in range(3):
            self.assertEqual("UNKNOWN", self.send())
        self.assertEqual(1, len(self.board.appends))

    def test_one_retry_only_under_enforced_dedup_and_with_the_first_row(self):
        self.board.mode = "drop"
        with mock.patch.object(lp, "DEDUP_ENFORCED", True):
            self.send(payload="BCB|first")
            self.send(payload="BCB|rebuilt-later")
            self.send()
        self.assertEqual(2, len(self.board.appends))
        self.assertEqual("BCB|first", cs.load()["tasks"][ASK]["ops"]["CLAUDE-X-1"]["row"][5])

    def test_an_append_that_says_ok_but_is_not_on_the_board_is_unknown(self):
        with mock.patch.object(lp.fa, "append_and_verify", lambda rid, row: 0):
            self.assertEqual("UNKNOWN", self.send())

    def test_the_same_id_with_another_payload_is_a_conflict_not_a_confirm(self):
        self.board.rows.append({"Row_ID": "CLAUDE-X-1", "Source_Tag": "claude-code-cli", "Payload": "BCB|other"})
        self.assertEqual("CONFLICT", self.send(payload="BCB|mine"))
        self.assertEqual([], self.board.appends)
        self.assertEqual("CONFLICT", self.send(payload="BCB|mine"))

    def test_an_unreadable_board_sends_and_saves_nothing(self):
        self.board.readable = False
        self.assertEqual("UNKNOWN", self.send())
        self.assertEqual([], self.board.appends)
        self.assertNotIn("CLAUDE-X-1", cs.load()["tasks"][ASK]["ops"])

    def test_a_crash_after_intent_is_not_followed_by_a_second_append(self):
        def crash(*a):
            raise KeyboardInterrupt
        with mock.patch.object(lp, "bounded", crash):
            with self.assertRaises(KeyboardInterrupt):
                self.send()
        self.assertEqual("INTENT", cs.load()["tasks"][ASK]["ops"]["CLAUDE-X-1"]["state"])
        self.assertEqual("UNKNOWN", self.send())
        self.assertEqual([], self.board.appends)


# ---------------------------------------------------------------- accept: protected authority, whole ask (items 2, 6a)
class Accept(Base):
    def test_valid_ask_posts_the_o3_ack_and_accepts(self):
        self.assertEqual(0, ca.accept(ASK, "mermaid", "lead to quote", reads=self.reads(protected())))
        row = self.board.rows[-1]
        self.assertEqual("CLAUDE-CODE-CLI-WAKE-ACK-%s" % ASK, row["Row_ID"])
        for part in ("phase=ACK", "event=worker.accepted", "task_id=%s" % ASK, "answers=CALLBRIDGE-WAKE-"):
            self.assertIn(part, row["Payload"])
        t = cs.load()["tasks"][ASK]
        self.assertEqual(("ACCEPTED", "full", len(WORDS)), (t["state"], t["provenance"], t["said_len"]))

    def test_an_ask_claimed_before_any_watermark_is_still_accepted(self):
        """Aya 8659 item 2: an earlier, unindexed ask A behind a later accepted B is found (no cursor is read)."""
        self.task(ASK2, state="ACCEPTED", entry="1799999999999-0")
        with cs.locked() as txn:
            txn.state["seen"] = "1799999999999-0"
        log = []
        self.assertEqual(0, ca.accept(ASK, "mermaid", "", reads=self.reads(protected(), log)))
        self.assertNotIn("inbox", sum(log, []))

    def test_a_long_ask_is_rebuilt_whole_from_its_parts(self):
        long = WORDS + " with every stage named" * 20
        self.assertEqual(0, ca.accept(ASK, "mermaid", "", reads=self.reads(protected(long))))
        self.assertEqual(len(long), cs.load()["tasks"][ASK]["said_len"])

    def test_a_tampered_part_is_refused(self):
        a = protected(WORDS + " and more " * 20)
        a["p1"] = json.dumps("delete every record")
        self.assertEqual(2, ca.accept(ASK, "mermaid", "", reads=self.reads(a)))
        self.assertEqual([], self.board.appends)

    def test_no_private_answer_is_unknown_auth_and_posts_nothing(self):
        a = protected()
        a["state"] = None
        self.assertEqual(2, ca.accept(ASK, "mermaid", "", reads=self.reads(a)))
        self.assertEqual([], self.board.appends)

    def test_a_state_for_another_agent_or_not_open_is_refused(self):
        for kw in ({"agent": "grok"}, {"state": "cancelled"}, {"state": "done"}):
            self.assertEqual(2, ca.accept(ASK, "mermaid", "", reads=self.reads(protected(**kw))))
        self.assertEqual([], self.board.appends)

    def test_a_malformed_digest_is_refused(self):
        a = protected()
        a["said"] = "not-a-digest"
        self.assertEqual(2, ca.accept(ASK, "mermaid", "", reads=self.reads(a)))
        self.assertEqual([], self.board.appends)

    def test_two_accepts_at_once_post_one_ack(self):
        out, go = [], threading.Barrier(2)

        def run():
            go.wait()
            out.append(ca.accept(ASK, "mermaid", "", reads=self.reads(protected())))
        ts = [threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(1, len(self.board.appends))
        self.assertIn(0, out)


# ---------------------------------------------------------------- done: cancel, withdraw order, DONE race (items 3, 4)
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

    def run_done(self, statuses, fetch=None, now=None):
        seq = list(statuses)
        reads = self.reads({"status": lambda: seq.pop(0) if seq else None})
        kw = {"now": now} if now else {}
        return ca.done(ASK, self.card, reads=reads, fetch=fetch or (lambda: {"items": []}), sleep=lambda s: None,
                       **kw)

    def to_stored(self):
        with mock.patch.object(ca, "PUBLISH_WAIT", 0.01):
            self.assertEqual(2, self.run_done([bstate()]))
        self.assertEqual("STORED", cs.load()["tasks"][ASK]["state"])

    def test_the_whole_path_ends_in_one_done_row_with_its_status_op(self):
        self.to_stored()
        self.assertEqual(0, self.run_done([bstate(), bstate()], fetch=self.published()))
        ids = [r["Row_ID"] for r in self.board.rows]
        self.assertEqual(["CLAUDE-CARD-%s" % ASK, "CLAUDE-DONE-%s" % ASK], ids)
        self.assertIn("answers=%s" % ASK, self.board.rows[-1]["Payload"])
        self.assertIn("status_op=CLAUDE-ST-status-x", self.board.rows[-1]["Payload"])
        self.assertEqual("DONE", cs.load()["tasks"][ASK]["state"])

    def test_cancelled_before_the_card_cancels_with_no_card(self):
        self.assertEqual(2, self.run_done([bstate("cancelled")]))
        self.assertEqual("CANCELLED", cs.load()["tasks"][ASK]["state"])
        self.assertEqual([], self.board.appends)

    def test_a_resumed_writing_step_still_checks_the_claim(self):
        self.assertEqual(2, self.run_done([None]))           # UNKNOWN_AUTH: nothing, and not a cancel
        self.assertEqual("ACCEPTED", cs.load()["tasks"][ASK]["state"])
        self.task(state="WRITING", field="%s-work" % ASK, card=ca.card_for(ASK, CARD))
        self.assertEqual(2, self.run_done([bstate("cancelled")]))
        self.assertEqual("CANCELLED", cs.load()["tasks"][ASK]["state"])
        self.assertEqual([], self.board.appends)

    def test_cancelled_after_the_card_is_not_terminal_until_the_withdraw_lands_after_the_card(self):
        self.to_stored()
        self.assertEqual(2, self.run_done([bstate("cancelled")], fetch=self.published()))
        self.assertEqual("CANCELLING", cs.load()["tasks"][ASK]["state"])
        self.assertEqual(2, self.run_done([]))                # CARD answer not seen yet: no withdraw sent
        self.assertNotIn("CLAUDE-CARD-%s-WD" % ASK, [r["Row_ID"] for r in self.board.rows])
        self.board.answer("CLAUDE-CARD-%s" % ASK)
        self.assertEqual(2, self.run_done([]))                # withdraw sent; its answer not seen
        self.assertIn("CLAUDE-CARD-%s-WD" % ASK, [r["Row_ID"] for r in self.board.rows])
        self.assertEqual("CANCELLING", cs.load()["tasks"][ASK]["state"])
        self.board.answer("CLAUDE-CARD-%s-WD" % ASK)
        self.assertEqual(0, self.run_done([]))
        self.assertEqual("WITHDRAWN", cs.load()["tasks"][ASK]["state"])
        self.assertNotIn("CLAUDE-DONE-%s" % ASK, [r["Row_ID"] for r in self.board.rows])

    def test_a_withdraw_answered_before_the_card_answer_is_not_final(self):
        self.to_stored()
        self.run_done([bstate("cancelled")], fetch=self.published())
        self.board.answer("CLAUDE-CARD-%s" % ASK)
        self.run_done([])                                     # WD sent
        wd_answer = "CLAUDE-CARD-%s-WD" % ASK
        card_answer = [r for r in self.board.rows if r["Row_ID"] == "ANS-CLAUDE-CARD-%s" % ASK][0]
        self.board.rows.remove(card_answer)                   # the CARD answer now lands AFTER the withdraw's
        self.board.answer(wd_answer)
        self.board.rows.append(card_answer)
        self.assertEqual(2, self.run_done([]))
        self.assertNotEqual("WITHDRAWN", cs.load()["tasks"][ASK]["state"])
        self.assertIn("CLAUDE-CARD-%s-WD2" % ASK, [r["Row_ID"] for r in self.board.rows], "a second withdraw goes")

    def test_an_unanswered_card_past_the_deadline_is_failed_card_may_be_live(self):
        self.to_stored()
        self.run_done([bstate("cancelled")], fetch=self.published())
        late = lambda: self.now + ca.MAX_AGE + 10
        self.assertEqual(2, self.run_done([], now=late))
        self.assertEqual("FAILED_CARD_MAY_BE_LIVE", cs.load()["tasks"][ASK]["state"])

    def test_a_cancel_between_the_last_read_and_done_supersedes_it_and_withdraws(self):
        self.to_stored()
        early = "2026-01-01T00:00:00Z"
        self.assertEqual(2, self.run_done([bstate(), bstate("cancelled", at=early)], fetch=self.published()))
        t = cs.load()["tasks"][ASK]
        self.assertEqual(("CANCELLING", True), (t["state"], t["done_superseded"]))

    def test_same_id_and_ts_with_another_body_is_not_published(self):
        self.to_stored()
        with mock.patch.object(ca, "PUBLISH_WAIT", 0.01):
            self.assertEqual(2, self.run_done([bstate()], fetch=self.published(body="flowchart LR\n  A --> Z")))
        self.assertEqual("STORED", cs.load()["tasks"][ASK]["state"])

    def test_a_card_of_the_wrong_type_is_refused_before_writing(self):
        write(self.card, dict(CARD, type="note"))
        self.assertEqual(2, self.run_done([bstate()]))
        self.assertEqual([], self.board.appends)

    def test_a_task_changed_while_waiting_re_plans(self):
        def meddle():
            self.task(note="changed elsewhere")
            return bstate()
        self.assertEqual(6, ca.done(ASK, self.card, reads=self.reads({"status": meddle}), sleep=lambda s: None))
        self.assertEqual([], self.board.appends)

    def test_status_reads_use_a_fresh_id_every_time(self):
        seen, real = [], ca.fa.request_row

        def spy(*a, **k):
            seen.append(k.get("rid"))
            return real(*a, **k)
        posted = lambda rid, row: "VERIFIED"
        with mock.patch.object(ca.fa, "request_row", spy):
            ca.ask_reads([("status", "get", "gov:x", {})], post=posted, wait=lambda r: {})
            ca.ask_reads([("status", "get", "gov:x", {})], post=posted, wait=lambda r: {})
        self.assertEqual(2, len(set(seen)))


# ---------------------------------------------------------------- migrate (v4 failures 1, 2; Aya 8659 item 1)
class Migrate(Base):
    def test_replay_rebuilds_from_evidence_not_from_the_old_files(self):
        now_ms = 1793330000000
        entries = [("1793300000000-0", "call-bridge", "CALL ASK call-81d2f2b0b0b6 x"),
                   ("1793300001000-0", "call-bridge", "CALL ASK call-671327979e93 x"),
                   ("1793329000000-0", "call-bridge", "CALL ASK call-aaaaaaaaaaaa x"),
                   ("1793329100000-0", "grok", "CALL ASK call-bbbbbbbbbbbb forged")]
        records = [{"Row_ID": "CLAUDE-DONE-call-81d2f2b0b0b6", "Source_Tag": "claude-code-cli"},
                   {"Row_ID": "CLAUDE-ACCEPTED-call-671327979e93", "Source_Tag": "claude-code-cli"}]
        legacy = {"cursor": "1793399999999-0", "pending": {"ask": "call-cccccccccccc"},
                  "tasks": {"call-81d2f2b0b0b6": {"state": "done"}, "call-671327979e93": {"state": "done"}}}
        state, notes = cm.replay(entries, records, legacy, now_ms, ca.ask_of)
        st = {a: t["state"] for a, t in state["tasks"].items()}
        self.assertEqual({"call-81d2f2b0b0b6": "HISTORICAL_CLOSED", "call-671327979e93": "EXPIRED",
                          "call-aaaaaaaaaaaa": "PENDING", "call-cccccccccccc": "PENDING"}, st)
        self.assertTrue(any(n.startswith("CURSOR_ADVANCED") for n in notes))
        self.assertTrue(cs.valid(state))

    def test_the_migrate_command_builds_a_new_state_and_refuses_to_overwrite(self):
        os.remove(cs.STATE)
        legacy = os.path.join(self.dir, "old.json")
        write(legacy, {"cursor": "1793300000000-0"})
        page = [{"id": "1793300000500-0", "by": "call-bridge", "text": "CALL ASK call-aaaaaaaaaaaa x"}]
        reads = self.reads({"migrate": page})
        self.assertEqual(0, ca.migrate(legacy, reads=reads, records=[], now=lambda: 1793300002.0))
        st = cs.load()
        self.assertEqual(("PENDING", "1793300000500-0"), (st["tasks"]["call-aaaaaaaaaaaa"]["state"], st["seen"]))
        self.assertEqual(2, ca.migrate(legacy, reads=reads, records=[]))

    def test_migrate_refuses_even_an_empty_existing_state(self):
        page = [{"id": "1793300000500-0", "by": "call-bridge", "text": "CALL ASK call-aaaaaaaaaaaa x"}]
        self.assertEqual(2, ca.migrate("nope.json", reads=self.reads({"migrate": page}), records=[]))
        self.assertEqual({}, cs.load()["tasks"])

    def test_the_cli_dispatches_migrate_without_an_ask(self):
        with mock.patch.object(ca, "migrate", lambda path: 0) as m:
            self.assertEqual(0, ca.main(["migrate"]))
        self.assertEqual(1, ca.main(["accept"]))

    def test_an_unanswered_page_writes_nothing(self):
        os.remove(cs.STATE)
        self.assertEqual(2, ca.migrate("nope.json", reads=self.reads({"migrate": None}), records=[]))
        self.assertFalse(os.path.exists(cs.STATE))

    def test_a_replay_over_the_bound_fails_closed(self):
        with self.assertRaises(SystemExit):
            cm.replay([("%d-0" % i, "call-bridge", "x") for i in range(501)], [], {}, 0, lambda t: None)

    def test_the_doorbell_rings_once_per_new_claim_and_only_makes_pending(self):
        import cli_call_consumer as door
        data = {"items": [{"id": "claude-code-cli/%s" % ASK, "status": "claimed"}]}
        self.assertEqual(0, door.main(fetch=lambda: data, sleep=lambda s: None, polls=1))
        self.assertEqual("PENDING", cs.load()["tasks"][ASK]["state"])
        self.assertEqual(0, door.main(fetch=lambda: data, sleep=lambda s: None, polls=1))
        self.task(ASK, state="ACCEPTED")
        self.assertEqual(1, door.main(fetch=lambda: data, sleep=lambda s: None, polls=1))
        self.assertFalse(cs.ring(ASK, "inbox"))
        self.assertEqual("ACCEPTED", cs.load()["tasks"][ASK]["state"])
        self.assertEqual([], self.board.appends)


if __name__ == "__main__":
    unittest.main(verbosity=1)

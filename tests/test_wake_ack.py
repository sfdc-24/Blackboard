"""scripts/wake_ack.py and its wiring in scripts/fleet_doorbell.py: Grok's runner TAKES the call bridge's wake.

What matters (call_bridge.py THE WAKE, CONFIRMED; Aya's source hold on d2d1618): a wake row for grok is ACKed the
moment the doorbell sees it, before the work, with phase=ACK answers=CALLBRIDGE-WAKE-<ask> from grok's own tag; one
ACK per ask (second ring, replay, restart); a ring for another agent or from another tag posts nothing; a failed post
is never retried; and end to end the bridge's In the making card stays up and the ask is never marked "did not
wake". No network: the board append and Redis are fakes. Run: python tests/test_wake_ack.py
"""
import datetime
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "cloud" / "bus-reconciler"))
sys.path.insert(0, str(ROOT / "tests"))

import append                                                            # noqa: E402
import call_bridge as cb                                                 # noqa: E402
import canvas_publish as cp                                              # noqa: E402
import fleet_doorbell as fd                                              # noqa: E402
import signature                                                         # noqa: E402
import test_call_bridge as tcb                                           # noqa: E402  (its fakes only)
import wake_ack as wa                                                    # noqa: E402

NOW = tcb.NOW
LOG = logging.getLogger("test-wake-ack")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False
ASK = "call-0123456789ab"


def board_row(spec, moment):
    """A row spec as append.py lays it on the board."""
    return [spec["row_id"], moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), spec["source_tag"],
            spec.get("target_surface", "ALL"), spec.get("action_type", "APPEND"), spec["payload"],
            spec.get("category", "OPEN"), spec.get("project_tag", ""), spec.get("gist", ""), spec.get("subgist", "")]


def ring_row(ask=ASK, agent="grok", moment=NOW, rering=False, sender="bus-reconciler"):
    spec = cb.wake_row(ask, agent, "workshop", moment)
    if rering:
        spec["row_id"] += cb.RERING_SUFFIX
        spec["payload"] = spec["payload"].replace("|id=%s|" % (cb.WAKE_PREFIX + ask), "|id=%s|" % spec["row_id"], 1)
    spec["source_tag"] = sender
    return board_row(spec, moment)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.posted, self.logged = [], []
        self.clock = lambda: NOW + datetime.timedelta(seconds=2)

    def acker(self, post=None):
        return fd.acker_for("grok", post=post or self.posted.append, path=Path(self.dir) / "acked.json",
                            clock=self.clock, log=self.logged.append)

    def bookmark(self, since):
        path = Path(self.dir) / "bm"
        path.write_text(since.isoformat(), encoding="utf-8")
        return path


class WakeIsAcked(Fixture):
    def test_a_wake_is_acked_within_seconds_and_before_the_ring_is_printed(self):
        order = []
        acker = fd.acker_for("grok", post=lambda spec: order.append(("ack", spec)),
                             path=Path(self.dir) / "acked.json", clock=self.clock, log=self.logged.append)
        started = time.monotonic()
        count = fd.check(lambda since: [ring_row()], "grok", path=self.bookmark(NOW - datetime.timedelta(minutes=1)),
                         out=lambda line: order.append(("print", line)), ack=acker)
        took = time.monotonic() - started
        self.assertEqual(1, count)
        self.assertEqual(["ack", "print"], [kind for kind, _ in order], "taken first, then the work")
        spec = order[0][1]
        self.assertTrue(order[1][1].startswith("RING "))
        self.assertLess(took, 2.0, "in process the ACK is immediate")
        self.assertIn("|text=Claim taken at %s;" % wa.stamp(self.clock()), spec["payload"])
        self.assertEqual("GROK-WAKE-ACK-" + ASK, spec["row_id"])
        self.assertEqual(("grok", "ACK"), (spec["source_tag"], spec["action_type"]))

    def test_the_ack_is_what_the_bridge_reads_as_taking_the_claim(self):
        spec = self.acker()(ring_row())
        payload = spec["payload"]
        self.assertTrue(cb.takes(payload))
        self.assertEqual([ASK], cb.answered_asks(payload))
        self.assertEqual("ACK", cb._field(payload, "phase"))
        self.assertEqual(cb.WAKE_PREFIX + ASK, cb._field(payload, "answers"))
        self.assertIn("grok", cb.ANSWERS_FROM["grok"])

    def test_the_ack_passes_the_board_writer_guards(self):
        spec = self.acker()(ring_row())
        self.assertEqual([], signature.check(spec["source_tag"], spec["payload"]))
        self.assertEqual(set(), append._conflicting_keys(spec["payload"]))
        self.assertTrue(spec["payload"].startswith("BCB|"))
        self.assertLess(len(spec["payload"]), 1400)
        self.assertNotIn("\n", spec["payload"])

    def test_it_carries_no_word_of_the_owner(self):
        row = ring_row()
        row[5] = row[5].replace("|text=", "|said=Port-au-Prince store secret plan|text=")
        spec = self.acker()(row)
        for word in ("Port", "Prince", "secret", "plan"):
            self.assertNotIn(word, json.dumps(spec))

    def test_the_second_ring_alone_is_acked_under_the_ask(self):
        spec = self.acker()(ring_row(rering=True))
        self.assertEqual([ASK], cb.answered_asks(spec["payload"]))
        self.assertIn("|ring=%s%s%s|" % (cb.WAKE_PREFIX, ASK, cb.RERING_SUFFIX), spec["payload"])


class OneAckPerAsk(Fixture):
    def test_a_duplicate_wake_the_second_ring_and_a_restart_post_one_ack(self):
        acker = self.acker()
        self.assertIsNotNone(acker(ring_row()))
        self.assertIsNone(acker(ring_row()))
        self.assertIsNone(acker(ring_row(rering=True)))
        self.assertIsNone(self.acker()(ring_row()), "a restart remembers")
        self.assertEqual(1, len(self.posted))

    def test_the_same_row_seen_by_two_doorbell_passes_is_acked_once(self):
        acker = self.acker()
        rows = [ring_row(), ring_row(rering=True)]
        for _ in range(3):
            fd.check(lambda since: rows, "grok", path=self.bookmark(NOW - datetime.timedelta(minutes=1)),
                     out=lambda line: None, ack=acker)
        self.assertEqual(1, len(self.posted))

    def test_another_ask_gets_its_own_ack(self):
        acker = self.acker()
        acker(ring_row())
        acker(ring_row(ask="call-abcdefabcdef"))
        self.assertEqual(["GROK-WAKE-ACK-" + ASK, "GROK-WAKE-ACK-call-abcdefabcdef"],
                         [s["row_id"] for s in self.posted])

    def test_a_failed_post_is_logged_and_never_retried(self):
        calls = []

        def boom(spec):
            calls.append(spec)
            raise SystemExit(1)
        acker = self.acker(post=boom)
        self.assertIsNone(acker(ring_row()))
        self.assertIsNone(acker(ring_row(rering=True)))
        self.assertEqual(1, len(calls))
        self.assertTrue(any("NOT confirmed" in line and "not retried" in line for line in self.logged))

    def test_an_unreadable_memory_file_is_an_empty_one(self):
        (Path(self.dir) / "acked.json").write_text("{not json", encoding="utf-8")
        self.assertIsNotNone(self.acker()(ring_row()))


class WrongTagIgnored(Fixture):
    def test_a_ring_for_another_agent_is_not_acked(self):
        for agent in ("gemini", "chatgpt-codex-desktop", "claude-code-cli"):
            self.assertIsNone(self.acker()(ring_row(agent=agent)), agent)
        self.assertEqual([], self.posted)

    def test_a_forged_ring_from_another_tag_is_not_acked(self):
        for sender in ("aya", "grok", "gemini", "whatsapp"):
            self.assertIsNone(self.acker()(ring_row(sender=sender)), sender)
        self.assertEqual([], self.posted)

    def test_rows_that_are_not_rings_are_not_acked(self):
        acker = self.acker()
        ring = ring_row()
        not_ask = list(ring)
        not_ask[5] = not_ask[5].replace("|phase=ASK|", "|phase=RESULT|")
        bad_ask = ring_row(ask="call-NOTHEX")
        mismatch = list(ring)
        mismatch[5] = mismatch[5].replace("|ask=%s|" % ASK, "|ask=call-ffffffffffff|")
        plain = ["X", NOW.isoformat(), "aya", "grok", "APPEND", "BCB|v=1|id=AYA-X|phase=ASK|to=grok|check this"]
        for row in (not_ask, bad_ask, mismatch, plain, [], None, ["only-an-id"]):
            self.assertIsNone(acker(row))
        self.assertEqual([], self.posted)

    def test_the_doorbell_does_not_ack_in_peek_or_for_unwired_agents(self):
        acker = self.acker()
        fd.check(lambda since: [ring_row()], "grok", path=self.bookmark(NOW - datetime.timedelta(minutes=1)),
                 peek=True, out=lambda line: None, ack=acker)
        self.assertEqual([], self.posted)
        self.assertIsNone(fd.acker_for("codex"))
        self.assertIsNone(fd.acker_for("gemini"))
        self.assertEqual({"grok": "grok"}, fd.ACK_WIRED)

    def test_the_acks_own_row_never_rings_any_doorbell(self):
        spec = self.acker()(ring_row())
        row = board_row(spec, NOW + datetime.timedelta(seconds=2))
        for agent in fd.AGENTS:
            self.assertFalse(fd.is_for(row, agent), agent)

    def test_the_memory_file_sits_beside_the_bookmark(self):
        os.environ["FLEET_DOORBELL_BOOKMARK_DIR"] = self.dir
        try:
            self.assertEqual(fd.bookmark("grok").parent, fd.acked_path("grok").parent)
            self.assertNotEqual(fd.acked_path("grok"), fd.acked_path("codex"))
        finally:
            del os.environ["FLEET_DOORBELL_BOOKMARK_DIR"]


class EndToEndWithTheBridge(unittest.TestCase):
    """The owner names Grok with a task in the room. The bridge claims it (its In the making card), rings Grok with
    its wake row; Grok's doorbell sees that row and ACKs it through the board; the bridge's settle reads the ACK: the
    wake is acked, the card stays In the making, there is no second ring and never "agent did not wake"."""

    def setUp(self):
        self.conn = tcb.FakeRedis()
        tcb.open_window(self.conn)
        self.rung, self.board = [], []
        self.dir = tempfile.mkdtemp()

    def at(self, moment):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: moment, ring=self.rung.append)

    def run_call(self, ack_after_seconds):
        tcb.turn(self.conn, "Grok, draw the sketch that draws itself.", seconds_ago=10)
        [ask] = self.at(NOW).tick()
        self.assertEqual(1, len(self.rung))
        started = time.monotonic()
        rung_at = NOW
        ack_at = rung_at + datetime.timedelta(seconds=ack_after_seconds)
        acker = fd.acker_for("grok", post=self.board.append, path=Path(self.dir) / "acked.json",
                             clock=lambda: ack_at, log=lambda text: None)
        bm = Path(self.dir) / "bm"
        bm.write_text((NOW - datetime.timedelta(minutes=1)).isoformat(), encoding="utf-8")
        wake = board_row(self.rung[0], rung_at)
        self.assertEqual(1, fd.check(lambda since: [wake], "grok", path=bm, calls_only=True, out=lambda l: None,
                                     ack=acker))
        self.assertEqual(1, len(self.board))
        self.at(ack_at).settle([board_row(self.board[0], ack_at)])
        self.wall = time.monotonic() - started
        return ask

    def wake(self, ask):
        return json.loads(self.conn.get(cb.WAKE_KEY % ask))

    def test_in_the_making_then_ack_accepted_and_never_did_not_wake(self):
        ask = self.run_call(ack_after_seconds=5)
        got = tcb.card(self.conn, "grok", ask)
        self.assertEqual("claimed in the room", got["activity"])
        self.assertTrue(got["title"].startswith("In the making"), got["title"])
        wake = self.wake(ask)
        self.assertEqual(("acked", "GROK-WAKE-ACK-" + ask), (wake["state"], wake["acked_by"]))
        acked = (cb._parse(wake["acked_at"]) - cb._parse(wake["rung_at"])).total_seconds()
        self.assertLessEqual(acked, 5)
        self.assertLess(self.wall, 2.0)
        for later in (cb.ACK_SECONDS, 2 * cb.ACK_SECONDS, 5 * cb.ACK_SECONDS):
            self.at(NOW + datetime.timedelta(seconds=later)).tick()
        self.assertEqual(1, len(self.rung), "no second ring")
        self.assertEqual("acked", self.wake(ask)["state"])
        got = tcb.card(self.conn, "grok", ask)
        self.assertNotEqual(cb.NO_WAKE, got["activity"])
        self.assertTrue(got["title"].startswith("In the making"))
        self.assertIsNotNone(cp.clean_item("grok", ask, json.dumps(got)), "the page shows it")
        print("\nSIMULATED ACK: ring->ACK accepted %.0f s on the simulated clock, %.1f ms in process (wall)" % (
            acked, self.wall * 1000), file=sys.stderr)

    def test_without_the_ack_the_same_call_is_marked_did_not_wake(self):
        tcb.turn(self.conn, "Grok, draw the sketch that draws itself.", seconds_ago=10)
        [ask] = self.at(NOW).tick()
        self.at(NOW + datetime.timedelta(seconds=cb.ACK_SECONDS)).tick()
        self.at(NOW + datetime.timedelta(seconds=2 * cb.ACK_SECONDS)).tick()
        self.assertEqual("no_wake", self.wake(ask)["state"])
        self.assertEqual(cb.NO_WAKE, tcb.card(self.conn, "grok", ask)["activity"])

    def test_the_second_ring_after_the_ack_posts_no_second_ack(self):
        ask = self.run_call(ack_after_seconds=3)
        acker = fd.acker_for("grok", post=self.board.append, path=Path(self.dir) / "acked.json",
                             log=lambda text: None)
        acker(ring_row(ask=ask, rering=True))
        self.assertEqual(1, len(self.board))

    def test_the_ack_then_the_result_turns_the_card_done(self):
        ask = self.run_call(ack_after_seconds=4)
        done = tcb.row("grok", "BCB|v=1|id=GROK-DONE|phase=RESULT|from=grok|answers=%s|text=sketch up" % ask)
        self.assertEqual([ask], self.at(NOW + datetime.timedelta(seconds=120)).settle([done]))
        self.assertEqual("done", tcb.card(self.conn, "grok", ask)["status"])


if __name__ == "__main__":
    unittest.main()

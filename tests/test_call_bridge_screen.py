"""R7b slow mode: he says it on the call, Grokbot clicks it (scripts/call_bridge.py, SCREEN).

Pins the classifier, the three routes (refused, standing look-only go, read-back and go-ahead), "stop", and that a
screen step is Grok's alone. Synthetic sentences only; the fixtures are test_call_bridge's. No network.
Run: python tests/test_call_bridge_screen.py
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_call_bridge import cb, cp, life, FakeRedis, LOG, NOW, ROOM, STREAM, at, open_window, turn   # noqa: E402

OWNER = "own_fixture_owner"
ROOT = Path(__file__).resolve().parents[1]
# Codex's HOLD on 0a4c516f: these said to Grok are no screen_ask, yet must never be a plain claim.
SENSITIVE = ("Greg, delete that record.", "Greg, open Named Credentials.", "Greg, put the password in the field.")


class Classifier(unittest.TestCase):
    def test_screen_steps_are_found_and_other_work_is_not(self):
        for said in ("Greg, click New on the Integration Procedures tab.", "Greg, scroll down.",
                     "Greg, open the Setup menu.", "Greg, go to the FlexCards list view.", "Greg, hover over the gear",
                     "Greg, double-click the first row", "Greg, select the Active checkbox"):
            self.assertTrue(cb.screen_ask(said), said)
        for said in ("Greg, open a PR for the logo.", "Greg, draft the sample queue flow.", "Greg, show me a picture",
                     "Greg, select a logo", "Greg, check the build", "Greg, make it shorter"):
            self.assertFalse(cb.screen_ask(said), said)

    def test_kinds(self):
        table = {
            "Greg, scroll down the page.": "look",
            "Greg, open the Setup menu.": "look",
            "Greg, go to the OmniScripts tab.": "look",
            "Greg, click New on the Integration Procedures tab.": "change",
            "Greg, open the record and click Edit.": "change",
            "Greg, type Acme into the name field.": "change",
            "Greg, scroll down and select the second row": "change",
            "Greg, click Activate on the FlexCard.": "own-go",
            "Greg, delete that record.": "own-go",
            "Greg, click Deploy.": "own-go",
            "Greg, open the profile page and change the permissions": "own-go",
            "Greg, type the password into the field.": "refused",
            "Greg, enter the verification code.": "refused",
            "Greg, click Log In.": "refused",
            "Greg, open Named Credentials.": "refused",
            "Greg, paste the API key into the field": "refused",
        }
        for said, kind in table.items():
            self.assertEqual(kind, cb.screen_kind(said), said)

    def test_the_standing_go_and_the_stop_are_whole_turns(self):
        def f(said, actor=OWNER):
            return {"kind": "turn.started", "actor_id": actor, "utterance": said}
        self.assertTrue(cb.drive_go(f("Greg, you can drive.")))
        self.assertTrue(cb.drive_go(f("Okay Greg, you can drive now")))
        self.assertFalse(cb.drive_go(f("You can drive.")), "Grok is named")
        self.assertFalse(cb.drive_go(f("Greg, you can drive the demo and delete the record")))
        self.assertFalse(cb.drive_go(f("Greg, you can drive.", actor="chair")))
        for said in ("Stop.", "Greg, stop!", "Enough.", "Okay, that's enough", "stop please"):
            self.assertTrue(cb.screen_stop(f(said)), said)
        for said in ("Greg, stop scrolling.", "Greg, don't click that button", "Wait, do not click Save"):
            self.assertTrue(cb.screen_stop(f(said)), said)
            self.assertFalse(cb.screen_turn(f(said)), said + ": a stop is never a step")
        for said in ("Don't stop now, keep going", "That is not enough", "stop the music"):
            self.assertFalse(cb.screen_stop(f(said)), said)
        # What screen_bound holds, said to Grok with a stop word, is a stop too; to another voice it is not.
        self.assertTrue(cb.screen_stop(f("Greg, don't delete that record")))
        self.assertTrue(cb.screen_stop(f("Greg, never type the password")))
        self.assertFalse(cb.screen_stop(f("Claude, don't remove the logo from the card")))

    def test_sensitive_words_to_grok_take_the_screen_path(self):
        def f(said):
            return {"kind": "turn.started", "actor_id": OWNER, "utterance": said}
        for said in SENSITIVE + ("Greg, deploy it.", "Greg, send the email now"):
            self.assertTrue(cb.screen_bound(said), said)
            self.assertTrue(cb.screen_turn(f(said)), said)
        self.assertEqual("own-go", cb.screen_kind(SENSITIVE[0]))
        self.assertEqual(["refused", "refused"], [cb.screen_kind(x) for x in SENSITIVE[1:]])
        for said in ("Greg, write a joke on a card.", "Greg, research the sample queue", "Greg, make it shorter"):
            self.assertFalse(cb.screen_bound(said), said)
            self.assertFalse(cb.screen_turn(f(said)), said)
        # Beside other voices a sensitive ask goes on to them; Grok's share is refused at the claim.
        self.assertFalse(cb.screen_turn(f("Greg and Claude, write the summary and send it to the board.")))
        self.assertTrue(cb.screen_turn(f("Claude and Greg, click New on the Integration Procedures tab.")))
        self.assertFalse(cb.screen_turn(f("Claude, delete the old card.")), "not Grok's")


class ScreenSteps(unittest.TestCase):
    def setUp(self):
        saved = cb.SCREEN
        cb.SCREEN = True
        self.addCleanup(setattr, cb, "SCREEN", saved)
        self.conn = FakeRedis()
        open_window(self.conn)

    def b(self, readback=True, owners=(OWNER,)):
        return cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW, readback=readback, owners=list(owners))

    def cards(self, agent="grok"):
        return {k: json.loads(v) for k, v in self.conn.hgetall(cp.PARTITION % ("workshop", agent)).items()}

    def inbox(self, agent="grok"):
        return [fields for _, fields in self.conn.streams.get("fleet:inbox:%s" % agent, [])]

    def heard(self, entry, seconds_ago, agents=("grok",)):
        return self.conn.xadd(STREAM, {
            "kind": cb.READBACK_SPOKEN, "actor_id": "chair", "room_id": ROOM, "session_epoch": "fixture-epoch",
            "control_sequence": "4", "proj_n": "2", "readback_id": cb.readback_id(ROOM, entry, list(agents))},
            entry_id=at(seconds_ago))

    def test_switched_off_click_is_still_not_a_task(self):
        cb.SCREEN = False
        turn(self.conn, "Greg, click New on the Integration Procedures tab.", seconds_ago=60)
        self.assertEqual([], self.b().tick())
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.assertEqual([], self.inbox())

    def test_a_changing_click_waits_for_the_read_back_and_his_go(self):
        entry = turn(self.conn, "Greg, click New on the Integration Procedures tab.", seconds_ago=60)
        self.assertEqual([], self.b().tick())
        self.assertEqual(["grok"], json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["agents"])
        self.assertEqual([], self.inbox(), "nothing reaches Grokbot before his go-ahead")
        self.heard(entry, 40)
        turn(self.conn, "Greg, go ahead.", seconds_ago=30)
        ask = cb.ask_id(ROOM, entry, "grok")
        self.assertEqual([ask], self.b().tick())
        [sent] = self.inbox()
        self.assertEqual(("screen-action", "change", ask), (sent["kind"], sent["screen_kind"], sent["ask"]))
        self.assertIn("click New on the Integration Procedures tab", sent["text"])
        self.assertIn("HARD LIMITS", sent["text"])
        self.assertIn("ONE CHANGING STEP", sent["text"])
        self.assertIn(cb.SCREEN_HOSTS[0], sent["text"])
        self.assertIn("sign in himself", sent["text"])
        self.assertNotIn("standing_go", sent)
        self.assertEqual("OPEN", json.loads(self.conn.get(life.TASK_KEY % ask))["state"])

    def test_an_irreversible_step_says_so(self):
        entry = turn(self.conn, "Greg, click Activate on the FlexCard.", seconds_ago=60)
        self.b().tick()
        self.heard(entry, 40)
        turn(self.conn, "Greg, go ahead.", seconds_ago=30)
        self.b().tick()
        [sent] = self.inbox()
        self.assertEqual("own-go", sent["screen_kind"])
        self.assertIn("ONE IRREVERSIBLE STEP", sent["text"])

    def test_a_credential_is_refused_and_never_proposed_or_rung(self):
        entry = turn(self.conn, "Greg, type the password into the sign in field.", seconds_ago=60)
        self.assertEqual([], self.b().tick())
        got = self.cards()[cb.proposal_id(ROOM, entry, "grok")]
        self.assertEqual(cb.NO_CREDENTIALS, got["title"][:len(cb.NO_CREDENTIALS)])
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        # The voice then says it cannot: still nothing is proposed, for Grok or anyone.
        self.conn.xadd(cb.SIDE_KEY % NOW.strftime("%Y%m%d"),
                       {"by": "chair", "voice": "grok", "room": ROOM, "text": "I can't type passwords."},
                       entry_id=at(50))
        turn(self.conn, "Greg, go ahead.", seconds_ago=30)
        self.assertEqual([], self.b().tick())
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.assertEqual([], self.inbox())

    def test_a_look_without_the_standing_go_is_read_back(self):
        turn(self.conn, "Greg, scroll down the page.", seconds_ago=60)
        self.assertEqual([], self.b().tick())
        self.assertIsNotNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.assertEqual([], self.inbox())

    def test_the_standing_go_runs_looks_only(self):
        drive = turn(self.conn, "Greg, you can drive.", seconds_ago=60)
        look = turn(self.conn, "Greg, scroll down the page.", seconds_ago=50)
        ask = cb.ask_id(ROOM, look, "grok")
        self.assertEqual([ask], self.b().tick())
        [sent] = self.inbox()
        self.assertEqual(("look", drive), (sent["screen_kind"], sent["standing_go"]))
        self.assertIn("LOOK ONLY", sent["text"])
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM), "no read-back for a look on the standing go")
        click = turn(self.conn, "Greg, click New.", seconds_ago=40)
        self.assertEqual([], self.b().tick(), "a changing step is never on the standing go")
        self.assertEqual(click, json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["entry"])
        self.assertEqual(1, len(self.inbox()))

    def test_the_standing_go_is_the_owners_and_this_sessions(self):
        turn(self.conn, "Greg, you can drive.", actor="own_someone_else", seconds_ago=60)
        turn(self.conn, "Greg, scroll down the page.", actor="own_someone_else", seconds_ago=50)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(), "not a listed owner: no standing go")
        self.assertIsNone(self.conn.get(cb.DRIVE_KEY % ROOM), "a guest's go is never held")
        self.conn.delete(cb.PROPOSAL_KEY % ROOM)
        turn(self.conn, "Greg, you can drive.", seconds_ago=40, session_epoch="another-epoch")
        turn(self.conn, "Greg, go to the OmniScripts tab.", seconds_ago=30)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox(), "a go from another chair session does not cover this one")

    def test_stop_revokes_the_go_and_stops_grokbot_once(self):
        turn(self.conn, "Greg, you can drive.", seconds_ago=60)
        turn(self.conn, "Greg, scroll down the page.", seconds_ago=55)
        self.b().tick()
        click = turn(self.conn, "Greg, click New.", seconds_ago=50)
        self.b().tick()
        stop = turn(self.conn, "Enough.", reason="continuity", seconds_ago=40)
        self.b().tick()
        stops = [f for f in self.inbox() if f.get("kind") == "screen-stop"]
        self.assertEqual(1, len(stops))
        self.assertEqual(stop, stops[0]["entry"])
        self.assertTrue(json.loads(self.conn.get(cb.DRIVE_KEY % ROOM))["stopped"])
        got = self.cards()[cb.proposal_id(ROOM, click, "grok")]
        self.assertEqual("done", got["status"], "the waiting click is closed")
        self.b().tick()
        self.assertEqual(1, len([f for f in self.inbox() if f.get("kind") == "screen-stop"]), "once per turn")
        turn(self.conn, "Greg, scroll down again.", seconds_ago=30)
        self.b().tick()
        self.assertEqual(1, len([f for f in self.inbox() if f.get("kind") == "screen-action"]),
                         "after stop a look waits for the read-back again")

    def test_stop_scrolling_on_the_standing_go_is_a_stop_not_a_scroll(self):
        turn(self.conn, "Greg, you can drive.", seconds_ago=60)
        turn(self.conn, "Greg, stop scrolling.", seconds_ago=50)
        self.assertEqual([], self.b().tick())
        self.assertEqual(["screen-stop"], [f.get("kind") for f in self.inbox()])

    def test_a_replayed_older_go_does_not_undo_a_stop(self):
        drive = turn(self.conn, "Greg, you can drive.", seconds_ago=60)
        stop = turn(self.conn, "Stop.", seconds_ago=50)
        self.b().tick()
        self.conn.delete(cb.CURSOR_KEY % ROOM)                 # a lost cursor: both turns are read again
        self.b().tick()
        held = json.loads(self.conn.get(cb.DRIVE_KEY % ROOM))
        self.assertEqual((stop, True), (held["entry"], held.get("stopped")))
        self.assertNotEqual(drive, held["entry"])

    def test_a_replayed_stop_stops_grokbot_once(self):
        turn(self.conn, "Greg, you can drive.", seconds_ago=60)
        turn(self.conn, "Greg, scroll down the page.", seconds_ago=55)
        turn(self.conn, "Stop.", seconds_ago=50)
        self.b().tick()
        self.conn.delete(cb.CURSOR_KEY % ROOM)                 # a lost cursor: every turn is read again
        self.b().tick()
        self.assertEqual(1, len([f for f in self.inbox() if f.get("kind") == "screen-stop"]))
        self.assertEqual(1, len([f for f in self.inbox() if f.get("kind") == "screen-action"]))

    def test_an_older_go_read_after_a_stop_grants_nothing(self):
        stop = at(40)
        self.conn.set(cb.DRIVE_KEY % ROOM, json.dumps({"entry": stop, "stopped": True, "actor": OWNER,
                                                       "epoch": "fixture-epoch"}))
        turn(self.conn, "Greg, you can drive.", seconds_ago=60)
        self.b().tick()
        self.assertEqual(stop, json.loads(self.conn.get(cb.DRIVE_KEY % ROOM))["entry"])

    def test_a_stopped_go_covers_no_look(self):
        self.conn.set(cb.DRIVE_KEY % ROOM, json.dumps({"entry": at(60), "stopped": True, "actor": OWNER,
                                                       "epoch": "fixture-epoch"}))
        turn(self.conn, "Greg, scroll down the page.", seconds_ago=50)
        self.assertEqual([], self.b().tick())
        self.assertEqual([], self.inbox())

    def test_a_click_for_another_voice_is_not_groks(self):
        turn(self.conn, "Claude, click New on the Integration Procedures tab.", seconds_ago=60)
        self.b().tick()
        self.assertEqual({}, self.cards())
        self.assertEqual([], self.inbox())

    def test_a_confirmed_step_is_a_new_claim_even_with_a_living_card(self):
        card = turn(self.conn, "Greg, write a joke on a card.", seconds_ago=200)
        self.b().tick()
        self.heard(card, 190)
        turn(self.conn, "Greg, go ahead.", seconds_ago=185)
        [first] = self.b().tick()
        info = json.loads(self.conn.hget(life.OPEN_KEY, first))
        info["field"] = first + "-work"                        # its worker put the card up: it is living
        self.conn.hset(life.OPEN_KEY, first, json.dumps(info))
        step = turn(self.conn, "Greg, click New on the Integration Procedures tab.", seconds_ago=60)
        self.b().tick()
        self.heard(step, 40)
        turn(self.conn, "Greg, go ahead.", seconds_ago=30)
        self.assertEqual([cb.ask_id(ROOM, step, "grok")], self.b().tick())
        self.assertEqual("screen-action", self.inbox()[-1].get("kind"))
        self.assertEqual([], getattr(self.conn, "lists", {}).get(cb.REVISE_KEY % first, []))

    def test_a_guests_screen_step_is_never_proposed_carded_put_in_an_inbox_or_rung(self):
        """Codex's HOLD on 921c35c6: a guest's step became a proposal the same guest could confirm."""
        rings = []
        b = cb.Bridge(self.conn, LOG, lambda: True, clock=lambda: NOW, readback=True, owners=[OWNER],
                      ring=rings.append)
        guest = "own_someone_else"
        for said in ("Greg, click New on the Integration Procedures tab.", "Greg, click Activate on the FlexCard.",
                     "Greg, scroll down the page."):
            self.conn.delete(cb.CURSOR_KEY % ROOM)
            entry = turn(self.conn, said, actor=guest, seconds_ago=60)
            self.heard(entry, 40)
            turn(self.conn, "Greg, go ahead.", actor=guest, seconds_ago=30)
            self.assertEqual([], b.tick(), said)
            self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM), said)
            self.assertEqual({}, self.cards(), said)
            self.assertEqual([], self.inbox(), said)
            self.assertEqual({}, self.conn.hgetall(cb.RING_KEY), said)
            self.assertEqual({}, self.conn.hgetall(life.OPEN_KEY), said)
        self.assertEqual([], rings)

    def test_a_guests_proposal_however_it_came_is_closed_not_claimed(self):
        guest = "own_someone_else"
        entry = turn(self.conn, "Greg, click New on the Integration Procedures tab.", actor=guest, seconds_ago=60)
        self.conn.set(cb.CURSOR_KEY % ROOM, entry)
        b = self.b()
        b._keep(ROOM, {"entry": entry, "said": "Greg, click New on the Integration Procedures tab.",
                       "agents": ["grok"], "actor": guest, "epoch": "fixture-epoch"})
        self.heard(entry, 40)
        turn(self.conn, "Greg, go ahead.", actor=guest, seconds_ago=30)
        self.assertEqual([], b.tick())
        self.assertEqual([], self.inbox())
        self.assertEqual({}, self.conn.hgetall(life.OPEN_KEY))
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))

    def test_a_standing_go_held_for_a_guest_covers_nothing(self):
        guest = "own_someone_else"
        self.conn.set(cb.DRIVE_KEY % ROOM, json.dumps({"entry": at(60), "actor": guest, "epoch": "fixture-epoch"}))
        self.assertEqual("", self.b()._driving(ROOM, guest, "fixture-epoch", at(10)))
        self.conn.set(cb.DRIVE_KEY % ROOM, json.dumps({"entry": at(60), "actor": OWNER, "epoch": "fixture-epoch"}))
        self.assertEqual(at(60), self.b()._driving(ROOM, OWNER, "fixture-epoch", at(10)))

    def test_a_screen_step_is_groks_alone(self):
        turn(self.conn, "Claude and Greg, click New on the Integration Procedures tab.", seconds_ago=60)
        self.b().tick()
        self.assertEqual(["grok"], json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["agents"])
        # Another voice saying it cannot click does not join it.
        self.conn.xadd(cb.SIDE_KEY % NOW.strftime("%Y%m%d"),
                       {"by": "chair", "voice": "claude", "room": ROOM, "text": "I can't click in Salesforce."},
                       entry_id=at(50))
        self.b().tick()
        self.assertEqual(["grok"], json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["agents"])
        self.assertEqual({}, self.cards("claude-code-cli"))

    def test_paused_a_screen_step_is_refused(self):
        """CALL_BRIDGE_READBACK=off pauses the read-back: a screen step is refused, in either mode (fail closed)."""
        for readback in (True, False):
            self.conn = FakeRedis()
            open_window(self.conn)
            with mock.patch.object(cb, "READBACK_OFF", True):
                b = self.b(readback=readback)
            self.assertTrue(b.paused, readback)
            entry = turn(self.conn, "Greg, click New.", seconds_ago=60)
            self.assertEqual([], b.tick())
            got = self.cards()[cb.proposal_id(ROOM, entry, "grok")]
            self.assertTrue(got["title"].startswith(cb.NO_SCREEN_READBACK), readback)
            self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
            self.assertEqual([], self.inbox())
            turn(self.conn, "Greg, you can drive.", seconds_ago=50)
            b.tick()
            self.assertIsNone(self.conn.get(cb.DRIVE_KEY % ROOM), "paused: no standing go is held")

    def test_direct_start_reads_back_screen_steps_alone(self):
        """Codex's HOLD on 0a4c516f: production is direct start (READBACK False). A screen step still needs the
        read-back and his go; every other ask claims at once, as before, even while a screen step waits."""
        b = self.b(readback=False)
        self.assertTrue(b.proposing())
        step = turn(self.conn, "Greg, click New on the Integration Procedures tab.", seconds_ago=60)
        self.assertEqual([], b.tick())
        self.assertEqual(step, json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["entry"])
        self.assertTrue(self.conn.hgetall(cb.READBACK_KEY % ROOM).get("id"), "the chair is asked to read it back")
        other = turn(self.conn, "Claude, write a joke on a card.", seconds_ago=55)
        self.assertEqual([cb.ask_id(ROOM, other, "claude-code-cli")], b.tick(), "an ordinary ask claims at once")
        self.assertEqual(step, json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["entry"], "the step still waits")
        turn(self.conn, "Greg, go ahead.", seconds_ago=50)
        self.assertEqual([], b.tick(), "before the read-back, his go starts nothing")
        self.heard(step, 45)
        turn(self.conn, "Greg, go ahead.", seconds_ago=40)
        ask = cb.ask_id(ROOM, step, "grok")
        self.assertEqual([ask], b.tick())
        [sent] = self.inbox()
        self.assertEqual(("screen-action", "change"), (sent["kind"], sent["screen_kind"]))
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.assertEqual({}, self.conn.hgetall(cb.READBACK_KEY % ROOM))
        self.assertNotIn(cb.proposal_id(ROOM, step, "grok"), self.cards(), "claimed: its proposal card is gone")

    def test_direct_start_a_screen_step_with_no_go_in_time_closes(self):
        step = turn(self.conn, "Greg, click New.", seconds_ago=cb.READBACK_SECONDS + 30)
        b = self.b(readback=False)
        b._screen(ROOM, step, {"kind": "turn.started", "actor_id": OWNER, "session_epoch": "fixture-epoch",
                                "utterance": "Greg, click New."}, "Greg, click New.", cb.event_time(step))
        self.assertIsNotNone(self.conn.get(cb.PROPOSAL_KEY % ROOM))
        self.conn.set(cb.CURSOR_KEY % ROOM, step)
        self.assertEqual([], b.tick())
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM), "a screen step's proposal runs out in direct start")
        self.assertEqual("done", self.cards()[cb.proposal_id(ROOM, step, "grok")]["status"])

    def test_sensitive_asks_to_grok_never_claim_plain_in_either_mode(self):
        """Codex's HOLD on 0a4c516f, end to end in both modes: a delete is an own-go screen step; a credential is
        refused; neither is ever a plain claim."""
        for readback in (True, False):
            for said in SENSITIVE:
                self.conn = FakeRedis()
                open_window(self.conn)
                b = self.b(readback=readback)
                entry = turn(self.conn, said, seconds_ago=60)
                self.assertEqual([], b.tick(), (readback, said))
                self.assertEqual([], self.inbox(), (readback, said))
                self.assertEqual({}, self.conn.hgetall(life.OPEN_KEY), (readback, said))
                if cb.screen_kind(said) == "refused":
                    self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM), (readback, said))
                    got = self.cards()[cb.proposal_id(ROOM, entry, "grok")]
                    self.assertTrue(got["title"].startswith(cb.NO_CREDENTIALS), (readback, said))
                    continue
                self.heard(entry, 40)
                turn(self.conn, "Greg, go ahead.", seconds_ago=30)
                self.assertEqual([cb.ask_id(ROOM, entry, "grok")], b.tick(), (readback, said))
                [sent] = self.inbox()
                self.assertEqual(("screen-action", "own-go"), (sent["kind"], sent["screen_kind"]), (readback, said))

    def test_said_to_grok_on_the_floor_a_delete_is_refused_not_claimed(self):
        """Direct start gives an unnamed follow-up to the voice holding the floor: never so for what screen_bound
        holds. It is refused for Grok, visibly."""
        b = self.b(readback=False)
        entry = turn(self.conn, "Delete that record.", reason="continuity", seconds_ago=20, turn_id="t-2")
        turn(self.conn, None, reason="", actor="grok", seconds_ago=15, kind="floor.granted", turn_id="t-2")
        self.assertEqual([], b.tick())
        self.assertEqual([], self.inbox())
        self.assertEqual({}, self.conn.hgetall(life.OPEN_KEY))
        self.assertTrue(self.cards()[cb.proposal_id(ROOM, entry, "grok")]["title"].startswith(cb.NOT_PLAIN))
        # His "do it" for it afterwards is no way round.
        turn(self.conn, "Do it.", seconds_ago=10)
        self.assertEqual([], b.tick())
        self.assertEqual([], self.inbox())

    def test_beside_another_voice_grok_never_takes_it_plain(self):
        """Two voices named make one team card, hosted by the first named. Hosted by another voice it goes on;
        hosted by Grok it is refused for Grok, visibly, and nothing is claimed."""
        b = self.b(readback=False)
        entry = turn(self.conn, "Claude and Greg, write the summary and send it to the board.", seconds_ago=30)
        self.assertEqual([cb.ask_id(ROOM, entry, "claude-code-cli")], b.tick())
        self.assertEqual([], self.inbox())
        entry = turn(self.conn, "Greg and Claude, write the summary and send it to the board.", seconds_ago=20)
        self.assertEqual([], b.tick())
        self.assertEqual([], self.inbox())
        self.assertTrue(self.cards()[cb.proposal_id(ROOM, entry, "grok")]["title"].startswith(cb.NOT_PLAIN))

    def test_a_voices_i_cant_never_claims_grok_plain(self):
        """The legacy refusal path (direct start) claims for the voice that said it cannot: never Grok, for this."""
        b = self.b(readback=False)
        turn(self.conn, "Delete that record.", reason="everyone", seconds_ago=20)
        self.conn.xadd(cb.SIDE_KEY % NOW.strftime("%Y%m%d"),
                       {"by": "chair", "voice": "grok", "room": ROOM, "text": "I can't delete records."},
                       entry_id=at(15))
        self.assertEqual([], b.tick())
        self.assertEqual([], self.inbox())
        self.assertEqual({}, self.conn.hgetall(life.OPEN_KEY))

    def test_a_voices_i_cant_never_proposes_grok_plain_or_joins_a_screen_step(self):
        """Read-back mode: Grok's "I can't" never proposes Grok plain for an unnamed delete, and another voice's
        "I can't" never joins a screen step that is no screen_ask ("Greg, delete that record")."""
        side = cb.SIDE_KEY % NOW.strftime("%Y%m%d")
        b = self.b(readback=True)
        turn(self.conn, "Delete that record.", reason="everyone", seconds_ago=60)
        self.conn.xadd(side, {"by": "chair", "voice": "grok", "room": ROOM, "text": "I can't delete records."},
                       entry_id=at(55))
        self.assertEqual([], b.tick())
        self.assertIsNone(self.conn.get(cb.PROPOSAL_KEY % ROOM), "Grok is never proposed plain for it")
        turn(self.conn, "Greg, delete that record.", seconds_ago=40)
        b.tick()
        self.assertEqual(["grok"], json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["agents"])
        self.conn.xadd(side, {"by": "chair", "voice": "claude", "room": ROOM, "text": "I can't delete records."},
                       entry_id=at(35))
        b.tick()
        self.assertEqual(["grok"], json.loads(self.conn.get(cb.PROPOSAL_KEY % ROOM))["agents"])

    def test_a_pending_asks_do_it_is_never_a_screen_go(self):
        """Direct start's "do it" confirms a pending ask, which was never read back: never a screen step, even if
        the pending record names Grok, a screen step and a listed owner."""
        b = self.b(readback=False)
        entry = turn(self.conn, "Greg, click New.", seconds_ago=60)
        self.conn.set(cb.CURSOR_KEY % ROOM, entry)
        self.conn.set(cb.PENDING_KEY % ROOM, json.dumps({
            "entry": entry, "said": "Greg, click New.", "agents": ["grok"], "named": True,
            "actor": OWNER, "epoch": "fixture-epoch"}), ex=cb.PENDING_SECONDS)
        turn(self.conn, "Do it.", seconds_ago=30)
        self.assertEqual([], b.tick())
        self.assertEqual([], self.inbox())
        self.assertEqual({}, self.conn.hgetall(life.OPEN_KEY))
        self.assertTrue(self.cards()[cb.proposal_id(ROOM, entry, "grok")]["title"].startswith(cb.NOT_PLAIN),
                        "refused as a plain claim, visibly")

    def test_the_last_gate_holds_a_plain_grok_claim(self):
        entry = turn(self.conn, "Greg, write a joke.", seconds_ago=60)
        b = self.b(readback=False)
        for said in SENSITIVE:
            self.assertIsNone(b._claim(ROOM, entry, "grok", said, NOW), said)
        self.assertEqual([], self.inbox())
        self.assertIsNotNone(b._claim(ROOM, entry, "grok", "Greg, write a joke.", NOW), "other work still claims")

    def test_a_claim_is_never_made_for_a_refused_kind_or_another_agent(self):
        entry = turn(self.conn, "Greg, click New.", seconds_ago=60)
        b = self.b()
        self.assertIsNone(b._claim(ROOM, entry, "grok", "x", NOW, screen="refused"))
        self.assertIsNone(b._claim(ROOM, entry, "claude-code-cli", "x", NOW, screen="look"))
        self.assertEqual([], self.inbox() + self.inbox("claude-code-cli"))


class Signal(unittest.TestCase):
    """The chair's promise follows the bridge (Codex's HOLD on conference 37dc359b): SCREEN_SIGNAL_KEY."""

    def setUp(self):
        saved = cb.SCREEN
        cb.SCREEN = True
        self.addCleanup(setattr, cb, "SCREEN", saved)
        self.conn = FakeRedis()
        open_window(self.conn)
        self.key = cb.SCREEN_SIGNAL_KEY % ROOM

    def b(self, owners=(OWNER,), holds=True):
        return cb.Bridge(self.conn, LOG, lambda: holds, clock=lambda: NOW, owners=list(owners))

    def test_on_while_the_bridge_proposes_screen_steps(self):
        turn(self.conn, "Hello everyone.", seconds_ago=30)
        self.b().tick()
        self.assertEqual("on", self.conn.get(self.key))
        self.assertEqual(cb.SCREEN_SIGNAL_SECONDS, self.conn.ttls[self.key])
        self.assertTrue(self.key.startswith("v1:conf:"), "the room's own key space, which the chair reads")

    def test_never_on_switched_off_with_no_owner_paused_or_without_the_lease(self):
        turn(self.conn, "Hello everyone.", seconds_ago=30)
        cb.SCREEN = False
        self.b().tick()
        self.assertIsNone(self.conn.get(self.key))
        cb.SCREEN = True
        self.b(holds=False).tick()
        self.assertIsNone(self.conn.get(self.key))
        self.b(holds=False)._screen_signal(ROOM, at(10), NOW)
        self.assertIsNone(self.conn.get(self.key), "the lease lost mid-pass: not written")
        self.conn.set(self.key, "on", ex=cb.SCREEN_SIGNAL_SECONDS)
        self.b(owners=()).tick()
        self.assertIsNone(self.conn.get(self.key), "no listed owner: no screen step can start")
        self.conn.set(self.key, "on", ex=cb.SCREEN_SIGNAL_SECONDS)
        with mock.patch.object(cb, "READBACK_OFF", True):
            paused = self.b()
        paused.tick()
        self.assertIsNone(self.conn.get(self.key), "paused: taken away at once")

    def test_a_room_long_quiet_gets_none(self):
        old = turn(self.conn, "Hello everyone.", seconds_ago=cb.SCREEN_LIVE_SECONDS + 60)
        self.conn.set(cb.CURSOR_KEY % ROOM, old)
        self.b().tick()
        self.assertIsNone(self.conn.get(self.key))


class StartupPath(unittest.TestCase):
    """Codex's HOLD on 0a4c516f: the production start (serve_requests: start() with no read-back override) with
    CALL_BRIDGE_SCREEN=on must read screen steps back, and CALL_BRIDGE_READBACK=off must refuse them."""

    def load(self, environ):
        spec = importlib.util.spec_from_file_location("call_bridge_startup", ROOT / "scripts" / "call_bridge.py")
        fresh = importlib.util.module_from_spec(spec)
        with mock.patch.dict("os.environ", environ):
            spec.loader.exec_module(fresh)
        return fresh                     # READBACK left as the code has it: direct start

    def started(self, fresh):
        conn = FakeRedis()
        open_window(conn)
        with mock.patch.dict("os.environ", {fresh.OWNERS_ENV: OWNER}):
            bridge = fresh.start(conn, LOG, lambda: False)
        bridge.stop.set()
        bridge.thread.join(10)
        bridge.holds, bridge.clock = (lambda: True), (lambda: NOW)
        return conn, bridge

    def test_screen_on_the_production_start_reads_screen_steps_back(self):
        fresh = self.load({"CALL_BRIDGE_SCREEN": "on"})
        self.assertFalse(fresh.READBACK, "production is direct start")
        conn, bridge = self.started(fresh)
        self.assertEqual((False, True, False), (bridge.readback, bridge.proposing(), bridge.paused))
        step = turn(conn, "Greg, click New on the Integration Procedures tab.", seconds_ago=60)
        self.assertEqual([], bridge.tick())
        self.assertIsNotNone(conn.get(fresh.PROPOSAL_KEY % ROOM))
        conn.xadd(STREAM, {"kind": fresh.READBACK_SPOKEN, "actor_id": "chair", "room_id": ROOM,
                           "session_epoch": "fixture-epoch", "control_sequence": "4", "proj_n": "2",
                           "readback_id": fresh.readback_id(ROOM, step, ["grok"])}, entry_id=at(40))
        turn(conn, "Greg, go ahead.", seconds_ago=30)
        self.assertEqual([fresh.ask_id(ROOM, step, "grok")], bridge.tick())
        [(_, sent)] = conn.streams["fleet:inbox:grok"]
        self.assertEqual("screen-action", sent["kind"])

    def test_screen_on_and_the_read_back_off_refuses_screen_steps_and_claims_the_rest(self):
        fresh = self.load({"CALL_BRIDGE_SCREEN": "on", "CALL_BRIDGE_READBACK": "off"})
        conn, bridge = self.started(fresh)
        self.assertTrue(bridge.paused)
        turn(conn, "Greg, click New.", seconds_ago=60)
        other = turn(conn, "Claude, write a joke on a card.", seconds_ago=50)
        self.assertEqual([fresh.ask_id(ROOM, other, "claude-code-cli")], bridge.tick())
        self.assertNotIn("fleet:inbox:grok", conn.streams)
        self.assertIsNone(conn.get(fresh.PROPOSAL_KEY % ROOM))

    def test_screen_off_nothing_proposes(self):
        fresh = self.load({"CALL_BRIDGE_SCREEN": "off"})
        conn, bridge = self.started(fresh)
        self.assertFalse(bridge.proposing())
        turn(conn, "Greg, click New.", seconds_ago=60)
        bridge.tick()
        self.assertIsNone(conn.get(fresh.PROPOSAL_KEY % ROOM))
        self.assertIsNone(conn.get(fresh.SCREEN_SIGNAL_KEY % ROOM))


if __name__ == "__main__":
    unittest.main()

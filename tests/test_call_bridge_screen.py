"""R7b slow mode: he says it on the call, Grokbot clicks it (scripts/call_bridge.py, SCREEN).

Pins the classifier, the three routes (refused, standing look-only go, read-back and go-ahead), "stop", and that a
screen step is Grok's alone. Synthetic sentences only; the fixtures are test_call_bridge's. No network.
Run: python tests/test_call_bridge_screen.py
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_call_bridge import cb, cp, life, FakeRedis, LOG, NOW, ROOM, STREAM, at, open_window, turn   # noqa: E402

OWNER = "own_fixture_owner"


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

    def test_without_the_read_back_a_screen_step_is_refused(self):
        entry = turn(self.conn, "Greg, click New.", seconds_ago=60)
        self.assertEqual([], self.b(readback=False).tick())
        got = self.cards()[cb.proposal_id(ROOM, entry, "grok")]
        self.assertTrue(got["title"].startswith(cb.NO_SCREEN_READBACK))
        self.assertEqual([], self.inbox())

    def test_a_claim_is_never_made_for_a_refused_kind_or_another_agent(self):
        entry = turn(self.conn, "Greg, click New.", seconds_ago=60)
        b = self.b()
        self.assertIsNone(b._claim(ROOM, entry, "grok", "x", NOW, screen="refused"))
        self.assertIsNone(b._claim(ROOM, entry, "claude-code-cli", "x", NOW, screen="look"))
        self.assertEqual([], self.inbox() + self.inbox("claude-code-cli"))


if __name__ == "__main__":
    unittest.main()

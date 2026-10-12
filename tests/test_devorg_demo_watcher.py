"""cloud/devorg-demo/watcher.py: the armed one-shot that hears one spoken request and puts things back.

What is pinned, and why it must hold:

    THE ARMING IS THE ONLY DOOR. Unarmed, the job exits at its config without touching the org or
    the bucket. Armed, it is bound to ONE room, ONE Case, ONE org - Aya's ruling on 2026-10-12:
    "bind the exact approved room ID AND task correlation, not whichever transcript appears next."

    ONE CHANGE, ONE RESTORE. However the run ends - the spoken word, expiry, the call closing, or
    an error after the change - the value read at arming is put back, exactly once.

    A SENTENCE IS A REQUEST ONLY IN ONE SHAPE. A question, a negation anywhere in the sentence, two
    values in one sentence, or words before the verb that are not a lead-in: refused whole.

    NOTHING REPLAYS. Lines present when the transcript was first seen live are skipped; the same
    line is never acted on twice (correlation dedup); a transcript that is already closed refuses.

No network: the store and the org below are the only outside things, and they record every call.
"""
import unittest
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "devorg_watcher", Path(__file__).resolve().parent.parent / "cloud" / "devorg-demo" / "watcher.py")
watcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watcher)

ROOM = "conf-demo-abc123"
ORG = "00Dbm00000wK2ibEAC"
CASE = "500bm000039LHSAAA4"


def transcript(state: str, owner_texts) -> str:
    lines = "\n".join("- **[13:0%d:00Z] Owner:** %s" % (i % 10, text) for i, text in enumerate(owner_texts))
    other = "- **[13:00:00Z] Grok:** Set the demo case priority to high."   # never an Owner line
    return "---\ntype: call-transcript\nroom: %s\nstate: %s\n---\n\n%s\n%s\n" % (ROOM, state, other, lines)


def env(**extra) -> dict:
    base = {"DEMO_ARMED": "1", "DEMO_ORG_ID": ORG, "DEMO_CASE_ID": CASE, "DEMO_ROOM": ROOM,
            "DEMO_EXPIRES_MINUTES": "5", "TRANSCRIPT_BUCKET": "bucket",
            "Headless_domain": "x.develop.my.salesforce.com", "Headless_consumer_key": "k",
            "Headless_consumer_secret": "s"}
    base.update(extra)
    return {name: value for name, value in base.items() if value is not None}


class FakeStore:
    """The bucket as the run sees it: a list of texts, served in order (the last one repeats)."""

    def __init__(self, texts, exists=True):
        self.texts = list(texts)
        self.exists = exists
        self.receipts = []

    def transcript_of(self, room):
        assert room == ROOM, "asked for a room the arming did not bind"
        return "conference/transcripts/%s/t.md" % room if self.exists else None

    def read(self, name):
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]

    def receipt(self, entry):
        self.receipts.append(entry)


class FakeOrg:
    def __init__(self, priority="Medium", org_id=ORG, kind="Developer Edition", fail_set=False):
        self.priority = priority
        self.org_id, self.kind = org_id, kind
        self.fail_set = fail_set
        self.sets = []

    def identity(self):
        return {"org_id": self.org_id, "type": self.kind, "sandbox": False}

    def case(self, case_id):
        assert case_id == CASE, "asked for a Case the arming did not bind"
        return {"Id": case_id, "CaseNumber": "00001036", "Subject": "s", "Priority": self.priority, "IsClosed": False}

    def set_priority(self, case_id, value):
        self.sets.append((case_id, value))
        if not self.fail_set:
            self.priority = value


class UntouchableOrg:
    def __getattr__(self, name):
        raise AssertionError("the org was touched while %s" % name)


def run(environ, store, org, ticks=600):
    """watcher.run with fakes: the clock advances one second a step, sleep does not sleep."""
    beat = iter(range(ticks))
    kept = watcher.Store, watcher.Org
    watcher.Store = lambda bucket: store
    watcher.Org = lambda *a: org
    try:
        return watcher.run(environ, clock=lambda: next(beat), sleep=lambda s: None)
    finally:
        watcher.Store, watcher.Org = kept


class TheArmingIsTheOnlyDoor(unittest.TestCase):
    def test_unarmed_exits_zero_before_anything_is_touched(self):
        for armed in (None, "0", "", "true", "yes"):
            code = run(env(DEMO_ARMED=armed), FakeStore([]), UntouchableOrg())
            self.assertEqual(0, code, armed)

    def test_each_binding_is_required_and_shaped(self):
        for name, bad in (("DEMO_ORG_ID", None), ("DEMO_ORG_ID", "not-an-org"), ("DEMO_CASE_ID", None),
                          ("DEMO_CASE_ID", "001bm00000AAAAAAA"), ("DEMO_ROOM", None), ("DEMO_ROOM", "lobby"),
                          ("DEMO_ROOM", "conf-UPPER"), ("DEMO_EXPIRES_MINUTES", "0"),
                          ("DEMO_EXPIRES_MINUTES", "46"), ("DEMO_EXPIRES_MINUTES", "soon"),
                          ("Headless_domain", None), ("Headless_consumer_key", " "),
                          ("Headless_consumer_secret", None)):
            code = run(env(**{name: bad}), FakeStore([]), UntouchableOrg())
            self.assertEqual(2, code, (name, bad))

    def test_the_wrong_org_refuses_before_any_write(self):
        org = FakeOrg(org_id="00D000000000001AAA")
        self.assertEqual(3, run(env(), FakeStore([transcript("live", [])]), org))
        self.assertEqual([], org.sets)

    def test_a_production_looking_org_refuses(self):
        org = FakeOrg(kind="Enterprise Edition")
        self.assertEqual(3, run(env(), FakeStore([transcript("live", [])]), org))
        self.assertEqual([], org.sets)


class OneChangeOneRestore(unittest.TestCase):
    def test_the_spoken_change_and_the_spoken_restore(self):
        org = FakeOrg()
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."]),
                           transcript("live", ["Set the demo case priority to high.",
                                               "Put the demo case priority back to medium."])])
        self.assertEqual(0, run(env(), store, org))
        self.assertEqual([(CASE, "High"), (CASE, "Medium")], org.sets)
        self.assertEqual(["change", "undo"], [r["kind"] for r in store.receipts])
        self.assertTrue(all(r["held"] for r in store.receipts))

    def test_expiry_restores_what_the_spoken_word_did_not(self):
        org = FakeOrg()
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."])])
        self.assertEqual(0, run(env(DEMO_EXPIRES_MINUTES="1"), store, org))
        self.assertEqual([(CASE, "High"), (CASE, "Medium")], org.sets)
        self.assertEqual("the arming ended", store.receipts[-1]["why"])

    def test_the_call_closing_restores_too(self):
        org = FakeOrg()
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Make the demo case priority to high."]),
                           transcript("closed", ["Make the demo case priority to high."])])
        self.assertEqual(0, run(env(), store, org))
        self.assertEqual([(CASE, "High"), (CASE, "Medium")], org.sets)

    def test_the_restore_target_is_the_value_read_at_arming(self):
        org = FakeOrg(priority="Low")
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."])])
        run(env(DEMO_EXPIRES_MINUTES="1"), store, org)
        self.assertEqual([(CASE, "High"), (CASE, "Low")], org.sets)

    def test_a_read_back_that_does_not_hold_is_exit_one(self):
        org = FakeOrg(fail_set=True)
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."])])
        self.assertEqual(1, run(env(DEMO_EXPIRES_MINUTES="1"), store, org))
        self.assertFalse(store.receipts[0]["held"])

    def test_a_second_high_is_refused_as_out_of_order(self):
        org = FakeOrg()
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high.",
                                               "Now set the demo case priority to high."]),
                           transcript("live", ["Set the demo case priority to high.",
                                               "Now set the demo case priority to high.",
                                               "Set the demo case priority to medium."])])
        self.assertEqual(0, run(env(), store, org))
        self.assertEqual([(CASE, "High"), (CASE, "Medium")], org.sets)
        self.assertEqual(["change", "refused", "undo"], [r["kind"] for r in store.receipts])


class NothingReplays(unittest.TestCase):
    def test_lines_present_when_first_seen_live_are_history(self):
        org = FakeOrg()
        store = FakeStore([transcript("live", ["Set the demo case priority to high."])])
        self.assertEqual(0, run(env(DEMO_EXPIRES_MINUTES="1"), store, org))
        self.assertEqual([], org.sets)

    def test_an_already_closed_transcript_refuses(self):
        org = FakeOrg()
        store = FakeStore([transcript("closed", [])])
        self.assertEqual(4, run(env(), store, org))
        self.assertEqual([], org.sets)

    def test_a_missing_transcript_expires_unspent(self):
        self.assertEqual(0, run(env(DEMO_EXPIRES_MINUTES="1"), FakeStore([], exists=False), FakeOrg()))

    def test_to_low_is_refused_as_out_of_scope(self):
        org = FakeOrg()
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to low."])])
        self.assertEqual(0, run(env(DEMO_EXPIRES_MINUTES="1"), store, org))
        self.assertEqual([], org.sets)
        self.assertEqual(["refused"], [r["kind"] for r in store.receipts])


class ASentenceIsARequestOnlyInOneShape(unittest.TestCase):
    def test_the_shapes_that_ask(self):
        for text, value in (("Set the demo case priority to high.", "High"),
                            ("Okay, please change the demo case priority to high", "High"),
                            ("Claude, update the demo case priority to high.", "High"),
                            ("Put the demo case priority back to medium.", "Medium"),
                            ("Restore the demo case priority to medium.", "Medium"),
                            ("So, mark the demo case priority to low.", "Low")):
            self.assertEqual(value, watcher.request(text), text)

    def test_the_shapes_that_do_not(self):
        for text in ("Can you set the demo case priority to high?",            # a question
                     "Do not set the demo case priority to high.",             # a negation
                     "Don't set the demo case priority to high.",              # its contraction
                     "Never set the demo case priority to high.",
                     "Set the demo case priority from medium to high.",        # two values
                     "Set the case priority to high.",                         # not the demo case
                     "Set the demo case priority to urgent.",                  # not a value
                     "The demo case priority moved to high.",                  # no instruction verb
                     "I wonder whether to set the demo case priority to high altogether",
                     "Set the demo case priority to high for an hour."):       # does not END at the value
            self.assertIsNone(watcher.request(text), text)

    def test_two_asking_sentences_in_one_line_refuse(self):
        self.assertIsNone(watcher.request(
            "Set the demo case priority to high. Set the demo case priority to medium."))

    def test_one_asking_sentence_beside_talk_still_asks(self):
        self.assertEqual("High", watcher.request(
            "That looks right. Set the demo case priority to high. Thanks everyone."))

    def test_the_owner_line_shape_and_the_correlation(self):
        lines = watcher.owner_lines(transcript("live", ["one", "two"]))
        self.assertEqual([(0, "13:00:00", "one"), (1, "13:01:00", "two")], lines)
        a = watcher.correlation(ROOM, 0, "13:00:00", "one")
        self.assertEqual(a, watcher.correlation(ROOM, 0, "13:00:00", "one"))
        self.assertNotEqual(a, watcher.correlation(ROOM, 1, "13:00:00", "one"))

    def test_front_matter_reads_the_header_and_only_the_header(self):
        header = watcher.front_matter(transcript("live", []))
        self.assertEqual({"type": "call-transcript", "room": ROOM, "state": "live"}, header)
        self.assertEqual({}, watcher.front_matter("no header\n---\nstate: live\n"))
        self.assertEqual({}, watcher.front_matter("---\nstate: live\n"))   # never closed


if __name__ == "__main__":
    unittest.main()

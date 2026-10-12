"""cloud/devorg-demo/watcher.py: the armed one-shot that hears one spoken request and puts things back.

What is pinned, and why it must hold (the reviewer's three batches on 6753b44, one corrected head):

    THE SCOPE IS SOURCE, NOT CONFIGURATION. The approved org, Case and values are module constants;
    the environment can only CONFIRM them. A Case that does not hold Medium at arming refuses:
    Low-to-High-to-Low is nobody's approval.

    THE ARMING IS THE ONLY DOOR, AND IT IS DURABLE. Unarmed, the job exits at its config without
    touching anything. Armed, it consumes a create-once object first, so a platform retry or a
    second execution against the same room refuses instead of acting twice.

    AN UNCERTAIN WRITE IS STILL A WRITE. The pending-undo journal is durable BEFORE the write
    leaves; a commit whose response is lost, or a read-back that raises, still ends in Medium. A
    restore attempt that itself fails is UNKNOWN - said, receipted, exit 1 - never claimed as done.

    A SENTENCE IS A REQUEST ONLY IN ONE SHAPE, and a negation anywhere in the line refuses the
    whole line. Words inside the subject ("the OTHER demo case") refuse. A transcript that says
    closed contributes no commands - closed is checked before new lines.

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
ORG = watcher.APPROVED_ORG
CASE = watcher.APPROVED_CASE


def transcript(state: str, owner_texts, room: str = ROOM) -> str:
    lines = "\n".join("- **[13:0%d:00Z] Owner:** %s" % (i % 10, text) for i, text in enumerate(owner_texts))
    other = "- **[13:00:00Z] Grok:** Set the demo case priority to high."   # never an Owner line
    return "---\ntype: call-transcript\nroom: %s\nstate: %s\n---\n\n%s\n%s\n" % (room, state, other, lines)


def env(**extra) -> dict:
    base = {"DEMO_ARMED": "1", "DEMO_ORG_ID": ORG, "DEMO_CASE_ID": CASE, "DEMO_ROOM": ROOM,
            "DEMO_EXPIRES_MINUTES": "5", "TRANSCRIPT_BUCKET": "bucket",
            "Headless_domain": "x.develop.my.salesforce.com", "Headless_consumer_key": "k",
            "Headless_consumer_secret": "s"}
    base.update(extra)
    return {name: value for name, value in base.items() if value is not None}


class FakeStore:
    """The bucket as the run sees it: a list of texts served in order (the last repeats), a record
    of receipts, and a real create-once ledger that survives across runs when shared."""

    def __init__(self, texts, exists=True, events=None):
        self.texts = list(texts)
        self.exists = exists
        self.receipts = []
        self.created = set()
        self.events = events if events is not None else []

    def transcript_of(self, room):
        assert room == ROOM, "asked for a room the arming did not bind"
        return "conference/transcripts/%s/t.md" % room if self.exists else None

    def read(self, name):
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]

    def receipt(self, entry):
        self.receipts.append(entry)

    def create_once(self, name, entry):
        if name in self.created:
            return False
        self.created.add(name)
        self.events.append(("created", name))
        return True


class FakeOrg:
    def __init__(self, priority="Medium", org_id=ORG, kind="Developer Edition",
                 fail_set=False, raise_after_commit=False, raise_readback=False,
                 fail_reads_after_set=False, events=None):
        self.priority = priority
        self.org_id, self.kind = org_id, kind
        self.fail_set = fail_set
        self.raise_after_commit = raise_after_commit
        self.raise_readback = raise_readback
        self.fail_reads_after_set = fail_reads_after_set
        self.sets = []
        self.events = events if events is not None else []

    def identity(self):
        return {"org_id": self.org_id, "type": self.kind, "sandbox": False}

    def case(self, case_id):
        assert case_id == CASE, "asked for a Case the scope did not pin"
        if self.raise_readback and self.sets:
            self.raise_readback = False                 # once: the read after the first write
            raise TimeoutError("the read-back timed out")
        if self.fail_reads_after_set and self.sets:
            raise TimeoutError("the org stopped answering")
        return {"Id": case_id, "CaseNumber": "00001036", "Subject": "s", "Priority": self.priority, "IsClosed": False}

    def set_priority(self, case_id, value):
        self.sets.append((case_id, value))
        self.events.append(("set", value))
        if not self.fail_set:
            self.priority = value
        if self.raise_after_commit:
            self.raise_after_commit = False             # once: the write landed, its answer was lost
            raise TimeoutError("the update's response was lost")


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

    def test_the_scope_is_source_not_configuration(self):
        # A well-shaped id that is not the approved one refuses before any network.
        for name, value in (("DEMO_ORG_ID", "00D000000000001AAA"), ("DEMO_CASE_ID", "500000000000001AAA")):
            code = run(env(**{name: value}), FakeStore([]), UntouchableOrg())
            self.assertEqual(2, code, name)

    def test_the_wrong_org_refuses_before_any_write(self):
        org = FakeOrg(org_id="00D000000000001AAA")
        self.assertEqual(3, run(env(), FakeStore([transcript("live", [])]), org))
        self.assertEqual([], org.sets)

    def test_a_production_looking_org_refuses(self):
        org = FakeOrg(kind="Enterprise Edition")
        self.assertEqual(3, run(env(), FakeStore([transcript("live", [])]), org))
        self.assertEqual([], org.sets)

    def test_a_case_not_holding_medium_refuses_the_arming(self):
        # The approved cycle is Medium to High to Medium; a Low or High baseline is outside it.
        for held in ("Low", "High"):
            org = FakeOrg(priority=held)
            self.assertEqual(3, run(env(), FakeStore([transcript("live", [])]), org), held)
            self.assertEqual([], org.sets)

    def test_the_arming_is_consumed_durably_so_a_rerun_refuses(self):
        first = FakeOrg()
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."]),
                           transcript("live", ["Set the demo case priority to high.",
                                               "Set the demo case priority to medium."])])
        self.assertEqual(0, run(env(), store, first))
        self.assertEqual([(CASE, "High"), (CASE, "Medium")], first.sets)
        second = FakeOrg()
        rerun_store = FakeStore([transcript("live", ["Set the demo case priority to high."])])
        rerun_store.created = store.created             # the bucket survives the process
        self.assertEqual(4, run(env(), rerun_store, second))
        self.assertEqual([], second.sets)


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
        self.assertTrue(all(r["room"] == ROOM and r["case_id"] == CASE for r in store.receipts))

    def test_the_pending_journal_is_durable_before_the_write_leaves(self):
        events = []
        org = FakeOrg(events=events)
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."])],
                          events=events)
        run(env(DEMO_EXPIRES_MINUTES="1"), store, org)
        pending = [name for kind, name in events if kind == "created" and "pending-" in name]
        self.assertEqual(1, len(pending))
        self.assertLess(events.index(("created", pending[0])), events.index(("set", "High")),
                        "the journal must exist before the write leaves")

    def test_expiry_restores_what_the_spoken_word_did_not(self):
        org = FakeOrg()
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."])])
        self.assertEqual(0, run(env(DEMO_EXPIRES_MINUTES="1"), store, org))
        self.assertEqual([(CASE, "High"), (CASE, "Medium")], org.sets)
        self.assertIn("the arming ended", store.receipts[-1]["why"])

    def test_the_call_closing_restores_too(self):
        org = FakeOrg()
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Make the demo case priority to high."]),
                           transcript("closed", ["Make the demo case priority to high."])])
        self.assertEqual(0, run(env(), store, org))
        self.assertEqual([(CASE, "High"), (CASE, "Medium")], org.sets)

    def test_a_commit_whose_response_is_lost_is_still_restored(self):
        # Reviewer on 6753b44: the write lands, its response times out, and the old code never knew
        # it had changed anything - the restore was skipped and High stayed.
        org = FakeOrg(raise_after_commit=True)
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."])])
        self.assertEqual(1, run(env(DEMO_EXPIRES_MINUTES="1"), store, org))
        self.assertEqual("Medium", org.priority)
        undo = [r for r in store.receipts if r["kind"] == "undo"]
        self.assertEqual(1, len(undo))
        self.assertIn("never heard", undo[0]["why"])
        self.assertTrue(undo[0]["held"])

    def test_a_read_back_that_raises_is_still_restored(self):
        org = FakeOrg(raise_readback=True)
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."])])
        self.assertEqual(1, run(env(DEMO_EXPIRES_MINUTES="1"), store, org))
        self.assertEqual("Medium", org.priority)
        self.assertEqual(["undo"], [r["kind"] for r in store.receipts])

    def test_a_restore_that_cannot_reach_the_org_is_unknown_never_done(self):
        org = FakeOrg(fail_reads_after_set=True)
        store = FakeStore([transcript("live", []),
                           transcript("live", ["Set the demo case priority to high."])])
        self.assertEqual(1, run(env(DEMO_EXPIRES_MINUTES="1"), store, org))
        self.assertEqual("High", org.priority)          # nothing pretended otherwise
        kinds = [r["kind"] for r in store.receipts]
        self.assertIn("undo-unknown", kinds)
        self.assertNotIn("undo", kinds)

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

    def test_a_closed_snapshot_contributes_no_commands(self):
        # The command first appears in the snapshot that says closed: the call is over, and the
        # sealed record is not a channel (closed is checked before new lines).
        org = FakeOrg()
        store = FakeStore([transcript("live", []),
                           transcript("closed", ["Set the demo case priority to high."])])
        self.assertEqual(0, run(env(DEMO_EXPIRES_MINUTES="1"), store, org))
        self.assertEqual([], org.sets)

    def test_a_transcript_whose_header_names_another_room_refuses(self):
        org = FakeOrg()
        store = FakeStore([transcript("live", [], room="conf-other-room")])
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


class TheWriteItselfIsGated(unittest.TestCase):
    """The two gates rechecked immediately before the PATCH, and the bounded recovery."""

    def source(self):
        return {"kind": "transcript", "room": ROOM, "line_time": "13:05:00", "place": 5,
                "correlation": "a" * 16}

    def test_a_case_drifted_off_medium_aborts_before_the_write(self):
        # An edit from elsewhere after arming must abort the mutation, not widen the cycle.
        org = FakeOrg(priority="Low")
        store = FakeStore([])
        demo = watcher.Demo(org, store, ROOM)
        demo.act("High", self.source())
        self.assertEqual([], org.sets)
        self.assertEqual(["refused"], [r["kind"] for r in store.receipts])
        self.assertIn("does not hold Medium now", store.receipts[0]["why"])

    def test_the_deadline_is_rechecked_immediately_before_the_write(self):
        # Polling and reads can cross the deadline after the loop entered.
        org = FakeOrg()
        store = FakeStore([])
        demo = watcher.Demo(org, store, ROOM, clock=lambda: 100.0, deadline=99.0)
        demo.act("High", self.source())
        self.assertEqual([], org.sets)
        self.assertEqual(["refused"], [r["kind"] for r in store.receipts])
        self.assertIn("expired before the write", store.receipts[0]["why"])

    def recovery_env(self):
        base = env(DEMO_ARMED=None, DEMO_ROOM=None)
        base["DEMO_RECOVER_CORRELATION"] = "a" * 16
        return base

    def journal_store(self):
        store = FakeStore([])
        store.journal = {"kind": "pending-undo", "correlation": "a" * 16, "room": ROOM,
                         "case_id": CASE, "field": "Priority", "restore_to": "Medium", "before": "Medium"}

        def read_json(name):
            if "pending-" in name:
                return dict(store.journal)
            if name in store.created:
                return {"kind": "resolved"}
            raise watcher.Refused(4, "there is no such journal object")
        store.read_json = read_json
        return store

    def test_recovery_restores_the_journalled_operation_once(self):
        org = FakeOrg(priority="High")
        store = self.journal_store()
        self.assertEqual(0, run(self.recovery_env(), store, org))
        self.assertEqual([(CASE, "Medium")], org.sets)
        undo = [r for r in store.receipts if r["kind"] == "undo"]
        self.assertEqual(1, len(undo))
        self.assertTrue(undo[0]["held"])
        self.assertIn("recovery", undo[0]["why"])
        # And only once: the resolved marker refuses a second recovery.
        again = FakeOrg(priority="High")
        store.texts = []
        self.assertEqual(4, run(self.recovery_env(), store, again))
        self.assertEqual([], again.sets)

    def test_recovery_of_a_case_already_at_the_restore_value_reconciles(self):
        org = FakeOrg(priority="Medium")
        store = self.journal_store()
        self.assertEqual(0, run(self.recovery_env(), store, org))
        self.assertEqual([], org.sets)
        self.assertEqual(["undo"], [r["kind"] for r in store.receipts])

    def test_recovery_leaves_a_third_value_to_a_human(self):
        org = FakeOrg(priority="Low")
        store = self.journal_store()
        self.assertEqual(3, run(self.recovery_env(), store, org))
        self.assertEqual([], org.sets)
        self.assertEqual(["refused"], [r["kind"] for r in store.receipts])

    def test_recovery_and_arming_refuse_to_combine(self):
        bad = self.recovery_env()
        bad["DEMO_ARMED"] = "1"
        self.assertEqual(2, run(bad, FakeStore([]), UntouchableOrg()))

    def test_a_malformed_recovery_correlation_refuses(self):
        bad = self.recovery_env()
        bad["DEMO_RECOVER_CORRELATION"] = "not-hex"
        self.assertEqual(2, run(bad, FakeStore([]), UntouchableOrg()))

    def test_a_failed_recovery_keeps_the_journal_pending_for_a_retry(self):
        # The resolved marker is committed only AFTER an authoritative Medium read-back: a marker
        # written first blocked the retry while Medium was unverified (the reviewer on 67494c5).
        broken = FakeOrg(priority="High", fail_set=True)
        store = self.journal_store()
        self.assertEqual(1, run(self.recovery_env(), store, broken))
        self.assertFalse(any("resolved-" in name for name in store.created))
        healed = FakeOrg(priority="High")
        self.assertEqual(0, run(self.recovery_env(), store, healed))
        self.assertEqual([(CASE, "Medium")], healed.sets)
        self.assertTrue(any("resolved-" in name for name in store.created))

    def test_the_finally_restore_refuses_a_third_value_like_recovery(self):
        # A concurrent edit to Low is someone else's work: the restore must not write over it.
        org = FakeOrg(priority="Low")
        store = FakeStore([])
        demo = watcher.Demo(org, store, ROOM)
        demo.changed = "a" * 16
        demo.restore("the arming ended")
        self.assertEqual([], org.sets)
        self.assertEqual(["refused"], [r["kind"] for r in store.receipts])
        self.assertTrue(demo.undo_unknown)
        self.assertFalse(demo.restored)

    def test_the_deadline_is_rechecked_at_the_mutation_boundary_itself(self):
        # Entry passes; the reads and the journal write cross the deadline; the PATCH must not go.
        ticks = iter([98.0, 100.0])     # entry check, then the boundary check
        org = FakeOrg()
        store = FakeStore([])
        demo = watcher.Demo(org, store, ROOM, clock=lambda: next(ticks), deadline=99.0)
        demo.act("High", self.source())
        self.assertEqual([], org.sets)
        kinds = [r["kind"] for r in store.receipts]
        self.assertEqual(["refused"], kinds)
        self.assertIn("mutation boundary", store.receipts[0]["why"])
        self.assertTrue(any("pending-" in name for name in store.created),
                        "the journal may exist; recovering it reconciles harmlessly")


class ASentenceIsARequestOnlyInOneShape(unittest.TestCase):
    def test_the_shapes_that_ask(self):
        for text, value in (("Set the demo case priority to high.", "High"),
                            ("Okay, please change the demo case priority to high", "High"),
                            ("Claude, update the demo case priority to high.", "High"),
                            ("Set the demo case's priority to high.", "High"),
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
                     "Set the other demo case priority to high.",              # another subject
                     "Set the second demo case priority to high.",
                     "Set the case priority to high.",                         # not the demo case
                     "Set the demo case priority to urgent.",                  # not a value
                     "The demo case priority moved to high.",                  # no instruction verb
                     "Set the demo case priority to high for an hour.",        # does not END at the value
                     "Set the demo case priority to high please."):
            self.assertIsNone(watcher.request(text), text)

    def test_a_negation_anywhere_in_the_line_refuses_the_whole_line(self):
        # Sentence order cannot carry a negation either: the line is judged whole.
        for text in ("Do not do this. Set the demo case priority to high.",
                     "Set the demo case priority to high. Actually no.",
                     "Wait. Set the demo case priority to high."):
            self.assertIsNone(watcher.request(text), text)

    def test_a_question_anywhere_in_the_line_refuses_the_whole_line(self):
        # A hypothetical beside an imperative poisons the whole utterance (Aya on 3ac1457).
        for text in ("Would this be the right command? Set the demo case priority to high.",
                     "Set the demo case priority to high. Or should we?",
                     "What happens if I say it? Set the demo case priority to high."):
            self.assertIsNone(watcher.request(text), text)

    def test_two_asking_sentences_in_one_line_refuse(self):
        self.assertIsNone(watcher.request(
            "Set the demo case priority to high. Set the demo case priority to medium."))

    def test_one_asking_sentence_beside_talk_still_asks(self):
        self.assertEqual("High", watcher.request(
            "That looks good. Set the demo case priority to high. Thanks everyone."))

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


class TheOrgClientItself(unittest.TestCase):
    """The real Org class over a stubbed wire: the startup ordering and the sanitized error."""

    def serve(self, answers):
        calls = []

        def fake_http(req, timeout):
            calls.append(req)
            return answers[len(calls) - 1]
        kept = watcher._http
        watcher._http = fake_http
        self.addCleanup(setattr, watcher, "_http", kept)
        return calls

    def test_the_first_query_acquires_the_token_before_building_its_request(self):
        # Reviewer on 6753b44: the first query built its request while the instance was still empty.
        token = (200, b'{"access_token": "t", "instance_url": "https://x.develop.my.salesforce.com"}')
        rows = (200, b'{"records": [{"Id": "x"}]}')
        calls = self.serve([token, rows])
        org = watcher.Org("x.develop.my.salesforce.com", "k", "s")
        org.query("SELECT Id FROM Organization")
        self.assertEqual(2, len(calls))
        self.assertIn("/services/oauth2/token", calls[0].full_url)
        self.assertTrue(calls[1].full_url.startswith("https://x.develop.my.salesforce.com/services/data/"),
                        calls[1].full_url)

    def test_an_org_error_is_logged_as_its_code_and_never_its_words(self):
        token = (200, b'{"access_token": "t", "instance_url": "https://x.develop.my.salesforce.com"}')
        refusal = (400, b'[{"message": "Record words that must never be logged", '
                        b'"errorCode": "FIELD_CUSTOM_VALIDATION_EXCEPTION"}]')
        self.serve([token, refusal])
        org = watcher.Org("x.develop.my.salesforce.com", "k", "s")
        with self.assertRaises(watcher.Refused) as caught:
            org.set_priority(CASE, "High")
        self.assertIn("FIELD_CUSTOM_VALIDATION_EXCEPTION", str(caught.exception))
        self.assertNotIn("never be logged", str(caught.exception))

    def test_a_token_whose_instance_is_not_a_developer_host_refuses(self):
        self.serve([(200, b'{"access_token": "t", "instance_url": "https://login.salesforce.com"}')])
        org = watcher.Org("x.develop.my.salesforce.com", "k", "s")
        with self.assertRaises(watcher.Refused):
            org.token()


if __name__ == "__main__":
    unittest.main()

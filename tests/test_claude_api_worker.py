"""claude-api worker (cloud/claude-api-worker/main.py): one bounded answer when the laptop is off.

The ways this goes wrong, each with a test that fails when the guard is removed:
a second model call for a task the doc already shows answered or taken, a call
before ACCEPTED is readable, a call with no reservation under the day cap, an
answer lost between the model and the receipt, an edited request run as the old
one, a timeout re-run, two passes both calling, a result line forging a receipt
or retriggering intake, and the laptop's own task taken while it is up.

Offline by construction: every outside call is an injected adapter.
"""
import importlib.util
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wk = load("claude_api_worker", REPO / "cloud" / "claude-api-worker" / "main.py")
gd = load("claude_api_gdocs", REPO / "cloud" / "claude-api-worker" / "gdocs.py")
di = wk.di

FOLDER = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"
FILE = "1pKWJYbJzMqxSA9Vy_PSR93BU2OAAgP-N3WBEkCj7oj4"
REQUEST = "SFDC24 - OWNER TRIGGER\n\nrequest: OT-20261010\n\nFix the broken telephone first.\n"
NOW = 1_760_000_000.0
AT = "2026-10-11T04:30:00Z"


def intake_lines(tuple_, states=("REQUESTED", "DISPATCHED")):
    """What drive-intake leaves in the doc before this worker ever sees the task."""
    lines = []
    for state in states:
        rid = di.receipt_id_for(tuple_, state)
        lines.append(di.receipt_line(state, tuple_, "2026-10-11T04:00:00Z") + " - id " + rid)
    return "\nRECEIPTS\n" + "\n".join(lines) + "\n"


class Docs:
    def __init__(self, text, revision="r1", folder=FOLDER, receipts=None):
        self.text, self.revision, self.folder, self.receipts = text, revision, folder, receipts
        self.reads = 0

    def read(self, file_id):
        self.reads += 1
        if self.receipts is not None:
            return {"text": self.text + self.receipts.text(), "revision_id": self.receipts.revision,
                    "folder_id": self.folder}
        return {"text": self.text, "revision_id": self.revision, "folder_id": self.folder}


class Receipts:
    """Revision-guarded append returning the new revision; `lose` raises AFTER landing, `drop` vanishes."""

    def __init__(self, lose=False, drop=False, revision="r1"):
        self.lines, self.lose, self.drop, self.revision, self.n = [], lose, drop, revision, 1

    def append(self, file_id, expected_revision, receipt_id, block):
        if not self.drop:
            self.lines.append((file_id, receipt_id, block))
            self.n += 1
            self.revision = "r%d" % self.n
        if self.lose:
            raise ConnectionError("response lost")
        return self.revision

    def has(self, file_id, receipt_id):
        return any(f == file_id and r == receipt_id for f, r, _ in self.lines)

    def states(self):
        return [b.strip().splitlines()[0].split(" - ")[0] for _, _, b in self.lines]

    def text(self):
        return "".join(b if b.startswith("\n") else "\n" + b for _, _, b in self.lines) + ("\n" if self.lines else "")


class Inbox:
    def __init__(self, entries=()):
        self.entries = list(entries)         # oldest first, like a stream
        self.reads = 0

    def read(self, agent, count):
        self.reads += 1
        return list(reversed(self.entries))[:count]


class Model:
    def __init__(self, text="The answer.\n\nFOR THE LAPTOP:\nnothing", fail=None, tokens=(1200, 300)):
        self.text, self.fail, self.tokens, self.calls = text, fail, tokens, []

    def __call__(self, system, prompt):
        self.calls.append((system, prompt))
        if self.fail:
            raise self.fail
        return {"text": self.text, "input_tokens": self.tokens[0], "output_tokens": self.tokens[1],
                "model": "claude-haiku-5-5", "stop_reason": "end_turn"}


def task_tuple(text=REQUEST):
    return di.correlate(FILE, text, "r1", wk.WORKER)


def entry(text=REQUEST, when=NOW - 60, recipient=wk.WORKER, task_id=None):
    t = task_tuple(text)
    fields = {"by": "drive-intake", "at": "2026-10-11T04:00:00Z", "kind": "drive_request",
              "text": di.gist(text)["text"], "chars": "40", "full": "1",
              "source_doc_id": FILE, "source_revision": t["source_revision"],
              "request_id": t["request_id"], "task_id": task_id or t["task_id"], "recipient_id": recipient}
    return ("%d-0" % int(when * 1000), fields)


def deps(text=REQUEST, receipts=None, **over):
    """Laptop OFF, cap and reservation set, one task on the inbox, intake's lines already in the doc."""
    t = task_tuple(text)
    receipts = receipts if receipts is not None else Receipts()
    docs = Docs(text + intake_lines(t), receipts=receipts)
    base = dict(enabled=True, inbox=Inbox([entry(text)]), docs=docs, receipts=receipts,
                claims=wk.MemoryClaims(), ledger=wk.MemoryLedger(), model=Model(),
                model_name="claude-haiku-5-5", folder_id=FOLDER, now=NOW, at=AT,
                day=lambda now: "2026-10-11", cap_cents=2500, reserve_cents=5)
    base.update(over)
    return wk.Deps(**base)


def laptop_up(d, age=10.0):
    d.claims.beats["claude-code-cli"] = NOW - age


class FlagTest(unittest.TestCase):
    def test_anything_but_exactly_on_is_off(self):
        for value in (None, "", "ON", "On", "1", "true", "yes", " on"):
            env = {} if value is None else {"CLAUDE_API_WORKER_ENABLED": value}
            self.assertFalse(wk.enabled_from_env(env), repr(value))
        self.assertTrue(wk.enabled_from_env({"CLAUDE_API_WORKER_ENABLED": "on"}))

    def test_off_reads_nothing_beats_nothing_calls_nothing_writes_nothing(self):
        d = deps(enabled=False)
        self.assertEqual("disabled", wk.run_once(d)["status"])
        self.assertEqual(0, d.inbox.reads)
        self.assertEqual(0, d.docs.reads)
        self.assertEqual([], d.model.calls)
        self.assertEqual([], d.receipts.lines)
        self.assertNotIn(wk.WORKER, d.claims.beats)


class RunTest(unittest.TestCase):
    """Laptop off: the task is accepted, answered once, and the doc shows exactly that."""

    def test_laptop_off_one_call_accepted_then_succeeded_with_the_answer_under_it(self):
        d = deps()
        out = wk.run_once(d)
        self.assertEqual("ran", out["status"], out)
        self.assertEqual(1, len(d.model.calls))
        self.assertEqual(["ACCEPTED", "SUCCEEDED"], d.receipts.states())
        accepted, succeeded = d.receipts.lines[0][2], d.receipts.lines[1][2]
        self.assertIn("attempt 1 - lease until ", accepted)
        self.assertIn("by claude-api", accepted)
        self.assertIn("\n> The answer.", succeeded)
        self.assertIn("\n> FOR THE LAPTOP:", succeeded)
        self.assertIn("tokens in 1200 out 300", succeeded)
        self.assertEqual("succeeded", out["considered"][0]["status"])
        self.assertEqual(NOW, d.claims.beats[wk.WORKER])          # the worker's own heartbeat
        self.assertEqual("done", d.claims.states[task_tuple()["task_id"]])

    def test_the_model_gets_the_request_and_the_doctrine_that_forbids_narrated_actions(self):
        d = deps()
        wk.run_once(d)
        system, prompt = d.model.calls[0]
        self.assertIn("Fix the broken telephone first.", prompt)
        self.assertNotIn("RECEIPTS", prompt)                       # the receipts are not the request
        self.assertNotIn("REQUESTED - task", prompt)
        self.assertIn("Never say you did", system)
        self.assertIn("FOR THE LAPTOP", system)
        self.assertIn("Mr. Salam", system)

    def test_a_second_pass_does_nothing_from_the_marker_and_nothing_from_the_doc_when_the_marker_is_gone(self):
        d = deps()
        wk.run_once(d)
        reads = d.docs.reads
        again = wk.run_once(d)
        self.assertEqual("idle", again["status"])
        self.assertEqual("done", again["considered"][0]["status"])
        self.assertEqual(1, len(d.model.calls))
        self.assertEqual(reads, d.docs.reads)                      # the marker saved the doc read
        d.claims.done_marks.clear()                                 # Redis forgot (every key expires)
        d.claims.states.clear()
        third = wk.run_once(d)
        self.assertEqual("done", third["considered"][0]["status"])
        self.assertEqual(1, len(d.model.calls))                     # the doc's SUCCEEDED line is the record
        self.assertIn(task_tuple()["task_id"], d.claims.done_marks)  # and the marker is restored

    def test_one_model_call_per_pass_and_the_rest_waits(self):
        other = REQUEST.replace("first.", "second.")
        t2 = task_tuple(other)
        d = deps()
        d.inbox = Inbox([entry(REQUEST, when=NOW - 120), entry(other, when=NOW - 60)])
        out = wk.run_once(d)
        self.assertEqual(1, len(d.model.calls))
        self.assertEqual(1, out["calls"])
        d2 = deps()
        d2.inbox = Inbox([entry(REQUEST, when=NOW - 120), entry(other, when=NOW - 60)])
        out2 = wk.run_once(d2)
        self.assertEqual(["succeeded", "left_for_next_pass"], [c["status"] for c in out2["considered"]])
        self.assertEqual(t2["task_id"], out2["considered"][1]["task"])

    def test_entries_that_are_not_drive_requests_for_this_worker_are_not_touched(self):
        d = deps()
        eid, fields = entry()
        d.inbox = Inbox([(eid, dict(fields, kind="note")), (eid, dict(fields, recipient_id="gemini"))])
        out = wk.run_once(d)
        self.assertEqual(["not_mine", "not_mine"], [c["status"] for c in out["considered"]])
        self.assertEqual(0, d.docs.reads)
        self.assertEqual([], d.model.calls)


class GateTest(unittest.TestCase):
    """Decision 3: it runs when he is not working, or when named, or after the laptop's 30 minutes."""

    def test_laptop_up_and_the_task_young_is_deferred_with_nothing_written(self):
        d = deps()
        laptop_up(d)
        out = wk.run_once(d)
        self.assertEqual("deferred", out["considered"][0]["status"], out)
        self.assertEqual([], d.model.calls)
        self.assertEqual([], d.receipts.lines)
        self.assertEqual({}, d.claims.states)

    def test_laptop_up_but_the_task_waited_thirty_minutes_is_taken(self):
        d = deps()
        laptop_up(d)
        d.inbox = Inbox([entry(when=NOW - 1801)])
        out = wk.run_once(d)
        self.assertEqual("succeeded", out["considered"][0]["status"], out)
        self.assertEqual(1801, out["considered"][0]["reassigned_after"])

    def test_laptop_up_exactly_at_the_threshold_still_waits(self):
        d = deps()
        laptop_up(d)
        d.inbox = Inbox([entry(when=NOW - 1799)])
        self.assertEqual("deferred", wk.run_once(d)["considered"][0]["status"])

    def test_laptop_up_and_the_doc_names_claude_api_runs_at_once(self):
        text = "to: claude-api\n" + REQUEST
        d = deps(text=text)
        laptop_up(d)
        self.assertEqual("succeeded", wk.run_once(d)["considered"][0]["status"])

    def test_a_doc_naming_another_worker_is_not_naming_this_one(self):
        d = deps(text="to: codex" + chr(10) + REQUEST)
        laptop_up(d)
        self.assertEqual("deferred", wk.run_once(d)["considered"][0]["status"])

    def test_a_stale_heartbeat_is_off(self):
        d = deps()
        laptop_up(d, age=301.0)
        self.assertEqual("succeeded", wk.run_once(d)["considered"][0]["status"])

    def test_a_task_the_laptop_already_accepted_in_the_doc_is_not_run(self):
        t = task_tuple()
        line = ("ACCEPTED - task %s - request %s - rev %s - recipient claude-code-cli - by claude-code-cli - t\n"
                % (t["task_id"], t["request_id"], t["source_revision"][:12]))
        d = deps()
        d.docs.text = d.docs.text + line
        out = wk.run_once(d)
        self.assertEqual("taken", out["considered"][0]["status"])
        self.assertEqual("claude-code-cli", out["considered"][0]["by"])
        self.assertEqual([], d.model.calls)
        self.assertEqual([], d.receipts.lines)
        self.assertIn(t["task_id"], d.claims.done_marks)

    def test_an_edited_request_is_superseded_not_run_as_the_old_one(self):
        d = deps()
        edited = REQUEST.replace("first.", "first, then the showcase.")
        d.docs.text = edited + intake_lines(task_tuple())
        out = wk.run_once(d)
        self.assertEqual("superseded", out["considered"][0]["status"])
        self.assertEqual(task_tuple(edited)["task_id"], out["considered"][0]["now"])
        self.assertEqual([], d.model.calls)
        self.assertEqual([], d.receipts.lines)

    def test_a_doc_outside_the_folder_or_unreadable_or_without_a_revision_is_not_run(self):
        d = deps()
        d.docs.folder = "somewhere-else"
        self.assertEqual("out_of_scope", wk.run_once(d)["considered"][0]["status"])
        d = deps()
        d.docs.receipts = None
        d.docs.revision = ""
        self.assertEqual("no_revision", wk.run_once(d)["considered"][0]["status"])

        class Broken(Docs):
            def read(self, file_id):
                raise ConnectionError()
        d = deps()
        d.docs = Broken(REQUEST)
        self.assertEqual("read_error", wk.run_once(d)["considered"][0]["status"])
        self.assertEqual([], d.model.calls)


class AcceptanceTest(unittest.TestCase):
    """Guard 6: the model is called only after ACCEPTED is readable in the doc."""

    def test_an_accepted_line_that_does_not_read_back_means_no_call_and_the_lease_is_given_up(self):
        d = deps(receipts=Receipts(drop=True))
        out = wk.run_once(d)
        self.assertEqual("accepted_receipt_owed", out["considered"][0]["status"], out)
        self.assertEqual([], d.model.calls)
        self.assertEqual("accepted:0:0:0", d.claims.states[task_tuple()["task_id"]])
        self.assertEqual(0, d.ledger.spent("2026-10-11"))          # nothing reserved either
        # the doc comes back: the next pass accepts and runs
        d.receipts.drop = False
        out = wk.run_once(d)
        self.assertEqual("succeeded", out["considered"][0]["status"], out)
        self.assertEqual(1, len(d.model.calls))

    def test_a_lost_append_response_is_read_back_not_written_twice(self):
        d = deps(receipts=Receipts(lose=True))
        out = wk.run_once(d)
        self.assertEqual("succeeded", out["considered"][0]["status"], out)
        self.assertEqual(["ACCEPTED", "SUCCEEDED"], d.receipts.states())
        self.assertEqual(1, len(d.model.calls))

    def test_two_passes_on_one_task_call_the_model_once(self):
        class Racing(wk.MemoryClaims):
            def claim(self, task_id, initial):
                self.states[task_id] = "accepted:%d:1" % int(NOW - 5)   # the other pass got there first
                return False
        d = deps(claims=Racing())
        out = wk.run_once(d)
        self.assertEqual("in_progress", out["considered"][0]["status"], out)
        self.assertEqual([], d.model.calls)
        self.assertEqual([], d.receipts.lines)

    def test_a_lease_ahead_of_this_clock_is_live_not_dead(self):
        d = deps()
        d.claims.states[task_tuple()["task_id"]] = "accepted:%d:1" % int(NOW + 30)
        self.assertEqual("in_progress", wk.run_once(d)["considered"][0]["status"])
        self.assertEqual([], d.model.calls)

    def test_a_dead_lease_with_the_attempts_spent_writes_unknown_and_never_calls(self):
        d = deps()
        d.claims.states[task_tuple()["task_id"]] = "accepted:%d:1" % int(NOW - 601)
        out = wk.run_once(d)
        self.assertEqual("unknown", out["considered"][0]["status"], out)
        self.assertEqual(["UNKNOWN"], d.receipts.states())
        self.assertIn("attempt 1 left no result", d.receipts.lines[0][2])
        self.assertEqual([], d.model.calls)
        self.assertEqual("idle", wk.run_once(d)["status"])

    def test_a_dead_lease_with_an_attempt_left_runs_once_more(self):
        d = deps(max_attempts=2)
        t = task_tuple()
        d.docs.text += wk.receipt_line("ACCEPTED", t, "2026-10-11T04:10:00Z", "feedfeedfeedfeed", "attempt 1") + chr(10)
        d.claims.states[t["task_id"]] = "accepted:%d:1" % int(NOW - 601)
        out = wk.run_once(d)
        self.assertEqual("succeeded", out["considered"][0]["status"], out)
        self.assertEqual(1, len(d.model.calls))
        self.assertEqual("done", d.claims.states[task_tuple()["task_id"]])


class SpendTest(unittest.TestCase):
    """Decision 2: the cap holds. No reservation, no call; an unknown outcome keeps its reservation."""

    def test_no_cap_configured_calls_nothing_and_says_why_in_the_doc(self):
        d = deps(cap_cents=0)
        out = wk.run_once(d)
        self.assertEqual("capped", out["considered"][0]["status"], out)
        self.assertEqual([], d.model.calls)
        self.assertEqual(["ACCEPTED", "QUEUED"], d.receipts.states())
        self.assertIn("no spend cap or reservation configured", d.receipts.lines[1][2])

    def test_the_day_cap_reached_queues_and_the_next_day_runs(self):
        d = deps(cap_cents=10, reserve_cents=5)
        d.ledger.days["2026-10-11"] = 6                             # the fleet already spent most of it
        out = wk.run_once(d)
        self.assertEqual("capped", out["considered"][0]["status"], out)
        self.assertEqual([], d.model.calls)
        self.assertIn("daily spend cap reached (6 of 10 cents CAD", d.receipts.lines[1][2])
        self.assertEqual(6, d.ledger.spent("2026-10-11"))          # the refused reservation left nothing
        self.assertEqual("capped", wk.run_once(d)["considered"][0]["status"])   # same day: still capped
        d.day = lambda now: "2026-10-12"
        out = wk.run_once(d)
        self.assertEqual("succeeded", out["considered"][0]["status"], out)
        self.assertEqual(1, len(d.model.calls))
        self.assertIn("attempt 1", d.receipts.lines[0][2])         # a refused call is not an attempt

    def test_a_reservation_is_made_before_the_call_and_settled_to_the_price_after(self):
        seen = []

        class Watching(Model):
            def __call__(self, system, prompt):
                seen.append(d.ledger.spent("2026-10-11"))
                return super().__call__(system, prompt)
        d = deps(model=Watching(), reserve_cents=5, price_in=1.0, price_out=5.0, cad_per_usd=1.4)
        wk.run_once(d)
        self.assertEqual([5], seen)                                 # reserved BEFORE the call
        # 1200 in * $1/M + 300 out * $5/M = $0.0027 * 1.4 = 0.378 cents -> 1 cent, rounded UP
        self.assertEqual(1, d.ledger.spent("2026-10-11"))
        self.assertIn("1 cents CAD", d.receipts.lines[1][2])
        self.assertEqual("1", d.ledger.calls[0][1]["settled"])

    def test_without_a_price_the_reservation_stands(self):
        d = deps(reserve_cents=7)
        wk.run_once(d)
        self.assertEqual(7, d.ledger.spent("2026-10-11"))
        self.assertIn("7 cents CAD reserved", d.receipts.lines[1][2])
        self.assertEqual("0", d.ledger.calls[0][1]["settled"])

    def test_a_timeout_is_unknown_keeps_the_reservation_and_is_never_run_again(self):
        d = deps(model=Model(fail=wk.ModelUnknown("timeout")), reserve_cents=5)
        out = wk.run_once(d)
        self.assertEqual("unknown", out["considered"][0]["status"], out)
        self.assertEqual(["ACCEPTED", "UNKNOWN"], d.receipts.states())
        self.assertIn("the reservation stands", d.receipts.lines[1][2])
        self.assertEqual(5, d.ledger.spent("2026-10-11"))
        self.assertEqual("idle", wk.run_once(d)["status"])
        self.assertEqual(1, len(d.model.calls))

    def test_a_model_error_is_failed_and_final_and_keeps_the_reservation(self):
        d = deps(model=Model(fail=wk.ModelError("unparseable answer")), reserve_cents=5)
        out = wk.run_once(d)
        self.assertEqual("failed", out["considered"][0]["status"], out)
        self.assertIn("unparseable answer", d.receipts.lines[1][2])
        self.assertEqual(5, d.ledger.spent("2026-10-11"))             # a 200 was billed
        self.assertEqual("idle", wk.run_once(d)["status"])

    def test_an_http_refusal_refunds_keeps_the_task_open_and_a_fixed_key_runs_it(self):
        """Cursor on 9d46250: a 401 or 529 is not an answer and not billed; FAILED closed the task
        with the reservation kept, and fixing the key reopened nothing."""
        d = deps(model=Model(fail=wk.ModelRejected("HTTP 401")), reserve_cents=5)
        out = wk.run_once(d)
        self.assertEqual("rejected", out["considered"][0]["status"], out)
        self.assertEqual(1, out["considered"][0]["rejections"])
        self.assertEqual(0, d.ledger.spent("2026-10-11"))             # refunded
        self.assertEqual(["ACCEPTED"], d.receipts.states())            # nothing final in the doc
        self.assertEqual("accepted:0:0:1", d.claims.states[task_tuple()["task_id"]])
        d.model = Model()                                              # the key is fixed
        out = wk.run_once(d)
        self.assertEqual("succeeded", out["considered"][0]["status"], out)
        self.assertIn("attempt 1", d.receipts.lines[0][2])             # a refusal was not an attempt
        self.assertEqual(5, d.ledger.spent("2026-10-11"))

    def test_refusals_are_bounded_then_failed_with_nothing_billed(self):
        d = deps(model=Model(fail=wk.ModelRejected("HTTP 529")), reserve_cents=5)
        for n in (1, 2):
            out = wk.run_once(d)
            self.assertEqual("rejected", out["considered"][0]["status"], out)
            self.assertEqual(n, out["considered"][0]["rejections"])
        out = wk.run_once(d)
        self.assertEqual("failed", out["considered"][0]["status"], out)
        self.assertIn("refused 3 times, last HTTP 529; nothing was billed", d.receipts.lines[1][2])
        self.assertEqual(0, d.ledger.spent("2026-10-11"))
        self.assertEqual(3, len(d.model.calls))
        self.assertEqual("idle", wk.run_once(d)["status"])
        self.assertEqual(3, len(d.model.calls))

    def test_the_adapter_classes_a_non_200_as_a_refusal(self):
        m = wk.AnthropicModel(key="k", model="m", post=lambda *a: (401, b""))
        with self.assertRaises(wk.ModelRejected):
            m("s", "p")

    def test_an_unconfigured_model_refunds_and_leaves_the_task_for_a_configured_pass(self):
        d = deps(model=None, reserve_cents=5)
        out = wk.run_once(d)
        self.assertEqual("unconfigured", out["considered"][0]["status"], out)
        self.assertIn("CLAUDE_API_WORKER_MODEL", out["considered"][0]["error"])
        self.assertEqual(0, d.ledger.spent("2026-10-11"))
        self.assertEqual(["ACCEPTED"], d.receipts.states())
        d.model = Model()
        self.assertEqual("succeeded", wk.run_once(d)["considered"][0]["status"])

    def test_actual_cents_rounds_up_and_needs_every_price(self):
        self.assertEqual(1, wk.actual_cents(1200, 300, 1.0, 5.0, 1.4))
        self.assertEqual(0, wk.actual_cents(0, 0, 1.0, 5.0, 1.4))
        self.assertIsNone(wk.actual_cents(1200, 300, None, 5.0, 1.4))
        self.assertIsNone(wk.actual_cents(1200, 300, 1.0, 5.0, None))
        self.assertEqual(141, wk.actual_cents(1_000_000, 0, 1.0, 5.0, 1.4000001))


class ResultTest(unittest.TestCase):
    """Guards 2 and 5: an answer is kept before its receipt; its lines can forge nothing."""

    def test_an_answer_whose_receipt_did_not_land_is_written_later_without_a_second_call(self):
        d = deps()
        d.receipts.drop = False
        calls = []

        class DropAfterAccepted(Receipts):
            def append(self, file_id, expected_revision, receipt_id, block):
                if block.lstrip("\n").startswith("SUCCEEDED"):
                    calls.append(block)
                    return self.revision                          # vanished
                return super().append(file_id, expected_revision, receipt_id, block)
        r = DropAfterAccepted()
        d = deps(receipts=r)
        out = wk.run_once(d)
        self.assertEqual("terminal_receipt_owed", out["considered"][0]["status"], out)
        task = task_tuple()["task_id"]
        self.assertTrue(d.claims.states[task].startswith("resulted:"))
        self.assertIn("The answer.", d.claims.results[task])
        # the doc accepts writes again; the next pass writes the SAME answer and calls nobody
        d.receipts = Receipts(revision=r.revision)
        d.docs.receipts = d.receipts
        for _, rid, block in r.lines:
            d.receipts.lines.append((FILE, rid, block))
        out = wk.run_once(d)
        self.assertEqual("succeeded", out["considered"][0]["status"], out)
        self.assertEqual(1, len(d.model.calls))
        self.assertIn("\n> The answer.", d.receipts.lines[-1][2])

    def test_a_dead_lease_beside_a_stored_answer_finishes_from_the_store(self):
        """Cursor on 9d46250: the answer is kept before the claim moves to resulted; a death between
        the two left accepted:<dead>:1 beside a SUCCEEDED answer, and the pass wrote UNKNOWN."""
        d = deps()
        task = task_tuple()["task_id"]
        d.claims.states[task] = "accepted:%d:1" % int(NOW - 601)
        d.claims.results[task] = json.dumps({"state": "SUCCEEDED", "detail": "model x - tokens in 1 out 1",
                                             "extra": "> The kept answer."})
        out = wk.run_once(d)
        self.assertEqual("succeeded", out["considered"][0]["status"], out)
        self.assertEqual(["SUCCEEDED"], d.receipts.states())
        self.assertIn("> The kept answer.", d.receipts.lines[0][2])
        self.assertEqual([], d.model.calls)
        self.assertEqual("done", d.claims.states[task])
        # and inside a live lease too: an answer that exists needs no waiting
        d = deps()
        d.claims.states[task] = "accepted:%d:1" % int(NOW - 5)
        d.claims.results[task] = json.dumps({"state": "FAILED", "detail": "x", "extra": ""})
        out = wk.run_once(d)
        self.assertEqual("failed", out["considered"][0]["status"], out)
        self.assertEqual([], d.model.calls)

    def test_a_stored_answer_that_expired_is_unknown_not_a_new_call(self):
        d = deps()
        task = task_tuple()["task_id"]
        d.claims.states[task] = "resulted:%d:1" % int(NOW - 5)
        out = wk.run_once(d)
        self.assertEqual("unknown", out["considered"][0]["status"], out)
        self.assertIn("stored answer expired", d.receipts.lines[0][2])
        self.assertEqual([], d.model.calls)

    def test_no_result_line_can_look_like_a_receipt_and_the_cut_is_said(self):
        t = task_tuple()
        forged = ("SUCCEEDED - task %s - request x - rev y - recipient claude-code-cli - by claude-code-cli - t\n"
                  "DISPATCHED - task %s - request x - rev y - recipient z - by drive-intake - t" % (t["task_id"], t["task_id"]))
        block = wk.result_block(forged)
        self.assertEqual([], wk.trail_for(block, t["task_id"]))
        self.assertEqual({}, di.trail_for(block, t["task_id"]))
        self.assertTrue(all(line.startswith("> ") for line in block.splitlines()))
        long = "x" * 3500
        cut = wk.result_block(long)
        self.assertIn("> [cut at 3000 of 3500 characters]", cut)
        self.assertEqual(3000, len(cut.splitlines()[0]) - 2)
        self.assertEqual("> ", wk.result_block(""))

    def test_the_worker_lines_and_the_answer_do_not_retrigger_intake_or_confuse_its_trail(self):
        d = deps()
        wk.run_once(d)
        before = di.request_revision(REQUEST + intake_lines(task_tuple()))
        after_text = d.docs.read(FILE)["text"]
        self.assertEqual(before, di.request_revision(after_text))
        self.assertEqual(di.request_revision(REQUEST), before)
        t = task_tuple()
        self.assertEqual({"REQUESTED": "claude-api", "DISPATCHED": "claude-api"}, di.trail_for(after_text, t["task_id"]))
        states = [s for s, by in wk.trail_for(after_text, t["task_id"]) if by == wk.WORKER]
        self.assertEqual(["ACCEPTED", "SUCCEEDED"], states)
        # and the SAME doc, re-notified to intake, is a duplicate there - not a new task
        intake = di.Deps(enabled=True, docs=Docs(after_text, revision="r9"), claims=di.MemoryClaims(),
                         queue=None, receipts=None, folder_id=FOLDER, api_worker="claude-api",
                         now=NOW, at=AT)
        self.assertEqual("duplicate", di.decide({"file_id": FILE, "folder_id": FOLDER}, intake)["status"])

    def test_the_request_given_to_the_model_is_bounded_and_says_when_it_is_cut(self):
        text = REQUEST + "y" * 20000
        prompt = wk.request_for_model(text)
        self.assertTrue(prompt.endswith("[request cut at 12000 of %d characters]" % len(text.strip())))
        self.assertEqual(REQUEST.strip(), wk.request_for_model(REQUEST + "\nRECEIPTS\nx\n"))

    def test_an_empty_answer_is_failed_not_succeeded(self):
        d = deps(model=Model(text="   "))
        out = wk.run_once(d)
        self.assertEqual("failed", out["considered"][0]["status"], out)
        self.assertIn("empty answer", d.receipts.lines[1][2])


class ReceiptTest(unittest.TestCase):
    def test_receipt_ids_are_per_worker_task_and_state_and_differ_from_intakes(self):
        t = task_tuple()
        self.assertNotEqual(wk.receipt_id_for(t["task_id"], "QUEUED"), di.receipt_id_for(t, "QUEUED"))
        self.assertNotEqual(wk.receipt_id_for(t["task_id"], "ACCEPTED"), wk.receipt_id_for(t["task_id"], "SUCCEEDED"))
        self.assertEqual(wk.receipt_id_for(t["task_id"], "ACCEPTED"), wk.receipt_id_for(t["task_id"], "ACCEPTED"))

    def test_the_line_carries_its_id_last_and_parses_in_the_shared_shape(self):
        t = task_tuple()
        rid = wk.receipt_id_for(t["task_id"], "ACCEPTED")
        line = wk.receipt_line("ACCEPTED", t, AT, rid, "attempt 1")
        self.assertTrue(line.endswith(" - id " + rid), line)
        self.assertEqual([("ACCEPTED", "claude-api")], wk.trail_for(line, t["task_id"]))
        self.assertEqual("ACCEPTED - task %s - request OT-20261010 - rev %s - recipient claude-api - by claude-api - %s - attempt 1 - id %s"
                         % (t["task_id"], t["source_revision"][:12], AT, rid), line)

    def test_intakes_lines_now_carry_their_ids_too(self):
        intake = di.Deps(enabled=True, docs=Docs(REQUEST), claims=di.MemoryClaims(), queue=_IntakeQueue(),
                         receipts=Receipts(), ring=lambda t: None, folder_id=FOLDER, api_worker="claude-api",
                         now=NOW, at=AT)
        out = di.decide({"file_id": FILE, "folder_id": FOLDER}, intake)
        self.assertEqual("dispatched", out["status"], out)
        for _, rid, block in intake.receipts.lines:
            self.assertTrue(block.rstrip().endswith(" - id " + rid), block)

    def test_a_heading_is_written_once_when_the_doc_has_none(self):
        r = Receipts()
        d = deps(receipts=r)
        d.docs.text = REQUEST                                       # intake's lines never landed
        wk.run_once(d)
        self.assertTrue(r.lines[0][2].startswith("\nRECEIPTS\n"))
        self.assertFalse(r.lines[1][2].startswith("\nRECEIPTS"))

    def test_terminal_without_a_writer_is_still_done_and_without_a_store_nothing_runs(self):
        d = deps()
        d.receipts = None
        out = wk.run_once(d)
        self.assertEqual("succeeded", out["considered"][0]["status"], out)
        self.assertEqual("no_receipt_writer", out["considered"][0]["receipt"])
        self.assertEqual("no_store", wk.run_once(deps(ledger=None))["status"])
        self.assertEqual("no_store", wk.run_once(deps(inbox=None))["status"])


class _IntakeQueue:
    def __init__(self):
        self.puts = []

    def holder(self, task_id):
        return None

    def put(self, tuple_, gist_, at):
        self.puts.append(tuple_)
        return True


class RedisShapeTest(unittest.TestCase):
    """The Redis adapters against a tiny emulation of the few commands they use."""

    class Client:
        def __init__(self):
            self.kv, self.ttl, self.streams, self.evals = {}, {}, {}, []

        def get(self, key):
            return self.kv.get(key)

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.kv:
                return None
            self.kv[key] = str(value)
            if ex:
                self.ttl[key] = ex
            return True

        def incrby(self, key, by):
            self.kv[key] = str(int(self.kv.get(key) or 0) + int(by))
            return int(self.kv[key])

        def expire(self, key, seconds):
            self.ttl[key] = seconds

        def xadd(self, key, fields):
            self.streams.setdefault(key, []).append(dict(fields))

        def xrevrange(self, key, count=None):
            rows = [("%d-0" % (i + 1), f) for i, f in enumerate(self.streams.get(key, []))]
            return list(reversed(rows))[:count]

        def eval(self, script, nkeys, *args):
            self.evals.append((script, args))
            keys, argv = args[:nkeys], args[nkeys:]
            if script == wk.CAS_LUA:
                if self.kv.get(keys[0]) == argv[0]:
                    self.set(keys[0], argv[1], ex=argv[2])
                    return 1
                return 0
            if script == wk.RESERVE_LUA:
                value = self.incrby(keys[0], argv[0])
                self.expire(keys[0], argv[2])
                if value > int(argv[1]):
                    self.incrby(keys[0], -int(argv[0]))
                    return 0
                return 1
            raise AssertionError("unknown script")

    def test_claims_live_under_fleet_worker_and_the_heartbeat_under_fleet_alive(self):
        c = self.Client()
        claims = wk.RedisClaims(c)
        self.assertTrue(claims.claim("task-1", "accepted:1:1"))
        self.assertFalse(claims.claim("task-1", "accepted:2:1"))
        self.assertEqual("accepted:1:1", claims.state("task-1"))
        self.assertTrue(claims.advance("task-1", "accepted:1:1", "done"))
        self.assertFalse(claims.advance("task-1", "accepted:1:1", "done"))
        self.assertEqual(wk.CLAIM_TTL, c.ttl["fleet:worker:claim:task-1"])
        self.assertFalse(claims.done("task-1"))
        claims.mark_done("task-1")
        self.assertTrue(claims.done("task-1"))
        self.assertEqual(wk.DONE_TTL, c.ttl["fleet:worker:done:task-1"])
        claims.keep_result("task-1", "{}")
        self.assertEqual("{}", claims.result("task-1"))
        self.assertEqual(wk.RESULT_TTL, c.ttl["fleet:worker:result:task-1"])
        claims.beat("claude-api", 1234.9)
        self.assertEqual("1234", c.kv["fleet:alive:claude-api"])
        self.assertEqual(1234.0, claims.heartbeat("claude-api"))
        self.assertIsNone(claims.heartbeat("claude-code-cli"))

    def test_the_ledger_reserves_in_one_script_and_takes_back_what_crosses_the_cap(self):
        c = self.Client()
        ledger = wk.RedisLedger(c)
        self.assertTrue(ledger.reserve("2026-10-11", 5, 10))
        self.assertTrue(ledger.reserve("2026-10-11", 5, 10))
        self.assertFalse(ledger.reserve("2026-10-11", 5, 10))
        self.assertEqual(10, ledger.spent("2026-10-11"))            # the refused one left nothing behind
        self.assertFalse(ledger.reserve("2026-10-11", 0, 10))
        self.assertFalse(ledger.reserve("2026-10-11", 5, 0))
        self.assertEqual(wk.SPEND_TTL, c.ttl["fleet:spend:2026-10-11"])
        ledger.settle("2026-10-11", -4)
        self.assertEqual(6, ledger.spent("2026-10-11"))
        ledger.settle("2026-10-11", -100)
        self.assertEqual(0, ledger.spent("2026-10-11"))
        ledger.record("2026-10-11", {"by": "claude-api", "cents": "1"})
        self.assertEqual([{"by": "claude-api", "cents": "1"}], c.streams["fleet:spend:2026-10-11:calls"])

    def test_the_inbox_reads_newest_first_and_decodes_bytes(self):
        c = self.Client()
        c.streams["fleet:inbox:claude-api"] = [{b"task_id": b"task-a"}, {"task_id": "task-b"}]
        rows = wk.RedisInbox(c).read("claude-api", 10)
        self.assertEqual([("2-0", {"task_id": "task-b"}), ("1-0", {"task_id": "task-a"})], rows)

    def test_entry_time_prefers_the_stream_clock_then_the_writers_at(self):
        self.assertEqual(1760000000.5, wk.entry_time("1760000000500-0", {}))
        self.assertEqual(1760000000.0, wk.entry_time("bad", {"at": "2025-10-09T08:53:20Z"}))
        self.assertIsNone(wk.entry_time("bad", {"at": "soon"}))


class ModelAdapterTest(unittest.TestCase):
    def test_the_request_shape_and_the_answer_parsed_with_usage(self):
        sent = []

        def post(url, body, headers, timeout):
            sent.append((url, json.loads(body), headers, timeout))
            return 200, json.dumps({"content": [{"type": "thinking", "thinking": "hm"},
                                                {"type": "text", "text": "Answer."}],
                                    "usage": {"input_tokens": 12, "output_tokens": 3},
                                    "model": "claude-haiku-5-5", "stop_reason": "end_turn"}).encode()
        m = wk.AnthropicModel(key="k", model="claude-haiku-5-5", max_tokens=777, post=post)
        out = m("sys", "ask")
        self.assertEqual({"text": "Answer.", "input_tokens": 12, "output_tokens": 3,
                          "model": "claude-haiku-5-5", "stop_reason": "end_turn"}, out)
        url, body, headers, timeout = sent[0]
        self.assertEqual(wk.AnthropicModel.URL, url)
        self.assertEqual({"model": "claude-haiku-5-5", "max_tokens": 777, "system": "sys",
                          "messages": [{"role": "user", "content": "ask"}]}, body)
        self.assertEqual("k", headers["x-api-key"])
        self.assertEqual(wk.MODEL_TIMEOUT, timeout)

    def test_no_key_or_model_is_unconfigured_before_any_post(self):
        for key, model in (("", "m"), ("k", "")):
            m = wk.AnthropicModel(key=key, model=model, post=lambda *a: (200, b"{}"))
            with self.assertRaises(wk.Unconfigured):
                m("s", "p")

    def test_errors_by_class(self):
        m = wk.AnthropicModel(key="k", model="m", post=lambda *a: (529, b""))
        with self.assertRaises(wk.ModelRejected):
            m("s", "p")
        m = wk.AnthropicModel(key="k", model="m", post=lambda *a: (200, b'{"content": [], "stop_reason": "max_tokens"}'))
        with self.assertRaisesRegex(wk.ModelError, "max_tokens"):
            m("s", "p")
        m = wk.AnthropicModel(key="k", model="m", post=lambda *a: (200, b"not json"))
        with self.assertRaises(wk.ModelError):
            m("s", "p")

    def test_the_default_deps_have_no_model_and_say_which_env_is_missing(self):
        with self.assertRaisesRegex(wk.Unconfigured, "CLAUDE_API_WORKER_MODEL"):
            wk.Deps().model("s", "p")


class GoogleDocsTest(unittest.TestCase):
    def test_extract_text_walks_paragraphs_and_tables_in_order(self):
        body = {"content": [
            {"paragraph": {"elements": [{"textRun": {"content": "a\n"}}, {"textRun": {"content": "b\n"}}]}},
            {"table": {"tableRows": [{"tableCells": [{"content": [{"paragraph": {"elements": [{"textRun": {"content": "cell\n"}}]}}]}]}]}},
            {"sectionBreak": {}},
            {"paragraph": {"elements": [{"inlineObjectElement": {}}, {"textRun": {"content": "c\n"}}]}},
        ]}
        self.assertEqual("a\nb\ncell\nc\n", gd.extract_text(body))
        self.assertEqual("", gd.extract_text({}))

    def test_append_is_one_guarded_insert_at_the_end_on_its_own_line(self):
        req = gd.append_request("LINE", "rev-7")
        self.assertEqual({"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": "\nLINE"}}],
                          "writeControl": {"requiredRevisionId": "rev-7"}}, req)
        self.assertEqual("\nRECEIPTS\nLINE", gd.append_request("\nRECEIPTS\nLINE", "r")["requests"][0]["insertText"]["text"])
        self.assertNotIn("writeControl", gd.append_request("x", ""))

    def test_read_append_and_has_against_a_fake_docs_api(self):
        doc = {"revisionId": "r1", "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "hello\n"}}]}}]}}
        calls = []

        def fetch(method, url, body, headers):
            calls.append((method, url, body, headers))
            self.assertEqual("Bearer tok", headers["Authorization"])
            if method == "GET" and "docs.googleapis.com" in url:
                return 200, json.dumps(doc).encode()
            if method == "GET" and "googleapis.com/drive" in url:
                return 200, b'{"parents": ["FOLDER1"]}'
            if method == "POST":
                sent = json.loads(body)
                if sent["writeControl"]["requiredRevisionId"] != doc["revisionId"]:
                    return 400, b'{"error": {"message": "the revision ID does not match"}}'
                doc["body"]["content"].append({"paragraph": {"elements": [{"textRun": {"content": sent["requests"][0]["insertText"]["text"].lstrip("\n") + "\n"}}]}})
                doc["revisionId"] = "r2"
                return 200, b'{"writeControl": {"requiredRevisionId": "r2"}}'
            raise AssertionError(url)
        g = gd.GoogleDocs(token_provider=lambda: "tok", fetch=fetch)
        self.assertEqual({"text": "hello\n", "revision_id": "r1", "folder_id": "FOLDER1"}, g.read(FILE))
        self.assertFalse(g.has(FILE, "abcd"))
        self.assertEqual("r2", g.append(FILE, "r1", "abcd", "ACCEPTED - task x - id abcd"))
        self.assertTrue(g.has(FILE, "abcd"))
        with self.assertRaises(gd.RevisionConflict):
            g.append(FILE, "r1", "efgh", "stale")
        self.assertTrue(all(FILE in url for _, url, _, _ in calls))

    def test_no_token_refuses_before_any_call(self):
        g = gd.GoogleDocs(token_provider=lambda: "", fetch=lambda *a: self.fail("called"))
        with self.assertRaises(gd.Unconfigured):
            g.read(FILE)
        with self.assertRaises(gd.Unconfigured):
            gd.metadata_token(fetch=lambda *a: (404, b""))
        self.assertEqual("t", gd.metadata_token(fetch=lambda m, u, b, h: (200, b'{"access_token": "t"}')))


if __name__ == "__main__":
    unittest.main()

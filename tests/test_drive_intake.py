"""Drive intake (cloud/drive-intake/main.py): one doc, one correlated task, one honest receipt.

The ways this goes wrong, each with a test that fails when the guard is removed:
a receipt that retriggers its own task, the same change arriving twice, a
heartbeat flip minting a second task for the same request, queued work lost
when the laptop returns, a failed ring never rung again, a crash between the
put and the claim re-putting to a second worker, two overlapping passes both
delivering, a hand-off or receipt whose response was lost, "neither worker"
happening silently, a receipt that claims more than the state reached, a
stale write revision on the second receipt, a busy inbox hiding the newest
entry, and a watch channel that expires with /healthz still saying ok.

Offline by construction: every outside call is an injected adapter.
"""
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("drive_intake_main", REPO / "cloud" / "drive-intake" / "main.py")
di = importlib.util.module_from_spec(spec)
spec.loader.exec_module(di)

FOLDER = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"
FILE = "1pKWJYbJzMqxSA9Vy_PSR93BU2OAAgP-N3WBEkCj7oj4"
REQUEST = "SFDC24 - OWNER TRIGGER\n\nrequest: OT-20261010\n\nFix the broken telephone first.\n"
RECEIPTS = "\n\nRECEIPTS\n"


class Docs:
    """A doc store whose read is counted, so a flag-off path can prove it read nothing.

    When bound to a Receipts double it reports that double's current revision,
    the way a real doc does after an append."""

    def __init__(self, text=REQUEST, revision="r1", folder=FOLDER, receipts=None):
        self.text, self.revision, self.folder, self.receipts = text, revision, folder, receipts
        self.reads = 0

    def read(self, file_id):
        self.reads += 1
        revision = self.receipts.revision if self.receipts is not None else self.revision
        return {"text": self.text, "revision_id": revision, "folder_id": self.folder}


class RevisionConflict(Exception):
    pass


class Receipts:
    """A revision-guarded append that returns the new revision, then a read-back.

    `lose` makes the append raise AFTER landing; `drop` makes it vanish.
    `guard` makes a stale expected revision a conflict, the way a real Docs
    adapter behaves."""

    def __init__(self, lose=False, drop=False, guard=False, revision="r1"):
        self.lines, self.lose, self.drop, self.guard = [], lose, drop, guard
        self.revision, self.n, self.appends = revision, 1, 0

    def append(self, file_id, expected_revision, receipt_id, block):
        self.appends += 1
        if self.guard and expected_revision != self.revision:
            raise RevisionConflict("expected %s, doc is at %s" % (expected_revision, self.revision))
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
        return [b.strip().splitlines()[-1].split(" - ")[0] for _, _, b in self.lines]

    def text(self):
        """What the doc looks like with every appended block in it."""
        return "".join(b if b.startswith("\n") else "\n" + b for _, _, b in self.lines) + "\n"


class Queue:
    """An inbox that can be read back by task id, like fleet:inbox."""

    def __init__(self, fail=None, fail_after=None):
        self.puts, self.fail, self.fail_after = [], fail, fail_after

    def has(self, recipient, task_id):
        return any(t["recipient_id"] == recipient and t["task_id"] == task_id for t, _, _ in self.puts)

    def put(self, tuple_, gist_, at):
        if self.fail:
            raise self.fail
        self.puts.append((dict(tuple_), dict(gist_), at))
        if self.fail_after:
            raise self.fail_after

    def recipients(self):
        return [t["recipient_id"] for t, _, _ in self.puts]


class Ring:
    def __init__(self, fail_first=0):
        self.calls, self.fail_first = [], fail_first

    def __call__(self, t):
        self.calls.append(dict(t))
        if len(self.calls) <= self.fail_first:
            raise TimeoutError("wake timed out")


def deps(**over):
    base = dict(enabled=True, docs=Docs(), claims=di.MemoryClaims(), queue=Queue(),
                receipts=Receipts(), ring=Ring(), folder_id=FOLDER, channel_token="tok",
                poll_token="poll", api_worker="claude-api", now=1000.0, at="2026-10-10T21:00:00Z")
    base.update(over)
    return di.Deps(**base)


def hint(file_id=FILE, folder=FOLDER):
    return {"file_id": file_id, "folder_id": folder}


def laptop_up(d, at=990.0):
    d.claims.beats["claude-code-cli"] = at


def laptop_down(d):
    d.claims.beats.pop("claude-code-cli", None)


def key_for(recipient="claude-api", text=REQUEST):
    return di.idempotency_key(di.correlate(FILE, text, "r1", recipient))


class FlagTest(unittest.TestCase):
    def test_anything_but_exactly_on_is_off(self):
        for value in (None, "", "ON", "On", "1", "true", "yes", " on"):
            env = {} if value is None else {"DRIVE_INTAKE_ENABLED": value}
            self.assertFalse(di.enabled_from_env(env), repr(value))
        self.assertTrue(di.enabled_from_env({"DRIVE_INTAKE_ENABLED": "on"}))

    def test_off_reads_no_doc_claims_nothing_and_writes_no_receipt(self):
        d = deps(enabled=False)
        self.assertEqual("disabled", di.decide(hint(), d)["status"])
        self.assertEqual(0, d.docs.reads)
        self.assertEqual({}, d.claims.states)
        self.assertEqual([], d.receipts.lines)
        self.assertEqual([], d.queue.puts)
        self.assertEqual((200, {"enabled": False, "intake": []}),
                         di.handle("POST", "/hooks/drive", {"X-Goog-Channel-Token": "tok",
                                                           "X-Goog-Resource-State": "update"}, b"", d))
        self.assertEqual([], di.poll(d))


class RetriggerTest(unittest.TestCase):
    """Guard 1: the receipt this service writes must never look like a new request."""

    def test_the_request_revision_ignores_everything_after_the_receipts_heading(self):
        bare = di.request_revision(REQUEST)
        with_receipts = di.request_revision(REQUEST + RECEIPTS + "REQUESTED - task x - ...\n")
        self.assertEqual(bare, with_receipts)
        self.assertEqual(bare, di.request_revision(REQUEST + "\n\n## Receipts:\n\nDONE by x\n"))
        self.assertEqual(bare, di.request_revision(REQUEST + "\nConverspan receipts\nline\n"))

    def test_a_line_in_our_own_receipt_shape_is_a_receipt_even_without_a_heading(self):
        line = "REQUESTED - task task-0123456789abcdef - request x - rev y - recipient z - by drive-intake - t\n"
        self.assertEqual(di.request_revision(REQUEST), di.request_revision(REQUEST + "\n" + line))

    def test_an_invisible_edit_is_not_a_new_request(self):
        self.assertEqual(di.request_revision(REQUEST),
                         di.request_revision(REQUEST.replace("\n", "   \r\n") + "\n\n\n"))

    def test_a_real_edit_is_a_new_request(self):
        self.assertNotEqual(di.request_revision(REQUEST),
                            di.request_revision(REQUEST.replace("telephone", "telephone today")))

    def test_our_own_receipt_coming_back_through_drive_is_a_duplicate_not_a_task(self):
        d = deps()
        first = di.decide(hint(), d)
        self.assertEqual("dispatched", first["status"], first)
        # Drive now notifies us of OUR append: new revisionId, receipts section grown.
        d.docs.text = REQUEST + RECEIPTS + d.receipts.lines[0][2] + "\n"
        d.docs.revision = "r2"
        again = di.decide(hint(), d)
        self.assertEqual("duplicate", again["status"], again)
        self.assertEqual(first["task"], again["task"])
        self.assertEqual(1, len(d.queue.puts))

    def test_the_block_actually_appended_to_a_doc_with_no_heading_does_not_retrigger(self):
        """Cursor on 05a8079: the appended LINE carried no heading, so fed back it minted a task."""
        d = deps(docs=Docs(text=REQUEST))                       # no RECEIPTS heading anywhere
        first = di.decide(hint(), d)
        self.assertEqual("dispatched", first["status"])
        self.assertTrue(d.receipts.lines[0][2].startswith("\nRECEIPTS\n"), d.receipts.lines[0][2])
        self.assertFalse(d.receipts.lines[1][2].startswith("\nRECEIPTS"))   # heading written once
        d.docs.text = REQUEST + d.receipts.text()               # exactly what the adapter appended
        d.docs.revision = d.receipts.revision
        again = di.decide(hint(), d)
        self.assertEqual("duplicate", again["status"], (again, d.docs.text))
        self.assertEqual(1, len(d.queue.puts))

    def test_a_real_edit_after_the_receipt_is_a_new_task(self):
        d = deps()
        first = di.decide(hint(), d)
        d.docs.text = REQUEST.replace("first.", "first, then the showcase.") + RECEIPTS + "x\n"
        d.docs.revision = "r2"
        second = di.decide(hint(), d)
        self.assertEqual("dispatched", second["status"], second)
        self.assertNotEqual(first["task"], second["task"])
        self.assertEqual(2, len(d.queue.puts))


class DuplicateTest(unittest.TestCase):
    """Guard 2: push and poll see the same change; Drive retries; one task."""

    def test_push_then_poll_is_one_task(self):
        d = deps(changes=lambda folder: [{"file_id": FILE, "folder_id": folder}])
        pushed = di.handle("POST", "/hooks/drive", {
            "x-goog-channel-token": "tok", "x-goog-resource-state": "update",
            "x-goog-resource-uri": "https://www.googleapis.com/drive/v3/files/%s?alt=json" % FILE,
        }, b"", d)
        self.assertEqual("dispatched", pushed[1]["intake"][0]["status"], pushed)
        polled = di.handle("POST", "/poll", {"authorization": "Bearer poll"}, b"", d)
        self.assertEqual("duplicate", polled[1]["intake"][0]["status"], polled)
        self.assertEqual(1, len(d.queue.puts))

    def test_the_identity_is_the_request_and_not_the_worker_it_went_to(self):
        t = di.correlate(FILE, REQUEST, "r1", "claude-code-cli")
        for part in ("source_doc_id", "source_revision", "request_id", "task_id"):
            changed = dict(t)
            changed[part] = changed[part] + "x"
            self.assertNotEqual(di.idempotency_key(t), di.idempotency_key(changed), part)
        # the recipient is routing, not identity; the Drive write revision is concurrency, not identity
        self.assertEqual(di.idempotency_key(t), di.idempotency_key(dict(t, recipient_id="claude-api")))
        self.assertEqual(di.idempotency_key(t), di.idempotency_key(dict(t, drive_revision="r999")))
        self.assertEqual(t["task_id"], di.correlate(FILE, REQUEST, "r1", "gemini")["task_id"])

    def test_the_tuple_is_deterministic_and_the_request_line_is_honoured(self):
        a = di.correlate(FILE, REQUEST, "r1", "claude-code-cli")
        b = di.correlate(FILE, REQUEST, "r2", "claude-code-cli")
        self.assertEqual(a["task_id"], b["task_id"])
        self.assertEqual("OT-20261010", a["request_id"])
        no_line = di.correlate(FILE, "just words\n", "r1", "gemini")
        self.assertTrue(no_line["request_id"].startswith("req-"))
        self.assertTrue(di.tuple_ok(a))
        self.assertFalse(di.tuple_ok(dict(a, recipient_id="somebody")))
        self.assertFalse(di.tuple_ok(dict(a, source_doc_id="short")))
        self.assertFalse(di.tuple_ok(dict(a, drive_revision="")))

    def test_two_overlapping_passes_deliver_once(self):
        """Cursor on d150d84: push and poll both observed `intake` and each put to a worker."""
        class Racing(di.MemoryClaims):
            """The other pass wins every transition the first time it is tried."""
            def __init__(self):
                super().__init__()
                self.lost = []

            def advance(self, key, current, new):
                if current == di.INTAKE and not self.lost:
                    self.lost.append(new)
                    self.states[key] = "delivering:claude-code-cli"   # the other pass bound the laptop
                    return False
                return super().advance(key, current, new)
        d = deps(claims=Racing())
        out = di.decide(hint(), d)
        self.assertEqual("in_progress", out["status"], out)
        self.assertEqual([], d.queue.puts)                      # the loser did not put
        self.assertEqual([], d.receipts.lines)
        # the next pass finishes the OTHER pass's delivery, to the worker it bound
        later = di.decide(hint(), d)
        self.assertEqual("dispatched", later["status"], later)
        self.assertEqual(["claude-code-cli"], d.queue.recipients())

    def test_a_pass_that_loses_the_race_after_its_put_writes_nothing_and_rings_nobody(self):
        """Two resumes of one delivering state can both put (the residual window, named in the
        module docstring); the one that then loses the compare-and-set must stop there, or the
        doc gets two REQUESTED lines and the worker two wakes."""
        class LateLoser(di.MemoryClaims):
            def advance(self, key, current, new):
                if new.startswith(di.ON_INBOX):
                    self.states[key] = "rung:claude-api"              # the other pass got here first
                    return False
                return super().advance(key, current, new)
        d = deps(claims=LateLoser())
        out = di.decide(hint(), d)
        self.assertEqual("in_progress", out["status"], out)
        self.assertEqual([], d.receipts.lines)
        self.assertEqual([], d.ring.calls)


class FailoverTest(unittest.TestCase):
    """The owner's rule: one worker or the other, never neither, never silently, never both."""

    def test_laptop_up_takes_the_work(self):
        self.assertEqual(("claude-code-cli", "REQUESTED"), di.pick_worker(False, 12.0, "claude-api"))
        self.assertEqual(("claude-code-cli", "REQUESTED"), di.pick_worker(True, 12.0, ""))

    def test_laptop_down_and_laptop_only_tools_is_queued_by_name(self):
        self.assertEqual(("claude-code-cli", "QUEUED"), di.pick_worker(True, None, "claude-api"))

    def test_laptop_down_goes_to_the_named_api_worker_only_if_one_is_named(self):
        self.assertEqual(("claude-api", "REQUESTED"), di.pick_worker(False, None, "claude-api"))
        self.assertEqual(("claude-code-cli", "BLOCKED"), di.pick_worker(False, None, ""))
        self.assertEqual(("claude-code-cli", "BLOCKED"), di.pick_worker(False, None, "nobody"))

    def test_heartbeat_age_absent_stale_future_or_unreadable_is_offline(self):
        self.assertIsNone(di.heartbeat_age(None, 1000.0))
        self.assertIsNone(di.heartbeat_age(di.MemoryClaims(), 1000.0))
        self.assertIsNone(di.heartbeat_age(di.MemoryClaims({"claude-code-cli": 1000.0 - 301}), 1000.0))
        self.assertIsNone(di.heartbeat_age(di.MemoryClaims({"claude-code-cli": 1000.0 + 5}), 1000.0))
        self.assertIsNone(di.heartbeat_age(di.MemoryClaims({"claude-code-cli": "soon"}), 1000.0))
        self.assertEqual(299.0, di.heartbeat_age(di.MemoryClaims({"claude-code-cli": 701.0}), 1000.0))

        class Broken(di.MemoryClaims):
            def heartbeat(self, agent):
                raise ConnectionError()
        self.assertIsNone(di.heartbeat_age(Broken(), 1000.0))

    def test_a_heartbeat_flip_does_not_mint_a_second_task(self):
        """Cursor on 05a8079: laptop down then up dispatched the same doc twice, to two workers."""
        d = deps()
        first = di.decide(hint(), d)
        self.assertEqual(("dispatched", "claude-api"), (first["status"], first["tuple"]["recipient_id"]))
        laptop_up(d)
        again = di.decide(hint(), d)
        self.assertEqual("duplicate", again["status"], again)
        self.assertEqual(1, len(d.queue.puts))
        self.assertEqual(1, len(d.claims.states))
        # and the other order
        d = deps()
        laptop_up(d)
        first = di.decide(hint(), d)
        self.assertEqual("claude-code-cli", first["tuple"]["recipient_id"])
        laptop_down(d)
        self.assertEqual("duplicate", di.decide(hint(), d)["status"])
        self.assertEqual(1, len(d.queue.puts))

    def test_a_crash_between_the_put_and_the_claim_resumes_to_the_same_worker(self):
        """Cursor on d150d84: the resume re-routed to the laptop and put the task there too."""
        d = deps()
        t = di.correlate(FILE, REQUEST, "r1", "claude-api")
        d.claims.states[key_for()] = "delivering:claude-api"     # bound, put landed, then it died
        d.queue.puts.append((dict(t), di.gist(REQUEST), d.at))
        laptop_up(d)                                             # routing would now say the laptop
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(["claude-api"], d.queue.recipients())    # no second put, nowhere
        self.assertEqual("claude-api", out["tuple"]["recipient_id"])
        self.assertEqual(["claude-api"], [c["recipient_id"] for c in d.ring.calls])

    def test_a_crash_before_the_put_resumes_to_the_bound_worker(self):
        d = deps()
        d.claims.states[key_for()] = "delivering:claude-api"     # bound, then died before the put
        laptop_up(d)
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(["claude-api"], d.queue.recipients())

    def test_work_queued_for_the_laptop_is_delivered_when_the_laptop_returns(self):
        """Cursor on 05a8079: QUEUED kept its claim and the second pass was a duplicate; nothing ever queued."""
        d = deps(docs=Docs(text=REQUEST + "Use the sf CLI against the sandbox.\n"))
        first = di.decide(hint(), d)
        self.assertEqual("queued", first["status"])
        self.assertEqual([], d.queue.puts)
        self.assertEqual(["QUEUED"], d.receipts.states())
        self.assertIn("awaiting the laptop", d.receipts.lines[0][2])
        # still down: not news, no second receipt
        self.assertEqual("waiting", di.decide(hint(), d)["status"])
        self.assertEqual(["QUEUED"], d.receipts.states())
        # laptop returns: delivered ONCE, to the laptop, same task id
        laptop_up(d)
        later = di.decide(hint(), d)
        self.assertEqual("dispatched", later["status"], later)
        self.assertEqual(first["task"], later["task"])
        self.assertEqual("claude-code-cli", later["tuple"]["recipient_id"])
        self.assertEqual(1, len(d.queue.puts))
        self.assertEqual(["QUEUED", "REQUESTED", "DISPATCHED"], d.receipts.states())
        self.assertIn("now that a worker is online", d.receipts.lines[1][2])
        self.assertEqual("duplicate", di.decide(hint(), d)["status"])

    def test_blocked_writes_a_receipt_the_phone_can_read_then_delivers_when_a_worker_is_named(self):
        d = deps(api_worker="")
        out = di.decide(hint(), d)
        self.assertEqual("blocked", out["status"])
        self.assertEqual([], d.queue.puts)
        self.assertEqual(["BLOCKED"], d.receipts.states())
        self.assertIn("no capable worker online", d.receipts.lines[0][2])
        self.assertEqual("waiting", di.decide(hint(), d)["status"])     # unchanged doc: not written twice
        d.api_worker = "claude-api"
        self.assertEqual("dispatched", di.decide(hint(), d)["status"])
        self.assertEqual(1, len(d.queue.puts))

    def test_a_live_heartbeat_routes_to_the_laptop(self):
        d = deps(claims=di.MemoryClaims({"claude-code-cli": 990.0}))
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"])
        self.assertEqual("claude-code-cli", out["tuple"]["recipient_id"])

    def test_an_addressed_doc_goes_where_it_says_and_an_unknown_address_is_refused(self):
        d = deps(docs=Docs(text="to: gemini\nrequest: G-1\nReview the architecture.\n"))
        out = di.decide(hint(), d)
        self.assertEqual("gemini", out["tuple"]["recipient_id"])
        d = deps(docs=Docs(text="To = Aya\nrelay this.\n"))
        self.assertEqual("chatgpt-codex-desktop", di.decide(hint(), d)["tuple"]["recipient_id"])
        # Cursor on 05a8079: "to: somebody" fell through to pick_worker and went to claude-api
        d = deps(docs=Docs(text="to: somebody\nrequest: S-1\nDo a thing.\n"))
        out = di.decide(hint(), d)
        self.assertEqual("bad_recipient", out["status"], out)
        self.assertEqual([], d.queue.puts)
        self.assertEqual({}, d.claims.states)


class RetryTest(unittest.TestCase):
    """A lost response is settled by reading back, never by doing it again; every hop is finished later."""

    def test_a_failed_queue_put_is_retryable_and_the_retry_delivers_once(self):
        d = deps(queue=Queue(fail=RuntimeError("redis down")))
        out = di.decide(hint(), d)
        self.assertEqual("queue_error", out["status"])
        self.assertEqual([], d.receipts.lines)      # nothing landed, so nothing is claimed in the doc
        self.assertEqual("delivering:claude-api", list(d.claims.states.values())[0])
        d.queue = Queue()
        self.assertEqual("dispatched", di.decide(hint(), d)["status"])
        self.assertEqual(1, len(d.queue.puts))

    def test_a_put_that_raised_after_landing_is_a_landed_put(self):
        """Cursor on 05a8079: a TimeoutError after xadd landed released the claim and the retry appended twice."""
        d = deps(queue=Queue(fail_after=TimeoutError("response lost")))
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(1, len(d.queue.puts))
        self.assertEqual("duplicate", di.decide(hint(), d)["status"])
        self.assertEqual(1, len(d.queue.puts))

    def test_a_claim_that_landed_before_a_crash_is_resumed_not_restarted(self):
        d = deps()
        d.claims.states[key_for()] = di.INTAKE                 # claimed, then the process died
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(1, len(d.queue.puts))
        self.assertEqual(["REQUESTED", "DISPATCHED"], d.receipts.states())

    def test_an_unconfigured_queue_names_the_gap_and_the_claim_resumes_later(self):
        d = deps(queue=di.InboxQueue(None))
        out = di.decide(hint(), d)
        self.assertEqual("unconfigured", out["status"])
        self.assertIn("fleet:inbox", out["error"])
        self.assertEqual([], d.receipts.lines)
        d.queue = Queue()
        self.assertEqual("dispatched", di.decide(hint(), d)["status"])

    def test_a_failed_ring_is_rung_by_the_next_pass_without_a_second_put(self):
        """Cursor on d150d84: on_inbox was treated as finished and a failed ring was never sent later."""
        d = deps(ring=Ring(fail_first=1))
        out = di.decide(hint(), d)
        self.assertEqual("queued_not_rung", out["status"])
        self.assertEqual(1, len(d.queue.puts))
        self.assertEqual(["REQUESTED"], d.receipts.states())     # the doc says exactly that much
        later = di.decide(hint(), d)
        self.assertEqual("dispatched", later["status"], later)
        self.assertEqual(2, len(d.ring.calls))                   # rung again, once
        self.assertEqual(1, len(d.queue.puts))                   # and not put again
        self.assertEqual(["REQUESTED", "DISPATCHED"], d.receipts.states())
        self.assertEqual("duplicate", di.decide(hint(), d)["status"])
        self.assertEqual(2, len(d.ring.calls))

    def test_the_default_ring_is_not_configured_and_a_real_one_installed_later_rings(self):
        d = deps(ring=None)
        out = di.decide(hint(), d)
        self.assertEqual("queued_not_rung", out["status"], out)
        self.assertIn("no ring configured", out["error"])
        self.assertEqual(["REQUESTED"], d.receipts.states())
        d.ring = Ring()
        self.assertEqual("dispatched", di.decide(hint(), d)["status"])
        self.assertEqual(1, len(d.ring.calls))
        self.assertEqual(1, len(d.queue.puts))

    def test_a_dispatched_receipt_not_read_back_is_owed_and_written_by_the_next_pass(self):
        """Cursor on d150d84: both receipts not read back, status still dispatched, the retry gave up."""
        d = deps(receipts=Receipts(drop=True))
        out = di.decide(hint(), d)
        self.assertEqual("dispatched_receipt_owed", out["status"], out)
        self.assertEqual(1, len(d.ring.calls))
        good = Receipts()
        d.receipts = good
        later = di.decide(hint(), d)
        self.assertEqual("dispatched", later["status"], later)
        self.assertEqual(["DISPATCHED"], good.states())          # only the owed line, once
        self.assertEqual(1, len(d.ring.calls))                   # and no second wake
        self.assertEqual(1, len(d.queue.puts))

    def test_a_receipt_id_does_not_carry_the_clock_so_a_retry_finds_its_own_line(self):
        t = di.correlate(FILE, REQUEST, "r1", "claude-api")
        self.assertEqual(di.receipt_id_for(t, "REQUESTED"), di.receipt_id_for(dict(t, drive_revision="r9"), "REQUESTED"))
        self.assertNotEqual(di.receipt_id_for(t, "REQUESTED"), di.receipt_id_for(t, "DISPATCHED"))
        d = deps()
        self.assertEqual("written", di.write_receipt(d, t, REQUEST, "REQUESTED", "t1"))
        self.assertEqual("written", di.write_receipt(d, t, REQUEST, "REQUESTED", "t2"))
        self.assertEqual(1, len(d.receipts.lines))

    def test_a_claim_lost_between_set_and_read_is_an_error_not_a_fresh_start(self):
        """claim() says somebody holds it and state() says nobody: a race or a dying store.
        Proceeding as if it were ours is the dual-execution case; the only safe answer is no."""
        class Flaky(di.MemoryClaims):
            def claim(self, key):
                return False

            def state(self, key):
                return None
        d = deps(claims=Flaky())
        out = di.decide(hint(), d)
        self.assertEqual("claim_error", out["status"], out)
        self.assertEqual([], d.queue.puts)
        self.assertEqual([], d.receipts.lines)

    def test_a_claim_store_that_cannot_answer_is_not_a_free_key(self):
        class Broken(di.MemoryClaims):
            def claim(self, key):
                raise ConnectionError("redis unreachable")
        d = deps(claims=Broken())
        self.assertEqual("claim_error", di.decide(hint(), d)["status"])
        self.assertEqual([], d.queue.puts)

    def test_an_inbox_that_cannot_be_read_back_is_unknown_not_a_second_put(self):
        class Blind(Queue):
            def has(self, recipient, task_id):
                raise ConnectionError("xrange failed")
        d = deps(queue=Blind())
        out = di.decide(hint(), d)
        self.assertEqual("queue_unknown", out["status"], out)
        self.assertEqual([], d.queue.puts)


class ReceiptTest(unittest.TestCase):
    """Guard 4 and 5: the receipt names the state reached and nothing past it, and is settled by read-back."""

    def test_the_receipt_trail_is_requested_then_dispatched_and_never_accepted(self):
        d = deps()
        out = di.decide(hint(), d)
        self.assertEqual(["REQUESTED", "DISPATCHED"], d.receipts.states())
        for _, _, block in d.receipts.lines:
            self.assertIn("task " + out["task"], block)
            self.assertIn("by drive-intake", block)
            self.assertNotIn("ACCEPTED", block)
            self.assertNotIn("PICKED UP", block)
        self.assertEqual("written", out["receipt"])
        self.assertEqual("written", out["receipt_dispatched"])

    def test_the_second_receipt_is_guarded_with_the_revision_the_first_one_produced(self):
        """Cursor on 05a8079: both appends sent the revision captured at read time; the second was stale."""
        d = deps(receipts=Receipts(guard=True, revision="r1"))
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual("written", out["receipt_dispatched"])
        self.assertEqual(["REQUESTED", "DISPATCHED"], d.receipts.states())
        self.assertEqual("r3", out["tuple"]["drive_revision"])

    def test_a_lost_append_response_refreshes_the_revision_from_the_doc(self):
        """Cursor on d150d84: the landed-but-lost append threw its new revision away, so the next
        guarded append offered r1 against a doc at r2 and the DISPATCHED line never landed."""
        receipts = Receipts(guard=True, lose=True, revision="r1")
        d = deps(receipts=receipts, docs=Docs(receipts=receipts))
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(["REQUESTED", "DISPATCHED"], receipts.states())
        self.assertEqual("r3", out["tuple"]["drive_revision"])

    def test_a_stale_guard_on_a_clean_miss_is_retried_once_with_the_fresh_revision(self):
        receipts = Receipts(guard=True, revision="r5")           # the doc moved under us
        d = deps(receipts=receipts, docs=Docs(receipts=receipts))
        t = di.correlate(FILE, REQUEST, "r1", "claude-api")
        self.assertEqual("written", di.write_receipt(d, t, REQUEST, "REQUESTED", d.at))
        self.assertEqual(2, receipts.appends)                   # one stale miss, one fresh hit
        self.assertEqual(receipts.revision, t["drive_revision"])  # and the tuple carries the doc's revision

    def test_a_lost_write_that_landed_is_written_and_one_that_vanished_is_unknown(self):
        t = di.correlate(FILE, REQUEST, "r1", "claude-code-cli")
        d = deps(receipts=Receipts(lose=True))
        self.assertEqual("written", di.write_receipt(d, t, REQUEST, "REQUESTED", d.at))
        d = deps(receipts=Receipts(lose=True, drop=True))
        self.assertEqual("unknown:ConnectionError", di.write_receipt(d, t, REQUEST, "REQUESTED", d.at))
        d = deps(receipts=Receipts(drop=True))
        self.assertEqual("not_read_back", di.write_receipt(d, t, REQUEST, "REQUESTED", d.at))

    def test_no_receipt_writer_is_said_not_assumed(self):
        d = deps(receipts=None)
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"])
        self.assertEqual("no_receipt_writer", out["receipt"])


class ScopeTest(unittest.TestCase):
    def test_another_folder_or_no_folder_is_ignored_before_and_after_the_read(self):
        d = deps()
        self.assertEqual("out_of_scope", di.decide(hint(folder="elsewhere"), d)["status"])
        self.assertEqual(0, d.docs.reads)
        d = deps(docs=Docs(folder="elsewhere"))
        self.assertEqual("out_of_scope", di.decide(hint(folder=""), d)["status"])
        # Cursor on 05a8079: a doc that reported no folder skipped the check
        d = deps(docs=Docs(folder=""))
        self.assertEqual("out_of_scope", di.decide(hint(folder=""), d)["status"])
        self.assertEqual([], d.queue.puts)

    def test_a_doc_without_a_write_revision_is_refused(self):
        d = deps(docs=Docs(revision=""))
        self.assertEqual("no_revision", di.decide(hint(), d)["status"])
        self.assertEqual({}, d.claims.states)

    def test_a_bad_file_id_and_an_empty_request_do_nothing(self):
        d = deps()
        self.assertEqual("bad_file_id", di.decide({"file_id": "../etc"}, d)["status"])
        d = deps(docs=Docs(text=RECEIPTS + "DONE by x\n"))
        self.assertEqual("no_request", di.decide(hint(), d)["status"])

    def test_a_doc_too_large_is_refused_not_trimmed_and_a_long_gist_says_so(self):
        d = deps(docs=Docs(text="x" * (di.MAX_DOC + 1)))
        self.assertEqual("too_large", di.decide(hint(), d)["status"])
        g = di.gist("word " * 400)
        self.assertFalse(g["full"])
        self.assertEqual(1999, g["chars"])
        self.assertEqual(di.MAX_GIST, len(g["text"]))
        self.assertTrue(di.gist(REQUEST)["full"])

    def test_a_read_that_fails_is_not_an_absent_request(self):
        class Down(Docs):
            def read(self, file_id):
                raise TimeoutError()
        d = deps(docs=Down())
        out = di.decide(hint(), d)
        self.assertEqual("read_error", out["status"])
        self.assertEqual({}, d.claims.states)


class StreamClient:
    """Enough of a Redis stream to prove the read-back reads the NEWEST entries."""

    def __init__(self):
        self.streams, self.n = {}, 0

    def xadd(self, key, fields):
        self.n += 1
        self.streams.setdefault(key, []).append(("%d-0" % self.n, dict(fields)))

    def xrevrange(self, key, count=None):
        rows = list(reversed(self.streams.get(key, [])))
        return rows[:count] if count else rows


class InboxQueueTest(unittest.TestCase):
    def test_the_queue_entry_carries_the_whole_tuple_and_is_read_back_by_task_id(self):
        q = di.InboxQueue(client=StreamClient())
        d = deps(queue=q)
        out = di.decide(hint(), d)
        fields = q.client.streams["fleet:inbox:claude-api"][0][1]
        self.assertEqual(["fleet:inbox:claude-api"], list(q.client.streams))
        for part in ("source_doc_id", "source_revision", "request_id", "task_id", "recipient_id"):
            self.assertEqual(out["tuple"][part], fields[part], part)
        self.assertEqual("drive-intake", fields["by"])
        self.assertEqual("1", fields["full"])
        self.assertEqual(str(len(" ".join(REQUEST.split()))), fields["chars"])
        self.assertTrue(q.has("claude-api", out["task"]))
        self.assertFalse(q.has("claude-code-cli", out["task"]))
        self.assertEqual("duplicate", di.decide(hint(), d)["status"])
        self.assertEqual(1, len(q.client.streams["fleet:inbox:claude-api"]))

    def test_a_busy_inbox_still_finds_the_entry_just_appended(self):
        """Cursor on d150d84: xrange read the OLDEST 500, so a lost response on a busy stream
        re-appended the same task on every later poll (501, 502, ...)."""
        client = StreamClient()
        for i in range(di.INBOX_LOOKBACK + 100):
            client.xadd("fleet:inbox:claude-api", {"task_id": "task-%016x" % i, "by": "older"})
        q = di.InboxQueue(client=client)

        class LossyPut(di.InboxQueue):
            def put(self, tuple_, gist_, at):
                super().put(tuple_, gist_, at)
                raise TimeoutError("response lost")
        d = deps(queue=LossyPut(client=client))
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(di.INBOX_LOOKBACK + 101, len(client.streams["fleet:inbox:claude-api"]))
        for _ in range(3):
            self.assertEqual("duplicate", di.decide(hint(), d)["status"])
        self.assertEqual(di.INBOX_LOOKBACK + 101, len(client.streams["fleet:inbox:claude-api"]))
        self.assertTrue(q.has("claude-api", out["task"]))

    def test_the_lua_compare_and_set_is_what_advance_runs(self):
        calls = []

        class Client:
            def eval(self, script, numkeys, key, current, new, ttl):
                calls.append((script, numkeys, key, current, new, ttl))
                return 1
        self.assertTrue(di.RedisClaims(Client()).advance("k", "intake", "delivering:grok"))
        self.assertEqual(1, calls[0][1])
        self.assertEqual("fleet:intake:claim:k", calls[0][2])
        self.assertIn("redis.call('get'", calls[0][0])
        self.assertEqual(("intake", "delivering:grok", di.CLAIM_TTL), calls[0][3:])


class ChannelTest(unittest.TestCase):
    """Guard 6: the day-eight outage is announced on day six, never read as ok by default, and read against NOW."""

    def test_no_expiry_on_record_is_unknown_not_ok(self):
        self.assertEqual({"channel": "unknown", "renew": True}, di.channel_health(None, 1000.0))
        self.assertEqual({"channel": "unknown", "renew": True}, di.channel_health("soon", 1000.0))

    def test_near_expiry_is_degraded_and_past_is_expired(self):
        self.assertEqual("ok", di.channel_health(1000.0 + 172801, 1000.0)["channel"])
        self.assertEqual("degraded", di.channel_health(1000.0 + 172800, 1000.0)["channel"])
        self.assertEqual("expired", di.channel_health(1000.0, 1000.0)["channel"])
        self.assertTrue(di.channel_health(1000.0 + 10, 1000.0)["renew"])

    def test_healthz_ok_follows_the_channel_and_never_shows_the_doc(self):
        d = deps(channel_expires_at=1000.0 + 3600, api_worker="")
        status, out = di.handle("GET", "/healthz", {}, b"", d)
        self.assertEqual(200, status)
        self.assertEqual("degraded", out["channel"])
        self.assertFalse(out["ok"])                                  # Cursor: ok:true next to unknown
        self.assertEqual({"claude-code-cli": "offline", "api": "none"}, out["worker"])
        self.assertNotIn("telephone", json.dumps(out))
        self.assertFalse(di.handle("GET", "/healthz", {}, b"", deps())[1]["ok"])
        self.assertTrue(di.handle("GET", "/healthz", {}, b"", deps(channel_expires_at=1000.0 + 10 ** 6))[1]["ok"])

    def test_a_server_reads_the_clock_now_not_the_clock_at_boot(self):
        """Cursor on d150d84: main() never moved deps.now, so a healthy expiry at boot stayed ok forever."""
        d = deps(now=None, channel_expires_at=time.time() + 10 ** 6)
        self.assertTrue(di.handle("GET", "/healthz", {}, b"", d)[1]["ok"])
        d.channel_expires_at = time.time() - 1
        self.assertEqual("expired", di.handle("GET", "/healthz", {}, b"", d)[1]["channel"])
        self.assertIsNone(di.Deps().now)


class AuthTest(unittest.TestCase):
    def test_an_empty_secret_refuses_rather_than_skips(self):
        self.assertFalse(di.channel_token_ok("", ""))
        self.assertFalse(di.bearer_ok("", "Bearer "))
        d = deps(channel_token="")
        self.assertEqual(401, di.handle("POST", "/hooks/drive", {"x-goog-channel-token": "",
                                                                  "x-goog-resource-state": "update"}, b"", d)[0])
        self.assertEqual(0, d.docs.reads)

    def test_a_wrong_token_reads_nothing(self):
        d = deps()
        self.assertEqual(401, di.handle("POST", "/hooks/drive", {"x-goog-channel-token": "nope",
                                                                  "x-goog-resource-state": "update"}, b"", d)[0])
        self.assertEqual(401, di.handle("POST", "/poll", {"authorization": "Bearer nope"}, b"", d)[0])
        self.assertEqual(0, d.docs.reads)

    def test_the_sync_ping_and_an_unknown_path_do_nothing(self):
        d = deps()
        self.assertEqual([], di.hints_from_push({"x-goog-resource-state": "sync"}))
        self.assertEqual((200, {"enabled": True, "intake": []}),
                         di.handle("POST", "/hooks/drive", {"x-goog-channel-token": "tok",
                                                           "x-goog-resource-state": "sync"}, b"", d))
        self.assertEqual(404, di.handle("GET", "/", {}, b"", d)[0])
        self.assertEqual(0, d.docs.reads)

    def test_a_push_without_a_file_id_asks_for_a_poll_and_is_not_a_task(self):
        hints = di.hints_from_push({"x-goog-resource-state": "change"})
        self.assertEqual([{"file_id": "", "poll": True}], hints)
        self.assertEqual("bad_file_id", di.decide(hints[0], deps())["status"])


class OfflineTest(unittest.TestCase):
    def test_the_module_opens_no_socket_on_import_and_names_no_credential(self):
        source = (REPO / "cloud" / "drive-intake" / "main.py").read_text(encoding="utf-8")
        for banned in ("import socket", "urllib.request.urlopen(", "import requests", "googleapiclient",
                       "SECRET_MANAGER", "service_account"):
            self.assertNotIn(banned, source, banned)
        self.assertNotIn("redis", sys.modules)


if __name__ == "__main__":
    unittest.main()

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
    """An inbox that can be read back by task id, like fleet:inbox, and whose put is
    append-unless-present the way PUT_ONCE_LUA is. `hold` parks a put until flush()."""

    def __init__(self, fail=None, fail_after=None, hold=False):
        self.puts, self.fail, self.fail_after, self.hold, self.held = [], fail, fail_after, hold, []

    def has(self, recipient, task_id):
        return any(t["recipient_id"] == recipient and t["task_id"] == task_id for t, _, _ in self.puts)

    def holder(self, task_id):
        for t, _, _ in self.puts:
            if t["task_id"] == task_id:
                return t["recipient_id"]
        return None

    def _land(self, tuple_, gist_, at):
        if self.has(tuple_["recipient_id"], tuple_["task_id"]):
            return False
        self.puts.append((dict(tuple_), dict(gist_), at))
        return True

    def put(self, tuple_, gist_, at):
        if self.fail:
            raise self.fail
        if self.hold:
            self.held.append((dict(tuple_), dict(gist_), at))
            raise TimeoutError("put still in flight")
        added = self._land(tuple_, gist_, at)
        if self.fail_after:
            raise self.fail_after
        return added

    def flush(self):
        """The in-flight puts finally reach Redis, in order."""
        results = [self._land(*h) for h in self.held]
        self.held = []
        return results

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

    def test_a_fresh_delivering_lease_is_not_resumed_and_a_dead_one_is(self):
        """Cursor on 57a23ab: two resumes of one delivering state both saw an empty inbox and both put."""
        d = deps()
        d.claims.states[key_for()] = "delivering:claude-api:%d" % (1000 - 30)   # another pass, 30 s in
        out = di.decide(hint(), d)
        self.assertEqual("in_progress", out["status"], out)
        self.assertEqual([], d.queue.puts)
        self.assertEqual([], d.receipts.lines)
        d.now = 1000.0 + di.DELIVER_LEASE + 1                                     # that pass is dead
        later = di.decide(hint(), d)
        self.assertEqual("dispatched", later["status"], later)
        self.assertEqual(["claude-api"], d.queue.recipients())

    def test_a_lease_ahead_of_this_clock_is_a_live_pass_not_a_dead_one(self):
        """Cursor on c408819: a one-second skew let a second pass treat a live lease as dead."""
        d = deps()
        d.claims.states[key_for()] = "delivering:claude-api:%d" % (1000 + 1)
        self.assertEqual("in_progress", di.decide(hint(), d)["status"])
        self.assertEqual([], d.queue.puts)

    def test_two_resumers_of_a_dead_lease_put_once(self):
        """Cursor on c408819: both resumers of delivering:<w>:0 (or an old lease) appended."""
        class Overtaken(di.MemoryClaims):
            """The other resumer takes the dead lease first."""
            def advance(self, key, current, new):
                if current.startswith("delivering:") and new.startswith("delivering:"):
                    self.states[key] = "delivering:claude-api:1000"     # taken by the other pass, now
                    return False
                return super().advance(key, current, new)
        for dead in ("delivering:claude-api:0", "delivering:claude-api", "delivering:claude-api:%d" % (1000 - 61)):
            d = deps(claims=Overtaken())
            d.claims.states[key_for()] = dead
            out = di.decide(hint(), d)
            self.assertEqual("in_progress", out["status"], (dead, out))
            self.assertEqual([], d.queue.puts, dead)                 # the loser never touched the inbox
            self.assertEqual([], d.receipts.lines, dead)
            self.assertEqual("in_progress", di.decide(hint(), d)["status"])   # and the fresh lease holds

    def test_the_bind_carries_the_clock_and_the_ring_carries_the_task_id(self):
        """The ring is sent before the claim moves to rung, so a crash in between rings again; that is
        safe only because the id the ring carries is the task id and #359 claims (agent, id) SET NX."""
        d = deps()
        out = di.decide(hint(), d)
        self.assertEqual(out["task"], d.ring.calls[0]["task_id"])
        self.assertEqual("claude-api", d.ring.calls[0]["recipient_id"])
        seen = []
        orig = d.claims.advance
        d2 = deps()
        d2.claims.advance = lambda key, current, new: (seen.append(new), orig.__func__(d2.claims, key, current, new))[1]
        di.decide(hint(), d2)
        self.assertTrue(seen[0].startswith("delivering:claude-api:1000"), seen)

    def test_a_put_that_outlives_its_lease_lands_once_because_the_queue_itself_dedupes(self):
        """Cursor on 07a6b1c: a put still in flight at +61 s was overtaken; a chain at 0, +61,
        +122 appended three times. The put is append-unless-present, so the late one is a no-op."""
        slow = Queue(hold=True)
        d = deps(queue=slow)
        first = di.decide(hint(), d)                              # takes the lease, put parked in flight
        self.assertEqual("queue_error", first["status"], first)  # (its own view: nothing landed yet)
        self.assertEqual(1, len(slow.held))
        d.now = 1000.0 + di.DELIVER_LEASE + 1                    # a second pass finds the lease dead
        slow.hold = False
        second = di.decide(hint(), d)
        self.assertEqual("dispatched", second["status"], second)
        self.assertEqual(1, len(slow.puts))
        self.assertEqual([False], slow.flush())                  # the slow put reaches Redis: not added
        self.assertEqual(1, len(slow.puts))
        self.assertEqual(1, len(d.ring.calls))
        for extra in (122, 183):
            d.now = 1000.0 + extra
            self.assertEqual("duplicate", di.decide(hint(), d)["status"])
        self.assertEqual(1, len(slow.puts))

    def test_the_wait_a_request_came_through_rides_the_claim_so_its_line_survives_a_crash(self):
        """Cursor on 07a6b1c: a QUEUED line not read back was abandoned once the route changed."""
        d = deps(docs=Docs(text=REQUEST + "Use the sf CLI.\n"))
        d.claims.states[key_for(text=REQUEST + "Use the sf CLI.\n")] = "delivering:claude-code-cli:0:q"
        laptop_up(d)
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(["QUEUED", "REQUESTED", "DISPATCHED"], d.receipts.states())
        self.assertIn("awaiting the laptop", d.receipts.lines[0][2])
        self.assertTrue(all(":q" in s for s in d.claims.states.values()) or
                        list(d.claims.states.values()) == [di.DONE])

    def test_a_wait_line_that_still_will_not_read_back_keeps_dispatched_owed(self):
        """The wait line is re-attempted before the put; if THAT also fails to land, the trail
        is still short and dispatched must wait for it, not for the last line alone."""
        class DropsQueued(Receipts):
            dropping = True

            def append(self, file_id, expected_revision, receipt_id, block):
                if self.dropping and "QUEUED - task" in block:
                    self.appends += 1
                    return self.revision
                return super().append(file_id, expected_revision, receipt_id, block)
        text = REQUEST + "Use the sf CLI.\n"
        d = deps(docs=Docs(text=text), receipts=DropsQueued())
        self.assertEqual("queued", di.decide(hint(), d)["status"])
        laptop_up(d)
        out = di.decide(hint(), d)
        self.assertEqual("dispatched_receipt_owed", out["status"], out)
        self.assertEqual(["REQUESTED", "DISPATCHED"], d.receipts.states())
        self.assertEqual(1, len(d.ring.calls))
        d.receipts.dropping = False
        later = di.decide(hint(), d)
        self.assertEqual("dispatched", later["status"], later)
        self.assertEqual(["REQUESTED", "DISPATCHED", "QUEUED"], d.receipts.states())
        self.assertEqual(1, len(d.ring.calls))
        self.assertEqual(1, len(d.queue.puts))

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
        self.assertEqual("delivering:claude-api:0:", list(d.claims.states.values())[0])   # bound, lease given up
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
        self.assertEqual(["REQUESTED", "DISPATCHED"], good.states())   # both owed lines, each once
        self.assertEqual(1, len(d.ring.calls))                   # and no second wake
        self.assertEqual(1, len(d.queue.puts))

    def test_a_requested_line_not_read_back_is_rewritten_before_dispatched(self):
        """Cursor on 57a23ab: only the REQUESTED append dropped; the doc ended with DISPATCHED alone."""
        class DropFirst(Receipts):
            def append(self, file_id, expected_revision, receipt_id, block):
                if self.appends == 0:
                    self.appends += 1
                    return self.revision                       # said ok, wrote nothing
                return super().append(file_id, expected_revision, receipt_id, block)
        d = deps(receipts=DropFirst())
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(["REQUESTED", "DISPATCHED"], d.receipts.states())
        # and the crash shape: on_inbox with the task queued and no REQUESTED line yet
        d = deps()
        t = di.correlate(FILE, REQUEST, "r1", "claude-api")
        d.claims.states[key_for()] = "on_inbox:claude-api"
        d.queue.puts.append((dict(t), di.gist(REQUEST), d.at))
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(["REQUESTED", "DISPATCHED"], d.receipts.states())
        self.assertEqual(1, len(d.ring.calls))

    def test_dispatched_needs_both_lines_read_back_not_just_the_last(self):
        """Cursor on c408819: every REQUESTED append dropped, DISPATCHED kept -> sealed as dispatched."""
        class DropRequested(Receipts):
            def append(self, file_id, expected_revision, receipt_id, block):
                if "REQUESTED - task" in block:
                    self.appends += 1
                    return self.revision                       # said ok, wrote nothing
                return super().append(file_id, expected_revision, receipt_id, block)
        d = deps(receipts=DropRequested())
        out = di.decide(hint(), d)
        self.assertEqual("dispatched_receipt_owed", out["status"], out)
        self.assertEqual(["DISPATCHED"], d.receipts.states())
        self.assertTrue(list(d.claims.states.values())[0].startswith("rung:"))
        good = Receipts()
        good.lines = list(d.receipts.lines)
        d.receipts = good
        later = di.decide(hint(), d)
        self.assertEqual("dispatched", later["status"], later)
        self.assertEqual(["DISPATCHED", "REQUESTED"], good.states())   # the owed line, once
        self.assertEqual(1, len(d.ring.calls))
        self.assertEqual("duplicate", di.decide(hint(), d)["status"])

    def test_a_queued_or_blocked_line_not_read_back_is_owed_until_it_is(self):
        """Cursor on c408819: the claim moved to queued before the write, so the next pass was `waiting`."""
        class SaysOkWritesNothingOnce(Receipts):
            def append(self, file_id, expected_revision, receipt_id, block):
                if self.appends == 0:
                    self.appends += 1
                    return self.revision
                return super().append(file_id, expected_revision, receipt_id, block)
        for text, api, expect in ((REQUEST + "Use the sf CLI.\n", "claude-api", "QUEUED"),
                                  (REQUEST, "", "BLOCKED")):
            d = deps(docs=Docs(text=text), api_worker=api, receipts=SaysOkWritesNothingOnce())
            first = di.decide(hint(), d)
            self.assertEqual(expect.lower(), first["status"], first)
            self.assertEqual("not_read_back", first["receipt"])
            self.assertEqual([], d.receipts.lines)
            again = di.decide(hint(), d)
            self.assertEqual("waiting", again["status"], again)   # rewritten, now it is only waiting
            self.assertEqual([expect], d.receipts.states())
            self.assertEqual("waiting", di.decide(hint(), d)["status"])
            self.assertEqual([expect], d.receipts.states())       # and not a third time
            # a writer that never lands says so every pass instead of "waiting"
            d = deps(docs=Docs(text=text), api_worker=api, receipts=Receipts(drop=True))
            di.decide(hint(), d)
            self.assertEqual(expect.lower() + "_receipt_owed", di.decide(hint(), d)["status"])
            # Cursor on 07a6b1c: the route then changes and the unwritten wait line was abandoned
            d.receipts = Receipts()
            if expect == "QUEUED":
                laptop_up(d)
            else:
                d.api_worker = "claude-api"
            out = di.decide(hint(), d)
            self.assertEqual("dispatched", out["status"], out)
            self.assertEqual([expect, "REQUESTED", "DISPATCHED"], d.receipts.states())
            self.assertEqual("duplicate", di.decide(hint(), d)["status"])
            self.assertEqual([expect, "REQUESTED", "DISPATCHED"], d.receipts.states())

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
                raise ConnectionError("read-back failed")

            def holder(self, task_id):
                raise ConnectionError("read-back failed")
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
    """Enough of a Redis stream to prove the read-back reads the NEWEST entries, and that
    the put is one atomic append-unless-present call (eval of PUT_ONCE_LUA)."""

    def __init__(self):
        self.streams, self.n, self.evals = {}, 0, []

    def xadd(self, key, fields):
        self.n += 1
        self.streams.setdefault(key, []).append(("%d-0" % self.n, dict(fields)))

    def xrevrange(self, key, count=None):
        rows = list(reversed(self.streams.get(key, [])))
        return rows[:count] if count else rows

    def get(self, key):
        return self.__dict__.setdefault("kv", {}).get(key)

    def set(self, key, value, nx=False, ex=None):
        kv = self.__dict__.setdefault("kv", {})
        if nx and key in kv:
            return None
        kv[key] = value
        return True

    def eval(self, script, numkeys, stream, marker, worker, ttl, *rest):
        """What PUT_ONCE_LUA does, in Python, so the test can check the script's shape AND its effect."""
        flat, depth = rest[:-1], int(rest[-1])
        self.evals.append((script, numkeys, stream, marker, worker, ttl, flat, depth))
        assert "'SET', KEYS[2]" in script and "'NX'" in script and "XREVRANGE" in script and "XADD" in script
        kv = self.__dict__.setdefault("kv", {})
        if marker in kv:
            return 0
        kv[marker] = worker
        fields = dict(zip(flat[0::2], flat[1::2]))
        for _id, row in self.xrevrange(stream, count=depth):
            if row.get("task_id") == fields["task_id"]:
                return 0
        self.xadd(stream, fields)
        return 1


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

    def test_an_entry_buried_under_newer_ones_is_still_one_entry_on_resume_or_reroute(self):
        """Cursor on 13af1f7: after 500 newer entries the resume appended again, and with the claim
        gone and the laptop fresh the same task went onto a second stream."""
        client = StreamClient()
        d = deps(queue=di.InboxQueue(client=client))
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        for i in range(di.INBOX_LOOKBACK + 100):               # bury it
            client.xadd("fleet:inbox:claude-api", {"task_id": "task-%016x" % i, "by": "newer"})
        # the crash shape: claim back at delivering, entry buried
        d.claims.states[key_for()] = "delivering:claude-api:0:"
        again = di.decide(hint(), d)
        self.assertEqual("dispatched", again["status"], again)
        copies = [f for _, f in client.streams["fleet:inbox:claude-api"] if f.get("task_id") == out["task"]]
        self.assertEqual(1, len(copies))
        # the aged-out shape: claim gone, laptop fresh, routing would say claude-code-cli
        d.claims.states.clear()
        laptop_up(d)
        rerouted = di.decide(hint(), d)
        self.assertNotIn("fleet:inbox:claude-code-cli", client.streams)
        self.assertEqual("claude-api", rerouted["tuple"]["recipient_id"] if rerouted.get("tuple") else "claude-api")
        self.assertEqual(1, len([f for _, f in client.streams["fleet:inbox:claude-api"] if f.get("task_id") == out["task"]]))
        self.assertEqual("claude-api", di.InboxQueue(client=client).holder(out["task"]))

    def test_the_put_is_one_atomic_append_unless_present_call(self):
        client = StreamClient()
        q = di.InboxQueue(client=client)
        t = di.correlate(FILE, REQUEST, "r1", "claude-api")
        g = di.gist(REQUEST)
        self.assertTrue(q.put(t, g, "t1"))
        self.assertFalse(q.put(t, g, "t2"))                      # already there: not appended
        self.assertEqual(1, len(client.streams["fleet:inbox:claude-api"]))
        self.assertEqual(2, len(client.evals))
        script, numkeys, stream, marker, worker, ttl, flat, depth = client.evals[0]
        self.assertEqual((2, "fleet:inbox:claude-api", "fleet:intake:put:" + t["task_id"], "claude-api", di.PUT_TTL),
                         (numkeys, stream, marker, worker, ttl))
        self.assertEqual(di.INBOX_FULL_SCAN, depth)
        # marker gone, entry present: the script's own scan refuses the second copy
        client.kv.clear()
        self.assertFalse(q.put(t, g, "t3"))
        self.assertEqual(1, len(client.streams["fleet:inbox:claude-api"]))
        self.assertEqual("claude-api", q.holder(t["task_id"]))      # and holder() found it by scan
        client.kv.clear()
        self.assertEqual("claude-api", q.holder(t["task_id"]))
        self.assertEqual("claude-api", client.kv["fleet:intake:put:" + t["task_id"]])   # marker put back
        self.assertGreater(di.PUT_TTL, di.CLAIM_TTL)             # the marker outlives the claim
        self.assertIn("task_id", flat)
        self.assertEqual("claude-api", q.holder(t["task_id"]))
        self.assertIsNone(q.holder("task-0000000000000000"))
        self.assertEqual(client.streams["fleet:inbox:claude-api"][0][1]["task_id"], t["task_id"])
        # and put never calls xadd directly - the dedupe and the append are one Redis call
        self.assertNotIn("self.client.xadd", (REPO / "cloud" / "drive-intake" / "main.py").read_text(encoding="utf-8"))

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


class WakeRingTest(unittest.TestCase):
    """The ring into #359: the message id IS the task id, and the body is signed the GitHub way."""

    def test_unconfigured_rings_nobody_and_says_which_setting_is_missing(self):
        t = di.correlate(FILE, REQUEST, "r1", "claude-api")
        for ring in (di.WakeRing(), di.WakeRing(url="https://x"), di.WakeRing(url="https://x", secret="s"),
                     di.WakeRing(secret="s", post=lambda *a: None)):
            with self.assertRaises(di.Unconfigured) as caught:
                ring(t)
            self.assertIn("INBOX_WAKE_URL", str(caught.exception))

    def test_a_200_that_woke_nobody_is_not_a_ring(self):
        """Cursor on 07a6b1c: #359 answered 200 unknown_agent for claude-api and this treated post() returning as rung."""
        t = di.correlate(FILE, REQUEST, "r1", "claude-api")
        for readout in (b'{"enabled": true, "woke": [{"status": "unknown_agent", "woke": false}]}',
                        b'{"enabled": false, "woke": []}',
                        b'{"enabled": true, "woke": [{"status": "unconfigured"}]}',
                        None, b"not json", b'{"woke": [{"status": "woke"}, {"status": "disabled"}]}'):
            ring = di.WakeRing(url="https://wake/wake", secret="s", post=lambda u, b, h, r=readout: r)
            with self.assertRaises(RuntimeError, msg=repr(readout)):
                ring(t)
        for readout in (b'{"enabled": true, "woke": [{"status": "woke"}]}',
                        b'{"enabled": true, "woke": [{"status": "duplicate"}]}',
                        {"woke": [{"status": "woke"}]}):
            di.WakeRing(url="https://wake/wake", secret="s", post=lambda u, b, h, r=readout: r)(t)
        d = deps(ring=di.WakeRing(url="https://wake/wake", secret="s",
                                  post=lambda u, b, h: b'{"woke": [{"status": "unknown_agent"}]}'))
        out = di.decide(hint(), d)
        self.assertEqual("queued_not_rung", out["status"], out)
        self.assertEqual("RuntimeError", out["error"])
        self.assertEqual(["REQUESTED"], d.receipts.states())       # and DISPATCHED was not written

    def test_a_refused_ring_does_not_keep_the_doc_empty(self):
        """Cursor on 13af1f7: REQUESTED dropped + #359 answering unknown_agent every time -> the doc
        stayed empty, because the owed lines were only retried after a ring succeeded."""
        class DropFirst(Receipts):
            def append(self, file_id, expected_revision, receipt_id, block):
                if self.appends == 0:
                    self.appends += 1
                    return self.revision                       # said ok, wrote nothing
                return super().append(file_id, expected_revision, receipt_id, block)
        refused = di.WakeRing(url="https://wake/wake", secret="s",
                              post=lambda u, b, h: b'{"woke": [{"status": "unknown_agent"}]}')
        d = deps(receipts=DropFirst(), ring=refused)
        self.assertEqual("queued_not_rung", di.decide(hint(), d)["status"])
        self.assertEqual([], d.receipts.lines)
        self.assertEqual("queued_not_rung", di.decide(hint(), d)["status"])
        self.assertEqual(["REQUESTED"], d.receipts.states())    # written on the second pass, ring still refused
        self.assertEqual(1, len(d.queue.puts))
        # the same for a wait line that missed on the delivering pass
        d = deps(docs=Docs(text=REQUEST + "Use the sf CLI.\n"), receipts=DropFirst(), ring=refused)
        self.assertEqual("queued", di.decide(hint(), d)["status"])
        laptop_up(d)
        self.assertEqual("queued_not_rung", di.decide(hint(), d)["status"])
        self.assertEqual(["QUEUED", "REQUESTED"], d.receipts.states())
        # and Cursor's exact shape: claim parked at on_inbox:...:q, task on the inbox, doc empty,
        # ring refused every pass - the wait line must still land before any ring
        text = REQUEST + "Use the sf CLI.\n"
        d = deps(docs=Docs(text=text), ring=refused)
        t = di.correlate(FILE, text, "r1", "claude-code-cli")
        d.claims.states[key_for(text=text)] = "on_inbox:claude-code-cli:1000:q"
        d.queue.puts.append((dict(t), di.gist(text), d.at))
        self.assertEqual("queued_not_rung", di.decide(hint(), d)["status"])
        self.assertEqual(["QUEUED", "REQUESTED"], d.receipts.states())
        self.assertEqual(1, len(d.queue.puts))

    def test_the_body_carries_the_task_id_as_the_message_id_and_the_signature_verifies(self):
        sent = []
        ring = di.WakeRing(url="https://wake/wake", secret="s3cret",
                           post=lambda u, b, h: (sent.append((u, b, h)), b'{"woke": [{"status": "woke"}]}')[1])
        t = di.correlate(FILE, REQUEST, "r1", "claude-code-cli")
        ring(t)
        ring(t)
        self.assertEqual(2, len(sent))
        url, body, headers = sent[0]
        self.assertEqual("https://wake/wake", url)
        parsed = json.loads(body.decode("utf-8"))
        self.assertEqual(t["task_id"], parsed["id"])                 # #359 dedupes on (to, id)
        self.assertEqual("claude-code-cli", parsed["to"])
        self.assertEqual("drive-intake", parsed["by"])
        self.assertNotIn("telephone", body.decode("utf-8"))          # never the doc's words
        self.assertEqual(sent[0][1], sent[1][1])                     # byte-identical, so the same seen key
        # #359's check: hmac.compare_digest("sha256=" + hex(hmac(secret, body)), header)
        import hashlib
        import hmac
        expect = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertEqual(expect, headers["X-Hub-Signature-256"])
        self.assertEqual("application/json", headers["Content-Type"])

    def test_decide_rings_through_the_adapter_with_the_task_id(self):
        sent = []
        d = deps(ring=di.WakeRing(url="https://wake/wake", secret="s",
                                  post=lambda u, b, h: (sent.append(b), b'{"woke": [{"status": "woke"}]}')[1]))
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(out["task"], json.loads(sent[0].decode("utf-8"))["id"])


class TrailTest(unittest.TestCase):
    """The doc is the record with no TTL. Cursor on 96b645c: every Redis key expires, and each
    expiry re-did the hop it guarded - a second put, a second ring after 30 days, a lost wait line."""

    def dispatched_doc(self, text=REQUEST, recipient="claude-api"):
        d = deps(docs=Docs(text=text), api_worker="claude-api")
        if recipient == "claude-code-cli":
            laptop_up(d)
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        doc = text + d.receipts.text()
        return out, doc

    def test_trail_for_reads_our_lines_and_nobody_elses(self):
        out, doc = self.dispatched_doc()
        trail = di.trail_for(doc, out["task"])
        self.assertEqual({"REQUESTED": "claude-api", "DISPATCHED": "claude-api"}, trail)
        self.assertEqual({}, di.trail_for(doc, "task-0000000000000000"))
        self.assertEqual({}, di.trail_for("DISPATCHED by somebody - task " + out["task"], out["task"]))

    def test_a_dispatched_line_in_the_doc_is_a_duplicate_with_every_key_gone(self):
        out, doc = self.dispatched_doc()
        d = deps(docs=Docs(text=doc, revision="r9"))     # fresh Redis: no claim, no marker
        laptop_up(d)                                      # routing would now pick the laptop
        again = di.decide(hint(), d)
        self.assertEqual("duplicate", again["status"], again)
        self.assertEqual("doc", again["by"])
        self.assertEqual([], d.queue.puts)
        self.assertEqual([], d.ring.calls)
        self.assertEqual([], d.receipts.lines)
        self.assertEqual({}, d.claims.states)             # not even claimed

    def test_a_requested_line_in_the_doc_means_no_second_put_and_the_ring_goes_to_that_worker(self):
        """The claim and the marker aged out, the seen key on #359 too; the doc still says who has it."""
        text = REQUEST + "Use the sf CLI.\n"
        d = deps(docs=Docs(text=text))
        self.assertEqual("queued", di.decide(hint(), d)["status"])
        laptop_up(d)
        d.ring = Ring(fail_first=1)
        self.assertEqual("queued_not_rung", di.decide(hint(), d)["status"])   # QUEUED, REQUESTED in the doc
        doc = text + d.receipts.text()
        task = di.correlate(FILE, text, "r1", "claude-code-cli")["task_id"]
        self.assertEqual({"QUEUED": "claude-code-cli", "REQUESTED": "claude-code-cli"}, di.trail_for(doc, task))
        fresh = deps(docs=Docs(text=doc, revision="r9"))  # every key gone; laptop DOWN now
        fresh.receipts.lines = list(d.receipts.lines)
        out = di.decide(hint(), fresh)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual([], fresh.queue.puts)            # no second put, on any stream
        self.assertEqual(["claude-code-cli"], [c["recipient_id"] for c in fresh.ring.calls])
        self.assertEqual(["QUEUED", "REQUESTED", "DISPATCHED"], fresh.receipts.states())
        self.assertEqual("duplicate", di.decide(hint(), fresh)["status"])

    def test_a_wait_line_in_the_doc_restores_the_wait_after_the_claim_expired(self):
        text = REQUEST + "Use the sf CLI.\n"
        d = deps(docs=Docs(text=text))
        self.assertEqual("queued", di.decide(hint(), d)["status"])
        doc = text + d.receipts.text()
        fresh = deps(docs=Docs(text=doc, revision="r9"))  # claim gone, laptop up
        fresh.receipts.lines = list(d.receipts.lines)
        laptop_up(fresh)
        out = di.decide(hint(), fresh)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(["QUEUED", "REQUESTED", "DISPATCHED"], fresh.receipts.states())
        self.assertEqual(1, len(fresh.queue.puts))
        self.assertTrue(":q" in list(fresh.claims.states.values())[0] or
                        list(fresh.claims.states.values()) == [di.DONE])

    def test_a_put_with_no_receipt_and_no_marker_is_still_not_put_again(self):
        """Cursor on b47305f: QUEUED stored, REQUESTED never, put landed, marker and claim gone,
        laptop fresh -> two copies on the laptop's stream; BLOCKED the same on claude-api; and
        the empty trail, same path, one copy on EACH stream."""
        client = StreamClient()
        shapes = (
            (REQUEST + "Use the sf CLI.\n", "claude-code-cli", True),     # QUEUED stored, laptop later up
            (REQUEST, "claude-api", False),                               # BLOCKED stored, api named later
            (REQUEST + "\nno receipts at all\n", "claude-api", True),     # empty trail, laptop fresh
        )
        for text, worker, laptop in shapes:
            client = StreamClient()
            t = di.correlate(FILE, text, "r1", worker)
            # the put landed...
            q = di.InboxQueue(client=client)
            self.assertTrue(q.put(t, di.gist(text), "t0"))
            for i in range(600):
                client.xadd("fleet:inbox:" + worker, {"task_id": "task-%016x" % i})
            client.kv.clear()                                        # ...the marker expired
            # ...and the doc shows only the wait line (or nothing)
            line = ("QUEUED" if "sf CLI" in text else "BLOCKED") + " - task " + t["task_id"] + \
                   " - request x - rev y - recipient " + worker + " - by drive-intake - t\n"
            doc = text + ("" if "no receipts" in text else "\nRECEIPTS\n" + line)
            d = deps(docs=Docs(text=doc, revision="r9"), queue=di.InboxQueue(client=client),
                     api_worker="claude-api" if worker == "claude-api" else "claude-api")
            if laptop:
                laptop_up(d)
            out = di.decide(hint(), d)
            self.assertEqual("dispatched", out["status"], (text[:30], out))
            copies = [f for s in client.streams.values() for _, f in s if f.get("task_id") == t["task_id"]]
            self.assertEqual(1, len(copies), (text[:30], list(client.streams)))
            self.assertEqual([worker], [c["recipient_id"] for c in d.ring.calls])
            self.assertEqual("REQUESTED", [s for s in d.receipts.states() if s != "QUEUED" and s != "BLOCKED"][0])

    def test_the_marker_gone_and_the_claim_back_at_delivering_is_still_one_copy(self):
        """Cursor on 96b645c: deleting fleet:intake:put:<task> with the entry buried appended again."""
        client = StreamClient()
        d = deps(queue=di.InboxQueue(client=client))
        out = di.decide(hint(), d)
        doc = REQUEST + d.receipts.text()
        for i in range(600):
            client.xadd("fleet:inbox:claude-api", {"task_id": "task-%016x" % i})
        client.kv.clear()                                 # the marker's EX fired
        d.docs.text, d.docs.revision = doc, "r9"
        d.claims.states[key_for()] = "delivering:claude-api:0:"
        again = di.decide(hint(), d)
        self.assertEqual("duplicate", again["status"], again)
        self.assertEqual(1, len([f for _, f in client.streams["fleet:inbox:claude-api"] if f.get("task_id") == out["task"]]))
        d.claims.states.clear()
        laptop_up(d)
        self.assertEqual("duplicate", di.decide(hint(), d)["status"])
        self.assertNotIn("fleet:inbox:claude-code-cli", client.streams)


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

    def test_a_changes_channel_push_reads_the_feed_and_delivers(self):
        """Copilot on #362: the folder trigger's push carries no file id; it was answered 200 with nothing done."""
        d = deps(changes=lambda folder: [{"file_id": FILE, "folder_id": folder}])
        push = {"x-goog-channel-token": "tok", "x-goog-resource-state": "change",
                "x-goog-resource-uri": "https://www.googleapis.com/drive/v3/changes?pageToken=7&alt=json"}
        code, out = di.handle("POST", "/hooks/drive", push, b"", d)
        self.assertEqual(200, code)
        self.assertEqual(["dispatched"], [r["status"] for r in out["intake"]], out)
        self.assertEqual(1, len(d.queue.puts))
        self.assertIsNotNone(d.claims.last_poll(), "the push's read of the feed goes on record as a poll")
        again = di.handle("POST", "/hooks/drive", push, b"", d)[1]
        self.assertEqual(["duplicate"], [r["status"] for r in again["intake"]], again)
        self.assertEqual(1, len(d.queue.puts), "a second push of the same change is one task")

    def test_a_changes_channel_push_with_no_feed_says_so(self):
        d = deps(changes=None)
        out = di.handle("POST", "/hooks/drive", {"x-goog-channel-token": "tok",
                                                 "x-goog-resource-state": "change"}, b"", d)[1]
        self.assertEqual(["no_store"], [r["status"] for r in out["intake"]], out)
        self.assertEqual(0, d.docs.reads)


class OfflineTest(unittest.TestCase):
    def test_the_module_opens_no_socket_on_import_and_names_no_credential(self):
        source = (REPO / "cloud" / "drive-intake" / "main.py").read_text(encoding="utf-8")
        for banned in ("import socket", "urllib.request.urlopen(", "import requests", "googleapiclient",
                       "SECRET_MANAGER", "service_account"):
            self.assertNotIn(banned, source, banned)
        self.assertNotIn("redis", sys.modules)


feed_spec = importlib.util.spec_from_file_location("drive_feed", REPO / "cloud" / "drive-intake" / "drive_feed.py")
drive_feed = importlib.util.module_from_spec(feed_spec)
feed_spec.loader.exec_module(drive_feed)


class WiringTest(unittest.TestCase):
    """What a deploy wires, read without a socket: the clock per request, the poll on record, health
    by poll, the scheduler's token header, and the dependencies built from env."""

    def test_the_receipt_clock_is_per_request_in_a_server_and_fixed_in_a_test(self):
        ticks = iter(["2026-10-11T05:00:00Z", "2026-10-11T05:00:01Z"])
        d = deps(at_provider=lambda: next(ticks))
        self.assertEqual("2026-10-11T05:00:00Z", d.at)
        self.assertEqual("2026-10-11T05:00:01Z", d.at)
        fixed = deps()
        self.assertEqual("2026-10-10T21:00:00Z", fixed.at)
        fixed.at = "later"
        self.assertEqual("later", fixed.at)
        d = deps(at_provider=lambda: "2026-10-11T06:00:00Z")
        di.decide(hint(), d)
        self.assertIn(" - 2026-10-11T06:00:00Z", d.receipts.lines[0][2])

    def test_a_poll_that_listed_and_decided_is_on_record_and_a_failed_one_is_not(self):
        d = deps(changes=lambda folder: [{"file_id": FILE, "folder_id": folder}], now=5000.0)
        self.assertIsNone(d.claims.last_poll())
        out = di.poll(d)
        self.assertEqual("dispatched", out[0]["status"])
        self.assertEqual(5000.0, d.claims.last_poll())
        self.assertEqual(5000.0, d.claims.watermark())                    # a plain list: the watermark is now
        self.assertFalse(d.claims.resume())                               # it finished, so the margin applies next
        self.assertEqual(set(), d.claims.pending())                       # finished: not kept
        d = deps(changes=lambda folder: {"hints": [{"file_id": FILE, "folder_id": folder}], "watermark": 4321.0,
                                         "complete": False}, now=5000.0)
        di.poll(d)
        self.assertEqual((5000.0, 4321.0), (d.claims.last_poll(), d.claims.watermark()))   # the feed's own watermark
        self.assertTrue(d.claims.resume())                                # capped: the next poll starts at 4321

        def broken(folder):
            raise ConnectionError()
        d = deps(changes=broken, now=5000.0)
        self.assertEqual("read_error", di.poll(d)[0]["status"])
        self.assertIsNone(d.claims.last_poll())
        d = deps(changes=None, now=5000.0)
        self.assertEqual("no_store", di.poll(d)[0]["status"])
        self.assertIsNone(d.claims.last_poll())
        # a hint that cannot even be decided keeps the poll off the record, and loses no other hint
        class Odd(dict):
            def get(self, key, default=None):
                raise ValueError("odd hint")
        d = deps(changes=lambda folder: [Odd({"file_id": FILE}), {"file_id": FILE, "folder_id": folder}], now=5000.0)
        out = di.poll(d)
        self.assertEqual(["decide_error", "dispatched"], [o["status"] for o in out])
        self.assertIsNone(d.claims.last_poll())

    def test_a_doc_queued_for_the_laptop_is_kept_and_delivered_by_a_later_poll_when_the_laptop_returns(self):
        """Cursor on 0bee083: queued at t=10000, the window had moved past the doc by t=10720, and the
        laptop's return at t=11320 found an empty feed and a claim still queued. Composed as main()
        wires it: the feed lists by modifiedTime since the watermark less the margin."""
        docs_in_folder = [{"id": FILE, "parents": [FOLDER], "modifiedTime": "2025-10-09T08:38:20Z"}]   # 1759999100

        def fetch(method, url, body, headers):
            since = url.split("modifiedTime+%3E+%27")[1].split("%27")[0].replace("%3A", ":")
            listed = [f for f in docs_in_folder if f["modifiedTime"] > since]
            return 200, json.dumps({"files": listed}).encode()
        d = deps(docs=Docs(text=REQUEST + "Use the sf CLI against the sandbox.\n"), api_worker="claude-api")
        feed = drive_feed.DriveFolder(lambda: "tok", fetch, d.claims)
        d.changes = feed.changes
        laptop_down(d)
        for tick in (1760000000.0, 1760000120.0, 1760000240.0, 1760000720.0):
            d.now = feed.now = tick
            out = di.poll(d)
        self.assertEqual("waiting", out[0]["status"], out)                # still queued, STILL SEEN each poll
        self.assertEqual({FILE}, d.claims.pending())
        self.assertEqual([], d.queue.puts)
        self.assertEqual(1760000720.0, d.claims.watermark())              # the cursor kept moving regardless
        d.now = feed.now = 1760001320.0
        laptop_up(d, at=1760001310.0)
        out = di.poll(d)
        self.assertEqual("dispatched", out[0]["status"], out)             # delivered on the laptop's return
        self.assertEqual(["claude-code-cli"], d.queue.recipients())
        self.assertEqual(set(), d.claims.pending())                       # finished: no longer kept
        self.assertEqual("idle", "idle" if di.poll(d) == [] else "busy") # the feed is empty and nothing is pending

    def test_unfinished_statuses_are_kept_and_finished_ones_are_not(self):
        d = deps(ring=lambda t: (_ for _ in ()).throw(di.Unconfigured("no ring")),
                 changes=lambda folder: [{"file_id": FILE, "folder_id": folder}], now=5000.0)
        out = di.poll(d)
        self.assertEqual("queued_not_rung", out[0]["status"])
        self.assertEqual({FILE}, d.claims.pending())                      # the ring is still owed: kept
        self.assertEqual(5000.0, d.claims.last_poll())                    # and the poll IS on record
        d.ring = Ring()
        d.changes = lambda folder: []                                     # the feed no longer lists it
        out = di.poll(d)
        self.assertEqual("dispatched", out[0]["status"], out)             # found through the pending set
        self.assertEqual(set(), d.claims.pending())

        class Broken(Docs):
            def read(self, file_id):
                raise ConnectionError()
        d = deps(docs=Broken(REQUEST), changes=lambda folder: [{"file_id": FILE, "folder_id": folder}], now=5000.0)
        self.assertEqual("read_error", di.poll(d)[0]["status"])
        self.assertEqual({FILE}, d.claims.pending())                      # a failed read is unfinished
        self.assertEqual(5000.0, d.claims.last_poll())
        for terminal in ("bad_recipient", "no_request", "too_large"):
            self.assertIn(terminal, di.FINISHED)
        d = deps(docs=Docs(text="to: somebody\n" + REQUEST), changes=lambda folder: [{"file_id": FILE, "folder_id": folder}], now=5000.0)
        self.assertEqual("bad_recipient", di.poll(d)[0]["status"])
        self.assertEqual(set(), d.claims.pending())                       # a permanent refusal is not kept

    def test_a_pending_set_that_cannot_be_read_or_kept_records_no_poll(self):
        class NoPending(di.MemoryClaims):
            def pending(self):
                raise ConnectionError()
        d = deps(claims=NoPending(), changes=lambda folder: [{"file_id": FILE, "folder_id": folder}], now=5000.0)
        self.assertEqual([{"status": "pending_unreadable", "task": None, "error": "ConnectionError"}], di.poll(d))
        self.assertIsNone(d.claims.last_poll())

        class NoKeep(di.MemoryClaims):
            def pending_add(self, file_id):
                raise ConnectionError()
        d = deps(claims=NoKeep(), ring=lambda t: (_ for _ in ()).throw(di.Unconfigured("x")),
                 changes=lambda folder: [{"file_id": FILE, "folder_id": folder}], now=5000.0)
        out = di.poll(d)
        self.assertEqual("decide_error", out[-1]["status"])
        self.assertIsNone(d.claims.last_poll())                           # the cursor must not pass a doc it cannot keep

    def test_the_polled_ring_is_a_dispatch_that_names_its_transport_and_is_never_the_default(self):
        env = {"DRIVE_INTAKE_ENABLED": "on", "DRIVE_INTAKE_FOLDER_ID": FOLDER, "DRIVE_INTAKE_POLL_TOKEN": "pt"}
        self.assertIs(di.deps_from_env(env, client=None, docs=None).ring, di.no_ring)
        self.assertIs(di.deps_from_env(dict(env, DRIVE_INTAKE_RING="polled"), client=None, docs=None).ring, di.polled_ring)
        self.assertIs(di.deps_from_env(dict(env, DRIVE_INTAKE_RING="POLLED "), client=None, docs=None).ring, di.polled_ring)
        self.assertIs(di.deps_from_env(dict(env, DRIVE_INTAKE_RING="yes"), client=None, docs=None).ring, di.no_ring)
        both = di.deps_from_env(dict(env, DRIVE_INTAKE_RING="polled", INBOX_WAKE_URL="https://w", INBOX_WAKE_SECRET="s"),
                                client=None, docs=None, post=lambda *a: b"{}")
        self.assertIsInstance(both.ring, di.WakeRing)                     # a real ring wins over polling
        d = deps(ring=di.polled_ring)
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        dispatched = [b for _, _, b in d.receipts.lines if "DISPATCHED - task" in b][0]
        self.assertIn("on fleet:inbox:claude-api, which the worker polls; no wake rung", dispatched)
        self.assertEqual(1, len(d.queue.puts))
        d = deps()                                                        # the default still refuses
        self.assertEqual("queued_not_rung", di.decide(hint(), deps(ring=di.no_ring))["status"])

    def test_health_reads_ok_on_a_fresh_poll_degraded_past_three_ticks_and_unknown_with_none(self):
        self.assertEqual({"poll": "unknown", "last_poll": None}, di.poll_health(None, 1000.0))
        self.assertEqual({"poll": "unknown", "last_poll": None}, di.poll_health("soon", 1000.0))
        self.assertEqual("ok", di.poll_health(1000.0 - 360, 1000.0)["poll"])
        self.assertEqual("degraded", di.poll_health(1000.0 - 361, 1000.0)["poll"])
        self.assertEqual("degraded", di.poll_health(1000.0 + 5, 1000.0)["poll"])     # a poll from the future
        self.assertEqual("ok", di.poll_health(1000.0 - 100, 1000.0, tick=60)["poll"])
        d = deps(now=1000.0)
        status, out = di.handle("GET", "/healthz", {}, b"", d)
        self.assertFalse(out["ok"])
        self.assertEqual(("unknown", "unknown"), (out["channel"], out["poll"]))
        d.claims.note_poll(900.0)
        status, out = di.handle("GET", "/healthz", {}, b"", d)
        self.assertTrue(out["ok"])                                       # the poll is the channel
        self.assertEqual(("unknown", "ok", 900, 100), (out["channel"], out["poll"], out["last_poll"], out["poll_age"]))
        d.claims.note_poll(1000.0 - 400)
        self.assertFalse(di.handle("GET", "/healthz", {}, b"", d)[1]["ok"])
        d.channel_expires_at = 1000.0 + 10 ** 6                           # a live watch is enough on its own
        self.assertTrue(di.handle("GET", "/healthz", {}, b"", d)[1]["ok"])

    def test_the_scheduler_may_send_the_poll_token_in_its_own_header(self):
        self.assertTrue(di.poll_token_ok("poll", {"authorization": "Bearer poll"}))
        self.assertTrue(di.poll_token_ok("poll", {"authorization": "Bearer eyJ-an-oidc-token",
                                                  di.POLL_TOKEN_HEADER: "poll"}))
        self.assertFalse(di.poll_token_ok("poll", {di.POLL_TOKEN_HEADER: "wrong"}))
        self.assertFalse(di.poll_token_ok("poll", {}))
        self.assertFalse(di.poll_token_ok("", {di.POLL_TOKEN_HEADER: ""}))
        d = deps(changes=lambda folder: [])
        self.assertEqual(200, di.handle("POST", "/poll", {"Authorization": "Bearer eyJ", "X-Drive-Intake-Poll-Token": "poll"}, b"", d)[0])
        self.assertEqual(401, di.handle("POST", "/poll", {"Authorization": "Bearer eyJ"}, b"", d)[0])
        # the token comes BEFORE the flag: with the flag off, no token still means 401, not "enabled: false"
        off = deps(enabled=False)
        self.assertEqual(401, di.handle("POST", "/poll", {}, b"", off)[0])
        self.assertEqual(401, di.handle("POST", "/hooks/drive", {"X-Goog-Resource-State": "update"}, b"", off)[0])
        self.assertEqual((200, {"enabled": False, "intake": []}),
                         di.handle("POST", "/poll", {"Authorization": "Bearer poll"}, b"", off))

    def test_deps_from_env_wires_what_a_deploy_gives_and_nothing_it_does_not(self):
        env = {"DRIVE_INTAKE_ENABLED": "on", "DRIVE_INTAKE_FOLDER_ID": FOLDER,
               "DRIVE_INTAKE_CHANNEL_TOKEN": "ct", "DRIVE_INTAKE_POLL_TOKEN": "pt",
               "DRIVE_INTAKE_API_WORKER": "claude-api", "DRIVE_INTAKE_HEARTBEAT_SECONDS": "300",
               "DRIVE_INTAKE_POLL_SECONDS": "120"}
        d = di.deps_from_env(env, client=None, docs=None, feed=None, post=None)
        self.assertTrue(d.enabled)
        self.assertEqual((FOLDER, "ct", "pt", "claude-api", 300, 120),
                         (d.folder_id, d.channel_token, d.poll_token, d.api_worker, d.heartbeat_window, d.poll_tick))
        self.assertIsNone(d.claims)
        self.assertIsNone(d.queue)
        self.assertIs(d.ring, di.no_ring)                                 # no wake url: no ring, named
        self.assertRegex(d.at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual("no_store", di.decide(hint(), d)["status"])      # nothing wired, nothing decided
        wired = di.deps_from_env(dict(env, INBOX_WAKE_URL="https://wake", INBOX_WAKE_SECRET="s"),
                                 client=object(), docs=Docs(), feed=lambda f: [], post=lambda *a: b"{}")
        self.assertIsInstance(wired.ring, di.WakeRing)
        half = di.deps_from_env(dict(env, INBOX_WAKE_URL="https://wake"), client=None, docs=None)
        self.assertIs(half.ring, di.no_ring)                              # a url without its secret is no ring
        self.assertIsInstance(wired.claims, di.RedisClaims)
        self.assertIsInstance(wired.queue, di.InboxQueue)
        self.assertIs(wired.receipts, wired.docs)
        off = di.deps_from_env({}, client=None, docs=None)
        self.assertFalse(off.enabled)
        self.assertEqual(di.DEFAULT_POLL_TICK, off.poll_tick)


class Step3ClaudeApiOnly(unittest.TestCase):
    """Gate 3 Step 3 (CO-20261013-001): "recipient claude-api only. No other folder, no other recipient." """

    def only(self, **over):
        over.setdefault("baseline", True)
        return deps(recipients=di.recipients_from_env({"DRIVE_INTAKE_RECIPIENTS": "claude-api"}), **over)

    def test_the_list_parses_and_never_falls_open(self):
        self.assertIsNone(di.recipients_from_env({}))
        self.assertEqual(frozenset({"claude-api"}), di.recipients_from_env({"DRIVE_INTAKE_RECIPIENTS": "claude-api"}))
        self.assertEqual(frozenset(), di.recipients_from_env({"DRIVE_INTAKE_RECIPIENTS": "nobody, *"}))
        self.assertEqual(frozenset(), di.recipients_from_env({"DRIVE_INTAKE_RECIPIENTS": ""}))

    def test_a_request_to_the_laptop_is_refused_not_rerouted(self):
        d = self.only(docs=Docs(text="to: claude-code-cli\n" + REQUEST))
        laptop_up(d)
        out = di.decide(hint(), d)
        self.assertEqual(("not_allowed", "claude-code-cli"), (out["status"], out["to"]))
        self.assertEqual([], d.queue.puts)

    def test_an_unaddressed_request_goes_to_claude_api_even_with_the_laptop_up(self):
        d = self.only()
        laptop_up(d)
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"], out)
        self.assertEqual(["claude-api"], [t["recipient_id"] for t, _, _ in d.queue.puts])

    def test_work_that_needs_the_laptop_is_blocked_and_said(self):
        text = REQUEST + "\nThis one is laptop only: open the local repo.\n"
        self.assertTrue(di.needs_cli_tools(text))
        d = self.only(docs=Docs(text=text))
        out = di.decide(hint(), d)
        self.assertEqual("blocked", out["status"], out)
        self.assertEqual([], d.queue.puts)

    def test_the_first_poll_is_a_baseline_and_lists_nothing(self):
        listed = []

        def feed(folder):
            listed.append(folder)
            return [{"file_id": FILE, "folder_id": folder}]
        d = self.only(changes=feed, now=5000.0)
        first = di.poll(d)
        self.assertEqual(["baseline"], [r["status"] for r in first], first)
        self.assertEqual([], listed, "the folder is not listed on the baseline")
        self.assertEqual([], d.queue.puts)
        self.assertEqual(5000.0, d.claims.floor())
        d.now = 5120.0
        second = di.poll(d)
        self.assertEqual(["dispatched"], [r["status"] for r in second], second)
        self.assertEqual(1, len(listed))

    def test_the_floor_is_set_once(self):
        d = self.only(changes=lambda folder: [], now=5000.0)
        di.poll(d)
        d.claims.beats.pop("watermark")         # the watermark aged out; the floor did not
        d.now = 9000.0
        self.assertEqual([], di.poll(d))
        self.assertEqual(5000.0, d.claims.floor())

    def test_the_feed_never_lists_before_the_floor(self):
        class Store:
            def watermark(self):
                return None                     # no watermark: the bounded look back of a day

            def floor(self):
                return 7200.0
        asked = []
        folder = drive_feed.DriveFolder(lambda: "tok", lambda method, url, body, headers: (
            asked.append(url) or (200, b'{"files": []}')), Store(), now=90000.0)
        folder.changes(FOLDER)
        self.assertIn(drive_feed.iso(7200.0), folder.last_query, folder.last_query)

    def test_a_deploy_names_claude_api_only(self):
        script = (REPO / "cloud" / "drive-intake" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn('RECIPIENTS="${DRIVE_INTAKE_RECIPIENTS:-claude-api}"', script)
        self.assertIn("DRIVE_INTAKE_RECIPIENTS=${RECIPIENTS}", script)
        self.assertIn('FIRST_POLL="${DRIVE_INTAKE_FIRST_POLL:-baseline}"', script)
        self.assertIn("DRIVE_INTAKE_FIRST_POLL=${FIRST_POLL}", script)
        d = di.deps_from_env({"DRIVE_INTAKE_RECIPIENTS": "claude-api", "DRIVE_INTAKE_FIRST_POLL": "baseline"},
                             client=None, docs=None)
        self.assertEqual((frozenset({"claude-api"}), True), (d.recipients, d.baseline))
        self.assertFalse(di.deps_from_env({}, client=None, docs=None).baseline)

    def test_without_the_baseline_the_first_poll_lists_as_before(self):
        d = self.only(changes=lambda folder: [{"file_id": FILE, "folder_id": folder}], baseline=False)
        self.assertEqual(["dispatched"], [r["status"] for r in di.poll(d)])


class FeedTest(unittest.TestCase):
    """The poll's question to Drive, and its bookkeeping."""

    def test_the_query_is_this_folder_docs_only_not_trashed_since(self):
        q = drive_feed.query_for(FOLDER, 1760000000)
        self.assertEqual("'%s' in parents and mimeType = 'application/vnd.google-apps.document' and "
                         "trashed = false and modifiedTime > '2025-10-09T08:53:20Z'" % FOLDER, q)
        with self.assertRaises(drive_feed.Unconfigured):
            drive_feed.query_for("", 1.0)

    def test_since_is_the_watermark_less_a_margin_else_a_bounded_look_back(self):
        self.assertEqual(1760000000.0 - 86400, drive_feed.since_for(None, 1760000000.0))
        self.assertEqual(900.0 - 600, drive_feed.since_for(900.0, 1000.0))
        self.assertEqual(1000.0, drive_feed.since_for(5000.0, 1000.0))          # never ahead of now
        self.assertEqual(1760000000.0 - 86400, drive_feed.since_for("soon", 1760000000.0))
        self.assertEqual(0.0, drive_feed.since_for(None, 1000.0))                # never before the epoch
        self.assertEqual(0.0, drive_feed.since_for(100.0, 1000.0))

    def test_hints_carry_the_file_and_its_parent(self):
        self.assertEqual([{"file_id": "A", "folder_id": "P"}, {"file_id": "B", "folder_id": FOLDER}],
                         drive_feed.hints_from([{"id": "A", "parents": ["P"]}, {"id": "B"}, {"id": ""}, None], FOLDER))

    def test_a_poll_lists_pages_oldest_first_writes_nothing_and_refuses_without_a_token(self):
        calls = []
        pages = [{"files": [{"id": "A", "parents": [FOLDER], "modifiedTime": "2025-10-09T08:50:00Z"}], "nextPageToken": "p2"},
                 {"files": [{"id": "B", "parents": [FOLDER], "modifiedTime": "2025-10-09T08:52:00Z"}]}]

        def fetch(method, url, body, headers):
            calls.append((method, url, headers))
            self.assertEqual("Bearer tok", headers["Authorization"])
            return 200, json.dumps(pages[len(calls) - 1]).encode()
        store = di.MemoryClaims()
        store.note_poll(1760000000.0, 1760000000.0)                               # 2025-10-09T08:53:20Z
        feed = drive_feed.DriveFolder(lambda: "tok", fetch, store, now=1760000100.0)
        answer = feed.changes(FOLDER)
        self.assertEqual([{"file_id": "A", "folder_id": FOLDER}, {"file_id": "B", "folder_id": FOLDER}], answer["hints"])
        self.assertEqual((1760000100.0, True), (answer["watermark"], answer["complete"]))   # complete: now
        self.assertEqual(2, len(calls))
        self.assertIn("pageToken=p2", calls[1][1])
        self.assertIn("modifiedTime+%3E+%272025-10-09T08%3A43%3A20Z%27", calls[0][1])   # the watermark less 600 s
        self.assertIn("orderBy=modifiedTime&", calls[0][1])                        # OLDEST first
        self.assertNotIn("desc", calls[0][1])
        self.assertEqual(1760000000.0, store.last_poll())                         # the feed wrote NOTHING
        self.assertEqual(1760000000.0, store.watermark())
        failing = drive_feed.DriveFolder(lambda: "tok", lambda *a: (500, b'{"files": []}'), di.MemoryClaims(), now=1000.0)
        with self.assertRaises(RuntimeError):
            failing.changes(FOLDER)
        self.assertIsNone(failing.store.last_poll())
        with self.assertRaises(drive_feed.Unconfigured):
            drive_feed.DriveFolder(lambda: "", lambda *a: (200, b"{}"), di.MemoryClaims(), now=1.0).changes(FOLDER)

    def test_a_listing_past_the_page_cap_is_incomplete_and_its_watermark_is_the_last_doc_returned(self):
        """Cursor on 0bee083: eleven pages, ten fetched, the poll recorded as caught up, the older docs
        past the cursor. Oldest first now, and the watermark is the last doc returned, so the next
        poll continues from there."""
        calls = []

        def fetch(method, url, body, headers):
            calls.append(url)
            page = len(calls)
            files = [{"id": "doc-%d-%d" % (page, i), "parents": [FOLDER],
                      "modifiedTime": "2025-10-09T0%d:%02d:00Z" % (page % 10, i)} for i in range(3)]
            answer = {"files": files}
            if page < 11:
                answer["nextPageToken"] = "p%d" % (page + 1)
            return 200, json.dumps(answer).encode()
        store = di.MemoryClaims()
        feed = drive_feed.DriveFolder(lambda: "tok", fetch, store, now=1760000000.0, max_pages=10)
        answer = feed.changes(FOLDER)
        self.assertEqual(10, len(calls))
        self.assertEqual(30, len(answer["hints"]))
        self.assertFalse(answer["complete"])
        self.assertEqual(drive_feed.epoch_of("2025-10-09T00:02:00Z"), answer["watermark"])   # page 10 (10 % 10 = 0), last doc
        self.assertIsNone(store.watermark())                                        # still nothing written here
        self.assertEqual(1760000000.0, drive_feed.epoch_of("2025-10-09T08:53:20.000Z"))
        self.assertIsNone(drive_feed.epoch_of("soon"))
        self.assertIsNone(drive_feed.epoch_of(""))

    def test_a_capped_listing_is_resumed_at_the_last_doc_and_the_margin_does_not_refill_the_cap(self):
        """150 docs, one per second, one page of 100. The margin is 600 s, so resuming ten minutes
        behind the last doc returns the same first page forever and the other 50 are never listed."""
        base = 1760000000.0
        docs = [{"id": "doc-%03d" % i, "parents": [FOLDER],
                 "modifiedTime": drive_feed.iso(base + i)} for i in range(150)]
        calls = []

        def fetch(method, url, body, headers):
            calls.append(url)
            since = url.split("modifiedTime+%3E+%27")[1].split("%27")[0].replace("%3A", ":")
            listed = [f for f in docs if f["modifiedTime"] > since]
            page, rest = listed[:drive_feed.PAGE], listed[drive_feed.PAGE:]
            answer = {"files": page}
            if rest:
                answer["nextPageToken"] = "more"
            return 200, json.dumps(answer).encode()

        store = di.MemoryClaims()
        feed = drive_feed.DriveFolder(lambda: "tok", fetch, store, now=base + 10000, max_pages=1)
        first = feed.changes(FOLDER)
        self.assertFalse(first["complete"])
        self.assertEqual("doc-099", first["hints"][-1]["file_id"])
        self.assertEqual(drive_feed.epoch_of(docs[99]["modifiedTime"]), first["watermark"])
        store.note_poll(base + 10000, first["watermark"], resume=True)
        feed.now = base + 10120
        second = feed.changes(FOLDER)
        resumed = "modifiedTime+%3E+%27" + drive_feed.iso(base + 99).replace(":", "%3A")
        rewound = "modifiedTime+%3E+%27" + drive_feed.iso(base + 99 - 600).replace(":", "%3A")
        self.assertIn(resumed, calls[-1])
        self.assertNotIn(rewound, calls[-1])
        self.assertEqual(["doc-%03d" % i for i in range(100, 150)], [h["file_id"] for h in second["hints"]])
        self.assertTrue(second["complete"])


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the two-Doc pilot; no Google, Redis or model sockets."""
import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloud" / "claude-api-worker"
INTAKE = ROOT / "cloud" / "drive-intake"
sys.path[:0] = [str(WORKER), str(INTAKE)]
contract = importlib.util.spec_from_file_location("drive_intake", INTAKE / "main.py")
di = importlib.util.module_from_spec(contract)
sys.modules["drive_intake"] = di
contract.loader.exec_module(di)
import phone_intake as pilot  # noqa: E402

spec = importlib.util.spec_from_file_location("api_worker", WORKER / "main.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def block(request_id="owner-20261011-01", body="Summarize the new change."):
    return (f"{pilot.BEGIN}\nREQUEST: {request_id}\nTO: claude-api\n"
            f"{body}\n{pilot.END}\n")


class Redis:
    def __init__(self):
        self.values = {}
        self.streams = {}
        self.fail_queue = False

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script, keys, *args):
        if script == pilot.APPROVE_ONCE_LUA:
            approval, issued, record, _ttl = args
            if approval in self.values or issued in self.values:
                return 0
            self.values[issued] = record
            self.values[approval] = record
            return 1
        if script == di.CAS_LUA:
            key, old, new, _ttl = args
            if self.values.get(key) != old:
                return 0
            self.values[key] = new
            return 1
        if script == di.PUT_ONCE_LUA:
            if self.fail_queue:
                raise RuntimeError("mock queue failure")
            stream, marker, recipient, _ttl, *flat = args
            fields = dict(zip(flat[::2], flat[1::2]))
            if marker in self.values:
                return 0
            self.values[marker] = recipient
            if any(row.get("task_id") == fields["task_id"] for _, row in self.streams.get(stream, [])):
                return 0
            rows = self.streams.setdefault(stream, [])
            rows.append((f"1700000000000-{len(rows)}", fields))
            return 1
        raise AssertionError("unexpected Redis script")

    def xrevrange(self, key, *args, count=None):
        return list(reversed(self.streams.get(key, [])))[:count]

    def xrange(self, key, min="-", max="+", count=None):
        def number(value):
            return tuple(int(part) for part in value.lstrip("(").split("-"))
        rows = [(entry_id, fields) for entry_id, fields in self.streams.get(key, [])
                if (min == "-" or number(entry_id) > number(min))
                and (max == "+" or number(entry_id) <= number(max))]
        return rows[:count]


class Docs:
    def __init__(self):
        self.documents = {file_id: {"text": "Architecture notes.\n",
                                    "revision_id": "r1", "folder_id": pilot.FOLDER_ID}
                          for file_id in pilot.DOC_IDS}
        self.reads = []
        self.appends = []
        self.append_then_raise = False
        self.unknown_readback = False

    def read(self, file_id):
        if self.unknown_readback and self.appends:
            raise TimeoutError("mock readback unavailable")
        self.reads.append(file_id)
        return dict(self.documents[file_id])

    def append(self, file_id, expected_revision, receipt_id, text):
        doc = self.documents[file_id]
        if expected_revision != doc["revision_id"]:
            raise ValueError("revision mismatch")
        doc["text"] += "\n" + text + "\n"
        doc["revision_id"] = "r" + str(int(doc["revision_id"][1:]) + 1)
        self.appends.append((file_id, expected_revision, receipt_id, text))
        if self.append_then_raise:
            self.append_then_raise = False
            raise TimeoutError("append response lost")
        return doc["revision_id"]

    def has(self, file_id, receipt_id):
        if self.unknown_readback and self.appends:
            raise TimeoutError("mock readback unavailable")
        return ("id " + receipt_id) in self.read(file_id)["text"]

    def add_request(self, file_id, request):
        doc = self.documents[file_id]
        old = doc["text"]
        match = di.RECEIPTS_HEADING.search(old)
        at = match.start() if match else len(old)
        doc["text"] = old[:at] + "\n" + request + old[at:]
        doc["revision_id"] = "r" + str(int(doc["revision_id"][1:]) + 1)


class PhoneIntakeTests(unittest.TestCase):
    def setUp(self):
        self.redis = Redis()
        self.docs = Docs()
        self.doc = pilot.DOC_IDS[0]
        self.now = 1791690000.0

    def pass_once(self):
        return pilot.run_prepass(self.redis, self.docs, enabled=True, now=self.now,
                                 at="2026-10-11T05:00:00Z")

    def fresh(self):
        self.pass_once()
        self.docs.add_request(self.doc, block())
        return self.pass_once()

    def target(self, request_id="owner-20261011-02", body="Handle the second change."):
        record = di.correlate(self.doc, block(request_id, body), "r1", "claude-api")
        record.pop("drive_revision")
        return pilot.target_from_env({"PHONE_DOC_PILOT_TARGET": json.dumps(record)})

    def targeted_deps(self, target, claims=None, model=None):
        return worker.Deps(
            enabled=True,
            inbox=worker.pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis, target=target),
            docs=self.docs, receipts=self.docs, claims=claims or worker.MemoryClaims(),
            ledger=worker.MemoryLedger(), model=model, model_name="offline-mock",
            folder_id=pilot.FOLDER_ID, now=self.now, at="2026-10-11T05:00:00Z",
            cap_cents=100, reserve_cents=1, price_in=1, price_out=1, cad_per_usd=1,
            max_input_tokens=12000, authorized_until=self.now + 3600)

    def test_untouched_historical_docs_initialize_then_stay_idle(self):
        self.docs.add_request(self.doc, block("owner-old-0001", "A historical request."))
        first = self.pass_once()
        second = self.pass_once()
        self.assertEqual([x["status"] for x in first], ["baseline_initialized"] * 2)
        self.assertTrue(all(x["status"] == "no_fresh_request" for x in second))
        self.assertEqual(self.redis.streams, {})
        self.assertEqual(self.docs.appends, [])

    def test_fresh_request_queues_once_with_honest_receipt_and_worker_admission(self):
        results = self.fresh()
        self.assertTrue(any(x["status"] == "queued_not_rung" and x.get("admitted") for x in results))
        rows = self.redis.streams["fleet:inbox:claude-api"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1]["source_doc_id"], self.doc)
        self.assertIn("Architecture notes.", self.docs.documents[self.doc]["text"])
        self.assertIn("REQUESTED - task", self.docs.documents[self.doc]["text"])
        self.assertNotIn("DISPATCHED - task", self.docs.documents[self.doc]["text"])
        inbox = pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis)
        self.assertEqual(len(inbox.read("claude-api", 500)), 1)
        focused = pilot.PilotDocs(self.docs).read(self.doc)["text"]
        self.assertIn("Summarize the new change.", focused)
        self.assertNotIn("Architecture notes.", focused)

    def test_disallowed_document_is_rejected_before_read_and_worker_admission(self):
        outside = "1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        with self.assertRaises(pilot.OutOfScope):
            pilot.PilotDocs(self.docs).read(outside)
        self.assertNotIn(outside, self.docs.reads)
        fake = {"kind": "drive_request", "source_doc_id": outside,
                "source_revision": "a" * 64, "request_id": "owner-new-0001",
                "task_id": "task-1234567890abcdef", "recipient_id": "claude-api"}
        self.assertFalse(pilot.admitted(fake, self.redis))

    def test_duplicate_and_receipt_only_edit_do_not_queue_again(self):
        self.fresh()
        before = len(self.docs.appends)
        self.docs.documents[self.doc]["text"] += "\nRECEIPT ONLY - no request\n"
        self.docs.documents[self.doc]["revision_id"] = "r9"
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "duplicate" for x in out))
        self.assertEqual(len(self.redis.streams["fleet:inbox:claude-api"]), 1)
        self.assertEqual(len(self.docs.appends), before)

    def test_second_new_owner_id_is_accepted_after_first_without_removing_history(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "queued_not_rung" and x.get("admitted") for x in out))
        rows = self.redis.streams["fleet:inbox:claude-api"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[1]["request_id"] for row in rows],
                         ["owner-20261011-01", "owner-20261011-02"])
        self.assertIn("owner-20261011-01", self.docs.documents[self.doc]["text"])
        self.assertIn("owner-20261011-02", self.docs.documents[self.doc]["text"])
        self.assertNotIn("fleet:intake:poll:last", self.redis.values)

    def test_queued_older_request_runs_before_newer_request_from_same_doc(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        self.pass_once()
        calls = []

        def model(_system, prompt):
            calls.append(prompt)
            return {"text": "A short answer.", "input_tokens": 10,
                    "output_tokens": 5, "model": "offline-mock", "stop_reason": "end_turn"}

        views = pilot.PilotDocs(self.docs)
        deps = worker.Deps(
            enabled=True, inbox=pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis),
            docs=self.docs, receipts=self.docs, claims=worker.MemoryClaims(),
            ledger=worker.MemoryLedger(), model=model, model_name="offline-mock",
            folder_id=pilot.FOLDER_ID, now=self.now, at="2026-10-11T05:00:00Z",
            cap_cents=100, reserve_cents=1, price_in=1, price_out=1, cad_per_usd=1,
            max_input_tokens=12000, authorized_until=self.now + 3600)
        deps.pilot_docs = views
        first = worker.run_once(deps)
        second = worker.run_once(deps)
        third = worker.run_once(deps)
        self.assertEqual((first["calls"], second["calls"]), (1, 1))
        self.assertEqual(first["considered"][0]["status"], "succeeded")
        self.assertEqual(second["considered"][0]["status"], "succeeded")
        self.assertEqual(third["calls"], 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("Summarize the new change.", calls[0])
        self.assertNotIn("Handle the second change.", calls[0])
        self.assertIn("Handle the second change.", calls[1])
        rows = self.redis.streams["fleet:inbox:claude-api"]
        self.assertEqual(len({row[1]["task_id"] for row in rows}), 2)
        self.assertEqual(sum("SUCCEEDED - task" in text for text in
                             self.docs.documents[self.doc]["text"].splitlines()), 2)

    def test_targeted_pilot_runs_b_without_consuming_older_approved_a(self):
        self.fresh()  # A is already approved and queued.
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        self.docs.add_request(pilot.DOC_IDS[1], block("owner-other-0001", "Unrelated Doc."))
        target = self.target()
        intake = pilot.run_prepass(self.redis, self.docs, enabled=True, target=target,
                                   now=self.now, at="2026-10-11T05:00:00Z")
        self.assertTrue(pilot.target_ready(intake, target), intake)
        self.assertEqual([row[1]["source_doc_id"] for row in
                          self.redis.streams["fleet:inbox:claude-api"]], [self.doc, self.doc])
        calls = []

        def model(_system, prompt):
            calls.append(prompt)
            return {"text": "B answer.", "input_tokens": 10, "output_tokens": 5,
                    "model": "offline-mock", "stop_reason": "end_turn"}

        deps = self.targeted_deps(target, model=model)
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        before_cursor = self.redis.values.get("fleet:worker:inbox_scan:claude-api")
        result = worker.run_once(deps)
        self.assertEqual((result["calls"], result["considered"][0]["task"]),
                         (1, target["task_id"]))
        self.assertEqual(len(calls), 1)
        self.assertIn("Handle the second change.", calls[0])
        self.assertNotIn("Summarize the new change.", calls[0])
        self.assertEqual(self.redis.values.get("fleet:worker:inbox_scan:claude-api"), before_cursor)
        self.assertNotIn("SUCCEEDED - task " + self.redis.streams["fleet:inbox:claude-api"][0][1]["task_id"],
                         self.docs.documents[self.doc]["text"])
        again = worker.run_once(deps)
        self.assertEqual(again["calls"], 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=target), target))
        self.assertEqual(len(self.redis.streams["fleet:inbox:claude-api"]), 2)

    def test_targeted_pilot_rejects_wrong_revision_and_missing_baseline(self):
        target = self.target()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        missing = pilot.run_prepass(self.redis, self.docs, enabled=True, target=target)
        self.assertFalse(pilot.target_ready(missing, target))
        self.assertEqual(self.redis.streams, {})
        self.pass_once()  # historical snapshot only
        self.docs.add_request(self.doc, block("owner-20261011-03", "A later request."))
        wrong = dict(target, source_revision="f" * 64)
        outcome = pilot.run_prepass(self.redis, self.docs, enabled=True, target=wrong)
        self.assertFalse(pilot.target_ready(outcome, wrong))
        self.assertEqual(self.redis.streams, {})
        self.assertEqual(self.docs.appends, [])

    def test_targeted_pilot_validates_both_baselines_before_queueing(self):
        self.pass_once()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        target = self.target()
        self.redis.values.pop(pilot._baseline_key(pilot.DOC_IDS[1]))
        outcome = pilot.run_prepass(self.redis, self.docs, enabled=True, target=target)
        self.assertFalse(pilot.target_ready(outcome, target))
        self.assertEqual(self.redis.streams, {})
        self.assertEqual(self.docs.appends, [])
        self.assertIsNone(self.redis.get(pilot._approval_key(self.doc, target["request_id"])))

    def test_expired_or_revoked_approval_is_not_issued_again_from_old_receipt(self):
        for cause in ("expired", "revoked"):
            with self.subTest(cause=cause):
                self.setUp()
                self.fresh()
                self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
                target = self.target()
                self.assertTrue(pilot.target_ready(pilot.run_prepass(
                    self.redis, self.docs, enabled=True, target=target), target))
                approval = pilot._approval_key(self.doc, target["request_id"])
                issued = pilot._issued_key(self.doc, target["request_id"])
                self.assertIsNotNone(self.redis.get(issued))
                row_count = len(self.redis.streams["fleet:inbox:claude-api"])
                self.redis.values.pop(approval)  # simulated TTL expiry or explicit removal
                outcome = pilot.run_prepass(self.redis, self.docs, enabled=True, target=target)
                self.assertFalse(pilot.target_ready(outcome, target))
                self.assertTrue(any(x["status"] == "approval_lapsed" for x in outcome))
                self.assertIsNone(self.redis.get(approval))
                self.assertEqual(len(self.redis.streams["fleet:inbox:claude-api"]), row_count)

    def test_approval_removed_during_accepted_never_reserves_or_calls(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        target = self.target()
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=target), target))
        approval = pilot._approval_key(self.doc, target["request_id"])
        original_append = self.docs.append

        def revoke_at_accept(file_id, revision, receipt_id, receipt):
            if "ACCEPTED - task" in receipt:
                self.redis.values.pop(approval, None)
            return original_append(file_id, revision, receipt_id, receipt)

        self.docs.append = revoke_at_accept
        calls = []
        deps = self.targeted_deps(target, model=lambda *_: calls.append(1))
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        self.assertGreater(deps.authorized_until, self.now)
        result = worker.run_once(deps)
        self.assertEqual(result["considered"][0]["status"], "approval_unconfirmed")
        self.assertEqual((result["calls"], calls, deps.ledger.days), (0, [], {}))
        self.assertIn("FAILED - task " + target["task_id"], self.docs.documents[self.doc]["text"])
        self.assertNotIn("fleet:worker:inbox_scan:claude-api", self.redis.values)

    def test_unreadable_approval_at_accepted_is_reported_as_unconfirmed(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        target = self.target()
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=target), target))
        approval = pilot._approval_key(self.doc, target["request_id"])
        original_append, original_get = self.docs.append, self.redis.get
        unreadable = False

        def append(file_id, revision, receipt_id, receipt):
            nonlocal unreadable
            if "ACCEPTED - task" in receipt:
                unreadable = True
            return original_append(file_id, revision, receipt_id, receipt)

        def get(key):
            if unreadable and key == approval:
                raise OSError("approval read unavailable")
            return original_get(key)

        self.docs.append, self.redis.get = append, get
        calls = []
        deps = self.targeted_deps(target, model=lambda *_: calls.append(1))
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        result = worker.run_once(deps)
        self.assertEqual(result["considered"][0]["status"], "approval_unconfirmed")
        self.assertEqual((result["calls"], calls, deps.ledger.days), (0, [], {}))
        self.assertIn("approval not confirmed", self.docs.documents[self.doc]["text"])

    def test_approval_removed_during_reservation_refunds_unstarted_call(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        target = self.target()
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=target), target))
        approval = pilot._approval_key(self.doc, target["request_id"])

        class RevokingLedger(worker.MemoryLedger):
            def __init__(self, redis):
                super().__init__()
                self.redis = redis
                self.reservations = []

            def reserve(self, day, cents, cap, *args):
                self.reservations.append(cents)
                admitted = super().reserve(day, cents, cap, *args)
                self.redis.values.pop(approval, None)
                return admitted

        ledger = RevokingLedger(self.redis)
        calls = []
        deps = self.targeted_deps(target, model=lambda *_: calls.append(1))
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        deps.ledger = ledger
        self.assertGreater(deps.authorized_until, self.now)
        result = worker.run_once(deps)
        self.assertEqual(result["considered"][0]["status"], "approval_unconfirmed")
        self.assertEqual(result["considered"][0]["ledger"], "unstarted_reservation_released")
        self.assertEqual((result["calls"], calls), (0, []))
        self.assertEqual(len(ledger.reservations), 1)
        self.assertEqual(ledger.days, {"2026-10-10": 0})
        self.assertEqual(worker.run_once(deps)["status"], "target_unavailable")
        self.assertNotIn("fleet:worker:inbox_scan:claude-api", self.redis.values)

    def test_unconfirmed_refund_after_approval_loss_keeps_reservation_for_reconciliation(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        target = self.target()
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=target), target))
        approval = pilot._approval_key(self.doc, target["request_id"])

        class RefundFails(worker.MemoryLedger):
            def reserve(self, day, cents, cap, *args):
                admitted = super().reserve(day, cents, cap, *args)
                self_redis.values.pop(approval, None)
                return admitted

            def settle(self, day, delta):
                raise OSError("refund acknowledgement unavailable")

        self_redis = self.redis
        calls = []
        deps = self.targeted_deps(target, model=lambda *_: calls.append(1))
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        deps.ledger = RefundFails()
        result = worker.run_once(deps)
        self.assertEqual(result["considered"][0]["status"],
                         "approval_unconfirmed_reservation_unknown")
        self.assertEqual((result["calls"], calls), (0, []))
        self.assertEqual(deps.ledger.days, {"2026-10-10": 2})
        self.assertEqual(worker.run_once(deps)["status"], "target_unavailable")

    def test_targeted_pilot_ignores_concurrent_unrelated_stream_append(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        target = self.target()
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=target), target))
        stream = "fleet:inbox:claude-api"
        unrelated = dict(self.redis.streams[stream][0][1], task_id="task-aaaaaaaaaaaaaaaa")
        self.redis.streams[stream].append(("1700000000000-99", unrelated))
        calls = []

        def model(*_):
            calls.append(1)
            self.docs.add_request(self.doc, block("owner-20261011-03", "Concurrent C."))
            return {"text": "B answer.", "input_tokens": 10, "output_tokens": 5,
                    "model": "offline-mock", "stop_reason": "end_turn"}

        deps = self.targeted_deps(target, model=model)
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        self.assertEqual(worker.run_once(deps)["calls"], 1)
        self.assertEqual(calls, [1])
        self.assertNotIn("fleet:worker:inbox_scan:claude-api", self.redis.values)
        self.assertIn("B answer.", self.docs.documents[self.doc]["text"])
        self.assertNotIn("SUCCEEDED - task task-aaaaaaaaaaaaaaaa", self.docs.documents[self.doc]["text"])

    def test_targeted_pilot_buried_or_duplicate_target_fails_closed(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        target = self.target()
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=target), target))
        inbox = pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis, target=target)
        stream = self.redis.streams["fleet:inbox:claude-api"]
        stream.append(("1700000000000-99", dict(stream[-1][1])))
        with self.assertRaises(pilot.TargetUnavailable):
            inbox.read("claude-api", 500)
        stream.pop()
        for n in range(500):
            stream.append((f"1700000000001-{n}", {"task_id": f"task-{n:016x}"}))
        with self.assertRaises(pilot.TargetUnavailable):
            inbox.read("claude-api", 500)
        deps = self.targeted_deps(target, model=lambda *_: self.fail("model called"))
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        self.assertEqual(worker.run_once(deps)["status"], "target_unavailable")
        self.assertNotIn("fleet:worker:inbox_scan:claude-api", self.redis.values)

    def test_targeted_pilot_wrong_queued_revision_or_approval_never_calls(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        target = self.target()
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=target), target))
        row = self.redis.streams["fleet:inbox:claude-api"][-1][1]
        row["source_revision"] = "f" * 64
        calls = []
        deps = self.targeted_deps(target, model=lambda *_: calls.append(1))
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        result = worker.run_once(deps)
        self.assertEqual(result["status"], "target_unavailable")
        self.assertEqual(result.get("calls", 0), 0)
        self.assertEqual(calls, [])
        self.assertEqual(deps.ledger.days, {})
        self.assertNotIn("fleet:worker:inbox_scan:claude-api", self.redis.values)

    def test_targeted_edit_before_accepted_and_stored_result_retry(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        target = self.target()
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=target), target))
        original_append = self.docs.append
        changed = False

        def edit_at_accept(file_id, revision, receipt_id, receipt):
            nonlocal changed
            if not changed and "ACCEPTED - task" in receipt:
                changed = True
                doc = self.docs.documents[file_id]
                doc["text"] = doc["text"].replace("Handle the second change.", "Changed B.")
                doc["revision_id"] = "r99"
            return original_append(file_id, revision, receipt_id, receipt)

        self.docs.append = edit_at_accept
        calls = []
        deps = self.targeted_deps(target, model=lambda *_: calls.append(1))
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        self.assertEqual(worker.run_once(deps)["calls"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(deps.ledger.days, {})
        self.assertNotIn("fleet:worker:inbox_scan:claude-api", self.redis.values)

        # A separate task gets a stored answer but loses its terminal append.
        # The next targeted pass must write that answer without another call.
        other = self.target("owner-20261011-04", "A fresh selected task.")
        self.docs.add_request(self.doc, block("owner-20261011-04", "A fresh selected task."))
        self.assertTrue(pilot.target_ready(pilot.run_prepass(
            self.redis, self.docs, enabled=True, target=other), other))
        failures = 0

        def fail_terminal(file_id, revision, receipt_id, receipt):
            nonlocal failures
            if "SUCCEEDED - task" in receipt and failures < 2:
                failures += 1
                raise TimeoutError("offline terminal write failure")
            return original_append(file_id, revision, receipt_id, receipt)

        self.docs.append = fail_terminal
        model_calls = []

        def answer(*_):
            model_calls.append(1)
            return {"text": "Stored answer.", "input_tokens": 10, "output_tokens": 5,
                    "model": "offline-mock", "stop_reason": "end_turn"}

        retry = self.targeted_deps(other, model=answer)
        retry.pilot_docs = pilot.PilotDocs(self.docs)
        first = worker.run_once(retry)
        second = worker.run_once(retry)
        self.assertEqual((first["calls"], second["calls"]), (1, 0))
        self.assertEqual(model_calls, [1])
        self.assertIn("Stored answer.", self.docs.documents[self.doc]["text"])
        self.assertNotIn("fleet:worker:inbox_scan:claude-api", self.redis.values)

    def test_pilot_main_requires_target_before_loading_external_clients(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"CLAUDE_API_WORKER_ENABLED": "on",
                                  "PHONE_DOC_INTAKE_ENABLED": "on",
                                  "PHONE_DOC_PILOT_TARGET": ""}), redirect_stdout(output):
            self.assertEqual(worker.main(), 1)
        self.assertIn("pilot_target_invalid", output.getvalue())
        output = io.StringIO()
        with patch.dict(os.environ, {"CLAUDE_API_WORKER_ENABLED": "on",
                                  "PHONE_DOC_INTAKE_ENABLED": "on",
                                  "CLAUDE_API_LANE_ENABLED": "on",
                                  "PHONE_DOC_PILOT_TARGET": json.dumps(self.target())}), \
                redirect_stdout(output):
            self.assertEqual(worker.main(), 1)
        self.assertIn("pilot_flags_invalid", output.getvalue())

    def test_edited_queued_block_is_superseded_without_using_newer_text(self):
        self.fresh()
        self.docs.add_request(self.doc, block("owner-20261011-02", "Handle the second change."))
        self.pass_once()
        doc = self.docs.documents[self.doc]
        doc["text"] = doc["text"].replace("Summarize the new change.", "Edited after queueing.")
        doc["revision_id"] = "r99"
        calls = []

        def model(_system, prompt):
            calls.append(prompt)
            return {"text": "A short answer.", "input_tokens": 10,
                    "output_tokens": 5, "model": "offline-mock", "stop_reason": "end_turn"}

        views = pilot.PilotDocs(self.docs)
        deps = worker.Deps(
            enabled=True, inbox=pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis),
            docs=self.docs, receipts=self.docs, claims=worker.MemoryClaims(),
            ledger=worker.MemoryLedger(), model=model, model_name="offline-mock",
            folder_id=pilot.FOLDER_ID, now=self.now, at="2026-10-11T05:00:00Z",
            cap_cents=100, reserve_cents=1, price_in=1, price_out=1, cad_per_usd=1,
            max_input_tokens=12000, authorized_until=self.now + 3600)
        deps.pilot_docs = views
        out = worker.run_once(deps)
        self.assertEqual([entry["status"] for entry in out["considered"]],
                         ["superseded", "succeeded"])
        self.assertEqual(len(calls), 1)
        self.assertIn("Handle the second change.", calls[0])
        self.assertNotIn("Edited after queueing.", calls[0])

    def test_edit_during_accepted_receipt_retry_never_calls_model(self):
        self.fresh()
        original_append = self.docs.append
        edited = False

        def edit_before_append(file_id, expected_revision, receipt_id, receipt):
            nonlocal edited
            if not edited and "ACCEPTED - task" in receipt:
                edited = True
                doc = self.docs.documents[file_id]
                doc["text"] = doc["text"].replace(
                    "Summarize the new change.", "Edited during receipt retry.")
                doc["revision_id"] = "r99"
            return original_append(file_id, expected_revision, receipt_id, receipt)

        self.docs.append = edit_before_append
        calls = []
        views = pilot.PilotDocs(self.docs)
        deps = worker.Deps(
            enabled=True, inbox=pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis),
            docs=self.docs, receipts=self.docs, claims=worker.MemoryClaims(),
            ledger=worker.MemoryLedger(), model=lambda *_: calls.append(1),
            model_name="offline-mock", folder_id=pilot.FOLDER_ID, now=self.now,
            at="2026-10-11T05:00:00Z", cap_cents=100, reserve_cents=1,
            price_in=1, price_out=1, cad_per_usd=1, max_input_tokens=12000,
            authorized_until=self.now + 3600)
        deps.pilot_docs = views
        out = worker.run_once(deps)
        self.assertTrue(edited)
        self.assertEqual(out["calls"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(out["considered"][0]["status"], "accepted_receipt_owed")
        self.assertNotIn("SUCCEEDED - task", self.docs.documents[self.doc]["text"])

    def test_edit_during_model_call_still_records_stored_result(self):
        self.fresh()
        calls = []

        def model(_system, _prompt):
            calls.append(1)
            doc = self.docs.documents[self.doc]
            doc["text"] = doc["text"].replace(
                "Summarize the new change.", "Edited while model was running.")
            doc["revision_id"] = "r99"
            return {"text": "A short answer.", "input_tokens": 10,
                    "output_tokens": 5, "model": "offline-mock", "stop_reason": "end_turn"}

        claims = worker.MemoryClaims()
        deps = worker.Deps(
            enabled=True, inbox=pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis),
            docs=self.docs, receipts=self.docs, claims=claims,
            ledger=worker.MemoryLedger(), model=model, model_name="offline-mock",
            folder_id=pilot.FOLDER_ID, now=self.now, at="2026-10-11T05:00:00Z",
            cap_cents=100, reserve_cents=1, price_in=1, price_out=1, cad_per_usd=1,
            max_input_tokens=12000, authorized_until=self.now + 3600)
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        out = worker.run_once(deps)
        self.assertEqual((out["calls"], out["considered"][0]["status"]), (1, "succeeded"))
        self.assertEqual(len(calls), 1)
        self.assertIn("SUCCEEDED - task", self.docs.documents[self.doc]["text"])
        self.assertIn("A short answer.", self.docs.documents[self.doc]["text"])
        self.assertTrue(claims.result(out["considered"][0]["task"]))

    def test_stored_result_recovers_after_edit_and_failed_terminal_append(self):
        self.fresh()
        original_append = self.docs.append
        terminal_failures = 0

        def fail_first_terminal(file_id, expected_revision, receipt_id, receipt):
            nonlocal terminal_failures
            if "SUCCEEDED - task" in receipt and terminal_failures < 2:
                terminal_failures += 1
                raise TimeoutError("terminal append temporarily unavailable")
            return original_append(file_id, expected_revision, receipt_id, receipt)

        self.docs.append = fail_first_terminal
        calls = []

        def model(_system, _prompt):
            calls.append(1)
            doc = self.docs.documents[self.doc]
            doc["text"] = doc["text"].replace(
                "Summarize the new change.", "Edited while model was running.")
            doc["revision_id"] = "r99"
            return {"text": "A short answer.", "input_tokens": 10,
                    "output_tokens": 5, "model": "offline-mock", "stop_reason": "end_turn"}

        claims = worker.MemoryClaims()
        deps = worker.Deps(
            enabled=True, inbox=pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis),
            docs=self.docs, receipts=self.docs, claims=claims,
            ledger=worker.MemoryLedger(), model=model, model_name="offline-mock",
            folder_id=pilot.FOLDER_ID, now=self.now, at="2026-10-11T05:00:00Z",
            cap_cents=100, reserve_cents=1, price_in=1, price_out=1, cad_per_usd=1,
            max_input_tokens=12000, authorized_until=self.now + 3600)
        deps.pilot_docs = pilot.PilotDocs(self.docs)
        first = worker.run_once(deps)
        self.assertEqual((first["calls"], first["considered"][0]["status"]),
                         (1, "terminal_receipt_owed"))
        task_id = first["considered"][0]["task"]
        self.assertTrue(claims.result(task_id))
        self.assertNotIn("SUCCEEDED - task", self.docs.documents[self.doc]["text"])
        second = worker.run_once(deps)
        self.assertEqual((second["calls"], second["considered"][0]["status"]),
                         (0, "succeeded"))
        self.assertEqual(len(calls), 1)
        self.assertIn("SUCCEEDED - task", self.docs.documents[self.doc]["text"])
        self.assertTrue(claims.done(task_id))

    def test_shared_parser_request_alias_cannot_smuggle_a_different_id(self):
        self.pass_once()
        smuggled = block().replace("REQUEST: owner-20261011-01",
                                   "REQUEST=forged-00001\nREQUEST: owner-20261011-01")
        self.docs.add_request(self.doc, smuggled)
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "intake_error" for x in out))
        self.assertEqual(self.redis.streams, {})

    def test_shared_parser_target_alias_cannot_route_to_laptop(self):
        self.pass_once()
        smuggled = block().replace("TO: claude-api", "# TO=claude-code-cli\nTO: claude-api")
        self.docs.add_request(self.doc, smuggled)
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "intake_error" for x in out))
        self.assertEqual(self.redis.streams, {})

    def test_request_below_receipts_heading_is_inert(self):
        self.pass_once()
        self.docs.documents[self.doc]["text"] += "\nRECEIPTS\n" + block()
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "no_fresh_request" for x in out))
        self.assertEqual(self.redis.streams, {})

    def test_quoted_receipt_id_cannot_approve_without_structured_readback(self):
        self.pass_once()
        self.docs.add_request(self.doc, block())
        doc = self.docs.documents[self.doc]
        focus = pilot.focused_text(doc["text"])
        tuple_ = di.correlate(self.doc, focus, doc["revision_id"], "claude-api")
        receipt_id = di.receipt_id_for(tuple_, di.REQUESTED)
        doc["text"] += "\nRECEIPTS\nRESULT: merely quoted id " + receipt_id + "\n"
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "queued_not_rung" and x.get("admitted") for x in out))
        self.assertEqual(len(self.docs.appends), 1)
        self.assertIn("REQUESTED - task", doc["text"])

    def test_moved_doc_blocks_worker_even_without_folder_env(self):
        self.fresh()
        self.docs.documents[self.doc]["folder_id"] = "1DifferentParentFolderAAAAAAAAAAAAAAAAAA"
        views = pilot.PilotDocs(self.docs)
        with self.assertRaises(pilot.OutOfScope):
            views.read(self.doc)
        calls = []
        deps = worker.Deps(
            enabled=True, inbox=pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis),
            docs=views, receipts=views, claims=worker.MemoryClaims(),
            ledger=worker.MemoryLedger(), model=lambda *_: calls.append(1),
            folder_id="", now=self.now, at="2026-10-11T05:00:00Z",
            cap_cents=100, reserve_cents=1)
        out = worker.run_once(deps)
        self.assertEqual(out["calls"], 0)
        self.assertEqual(calls, [])
        self.assertTrue(any(x["status"] == "read_error" for x in out["considered"]))

    def test_lost_append_response_reads_back_without_duplicate(self):
        self.pass_once()
        self.docs.add_request(self.doc, block())
        self.docs.append_then_raise = True
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "queued_not_rung" and x.get("admitted") for x in out))
        self.assertEqual(len(self.docs.appends), 1)
        self.assertEqual(len(self.redis.streams["fleet:inbox:claude-api"]), 1)

    def test_ambiguous_append_waits_for_readback_before_worker_admission(self):
        self.pass_once()
        self.docs.add_request(self.doc, block())
        self.docs.unknown_readback = True
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "queued_not_rung" and not x.get("admitted") for x in out))
        self.assertTrue(any(key.startswith("fleet:intake:put:") for key in self.redis.values))
        self.assertFalse(any(key.startswith("fleet:intake:pilot:approved:")
                             for key in self.redis.values))
        self.docs.unknown_readback = False
        self.now += 1
        retry = self.pass_once()
        self.assertTrue(any(x["status"] == "approval_lapsed" for x in retry))
        self.assertFalse(any(key.startswith("fleet:intake:pilot:approved:")
                             for key in self.redis.values))
        self.assertEqual(len(self.redis.streams["fleet:inbox:claude-api"]), 1)

    def test_queue_failure_never_approves_then_recovers(self):
        self.pass_once()
        self.docs.add_request(self.doc, block())
        self.redis.fail_queue = True
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "queue_error" for x in out))
        self.assertFalse(any(key.startswith("fleet:intake:pilot:approved:")
                             for key in self.redis.values))
        self.redis.fail_queue = False
        self.now += 1
        retry = self.pass_once()
        self.assertTrue(any(x["status"] == "queued_not_rung" and x.get("admitted") for x in retry))

    def test_ambiguous_blocks_and_same_id_edit_fail_closed(self):
        self.pass_once()
        self.docs.add_request(self.doc, block("owner-new-0001"))
        self.docs.add_request(self.doc, block("owner-new-0002"))
        out = self.pass_once()
        self.assertTrue(any(x["status"] == "ambiguous_new_requests" for x in out))
        self.assertEqual(self.redis.streams, {})
        self.docs.documents[self.doc]["text"] = "History\n" + block("owner-new-0001")
        self.docs.documents[self.doc]["revision_id"] = "r10"
        self.pass_once()
        self.docs.documents[self.doc]["text"] = "History\n" + block("owner-new-0001", "Edited")
        self.pass_once()
        self.assertEqual(len(self.redis.streams["fleet:inbox:claude-api"]), 1)

    def test_worker_filters_unapproved_and_laptop_lane_unchanged(self):
        self.fresh()
        actual = self.redis.streams["fleet:inbox:claude-api"][0][1]
        forged = dict(actual, source_doc_id="1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.redis.streams["fleet:inbox:claude-api"].append(("1700000000000-1", forged))
        inbox = pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis)
        self.assertEqual(len(inbox.read("claude-api", 500)), 1)
        self.redis.streams["fleet:inbox:claude-code-cli"] = [("1700000000000-2", forged)]
        self.assertEqual(len(inbox.read("claude-code-cli", 500)), 1)

    def test_actual_worker_pass_sees_only_fresh_block(self):
        self.fresh()
        calls = []

        def model(system, prompt):
            calls.append(prompt)
            return {"text": "A short answer.", "input_tokens": 10,
                    "output_tokens": 5, "model": "offline-mock", "stop_reason": "end_turn"}

        views = pilot.PilotDocs(self.docs)
        deps = worker.Deps(
            enabled=True, inbox=pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis),
            docs=views, receipts=views, claims=worker.MemoryClaims(),
            ledger=worker.MemoryLedger(), model=model, model_name="offline-mock",
            folder_id=pilot.FOLDER_ID, now=self.now, at="2026-10-11T05:00:00Z",
            cap_cents=100, reserve_cents=1, price_in=1, price_out=1, cad_per_usd=1,
            max_input_tokens=12000, authorized_until=self.now + 3600)
        out = worker.run_once(deps)
        self.assertEqual(out["calls"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("Summarize the new change.", calls[0])
        self.assertNotIn("Architecture notes.", calls[0])
        self.assertIn("SUCCEEDED - task", self.docs.documents[self.doc]["text"])
        self.assertNotEqual("inbox_checkpoint_error", out["status"])

    def test_pilot_scan_passes_unapproved_pages_without_touching_laptop_lane(self):
        self.fresh()
        stream = self.redis.streams["fleet:inbox:claude-api"]
        approved = stream[0]
        unapproved = dict(approved[1], source_doc_id="1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        stream[:0] = [("1699999999999-%d" % i, unapproved) for i in range(501)]
        inbox = pilot.PilotInbox(worker.RedisInbox(self.redis), self.redis)
        self.assertEqual([], inbox.read("claude-api", 500))
        self.assertEqual([approved], inbox.read("claude-api", 500))
        inbox.checkpoint("claude-api", approved[0])
        self.assertIn("fleet:worker:inbox_scan:claude-api", self.redis.values)

    def test_explicit_two_doc_scan_does_not_touch_global_pending_or_poll_cursor(self):
        self.fresh()
        self.assertNotIn("fleet:intake:pending", self.redis.values)
        self.assertNotIn("fleet:intake:poll:last", self.redis.values)
        self.assertEqual(set(), pilot.PilotClaims(self.redis).pending())


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the two-Doc pilot; no Google, Redis or model sockets."""
import importlib.util
import sys
import unittest
from pathlib import Path


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
        self.assertTrue(any(x["status"] == "queued_not_rung" and x.get("admitted") for x in retry))
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

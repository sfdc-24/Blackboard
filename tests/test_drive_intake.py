"""Drive intake (cloud/drive-intake/main.py): one doc, one correlated task, one honest receipt.

The ways this goes wrong, each with a test that fails when the guard is removed:
a receipt that retriggers its own task, the same change arriving twice, a
failed hand-off that can never be retried, "neither worker" happening silently,
a receipt that claims more than the state reached, and a watch channel that
expires on day eight with /healthz still saying ok.

Offline by construction: every outside call is an injected adapter.
"""
import importlib.util
import json
import sys
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
    """A doc store whose read is counted, so a flag-off path can prove it read nothing."""

    def __init__(self, text=REQUEST, revision="r1", folder=FOLDER):
        self.text, self.revision, self.folder = text, revision, folder
        self.reads = 0

    def read(self, file_id):
        self.reads += 1
        return {"text": self.text, "revision_id": self.revision, "folder_id": self.folder}


class Receipts:
    """Append then read back. `lose` makes the append raise AFTER landing; `drop` makes it vanish."""

    def __init__(self, lose=False, drop=False):
        self.lines, self.lose, self.drop = [], lose, drop

    def append(self, file_id, expected_revision, receipt_id, line):
        if not self.drop:
            self.lines.append((file_id, receipt_id, line))
        if self.lose:
            raise ConnectionError("response lost")

    def has(self, file_id, receipt_id):
        return any(f == file_id and r == receipt_id for f, r, _ in self.lines)


class Queue:
    def __init__(self, fail=None):
        self.puts, self.fail = [], fail

    def put(self, tuple_, gist_, at):
        if self.fail:
            raise self.fail
        self.puts.append((dict(tuple_), dict(gist_), at))


def deps(**over):
    base = dict(enabled=True, docs=Docs(), claims=di.MemoryClaims(), queue=Queue(),
                receipts=Receipts(), folder_id=FOLDER, channel_token="tok", poll_token="poll", api_worker="claude-api",
                now=1000.0, at="2026-10-10T21:00:00Z")
    base.update(over)
    return di.Deps(**base)


def hint(file_id=FILE, folder=FOLDER):
    return {"file_id": file_id, "folder_id": folder}


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
        self.assertEqual(set(), d.claims.keys)
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

    def test_the_idempotency_key_is_the_whole_tuple(self):
        t = di.correlate(FILE, REQUEST, "r1", "claude-code-cli")
        for part in ("source_doc_id", "source_revision", "request_id", "task_id", "recipient_id"):
            changed = dict(t)
            changed[part] = changed[part] + "x"
            self.assertNotEqual(di.idempotency_key(t), di.idempotency_key(changed), part)
        # and the Drive write revision is NOT in it - that is the concurrency guard, not identity
        other = dict(t, drive_revision="r999")
        self.assertEqual(di.idempotency_key(t), di.idempotency_key(other))

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


class RetryTest(unittest.TestCase):
    """A failed hand-off releases the claim; a hand-off that landed keeps it."""

    def test_a_failed_queue_put_releases_the_claim(self):
        d = deps(queue=Queue(fail=RuntimeError("redis down")))
        out = di.decide(hint(), d)
        self.assertEqual("queue_error", out["status"])
        self.assertEqual(set(), d.claims.keys)
        self.assertEqual([], d.receipts.lines)      # nothing landed, so nothing is claimed in the doc
        d.queue = Queue()
        self.assertEqual("dispatched", di.decide(hint(), d)["status"])

    def test_an_unconfigured_queue_releases_the_claim_and_names_the_gap(self):
        d = deps(queue=di.InboxQueue(None))
        out = di.decide(hint(), d)
        self.assertEqual("unconfigured", out["status"])
        self.assertIn("fleet:inbox", out["error"])
        self.assertEqual(set(), d.claims.keys)

    def test_a_task_on_the_queue_whose_ring_failed_keeps_its_claim(self):
        def ring(_tuple):
            raise di.Unconfigured("INBOX_WAKE_URL unset")
        d = deps(ring=ring)
        out = di.decide(hint(), d)
        self.assertEqual("queued_not_rung", out["status"])
        self.assertEqual(1, len(d.queue.puts))
        self.assertEqual(1, len(d.claims.keys))         # the task IS on the queue; a sweep rings it
        states = [line.split(" - ")[0] for _, _, line in d.receipts.lines]
        self.assertEqual(["REQUESTED"], states)         # and the doc says exactly that much

    def test_a_claim_store_that_cannot_answer_is_not_a_free_key(self):
        class Broken(di.MemoryClaims):
            def claim(self, key):
                raise ConnectionError("redis unreachable")
        d = deps(claims=Broken())
        self.assertEqual("claim_error", di.decide(hint(), d)["status"])
        self.assertEqual([], d.queue.puts)


class FailoverTest(unittest.TestCase):
    """The owner's rule: one worker or the other, never neither, never silently."""

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

    def test_blocked_writes_a_receipt_the_phone_can_read_and_keeps_the_claim(self):
        d = deps(api_worker="")
        out = di.decide(hint(), d)
        self.assertEqual("blocked", out["status"])
        self.assertEqual([], d.queue.puts)
        self.assertEqual(1, len(d.receipts.lines))
        line = d.receipts.lines[0][2]
        self.assertTrue(line.startswith("BLOCKED - task "), line)
        self.assertIn("no capable worker online", line)
        self.assertEqual("duplicate", di.decide(hint(), d)["status"])   # unchanged doc: not queued twice

    def test_queued_for_the_laptop_when_the_request_needs_the_sf_cli(self):
        d = deps(docs=Docs(text=REQUEST + "Use the sf CLI against the sandbox.\n"), api_worker="claude-api")
        out = di.decide(hint(), d)
        self.assertEqual("queued", out["status"])
        self.assertEqual("claude-code-cli", out["tuple"]["recipient_id"])
        self.assertIn("awaiting the laptop", d.receipts.lines[0][2])

    def test_a_live_heartbeat_routes_to_the_laptop(self):
        d = deps(claims=di.MemoryClaims({"claude-code-cli": 990.0}), api_worker="claude-api")
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"])
        self.assertEqual("claude-code-cli", out["tuple"]["recipient_id"])

    def test_an_addressed_doc_goes_where_it_says(self):
        d = deps(docs=Docs(text="to: gemini\nrequest: G-1\nReview the architecture.\n"))
        out = di.decide(hint(), d)
        self.assertEqual("gemini", out["tuple"]["recipient_id"])
        d = deps(docs=Docs(text="To = Aya\nrelay this.\n"))
        self.assertEqual("chatgpt-codex-desktop", di.decide(hint(), d)["tuple"]["recipient_id"])


class ReceiptTest(unittest.TestCase):
    """Guard 3: the receipt names the state reached and nothing past it."""

    def test_the_receipt_trail_is_requested_then_dispatched_and_never_accepted(self):
        d = deps()
        out = di.decide(hint(), d)
        states = [line.split(" - ")[0] for _, _, line in d.receipts.lines]
        self.assertEqual(["REQUESTED", "DISPATCHED"], states)
        for _, _, line in d.receipts.lines:
            self.assertIn("task " + out["task"], line)
            self.assertIn("by drive-intake", line)
            self.assertNotIn("ACCEPTED", line)
            self.assertNotIn("PICKED UP", line)
        self.assertEqual("written", out["receipt"])
        self.assertEqual("written", out["receipt_dispatched"])

    def test_a_lost_write_that_landed_is_written_and_one_that_vanished_is_unknown(self):
        t = di.correlate(FILE, REQUEST, "r1", "claude-code-cli")
        d = deps(receipts=Receipts(lose=True))
        self.assertEqual("written", di.write_receipt(d, t, "REQUESTED", d.at))
        d = deps(receipts=Receipts(lose=True, drop=True))
        self.assertEqual("unknown:ConnectionError", di.write_receipt(d, t, "REQUESTED", d.at))
        d = deps(receipts=Receipts(drop=True))
        self.assertEqual("not_read_back", di.write_receipt(d, t, "REQUESTED", d.at))

    def test_no_receipt_writer_is_said_not_assumed(self):
        d = deps(receipts=None)
        out = di.decide(hint(), d)
        self.assertEqual("dispatched", out["status"])
        self.assertEqual("no_receipt_writer", out["receipt"])


class ScopeTest(unittest.TestCase):
    def test_another_folder_is_ignored_before_and_after_the_read(self):
        d = deps()
        self.assertEqual("out_of_scope", di.decide(hint(folder="elsewhere"), d)["status"])
        self.assertEqual(0, d.docs.reads)
        d = deps(docs=Docs(folder="elsewhere"))
        self.assertEqual("out_of_scope", di.decide(hint(folder=""), d)["status"])
        self.assertEqual([], d.queue.puts)

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
        self.assertEqual(set(), d.claims.keys)

    def test_the_queue_entry_carries_the_whole_tuple(self):
        d = deps(queue=di.InboxQueue(client=type("C", (), {
            "calls": [], "xadd": lambda self, key, fields: self.calls.append((key, fields))})()))
        out = di.decide(hint(), d)
        key, fields = d.queue.client.calls[0]
        self.assertEqual("fleet:inbox:claude-api", key) if out["tuple"]["recipient_id"] == "claude-api" \
            else self.assertEqual("fleet:inbox:claude-code-cli", key)
        for part in ("source_doc_id", "source_revision", "request_id", "task_id", "recipient_id"):
            self.assertEqual(out["tuple"][part], fields[part], part)
        self.assertEqual("drive-intake", fields["by"])
        self.assertEqual("1", fields["full"])
        self.assertEqual(str(len(" ".join(REQUEST.split()))), fields["chars"])


class ChannelTest(unittest.TestCase):
    """Guard 4: the day-eight outage is announced on day six, and never read as ok by default."""

    def test_no_expiry_on_record_is_unknown_not_ok(self):
        self.assertEqual({"channel": "unknown", "renew": True}, di.channel_health(None, 1000.0))
        self.assertEqual({"channel": "unknown", "renew": True}, di.channel_health("soon", 1000.0))

    def test_near_expiry_is_degraded_and_past_is_expired(self):
        self.assertEqual("ok", di.channel_health(1000.0 + 172801, 1000.0)["channel"])
        self.assertEqual("degraded", di.channel_health(1000.0 + 172800, 1000.0)["channel"])
        self.assertEqual("expired", di.channel_health(1000.0, 1000.0)["channel"])
        self.assertTrue(di.channel_health(1000.0 + 10, 1000.0)["renew"])

    def test_healthz_reports_the_channel_and_the_workers_and_never_the_doc(self):
        d = deps(channel_expires_at=1000.0 + 3600, api_worker="")
        status, out = di.handle("GET", "/healthz", {}, b"", d)
        self.assertEqual(200, status)
        self.assertEqual("degraded", out["channel"])
        self.assertEqual({"claude-code-cli": "offline", "api": "none"}, out["worker"])
        self.assertNotIn("telephone", json.dumps(out))


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

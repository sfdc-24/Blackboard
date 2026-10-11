#!/usr/bin/env python3
"""Inbox wake stays off, wakes once per message id, and routes the four agents.

No network. The wakers are fakes. Run: python3 tests/test_inbox_wake.py
"""
import hashlib
import hmac
import importlib.util
import inspect
import json
import os
import pathlib
import sys
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "inbox_wake", REPO / "cloud" / "inbox-wake" / "main.py")
wake = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wake)


def sign(secret, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + mac


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.deleted = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = (value, ex)
        return True

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)


class Recording:
    def __init__(self):
        self.github = []
        self.jobs = []
        self.http = []

    def github_call(self, workflow, inputs):
        self.github.append((workflow, inputs))

    def job_call(self, job):
        self.jobs.append(job)

    def http_call(self, url, body):
        self.http.append((url, body))


def deps(enabled=True, seen=None, recording=None, reader=None, secret="hook", sweep="sweep"):
    recording = recording or Recording()
    return wake.Deps(
        enabled=enabled,
        seen=seen if seen is not None else wake.MemorySeen(),
        reader=reader,
        webhook_secret=secret,
        sweep_token=sweep,
        wakers=wake.Wakers(
            github_token="token" if enabled else "",
            grok_url="https://grok.example/wake" if enabled else "",
            github=recording.github_call,
            run_job=recording.job_call,
            http_post=recording.http_call,
        ),
    ), recording


class FlagDefaultsOff(unittest.TestCase):
    def test_only_the_exact_string_on_enables(self):
        for value in ("", "off", "ON", "true", "1", "yes"):
            self.assertFalse(wake.enabled_from_env({"INBOX_WAKE_ENABLED": value}), value)
        self.assertFalse(wake.enabled_from_env({}))
        self.assertTrue(wake.enabled_from_env({"INBOX_WAKE_ENABLED": "on"}))

    def test_a_disabled_wake_does_not_call_anyone(self):
        d, rec = deps(enabled=False, seen=wake.MemorySeen())
        body = json.dumps({"id": "m-1", "to": "grok", "text": "ping"}).encode()
        status, payload = wake.handle("POST", "/wake", {}, body, d)
        self.assertEqual(200, status)
        self.assertFalse(payload["enabled"])
        self.assertEqual([], payload["woke"])
        self.assertEqual([], rec.http)
        self.assertEqual(set(), d.seen.ids)

    def test_a_disabled_sweep_does_not_read(self):
        def reader(agent):
            raise AssertionError("reader called while disabled")
        d, rec = deps(enabled=False, reader=reader)
        status, payload = wake.handle(
            "POST", "/sweep", {"authorization": "Bearer sweep"}, b"{}", d)
        self.assertEqual(200, status)
        self.assertEqual([], payload["woke"])
        self.assertEqual([], rec.jobs)


class RoutesAndIdempotency(unittest.TestCase):
    def test_each_agent_gets_its_route_once(self):
        d, rec = deps()
        pairs = [
            ("claude-code-cli", "github_actions", "inbox-wake-claude.yml"),
            ("aya", "github_actions", "inbox-wake-codex.yml"),
            ("gemini", "cloud_run_job", "gemini-waker"),
            ("grok-bot", "http", "GROK_WAKE_URL"),
        ]
        for i, (who, kind, target) in enumerate(pairs):
            got = wake.decide({"id": "msg-%d" % i, "to": who, "by": "cursor", "text": "hello"}, d)
            self.assertEqual("woke", got["status"], got)
            self.assertEqual(kind, got["route"])
            self.assertEqual(target, got["target"])
        self.assertEqual(["inbox-wake-claude.yml", "inbox-wake-codex.yml"],
                         [item[0] for item in rec.github])
        self.assertNotIn("text", rec.github[0][1])
        self.assertEqual(["gemini-waker"], rec.jobs)
        self.assertEqual("hello", rec.http[0][1]["text"])
        self.assertEqual("https://grok.example/wake", rec.http[0][0])

    def test_the_same_id_does_not_wake_twice_and_two_inboxes_do(self):
        d, rec = deps()
        first = wake.decide({"id": "1-0", "to": "grok", "text": "a"}, d)
        second = wake.decide({"id": "1-0", "to": "grok", "text": "a"}, d)
        other = wake.decide({"id": "1-0", "to": "gemini", "text": "a"}, d)
        self.assertEqual("woke", first["status"])
        self.assertEqual("duplicate", second["status"])
        self.assertFalse(second["woke"])
        self.assertEqual("woke", other["status"])
        self.assertEqual(1, len(rec.http))
        self.assertEqual(1, len(rec.jobs))

    def test_redis_set_nx_is_the_claim(self):
        client = FakeRedis()
        seen = wake.RedisSeen(client)
        self.assertTrue(seen.claim("grok:1-0"))
        self.assertFalse(seen.claim("grok:1-0"))
        self.assertEqual(2592000, client.store["fleet:wake:seen:grok:1-0"][1])
        seen.release("grok:1-0")
        self.assertNotIn("fleet:wake:seen:grok:1-0", client.store)
        self.assertTrue(seen.claim("grok:1-0"))

    def test_an_unconfigured_grok_releases_the_id(self):
        d, rec = deps()
        d.wakers.grok_url = ""
        first = wake.decide({"id": "g-1", "to": "grok", "text": "ping"}, d)
        self.assertEqual("unconfigured", first["status"])
        self.assertFalse(first["woke"])
        self.assertEqual([], rec.http)
        d.wakers.grok_url = "https://grok.example/wake"
        second = wake.decide({"id": "g-1", "to": "grok", "text": "ping"}, d)
        self.assertEqual("woke", second["status"])

    def test_unknown_and_bad_ids_do_not_wake(self):
        d, rec = deps()
        self.assertEqual("unknown_agent", wake.decide({"id": "x", "to": "nobody"}, d)["status"])
        self.assertEqual("bad_id", wake.decide({"id": "has space", "to": "grok"}, d)["status"])
        self.assertEqual([], rec.http)
        self.assertEqual(set(), d.seen.ids)

    def test_no_store_does_not_wake(self):
        d, rec = deps(seen=None)
        d.seen = None
        got = wake.decide({"id": "m-9", "to": "gemini"}, d)
        self.assertEqual("no_store", got["status"])
        self.assertEqual([], rec.jobs)


class SweepAndWebhook(unittest.TestCase):
    def test_sweep_wakes_only_what_is_new(self):
        def reader(agent):
            if agent == "broken":
                raise OSError("down")
            if agent == "grok":
                return [{"id": "1-0", "text": "ping", "by": "claude-code-cli"}]
            if agent == "gemini":
                raise RuntimeError("redis")
            return []
        d, rec = deps(reader=reader)
        first = wake.sweep(d)
        second = wake.sweep(d)
        woke = [item for item in first if item["status"] == "woke"]
        errors = [item for item in first if item["status"] == "read_error"]
        self.assertEqual(["grok"], [item["to"] for item in woke])
        self.assertEqual(["gemini"], [item["to"] for item in errors])
        self.assertEqual("duplicate", [item for item in second if item.get("id") == "1-0"][0]["status"])
        self.assertEqual(1, len(rec.http))

    def test_webhook_checks_the_signature_and_a_comment_line(self):
        d, rec = deps()
        body = json.dumps({
            "action": "created",
            "comment": {"body": "inbox to=claude-code-cli id=row-7 by=grok text=are you there"},
        }).encode()
        status, payload = wake.handle("POST", "/wake", {
            "X-GitHub-Event": "issue_comment",
            "X-Hub-Signature-256": "sha256=dead",
        }, body, d)
        self.assertEqual(401, status)
        self.assertEqual([], rec.github)
        status, payload = wake.handle("POST", "/wake", {
            "X-GitHub-Event": "issue_comment",
            "X-Hub-Signature-256": sign("hook", body),
        }, body, d)
        self.assertEqual(200, status)
        self.assertEqual("woke", payload["woke"][0]["status"])
        workflow, inputs = rec.github[0]
        self.assertEqual("inbox-wake-claude.yml", workflow)
        self.assertEqual({"message_id": "row-7", "by": "grok"}, inputs)

    def test_sweep_endpoint_requires_the_bearer(self):
        d, _rec = deps(reader=lambda agent: [])
        status, payload = wake.handle("POST", "/sweep", {}, b"{}", d)
        self.assertEqual(401, status)
        status, payload = wake.handle(
            "POST", "/sweep", {"Authorization": "Bearer sweep"}, b"{}", d)
        self.assertEqual(200, status)
        self.assertTrue(payload["enabled"])

    def test_healthz_reports_the_flag_and_does_not_wake(self):
        d, rec = deps(enabled=False)
        status, payload = wake.handle("GET", "/healthz", {}, b"", d)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["enabled"])
        self.assertEqual([], rec.http)


class DefinitionStaysOff(unittest.TestCase):
    def test_image_and_workflows_default_off_and_hold_no_secret(self):
        docker = (REPO / "cloud" / "inbox-wake" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ENV INBOX_WAKE_ENABLED=off", docker)
        build = (REPO / "cloud" / "inbox-wake" / "cloudbuild.yaml").read_text(encoding="utf-8")
        self.assertIn("inbox-wake:unreleased", build)
        self.assertNotIn("REDIS_AUTH", build)
        ignore = (REPO / ".gcloudignore").read_text(encoding="utf-8")
        self.assertIn("!/cloud/inbox-wake", ignore)
        for name in ("inbox-wake-sweep.yml", "inbox-wake-claude.yml", "inbox-wake-codex.yml"):
            text = (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertIn("vars.INBOX_WAKE_ENABLED == 'on'", text)
            for needle in ("BEGIN PRIVATE KEY", "AIza", "REDIS_AUTH"):
                self.assertNotIn(needle, text, name)
        sweep = (REPO / ".github" / "workflows" / "inbox-wake-sweep.yml").read_text(encoding="utf-8")
        self.assertIn("*/15 * * * *", sweep)


DOC = "1pKWJYbJzMqxSA9Vy_PSR93BU2OAAgP-N3WBEkCj7oj4"
REV = "ab" * 32
TASK = "task-0123456789abcdef"
REQ = "OT-20261010"
STAMP = "2026-10-10T22:12:00Z"


def entries(client):
    """Responder entries only. The claim key is the lock, not the entry."""
    found = {}
    for key, (value, _ex) in client.store.items():
        if key.startswith(wake.RECEIPT_ENTRY_PREFIX):
            found[key] = json.loads(value)
    return found


def drive_key(doc=DOC, rev=REV, req=REQ, task=TASK):
    """cloud/drive-intake idempotency_key: doc, request revision, request id, task id."""
    return hashlib.sha256("\x1f".join((doc, rev, req, task)).encode("utf-8")).hexdigest()


def receipt_deps(client, clock=STAMP):
    d, rec = deps(seen=wake.RedisSeen(client))
    d.receipts = wake.RedisReceipts(client)
    d.clock = lambda: clock
    return d, rec


class FlagOffWritesNothing(unittest.TestCase):
    def test_only_the_exact_string_on_enables_receipts(self):
        for value in ("", "off", "ON", "true", "1", "yes", " on"):
            self.assertFalse(wake.receipts_enabled({"INBOX_WAKE_RECEIPTS": value}), value)
        self.assertFalse(wake.receipts_enabled({}))
        self.assertTrue(wake.receipts_enabled({"INBOX_WAKE_RECEIPTS": "on"}))

    def test_the_image_defaults_the_receipt_flag_off(self):
        docker = (REPO / "cloud" / "inbox-wake" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ENV INBOX_WAKE_RECEIPTS=off", docker)

    def test_wake_off_does_not_attach_a_writer_even_if_receipts_are_requested(self):
        saved = os.environ.get("INBOX_WAKE_ENABLED"), os.environ.get("INBOX_WAKE_RECEIPTS")
        os.environ.pop("INBOX_WAKE_ENABLED", None)
        os.environ["INBOX_WAKE_RECEIPTS"] = "on"
        try:
            built = wake.deps_from_env()
        finally:
            for name, value in zip(("INBOX_WAKE_ENABLED", "INBOX_WAKE_RECEIPTS"), saved):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertFalse(built.enabled)
        self.assertIsNone(built.seen)
        self.assertIsNone(built.receipts)

    def test_flag_off_writes_no_responder_entry(self):
        client = FakeRedis()
        saved = os.environ.get("INBOX_WAKE_RECEIPTS")
        os.environ["INBOX_WAKE_RECEIPTS"] = "off"
        try:
            d, rec = deps()
            wake.attach_store(d, client)
        finally:
            if saved is None:
                os.environ.pop("INBOX_WAKE_RECEIPTS", None)
            else:
                os.environ["INBOX_WAKE_RECEIPTS"] = saved
        self.assertIsNone(d.receipts)
        got = wake.decide({"id": "m-1", "to": "grok", "text": "ping"}, d)
        self.assertEqual("woke", got["status"])
        self.assertTrue(got["woke"])
        self.assertNotIn("receipt", got)
        self.assertEqual(["fleet:wake:seen:grok:m-1"], list(client.store))
        self.assertEqual({}, entries(client))
        self.assertEqual(1, len(rec.http))


class PickupReceipt(unittest.TestCase):
    def test_first_pickup_writes_one_received_entry_with_the_whole_tuple(self):
        client = FakeRedis()
        d, rec = receipt_deps(client)
        message = {
            "id": "1-0", "to": "grok-bot", "by": "drive-intake", "text": "fix the telephone",
            "source_doc_id": DOC, "source_revision": REV,
            "request_id": REQ, "task_id": TASK, "recipient_id": "gemini",
        }
        got = wake.decide(message, d)
        self.assertEqual("woke", got["status"])
        self.assertEqual("written", got["receipt"])
        self.assertEqual(1, len(rec.http))
        found = entries(client)
        self.assertEqual(1, len(found))
        key, item = next(iter(found.items()))
        self.assertEqual(wake.RECEIPT_ENTRY_PREFIX + drive_key(), key)
        self.assertEqual("1-0", item["message_id"])
        self.assertEqual(STAMP, item["received_at"])
        self.assertRegex(item["received_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual("grok", item["recipient_id"])
        self.assertEqual("RECEIVED", item["state"])
        self.assertEqual(DOC, item["source_doc_id"])
        self.assertEqual(REV, item["request_revision"])
        self.assertEqual(REV, item["source_revision"])
        self.assertEqual(REQ, item["request_id"])
        self.assertEqual(TASK, item["task_id"])
        corr = item["correlation"]
        self.assertEqual(DOC, corr["source_doc_id"])
        self.assertEqual(REV, corr["request_revision"])
        self.assertEqual(REV, corr["source_revision"])
        self.assertEqual(REQ, corr["request_id"])
        self.assertEqual(TASK, corr["task_id"])
        self.assertEqual("grok", corr["recipient_id"])
        self.assertEqual(drive_key(), item["idempotency_key"])
        self.assertNotIn("text", item)
        self.assertLessEqual(len(json.dumps(item)), 2048)
        self.assertEqual(2592000, client.store[key][1])

    def test_the_drive_key_ignores_recipient_and_message_id(self):
        one = wake.idempotency_key(
            {"source_doc_id": DOC, "request_revision": REV, "request_id": REQ,
             "task_id": TASK, "recipient_id": "grok"}, "1-0")
        two = wake.idempotency_key(
            {"source_doc_id": DOC, "source_revision": REV, "request_id": REQ,
             "task_id": TASK, "recipient_id": "gemini"}, "9-9")
        self.assertEqual(drive_key(), one)
        self.assertEqual(one, two)
        changed = wake.idempotency_key(
            {"source_doc_id": DOC, "request_revision": REV, "request_id": REQ,
             "task_id": TASK + "ff", "recipient_id": "grok"}, "1-0")
        self.assertNotEqual(one, changed)

    def test_a_board_row_keeps_an_empty_tuple_and_a_repeat_writes_nothing(self):
        client = FakeRedis()
        d, rec = receipt_deps(client)
        body = json.dumps({
            "action": "created",
            "comment": {"body": "inbox to=claude-code-cli id=row-7 by=grok text=are you there"},
        }).encode()
        headers = {
            "X-GitHub-Event": "issue_comment",
            "X-Hub-Signature-256": sign("hook", body),
        }
        status, payload = wake.handle("POST", "/wake", headers, body, d)
        self.assertEqual(200, status)
        self.assertEqual("woke", payload["woke"][0]["status"])
        found = entries(client)
        self.assertEqual(1, len(found))
        item = next(iter(found.values()))
        self.assertEqual("row-7", item["message_id"])
        self.assertEqual(STAMP, item["received_at"])
        self.assertEqual("claude-code-cli", item["recipient_id"])
        self.assertEqual("RECEIVED", item["state"])
        for field in ("source_doc_id", "request_revision", "source_revision", "request_id", "task_id"):
            self.assertEqual("", item[field])
            self.assertEqual("", item["correlation"][field])
        self.assertEqual("claude-code-cli", item["correlation"]["recipient_id"])
        self.assertEqual(item["idempotency_key"], wake.idempotency_key(item["correlation"], "row-7"))
        before = dict(client.store)
        status, payload = wake.handle("POST", "/wake", headers, body, d)
        self.assertEqual(200, status)
        self.assertEqual("duplicate", payload["woke"][0]["status"])
        self.assertFalse(payload["woke"][0]["woke"])
        self.assertEqual(before, client.store)
        self.assertEqual(1, len(entries(client)))
        self.assertEqual(1, len(rec.github))

    def test_two_board_messages_are_two_entries(self):
        client = FakeRedis()
        d, _rec = receipt_deps(client)
        wake.decide({"id": "row-1", "to": "grok", "text": "one"}, d)
        wake.decide({"id": "row-2", "to": "grok", "text": "two"}, d)
        self.assertEqual(2, len(entries(client)))

    def test_the_same_drive_request_under_a_new_stream_id_does_not_write_a_second_entry(self):
        client = FakeRedis()
        d, _rec = receipt_deps(client)
        base = {"to": "grok", "text": "x", "source_doc_id": DOC, "source_revision": REV,
                "request_id": REQ, "task_id": TASK}
        self.assertEqual("woke", wake.decide(dict(base, id="1-0"), d)["status"])
        again = wake.decide(dict(base, id="2-0"), d)
        self.assertEqual("duplicate", again["receipt"])
        self.assertEqual(1, len(entries(client)))

    def test_the_entry_is_one_set_nx_with_a_ttl_and_a_repeat_does_not_write(self):
        class Order(FakeRedis):
            def __init__(self):
                super().__init__()
                self.ops = []

            def get(self, key):
                raise AssertionError("check-then-set read %s" % key)

            def exists(self, key):
                raise AssertionError("check-then-set exists %s" % key)

            def set(self, key, value, nx=False, ex=None):
                self.ops.append((key, bool(nx), ex, value))
                return super().set(key, value, nx=nx, ex=ex)

        client = Order()
        receipts = wake.RedisReceipts(client)
        entry = wake.receipt_entry({"id": "m-1", "to": "grok"}, "grok", STAMP)
        self.assertEqual("written", receipts.record(entry))
        self.assertEqual(1, len(client.ops))
        key, nx, ttl, payload = client.ops[0]
        self.assertTrue(key.startswith(wake.RECEIPT_ENTRY_PREFIX))
        self.assertTrue(nx)
        self.assertEqual(wake.SEEN_TTL, ttl)
        self.assertEqual("RECEIVED", json.loads(payload)["state"])
        before = dict(client.store)
        self.assertEqual("duplicate", receipts.record(entry))
        self.assertEqual(before, client.store)
        self.assertEqual(2, len(client.ops))
        self.assertTrue(client.ops[1][1])
        self.assertEqual(1, len(entries(client)))

    def test_sweep_copies_the_inbox_tuple_onto_the_receipt(self):
        class Stream:
            def xrange(self, key, count=None):
                self.key = key
                return [(b"1-0", {
                    b"text": b"hello", b"by": b"drive-intake", b"at": STAMP.encode(),
                    b"source_doc_id": DOC.encode(), b"source_revision": REV.encode(),
                    b"request_id": REQ.encode(), b"task_id": TASK.encode(),
                    b"recipient_id": b"gemini",
                })]

        stream = Stream()
        rows = wake.read_inbox(stream, "grok")
        self.assertEqual("fleet:inbox:grok", stream.key)
        self.assertEqual(DOC, rows[0]["source_doc_id"])
        self.assertEqual(REV, rows[0]["source_revision"])
        client = FakeRedis()
        d, rec = receipt_deps(client)

        def reader(agent):
            return rows if agent == "grok" else []

        d.reader = reader
        out = wake.sweep(d)
        woke = [item for item in out if item.get("status") == "woke"]
        self.assertEqual(["grok"], [item["to"] for item in woke])
        item = next(iter(entries(client).values()))
        self.assertEqual("1-0", item["message_id"])
        self.assertEqual(drive_key(), item["idempotency_key"])
        self.assertEqual("grok", item["recipient_id"])
        self.assertEqual(1, len(rec.http))
        wake.sweep(d)
        self.assertEqual(1, len(entries(client)))


class ReceiptWriteFailure(unittest.TestCase):
    def test_a_redis_exception_is_an_error_and_does_not_wake(self):
        class EntryWriteFails(FakeRedis):
            def set(self, key, value, nx=False, ex=None):
                if key.startswith(wake.RECEIPT_ENTRY_PREFIX):
                    raise OSError("redis down")
                return super().set(key, value, nx=nx, ex=ex)

        client = EntryWriteFails()
        d, rec = receipt_deps(client)
        with self.assertLogs("inbox_wake", level="ERROR") as caught:
            got = wake.decide({"id": "m-1", "to": "grok", "text": "ACCEPTED please"}, d)
        self.assertEqual("error", got["status"])
        self.assertFalse(got["woke"])
        self.assertNotEqual("woke", got["status"])
        self.assertNotEqual("success", got["status"])
        self.assertEqual([], rec.http)
        self.assertEqual({}, entries(client))
        self.assertNotIn("fleet:wake:seen:grok:m-1", client.store)
        self.assertTrue(any("error" in line and "m-1" in line for line in caught.output))
        self.assertFalse(any("ACCEPTED" in line for line in caught.output))

    def test_a_rejected_write_is_an_error_and_does_not_wake(self):
        class EntryWriteRejected(FakeRedis):
            def set(self, key, value, nx=False, ex=None):
                if key.startswith(wake.RECEIPT_ENTRY_PREFIX):
                    return False
                return super().set(key, value, nx=nx, ex=ex)

        client = EntryWriteRejected()
        d, rec = receipt_deps(client)
        with self.assertLogs("inbox_wake", level="ERROR") as caught:
            got = wake.decide({"id": "m-2", "to": "grok", "text": "ping"}, d)
        self.assertEqual("error", got["status"])
        self.assertFalse(got["woke"])
        self.assertEqual([], rec.http)
        self.assertEqual({}, entries(client))
        self.assertTrue(any("error" in line for line in caught.output))

    def test_a_redis_outage_is_a_fast_reported_error(self):
        class Outage(FakeRedis):
            def set(self, key, value, nx=False, ex=None):
                raise TimeoutError("redis outage")

        client = Outage()
        d, rec = receipt_deps(client)
        started = time.monotonic()
        with self.assertLogs("inbox_wake", level="ERROR") as caught:
            got = wake.decide({"id": "m-3", "to": "gemini", "text": "ping"}, d)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual("error", got["status"])
        self.assertFalse(got["woke"])
        self.assertNotEqual("success", got["status"])
        self.assertEqual([], rec.jobs)
        self.assertEqual({}, entries(client))
        self.assertTrue(any("error" in line and "m-3" in line for line in caught.output))
        self.assertLessEqual(wake.REDIS_CONNECT_TIMEOUT, 2.0)
        self.assertLessEqual(wake.REDIS_READ_TIMEOUT, 2.0)
        self.assertLess(wake.REDIS_CONNECT_TIMEOUT, 10)
        self.assertLess(wake.REDIS_READ_TIMEOUT, 10)


class ConcurrentPickup(unittest.TestCase):
    def test_two_concurrent_pickups_produce_exactly_one_entry(self):
        # Two is the required case. Three is the service's instance cap.
        for n in (2, 3):
            with self.subTest(instances=n):
                self.assertEqual(1, self._pickups(n))

    def _pickups(self, n):
        client = AtomicRedis()
        d, _rec = receipt_deps(client)
        barrier = threading.Barrier(n)
        results = []
        gate = threading.Lock()

        def pickup():
            barrier.wait()
            got = wake.decide({"id": "m-race", "to": "grok", "text": "ping"}, d)
            with gate:
                results.append(got)

        threads = [threading.Thread(target=pickup) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(n, len(results))
        found = entries(client)
        self.assertEqual(1, len(found))
        item = next(iter(found.values()))
        self.assertEqual("RECEIVED", item["state"])
        self.assertNotEqual("ACCEPTED", item["state"])
        receipt_sets = [op for op in client.commands
                        if op[0] == "set" and str(op[1]).startswith(wake.RECEIPT_ENTRY_PREFIX)]
        self.assertGreaterEqual(len(receipt_sets), 1)
        self.assertTrue(all(op[2] and op[3] == wake.SEEN_TTL for op in receipt_sets))
        self.assertEqual(1, sum(1 for op in receipt_sets if op[4]))
        self.assertFalse(any(op[0] in ("get", "exists") for op in client.commands))
        self.assertEqual(1, sum(1 for got in results if got["status"] == "woke"))
        return len(found)


class AtomicRedis(FakeRedis):
    """SET NX is atomic, the way Redis is. A get and a later set are not one command."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self.commands = []

    def get(self, key):
        with self._lock:
            self.commands.append(("get", key, False, None, False))
            item = self.store.get(key)
        return None if item is None else item[0]

    def exists(self, key):
        with self._lock:
            self.commands.append(("exists", key, False, None, False))
            return key in self.store

    def set(self, key, value, nx=False, ex=None):
        with self._lock:
            wrote = not (nx and key in self.store)
            self.commands.append(("set", key, bool(nx), ex, wrote))
            if not wrote:
                return None
            self.store[key] = (value, ex)
            return True

    def delete(self, key):
        with self._lock:
            self.commands.append(("delete", key, False, None, True))
            self.deleted.append(key)
            self.store.pop(key, None)


class PrivateRangeAndTimeouts(unittest.TestCase):
    def test_only_private_ranges_only_destinations_count_as_reachable(self):
        for host in ("10.1.2.3", "172.16.5.5", "192.168.0.1", "100.64.0.1"):
            self.assertTrue(wake.host_on_private_range(host), host)
        for host in ("8.8.8.8", "1.2.3.4", "169.254.1.1", "redis.internal", "", "  "):
            self.assertFalse(wake.host_on_private_range(host), host)

    def test_timeouts_replace_the_long_defaults_on_the_client(self):
        class Pool:
            def __init__(self):
                self.connection_kwargs = {
                    "socket_connect_timeout": 10,
                    "socket_timeout": 10,
                }

        class Client:
            def __init__(self):
                self.connection_pool = Pool()

        client = Client()
        wake.apply_redis_timeouts(client)
        self.assertEqual(wake.REDIS_CONNECT_TIMEOUT, client.connection_pool.connection_kwargs["socket_connect_timeout"])
        self.assertEqual(wake.REDIS_READ_TIMEOUT, client.connection_pool.connection_kwargs["socket_timeout"])
        self.assertLessEqual(wake.REDIS_CONNECT_TIMEOUT, 2.0)
        self.assertLessEqual(wake.REDIS_READ_TIMEOUT, 2.0)


class NeverAccepted(unittest.TestCase):
    def test_the_service_never_emits_accepted(self):
        self.assertEqual((wake.RECEIVED, wake.DISPATCHED), wake.RECEIPT_STATES)
        self.assertNotIn("ACCEPTED", wake.RECEIPT_STATES)
        client = FakeRedis()
        receipts = wake.RedisReceipts(client)
        refused = wake.receipt_entry({"id": "m-1", "text": "ACCEPTED"}, "grok", STAMP)
        refused["state"] = "ACCEPTED"
        with self.assertLogs("inbox_wake", level="ERROR"):
            self.assertEqual("error", receipts.record(refused))
        self.assertEqual({}, client.store)
        d, _rec = receipt_deps(client)
        got = wake.decide({"id": "m-2", "to": "grok", "text": "status ACCEPTED"}, d)
        self.assertEqual("woke", got["status"])
        self.assertEqual("RECEIVED", got["receipt"] and next(iter(entries(client).values()))["state"])
        blob = json.dumps({key: value[0] for key, value in client.store.items()})
        self.assertNotIn("ACCEPTED", blob)
        for item in entries(client).values():
            self.assertIn(item["state"], ("RECEIVED", "DISPATCHED"))
            self.assertNotEqual("ACCEPTED", item["state"])
        source = (REPO / "cloud" / "inbox-wake" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn('"ACCEPTED"', source)
        self.assertNotIn("'ACCEPTED'", source)


class BoardRedis(FakeRedis):
    """SET NX under a lock, plus streams. get/exists are refused on purpose."""

    def __init__(self, fail_on=""):
        super().__init__()
        self.streams = {}
        self.lock = threading.Lock()
        self.fail_on = fail_on
        self.seq = 0

    def get(self, *args, **kwargs):
        raise AssertionError("check-then-set")

    def exists(self, *args, **kwargs):
        raise AssertionError("check-then-set")

    def set(self, key, value, nx=False, ex=None):
        with self.lock:
            if self.fail_on == "set":
                raise TimeoutError("redis")
            return super().set(key, value, nx=nx, ex=ex)

    def xadd(self, key, fields, **kwargs):
        with self.lock:
            if self.fail_on == "xadd":
                raise TimeoutError("redis")
            self.seq += 1
            entry_id = "%d-0" % self.seq
            self.streams.setdefault(key, []).append((entry_id, dict(fields)))
            return entry_id

    def xrange(self, key, min="-", max="+", count=None):
        with self.lock:
            if self.fail_on == "xrange":
                raise TimeoutError("redis")
            rows = list(self.streams.get(key, []))
            if min not in ("-", None) and max == min:
                rows = [row for row in rows if row[0] == min]
            return rows


def board_row(row_id="ROW-1", to="grok", phase="APPEND", payload=None):
    if payload is None:
        payload = "BCB|v=1|id=MSG1|phase=APPEND|from=claude-code-cli|to=%s|hello" % to
    return [row_id, "2026-10-11T03:00:00Z", "claude-code-cli", to, phase,
            payload, "OPEN", "Blackboard", "gist", ""]


def board_deps(client, rows=None, mirror=True, secret="board-hook"):
    rows = {} if rows is None else rows

    def fetch(row_id, rows=rows):
        if row_id not in rows:
            raise LookupError("row")
        return rows[row_id]

    d, rec = deps(secret="hook", sweep="sweep")
    d.board_mirror = mirror
    d.board_secret = secret
    d.board = wake.RedisBoard(client) if mirror else None
    d.board_fetch = fetch
    d.board_window = None
    return d, rec


def signed_event(row_id="ROW-1", secret="board-hook", **extra):
    event = {
        "row_id": row_id, "sheet_row": 12, "appended_at": "lied",
        "from": "lied", "to": "owner", "phase": "lied", "id": "lied",
        "sha256": "deadbeef",
    }
    event.update(extra)
    body = json.dumps(event).encode("utf-8")
    return body, {"X-Board-Signature-256": sign(secret, body)}


class BoardMirrorFlag(unittest.TestCase):
    def test_only_the_exact_string_on_enables_the_mirror(self):
        for value in ("", "off", "ON", "true", "1", "yes", " on"):
            self.assertFalse(wake.board_mirror_enabled({"INBOX_WAKE_BOARD_MIRROR": value}), value)
        self.assertFalse(wake.board_mirror_enabled({}))
        self.assertTrue(wake.board_mirror_enabled({"INBOX_WAKE_BOARD_MIRROR": "on"}))

    def test_the_image_defaults_the_mirror_off(self):
        docker = (REPO / "cloud" / "inbox-wake" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ENV INBOX_WAKE_BOARD_MIRROR=off", docker)
        self.assertIn("COPY scripts/bus.py /app/bus.py", docker)

    def test_flag_off_is_a_no_op(self):
        client = BoardRedis()
        called = {"n": 0}

        def fetch(row_id):
            called["n"] += 1
            raise AssertionError("fetch while disabled")

        d, _rec = board_deps(client, mirror=False)
        d.board_fetch = fetch
        body, headers = signed_event()
        status, payload = wake.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status)
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["mirrored"])
        self.assertEqual(0, called["n"])
        self.assertEqual({}, client.store)
        self.assertEqual({}, client.streams)
        # Wake itself is unchanged: still disabled when its own flag is off.
        d.enabled = False
        wake_body = json.dumps({"id": "m-1", "to": "grok", "text": "ping"}).encode()
        status, payload = wake.handle("POST", "/wake", {
            "X-Hub-Signature-256": sign("hook", wake_body),
        }, wake_body, d)
        self.assertEqual(200, status)
        self.assertFalse(payload["enabled"])
        self.assertEqual([], payload["woke"])

    def test_unsigned_and_mismatched_are_rejected(self):
        client = BoardRedis()
        row = board_row()
        d, _rec = board_deps(client, {"ROW-1": row})
        body = signed_event()[0]
        status, payload = wake.handle("POST", "/hooks/board", {}, body, d)
        self.assertEqual(401, status)
        status, payload = wake.handle("POST", "/hooks/board", {
            "X-Board-Signature-256": "sha256=dead",
        }, body, d)
        self.assertEqual(401, status)
        self.assertEqual({}, client.streams)
        self.assertEqual({}, client.store)


class BoardMirrorWrite(unittest.TestCase):
    def test_an_append_row_is_mirrored_from_the_board_not_the_event(self):
        client = BoardRedis()
        payload = "BCB|v=1|id=MSG1|phase=APPEND|hello"
        row = board_row(payload=payload, to="grok", phase="APPEND")
        d, _rec = board_deps(client, {"ROW-1": row})
        body, headers = signed_event()
        with self.assertLogs("inbox_wake", level="INFO") as logs:
            status, got = wake.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status)
        self.assertTrue(got["ok"])
        self.assertEqual("mirrored", got["status"])
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.assertEqual(digest, got["sha256"])
        self.assertNotEqual("deadbeef", got["sha256"])
        self.assertTrue(got["stream_id"])
        board_entries = client.streams[wake.BOARD_STREAM]
        self.assertEqual(1, len(board_entries))
        stored = board_entries[0][1]
        self.assertEqual("APPEND", stored["phase"])
        self.assertEqual("grok", stored["to"])
        self.assertEqual("MSG1", stored["id"])
        self.assertEqual(digest, stored["sha256"])
        self.assertEqual(got["stream_id"], board_entries[0][0])
        self.assertEqual(["fleet:inbox:grok"], [item["key"] for item in got["inboxes"]])
        self.assertEqual(digest, got["inboxes"][0]["sha256"])
        self.assertEqual(1, len(client.streams["fleet:inbox:grok"]))
        self.assertTrue(any(got["stream_id"] in line for line in logs.output))
        # The claim is the SET NX marker. No second write of the same row.
        self.assertIn(wake.MIRROR_PREFIX + "ROW-1", client.store)

    def test_to_all_fans_out_once(self):
        client = BoardRedis()
        row = board_row(to="ALL", phase="APPEND")
        d, _rec = board_deps(client, {"ROW-1": row})
        body, headers = signed_event(to="grok")
        status, got = wake.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status, got)
        self.assertEqual(list(wake.BOARD_AGENTS), [item["key"][len("fleet:inbox:"):] for item in got["inboxes"]])
        self.assertEqual(1, len(client.streams[wake.BOARD_STREAM]))
        for agent in wake.BOARD_AGENTS:
            self.assertEqual(1, len(client.streams["fleet:inbox:" + agent]), agent)

    def test_three_concurrent_events_produce_one_stream_entry(self):
        client = BoardRedis()
        row = board_row()
        d, _rec = board_deps(client, {"ROW-1": row})
        body, headers = signed_event()
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def go():
            try:
                barrier.wait(timeout=5)
                results.append(wake.handle("POST", "/hooks/board", headers, body, d))
            except Exception as exc:  # noqa: BLE001 - the assertion is the result list
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual([], errors)
        self.assertEqual(3, len(results))
        statuses = sorted(item[1]["status"] for item in results)
        self.assertEqual(["duplicate", "duplicate", "mirrored"], statuses)
        self.assertEqual(1, len(client.streams[wake.BOARD_STREAM]))
        self.assertEqual(1, len(client.streams["fleet:inbox:grok"]))
        self.assertTrue(all(item[0] == 200 for item in results))

    def test_a_redis_timeout_is_an_error_and_returns_fast(self):
        client = BoardRedis(fail_on="set")
        row = board_row()
        d, _rec = board_deps(client, {"ROW-1": row})
        body, headers = signed_event()
        started = time.monotonic()
        status, got = wake.handle("POST", "/hooks/board", headers, body, d)
        elapsed = time.monotonic() - started
        self.assertEqual(503, status)
        self.assertFalse(got["ok"])
        self.assertEqual("error", got["status"])
        self.assertLess(elapsed, 0.5)
        self.assertEqual({}, client.streams)

    def test_a_readback_mismatch_is_not_success(self):
        class Liar(BoardRedis):
            def xrange(self, key, min="-", max="+", count=None):
                rows = super().xrange(key, min=min, max=max, count=count)
                lied = []
                for entry_id, fields in rows:
                    fields = dict(fields)
                    fields["sha256"] = "0" * 64
                    lied.append((entry_id, fields))
                return lied

        client = Liar()
        row = board_row()
        d, _rec = board_deps(client, {"ROW-1": row})
        body, headers = signed_event()
        status, got = wake.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(503, status)
        self.assertFalse(got["ok"])
        self.assertEqual("error", got["status"])
        self.assertNotEqual(True, got.get("mirrored"))

    def test_fetch_uses_the_row_id_and_ignores_a_substring_hit(self):
        good = board_row("ROW-1")
        quoted = board_row("OTHER", payload="see ROW-1 later")
        found = wake.fetch_board_row("ROW-1", reader=lambda row_id: [quoted, good, quoted])
        self.assertEqual("ROW-1", found[0])
        with self.assertRaises(LookupError):
            wake.fetch_board_row("ROW-1", reader=lambda row_id: [quoted, good, good])


class BoardReconcile(unittest.TestCase):
    def test_flag_off_does_not_read(self):
        def window(since):
            raise AssertionError("window read while disabled")
        d, _rec = deps()
        d.board_mirror = False
        d.board_window = window
        status, got = wake.handle(
            "POST", "/hooks/board/reconcile", {"Authorization": "Bearer sweep"}, b"{}", d)
        self.assertEqual(200, status)
        self.assertFalse(got["enabled"])
        self.assertEqual([], got["mirrored"])

    def test_one_pass_is_bounded_and_exits(self):
        client = BoardRedis()
        rows = [board_row("ROW-%d" % i, to="grok") for i in range(80)]
        calls = {"n": 0}

        def window(since):
            calls["n"] += 1
            self.assertIn("T", since)
            return rows

        d, _rec = board_deps(client, {})
        d.board_window = window
        source = inspect.getsource(wake.reconcile_once)
        self.assertNotIn("while ", source)
        self.assertNotIn("sleep(", source)
        got = wake.reconcile_once(d, minutes=5, now="2026-10-11T03:10:00Z")
        self.assertEqual(1, calls["n"])
        self.assertEqual(wake.RECONCILE_MAX, got["seen"])
        self.assertEqual(wake.RECONCILE_MAX, len(got["mirrored"]))
        self.assertEqual(wake.RECONCILE_MAX, len(client.streams[wake.BOARD_STREAM]))
        self.assertTrue(got["ok"])
        again = wake.reconcile_once(d, minutes=5, now="2026-10-11T03:10:00Z")
        self.assertEqual(2, calls["n"])
        self.assertEqual([], again["mirrored"])
        self.assertEqual(wake.RECONCILE_MAX, len(client.streams[wake.BOARD_STREAM]))
        self.assertTrue(all(item["status"] == "duplicate" for item in again["skipped"]))

    def test_reconcile_requires_the_bearer_and_a_timeout_is_an_error(self):
        client = BoardRedis(fail_on="set")
        calls = {"n": 0}

        def window(since):
            calls["n"] += 1
            return [board_row()]

        d, _rec = board_deps(client, {})
        d.board_window = window
        status, _got = wake.handle("POST", "/hooks/board/reconcile", {}, b"{}", d)
        self.assertEqual(401, status)
        self.assertEqual(0, calls["n"])
        started = time.monotonic()
        status, got = wake.handle(
            "POST", "/hooks/board/reconcile", {"Authorization": "Bearer sweep"}, b"{}", d)
        self.assertEqual(503, status)
        self.assertFalse(got["ok"])
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(1, calls["n"])


class BoardHookScript(unittest.TestCase):
    def test_the_trigger_signs_by_name_and_does_not_fetch_on_append(self):
        text = (REPO / "gas-bus" / "Code.js").read_text(encoding="utf-8")
        self.assertIn("function onBoardEdit", text)
        self.assertIn("function onBoardChange", text)
        self.assertIn("getProperty('BOARD_HOOK_SECRET')", text)
        self.assertIn("getProperty('BOARD_HOOK_URL')", text)
        self.assertIn("X-Board-Signature-256", text)
        self.assertNotIn("BOARD_HOOK_SECRET=", text)
        append = text.split("function handleAppend_", 1)[1].split("function findDuplicate_", 1)[0]
        self.assertIn("queueBoardHook_", append)
        self.assertNotIn("UrlFetchApp", append)
        post = text.split("function postBoardHook_", 1)[1].split("function hmacHex_", 1)[0]
        self.assertIn("muteHttpExceptions: true", post)
        self.assertNotIn("while", post)
        deliver = text.split("function deliverBoardHook_", 1)[1].split(
            "function removeBoardHookTriggers_", 1)[0]
        self.assertNotIn("while", deliver)
        self.assertNotIn("newTrigger", deliver)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)

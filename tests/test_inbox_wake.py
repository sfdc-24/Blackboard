#!/usr/bin/env python3
"""Inbox wake stays off, wakes once per message id, and routes the four agents.

No network. The wakers are fakes. Run: python3 tests/test_inbox_wake.py
"""
import hashlib
import hmac
import importlib.util
import json
import pathlib
import sys
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


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)

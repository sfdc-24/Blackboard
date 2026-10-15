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


class ClaudeApiOnly(unittest.TestCase):
    """Gate 3 Step 3 (CO-20261013-001): "recipient claude-api only. No other folder, no other recipient."""

    def test_claude_api_is_woken_by_running_its_job_once(self):
        d, rec = deps()
        got = wake.decide({"id": "task-0123456789abcdef", "to": "claude-api", "by": "drive-intake"}, d)
        self.assertEqual(("woke", "cloud_run_job", "claude-api-worker"), (got["status"], got["route"], got["target"]))
        self.assertEqual("duplicate", wake.decide({"id": "task-0123456789abcdef", "to": "claude-api"}, d)["status"])
        self.assertEqual(["claude-api-worker"], rec.jobs)

    def test_a_deploy_that_names_claude_api_wakes_nobody_else(self):
        d, rec = deps()
        d.agents = wake.agents_from_env({"INBOX_WAKE_AGENTS": "claude-api"})
        self.assertEqual(frozenset({"claude-api"}), d.agents)
        for who in ("grok", "gemini", "claude-code-cli", "aya"):
            got = wake.decide({"id": "m-1", "to": who, "text": "x"}, d)
            self.assertEqual("not_allowed", got["status"], who)
            self.assertFalse(got["woke"])
        self.assertEqual(set(), d.seen.ids, "nothing is claimed for a recipient it may not wake")
        self.assertEqual(([], [], []), (rec.github, rec.jobs, rec.http))
        self.assertEqual("woke", wake.decide({"id": "m-1", "to": "claude-api"}, d)["status"])

    def test_an_unset_or_wrong_list_wakes_nobody(self):
        for environ in ({}, {"INBOX_WAKE_AGENTS": ""}, {"INBOX_WAKE_AGENTS": "nobody, *"}):
            self.assertEqual(frozenset(), wake.agents_from_env(environ), environ)
        self.assertEqual(frozenset({"claude-api"}), wake.agents_from_env({"INBOX_WAKE_AGENTS": "claude-api,nobody"}))
        d, rec = deps()
        d.agents = wake.agents_from_env({})
        self.assertEqual("not_allowed", wake.decide({"id": "m-2", "to": "claude-api"}, d)["status"])
        self.assertEqual([], rec.jobs)

    def test_the_sweep_reads_only_the_inboxes_it_may_wake(self):
        read = []

        def reader(agent):
            read.append(agent)
            return [{"id": "task-0123456789abcdef", "text": "", "by": "drive-intake"}]
        d, rec = deps(reader=reader)
        d.agents = frozenset({"claude-api"})
        out = wake.sweep(d)
        self.assertEqual(["claude-api"], read)
        self.assertEqual(["woke"], [item["status"] for item in out])
        self.assertEqual(["claude-api-worker"], rec.jobs)

    def test_a_drive_entry_is_swept_under_its_task_id_so_ring_and_sweep_are_one_wake(self):
        class Stream:
            def xrange(self, key, count=50):
                assert key == "fleet:inbox:claude-api", key
                return [(b"1700000000000-0", {b"task_id": b"task-0123456789abcdef", b"by": b"drive-intake",
                                              b"text": b"hello"}),
                        (b"1700000000001-0", {b"by": b"grok", b"text": b"no task id"})]
        entries = wake.read_inbox(Stream(), "claude-api")
        self.assertEqual(["task-0123456789abcdef", "1700000000001-0"], [e["id"] for e in entries])
        d, rec = deps()
        self.assertEqual("woke", wake.decide({"id": "task-0123456789abcdef", "to": "claude-api"}, d)["status"])
        self.assertEqual("duplicate", wake.decide(dict(entries[0], to="claude-api"), d)["status"])
        self.assertEqual(1, len(rec.jobs))


class TheImageCanRunAJob(unittest.TestCase):
    def test_the_metadata_token_needs_nothing_the_image_does_not_carry(self):
        source = (REPO / "cloud" / "inbox-wake" / "main.py").read_text(encoding="utf-8")
        docker = (REPO / "cloud" / "inbox-wake" / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("import state_store", source)
        for module in ("redis_dual",):
            self.assertIn("/app/%s.py" % module, docker)

        class Answer:
            def __init__(self, raw):
                self.raw = raw

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.raw
        seen = []

        def opener(req, timeout=None):
            seen.append((req.full_url, req.get_header("Metadata-flavor")))
            return Answer(b'{"access_token": "tok", "expires_in": 3599}')
        self.assertEqual("tok", wake._metadata_token(opener))
        self.assertEqual("Google", seen[0][1])
        with self.assertRaises(wake.Unconfigured):
            wake._metadata_token(lambda req, timeout=None: Answer(b"{}"))


class TheDeployScript(unittest.TestCase):
    """cloud/inbox-wake/deploy.sh (Gate 3 Step 3): private, flag off, claude-api only, and every variable
    main.py reads is set by it or is knowingly optional."""
    OPTIONAL = {"GITHUB_WAKE_TOKEN", "GROK_WAKE_URL", "INBOX_WAKE_SWEEP_TOKEN", "PORT"}

    def setUp(self):
        self.script = (REPO / "cloud" / "inbox-wake" / "deploy.sh").read_text(encoding="utf-8")

    def test_private_off_and_claude_api_only(self):
        self.assertIn("--no-allow-unauthenticated", self.script)
        self.assertNotIn("allUsers", self.script)
        self.assertNotIn("--allow-unauthenticated", self.script.replace("--no-allow-unauthenticated", ""))
        self.assertIn("INBOX_WAKE_ENABLED=off,", self.script)
        self.assertIn('AGENTS="${INBOX_WAKE_AGENTS:-claude-api}"', self.script)
        self.assertIn('EGRESS="private-ranges-only"', self.script)
        self.assertIn("run jobs add-iam-policy-binding", self.script)
        self.assertNotIn("projects add-iam-policy-binding", self.script, "no project-wide grant")

    def test_every_variable_main_reads_is_set_or_optional(self):
        import re
        source = (REPO / "cloud" / "inbox-wake" / "main.py").read_text(encoding="utf-8")
        read = set(re.findall(r'environ\.get\(\s*"([A-Z0-9_]+)"', source)) | {wake.AGENTS_ENV}
        missing = sorted(name for name in read - self.OPTIONAL if name + "=" not in self.script)
        self.assertEqual([], missing)


class TheDriveIntakeRing(unittest.TestCase):
    """drive-intake's WakeRing (cloud/drive-intake/main.py) posts to POST /wake: the two must agree on the
    body, the signature and the answer, or every Drive request stays queued_not_rung."""

    def test_the_ring_is_signed_routed_and_read_as_rung(self):
        spec = importlib.util.spec_from_file_location("drive_intake_for_ring", REPO / "cloud" / "drive-intake" / "main.py")
        intake = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(intake)
        d, rec = deps(secret="shared")
        d.agents = frozenset({"claude-api"})

        def post(url, body, headers):
            status, payload = wake.handle("POST", "/wake", headers, body, d)
            self.assertEqual(200, status, payload)
            return payload
        ring = intake.WakeRing("https://inbox-wake.example/wake", "shared", post=post)
        task = {"task_id": "task-0123456789abcdef", "recipient_id": "claude-api",
                "request_id": "req-1", "source_doc_id": "doc-0123456789"}
        ring(task)                                  # woke: no exception
        ring(task)                                  # duplicate: still rung, no second wake
        self.assertEqual(["claude-api-worker"], rec.jobs)
        with self.assertRaises(RuntimeError):       # a recipient this deploy may not wake is not a ring
            ring(dict(task, recipient_id="claude-code-cli"))
        wrong = intake.WakeRing("https://inbox-wake.example/wake", "not the secret",
                                post=lambda url, body, headers: wake.handle("POST", "/wake", headers, body, d)[1])
        with self.assertRaises(RuntimeError):
            wrong(dict(task, task_id="task-fedcba9876543210"))


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

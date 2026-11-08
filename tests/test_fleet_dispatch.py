#!/usr/bin/env python3
"""Fleet dispatch stays off, rings a cloud lane once, and does not deploy.

No network. Redis is a fake. The ring is a fake, except one test that drives
inbox-wake's Wakers with injected callables. Run: python3 tests/test_fleet_dispatch.py
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fleet_dispatch", REPO / "cloud" / "fleet-dispatch" / "main.py")
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)

SCRIPT = REPO / "cloud" / "fleet-dispatch" / "deploy.sh"
ON = {"FLEET_DISPATCH": "on", "FLEET_DISPATCH_AGENTS": "gemini,claude-api,grok"}


class FakeRedis:
    """Enough of redis-py for XREAD, SET NX and GET. No socket."""

    def __init__(self):
        self.streams = {}
        self.strings = {}
        self.ttl = {}
        self.ops = []
        self.reads = []
        self.fail_last = False

    def add(self, agent, entry_id, fields=None):
        self.streams.setdefault(dispatch.lane(agent), []).append(
            (entry_id, dict(fields or {})))

    def get(self, key):
        return self.strings.get(key)

    def set(self, key, value, nx=False, ex=None):
        self.ops.append(("set", key))
        if key.startswith(dispatch.LAST) and self.fail_last:
            raise RuntimeError("killed before the outcome was recorded")
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        self.ttl[key] = ex
        return True

    def delete(self, key):
        self.ops.append(("delete", key))
        self.strings.pop(key, None)
        self.ttl.pop(key, None)
        return 1

    def xread(self, streams, count=None, block=None):
        self.reads.append((dict(streams), block))
        out = []
        for name, cursor in streams.items():
            rows = []
            for entry_id, fields in self.streams.get(name, []):
                if dispatch.stream_id_key(entry_id) > dispatch.stream_id_key(cursor):
                    rows.append((entry_id, fields))
            if rows:
                out.append((name, rows))
        return out


def rings_of():
    calls = []

    def ring(agent, message):
        calls.append((agent, message["id"], message.get("text") or ""))
    return calls, ring


class FlagAndAllowlist(unittest.TestCase):
    def test_only_the_exact_string_on_enables(self):
        for value in ("", "off", "ON", "true", "1", "yes", "on "):
            self.assertFalse(dispatch.enabled_from_env({"FLEET_DISPATCH": value}), value)
        self.assertFalse(dispatch.enabled_from_env({}))
        self.assertTrue(dispatch.enabled_from_env({"FLEET_DISPATCH": "on"}))

    def test_flag_off_opens_no_connection(self):
        def factory():
            raise AssertionError("opened a connection")

        for value in ("", "off", "ON", "true", "1", "yes"):
            env = {"FLEET_DISPATCH": value, "FLEET_DISPATCH_AGENTS": "gemini"}
            self.assertIsNone(dispatch.connect(env, factory), value)
            self.assertEqual("idle", dispatch.run(env, factory=factory, iterations=1), value)
        self.assertIsNone(dispatch.connect({"FLEET_DISPATCH_AGENTS": "gemini"}, factory))
        self.assertEqual("idle", dispatch.run(
            {"FLEET_DISPATCH_AGENTS": "gemini"}, factory=factory, iterations=1))

    def test_flag_on_opens_one_connection_and_blocks(self):
        client = FakeRedis()
        opened = []

        def factory():
            opened.append("open")
            return client

        env = {"FLEET_DISPATCH": "on", "FLEET_DISPATCH_AGENTS": "gemini"}
        self.assertIs(client, dispatch.connect(env, factory))
        self.assertEqual("ran", dispatch.run(env, factory=factory, ring=lambda *_a: None,
                                              iterations=1))
        self.assertEqual(["open", "open"], opened)
        self.assertEqual(30000, client.reads[0][1])

    def test_an_empty_allowlist_opens_nothing(self):
        def factory():
            raise AssertionError("opened a connection")

        for env in (
            {"FLEET_DISPATCH": "on"},
            {"FLEET_DISPATCH": "on", "FLEET_DISPATCH_AGENTS": ""},
            {"FLEET_DISPATCH": "on", "FLEET_DISPATCH_AGENTS": "claude-code-cli"},
            {"FLEET_DISPATCH": "on", "FLEET_DISPATCH_AGENTS": "not-an-agent"},
        ):
            self.assertEqual((), dispatch.allowed_agents(env))
            self.assertIsNone(dispatch.connect(env, factory))
            self.assertEqual("idle", dispatch.run(env, factory=factory, iterations=1))

    def test_an_agent_off_the_allowlist_is_never_rung(self):
        client = FakeRedis()
        client.add("gemini", "10-0", {"text": "gem"})
        client.add("claude-api", "10-1", {"text": "api"})
        client.add("grok", "10-2", {"text": "grok"})
        calls, ring = rings_of()
        env = {"FLEET_DISPATCH": "on", "FLEET_DISPATCH_AGENTS": "gemini"}
        agents = dispatch.allowed_agents(env)
        self.assertEqual(("gemini",), agents)
        got = dispatch.tick(client, agents, ring, clock=lambda: 5)
        self.assertEqual([("gemini", "10-0")], [(item["agent"], item["id"]) for item in got])
        self.assertEqual([("gemini", "10-0", "gem")], calls)
        self.assertEqual({"fleet:inbox:gemini": "0-0"}, client.reads[0][0])
        self.assertNotIn("fleet:dispatch:cursor:claude-api", client.strings)
        self.assertNotIn("fleet:dispatch:cursor:grok", client.strings)
        self.assertNotIn("fleet:wake:seen:claude-api:10-1", client.strings)
        self.assertNotIn("fleet:wake:seen:grok:10-2", client.strings)

    def test_cloud_lanes_are_the_cloud_routes_only(self):
        self.assertEqual({"gemini", "grok", "claude-api"}, set(dispatch.cloud_agents()))
        for laptop in ("claude-code-cli", "chatgpt-codex-desktop"):
            self.assertNotIn(laptop, dispatch.cloud_agents())
        self.assertEqual(("grok", "gemini"), dispatch.allowed_agents(
            {"FLEET_DISPATCH_AGENTS": "grok-bot, gemini"}))


class RingOnce(unittest.TestCase):
    def test_an_entry_rings_once(self):
        client = FakeRedis()
        client.add("gemini", "1700000000000-0", {"text": "hello", "by": "cursor",
                                                "task_id": "task-9"})
        calls, ring = rings_of()
        got = dispatch.tick(client, ("gemini",), ring, clock=lambda: 50)
        self.assertEqual([{"agent": "gemini", "id": "1700000000000-0",
                           "outcome": "woke", "at": 50}], got)
        self.assertEqual([("gemini", "1700000000000-0", "hello")], calls)
        self.assertEqual(30000, client.reads[0][1])
        seen = "fleet:wake:seen:gemini:1700000000000-0"
        self.assertIn(seen, client.strings)
        self.assertEqual(dispatch.inbox_wake.SEEN_TTL, client.ttl[seen])
        self.assertNotIn("fleet:wake:seen:gemini:task-9", client.strings)
        self.assertEqual("1700000000000-0", client.get("fleet:dispatch:cursor:gemini"))
        last = json.loads(client.get("fleet:dispatch:last:gemini"))
        self.assertEqual({"at": 50, "id": "1700000000000-0", "outcome": "woke"}, last)

    def test_a_reread_rings_nothing(self):
        client = FakeRedis()
        client.add("gemini", "4-0", {"text": "once"})
        calls, ring = rings_of()
        first = dispatch.tick(client, ("gemini",), ring, clock=lambda: 1)
        second = dispatch.tick(client, ("gemini",), ring, clock=lambda: 2)
        self.assertEqual("woke", first[0]["outcome"])
        self.assertEqual([], second)
        self.assertEqual([("gemini", "4-0", "once")], calls)
        self.assertEqual("4-0", client.reads[1][0]["fleet:inbox:gemini"])

    def test_a_burst_collapses_to_one_ring_carrying_the_newest_id(self):
        client = FakeRedis()
        client.add("claude-api", "1-0", {"text": "old"})
        client.add("claude-api", "3-0", {"text": "new"})
        client.add("claude-api", "2-0", {"text": "mid"})
        calls, ring = rings_of()
        got = dispatch.tick(client, ("claude-api",), ring, clock=lambda: 9)
        self.assertEqual([("claude-api", "3-0", "new")], calls)
        self.assertEqual("3-0", got[0]["id"])
        self.assertEqual("woke", got[0]["outcome"])
        for entry_id in ("1-0", "2-0", "3-0"):
            self.assertIn("fleet:wake:seen:claude-api:" + entry_id, client.strings)
        self.assertEqual("3-0", client.get("fleet:dispatch:cursor:claude-api"))
        # The next window does not see the collapsed ids again.
        self.assertEqual([], dispatch.tick(client, ("claude-api",), ring, clock=lambda: 10))
        self.assertEqual(1, len(calls))

    def test_the_cursor_moves_only_after_the_outcome_is_recorded(self):
        client = FakeRedis()
        client.add("gemini", "8-0", {"text": "x"})
        _calls, ring = rings_of()
        client.fail_last = True
        with self.assertRaises(RuntimeError):
            dispatch.deliver(client, "gemini", [("8-0", {"text": "x"})], ring, clock=lambda: 3)
        self.assertNotIn("fleet:dispatch:cursor:gemini", client.strings)
        self.assertNotIn("fleet:dispatch:last:gemini", client.strings)
        self.assertIn("fleet:wake:seen:gemini:8-0", client.strings)

        client.fail_last = False
        fresh = FakeRedis()
        fresh.add("gemini", "8-0", {"text": "x"})
        dispatch.deliver(fresh, "gemini", [("8-0", {"text": "x"})], ring, clock=lambda: 4)
        keys = [key for op, key in fresh.ops if op == "set"]
        self.assertLess(keys.index("fleet:dispatch:last:gemini"),
                        keys.index("fleet:dispatch:cursor:gemini"))
        self.assertEqual("8-0", fresh.get("fleet:dispatch:cursor:gemini"))

    def test_a_crash_between_ring_and_record_rerings_at_most_once(self):
        client = FakeRedis()
        client.add("gemini", "7-0", {"text": "wake"})
        calls, ring = rings_of()
        client.fail_last = True
        with self.assertRaises(RuntimeError):
            dispatch.tick(client, ("gemini",), ring, clock=lambda: 1)
        self.assertEqual([("gemini", "7-0", "wake")], calls)
        self.assertNotIn("fleet:dispatch:cursor:gemini", client.strings)

        client.fail_last = False
        recovered = dispatch.tick(client, ("gemini",), ring, clock=lambda: 2)
        self.assertEqual([("gemini", "7-0", "wake")], calls)
        self.assertEqual("duplicate", recovered[0]["outcome"])
        self.assertEqual("7-0", recovered[0]["id"])
        self.assertEqual("7-0", client.get("fleet:dispatch:cursor:gemini"))
        last = json.loads(client.get("fleet:dispatch:last:gemini"))
        self.assertEqual({"at": 2, "id": "7-0", "outcome": "duplicate"}, last)

        self.assertEqual([], dispatch.tick(client, ("gemini",), ring, clock=lambda: 3))
        self.assertEqual(1, len(calls))

    def test_a_failed_ring_releases_the_claim_and_still_records(self):
        client = FakeRedis()
        client.add("grok", "5-0", {"text": "ping"})

        def ring(agent, message):
            raise dispatch.inbox_wake.Unconfigured("GROK_WAKE_URL unset")

        got = dispatch.tick(client, ("grok",), ring, clock=lambda: 6)
        self.assertEqual("unconfigured", got[0]["outcome"])
        self.assertNotIn("fleet:wake:seen:grok:5-0", client.strings)
        self.assertEqual("5-0", client.get("fleet:dispatch:cursor:grok"))
        self.assertEqual([], dispatch.tick(client, ("grok",), ring, clock=lambda: 7))

    def test_the_ring_is_inbox_wake_dispatch(self):
        jobs = []
        posts = []
        wakers = dispatch.inbox_wake.Wakers(
            grok_url="https://grok.example/wake",
            run_job=jobs.append,
            http_post=lambda url, body: posts.append((url, body)),
        )
        ring = dispatch.ring_for(wakers)
        client = FakeRedis()
        client.add("gemini", "1-0", {"text": "g", "by": "cursor"})
        client.add("claude-api", "2-0", {"text": "c", "by": "cursor"})
        client.add("grok", "3-0", {"text": "hello", "by": "cursor", "task_id": "task-1"})
        dispatch.tick(client, ("gemini", "claude-api", "grok"), ring, clock=lambda: 1)
        self.assertEqual(["gemini-waker", "claude-api-worker"], jobs)
        self.assertEqual("https://grok.example/wake", posts[0][0])
        self.assertEqual("3-0", posts[0][1]["id"])
        self.assertEqual("hello", posts[0][1]["text"])
        self.assertNotEqual("task-1", posts[0][1]["id"])
        self.assertEqual(
            ["cloud_run_job", "cloud_run_job", "http"],
            [item["kind"] for item in wakers.calls])


class DeployScript(unittest.TestCase):
    def test_plan_quotes_a_valid_host_and_does_not_run_it(self):
        proc, marker = _plan({"REDIS_HOST": "10.0.0.8", "REDIS_PORT": "6378",
                              "FLEET_DISPATCH_AGENTS": "gemini,claude-api"})
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertFalse(marker.exists(), "plan executed a generated command")
        text = proc.stdout
        self.assertIn("set -euo pipefail", text)
        self.assertIn("run worker-pools deploy fleet-dispatch", text)
        self.assertIn("--instances=1", text)
        self.assertIn("--vpc-egress=private-ranges-only", text)
        self.assertIn("--network=default", text)
        self.assertIn("--subnet=default", text)
        self.assertIn("--service-account=fleet-dispatch@", text)
        self.assertIn("FLEET_DISPATCH=off", text)
        self.assertNotIn("FLEET_DISPATCH=on", text)
        self.assertIn("REDIS_HOST=10.0.0.8", text)
        self.assertIn("FLEET_DISPATCH_AGENTS=gemini,claude-api", text)
        self.assertRegex(text, r"'--set-env-vars=\^;\^[^']+'")
        self.assertIn("/secrets/auth/redis-auth=REDIS_AUTH_STRING:latest", text)
        self.assertNotIn("REDIS_AUTH_STRING=REDIS_AUTH_STRING", text)
        syntax = subprocess.run(["bash", "-n"], input=text, capture_output=True, text=True)
        self.assertEqual(0, syntax.returncode, syntax.stderr)
        for job in ("gemini-waker", "claude-api-worker"):
            self.assertIn(job, SCRIPT.read_text(encoding="utf-8"))
        jobs = [target for _agent, (kind, target) in dispatch.inbox_wake.ROUTES.items()
                if kind == "cloud_run_job"]
        self.assertEqual(["gemini-waker", "claude-api-worker"], jobs)

    def test_plan_refuses_operator_values_that_would_break_the_script(self):
        refused = (
            {"REDIS_HOST": ""},
            {"REDIS_HOST": "10.0.0.1'; touch /tmp/owned"},
            {"REDIS_HOST": "10.0.0.256"},
            {"REDIS_HOST": "10.0.0.8", "REDIS_PORT": "0"},
            {"REDIS_HOST": "10.0.0.8", "REDIS_PORT": "65536"},
            {"REDIS_HOST": "10.0.0.8", "FLEET_DISPATCH_AGENTS": "gemini;curl"},
            {"REDIS_HOST": "10.0.0.8", "GROK_WAKE_URL": "http://grok.example/wake"},
            {"REDIS_HOST": "10.0.0.8", "GROK_WAKE_URL": "https://grok.example/a'b"},
        )
        for env in refused:
            proc, marker = _plan(env)
            self.assertNotEqual(0, proc.returncode, env)
            self.assertIn("refusing", proc.stderr, env)
            self.assertNotIn("worker-pools deploy", proc.stdout, env)
            self.assertFalse(marker.exists(), env)
            self.assertNotIn("touch", proc.stdout)

    def test_the_image_defaults_off_and_the_build_can_see_it(self):
        docker = (REPO / "cloud" / "fleet-dispatch" / "Dockerfile").read_text(encoding="utf-8")
        ignore = (REPO / ".gcloudignore").read_text(encoding="utf-8")
        self.assertIn("ENV FLEET_DISPATCH=off", docker)
        self.assertIn("COPY cloud/fleet-dispatch/main.py /app/main.py", docker)
        self.assertIn("COPY cloud/inbox-wake/main.py /app/inbox_wake.py", docker)
        self.assertIn("COPY scripts/redis_dual.py /app/redis_dual.py", docker)
        self.assertIn("!/cloud/fleet-dispatch", ignore)
        self.assertEqual(0, subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True).returncode)
        usage = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(2, usage.returncode)
        self.assertIn("plan", usage.stdout)


def _plan(overrides):
    marker_dir = pathlib.Path(os.environ.get("TMPDIR") or "/tmp")
    # A unique marker per call, so one refusal cannot hide behind another's file.
    marker = marker_dir / ("fleet-dispatch-gcloud-%s" % os.getpid())
    # The name includes the overrides so two calls in one process do not share it.
    marker = marker.with_name(marker.name + "-%s" % abs(hash(tuple(sorted(overrides.items())))))
    if marker.exists():
        marker.unlink()
    fake_dir = marker.with_name(marker.name + "-bin")
    fake_dir.mkdir(exist_ok=True)
    fake = fake_dir / "gcloud"
    fake.write_text("#!/bin/sh\ntouch '%s'\nexit 99\n" % marker, encoding="utf-8")
    fake.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_dir) + os.pathsep + env.get("PATH", "")
    env["GCLOUD"] = str(fake)
    for key in ("PROJECT", "REGION", "NETWORK", "SUBNET", "TAG", "POOL", "SA_NAME",
                "REDIS_HOST", "REDIS_PORT", "GROK_WAKE_URL", "FLEET_DISPATCH_AGENTS"):
        env.pop(key, None)
    env.update(overrides)
    proc = subprocess.run(["bash", str(SCRIPT), "plan"], capture_output=True, text=True, env=env)
    return proc, marker


if __name__ == "__main__":
    unittest.main()

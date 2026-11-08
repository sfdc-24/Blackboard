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
from unittest import mock

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

    def get(self, key):
        found = self.store.get(key)
        return found[0] if found else None


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
            def xrevrange(self, key, count=50):
                assert key == "fleet:inbox:claude-api", key
                return [(b"1700000000001-0", {b"by": b"grok", b"text": b"no task id"}),
                        (b"1700000000000-0", {b"task_id": b"task-0123456789abcdef", b"by": b"drive-intake",
                                              b"text": b"hello"})]
        entries = wake.read_inbox(Stream(), "claude-api")
        self.assertEqual(["task-0123456789abcdef", "1700000000001-0"], [e["id"] for e in entries])
        d, rec = deps()
        self.assertEqual("woke", wake.decide({"id": "task-0123456789abcdef", "to": "claude-api"}, d)["status"])
        self.assertEqual("duplicate", wake.decide(dict(entries[0], to="claude-api"), d)["status"])
        self.assertEqual(1, len(rec.jobs))


class TheSweepReadsTheNewest(unittest.TestCase):
    def test_a_ring_missed_past_the_first_fifty_is_still_swept(self):
        """Copilot on #412: XRANGE with no lower bound read the oldest 50 for ever."""
        entries = [("1700000000%03d-0" % i, {"task_id": "task-%016x" % i, "by": "drive-intake"}) for i in range(120)]

        class Stream:
            def xrevrange(self, key, count=50):
                return list(reversed(entries))[:count]
        read = wake.read_inbox(Stream(), "claude-api")
        self.assertEqual(wake.SWEEP_WINDOW, len(read))
        self.assertEqual("task-%016x" % 119, read[-1]["id"], "the newest is read")
        self.assertEqual("task-%016x" % 70, read[0]["id"], "oldest first within the window")
        d, rec = deps(reader=lambda agent: read if agent == "claude-api" else [])
        d.agents = frozenset({"claude-api"})
        for item in read[:-1]:
            d.seen.claim("claude-api:" + item["id"])     # every ring but the newest landed
        out = wake.sweep(d)
        self.assertEqual(["woke"], [o["status"] for o in out if o["status"] != "duplicate"])
        self.assertEqual(["claude-api-worker"], rec.jobs)


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
        self.assertIn("INBOX_WAKE_REDIS=off,", self.script)
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


class Lane:
    """fleet:inbox:claude-api. Called, it is read_inbox: the sweep's newest window, oldest first. read() is
    read_lane: XRANGE forward from `start`, inclusive, at most `count`. add() is an XADD; trim(n) is a
    MAXLEN trim to the newest n."""

    def __init__(self):
        self.entries, self.reads, self.pages, self.seq = [], 0, [], 0

    def add(self, by="grok", task_id=""):
        self.seq += 1
        sid = "%d-0" % (1000 + self.seq)
        self.entries.append({"id": task_id or sid, "stream_id": sid, "by": by, "text": "", "at": ""})
        return sid

    def trim(self, keep):
        del self.entries[:max(0, len(self.entries) - keep)]

    def head(self):
        return self.entries[-1]["stream_id"] if self.entries else ""

    def __call__(self, agent):
        self.reads += 1
        return list(self.entries[-wake.SWEEP_WINDOW:]) if agent == "claude-api" else []

    def read(self, agent, start, count):
        self.reads += 1
        self.pages.append((start, count))
        floor = wake._stream_key(start) or (0, -1)
        found = [e for e in self.entries if wake._stream_key(e["stream_id"]) >= floor] if start else self.entries
        return list(found[:count]) if agent == "claude-api" else []


class Worker:
    """claude-api-worker as the wake sees it: each start considers at most `cap` entries after its own
    position (fleet:worker:inbox_scan:claude-api `after`). stalled: a start that moves nothing."""

    def __init__(self, lane, cap=500):
        self.lane, self.cap, self.after, self.stalled, self.starts = lane, cap, "", False, []

    def progress(self, agent):
        return self.after, self.lane.head()

    def run(self, job):
        self.starts.append((job, self.after))
        if self.stalled:
            return
        floor = wake._stream_key(self.after) or (0, -1)
        ahead = [e for e in self.lane.entries if wake._stream_key(e["stream_id"]) > floor][:self.cap]
        if ahead:
            self.after = ahead[-1]["stream_id"]


def lane_deps(on=True, run_job=None, cap=500):
    """claude-api only, the lane path on (or off), a memory cursor, a lane and a worker that keeps its own
    position. rec.jobs records every start."""
    d, rec = deps()
    d.agents = frozenset({"claude-api"})
    d.redis_wake = on
    d.cursor = wake.MemoryCursor()
    d.reader = lane = Lane()
    d.lane = lane.read
    worker = Worker(lane, cap=cap)
    d.progress = worker.progress

    def started(job):
        rec.jobs.append(job)
        worker.run(job)
    d.wakers.run_job = run_job or started
    d.worker = worker
    return d, rec, lane


def nudge(d, lane="claude-api", secret="hook", signature=None):
    body = json.dumps({"by": "bus-reconciler", "lane": lane}, sort_keys=True).encode()
    return wake.handle("POST", "/wake", {
        "X-GitHub-Event": wake.LANE_EVENT,
        "X-Hub-Signature-256": signature or sign(secret, body),
    }, body, d)


class TheLanePath(unittest.TestCase):
    """INBOX_WAKE_REDIS: a nudge (or the sweep) starts the worker while the worker's OWN position is behind
    the lane's head, once per position. The wake cursor reads forward and moves only once the worker was
    started for its entries or is already past them."""

    def test_only_the_exact_string_on_enables_the_lane_path(self):
        self.assertTrue(wake.redis_wake_from_env({"INBOX_WAKE_REDIS": "on"}))
        for value in ("", "off", "ON", "true", "1", " on"):
            self.assertFalse(wake.redis_wake_from_env({"INBOX_WAKE_REDIS": value}), value)
        self.assertFalse(wake.redis_wake_from_env({}))
        self.assertFalse(wake.Deps().redis_wake)
        self.assertIsNone(wake.Deps().cursor)
        self.assertIsNone(wake.Deps().progress)

    def test_a_new_entry_starts_the_worker_once_and_the_cursor_moves_only_after(self):
        cursor_at_start = []
        d, rec, lane = lane_deps()
        d.wakers.run_job = lambda job: (cursor_at_start.append(d.cursor.get("claude-api")),
                                        rec.jobs.append(job), d.worker.run(job))
        sid = lane.add()
        status, payload = nudge(d)
        self.assertEqual(200, status)
        self.assertEqual("woke", payload["woke"][0]["status"])
        self.assertEqual(["claude-api-worker"], rec.jobs)
        self.assertEqual([""], cursor_at_start, "the cursor had not moved when the worker was started")
        self.assertEqual(sid, d.cursor.get("claude-api"))
        status, payload = nudge(d)                  # the same lane again, the worker caught up: nothing
        self.assertEqual("idle", payload["woke"][0]["status"])
        self.assertEqual(["claude-api-worker"], rec.jobs)

    def test_a_failed_start_keeps_the_cursor_and_the_next_nudge_retries(self):
        d, rec, lane = lane_deps()
        attempts = []

        def run_job(job):
            attempts.append(job)
            if len(attempts) == 1:
                raise OSError("run API down")
            d.worker.run(job)
        d.wakers.run_job = run_job
        sid = lane.add()
        self.assertEqual("error", nudge(d)[1]["woke"][0]["status"])
        self.assertEqual("", d.cursor.get("claude-api"), "a failed start does not move the cursor")
        self.assertEqual("woke", nudge(d)[1]["woke"][0]["status"], "the same position is retried")
        self.assertEqual(sid, d.cursor.get("claude-api"))
        self.assertEqual(2, len(attempts))
        self.assertEqual("idle", nudge(d)[1]["woke"][0]["status"])
        self.assertEqual(2, len(attempts))

    def test_an_unconfigured_start_keeps_the_cursor(self):
        d, rec, lane = lane_deps()
        d.wakers.run_job = None                     # no metadata token: the real route is Unconfigured
        d.wakers.token_provider = None
        lane.add()
        self.assertEqual("unconfigured", nudge(d)[1]["woke"][0]["status"])
        self.assertEqual("", d.cursor.get("claude-api"))

    def test_1200_behind_a_500_cap_is_three_starts_and_no_tail_is_stranded(self):
        """Codex on c497137a: one start takes at most 500, so a wake that counts only ITS cursor left the
        tail with no start until a new entry came. The wake follows the worker's own position."""
        d, rec, lane = lane_deps(cap=500)
        sids = [lane.add() for _ in range(1200)]
        with mock.patch.object(wake, "DRAIN_PAGES", 30):     # the wake cursor catches up in one call
            first = nudge(d)[1]["woke"][0]
            self.assertEqual(("woke", 1200, False), (first["status"], first["entries"], first["more"]))
            self.assertEqual(sids[-1], d.cursor.get("claude-api"))
            self.assertEqual(sids[499], d.worker.after)
            for _ in range(5):
                wake.sweep(d)
        self.assertEqual(3, len(rec.jobs))
        self.assertEqual(["", sids[499], sids[999]], [after for _job, after in d.worker.starts])
        self.assertEqual(sids[-1], d.worker.after, "the worker reached the head")
        self.assertEqual("idle", nudge(d)[1]["woke"][0]["status"])

    def test_1200_behind_with_the_paged_wake_cursor_still_catches_up(self):
        d, rec, lane = lane_deps(cap=500)
        sids = [lane.add() for _ in range(1200)]
        for _ in range(10):
            wake.sweep(d)
        self.assertEqual(sids[-1], d.worker.after)
        self.assertEqual(sids[-1], d.cursor.get("claude-api"))
        self.assertLessEqual(len(rec.jobs), 4)
        self.assertEqual(len(rec.jobs), len({after for _job, after in d.worker.starts}),
                         "never two starts at one worker position")

    def test_a_stalled_worker_is_not_started_twice_for_one_position(self):
        d, rec, lane = lane_deps()
        d.worker.stalled = True
        lane.add()
        self.assertEqual("woke", nudge(d)[1]["woke"][0]["status"])
        for _ in range(3):
            self.assertEqual("duplicate", wake.sweep(d)[0]["status"])
        self.assertEqual(1, len(rec.jobs))
        lane.add()                                  # a new head is a new start
        self.assertEqual("woke", nudge(d)[1]["woke"][0]["status"])
        self.assertEqual(2, len(rec.jobs))

    def test_an_accepted_start_that_never_moves_is_started_again_after_the_scan_ttl_and_not_before(self):
        """Codex on e828e71d: the run API accepted the start, the execution died before the worker moved.
        The position claim lapses after the job's timeout plus a margin; a 30-day claim never would."""
        clock = {"t": 5000.0}
        d, rec, lane = lane_deps()
        d.seen = wake.MemorySeen(clock=lambda: clock["t"])
        d.worker.stalled = True
        lane.add()
        self.assertEqual("woke", nudge(d)[1]["woke"][0]["status"])
        for step in (60, wake.WORKER_TASK_TIMEOUT, wake.SCAN_CLAIM_TTL - 1):
            clock["t"] = 5000.0 + step
            self.assertEqual("duplicate", wake.sweep(d)[0]["status"], step)
        self.assertEqual(1, len(rec.jobs), "no second start inside the window")
        clock["t"] = 5000.0 + wake.SCAN_CLAIM_TTL
        self.assertEqual("woke", wake.sweep(d)[0]["status"])
        self.assertEqual(2, len(rec.jobs))
        self.assertEqual([d.worker.starts[0][1]] * 2, [after for _job, after in d.worker.starts],
                         "the same position, started again")
        clock["t"] += 1
        self.assertEqual("duplicate", wake.sweep(d)[0]["status"], "and the new claim holds its own window")

    def test_progress_that_moves_is_a_new_key_and_an_immediate_start(self):
        clock = {"t": 5000.0}
        d, rec, lane = lane_deps(cap=1)
        d.seen = wake.MemorySeen(clock=lambda: clock["t"])
        lane.add()
        lane.add()
        self.assertEqual("woke", nudge(d)[1]["woke"][0]["status"])
        clock["t"] += 1                             # well inside the window: the worker moved, so a new key
        self.assertEqual("woke", wake.sweep(d)[0]["status"])
        self.assertEqual(2, len(rec.jobs))
        self.assertEqual("idle", wake.sweep(d)[0]["status"])

    def test_the_scan_claim_ttl_is_the_worker_job_timeout_plus_a_margin(self):
        import re
        script = (REPO / "cloud" / "claude-api-worker" / "deploy.sh").read_text(encoding="utf-8")
        found = re.findall(r"--task-timeout (\d+)([smh])", script)
        self.assertEqual(1, len(found), "one job definition to read the timeout from")
        number, unit = found[0]
        self.assertEqual(int(number) * {"s": 1, "m": 60, "h": 3600}[unit], wake.WORKER_TASK_TIMEOUT)
        self.assertIn("--max-retries 0", script, "no retry inside the job lengthens a run")
        self.assertEqual(wake.WORKER_TASK_TIMEOUT + wake.SCAN_CLAIM_MARGIN, wake.SCAN_CLAIM_TTL)
        self.assertGreater(wake.SCAN_CLAIM_MARGIN, 0)
        self.assertLess(wake.SCAN_CLAIM_TTL, 3600, "well short of the 30-day seen ttl")
        client = FakeRedis()
        d, rec, lane = lane_deps()
        d.seen = wake.RedisSeen(client)
        lane.add()
        nudge(d)
        ttls = {key: ex for key, (_value, ex) in client.store.items()}
        scan = [key for key in ttls if key.startswith("fleet:wake:seen:claude-api:scan:")]
        self.assertEqual(1, len(scan))
        self.assertEqual(wake.SCAN_CLAIM_TTL, ttls[scan[0]])
        entry = "fleet:wake:seen:claude-api:" + lane.entries[0]["id"]
        self.assertEqual(wake.SEEN_TTL, ttls[entry], "an entry claim keeps the 30-day ttl")

    def test_a_burst_is_one_start_and_no_later_sweep_starts_it_again(self):
        d, rec, lane = lane_deps()
        sids = [lane.add() for _ in range(3)]
        payload = nudge(d)[1]["woke"][0]
        self.assertEqual(("woke", 3), (payload["status"], payload["entries"]))
        self.assertEqual(["claude-api-worker"], rec.jobs)
        self.assertEqual(sids[-1], d.cursor.get("claude-api"))
        self.assertEqual("idle", wake.sweep(d)[0]["status"])
        d.redis_wake = False                        # and the old per-entry sweep, after a rollback
        out = wake.sweep(d)
        self.assertEqual(["duplicate"] * 3, [item["status"] for item in out])
        self.assertEqual(["claude-api-worker"], rec.jobs)

    def test_entries_the_worker_is_already_past_start_nothing(self):
        d, rec, lane = lane_deps()
        lane.add(by="drive-intake", task_id="task-0123456789abcdef")
        d.worker.run("claude-api-worker")           # drive-intake's own ring ran it
        payload = nudge(d)[1]["woke"][0]
        self.assertEqual(("caught_up", 1, False), (payload["status"], payload["entries"], payload["lagging"]))
        self.assertEqual([], rec.jobs)
        self.assertEqual(lane.entries[0]["stream_id"], d.cursor.get("claude-api"))

    def test_the_sweep_drains_the_lane_when_a_nudge_never_came(self):
        d, rec, lane = lane_deps()
        lane.add()
        lane.add()
        out = wake.sweep(d)
        self.assertEqual(1, len(out))
        self.assertEqual(("woke", 2), (out[0]["status"], out[0]["entries"]))
        self.assertEqual(["claude-api-worker"], rec.jobs)
        self.assertEqual(lane.entries[-1]["stream_id"], d.cursor.get("claude-api"))

    def test_the_nudge_is_signed_and_names_a_lane_this_deploy_may_wake(self):
        d, rec, lane = lane_deps()
        lane.add()
        self.assertEqual(401, nudge(d, signature="sha256=dead")[0])
        self.assertEqual(401, nudge(d, secret="not the secret")[0])
        self.assertEqual(0, lane.reads)
        self.assertEqual("no_lane", nudge(d, lane="grok")[1]["woke"][0]["status"])
        self.assertEqual("no_lane", nudge(d, lane="")[1]["woke"][0]["status"])
        d.agents = frozenset({"grok"})
        self.assertEqual("not_allowed", nudge(d)[1]["woke"][0]["status"])
        self.assertEqual(0, lane.reads)
        self.assertEqual([], rec.jobs)
        self.assertEqual("", d.cursor.get("claude-api"))

    def test_the_flag_off_changes_nothing(self):
        """Off: a nudge drains nothing and the sweep is the per-entry sweep it was, cursor untouched."""
        d, rec, lane = lane_deps(on=False)
        d.progress = lambda agent: self.fail("the worker's progress is read with the flag off")
        for _ in range(2):
            lane.add()
        payload = nudge(d)[1]["woke"][0]
        self.assertEqual("lane_off", payload["status"])
        self.assertEqual(0, lane.reads)
        self.assertEqual([], rec.jobs)
        out = wake.sweep(d)
        self.assertEqual(["woke", "woke"], [item["status"] for item in out])
        self.assertTrue(all("lane" not in item and "cursor" not in item for item in out))
        self.assertEqual(["claude-api-worker"] * 2, rec.jobs)
        self.assertEqual({}, d.cursor.ids)
        d2, rec2, lane2 = lane_deps(on=False)
        d2.enabled = False
        self.assertEqual(200, nudge(d2)[0])
        self.assertEqual([], rec2.jobs)

    def test_the_redis_cursor_is_a_fleet_key_with_the_fleet_ttl(self):
        client = FakeRedis()
        cursor = wake.RedisCursor(client)
        self.assertEqual("", cursor.get("claude-api"))
        cursor.set("claude-api", "1700000000000-3")
        self.assertEqual(("1700000000000-3", wake.SEEN_TTL), client.store["fleet:wake:cursor:claude-api"])
        self.assertEqual("1700000000000-3", cursor.get("claude-api"))

    def test_stream_ids_compare_as_numbers(self):
        d, rec, lane = lane_deps()
        lane.entries = [{"id": "9-0", "stream_id": "9-0", "by": "grok"},
                        {"id": "10-0", "stream_id": "10-0", "by": "grok"}]
        d.cursor.set("claude-api", "9-0")
        d.worker.after = "9-0"
        self.assertGreater(wake._stream_key("10-0"), wake._stream_key("9-0"))
        payload = nudge(d)[1]["woke"][0]
        self.assertEqual(("woke", 1), (payload["status"], payload["entries"]))
        self.assertEqual("10-0", d.cursor.get("claude-api"))

    def test_120_entries_behind_are_all_covered_in_order_by_one_start(self):
        """Codex on c497137a: the newest-50 window skipped what lay behind it. The drain reads forward."""
        d, rec, lane = lane_deps()
        lane.add()
        self.assertEqual("woke", nudge(d)[1]["woke"][0]["status"])
        sids = [lane.add() for _ in range(120)]
        claims = []
        claim = d.seen.claim
        d.seen.claim = lambda key, ttl=None: (claims.append(key), claim(key, ttl))[1]
        payload = nudge(d)[1]["woke"][0]
        self.assertEqual(("woke", 120, False), (payload["status"], payload["entries"], payload["more"]))
        self.assertEqual(["claude-api-worker"] * 2, rec.jobs, "one start for the 120")
        self.assertEqual(["claude-api:" + s for s in sids], claims[1:], "every entry, in order")
        self.assertTrue(claims[0].startswith("claude-api:scan:"), "the start is claimed by position")
        self.assertEqual(sids[-1], d.cursor.get("claude-api"))
        self.assertTrue(all(start for start, _count in lane.pages[1:]), "every page starts at an id")
        d.redis_wake = False                        # a rollback: the per-entry sweep starts none of them
        self.assertEqual({"duplicate"}, {item["status"] for item in wake.sweep(d)})
        self.assertEqual(2, len(rec.jobs))

    def test_the_page_bound_keeps_a_call_short_and_the_rest_drains_next_time(self):
        d, rec, lane = lane_deps()
        limit = wake.DRAIN_PAGE * wake.DRAIN_PAGES
        sids = [lane.add() for _ in range(limit + 30)]
        payload = nudge(d)[1]["woke"][0]
        self.assertEqual(("woke", limit, True), (payload["status"], payload["entries"], payload["more"]))
        self.assertLessEqual(len(lane.pages), wake.DRAIN_PAGES)
        self.assertEqual(sids[limit - 1], d.cursor.get("claude-api"), "the cursor at the last entry covered")
        payload = wake.sweep(d)[0]
        self.assertEqual(("caught_up", 30, False), (payload["status"], payload["entries"], payload["more"]))
        self.assertEqual(sids[-1], d.cursor.get("claude-api"))
        self.assertEqual(["claude-api-worker"], rec.jobs, "the worker was already past the rest")
        self.assertEqual("idle", nudge(d)[1]["woke"][0]["status"])

    def test_a_lane_trimmed_past_the_cursor_is_logged_without_words_and_drains_on(self):
        d, rec, lane = lane_deps()
        lane.add(by="the guest's home address")
        self.assertEqual("woke", nudge(d)[1]["woke"][0]["status"])
        cursor = d.cursor.get("claude-api")
        sids = [lane.add() for _ in range(5)]
        lane.trim(3)                                # MAXLEN ~ took the cursor's entry and two after it
        with self.assertLogs("inbox-wake", level="WARNING") as logs:
            payload = nudge(d)[1]["woke"][0]
        self.assertEqual(("woke", 3, True), (payload["status"], payload["entries"], payload["gap"]))
        self.assertEqual(sids[-1], d.cursor.get("claude-api"))
        text = " ".join(logs.output)
        self.assertIn("trimmed", text)
        for word in (cursor, sids[0], "address", "guest"):
            self.assertNotIn(word, text)
        with self.assertRaises(AssertionError):     # an untrimmed lane logs nothing
            with self.assertLogs("inbox-wake", level="WARNING"):
                lane.add()
                self.assertFalse(nudge(d)[1]["woke"][0]["gap"])

    def test_read_lane_is_a_forward_xrange_from_the_cursor(self):
        class Client:
            def __init__(self):
                self.asked = []

            def xrange(self, key, min=None, max=None, count=None):
                self.asked.append((key, min, max, count))
                return [(b"1700000000001-0", {b"by": b"grok"})]
        client = Client()
        out = wake.read_lane(client, "claude-api", "1700000000000-0", 51)
        self.assertEqual([("fleet:inbox:claude-api", "1700000000000-0", "+", 51)], client.asked)
        self.assertEqual(["1700000000001-0"], [e["stream_id"] for e in out])
        wake.read_lane(client, "claude-api", "", 51)
        self.assertEqual("-", client.asked[-1][1])

    def test_read_progress_is_the_worker_s_own_scan_state_and_the_lane_head(self):
        class Client(FakeRedis):
            head = [(b"1700000000009-0", {})]

            def xrevrange(self, key, count=None):
                self.asked = (key, count)
                return self.head
        client = Client()
        self.assertEqual(("", "1700000000009-0"), wake.read_progress(client, "claude-api"))
        self.assertEqual(("fleet:inbox:claude-api", 1), client.asked)
        client.set("fleet:worker:inbox_scan:claude-api",
                   json.dumps({"after": "1700000000004-0", "through": "1700000000009-0"}))
        self.assertEqual(("1700000000004-0", "1700000000009-0"), wake.read_progress(client, "claude-api"))
        for raw in ("{}", "not json", json.dumps({"after": "the guest"}), json.dumps(["x"])):
            client.set("fleet:worker:inbox_scan:claude-api", raw)
            self.assertEqual("", wake.read_progress(client, "claude-api")[0], raw)
        client.head = []
        self.assertEqual("", wake.read_progress(client, "claude-api")[1])
        worker = (REPO / "cloud" / "claude-api-worker" / "main.py").read_text(encoding="utf-8")
        self.assertIn('"%s" + agent' % wake.SCAN_KEY, worker, "the key the worker writes")

    def test_read_inbox_hands_over_the_stream_id(self):
        class Client:
            def xrevrange(self, key, count=None):
                return [(b"1700000000002-0", {b"by": b"grok"}),
                        (b"1700000000001-0", {b"by": b"drive-intake", b"task_id": b"task-0123456789abcdef"})]
        out = wake.read_inbox(Client(), "claude-api")
        self.assertEqual(["1700000000001-0", "1700000000002-0"], [e["stream_id"] for e in out])
        self.assertEqual(["task-0123456789abcdef", "1700000000002-0"], [e["id"] for e in out])


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

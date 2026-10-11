#!/usr/bin/env python3
"""Board mirror is its own writer. Default off. No network.

Run: python3 tests/test_board_mirror.py
"""
import hashlib
import hmac
import importlib.util
import inspect
import json
import pathlib
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "board_mirror", REPO / "cloud" / "board-mirror" / "main.py")
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)

NOW = 1800000000
AUDIENCE = "https://board-mirror-example.run.app"
CALLER = "board-hook@sfdc24.iam.gserviceaccount.com"
SECRET = "board-hook"


def sign(secret, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return "sha256=" + mac


def lookup(token):
    if token != "good-token":
        raise ValueError("token")
    return {"aud": AUDIENCE, "email": CALLER, "email_verified": "true"}


class BoardRedis:
    """SET NX under a lock, plus streams. get/exists are refused on purpose."""

    def __init__(self, fail_on=""):
        self.store = {}
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
            if nx and key in self.store:
                return None
            self.store[key] = (value, ex)
            return True

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


def board_deps(client, rows=None, enabled=True, secret=SECRET):
    rows = {} if rows is None else rows

    def fetch(row_id, rows=rows):
        if row_id not in rows:
            raise LookupError("row")
        return rows[row_id]

    return mirror.Deps(
        enabled=enabled,
        hook_secret=secret,
        audience=AUDIENCE,
        callers=(CALLER,),
        oidc_lookup=lookup,
        board=mirror.RedisBoard(client) if enabled else None,
        board_fetch=fetch,
        now=lambda: NOW,
        reconcile_token="sweep",
    )


def signed_batch(row_ids=("ROW-1",), secret=SECRET, ts=NOW, nonce="nonce-001", **extra):
    events = [{"row_id": row_id, "sheet_row": 12, "to": "owner", "sha256": "deadbeef"}
              for row_id in row_ids]
    event = {"ts": ts, "nonce": nonce, "events": events}
    event.update(extra)
    body = json.dumps(event).encode("utf-8")
    return body, {
        "X-Board-Signature-256": sign(secret, body),
        "Authorization": "Bearer good-token",
    }


class BoardMirrorFlag(unittest.TestCase):
    def test_only_the_exact_string_on_enables_the_mirror(self):
        for value in ("", "off", "ON", "true", "1", "yes", " on"):
            self.assertFalse(mirror.enabled_from_env({"BOARD_MIRROR_ENABLED": value}), value)
        self.assertFalse(mirror.enabled_from_env({}))
        self.assertTrue(mirror.enabled_from_env({"BOARD_MIRROR_ENABLED": "on"}))

    def test_the_image_defaults_off_and_is_its_own_service(self):
        docker = (REPO / "cloud" / "board-mirror" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ENV BOARD_MIRROR_ENABLED=off", docker)
        self.assertIn("COPY scripts/bus.py /app/bus.py", docker)
        build = (REPO / "cloud" / "board-mirror" / "cloudbuild.yaml").read_text(encoding="utf-8")
        self.assertIn("board-mirror:unreleased", build)
        self.assertNotIn("REDIS_AUTH", build)
        ignore = (REPO / ".gcloudignore").read_text(encoding="utf-8")
        self.assertIn("!/cloud/board-mirror", ignore)
        wake_docker = (REPO / "cloud" / "inbox-wake" / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("BOARD_MIRROR", wake_docker)
        self.assertNotIn("COPY scripts/bus.py", wake_docker)

    def test_roles_name_both_accounts_and_refuse_the_bridge(self):
        text = (REPO / "cloud" / "board-mirror" / "main.py").read_text(encoding="utf-8")
        self.assertIn("board-mirror@sfdc24.iam.gserviceaccount.com", text)
        self.assertIn("inbox-wake@sfdc24.iam.gserviceaccount.com", text)
        self.assertIn("redis-tool-bridge@sfdc24.iam.gserviceaccount.com", text)
        self.assertIn("roles/secretmanager.secretAccessor", text)
        self.assertIn("roles/redis.admin", text)
        self.assertIn("--no-allow-unauthenticated", text)
        self.assertIn("fleet:board:nonce:", text)
        self.assertIn("fleet:board:mirrored:", text)
        self.assertNotIn('"ACCEPTED"', text)
        self.assertNotIn("'ACCEPTED'", text)

    def test_flag_off_is_a_no_op_before_auth(self):
        client = BoardRedis()
        called = {"n": 0}

        def fetch(row_id):
            called["n"] += 1
            raise AssertionError("fetch while disabled")

        def explode(token):
            raise AssertionError("oidc while disabled")

        d = board_deps(client, enabled=False)
        d.board_fetch = fetch
        d.oidc_lookup = explode
        body, headers = signed_batch()
        status, payload = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status)
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["mirrored"])
        self.assertEqual(0, called["n"])
        self.assertEqual({}, client.store)
        self.assertEqual({}, client.streams)

    def test_a_bad_token_or_signature_writes_nothing(self):
        client = BoardRedis()
        d = board_deps(client, {"ROW-1": board_row()})
        body, headers = signed_batch()
        status, _payload = mirror.handle("POST", "/hooks/board", {
            "X-Board-Signature-256": headers["X-Board-Signature-256"],
        }, body, d)
        self.assertEqual(401, status)
        status, _payload = mirror.handle("POST", "/hooks/board", {
            "Authorization": "Bearer good-token",
            "X-Board-Signature-256": "sha256=dead",
        }, body, d)
        self.assertEqual(401, status)
        d.callers = ()
        status, _payload = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(401, status)
        self.assertEqual({}, client.streams)
        self.assertEqual({}, client.store)

    def test_a_stale_or_future_timestamp_is_refused(self):
        client = BoardRedis()
        d = board_deps(client, {"ROW-1": board_row()})
        for ts in (NOW - 301, NOW + 61):
            body, headers = signed_batch(ts=ts, nonce="n-%s" % ts)
            status, payload = mirror.handle("POST", "/hooks/board", headers, body, d)
            self.assertEqual(401, status, ts)
            self.assertEqual("stale", payload["error"])
        self.assertEqual({}, client.store)
        body, headers = signed_batch(ts=NOW - mirror.MAX_AGE_SECONDS, nonce="edge-old")
        status, payload = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status, payload)
        body, headers = signed_batch(ts=NOW + mirror.MAX_FUTURE_SECONDS, nonce="edge-new")
        status, payload = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status, payload)
        self.assertEqual("duplicate", payload["status"])


class BoardMirrorWrite(unittest.TestCase):
    def test_an_append_row_is_mirrored_from_the_board_not_the_event(self):
        client = BoardRedis()
        payload = "BCB|v=1|id=MSG1|phase=APPEND|hello"
        row = board_row(payload=payload, to="grok", phase="APPEND")
        d = board_deps(client, {"ROW-1": row})
        body, headers = signed_batch()
        with self.assertLogs("board_mirror", level="INFO") as logs:
            status, got = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status)
        self.assertTrue(got["ok"])
        self.assertEqual("mirrored", got["status"])
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.assertEqual(digest, got["sha256"])
        self.assertNotEqual("deadbeef", got["sha256"])
        self.assertTrue(got["stream_id"])
        board_entries = client.streams[mirror.BOARD_STREAM]
        self.assertEqual(1, len(board_entries))
        stored = board_entries[0][1]
        self.assertEqual("APPEND", stored["phase"])
        self.assertEqual("grok", stored["to"])
        self.assertEqual("MSG1", stored["id"])
        self.assertEqual(digest, stored["sha256"])
        self.assertEqual(["fleet:inbox:grok"], [item["key"] for item in got["inboxes"]])
        self.assertTrue(any(got["stream_id"] in line and digest in line for line in logs.output))
        self.assertEqual((digest, mirror.SEEN_TTL), client.store[mirror.MIRROR_PREFIX + "ROW-1"])
        self.assertEqual(("1", mirror.NONCE_TTL), client.store[mirror.NONCE_PREFIX + "nonce-001"])
        self.assertGreaterEqual(mirror.NONCE_TTL, 300)
        for key in client.store:
            self.assertTrue(key.startswith("fleet:board:"), key)
        for key in client.streams:
            self.assertTrue(
                key == mirror.BOARD_STREAM or key.startswith(mirror.INBOX_PREFIX), key)

    def test_a_replayed_nonce_writes_nothing_more(self):
        client = BoardRedis()
        d = board_deps(client, {"ROW-1": board_row()})
        body, headers = signed_batch()
        status, first = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status)
        self.assertEqual("mirrored", first["status"])
        status, second = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(401, status)
        self.assertEqual("replay", second["error"])
        self.assertEqual(1, len(client.streams[mirror.BOARD_STREAM]))
        self.assertEqual(1, len(client.streams["fleet:inbox:grok"]))

    def test_a_batch_is_one_nonce_and_one_entry_per_row(self):
        client = BoardRedis()
        rows = {"ROW-1": board_row("ROW-1", to="grok"), "ROW-2": board_row("ROW-2", to="cursor")}
        d = board_deps(client, rows)
        body, headers = signed_batch(("ROW-1", "ROW-2"), nonce="batch-001")
        status, got = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status, got)
        self.assertEqual(2, len(got["results"]))
        self.assertEqual(2, len(client.streams[mirror.BOARD_STREAM]))
        self.assertEqual(1, len(client.streams["fleet:inbox:grok"]))
        self.assertEqual(1, len(client.streams["fleet:inbox:cursor"]))
        self.assertIn(mirror.NONCE_PREFIX + "batch-001", client.store)

    def test_to_all_fans_out_once(self):
        client = BoardRedis()
        d = board_deps(client, {"ROW-1": board_row(to="ALL", phase="APPEND")})
        body, headers = signed_batch()
        status, got = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(200, status, got)
        self.assertEqual(
            list(mirror.BOARD_AGENTS),
            [item["key"][len("fleet:inbox:"):] for item in got["inboxes"]])
        self.assertEqual(1, len(client.streams[mirror.BOARD_STREAM]))
        for agent in mirror.BOARD_AGENTS:
            self.assertEqual(1, len(client.streams["fleet:inbox:" + agent]), agent)

    def test_three_concurrent_events_produce_one_stream_entry(self):
        client = BoardRedis()
        d = board_deps(client, {"ROW-1": board_row()})
        bodies = [signed_batch(nonce="nonce-%02d" % i) for i in range(3)]
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def go(pair):
            try:
                barrier.wait(timeout=5)
                results.append(mirror.handle("POST", "/hooks/board", pair[1], pair[0], d))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=go, args=(pair,)) for pair in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual([], errors)
        self.assertEqual(3, len(results))
        statuses = sorted(item[1]["status"] for item in results)
        self.assertEqual(["duplicate", "duplicate", "mirrored"], statuses)
        self.assertEqual(1, len(client.streams[mirror.BOARD_STREAM]))
        self.assertEqual(1, len(client.streams["fleet:inbox:grok"]))
        self.assertTrue(all(item[0] == 200 for item in results))

    def test_a_redis_timeout_is_an_error_and_returns_fast(self):
        client = BoardRedis(fail_on="set")
        d = board_deps(client, {"ROW-1": board_row()})
        body, headers = signed_batch()
        started = time.monotonic()
        status, got = mirror.handle("POST", "/hooks/board", headers, body, d)
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
        d = board_deps(client, {"ROW-1": board_row()})
        body, headers = signed_batch()
        status, got = mirror.handle("POST", "/hooks/board", headers, body, d)
        self.assertEqual(503, status)
        self.assertFalse(got["ok"])
        self.assertNotEqual(True, got.get("mirrored"))

    def test_fetch_uses_the_row_id_and_ignores_a_substring_hit(self):
        good = board_row("ROW-1")
        quoted = board_row("OTHER", payload="see ROW-1 later")
        found = mirror.fetch_board_row("ROW-1", reader=lambda row_id: [quoted, good, quoted])
        self.assertEqual("ROW-1", found[0])
        with self.assertRaises(LookupError):
            mirror.fetch_board_row("ROW-1", reader=lambda row_id: [quoted, good, good])


class BoardReconcile(unittest.TestCase):
    def test_flag_off_does_not_read(self):
        def window(since):
            raise AssertionError("window read while disabled")
        d = mirror.Deps(enabled=False, board_window=window)
        status, got = mirror.handle(
            "POST", "/hooks/board/reconcile", {"Authorization": "Bearer good-token"}, b"{}", d)
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

        d = board_deps(client, {})
        d.board_window = window
        source = inspect.getsource(mirror.reconcile_once)
        self.assertNotIn("while ", source)
        self.assertNotIn("sleep(", source)
        got = mirror.reconcile_once(d, minutes=5, now="2026-10-11T03:10:00Z")
        self.assertEqual(1, calls["n"])
        self.assertEqual(mirror.RECONCILE_MAX, got["seen"])
        self.assertEqual(mirror.RECONCILE_MAX, len(got["mirrored"]))
        self.assertEqual(mirror.RECONCILE_MAX, len(client.streams[mirror.BOARD_STREAM]))
        again = mirror.reconcile_once(d, minutes=5, now="2026-10-11T03:10:00Z")
        self.assertEqual(2, calls["n"])
        self.assertEqual([], again["mirrored"])
        self.assertTrue(all(item["status"] == "duplicate" for item in again["skipped"]))

    def test_reconcile_requires_the_token_and_a_timeout_is_an_error(self):
        client = BoardRedis(fail_on="set")
        calls = {"n": 0}

        def window(since):
            calls["n"] += 1
            return [board_row()]

        d = board_deps(client, {})
        d.board_window = window
        headers = {
            "Authorization": "Bearer good-token",
            "X-Board-Reconcile": "Bearer sweep",
        }
        status, _got = mirror.handle("POST", "/hooks/board/reconcile", {
            "Authorization": "Bearer good-token",
        }, b"{}", d)
        self.assertEqual(401, status)
        self.assertEqual(0, calls["n"])
        started = time.monotonic()
        status, got = mirror.handle("POST", "/hooks/board/reconcile", headers, b"{}", d)
        self.assertEqual(503, status)
        self.assertFalse(got["ok"])
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(1, calls["n"])


class BoardHookScript(unittest.TestCase):
    def test_installable_triggers_do_not_churn_and_post_once(self):
        text = (REPO / "gas-bus" / "Code.js").read_text(encoding="utf-8")
        manifest = (REPO / "gas-bus" / "appsscript.json").read_text(encoding="utf-8")
        self.assertIn("function onBoardEdit", text)
        self.assertIn("function onBoardChange", text)
        self.assertIn("getProperty('BOARD_HOOK_SECRET')", text)
        self.assertIn("getProperty('BOARD_HOOK_URL')", text)
        self.assertIn("getIdentityToken", text)
        self.assertIn("X-Board-Signature-256", text)
        self.assertIn("Authorization", text)
        self.assertIn("Utilities.getUuid()", text)
        self.assertNotIn("BOARD_HOOK_SECRET=", text)
        for banned in ("timeBased", "newTrigger", "deleteTrigger", "queueBoardHook_",
                       "deliverBoardHook_"):
            self.assertNotIn(banned, text, banned)
        self.assertNotIn("script.scriptapp", manifest)
        append = text.split("function handleAppend_", 1)[1].split("function findDuplicate_", 1)[0]
        self.assertNotIn("UrlFetchApp", append)
        self.assertNotIn("postBoardHook_", append)
        post = text.split("function postBoardHook_", 1)[1].split("function hmacHex_", 1)[0]
        self.assertEqual(1, post.count("UrlFetchApp.fetch"))
        self.assertIn("muteHttpExceptions: true", post)
        self.assertNotIn("while", post)
        change = text.split("function onBoardChange", 1)[1].split("function boardEvent_", 1)[0]
        self.assertIn("INSERT_ROW", change)
        self.assertIn("EDIT", change)
        edit = text.split("function onBoardEdit", 1)[1].split("function onBoardChange", 1)[0]
        self.assertEqual(1, edit.count("postBoardHook_("))


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    raise SystemExit(0 if result.wasSuccessful() else 1)

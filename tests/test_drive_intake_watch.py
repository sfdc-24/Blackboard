"""drive-intake's push channel (cloud/drive-intake/drive_watch.py and the hook in main.py).

The ways this goes wrong, each with a test that fails when the guard is removed:
  - a renewal that stops the old channel before the new one is on record (a gap)
  - a renewal that opens a channel when the current one has days left (channel churn)
  - Drive's refusal (e.g. push.webhookUrlUnauthorized) swallowed instead of read out
  - the resolver keeping a doc outside the folder, a trashed or removed one, or a non-doc
  - the page token not advancing, so every push re-reads the same changes
  - a push from a channel not on record doing work
  - a resolver failure losing the task instead of falling back to the poll
  - /watch/renew answering without the poll token
  - /healthz reading the deploy's setting when a watch record exists
  - the watch switched on by anything but DRIVE_INTAKE_WATCH=on, or the image lacking drive_watch.py

Offline: Drive is a fake fetch, Redis a dict.
"""
import importlib.util
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("drive_intake_tests_base", REPO / "tests" / "test_drive_intake.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
di = base.di
_spec = importlib.util.spec_from_file_location("drive_watch", REPO / "cloud" / "drive-intake" / "drive_watch.py")
dw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dw)

FOLDER, FILE = base.FOLDER, base.FILE
DOC = "application/vnd.google-apps.document"
PUSH_URL = "https://drive-intake-96522051727.us-central1.run.app/hooks/drive"
NOW = 1000.0


class Redis:
    def __init__(self):
        self.data, self.ttls = {}, {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key], self.ttls[key] = value, ex
        return True


class Drive:
    """A fake fetch: answers by URL and keeps every call in order."""

    def __init__(self, changes=None, watch_status=200, watch_body=None, stop_status=204):
        self.calls = []
        self.changes = changes or {}          # page token -> changes.list answer
        self.watch_status, self.watch_body, self.stop_status = watch_status, watch_body, stop_status

    def __call__(self, method, url, body, headers):
        sent = json.loads(body.decode("utf-8")) if body else None
        self.calls.append((method, url, sent, headers.get("Authorization")))
        if "/changes/startPageToken" in url:
            return 200, b'{"startPageToken": "100"}'
        if "/changes/watch" in url:
            if self.watch_status != 200:
                return self.watch_status, json.dumps(self.watch_body or {}).encode("utf-8")
            return 200, json.dumps({"kind": "api#channel", "id": sent["id"], "resourceId": "res-" + sent["id"][-4:],
                                    "expiration": sent["expiration"]}).encode("utf-8")
        if "/channels/stop" in url:
            return self.stop_status, b""
        if url.startswith(dw.CHANGES + "?"):
            token = url.split("pageToken=", 1)[1].split("&", 1)[0]
            return 200, json.dumps(self.changes.get(token, {"newStartPageToken": token, "changes": []})).encode("utf-8")
        raise AssertionError(url)

    def named(self, part):
        return [c for c in self.calls if part in c[1]]


def change(file_id, parents=(FOLDER,), mime=DOC, trashed=False, removed=False):
    return {"fileId": file_id, "removed": removed, "file": {"mimeType": mime, "parents": list(parents), "trashed": trashed}}


def watch(drive=None, redis=None, now=NOW):
    return dw.DriveWatch(lambda: "t0ken", drive or Drive(), redis if redis is not None else Redis(), now=now)


class RenewTest(unittest.TestCase):
    def test_the_first_renewal_takes_a_start_token_and_opens_a_channel(self):
        drive, redis = Drive(), Redis()
        out = watch(drive, redis).renew(PUSH_URL, "tok", warn=172800)
        self.assertEqual("renewed", out["status"])
        self.assertEqual(dw.CHANNEL_SECONDS, out["seconds_left"])
        sent = drive.named("/changes/watch")[0]
        self.assertIn("pageToken=100", sent[1])
        self.assertEqual(("web_hook", PUSH_URL, "tok"), (sent[2]["type"], sent[2]["address"], sent[2]["token"]))
        self.assertEqual(str(int((NOW + dw.CHANNEL_SECONDS) * 1000)), sent[2]["expiration"])
        record = json.loads(redis.data[dw.WATCH_KEY])
        self.assertEqual(sent[2]["id"], record["id"])
        self.assertEqual("100", redis.data[dw.PAGE_KEY])
        self.assertEqual([], drive.named("/channels/stop"))

    def test_a_channel_with_days_left_is_left_alone(self):
        drive, redis = Drive(), Redis()
        watch(drive, redis).renew(PUSH_URL, "tok", warn=172800)
        calls = len(drive.calls)
        out = watch(drive, redis, now=NOW + 86400).renew(PUSH_URL, "tok", warn=172800)
        self.assertEqual("current", out["status"])
        self.assertEqual(calls, len(drive.calls), "no Drive call for a channel that is not due")

    def test_the_next_channel_is_on_record_before_the_old_one_stops(self):
        drive, redis = Drive(), Redis()
        watch(drive, redis).renew(PUSH_URL, "tok", warn=172800)
        old = json.loads(redis.data[dw.WATCH_KEY])
        seen_at_stop = []
        real = drive.__call__

        def spy(method, url, body, headers):
            if "/channels/stop" in url:
                seen_at_stop.append(json.loads(redis.data[dw.WATCH_KEY])["id"])
            return real(method, url, body, headers)
        w = dw.DriveWatch(lambda: "t0ken", spy, redis, now=NOW + 6 * 86400)
        out = w.renew(PUSH_URL, "tok", warn=172800)
        new = json.loads(redis.data[dw.WATCH_KEY])
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual({"id": old["id"], "resource_id": old["resource_id"]}, new["previous"])
        self.assertEqual([new["id"]], seen_at_stop, "the stop ran only once the new channel was recorded")
        self.assertEqual({"id": old["id"], "resourceId": old["resource_id"]}, drive.named("/channels/stop")[0][2])
        self.assertEqual("stopped", out["stopped_previous"])
        self.assertTrue(w.channel_ok(old["id"]) and w.channel_ok(new["id"]), "both answer during the overlap")
        self.assertFalse(w.channel_ok("drive-intake-someone-else"))

    def test_a_failed_stop_keeps_the_new_channel(self):
        drive, redis = Drive(stop_status=404), Redis()
        watch(drive, redis).renew(PUSH_URL, "tok", warn=172800)
        out = watch(drive, redis, now=NOW + 6 * 86400).renew(PUSH_URL, "tok", warn=172800)
        self.assertEqual("left_to_expire:RuntimeError", out["stopped_previous"])
        self.assertEqual("renewed", out["status"])

    def test_drives_refusal_is_read_out_and_nothing_is_recorded(self):
        drive, redis = Drive(watch_status=401, watch_body={"error": {"errors": [
            {"reason": "push.webhookUrlUnauthorized", "message": "Unauthorized WebHook callback channel"}]}}), Redis()
        with self.assertRaises(RuntimeError) as caught:
            watch(drive, redis).renew(PUSH_URL, "tok", warn=172800)
        self.assertEqual("changes.watch answered 401 push.webhookUrlUnauthorized", str(caught.exception))
        self.assertNotIn(dw.WATCH_KEY, redis.data)

    def test_no_https_address_or_no_token_is_refused_before_drive(self):
        drive = Drive()
        for address, token in (("http://x.run.app/hooks/drive", "tok"), ("", "tok"), (PUSH_URL, "")):
            with self.assertRaises(dw.Unconfigured):
                watch(drive).renew(address, token, warn=172800)
        self.assertEqual([], drive.calls)


class ResolveTest(unittest.TestCase):
    def test_only_live_docs_in_the_folder_and_the_token_advances(self):
        drive, redis = Drive(changes={"100": {"nextPageToken": "101", "changes": [
            change(FILE), change("other-folder-doc", parents=("elsewhere",)), change("sheet", mime="application/vnd.google-apps.spreadsheet"),
            change("binned", trashed=True), change("gone", removed=True), change(FILE)]},
            "101": {"newStartPageToken": "102", "changes": [change("second-doc-id-1234")]}}), Redis()
        redis.data[dw.PAGE_KEY] = "100"
        w = watch(drive, redis)
        hints, token = w.resolve(FOLDER)
        self.assertEqual([FILE, "second-doc-id-1234"], [h["file_id"] for h in hints])
        self.assertEqual("102", token)
        self.assertIn("pageToken=101", drive.calls[-1][1])
        self.assertEqual("100", redis.data[dw.PAGE_KEY], "resolve writes nothing (Aya on bddafa3)")
        w.advance(token)
        self.assertEqual("102", redis.data[dw.PAGE_KEY])

    def test_no_token_on_record_is_an_error_not_an_empty_feed(self):
        with self.assertRaises(dw.Unconfigured):
            watch().resolve(FOLDER)


def push_deps(drive, redis, **over):
    d = base.deps(changes=lambda folder: [], **over)
    d.watch = dw.DriveWatch(lambda: "t0ken", drive, redis, now=NOW)
    d.push_url = PUSH_URL
    return d


def push(channel_id, state="change"):
    return {"x-goog-channel-token": "tok", "x-goog-channel-id": channel_id, "x-goog-resource-state": state,
            "x-goog-resource-uri": "https://www.googleapis.com/drive/v3/changes?pageToken=100&alt=json"}


class HookTest(unittest.TestCase):
    def setUp(self):
        self.drive = Drive(changes={"100": {"newStartPageToken": "101", "changes": [change(FILE)]}})
        self.redis = Redis()
        self.d = push_deps(self.drive, self.redis)
        self.d.watch.renew(PUSH_URL, "tok", warn=172800)
        self.channel = json.loads(self.redis.data[dw.WATCH_KEY])["id"]

    def test_a_push_resolves_through_the_changes_feed_and_dispatches(self):
        code, out = di.handle("POST", "/hooks/drive", push(self.channel), b"", self.d)
        self.assertEqual(200, code)
        self.assertEqual([("dispatched", "push")], [(r["status"], r["via"]) for r in out["intake"]], out)
        self.assertEqual(1, len(self.d.queue.puts))
        self.assertEqual("101", self.redis.data[dw.PAGE_KEY])
        again = di.handle("POST", "/hooks/drive", push(self.channel), b"", self.d)[1]
        self.assertEqual([], again["intake"], "the token moved on: a second push reads no old change")
        self.redis.data[dw.PAGE_KEY] = "100"   # Drive replays from the old token
        replay = di.handle("POST", "/hooks/drive", push(self.channel), b"", self.d)[1]
        self.assertEqual(["duplicate"], [r["status"] for r in replay["intake"]])
        self.assertEqual(1, len(self.d.queue.puts), "a replay is one task")

    def test_a_channel_not_on_record_does_no_work(self):
        out = di.handle("POST", "/hooks/drive", push("drive-intake-stale"), b"", self.d)[1]
        self.assertEqual("unknown channel", out.get("ignored"))
        self.assertEqual(0, self.d.docs.reads)
        self.assertEqual(0, len(self.d.queue.puts))

    def test_the_sync_ping_does_nothing(self):
        out = di.handle("POST", "/hooks/drive", push(self.channel, state="sync"), b"", self.d)[1]
        self.assertEqual([], out["intake"])
        self.assertEqual(0, self.d.docs.reads)

    def test_a_resolver_failure_falls_back_to_the_poll(self):
        del self.redis.data[dw.PAGE_KEY]
        self.d.changes = lambda folder: [{"file_id": FILE, "folder_id": folder}]
        out = di.handle("POST", "/hooks/drive", push(self.channel), b"", self.d)[1]
        self.assertEqual(["push_resolve_error", "dispatched"], [r["status"] for r in out["intake"]], out)
        self.assertEqual(1, len(self.d.queue.puts))

    def test_the_hints_are_pending_before_the_cursor_moves(self):
        """Aya on bddafa3: the cursor advanced before the hints were durable, so a crash lost the push."""
        order = []
        claims, redis = self.d.claims, self.redis
        real_add, real_set = claims.pending_add, redis.set

        def add(file_id):
            order.append(("pending", file_id))
            return real_add(file_id)

        def put(key, value, ex=None, nx=False):
            if key == dw.PAGE_KEY:
                order.append(("cursor", value))
            return real_set(key, value, ex=ex, nx=nx)
        claims.pending_add, redis.set = add, put
        di.handle("POST", "/hooks/drive", push(self.channel), b"", self.d)
        self.assertEqual([("pending", FILE), ("cursor", "101")], order[:2])

    def test_a_crash_after_the_cursor_moved_leaves_the_doc_for_the_poll(self):
        def crash(hint, deps):
            raise RuntimeError("the process died here")
        original = di.decide
        di.decide = crash
        try:
            out = di.handle("POST", "/hooks/drive", push(self.channel), b"", self.d)[1]
        finally:
            di.decide = original
        self.assertEqual(["decide_error"], [r["status"] for r in out["intake"]])
        self.assertEqual("101", self.redis.data[dw.PAGE_KEY])
        self.assertIn(FILE, self.d.claims.pending(), "the doc is pending, so the next poll decides it")
        self.d.changes = lambda folder: []
        polled = di.handle("POST", "/poll", {"authorization": "Bearer poll"}, b"", self.d)[1]
        self.assertEqual(["dispatched"], [r["status"] for r in polled["intake"]], polled)

    def test_an_unwritable_pending_set_keeps_the_cursor_and_polls(self):
        def refuse(file_id):
            raise ConnectionError("redis down")
        self.d.claims.pending_add = refuse
        out = di.handle("POST", "/hooks/drive", push(self.channel), b"", self.d)[1]
        self.assertEqual("pending_unwritable", out["intake"][0]["status"])
        self.assertEqual("100", self.redis.data[dw.PAGE_KEY], "the cursor did not move")

    def test_an_unreadable_channel_record_fails_closed(self):
        """Aya on bddafa3: an exception reading the record used to count as a known channel."""
        def broken(channel_id):
            raise ConnectionError("redis down")
        self.d.watch.channel_ok = broken
        out = di.handle("POST", "/hooks/drive", push(self.channel), b"", self.d)[1]
        self.assertEqual("unknown channel", out.get("ignored"))
        self.assertEqual(0, self.d.docs.reads)

    def test_a_wrong_channel_token_is_still_401(self):
        bad = push(self.channel)
        bad["x-goog-channel-token"] = "nope"
        self.assertEqual(401, di.handle("POST", "/hooks/drive", bad, b"", self.d)[0])

    def test_renew_needs_the_poll_token_and_reads_out_the_result(self):
        self.assertEqual(401, di.handle("POST", "/watch/renew", {}, b"", self.d)[0])
        code, out = di.handle("POST", "/watch/renew", {"authorization": "Bearer poll"}, b"", self.d)
        self.assertEqual((200, "current"), (code, out["watch"]["status"]))
        forced = di.handle("POST", "/watch/renew", {"authorization": "Bearer poll", "x-drive-intake-renew": "force=1"},
                           b"", self.d)[1]
        self.assertEqual("renewed", forced["watch"]["status"])

    def test_renew_failure_is_a_readout(self):
        d = push_deps(Drive(watch_status=403, watch_body={"error": {"errors": [{"reason": "forbidden"}]}}), Redis())
        out = di.handle("POST", "/watch/renew", {"authorization": "Bearer poll"}, b"", d)[1]
        self.assertEqual(("renew_failed", "changes.watch answered 403 forbidden"), (out["watch"], out["error"]))

    def test_healthz_reads_the_record(self):
        self.d.channel_expires_at = None
        out = di.handle("GET", "/healthz", {}, b"", self.d)[1]
        self.assertEqual(("ok", dw.CHANNEL_SECONDS), (out["channel"], out["seconds_left"]))


def _bash():
    import shutil  # noqa: PLC0415
    for candidate in ("C:/Program Files/Git/bin/bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).exists() and "system32" not in str(candidate).lower():
            return candidate
    return None


class UnwatchTest(unittest.TestCase):
    """Aya on bddafa3: unwatch ignored a failed allUsers removal and still claimed the rollback."""

    def run_unwatch(self, members, policy_fails=False):
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        bash = _bash()
        if not bash:
            self.skipTest("no bash on this box")
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "gcloud"
            stub.write_text("#!/usr/bin/env bash\n"
                            "case \"$*\" in\n"
                            "  *get-iam-policy*) [ \"$FAKE_POLICY_FAILS\" = 1 ] && exit 1; echo \"$FAKE_MEMBERS\" ;;\n"
                            "  *remove-iam-policy-binding*) exit 1 ;;\n"
                            "  *) exit 0 ;;\n"
                            "esac\n", encoding="utf-8", newline="\n")
            stub.chmod(0o755)
            env = dict(__import__("os").environ, GCLOUD=stub.as_posix(), FAKE_MEMBERS=members,
                       FAKE_POLICY_FAILS="1" if policy_fails else "0")
            return subprocess.run([bash, "cloud/drive-intake/deploy.sh", "unwatch"], cwd=str(REPO), env=env,
                                  capture_output=True, text=True, timeout=60)

    def test_allUsers_still_present_is_a_failure(self):
        run = self.run_unwatch("serviceAccount:drive-intake-poller@sfdc24.iam.gserviceaccount.com;allUsers")
        self.assertNotEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertIn("FAILED: allUsers still holds run.invoker", run.stdout)

    def test_an_unreadable_policy_is_a_failure_not_a_success(self):
        run = self.run_unwatch("", policy_fails=True)
        self.assertNotEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertIn("FAILED: could not read", run.stdout)

    def test_allUsers_gone_is_the_read_back_success(self):
        run = self.run_unwatch("serviceAccount:drive-intake-poller@sfdc24.iam.gserviceaccount.com")
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertIn("allUsers removed (read back)", run.stdout)


class WiringTest(unittest.TestCase):
    def test_only_exactly_on_switches_the_watch_on(self):
        marker = object()
        for value in (None, "", "ON", "1", "true", " on"):
            env = {} if value is None else {"DRIVE_INTAKE_WATCH": value}
            self.assertIsNone(di.deps_from_env(env, watch=marker).watch, repr(value))
        on = di.deps_from_env({"DRIVE_INTAKE_WATCH": "on", "DRIVE_INTAKE_PUSH_URL": PUSH_URL}, watch=marker)
        self.assertIs(marker, on.watch)
        self.assertEqual(PUSH_URL, on.push_url)

    def test_the_image_carries_the_watch(self):
        dockerfile = (REPO / "cloud" / "drive-intake" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY cloud/drive-intake/drive_watch.py /app/drive_watch.py", dockerfile)


if __name__ == "__main__":
    unittest.main()

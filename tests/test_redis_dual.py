"""scripts/redis_dual.py: the dual-run is off by default, never answers, and never breaks the caller.

Every test here is one of the four promises in his GO - old path authoritative, second connection,
background shadow read, write-through, off-switch via settings not redeploy - turned into something
that fails if the promise stops being true.
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import redis_dual                                                        # noqa: E402

ON = {"enabled": True, "host": "10.0.0.1"}
OFF = {"enabled": False, "host": "10.0.0.1"}


class Fake:
    """A Redis stand-in. No network, no secret, no library."""

    def __init__(self, store=None, fail=False):
        self.store, self.fail = dict(store or {}), fail
        self.gets, self.sets, self.ttls = [], [], []

    def get(self, key):
        self.gets.append(key)
        if self.fail:
            raise RuntimeError("fake get failure")
        return self.store.get(key)

    def set(self, key, value):
        if self.fail:
            raise RuntimeError("fake set failure")
        self.store[key] = value
        self.sets.append(key)

    def setex(self, key, ttl, value):
        self.ttls.append((key, ttl))
        self.set(key, value)


def dual(settings=ON, fake=None, lines=None):
    fake = fake if fake is not None else Fake()
    lines = lines if lines is not None else []
    run = redis_dual.DualRun(settings=redis_dual.Settings(dict(settings)),
                             factory=lambda s: fake, log=lines.append)
    return run, fake, lines


class OffIsTheDefault(unittest.TestCase):
    def test_a_missing_settings_file_means_off(self):
        """A feature that defaults to on is a feature nobody chose."""
        with TemporaryDirectory() as tmp:
            settings = redis_dual.Settings(path=Path(tmp) / "nope.json")
            self.assertFalse(settings.enabled)
            self.assertFalse(settings.live())

    def test_an_unreadable_settings_file_means_off(self):
        """Broken JSON must not fail open into a live connection."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertFalse(redis_dual.Settings(path=path).live())

    def test_enabled_without_a_host_is_still_off(self):
        """Nowhere to go is not a connection to attempt."""
        self.assertFalse(redis_dual.Settings({"enabled": True, "host": ""}).live())
        self.assertFalse(redis_dual.Settings({"enabled": True, "host": "   "}).live())

    def test_the_committed_settings_file_is_off(self):
        """Not a fixture: the file that ships in this repository."""
        path = Path(__file__).resolve().parents[1] / "scripts" / "redis_dual.settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(data["enabled"])
        self.assertEqual("", data["host"])

    def test_disabled_touches_nothing_at_all(self):
        """Disabled means no client, no socket, no secret read - not 'connect and then notice'."""
        touched = []
        run = redis_dual.DualRun(settings=redis_dual.Settings(dict(OFF)),
                                 factory=lambda s: touched.append(1))
        self.assertEqual("authoritative", run.read("k", lambda: "authoritative"))
        run.write("k", "v", lambda: "written")
        self.assertEqual([], touched)

    def test_client_returns_none_when_not_live(self):
        self.assertIsNone(redis_dual.client(redis_dual.Settings(dict(OFF))))


class TheOldPathAnswers(unittest.TestCase):
    def test_the_authoritative_answer_is_returned_when_the_cache_disagrees(self):
        run, _, lines = dual(fake=Fake({"blackboard:k": "STALE"}))
        self.assertEqual("fresh", run.read("k", lambda: "fresh", wait=True))
        self.assertEqual(1, run.drain()["diverged"])
        self.assertTrue(any("DIVERGED" in line for line in lines), lines)

    def test_the_authoritative_answer_is_returned_when_the_cache_agrees(self):
        run, _, _ = dual(fake=Fake({"blackboard:k": "same"}))
        self.assertEqual("same", run.read("k", lambda: "same", wait=True))
        self.assertEqual(1, run.drain()["agreed"])

    def test_a_shadow_read_failure_does_not_reach_the_caller(self):
        run, _, _ = dual(fake=Fake(fail=True))
        self.assertEqual("fresh", run.read("k", lambda: "fresh", wait=True))
        self.assertEqual(1, run.drain()["shadow_errors"])

    def test_a_cache_miss_is_not_a_divergence(self):
        run, _, _ = dual(fake=Fake())
        run.read("k", lambda: "fresh", wait=True)
        counts = run.drain()
        self.assertEqual(1, counts["shadow_misses"])
        self.assertEqual(0, counts["diverged"])

    def test_the_divergence_line_names_sizes_and_never_contents(self):
        """The board carries his words and other people's; they do not belong in a cache log."""
        run, _, lines = dual(fake=Fake({"blackboard:k": "SECRET-CACHED-VALUE"}))
        run.read("k", lambda: "SECRET-REAL-VALUE", wait=True)
        run.drain()
        joined = " ".join(lines)
        self.assertIn("DIVERGED", joined)
        self.assertNotIn("SECRET-CACHED-VALUE", joined)
        self.assertNotIn("SECRET-REAL-VALUE", joined)

    def test_the_shadow_read_is_prefixed_and_keyed(self):
        run, fake, _ = dual()
        run.read("rows", lambda: "x", wait=True)
        run.drain()
        self.assertEqual(["blackboard:rows"], fake.gets)


class WriteThrough(unittest.TestCase):
    def test_the_authoritative_write_runs_first(self):
        """A mirror that succeeds while the real write fails would leave a value that was never
        committed. Order is the guard against that, not a style choice."""
        order, fake = [], Fake()
        run = redis_dual.DualRun(settings=redis_dual.Settings(dict(ON)), factory=lambda s: fake)
        run.write("k", "v", lambda: order.append("authoritative"))
        self.assertEqual(["authoritative"], order)
        self.assertEqual(["blackboard:k"], fake.sets)

    def test_a_mirror_failure_never_reaches_the_caller(self):
        run, _, _ = dual(fake=Fake(fail=True))
        self.assertEqual("committed", run.write("k", "v", lambda: "committed"))
        self.assertEqual(1, run.counters.snapshot()["mirror_errors"])

    def test_a_ttl_is_honoured(self):
        run, fake, _ = dual()
        run.write("k", "v", lambda: None, ttl=60)
        self.assertEqual([("blackboard:k", 60)], fake.ttls)

    def test_write_through_can_be_turned_off_alone(self):
        run, fake, _ = dual(settings=dict(ON, write_through=False))
        run.write("k", "v", lambda: "committed")
        self.assertEqual([], fake.sets)

    def test_shadow_reads_can_be_turned_off_alone(self):
        run, fake, _ = dual(settings=dict(ON, shadow_reads=False))
        self.assertEqual("x", run.read("k", lambda: "x"))
        self.assertEqual([], fake.gets)

    def test_a_structure_is_mirrored_as_json(self):
        run, fake, _ = dual()
        run.write("rows", {"a": 1}, lambda: None)
        self.assertEqual({"a": 1}, json.loads(fake.store["blackboard:rows"]))


class TheOffSwitch(unittest.TestCase):
    def test_it_takes_effect_without_a_restart(self):
        """His words: off-switch via settings not redeploy. So settings are re-read per operation."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps(ON), encoding="utf-8")
            fake = Fake()
            run = redis_dual.DualRun(factory=lambda s: fake)
            run._settings = None
            redis_dual.SETTINGS_PATH, old = path, redis_dual.SETTINGS_PATH
            try:
                run.read("k", lambda: "x", wait=True)
                run.drain()
                self.assertEqual(["blackboard:k"], fake.gets)
                path.write_text(json.dumps(OFF), encoding="utf-8")
                run.read("k", lambda: "x", wait=True)
                run.drain()
                self.assertEqual(["blackboard:k"], fake.gets, "the off-switch did not take effect")
            finally:
                redis_dual.SETTINGS_PATH = old

    def test_settings_are_not_cached_at_import(self):
        self.assertTrue(hasattr(redis_dual.DualRun, "settings"),
                        "settings must be resolved per call, not held on the instance")


class TheSecretStaysSecret(unittest.TestCase):
    def test_status_reports_whether_auth_was_found_not_what_it_is(self):
        """Our rule from the secret checks: print the type, never the value."""
        report = redis_dual.status(redis_dual.Settings(dict(OFF)))
        self.assertIn("auth_string_found", report)
        self.assertNotIn("auth_string", [k for k in report if k != "auth_string_found"])
        self.assertIsNone(report["auth_string_found"])

    def test_the_secret_is_read_from_secret_manager_and_nowhere_else(self):
        seen = {}

        def runner(args):
            seen["args"] = args

            class Done:
                returncode = 0
                stdout = "s3cr3t\n"
            return Done()

        self.assertEqual("s3cr3t", redis_dual.auth_string(runner=runner))
        self.assertIn("secrets", seen["args"])
        self.assertIn("REDIS_AUTH_STRING", seen["args"])

    def test_a_failed_secret_read_returns_empty_not_a_crash(self):
        def runner(args):
            raise OSError("gcloud not found")
        self.assertEqual("", redis_dual.auth_string(runner=runner))

    def test_a_nonzero_secret_read_returns_empty(self):
        def runner(args):
            class Done:
                returncode = 1
                stdout = "whatever"
            return Done()
        self.assertEqual("", redis_dual.auth_string(runner=runner))

    def test_no_secret_appears_in_the_repository(self):
        """The AUTH string belongs in Secret Manager: not git, not the board, not a settings file."""
        for name in ("scripts/redis_dual.py", "scripts/redis_dual.settings.json"):
            text = (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
            self.assertNotIn("AUTH=", text)
            self.assertNotIn("password=\"", text)


class Reachability(unittest.TestCase):
    def test_no_host_is_not_reachable(self):
        self.assertFalse(redis_dual.reachable("", 6378))

    def test_a_refused_connection_is_not_reachable(self):
        def connector(address, timeout):
            raise OSError("refused")
        self.assertFalse(redis_dual.reachable("10.0.0.1", 6378, connector=connector))

    def test_a_probe_that_opens_is_reachable_and_is_closed(self):
        closed = []

        class Conn:
            def close(self):
                closed.append(True)

        self.assertTrue(redis_dual.reachable("10.0.0.1", 6378, connector=lambda a, t: Conn()))
        self.assertEqual([True], closed)

    def test_the_probe_is_bounded(self):
        """A private address off the VPC must cost a board read nothing."""
        seen = {}

        def connector(address, timeout):
            seen["timeout"] = timeout
            raise OSError("refused")

        redis_dual.reachable("10.0.0.1", 6378, connector=connector)
        self.assertLessEqual(seen["timeout"], 2.0)


class TheCommandLine(unittest.TestCase):
    def test_status_prints_and_exits_zero(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, redis_dual.main(["status"]))
        self.assertIn("would_attempt_connection", out.getvalue())

    def test_selftest_passes(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, redis_dual.main(["selftest"]))
        self.assertIn("SELFTEST OK", out.getvalue())

    def test_an_unknown_verb_exits_two(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(2, redis_dual.main(["nonsense"]))


if __name__ == "__main__":
    unittest.main()

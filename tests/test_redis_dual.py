"""scripts/redis_dual.py: the dual-run is off by default, never answers, and never breaks the caller.

Every test here is one of the four promises in his GO - old path authoritative, second connection,
background shadow read, write-through, off-switch via settings not redeploy - turned into something
that fails if the promise stops being true.
"""
import atexit
import io
import json
import os
import shutil
import ssl
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import redis_dual                                                        # noqa: E402

def KEY(name):
    """A key as the code builds it. Never the literal: the prefix carries a VERSION now, and a test
    that hardcodes it would have to be edited on every migration - which is the thing the version
    exists to make cheap."""
    return redis_dual.Settings.DEFAULTS["key_prefix"] + name


# Both switches, because they are separate now: `enabled` is the dual-run, `connect` is
# permission to open a socket at all. A fixture that set only one would be testing a
# configuration no deployed job has.
ON = {"enabled": True, "connect": True, "host": "10.0.0.1"}
OFF = {"enabled": False, "connect": False, "host": "10.0.0.1"}
CONNECT_ONLY = {"enabled": False, "connect": True, "host": "10.0.0.1"}


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
        self.assertFalse(data["enabled"], "the DUAL-RUN ships off")
        self.assertEqual("", data["host"], "this repository is public")
        # connect ships ON so the deliberate jobs - probe, reconciler, request worker, viewer - work
        # without asserting the dual-run's switch. That is safe on its own: with no host committed,
        # connect=true authorises a connection to nowhere.
        self.assertTrue(data["connect"])

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


class TheEnvironmentOverrides(unittest.TestCase):
    """This repository is PUBLIC, so the instance address arrives as REDIS_HOST and is committed
    nowhere. The process environment wins over the file, the same contract as bus.load_env."""

    def test_the_host_comes_from_the_environment(self):
        """The ADDRESS still comes from the environment - that part was always right, and is why
        nothing private is committed. What changed is that supplying it no longer switches anything
        on by itself."""
        s = redis_dual.Settings(environ={"REDIS_HOST": "10.1.2.3", "REDIS_DUAL_ENABLED": "true"})
        self.assertEqual("10.1.2.3", s.host)

    def test_the_environment_cannot_switch_the_dual_run_on_by_itself(self):
        """THE BLOCKER, written as the test that was missing. This assertion used to read
        assertTrue, which is the defeated off-switch recorded as a guarantee: the deployed
        bus-requests job carries REDIS_DUAL_ENABLED=true, so a file edit could never have stopped
        it. The file is the authority; the environment can only agree with it."""
        s = redis_dual.Settings({"enabled": False, "host": "10.0.0.1"},
                                environ={"REDIS_DUAL_ENABLED": "true"})
        self.assertFalse(s.live())

    def test_a_missing_file_vetoes_environment_enablement(self):
        with TemporaryDirectory() as tmp:
            s = redis_dual.Settings(path=Path(tmp) / "nope.json",
                                    environ={"REDIS_DUAL_ENABLED": "true",
                                             "REDIS_CONNECT": "true",
                                             "REDIS_HOST": "10.0.0.1"})
            self.assertFalse(s.file_ok)
            self.assertFalse(s.live())
            self.assertFalse(s.connect_ok())

    def test_a_malformed_file_vetoes_environment_enablement(self):
        """Broken JSON is not a file that said nothing. It is a file that can authorise nothing."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("{not json", encoding="utf-8")
            s = redis_dual.Settings(path=path,
                                    environ={"REDIS_DUAL_ENABLED": "true",
                                             "REDIS_CONNECT": "true",
                                             "REDIS_HOST": "10.0.0.1"})
            self.assertFalse(s.live())
            self.assertFalse(s.connect_ok())

    def test_a_file_listing_something_other_than_an_object_vetoes_too(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            s = redis_dual.Settings(path=path, environ={"REDIS_CONNECT": "true",
                                                        "REDIS_HOST": "10.0.0.1"})
            self.assertFalse(s.connect_ok())

    def test_turning_the_dual_run_off_leaves_diagnostics_able_to_connect(self):
        """The reason the two switches exist. A probe or a reconciler run is a measurement, not a
        dual-run, and it must not have to assert the dual-run's switch to open a socket."""
        s = redis_dual.Settings(dict(CONNECT_ONLY))
        self.assertFalse(s.live())
        self.assertTrue(s.connect_ok())

    def test_turning_connect_off_stops_every_connection_including_diagnostics(self):
        s = redis_dual.Settings({"enabled": True, "connect": False, "host": "10.0.0.1"},
                                environ={"REDIS_CONNECT": "true"})
        self.assertFalse(s.connect_ok())
        self.assertIsNone(redis_dual.client(s, precheck=False))

    def test_a_host_alone_does_not_turn_it_on(self):
        """Supplying an address is not consent to use it."""
        self.assertFalse(redis_dual.Settings(environ={"REDIS_HOST": "10.1.2.3"}).live())

    def test_enabled_alone_does_not_turn_it_on(self):
        self.assertFalse(redis_dual.Settings(environ={"REDIS_DUAL_ENABLED": "true"}).live())

    def test_an_unparseable_override_is_ignored_and_never_fails_open(self):
        env = {"REDIS_HOST": "10.0.0.1", "REDIS_DUAL_ENABLED": "true", "REDIS_PORT": "not-a-number"}
        self.assertEqual(redis_dual.DEFAULT_PORT, redis_dual.Settings(environ=env).port)

    def test_an_empty_override_is_not_an_override(self):
        """An empty REDIS_HOST must not look like a configured one."""
        s = redis_dual.Settings({"host": "from-file", "enabled": True}, environ={"REDIS_HOST": ""})
        self.assertEqual("from-file", s.host)

    def test_a_falsy_enabled_override_turns_it_off(self):
        s = redis_dual.Settings({"enabled": True, "host": "10.0.0.1"},
                                environ={"REDIS_DUAL_ENABLED": "false"})
        self.assertFalse(s.live())

    def test_no_private_address_is_committed_anywhere(self):
        """The whole point of the override: nothing in this repo names a real instance.

        The forbidden addresses are ASSEMBLED FROM OCTETS, not written out, for two reasons. This file
        is scanned too - it caught its own fixture the first time it ran, which is the guard working on
        the author - and a guard that has to spell the thing it forbids cannot scan itself."""
        forbidden = (".".join(("10", "54", "72", "180")),      # us-central1, redis-central
                     ".".join(("10", "54", "126", "124")))     # us-east4, redis-instance
        root = Path(__file__).resolve().parents[1]
        for name in ("scripts/redis_dual.py", "scripts/redis_dual.settings.json",
                     "tests/test_redis_dual.py"):
            text = (root / name).read_text(encoding="utf-8")
            for address in forbidden:
                self.assertNotIn(address, text, name)
        committed = json.loads((root / "scripts" / "redis_dual.settings.json").read_text(encoding="utf-8"))
        self.assertEqual("", committed["host"])


class EveryKeyCarriesTheVersion(unittest.TestCase):
    """Gemini, architect lead, 2026-10-06 15:21:43Z: implement the v1 prefix immediately, because
    schemas always change and without a version namespace a migration forces downtime or key
    collisions. I had applied the dual-run philosophy to every store EXCEPT the keyspace itself."""

    def test_the_default_prefix_is_versioned(self):
        self.assertTrue(redis_dual.Settings.DEFAULTS["key_prefix"].startswith(redis_dual.KEY_VERSION))

    def test_the_shipped_settings_file_is_versioned(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "redis_dual.settings.json"
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["key_prefix"]
                        .startswith(redis_dual.KEY_VERSION))

    def test_every_key_this_fleet_writes_is_versioned(self):
        """One assertion over every module that names a key, so a new one cannot forget."""
        import sys as _sys
        root = Path(__file__).resolve().parents[1]
        _sys.path.insert(0, str(root / "cloud" / "bus-reconciler"))
        import bus_reconcile, bus_request                                # noqa: PLC0415
        keys = [bus_reconcile.ROW_KEY, bus_reconcile.INDEX_KEY, bus_reconcile.COMPARE_KEY,
                bus_request.PROBE_NAMESPACE, redis_dual.Settings.DEFAULTS["key_prefix"]]
        for key in keys:
            self.assertTrue(key.startswith(redis_dual.KEY_VERSION), key)

    def test_a_shadow_read_uses_the_versioned_key(self):
        run, fake, _ = dual()
        run.read("rows", lambda: "x", wait=True)
        run.drain()
        self.assertEqual([redis_dual.Settings.DEFAULTS["key_prefix"] + "rows"], fake.gets)


class TheOldPathAnswers(unittest.TestCase):
    def test_the_authoritative_answer_is_returned_when_the_cache_disagrees(self):
        run, _, lines = dual(fake=Fake({KEY("k"): "STALE"}))
        self.assertEqual("fresh", run.read("k", lambda: "fresh", wait=True))
        self.assertEqual(1, run.drain()["diverged"])
        self.assertTrue(any("DIVERGED" in line for line in lines), lines)

    def test_the_authoritative_answer_is_returned_when_the_cache_agrees(self):
        run, _, _ = dual(fake=Fake({KEY("k"): "same"}))
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
        run, _, lines = dual(fake=Fake({KEY("k"): "SECRET-CACHED-VALUE"}))
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
        self.assertEqual([KEY("rows")], fake.gets)


class WriteThrough(unittest.TestCase):
    def test_the_authoritative_write_runs_first(self):
        """A mirror that succeeds while the real write fails would leave a value that was never
        committed. Order is the guard against that, not a style choice."""
        order, fake = [], Fake()
        run = redis_dual.DualRun(settings=redis_dual.Settings(dict(ON)), factory=lambda s: fake)
        run.write("k", "v", lambda: order.append("authoritative"))
        self.assertEqual(["authoritative"], order)
        self.assertEqual([KEY("k")], fake.sets)

    def test_a_mirror_failure_never_reaches_the_caller(self):
        run, _, _ = dual(fake=Fake(fail=True))
        self.assertEqual("committed", run.write("k", "v", lambda: "committed"))
        self.assertEqual(1, run.counters.snapshot()["mirror_errors"])

    def test_a_ttl_is_honoured(self):
        run, fake, _ = dual()
        run.write("k", "v", lambda: None, ttl=60)
        self.assertEqual([(KEY("k"), 60)], fake.ttls)

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
        self.assertEqual({"a": 1}, json.loads(fake.store[KEY("rows")]))


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
                self.assertEqual([KEY("k")], fake.gets)
                path.write_text(json.dumps(OFF), encoding="utf-8")
                run.read("k", lambda: "x", wait=True)
                run.drain()
                self.assertEqual([KEY("k")], fake.gets, "the off-switch did not take effect")
            finally:
                redis_dual.SETTINGS_PATH = old

    def test_settings_are_not_cached_at_import(self):
        self.assertTrue(hasattr(redis_dual.DualRun, "settings"),
                        "settings must be resolved per call, not held on the instance")


class ThePrecheckIsNotTheVerdict(unittest.TestCase):
    """A 1.5-second TCP pre-check from a cold gen2 container said "unreachable" and made the
    connectivity probe report FAIL on a path that had worked seconds earlier from an identically
    configured job. A latency guard is not a reachability verdict."""

    def test_the_precheck_can_be_skipped(self):
        """A job whose whole purpose is to connect must let the connect be the answer."""
        made = []
        settings = redis_dual.Settings(dict(ON))
        got = redis_dual.client(settings, factory=lambda s: made.append(1) or "conn", precheck=False)
        self.assertEqual("conn", got)
        self.assertEqual([1], made)

    def test_the_connect_timeout_is_longer_than_the_precheck(self):
        """A connection that is actually wanted should not inherit a hot path's impatience."""
        self.assertGreater(redis_dual.CONNECT_SECONDS, redis_dual.PROBE_SECONDS)

    def test_the_precheck_window_is_configurable(self):
        """1.5 s was chosen for a laptop and is used from a cold container."""
        self.assertIn("REDIS_PROBE_SECONDS", (Path(__file__).resolve().parents[1]
                                              / "scripts" / "redis_dual.py").read_text(encoding="utf-8"))

    def test_off_still_means_off_even_without_the_precheck(self):
        """Skipping the guard must not skip the switch."""
        touched = []
        self.assertIsNone(redis_dual.client(redis_dual.Settings(dict(OFF)),
                                            factory=lambda s: touched.append(1), precheck=False))
        self.assertEqual([], touched)


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


class TheStatusKeysAreAnInterface(unittest.TestCase):
    """Three Cloud Run entrypoints read fixed key names out of status(). Renaming one of them -
    `enabled` to `enabled_in_file` - broke all three with a KeyError at STARTUP, and every suite
    still passed, because they all exercise status() directly and none tested what the entrypoints
    READ out of it. The re-prove execution found it. A dict lookup across a module boundary is an
    interface, and this is the test that was missing for it."""

    def test_every_key_an_entrypoint_logs_exists(self):
        report = redis_dual.status(redis_dual.Settings(dict(OFF)))
        for name in redis_dual.STATUS_FOR_LOG:
            self.assertIn(name, report)

    def test_the_entrypoints_name_no_key_of_their_own(self):
        """They must spell the set once, in redis_dual, or this test is the only thing standing
        between a rename and another startup KeyError in production."""
        root = Path(__file__).resolve().parents[1]
        for rel in ("cloud/bus-reconciler/serve_requests.py", "cloud/bus-reconciler/main.py",
                    "cloud/bus-reconciler/probe.py"):
            source = (root / rel).read_text(encoding="utf-8")
            if "status[k]" in source:
                self.assertIn("redis_dual.STATUS_FOR_LOG", source, rel)

    def test_the_status_report_distinguishes_vetoed_from_simply_off(self):
        """The field that makes the off-switch fix visible: with the environment asserting the
        dual-run and the file refusing it, a reader must be able to see both facts at once."""
        report = redis_dual.status(redis_dual.Settings({"enabled": False, "connect": True,
                                                        "host": "10.0.0.1"},
                                                       environ={"REDIS_DUAL_ENABLED": "true"}))
        self.assertFalse(report["dual_run_live"])
        self.assertTrue(report["connect_permitted"])
        self.assertTrue(report["settings_file_readable"])


class TheDiagnosticCannotBecomeTheFailure(unittest.TestCase):
    """Copilot, PR 323, two findings about the observer harming what it observes."""

    def test_a_logger_that_raises_does_not_fail_a_committed_write(self):
        """The sharp version: the authoritative write COMMITTED, the mirror failed, and the log call
        about the mirror raised - so write() raised and the caller would retry a write that had
        already landed. A diagnostic turning one success into a duplicate."""
        def explode(_line):
            raise OSError("the log device is full")

        run = redis_dual.DualRun(settings=redis_dual.Settings(dict(ON)),
                                 factory=lambda s: Fake(fail=True), log=explode)
        self.assertEqual("committed", run.write("k", "v", lambda: "committed"))
        counts = run.counters.snapshot()
        self.assertEqual(1, counts["mirror_errors"])
        self.assertEqual(1, counts["log_errors"], "the logging failure must be counted, not hidden")

    def test_a_logger_that_raises_does_not_fail_a_read(self):
        def explode(_line):
            raise OSError("the log device is full")

        run = redis_dual.DualRun(settings=redis_dual.Settings(dict(ON)),
                                 factory=lambda s: Fake({KEY("k"): "STALE"}), log=explode)
        self.assertEqual("fresh", run.read("k", lambda: "fresh", wait=True))
        run.drain()
        self.assertEqual(1, run.counters.snapshot()["diverged"])

    def test_shadow_workers_are_bounded_and_reaped(self):
        """20 enabled reads used to leave 20 Thread objects, because only drain() ever removed one.
        Finished workers are reaped on admission now, so the list cannot grow with the read count."""
        run = redis_dual.DualRun(settings=redis_dual.Settings(dict(ON)),
                                 factory=lambda s: Fake({KEY("k"): "fresh"}))
        for _ in range(20):
            self.assertEqual("fresh", run.read("k", lambda: "fresh", wait=True))
        run.drain()
        self.assertLessEqual(len(run.threads), run.max_shadows)
        self.assertEqual(20, run.counters.snapshot()["shadow_reads"])

    def test_over_the_ceiling_a_shadow_is_shed_and_the_answer_still_returns(self):
        """Admission is NON-BLOCKING: past the ceiling the shadow is skipped and counted. The
        authoritative answer must never queue behind a cache that is already struggling."""
        release = threading.Event()

        class Slow(Fake):
            def get(self, key):
                release.wait(timeout=5)
                return super().get(key)

        run = redis_dual.DualRun(settings=redis_dual.Settings(dict(ON)),
                                 factory=lambda s: Slow({KEY("k"): "fresh"}), max_shadows=2)
        try:
            for _ in range(6):
                self.assertEqual("fresh", run.read("k", lambda: "fresh"))
            self.assertGreaterEqual(run.counters.snapshot()["shadow_shed"], 1)
            self.assertLessEqual(len(run.threads), 2)
        finally:
            release.set()
            run.drain()

    def test_a_worker_that_cannot_start_returns_the_authoritative_answer(self):
        """thread.start() raising RuntimeError when the process is out of threads must not escape
        into the caller: the answer it asked for is already in hand."""
        run = redis_dual.DualRun(settings=redis_dual.Settings(dict(ON)),
                                 factory=lambda s: Fake({KEY("k"): "fresh"}))

        class Unstartable:
            def start(self):
                raise RuntimeError("can't start new thread")

            def is_alive(self):
                return False

            def join(self, timeout=None):
                return None

        original = redis_dual.threading.Thread
        redis_dual.threading.Thread = lambda *a, **k: Unstartable()
        try:
            self.assertEqual("fresh", run.read("k", lambda: "fresh"))
        finally:
            redis_dual.threading.Thread = original
        self.assertEqual([], run.threads, "a worker that never started must not be tracked")
        self.assertEqual(1, run.counters.snapshot()["shadow_errors"])



# ---------------------------------------------------------------- TLS trust (Cursor, PR 340)

# A throwaway self-signed certificate made for these tests; its key was discarded. It stands in for
# the instance CA that Cloud Run mounts. Not an authority anything trusts. Inline rather than a
# fixture file because *.pem is gitignored here, and that guard is worth keeping.
TEST_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIDSzCCAjOgAwIBAgIUF8AEBr6C21aPBNjnzllEUMWV5rIwDQYJKoZIhvcNAQEL
BQAwNDEyMDAGA1UEAwwpcmVkaXMtZHVhbCB0ZXN0IENBIChub3QgYSByZWFsIGF1
dGhvcml0eSkwIBcNMjYxMDA4MjIyOTI0WhgPMjEyNjA5MTQyMjI5MjRaMDQxMjAw
BgNVBAMMKXJlZGlzLWR1YWwgdGVzdCBDQSAobm90IGEgcmVhbCBhdXRob3JpdHkp
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvp7oa98H10ouLe15D9b5
yu4oqJctg8z++3SYUzRgewpVUfd3tSqhpIB8TTOrSY9L4JBRuFr6RkGU60q29GzM
T3qOUyNXuacxd+npyKMKeBfq6L4HZ2VgAhRL9zXK8w+EdNn8K7VjbJlvttS7zGkb
DTi2P3NU2mXZrEop3z1nlRqr2cOjeCq4I5Imtn3jfODzjJUwbFUzPMUmxKB1Rg+F
2jCqJ+IwoMxgievcDX5pWngTRxCDqH0GNfLABJlU8kHiFALqPHP2lOgi8AQW2HhW
f2LyXej3V4bvy3ZuiohVr3lQ6AEtoaflEd/0Dark4yiycmBE+966JCiPXi8RxHUT
PQIDAQABo1MwUTAdBgNVHQ4EFgQUyZVD3l6jvHH7Q9S9bXX38oe2URowHwYDVR0j
BBgwFoAUyZVD3l6jvHH7Q9S9bXX38oe2URowDwYDVR0TAQH/BAUwAwEB/zANBgkq
hkiG9w0BAQsFAAOCAQEAkhtqS7VnYkLQW7knbuh/GHYx7Vp0ihIo6Nj84hfzvsxC
BUX0x5y0kxF33KLgooxM3ZJNeY4anrB0vjSiJtoPQTrhnreCEo3pEzMDuK13QfQ6
eyiXStxT1SiaFjUcJCXOFNQ298IL4GlCXkOLiYkHbpHgWaFuKoqONJAhrC57rm/s
X2bYkaqlyHo7R8kAPWHIo40EEOBqHm39BFUMYUJ8mE1wCegzyBiqtaJbvohtOtP7
6CaYoofcBpaHPlH4R7rbOF1QkZKq+KIPEI4AVSYV11msbUgZdR7oFRELNUUco3f1
swPgFp5/IDYokSXKt91nIMmCmWiCSJtoRyMGZKSOtw==
-----END CERTIFICATE-----
"""
_TEST_CA_DIR = tempfile.mkdtemp(prefix="redis-dual-test-ca-")
atexit.register(shutil.rmtree, _TEST_CA_DIR, True)
TEST_CA = Path(_TEST_CA_DIR) / "redis-ca.pem"
TEST_CA.write_text(TEST_CA_PEM, encoding="ascii")
TEST_CA_CN = "redis-dual test CA (not a real authority)"


class TrustRecorder:
    """Records what a TLS handshake WOULD trust, without a socket.

    Patches ssl so that loading the platform's default roots is RECORDED (and not done), and so that
    wrap_socket hands back the context it was called on instead of shaking hands. A context built
    the way redis-py 5.3.1 builds it - create_default_context() and then the CA added on top - shows
    up here as a default-trust load; a context built from nothing does not."""

    def __init__(self):
        self.default_loads, self.contexts = [], []

    def __enter__(self):
        recorder = self

        def load_default_certs(ctx, *a, **k):
            recorder.default_loads.append("load_default_certs")

        def set_default_verify_paths(ctx, *a, **k):
            recorder.default_loads.append("set_default_verify_paths")

        def wrap_socket(ctx, sock, server_hostname=None, **k):
            recorder.contexts.append((ctx, server_hostname))
            return "wrapped"

        self.patches = [mock.patch.object(ssl.SSLContext, "load_default_certs", load_default_certs),
                        mock.patch.object(ssl.SSLContext, "set_default_verify_paths",
                                          set_default_verify_paths),
                        mock.patch.object(ssl.SSLContext, "wrap_socket", wrap_socket)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        return False


def trusted_subjects(ctx):
    """The CN of every CA a context trusts, as loaded into it."""
    names = []
    for cert in ctx.get_ca_certs():
        for rdn in cert.get("subject", ()):
            for key, value in rdn:
                if key == "commonName":
                    names.append(value)
    return names


def fake_redis_module():
    """redis-py's shape, small enough to read: Redis builds a pool, the pool builds connections, and
    SSLConnection._wrap_socket_with_ssl is redis-py 5.3.1's own trust logic - the default context
    with the CA ADDED. That is what makes the old client() fail these tests. The real library is
    exercised in tests/test_redis_tool_bridge.py, where CI installs redis==5.3.1."""
    redis = types.ModuleType("redis")
    connection = types.ModuleType("redis.connection")

    class Connection:
        def __init__(self, host="", **kwargs):
            self.host, self.kwargs = host, kwargs

    class SSLConnection(Connection):
        def __init__(self, ssl_cert_reqs="required", ssl_ca_certs=None, ssl_check_hostname=False,
                     **kwargs):
            super().__init__(**kwargs)
            self.cert_reqs = {"required": ssl.CERT_REQUIRED, "none": ssl.CERT_NONE}[ssl_cert_reqs]
            self.ca_certs, self.check_hostname = ssl_ca_certs, ssl_check_hostname

        def _wrap_socket_with_ssl(self, sock):
            context = ssl.create_default_context()
            context.check_hostname = self.check_hostname
            context.verify_mode = self.cert_reqs
            if self.ca_certs is not None:
                context.load_verify_locations(cafile=self.ca_certs)
            return context.wrap_socket(sock, server_hostname=self.host)

    class ConnectionPool:
        def __init__(self, connection_class=Connection, **kwargs):
            self.connection_class, self.connection_kwargs = connection_class, kwargs

        def make_connection(self):
            return self.connection_class(**self.connection_kwargs)

    class Redis:
        def __init__(self, ssl=False, **kwargs):
            self.kwargs = dict(kwargs, ssl=ssl)
            self.connection_pool = ConnectionPool(
                connection_class=SSLConnection if ssl else Connection, **kwargs)

    connection.Connection, connection.SSLConnection = Connection, SSLConnection
    redis.Redis, redis.ConnectionPool, redis.connection = Redis, ConnectionPool, connection
    return redis


def connect_with(redis_module, settings=None, environ=None):
    """redis_dual.client() against `redis_module`, with a throwaway auth file and a clean switch."""
    settings = settings or redis_dual.Settings(dict(CONNECT_ONLY, ca_cert_path=str(TEST_CA)),
                                               environ={})
    with TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth"
        auth.write_text("not-a-real-auth-string", encoding="utf-8")
        env = dict(environ or {}, REDIS_AUTH_FILE=str(auth))
        modules = {"redis": redis_module, "redis.connection": redis_module.connection}
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, modules):
            if "REDIS_TLS_CHECK_HOSTNAME" not in env:
                os.environ.pop("REDIS_TLS_CHECK_HOSTNAME", None)
            return redis_dual.client(settings, precheck=False)


def first_handshake(test, conn):
    """What the first connection the pool makes would trust. No socket is opened."""
    with TrustRecorder() as seen:
        test.assertEqual("wrapped",
                         conn.connection_pool.make_connection()._wrap_socket_with_ssl(object()))
    test.assertEqual(1, len(seen.contexts))
    return seen, seen.contexts[0][0], seen.contexts[0][1]


class TheMountedCAIsTheWholeTrustStore(unittest.TestCase):
    """Cursor, PR 340, FAIL on 36479378: client() let redis-py start from the PUBLIC CA bundle and
    add the mounted CA to it, with hostname matching off - so any certificate chained to any public
    root, for any name, completed the handshake the AUTH string is sent over."""

    def test_no_default_roots_are_loaded_only_the_mounted_ca(self):
        seen, ctx, server_hostname = first_handshake(self, connect_with(fake_redis_module()))
        self.assertEqual([], seen.default_loads,
                         "the public CA bundle was loaded: the mounted CA must REPLACE it")
        self.assertEqual([TEST_CA_CN], trusted_subjects(ctx))
        self.assertEqual(ssl.CERT_REQUIRED, ctx.verify_mode)
        self.assertEqual("10.0.0.1", server_hostname)

    def test_hostname_matching_is_off_by_default_so_the_live_jobs_keep_working(self):
        conn = connect_with(fake_redis_module())
        self.assertIs(False, conn.kwargs["ssl_check_hostname"])
        _, ctx, _ = first_handshake(self, conn)
        self.assertFalse(ctx.check_hostname)

    def test_the_switch_turns_hostname_matching_on(self):
        conn = connect_with(fake_redis_module(), environ={"REDIS_TLS_CHECK_HOSTNAME": "true"})
        self.assertIs(True, conn.kwargs["ssl_check_hostname"])
        seen, ctx, _ = first_handshake(self, conn)
        self.assertTrue(ctx.check_hostname)
        self.assertEqual([], seen.default_loads)

    def test_a_garbled_switch_is_off(self):
        for value in ("", "maybe", "0", "false", "off"):
            self.assertFalse(redis_dual.check_hostname_on({"REDIS_TLS_CHECK_HOSTNAME": value}),
                             value)
        self.assertTrue(redis_dual.check_hostname_on({"REDIS_TLS_CHECK_HOSTNAME": " ON "}))

    def test_without_a_ca_path_the_library_default_is_unchanged(self):
        """Not this fix's scope, and not a silent change for a caller that never configured a CA."""
        module = fake_redis_module()
        conn = connect_with(module, settings=redis_dual.Settings(dict(CONNECT_ONLY), environ={}))
        self.assertIs(module.connection.SSLConnection, conn.connection_pool.connection_class)
        self.assertNotIn("ssl_ca_certs", conn.kwargs)

    def test_the_other_kwargs_are_what_they_were(self):
        """client() is shared with bus-requests, milestones-sync and the reconciler."""
        conn = connect_with(fake_redis_module())
        for key, value in (("host", "10.0.0.1"), ("port", redis_dual.DEFAULT_PORT), ("ssl", True),
                           ("ssl_cert_reqs", "required"), ("ssl_ca_certs", str(TEST_CA)),
                           ("password", "not-a-real-auth-string"), ("decode_responses", True)):
            self.assertEqual(value, conn.kwargs[key], key)

    def test_no_ca_path_is_an_error_not_a_fall_back_to_the_defaults(self):
        with self.assertRaises(ValueError):
            redis_dual.private_ca_context("")

    def test_the_context_alone_trusts_one_ca(self):
        with TrustRecorder() as seen:
            ctx = redis_dual.private_ca_context(str(TEST_CA))
        self.assertEqual([], seen.default_loads)
        self.assertEqual([TEST_CA_CN], trusted_subjects(ctx))
        self.assertEqual(ssl.CERT_REQUIRED, ctx.verify_mode)
        self.assertFalse(ctx.check_hostname)
        self.assertTrue(redis_dual.private_ca_context(str(TEST_CA), True).check_hostname)

    def test_the_recorder_sees_the_old_shape(self):
        """The control: the library's own default-plus-CA context IS caught by the recorder, so an
        empty default_loads above means something."""
        with TrustRecorder() as seen:
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cafile=str(TEST_CA))
        self.assertTrue(seen.default_loads)


if __name__ == "__main__":
    unittest.main()

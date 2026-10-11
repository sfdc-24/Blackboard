"""serve_requests live mode: a meeting's side conversation answered every few seconds, bounded."""
import datetime
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "cloud" / "bus-reconciler"))

import serve_requests as sr                                               # noqa: E402


class LockRedis:
    """Just enough Redis for the worker lock: SET NX/EX, GET, DELETE. TTLs are not modelled."""

    def __init__(self, held=None):
        self.data = {} if held is None else {sr.WORKER_LOCK_KEY: held}
        self.sets, self.evals = [], []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        self.sets.append((key, value, nx, ex))
        return True

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)

    def eval(self, script, numkeys, key, me, *args):
        """The two Lua scripts, each atomic as it is on the server: compare, then write."""
        self.evals.append(script)
        if script == sr.RENEW_LUA:
            if self.data.get(key) != me:
                return None
            self.data[key] = me
            self.sets.append((key, me, False, int(args[0])))
            return "OK"
        if script == sr.DROP_LUA:
            if self.data.get(key) != me:
                return 0
            self.data.pop(key, None)
            return 1
        raise AssertionError("unexpected script")


def frozen_clock(start):
    clock = {"t": start}

    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"]

    def sleep(seconds):
        clock["t"] = clock["t"] + datetime.timedelta(seconds=seconds)
    return clock, FakeDT, sleep


START = datetime.datetime(2026, 10, 11, 22, 0, tzinfo=datetime.timezone.utc)


class OneWorkerAtATime(unittest.TestCase):
    """2026-10-11: bus-requests-27gvw started beside bus-requests-kdn8k; both answered one request and
    both exited 2 on the duplicate row, and the side channel had no worker from 22:15Z."""

    def run_main(self, conn, env, one_pass):
        clock, FakeDT, sleep = frozen_clock(START)
        with mock.patch.object(sr.datetime, "datetime", FakeDT), \
                mock.patch.object(sr, "one_pass", side_effect=one_pass), \
                mock.patch.object(sr.time, "sleep", side_effect=sleep), \
                mock.patch.object(sr.redis_dual, "client", return_value=conn), \
                mock.patch.dict(sr.os.environ, env), \
                mock.patch("bus.load_env", return_value={}):
            return sr.main()

    def test_a_second_live_worker_does_not_run_while_the_first_holds_the_lock(self):
        passes = []
        conn = LockRedis(held="bus-requests-kdn8k")
        env = {"LOOP_UNTIL": "2026-10-11T22:02:00Z", "CLOUD_RUN_EXECUTION": "bus-requests-27gvw"}
        self.assertEqual(0, self.run_main(conn, env, lambda *a, **k: passes.append(1)))
        self.assertEqual([], passes, "no pass at all: the holder is answering")
        self.assertEqual("bus-requests-kdn8k", conn.get(sr.WORKER_LOCK_KEY), "and its lock is left alone")

    def test_a_one_pass_run_does_not_run_beside_a_live_worker(self):
        passes = []
        conn = LockRedis(held="bus-requests-kdn8k")
        env = {"LOOP_UNTIL": "", "CLOUD_RUN_EXECUTION": "bus-requests-once"}
        self.assertEqual(0, self.run_main(conn, env, lambda *a, **k: passes.append(1)))
        self.assertEqual([], passes)

    def test_the_lock_is_taken_renewed_and_released(self):
        passes = []
        conn = LockRedis()
        env = {"LOOP_UNTIL": "2026-10-11T22:01:00Z", "LOOP_INTERVAL": "10", "CLOUD_RUN_EXECUTION": "bus-requests-a"}
        self.assertEqual(0, self.run_main(conn, env, lambda *a, **k: passes.append(1)))
        self.assertEqual(6, len(passes))
        mine = [s for s in conn.sets if s[0] == sr.WORKER_LOCK_KEY]
        self.assertTrue(mine[0][2], "taken with NX")
        self.assertTrue(all(ex >= sr.LIVE_LOCK_SECONDS for _, _, _, ex in mine), "every write carries a TTL")
        self.assertIsNone(conn.get(sr.WORKER_LOCK_KEY), "released when the loop ends")

    def test_a_retried_task_of_the_same_execution_takes_its_lock_back(self):
        passes = []
        conn = LockRedis(held="bus-requests-a")
        env = {"LOOP_UNTIL": "", "CLOUD_RUN_EXECUTION": "bus-requests-a"}
        self.assertEqual(0, self.run_main(conn, env, lambda *a, **k: passes.append(1)))
        self.assertEqual([1], passes)

    def test_a_worker_that_loses_the_lock_stops(self):
        conn = LockRedis()
        passes = []

        def pass_then_lose(*a, **k):
            passes.append(1)
            if len(passes) == 2:
                conn.data[sr.WORKER_LOCK_KEY] = "bus-requests-b"     # expired and taken by another
        env = {"LOOP_UNTIL": "2026-10-11T22:05:00Z", "CLOUD_RUN_EXECUTION": "bus-requests-a"}
        self.assertEqual(0, self.run_main(conn, env, pass_then_lose))
        self.assertEqual(2, len(passes))
        self.assertEqual("bus-requests-b", conn.get(sr.WORKER_LOCK_KEY), "the new holder's lock is not dropped")

    def test_an_unconfirmed_append_ends_the_pass_not_the_live_channel(self):
        calls = []

        def duplicate_row(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise SystemExit(2)                                # append.py: two copies read back
        env = {"LOOP_UNTIL": "2026-10-11T22:01:00Z", "LOOP_INTERVAL": "10", "CLOUD_RUN_EXECUTION": "bus-requests-a"}
        self.assertEqual(0, self.run_main(LockRedis(), env, duplicate_row))
        self.assertEqual(6, len(calls), "every pass after the bad one still ran")

    # --- Cursor and Copilot on 6dc6f86 ---

    def test_renewal_is_one_compare_and_set_never_a_get_then_set(self):
        conn = LockRedis(held="bus-requests-a")
        self.assertTrue(sr.keep_lock(conn, "bus-requests-a", 300))
        self.assertEqual([sr.RENEW_LUA], conn.evals)
        conn.data[sr.WORKER_LOCK_KEY] = "bus-requests-b"            # expired and taken in between
        self.assertFalse(sr.keep_lock(conn, "bus-requests-a", 300))
        self.assertEqual("bus-requests-b", conn.get(sr.WORKER_LOCK_KEY), "the new holder is not overwritten")

    def test_an_expired_lock_nobody_took_is_taken_back_with_nx(self):
        conn = LockRedis()
        self.assertTrue(sr.keep_lock(conn, "bus-requests-a", 300))
        self.assertEqual("bus-requests-a", conn.get(sr.WORKER_LOCK_KEY))
        self.assertTrue(conn.sets[-1][2], "taken back with NX")

    def test_release_never_deletes_another_holder(self):
        conn = LockRedis(held="bus-requests-b")
        sr.drop_lock(conn, "bus-requests-a")
        self.assertEqual("bus-requests-b", conn.get(sr.WORKER_LOCK_KEY))
        sr.drop_lock(conn, "bus-requests-b")
        self.assertIsNone(conn.get(sr.WORKER_LOCK_KEY))

    def test_a_retried_task_reclaims_by_compare_never_over_another(self):
        conn = LockRedis(held="bus-requests-b")
        self.assertEqual("bus-requests-b", sr.take_lock(conn, "bus-requests-a", 300))
        self.assertEqual("bus-requests-b", conn.get(sr.WORKER_LOCK_KEY))
        self.assertIsNone(sr.take_lock(conn, "bus-requests-b", 300))

    def test_the_lock_is_renewed_before_every_append_and_a_lost_lock_stops_the_pass(self):
        written, renewals = [], []
        state = {"ours": True}

        def renew():
            renewals.append(1)
            return state["ours"]
        write = sr.guarded(written.append, renew)
        write({"row_id": "R1"})
        state["ours"] = False
        with self.assertRaises(sr.LockLost):
            write({"row_id": "R2"})
        self.assertEqual([{"row_id": "R1"}], written, "nothing is written once the lock is gone")
        self.assertEqual(2, len(renewals))

    def test_the_live_loop_hands_each_pass_a_renewal(self):
        seen = []

        def capture(*a, **k):
            seen.append(k.get("renew"))
        env = {"LOOP_UNTIL": "2026-10-11T22:00:30Z", "LOOP_INTERVAL": "10", "CLOUD_RUN_EXECUTION": "bus-requests-a"}
        conn = LockRedis()
        self.assertEqual(0, self.run_main(conn, env, capture))
        self.assertTrue(seen and all(callable(r) for r in seen))
        self.assertTrue(seen[0](), "the renewal a pass gets renews this execution's lock")

    def test_the_lock_outlasts_one_board_call(self):
        self.assertGreaterEqual(sr.LIVE_LOCK_SECONDS, 300)

    def test_no_lock_no_run(self):
        class Down(LockRedis):
            def set(self, *a, **k):
                raise ConnectionError("redis down")
        passes = []
        env = {"LOOP_UNTIL": "", "CLOUD_RUN_EXECUTION": "bus-requests-a"}
        self.assertEqual(1, self.run_main(Down(), env, lambda *a, **k: passes.append(1)))
        self.assertEqual([], passes)


class LiveMode(unittest.TestCase):
    def test_loop_until_parses_or_falls_back_to_one_pass(self):
        self.assertEqual(datetime.datetime(2026, 10, 9, 3, 15, tzinfo=datetime.timezone.utc),
                         sr._loop_until("2026-10-09T03:15:00Z"))
        self.assertIsNone(sr._loop_until(""))
        self.assertIsNone(sr._loop_until("after the call"))

    def test_the_loop_runs_passes_and_stops_at_its_deadline(self):
        passes, git_flags = [], []
        start = datetime.datetime(2026, 10, 9, 2, 0, tzinfo=datetime.timezone.utc)
        clock = {"t": start}

        class FakeDT(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["t"]

        def fake_pass(conn, env, execution, with_git=True, quiet=False, renew=None):
            passes.append(clock["t"])
            git_flags.append(with_git)

        def fake_sleep(seconds):
            clock["t"] = clock["t"] + datetime.timedelta(seconds=seconds)

        env = {"LOOP_UNTIL": "2026-10-09T02:02:00Z", "LOOP_INTERVAL": "10", "GITHUB_READ_TOKEN": "t"}
        with mock.patch.object(sr.datetime, "datetime", FakeDT), \
                mock.patch.object(sr, "one_pass", side_effect=fake_pass), \
                mock.patch.object(sr.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(sr.redis_dual, "client", return_value=LockRedis()), \
                mock.patch.dict(sr.os.environ, env), \
                mock.patch("bus.load_env", return_value={}):
            self.assertEqual(0, sr.main())
        self.assertEqual(12, len(passes), "every 10 s for two minutes")
        self.assertEqual(2, sum(git_flags), "with a token, GitHub at most once a minute")

    def test_without_a_token_github_is_read_every_144_seconds(self):
        flags = []
        clock = {"t": datetime.datetime(2026, 10, 9, 2, 0, tzinfo=datetime.timezone.utc)}

        class FakeDT(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["t"]

        env = {"LOOP_UNTIL": "2026-10-09T02:05:00Z", "LOOP_INTERVAL": "10", "GITHUB_READ_TOKEN": ""}
        with mock.patch.object(sr.datetime, "datetime", FakeDT),                 mock.patch.object(sr, "one_pass", side_effect=lambda *a, **k: flags.append(k["with_git"])),                 mock.patch.object(sr.time, "sleep", side_effect=lambda s: clock.__setitem__("t", clock["t"] + datetime.timedelta(seconds=s))),                 mock.patch.object(sr.redis_dual, "client", return_value=LockRedis()),                 mock.patch.dict(sr.os.environ, env),                 mock.patch("bus.load_env", return_value={}):
            self.assertEqual(0, sr.main())
        self.assertEqual(30, len(flags))
        self.assertEqual(2, sum(flags), "at 0 and 150 s in five minutes: two repos stay under 60 calls an hour")

    def test_a_failed_pass_does_not_end_the_channel(self):
        calls = {"n": 0}
        start = datetime.datetime(2026, 10, 9, 2, 0, tzinfo=datetime.timezone.utc)
        clock = {"t": start}

        class FakeDT(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["t"]

        def flaky(*a, **k):
            calls["n"] += 1
            raise ConnectionError("board flapped")

        with mock.patch.object(sr.datetime, "datetime", FakeDT), \
                mock.patch.object(sr, "one_pass", side_effect=flaky), \
                mock.patch.object(sr.time, "sleep", side_effect=lambda s: clock.__setitem__("t", clock["t"] + datetime.timedelta(seconds=s))), \
                mock.patch.object(sr.redis_dual, "client", return_value=LockRedis()), \
                mock.patch.dict(sr.os.environ, {"LOOP_UNTIL": "2026-10-09T02:01:00Z"}), \
                mock.patch("bus.load_env", return_value={}):
            self.assertEqual(0, sr.main())
        self.assertEqual(6, calls["n"])


if __name__ == "__main__":
    unittest.main()

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

        def fake_pass(conn, env, execution, with_git=True, quiet=False):
            passes.append(clock["t"])
            git_flags.append(with_git)

        def fake_sleep(seconds):
            clock["t"] = clock["t"] + datetime.timedelta(seconds=seconds)

        env = {"LOOP_UNTIL": "2026-10-09T02:02:00Z", "LOOP_INTERVAL": "10"}
        with mock.patch.object(sr.datetime, "datetime", FakeDT), \
                mock.patch.object(sr, "one_pass", side_effect=fake_pass), \
                mock.patch.object(sr.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(sr.redis_dual, "client", return_value=object()), \
                mock.patch.dict(sr.os.environ, env), \
                mock.patch("bus.load_env", return_value={}):
            self.assertEqual(0, sr.main())
        self.assertEqual(12, len(passes), "every 10 s for two minutes")
        self.assertEqual(2, sum(git_flags), "GitHub at most once a minute")

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
                mock.patch.object(sr.redis_dual, "client", return_value=object()), \
                mock.patch.dict(sr.os.environ, {"LOOP_UNTIL": "2026-10-09T02:01:00Z"}), \
                mock.patch("bus.load_env", return_value={}):
            self.assertEqual(0, sr.main())
        self.assertEqual(6, calls["n"])


if __name__ == "__main__":
    unittest.main()

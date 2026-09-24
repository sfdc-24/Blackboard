"""The board probe's read retry covers timeouts and dropped connections.

2026-09-24: the 07:15Z board-probe run raised TimeoutError on its first read
and exited 1. The retry loop only handled a non-200, a non-JSON body or a
health blob, so an exception from the read escaped it and the soak counted
the hour as missed.
"""
import importlib.util
import json
import os
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
_spec = importlib.util.spec_from_file_location(
    "board_probe_main", os.path.join(ROOT, "cloud", "board-probe", "main.py"))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

GOOD = json.dumps({"rows": [["id-1", "2026-09-24T07:00:00Z"]], "filtered": True})


class ReadRetry(unittest.TestCase):
    def setUp(self):
        self.saved = (probe.board.bus_get, probe.board.load_env, probe.RETRY_SLEEP, probe.ATTEMPTS)
        probe.board.load_env = lambda: {}
        probe.RETRY_SLEEP = 0
        probe.ATTEMPTS = 3

    def tearDown(self):
        probe.board.bus_get, probe.board.load_env, probe.RETRY_SLEEP, probe.ATTEMPTS = self.saved

    def script(self, *outcomes):
        calls = []

        def fake(env, params):
            calls.append(params)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        probe.board.bus_get = fake
        return calls

    def test_a_timeout_then_a_good_read_succeeds(self):
        calls = self.script(TimeoutError("The read operation timed out"), (200, GOOD))
        fp = probe.fingerprint()
        self.assertEqual(len(calls), 2)
        self.assertEqual(fp["read_attempts"], ["error TimeoutError", "ok"])
        self.assertEqual(fp["rows_returned"], 1)

    def test_a_dropped_connection_then_a_good_read_succeeds(self):
        self.script(urllib.error.URLError("connection reset"), (200, GOOD))
        self.assertEqual(probe.fingerprint()["read_attempts"], ["error URLError", "ok"])

    def test_every_attempt_timing_out_fails_with_the_reason(self):
        self.script(*[TimeoutError("t")] * 3)
        with self.assertRaises(SystemExit) as cm:
            probe.fingerprint()
        self.assertIn("board unreadable after 3 attempts", str(cm.exception))
        self.assertIn("error TimeoutError", str(cm.exception))

    def test_every_retry_is_still_a_read(self):
        calls = self.script(TimeoutError("t"), (503, ""), (200, GOOD))
        probe.fingerprint()
        self.assertEqual([c["action"] for c in calls], ["read", "read", "read"])

    def test_a_programming_error_is_not_swallowed_as_a_retry(self):
        self.script(KeyError("bug"), (200, GOOD))
        with self.assertRaises(KeyError):
            probe.fingerprint()


if __name__ == "__main__":
    unittest.main()

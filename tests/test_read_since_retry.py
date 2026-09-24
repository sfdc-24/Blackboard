"""agent_waker.read_since treats a timeout or a dropped connection as a failed
attempt, not a crash. Its retry loop already covered non-JSON bodies and health
pings, but an exception from _fetch escaped it; the cloud board-probe lost its
07Z run on 2026-09-24 to the same shape (#224). read_since feeds the cloud
board-watcher and the gemini and claude-api wakers.
"""
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import agent_waker as aw  # noqa: E402

ENV = {"BUS_URL": "https://example.invalid/exec", "BUS_SECRET": "test-only"}
GOOD = json.dumps({"rows": [["id-1", "2026-09-24T07:00:00Z"]], "total": 1, "filtered": 1})


class ReadSinceRetry(unittest.TestCase):
    def setUp(self):
        self.saved = aw._fetch

    def tearDown(self):
        aw._fetch = self.saved

    def script(self, *outcomes):
        calls = []

        def fake(url, payload, *a, **k):
            calls.append(payload)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        aw._fetch = fake
        return calls

    def test_a_timeout_then_rows_succeeds(self):
        calls = self.script(TimeoutError("The read operation timed out"), GOOD)
        got = aw.read_since(ENV, "2026-09-24T00:00:00Z", tries=3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(got["rows"], [["id-1", "2026-09-24T07:00:00Z"]])

    def test_a_dropped_connection_then_rows_succeeds(self):
        self.script(urllib.error.URLError("connection reset"), GOOD)
        self.assertEqual(aw.read_since(ENV, "2026-09-24T00:00:00Z", tries=3)["total"], 1)

    def test_every_attempt_timing_out_ends_as_before(self):
        self.script(*[TimeoutError("t")] * 3)
        with self.assertRaises(SystemExit) as cm:
            aw.read_since(ENV, "2026-09-24T00:00:00Z", tries=3)
        self.assertIn("after 3 attempts", str(cm.exception))

    def test_every_retry_is_still_a_read(self):
        calls = self.script(TimeoutError("t"), "not json", GOOD)
        aw.read_since(ENV, "2026-09-24T00:00:00Z", tries=3)
        self.assertEqual([c["action"] for c in calls], ["read", "read", "read"])

    def test_a_programming_error_is_not_swallowed(self):
        self.script(KeyError("bug"), GOOD)
        with self.assertRaises(KeyError):
            aw.read_since(ENV, "2026-09-24T00:00:00Z", tries=3)


if __name__ == "__main__":
    unittest.main()

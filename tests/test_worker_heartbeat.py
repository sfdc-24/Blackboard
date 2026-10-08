"""PY-02's heartbeat: the worker says when it last ran, under gov:, readable by every agent, writable by none."""
import datetime
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bus_request as br                                                  # noqa: E402
from test_redis_gov import ACL, FakeRedis, run                            # noqa: E402

NOW = datetime.datetime(2026, 10, 8, 19, 0, 0, tzinfo=datetime.timezone.utc)


class Heartbeat(unittest.TestCase):
    def test_it_writes_one_expiring_json_value(self):
        conn = FakeRedis()
        self.assertTrue(br.heartbeat(conn, "idle", now=NOW, execution="bus-requests-x", answered=3))
        value = json.loads(conn.get(br.HEARTBEAT_KEY))
        self.assertEqual({"state": "idle", "at": "2026-10-08T19:00:00Z", "execution": "bus-requests-x",
                          "answered": 3}, value)
        self.assertEqual(3600, conn.ttls[br.HEARTBEAT_KEY], "absent after an hour means no recent run")

    def test_a_failing_redis_never_costs_the_run(self):
        class Down:
            def set(self, *a, **k):
                raise ConnectionError("down")
        self.assertFalse(br.heartbeat(Down(), "running"))

    def test_every_agent_can_read_it_and_none_can_write_it(self):
        conn = FakeRedis()
        br.heartbeat(conn, "idle", now=NOW)
        got = run(conn, "cursor", "get", br.HEARTBEAT_KEY)
        self.assertTrue(got["ok"], got)
        self.assertIn("idle", got["value"])
        refused = run(conn, "grok", "set", br.HEARTBEAT_KEY, val="forged")
        self.assertIn("protected", refused["refused"])
        self.assertEqual("idle", json.loads(conn.get(br.HEARTBEAT_KEY))["state"])

    def test_the_entrypoint_beats_at_start_and_end(self):
        src = (ROOT / "cloud" / "bus-reconciler" / "serve_requests.py").read_text(encoding="utf-8")
        self.assertIn('bus_request.heartbeat(conn, "running"', src)
        self.assertIn('bus_request.heartbeat(conn, "idle"', src)
        self.assertLess(src.index('heartbeat(conn, "running"'), src.index("bus_request.handle("))
        self.assertGreater(src.index('heartbeat(conn, "idle"'), src.index("bus_request.handle("))


if __name__ == "__main__":
    unittest.main()

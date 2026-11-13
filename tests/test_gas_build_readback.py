"""Case 00001051: the bus deployment carries a build identity and a read-back refuses a stale one."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gas_build_readback as rb  # noqa: E402

GOOD = "a" * 40
OLD = "b" * 40


def ping(**fields):
    body = {"ok": True, "service": "sfdc24-blackboard-bus", "time": "now"}
    body.update(fields)
    return lambda: json.dumps(body)


class ReadBack(unittest.TestCase):
    def test_the_live_build_matching_the_commit_passes(self):
        ok, why = rb.check(GOOD, ping(build=GOOD))
        self.assertTrue(ok, why)

    def test_each_way_a_deployment_can_be_wrong_fails(self):
        def boom():
            raise OSError("down")
        cases = [
            (ping(build=OLD), "not " + GOOD[:12]),               # a republished old version
            (ping(build="unset"), "not " + GOOD[:12]),           # pushed without a stamp
            (ping(), "reports no build"),                       # the deployment predates the stamp
            (lambda: json.dumps({"ok": False}), "ping not ok"),
            (lambda: "<html>", "did not return JSON"),
            (boom, "bus unreachable"),
        ]
        for fetch, words in cases:
            with self.subTest(words=words):
                ok, why = rb.check(GOOD, fetch)
                self.assertFalse(ok)
                self.assertIn(words, why)

    def test_a_short_or_bad_commit_is_refused(self):
        self.assertFalse(rb.check("abc123", ping(build="abc123"))[0])


class Stamp(unittest.TestCase):
    def test_stamp_writes_the_commit_the_ping_will_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Build.js"
            rb.stamp(GOOD, path)
            text = path.read_bytes().decode("utf-8")
            self.assertIn("var BUILD_SHA = '%s';" % GOOD, text)
            self.assertNotIn("\r\n", text, "LF only - the CRLF trap on this laptop")

    def test_stamp_refuses_a_partial_commit(self):
        with self.assertRaises(SystemExit):
            rb.stamp("abc")


class Source(unittest.TestCase):
    def test_the_bus_ping_returns_the_build(self):
        code = (ROOT / "gas-bus" / "Code.js").read_text(encoding="utf-8")
        self.assertIn("build: (typeof BUILD_SHA === 'string' ? BUILD_SHA : 'unset')", code)
        self.assertIn("var BUILD_SHA", (ROOT / "gas-bus" / "Build.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

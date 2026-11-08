"""scripts/estimate.py: the ledger records only what the board holds, and no two rows share an id.

No network: append.py is replaced by a fake that answers READ-BACK OK (or not) and keeps the payload.
"""
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import estimate  # noqa: E402


class FakeAppend:
    def __init__(self, ok=True):
        self.ok, self.payloads = ok, []

    def __call__(self, cmd, **_kw):
        self.payloads.append(json.loads(Path(cmd[2]).read_text(encoding="utf-8"))["payload"])
        out = "READ-BACK OK: row present exactly once" if self.ok else "READ-BACK FAILED: row absent"
        return mock.Mock(stdout=out, stderr="")


class Estimate(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        self.ledger = Path(path)
        self.addCleanup(lambda: self.ledger.exists() and self.ledger.unlink())
        for name, value in (("LEDGER", self.ledger), ("ME", "claude-code-cli"), ("SESSION", "")):
            patcher = mock.patch.object(estimate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cmd(self, fake, *argv):
        with mock.patch.object(estimate.subprocess, "run", fake), mock.patch.object(sys, "argv", ["estimate"] + list(argv)):
            with redirect_stdout(io.StringIO()):
                estimate.main()

    def start(self, fake, task="t1"):
        self.run_cmd(fake, "start", "--task", task, "--minutes", "10", "--what", "w")

    def test_a_start_that_does_not_read_back_fails_and_records_nothing(self):
        with self.assertRaises(SystemExit) as cm:
            self.start(FakeAppend(ok=False))
        self.assertIn("NOT recorded", str(cm.exception))
        self.assertEqual({}, estimate.load())

    def test_a_start_that_reads_back_is_recorded(self):
        self.start(FakeAppend())
        self.assertIn("t1", estimate.load())

    def test_a_done_that_does_not_read_back_leaves_the_task_open(self):
        self.start(FakeAppend())
        with self.assertRaises(SystemExit):
            self.run_cmd(FakeAppend(ok=False), "done", "--task", "t1")
        self.assertNotIn("actual_minutes", estimate.load()["t1"])
        self.run_cmd(FakeAppend(), "done", "--task", "t1")            # a second done can still close it
        self.assertIn("actual_minutes", estimate.load()["t1"])

    def test_a_reestimate_that_does_not_read_back_is_not_counted(self):
        self.start(FakeAppend())
        with self.assertRaises(SystemExit):
            self.run_cmd(FakeAppend(ok=False), "reestimate", "--task", "t1", "--minutes", "5", "--reason", "r")
        self.assertEqual([], estimate.load()["t1"].get("reestimates", []))
        fake = FakeAppend()
        self.run_cmd(fake, "reestimate", "--task", "t1", "--minutes", "5", "--reason", "r")
        self.assertIn("|reestimate=1|", fake.payloads[0])

    def test_the_session_goes_into_the_row_id_whole(self):
        fake = FakeAppend()
        with mock.patch.object(estimate, "SESSION", "vscode-second"):
            self.start(fake)
        self.assertIn("id=CLAUDE-CODE-CLI-VSCODE-SECOND-ESTIMATE-", fake.payloads[0])
        self.assertIn("|session=vscode-second|", fake.payloads[0])

    def test_two_rows_in_the_same_second_with_no_session_differ_only_by_the_nonce(self):
        # Deterministic: the clock and the nonce are both fixed, so the ids are known exactly.
        fake = FakeAppend()
        fixed = estimate.now()
        stamp = fixed.strftime("%Y%m%dT%H%M%SZ")
        nonces = iter(["A" * 32, "B" * 32])
        with mock.patch.object(estimate, "now", lambda: fixed), mock.patch.object(estimate, "nonce", lambda: next(nonces)):
            self.start(fake, "a")
            self.start(fake, "b")
        ids = [p.split("|")[2] for p in fake.payloads]
        self.assertEqual(["id=CLAUDE-CODE-CLI-ESTIMATE-%s-%s" % (stamp, c * 32) for c in "AB"], ids)

    def test_the_nonce_is_128_bits_of_hex(self):
        values = {estimate.nonce() for _ in range(3)}
        for v in values:
            self.assertRegex(v, r"^[0-9A-F]{32}$")
        self.assertEqual(3, len(values))   # chance of a repeat among 3 draws of 128 bits is about 2^-126

    def test_an_unsafe_session_is_refused_before_posting(self):
        fake = FakeAppend()
        for bad in ("B76", "has space", "x" * 25, "-lead", "a|b"):
            with mock.patch.object(estimate, "SESSION", bad), self.assertRaises(SystemExit):
                self.start(fake)
        self.assertEqual([], fake.payloads)

    def test_a_tag_outside_the_roster_is_refused_before_posting(self):
        fake = FakeAppend()
        with mock.patch.object(estimate, "ME", "claude-code-cli-b76"), self.assertRaises(SystemExit) as cm:
            self.start(fake)
        self.assertIn("ESTIMATE_SESSION", str(cm.exception))
        self.assertEqual([], fake.payloads)

    def test_no_tag_is_refused(self):
        with mock.patch.object(estimate, "ME", ""), self.assertRaises(SystemExit):
            self.start(FakeAppend())


if __name__ == "__main__":
    unittest.main()

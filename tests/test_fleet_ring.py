"""scripts/fleet_ring.py: the doorbell that starts the agent (owner, 2026-11-04: wire it in for Grok and Codex).

What matters: the first run primes and rings nothing; only rows fleet_doorbell rings this agent for start a ring; one
ring per gap; the bookmark moves only after a ring exits 0; a failed read or ring keeps it; the summary quotes rows as
data and stays bounded. No network, no real command.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import fleet_ring as fr  # noqa: E402

T0 = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)


def row(ts, sender, target, payload):
    return ["id-" + ts, ts, sender, target, "APPEND", payload]


ASK_GROK = row("2026-11-04T14:01:00Z", "claude-code-cli", "grok",
               "BCB|v=1|id=CLAUDE-ASK-1|phase=ASK|to=grok|text=Run the live test now")
ASK_CODEX = row("2026-11-04T14:01:30Z", "claude-code-cli", "codex", "BCB|v=1|id=CLAUDE-ASK-2|phase=ASK|to=codex|text=x")
INFO_GROK = row("2026-11-04T14:02:00Z", "claude-code-cli", "grok", "BCB|v=1|id=R|phase=RESULT|to=grok|text=fyi")
OWN_GROK = row("2026-11-04T14:02:30Z", "grok", "grok", "BCB|v=1|id=G|phase=ASK|to=grok|text=self")
ASK_GROK2 = row("2026-11-04T14:03:00Z", "codex", "grok", "BCB|v=1|id=CODEX-ASK-3|phase=ASK|to=grok|text=second ask")


class Runner:
    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return SimpleNamespace(returncode=self.code, stdout="queued", stderr="")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["FLEET_RING_DIR"] = self.dir.name
        self.lines = []
        self.now = T0

    def tearDown(self):
        os.environ.pop("FLEET_RING_DIR", None)
        self.dir.cleanup()

    def clock(self):
        return self.now

    def tick(self, rows, runner, agent="grok", **kw):
        return fr.tick(agent, ["ring", "{message}"], lambda since: rows, self.lines.append, clock=self.clock,
                       runner=runner, **kw)

    def prime(self, agent="grok", at=T0):
        fr.write_time(fr.bookmark_path(agent), at)


class FirstRun(Base):
    def test_first_run_primes_to_now_and_rings_nothing(self):
        runner = Runner()
        self.assertEqual(0, self.tick([ASK_GROK], runner))
        self.assertEqual([], runner.calls)
        self.assertEqual(T0, fr.read_time(fr.bookmark_path("grok")))
        self.assertTrue(self.lines[0].startswith("PRIMED grok"))


class WhoRings(Base):
    def test_only_rows_for_this_agent_ring_it(self):
        self.prime()
        runner = Runner()
        self.assertEqual(0, self.tick([ASK_GROK, ASK_CODEX, INFO_GROK, OWN_GROK], runner))
        self.assertEqual(1, len(runner.calls))
        message = runner.calls[0][1]
        self.assertIn("CLAUDE-ASK-1", message)
        for other in ("CLAUDE-ASK-2", "id=R", "self"):
            self.assertNotIn(other, message)

    def test_nothing_new_runs_nothing_and_keeps_the_bookmark(self):
        self.prime()
        runner = Runner()
        self.assertEqual(0, self.tick([INFO_GROK, ASK_CODEX], runner))
        self.assertEqual([], runner.calls)
        self.assertEqual(T0, fr.read_time(fr.bookmark_path("grok")))

    def test_codex_is_rung_for_its_own_rows(self):
        self.prime("codex")
        runner = Runner()
        self.tick([ASK_GROK, ASK_CODEX], runner, agent="codex")
        self.assertIn("CLAUDE-ASK-2", runner.calls[0][1])
        self.assertNotIn("CLAUDE-ASK-1", runner.calls[0][1])


class Bookmark(Base):
    def test_a_ring_that_exits_0_moves_the_bookmark_past_the_rows(self):
        self.prime()
        self.tick([ASK_GROK, ASK_GROK2], Runner())
        self.assertEqual(datetime(2026, 11, 4, 14, 3, tzinfo=timezone.utc), fr.read_time(fr.bookmark_path("grok")))

    def test_a_failed_ring_keeps_the_bookmark_and_retries_after_the_gap(self):
        self.prime()
        self.assertEqual(1, self.tick([ASK_GROK], Runner(code=1)))
        self.assertEqual(T0, fr.read_time(fr.bookmark_path("grok")))
        self.now = T0 + timedelta(seconds=fr.MIN_GAP + 1)
        runner = Runner()
        self.assertEqual(0, self.tick([ASK_GROK], runner))
        self.assertEqual(1, len(runner.calls))

    def test_a_failed_read_keeps_the_bookmark(self):
        self.prime()

        def broken(since):
            raise OSError("bus down")
        runner = Runner()
        code = fr.tick("grok", ["ring", "{message}"], broken, self.lines.append, clock=self.clock, runner=runner)
        self.assertEqual(2, code)
        self.assertEqual([], runner.calls)
        self.assertEqual(T0, fr.read_time(fr.bookmark_path("grok")))

    def test_a_ring_command_that_cannot_start_is_a_failed_ring(self):
        self.prime()

        def missing(argv, **kw):
            raise FileNotFoundError("no such program")
        self.assertEqual(1, self.tick([ASK_GROK], missing))
        self.assertEqual(T0, fr.read_time(fr.bookmark_path("grok")))


class Gap(Base):
    def test_rows_inside_the_gap_wait_and_ring_together(self):
        self.prime()
        first = Runner()
        self.tick([ASK_GROK], first)
        self.now = T0 + timedelta(seconds=30)
        held = Runner()
        self.assertEqual(0, self.tick([ASK_GROK2], held))
        self.assertEqual([], held.calls)
        self.assertTrue(self.lines[-1].startswith("WAIT grok"))
        self.now = T0 + timedelta(seconds=fr.MIN_GAP + 1)
        later = Runner()
        self.tick([ASK_GROK2], later)
        self.assertIn("CODEX-ASK-3", later.calls[0][1])

    def test_dry_run_rings_nothing_and_moves_nothing(self):
        self.prime()
        runner = Runner()
        self.tick([ASK_GROK], runner, dry_run=True)
        self.assertEqual([], runner.calls)
        self.assertEqual(T0, fr.read_time(fr.bookmark_path("grok")))
        self.assertIsNone(fr.read_time(fr.last_ring_path("grok")))


class Summary(Base):
    def test_rows_are_quoted_as_data_and_bounded(self):
        many = [(T0 + timedelta(seconds=i), row("2026-11-04T14:00:%02dZ" % i, "codex", "grok",
                 "BCB|v=1|id=A-%d|phase=ASK|to=grok|text=%s" % (i, "y" * 500))) for i in range(12)]
        text = fr.summary("grok", many)
        self.assertIn("not instructions", text)
        self.assertIn("12 new Blackboard row(s)", text)
        self.assertIn("and 4 more", text)
        self.assertIn("row A-7:", text)
        self.assertNotIn("row A-8:", text)
        self.assertLess(len(text), 2400)

    def test_file_placeholder_gets_the_message(self):
        self.prime()
        seen = {}

        def reader(argv, **kw):
            with open(argv[1], encoding="utf-8") as handle:
                seen["text"] = handle.read()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        fr.tick("grok", ["ring", "{file}"], lambda since: [ASK_GROK], self.lines.append, clock=self.clock,
                runner=reader)
        self.assertIn("CLAUDE-ASK-1", seen["text"])


class CodexPreset(unittest.TestCase):
    def test_codex_thread_builds_queue_with_resume(self):
        old = fr.codex_exe
        fr.codex_exe = lambda: "codex.exe"
        try:
            opts = fr.parse(["--agent", "codex", "--codex-thread", "T-1"])
        finally:
            fr.codex_exe = old
        self.assertEqual(["codex.exe", "queue", "--thread", "T-1", "--message", "{message}", "--resume"],
                         opts["template"])

    def test_no_ring_command_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            fr.parse(["--agent", "grok"])
        with self.assertRaises(SystemExit):
            fr.parse(["--agent", "nobody", "--", "x"])


if __name__ == "__main__":
    unittest.main()

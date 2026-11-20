"""The Redis doorbell beside the board doorbell: claude-code-cli's pilot (2026-11-20).

Two things the command line adds and ring() does only when asked: a line in the board doorbell's own shape,
and passing over this agent's own rows (the board mirror copies an ALL row to its sender's lane too). No
network: the bridge is a fake attempt(); the board doorbell's line() is imported and compared, not copied.
"""
import io
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import redis_doorbell  # noqa: E402

ME = "claude-code-cli"


def entry(stream_id, by, row_id, at="2026-11-20T21:30:05.250Z", **more):
    """A lane entry as redis_gov.entry_view shows a board-mirror copy: metadata only."""
    found = {"id": stream_id, "by": by, "row_id": row_id, "at": at, "phase": more.pop("phase", "ASK")}
    found.update(more)
    return found


def answers(*batches):
    """A fake attempt(): the xwait answers in order, then quiet lanes. Records every call."""
    calls, queue = [], list(batches)

    def attempt(op, key, *, me, after=None, count=None):
        calls.append((op, key, me, after))
        if op == "xrange":
            return {"ok": True, "value": [{"id": "5-0"}]}
        batch = queue.pop(0) if queue else []
        return {"ok": True, "value": batch, "last": batch[-1]["id"] if batch else after}
    return attempt, calls


class OwnRows(unittest.TestCase):
    def setUp(self):
        self.cursor = pathlib.Path(tempfile.mkdtemp()) / "cursor"
        redis_doorbell.write_cursor(self.cursor, "6-0")

    def test_its_own_rows_do_not_ring_and_the_cursor_moves_past_them(self):
        attempt, calls = answers([entry("7-0", ME, "CLAUDE-CLI-STATUS-1"), entry("8-0", "Claude-Code-CLI", "X-2")],
                                 [entry("9-0", "grok", "GROK-ORDER-3")])
        out = io.StringIO()
        code = redis_doorbell.ring(ME, attempt=attempt, out=out, cursor_file=self.cursor, rounds=3, skip_own=True)
        self.assertEqual(0, code)
        printed = out.getvalue().splitlines()
        self.assertEqual(1, len(printed), printed)
        self.assertIn("GROK-ORDER-3", printed[0])
        # The second wait starts after the own rows, not at the old cursor: they are never read again.
        self.assertEqual("8-0", calls[1][3])
        self.assertEqual("9-0", self.cursor.read_text(encoding="utf-8"))

    def test_a_batch_of_only_own_rows_moves_the_cursor_on_disk_before_the_next_wait(self):
        attempt, calls = answers([entry("7-0", ME, "CLAUDE-CLI-STATUS-1")])
        out = io.StringIO()
        code = redis_doorbell.ring(ME, attempt=attempt, out=out, cursor_file=self.cursor, rounds=1, skip_own=True)
        self.assertEqual(0, code)
        self.assertEqual("", out.getvalue())
        self.assertEqual("7-0", self.cursor.read_text(encoding="utf-8"))

    def test_peek_passes_over_own_rows_in_memory_and_leaves_the_file(self):
        attempt, calls = answers([entry("7-0", ME, "CLAUDE-CLI-STATUS-1")], [entry("8-0", "gemini", "GEM-1")])
        out = io.StringIO()
        redis_doorbell.ring(ME, attempt=attempt, out=out, cursor_file=self.cursor, rounds=3, skip_own=True,
                            peek=True)
        self.assertEqual("7-0", calls[1][3])
        self.assertIn("GEM-1", out.getvalue())
        self.assertEqual("6-0", self.cursor.read_text(encoding="utf-8"))

    def test_the_compliance_runner_s_ring_under_its_own_tag_still_rings(self):
        # claude_cli_inbox.OWN_RING: the runner posts under this agent's tag, to this agent. Hidden, six R-PR
        # rings went unseen on Nov 14.
        ring_row = entry("7-0", ME, "CLAUDE-CLI-COMPLIANCE-RING-R-PR-20261120T2130Z")
        attempt, _ = answers([ring_row])
        out = io.StringIO()
        redis_doorbell.ring(ME, attempt=attempt, out=out, cursor_file=self.cursor, rounds=1, skip_own=True)
        self.assertIn("CLAUDE-CLI-COMPLIANCE-RING-R-PR-", out.getvalue())

    def test_another_agent_whose_name_begins_like_this_one_is_not_own(self):
        self.assertFalse(redis_doorbell.is_own(ME, entry("1-0", "claude-api", "A-1")))
        self.assertFalse(redis_doorbell.is_own(ME, entry("1-0", "claude", "A-1")))
        self.assertFalse(redis_doorbell.is_own(ME, entry("1-0", "", "A-1")))
        self.assertTrue(redis_doorbell.is_own(ME, entry("1-0", "claude-code-cli-9a", "A-1")))

    def test_ring_alone_keeps_its_old_behaviour_so_redis_ring_is_unchanged(self):
        # redis_ring.wait_once calls ring(peek=True, rounds=1) and moves the cursor itself from what was printed.
        # Filtering there would print nothing for an own-only batch and leave its cursor behind them forever.
        attempt, _ = answers([entry("7-0", ME, "CLAUDE-CLI-STATUS-1")])
        out = io.StringIO()
        redis_doorbell.ring(ME, attempt=attempt, out=out, cursor_file=self.cursor, rounds=1, peek=True)
        self.assertIn("CLAUDE-CLI-STATUS-1", out.getvalue())

    def test_the_command_line_passes_over_own_rows_unless_told_not_to(self):
        seen = {}
        real = redis_doorbell.ring

        def spy(me, **kw):
            seen.update(kw, me=me)
            return 0
        redis_doorbell.ring = spy
        try:
            redis_doorbell.main(["--me", "Claude-Code-CLI", "--format", "line"])
            self.assertEqual({"me": ME, "peek": False, "fmt": "line", "skip_own": True}, seen)
            redis_doorbell.main(["--me", ME, "--include-own"])
            self.assertEqual("json", seen["fmt"])
            self.assertFalse(seen["skip_own"])
        finally:
            redis_doorbell.ring = real


class TheLine(unittest.TestCase):
    def test_a_line_has_the_board_doorbell_s_shape(self):
        import claude_cli_inbox
        row = ["GROK-ORDER-3", "2026-11-20T21:30:05.250Z", "grok", ME, "", "id=GROK-ORDER-3|phase=ASK|text"]
        board = claude_cli_inbox.line(datetime(2026, 11, 20, 21, 30, 5, 250000, tzinfo=timezone.utc), row)
        redis = redis_doorbell.line(ME, entry("1732138205250-0", "grok", "GROK-ORDER-3", thread_id="TH-CALL-1"))
        # Same three leading columns, split the same way; the fourth names the row instead of quoting it.
        self.assertEqual(board.split(" | ")[:3], redis.split(" | ")[:3])
        self.assertEqual("id=GROK-ORDER-3|phase=ASK|thread_id=TH-CALL-1|via=redis:1732138205250-0",
                         redis.split(" | ")[3])

    def test_a_time_that_is_not_one_is_shown_as_it_came_and_missing_fields_as_a_dash(self):
        text = redis_doorbell.line(ME, {"id": "1-0", "at": "soon"})
        self.assertEqual("soon | from - | to claude-code-cli | id=-|via=redis:1-0", text)

    def test_line_format_through_ring(self):
        cursor = pathlib.Path(tempfile.mkdtemp()) / "cursor"
        redis_doorbell.write_cursor(cursor, "6-0")
        attempt, _ = answers([entry("7-0", "whatsapp", "WA-1", at="2026-11-20T21:30:05+00:00")])
        out = io.StringIO()
        redis_doorbell.ring(ME, attempt=attempt, out=out, cursor_file=cursor, rounds=1, fmt="line")
        self.assertEqual("2026-11-20T21:30:05Z | from whatsapp | to claude-code-cli | id=WA-1|phase=ASK|via=redis:7-0",
                         out.getvalue().strip())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Board rows are never trimmed silently (scripts/board_row.py, 2026-10-09).

The defect: every Gemini waker reply stopped at 1,618-1,623 chars, mid-word, because
agent_waker.post_reply sliced text[:1500] and fleet_agent then added its BCB header. These tests
prove the fix end to end with a 5,000-char reply, and they hold every writer to the one rule so the
same slice cannot come back in another agent's poster.

Offline: the bus is faked and the spill store is a temp directory. Run: python tests/test_board_row.py
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import board_row  # noqa: E402
import fleet_agent  # noqa: E402
import state_store  # noqa: E402


def five_thousand_char_reply():
    """Sentences numbered so any lost, reordered or duplicated span is visible."""
    out, i = [], 0
    while len(" ".join(out)) < 5000:
        i += 1
        out.append("Sentence %03d of the architecture answer keeps Redis as the live store." % i)
    text = " ".join(out)
    return text[:5000].rsplit(" ", 1)[0] + " END-OF-REPLY."


class FakeBoard:
    """The bus, in memory: append stores the row, read returns what was stored."""

    def __init__(self):
        self.rows = []

    def fetch(self, url, payload, tries=1):
        assert payload["action"] == "append"
        self.rows.append(list(payload["sheetRow"]))
        return json.dumps({"ok": True})

    def read_board(self, quiet=False, **_):
        header = ["Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type", "Payload",
                  "Category", "Project Tag", "Gist", "Subgist"]
        return {"rows": [header] + self.rows}


def post_through_fleet_agent(argv, board, spill_dir):
    """fleet_agent post, in-process, against the fake bus and a temp spill store."""
    args = fleet_agent.build_parser().parse_args(argv)
    with mock.patch.dict(os.environ, {"BOARD_SPILL_URI": spill_dir}), \
            mock.patch.object(fleet_agent, "_bus_fetch", board.fetch), \
            mock.patch.object(fleet_agent, "_bus_load_env", return_value={"BUS_URL": "x", "BUS_SECRET": "y"}), \
            mock.patch.object(fleet_agent, "read_board", board.read_board):
        return fleet_agent.cmd_post(args)


class FiveThousandCharReplySurvives(unittest.TestCase):
    """The regression test the owner asked for: a 5,000-char reply, end to end."""

    def test_waker_reply_survives_whole_and_the_row_is_a_valid_pointer(self):
        import agent_waker as aw
        body = five_thousand_char_reply()
        self.assertGreaterEqual(len(body), 4950)
        reply = ("wakerreply=1|answers=GROK-ARCH-ASK-1|evidence=STATED|route=api-key (GEMINI_API_KEY)|"
                 "cost=model gemini-3.1-pro-preview in 1000 out 1300 thought 900 est_usd unpriced|"
                 "Answered by the gemini waker, which asks the gemini deployment and posts what it says. "
                 "REPLY: " + body)
        board = FakeBoard()
        spill = tempfile.mkdtemp(prefix="board-full-test-")

        def fake_run(args, cwd=None, capture_output=True, text=True, timeout=None):
            # agent_waker shells out to fleet_agent post; run that exact argv in-process.
            self.assertEqual("post", args[2])
            self.assertNotIn(reply[:1500], args[3:], "the reply must not travel as a sliced argv")
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = post_through_fleet_agent(args[2:], board, spill)
            return subprocess.CompletedProcess(args, rc, buf.getvalue(), "")

        with mock.patch.object(aw.subprocess, "run", side_effect=fake_run):
            ok = aw.post_reply("gemini", {"project": "FLEET"}, reply, "grok;ALL", "GROK-ARCH-ASK-1",
                               False, phase="RESULT")
        self.assertTrue(ok, "the reply was not VERIFIED on the (fake) board")
        self.assertEqual(1, len(board.rows))
        payload = board.rows[0][5]

        # The row: under the limit, not in the cap band, a valid pointer.
        self.assertLessEqual(len(payload), board_row.ROW_LIMIT)
        self.assertEqual([], board_row.problems(payload))
        self.assertRegex(payload, r"\|full=file:[^|]+board-full-GEMINI-WAKE-[^|]+\.json\|")
        self.assertIn("wakerreply=1|answers=GROK-ARCH-ASK-1|", payload, "fields must stay readable")
        self.assertIn("Summary only. Full text:", payload)
        summary = payload.split("REPLY: ", 1)[1].split(" [Summary only.")[0]
        self.assertTrue(summary.endswith("."), "the summary must end on a whole sentence: %r" % summary[-40:])

        # The full text: byte-for-byte what the row would have said, verified by chars= and sha256=.
        full = board_row.resolve(payload)
        self.assertTrue(full.endswith(body), "the 5,000-char body did not survive whole")
        self.assertIn("Sentence 001", full)
        self.assertIn("END-OF-REPLY.", full)
        self.assertEqual(int(board_row.field(payload, "chars")), len(full))
        self.assertEqual(board_row.field(payload, "sha256"), board_row.sha256(full))
        self.assertTrue(full.startswith("BCB|v=1|id=GEMINI-WAKE-GROK-ARCH-ASK-1"))

    def test_a_tampered_spill_is_caught(self):
        board, spill = FakeBoard(), tempfile.mkdtemp()
        self.assertEqual(0, post_through_fleet_agent(["post", five_thousand_char_reply(), "--id", "T-1"], board, spill))
        pointer = board_row.field(board.rows[0][5], "full")
        path = Path(pointer.split("file:", 1)[-1])
        self.assertTrue(path.is_file(), pointer)
        rec = json.loads(path.read_text())
        rec["text"] = rec["text"][:-1]
        path.write_text(json.dumps(rec))
        with self.assertRaises(board_row.RowRejected):
            board_row.resolve(board.rows[0][5])


class NeverSilent(unittest.TestCase):
    def test_the_old_defect_is_flagged(self):
        """header + text[:1500] is exactly the 1,618-1,623 band seen on the board."""
        header = "BCB|v=1|id=GEMINI-WAKE-GROK-SITE-CLEANUP-RT-20261009T1221Z-e7d69943bf|phase=RESULT|class=NOTE|from=gemini|to=grok,ALL|"
        row = header + five_thousand_char_reply()[:1500]
        self.assertTrue(board_row.CAP_BAND[0] <= len(row) <= board_row.CAP_BAND[1], len(row))
        with self.assertRaises(board_row.RowRejected):
            board_row.guard(row)

    def test_short_rows_pass_untouched(self):
        payload, info = board_row.fit("BCB|v=1|id=X|", "to=ALL|hello.", key="X")
        self.assertEqual("BCB|v=1|id=X|to=ALL|hello.", payload)
        self.assertFalse(info["spilled"] or info["truncated"])

    def test_no_store_means_a_visible_truncated_mark(self):
        def broken(key, text):
            raise OSError("bucket unreachable")
        payload, info = board_row.fit("BCB|v=1|id=X|", "a=1|" + five_thousand_char_reply(), key="X", spill=broken)
        self.assertTrue(info["truncated"])
        self.assertIn("|truncated=1|chars=", payload)
        self.assertIn("[TRUNCATED:", payload)
        self.assertIn("bucket unreachable", payload)
        self.assertLessEqual(len(payload), board_row.ROW_LIMIT)
        self.assertEqual([], board_row.problems(payload))

    def test_over_limit_without_pointer_is_rejected(self):
        with self.assertRaises(board_row.RowRejected):
            board_row.guard("BCB|v=1|id=X|" + "x" * 1600)

    def test_pointer_row_needs_chars_and_sha(self):
        self.assertTrue(board_row.problems("BCB|v=1|id=X|full=gs://b/x.json|short"))

    def test_prebuilt_payload_keeps_its_envelope_first(self):
        payload, _ = board_row.fit("", "BCB|v=1|id=P|from=grok|to=ALL|" + five_thousand_char_reply(),
                                   key="P", spill=lambda k, t: "gs://b/board-full-P.json")
        self.assertTrue(payload.startswith("BCB|v=1|id=P|from=grok|to=ALL|full=gs://b/board-full-P.json|"))

    def test_spill_is_create_only_and_unique_per_row(self):
        """Measured failure on a312e13: spill('ab/c') and spill('abc') shared board-full-abc.json,
        and the second raise was a Conflict rather than a second object. The name now includes the
        payload sha256, and save is called with token None (GCS ifGenerationMatch=0)."""
        store = state_store.open_store(tempfile.mkdtemp())
        tokens = []
        real_save = store.save

        def recording_save(name, state, token):
            tokens.append((name, token))
            return real_save(name, state, token)

        store.save = recording_save
        spill = board_row.store_spill(store)
        p1 = spill("K", "one text")
        self.assertEqual(p1, spill("K", "one text"), "same text, same pointer, no second object")
        p2 = spill("K", "a different text")
        self.assertNotEqual(p1, p2, "different text must not reuse the first object")
        first = json.loads(Path(p1.split("file:", 1)[-1]).read_text())
        self.assertEqual("one text", first["text"], "the first object was overwritten")
        # The lossy id that collided before this fix.
        collapsed_a = spill("ab/c", "alpha")
        collapsed_b = spill("abc", "beta")
        self.assertNotEqual(collapsed_a, collapsed_b)
        self.assertTrue(all(token is None for _, token in tokens), tokens)
        self.assertEqual(len({name for name, _ in tokens}), 4, tokens)
        # A generation-match miss with different bytes must not become an update.
        path = Path(p1.split("file:", 1)[-1])
        path.write_text(json.dumps({"row_id": "K", "sha256": "0" * 64, "text": "tampered", "chars": 8}))
        with self.assertRaises(Exception):
            spill("K", "one text")
        self.assertEqual("tampered", json.loads(path.read_text())["text"])


class SpillIsOffThePost(unittest.TestCase):
    """The board append must not wait on the spill write."""

    def test_a_long_post_appends_before_the_object_exists(self):
        order = []
        board, spill_dir = FakeBoard(), tempfile.mkdtemp()

        original = board.fetch

        def fetch(url, payload, tries=1):
            order.append("post")
            pointer = board_row.field(payload["sheetRow"][5], "full")
            path = Path(pointer.split("file:", 1)[-1])
            self.assertFalse(path.exists(), "the spill object existed before the append returned")
            return original(url, payload, tries)

        real_commit = board_row.commit

        def commit(info, spill):
            order.append("spill")
            return real_commit(info, spill)

        board.fetch = fetch
        with mock.patch.object(board_row, "commit", commit):
            rc = post_through_fleet_agent(
                ["post", five_thousand_char_reply(), "--id", "ORDER-1"], board, spill_dir)
        self.assertEqual(0, rc)
        self.assertEqual(["post", "spill"], order)
        full = board_row.resolve(board.rows[0][5])
        self.assertTrue(full.endswith("END-OF-REPLY."))

    def test_a_short_post_does_not_spill(self):
        order = []
        board, spill_dir = FakeBoard(), tempfile.mkdtemp()

        original = board.fetch

        def fetch(url, payload, tries=1):
            order.append("post")
            return original(url, payload, tries)

        def commit(info, spill):
            order.append("spill")
            raise AssertionError("a short row must not commit a spill")

        board.fetch = fetch
        with mock.patch.object(board_row, "commit", commit):
            rc = post_through_fleet_agent(["post", "hello from the short path", "--id", "SHORT-1"],
                                          board, spill_dir)
        self.assertEqual(0, rc)
        self.assertEqual(["post"], order)
        self.assertNotIn("full=", board.rows[0][5])


class ModelCutOffIsSaid(unittest.TestCase):
    def load_gemini(self, env):
        with mock.patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location("gemini_agent_cut", REPO / "scripts" / "gemini_agent.py")
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
        return m

    def run_ask(self, m, responses):
        seen = []

        def post(url, headers, payload, timeout=60):
            seen.append(payload)
            return responses.pop(0)
        with mock.patch.object(m, "_post", side_effect=post), \
                mock.patch.object(m, "api_key", return_value=("GEMINI_API_KEY", "k")):
            return m.ask("q"), seen

    def resp(self, text, status, token=None):
        d = {"output_text": text, "status": status,
             "usage": {"total_tokens": 10, "total_input_tokens": 5, "total_output_tokens": 5, "total_thought_tokens": 0}}
        if token:
            d["continuation_token"] = token
        return 200, json.dumps(d)

    def test_incomplete_is_continued_once(self):
        m = self.load_gemini({})
        (text, _), seen = self.run_ask(m, [self.resp("first half, ", "incomplete", "tok1"),
                                           self.resp("second half.", "completed")])
        self.assertEqual("first half, second half.", text)
        self.assertEqual("tok1", seen[1]["continuation_token"])
        self.assertIsNone(m.ask.last_finish)
        self.assertEqual(10, m.ask.last_usage["out"])

    def test_still_incomplete_is_marked(self):
        m = self.load_gemini({"GEMINI_MAX_CONTINUATIONS": "0"})
        (text, _), seen = self.run_ask(m, [self.resp("cut mid", "incomplete", "tok1")])
        self.assertEqual(1, len(seen), "no continuation when the job sets 0")
        self.assertEqual("MAX_TOKENS", m.ask.last_finish)


class OneWritePath(unittest.TestCase):
    """Poka-yoke on the code: the slice cannot come back in any agent's poster."""

    # Short, fixed-shape system rows whose length is bounded by construction (ids and URLs only).
    BOUNDED = {"wa_board_outbox.py", "glasses_capture.py"}

    def py_files(self):
        return sorted(list((REPO / "scripts").glob("*.py")) + list((REPO / "cloud").glob("*/main.py")))

    def test_no_slice_on_text_handed_to_a_post(self):
        bad = []
        for f in self.py_files():
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if re.search(r'"post",\s*[^,\n]*\[\s*:\s*\d+\s*\]', line):
                    bad.append("%s:%d %s" % (f.name, n, line.strip()))
        self.assertEqual([], bad, "a post must hand over the whole text; fleet_agent applies the limit")

    def test_every_sheet_writer_goes_through_board_row(self):
        missing = []
        for f in self.py_files():
            src = f.read_text(encoding="utf-8", errors="replace")
            if '"sheetRow"' in src and "board_row" not in src and f.name not in self.BOUNDED \
                    and f.name not in ("bus.py", "board_row.py"):
                missing.append(f.name)
        self.assertEqual([], missing, "these write board rows without scripts/board_row.py")


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)

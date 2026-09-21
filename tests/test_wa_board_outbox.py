#!/usr/bin/env python3
"""Offline contract tests for the Blackboard → WhatsApp outbox.

No bus. No Graph. No .env. The suite imports the parser and the
select/prime helpers and feeds them synthetic rows. A green tick here
means the contract is wired, not that a message reached Mr. Salam.

Run: python3 tests/test_wa_board_outbox.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import wa_board_outbox as ox  # noqa: E402


def row(row_id, payload, source="claude-code-cli", action="APPEND", gist=""):
    return [
        row_id,
        "2026-09-21T00:00:00Z",
        source,
        "wa-outbox",
        action,
        payload,
        "OPEN",
        "Blackboard",
        gist,
        "",
    ]


WA_SEND = (
    "BCB|v=1|id=WA-CLAUDE-001|phase=WA_SEND|from=claude-code-cli|"
    "to=wa-outbox|kind=ASK|\nNeed a yes/no on landing the outbox PR."
)
WA_OUT = "WA_OUT|PR 50 review is waiting on the clasp credential."
NOTE = (
    "BCB|v=1|id=WA-DELIVERED-WRK-1|phase=NOTE|class=DELIVERY|"
    "from=wa-outbox|to=ALL|answers=WRK-1|delivered=WRK-1"
)
OTHER = "BCB|v=1|id=GROK-OTHER|phase=RESULT|from=grok-bot|to=ALL|hello"


class ParseContract(unittest.TestCase):
    def test_phase_wa_send_uses_from_and_trailing_body(self):
        req = ox.parse_wa_request(row("WRK-1", WA_SEND))
        self.assertIsNotNone(req)
        self.assertEqual(req["tag"], "claude-code-cli")
        self.assertEqual(req["kind"], "ASK")
        self.assertEqual(req["bcb_id"], "WA-CLAUDE-001")
        self.assertIn("Need a yes/no", req["text"])

    def test_text_field_wins_over_bare_segment(self):
        payload = (
            "BCB|v=1|id=WA-X|phase=WA_SEND|from=codex|to=wa-outbox|"
            "text=the real body|ignored bare"
        )
        req = ox.parse_wa_request(row("WRK-x", payload, source="someone-else"))
        self.assertEqual(req["text"], "the real body")
        self.assertEqual(req["tag"], "codex")

    def test_source_tag_fills_in_when_from_is_missing(self):
        payload = "BCB|v=1|id=WA-Y|phase=WA_SEND|to=wa-outbox|hello from gist"
        req = ox.parse_wa_request(row("WRK-y", payload, source="copilot", gist="fallback"))
        self.assertEqual(req["tag"], "copilot")
        self.assertEqual(req["text"], "hello from gist")

    def test_wa_out_prefix_keeps_pipes_in_the_body(self):
        req = ox.parse_wa_request(row("WRK-2", "WA_OUT|a|b|c", source="codex"))
        self.assertEqual(req["tag"], "codex")
        self.assertEqual(req["text"], "a|b|c")
        self.assertEqual(req["kind"], "STATUS")

    def test_action_type_wa_send_is_enough(self):
        payload = "BCB|v=1|id=WA-Z|from=grok-bot|to=wa-outbox|body=via action"
        req = ox.parse_wa_request(row("WRK-z", payload, source="grok-bot", action="WA_SEND"))
        self.assertEqual(req["text"], "via action")

    def test_unrelated_and_empty_and_bad_tag_are_not_requests(self):
        self.assertIsNone(ox.parse_wa_request(row("a", OTHER)))
        self.assertIsNone(ox.parse_wa_request(row("b", "BCB|v=1|phase=WA_SEND|from=ok|to=wa-outbox|")))
        self.assertIsNone(ox.parse_wa_request(row(
            "c", "BCB|v=1|id=WA-BAD|phase=WA_SEND|to=wa-outbox|hello", source="bad tag!"
        )))
        self.assertIsNone(ox.parse_wa_request(["only-one-cell"]))
        self.assertIsNone(ox.parse_wa_request(row("d", NOTE)))

    def test_to_field_is_never_a_recipient(self):
        payload = (
            "BCB|v=1|id=WA-SMUGGLE|phase=WA_SEND|from=codex|"
            "to=16470000000|text=should still send to Governor only"
        )
        req = ox.parse_wa_request(row("WRK-smuggle", payload, source="codex"))
        self.assertEqual(req["text"], "should still send to Governor only")
        self.assertNotIn("16470000000", req["tag"])
        self.assertNotIn("to", req)
        argv = ox.notify_argv(req, Path("body.txt"))
        joined = " ".join(argv)
        self.assertNotIn("16470000000", joined)
        self.assertNotIn(" -To ", joined)
        self.assertIn("-Tag", argv)
        self.assertEqual(argv[argv.index("-Tag") + 1], "codex")


class Idempotency(unittest.TestCase):
    def test_empty_state_selects_both_shapes_and_skips_noise(self):
        rows = [
            row("WRK-1", WA_SEND),
            row("WRK-2", WA_OUT, source="codex"),
            row("WRK-3", OTHER, source="grok-bot"),
        ]
        pending = ox.select_undelivered(rows, {"delivered_row_ids": [], "delivered_bcb_ids": []})
        self.assertEqual([p["row_id"] for p in pending], ["WRK-1", "WRK-2"])

    def test_local_state_and_note_row_both_count_as_delivered(self):
        rows = [
            row("WRK-1", WA_SEND),
            row("WRK-2", WA_OUT, source="codex"),
            row("WRK-note", NOTE, source="wa-outbox", action="NOTE"),
        ]
        state = {"delivered_row_ids": ["WRK-2"], "delivered_bcb_ids": []}
        pending = ox.select_undelivered(rows, state)
        self.assertEqual(pending, [])

    def test_duplicate_bcb_id_is_not_sent_twice(self):
        rows = [
            row("WRK-1", WA_SEND),
            row("WRK-1b", WA_SEND),
        ]
        pending = ox.select_undelivered(rows, {"delivered_row_ids": [], "delivered_bcb_ids": []})
        self.assertEqual([p["row_id"] for p in pending], ["WRK-1"])

    def test_prime_records_history_so_the_next_select_is_empty(self):
        rows = [row("WRK-1", WA_SEND), row("WRK-2", WA_OUT, source="codex")]
        state = {"delivered_row_ids": [], "delivered_bcb_ids": []}
        ox.prime_state(rows, state)
        self.assertIn("WRK-1", state["delivered_row_ids"])
        self.assertIn("WA-CLAUDE-001", state["delivered_bcb_ids"])
        self.assertEqual(ox.select_undelivered(rows, state), [])


class DryRunAndState(unittest.TestCase):
    def test_dry_run_does_not_write_state_or_call_notify(self):
        rows = [row("WRK-1", WA_SEND)]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = {"delivered_row_ids": ["already"], "delivered_bcb_ids": []}
            with mock.patch.object(ox, "send_via_notify") as send:
                rc = ox.run_once(
                    rows, state, dry_run=True, send=False, note=False,
                    state_path=state_path,
                )
            self.assertEqual(rc, 0)
            send.assert_not_called()
            self.assertFalse(state_path.exists())

    def test_live_success_marks_state_before_returning(self):
        rows = [row("WRK-1", WA_SEND)]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = {"delivered_row_ids": ["seed"], "delivered_bcb_ids": []}
            with mock.patch.object(ox, "send_via_notify", return_value=(True, "HTTP 200")):
                rc = ox.run_once(
                    rows, state, dry_run=False, send=True, note=False,
                    state_path=state_path,
                )
            self.assertEqual(rc, 0)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("WRK-1", saved["delivered_row_ids"])
            again = ox.select_undelivered(rows, saved)
            self.assertEqual(again, [])

    def test_failed_send_is_not_marked_delivered(self):
        rows = [row("WRK-1", WA_SEND)]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = {"delivered_row_ids": ["seed"], "delivered_bcb_ids": []}
            with mock.patch.object(ox, "send_via_notify", return_value=(False, "SEND FAILED")):
                rc = ox.run_once(
                    rows, state, dry_run=False, send=True, note=False,
                    state_path=state_path,
                )
            self.assertEqual(rc, 1)
            self.assertNotIn("WRK-1", state["delivered_row_ids"])
            if state_path.exists():
                saved = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertNotIn("WRK-1", saved["delivered_row_ids"])

    def test_first_run_primes_instead_of_sending_history(self):
        rows = [row("WRK-1", WA_SEND)]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = {"delivered_row_ids": [], "delivered_bcb_ids": []}
            with mock.patch.object(ox, "send_via_notify") as send:
                rc = ox.run_once(
                    rows, state, dry_run=False, send=True, note=False,
                    state_path=state_path,
                )
            self.assertEqual(rc, 0)
            send.assert_not_called()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("WRK-1", saved["delivered_row_ids"])
            self.assertTrue(saved.get("primed_at"))

    def test_fixture_dry_run_from_argv(self):
        fixture = HERE / "fixtures" / "wa_outbox_rows.json"
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            rc = ox.main([
                "once", "--dry-run",
                "--fixture", str(fixture),
                "--state", str(state_path),
            ])
            self.assertEqual(rc, 0)
            self.assertFalse(state_path.exists())


class SourceGuards(unittest.TestCase):
    def test_outbox_never_reads_meta_or_chooses_a_recipient(self):
        source = (SCRIPTS / "wa_board_outbox.py").read_text(encoding="utf-8")
        executable = source.split('if __name__', 1)[0]
        self.assertNotIn('env.get("META_TOKEN")', executable)
        self.assertNotIn("env['META_TOKEN']", executable)
        self.assertNotIn("graph.facebook.com", executable)
        self.assertNotIn("[string]$To", executable)
        self.assertIn("wa_notify.ps1", executable)
        self.assertIn("-Tag", executable)
        self.assertNotIn('add_argument("--to"', executable)

    def test_wrapper_does_not_pass_a_token(self):
        source = (SCRIPTS / "wa_board_outbox.ps1").read_text(encoding="utf-8")
        body = source.split("#>", 1)[-1]
        self.assertNotIn("META_TOKEN", body)
        self.assertNotIn("WA_TO", body)
        self.assertIn("wa_board_outbox.py", body)


if __name__ == "__main__":
    os.chdir(ROOT)
    unittest.main()

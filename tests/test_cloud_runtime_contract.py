#!/usr/bin/env python3
"""The unattended clients must start without a laptop-specific credential path."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_waker
import board_say
import board_waker
import bus


class CloudRuntimeContract(unittest.TestCase):
    def test_bus_accepts_injected_pair_without_env_file(self):
        with mock.patch.dict(os.environ, {
            "BUS_URL": "https://example.invalid/exec",
            "BUS_SECRET": "injected-not-a-real-secret",
        }, clear=True):
            self.assertEqual(bus.load_env("/missing/blackboard.env"), {
                "BUS_URL": "https://example.invalid/exec",
                "BUS_SECRET": "injected-not-a-real-secret",
            })

    def test_partial_injection_fails_closed(self):
        with mock.patch.dict(os.environ, {"BUS_URL": "https://example.invalid"}, clear=True):
            with self.assertRaises(SystemExit) as caught:
                bus.load_env("/missing/blackboard.env")
        self.assertIn("BUS_URL", str(caught.exception))
        self.assertNotIn("https://example.invalid", str(caught.exception))

    def test_all_writers_share_the_portable_loader(self):
        expected = {"BUS_URL": "https://example.invalid", "BUS_SECRET": "x"}
        with mock.patch.object(bus, "load_env", return_value=expected), \
             mock.patch.object(board_say, "_load_bus_env", return_value=expected), \
             mock.patch.object(agent_waker, "_load_bus_env", return_value=expected):
            self.assertEqual(board_say.load_env(), expected)
            self.assertEqual(agent_waker.load_env(), expected)

    def test_agent_state_can_live_outside_ephemeral_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"BLACKBOARD_STATE_DIR": tmp}, clear=False):
            path = agent_waker.state_path("claude-api")
            self.assertEqual(Path(path).parent, Path(tmp))

    def test_board_state_creates_a_durable_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "durable" / "state.json"
            with mock.patch.object(board_waker, "STATE", target):
                board_waker.save_state({"watermark": "2026-09-22T00:00:00Z"})
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                '{\n "watermark": "2026-09-22T00:00:00Z"\n}',
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

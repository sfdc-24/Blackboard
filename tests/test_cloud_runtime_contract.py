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

    def test_blackboard_env_names_the_file_that_is_read(self):
        r"""The live scheduled lane rests on this one variable.

        run-waker.ps1 sets $env:BLACKBOARD_ENV to the home repo's .env before
        starting python, because the waker's own checkout has none. #172 removed
        the hardcoded C:\Users\salam\Quantum\Blackboard\.env fallback that
        used to cover this, so if the variable stops being honoured the
        unattended waker loses its credentials - and a mutant that made bus.py
        ignore it passed every other test in this file.

        NOTE ON THE RELOAD, which is a real constraint and not test scaffolding:
        bus.ENV_PATH is computed once at import time. The launcher sets the
        variable before python starts, so that is correct there; anything trying
        to set it in-process after importing bus will be ignored.
        """
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "elsewhere.env"
            named.write_text("BUS_URL=https://named.invalid/exec\n"
                             "BUS_SECRET=from-the-named-file\n", encoding="utf-8")
            with mock.patch.dict(os.environ,
                                 {"BLACKBOARD_ENV": str(named)}, clear=True):
                reloaded = importlib.reload(bus)
                try:
                    self.assertEqual(Path(reloaded.ENV_PATH), named)
                    self.assertEqual(reloaded.load_env(), {
                        "BUS_URL": "https://named.invalid/exec",
                        "BUS_SECRET": "from-the-named-file",
                    })
                finally:
                    importlib.reload(bus)

    def test_without_the_variable_the_repo_env_is_the_default(self):
        """The local contract has to keep working, not just the cloud one."""
        with mock.patch.dict(os.environ, {}, clear=True):
            reloaded = importlib.reload(bus)
            try:
                self.assertEqual(Path(reloaded.ENV_PATH),
                                 Path(reloaded.ROOT) / ".env")
            finally:
                importlib.reload(bus)

    def test_an_injected_pair_still_wins_over_a_named_file(self):
        """Order matters: a broker's values must not be shadowed by a stale file."""
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "stale.env"
            named.write_text("BUS_URL=https://stale.invalid/exec\n"
                             "BUS_SECRET=stale\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "BLACKBOARD_ENV": str(named),
                "BUS_URL": "https://injected.invalid/exec",
                "BUS_SECRET": "injected",
            }, clear=True):
                reloaded = importlib.reload(bus)
                try:
                    self.assertEqual(reloaded.load_env()["BUS_URL"],
                                     "https://injected.invalid/exec")
                finally:
                    importlib.reload(bus)

    def test_a_named_file_missing_both_values_refuses_rather_than_half_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "half.env"
            named.write_text("BUS_URL=https://half.invalid/exec\n", encoding="utf-8")
            with mock.patch.dict(os.environ,
                                 {"BLACKBOARD_ENV": str(named)}, clear=True):
                reloaded = importlib.reload(bus)
                try:
                    with self.assertRaises(SystemExit) as caught:
                        reloaded.load_env()
                    self.assertIn("BUS_SECRET", str(caught.exception))
                finally:
                    importlib.reload(bus)

    def test_no_refusal_message_ever_prints_a_credential_value(self):
        """These messages land in scheduled-task logs and in his WhatsApp."""
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "half.env"
            named.write_text("BUS_URL=https://half.invalid/exec\n"
                             "BUS_SECRET=\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "BLACKBOARD_ENV": str(named),
                "BUS_SECRET": "a-secret-that-must-not-be-echoed",
            }, clear=True):
                reloaded = importlib.reload(bus)
                try:
                    with self.assertRaises(SystemExit) as caught:
                        reloaded.load_env()
                    self.assertNotIn("a-secret-that-must-not-be-echoed",
                                     str(caught.exception))
                finally:
                    importlib.reload(bus)

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

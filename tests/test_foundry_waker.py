#!/usr/bin/env python3
"""Addressing and dedupe for the Foundry waker.

The whole value of this waker is that it answers what is addressed to Foundry
and stays silent otherwise. Both halves of that are failure modes with a
history on this fleet:

  - too narrow: the WhatsApp poller listened only for messages starting "Grok",
    so everything Mr Salam addressed to claude-code-cli, Foundry or Gemini was
    read by nothing at all. A doorbell wired to one name is not a doorbell.
  - too wide: a row that merely MENTIONS foundry is not addressed to it, and an
    agent that answers every mention of its own name is noise. Worse, a waker
    that treats its own posts as incoming answers itself forever.

Run: python -m unittest tests.test_foundry_waker -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import foundry_waker as fw  # noqa: E402


def row(source, target, payload, ts="2026-09-19T12:00:00Z", rid="R1"):
    """A board row in the live column order, confirmed 2026-09-19."""
    return [rid, ts, source, target, "APPEND", payload]


class Addressing(unittest.TestCase):
    def test_whatsapp_prefix_is_an_address(self):
        """The prefix IS the address — Mr Salam's 2026-09-19T04:26Z ask.

        His message arrives tagged `whatsapp` with the sheet in Target_Surface
        and no to= anywhere, so every ordinary addressing field misses it.
        """
        self.assertTrue(fw.addressed_to_me(
            row("whatsapp", "Blackboard Alpha DB", "Foundry - TRACK speed, TRACK AI AGENTS")))
        self.assertTrue(fw.addressed_to_me(
            row("whatsapp", "Blackboard Alpha DB", "foundry: what is p95 on the ask bar?")))

    def test_whatsapp_for_another_agent_is_not_mine(self):
        self.assertFalse(fw.addressed_to_me(
            row("whatsapp", "Blackboard Alpha DB", "Gemini - make sure architecture supports speed")))

    def test_a_mention_is_not_an_address(self):
        """'ask foundry about speed' is about Foundry, not to Foundry."""
        self.assertFalse(fw.addressed_to_me(
            row("whatsapp", "Blackboard Alpha DB", "ask foundry about speed")))

    def test_board_to_and_cc_and_target_surface(self):
        self.assertTrue(fw.addressed_to_me(
            row("grok", "claude-code-cli", "BCB|v=1|to=foundry;ALL|ask=x")))
        self.assertTrue(fw.addressed_to_me(
            row("grok", "claude-code-cli", "BCB|v=1|to=grok|cc=foundry|ask=x")))
        self.assertTrue(fw.addressed_to_me(
            row("grok", "foundry;ALL", "BCB|v=1|ask=x")))

    def test_not_addressed(self):
        self.assertFalse(fw.addressed_to_me(
            row("grok", "codex", "BCB|v=1|to=codex;gemini|ask=x")))

    def test_own_rows_are_recognised(self):
        """Without this the waker answers itself forever."""
        self.assertTrue(fw.is_from_me(row("foundry", "grok", "anything")))
        self.assertFalse(fw.is_from_me(row("grok", "foundry", "anything")))


class Selection(unittest.TestCase):
    def test_own_rows_are_never_selected(self):
        rows = [row("foundry", "grok;ALL", "BCB|v=1|id=X|reply", rid="A")]
        self.assertEqual(fw.select(rows, set()), [])

    def test_repeat_asks_collapse_to_the_newest(self):
        """Six grok nudges under one BCB id must not become six replies."""
        rows = [
            row("grok", "foundry", "BCB|v=1|id=NUDGE-1|ask=wake up",
                ts="2026-09-19T09:00:00Z", rid="A"),
            row("grok", "foundry", "BCB|v=1|id=NUDGE-1|ask=wake up",
                ts="2026-09-19T11:00:00Z", rid="B"),
            row("grok", "foundry", "BCB|v=1|id=NUDGE-1|ask=wake up",
                ts="2026-09-19T13:00:00Z", rid="C"),
        ]
        picked = fw.select(rows, set())
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["row"][0], "C", "newest of the group wins")
        self.assertEqual(picked[0]["reasks"], 2, "and it says how many it collapsed")

    def test_already_answered_ids_are_skipped(self):
        rows = [row("grok", "foundry", "BCB|v=1|id=DONE-1|ask=x", rid="A")]
        self.assertEqual(fw.select(rows, {"DONE-1"}), [])

    def test_unparseable_timestamp_is_skipped_not_guessed(self):
        rows = [row("grok", "foundry", "BCB|v=1|id=Z|ask=x", ts="Friday, September 4", rid="A")]
        self.assertEqual(fw.select(rows, set()), [])

    def test_oldest_first_so_the_backlog_drains_in_order(self):
        rows = [
            row("grok", "foundry", "BCB|v=1|id=B2|ask=x", ts="2026-09-19T13:00:00Z", rid="B"),
            row("grok", "foundry", "BCB|v=1|id=A1|ask=x", ts="2026-09-19T09:00:00Z", rid="A"),
        ]
        picked = fw.select(rows, set())
        self.assertEqual([p["row"][0] for p in picked], ["A", "B"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

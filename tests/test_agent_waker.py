#!/usr/bin/env python3
"""Addressing and dedupe for the shared agent waker.

The whole value of this waker is that it answers what is addressed to its agent
and stays silent otherwise. Both halves of that are failure modes with a history
on this fleet:

  - too narrow: the WhatsApp poller listened only for messages starting "Grok",
    so everything Mr Salam addressed to claude-code-cli, Foundry or Gemini was
    read by nothing at all. A doorbell wired to one name is not a doorbell.
  - too wide: a row that merely MENTIONS an agent is not addressed to it, and an
    agent that answers every mention of its own name is noise. Worse, a waker
    that treats its own posts as incoming answers itself forever.

Every addressing case runs for BOTH tags. One waker serves both, so a rule that
holds for foundry and not for gemini is a bug in the shared path, and the only
way to see it is to assert it twice.

Run: python -m unittest tests.test_agent_waker -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import agent_waker as aw  # noqa: E402

TAGS = ["foundry", "gemini"]


def row(source, target, payload, ts="2026-09-19T12:00:00Z", rid="R1"):
    """A board row in the live column order, confirmed 2026-09-19."""
    return [rid, ts, source, target, "APPEND", payload]


class Registry(unittest.TestCase):
    def test_every_agent_has_an_adapter_and_a_doctrine(self):
        for tag, cfg in aw.AGENTS.items():
            with self.subTest(tag=tag):
                self.assertIn("module", cfg)
                self.assertIn("project", cfg)
                self.assertGreater(len(cfg["doctrine"]), 200)

    def test_doctrine_forbids_an_eta_it_cannot_keep(self):
        """The one sentence that keeps a reply from becoming a false promise."""
        for tag, cfg in aw.AGENTS.items():
            with self.subTest(tag=tag):
                self.assertIn("NEVER reply YES with an ETA", cfg["doctrine"])
                self.assertIn("no shell", cfg["doctrine"].lower())

    def test_state_files_do_not_collide(self):
        paths = {aw.state_path(t) for t in TAGS}
        self.assertEqual(len(paths), len(TAGS))
        self.assertTrue(aw.state_path("foundry").endswith(".foundry_waker_state.json"),
                        "renaming this orphans the watermark the live task already uses")


class Addressing(unittest.TestCase):
    def test_whatsapp_prefix_is_an_address(self):
        """The prefix IS the address - Mr Salam's 2026-09-19T04:26Z ask.

        His message arrives tagged `whatsapp` with the sheet in Target_Surface
        and no to= anywhere, so every ordinary addressing field misses it.
        """
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.assertTrue(aw.addressed_to(
                    row("whatsapp", "Blackboard Alpha DB", tag.capitalize() + " - TRACK speed"), tag))
                self.assertTrue(aw.addressed_to(
                    row("whatsapp", "Blackboard Alpha DB", tag + ": what is p95?"), tag))

    def test_whatsapp_for_another_agent_is_not_mine(self):
        self.assertFalse(aw.addressed_to(
            row("whatsapp", "Blackboard Alpha DB", "Gemini - make sure architecture holds"), "foundry"))
        self.assertFalse(aw.addressed_to(
            row("whatsapp", "Blackboard Alpha DB", "Foundry - TRACK speed"), "gemini"))

    def test_a_mention_is_not_an_address(self):
        """'ask gemini about speed' is about Gemini, not to Gemini."""
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.assertFalse(aw.addressed_to(
                    row("whatsapp", "Blackboard Alpha DB", "ask %s about speed" % tag), tag))

    def test_board_to_and_cc_and_target_surface(self):
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.assertTrue(aw.addressed_to(
                    row("grok", "claude-code-cli", "BCB|v=1|to=%s;ALL|ask=x" % tag), tag))
                self.assertTrue(aw.addressed_to(
                    row("grok", "claude-code-cli", "BCB|v=1|to=grok|cc=%s|ask=x" % tag), tag))
                self.assertTrue(aw.addressed_to(
                    row("grok", "%s;ALL" % tag, "BCB|v=1|ask=x"), tag))

    def test_not_addressed(self):
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.assertFalse(aw.addressed_to(
                    row("grok", "codex", "BCB|v=1|to=codex;chatgpt|ask=x"), tag))

    def test_own_rows_are_recognised(self):
        """Without this the waker answers itself forever."""
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.assertTrue(aw.is_from(row(tag, "grok", "anything"), tag))
                self.assertFalse(aw.is_from(row("grok", tag, "anything"), tag))

    def test_one_agent_does_not_answer_the_others_mail(self):
        r = row("grok", "foundry", "BCB|v=1|to=foundry|ask=x")
        self.assertTrue(aw.addressed_to(r, "foundry"))
        self.assertFalse(aw.addressed_to(r, "gemini"))


class Selection(unittest.TestCase):
    def test_own_rows_are_never_selected(self):
        for tag in TAGS:
            with self.subTest(tag=tag):
                rows = [row(tag, "grok;ALL", "BCB|v=1|id=X|reply", rid="A")]
                self.assertEqual(aw.select(rows, set(), tag), [])

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
        picked = aw.select(rows, set(), "foundry")
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["row"][0], "C", "newest of the group wins")
        self.assertEqual(picked[0]["reasks"], 2, "and it says how many it collapsed")

    def test_already_answered_ids_are_skipped(self):
        rows = [row("grok", "gemini", "BCB|v=1|id=DONE-1|ask=x", rid="A")]
        self.assertEqual(aw.select(rows, {"DONE-1"}, "gemini"), [])

    def test_unparseable_timestamp_is_skipped_not_guessed(self):
        rows = [row("grok", "gemini", "BCB|v=1|id=Z|ask=x", ts="Friday, September 4", rid="A")]
        self.assertEqual(aw.select(rows, set(), "gemini"), [])

    def test_oldest_first_so_the_backlog_drains_in_order(self):
        rows = [
            row("grok", "gemini", "BCB|v=1|id=B2|ask=x", ts="2026-09-19T13:00:00Z", rid="B"),
            row("grok", "gemini", "BCB|v=1|id=A1|ask=x", ts="2026-09-19T09:00:00Z", rid="A"),
        ]
        picked = aw.select(rows, set(), "gemini")
        self.assertEqual([p["row"][0] for p in picked], ["A", "B"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

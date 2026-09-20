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

TAGS = ["foundry", "gemini", "grok"]

# A sender that is NOT one of the agents above. These fixtures used to say
# "grok" for this, which was fine while grok was only ever a bystander; the
# moment grok became a registered tag, "a row from another agent" and "a row
# from me" became the same fixture and the echo guard was no longer tested.
OTHER = "cowork-chrome"


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
                    row(OTHER, "claude-code-cli", "BCB|v=1|to=%s;ALL|ask=x" % tag), tag))
                self.assertTrue(aw.addressed_to(
                    row(OTHER, "claude-code-cli", "BCB|v=1|to=%s|cc=%s|ask=x" % (OTHER, tag)), tag))
                self.assertTrue(aw.addressed_to(
                    row(OTHER, "%s;ALL" % tag, "BCB|v=1|ask=x"), tag))

    def test_not_addressed(self):
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.assertFalse(aw.addressed_to(
                    row(OTHER, "codex", "BCB|v=1|to=codex;chatgpt|ask=x"), tag))

    def test_own_rows_are_recognised(self):
        """Without this the waker answers itself forever."""
        for tag in TAGS:
            with self.subTest(tag=tag):
                self.assertTrue(aw.is_from(row(tag, OTHER, "anything"), tag))
                self.assertFalse(aw.is_from(row(OTHER, tag, "anything"), tag))

    def test_one_agent_does_not_answer_the_others_mail(self):
        r = row(OTHER, "foundry", "BCB|v=1|to=foundry|ask=x")
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
            row(OTHER, "foundry", "BCB|v=1|id=NUDGE-1|ask=wake up",
                ts="2026-09-19T09:00:00Z", rid="A"),
            row(OTHER, "foundry", "BCB|v=1|id=NUDGE-1|ask=wake up",
                ts="2026-09-19T11:00:00Z", rid="B"),
            row(OTHER, "foundry", "BCB|v=1|id=NUDGE-1|ask=wake up",
                ts="2026-09-19T13:00:00Z", rid="C"),
        ]
        picked = aw.select(rows, set(), "foundry")
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["row"][0], "C", "newest of the group wins")
        self.assertEqual(picked[0]["reasks"], 2, "and it says how many it collapsed")

    def test_already_answered_ids_are_skipped(self):
        rows = [row(OTHER, "gemini", "BCB|v=1|id=DONE-1|ask=x", rid="A")]
        self.assertEqual(aw.select(rows, {"DONE-1"}, "gemini"), [])

    def test_unparseable_timestamp_is_skipped_not_guessed(self):
        rows = [row(OTHER, "gemini", "BCB|v=1|id=Z|ask=x", ts="Friday, September 4", rid="A")]
        self.assertEqual(aw.select(rows, set(), "gemini"), [])

    def test_oldest_first_so_the_backlog_drains_in_order(self):
        rows = [
            row(OTHER, "gemini", "BCB|v=1|id=B2|ask=x", ts="2026-09-19T13:00:00Z", rid="B"),
            row(OTHER, "gemini", "BCB|v=1|id=A1|ask=x", ts="2026-09-19T09:00:00Z", rid="A"),
        ]
        picked = aw.select(rows, set(), "gemini")
        self.assertEqual([p["row"][0] for p in picked], ["A", "B"])


class Budget(unittest.TestCase):
    """The budget that made every Foundry reply an empty one.

    The waker shipped asking for 700 tokens. claude-opus-5 on the Foundry
    anthropic route emits a thinking block first and thinking counts against
    max_tokens, so on 2026-09-20 every pass returned stop_reason=max_tokens,
    blocks=['thinking'], out 700 of 700, and not one word of prose. The doorbell
    rang, the call was billed, the caller heard nothing.

    foundry_agent.py already carried the measurement: 1024 empty, 4096 a full
    answer to the identical prompt. These assertions exist so the number cannot
    drift back under the figure that is known to fail.
    """

    def test_default_budget_is_above_the_figure_known_to_fail(self):
        self.assertGreater(
            aw.DEFAULT_MAX_TOKENS, 1024,
            "1024 was MEASURED empty on claude-opus-5 via Foundry; a default at "
            "or below it buys thinking tokens and no answer")

    def test_call_agent_forwards_the_budget_to_an_adapter_that_takes_one(self):
        seen = {}

        class Adapter(object):
            @staticmethod
            def ask(prompt, max_tokens=None):
                seen["max_tokens"] = max_tokens
                return ("text", "route")

        aw.sys.modules["fake_budget_adapter"] = Adapter
        try:
            aw.call_agent({"module": "fake_budget_adapter"}, "hello")
            self.assertEqual(seen["max_tokens"], aw.DEFAULT_MAX_TOKENS)
            aw.call_agent({"module": "fake_budget_adapter"}, "hello", 2048)
            self.assertEqual(seen["max_tokens"], 2048)
        finally:
            del aw.sys.modules["fake_budget_adapter"]

    def test_an_adapter_without_the_argument_is_still_called(self):
        """gemini_agent.ask takes no max_tokens. The fallback is a signature
        test, and it must not turn that adapter into a silent no-answer."""
        calls = []

        class Adapter(object):
            @staticmethod
            def ask(prompt):
                calls.append(prompt)
                return ("text", "route")

        aw.sys.modules["fake_plain_adapter"] = Adapter
        try:
            text, route = aw.call_agent({"module": "fake_plain_adapter"}, "hello")
            self.assertEqual(calls, ["hello"])
            self.assertEqual(text, "text")
        finally:
            del aw.sys.modules["fake_plain_adapter"]


class TagBoundaries(unittest.TestCase):
    """grok is a PREFIX of grok-bot, and they are different lanes.

    grok is the xAI API route this waker drives. grok-bot is the Grok Bot
    desktop app that drives the laptop itself. Before these assertions, every
    addressing field was matched with `me in field`, so the moment grok was
    registered it began claiming grok-bot's mail - answering as the wrong party,
    with a doctrine that describes a different set of powers.
    """

    def test_grok_does_not_answer_grok_bots_mail(self):
        self.assertFalse(aw.addressed_to(row("claude-code-cli", "grok-bot", "x"), "grok"))
        self.assertFalse(
            aw.addressed_to(row("claude-code-cli", "ALL", "BCB|v=1|to=grok-bot|ask=x"), "grok"))

    def test_grok_still_answers_its_own(self):
        self.assertTrue(aw.addressed_to(row("claude-code-cli", "grok", "x"), "grok"))
        self.assertTrue(
            aw.addressed_to(row("claude-code-cli", "ALL", "BCB|v=1|to=grok;foundry|ask=x"), "grok"))
        self.assertTrue(
            aw.addressed_to(row("claude-code-cli", "ALL",
                                "BCB|v=1|to=codex|cc=vm-cowork;grok;ALL|ask=x"), "grok"))

    def test_a_multi_tag_target_still_matches_each_tag(self):
        r = row("claude-code-cli", "foundry;gemini;chat-mobile", "x")
        for tag in ("foundry", "gemini"):
            with self.subTest(tag=tag):
                self.assertTrue(aw.addressed_to(r, tag))
        self.assertFalse(aw.addressed_to(r, "grok"))

    def test_his_whatsapp_prefix_reaches_grok(self):
        """2026-09-19T22:59Z, unanswered for sixteen hours: "Grok can you reply?"."""
        r = row("whatsapp", "Blackboard Alpha DB", "Grok can you reply?")
        self.assertTrue(aw.addressed_to(r, "grok"))
        self.assertFalse(aw.addressed_to(r, "gemini"))


class GrokLane(unittest.TestCase):
    def test_grok_doctrine_separates_the_api_from_the_desktop_app(self):
        """The one confusion that would make a grok reply actively misleading."""
        d = aw.AGENTS["grok"]["doctrine"]
        self.assertIn("desktop app", d.lower())
        self.assertIn("cannot", d.lower())

    def test_grok_state_file_is_not_the_whatsapp_pollers(self):
        self.assertTrue(aw.state_path("grok").endswith(".grok_waker_state.json"),
                        "must not collide with .grok_wa_inbox_state.json")


class HisMessagesFirst(unittest.TestCase):
    """A person waiting is not a queue of agent notes."""

    def test_whatsapp_outranks_older_fleet_chatter(self):
        rows = [
            row("claude-code-cli", "grok", "BCB|v=1|id=OLD-1|ask=x",
                ts="2026-09-18T09:00:00Z", rid="OLD"),
            row("whatsapp", "Blackboard Alpha DB", "Grok can you reply?",
                ts="2026-09-19T22:59:00Z", rid="HIM"),
        ]
        picked = aw.select(rows, set(), "grok")
        self.assertEqual(picked[0]["row"][0], "HIM",
                         "his message must not wait behind the backlog")

    def test_two_of_his_still_drain_oldest_first(self):
        rows = [
            row("whatsapp", "Blackboard Alpha DB", "Grok second",
                ts="2026-09-19T23:10:00Z", rid="B"),
            row("whatsapp", "Blackboard Alpha DB", "Grok first",
                ts="2026-09-19T22:59:00Z", rid="A"),
        ]
        self.assertEqual([p["row"][0] for p in aw.select(rows, set(), "grok")], ["A", "B"])


class PeerEcho(unittest.TestCase):
    """Three agents paying for each other's conversation, for ever.

    Replies are posted `to=<sender>;ALL`. foundry answers a row from grok and
    addresses it to grok; grok reads it as mail and answers THAT, addressed to
    foundry. Measured on the live board within an hour of grok being
    registered, and legible in the Row_IDs it left:

        GROK-WAKE-FOUNDRY-WAKE-GROK-OVERNIGHT-FLEET-1312
        FOUNDRY-WAKE-GROK-WAKE-FOUNDRY-WAKE-GROK-OVERNIGHT-FL

    `is_from` only ever stopped an agent answering ITSELF.
    """

    def test_a_peers_reply_is_not_an_ask(self):
        for tag in TAGS:
            for peer in TAGS:
                if peer == tag:
                    continue
                with self.subTest(tag=tag, peer=peer):
                    r = row(peer, "%s;ALL" % tag,
                            "%s|answers=X|evidence=STATED|route=y|REPLY: ..."
                            % aw.WAKER_REPLY_MARK)
                    self.assertTrue(aw.is_waker_reply(r))
                    self.assertEqual(aw.select([r], set(), tag), [])

    def test_rows_written_before_the_marker_existed_are_still_caught(self):
        """The board already holds these. The marker cannot reach backwards."""
        r = row("foundry", "grok;ALL", "answers=GROK-OVERNIGHT-FLEET-1312|evidence=STATED|y")
        self.assertTrue(aw.is_waker_reply(r))
        self.assertEqual(aw.select([r], set(), "grok"), [])

    def test_it_does_not_gag_claude_code_cli_answering_him(self):
        """`answers=` alone is not the signal - the SENDER carries it.

        claude-code-cli writes `answers=WRK-...` when replying to Mr Salam, and
        those rows SHOULD be answered by the agents. Gating on the substring
        alone would silence exactly the traffic that matters.
        """
        r = row("claude-code-cli", "whatsapp;grok;ALL",
                "answers=WRK-477e7897|evidence=MEASURED|what was fixed today")
        self.assertFalse(aw.is_waker_reply(r))
        self.assertEqual(len(aw.select([r], set(), "grok")), 1)

    def test_a_genuine_ask_between_agents_still_gets_through(self):
        """Peers may still ask each other things. Only REPLIES are gagged."""
        r = row("grok", "foundry", "BCB|v=1|id=ASK-1|ask=benchmark this build")
        self.assertFalse(aw.is_waker_reply(r))
        self.assertEqual(len(aw.select([r], set(), "foundry")), 1)

    def test_his_whatsapp_is_never_mistaken_for_an_echo(self):
        r = row("whatsapp", "Blackboard Alpha DB", "Grok can you reply?")
        self.assertFalse(aw.is_waker_reply(r))


class SourceHygiene(unittest.TestCase):
    def test_no_control_bytes_in_the_module(self):
        """The escape that got written instead of escaped.

        A \b typed into a regex through a shell heredoc reached this file as a
        real 0x08 byte. The guard then matched nothing, and grep printed the
        line as though it were fine, because a backspace renders as nothing.
        Cheap to assert; invisible to read.
        """
        src = open(aw.__file__, encoding="utf-8").read()
        allowed = {chr(9), chr(10), chr(13)}
        bad = sorted({c for c in src if ord(c) < 32 and c not in allowed})
        self.assertEqual(bad, [], "control bytes in source: %r" % bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)

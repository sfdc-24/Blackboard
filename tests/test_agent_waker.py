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
    def test_the_standby_never_claims_claude_code_cli_is_closed(self):
        # 2026-09-24: the standby told him "the claude-code-cli lane is not open
        # right now" while that session was working and replied minutes later.
        # The standby cannot know whether the session is open, so it must not
        # say it is closed.
        # Checked as the exact old claims, not bare words: the new doctrine
        # has to NAME them to forbid them ("never say it is closed"), and a
        # substring cannot carry that negation.
        doctrine = " ".join(aw.AGENTS["claude-api"]["doctrine"].lower().split())
        self.assertNotIn("the claude-code-cli lane is not open right now", doctrine)
        self.assertNotIn("addressed claude-code-cli and no session was open", doctrine)
        self.assertIn("you do not know whether the claude-code-cli session is open", doctrine)
        # 2026-09-29: the opposite claim failed too. It promised "the claude-code-cli session will
        # also see your message" while the session's watch had lapsed; four messages went
        # unanswered for 20 minutes. The standby says the message is queued, and how he will know.
        self.assertNotIn("say, in one short line before your answer, something like: this is the api "
                         "standby answering straight away; the claude-code-cli session will also see", doctrine)
        self.assertIn("your message is queued for the claude-code-cli session, which confirms here when it "
                      "picks it up - if it has not confirmed within about 15 minutes, it has not picked it up "
                      "and may be unavailable", doctrine)
        self.assertNotIn("it is not running", doctrine)            # silence is not a diagnosis (Codex on #294)

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

    def test_answers_in_the_TEXT_is_not_a_reply(self):
        """2026-10-08: the board watcher dropped the one Gemini review Mr. Salam approved, because
        Grok's text told Gemini how to reply: "Reply RESULT answers=..."."""
        r = row("grok", "gemini",
                "BCB|v=1|id=GROK-GOV-ARCH-REVIEW-GEMINI-1415|phase=REQUEST|from=grok|to=gemini|text=ONE "
                "ASK for Gemini. Reply RESULT answers=GROK-GOV-ARCH-REVIEW-GEMINI-1415 with evidence=STATED.")
        self.assertFalse(aw.is_waker_reply(r))
        self.assertEqual(len(aw.select([r], set(), "gemini")), 1)
        # Even where the phase is not an ask phase, a mention in the text is not the field.
        for payload in ("BCB|v=1|id=U-1|phase=UPDATE|to=gemini|text=re-check, then reply answers=U-1",
                        "BCB|v=1|id=U-2|to=gemini|text=see answers=X for the format"):
            self.assertFalse(aw.is_waker_reply(row("grok", "gemini", payload)), payload)

    def test_a_peers_dispatch_that_follows_up_is_still_an_ask(self):
        for phase in ("DISPATCH", "REQUEST", "TASK", "ASK"):
            r = row("grok", "gemini", "BCB|v=1|id=G-%s|phase=%s|answers=EARLIER-1|ask=review this" % (phase, phase))
            self.assertFalse(aw.is_waker_reply(r), phase)

    def test_a_peers_result_or_done_with_the_field_is_still_a_reply(self):
        for phase in ("RESULT", "DONE", "NOTE"):
            r = row("grok", "gemini", "BCB|v=1|id=G-%s|phase=%s|answers=EARLIER-1|text=done" % (phase, phase))
            self.assertTrue(aw.is_waker_reply(r), phase)

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


class StandbyForHimOnly(unittest.TestCase):
    """claude-api answers HIS messages to claude-code-cli, and nothing else.

    He wrote "Claude-code-cli can you answers questions on health science?" on
    WhatsApp at 2026-09-20T21:23Z. It reached the board and sat three hours,
    because foundry, gemini and grok each have a doorbell and the
    claude-code-cli tag has none.

    The standby is deliberately narrow. The fleet writes to claude-code-cli
    constantly - every waker reply is addressed to it, and codex sends it long
    technical reviews that want a lane with a shell, not a model. Answering
    those would be noise and spend. His messages are the only ones with nobody
    else watching them.
    """

    HIS = "Claude-code-cli can you answers questions on health science?"

    def test_his_whatsapp_message_reaches_the_standby(self):
        r = row("whatsapp", "Blackboard Alpha DB", self.HIS)
        self.assertTrue(aw.addressed_to(r, "claude-api"))
        self.assertEqual(len(aw.select([r], set(), "claude-api")), 1)

    def test_a_row_addressed_to_it_directly_still_works(self):
        self.assertTrue(aw.addressed_to(row("whatsapp", "claude-api", "x"), "claude-api"))

    def test_codex_reviews_to_claude_code_cli_are_left_alone(self):
        """These want the lane with a shell. A model answering them is noise."""
        r = row("chatgpt-codex-desktop", "claude-code-cli,ALL",
                "BCB|v=1|to=claude-code-cli|a long technical review")
        self.assertFalse(aw.addressed_to(r, "claude-api"))

    def test_waker_replies_to_claude_code_cli_are_left_alone(self):
        r = row("gemini", "claude-code-cli;ALL",
                "%s|answers=X|evidence=STATED|y" % aw.WAKER_REPLY_MARK)
        self.assertFalse(aw.addressed_to(r, "claude-api"))

    def test_it_does_not_take_another_agents_prefix(self):
        """He addresses one agent at a time. Grok's mail stays grok's."""
        r = row("whatsapp", "Blackboard Alpha DB", "Grok can you reply?")
        self.assertFalse(aw.addressed_to(r, "claude-api"))
        self.assertTrue(aw.addressed_to(r, "grok"))

    def test_the_standby_is_not_the_lane_it_stands_in_for(self):
        """The doctrine must say so. claude-code-cli means a shell and a PR;
        a model endpoint answering under that name is the false-capability
        claim every doctrine here exists to stop."""
        d = aw.AGENTS["claude-api"]["doctrine"]
        self.assertIn("claude-api", d)
        self.assertIn("NOT", d)
        self.assertIn("claude-code-cli", d)
        for cannot in ("no shell", "no repository"):
            self.assertIn(cannot, d.lower())

    def test_only_the_standby_has_standby_config(self):
        for tag, cfg in aw.AGENTS.items():
            with self.subTest(tag=tag):
                if tag == "claude-api":
                    self.assertEqual(cfg["standby_when_sender"], ("whatsapp",))
                else:
                    self.assertNotIn("standby_for", cfg)


class ReplyRowId(unittest.TestCase):
    def test_the_readable_prefix_keeps_the_source_id_and_a_hash_of_all_of_it(self):
        rid = aw.reply_row_id("gemini", "SYNTHETIC.ASK")
        self.assertTrue(rid.startswith("GEMINI-WAKE-SYNTHETIC.ASK-"), rid)
        self.assertEqual(len(rid.rsplit("-", 1)[-1]), 10)

    def test_a_dot_and_an_underscore_do_not_share_a_reply_id(self):
        first, second = "SYNTHETIC.ASK", "SYNTHETIC_ASK"
        self.assertEqual(aw.legacy_reply_row_id("gemini", first),
                         aw.legacy_reply_row_id("gemini", second))
        self.assertNotEqual(aw.reply_row_id("gemini", first),
                            aw.reply_row_id("gemini", second))

    def test_two_long_ids_with_the_same_forty_character_prefix_do_not_collide(self):
        first, second = ("L" * 40) + "ONE", ("L" * 40) + "TWO"
        self.assertEqual(aw.legacy_reply_row_id("gemini", first),
                         aw.legacy_reply_row_id("gemini", second))
        self.assertNotEqual(aw.reply_row_id("gemini", first),
                            aw.reply_row_id("gemini", second))
        self.assertTrue(aw.reply_row_id("gemini", first).startswith("GEMINI-WAKE-" + ("L" * 40) + "-"))


class ReplyPhaseTest(unittest.TestCase):
    """A waker's answer to a row that asks for a result is a RESULT (the owner via Grok, 2026-09-29:
    Gemini never posted one; every waker reply was phase=DONE)."""

    def row(self, payload):
        return ["id", "2026-09-29T06:24:08Z", "grok", "gemini", "APPEND", payload]

    def test_a_dispatch_or_ask_is_answered_with_a_result(self):
        for phase in ("DISPATCH", "REVIEW_REQUEST", "REQUEST", "REVIEW", "ORDER", "TASK", "HANDOFF",
                      "BATON", "ASK", "dispatch"):
            self.assertEqual("RESULT", aw.reply_phase(self.row("BCB|v=1|id=X|phase=%s|from=grok|to=gemini" % phase)), phase)

    def test_anything_else_is_answered_with_done(self):
        for payload in ("BCB|v=1|id=X|phase=NOTE|from=grok", "BCB|v=1|id=X|phase=RESULT|from=codex",
                        "BCB|v=1|id=X|phase=REVIEW_RESULT|from=codex", "BCB|v=1|id=X|phase=REQ|from=grok",
                        "BCB|v=1|id=X|phase=PROGRESS|from=grok", "BCB|v=1|id=X|phase=HOLD|from=grok",
                        "no bcb payload at all", "BCB|v=1|id=X|note=phase=DISPATCH-like text"):
            self.assertEqual("DONE", aw.reply_phase(self.row(payload)), payload)
        self.assertEqual("DONE", aw.reply_phase(["id", "ts"]))                   # a short row

    def test_the_phase_reaches_the_post(self):
        from unittest import mock
        seen = []

        def run(args, **kw):
            seen.append(args)
            return mock.Mock(stdout="VERIFIED on the board", stderr="")
        with mock.patch.object(aw.subprocess, "run", side_effect=run):
            self.assertTrue(aw.post_reply("gemini", {"project": "Blackboard"}, "text", "grok;ALL", "X", False,
                                          phase="RESULT"))
        self.assertEqual("RESULT", seen[0][seen[0].index("--phase") + 1])

    def test_the_set_is_the_board_s_measured_ask_set_plus_ask(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "tools", "board_governor"))
        import board_facts
        self.assertEqual(set(board_facts.ASK_PHASES) | {"ASK"}, set(aw.RESULT_FOR))

    def test_fleet_agent_parses_a_result_post(self):
        import fleet_agent
        args = fleet_agent.build_parser().parse_args(["post", "text", "--phase", "RESULT"])
        self.assertEqual("RESULT", args.phase)
        with self.assertRaises(SystemExit):                       # the choices still bind
            fleet_agent.build_parser().parse_args(["post", "text", "--phase", "FINISHED"])


class ALongAnswerIsNeverTrimmed(unittest.TestCase):
    """Grok's scorecard, 2026-10-09 12:25Z: all 13 of 13 Gemini waker replies that day stopped
    mid-word at about 1,620 characters, because post_reply posted text[:1500]. A row may still be
    the shorter thing; what it may never be is silently shorter, or cut inside a word."""

    LONG = ("Redis is reachable from the worker pool over Direct VPC egress. "
            "The chair reads the live state each turn and caches it for five seconds. ") * 40

    def test_the_answer_is_five_thousand_characters(self):
        self.assertGreater(len(self.LONG), 5000)                  # the case Grok asked to be tested

    def test_a_five_thousand_character_answer_ends_in_a_whole_sentence(self):
        head = "wakerreply=1|answers=X|evidence=STATED|route=r|REPLY: "
        tail = " okf=https://github.com/sfdc-24/conference/pull/900"
        self.assertTrue(aw.over_board_cap(head, self.LONG, tail))
        row = aw.fit_reply(head, self.LONG, tail)
        self.assertLessEqual(len(row), aw.BOARD_TEXT_CAP)
        self.assertTrue(row.startswith(head))
        self.assertTrue(row.endswith(tail))
        body = row[len(head):-len(tail)]
        self.assertIn(body, " ".join(self.LONG.split()))          # a prefix of what was said
        self.assertTrue(body.rstrip().endswith("."))              # a whole sentence, not a cut word
        self.assertNotIn("  ", body)

    def test_the_row_says_how_long_the_whole_answer_is_and_where_it_is(self):
        head = "wakerreply=1|answers=X|evidence=STATED|route=r|okf=https://x/900|REPLY: "
        pr = "https://github.com/sfdc-24/conference/pull/900"
        tail = (" okf=%s [This row carries as much of the answer as fits; the whole %d-character"
                " answer is the OKF file at %s.]" % (pr, len(self.LONG), pr))
        row = aw.fit_reply(head, self.LONG, tail)
        self.assertLessEqual(len(row), aw.BOARD_TEXT_CAP)
        self.assertIn(str(len(self.LONG)), row)
        self.assertIn(pr, row)

    def test_a_short_answer_is_not_touched_at_all(self):
        head, tail = "wakerreply=1|REPLY: ", " okf=https://x/1"
        short = "Yes. Redis is live on the pool."
        self.assertFalse(aw.over_board_cap(head, short, tail))
        self.assertEqual(head + short + tail, aw.fit_reply(head, short, tail))

    def test_a_cut_never_lands_inside_a_word(self):
        for cap in range(20, 400, 7):
            cut = aw.ends_whole(self.LONG, cap)
            self.assertLessEqual(len(cut), cap)
            self.assertTrue(self.LONG.startswith(cut), cap)
            rest = self.LONG[len(cut):]
            self.assertTrue(not rest or not cut or cut[-1] in " .!?" or rest[0] in " .!?",
                            "cut inside a word at cap %d: %r|%r" % (cap, cut[-12:], rest[:12]))

    def test_post_reply_no_longer_trims_mid_word(self):
        from unittest import mock
        seen = []

        def run(args, **kw):
            seen.append(args)
            return mock.Mock(stdout="VERIFIED on the board", stderr="")
        with mock.patch.object(aw.subprocess, "run", side_effect=run):
            self.assertTrue(aw.post_reply("gemini", {"project": "Blackboard"}, self.LONG,
                                          "grok;ALL", "X", False))
        posted = seen[0][seen[0].index("post") + 1]
        self.assertLessEqual(len(posted), aw.BOARD_TEXT_CAP)
        self.assertTrue(posted.rstrip().endswith(".") or posted[-1] != self.LONG[len(posted)])
        self.assertNotEqual(self.LONG[:1500], posted)             # the old trim, gone
        gist = seen[0][seen[0].index("--gist") + 1]
        self.assertLessEqual(len(gist), 160)
        self.assertTrue(self.LONG.startswith(gist))

    def test_the_reason_a_file_was_being_landed_is_named(self):
        self.assertIn("asked for", aw.okf_reason("BCB|v=1|land=okf|ask=write it", False))
        self.assertIn("too long", aw.okf_reason("BCB|v=1|ask=just answer", True))


class TheBoardIsTheSharedDEDUPE(unittest.TestCase):
    """MEASURED ON THE LIVE BOARD, 2026-10-07. One Row_ID,
    GEMINI-WAKE-CCC-GEMINI-COMPARISON-DESIGN-20261007T04-be7c918643, appended TWICE with DIFFERENT
    answers 4.5 minutes apart - 989 characters at 05:21:14Z and 874 at 05:25:56Z. One identifier,
    two different statements of what Gemini said.

    reply_row_id() is deterministic, so a second answer necessarily reuses the first's Row_ID. The
    only dedupe was `answered_ids`, the laptop's state file, with claim_answer() returning True
    because "the laptop waker is the only writer of its file, so the claim is free" - true of the
    FILE, false of the BOARD. cloud/agent-waker dedupes against a shared GCS store instead, so the
    laptop pass and the cloud pass each looked in their own store and each answered.

    The board is the only store both runners share."""

    def test_an_ask_already_answered_on_the_board_is_not_selected(self):
        """With EMPTY local state, which is the cloud runner's view of a laptop pass."""
        ask = row("claude-code-cli", "gemini", "BCB|v=1|id=ASK-1|phase=ASK|text=q",
                  ts="2026-09-19T12:00:00Z", rid="ASK-1")
        reply = row("gemini", "claude-code-cli;ALL",
                    "BCB|v=1|id=GEMINI-WAKE-ASK-1|wakerreply=1|answers=ASK-1|text=a",
                    ts="2026-09-19T12:04:00Z", rid="GEMINI-WAKE-ASK-1")
        self.assertEqual([], aw.select([ask, reply], set(), "gemini"),
                         "the board already shows this answered")

    def test_an_unanswered_ask_is_still_selected(self):
        """The positive control. If this ever fails, the guard above is a mute button."""
        ask = row("claude-code-cli", "gemini", "BCB|v=1|id=ASK-2|phase=ASK|text=q",
                  ts="2026-09-19T12:00:00Z", rid="ASK-2")
        self.assertEqual(1, len(aw.select([ask], set(), "gemini")))

    def test_another_agents_reply_does_not_count_as_mine(self):
        """grok answering ASK-3 must not stop gemini from answering it."""
        ask = row("claude-code-cli", "gemini;grok", "BCB|v=1|id=ASK-3|phase=ASK|text=q",
                  ts="2026-09-19T12:00:00Z", rid="ASK-3")
        theirs = row("grok", "claude-code-cli;ALL",
                     "BCB|v=1|id=GROK-WAKE-ASK-3|wakerreply=1|answers=ASK-3|text=a",
                     ts="2026-09-19T12:01:00Z", rid="GROK-WAKE-ASK-3")
        self.assertEqual(1, len(aw.select([ask, theirs], set(), "gemini")))

    def test_answered_on_board_reads_only_my_marked_replies(self):
        mine = row("gemini", "x;ALL", "BCB|v=1|wakerreply=1|answers=A-1", rid="r1")
        unmarked = row("gemini", "x;ALL", "BCB|v=1|answers=A-2", rid="r2")
        other = row("grok", "x;ALL", "BCB|v=1|wakerreply=1|answers=A-3", rid="r3")
        self.assertEqual({"A-1"}, aw.answered_on_board([mine, unmarked, other], "gemini"))

    def test_the_reply_row_id_is_deterministic_which_is_why_this_matters(self):
        """Two answers to one ask cannot coexist: they are the same Row_ID by construction. The
        dedupe has to happen BEFORE the model call, not be caught at the append."""
        self.assertEqual(aw.reply_row_id("gemini", "ASK-1"),
                         aw.reply_row_id("gemini", "ASK-1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

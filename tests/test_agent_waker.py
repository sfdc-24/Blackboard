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





class RedisRequests(unittest.TestCase):
    """A REDIS-OP line in a reply becomes one governed request row, posted by the adapter.

    The access list has named gemini and grok since 2026-10-08, and neither could produce the one row
    the worker reads (Action_Type AYA_REQ): post_reply writes a phase row. These pin the only path
    a waker's model has - the structured line - and that it never leaks into the pipe-delimited row.
    Grok's NO-GO on #367 added the rest: the line is read strictly, a model may name only the
    governed namespaces, a cut reply sends nothing, and every outcome is its own state on the row.
    """

    REQ = {"op": "xadd", "key": "conf:side:20261011", "val": "v", "field": None, "count": None}

    def setUp(self):
        # The cloud runner provides the durable posting claim; these tests stand in for it. The one
        # test about the default restores the real function, which refuses.
        self._real_reserve = aw.reserve_redis_side_effect
        aw.reserve_redis_side_effect = lambda answers_id: True

    def tearDown(self):
        aw.reserve_redis_side_effect = self._real_reserve

    def receipt(self, me, answers=""):
        """The one line fleet_agent prints for THIS request, and nothing else counts."""
        return "  VERIFIED on the board: Row_ID=%s Source_Tag=%s Target_Surface=bus-reconciler" % (
            aw.request_id(me, answers), me)

    def _run(self, out, calls=None, code=0):
        """Replace subprocess.run with one that records argv and answers `out` with exit `code`."""
        class Done:
            def __init__(self, text):
                self.stdout, self.stderr, self.returncode = text, "", code

        def fake(args, **kw):
            if calls is not None:
                calls.append(args)
            return Done(out)
        return fake

    def test_a_well_formed_line_is_the_request_and_leaves_the_body(self):
        req, body = aw.redis_request("Short answer.\nREDIS-OP|op=xadd|key=conf:side:20261011|val=the lease holds\nMore.")
        self.assertEqual({"op": "xadd", "key": "conf:side:20261011", "val": "the lease holds",
                          "field": None, "count": None}, req)
        self.assertEqual("Short answer.\nMore.", body)

    def test_the_mark_is_case_insensitive_and_a_trailing_pipe_is_forgiven(self):
        req, _ = aw.redis_request("redis-op|op=get|key=proj:first|")
        self.assertEqual(("get", "proj:first"), (req["op"], req["key"]))

    def test_a_repeated_field_is_malformed_never_a_guess(self):
        # Grok on #367: op=xadd|op=del was quietly an add; key=a|key=b was quietly a.
        for text in ("REDIS-OP|op=xadd|op=del|key=proj:a|val=1",
                     "REDIS-OP|op=get|key=proj:first|key=proj:second"):
            with self.subTest(text=text):
                report = {}
                req, body = aw.redis_request(text, report)
                self.assertIsNone(req)
                self.assertIn("a repeated field", body)
                self.assertEqual(1, report["dropped"])

    def test_a_pipe_inside_a_value_or_an_unknown_field_is_malformed(self):
        # Grok on #367: val=a|b was quietly a, with the rest of the value lost.
        for text, why in (("REDIS-OP|op=set|key=proj:a|val=a|b", "a segment with no field name"),
                          ("REDIS-OP|op=set|key=proj:a|val=a|ttl=5", "a field that is not one of"),
                          ("REDIS-OP|op=set|key=proj:a|val=a|=b", "a segment with no field name")):
            with self.subTest(text=text):
                req, body = aw.redis_request(text)
                self.assertIsNone(req)
                self.assertIn(why, body)
                self.assertNotIn("|", body.splitlines()[-1], "the note itself carries no pipe")

    def test_a_mention_in_prose_is_not_a_request(self):
        req, body = aw.redis_request("As the doctrine says, a REDIS-OP|op=get line asks for a read.")
        self.assertIsNone(req)
        self.assertIn("REDIS-OP|op=get", body)      # prose stays prose

    def test_one_request_per_reply_and_the_rest_is_said_not_dropped(self):
        req, body = aw.redis_request("REDIS-OP|op=set|key=proj:a|val=1\nREDIS-OP|op=set|key=proj:b|val=2\nprose")
        self.assertEqual("proj:a", req["key"])
        self.assertNotIn("REDIS-OP|", body.splitlines()[0])
        self.assertIn("1 REDIS-OP line in this reply could not be posted", body)
        self.assertIn("one request per reply", body)

    def test_a_malformed_line_makes_no_request_and_never_reaches_the_row(self):
        for text in ("REDIS-OP|op=set|key=proj:a",            # a write with no value
                     "REDIS-OP|op=get|key=proj:a b",           # whitespace in the key
                     "REDIS-OP|op=flushall|key=proj:a",        # not an op the governor knows
                     "REDIS-OP|key=proj:a"):                   # no op at all
            with self.subTest(text=text):
                req, body = aw.redis_request(text)
                self.assertIsNone(req)
                self.assertNotIn("REDIS-OP|", body)
                self.assertIn("could not be posted", body)

    def test_a_read_carries_its_count(self):
        req, _ = aw.redis_request("REDIS-OP|op=xrange|key=conf:side:20261011|count=5")
        self.assertEqual(("xrange", "5"), (req["op"], req["count"]))

    def test_a_count_that_is_not_a_small_whole_number_is_malformed_and_never_echoed(self):
        # count is model-chosen; argparse would otherwise echo a bad one into the output and the reply.
        for count in ("abc", "-1", "0", "10000", "5 --val x", "AKIAIOSFODNN7EXAMPLE"):
            with self.subTest(count=count):
                req, body = aw.redis_request("REDIS-OP|op=xrange|key=conf:side:20261011|count=%s" % count)
                self.assertIsNone(req)
                self.assertIn("count is not a whole number", body)
                self.assertNotIn(count, body)

    def test_post_request_goes_through_fleet_agent_request_and_trusts_only_the_read_back(self):
        calls = []
        real = aw.subprocess.run
        aw.subprocess.run = self._run("  request X: xadd conf:side:20261011\n" + self.receipt("gemini"), calls)
        try:
            state, rid = aw.post_request("gemini", {"project": "FLEET"},
                                         dict(self.REQ, val="v|with pipe"), verbose=False)
        finally:
            aw.subprocess.run = real
        self.assertEqual(("posted", aw.request_id("gemini", "")), (state, rid))
        argv = calls[0]
        self.assertTrue(argv[1].endswith(os.path.join("scripts", "fleet_agent.py")))
        self.assertEqual(["request", "xadd", "conf:side:20261011", "--tag", "gemini", "--project", "FLEET",
                          "--id", aw.request_id("gemini", ""), "--val", "v|with pipe"], argv[2:])

    def test_post_request_without_the_read_back_is_unconfirmed_not_refused(self):
        real = aw.subprocess.run
        aw.subprocess.run = self._run("  append said: ok:true\n  NOT FOUND after append")
        try:
            state, why = aw.post_request("grok", {"project": "FLEET"}, dict(self.REQ, op="xrange", val=None), False)
        finally:
            aw.subprocess.run = real
        self.assertEqual("unconfirmed", state)
        self.assertIn("NOT FOUND", why)

    def test_what_fleet_agent_refused_before_posting_is_refused_here_too(self):
        # The tool says "refused before posting" (its own gates, or an id already used by a different
        # request) with exit 2, and "NOT SENT" (the board could not be read first) with exit 1:
        # nothing was appended. The words and the exit code must agree; words alone with exit 0 are
        # a contradiction, and a contradiction is unconfirmed (Aya's supplement to #367).
        for out, code, expect in (
            ("  refused before posting: request id already used by a different request", 2, "refused"),
            ("  NOT SENT: the board could not be read before posting", 1, "refused"),
            ("  refused before posting: request id already used by a different request", 0, "unconfirmed"),
        ):
            with self.subTest(out=out, code=code):
                real = aw.subprocess.run
                aw.subprocess.run = self._run(out, code=code)
                try:
                    state, why = aw.post_request("grok", {"project": "FLEET"}, self.REQ, False)
                finally:
                    aw.subprocess.run = real
                self.assertEqual(expect, state)

    def test_the_named_agents_carry_the_flag_and_the_rules(self):
        for tag in ("gemini", "grok"):
            with self.subTest(tag=tag):
                cfg = aw.AGENTS[tag]
                self.assertTrue(cfg.get("redis_requests"))
                self.assertIn("REDIS-OP|op=xadd|key=conf:side:YYYYMMDD", cfg["doctrine"])
                self.assertIn("Every agent inbox and other key is refused", cfg["doctrine"])
                self.assertIn("redis_state=posted", cfg["doctrine"])
                self.assertIn("governed Redis request", aw.hands_note(tag, cfg))

    def test_a_credential_shaped_key_or_field_is_refused_before_anything_is_posted(self):
        # Copilot on #365: the key reaches the row's gist, the audit and the worker's logs verbatim.
        calls = []
        real = aw.subprocess.run
        aw.subprocess.run = self._run("", calls)
        try:
            for part, request in (
                ("key", dict(self.REQ, op="get", key="proj:AKIAIOSFODNN7EXAMPLE", val=None)),
                ("field", dict(self.REQ, op="hset", key="proj:h", field="AKIAIOSFODNN7EXAMPLE")),
            ):
                with self.subTest(part=part):
                    state, why = aw.post_request("gemini", {"project": "FLEET"}, request, False)
                    self.assertEqual("refused", state)
                    self.assertIn("nothing was sent", why)
        finally:
            aw.subprocess.run = real
        self.assertEqual([], calls, "no subprocess, so no row and no log line")

    def test_a_key_other_than_the_public_stream_is_refused_before_anything_is_posted(self):
        # Grok on #367 (item 1): the access list grants read `*`, so through this hook a model could
        # have gov:audit or ANOTHER agent's inbox read and its contents posted onto the board. One
        # rule, owned by fleet_agent, for the model's path and the command line alike.
        calls = []
        real = aw.subprocess.run
        aw.subprocess.run = self._run("", calls)
        try:
            for key in ("gov:audit", "v1:agent:grok:inbox", "v1:bus:row:R-1", "probe:x", "anything",
                        "fleet:inbox:grok", "fleet:status", "fleet:inbox:gemini-x"):
                for op in ("get", "xrange", "set"):
                    with self.subTest(key=key, op=op):
                        state, why = aw.post_request("gemini", {"project": "FLEET"},
                                                     dict(self.REQ, op=op, key=key, val="v"), False)
                        self.assertEqual("refused", state)
                        self.assertIn("only the public shared side stream", why)
                        self.assertNotIn(key, why, "the refused key is not echoed")
            # The heartbeat is not available over this unauthenticated board route.
            state, why = aw.post_request("gemini", {"project": "FLEET"},
                                         dict(self.REQ, op="set", key="gov:worker:heartbeat", val="v"), False)
            self.assertEqual("refused", state)
        finally:
            aw.subprocess.run = real
        self.assertEqual([], calls, "no subprocess, so no row, no audit entry and no log line")

    def test_public_side_stream_reads_and_writes_go_through(self):
        asked = aw.request_id("gemini", "")
        for op, key in (("xadd", "conf:side:20261011"), ("xrange", "conf:side:20261011")):
            with self.subTest(op=op, key=key):
                calls = []
                real = aw.subprocess.run
                aw.subprocess.run = self._run(self.receipt("gemini"), calls)
                try:
                    state, rid = aw.post_request("gemini", {"project": "FLEET"},
                                                 dict(self.REQ, op=op, key=key,
                                                      val="v" if op == "xadd" else None), False)
                finally:
                    aw.subprocess.run = real
                self.assertEqual(("posted", asked, 1), (state, rid, len(calls)))

    def test_fleet_agent_exit_codes_decide_refused_from_unconfirmed(self):
        # EXIT_NOT_SENT (1) and EXIT_REFUSED (2): nothing was appended. EXIT_UNCONFIRMED (3): it may have.
        for code, out, expect in ((1, "  NOT SENT: the board could not be read", "refused"),
                                  (2, "  refused before posting: request id X is already used", "refused"),
                                  (3, "  UNCONFIRMED: not found after the append", "unconfirmed"),
                                  (0, "  something else entirely", "unconfirmed")):
            with self.subTest(code=code):
                real = aw.subprocess.run
                aw.subprocess.run = self._run(out, code=code)
                try:
                    state, why = aw.post_request("grok", {"project": "FLEET"}, self.REQ, False)
                finally:
                    aw.subprocess.run = real
                self.assertEqual(expect, state)

    def test_the_hands_note_never_quotes_the_key_and_each_state_is_its_own_sentence(self):
        request = dict(self.REQ, op="get", key="proj:AKIAIOSFODNN7EXAMPLE", val=None)
        state, why = aw.post_request("gemini", {"project": "FLEET"}, request, False)
        self.assertEqual("refused", state)
        notes = {s: aw.request_note(request, s, {"posted": "GEMINI-REDIS-1", "cut": ""}.get(s, why))
                 for s in aw.REQUEST_STATES}
        for s, note in notes.items():
            with self.subTest(state=s):
                self.assertNotIn("AKIA", note)
                self.assertTrue(note, "every state has a sentence")
        self.assertIn("not confirmed as posted", notes["unconfirmed"])
        self.assertNotIn("not posted", notes["unconfirmed"])
        self.assertIn("nothing was sent", notes["refused"])
        self.assertIn("output cap", notes["cut"])
        self.assertIn("malformed", notes["malformed"])
        self.assertIn("GEMINI-REDIS-1", notes["posted"])
        self.assertEqual(len(aw.REQUEST_STATES), len(set(notes.values())), "no two states read alike")

    def test_the_request_id_is_stable_per_source_ask_so_a_retry_cannot_write_twice(self):
        first = aw.request_id("gemini", "GROK-ASK-REDIS-20261011T0400Z")
        self.assertEqual(first, aw.request_id("gemini", "GROK-ASK-REDIS-20261011T0400Z"))
        self.assertNotEqual(first, aw.request_id("gemini", "GROK-ASK-REDIS-20261011T0401Z"))
        self.assertNotEqual(first, aw.request_id("grok", "GROK-ASK-REDIS-20261011T0400Z"))
        self.assertRegex(first, r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$")      # bus_request's id grammar
        argv = []
        real = aw.subprocess.run
        aw.subprocess.run = self._run("  VERIFIED on the board: Row_ID=GEMINI-REDIS-x Source_Tag=gemini", argv)
        try:
            aw.post_request("gemini", {"project": "FLEET"}, self.REQ, False, answers="GROK-ASK-REDIS-20261011T0400Z")
        finally:
            aw.subprocess.run = real
        self.assertIn("--id", argv[0])
        self.assertEqual(first, argv[0][argv[0].index("--id") + 1])

    def test_a_missing_read_back_is_unconfirmed_never_absent(self):
        real = aw.subprocess.run
        aw.subprocess.run = self._run("  append said: ok:true\n  COULD NOT VERIFY")
        try:
            state, why = aw.post_request("grok", {"project": "FLEET"}, dict(self.REQ, op="xrange", val=None), False)
        finally:
            aw.subprocess.run = real
        self.assertEqual("unconfirmed", state)
        self.assertNotIn("not posted", why)

    def test_the_redis_line_is_named_as_the_one_exception_to_no_pipes(self):
        for tag in ("gemini", "grok"):
            with self.subTest(tag=tag):
                self.assertIn("ONE\n  exception to the no-pipes rule", aw.AGENTS[tag]["doctrine"])

    # THE HOOK AS MAIN RUNS IT: the cut check, the states on the row, and what the log line carries.

    def test_a_cut_reply_sends_nothing_and_says_so(self):
        # Grok on #367: a del on a key shortened by the output cap went through, because the cut
        # check ran after the post. Now nothing is sent from a cut reply, and the row says cut.
        calls = []
        real = aw.subprocess.run
        aw.subprocess.run = self._run("  VERIFIED on the board: Row_ID=x Source_Tag=gemini", calls)
        try:
            text, rid, state, sentence, note = aw.redis_hook(
                "gemini", aw.AGENTS["gemini"], "Done.\nREDIS-OP|op=del|key=proj:sho", "ASK-1",
                model_cut=True, verbose=False)
        finally:
            aw.subprocess.run = real
        self.assertEqual([], calls, "nothing is sent from a cut reply")
        self.assertEqual(("Done.", "cut"), (text, state))
        self.assertEqual(aw.request_id("gemini", "ASK-1"), rid)
        self.assertIn("was not sent", sentence)
        self.assertNotIn("proj:sho", sentence)

    def test_the_hook_posts_an_intact_reply_and_names_the_row(self):
        calls = []
        asked = aw.request_id("gemini", "ASK-1")
        real = aw.subprocess.run
        aw.subprocess.run = self._run(self.receipt("gemini", "ASK-1"), calls)
        try:
            text, rid, state, sentence, note = aw.redis_hook(
                "gemini", aw.AGENTS["gemini"], "Done.\nREDIS-OP|op=xadd|key=conf:side:20261011|val=v", "ASK-1",
                model_cut=False, verbose=False)
        finally:
            aw.subprocess.run = real
        self.assertEqual(1, len(calls))
        self.assertEqual(asked, calls[0][calls[0].index("--id") + 1], "the id asked for")
        self.assertEqual(("Done.", "posted", asked), (text, state, rid), "the id read back")
        self.assertIn("as row %s" % asked, sentence)
        self.assertIn("conf:side:20261011", note, "a posted key may be logged: it passed every gate")

    def test_a_refused_key_reaches_neither_the_sentence_nor_the_log_line(self):
        calls = []
        real = aw.subprocess.run
        aw.subprocess.run = self._run("", calls)
        try:
            text, rid, state, sentence, note = aw.redis_hook(
                "gemini", aw.AGENTS["gemini"], "Done.\nREDIS-OP|op=xrange|key=gov:audit|count=50", "ASK-1",
                model_cut=False, verbose=False)
        finally:
            aw.subprocess.run = real
        self.assertEqual([], calls)
        self.assertEqual("refused", state)
        self.assertTrue(rid)
        for carried in (sentence, note):
            self.assertNotIn("gov:audit", carried)

    def test_a_malformed_line_is_its_own_state_and_still_carries_the_asks_id(self):
        calls = []
        real = aw.subprocess.run
        aw.subprocess.run = self._run("", calls)
        try:
            text, rid, state, sentence, note = aw.redis_hook(
                "grok", aw.AGENTS["grok"], "REDIS-OP|op=xadd|op=del|key=proj:a|val=1\nprose", "ASK-2",
                model_cut=False, verbose=False)
        finally:
            aw.subprocess.run = real
        self.assertEqual([], calls)
        self.assertEqual(("malformed", aw.request_id("grok", "ASK-2")), (state, rid))
        self.assertIn("a repeated field", sentence)
        self.assertIn("could not be posted", text)

    def test_a_receipt_that_is_not_this_requests_is_unconfirmed_never_posted(self):
        # Aya's supplement to #367: "posted" means THIS request's receipt and nothing contradicting it.
        asked = aw.request_id("gemini", "")
        for out in ("  VERIFIED on the board: Row_ID=GEMINI-REDIS-other Source_Tag=gemini Target_Surface=bus-reconciler",
                    "  VERIFIED on the board: Row_ID=%s Source_Tag=grok Target_Surface=bus-reconciler" % asked,
                    "  VERIFIED on the board: Row_ID=%s Source_Tag=gemini Target_Surface=grok" % asked,
                    self.receipt("gemini") + "\n  NOT SENT: the board could not be read",
                    self.receipt("gemini") + "\n" + self.receipt("gemini")):
            with self.subTest(out=out[-60:]):
                real = aw.subprocess.run
                aw.subprocess.run = self._run(out)
                try:
                    state, why = aw.post_request("gemini", {"project": "FLEET"}, self.REQ, False)
                finally:
                    aw.subprocess.run = real
                self.assertEqual("unconfirmed", state)

    def test_the_three_words_count_only_at_the_start_of_a_line_and_the_reason_carries_no_key_or_pipe(self):
        real = aw.subprocess.run
        aw.subprocess.run = self._run("  request X: set proj:k\n" + self.receipt("gemini")
                                      + "\n  note: the word UNCONFIRMED inside a sentence is not a verdict")
        try:
            state, rid = aw.post_request("gemini", {"project": "FLEET"}, self.REQ, False)
        finally:
            aw.subprocess.run = real
        self.assertEqual("posted", state)
        aw.subprocess.run = self._run("  UNCONFIRMED: Row_ID=X has changed content; do not reuse the id.\n"
                                      "  expected: 'BCB|v=1|id=X|req=X|do=redis-op|op=set|key=proj:k'", code=3)
        try:
            state, why = aw.post_request("gemini", {"project": "FLEET"}, self.REQ, False)
        finally:
            aw.subprocess.run = real
        self.assertEqual("unconfirmed", state)
        self.assertNotIn("|", why, "the reason rides a pipe-delimited row")
        self.assertNotIn("proj:k", why, "the key is masked everywhere the output goes")

    def test_an_empty_val_on_a_write_is_said_as_such_and_never_sent(self):
        req, body = aw.redis_request("REDIS-OP|op=set|key=proj:k|val=")
        self.assertIsNone(req)
        self.assertIn("a write with no val", body)
        req, _ = aw.redis_request("REDIS-OP|op=get|key=proj:k|val=")
        self.assertIsNotNone(req, "an empty field is absent, not a shape error")
        self.assertIsNone(req["val"])

    def test_a_timeout_is_unconfirmed_even_when_the_captured_marker_is_missing(self):
        # TimeoutExpired.output can be None after a child flushed its marker and appended.
        def raising(partial):
            def run(args, **kw):
                raise aw.subprocess.TimeoutExpired(cmd=args, timeout=1, output=partial)
            return run
        real = aw.subprocess.run
        try:
            for partial in (None, "  request X: xadd conf:side:20261011\n",
                            b"  appending Row_ID=X\n"):
                with self.subTest(partial=partial):
                    aw.subprocess.run = raising(partial)
                    state, why = aw.post_request("gemini", {"project": "FLEET"}, self.REQ, False)
                    self.assertEqual("unconfirmed", state)
                    self.assertIn("may have landed", why)
        finally:
            aw.subprocess.run = real

    def test_post_reply_timeout_is_unknown_and_only_a_completed_refusal_is_not_sent(self):
        def raising(partial):
            def run(args, **kw):
                raise aw.subprocess.TimeoutExpired(cmd=args, timeout=1, output=partial)
            return run
        real = aw.subprocess.run
        try:
            for partial in (None, "  reading the board first\n",
                            "  appending Row_ID=GEMINI-WAKE-x\n"):
                with self.subTest(partial=partial):
                    aw.subprocess.run = raising(partial)
                    res = aw.post_reply("gemini", {"project": "FLEET"}, "text", "grok;ALL", "ASK-1", False)
                    self.assertIs(False, res)
            aw.subprocess.run = self._run("  NOT SENT: the board could not be read", code=1)
            self.assertIs(aw.NOT_SENT, aw.post_reply(
                "gemini", {"project": "FLEET"}, "text", "grok;ALL", "ASK-1", False))
        finally:
            aw.subprocess.run = real

    def test_a_local_child_can_write_then_timeout_with_no_reported_output(self):
        """Reproduce the timing with a local file; the adapter still treats absent output as unknown."""
        import tempfile
        from pathlib import Path
        real = aw.subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            proof = Path(directory) / "child-wrote.txt"

            def timed_out_after_side_effect(args, **kw):
                child = ("import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('wrote'); "
                         "print('appending Row_ID=X', flush=True); time.sleep(5)")
                with self.assertRaises(aw.subprocess.TimeoutExpired):
                    real([sys.executable, "-c", child, str(proof)], timeout=1,
                         capture_output=True, text=True)
                self.assertEqual("wrote", proof.read_text())
                # A parent can lose the child's captured bytes despite its flush and side effect.
                raise aw.subprocess.TimeoutExpired(cmd=args, timeout=1, output=None)

            aw.subprocess.run = timed_out_after_side_effect
            try:
                state, _ = aw.post_request("gemini", {"project": "FLEET"}, self.REQ, False)
                self.assertEqual("unconfirmed", state)
                proof.unlink()
                self.assertIs(False, aw.post_reply(
                    "gemini", {"project": "FLEET"}, "text", "grok;ALL", "ASK-1", False))
                self.assertEqual("wrote", proof.read_text())
            finally:
                aw.subprocess.run = real

    def test_without_a_durable_posting_claim_a_request_is_refused_before_the_subprocess(self):
        # Outside the cloud runner nothing can fence a retry, so the adapter fails closed.
        aw.reserve_redis_side_effect = self._real_reserve
        self.assertFalse(aw.reserve_redis_side_effect("ASK-1"))
        calls = []
        real = aw.subprocess.run
        aw.subprocess.run = self._run(self.receipt("gemini", "ASK-1"), calls)
        try:
            text, rid, state, sentence, note = aw.redis_hook(
                "gemini", aw.AGENTS["gemini"], "Done.\nREDIS-OP|op=xadd|key=conf:side:20261011|val=v", "ASK-1",
                model_cut=False, verbose=False)
        finally:
            aw.subprocess.run = real
        self.assertEqual(([], "refused"), (calls, state))
        self.assertIn("durable posting claim", sentence)
        self.assertEqual(aw.request_id("gemini", "ASK-1"), rid)

    def test_a_refused_key_takes_no_claim_and_a_cut_reply_takes_none_either(self):
        taken = []
        aw.reserve_redis_side_effect = lambda answers_id: taken.append(answers_id) or True
        for text, cut in (("REDIS-OP|op=xrange|key=gov:audit|count=5", False),
                          ("REDIS-OP|op=del|key=proj:sho", True)):
            with self.subTest(text=text):
                _, rid, state, sentence, note = aw.redis_hook("gemini", aw.AGENTS["gemini"], text, "ASK-9",
                                                              model_cut=cut, verbose=False)
                self.assertIn(state, ("refused", "cut"))
        self.assertEqual([], taken, "the claim is taken only for a request that will be sent")

    def test_each_op_takes_exactly_its_fields(self):
        for text in ("REDIS-OP|op=get|key=proj:x|val=1",            # a read with a value
                     "REDIS-OP|op=del|key=proj:x|count=3",          # a delete with a count
                     "REDIS-OP|op=hset|key=proj:h|val=v",           # an hset with no field
                     "REDIS-OP|op=xadd|key=conf:side:1|val=v|count=2",
                     "REDIS-OP|op=set|key=proj:x|field=f|val=v"):
            with self.subTest(text=text):
                req, body = aw.redis_request(text)
                self.assertIsNone(req)
                self.assertIn("takes exactly", body)
        for text in ("REDIS-OP|op=xrange|key=conf:side:1", "REDIS-OP|op=xrange|key=conf:side:1|count=8",
                     "REDIS-OP|op=hset|key=proj:h|field=f|val=v", "REDIS-OP|op=del|key=proj:x",
                     "REDIS-OP|op=hgetall|key=proj:h"):
            with self.subTest(text=text):
                req, _ = aw.redis_request(text)
                self.assertIsNotNone(req, text)

    def test_verbose_output_is_scrubbed_and_the_key_masked(self):
        import contextlib
        import io
        real = aw.subprocess.run
        aw.subprocess.run = self._run("  request X: xadd conf:side:20261011 token AKIAIOSFODNN7EXAMPLE seen\n"
                                      "  UNCONFIRMED: not found after the append", code=3)
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                state, why = aw.post_request("gemini", {"project": "FLEET"}, self.REQ, verbose=True)
        finally:
            aw.subprocess.run = real
        self.assertEqual("unconfirmed", state)
        printed = captured.getvalue()
        self.assertIn("[key]", printed)
        self.assertNotIn("conf:side:20261011", printed)
        self.assertNotIn("AKIA", printed)
        self.assertNotIn("AKIA", why)

    def test_an_agent_without_the_flag_is_told_nothing_and_posts_nothing(self):
        for tag, cfg in aw.AGENTS.items():
            if cfg.get("redis_requests"):
                continue
            with self.subTest(tag=tag):
                self.assertNotIn("REDIS-OP", cfg["doctrine"])
                self.assertNotIn("governed Redis request", aw.hands_note(tag, cfg))
                out = aw.redis_hook(tag, cfg, "REDIS-OP|op=set|key=proj:a|val=1", "ASK-3", False, False)
                self.assertEqual(("REDIS-OP|op=set|key=proj:a|val=1", "", "", "", ""), out,
                                 "the line stays prose for an agent the hook is not on for")


class AnAdapterThatDoesNotSayIsTreatedAsCut(unittest.TestCase):
    """The cut gate fails closed: an adapter without a last_finish attribute at all gets no Redis
    request sent on its word (the second adversarial check on c519d1d found grok_agent in that state
    and the gate silently open). The adapters the hook is on for both report it; this pins the rule
    for the next one."""

    def test_every_hooked_agents_adapter_reports_its_finish(self):
        import importlib
        for tag, cfg in aw.AGENTS.items():
            if not cfg.get("redis_requests"):
                continue
            with self.subTest(tag=tag):
                mod = importlib.import_module(cfg["module"])
                source = open(mod.__file__, encoding="utf-8").read()
                self.assertIn("ask.last_finish", source, "%s never says whether its reply was cut" % cfg["module"])

    def test_the_rule_is_the_one_main_applies(self):
        # The same expression main() uses, on a bare function with no last_finish: cut.
        def bare_ask(*a, **k):
            return "x", "m"
        finish = getattr(bare_ask, "last_finish", None)
        self.assertTrue(finish == "MAX_TOKENS" or not hasattr(bare_ask, "last_finish"))
        bare_ask.last_finish = None
        self.assertFalse(getattr(bare_ask, "last_finish", None) == "MAX_TOKENS" or not hasattr(bare_ask, "last_finish"))
        source = open(aw.__file__, encoding="utf-8").read()
        self.assertIn('finish_known = hasattr(asker, "last_finish")', source)
        self.assertIn("finish_known=finish_known", source)

    def test_an_adapter_that_does_not_say_gets_no_request_sent_but_its_reply_is_not_marked_cut(self):
        # The hook fails closed; the row marker (model_cut) stays what the adapter said - here, nothing.
        real_reserve, real_run = aw.reserve_redis_side_effect, aw.subprocess.run
        calls = []
        aw.reserve_redis_side_effect = lambda a: True
        aw.subprocess.run = lambda args, **kw: calls.append(args)
        try:
            text, rid, state, sentence, note = aw.redis_hook(
                "gemini", aw.AGENTS["gemini"], "Done.\nREDIS-OP|op=set|key=proj:k|val=v", "ASK-7",
                model_cut=False, verbose=False, finish_known=False)
        finally:
            aw.reserve_redis_side_effect, aw.subprocess.run = real_reserve, real_run
        self.assertEqual(([], "cut", "Done."), (calls, state, text))
        self.assertIn("does not say whether the reply was cut", sentence)
        # and a reply that IS cut keeps its own sentence
        _, _, state2, sentence2, _ = aw.redis_hook("gemini", aw.AGENTS["gemini"], "REDIS-OP|op=get|key=proj:k",
                                                   "ASK-7", model_cut=True, verbose=False, finish_known=True)
        self.assertEqual("cut", state2)
        self.assertIn("output cap", sentence2)


class GrokSaysWhenItWasCut(unittest.TestCase):
    """Grok on #367 (item 4): the cut gate reads ask.last_finish, which only gemini_agent set. The
    xAI API says finish_reason "length" for a reply cut at max_tokens; grok_agent now says MAX_TOKENS
    on the function, the same word gemini_agent uses, so a cut Grok reply posts nothing either."""

    def test_finish_reason_length_is_MAX_TOKENS_on_the_function_and_nothing_else_is(self):
        import grok_agent
        real = grok_agent._post
        try:
            for reason, expect in (("length", "MAX_TOKENS"), ("stop", None), (None, None), ("LENGTH", "MAX_TOKENS")):
                grok_agent._post = lambda url, payload, timeout=600, _r=reason: {
                    "usage": {}, "choices": [{"message": {"content": "x"}, "finish_reason": _r}]}
                text, used = grok_agent.ask("q", model="grok-test")
                self.assertEqual(expect, grok_agent.ask.last_finish, reason)
        finally:
            grok_agent._post = real


if __name__ == "__main__":
    unittest.main()

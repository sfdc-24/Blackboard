"""scripts/unacked_watchdog.py: an ask to a live agent unanswered for 15 minutes raises ONE alert.

What matters: an answered ask is quiet; an old silent one alerts; a young one waits; an ask to several
agents alerts only for the silent ones; only the addressed agent's own row answers for it; an alert
already on the board or in state is never repeated; --dry-run writes nothing; the wake is one
foreground agent_waker pass per agent. No network: the reader, poster and waker are stand-ins.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import unacked_watchdog as uw  # noqa: E402

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
LIVE = {"gemini", "grok", "chatgpt-codex-desktop", "claude-code-cli"}
ALIAS = {"codex": "chatgpt-codex-desktop", "codex-desktop": "chatgpt-codex-desktop"}


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row(row_id, minutes_ago, sender, payload):
    return [row_id, ts(minutes_ago), sender, "ALL", "APPEND", payload, "", "", "", ""]


def ask(ask_id, minutes_ago, to, sender="claude-code-cli", extra=""):
    return row(ask_id, minutes_ago, sender,
               "BCB|v=1|id=%s|phase=ASK|class=ORDER|from=%s|to=%s%s|text=fixture ask"
               % (ask_id, sender, to, extra))


def reply(row_id, minutes_ago, sender, key, target, phase="ACK"):
    return row(row_id, minutes_ago, sender,
               "BCB|v=1|id=%s|phase=%s|from=%s|%s=%s|text=fixture reply"
               % (row_id, phase, sender, key, target))


# The four fixtures the order names.
ANSWERED = ask("FX-ASK-ANSWERED", 40, "gemini")
ANSWERED_REPLY = reply("FX-GEMINI-ACK-1", 30, "gemini", "answers", "FX-ASK-ANSWERED")
UNANSWERED_OLD = ask("FX-ASK-SILENT", 20, "gemini")
YOUNG = ask("FX-ASK-YOUNG", 10, "gemini")
MULTI = ask("FX-ASK-MULTI", 25, "gemini;grok;codex")
MULTI_REPLY = reply("FX-GROK-RESULT-1", 18, "grok", "answers", "FX-ASK-MULTI", phase="RESULT")

FIXTURE_BOARD = [ANSWERED, ANSWERED_REPLY, UNANSWERED_OLD, YOUNG, MULTI, MULTI_REPLY]


def find(rows, skip=()):
    return {c["ask_id"]: c for c in uw.find_unanswered(rows, NOW, LIVE, ALIAS, skip=skip)}


class Detection(unittest.TestCase):
    def test_the_four_fixtures(self):
        got = find(FIXTURE_BOARD)
        self.assertNotIn("FX-ASK-ANSWERED", got)
        self.assertNotIn("FX-ASK-YOUNG", got)
        self.assertEqual(got["FX-ASK-SILENT"]["silent"], ["gemini"])
        self.assertEqual(got["FX-ASK-SILENT"]["age_min"], 20)
        self.assertEqual(got["FX-ASK-MULTI"]["silent"], ["gemini", "chatgpt-codex-desktop"])
        self.assertEqual(set(got), {"FX-ASK-SILENT", "FX-ASK-MULTI"})

    def test_oldest_first(self):
        order = [c["ask_id"] for c in uw.find_unanswered(FIXTURE_BOARD, NOW, LIVE, ALIAS)]
        self.assertEqual(order, ["FX-ASK-MULTI", "FX-ASK-SILENT"])

    def test_exactly_at_the_sla_alerts_and_a_minute_before_does_not(self):
        self.assertIn("A", find([ask("A", 15, "gemini")]))
        self.assertNotIn("B", find([ask("B", 14, "gemini")]))

    def test_a_late_answer_before_the_pass_is_still_an_answer(self):
        rows = [ask("A", 60, "gemini"), reply("R", 1, "gemini", "answers", "A")]
        self.assertEqual(find(rows), {})

    def test_another_agents_reply_does_not_answer_for_the_one_asked(self):
        rows = [ask("A", 30, "gemini"), reply("R", 20, "grok", "answers", "A")]
        self.assertEqual(find(rows)["A"]["silent"], ["gemini"])

    def test_reply_to_and_closes_answer_too(self):
        for key in ("reply_to", "closes"):
            rows = [ask("A", 30, "gemini"), reply("R", 20, "gemini", key, "A")]
            self.assertEqual(find(rows), {}, key)

    def test_a_list_valued_answer_and_case_do_not_hide_it(self):
        rows = [ask("FX-A", 30, "gemini"), reply("R", 20, "gemini", "answers", "fx-other;fx-a")]
        self.assertEqual(find(rows), {})

    def test_answering_the_row_id_of_a_reask_counts(self):
        reask = row("ROW-2", 25, "claude-code-cli",
                    "BCB|v=1|id=SHARED|phase=ASK|from=claude-code-cli|to=gemini|text=again")
        first = row("ROW-1", 40, "claude-code-cli",
                    "BCB|v=1|id=SHARED|phase=ASK|from=claude-code-cli|to=gemini|text=first")
        self.assertEqual(find([first, reask])["SHARED"]["age_min"], 40, "clock starts at the first copy")
        self.assertEqual(find([first, reask, reply("R", 5, "gemini", "answers", "ROW-2")]), {})

    def test_an_alias_answers_for_its_instance(self):
        rows = [ask("A", 30, "codex"), reply("R", 20, "codex-desktop", "answers", "A")]
        self.assertEqual(find(rows), {})
        self.assertEqual(find([ask("A", 30, "codex")])["A"]["silent"], ["chatgpt-codex-desktop"])

    def test_a_sender_without_from_is_known_by_its_source_tag(self):
        bare = row("uuid-1", 20, "grok", "BCB|v=1|answers=A|reply_to=A|text=on it")
        self.assertEqual(find([ask("A", 30, "grok"), bare]), {})

    def test_not_addressed_to_a_live_agent_is_not_an_ask_here(self):
        rows = [ask("CC", 30, "nobody", extra="|cc=gemini"),          # cc= is a copy
                ask("FAM", 30, "claude"),                             # a family, not an instance
                ask("SELF", 30, "claude-code-cli"),                   # the asker itself
                ask("DORMANT", 30, "grok-bot")]                       # not live
        self.assertEqual(find(rows), {})

    def test_only_ask_phases_count(self):
        note = row("N", 30, "claude-code-cli",
                   "BCB|v=1|id=N|phase=NOTE|from=claude-code-cli|to=gemini|text=fyi")
        self.assertEqual(find([note]), {})
        dispatch = row("D", 30, "claude-code-cli",
                       "BCB|v=1|id=D|phase=DISPATCH|from=claude-code-cli|to=gemini|text=go")
        self.assertIn("D", find([dispatch]))

    def test_a_non_canonical_row_is_not_acted_on(self):
        rows = [row("X", 30, "claude-code-cli", "BCB|v=2|id=X|phase=ASK|to=gemini|text=x"),
                row("Y", 30, "claude-code-cli", "BCB|v=1|id=Y|id=Z|phase=ASK|to=gemini|text=x"),
                row("W", 30, "claude-code-cli", "phase=ASK|to=gemini|text=no envelope")]
        self.assertEqual(find(rows), {})

    def test_an_unparseable_timestamp_is_skipped_not_guessed(self):
        r = ask("A", 30, "gemini")
        r[1] = "Monday afternoon"
        self.assertEqual(find([r]), {})

    def test_the_asker_can_withdraw_it(self):
        field = [ask("A", 30, "gemini"), ask("B", 20, "gemini", extra="|supersedes=A")]
        self.assertNotIn("A", find(field))
        prose = [ask("A", 30, "gemini"),
                 row("B", 20, "claude-code-cli", "BCB|v=1|id=B|phase=ASK|from=claude-code-cli|to=gemini"
                     "|text=review the new head instead; it supersedes A, whose scope stands")]
        self.assertNotIn("A", find(prose))
        stranger = [ask("A", 30, "gemini"), reply("S", 20, "grok", "supersedes", "A")]
        self.assertIn("A", find(stranger), "only the asker can withdraw its ask")

    def test_never_repeated(self):
        on_board = row(uw.ALERT_PREFIX + "FX-ASK-SILENT", 2, "claude-code-cli",
                       "BCB|v=1|id=%sFX-ASK-SILENT|phase=ALERT|ask=FX-ASK-SILENT|text=x"
                       % uw.ALERT_PREFIX)
        self.assertNotIn("FX-ASK-SILENT", find(FIXTURE_BOARD + [on_board]))
        self.assertNotIn("FX-ASK-SILENT", find(FIXTURE_BOARD, skip={"fx-ask-silent"}))

    def test_an_alert_is_never_an_answer_from_its_poster(self):
        rows = [ask("A", 40, "claude-code-cli", sender="claude-mobile")]
        cand = find(rows)["A"]
        rid, payload = uw.build_payload(cand, "claude-code-cli", set(), NOW)
        alert = row(rid, 1, "claude-code-cli", payload)
        self.assertEqual(uw.find_unanswered(rows + [alert], NOW, LIVE, ALIAS, skip=()), [],
                         "skipped because it is alerted ...")
        self.assertNotIn("answers=", payload.split("|text=")[0])
        self.assertNotIn("reply_to=", payload)


class Payload(unittest.TestCase):
    def test_shape(self):
        cand = find(FIXTURE_BOARD)["FX-ASK-MULTI"]
        rid, payload = uw.build_payload(cand, "claude-code-cli", {"gemini"}, NOW)
        self.assertEqual(rid, "SLA-ALERT-UNACKED-15M-FX-ASK-MULTI")
        self.assertTrue(payload.startswith("BCB|v=1|id=%s|phase=ALERT|" % rid))
        for part in ("|thread_id=TH-BINDING|", "|ask=FX-ASK-MULTI|", "|to=gemini;chatgpt-codex-desktop|",
                     "|cc=claude-code-cli|", "|from=claude-code-cli|",
                     "|wake=gemini:agent_waker;chatgpt-codex-desktop:ring-only|",
                     "|posted_utc=2026-11-23T12:00:00Z|"):
            self.assertIn(part, payload)
        text = payload.split("|text=", 1)[1]
        self.assertNotIn("|", text)
        self.assertNotIn("\n", payload)
        self.assertLessEqual(len(payload), uw.MAX_ROW)

    def test_append_guards_accept_it(self):
        import append
        import signature
        cand = find(FIXTURE_BOARD)["FX-ASK-MULTI"]
        _, payload = uw.build_payload(cand, "claude-code-cli", {"gemini"}, NOW)
        self.assertEqual(signature.check("claude-code-cli", payload), [])
        self.assertEqual(append._conflicting_keys(payload), set())

    def test_a_hostile_id_cannot_break_the_envelope_or_the_limit(self):
        cand = {"ask_id": "A|to=everyone " + "x" * 3000, "asked": NOW, "asker": "grok|x",
                "class": "ORDER|v=9", "phase": "ASK", "to": ["gemini"], "silent": ["gemini"],
                "age_min": 99}
        rid, payload = uw.build_payload(cand, "claude-code-cli", set(), NOW)
        self.assertNotIn("|", rid)
        self.assertLessEqual(len(payload), uw.MAX_ROW)
        self.assertEqual(payload.count("|to="), 1)
        self.assertEqual(payload.count("|v="), 1)


class Roster(unittest.TestCase):
    def test_live_and_aliases_come_from_the_real_roster(self):
        live, alias = uw.load_roster()
        self.assertIn("gemini", live)
        self.assertIn("chatgpt-codex-desktop", live)
        self.assertEqual(alias.get("codex"), "chatgpt-codex-desktop")
        for not_live in ("claude", "whatsapp", "grok-bot", "foundry", "codex"):
            self.assertNotIn(not_live, live)

    def test_waker_agents_are_read_from_the_waker_source(self):
        agents = uw.waker_agents()
        self.assertIn("gemini", agents)
        self.assertIn("grok", agents)
        self.assertNotIn("chatgpt-codex-desktop", agents)


class Pass(unittest.TestCase):
    """main() end to end, with stand-ins for the board, append.py and agent_waker.py."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.old = os.environ.get("BLACKBOARD_STATE_DIR")
        os.environ["BLACKBOARD_STATE_DIR"] = self.dir.name
        self.posted, self.woken = [], []

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BLACKBOARD_STATE_DIR", None)
        else:
            os.environ["BLACKBOARD_STATE_DIR"] = self.old

    def run_pass(self, argv, rows, post_ok=True):
        def post(spec):
            self.posted.append(spec)
            return post_ok, "stand-in"

        def wake(agent):
            self.woken.append(agent)
            return "exit 0"
        out = io.StringIO()
        with redirect_stdout(out):
            code = uw.main(argv, read=lambda since: list(rows), post=post, wake=wake, now=NOW)
        return code, out.getvalue()

    def state(self):
        path = os.path.join(self.dir.name, ".unacked_watchdog_state.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_dry_run_writes_nothing(self):
        code, out = self.run_pass(["--dry-run"], FIXTURE_BOARD)
        self.assertEqual(code, 0)
        self.assertEqual((self.posted, self.woken, self.state()), ([], [], None))
        self.assertIn("ALERT FX-ASK-SILENT", out)
        self.assertIn("ALERT FX-ASK-MULTI", out)
        self.assertIn("would post", out)
        self.assertIn("would wake through agent_waker: gemini", out)

    def test_a_pass_posts_once_per_ask_and_wakes_each_agent_once(self):
        code, _ = self.run_pass([], FIXTURE_BOARD)
        self.assertEqual(code, 0)
        self.assertEqual([s["row_id"] for s in self.posted],
                         ["SLA-ALERT-UNACKED-15M-FX-ASK-MULTI", "SLA-ALERT-UNACKED-15M-FX-ASK-SILENT"])
        self.assertEqual(self.posted[0]["source_tag"], "claude-code-cli")
        self.assertEqual(self.woken, ["gemini"], "gemini is silent on two asks and woken once")
        self.assertEqual(set(self.state()["alerted"]), {"fx-ask-multi", "fx-ask-silent"})

        self.posted, self.woken = [], []
        self.run_pass([], FIXTURE_BOARD)
        self.assertEqual((self.posted, self.woken), ([], []), "the state file stops a repeat")

    def test_the_board_stops_a_repeat_when_the_state_file_is_gone(self):
        self.run_pass([], FIXTURE_BOARD)
        alerts = [row(s["row_id"], 1, "claude-code-cli", s["payload"]) for s in self.posted]
        os.remove(os.path.join(self.dir.name, ".unacked_watchdog_state.json"))
        self.posted, self.woken = [], []
        self.run_pass([], FIXTURE_BOARD + alerts)
        self.assertEqual(self.posted, [])

    def test_an_unconfirmed_post_is_not_remembered_and_wakes_nobody(self):
        code, out = self.run_pass([], [UNANSWERED_OLD], post_ok=False)
        self.assertEqual(code, 1)
        self.assertIn("POST UNCONFIRMED", out)
        self.assertEqual(self.woken, [])
        self.assertEqual(self.state(), None)

    def test_max_caps_one_pass_and_no_wake_skips_the_waker(self):
        self.run_pass(["--max", "1", "--no-wake"], FIXTURE_BOARD)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.woken, [])

    def test_the_real_wake_is_a_foreground_agent_waker_call(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))

            class Done:
                returncode = 0
            return Done()
        self.assertEqual(uw.wake_agent("gemini", run=fake_run), "exit 0")
        argv, kw = calls[0]
        self.assertEqual(argv[-3:], [uw.WAKER_PATH, "--agent", "gemini"][-3:])
        self.assertTrue(argv[-3].endswith("agent_waker.py"))
        self.assertIn("timeout", kw)
        self.assertNotIn("&", argv)


if __name__ == "__main__":
    unittest.main()

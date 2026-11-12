#!/usr/bin/env python3
"""Tests for scripts/compliance_runner.py. Offline: every row, PR list and process list is a fake fixture.

Run: python -m unittest tests.test_compliance_runner -v     (or: python tests/test_compliance_runner.py)
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "scripts", "compliance_runner.py")
_spec = importlib.util.spec_from_file_location("compliance_runner", SRC)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)

NOW = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)          # 10:00 pm Nov 11 in Toronto
GB = 1024 ** 3
ALL_PROCS = ["python run_clay.py", "python run_cody.py", "python run_jemma.py", "python clay_wait.py"]


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def cells(rid, minutes_ago, source, payload, target="ALL"):
    return [rid, ts(minutes_ago), source, target, "APPEND", payload, "TASK", "Blackboard", rid, "", "", ""]


def bcbrow(rid, minutes_ago, frm, extra="", text="fake text", thread="TH-FAKE", to="all", phase="RESULT"):
    p = "BCB|v=1|id=%s|phase=%s|class=TASK|from=%s|to=%s|thread_id=%s%s|text=%s" % (
        rid, phase, frm, to, thread, extra, text)
    return cells(rid, minutes_ago, frm, p)


def rows(*c):
    return cr.parse_rows(list(c))


def rules(findings):
    return sorted(f["rule"] for f in findings)


class Parse(unittest.TestCase):
    def test_bcb_and_space_rows(self):
        r = rows(bcbrow("FAKE-1", 5, "grok", "|answers=FAKE-0"))[0]
        self.assertEqual((r.id, r.sender, r.thread), ("FAKE-1", "grok", "TH-FAKE"))
        self.assertEqual(r.refs, {"FAKE-0"})
        p = "|id=FAKE-2| thread_id=TH-ESTIMATES task=draft two notes| by=cursor relayer=grok text='a=b c'"
        r = rows(cells("uuid-x", 5, "grok", p))[0]
        self.assertEqual(r.id, "FAKE-2")
        self.assertEqual(r.f["task"], "draft two notes")
        self.assertEqual(r.f["text"], "a=b c")
        self.assertEqual(r.who, {"grok", "cursor"})

    def test_canonical(self):
        self.assertEqual(cr.canonical("chatgpt-codex-desktop-abc"), "chatgpt-codex-desktop")
        self.assertEqual(cr.canonical("claude"), "claude-code-cli")
        self.assertEqual(cr.canonical("vm-claude-code-cli"), "claude-code-cli")


class PrNames(unittest.TestCase):
    def flagged(self, text, extra=""):
        return cr.detect_pr_names(rows(bcbrow("FAKE-PR-ROW", 5, "grok", extra, text=text)), NOW)

    def test_bare_numbers_flagged(self):
        for t in ("merged #428 today", "see PR 428", "PR428 is green", "PR #428 needs review"):
            self.assertEqual(rules(self.flagged(t)), ["R-PR"], t)

    def test_repo_named_not_flagged(self):
        for t in ("merged sfdc-24/conference #428", "merged conference #428", "Blackboard PR #497 ok",
                  "sfdc24-site #12 deployed", "no numbers here", "Case 00009999 open",
                  "conference #427 and conference #428 merged", "conference: #428", "Blackboard PRs #484",
                  "never write '#428' bare", "never write '#427/#428' bare"):
            self.assertEqual(self.flagged(t), [], t)

    def test_list_after_a_bare_number_still_flagged(self):
        self.assertEqual(rules(self.flagged("merged #427 and #428")), ["R-PR"])
        self.assertEqual(cr.bare_pr_refs("merged #427 and #428"), ["427", "428"])
        self.assertEqual(cr.bare_pr_refs("conference #427, later #430"), ["430"])

    def test_strict_each_number_needs_its_repo(self):
        for t in ("conference #427 and #428 merged", "Blackboard 2 open (#484,#485)", "conference #427/#428",
                  "sfdc-24/conference is at 3 open PRs (#420)", "pushed to grok/g21-x #428",
                  "cursor/c35-on-main (#428"):
            self.assertEqual(rules(self.flagged(t)), ["R-PR"], t)
        self.assertEqual(cr.bare_pr_refs("conference #427/#428"), ["428"])

    def test_owner_words_not_judged(self):
        p = "BCB|v=1|id=FAKE-OWN|phase=RESULT|class=LOG|from=gemini|to=owner|thread_id=TH-GEMINI-OWNER-20261111|fake #428"
        self.assertEqual(cr.detect_pr_names(rows(cells("FAKE-OWN", 5, "gemini", p)), NOW), [])
        self.assertEqual(self.flagged("fake #428", "|msg_type=PROMPT"), [])

    def test_ids_not_flagged(self):
        self.assertEqual(self.flagged("see FAKE-PR428-ROW", "|answers=FAKE-CLI-PR428-20261112T0100Z"), [])

    def test_pr_field(self):
        self.assertEqual(rules(self.flagged("x", "|pr=428")), ["R-PR"])
        self.assertEqual(self.flagged("x", "|repo=sfdc-24/conference|pr=428"), [])

    def test_non_fleet_ignored(self):
        self.assertEqual(cr.detect_pr_names(rows(bcbrow("FAKE-W", 5, "whatsapp", text="#428")), NOW), [])


class OwnerLog(unittest.TestCase):
    def prompt(self, rid, ago, thread="TH-GROK-OWNER-20261111"):
        return bcbrow(rid, ago, "grok", "|msg_type=PROMPT", thread=thread)

    def test_prompt_without_reply(self):
        f = cr.detect_owner_log(rows(self.prompt("FAKE-P1", 30)), NOW)
        self.assertEqual([x["key"] for x in f], ["R9:FAKE-P1"])

    def test_prompt_with_reply(self):
        rr = rows(self.prompt("FAKE-P1", 30),
                  bcbrow("FAKE-R1", 28, "grok", "|msg_type=PROMPT_RESPONSE|reply_to=FAKE-P1",
                         thread="TH-GROK-OWNER-20261111"))
        self.assertEqual(cr.detect_owner_log(rr, NOW), [])

    def test_young_prompt_not_due(self):
        self.assertEqual(cr.detect_owner_log(rows(self.prompt("FAKE-P1", 5)), NOW), [])

    def test_thread_date_and_shape(self):
        f = cr.detect_owner_log(rows(self.prompt("FAKE-P1", 5, "TH-GROK-OWNER-20261110")), NOW)
        self.assertEqual([x["key"] for x in f], ["R9-THREAD:grok:20261111"])
        f = cr.detect_owner_log(rows(self.prompt("FAKE-P2", 5, "TH-OWNER-GEMINI")), NOW)
        self.assertEqual(len(f), 1)


class Estimates(unittest.TestCase):
    def est(self, rid, ago, task, done_in):
        done = (NOW - timedelta(minutes=ago) + timedelta(minutes=done_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return bcbrow(rid, ago, "chatgpt-codex-desktop", "|msg_type=ESTIMATE|task=%s|est_done=%s" % (task, done),
                      thread="TH-ESTIMATES")

    def test_overdue_flagged(self):
        f = cr.detect_estimates(rows(self.est("FAKE-E1", 60, "t1", 30)), NOW)
        self.assertEqual([x["key"] for x in f], ["R10a:FAKE-E1"])

    def test_done_clears(self):
        rr = rows(self.est("FAKE-E1", 60, "t1", 30),
                  bcbrow("FAKE-ED1", 40, "chatgpt-codex-desktop", "|msg_type=ESTIMATE_DONE|task=t1",
                         thread="TH-ESTIMATES"))
        self.assertEqual(cr.detect_estimates(rr, NOW), [])

    def test_not_due_and_reestimate(self):
        self.assertEqual(cr.detect_estimates(rows(self.est("FAKE-E1", 20, "t1", 10)), NOW), [])
        rr = rows(self.est("FAKE-E1", 60, "t1", 30), self.est("FAKE-E2", 20, "t1", 60))
        self.assertEqual(cr.detect_estimates(rr, NOW), [])

    def test_started_plus_minutes(self):
        p = "|id=FAKE-ESTIMATE-X| thread=TH-ESTIMATES task=t9 started=%s est_minutes=10" % ts(60)[:19]
        f = cr.detect_estimates(rows(cells("uuid-e", 60, "grok", p)), NOW)
        self.assertEqual(rules(f), ["R10a"])


class Acks(unittest.TestCase):
    def ask(self, rid, ago, to, cls="TASK"):
        r = bcbrow(rid, ago, "claude-code-cli", "", to=to, phase="ASK")
        r[5] = r[5].replace("class=TASK", "class=" + cls)
        return r

    def test_unanswered(self):
        f = cr.detect_acks(rows(self.ask("FAKE-A1", 30, "grok;gemini")), NOW)
        self.assertEqual(sorted(x["agent"] for x in f), ["gemini", "grok"])

    def test_answered_and_young(self):
        rr = rows(self.ask("FAKE-A1", 30, "grok"), bcbrow("FAKE-K1", 25, "grok", "|answers=FAKE-A1", phase="ACK"))
        self.assertEqual(cr.detect_acks(rr, NOW), [])
        self.assertEqual(cr.detect_acks(rows(self.ask("FAKE-A2", 5, "grok")), NOW), [])

    def test_cursor_answered_through_grok(self):
        rr = rows(self.ask("FAKE-A1", 30, "cursor"),
                  cells("u1", 20, "grok", "|id=FAKE-K2| thread_id=TH-FAKE answers=FAKE-A1 by=cursor relayer=grok"))
        self.assertEqual(cr.detect_acks(rr, NOW), [])

    def test_compliance_asks_exempt(self):
        self.assertEqual(cr.detect_acks(rows(self.ask("FAKE-A3", 30, "grok", "COMPLIANCE")), NOW), [])


class Threads(unittest.TestCase):
    def test_missing(self):
        rr = rows(cells("u1", 10, "grok", "|id=FAKE-T1| thread=TH-FAKE text=x"),
                  cells("u2", 9, "grok", "|id=FAKE-T2| text=x"))
        f = cr.detect_threads(rr, NOW)
        self.assertEqual([x["key"] for x in f], ["R-THREAD:grok:20261111"])
        self.assertEqual(f[0]["count"], 2)

    def test_present_and_non_fleet(self):
        rr = rows(bcbrow("FAKE-T3", 5, "grok"), cells("u3", 5, "bus-reconciler", "|id=FAKE-T4| text=x"))
        self.assertEqual(cr.detect_threads(rr, NOW), [])


class PrCap(unittest.TestCase):
    def prs(self, n, prefix="grok/"):
        return [{"number": i, "headRefName": prefix + str(i)} for i in range(n)]

    def test_cap_six_until_friday(self):
        self.assertEqual(rules(cr.detect_prcap({"sfdc-24/conference": self.prs(7)}, NOW)), ["R-PRCAP"])
        self.assertEqual(cr.detect_prcap({"sfdc-24/conference": self.prs(6)}, NOW), [])
        self.assertEqual(cr.detect_prcap({"sfdc-24/conference": None}, NOW), [])

    def test_cap_three_after(self):
        later = datetime(2026, 11, 14, 5, 0, tzinfo=timezone.utc)
        f = cr.detect_prcap({"sfdc-24/Blackboard": self.prs(4, "cursor/")}, later)
        self.assertEqual([(x["rule"], x["agent"]) for x in f], [("R-PRCAP", "cursor")])
        self.assertEqual(cr.detect_prcap({"sfdc-24/Blackboard": self.prs(3)}, later), [])


class Health(unittest.TestCase):
    def test_low_ram_and_missing(self):
        f = cr.detect_health(2 * GB, ["python run_clay.py"], NOW)
        self.assertEqual(sorted(x["key"] for x in f),
                         ["R-HEALTH:proc:clay_wait", "R-HEALTH:proc:run_cody", "R-HEALTH:proc:run_jemma",
                          "R-HEALTH:ram"])

    def test_healthy_and_unknown(self):
        self.assertEqual(cr.detect_health(8 * GB, ALL_PROCS, NOW), [])
        self.assertEqual(cr.detect_health(None, None, NOW), [])


class Cycle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["COMPLIANCE_STATE_DIR"] = self.dir
        self.posted, self.notes = [], []
        self.board = []

    def tearDown(self):
        os.environ.pop("COMPLIANCE_STATE_DIR", None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def deps(self, board=None):
        return {"read_rows": lambda: board if board is not None else self.board,
                "pr_lists": lambda: {}, "free_ram": lambda: 8 * GB, "cmdlines": lambda: ALL_PROCS,
                "post": lambda s: self.posted.append(s) or "ok",
                "notify": lambda t: self.notes.append(t) or True,
                "state_path": os.path.join(self.dir, "state.json")}

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
            return fh.read()

    def state(self):
        return json.loads(self.read("state.json"))

    def asks(self, n):
        return [bcbrow("FAKE-ASK-%02d" % i, 40, "claude-code-cli", "", to="grok", phase="ASK") for i in range(n)]

    def rings(self):
        return [s for s in self.posted if "RING-R" in s["row_id"]]

    def test_ring_format(self):
        cr.run_cycle(self.deps(self.asks(1)), NOW, out=lambda *_: None)
        p = self.rings()[0]["payload"]
        self.assertNotIn("|", p.split("|text=", 1)[1])
        self.assertLessEqual(len(p), cr.MAX_ROW_CHARS)
        for part in ("phase=ASK", "class=COMPLIANCE", "to=grok", "thread_id=TH-COMPLIANCE", "gap_row=FAKE-ASK-00",
                     "from=claude-code-cli"):
            self.assertIn(part, p)

    def test_dedupe(self):
        cr.run_cycle(self.deps(self.asks(2)), NOW, out=lambda *_: None)
        self.assertEqual(len(self.rings()), 2)
        cr.run_cycle(self.deps(self.asks(2)), NOW + timedelta(minutes=15), out=lambda *_: None)
        self.assertEqual(len(self.rings()), 2)

    def test_board_ring_counts_as_rung(self):
        prior = cr.ring_spec(cr.detect_acks(rows(*self.asks(1)), NOW)[0], NOW - timedelta(minutes=5), 1)
        board = self.asks(1) + [cells(prior["row_id"], 5, "claude-code-cli", prior["payload"])]
        cr.run_cycle(self.deps(board), NOW, out=lambda *_: None)
        self.assertEqual(self.rings(), [])

    def test_escalation_after_two_cycles_once(self):
        board = self.asks(1)
        for i in range(4):
            cr.run_cycle(self.deps(board), NOW + timedelta(minutes=15 * i), out=lambda *_: None)
            self.assertEqual(len(self.notes), 0 if i < 2 else 1, "cycle %d" % i)
        self.assertLessEqual(len(self.notes[0].splitlines()), 3)
        self.assertNotIn("fake text", self.notes[0])
        st = self.state()
        self.assertTrue(all(g["escalated"] for g in st["gaps"].values()))

    def test_no_whatsapp(self):
        board = self.asks(1)
        for i in range(3):
            cr.run_cycle(self.deps(board), NOW + timedelta(minutes=15 * i), whatsapp=False, out=lambda *_: None)
        self.assertEqual(self.notes, [])

    def test_batching_cap(self):
        cr.run_cycle(self.deps(self.asks(12)), NOW, out=lambda *_: None)
        self.assertEqual(len(self.rings()), 8)
        summ = [s for s in self.posted if "RING-SUMMARY" in s["row_id"]]
        self.assertEqual(len(summ), 1)
        self.assertIn("4 more", summ[0]["payload"])
        st = self.state()
        self.assertEqual(len(st["gaps"]), 12)

    def test_cleared_dropped(self):
        board = self.asks(1)
        cr.run_cycle(self.deps(board), NOW, out=lambda *_: None)
        board = board + [bcbrow("FAKE-ACK", 30, "grok", "|answers=FAKE-ASK-00", phase="ACK")]
        cr.run_cycle(self.deps(board), NOW + timedelta(minutes=15), out=lambda *_: None)
        st = self.state()
        self.assertEqual(st["gaps"], {})

    def test_pr_gap_cleared_by_ack_of_ring(self):
        board = [bcbrow("FAKE-PRX", 10, "grok", text="merged #428")]
        cr.run_cycle(self.deps(board), NOW, out=lambda *_: None)
        rid = self.rings()[0]["row_id"]
        board = board + [bcbrow("FAKE-ACK2", 0, "grok", "|answers=%s" % rid, phase="ACK")]
        found = cr.run_cycle(self.deps(board), NOW + timedelta(minutes=15), out=lambda *_: None)
        self.assertEqual([f for f in found if f["rule"] == "R-PR"], [])

    def test_heartbeat_once_per_six_hours(self):
        cr.run_cycle(self.deps([]), NOW, out=lambda *_: None)
        cr.run_cycle(self.deps([]), NOW + timedelta(hours=1), out=lambda *_: None)
        hb = [s for s in self.posted if "HEARTBEAT" in s["row_id"]]
        self.assertEqual(len(hb), 1)
        self.assertIn("phase=RESULT", hb[0]["payload"])
        self.assertIn("to=gemini", hb[0]["payload"])
        cr.run_cycle(self.deps([]), NOW + timedelta(hours=6), out=lambda *_: None)
        self.assertEqual(len([s for s in self.posted if "HEARTBEAT" in s["row_id"]]), 2)

    def test_dry_run_posts_nothing(self):
        rc = cr.main(["--dry-run"], deps=self.deps(self.asks(3)), now=NOW)
        self.assertEqual(rc, 1)
        self.assertEqual((self.posted, self.notes), ([], []))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state.json")))
        self.assertEqual(cr.main(["--dry-run"], deps=self.deps([]), now=NOW), 0)

    def test_main_never_raises(self):
        d = self.deps()

        def boom():
            raise OSError("fake outage")
        d["read_rows"] = boom
        self.assertEqual(cr.main([], deps=d, now=NOW), 0)
        self.assertIn("fake outage", self.read("runner.log"))

    def test_refused_post_rings_again_next_cycle(self):
        d = self.deps(self.asks(1))
        d["post"] = lambda s: self.posted.append(s) or "refused"
        cr.run_cycle(d, NOW, out=lambda *_: None)
        cr.run_cycle(d, NOW + timedelta(minutes=15), out=lambda *_: None)
        self.assertEqual(len(self.rings()), 2)


class Hardening(unittest.TestCase):
    """The reviewed failure modes, each on fake rows."""

    def test_hybrid_pipe_values_and_prose_fields(self):
        p = ("|id=FAKE-H1| thread_id=TH-ESTIMATES| msg_type=ESTIMATE_DONE| task=G1| what=refuse to=gemini rows "
             "that expect an answer unless phase=ASK, with a flag| by=grok|")
        r = rows(cells("u-h1", 5, "grok", p))[0]
        self.assertEqual(r.msg_type, "ESTIMATE_DONE")
        self.assertEqual(r.phase, "")
        self.assertNotIn("to", r.f)
        r = rows(cells("u-h2", 5, "grok", "|id=FAKE-H2| msg_type=ESTIMATE_DONE| TASK ID: G15"))[0]
        self.assertEqual(r.msg_type, "ESTIMATE_DONE")

    def test_sender_from_commit_falls_back_to_source(self):
        r = rows(cells("u-s", 5, "grok", "|id=FAKE-S1| thread_id=TH-FAKE from=1234abcd -> new branch text=x"))[0]
        self.assertEqual(r.sender, "grok")
        self.assertIn("grok", r.who)

    def test_hybrid_ask_rows_are_asks(self):
        a1 = cells("u-a1", 30, "grok", "|id=GROK-ASK-FAKE-ONE-20261112T0200Z| thread=TH-FAKE text=fake",
                   target="claude-code-cli")
        a2 = cells("u-a2", 30, "grok", "|id=FAKE-MT-1| thread_id=TH-FAKE msg_type=ASK Owner (fake) text=fake",
                   target="gemini")
        f = cr.detect_acks(rows(a1, a2), NOW)
        self.assertEqual(sorted((x["row_id"], x["agent"]) for x in f),
                         [("FAKE-MT-1", "gemini"), ("GROK-ASK-FAKE-ONE-20261112T0200Z", "claude-code-cli")])
        notask = bcbrow("GROK-RESULT-FAKE-ASK-X", 30, "grok", to="gemini", phase="RESULT")
        self.assertEqual(cr.detect_acks(rows(notask), NOW), [])

    def test_late_ack_is_a_late_finding(self):
        a = bcbrow("FAKE-AL", 60, "claude-code-cli", to="chatgpt-codex-desktop", phase="ASK")
        k = bcbrow("FAKE-KL", 35, "chatgpt-codex-desktop", "|answers=FAKE-AL", phase="ACK")
        f = cr.detect_acks(rows(a, k), NOW)
        self.assertEqual(len(f), 1)
        self.assertTrue(f[0]["historic"])
        self.assertIn("25 min", f[0]["missing"])

    def test_machine_asks_skipped_and_grok_answers_for_cursor(self):
        m = bcbrow("FAKE-WAKE", 30, "bus-reconciler", to="grok", phase="ASK")
        self.assertEqual(cr.detect_acks(rows(m), NOW), [])
        a = bcbrow("FAKE-AC", 30, "gemini", to="grok,cursor", phase="ASK")
        k = bcbrow("FAKE-KC", 25, "grok", "|answers=FAKE-AC", phase="ACK")
        self.assertEqual(cr.detect_acks(rows(a, k), NOW), [])

    def test_own_ring_never_answers_the_ask(self):
        ask = bcbrow("FAKE-AS", 40, "gemini", to="claude-code-cli;grok", phase="ASK")
        first = cr.detect_acks(rows(ask), NOW)[0]
        ring = cr.ring_spec(first, NOW - timedelta(minutes=20), 1)
        self.assertNotIn("|answers=", ring["payload"])
        forged = ring["payload"].replace("gap_row=", "answers=")
        rr = rows(ask, cells(ring["row_id"], 20, "claude-code-cli", forged))
        self.assertEqual(len(cr.detect_acks(rr, NOW)), 2)

    def test_r9_times_from_said_at(self):
        said = ts(70)[:19] + "Z"
        p = bcbrow("FAKE-P9", 30, "grok", "|msg_type=PROMPT|said_at=%s" % said, thread="TH-GROK-OWNER-20261111")
        r = bcbrow("FAKE-R9", 29, "grok", "|msg_type=PROMPT_RESPONSE|reply_to=FAKE-P9|said_at=%s" % said,
                   thread="TH-GROK-OWNER-20261111")
        f = cr.detect_owner_log(rows(p, r), NOW)
        self.assertEqual([x["key"] for x in f], ["R9-LAG:grok:20261111"])
        self.assertTrue(f[0]["historic"])
        p2 = bcbrow("FAKE-P10", 60, "grok", "|msg_type=PROMPT", thread="TH-GROK-OWNER-20261111")
        r2 = bcbrow("FAKE-R10", 30, "grok", "|msg_type=PROMPT_RESPONSE|reply_to=FAKE-P10",
                    thread="TH-GROK-OWNER-20261111")
        f = cr.detect_owner_log(rows(p2, r2), NOW)
        self.assertEqual([(x["key"], x.get("historic")) for x in f], [("R9:FAKE-P10", True)])

    def test_r9_thread_label_and_untyped_owner_log(self):
        p = bcbrow("FAKE-PL", 5, "grok", "|msg_type=PROMPT", thread="TH-CLI-OWNER-20261111")
        self.assertEqual([x["key"] for x in cr.detect_owner_log(rows(p), NOW)], ["R9-THREAD:grok:20261111"])
        u = cells("FAKE-U", 30, "gemini", "BCB|v=1|id=FAKE-U|phase=RESULT|class=LOG|from=gemini|to=owner|"
                  "thread_id=TH-GEMINI-OWNER-20261111|fake words")
        keys = sorted(x["key"] for x in cr.detect_owner_log(rows(u), NOW))
        self.assertEqual(keys, ["R9-TYPE:gemini:20261111", "R9:FAKE-U"])

    def test_estimate_task_key_supersede_and_id_match(self):
        def est(rid, ago, task, done_in):
            done = (NOW - timedelta(minutes=ago) + timedelta(minutes=done_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return bcbrow(rid, ago, "grok", "|msg_type=ESTIMATE|task=%s|est_done=%s" % (task, done),
                          thread="TH-ESTIMATES")
        done = bcbrow("FAKE-ESTIMATE-DONE-C9", 50, "grok", "|task=C9", thread="TH-ESTIMATES")
        self.assertEqual(cr.detect_estimates(rows(est("FAKE-E9", 60, "c9-fake-name", 20), done), NOW), [])
        sup = bcbrow("FAKE-SUP", 55, "grok", "|msg_type=ESTIMATE_SUPERSEDED|supersedes=FAKE-E8",
                     thread="TH-ESTIMATES")
        self.assertEqual(cr.detect_estimates(rows(est("FAKE-E8", 60, "free text task", 5), sup), NOW), [])
        self.assertEqual(rules(cr.detect_estimates(rows(sup), NOW)), [])
        byid = cells("u-d7", 40, "grok", "|id=FAKE-ESTDONE-7| thread_id=TH-ESTIMATES task=other estimate=FAKE-E7")
        self.assertEqual(cr.detect_estimates(rows(est("FAKE-E7", 60, "draft notes", 30), byid), NOW), [])
        late = bcbrow("FAKE-ESTIMATE-DONE-C6", 5, "grok", "|task=C6", thread="TH-ESTIMATES")
        f = cr.detect_estimates(rows(est("FAKE-E6", 60, "C6", 10), late), NOW)
        self.assertEqual([(x["key"], x.get("historic")) for x in f], [("R10a:FAKE-E6", True)])

    def test_pr_gap_needs_ring_ack_or_correction(self):
        bad = bcbrow("FAKE-PRB", 10, "grok", text="merged #428")
        f = cr.detect_pr_names(rows(bad), NOW)[0]
        reply = rows(bad, bcbrow("FAKE-RE", 5, "grok", "|reply_to=FAKE-PRB", text="more"))
        self.assertFalse(cr.ack_cleared(f, reply, []))
        corr = rows(bad, bcbrow("FAKE-CO", 5, "grok", "|msg_type=CORRECTION|answers=FAKE-PRB",
                                text="conference #428"))
        self.assertTrue(cr.ack_cleared(f, corr, []))


class CycleHardening(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["COMPLIANCE_STATE_DIR"] = self.dir
        self.posted, self.notes = [], []

    def tearDown(self):
        os.environ.pop("COMPLIANCE_STATE_DIR", None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def deps(self, board, **over):
        d = {"read_rows": lambda: board, "pr_lists": lambda: {}, "free_ram": lambda: 8 * GB,
             "cmdlines": lambda: ALL_PROCS, "post": lambda s: self.posted.append(s) or "ok",
             "notify": lambda t: self.notes.append(t) or True, "state_path": os.path.join(self.dir, "state.json")}
        d.update(over)
        return d

    def state(self):
        with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def asks(self, n, to, prefix="FAKE-ASK"):
        return [bcbrow("%s-%s-%02d" % (prefix, to.upper()[:4], i), 40, "gemini", to=to, phase="ASK")
                for i in range(n)]

    def test_other_agents_and_severe_rules_ring_first(self):
        board = (self.asks(6, "claude-code-cli") + self.asks(2, "grok")
                 + [bcbrow("FAKE-PR-%d" % i, 10, "chatgpt-codex-desktop", text="merged #4%d2" % i)
                    for i in range(3)])
        cr.run_cycle(self.deps(board), NOW, out=lambda *_: None)
        rings = [s for s in self.posted if "RING-R" in s["row_id"]]
        tos = [re.search(r"\|to=([^|]+)", s["payload"]).group(1) for s in rings]
        self.assertEqual(tos[:5], ["grok", "grok", "chatgpt-codex-desktop", "chatgpt-codex-desktop",
                                   "chatgpt-codex-desktop"])
        self.assertEqual(tos[5:], ["claude-code-cli"] * 3)
        summ = [s for s in self.posted if "RING-SUMMARY" in s["row_id"]]
        self.assertEqual(len(summ), 1)
        self.assertIn("|to=claude-code-cli|", summ[0]["payload"])
        self.assertIn("|gaps=", summ[0]["payload"])

    def test_summary_records_only_what_fits(self):
        long = "FAKE-" + "X" * 60
        board = [bcbrow("%s-%02d" % (long, i), 40, "gemini", to="grok", phase="ASK") for i in range(40)]
        cr.run_cycle(self.deps(board), NOW, out=lambda *_: None)
        summ = [s for s in self.posted if "RING-SUMMARY" in s["row_id"]]
        self.assertEqual(len(summ), 1)
        self.assertLessEqual(len(summ[0]["payload"]), cr.MAX_ROW_CHARS)
        carried = re.search(r"\|gaps=([^|]+)", summ[0]["payload"]).group(1).split(";")
        st = self.state()
        self.assertEqual(len(st["gaps"]), 8 + len(carried))
        self.assertLess(len(st["gaps"]), 40)
        for k in carried:
            self.assertIn(k, st["gaps"])
        board2 = board + [cells(s["row_id"], 0, "claude-code-cli", s["payload"]) for s in self.posted]
        on_board = cr.board_rings(cr.parse_rows(board2))
        for k in carried:
            self.assertIn(k, on_board)

    def test_failed_source_clears_nothing_and_no_second_escalation(self):
        board = [bcbrow("FAKE-KEEP", 10, "grok", text="x")]
        free = {"v": 2 * GB}

        def ram():
            if free["v"] is None:
                raise OSError("fake probe failure")
            return free["v"]
        d = self.deps(board, free_ram=ram)
        for i in range(3):
            cr.run_cycle(d, NOW + timedelta(minutes=15 * i), out=lambda *_: None)
        self.assertEqual(len(self.notes), 1)
        free["v"] = None
        cr.run_cycle(d, NOW + timedelta(minutes=45), out=lambda *_: None)
        self.assertIn("R-HEALTH:ram", self.state()["gaps"])
        free["v"] = 8 * GB
        cr.run_cycle(d, NOW + timedelta(minutes=60), out=lambda *_: None)
        self.assertNotIn("R-HEALTH:ram", self.state()["gaps"])
        free["v"] = 2 * GB
        for i in range(5, 9):
            cr.run_cycle(d, NOW + timedelta(minutes=15 * i), out=lambda *_: None)
        self.assertEqual(len(self.notes), 1)

    def test_empty_board_read_clears_nothing(self):
        cr.run_cycle(self.deps(self.asks(1, "grok")), NOW, out=lambda *_: None)
        cr.run_cycle(self.deps([]), NOW + timedelta(minutes=15), out=lambda *_: None)
        self.assertEqual(len(self.state()["gaps"]), 1)

    def test_notify_timeout_counts_as_escalated(self):
        def boom(text):
            self.notes.append(text)
            raise subprocess.TimeoutExpired("wa", 120)
        d = self.deps(self.asks(1, "grok"), notify=boom)
        for i in range(5):
            cr.run_cycle(d, NOW + timedelta(minutes=15 * i), out=lambda *_: None)
        self.assertEqual(len(self.notes), 1)
        g = list(self.state()["gaps"].values())[0]
        self.assertTrue(g["escalated"] and g["escalation_uncertain"])

    def test_failed_notify_retries_after_an_hour_not_every_cycle(self):
        d = self.deps(self.asks(1, "grok"), notify=lambda t: self.notes.append(t) or "failed")
        for i in range(7):
            cr.run_cycle(d, NOW + timedelta(minutes=15 * i), out=lambda *_: None)
        self.assertEqual(len(self.notes), 2)          # cycle 2, then cycle 6 (60 min later)

    def test_late_findings_ring_but_never_escalate(self):
        board = [bcbrow("FAKE-AL2", 60, "claude-code-cli", to="grok", phase="ASK"),
                 bcbrow("FAKE-KL2", 35, "grok", "|answers=FAKE-AL2", phase="ACK")]
        for i in range(4):
            cr.run_cycle(self.deps(board), NOW + timedelta(minutes=15 * i), out=lambda *_: None)
        self.assertEqual(len([s for s in self.posted if "RING-R-ACK" in s["row_id"]]), 1)
        self.assertEqual(self.notes, [])

    def test_state_saved_after_each_post(self):
        n = {"c": 0}

        def post(s):
            n["c"] += 1
            if n["c"] == 2:
                raise KeyboardInterrupt("fake kill mid-cycle")
            self.posted.append(s)
            return "ok"
        try:
            cr.run_cycle(self.deps(self.asks(3, "grok"), post=post), NOW, out=lambda *_: None)
        except KeyboardInterrupt:
            pass
        self.assertEqual(len(self.state()["gaps"]), 1)

    def test_budget_stops_posting(self):
        t = {"v": 0.0}

        def clock():
            t["v"] += 400.0
            return t["v"]
        cr.run_cycle(self.deps(self.asks(4, "grok"), clock=clock), NOW, out=lambda *_: None)
        self.assertLess(len(self.posted), 4)

    def test_lock(self):
        path = os.path.join(self.dir, "lock")
        cr.take_lock(path)
        with self.assertRaises(cr.Busy):
            cr.take_lock(path)
        cr.take_lock(path, now_s=os.path.getmtime(path) + cr.LOCK_STALE_S + 1)
        rc = cr.main([], deps=self.deps(self.asks(1, "grok")), now=NOW)
        self.assertEqual((rc, self.posted), (0, []))
        cr.drop_lock(path)
        self.assertFalse(os.path.exists(path))

    def test_ring_ids_have_seconds(self):
        cr.run_cycle(self.deps(self.asks(1, "grok")), NOW, out=lambda *_: None)
        self.assertRegex(self.posted[0]["row_id"], r"^CLAUDE-CLI-COMPLIANCE-RING-R-ACK-\d{8}T\d{6}Z-01$")

    def test_escalation_text_dedupes_ids(self):
        t = cr.escalation_text([{"agent": "grok", "rule": "R-ACK", "ring_id": "FAKE-S", "key": "a"}] * 3)
        self.assertEqual(t.splitlines()[2], "Rings on TH-COMPLIANCE: FAKE-S.")


if __name__ == "__main__":
    unittest.main(verbosity=2)

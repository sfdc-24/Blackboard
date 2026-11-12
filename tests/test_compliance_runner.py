#!/usr/bin/env python3
"""Tests for scripts/compliance_runner.py. Offline: every row, PR list and process list is a fake fixture.

Run: python -m unittest tests.test_compliance_runner -v     (or: python tests/test_compliance_runner.py)
"""
import importlib.util
import json
import os
import shutil
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
                  "conference #427 and #428 merged", "Blackboard 2 open (#484,#485)", "conference #427/#428",
                  "never write '#428' bare"):
            self.assertEqual(self.flagged(t), [], t)

    def test_list_after_a_bare_number_still_flagged(self):
        self.assertEqual(rules(self.flagged("merged #427 and #428")), ["R-PR"])
        self.assertEqual(cr.bare_pr_refs("merged #427 and #428"), ["427", "428"])
        self.assertEqual(cr.bare_pr_refs("conference #427, later #430"), ["430"])

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
        for part in ("phase=ASK", "class=COMPLIANCE", "to=grok", "thread_id=TH-COMPLIANCE", "answers=FAKE-ASK-00",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

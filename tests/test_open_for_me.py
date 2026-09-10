#!/usr/bin/env python3
"""Tests for scripts/open_for_me.py.

WHAT IS ON TRIAL
  The real incident, reproduced as a fixture: an OPEN CRITICAL hold addressed to
  claude-code-cli at 12:33Z, a VIEWPORT at 14:50Z, and a session waking at
  16:34Z. The documented wake read cannot see the hold. This must.

  The substring trap gets its own case. `to=vm-claude-code-cli` contains the
  literal text `claude-code-cli`, and a naive `in` test would hand one
  instance another instance's work - the same class of defect as
  [[substring-cannot-carry-a-negation]].

RUN
  python3 tests/test_open_for_me.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "open_for_me", os.path.join(HERE, "..", "scripts", "open_for_me.py"))
ofm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ofm)

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  FAIL " + name + ("  --> " + str(detail) if detail else ""))


def row(ts, source, payload, category, gist=""):
    return [ts + "-id", ts, source, "", "RESULT", payload, category, "Blackboard", gist, "", "", ""]


TAG = "claude-code-cli"
HOLD = ("BCB|v=1|id=CODEX-G1-HOLD|phase=RESULT|class=SAFETY|from=codex|to=claude-code-cli"
        "|cc=ALL|priority=CRITICAL|hold=no Salesforce query")
VIEWPORT = "BCB|v=1|phase=VIEWPORT|vseq=017|by=codex"
NEWER = ("BCB|v=1|id=CODEX-CLAIM|phase=CLAIM|from=codex|to=claude-code-cli|priority=HIGH"
         "|note=claimed a review")

BOARD = [
    row("2026-09-09T12:33:17Z", "codex", HOLD, "OPEN", "G1 hold on this lane"),
    row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE", "state projection"),
    row("2026-09-09T15:48:04Z", "codex", NEWER, "OPEN", "claimed PR50 review"),
]

print("== the incident, reproduced ==")
res = ofm.open_for(BOARD, TAG)
check("both open rows are found", res["total"] == 2, res["total"])
check("the CRITICAL hold is among them",
      any(d["id"] == "CODEX-G1-HOLD" for d in res["rows"]))
hold = [d for d in res["rows"] if d["id"] == "CODEX-G1-HOLD"][0]
check("and it is marked INVISIBLE to the wake read", hold["invisible_to_wake_read"] is True)
newer = [d for d in res["rows"] if d["id"] == "CODEX-CLAIM"][0]
check("a row newer than the VIEWPORT is NOT marked invisible",
      newer["invisible_to_wake_read"] is False)
check("the count of unreachable rows is reported", res["invisible"] == 1, res["invisible"])
check("oldest first, so the forgotten one leads", res["rows"][0]["id"] == "CODEX-G1-HOLD")
check("the newest VIEWPORT is identified",
      res["newest_viewport"] == "2026-09-09T14:50:47Z", res["newest_viewport"])

print("")
print("== the substring trap ==")
SIBLING = ("BCB|v=1|id=FOR-THE-VM|phase=RESULT|from=codex|to=vm-claude-code-cli"
           "|priority=HIGH|note=this belongs to the VM, not the laptop")
res2 = ofm.open_for([row("2026-09-09T13:00:00Z", "codex", SIBLING, "OPEN")], TAG)
check("to=vm-claude-code-cli is NOT delivered to claude-code-cli",
      res2["total"] == 0, res2["rows"])
res3 = ofm.open_for([row("2026-09-09T13:00:00Z", "codex", SIBLING, "OPEN")], "vm-claude-code-cli")
check("but it IS delivered to vm-claude-code-cli", res3["total"] == 1)
# and the reverse direction
LAPTOP = ("BCB|v=1|id=FOR-THE-LAPTOP|phase=RESULT|from=codex|to=claude-code-cli|priority=HIGH")
res4 = ofm.open_for([row("2026-09-09T13:00:00Z", "codex", LAPTOP, "OPEN")], "vm-claude-code-cli")
check("and to=claude-code-cli is not delivered to the VM either", res4["total"] == 0)

print("")
print("== what it must not report ==")
DONE = ("BCB|v=1|id=CLOSED|phase=RESULT|from=codex|to=claude-code-cli|priority=CRITICAL")
res5 = ofm.open_for([row("2026-09-09T10:00:00Z", "codex", DONE, "DONE")], TAG)
check("a DONE row is not open work, however urgent it was", res5["total"] == 0)
MINE = ("BCB|v=1|id=MY-OWN|phase=RESULT|from=claude-code-cli|to=claude-code-cli|priority=HIGH")
res6 = ofm.open_for([row("2026-09-09T10:00:00Z", TAG, MINE, "OPEN")], TAG)
check("a row this tag WROTE is not work for this tag", res6["total"] == 0)
NOISE = "not a BCB row at all, just prose"
res7 = ofm.open_for([row("2026-09-09T10:00:00Z", "codex", NOISE, "OPEN")], TAG)
check("a non-BCB row is skipped rather than guessed at", res7["total"] == 0)
BROADCAST = ("BCB|v=1|id=TO-EVERYONE|phase=RESULT|from=codex|to=ALL|priority=NORMAL")
res8 = ofm.open_for([row("2026-09-09T10:00:00Z", "codex", BROADCAST, "OPEN")], TAG)
check("to=ALL is excluded by default - it is most of the board", res8["total"] == 0)
res9 = ofm.open_for([row("2026-09-09T10:00:00Z", "codex", BROADCAST, "OPEN")], TAG, include_all=True)
check("and included only when asked for", res9["total"] == 1)

print("")
print("== priority filter ==")
many = [row("2026-09-09T10:00:00Z", "codex",
            "BCB|v=1|id=LOW-ONE|from=codex|to=claude-code-cli|priority=NORMAL", "OPEN"),
        row("2026-09-09T11:00:00Z", "codex",
            "BCB|v=1|id=BIG-ONE|from=codex|to=claude-code-cli|priority=CRITICAL", "OPEN")]
res10 = ofm.open_for(many, TAG, min_priority="HIGH")
check("min-priority HIGH keeps CRITICAL", any(d["id"] == "BIG-ONE" for d in res10["rows"]))
check("and drops NORMAL", not any(d["id"] == "LOW-ONE" for d in res10["rows"]))

print("")
print("== a short row must not shift fields ==")
short = ["id", "2026-09-09T10:00:00Z", "codex"]        # REQ-B4TQX9 shape
try:
    res11 = ofm.open_for([short], TAG)
    check("a short row is survived, not crashed on", res11["total"] == 0)
except Exception as e:
    check("a short row is survived, not crashed on", False, repr(e))

print("")
print("== the field parser is anchored ==")
# `auto=` ends in the same two letters as `to=`; an unanchored regex matches it.
tricky = "BCB|v=1|id=X|auto=somebody-else|to=claude-code-cli|priority=HIGH"
check("to= is read, not auto=", ofm.field(tricky, "to") == "claude-code-cli",
      ofm.field(tricky, "to"))
check("and auto= reads as itself", ofm.field(tricky, "auto") == "somebody-else")

print("")
print("== a STANDING HOLD outlives the task that declared it ==")
# The real row: priority CRITICAL, addressed to this tag, carrying a hold - and
# filed Category=DONE because the investigation was done. The first version of
# open_for_me skipped it, which would have made this a fix that could not
# prevent the incident that motivated it.
REAL_HOLD = ("BCB|v=1|id=CODEX-01A0839E-G1-FIRST-CONTACT-HOLD-20260909|phase=RESULT|class=SAFETY"
             "|from=codex|to=claude-code-cli|priority=CRITICAL"
             "|hold=no further token requests, MCP connection, Salesforce query or scan")
res_h = ofm.open_for([row("2026-09-09T12:33:17Z", "codex", REAL_HOLD, "DONE",
                          "G1 first-contact evidence preserved; further Salesforce access held.")], TAG)
check("a hold filed DONE is STILL surfaced", res_h["total"] == 1, res_h["rows"])
check("and it is labelled a standing hold",
      res_h["rows"] and res_h["rows"][0]["standing_hold"] is True)
check("and its real Category is reported, not hidden",
      res_h["rows"] and res_h["rows"][0]["category"] == "DONE")
# but a plain DONE row with no hold is still not work
check("a DONE row WITHOUT a hold is still excluded",
      ofm.open_for([row("2026-09-09T10:00:00Z", "codex", DONE, "DONE")], TAG)["total"] == 0)
# and a hold this tag wrote is still not work for this tag
MY_HOLD = ("BCB|v=1|id=MY-HOLD|from=claude-code-cli|to=claude-code-cli|priority=HIGH"
           "|hold=something I told myself")
check("a hold this tag WROTE is not surfaced back to it",
      ofm.open_for([row("2026-09-09T10:00:00Z", TAG, MY_HOLD, "DONE")], TAG)["total"] == 0)

print("")
print("{0} passed, {1} failed".format(PASS, FAIL))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAIL else 0)

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
import json
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
check("the horizon is identified", res["horizon_trusted"] is True)
check("and it is the VIEWPORT row", res["horizon"]["ts"] == "2026-09-09T14:50:47Z",
      res["horizon"])

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
print("== the horizon is chosen by vseq, and refuses a future stamp ==")
# Live on the board 2026-09-10: v018 was written with a timestamp FOUR HOURS
# AHEAD and v019, the correction, carried an earlier stamp. "Newest by
# timestamp" therefore chose the superseded one, put the horizon in the future,
# and marked all 109 rows unreachable at once.
import datetime as _dt
NOW = _dt.datetime(2026, 9, 10, 3, 30, tzinfo=_dt.timezone.utc)
V018 = "BCB|v=1|phase=VIEWPORT|vseq=018|by=codex"
V019 = "BCB|v=1|phase=VIEWPORT|vseq=019|by=codex|note=corrected UTC timestamp"
skewed = [row("2026-09-10T07:00:26Z", "codex", V018, "DONE"),
          row("2026-09-10T03:02:20Z", "codex", V019, "DONE")]
h = ofm.newest_viewport(skewed, now=NOW)
check("a future-dated VIEWPORT is refused", h["vseq"] == 19, h)
check("and the correction becomes the horizon", h["ts"].startswith("2026-09-10T03:02"), h)
# without the future one present, vseq still decides over timestamp
h2 = ofm.newest_viewport([row("2026-09-10T02:00:00Z", "codex", V019, "DONE"),
                          row("2026-09-10T03:00:00Z", "codex", V018, "DONE")], now=NOW)
check("vseq beats a later timestamp", h2["vseq"] == 19, h2)

print("")
print("== the horizon is parsed, not substring-matched ==")
PROSE = ("BCB|v=1|id=CHATTY|phase=RESULT|from=codex|to=claude-code-cli|priority=LOW"
         "|note=I will write a phase=VIEWPORT row later today")
h3 = ofm.newest_viewport([row("2026-09-10T06:00:00Z", "codex", PROSE, "OPEN")], now=NOW)
check("prose mentioning phase=VIEWPORT does not move the horizon", h3 is None, h3)

print("")
print("== no trustworthy horizon FAILS CLOSED ==")
only_hold = [row("2026-09-09T12:33:17Z", "codex", HOLD, "OPEN", "a hold")]
rf = ofm.open_for(only_hold, TAG, now=NOW)
check("with no VIEWPORT at all, horizon_trusted is False", rf["horizon_trusted"] is False)
check("and every row is treated as unreachable", rf["invisible"] == rf["total"] == 1)
BAD_TS = [["id", "not-a-timestamp", "codex", "", "RESULT", HOLD, "OPEN", "p", "g", "", "", ""]]
rb = ofm.open_for(BAD_TS, TAG, now=NOW)
check("an unparseable row timestamp is unreachable, not assumed fresh",
      rb["total"] == 1 and rb["rows"][0]["invisible_to_wake_read"] is True)

print("")
print("== a hold has a lifecycle: it is not immortal ==")
PR40_HOLD = ("BCB|v=1|id=PR40-4CCB-HOLD|phase=RESULT|from=codex|to=claude-code-cli"
             "|priority=HIGH|pr=https://github.com/sfdc-24/Blackboard/pull/40"
             "|exact_head=4ccb0cd93f237eac6909b56739be698b752846e4"
             "|verdict=NO-GO|hold=do not merge PR40 at this head")
PR40_GO = ("BCB|v=1|id=PR40-8230-GO|phase=RESULT|from=codex|to=claude-code-cli"
           "|priority=HIGH|pr=https://github.com/sfdc-24/Blackboard/pull/40"
           "|exact_head=82300d1d5bd265418235b83aaa81e9af1084a841|verdict=GO")
live_shape = [row("2026-09-09T12:00:06Z", "codex", PR40_HOLD, "DONE", "PR40 4ccb NO-GO"),
              row("2026-09-09T14:18:59Z", "codex", PR40_GO, "DONE", "PR40 8230 GO"),
              row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")]
rl = ofm.open_for(live_shape, TAG, now=NOW)
held = [d for d in rl["rows"] if d["id"] == "PR40-4CCB-HOLD"]
check("a hold settled by a later GO on the same PR is still SHOWN", len(held) == 1)
check("but marked SUPERSEDED_LIKELY rather than ACTIVE",
      held and held[0]["hold_lifecycle"] == ofm.SUPERSEDED_LIKELY, held)
check("with the clearing row named", held and "PR40-8230-GO" in held[0]["hold_note"])
check("and it does NOT count as an active hold", rl["active_holds"] == [], rl["active_holds"])

EXPLICIT = ("BCB|v=1|id=CLEARER|phase=RESULT|from=codex|to=claude-code-cli"
            "|clears=CODEX-G1-HOLD|note=released")
cleared = [row("2026-09-09T12:33:17Z", "codex", HOLD, "OPEN", "a hold"),
           row("2026-09-09T16:00:00Z", "codex", EXPLICIT, "DONE"),
           row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")]
rc = ofm.open_for(cleared, TAG, now=NOW)
check("an EXPLICITLY cleared hold disappears entirely",
      not any(d["id"] == "CODEX-G1-HOLD" for d in rc["rows"]), rc["rows"])
# and an uncleared one still counts
ru = ofm.open_for([row("2026-09-09T12:33:17Z", "codex", HOLD, "OPEN", "a hold"),
                   row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")], TAG, now=NOW)
check("an uncleared hold IS an active hold", ru["active_holds"] == ["CODEX-G1-HOLD"])

print("")
print("== ONLY THE PLACER MAY LIFT THEIR OWN HOLD ==")
# The board is append-only and every instance can write to it. The first version
# honoured any row carrying clears=<id>, so ANY writer could retire a CRITICAL
# safety hold and turn the gate green. codex reproduced that against 8fdf4bd.
def with_hold(*extra):
    return [row("2026-09-09T12:33:17Z", "codex", HOLD, "OPEN", "a hold"),
            row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")] + list(extra)

IMPOSTOR = ("BCB|v=1|id=IMPOSTOR|phase=RESULT|from=somebody-else|to=claude-code-cli"
            "|clears=CODEX-G1-HOLD|note=released")
r_imp = ofm.open_for(with_hold(row("2026-09-09T16:00:00Z", "somebody-else", IMPOSTOR, "DONE")),
                     TAG, now=NOW)
check("a clear from a DIFFERENT tag does not lift the hold",
      r_imp["active_holds"] == ["CODEX-G1-HOLD"], r_imp["active_holds"])
held = [d for d in r_imp["rows"] if d["id"] == "CODEX-G1-HOLD"][0]
check("and the rejected attempt is reported, not silently dropped",
      "UNAUTHORISED" in held["hold_note"], held["hold_note"])

# Forging the payload alone is not enough: Source_Tag is written by the bus.
FORGED = ("BCB|v=1|id=FORGED|phase=RESULT|from=codex|to=claude-code-cli"
          "|clears=CODEX-G1-HOLD")
r_forge = ofm.open_for(with_hold(row("2026-09-09T16:00:00Z", "somebody-else", FORGED, "DONE")),
                       TAG, now=NOW)
check("claiming from=codex while writing under another Source_Tag fails",
      r_forge["active_holds"] == ["CODEX-G1-HOLD"], r_forge["active_holds"])

# Prose that merely contains the id is not a clear.
PROSE_CLEAR = "I think CODEX-G1-HOLD clears=CODEX-G1-HOLD should be lifted"
r_prose = ofm.open_for(with_hold(row("2026-09-09T16:00:00Z", "codex", PROSE_CLEAR, "DONE")),
                       TAG, now=NOW)
check("a non-BCB row mentioning the id does not lift it",
      r_prose["active_holds"] == ["CODEX-G1-HOLD"], r_prose["active_holds"])

# An EARLIER clear cannot pre-authorise a later hold.
EARLY = ("BCB|v=1|id=EARLY|phase=RESULT|from=codex|to=claude-code-cli|clears=CODEX-G1-HOLD")
r_early = ofm.open_for(with_hold(row("2026-09-09T10:00:00Z", "codex", EARLY, "DONE")),
                       TAG, now=NOW)
check("a clear written BEFORE the hold does not lift it",
      r_early["active_holds"] == ["CODEX-G1-HOLD"], r_early["active_holds"])

print("")
print("== a GO only supersedes on a DIFFERENT exact head, from the placer ==")
# REAL SHA LENGTHS. These fixtures used to say exact_head=4ccb and 8230 - four
# characters, shorter than git's own shortest unambiguous default. That made
# every assertion below a statement about strings git would refuse to resolve,
# and it hid the fail-open: `not same_commit(...)` called two uncomparable
# stubs "different" and lifted the hold.
HEAD_HELD = "4ccb0cd93f237eac6909b56739be698b752846e4"
HEAD_OTHER = "82300d1d5bd265418235b83aaa81e9af1084a841"
H4CCB = ("BCB|v=1|id=PR40-HOLD|phase=RESULT|from=codex|to=claude-code-cli|priority=HIGH"
         "|pr=https://x/pull/40|exact_head=" + HEAD_HELD + "|verdict=NO-GO|hold=do not merge")
def with_pr_hold(*extra):
    return [row("2026-09-09T12:00:00Z", "codex", H4CCB, "DONE", "PR40 4ccb NO-GO"),
            row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")] + list(extra)

SAME_HEAD_GO = ("BCB|v=1|id=SAME-GO|phase=RESULT|from=codex|to=claude-code-cli"
                "|pr=https://x/pull/40|exact_head=" + HEAD_HELD[:7] + "|verdict=GO")
r_same = ofm.open_for(with_pr_hold(row("2026-09-09T15:00:00Z", "codex", SAME_HEAD_GO, "DONE")),
                      TAG, now=NOW)
check("a GO on the SAME head the hold objected to clears nothing",
      r_same["active_holds"] == ["PR40-HOLD"], r_same["active_holds"])

NO_HEAD_GO = ("BCB|v=1|id=NOHEAD-GO|phase=RESULT|from=codex|to=claude-code-cli"
              "|pr=https://x/pull/40|verdict=GO")
r_nohead = ofm.open_for(with_pr_hold(row("2026-09-09T15:00:00Z", "codex", NO_HEAD_GO, "DONE")),
                        TAG, now=NOW)
check("a GO with NO exact_head clears nothing",
      r_nohead["active_holds"] == ["PR40-HOLD"], r_nohead["active_holds"])

OTHER_GO = ("BCB|v=1|id=OTHER-GO|phase=RESULT|from=somebody-else|to=claude-code-cli"
            "|pr=https://x/pull/40|exact_head=" + HEAD_OTHER + "|verdict=GO")
r_other = ofm.open_for(with_pr_hold(row("2026-09-09T15:00:00Z", "somebody-else", OTHER_GO, "DONE")),
                       TAG, now=NOW)
check("a GO from someone who did not place the hold clears nothing",
      r_other["active_holds"] == ["PR40-HOLD"], r_other["active_holds"])

GOOD_GO = ("BCB|v=1|id=GOOD-GO|phase=RESULT|from=codex|to=claude-code-cli"
           "|pr=https://x/pull/40|exact_head=" + HEAD_OTHER + "|verdict=GO")
r_good = ofm.open_for(with_pr_hold(row("2026-09-09T15:00:00Z", "codex", GOOD_GO, "DONE")),
                      TAG, now=NOW)
check("but the placer's GO on a DIFFERENT head does supersede",
      r_good["active_holds"] == [], r_good["active_holds"])

print("")
print("== a head we cannot COMPARE must never lift a hold ==")
# The fail-open codex found at 34beded. `not same_commit(a, b)` was read as
# "proven different", but it is also true for every head that cannot be parsed.
# Each of these used to supersede a CRITICAL safety hold.
for bad, why in [("main", "a branch name, not a SHA"),
                 ("8230", "hex but shorter than git's own minimum"),
                 ("HEAD~1", "a revision expression"),
                 ("82300d1d5bd265418235b83aaa81e9af1084a84z", "one non-hex character"),
                 ("", "no head at all")]:
    payload = ("BCB|v=1|id=BAD-GO|phase=RESULT|from=codex|to=claude-code-cli"
               "|pr=https://x/pull/40|exact_head=" + bad + "|verdict=GO")
    rb = ofm.open_for(with_pr_hold(row("2026-09-09T15:00:00Z", "codex", payload, "DONE")),
                      TAG, now=NOW)
    check("a GO on " + why + " leaves the hold ACTIVE",
          rb["active_holds"] == ["PR40-HOLD"], rb["active_holds"])

# And the rejection is VISIBLE - a clear that was refused is worth seeing.
rb_note = ofm.open_for(
    with_pr_hold(row("2026-09-09T15:00:00Z", "codex",
                     "BCB|v=1|id=BAD-GO|phase=RESULT|from=codex|to=claude-code-cli"
                     "|pr=https://x/pull/40|exact_head=main|verdict=GO", "DONE")),
    TAG, now=NOW)
check("and the refusal says the hold STANDS",
      any("STANDS" in d["hold_note"] for d in rb_note["rows"]),
      [d["hold_note"] for d in rb_note["rows"]])

print("")
print("== commit_relation has three states, not two ==")
check("equal full SHAs are SAME", ofm.commit_relation(HEAD_HELD, HEAD_HELD) == ofm.SAME)
check("a 7-char prefix is SAME", ofm.commit_relation(HEAD_HELD[:7], HEAD_HELD) == ofm.SAME)
check("two real, unrelated SHAs are DIFFERENT",
      ofm.commit_relation(HEAD_HELD, HEAD_OTHER) == ofm.DIFFERENT)
check("a 6-char prefix is UNCOMPARABLE, not SAME",
      ofm.commit_relation(HEAD_HELD[:6], HEAD_HELD) == ofm.UNCOMPARABLE)
check("a branch name is UNCOMPARABLE, not DIFFERENT",
      ofm.commit_relation("main", HEAD_HELD) == ofm.UNCOMPARABLE)
check("an empty head is UNCOMPARABLE", ofm.commit_relation("", HEAD_HELD) == ofm.UNCOMPARABLE)
check("same_commit still answers the narrow yes/no",
      ofm.same_commit(HEAD_HELD[:7], HEAD_HELD) and not ofm.same_commit("main", HEAD_HELD))
# The inversion itself, stated as a property: UNCOMPARABLE must never be
# reachable through `not same_commit` being treated as DIFFERENT.
check("UNCOMPARABLE is not DIFFERENT",
      ofm.UNCOMPARABLE != ofm.DIFFERENT and ofm.UNCOMPARABLE != ofm.SAME)

print("")
print("== a clearing row must be BCB v=1, not merely start with BCB| ==")
V999 = ("BCB|v=999|id=FUTURE-CLEAR|phase=RESULT|from=codex|to=claude-code-cli"
        "|clears=PR40-HOLD")
r999 = ofm.open_for(with_pr_hold(row("2026-09-09T15:00:00Z", "codex", V999, "DONE")),
                    TAG, now=NOW)
check("a v=999 envelope cannot clear a v=1 hold",
      r999["active_holds"] == ["PR40-HOLD"], r999["active_holds"])
V1 = ("BCB|v=1|id=REAL-CLEAR|phase=RESULT|from=codex|to=claude-code-cli"
      "|clears=PR40-HOLD")
rv1 = ofm.open_for(with_pr_hold(row("2026-09-09T15:00:00Z", "codex", V1, "DONE")),
                   TAG, now=NOW)
check("but a v=1 clear from the placer still works (the control)",
      rv1["active_holds"] == [], rv1["active_holds"])
check("is_canonical_bcb rejects a missing version",
      not ofm.is_canonical_bcb("BCB|id=x|to=y"))

print("")
print("== a key written twice with two values grants nothing ==")
# field() returns the FIRST match, so BCB|v=1|v=999 satisfied a check for v=1
# while also declaring v=999. A reader taking the last value would disagree
# about what the row says - and it was a row that could lift a safety hold.
SMUGGLE = ("BCB|v=1|v=999|id=SMUGGLE|phase=RESULT|from=codex|to=claude-code-cli"
           "|clears=PR40-HOLD")
rs = ofm.open_for(with_pr_hold(row("2026-09-09T15:00:00Z", "codex", SMUGGLE, "DONE")),
                  TAG, now=NOW)
check("a payload declaring v=1 AND v=999 cannot clear",
      rs["active_holds"] == ["PR40-HOLD"], rs["active_holds"])
check("is_canonical_bcb refuses conflicting versions",
      not ofm.is_canonical_bcb(SMUGGLE))
check("but the SAME value twice is harmless",
      ofm.is_canonical_bcb("BCB|v=1|v=1|id=x|to=y"))
for key in ("from", "clears", "verdict", "pr", "exact_head"):
    payload = "BCB|v=1|id=x|to=y|{0}=a|{0}=b".format(key)
    check("a conflicting " + key + " makes the row non-canonical",
          not ofm.is_canonical_bcb(payload))
check("fields() returns every value, not just the first",
      ofm.fields("BCB|v=1|v=999", "v") == ["1", "999"],
      ofm.fields("BCB|v=1|v=999", "v"))

# The other half of the split: it must still be SHOWN. Dropping an ambiguous
# row would recreate the invisible-hold defect this tool exists to fix.
AMBIG_HOLD = ("BCB|v=1|id=AMBIG-HOLD|from=codex|from=someone-else"
              "|to=claude-code-cli|priority=CRITICAL|hold=stop")
ra = ofm.open_for([row("2026-09-09T12:00:00Z", "codex", AMBIG_HOLD, "DONE"),
                   row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")],
                  TAG, now=NOW)
check("an ambiguous row is still surfaced, not dropped",
      any(d["id"] == "AMBIG-HOLD" for d in ra["rows"]), ra["rows"])
check("and it is flagged as ambiguous",
      any(d.get("ambiguous_payload") for d in ra["rows"]))
check("and the render says so", "AMBIGUOUS PAYLOAD" in ofm.render(ra))

print("")
print("== hex is not the same thing as a SHA ==")
FORTY = "a" * 40
FORTYONE = "b" * 41
check("41 hex characters is UNCOMPARABLE, not DIFFERENT",
      ofm.commit_relation(FORTYONE, FORTY) == ofm.UNCOMPARABLE,
      ofm.commit_relation(FORTYONE, FORTY))
check("a 41-hex GO therefore cannot lift a hold",
      ofm.open_for(with_pr_hold(row(
          "2026-09-09T15:00:00Z", "codex",
          "BCB|v=1|id=LONG-GO|phase=RESULT|from=codex|to=claude-code-cli"
          "|pr=https://x/pull/40|exact_head=" + FORTYONE + "|verdict=GO",
          "DONE")), TAG, now=NOW)["active_holds"] == ["PR40-HOLD"])
check("exactly 40 still works", ofm.commit_relation(FORTY, FORTY) == ofm.SAME)
check("and two real 40-char SHAs still compare DIFFERENT",
      ofm.commit_relation(HEAD_HELD, HEAD_OTHER) == ofm.DIFFERENT)

# THE LIVE SHAPE. Every fixture above uses exact_head - which I invented. The
# real PR40 rows on Blackboard - Alpha DB use `reviewed_head=`, so requiring
# exact_head made the supersede path unreachable on real data while all of the
# invented fixtures went on passing. A test built on an assumed field name
# validates the assumption, not the board.
LIVE_HOLD = ("BCB|v=1|id=CODEX-01A0839E-PR40-4CCB-NOGO-20260909|phase=RESULT"
             "|from=chatgpt-codex-desktop-01a0839e|to=claude-code-cli|priority=HIGH"
             "|pr=https://github.com/sfdc-24/Blackboard/pull/40"
             "|reviewed_head=4ccb0cd93f237eac6909b56739be698b752846e4|verdict=NO-GO"
             "|hold=wrap-up disabled; green CI does not cover these event contracts")
LIVE_GO = ("BCB|v=1|id=CODEX-01A0839E-PR40-8230-GO-20260909|phase=RESULT"
           "|from=chatgpt-codex-desktop-01a0839e|to=claude-code-cli|priority=HIGH"
           "|pr=https://github.com/sfdc-24/Blackboard/pull/40"
           "|reviewed_head=82300d1d5bd265418235b83aaa81e9af1084a841|verdict=GO")
live = [row("2026-09-09T12:00:06Z", "chatgpt-codex-desktop-01a0839e", LIVE_HOLD, "DONE"),
        row("2026-09-09T14:18:59Z", "chatgpt-codex-desktop-01a0839e", LIVE_GO, "DONE"),
        row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")]
r_live = ofm.open_for(live, TAG, now=NOW)
check("the LIVE row shape (reviewed_head) supersedes correctly",
      r_live["active_holds"] == [], r_live["active_holds"])
check("head_of reads reviewed_head when exact_head is absent",
      ofm.head_of(LIVE_GO).startswith("82300d1d"), ofm.head_of(LIVE_GO))
check("and still prefers exact_head when both are present",
      ofm.head_of("BCB|v=1|exact_head=aaa|reviewed_head=bbb") == "aaa")

print("")
print("== a short SHA and a full SHA are the SAME commit ==")
# The fleet writes both. The live PR40 hold carries 4ccb0cd9... while its id
# says PR40-4CCB. String inequality called those different, so a GO on the SAME
# commit at a different length would have superseded a hold placed on it - a
# fail-open inside the thing meant to keep holds alive. Found by codex at a5453de.
FULL = "4ccb0cd93f237eac6909b56739be698b752846e4"
check("full vs its own 7-char prefix is the same commit",
      ofm.same_commit(FULL, FULL[:7]))
check("full vs its 12-char prefix is the same commit", ofm.same_commit(FULL[:12], FULL))
check("identical strings are the same commit", ofm.same_commit(FULL, FULL))
check("a genuinely different SHA is NOT the same commit",
      not ofm.same_commit(FULL, "82300d1d5bd265418235b83aaa81e9af1084a841"))
check("a prefix shorter than 7 is refused, not guessed",
      not ofm.same_commit(FULL, "4ccb"))
check("non-hex is refused", not ofm.same_commit(FULL, "4ccb0cd-branch"))
check("empty is refused", not ofm.same_commit(FULL, "") and not ofm.same_commit("", FULL))

# And the behaviour that matters: a GO on the same commit must NOT lift the hold.
SHORT_HOLD = ("BCB|v=1|id=SHORT-HOLD|phase=RESULT|from=codex|to=claude-code-cli"
              "|pr=https://x/pull/40|reviewed_head=" + FULL[:12] + "|verdict=NO-GO"
              "|hold=do not merge this head")
SAME_COMMIT_GO = ("BCB|v=1|id=SAMECOMMIT-GO|phase=RESULT|from=codex|to=claude-code-cli"
                  "|pr=https://x/pull/40|reviewed_head=" + FULL + "|verdict=GO")
r_sha = ofm.open_for([row("2026-09-09T12:00:00Z", "codex", SHORT_HOLD, "DONE"),
                      row("2026-09-09T15:00:00Z", "codex", SAME_COMMIT_GO, "DONE"),
                      row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")], TAG, now=NOW)
check("a GO on the SAME commit written longer does NOT lift the hold",
      r_sha["active_holds"] == ["SHORT-HOLD"], r_sha["active_holds"])

print("")
print("== board text is sanitised and bounded ==")
NASTY = ("BCB|v=1|id=NASTY|phase=RESULT|from=codex|to=claude-code-cli|priority=HIGH")
NEWLINE = chr(10)
ESC = chr(27)
BEL = chr(7)
# Built from chr() rather than backslash escapes. This file has been corrupted
# twice by passing escapes through a shell heredoc, and a mangled test that
# still passes is worse than no test at all.
nasty_gist = ("line one" + NEWLINE + "line two" + ESC + "[31mred" + BEL + " "
              + ("x" * 500))
rn = ofm.open_for([row("2026-09-09T13:00:00Z", "codex", NASTY, "OPEN", nasty_gist),
                   row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")], TAG, now=NOW)
g = rn["rows"][0]["gist"]
check("newlines are removed", NEWLINE not in g)
check("escape sequences are removed", ESC not in g and BEL not in g)
check("and it is bounded", len(g) <= ofm.MAX_GIST, len(g))
check("truncation is visible", g.endswith("..."))
big = [row("2026-09-09T13:00:{0:02d}Z".format(i % 60), "codex",
           "BCB|v=1|id=R{0}|from=codex|to=claude-code-cli|priority=LOW".format(i), "OPEN")
       for i in range(300)]
big.append(row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE"))
rbig = ofm.open_for(big, TAG, now=NOW)
check("row DISPLAY is capped", rbig["shown"] == ofm.MAX_ROWS, rbig["shown"])
check("but the total reports ALL matching rows", rbig["total"] == 300, rbig["total"])
check("and the number withheld is reported", rbig["truncated"] == 100, rbig["truncated"])
check("the cap applies to the JSON too, not just the text",
      len(json.dumps(rbig)) < 200000 and len(rbig["rows"]) == ofm.MAX_ROWS)

print("")
print("== THE CAP IS A DISPLAY LIMIT, NEVER A SAFETY DECISION ==")
# codex reproduced this against 8fdf4bd: a hold sitting past the display cap
# vanished from active_holds and the gate exited 0. A cap that silently decides
# "no holds" is a fail-open dressed as tidiness.
buried = [row("2026-09-09T13:00:{0:02d}Z".format(i % 60), "codex",
              "BCB|v=1|id=NOISE{0}|from=codex|to=claude-code-cli|priority=LOW".format(i),
              "OPEN") for i in range(250)]
# timestamped LAST so it sorts past the cap
buried.append(row("2026-09-09T23:59:59Z", "codex",
                  "BCB|v=1|id=BURIED-HOLD|from=codex|to=claude-code-cli|priority=CRITICAL"
                  "|hold=do not touch the thing", "DONE", "a hold past the cap"))
buried.append(row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE"))
rb2 = ofm.open_for(buried, TAG, now=NOW)
check("the buried hold is NOT in the displayed rows",
      not any(d["id"] == "BURIED-HOLD" for d in rb2["rows"]))
check("but it IS in active_holds anyway", "BURIED-HOLD" in rb2["active_holds"],
      rb2["active_holds"])
check("and it is called out as not displayed",
      "BURIED-HOLD" in rb2["active_holds_beyond_cap"], rb2["active_holds_beyond_cap"])
check("the unreachable count also covers rows past the cap",
      rb2["invisible"] >= ofm.MAX_ROWS, rb2["invisible"])
check("and the render says the counts cover everything",
      "cover ALL of them" in ofm.render(rb2))

print("")
print("== the board envelope is validated, not assumed ==")
HDR = list(ofm.BOARD_HEADER) + ["", ""]
GOOD_ROW = row("2026-09-09T15:00:00Z", "codex", "BCB|v=1|id=X|to=claude-code-cli", "OPEN")


def refuses(data, why):
    try:
        ofm.load_board(data)
        check("refuses " + why, False, "accepted it")
    except ofm.BoardError:
        check("refuses " + why, True)


refuses({"ok": False, "error": "nope", "rows": [HDR, GOOD_ROW]},
        "an ok=false error envelope that happens to carry rows")
refuses({"rows": []}, "an empty rows list")
refuses({"rows": [["Row_ID", "Timestamp", "WRONG"]]}, "a header that is not BCB-1")
refuses({"rows": [HDR, "this is a string, not a row"]}, "a row that is a string")
refuses({"rows": [HDR, {"Row_ID": "x"}]}, "a row that is a dict")
refuses({"rows": [HDR, GOOD_ROW + [{"nested": 1}]]}, "a cell that is an object")
refuses({"rows": [HDR, [ "x" * (ofm.MAX_CELL_CHARS + 1) ]]}, "an oversized cell")
refuses({"rows": [HDR, GOOD_ROW + ["x"] * ofm.MAX_ROW_CELLS]}, "an absurdly wide row")
refuses("just a string", "a JSON string instead of a board")
refuses({"rows": [list(ofm.BOARD_HEADER) + ["SNEAKY"]]}, "content past the named columns")

ok_rows = ofm.load_board({"ok": True, "rows": [HDR, GOOD_ROW]})
check("accepts the real envelope and returns DATA rows only",
      len(ok_rows) == 1 and ok_rows[0][0] == GOOD_ROW[0], ok_rows)
check("a bare list of rows with a header is accepted too",
      len(ofm.load_board([HDR, GOOD_ROW])) == 1)
# The silent-drop bug: rows[1:] on a headerless file ate a real row and said
# nothing. It must now refuse rather than quietly answer about fewer rows.
refuses([GOOD_ROW, GOOD_ROW], "a headerless list, rather than eating row one")

print("")
print("== credential-shaped text never reaches stdout or JSON ==")
SECRETS = [
    ("AKIAIOSFODNN7EXAMPLE", "an AWS key id"),
    ("ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "a GitHub token"),
    ("xoxb-123456789012-abcdefghijklmnop", "a Slack bot token"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g", "a JWT"),
    ("client_secret=hunter2hunter2hunter2", "a labelled client secret"),
    ("Bearer: 00D5g000004abcdEAA!AQEAQxyz123456789", "a bearer line"),
]
for secret, why in SECRETS:
    out = ofm.sanitize("leaked " + secret + " here", 200)
    check("redacts " + why, secret not in out and ofm.REDACTED in out, out)

# The control that keeps redaction from eating the thing it protects: a git SHA
# is 40 lowercase hex characters and must survive, or the hold comparison above
# starts comparing "[REDACTED]" to "[REDACTED]" and calls everything the same.
check("but a full git SHA is NOT redacted",
      HEAD_HELD in ofm.sanitize("head " + HEAD_HELD, 200),
      ofm.sanitize("head " + HEAD_HELD, 200))
check("and a short SHA is not redacted either",
      HEAD_HELD[:7] in ofm.sanitize("head " + HEAD_HELD[:7], 200))

print("")
print("== every serialised field is bounded, not just the gist ==")
LONG = "z" * 5000
noisy = [HDR,
         # hold= so it surfaces whatever the (absurd) Category cell says - which
         # is the point: Category is a board cell too, and it must be bounded.
         [LONG, LONG, LONG, "", "RESULT",
          "BCB|v=1|id=" + LONG + "|phase=" + LONG + "|to=claude-code-cli"
          "|priority=" + LONG + "|hold=stop",
          LONG, "", LONG, "", "", ""],
         row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE")]
rn = ofm.open_for(ofm.load_board(noisy), TAG, now=NOW)
if rn["rows"]:
    d = rn["rows"][0]
    for fieldname, cap in [("ts", ofm.MAX_TS), ("from", ofm.MAX_TAG), ("id", ofm.MAX_ID),
                           ("phase", ofm.MAX_PHASE), ("priority", ofm.MAX_PRIORITY),
                           ("gist", ofm.MAX_GIST), ("category", ofm.MAX_CATEGORY)]:
        check("field " + fieldname + " is capped at " + str(cap),
              len(d[fieldname]) <= cap, (fieldname, len(d[fieldname])))
    check("the whole rendered line is bounded too", len(ofm.render(rn)) < 4000,
          len(ofm.render(rn)))
else:
    check("the noisy row was surfaced at all", False, rn)

print("")
print("== bounding the hold ARRAY must not bound the hold DECISION ==")
many = []
for i in range(ofm.MAX_HOLDS_LISTED + 25):
    many.append(row("2026-09-09T12:{0:02d}:00Z".format(i % 60), "codex-" + str(i),
                    "BCB|v=1|id=HOLD-{0}|from=codex-{0}|to=claude-code-cli"
                    "|priority=CRITICAL|hold=stop".format(i), "DONE"))
many.append(row("2026-09-09T14:50:47Z", "codex", VIEWPORT, "DONE"))
rm = ofm.open_for(many, TAG, now=NOW)
check("the listed array is capped",
      len(rm["active_holds"]) == ofm.MAX_HOLDS_LISTED, len(rm["active_holds"]))
check("but the TOTAL is exact",
      rm["active_holds_total"] == ofm.MAX_HOLDS_LISTED + 25, rm["active_holds_total"])
check("and the render admits how many it did not list",
      "and 25 more not listed" in ofm.render(rm))
# The row-201 fail-open, one level down: if the gate asked "is the array empty?"
# a board with only capped-away holds would exit 0. It asks the count.
check("the exit decision reads the count, not the capped array",
      rm["active_holds_total"] > len(rm["active_holds"]) and rm["active_holds_total"] > 0)

print("")
print("{0} passed, {1} failed".format(PASS, FAIL))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAIL else 0)

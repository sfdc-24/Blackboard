#!/usr/bin/env python3
"""Negative controls for tests/order_crossdiff.py.

WHAT IS ON TRIAL
  The differ is the thing that renders a verdict on whether the ORDER port
  preserves behaviour. It reported "No behavioural differences" and exited 0
  when BOTH sides had failed identically - on PowerShell 7.4.6 every fixture
  pass errored on board or state JSON, and the diff called it a match. Codex
  reproduced it against 373f9da.

  Two runs agreeing that they both broke is not evidence a port works. It is
  evidence that nothing was measured, wearing a green result.

  So the cases here are mostly about what the differ must REFUSE. A differ
  that only ever says "same" is not a differ.

  No network, no PowerShell, no artifacts on disk beyond a temp file.

RUN
  python3 tests/test_order_crossdiff.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIFFER = os.path.join(HERE, "order_crossdiff.py")

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


def artifact(platform, version, passes, decisions=None, fixture="A" * 64):
    return {
        "platform": platform,
        "ps_version": version,
        "ps_edition": "Desktop" if platform == "Windows" else "Core",
        "fixture_sha": fixture,
        "passes": passes,
        "state": {},
        "log_decisions": decisions if decisions is not None else {
            "replay": [{"event": "poll_started", "level": "", "code": "", "row": ""}]},
    }


def good_pass(scenario, n, status="candidate_observed", row="old-order-044"):
    verdict = {"ok": True, "status": status, "run_id": "<RUNID32>",
               "row_id": row, "mode": "Observe"}
    return {"scenario": scenario, "pass": n,
            "stdout": json.dumps(verdict, separators=(",", ":")), "threw": ""}


def run_differ(a, b):
    paths = []
    for doc in (a, b):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        json.dump(doc, fh)
        fh.close()
        paths.append(fh.name)
    try:
        proc = subprocess.run([sys.executable, DIFFER, paths[0], paths[1]],
                              capture_output=True, text=True)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    finally:
        for p in paths:
            os.unlink(p)


print("== the case that shipped: two identical FAILURES are not a match ==")
# Exactly what PowerShell 7.4.6 produced - both sides threw the same thing.
broken = [{"scenario": "replay", "pass": 1, "stdout": "",
           "threw": "BOARD_READ_JSON_INVALID"}]
code, out = run_differ(artifact("Windows", "5.1.19041", broken),
                       artifact("Linux", "7.4.6", broken))
check("it refuses rather than reporting a match", code != 0, "exit " + str(code))
check("and says agreement would be agreement about failure",
      "agreement about failure" in out, out[:200])
check("it does NOT claim there were no differences",
      "No behavioural differences" not in out)

print("")
print("== other shapes of not-actually-running ==")
for label, passes in [
        ("a pass with empty stdout",
         [{"scenario": "replay", "pass": 1, "stdout": "", "threw": ""}]),
        ("a pass whose stdout is not JSON",
         [{"scenario": "replay", "pass": 1, "stdout": "Exception at line 12",
           "threw": ""}]),
        ("a verdict with ok=false",
         [{"scenario": "replay", "pass": 1,
           "stdout": '{"ok":false,"status":"error"}', "threw": ""}]),
        ("a verdict with ok as the STRING true",
         [{"scenario": "replay", "pass": 1,
           "stdout": '{"ok":"true","status":"x"}', "threw": ""}]),
]:
    code, out = run_differ(artifact("Windows", "5.1.19041", passes),
                           artifact("Linux", "7.5.4", passes))
    check("refuses " + label, code != 0, "exit " + str(code))

# A run_error in the log is a failure even when stdout looks fine.
code, out = run_differ(
    artifact("Windows", "5.1.19041", [good_pass("replay", 1)],
             {"replay": [{"event": "run_error", "level": "error",
                          "code": "EXCEPTION_CALLING_GETCURRENT", "row": ""}]}),
    artifact("Linux", "7.5.4", [good_pass("replay", 1)],
             {"replay": [{"event": "run_error", "level": "error",
                          "code": "EXCEPTION_CALLING_GETCURRENT", "row": ""}]}))
check("refuses a logged run_error even with a clean-looking verdict", code != 0)

print("")
print("== and it refuses to compare a host with itself ==")
code, out = run_differ(artifact("Linux", "7.5.4", [good_pass("replay", 1)]),
                       artifact("Linux", "7.5.4", [good_pass("replay", 1)]))
check("same platform and version is not a cross-host comparison", code != 0)
check("and it says so", "same host twice" in out, out[:200])

print("")
print("== THE POSITIVE CONTROL: a real match still passes ==")
# Without this, every assertion above is satisfied by a differ that always
# refuses, which would be useless in a different way.
good = [good_pass("seeded", 1, "tail_seeded", ""), good_pass("replay", 1)]
code, out = run_differ(artifact("Windows", "5.1.19041", good),
                       artifact("Linux", "7.5.4", good))
check("two real, agreeing runs are reported as a match", code == 0,
      "exit " + str(code) + " " + out[-300:])
check("and it says so explicitly", "No behavioural differences" in out)

print("")
print("== a real DIFFERENCE is still caught ==")
diff_b = [good_pass("seeded", 1, "tail_seeded", ""),
          good_pass("replay", 1, "no_eligible_order", "")]
code, out = run_differ(artifact("Windows", "5.1.19041", good),
                       artifact("Linux", "7.5.4", diff_b))
check("differing verdicts are reported as a difference", code != 0)
check("and the differing stdout is shown", "no_eligible_order" in out)

print("")
print("== a different fixture is refused before anything is compared ==")
code, out = run_differ(artifact("Windows", "5.1.19041", good, fixture="A" * 64),
                       artifact("Linux", "7.5.4", good, fixture="B" * 64))
check("mismatched fixture sha stops the comparison", code != 0)

print("")
print("{0} passed, {1} failed".format(PASS, FAIL))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAIL else 0)

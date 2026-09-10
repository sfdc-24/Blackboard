#!/usr/bin/env python3
"""Diff two order_fixture_crosscheck artifacts.

Equal output on equal input is the only evidence that the port preserves
behaviour. This compares the parts that are supposed to match and REPORTS the
parts that are allowed to differ rather than hiding them - a differ that quietly
drops fields is how a port gets declared equivalent when it is not.
"""
import json
import sys

a_path, b_path = sys.argv[1], sys.argv[2]
a = json.load(open(a_path, encoding="utf-8-sig"))
b = json.load(open(b_path, encoding="utf-8-sig"))

print("A: {0:<10} PowerShell {1} ({2})".format(a["platform"], a["ps_version"], a["ps_edition"]))
print("B: {0:<10} PowerShell {1} ({2})".format(b["platform"], b["ps_version"], b["ps_edition"]))
print("")

problems = []


def pass_succeeded(p):
    """Did this pass actually RUN, or merely finish?

    THE FAILURE ORACLE. Without one, this differ compares two failures and
    reports MATCH - which is exactly what it did on PowerShell 7.4.6, where
    every pass errored on board or state JSON and the diff still said "no
    behavioural differences" and exited 0. Codex reproduced it against 373f9da.

    Two runs agreeing that they both broke is not evidence that a port
    preserves behaviour. It is evidence that nothing was measured, and it is
    worse than a red result because it wears a green one.
    """
    if p.get("threw"):
        return False, "threw: " + str(p["threw"])[:120]
    out = (p.get("stdout") or "").strip()
    if not out:
        return False, "no stdout at all"
    try:
        verdict = json.loads(out.splitlines()[-1])
    except Exception:
        return False, "last stdout line is not the JSON verdict: " + out.splitlines()[-1][:100]
    if not isinstance(verdict, dict):
        return False, "verdict is not an object"
    if verdict.get("ok") is not True:
        return False, "verdict ok is {0!r}, not true".format(verdict.get("ok"))
    return True, ""


def side_ran(doc, label):
    """Every pass on this side must have succeeded, and no run_error logged."""
    ok = True
    for p in doc["passes"]:
        good, why = pass_succeeded(p)
        if not good:
            ok = False
            problems.append("{0} {1} pass {2} did not run: {3}".format(
                label, p.get("scenario", "?"), p.get("pass", "?"), why))
    for scenario, decisions in (doc.get("log_decisions") or {}).items():
        for d in decisions or []:
            if str(d.get("event")) in ("run_error", "<UNPARSEABLE>"):
                ok = False
                problems.append("{0} {1} logged {2} [{3}]".format(
                    label, scenario, d.get("event"), d.get("code") or ""))
    return ok


# The oracle runs BEFORE any comparison, because a comparison between two
# broken runs has nothing to say.
a_ran = side_ran(a, "A")
b_ran = side_ran(b, "B")
print("side A actually ran : {0}".format(a_ran))
print("side B actually ran : {0}".format(b_ran))
if not (a_ran and b_ran):
    print("")
    print("REFUSING TO COMPARE. At least one side did not produce a real run, so")
    print("any agreement between them would be agreement about failure.")
    for p in problems:
        print("  - " + p)
    sys.exit(1)
print("")

# A run must also be from two DIFFERENT platforms, or it is a comparison with
# itself wearing two filenames.
if a["platform"] == b["platform"] and a["ps_version"] == b["ps_version"]:
    print("REFUSING TO COMPARE: both artifacts are {0} PowerShell {1}. That is the "
          "same host twice, not a cross-host comparison.".format(
              a["platform"], a["ps_version"]))
    sys.exit(1)

# The input must be identical or nothing below means anything.
if a["fixture_sha"] != b["fixture_sha"]:
    problems.append("DIFFERENT FIXTURE: {0} vs {1}".format(a["fixture_sha"][:16], b["fixture_sha"][:16]))
    print("!! the two runs did not read the same fixture; stopping")
    sys.exit(1)
print("same fixture      : {0}".format(a["fixture_sha"][:32]))


def decisions(doc, scenario):
    raw = doc["log_decisions"].get(scenario) or []
    return [(d.get("event"), d.get("level"), d.get("code"), d.get("row")) for d in raw]


scenarios = sorted(set(list(a["log_decisions"].keys()) + list(b["log_decisions"].keys())))
for s in scenarios:
    da, db = decisions(a, s), decisions(b, s)
    if da == db:
        print("scenario {0:<8}: MATCH ({1} decisions)".format(s, len(da)))
        for d in da:
            print("      {0}{1}{2}".format(
                d[0],
                " [" + d[2] + "]" if d[2] else "",
                " row=" + d[3] if d[3] else ""))
    else:
        problems.append("scenario {0}: decisions differ".format(s))
        print("scenario {0:<8}: DIFFER".format(s))
        for i in range(max(len(da), len(db))):
            x = da[i] if i < len(da) else None
            y = db[i] if i < len(db) else None
            mark = "  " if x == y else ">>"
            print("   {0} A={1}".format(mark, x))
            print("   {0} B={1}".format(mark, y))

print("")
pa = {(p["scenario"], p["pass"]): p for p in a["passes"]}
pb = {(p["scenario"], p["pass"]): p for p in b["passes"]}
for key in sorted(set(pa) | set(pb)):
    x, y = pa.get(key), pb.get(key)
    label = "{0} pass {1}".format(key[0], key[1])
    if not x or not y:
        problems.append(label + ": missing on one side")
        print("{0:<18}: MISSING ON ONE SIDE".format(label))
        continue
    if x["stdout"] == y["stdout"] and x["threw"] == y["threw"]:
        print("{0:<18}: stdout MATCH".format(label))
    else:
        problems.append(label + ": stdout differs")
        print("{0:<18}: STDOUT DIFFERS".format(label))
        print("   A: " + (x["stdout"] or "<none>")[:240])
        print("   B: " + (y["stdout"] or "<none>")[:240])
        if x["threw"] or y["threw"]:
            print("   A threw: " + (x["threw"] or "<none>")[:200])
            print("   B threw: " + (y["threw"] or "<none>")[:200])

print("")
if problems:
    print("{0} DIFFERENCE(S):".format(len(problems)))
    for p in problems:
        print("  - " + p)
    sys.exit(1)
print("No behavioural differences in decisions or stdout.")
print("This is equal output on equal input. It is NOT proof the port is")
print("complete: only the fixture's paths were exercised, in Observe mode,")
print("with no provider invocation and no board write.")
sys.exit(0)

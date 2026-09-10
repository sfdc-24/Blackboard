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

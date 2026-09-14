#!/usr/bin/env python3
"""Diff two order_fixture_crosscheck artifacts.

Equal output on equal input is the only evidence that the port preserves
behaviour. This compares the parts that are supposed to match and REPORTS the
parts that are allowed to differ rather than hiding them - a differ that quietly
drops fields is how a port gets declared equivalent when it is not.
"""
import json
import sys
import re


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result

    def constant(value):
        raise ValueError('invalid JSON constant: ' + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


a_path, b_path = sys.argv[1], sys.argv[2]
with open(a_path, encoding="utf-8-sig") as source:
    a = strict_json(source.read())
with open(b_path, encoding="utf-8-sig") as source:
    b = strict_json(source.read())

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
    if type(p.get("exit_code")) is not int or p["exit_code"] != 0:
        return False, "runner exit_code must be integer zero"
    if p.get("threw") != "":
        return False, "threw: " + str(p["threw"])[:120]
    out = (p.get("stdout") or "").strip()
    if not out:
        return False, "no stdout at all"
    try:
        verdict = strict_json(out.splitlines()[-1])
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
    expected = {(s, n) for s in ("seeded", "replay") for n in (1, 2)}
    passes = doc.get("passes")
    if not isinstance(passes, list) or len(passes) != 4:
        problems.append(label + ": expected exactly four fixture passes")
        return False
    identities = []
    for p in passes:
        if not isinstance(p, dict) or type(p.get("pass")) is not int or not isinstance(p.get("scenario"), str):
            problems.append(label + ": invalid pass identity")
            return False
        identities.append((p["scenario"], p["pass"]))
    if set(identities) != expected:
        problems.append(label + ": expected seeded/replay passes 1 and 2 without duplicates")
        ok = False
    for p in passes:
        good, why = pass_succeeded(p)
        if not good:
            ok = False
            problems.append("{0} {1} pass {2} did not run: {3}".format(
                label, p.get("scenario", "?"), p.get("pass", "?"), why))
    scenarios = {"seeded", "replay"}
    logs = doc.get("log_decisions")
    states = doc.get("state")
    if not isinstance(logs, dict) or set(logs) != scenarios:
        problems.append(label + ": missing scenario decision evidence")
        return False
    if not isinstance(states, dict) or set(states) != scenarios:
        problems.append(label + ": missing scenario state evidence")
        return False
    for scenario, state in states.items():
        try:
            parsed = strict_json(state)
            if not isinstance(parsed, dict) or not parsed:
                raise ValueError("empty state")
        except (ValueError, TypeError):
            problems.append(label + " " + scenario + ": missing or invalid persisted state")
            ok = False
    for scenario, decisions in logs.items():
        if not isinstance(decisions, list) or not decisions:
            problems.append(label + " " + scenario + ": empty decision evidence")
            ok = False
            continue
        if any(not isinstance(d, dict) or not isinstance(d.get("event"), str) for d in decisions):
            problems.append(label + " " + scenario + ": invalid decision evidence")
            ok = False
            continue
        if sum(d.get("event") == "poll_started" for d in decisions) != 2:
            problems.append(label + " " + scenario + ": expected two poll_started events")
            ok = False
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
def endpoint(doc):
    version = doc.get("ps_version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
        return None
    parts = tuple(int(p) for p in version.split("."))
    if doc.get("platform") == "Windows" and doc.get("ps_edition") == "Desktop" and parts[:2] == (5, 1):
        return "Windows"
    if doc.get("platform") == "Linux" and doc.get("ps_edition") == "Core" and parts[:2] >= (7, 5):
        return "Linux"
    return None


if {endpoint(a), endpoint(b)} != {"Windows", "Linux"}:
    print("REFUSING TO COMPARE: require Windows/Desktop 5.1 versus Linux/Core 7.5+.")
    print("The same host twice or two Windows runtimes is not a cross-platform comparison.")
    sys.exit(1)

# The input must be identical or nothing below means anything.
for doc in (a, b):
    if not isinstance(doc.get("fixture_sha"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", doc["fixture_sha"]):
        print("REFUSING TO COMPARE: invalid fixture SHA-256")
        sys.exit(1)
if a["fixture_sha"].lower() != b["fixture_sha"].lower():
    problems.append("DIFFERENT FIXTURE: {0} vs {1}".format(a["fixture_sha"][:16], b["fixture_sha"][:16]))
    print("!! the two runs did not read the same fixture; stopping")
    sys.exit(1)
print("same fixture      : {0}".format(a["fixture_sha"][:32]))


def decisions(doc, scenario):
    raw = doc["log_decisions"].get(scenario) or []
    return [(d.get("event"), d.get("level"), d.get("code"), d.get("row")) for d in raw]


scenarios = sorted(set(list(a["log_decisions"].keys()) + list(b["log_decisions"].keys())))
for s in scenarios:
    # Preserve JSON types: Python's direct equality would equate true with 1.
    sa = json.dumps(strict_json(a["state"][s]), sort_keys=True, separators=(",", ":"))
    sb = json.dumps(strict_json(b["state"][s]), sort_keys=True, separators=(",", ":"))
    if sa != sb:
        problems.append("scenario {0}: persisted state differs".format(s))
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
print("No behavioural differences in decisions, stdout, or persisted state.")
print("This is equal output on equal input. It is NOT proof the port is")
print("complete: only the fixture's paths were exercised, in Observe mode,")
print("with no provider invocation and no board write.")
sys.exit(0)

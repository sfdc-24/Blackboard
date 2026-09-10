#!/usr/bin/env python3
"""Tests for scripts/xray_score.py.

WHAT IS ON TRIAL
  Two ways this engine can give a confident answer it has not earned.

  ONE - counting a population that could not contain a defect. The stale-user
  check treated "never logged in" as "stale", so in an org whose active users
  were all provisioned inside the window, every one of them scored as a defect.

  TWO - treating "could not find out" as "nothing to find". With every query
  failing, the first version built zero checks, reported that no check had a
  population that could contain a defect, and exited 0. A total outage was
  indistinguishable from a clean org.

  So the engine has THREE states, and several cases below assert
  NOT_APPLICABLE or UNKNOWN exactly where a naive implementation would happily
  return a number - because returning a number was the defect.

FIXTURES ARE SYNTHETIC
  Every number here is invented. The measurements that exposed the metric defect
  came from a scan taken under an active hold, so nothing in this file is
  derived from them.

  No network. No org. These run anywhere.

RUN
  python3 tests/test_xray_score.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "..", "scripts", "xray_score.py")
spec = importlib.util.spec_from_file_location("xray_score", ENGINE)
xs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xs)

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


print("== defect one: a population that could not contain a defect ==")
fresh = xs.Check("dormant_users", "Active users with no login in 90 days",
                 opportunities=0, defects=0, unit="user",
                 note="every active user was provisioned inside the last 90 days")
check("no opportunities means NOT_APPLICABLE", fresh.state == xs.NOT_APPLICABLE, fresh.state)
check("DPMO is NOT_APPLICABLE, not 0", fresh.dpmo == xs.NOT_APPLICABLE, fresh.dpmo)
check("sigma is NOT_APPLICABLE, not a number", fresh.sigma == xs.NOT_APPLICABLE, fresh.sigma)
check("and it carries the reason a reader needs", "provisioned inside" in fresh.note)

# The naive shape, with invented counts.
naive = xs.Check("naive", "counting the whole population", opportunities=20, defects=17)
check("the naive definition scores a defect rate anyway", naive.state == xs.SCORED)
check("and it is a large, confident, wrong number", naive.dpmo == 850000, naive.dpmo)
check("with a correspondingly awful sigma", naive.sigma["value"] < 1.0, naive.sigma)

print("")
print("== defect two: could not find out is not nothing to find ==")
unknown = xs.Check("dormant_users", "Active users", None, None, "user",
                   reason="a User COUNT() query did not return")
check("a failed query gives UNKNOWN", unknown.state == xs.UNKNOWN, unknown.state)
check("DPMO is UNKNOWN, not 0 and not NOT_APPLICABLE", unknown.dpmo == xs.UNKNOWN)
check("sigma is UNKNOWN too", unknown.sigma == xs.UNKNOWN)
check("and it says why", "did not return" in unknown.reason)
check("UNKNOWN is distinct from NOT_APPLICABLE", xs.UNKNOWN != xs.NOT_APPLICABLE)

# A total outage, driven through the real build_checks with an injected counter.
none_counter = lambda base, token, soql: None
outage = xs.build_checks("https://example.invalid", "tok", counter=none_counter)
check("a total outage still emits every check", len(outage) == 2, len(outage))
check("all of them UNKNOWN", all(c.state == xs.UNKNOWN for c in outage),
      [c.state for c in outage])
sm = xs.summarise(outage)
check("the summary says INCOMPLETE, not 'nothing to find'",
      sm["verdict"].startswith("INCOMPLETE"), sm["verdict"][:60])
check("and names which checks were not established",
      sorted(sm["checks_unknown"]) == ["dormant_users", "inactive_flows"], sm["checks_unknown"])
check("an outage is NOT reported as NOT SCOREABLE",
      "NOT SCOREABLE" not in sm["verdict"])

# Partial failure must not look like a complete run.
def only_flows(base, token, soql):
    if "FROM User" in soql:
        return None
    return 85 if "IsActive=true" in soql else 110


partial = xs.build_checks("https://x", "tok", counter=only_flows)
sp = xs.summarise(partial)
check("a partial failure still scores what it can",
      any(c.state == xs.SCORED for c in partial))
check("but the run is still INCOMPLETE", sp["verdict"].startswith("INCOMPLETE"))
check("and the unscored check is named", sp["checks_unknown"] == ["dormant_users"])

print("")
print("== units are not interchangeable ==")
mixed = [xs.Check("u", "users", 20, 9, "user"), xs.Check("f", "flows", 110, 25, "flow")]
sm2 = xs.summarise(mixed)
check("no cross-unit total is offered at all", sm2["cross_unit_total"] is None)
check("each unit rolls up separately", sorted(sm2["by_unit"]) == ["flow", "user"])
check("the user rollup uses only user opportunities",
      sm2["by_unit"]["user"]["opportunities"] == 20)
check("the flow rollup uses only flow opportunities",
      sm2["by_unit"]["flow"]["opportunities"] == 110)
check("and their DPMOs differ, as they must",
      sm2["by_unit"]["user"]["dpmo"] != sm2["by_unit"]["flow"]["dpmo"])

# An unscoreable check must not dilute a real one.
real = xs.Check("inactive_flows", "flows", 110, 25, "flow")
mix2 = xs.summarise([fresh, real])
check("an unscoreable check adds nothing to any denominator",
      mix2["by_unit"]["flow"]["opportunities"] == 110)
check("and is still named rather than dropped",
      mix2["checks_not_applicable"] == ["dormant_users"])
check("counts are reported honestly",
      mix2["checks_scored"] == 1 and mix2["checks_total"] == 2)

print("")
print("== a clean run is reported as clean, not as infinite ==")
perfect = xs.Check("perfect", "nothing wrong", 500, 0, "record")
check("zero defects gives 0 DPMO", perfect.dpmo == 0)
check("but sigma is BOUNDED rather than a made-up huge number",
      perfect.sigma["bounded"] is True and perfect.sigma["value"] is None, perfect.sigma)
check("and it explains why", "unbounded above" in perfect.sigma["why"])
worst = xs.Check("worst", "everything wrong", 7, 7, "record")
check("every opportunity a defect gives 1,000,000 DPMO", worst.dpmo == 1000000)
check("and sigma 0.0, not an exception", worst.sigma["value"] == 0.0)

print("")
print("== the arithmetic is real, not approximated ==")
check("inverse normal at 0.5 is 0", abs(xs.inv_norm_cdf(0.5)) < 1e-9)
check("inverse normal at 0.975 is 1.95996", abs(xs.inv_norm_cdf(0.975) - 1.959964) < 1e-4)
check("inverse normal at 0.025 is -1.95996", abs(xs.inv_norm_cdf(0.025) + 1.959964) < 1e-4)
check("it is symmetric", abs(xs.inv_norm_cdf(0.9) + xs.inv_norm_cdf(0.1)) < 1e-9)
six = xs.Check("six", "the textbook anchor", 1000000, 4, "record")
check("3.4-ish DPMO lands near 6 sigma with the stated 1.5 shift",
      abs(six.sigma["value"] - 6.0) < 0.15, six.sigma)

print("")
print("== a Check refuses inputs that mean the QUERY is wrong ==")
try:
    xs.Check("bad", "more defects than chances", 3, 9)
    check("more defects than opportunities is refused", False, "no exception")
except ValueError as e:
    check("more defects than opportunities is refused", True)
    check("and the message blames the query, not the org", "query is wrong" in str(e))
try:
    xs.Check("neg", "negative", -1, 0)
    check("negative counts are refused", False, "no exception")
except ValueError:
    check("negative counts are refused", True)

# More active flows than total flows means the two queries disagree.
def contradictory(base, token, soql):
    return 5 if "IsActive=true" in soql else 2


bad_flows = xs.build_checks("https://x", "tok", counter=contradictory)
flows = [c for c in bad_flows if c.key == "inactive_flows"][0]
check("more active than total is UNKNOWN, not negative defects",
      flows.state == xs.UNKNOWN, flows.state)

print("")
print("== read-only is enforced at runtime, not asserted in a comment ==")
for bad_path, why in [("/services/apexrest/thing", "not a Data API path"),
                      ("https://evil.example/services/data/v67.0/query", "absolute foreign URL")]:
    try:
        xs.get_json("https://org.my.salesforce.com", "tok", bad_path)
        check("refused: " + why, False, "no exception raised")
    except ValueError:
        check("refused: " + why, True)
    except Exception as e:
        # A network error would mean the guard did NOT fire first.
        check("refused: " + why, False, "guard did not fire, got " + type(e).__name__)
check("a redirect handler is installed rather than followed",
      hasattr(xs, "NoRedirect"))
src = open(ENGINE, encoding="utf-8").read()
# Exactly one raw urlopen (the OAuth token POST) and exactly one opener.open
# (inside get_json). Any third network call would be a path around the boundary.
check("only get_token calls urlopen directly",
      src.count("urllib.request.urlopen") == 1, src.count("urllib.request.urlopen"))
check("only get_json opens the guarded opener",
      src.count("opener.open") == 1, src.count("opener.open"))
check("count() goes through get_json rather than building its own request",
      "def count(" in src and "get_json(base, token," in src)

print("")
print("== no held-scan measurements are embedded IN THE ENGINE ==")
# Needles are assembled from fragments so this file does not itself contain the
# literals it forbids - the first version searched its own source and failed
# every case for that reason.
needles = ["9" + "00,000", "9" + "00000 DPMO", "00" + "Dbm000", "dbm0" + "0000wk2ibeac",
           "abdus." + "omnistudio", "sigma 0" + ".22"]
for leaked in needles:
    check("absent from the engine: " + leaked[:14], leaked not in src)

print("")
print("{0} passed, {1} failed".format(PASS, FAIL))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAIL else 0)

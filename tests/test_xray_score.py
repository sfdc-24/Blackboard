#!/usr/bin/env python3
"""Tests for scripts/xray_score.py.

WHAT IS ON TRIAL
  The engine's job is to refuse to score what it cannot score. The first version
  of this metric, run against the real workshop org on 2026-09-09, counted
  "never logged in" as "stale" and reported 900,000 DPMO and sigma 0.22 for an
  org whose every active user had been created inside the 90-day window. Not one
  of them had had the chance to go dormant.

  So the cases below are written against THAT failure. Several assert that the
  engine returns NOT_APPLICABLE where a naive implementation would happily
  return a number - because returning a number was the defect.

  No network. No org. These run anywhere.

RUN
  python3 tests/test_xray_score.py
"""
import importlib.util
import math
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


print("== the defect this engine exists to prevent ==")
# The real shape: 10 active users, 9 never logged in, but NONE of them has been
# in the org for 90 days. Opportunities is 0.
fresh_org = xs.Check("dormant_users", "Active users with no login in 90 days",
                     opportunities=0, defects=0, unit="user",
                     note="every active user was provisioned inside the last 90 days")
check("a check with no opportunities is NOT scoreable", not fresh_org.scoreable)
check("and its DPMO is NOT_APPLICABLE, not 0",
      fresh_org.dpmo == xs.NOT_APPLICABLE, fresh_org.dpmo)
check("and its sigma is NOT_APPLICABLE, not a number",
      fresh_org.sigma == xs.NOT_APPLICABLE, fresh_org.sigma)
check("and it carries the reason a reader needs", "provisioned inside" in fresh_org.note)

# The naive version, for contrast: 9 defects over all 10 active users.
naive = xs.Check("naive", "the old definition", opportunities=10, defects=9)
check("THE OLD DEFINITION really did produce 900,000 DPMO", naive.dpmo == 900000, naive.dpmo)
check("and a sigma around 0.22, which is what would have been shown to a customer",
      abs(naive.sigma["value"] - 0.22) < 0.02, naive.sigma)

print("")
print("== scoring nothing is not scoring perfectly ==")
summary = xs.summarise([fresh_org])
check("a rollup of only-unscoreable checks is NOT_APPLICABLE",
      summary["dpmo"] == xs.NOT_APPLICABLE, summary["dpmo"])
check("it says the org is UNMEASURED rather than clean",
      "unmeasured one" in summary["verdict"], summary["verdict"])
check("and it names which checks were skipped",
      summary["checks_not_applicable"] == ["dormant_users"], summary["checks_not_applicable"])

# An unscoreable check must not dilute a real one.
real = xs.Check("inactive_flows", "Flow definitions that are not active",
                opportunities=110, defects=25)
mixed = xs.summarise([fresh_org, real])
check("an unscoreable check does not add 0/0 to the denominator",
      mixed["opportunities"] == 110, mixed["opportunities"])
check("the scored rollup matches the scoreable check alone",
      mixed["dpmo"] == real.dpmo, (mixed["dpmo"], real.dpmo))
check("and the skipped check is still named, not dropped",
      mixed["checks_not_applicable"] == ["dormant_users"])
check("counts are reported honestly", mixed["checks_scored"] == 1 and mixed["checks_total"] == 2)

print("")
print("== a clean run is reported as clean, not as infinite ==")
perfect = xs.Check("perfect", "nothing wrong", opportunities=500, defects=0)
check("zero defects gives 0 DPMO", perfect.dpmo == 0)
check("but sigma is BOUNDED rather than a made-up huge number",
      perfect.sigma["bounded"] is True and perfect.sigma["value"] is None, perfect.sigma)
check("and it explains why", "unbounded above" in perfect.sigma["why"])

worst = xs.Check("worst", "everything wrong", opportunities=7, defects=7)
check("every opportunity a defect gives 1,000,000 DPMO", worst.dpmo == 1000000)
check("and sigma 0.0, not an exception", worst.sigma["value"] == 0.0)

print("")
print("== the arithmetic is real, not approximated ==")
# Known quantiles of the standard normal.
check("inverse normal at 0.5 is 0", abs(xs.inv_norm_cdf(0.5)) < 1e-9)
check("inverse normal at 0.975 is 1.95996", abs(xs.inv_norm_cdf(0.975) - 1.959964) < 1e-4,
      xs.inv_norm_cdf(0.975))
check("inverse normal at 0.025 is -1.95996", abs(xs.inv_norm_cdf(0.025) + 1.959964) < 1e-4)
check("it is symmetric", abs(xs.inv_norm_cdf(0.9) + xs.inv_norm_cdf(0.1)) < 1e-9)
# The classic six-sigma anchor: 3.4 DPMO with the 1.5 shift.
six = xs.Check("six", "the textbook anchor", opportunities=1000000, defects=4)
check("3.4-ish DPMO lands near 6 sigma with the stated 1.5 shift",
      abs(six.sigma["value"] - 6.0) < 0.15, six.sigma)

print("")
print("== a Check refuses inputs that mean the QUERY is wrong ==")
try:
    xs.Check("bad", "more defects than chances", opportunities=3, defects=9)
    check("more defects than opportunities is refused", False, "no exception raised")
except ValueError as e:
    check("more defects than opportunities is refused", True)
    check("and the message blames the query, not the org", "query is wrong" in str(e), str(e))
try:
    xs.Check("neg", "negative", opportunities=-1, defects=0)
    check("negative counts are refused", False, "no exception raised")
except ValueError:
    check("negative counts are refused", True)

print("")
print("== the engine is read-only by construction ==")
src = open(ENGINE, encoding="utf-8").read()
for verb in ('method="POST"', "'POST'", '"PATCH"', '"DELETE"', "composite/sobjects"):
    pass
# The token call is the one POST, and it is to the OAuth endpoint. Nothing else
# may write. This is asserted rather than trusted because a scoring engine that
# can mutate the org it scores is a different, far more dangerous product.
data_writes = [ln for ln in src.splitlines()
               if "/services/data/" in ln and ("data=" in ln or "method" in ln.lower())]
check("no Data API call carries a body or a method override", data_writes == [], data_writes)
check("the only urlopen with data= is the OAuth token request",
      src.count("data=body") == 1 and "oauth2/token" in src)

print("")
print("{0} passed, {1} failed".format(PASS, FAIL))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAIL else 0)

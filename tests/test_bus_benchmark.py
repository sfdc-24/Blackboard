#!/usr/bin/env python3
"""Tests for scripts/bus_benchmark.py.

WHAT IS ON TRIAL
  This benchmark POSTS THE LIVE BUS SECRET. So the cases that matter most are
  not about timing at all - they are about where it is willing to send that
  secret, and whether it will report a number it did not earn.

  Three real defects, all reproduced here as controls:

  1. It accepted a plaintext http:// URL and a redirect to any origin. Same
     class of hole as the token exchange in xray_score.py: the read path was
     hardened and the path actually carrying the credential was not.
  2. It timed a 63-byte empty response as a successful 755ms read, which made
     the target look ~16x faster than a measurement that never happened.
  3. Nearest-rank p95 used round() instead of ceil(), so p95 of 12 samples
     reported rank 11 - excluding the slowest sample, which is the entire point
     of a p95.

  No network. No secret. These run anywhere.

RUN
  python3 tests/test_bus_benchmark.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "bus_benchmark.py")
spec = importlib.util.spec_from_file_location("bus_benchmark", SCRIPT)
bb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bb)

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


print("== where it will and will not send a secret ==")
for bad, why in [("http://script.google.com/macros/s/x", "plaintext to a real host"),
                 ("http://evil.example/x", "plaintext to a foreign host"),
                 ("ftp://script.google.com/x", "not even http"),
                 ("//script.google.com/x", "scheme-relative")]:
    try:
        bb.assert_transport_ok(bad)
        check("refused: " + why, False, "accepted " + bad)
    except ValueError:
        check("refused: " + why, True)

for good, why in [("https://script.google.com/macros/s/x", "https anywhere"),
                  ("http://127.0.0.1:8787/", "loopback by IP"),
                  ("http://localhost:8787/", "loopback by name")]:
    try:
        bb.assert_transport_ok(good)
        check("allowed: " + why, True)
    except ValueError as e:
        check("allowed: " + why, False, str(e))

print("")
print("== redirect origins ==")
check("same origin is same origin",
      bb.same_origin("https://a.example/x", "https://a.example/y"))
check("a different host is not",
      not bb.same_origin("https://a.example/x", "https://b.example/x"))
check("a different scheme is not",
      not bb.same_origin("https://a.example/x", "http://a.example/x"))
check("a different port is not",
      not bb.same_origin("https://a.example:443/x", "https://a.example:8443/x"))

print("")
print("== the deadline is end-to-end, not per hop ==")
d = bb.Deadline(5)
first = d.remaining()
check("a fresh deadline has budget", 0 < first <= 5, first)
second = d.remaining()
check("the budget SHRINKS between calls rather than resetting",
      second <= first, (first, second))
expired = bb.Deadline(-1)
try:
    expired.remaining()
    check("an expired deadline raises", False, "no exception")
except TimeoutError:
    check("an expired deadline raises", True)

print("")
print("== nearest-rank percentile uses ceil, not round ==")
twelve = list(range(1, 13))          # 1..12, so the rank IS the value
check("p95 of 12 samples is rank 12, not 11",
      bb.percentile(twelve, 95) == 12, bb.percentile(twelve, 95))
check("p50 of 12 samples is rank 6", bb.percentile(twelve, 50) == 6,
      bb.percentile(twelve, 50))
check("p100 is the largest", bb.percentile(twelve, 100) == 12)
check("p95 never exceeds the sample count",
      bb.percentile([5], 95) == 5, bb.percentile([5], 95))
check("an empty sample gives None, not zero", bb.percentile([], 95) is None)

print("")
print("== it refuses to report a number it did not earn ==")
none_at_all = bb.summarise([None, None, None])
check("all-failed is UNKNOWN", none_at_all["state"] == bb.UNKNOWN, none_at_all)
check("and says why", "no measurement here" in none_at_all.get("why", ""))
check("UNKNOWN carries no p50 that could be quoted", "p50" not in none_at_all)
mixed = bb.summarise([10.0, None, 30.0])
check("a partial run is MEASURED but counts the failures",
      mixed["state"] == "MEASURED" and mixed["failed"] == 1, mixed)
check("and reports how many actually succeeded", mixed["ok"] == 2)

print("")
print("{0} passed, {1} failed".format(PASS, FAIL))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAIL else 0)

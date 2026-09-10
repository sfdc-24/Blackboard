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
import json
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
print("== what counts as a board, and what only looks like one ==")
GOOD = json.dumps({"ok": True, "rows": [["Row_ID"], ["a"], ["b"]]}).encode()
n, why = bb.board_rows(GOOD)
check("a real board counts its rows", n == 3 and why is None, (n, why))

for raw, why_expected in [
        (json.dumps({"ok": False, "error": "nope", "rows": [["x"]]}).encode(),
         "an ok=false envelope that still carries rows"),
        (json.dumps({"rows": "1963"}).encode(),
         "rows as a STRING, whose len() is 4"),
        (json.dumps({"rows": {"a": 1}}).encode(),
         "rows as a dict"),
        (json.dumps({"rows": []}).encode(), "zero rows"),
        (json.dumps({"rows": [["ok"], "not-a-row"]}).encode(),
         "a row that is not a list"),
        (json.dumps([1, 2, 3]).encode(), "a JSON array instead of an object"),
        (b"<html>redirect artifact</html>", "an HTML redirect artifact"),
        (b"", "an empty body"),
        (None, "no body at all")]:
    n, why = bb.board_rows(raw)
    check("refuses " + why_expected, n is None and bool(why), (n, why))

print("")
print("== every number stays with the request that produced it ==")
# The defect: sizes and rows were separate lists, appended under different
# conditions, then quoted as sizes[0] and rows[0] - one line describing one
# request, assembled from two.
calls = []


def fake_post_factory(script):
    def fake_post(url, payload, timeout=90):
        calls.append(url)
        item = script[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item
    return fake_post


real_post = bb.http_post
bb.http_post = fake_post_factory([
    b"tiny redirect artifact",                                   # invalid, 22 bytes
    json.dumps({"ok": True, "rows": [["h"]] + [["r"]] * 99}).encode(),   # valid, 100 rows
])
samples = bb.bench_read("https://x/y", "s", "t", 2)
bb.http_post = real_post
check("the invalid sample carries its own byte count and no rows",
      samples[0]["bytes"] == 22 and samples[0]["rows"] is None, samples[0])
check("and records WHY it was not a board", bool(samples[0]["why"]), samples[0])
check("the invalid sample contributes no timing", samples[0]["ms"] is None)
check("the valid sample carries both its bytes and its rows",
      samples[1]["rows"] == 100 and samples[1]["bytes"] > 100, samples[1])
check("the invalid sample's bytes are NOT the ones a report would quote",
      [s for s in samples if s["rows"] is not None][0]["bytes"] != 22)

print("")
print("== the exit code distinguishes clean, partial, and no measurement ==")


def side(read_state, failed=0, health_failed=0, drift=None):
    s = {"full_read_ms": {"state": read_state, "failed": failed},
         "health_ms": {"state": "MEASURED", "failed": health_failed}}
    if drift:
        s["row_count_varied"] = drift
    return s


def rep(cur, tgt):
    return {"sides": {"current": cur, "target": tgt}}


check("all clean is 0",
      bb.verdict(rep(side("MEASURED"), side("MEASURED"))) == bb.EXIT_OK)
check("one failed read sample is NOT 0",
      bb.verdict(rep(side("MEASURED", failed=1), side("MEASURED"))) == bb.EXIT_INCOMPLETE)
check("nine of ten failing is NOT 0 (the whole point)",
      bb.verdict(rep(side("MEASURED", failed=9), side("MEASURED"))) == bb.EXIT_INCOMPLETE)
check("a failed HEALTH probe is NOT 0",
      bb.verdict(rep(side("MEASURED"), side("MEASURED", health_failed=1)))
      == bb.EXIT_INCOMPLETE)
check("row-count drift is NOT 0",
      bb.verdict(rep(side("MEASURED"), side("MEASURED", drift=[10, 11])))
      == bb.EXIT_INCOMPLETE)
check("no measurement at all is 2",
      bb.verdict(rep(side(bb.UNKNOWN), side("MEASURED"))) == bb.EXIT_UNKNOWN)
check("UNKNOWN outranks INCOMPLETE",
      bb.verdict(rep(side("MEASURED", failed=3), side(bb.UNKNOWN))) == bb.EXIT_UNKNOWN)
check("the three codes are distinct",
      len({bb.EXIT_OK, bb.EXIT_INCOMPLETE, bb.EXIT_UNKNOWN}) == 3)

print("")
print("== redirects are followed one hop at a time, each one checked ==")
check("there is a hop ceiling", isinstance(bb.MAX_HOPS, int) and bb.MAX_HOPS >= 2)
for bad, why in [("http://script.googleusercontent.com/x", "a downgrade to plaintext"),
                 ("https://evil.example/x", "a hop to a foreign origin"),
                 ("https://notgoogle.com.evil.example/x", "a lookalike host")]:
    try:
        bb._redirect_allowed("https://script.google.com/macros/s/x", bad)
        check("refuses " + why, False, "allowed " + bad)
    except ValueError:
        check("refuses " + why, True)
for good, why in [("https://script.googleusercontent.com/echo", "Google's own script host"),
                  ("https://script.google.com/macros/s/y", "the same origin")]:
    try:
        bb._redirect_allowed("https://script.google.com/macros/s/x", good)
        check("allows " + why, True)
    except ValueError as e:
        check("allows " + why, False, str(e))

# The deadline must be SHARED across hops, not renewed per hop. Proven by
# behaviour: a deadline already spent must refuse the next hop outright.
spent = bb.Deadline(-1)
try:
    bb._open_no_follow(bb.urllib.request.Request("https://example.invalid/"), spent)
    check("a spent deadline stops the next hop", False, "it opened anyway")
except TimeoutError:
    check("a spent deadline stops the next hop", True)
except Exception as e:
    check("a spent deadline stops the next hop", False, type(e).__name__ + ": " + str(e))

print("")
print("== the same row COUNT is not the same board ==")
A = json.dumps({"ok": True, "rows": [["h"], ["alpha"], ["beta"]]}).encode()
A2 = json.dumps({"ok": True, "rows": [["h"], ["alpha"], ["beta"]]}).encode()
B = json.dumps({"ok": True, "rows": [["h"], ["alpha"], ["DIFFERENT"]]}).encode()
check("identical content gives identical digests",
      bb.board_digest(A) == bb.board_digest(A2), (bb.board_digest(A), bb.board_digest(A2)))
check("the SAME ROW COUNT with different content does NOT",
      bb.board_digest(A) != bb.board_digest(B), (bb.board_digest(A), bb.board_digest(B)))
check("and the counts really are equal, which is the whole point",
      bb.board_rows(A)[0] == bb.board_rows(B)[0] == 3)
SPACED = b'{"ok": true, "rows": [ ["h"] , ["alpha"] , ["beta"] ] }'
check("whitespace differences are not content differences",
      bb.board_digest(SPACED) == bb.board_digest(A),
      (bb.board_digest(SPACED), bb.board_digest(A)))
check("a non-board gives no digest", bb.board_digest(b"<html>") is None)
check("the digest is short enough to read", len(bb.board_digest(A)) == 16)

print("")
print("== the redirect allowlist is Apps Script, not Google ==")
for host, ok in [("script.google.com", True),
                 ("script.googleusercontent.com", True),
                 ("abc123-script.googleusercontent.com", True),
                 ("SCRIPT.GOOGLE.COM", True),
                 ("sites.google.com", False),
                 ("accounts.google.com", False),
                 ("drive.google.com", False),
                 ("evilgoogleusercontent.com", False),
                 ("googleusercontent.com.evil.example", False),
                 ("script.google.com.evil.example", False),
                 ("", False)]:
    got = bb._is_apps_script_host(host)
    check("{0!r} allowed={1}".format(host or "<empty>", ok), got == ok, got)

print("")
print("== a body is read under the DEADLINE, not just a socket timeout ==")


class DribbleResponse:
    """Never ends. `read()` always returns one more byte."""

    def read(self, n=None):
        return b"x"


try:
    bb._read_bounded(DribbleResponse(), bb.Deadline(-1))
    check("an already-spent deadline reads nothing at all", False, "it read anyway")
except TimeoutError:
    check("an already-spent deadline reads nothing at all", True)

# The stronger claim: a deadline that expires PARTWAY THROUGH an endless body
# must interrupt it. A spent-deadline test alone only proves the first check
# fires - it would pass even if the loop never checked again.
import time as _time
started = _time.perf_counter()
try:
    bb._read_bounded(DribbleResponse(), bb.Deadline(0.4))
    check("an endless body is cut off MID-READ", False, "it read forever")
except TimeoutError:
    elapsed = _time.perf_counter() - started
    check("an endless body is cut off MID-READ", 0.3 < elapsed < 8.0, elapsed)


class BigResponse:
    def __init__(self):
        self.left = bb.MAX_BODY_BYTES + bb.READ_CHUNK

    def read(self, n=None):
        n = n or bb.READ_CHUNK
        take = min(n, self.left)
        self.left -= take
        return b"y" * take


try:
    bb._read_bounded(BigResponse(), bb.Deadline(30))
    check("an oversized body is refused", False, "it was accepted")
except ValueError:
    check("an oversized body is refused", True)


class NormalResponse:
    def __init__(self, payload):
        self.buf = payload

    def read(self, n=None):
        n = n or bb.READ_CHUNK
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


check("an ordinary body still reads back exactly",
      bb._read_bounded(NormalResponse(A), bb.Deadline(30)) == A)

print("")
print("{0} passed, {1} failed".format(PASS, FAIL))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAIL else 0)

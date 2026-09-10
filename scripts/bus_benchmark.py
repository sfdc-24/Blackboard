#!/usr/bin/env python3
"""bus_benchmark - compare the CURRENT bus against the MIGRATION TARGET.

WHY THIS EXISTS
  Mr. Salam's direction, 2026-09-10: "this type of large migration and system
  setup should be blackboard's niche and a capability, so benchmark performance,
  execution before and after so we can showcase this as a capability we can
  deliver for clients."

  A migration nobody can measure is a migration nobody will buy. So this
  measures the SAME operations against BOTH buses and reports the distribution,
  not a single number - a mean alone hides exactly the tail latency that makes a
  system feel slow.

WHAT IS AND IS NOT COMPARABLE, STATED HONESTLY
  The current bus is Google Apps Script in front of a Sheet. The target is
  stdlib Python in front of SQLite on a GCloud VM. They are not the same
  software, so this is not a like-for-like engine comparison and must never be
  sold as one.

  What IS comparable is what a CLIENT actually experiences: how long a health
  check takes, and how long it takes to read the whole board. Those are the
  operations every fleet client performs, so those are the ones measured.

  READS ONLY against the live bus, deliberately. Benchmarking writes against a
  production board would leave junk rows on it for ever, and the board is a
  permanent append-only record. Write timings are collected on the target only,
  and labelled as such rather than silently compared against nothing.

WHAT IT REFUSES TO DO
  It does not report a comparison when a side failed. A missing measurement is
  reported as UNKNOWN with the reason, never as a zero or an omission - the same
  rule scripts/xray_score.py enforces, for the same reason.

USAGE
  python3 scripts/bus_benchmark.py --live-url URL --live-secret-env BUS_SECRET \
      --target-url http://127.0.0.1:8787/ --target-secret-env TARGET_SECRET \
      --runs 12 --json out.json

  Secrets are read from NAMED ENVIRONMENT VARIABLES, never from argv (D-18),
  because a command line is visible in the process table and in shell history.
"""
import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UNKNOWN = "UNKNOWN"


def percentile(values, pct):
    """Nearest-rank percentile. No interpolation: with a dozen samples,
    interpolating invents a number that was never observed.

    CEIL, not round. Nearest-rank is defined as ceil(P/100 * N); using round()
    gave p95 of 12 samples as rank 11 (0.95*12 = 11.4 rounds down), so the
    reported p95 excluded the slowest sample - which is the one a p95 exists to
    show. codex caught it at 1beac6f.
    """
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(k, len(ordered)) - 1]


def summarise(samples, unit="ms"):
    ok = [s for s in samples if s is not None]
    out = {"unit": unit, "runs": len(samples), "ok": len(ok),
           "failed": len(samples) - len(ok)}
    if not ok:
        out["state"] = UNKNOWN
        out["why"] = ("every attempt failed or returned no rows; there is no "
                      "measurement here")
        return out
    out["state"] = "MEASURED"
    out["min"] = round(min(ok), 1)
    out["p50"] = round(percentile(ok, 50), 1)
    out["p95"] = round(percentile(ok, 95), 1)
    out["max"] = round(max(ok), 1)
    out["mean"] = round(statistics.fmean(ok), 1)
    if len(ok) > 1:
        out["stdev"] = round(statistics.stdev(ok), 1)
    return out


def timed(fn):
    """Return (elapsed_ms, result) or (None, error-string)."""
    start = time.perf_counter()
    try:
        result = fn()
        return (time.perf_counter() - start) * 1000.0, result
    except Exception as exc:                       # noqa: BLE001 - any failure is a failure
        return None, "{0}: {1}".format(type(exc).__name__, exc)


LOOPBACK = ("127.0.0.1", "::1", "localhost")


def assert_transport_ok(url, where="request"):
    """HTTPS, or literal loopback. Nothing else.

    This benchmark POSTS THE LIVE BUS SECRET. An http:// URL sends it in the
    clear and a redirect to another origin sends it somewhere nobody approved,
    so both are refused rather than measured. codex found both accepted at
    1beac6f - the same class of hole as the token exchange in xray_score.py,
    which is not a coincidence: I hardened the read path and left the path that
    actually carries the credential.
    """
    parts = urllib.parse.urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return parts
    if parts.scheme == "http" and host in LOOPBACK:
        return parts        # loopback never leaves the machine
    raise ValueError("refusing a non-HTTPS {0} to {1} - this carries a secret"
                     .format(where, host or url[:40]))


def same_origin(a, b):
    """Scheme, host and port must all match. A redirect that changes any of
    them is a different destination, whatever the path says."""
    pa, pb = urllib.parse.urlparse(a), urllib.parse.urlparse(b)
    return (pa.scheme == pb.scheme
            and (pa.hostname or "").lower() == (pb.hostname or "").lower()
            and pa.port == pb.port)


class Deadline:
    """One end-to-end budget for a whole operation.

    The two-hop read previously passed the FULL timeout to each hop, so a slow
    bus could take twice the stated budget and the recorded latency meant
    something different from what the flag said.
    """

    def __init__(self, seconds):
        self.expires = time.monotonic() + seconds

    def remaining(self):
        left = self.expires - time.monotonic()
        if left <= 0:
            raise TimeoutError("end-to-end deadline exceeded")
        return left


MAX_HOPS = 4        # one POST plus the bus's own redirect, with room to spare


def _redirect_allowed(origin_url, next_url):
    """May the request carrying our secret follow this hop?

    Same origin is always fine. Apps Script legitimately sends
    script.google.com to script.googleusercontent.com, so a cross-origin hop is
    allowed only within Google's own script hosts - and assert_transport_ok has
    already refused any downgrade to plaintext.
    """
    assert_transport_ok(next_url, "redirect")
    if same_origin(origin_url, next_url):
        return
    host = (urllib.parse.urlparse(next_url).hostname or "").lower()
    if not host.endswith((".google.com", ".googleusercontent.com")):
        raise ValueError("refusing a cross-origin redirect to " + host)


def _open_no_follow(req, deadline):
    """One hop. Never follows a redirect by itself; raises _Redirected instead."""
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=deadline.remaining()) as resp:
        return resp.read()


def http_get(url, deadline=None, timeout=45):
    """GET, following redirects MANUALLY so each hop is checked.

    This used the default opener, which follows redirects on its own: hops after
    the first got no transport check, no origin check, and a FRESH socket
    timeout each - so a chain of slow redirects could run far past the deadline
    the caller thought it had set. Every hop is now validated against the
    original origin and drawn from one shared budget.
    """
    assert_transport_ok(url, "GET")
    deadline = deadline or Deadline(timeout)
    current = url
    for _ in range(MAX_HOPS):
        try:
            return _open_no_follow(urllib.request.Request(current), deadline)
        except _Redirected as hop:
            _redirect_allowed(url, hop.location)
            current = hop.location
    raise ValueError("too many redirects (more than {0})".format(MAX_HOPS))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture a 302 instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Redirected(newurl)


class _Redirected(Exception):
    def __init__(self, location):
        super().__init__(location)
        self.location = location


def http_post(url, payload, timeout=90):
    """POST, honouring the v1 bus's redirect contract.

    The live Apps Script bus answers a POST with a 302 whose Location must be
    fetched with a PLAIN GET; following the redirect automatically returns a
    redirect artifact instead of the JSON. scripts/bus.ps1 has done the two-hop
    dance since REQ-PR4EXZ made it standing law, and a benchmark that does not
    is not measuring the same operation a real client performs.

    Measured the hard way: the first version of this file did a plain POST and
    got 63 bytes and zero rows back from the live bus, then reported it as a
    successful 755ms read - which would have made the target look ~16x faster
    than a thing that had not actually been measured at all.
    """
    assert_transport_ok(url, "POST")
    deadline = Deadline(timeout)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        return _open_no_follow(req, deadline)
    except _Redirected as hop:
        # Hop two onward: PLAIN GETs on the Location, which return the real
        # JSON. Each Location is validated before it is fetched - a redirect
        # that downgrades to http, or moves to another origin, would carry the
        # request that just contained the secret somewhere unapproved.
        #
        # This used urlopen with the DEFAULT opener, so any further redirect was
        # followed automatically: unvalidated, and with a fresh timeout that
        # escaped the deadline entirely. http_get now shares this walk, and both
        # draw from the one Deadline created above.
        _redirect_allowed(url, hop.location)
        return http_get(hop.location, deadline=deadline)


def bench_health(url, runs):
    return [timed(lambda: http_get(url))[0] for _ in range(runs)]


def board_rows(raw):
    """How many board rows did this response actually carry? Or why it is not one.

    Returns (row_count, None) or (None, reason).

    `len(json.loads(res).get("rows", []))` was doing this job, and it accepted
    things that are not boards:
      * `{"ok": false, "error": "..."}` with a rows key alongside - an error
        envelope timed as a successful read;
      * `{"rows": "1963"}` - a STRING, whose len() is 4, reported as a
        successful read of four rows;
      * `{"rows": {"a": 1}}` - a dict, len() 1.
    A benchmark that counts those is not measuring a board read.
    """
    if not isinstance(raw, (bytes, bytearray)):
        return None, "no response body"
    try:
        obj = json.loads(raw)
    except Exception:
        return None, "response is not JSON"
    if not isinstance(obj, dict):
        return None, "response is a {0}, not an object".format(type(obj).__name__)
    if "ok" in obj and not obj["ok"]:
        return None, "the bus answered ok=false"
    rows = obj.get("rows")
    if not isinstance(rows, list):
        return None, "rows is a {0}, not a list".format(type(rows).__name__)
    if not rows:
        return None, "the board came back with zero rows"
    if not all(isinstance(r, list) for r in rows):
        return None, "rows contains something that is not a row"
    return len(rows), None


def bench_read(url, secret, title, runs):
    """Full-board read - the operation every fleet client actually performs.

    Every number a sample produces stays WITH that sample. The previous version
    appended to `sizes` before it knew whether the response was a board, and
    `rows` only when it was, then the report quoted sizes[0] and rows[0] - so a
    failed read's byte count could be printed beside a different, successful
    read's row count, as one line describing one request that never happened.
    """
    samples = []
    for _ in range(runs):
        ms, res = timed(lambda: http_post(url, {"action": "read", "title": title,
                                                "secret": secret}))
        rec = {"ms": None, "bytes": None, "rows": None, "why": None}
        if ms is None:
            rec["why"] = str(res)          # timed() puts the error string here
            samples.append(rec)
            continue
        rec["bytes"] = len(res) if isinstance(res, (bytes, bytearray)) else None
        count, why = board_rows(res)
        if count is None:
            # HTTP succeeding is not the same as the board arriving. Timing a
            # redirect artifact as if it were a board read is how a benchmark
            # reported 755ms for a 63-byte response carrying no board at all.
            rec["why"] = why
        else:
            rec["rows"] = count
            rec["ms"] = ms
        samples.append(rec)
    return samples


def main():
    ap = argparse.ArgumentParser(description="Compare the current bus with the migration target")
    ap.add_argument("--live-url", required=True)
    ap.add_argument("--live-secret-env", default="BUS_SECRET")
    ap.add_argument("--target-url", required=True)
    ap.add_argument("--target-secret-env", default="TARGET_SECRET")
    ap.add_argument("--title", default="Blackboard - Alpha DB")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    live_secret = os.environ.get(args.live_secret_env, "")
    target_secret = os.environ.get(args.target_secret_env, "")
    missing = [n for n, v in ((args.live_secret_env, live_secret),
                              (args.target_secret_env, target_secret)) if not v]
    if missing:
        sys.exit("missing secret env var(s): {0} - secrets come from the "
                 "environment, never from argv (D-18)".format(", ".join(missing)))

    report = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "runs": args.runs, "title": args.title, "sides": {}}

    for label, url, secret in (("current", args.live_url, live_secret),
                               ("target", args.target_url, target_secret)):
        # urlparse lives in urllib.parse. It is also visible as
        # urllib.request.urlparse by re-export, which works by accident and is
        # not something to depend on.
        side = {"url_host": urllib.parse.urlparse(url).hostname or "?"}
        side["health_ms"] = summarise(bench_health(url, args.runs))
        samples = bench_read(url, secret, args.title, args.runs)
        side["full_read_ms"] = summarise([s["ms"] for s in samples])

        # PAIRED. bytes and rows are quoted from ONE valid sample, never
        # assembled from whichever sample happened to reach each list first.
        valid = [s for s in samples if s["rows"] is not None]
        if valid:
            side["bytes"] = valid[0]["bytes"]
            side["rows"] = valid[0]["rows"]
        else:
            side["bytes"] = UNKNOWN
            side["rows"] = UNKNOWN

        counts = sorted({s["rows"] for s in valid})
        if len(counts) > 1:
            # The board is append-only and genuinely grows, so drift is not
            # automatically a defect - but it means the samples did not all
            # measure the same read, and figures across a drifting run cannot be
            # quoted as one measurement. It is reported, and it fails the run.
            side["row_count_varied"] = counts
            side["drift_rows"] = counts[-1] - counts[0]
        # Why each failed sample failed, deduplicated - a bare failure count
        # tells you something broke but never what.
        reasons = sorted({s["why"] for s in samples if s["ms"] is None and s["why"]})
        if reasons:
            side["failure_reasons"] = reasons[:10]
        report["sides"][label] = side

    print("bus_benchmark  " + report["measured_at"] + "   runs=" + str(args.runs))
    print("")
    print("{0:<16}{1:>14}{2:>14}{3:>14}{4:>14}".format("", "health p50", "health p95",
                                                       "read p50", "read p95"))
    print("-" * 72)
    for label in ("current", "target"):
        s = report["sides"][label]
        h, r = s["health_ms"], s["full_read_ms"]

        def cell(d, key):
            return UNKNOWN if d["state"] == UNKNOWN else "{0:,.0f} ms".format(d[key])
        print("{0:<16}{1:>14}{2:>14}{3:>14}{4:>14}".format(
            label, cell(h, "p50"), cell(h, "p95"), cell(r, "p50"), cell(r, "p95")))
    print("")
    for label in ("current", "target"):
        s = report["sides"][label]
        print("{0:<8} rows={1}  bytes={2}  read failures={3}/{4}".format(
            label, s["rows"], s["bytes"], s["full_read_ms"].get("failed", "?"), args.runs))
        if s["full_read_ms"]["state"] == UNKNOWN:
            print("         " + s["full_read_ms"]["why"])
        for reason in s.get("failure_reasons", []):
            print("         failed sample: " + reason)
        if s.get("row_count_varied"):
            print("         ROW COUNT DRIFTED across samples: {0} - these "
                  "samples did not all measure the same read".format(
                      s["row_count_varied"]))
        hf = s["health_ms"].get("failed", 0)
        if hf:
            print("         health probe failed {0}/{1}".format(hf, args.runs))

    print("")
    print("HOW TO READ THIS, AND HOW NOT TO:")
    print("  * NOT a like-for-like engine comparison. Apps Script over a Sheet")
    print("    versus stdlib Python over SQLite is two different systems.")
    print("  * NETWORK DISTANCE IS IN THESE NUMBERS. Run from the instance, the")
    print("    target is localhost and the current bus is a remote HTTPS call to")
    print("    Google. A client somewhere else will NOT see the target figure -")
    print("    part of the gap is proximity, not software. Run this from the")
    print("    client's own machine before quoting it to a client.")
    print("  * Row counts may differ between sides. The target holds a snapshot")
    print("    taken at import; the live board keeps growing. A difference is")
    print("    staleness, not a defect - but a LARGE one means the snapshot is old.")
    print("  * Reads only against the live bus. Benchmark writes would leave junk")
    print("    rows on a permanent append-only record for ever.")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print("")
        print("wrote " + args.json_out)

    return verdict(report)


# Exit codes. `2 if UNKNOWN in both else 0` treated nine failures out of ten as
# a clean run, because one surviving sample makes the state MEASURED. A figure
# assembled from a run that was mostly failing is not a figure to quote, and a
# benchmark that exits 0 on it is asserting that it is.
EXIT_OK = 0
EXIT_INCOMPLETE = 3      # it measured, but not cleanly enough to quote
EXIT_UNKNOWN = 2         # it did not measure at all


def verdict(report):
    """0 only when every sample on both sides succeeded and nothing drifted."""
    worst = EXIT_OK
    for label in ("current", "target"):
        s = report["sides"][label]
        if s["full_read_ms"]["state"] == UNKNOWN:
            return EXIT_UNKNOWN
        if (s["full_read_ms"].get("failed")
                or s["health_ms"].get("failed")
                or s["health_ms"]["state"] == UNKNOWN
                or s.get("row_count_varied")):
            worst = EXIT_INCOMPLETE
    return worst


if __name__ == "__main__":
    sys.exit(main())

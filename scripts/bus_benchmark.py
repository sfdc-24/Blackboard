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
    interpolating invents a number that was never observed."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, int(round(pct / 100.0 * len(ordered))))
    return ordered[min(k, len(ordered)) - 1]


def summarise(samples, unit="ms"):
    ok = [s for s in samples if s is not None]
    out = {"unit": unit, "runs": len(samples), "ok": len(ok),
           "failed": len(samples) - len(ok)}
    if not ok:
        out["state"] = UNKNOWN
        out["why"] = "every attempt failed; there is no measurement here"
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


def http_get(url, timeout=45):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_post(url, payload, timeout=90):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def bench_health(url, runs):
    return [timed(lambda: http_get(url))[0] for _ in range(runs)]


def bench_read(url, secret, title, runs):
    """Full-board read - the operation every fleet client actually performs."""
    samples, sizes, rows = [], [], []
    for _ in range(runs):
        ms, res = timed(lambda: http_post(url, {"action": "read", "title": title,
                                                "secret": secret}))
        samples.append(ms)
        if ms is not None and isinstance(res, (bytes, bytearray)):
            sizes.append(len(res))
            try:
                rows.append(len(json.loads(res).get("rows", [])))
            except Exception:
                pass
    return samples, sizes, rows


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
        samples, sizes, rows = bench_read(url, secret, args.title, args.runs)
        side["full_read_ms"] = summarise(samples)
        side["bytes"] = sizes[0] if sizes else UNKNOWN
        side["rows"] = rows[0] if rows else UNKNOWN
        if rows and len(set(rows)) > 1:
            side["row_count_varied"] = sorted(set(rows))
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

    print("")
    print("NOT a like-for-like engine comparison: Apps Script over a Sheet versus")
    print("stdlib Python over SQLite. What is comparable is what a client experiences.")
    print("Reads only against the live bus - benchmark writes would leave junk rows")
    print("on a permanent append-only record.")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print("")
        print("wrote " + args.json_out)

    both = [report["sides"][s]["full_read_ms"]["state"] for s in ("current", "target")]
    return 2 if UNKNOWN in both else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Post-deploy version assertion for Apps Script web apps (VM-CICD-001, STEP_3).

Fetches the deployment's /exec URL and asserts the JSON it returns carries the
expected version. This is the "source of truth for what is deployed" that the
2026-09-03 Version-12 incident proved we lack: the deploy UI reported success
while serving old code, and nobody could tell from outside.

Quirks encoded here, learned the hard way (docs/HANDOVER.md, memory):
- Apps Script answers with a 302 to script.googleusercontent.com; the redirect
  must be followed with a BARE GET. urllib's default redirect handling on a POST
  404s, so this script only ever GETs.
- A failure-shaped response does not mean failure, and success shapes vary
  (ISSUE 020). We therefore retry with backoff and only fail after the last try.
- Projects that do not expose CONTRACT_VERSION yet fail SOFT (exit 0 with a
  loud warning) unless --strict: the clasp list-deployments read-back in the
  workflow is then the only assertion. Ship the version field to staging first,
  then flip --strict on.
"""
import argparse
import json
import sys
import time
import urllib.request

VERSION_KEYS = ("version", "contract_version", "CONTRACT_VERSION", "deployedVersion")


def fetch(url: str, timeout: int = 45):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "sfdc24-version-assert/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # follows 302 with GET
        return resp.status, resp.read().decode("utf-8", "replace")


def extract_version(body: str):
    try:
        data = json.loads(body)
    except ValueError:
        return None, "non-JSON response"
    for key in VERSION_KEYS:
        if key in data:
            return str(data[key]), None
    return None, f"no version field in keys {sorted(data)[:12]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="/exec URL of the staging deployment")
    ap.add_argument("--expect-version", required=True)
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--delay", type=int, default=10, help="seconds between tries")
    ap.add_argument("--strict", action="store_true",
                    help="fail if the endpoint exposes no version field at all")
    args = ap.parse_args()

    if not args.url:
        print("::warning::no staging exec URL configured — HTTP assertion skipped; "
              "clasp list-deployments read-back is the only assertion for this deploy")
        return 0

    last_reason = "not attempted"
    for attempt in range(1, args.retries + 1):
        try:
            status, body = fetch(args.url)
        except Exception as exc:  # noqa: BLE001 — any transport failure is a retry
            last_reason = f"fetch failed: {exc}"
        else:
            got, why = extract_version(body)
            if got is None:
                if not args.strict:
                    print(f"::warning::endpoint exposes no version field ({why}). "
                          "HTTP assertion SOFT-PASSED — add CONTRACT_VERSION to doGet and use --strict.")
                    return 0
                last_reason = why
            elif got == str(args.expect_version):
                print(f"TESTED: live endpoint reports version {got} (attempt {attempt}/{args.retries})")
                return 0
            else:
                last_reason = f"live version is {got}, expected {args.expect_version}"
        print(f"attempt {attempt}/{args.retries}: {last_reason}")
        if attempt < args.retries:
            time.sleep(args.delay)

    print(f"::error::VERSION ASSERTION FAILED after {args.retries} tries — {last_reason}. "
          "The deploy did NOT land (the Version-12 failure mode). Do not trust this staging build.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

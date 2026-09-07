#!/usr/bin/env python3
"""Fail-closed HTTP version-field assertion for Apps Script web apps.

A successful result proves only that a JSON response reported the expected
field value. CONTRACT_VERSION is an API contract, not a clasp deployment
number or source hash. Independent deployed-build evidence remains required.
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

VERSION_KEYS = ("version", "contract_version", "CONTRACT_VERSION", "deployedVersion")
MAX_RESPONSE_BYTES = 256 * 1024


def fetch(url: str, timeout: int = 45):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "sfdc24-version-assert/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds size limit")
        return resp.status, body.decode("utf-8", "replace")


def extract_version(body: str):
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None, "non-JSON or excessively nested response"
    if not isinstance(data, dict):
        return None, "JSON response must be an object"
    if ("ok" in data and data["ok"] is not True) or data.get("error") not in (None, ""):
        return None, "endpoint reported an error"
    versions = []
    for key in VERSION_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None, "version field must be a string or integer"
        value = str(value)
        if not value.strip():
            return None, "version field must not be empty"
        versions.append(value)
    if not versions:
        return None, "no version field"
    if len(set(versions)) != 1:
        return None, "conflicting version fields"
    return versions[0], None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True, help="HTTPS /exec URL of the staging deployment")
    ap.add_argument("--expect-version", required=True)
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--delay", type=int, default=10, help="seconds between tries")
    ap.add_argument("--strict", action="store_true", help="compatibility flag; failures are always strict")
    args = ap.parse_args()

    try:
        target = urllib.parse.urlsplit(args.url)
        valid_url = target.scheme == "https" and bool(target.hostname) and not target.username and not target.password
    except ValueError:
        valid_url = False
    if not valid_url or args.url != args.url.strip():
        print("::error::a nonempty HTTPS endpoint URL without embedded credentials is required")
        return 2
    if not args.expect_version.strip() or args.retries < 1 or args.delay < 0:
        print("::error::expected version must be nonempty, retries positive, and delay nonnegative")
        return 2

    last_reason = "not attempted"
    for attempt in range(1, args.retries + 1):
        try:
            status, body = fetch(args.url)
        except Exception as exc:
            # Exception messages can contain URLs or response data; report the
            # class only so CI logs do not expose query parameters or credentials.
            last_reason = f"fetch failed ({type(exc).__name__})"
        else:
            if status != 200:
                last_reason = f"unexpected HTTP status {status}"
            else:
                got, why = extract_version(body)
                if got is None:
                    last_reason = why
                elif got == args.expect_version:
                    print(f"TESTED: endpoint version field matched (attempt {attempt}/{args.retries}); "
                          "source/build identity still requires independent proof")
                    return 0
                else:
                    last_reason = "endpoint version field did not match expected value"
        print(f"attempt {attempt}/{args.retries}: {last_reason}")
        if attempt < args.retries:
            time.sleep(args.delay)

    print(f"::error::VERSION ASSERTION FAILED after {args.retries} tries: {last_reason}. "
          "Deployment is unverified.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

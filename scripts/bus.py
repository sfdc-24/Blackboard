#!/usr/bin/env python3
"""Python client for the Blackboard bus. See README-board-clients.md.

Apps Script quirk: the /exec URL 302s to a googleusercontent host and the
redirect must be followed with a BARE GET (no method/body carried over).
So: suppress redirects, catch the HTTPError, urlopen(Location).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Derived from this file's location, never hardcoded. An absolute machine path
# here is what makes a client work for exactly one checkout on exactly one box —
# the same defect that has made the tracked .clasp.json unusable on this VM.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.environ.get("BLACKBOARD_ENV") or os.path.join(ROOT, ".env")


def load_env(path=None):
    """Read BUS_URL and BUS_SECRET from a gitignored .env (doctrine D-18).

    Defaults to <repo root>/.env; override with the BLACKBOARD_ENV variable.
    """
    path = path or ENV_PATH
    if not os.path.exists(path):
        raise SystemExit(
            f"No .env at {path}.\n"
            "The bus credentials never live in the repository (doctrine D-18).\n"
            "Create it with BUS_URL= and BUS_SECRET=, or point BLACKBOARD_ENV at one."
        )
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in ("BUS_URL", "BUS_SECRET") if not env.get(k)]
    if missing:
        raise SystemExit(f"{path} is missing: {', '.join(missing)}")
    return env


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def _fetch_once(url, data=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with OPENER.open(req, timeout=120) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if e.code in (301, 302, 303, 307, 308) and loc:
            with urllib.request.urlopen(loc, timeout=120) as resp2:
                return resp2.read().decode("utf-8", "replace")
        raise


def fetch(url, data=None, tries=5):
    """The googleusercontent redirect target intermittently 404s. Retry.

    Safe for reads (idempotent). For an append the CALLER must read back:
    the v1 bus does not dedup, so a retry after a request that actually
    landed would double the row.
    """
    last = None
    for attempt in range(tries):
        try:
            return _fetch_once(url, data)
        except urllib.error.HTTPError as e:
            last = e
            print(f"[fetch attempt {attempt+1}] HTTP {e.code}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise last


def read_board(env, title="Blackboard - Alpha DB", tries=4):
    url = env["BUS_URL"]
    payload = {"action": "read", "secret": env["BUS_SECRET"], "title": title}
    for attempt in range(tries):
        body = fetch(url, payload)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            print(f"[attempt {attempt+1}] non-JSON: {body[:200]}", file=sys.stderr)
            continue
        if "rows" in obj:
            return obj
        print(f"[attempt {attempt+1}] health blob, retrying", file=sys.stderr)
    raise SystemExit("bus never returned rows")


def main():
    env = load_env()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    obj = read_board(env)
    rows = obj["rows"]
    header = rows[0]
    out = {
        "total_rows": len(rows) - 1,
        "header": header,
        "tail": rows[-n:] if n else rows[1:],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

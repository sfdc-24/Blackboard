#!/usr/bin/env python3
"""Python client for the Blackboard bus. Apps Script 302 -> bare GET.
D-18: credentials from BLACKBOARD_ENV or <repo>/.env — never print secrets.
"""
import json, os, sys, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.environ.get("BLACKBOARD_ENV") or os.path.join(ROOT, ".env")

def load_env(path=None):
    path = path or ENV_PATH
    if not os.path.exists(path):
        raise SystemExit(f"No .env at {path}")
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in ("BUS_URL", "BUS_SECRET") if not env.get(k)]
    if missing: raise SystemExit(f"missing {missing}")
    return env

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None

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

def fetch(url, data=None, tries=None):
    if isinstance(data, dict):
        action = str(data.get("action") or "append").lower()
    else:
        action = "append" if data is not None else None
    if tries is None:
        tries = 1 if action == "append" else 5
    if action == "append" and tries != 1:
        raise ValueError("append requires exactly one attempt")
    last = None
    for attempt in range(tries):
        try:
            return _fetch_once(url, data)
        except urllib.error.HTTPError as e:
            last = e
            print(f"[fetch attempt {attempt+1}] HTTP {e.code}", file=sys.stderr)
            if attempt + 1 < tries:
                time.sleep(2 * (attempt + 1))
    raise last

def read_board(env, title="Blackboard - Alpha DB", tries=4):
    payload = {"action": "read", "secret": env["BUS_SECRET"], "title": title}
    for attempt in range(tries):
        body = fetch(env["BUS_URL"], payload)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        if "rows" in obj:
            return obj
    raise SystemExit("bus never returned rows")

def main():
    env = load_env()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    obj = read_board(env)
    rows = obj["rows"]
    print(json.dumps({"total_rows": len(rows)-1, "tail_count": min(n, max(0,len(rows)-1))}))

if __name__ == "__main__":
    main()

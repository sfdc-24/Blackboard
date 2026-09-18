#!/usr/bin/env python3
"""Stdlib Pipedream Connect client (OAuth client_credentials).
Usage:
  python pipedream_connect.py check [--project proj_...] [--env production]
Prints ok/fail only — never echoes tokens/secrets (D-18).
Env: PIPEDREAM_CLIENT_ID, PIPEDREAM_CLIENT_SECRET from BLACKBOARD_ENV or .env
"""
from __future__ import annotations
import argparse, json, os, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_CANDIDATES = [
    os.environ.get("BLACKBOARD_ENV"),
    str(ROOT / ".env"),
    "/workspace/uploads/akatia-blackboard.env",
    "/workspace/uploads/blackboard.env",
]

def load_env():
    env = {}
    for p in ENV_CANDIDATES:
        if not p or not Path(p).is_file():
            continue
        for line in Path(p).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
        break
    return env

def post_json(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
        headers={**(headers or {}), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())

def get_json(url, headers=None, timeout=60):
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())

def token(env):
    cid = env.get("PIPEDREAM_CLIENT_ID") or os.environ.get("PIPEDREAM_CLIENT_ID")
    sec = env.get("PIPEDREAM_CLIENT_SECRET") or os.environ.get("PIPEDREAM_CLIENT_SECRET")
    if not cid or not sec:
        raise SystemExit("missing PIPEDREAM_CLIENT_ID/SECRET in .env")
    status, body = post_json(
        "https://api.pipedream.com/v1/oauth/token",
        {"grant_type": "client_credentials", "client_id": cid, "client_secret": sec},
    )
    tok = body.get("access_token")
    if status != 200 or not tok:
        raise SystemExit(f"token_fail status={status}")
    return tok

def cmd_check(args):
    env = load_env()
    project = args.project or env.get("PIPEDREAM_PROJECT_ID") or "proj_5DsGpGe"
    pd_env = args.env or env.get("PIPEDREAM_ENVIRONMENT") or "production"
    try:
        tok = token(env)
    except SystemExit as e:
        print(f"ok=false reason=token {e}")
        return 1
    except urllib.error.HTTPError as e:
        print(f"ok=false reason=token_http status={e.code}")
        return 1
    except Exception as e:
        print(f"ok=false reason=token_err type={type(e).__name__}")
        return 1
    url = f"https://api.pipedream.com/v1/connect/{urllib.parse.quote(project)}/accounts?limit=1"
    headers = {"Authorization": f"Bearer {tok}", "x-pd-environment": pd_env}
    try:
        status, body = get_json(url, headers=headers)
    except urllib.error.HTTPError as e:
        print(f"ok=false reason=connect_http status={e.code} project={project} env={pd_env}")
        return 1
    except Exception as e:
        print(f"ok=false reason=connect_err type={type(e).__name__}")
        return 1
    n = len(body.get("data") or body.get("accounts") or [])
    print(f"ok=true project={project} env={pd_env} accounts_sample={n} http={status}")
    return 0

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--project", default="")
    c.add_argument("--env", default="")
    args = ap.parse_args()
    if args.cmd == "check":
        raise SystemExit(cmd_check(args))

if __name__ == "__main__":
    main()

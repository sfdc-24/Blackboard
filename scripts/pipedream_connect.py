#!/usr/bin/env python3
"""SFDC24 — Pipedream Connect client (stdlib only).

Uses existing OAuth client + Connect project:
  PIPEDREAM_CLIENT_ID
  PIPEDREAM_CLIENT_SECRET
  PIPEDREAM_PROJECT_ID          (default proj_5DsGpGe)
  PIPEDREAM_PROJECT_ENVIRONMENT (default production)

D-18: secrets from .env only — never argv, never printed.
Docs: docs/PIPEDREAM-CONNECT.md

Usage (from repo root):
  python scripts/pipedream_connect.py check
  python scripts/pipedream_connect.py token --external-user-id grok-bot
  python scripts/pipedream_connect.py apps --q whatsapp
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ENV = REPO / ".env"
TOKEN_URL = "https://api.pipedream.com/v1/oauth/token"
API = "https://api.pipedream.com/v1"


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"env file not found: {path}")
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def cfg(env: dict[str, str]) -> dict[str, str]:
    need = ["PIPEDREAM_CLIENT_ID", "PIPEDREAM_CLIENT_SECRET"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        raise SystemExit(f"missing in .env: {', '.join(missing)}")
    return {
        "client_id": env["PIPEDREAM_CLIENT_ID"],
        "client_secret": env["PIPEDREAM_CLIENT_SECRET"],
        "project_id": env.get("PIPEDREAM_PROJECT_ID") or "proj_5DsGpGe",
        "project_environment": env.get("PIPEDREAM_PROJECT_ENVIRONMENT") or "production",
    }


def http_json(method: str, url: str, *, headers: dict | None = None, body: dict | None = None, timeout: int = 60):
    data = None if body is None else json.dumps(body).encode("utf-8")
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if e.fp else ""
        try:
            payload = json.loads(raw) if raw else {"error": raw}
        except json.JSONDecodeError:
            payload = {"error": raw[:400]}
        return e.code, payload


def access_token(c: dict[str, str], scope: str | None = None) -> str:
    body = {
        "grant_type": "client_credentials",
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
    }
    if scope:
        body["scope"] = scope
    code, payload = http_json("POST", TOKEN_URL, body=body)
    if code != 200 or not payload.get("access_token"):
        # never dump client secret; error body is ok if it has no secret
        raise SystemExit(f"oauth token failed HTTP {code}: {payload}")
    return payload["access_token"]


def auth_headers(token: str, c: dict[str, str]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "x-pd-environment": c["project_environment"],
    }


def cmd_check(c: dict[str, str]) -> int:
    token = access_token(c)
    # List apps is a cheap Connect-authenticated probe
    url = f"{API}/connect/{c['project_id']}/apps?limit=1"
    code, payload = http_json("GET", url, headers=auth_headers(token, c))
    ok = code == 200
    print(
        json.dumps(
            {
                "ok": ok,
                "http": code,
                "project_id": c["project_id"],
                "project_environment": c["project_environment"],
                "client_id_present": True,
                "secret_present": True,
                "apps_probe": "ok" if ok else payload,
            },
            indent=2,
        )
    )
    return 0 if ok else 1


def cmd_token(c: dict[str, str], external_user_id: str, expires_in: int) -> int:
    token = access_token(c)
    url = f"{API}/connect/{c['project_id']}/tokens"
    body = {"external_user_id": external_user_id, "expires_in": expires_in}
    code, payload = http_json("POST", url, headers=auth_headers(token, c), body=body)
    if code not in (200, 201):
        print(json.dumps({"ok": False, "http": code, "error": payload}, indent=2))
        return 1
    # Connect token is a credential — show only metadata + redacted preview
    ct = payload.get("token") or payload.get("connect_token") or ""
    preview = (ct[:6] + "…" + ct[-4:]) if len(ct) > 12 else "(set)"
    print(
        json.dumps(
            {
                "ok": True,
                "http": code,
                "external_user_id": external_user_id,
                "expires_at": payload.get("expires_at") or payload.get("expires_in"),
                "connect_token_preview": preview,
                "note": "full token not printed (D-18); use --print-token only on a trusted console",
            },
            indent=2,
        )
    )
    return 0


def cmd_token_print(c: dict[str, str], external_user_id: str, expires_in: int) -> int:
    """Print full connect token — for local wiring only."""
    token = access_token(c)
    url = f"{API}/connect/{c['project_id']}/tokens"
    body = {"external_user_id": external_user_id, "expires_in": expires_in}
    code, payload = http_json("POST", url, headers=auth_headers(token, c), body=body)
    if code not in (200, 201):
        print(json.dumps({"ok": False, "http": code, "error": payload}, indent=2), file=sys.stderr)
        return 1
    ct = payload.get("token") or payload.get("connect_token")
    if not ct:
        print(json.dumps(payload, indent=2))
        return 1
    sys.stdout.write(ct)
    if not ct.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_apps(c: dict[str, str], q: str | None, limit: int) -> int:
    token = access_token(c)
    qs = urllib.parse.urlencode({k: v for k, v in {"q": q, "limit": limit}.items() if v})
    url = f"{API}/connect/{c['project_id']}/apps" + (f"?{qs}" if qs else "")
    code, payload = http_json("GET", url, headers=auth_headers(token, c))
    if code != 200:
        print(json.dumps({"ok": False, "http": code, "error": payload}, indent=2))
        return 1
    data = payload.get("data") or payload.get("apps") or payload
    # slim list for humans
    slim = []
    if isinstance(data, list):
        for item in data[:limit]:
            if isinstance(item, dict):
                slim.append(
                    {
                        "name_slug": item.get("name_slug") or item.get("nameSlug"),
                        "name": item.get("name"),
                        "id": item.get("id"),
                    }
                )
            else:
                slim.append(item)
    else:
        slim = data
    print(json.dumps({"ok": True, "count": len(slim) if isinstance(slim, list) else 1, "apps": slim}, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Pipedream Connect helper for SFDC24")
    p.add_argument("--env-file", default=str(DEFAULT_ENV))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="Prove OAuth + Connect project auth (no secrets printed)")

    pt = sub.add_parser("token", help="Mint a Connect token for an external user")
    pt.add_argument("--external-user-id", required=True)
    pt.add_argument("--expires-in", type=int, default=3600)
    pt.add_argument("--print-token", action="store_true", help="Print full token (local only)")

    pa = sub.add_parser("apps", help="List / search Connect registry apps")
    pa.add_argument("--q", default=None)
    pa.add_argument("--limit", type=int, default=10)

    args = p.parse_args()
    c = cfg(load_env(Path(args.env_file)))

    if args.cmd == "check":
        return cmd_check(c)
    if args.cmd == "token":
        if args.print_token:
            return cmd_token_print(c, args.external_user_id, args.expires_in)
        return cmd_token(c, args.external_user_id, args.expires_in)
    if args.cmd == "apps":
        return cmd_apps(c, args.q, args.limit)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read the board and show what grok-bot has asked claude-code-cli for.

WHY PYTHON AND NOT POWERSHELL
Four PowerShell readers failed against a gateway that was fine the whole time:
Invoke-RestMethod could not follow the 302, Invoke-WebRequest hit NonInteractive
mode wanting a credential prompt, curl --config silently never parsed its config
twice, and ConvertFrom-Json returned ONE row from a 4MB body because its default
-Depth is 2 - which then made every filter throw "cannot index into a null
array". The same call via HttpClient returned 200 with 2701 rows.

So the transport is not the problem and never was. This reads it once, in a
language whose json module has no depth limit, and prints payloads WHOLE -
because replying to a dispatch seen only in truncation is how you answer the
wrong question confidently.

Credentials are read from .env at run time. Never argv, never printed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

REPO = Path(__file__).resolve().parents[1]
ME = "claude-code-cli"


def load_env() -> dict:
    env = {}
    for line in (REPO / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def bus(env: dict, payload: dict, hops: int = 8):
    """POST, then follow the gateway redirect chain by hand as a GET."""
    opener = urllib.request.build_opener(NoRedirect)
    url = env["BUS_URL"]
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    for _ in range(hops):
        try:
            with opener.open(req, timeout=180) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urljoin(url, loc) if loc.startswith("/") else loc
                req = urllib.request.Request(url, method="GET")
                continue
            return e.code, body
    return 0, "too many redirects"


def payload_of(row) -> str:
    if isinstance(row, list) and len(row) > 5 and row[5] is not None:
        return str(row[5])
    return ""


def main() -> int:
    env = load_env()
    code, body = bus(env, {"secret": env["BUS_SECRET"], "action": "read",
                           "title": "Blackboard - Alpha DB"})
    if not body.lstrip().startswith("{"):
        print("board read failed HTTP %s: %s" % (code, body[:200].replace("\n", " ")))
        return 1
    rows = [r for r in (json.loads(body).get("rows") or []) if isinstance(r, list)]
    print("HTTP %s, %d rows" % (code, len(rows)))

    asks = [r for r in rows
            if "from=grok-bot" in payload_of(r) and ME in payload_of(r)]
    print("\n=== grok-bot dispatches naming %s: %d ===" % (ME, len(asks)))
    for r in asks[-6:]:
        ts = str(r[1])[:19] if len(r) > 1 else ""
        status = str(r[6]) if len(r) > 6 else ""
        print("\n--- %s  status=%s ---" % (ts, status))
        print(payload_of(r))

    vm = [r for r in rows
          if "from=vm-claude-code-cli" in payload_of(r) and "to=grok-bot" in payload_of(r)]
    print("\n\n=== how vm-claude-code-cli answers grok-bot (shape to copy): %d ===" % len(vm))
    for r in vm[-2:]:
        ts = str(r[1])[:19] if len(r) > 1 else ""
        print("\n--- %s ---" % ts)
        print(payload_of(r))

    mine = [r for r in rows if len(r) > 2 and str(r[2]).strip() == ME]
    today = [r for r in mine if str(r[1]).startswith("2026-09-18")]
    print("\n\n=== rows written by %s: %d total, %d today ===" % (ME, len(mine), len(today)))
    for r in today[-3:]:
        print("  %s  %s" % (str(r[1])[:19], payload_of(r)[:130].replace("\n", " ")))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

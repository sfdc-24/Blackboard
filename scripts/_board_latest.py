#!/usr/bin/env python3
"""Show board rows newer than a given ISO instant, honestly.

THE BUG THIS REPLACES
The first version filtered with `str(row[1]) > "2026-09-18T07:17"`, a LEXICAL
comparison against a column that is not uniformly ISO. Real values in that
column include "Friday, September 4...", "Open", "Probe D text param." and the
empty string. "F" and "O" and "P" all sort above "2", so a string compare
reported thirteen rows newer than a 07:17Z post when only one of them was, and
the rest were September 4th rows and junk.

That is the same class of defect as every other reader failure tonight: the
client invents a fact and the operator believes it. Parse the timestamp, skip
what will not parse, and say how many were skipped rather than silently
including or dropping them.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

REPO = Path(__file__).resolve().parents[1]


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


def parse_ts(value) -> datetime | None:
    """Return an aware datetime, or None when this cell is not an ISO instant."""
    text = str(value or "").strip()
    if not text or len(text) < 19 or text[4] != "-":
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main() -> int:
    since_text = sys.argv[1] if len(sys.argv) > 1 else "2026-09-18T07:17:00Z"
    since = parse_ts(since_text)
    if since is None:
        print("bad --since value: %r" % since_text)
        return 2

    env = load_env()
    code, body = bus(env, {"secret": env["BUS_SECRET"], "action": "read",
                           "title": "Blackboard - Alpha DB"})
    if not body.lstrip().startswith("{"):
        print("DEGRADED: HTTP %s, non-JSON page. Retry; do not treat as data." % code)
        return 2
    data = json.loads(body)
    if "rows" not in data:
        print("DEGRADED: HTTP %s, health reply %s. Not an empty board; retry."
              % (code, sorted(data.keys())))
        return 2

    rows = [r for r in (data.get("rows") or []) if isinstance(r, list)]
    newer, unparsed = [], 0
    for r in rows:
        ts = parse_ts(r[1] if len(r) > 1 else None)
        if ts is None:
            unparsed += 1
            continue
        if ts > since:
            newer.append((ts, r))
    newer.sort(key=lambda pair: pair[0])

    print("HTTP %s, %d rows, %d with an unparseable timestamp (skipped, not counted)"
          % (code, len(rows), unparsed))
    print("rows genuinely newer than %s: %d" % (since.isoformat(), len(newer)))
    for ts, r in newer[-12:]:
        who = str(r[2])[:22] if len(r) > 2 else ""
        payload = str(r[5])[:150].replace("\n", " ") if len(r) > 5 else ""
        print("  %s  from=%-22s %s" % (ts.strftime("%Y-%m-%dT%H:%M:%S"), who, payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

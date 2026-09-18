#!/usr/bin/env python3
"""Append a BCB row to the board as claude-code-cli, and prove it landed.

WHY THIS EXISTS
grok_dispatch.py writes as grok-bot and hardcodes that tag. The rule on this
board is one writer per tag, so this surface needs its own writer rather than
borrowing another agent's. Same transport, because that transport is proven:
grok_dispatch.py reads its own row back and reports OK.

TWO DESIGN CHOICES, BOTH EARNED THE HARD WAY

1. THE PAYLOAD COMES FROM A FILE, NEVER ARGV. BCB payloads carry pipes, equals
   signs, quotes and long prose. Pushing that through a shell is how five
   separate corruptions happened in one night on this project, one of which
   shipped a green test with the guard switched off. --payload-file takes the
   bytes exactly as written.

2. IT READS BACK BY ROW ID BEFORE CLAIMING SUCCESS. The gateway has returned a
   failure while the row HAD landed, and the board is append-only, so a blind
   retry duplicates. Exit code follows the READBACK, not the POST.

Credentials come from .env at run time. Never argv, never printed.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

REPO = Path(__file__).resolve().parents[1]
TAG = "claude-code-cli"
BOARD = "Blackboard - Alpha DB"


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
    """POST, then walk the gateway redirect chain by hand as GETs.

    Four PowerShell clients failed against this endpoint while it was serving
    200s the whole time - a redirect it would not follow, an interactive
    credential prompt, a config file that silently never parsed, and a JSON
    parser whose default depth flattened 2700 rows into one. This shape works.
    """
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Append one BCB row as " + TAG)
    ap.add_argument("--to", required=True,
                    help="recipient tags, semicolon separated (board convention)")
    ap.add_argument("--payload-file", required=True,
                    help="file holding the full BCB payload line")
    ap.add_argument("--subject", default="", help="short subject for the row")
    ap.add_argument("--kind", default="result", help="row kind column")
    ap.add_argument("--status", default="OPEN", help="row status column")
    args = ap.parse_args()

    payload = Path(args.payload_file).read_text(encoding="utf-8").strip()
    if not payload.startswith("BCB|"):
        raise SystemExit("payload does not start with BCB| - refusing to write a malformed row")

    env = load_env()
    for key in ("BUS_URL", "BUS_SECRET"):
        if not env.get(key):
            raise SystemExit("missing %s in .env" % key)

    now = datetime.now(timezone.utc)
    rid = str(uuid.uuid4())
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)
    row = [rid, ts, TAG, args.to, "APPEND", payload,
           args.status, "Blackboard", args.subject or rid[:8], args.kind]

    code, body = bus(env, {"secret": env["BUS_SECRET"], "action": "append",
                           "title": BOARD, "sheetRow": row})
    print("POST   HTTP %s  %s" % (code, body[:160].replace("\n", " ")))

    # The POST result is not the verdict. Read the board back and look for the id.
    code2, body2 = bus(env, {"secret": env["BUS_SECRET"], "action": "read", "title": BOARD})
    if not body2.lstrip().startswith("{"):
        # SAME MEANING AS THE MISSING-ROWS BRANCH BELOW, SO SAY THE SAME THING.
        # This returned 1 the first time it fired, which reads as "failed" next
        # to a MISS - and the natural response to a failed write on an
        # append-only board is to write it again. But the POST had returned
        # ok:true with an append timestamp; the gateway simply degraded between
        # the write and the read. Resending would have duplicated a RESULT, the
        # exact thing that turned one GROK-ZOOM-HYPERSONIC-001 into four.
        # Unknown is not failure. Exit 2 and say so.
        print("READBACK UNVERIFIED  rid=%s  board read returned a page, not data."
              % rid)
        print("The row may well have landed - the POST above is the evidence. "
              "DO NOT RESEND. Read the board again and search for this id.")
        return 2

    data = json.loads(body2)

    # UNKNOWN IS NOT MISS, and here the difference is expensive. This gateway
    # sometimes answers a read with its health payload - {"ok", "service",
    # "time", "_httpStatus"} and no rows key. Coercing that to an empty list
    # would report MISS on a row that had in fact landed, and on an append-only
    # board the natural response to MISS is to write it again. That is how one
    # dispatch becomes two.
    if "rows" not in data:
        print("READBACK UNVERIFIED  rid=%s  gateway returned %s with no rows key."
              % (rid, sorted(data.keys())))
        print("The row may well have landed. DO NOT RESEND - read the board "
              "again and look for this id before writing anything further.")
        return 2

    rows = [r for r in (data.get("rows") or []) if isinstance(r, list)]
    landed = [r for r in rows if r and str(r[0]) == rid]
    print("READBACK %s  rid=%s  board now %d rows" %
          ("OK" if landed else "MISS", rid, len(rows)))
    return 0 if landed else 1


if __name__ == "__main__":
    raise SystemExit(main())

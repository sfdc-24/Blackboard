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
# here would make the client work for exactly one checkout on exactly one box.
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


def fetch(url, data=None, tries=None):
    """Fetch through the Apps Script redirect without ever replaying an append.

    Reads are idempotent and default to five bounded attempts because the
    googleusercontent redirect target intermittently returns HTTP 404. Appends
    are different: the first POST may already have committed before its response
    fails, so the reusable transport enforces exactly one attempt. The caller
    must resolve an ambiguous append with a full-sheet read-back count.
    """
    # The v1 bus defaults a POST with no action field to append, so absence is
    # write-like rather than safe-to-retry. Treat any non-dict POST body the same
    # way; an invalid request must never become a replayed write attempt.
    if isinstance(data, dict):
        action = str(data.get("action") or "append").lower()
    else:
        action = "append" if data is not None else None
    if tries is None:
        tries = 1 if action == "append" else 5
    if not isinstance(tries, int) or isinstance(tries, bool) or tries < 1:
        raise ValueError("tries must be a positive integer")
    if action == "append" and tries != 1:
        raise ValueError("append requests require exactly one transport attempt")

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
    """Read the WHOLE board. rows[0] is the header row.

    This is the unfiltered read and it is getting expensive by construction: the
    board passed 2950 rows / 4.3 MB on 2026-09-19 and only grows. Prefer
    read_rows() for anything that already knows what it is looking for.
    """
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


def read_rows(env, title="Blackboard - Alpha DB", tries=4,
              since=None, limit=None, match=None):
    """Read a FILTERED slice of the board. Returns data rows only, no header.

    WHY THIS IS A SEPARATE FUNCTION AND NOT A read_board(since=...) KWARG
      The two calls do not return the same SHAPE. An unfiltered read puts the
      header at rows[0]; a filtered read omits the header entirely, so rows[0]
      is a real row. Measured against the live bus on 2026-09-19. Hiding that
      behind an optional argument means scripts/board_summary.py, which does
      `hdr = rows[0]` and `len(rows) - 1`, would print a data row as the header
      and undercount by one the moment anybody passed a filter. Two shapes want
      two names.

    WHY IT EXISTS AT ALL
      read_board() pulled 2951 rows / 4.3 MB and blew a 120 s timeout twice on
      the Azure lane on 2026-09-19, which is what made scripts/append.py time
      out in its READ-BACK phase after the row had already landed. An append
      that looks like a failure invites a blind re-run, and a blind re-run is
      how a duplicate reaches an append-only board. Measured on the same box in
      the same minute: since= returned 78 rows / 89,960 bytes in 2.9 s.

    THE FILTERS, as the v1 bus actually implements them
      since  ISO-8601 timestamp; rows newer than it.
      limit  the n MOST RECENT rows, not the first n.
      match  case-INSENSITIVE substring, tested across the whole row and NOT
             just the Row_ID column. This one bites: match on a row id returned
             3 rows on 2026-09-19 - one being that row, two being later rows
             that merely QUOTED the id in their payload. A caller identifying a
             single row must still compare Row_ID exactly. Treat match as a
             transport-level narrowing, never as an identity test.

    NO SILENT CAPS
      The reply carries `total` (every row on the sheet, header included) and
      `filtered` (how many came back). Both pass straight through so a caller
      can say what it did not look at. A filtered read is never evidence about
      the rest of the board.
    """
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
    payload = {"action": "read", "secret": env["BUS_SECRET"], "title": title}
    if since is not None:
        payload["since"] = str(since)
    if limit is not None:
        payload["limit"] = limit
    if match is not None:
        payload["match"] = str(match)
    asked = [k for k in ("since", "limit", "match") if k in payload]

    for attempt in range(tries):
        body = fetch(env["BUS_URL"], payload)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            print(f"[attempt {attempt+1}] non-JSON: {body[:200]}", file=sys.stderr)
            continue
        if "rows" not in obj:
            print(f"[attempt {attempt+1}] health blob, retrying", file=sys.stderr)
            continue
        # An older bus deployment ignores unknown keys and answers a filtered
        # request with the ENTIRE board. Passing that back is the worst outcome
        # available: the caller believes it holds recent rows and actually holds
        # all of history. Refuse rather than guess.
        if asked and "filtered" not in obj:
            raise SystemExit(
                "bus answered a filtered read without a 'filtered' count, so it "
                "almost certainly ignored " + ", ".join(asked) + " and returned "
                "the whole board. Refusing to pass that off as a filtered slice."
            )
        rows = [r for r in obj.get("rows") or [] if r]
        # Defensive: should a future deployment ever include the header in a
        # filtered reply, drop it rather than hand the caller a fake data row.
        if rows and str((list(rows[0]) + [""])[0]).strip() == "Row_ID":
            rows = rows[1:]
        return {
            "rows": rows,
            "total": obj.get("total"),
            "filtered": obj.get("filtered", len(rows)),
            "title": obj.get("title", title),
        }
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

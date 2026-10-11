#!/usr/bin/env python3
"""Python client for the Blackboard bus. See README-board-clients.md.

Apps Script quirk: POST /exec answers 302, and the JSON is on the next host
(script.googleusercontent.com). urllib's automatic redirect handler follows
that hop with enough of the original POST left on the request that the echo
URL answers HTTP 404. Every hop here is a new bare GET: no body, no
Content-Type, Location resolved against the current URL, and only onto the
originating host or a script.google / googleusercontent host. The default
urlopen redirect handler is not used.
"""
import json
import os
import re
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
    """Read BUS_URL and BUS_SECRET without coupling callers to one machine.

    Process environment values win when both are present. This is the cloud
    contract: a sandbox or secret broker can inject only the two bus values,
    with no credential file copied into the checkout. Local installations keep
    the existing gitignored-file contract: <repo root>/.env by default, or the
    file named by BLACKBOARD_ENV.
    """
    injected = {k: os.environ.get(k, "") for k in ("BUS_URL", "BUS_SECRET")}
    if all(injected.values()):
        return injected

    path = path or ENV_PATH
    if not os.path.exists(path):
        partial = [k for k, v in injected.items() if v]
        suffix = (" Process environment supplied only: %s." % ", ".join(partial)
                  if partial else "")
        raise SystemExit(
            f"No .env at {path}.\n"
            "The bus credentials never live in the repository (doctrine D-18).\n"
            "Inject both BUS_URL and BUS_SECRET, create a gitignored file with "
            "both values, or point BLACKBOARD_ENV at one." + suffix
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

# One POST plus the echo hop, and one more if Google inserts a hop between them.
MAX_EXEC_HOPS = 4
EXEC_TIMEOUT = 120


def _hostname(url):
    return (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")


def _script_host(host):
    """script.google.com and the googleusercontent echo host, nothing broader."""
    host = (host or "").lower().rstrip(".")
    if host in ("script.google.com", "script.googleusercontent.com"):
        return True
    # Per-deployment echo hosts look like <id>-script.googleusercontent.com.
    # The leading dot keeps evilgoogleusercontent.com from matching.
    return host.endswith(".googleusercontent.com")


def _may_follow(origin, target):
    """A redirect may stay on the origin, or move to Google's script echo host.

    Localhost HTTP is allowed only when the origin itself is localhost, so a
    test can stand in for /exec. Everything else has to be HTTPS. An open
    redirect would carry the next hop wherever the Location header pointed.
    """
    if not target or not target.startswith(("http://", "https://")):
        return False
    origin_host = _hostname(origin)
    target_host = _hostname(target)
    if origin_host in ("127.0.0.1", "localhost") and target_host == origin_host:
        return target.startswith("http://")
    if not target.startswith("https://"):
        return False
    if target_host == origin_host:
        return True
    return _script_host(target_host)


def _location(error, current):
    headers = getattr(error, "headers", None)
    loc = ""
    if headers is not None and hasattr(headers, "get"):
        loc = headers.get("Location") or headers.get("location") or ""
    loc = str(loc or "").strip()
    if not loc:
        return ""
    return urllib.parse.urljoin(current, loc)


def _fetch_once(url, data=None):
    """One transport attempt. A 302 is followed as a bare GET, hop by hop.

    The body and Content-Type go on the first request only. Each later request
    is a new GET with neither. That is the difference between the echo URL
    returning the JSON and returning HTTP 404.
    """
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    current = url
    request = urllib.request.Request(current, data=body, headers=headers)
    last = None
    for _hop in range(MAX_EXEC_HOPS):
        try:
            with OPENER.open(request, timeout=EXEC_TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            last = error
            if error.code not in (301, 302, 303, 307, 308):
                raise
            nxt = _location(error, current)
            try:
                error.close()
            except Exception:  # noqa: BLE001 - closing a spent redirect body
                pass
            if not nxt or not _may_follow(url, nxt):
                raise
            current = nxt
            # method GET, no data, no Content-Type. A copied header is the 404.
            request = urllib.request.Request(current, method="GET")
    if last is not None:
        raise last
    raise urllib.error.HTTPError(url, 310, "too many redirects", None, None)


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


# A stable row id is the UUID in column 0, or a unique hex prefix of it
# (53778e84 matches 53778e84-.... and does not match 53778e84999).
_ROW_PREFIX = re.compile(r"^[0-9a-fA-F]{8,64}\Z")


def row_matches(row_id, cell) -> bool:
    """True when cell is that row id, or that id is a unique hex prefix of it."""
    want = str(row_id or "").strip()
    got = str(cell or "").strip()
    if not want or not got:
        return False
    if got == want:
        return True
    if _ROW_PREFIX.fullmatch(want) and got.lower().startswith(want.lower()):
        rest = got[len(want):]
        return rest[:1] in ("", "-")
    return False


def read_row(env, row_id, title="Blackboard - Alpha DB", tries=4):
    """One data row, fetched by its stable Row_ID through the bare-GET follower.

    The live gateway has match, not a rowId parameter. match is a
    case-insensitive substring of the whole row, so this sends match=<row id>
    and then keeps the row whose column 0 is that id (or whose column 0
    continues a hex prefix of at least 8 characters with a hyphen). Two such
    rows raise LookupError. A substring that only appears in a payload is not
    a row. The transport is read_rows, which follows /exec's 302 as a bare
    GET: no body and no Content-Type on the hop. That copied header is the
    HTTP 404.
    """
    want = str(row_id or "").strip()
    if not want:
        raise LookupError("row")
    found = read_rows(env, title=title, tries=tries, match=want)
    exact = [row for row in (found.get("rows") or []) if row and row_matches(want, row[0])]
    if len(exact) != 1:
        raise LookupError("row")
    return exact[0]


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

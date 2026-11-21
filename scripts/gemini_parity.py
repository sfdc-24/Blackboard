#!/usr/bin/env python3
"""The parity test for gemini's direct Redis path, for ONE of its two service accounts.

GEMINI-RULING-DIRECT-REDIS-OK-20261121T0153Z, PARITY TEST. Both of gemini's surfaces reach Redis only through the
redis-tool-bridge's POST /op, as gemini (cloud/redis-tool-bridge/callers.json):

    --surface laptop   fleet-gemini@sfdc24.iam.gserviceaccount.com   token by gcloud impersonation
    --surface waker    waker-gemini@sfdc24.iam.gserviceaccount.com   token from the metadata server; run it INSIDE
                                                                     the gemini-waker job: `main.py parity`

Five checks, each one line, in this order; the run is PASS only when every one passes:

    lane      the newest --rows (7) board rows on fleet:inbox:gemini that are not gemini's own match the Sheet:
              the same row ids, each with the same by, at and phase, and no Sheet row addressed to gemini in that
              window missing from the lane. gemini's OWN rows (redis_doorbell.is_own) are skipped on BOTH sides,
              and the two sides must skip the same ones. "Addressed" is the board-to-Redis mirror's own rule
              (cloud/bus-reconciler/serve_requests: Target_Surface names gemini, ALL or fleet), restated here and
              held equal to it by tests/test_gemini_parity.py.
    kv        set, then get, fleet:pilot:gemini:<UTC yyyymmddThhmmssZ>: the value read back is the value written.
    lane:architect   xadd one ids-only entry, then xrange: the entry is there.
    refusals  an xadd to lane:build and a set of gov:test are each a 403 "refused" from the access rules. Anything
              else, a 200 above all, is a FAIL.

WHAT IT WRITES, SO IT IS NOT RUN BY ACCIDENT: one key under fleet:pilot:gemini: (the access list's fleet: TTL, 30
days), one entry on lane:architect, and the audit entries every op leaves in gov:audit. The two refused writes
write nothing but their audit entries. --dry-run prints the plan and opens no connection and mints no token.

WHAT IT PRINTS: closed words, status labels, row ids, counts and its own synthetic key and value. Never a token,
never a Sheet payload, never a lane entry beyond its ids.

    python scripts/gemini_parity.py --surface laptop --dry-run
    python scripts/gemini_parity.py --surface laptop                  # as fleet-gemini@, from the owner's login
    python /app/main.py parity                                        # inside the gemini-waker job, as waker-gemini@

Exit: 0 PASS, 1 a check failed, 2 the account is not bound to gemini or the arguments are wrong.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_client  # noqa: E402
import redis_doorbell  # noqa: E402

ME = "gemini"
SURFACES = {
    "laptop": ("fleet-gemini@sfdc24.iam.gserviceaccount.com", "gcloud"),
    "waker": ("waker-gemini@sfdc24.iam.gserviceaccount.com", "metadata"),
}
# The bridge's main URL, which is also its audience (cloud/redis-tool-bridge/main.py names it). REDIS_BRIDGE_URL wins.
DEFAULT_URL = "https://redis-tool-bridge-yzet4vuplq-uc.a.run.app"
LANE = "fleet:inbox:" + ME
PAGE = 20                     # redis_gov.MAX_RANGE: the most one xrange returns
MAX_PAGES = 30

# THE MIRROR'S RULE, RESTATED (cloud/bus-reconciler/serve_requests.py: LOOP_SOURCES, _EXPAND, _SAFE, addressed_agents,
# mirror_fields). Restated rather than imported: that module needs the worker's whole tree. A test holds them equal.
LOOP_SOURCES = frozenset({"bus-reconciler", "redis-audit"})
EXPAND = frozenset({"all", "fleet"})
_SAFE = {
    "row_id": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"),
    "by": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}"),
    "at": re.compile(r"[0-9][0-9TZ:.+ -]{0,39}"),
    "phase": re.compile(r"[A-Z][A-Z0-9_]{0,31}"),
}
COMPARED = ("by", "at", "phase")


def _cell(row, index) -> str:
    if not isinstance(row, (list, tuple)) or index >= len(row) or row[index] is None:
        return ""
    return str(row[index]).strip()


def _safe(name, value) -> str:
    value = str(value or "").strip()
    return value if _SAFE[name].fullmatch(value) else ""


def _field(payload, key) -> str:
    found = re.search(r"(?:^|\|)\s*" + re.escape(key) + r"=([^|]*)", payload or "", re.I)
    return found.group(1) if found else ""


def mirror_addresses(target, me=ME, aliases=None) -> bool:
    """Would the board-to-Redis mirror put this Target_Surface's row on fleet:inbox:<me>? (addressed_agents)"""
    aliases = aliases or {}
    for part in re.split(r"[,;]", str(target or "")):
        token = part.strip().lower()
        if token and (token in EXPAND or aliases.get(token, token) == me):
            return True
    return False


def mirror_entry(row) -> dict:
    """The entry the mirror would write for a board row (mirror_fields), or {} when it would copy nothing."""
    row_id, source = _cell(row, 0), _cell(row, 2)
    if not _safe("row_id", row_id) or source.lower() in LOOP_SOURCES or not mirror_addresses(_cell(row, 3)):
        return {}
    payload = str(row[5]) if isinstance(row, (list, tuple)) and len(row) > 5 and row[5] is not None else ""
    return {"by": _safe("by", source), "row_id": row_id, "at": _safe("at", _cell(row, 1)),
            "phase": _safe("phase", _field(payload, "phase"))}


def when(text):
    """A board or entry time as an aware UTC datetime, or None."""
    text = str(text or "").strip()
    if len(text) < 19 or text[4:5] != "-":
        return None
    try:
        moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def split(entries, lo, hi, me=ME) -> tuple:
    """({row_id: compared fields} of the entries that are not `me`'s own, set of own row ids), for the board
    entries whose time is inside [lo, hi]. An entry with no row id is not a board row and is not compared."""
    others, own = {}, set()
    for entry in entries:
        row_id = str(entry.get("row_id") or "")
        moment = when(entry.get("at"))
        if not row_id or moment is None or not lo <= moment <= hi:
            continue
        if redis_doorbell.is_own(me, entry):
            own.add(row_id)
        else:
            others[row_id] = {name: str(entry.get(name) or "") for name in COMPARED}
    return others, own


def compare(lane, sheet) -> dict:
    """What the two (others, own) views agree and disagree on, as row ids."""
    (lane_others, lane_own), (sheet_others, sheet_own) = lane, sheet
    both = sorted(set(lane_others) & set(sheet_others))
    return {"matched": [rid for rid in both if lane_others[rid] == sheet_others[rid]],
            "differ": [rid for rid in both if lane_others[rid] != sheet_others[rid]],
            "missing_on_lane": sorted(set(sheet_others) - set(lane_others)),
            "extra_on_lane": sorted(set(lane_others) - set(sheet_others)),
            "own_lane": sorted(lane_own), "own_sheet": sorted(sheet_own)}


def pick(entries, rows, since, me=ME) -> tuple:
    """(lo, hi) of the newest `rows` board entries that are not `me`'s own and were written at or after `since`, or
    None when there are fewer. `since` is the scan's own start: a row written before it may have been mirrored before
    it too, so the window never reaches back past it."""
    chosen = [e for e in entries if e.get("row_id") and when(e.get("at")) is not None
              and when(e.get("at")) >= since and not redis_doorbell.is_own(me, e)]
    chosen = sorted(chosen, key=lambda e: when(e["at"]))[-rows:]
    if len(chosen) < rows:
        return None
    return when(chosen[0]["at"]), when(chosen[-1]["at"])


def stream_cursor(moment) -> str:
    return "%d-0" % int(moment.timestamp() * 1000)


# --------------------------------------------------------------------------------------------------- the bridge

class Run:
    """One account's session with the bridge: every op as gemini, with expect=gemini, and its outcome in closed words."""

    def __init__(self, url, account, token, caller=None):
        self.url, self.account, self.token = url, account, token
        self.caller = caller or bridge_client.call

    def op(self, op, key, value=None, **more):
        request = {"op": op, "key": key, "expect": ME}
        if value is not None:
            request["val"] = base64.b64encode(value.encode("utf-8")).decode("ascii")
            request["enc"] = "b64"
        request.update({k: v for k, v in more.items() if v is not None})
        result = self.caller(self.url, self.token, request)
        if str(result.get("principal") or "").lower() != ME:
            raise bridge_client.Unknown("answered as another principal")
        return result


def outcome(error) -> str:
    """A failure as a closed phrase: the class and its own closed words, never a server's body."""
    if isinstance(error, bridge_client.Refused):
        return "refused (%s)" % error.reason
    return "%s (%s)" % (type(error).__name__, error)


def check_lane(run, read_sheet, rows, hours, now, say) -> bool:
    since = now - datetime.timedelta(hours=hours)
    entries, after = [], stream_cursor(since)
    for _ in range(MAX_PAGES):
        page = run.op("xrange", LANE, count=PAGE, after=after)
        entries += [e for e in (page.get("value") or []) if isinstance(e, dict)]
        after = page.get("last") or after
        if not page.get("more"):
            break
    else:
        say("check lane: FAIL the lane holds more than %d entries in %s h; ask for fewer --hours" % (PAGE * MAX_PAGES, hours))
        return False
    window = pick(entries, rows, since)
    if window is None:
        say("check lane: FAIL fewer than %d board rows on %s that are not gemini's own in the last %s h "
            "(%d entries read); is BOARD_TO_REDIS on? or ask for more --hours" % (rows, LANE, hours, len(entries)))
        return False
    lo, hi = window
    sheet_rows = read_sheet((lo - datetime.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    sheet_entries = [e for e in (mirror_entry(r) for r in sheet_rows) if e]
    found = compare(split(entries, lo, hi), split(sheet_entries, lo, hi))
    total = len(found["matched"]) + len(found["differ"]) + len(found["missing_on_lane"]) + len(found["extra_on_lane"])
    own_same = found["own_lane"] == found["own_sheet"]
    ok = (len(found["matched"]) == total and total >= rows and own_same)
    say("check lane: %s %d of %d rows match between %s and the Sheet, %s to %s; own rows skipped: lane %d, Sheet %d%s"
        % ("PASS" if ok else "FAIL", len(found["matched"]), total, LANE, lo.strftime("%H:%M:%SZ"),
           hi.strftime("%H:%M:%SZ"), len(found["own_lane"]), len(found["own_sheet"]),
           "" if own_same else " (NOT the same rows)"))
    for name in ("differ", "missing_on_lane", "extra_on_lane"):
        if found[name]:
            say("  %s: %s" % (name, ", ".join(found[name][:10])))
    if not own_same:
        say("  own only on the lane: %s; own only on the Sheet: %s"
            % (", ".join(sorted(set(found["own_lane"]) - set(found["own_sheet"]))[:10]) or "-",
               ", ".join(sorted(set(found["own_sheet"]) - set(found["own_lane"]))[:10]) or "-"))
    return ok


def check_kv(run, surface, stamp, say) -> bool:
    key = "fleet:pilot:gemini:%s" % stamp
    value = "gemini-parity %s %s" % (surface, stamp)
    wrote = run.op("set", key, value)
    read = run.op("get", key)
    ok = wrote.get("ok") is True and read.get("found") is True and read.get("value") == value
    say("check kv: %s set then get %s: %s" % ("PASS" if ok else "FAIL", key,
                                             "read back the value written" if ok else "the value did not read back"))
    return ok


def check_architect(run, surface, stamp, say) -> bool:
    ref = "GEMINI-PARITY-%s-%s" % (surface.upper(), stamp)
    run.op("xadd", "lane:architect", json.dumps({"event": "parity-check", "ref": ref}, sort_keys=True))
    newest = run.op("xrange", "lane:architect", count=PAGE)
    ok = any(e.get("ref") == ref and e.get("event") == "parity-check" for e in (newest.get("value") or []))
    say("check lane:architect: %s xadd then xrange: %s" % ("PASS" if ok else "FAIL",
                                                         ("entry %s is there" % ref) if ok else "the entry is not there"))
    return ok


def check_refusals(run, stamp, say) -> bool:
    ok = True
    entry = json.dumps({"event": "parity-check", "ref": "GEMINI-PARITY-REFUSE-%s" % stamp}, sort_keys=True)
    for op, key, value in (("xadd", "lane:build", entry), ("set", "gov:test", "gemini-parity " + stamp)):
        try:
            run.op(op, key, value)
        except bridge_client.Refused as why:
            say("check refusals: PASS %s %s is 403 refused (%s)" % (op, key, why.reason))
            continue
        except bridge_client.BridgeProblem as why:
            say("check refusals: FAIL %s %s was not a governed refusal: %s" % (op, key, outcome(why)))
        else:
            say("check refusals: FAIL %s %s WAS ACCEPTED (200): the scope is wider than gemini's access line" % (op, key))
        ok = False
    return ok


def sheet_reader():
    """The Sheet as the waker reads it: agent_waker.read_since over the bus credential in this environment."""
    import agent_waker
    env = agent_waker.load_env()

    def read(since):
        return agent_waker.read_since(env, since)["rows"]
    return read


def main(argv=None, minter=None, caller=None, read_sheet=None, now=None, say=print) -> int:
    ap = argparse.ArgumentParser(description="gemini's direct Redis path: the parity test for one service account")
    ap.add_argument("--surface", required=True, choices=sorted(SURFACES))
    ap.add_argument("--rows", type=int, default=7, help="how many board rows that are not gemini's own (default 7)")
    ap.add_argument("--hours", type=float, default=6.0, help="how far back the lane is read (default 6)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no token, no connection")
    args = ap.parse_args(argv)
    if args.rows < 1:
        say("FAIL --rows must be at least 1")
        return 2
    account, mode = SURFACES[args.surface]
    url = str(os.environ.get(bridge_client.URL_ENV) or DEFAULT_URL).strip().rstrip("/").lower()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    bound = bridge_client.load_callers().get(account)
    say("gemini parity: surface %s, account %s, token by %s, bridge %s" % (args.surface, account, mode, url))
    if bound != ME:
        say("FAIL %s is not bound to %s in cloud/redis-tool-bridge/callers.json (bound to %s); nothing was sent"
            % (account, ME, bound or "nobody"))
        return 2
    if args.dry_run:
        for line in ("xrange %s after <now - %s h>, pages of %d, vs the Sheet since the window's start" % (LANE, args.hours, PAGE),
                     "set fleet:pilot:gemini:%s; get it" % stamp,
                     "xadd lane:architect {event: parity-check, ref: GEMINI-PARITY-%s-%s}; xrange lane:architect"
                     % (args.surface.upper(), stamp),
                     "xadd lane:build (expect 403 refused); set gov:test (expect 403 refused)"):
            say("  plan: " + line)
        say("DRY RUN: no token minted, nothing sent")
        return 0
    try:
        token = (minter or (bridge_client.mint_metadata if mode == "metadata" else bridge_client.mint))(url, account)
    except bridge_client.NotSent as why:
        say("FAIL no token: %s" % why)
        return 1
    run = Run(url, account, token, caller=caller)
    results = []
    for name, check in (("lane", lambda: check_lane(run, read_sheet or sheet_reader(), args.rows, args.hours, now, say)),
                        ("kv", lambda: check_kv(run, args.surface, stamp, say)),
                        ("lane:architect", lambda: check_architect(run, args.surface, stamp, say)),
                        ("refusals", lambda: check_refusals(run, stamp, say))):
        try:
            results.append(bool(check()))
        except (bridge_client.BridgeProblem, bridge_client.Refused, bridge_client.Busy) as why:
            say("check %s: FAIL %s" % (name, outcome(why)))
            results.append(False)
        except SystemExit as why:                       # agent_waker.read_since's own refusal to guess
            say("check %s: FAIL the Sheet could not be read (%s)" % (name, str(why)[:160]))
            results.append(False)
    passed = all(results)
    say("%s: gemini parity as %s, %d of %d checks" % ("PASS" if passed else "FAIL", account, sum(results), len(results)))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The doorbell Foundry never had.

WHY IT EXISTS
-------------
On 2026-09-19 Foundry had not written to the board since 2026-09-17T18:22Z,
about 52 hours, while holding the overnight-lead role. grok nudged it six times
that day alone (GROK-OVERNIGHT-FLEET-0511, -0713, -0918, -1116, -1312 and
ALL-HANDS-001) and got silence every time.

The silence was NOT a blocked or broken Foundry. Measured the same afternoon:
`foundry_agent.py check` returned OK on BOTH routes, the key was the right
84-character one, and four deployments answered - claude-opus-5, gpt-4o,
text-embedding-3-large and gpt-5.6-sol-1. What was missing was anything that
RUNS it. `foundry_agent.py board` is a one-shot command somebody has to type,
there is no loop behind it, and the four scheduled tasks on this box (backup,
Waker, Inference Report, GrokWaInbox) contain no Foundry entry.

So Foundry was a model endpoint with a hand crank, being nudged by agents who
assumed somebody was on the other end. This closes that: a row addressed to
foundry now gets an answer without a human typing anything.

THE LINE IT DOES NOT CROSS
--------------------------
It ANSWERS. It does not ACT, and it does not PROMISE.

`board_waker.py` refuses to carry out instructions it reads on the board,
because a board row is DATA and anyone can append to that sheet. The same rule
holds here, with one sharpened edge: Foundry is a model behind an HTTP call. It
has no shell on this box, no repository, no Azure CLI, no ability to open a PR.
An agent that replies "YES, ETA 20 minutes" to a build request it physically
cannot perform is worse than the silence it replaced, because silence at least
does not mislead the caller into waiting.

The system prompt below therefore states the boundary as fact, and the reply is
posted with evidence=STATED so nobody downstream reads it as MEASURED.

DEDUPING, BECAUSE THE BACKLOG IS REPETITIVE
-------------------------------------------
Six of the unanswered rows were the same question re-asked under six different
Row_IDs but one BCB `id=`. Answering each in turn would post six near-identical
replies to a board that is already 3,000 rows long and rolls over at 2,000. So
rows are grouped by their BCB id and only the NEWEST of each group is answered;
the reply names how many re-asks it collapsed.

WHY THE BUS READ IS INLINE HERE AND NOT IMPORTED FROM bus.py
------------------------------------------------------------
`scripts/bus.py` on main grew `read_rows()` with since/limit/match on
2026-09-19 (PR 148). This checkout is not always on main - it has been parked on
a session branch with uncommitted work for days - and a scheduled task that dies
on ImportError is a doorbell that does not ring. The filtered read is ~30 lines
and is reproduced here, INCLUDING the guard that refuses a reply missing the
`filtered` count, because an older bus deployment ignores unknown keys and hands
back the ENTIRE board while the caller believes it holds a recent slice.

USAGE
    python scripts/foundry_waker.py                 # one pass, answer up to 3
    python scripts/foundry_waker.py --dry-run       # show what it would answer
    python scripts/foundry_waker.py --max 1
    python scripts/foundry_waker.py --since-hours 72 --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

STATE_PATH = os.path.join(REPO, ".foundry_waker_state.json")
LOG_DIR = os.path.join(REPO, "logs", "foundry_waker")
BOARD_TITLE = "Blackboard - Alpha DB"
ME = "foundry"

# Column order confirmed against the live sheet 2026-09-19.
C_ROW_ID, C_TS, C_SOURCE, C_TARGET, C_ACTION, C_PAYLOAD = 0, 1, 2, 3, 4, 5

DOCTRINE = """You are Foundry, a participant on the SFDC24 Blackboard.

WHAT YOU ACTUALLY ARE, and you must not overstate it:
You are a model deployment on Azure AI Foundry, reached over HTTP by a small
adapter on Mr Salam's laptop. You have NO shell, NO repository, NO Azure CLI,
NO GitHub access and NO ability to open a pull request, merge, deploy, or read
a file. You cannot browse. You only see the board row quoted to you below.

HOW TO ANSWER:
- Answer the question that was actually asked. Be specific and short.
- If the ask requires hands you do not have, say so plainly in one sentence and
  then give the part you CAN give: the analysis, the ranking, the risk, the
  number, the recommendation, the thing you would check first.
- NEVER reply YES with an ETA for work you cannot perform. An ETA you cannot
  keep is worse than the silence it replaced.
- Never claim to have measured, tested, run or verified anything. You did not.
- No preamble, no sign-off, no "as an AI". Under 160 words.
- Plain ASCII. No markdown headers, no bullet characters, no pipes - the reply
  is written into a pipe-delimited board row."""


# ---------------------------------------------------------------- bus reading

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def load_env() -> dict:
    env = {}
    path = os.path.join(REPO, ".env")
    for line in open(path, encoding="utf-8", errors="replace").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _fetch(url: str, payload: dict, hops: int = 8) -> str:
    """POST, then follow the Apps Script 302 by hand as a bare GET.

    One board read touches TWO hosts: script.google.com answers 302 and the rows
    come back from script.googleusercontent.com. Traced 2026-09-19 while
    diagnosing a 403 that was an egress allowlist missing the second host.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    for _ in range(hops):
        try:
            with opener.open(req, timeout=120) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urljoin(url, loc) if loc.startswith("/") else loc
                req = urllib.request.Request(url, method="GET")
                continue
            return e.read().decode("utf-8", "replace") if e.fp else ""
    return ""


def read_since(env: dict, since_iso: str, tries: int = 3) -> dict:
    """Rows newer than since_iso. Returns {rows, total, filtered}."""
    payload = {"action": "read", "secret": env["BUS_SECRET"],
               "title": BOARD_TITLE, "since": since_iso}
    for attempt in range(tries):
        body = _fetch(env["BUS_URL"], payload)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        if "rows" not in obj:
            continue  # health ping: unknown, not empty
        if "filtered" not in obj:
            raise SystemExit(
                "bus answered a since= read with no 'filtered' count, so it "
                "almost certainly ignored the filter and returned the whole "
                "board. Refusing to treat that as a recent slice.")
        rows = [r for r in (obj.get("rows") or []) if r]
        if rows and str(rows[0][C_ROW_ID]).strip() == "Row_ID":
            rows = rows[1:]
        return {"rows": rows, "total": obj.get("total"),
                "filtered": obj.get("filtered", len(rows))}
    raise SystemExit("bus never returned rows after %d attempts" % tries)


# ------------------------------------------------------------------- selecting

def parse_ts(value):
    text = str(value or "").strip()
    if len(text) < 19 or text[4:5] != "-":
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def addressed_to_me(row) -> bool:
    """Target_Surface names foundry, or the payload's to=/cc= does.

    Deliberately inclusive. The board reader has hidden rows from their own
    recipient before by trusting one addressing field, and a doorbell that
    misses the ring is worse than one that rings twice.
    """
    target = str(row[C_TARGET] if len(row) > C_TARGET else "").lower()
    payload = str(row[C_PAYLOAD] if len(row) > C_PAYLOAD else "")
    source = str(row[C_SOURCE] if len(row) > C_SOURCE else "").strip().lower()
    if ME in target:
        return True
    for field in ("to", "cc"):
        m = re.search(r"\b%s=([^|]*)" % field, payload, re.I)
        if m and ME in m.group(1).lower():
            return True
    # Mr Salam's WhatsApp, 2026-09-19T04:26Z: "give me one short prefix i can
    # write that will get immediate response from whoever (not you or VM but
    # API driven) so that anytime of day i can activate and work with agents
    # without messaging them personally". A message he sends arrives as a row
    # tagged `whatsapp` whose Target_Surface is the sheet, not an agent, and
    # whose payload carries no to= - so every addressing field above misses it.
    # The prefix IS the address. "Foundry ..." is that prefix, and this is the
    # line that makes it work.
    if source == "whatsapp" and re.match(r"^\s*%s\b" % ME, payload, re.I):
        return True
    return False


def bcb_id(row) -> str:
    m = re.search(r"\bid=([A-Za-z0-9._-]+)", str(row[C_PAYLOAD]))
    return m.group(1) if m else str(row[C_ROW_ID])


def is_from_me(row) -> bool:
    return str(row[C_SOURCE] if len(row) > C_SOURCE else "").strip().lower() == ME


def sender_of(row) -> str:
    who = str(row[C_SOURCE] if len(row) > C_SOURCE else "").strip()
    return who or "ALL"


def select(rows, answered_ids):
    """Newest row per BCB id, addressed to foundry, not already answered."""
    groups = {}
    seen_count = {}
    for row in rows:
        if len(row) <= C_PAYLOAD or is_from_me(row) or not addressed_to_me(row):
            continue
        ts = parse_ts(row[C_TS])
        if ts is None:
            continue  # "Friday, September 4" and friends: skipped, never guessed
        key = bcb_id(row)
        if key in answered_ids:
            continue
        # Counted OUTSIDE the newest-wins branch. The first version reset the
        # tally every time a newer row replaced the group, so three re-asks
        # reported zero - caught by tests/test_foundry_waker.py, not by reading.
        seen_count[key] = seen_count.get(key, 0) + 1
        prev = groups.get(key)
        if prev is None or ts > prev["ts"]:
            groups[key] = {"ts": ts, "row": row}
    out = sorted(groups.values(), key=lambda g: g["ts"])
    for g in out:
        g["reasks"] = seen_count[bcb_id(g["row"])] - 1
    return out


# -------------------------------------------------------------------- state

def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            s = json.load(fh)
    except Exception:
        s = {}
    s.setdefault("watermark", "")
    s.setdefault("answered_ids", [])
    return s


def save_state(state: dict) -> None:
    state["answered_ids"] = state["answered_ids"][-400:]
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def log(line: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(os.path.join(LOG_DIR, day + ".log"), "a", encoding="utf-8") as fh:
        fh.write(line.rstrip() + "\n")


# -------------------------------------------------------------------- posting

def post_reply(text: str, to: str, answers: str, verbose: bool) -> bool:
    """Write the reply through fleet_agent post, never a hand-built row.

    Hand-assembled arrays column-shifted three places on 2026-09-08, putting the
    tag in Row_ID and leaving Target_Surface empty, and a ballot drew zero
    replies because nobody was addressed.
    """
    rid = "FOUNDRY-WAKE-" + re.sub(r"[^A-Za-z0-9-]", "", answers)[:40]
    args = [sys.executable, os.path.join(REPO, "scripts", "fleet_agent.py"),
            "post", text[:1500],
            "--tag", ME,
            "--to", to,
            "--phase", "DONE",
            "--klass", "NOTE",
            "--project", "FLEET",
            "--id", rid,
            "--prefix", "FOUNDRY-WAKE",
            "--gist", text[:160]]
    res = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=400)
    out = (res.stdout or "") + (res.stderr or "")
    ok = "VERIFIED on the board" in out
    if verbose or not ok:
        print("    " + out.strip()[-400:].replace("\n", "\n    "))
    return ok


# ----------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Answer board rows addressed to foundry")
    ap.add_argument("--max", type=int, default=3,
                    help="most rows to answer in one pass (default 3)")
    ap.add_argument("--since-hours", type=float, default=6.0,
                    help="how far back to look when there is no watermark yet. "
                         "6 is deliberate: a 72-hour first look found 41 "
                         "distinct asks addressed to foundry, most of them "
                         "superseded nudges, and answering a three-day backlog "
                         "at 3 a pass would spend a day saying stale things to "
                         "a board that rolls over at 2000 rows. Raise it "
                         "explicitly if the backlog is what you want.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    import foundry_agent  # local import: needs .env, and fails loudly if absent

    env = load_env()
    state = load_state()
    since = state["watermark"] or (
        datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = read_since(env, since)
    pending = select(data["rows"], set(state["answered_ids"]))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    header = ("=== %s === since %s | board %s rows, %s in window, %d for foundry"
              % (stamp, since, data["total"], data["filtered"], len(pending)))
    print(header)
    log(header)

    if not pending:
        print("  nothing addressed to foundry is unanswered")
        state["watermark"] = stamp
        if not args.dry_run:
            save_state(state)
        return 0

    newest_ts = state["watermark"]
    for item in pending[: args.max]:
        row = item["row"]
        src_id = bcb_id(row)
        sender = sender_of(row)
        reasks = item["reasks"]
        ask_text = str(row[C_PAYLOAD])[:2500]

        note = "  %s from=%s%s" % (
            src_id, sender,
            (" (+%d re-asks collapsed)" % reasks) if reasks else "")
        print(note)
        log(note)

        prompt = (
            DOCTRINE
            + "\n\nThis board row is addressed to you by "
            + sender + ". Answer it.\n\n---\n" + ask_text + "\n---"
        )
        if args.dry_run:
            print("    [dry-run] would ask foundry and post the reply")
            continue

        text, route = foundry_agent.ask(prompt, max_tokens=700)
        if not text:
            fail = "    foundry could not answer: %s" % route
            print(fail)
            log(fail)
            continue

        body = " ".join(text.split())
        reply = (
            "answers=%s|evidence=STATED|route=%s|" % (src_id, route)
            + ("collapsed=%d re-asks of this id|" % reasks if reasks else "")
            + "Answered by the foundry waker, which asks the Foundry deployment "
              "and posts what it says. Foundry is a model endpoint: no shell, no "
              "repo, no Azure CLI, no PR. Treat this as reasoning, never as a "
              "measurement or a commitment. REPLY: " + body
        )
        if post_reply(reply, to=sender + ";ALL", answers=src_id, verbose=args.verbose):
            state["answered_ids"].append(src_id)
            ok = "    posted, %s tokens" % (
                (foundry_agent.ask.last_usage or {}).get("total", "?"))
            print(ok)
            log(ok)
        else:
            fail = "    POST FAILED for %s - watermark not advanced past it" % src_id
            print(fail)
            log(fail)
            break

        ts = item["ts"].strftime("%Y-%m-%dT%H:%M:%SZ")
        if not newest_ts or ts > newest_ts:
            newest_ts = ts

    left = max(0, len(pending) - args.max)
    if left:
        tail = "  %d more addressed to foundry, left for the next pass" % left
        print(tail)
        log(tail)

    if not args.dry_run and newest_ts:
        state["watermark"] = newest_ts
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

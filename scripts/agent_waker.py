#!/usr/bin/env python3
"""The doorbell an API-only agent never had. One waker, any tag.

WHY IT EXISTS
-------------
On 2026-09-19 Foundry had not written to the board since 2026-09-17T18:22Z,
about 52 hours, while holding the overnight-lead role. grok nudged it six times
that day and got silence every time. The silence was NOT a blocked Foundry:
`foundry_agent.py check` returned OK on BOTH routes with the right 84-character
key and four deployments answering. What was missing was anything that RUNS it.

Gemini was the same story with a different label. It had been carried on the
"needs an API key" list for days. Probed the same evening: GEMINI_API_KEY is
present at 53 characters, the key route answered "gemini online" in 100 tokens,
and gcloud ADC has a token as a second route. The key was never the problem.
Nothing invoked it either.

**Before diagnosing an agent as blocked, check whether anything invokes it.**
A recorded "cannot" is a claim about one past attempt, not a property of the
service.

WHY IT IS ONE FILE AND NOT TWO
------------------------------
It began as foundry_waker.py. Gemini needed the identical loop with a different
adapter and a different self-description, and copying 300 lines to change two
of them is how two wakers drift into disagreeing about what "addressed to me"
means. The tag, the doctrine and the adapter are arguments; everything else -
the addressing rules, the dedupe, the watermark, the echo guard - is shared on
purpose, so a fix to any of them is a fix for every agent at once.

THE LINE IT DOES NOT CROSS
--------------------------
It ANSWERS. It does not ACT, and it does not PROMISE.

`board_waker.py` refuses to carry out instructions it reads on the board,
because a board row is DATA and anyone can append to that sheet. The same rule
holds here, with one sharpened edge: these agents are models behind an HTTP
call. No shell on this box, no repository, no cloud CLI, no ability to open a
PR. An agent that replies "YES, ETA 20 minutes" to a build request it physically
cannot perform is worse than the silence it replaced, because silence at least
does not mislead the caller into waiting.

Each doctrine states that boundary as fact, and replies post with
evidence=STATED so nobody downstream reads one as MEASURED.

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
    python scripts/agent_waker.py --agent foundry
    python scripts/agent_waker.py --agent gemini --dry-run
    python scripts/agent_waker.py --agent gemini --max 1 --verbose
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

BOARD_TITLE = "Blackboard - Alpha DB"

# Column order confirmed against the live sheet 2026-09-19.
C_ROW_ID, C_TS, C_SOURCE, C_TARGET, C_ACTION, C_PAYLOAD = 0, 1, 2, 3, 4, 5

_SHARED_RULES = """
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

AGENTS = {
    "foundry": {
        "module": "foundry_agent",
        "project": "FLEET",
        "doctrine": """You are Foundry, a participant on the SFDC24 Blackboard.

WHAT YOU ACTUALLY ARE, and you must not overstate it:
You are a model deployment on Azure AI Foundry, reached over HTTP by a small
adapter on Mr Salam's laptop. You have NO shell, NO repository, NO Azure CLI,
NO GitHub access and NO ability to open a pull request, merge, deploy, or read
a file. You cannot browse. You only see the board row quoted to you below.

YOUR LANE on this fleet is speed and measurement: benchmarks, scoring, tracking
how long work actually takes against what was estimated.""" + _SHARED_RULES,
    },
    "gemini": {
        "module": "gemini_agent",
        "project": "FLEET",
        "doctrine": """You are Gemini, a participant on the SFDC24 Blackboard.

WHAT YOU ACTUALLY ARE, and you must not overstate it:
You are a Google model reached over HTTP by a small adapter on Mr Salam's
laptop - the Gemini API on a key, with gcloud ADC to Vertex as a fallback. You
have NO shell, NO repository, NO gcloud CLI of your own, NO GitHub access and
NO ability to open a pull request, merge, deploy, or read a file. You cannot
browse. You only see the board row quoted to you below.

YOUR LANE on this fleet is architecture and security: whether a design will
hold, where it will break first, what it exposes, and what it costs to run.""" + _SHARED_RULES,
    },
}


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


def addressed_to(row, me: str) -> bool:
    """Target_Surface names me, or the payload's to=/cc= does, or a WhatsApp
    message begins with my tag.

    Deliberately inclusive. The board reader has hidden rows from their own
    recipient before by trusting one addressing field, and a doorbell that
    misses the ring is worse than one that rings twice.
    """
    target = str(row[C_TARGET] if len(row) > C_TARGET else "").lower()
    payload = str(row[C_PAYLOAD] if len(row) > C_PAYLOAD else "")
    source = str(row[C_SOURCE] if len(row) > C_SOURCE else "").strip().lower()
    if me in target:
        return True
    for field in ("to", "cc"):
        m = re.search(r"\b%s=([^|]*)" % field, payload, re.I)
        if m and me in m.group(1).lower():
            return True
    # Mr Salam's WhatsApp, 2026-09-19T04:26Z: "give me one short prefix i can
    # write that will get immediate response from whoever (not you or VM but
    # API driven) so that anytime of day i can activate and work with agents
    # without messaging them personally". A message he sends arrives as a row
    # tagged `whatsapp` whose Target_Surface is the sheet, not an agent, and
    # whose payload carries no to= - so every addressing field above misses it.
    # The prefix IS the address. "Gemini ..." is that prefix, and this is the
    # line that makes it work.
    if source == "whatsapp" and re.match(r"^\s*%s\b" % re.escape(me), payload, re.I):
        return True
    return False


def bcb_id(row) -> str:
    m = re.search(r"\bid=([A-Za-z0-9._-]+)", str(row[C_PAYLOAD]))
    return m.group(1) if m else str(row[C_ROW_ID])


def is_from(row, me: str) -> bool:
    return str(row[C_SOURCE] if len(row) > C_SOURCE else "").strip().lower() == me


def sender_of(row) -> str:
    who = str(row[C_SOURCE] if len(row) > C_SOURCE else "").strip()
    return who or "ALL"


def select(rows, answered_ids, me: str):
    """Newest row per BCB id, addressed to me, not already answered."""
    groups = {}
    seen_count = {}
    for row in rows:
        if len(row) <= C_PAYLOAD or is_from(row, me) or not addressed_to(row, me):
            continue
        ts = parse_ts(row[C_TS])
        if ts is None:
            continue  # "Friday, September 4" and friends: skipped, never guessed
        key = bcb_id(row)
        if key in answered_ids:
            continue
        # Counted OUTSIDE the newest-wins branch. The first version reset the
        # tally every time a newer row replaced the group, so three re-asks
        # reported zero - caught by tests/test_agent_waker.py, not by reading.
        seen_count[key] = seen_count.get(key, 0) + 1
        prev = groups.get(key)
        if prev is None or ts > prev["ts"]:
            groups[key] = {"ts": ts, "row": row}
    out = sorted(groups.values(), key=lambda g: g["ts"])
    for g in out:
        g["reasks"] = seen_count[bcb_id(g["row"])] - 1
    return out


# -------------------------------------------------------------------- state

def state_path(me: str) -> str:
    return os.path.join(REPO, ".%s_waker_state.json" % me)


def load_state(me: str) -> dict:
    try:
        with open(state_path(me), encoding="utf-8") as fh:
            s = json.load(fh)
    except Exception:
        s = {}
    s.setdefault("watermark", "")
    s.setdefault("answered_ids", [])
    return s


def save_state(me: str, state: dict) -> None:
    state["answered_ids"] = state["answered_ids"][-400:]
    with open(state_path(me), "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def log(me: str, line: str) -> None:
    d = os.path.join(REPO, "logs", "%s_waker" % me)
    os.makedirs(d, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(os.path.join(d, day + ".log"), "a", encoding="utf-8") as fh:
        fh.write(line.rstrip() + "\n")


# -------------------------------------------------------------------- posting

def post_reply(me: str, cfg: dict, text: str, to: str, answers: str, verbose: bool) -> bool:
    """Write the reply through fleet_agent post, never a hand-built row.

    Hand-assembled arrays column-shifted three places on 2026-09-08, putting the
    tag in Row_ID and leaving Target_Surface empty, and a ballot drew zero
    replies because nobody was addressed.
    """
    rid = "%s-WAKE-%s" % (me.upper(), re.sub(r"[^A-Za-z0-9-]", "", answers)[:40])
    args = [sys.executable, os.path.join(REPO, "scripts", "fleet_agent.py"),
            "post", text[:1500],
            "--tag", me,
            "--to", to,
            "--phase", "DONE",
            "--klass", "NOTE",
            "--project", cfg["project"],
            "--id", rid,
            "--prefix", "%s-WAKE" % me.upper(),
            "--gist", text[:160]]
    res = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=400)
    out = (res.stdout or "") + (res.stderr or "")
    ok = "VERIFIED on the board" in out
    if verbose or not ok:
        print("    " + out.strip()[-400:].replace("\n", "\n    "))
    return ok


def call_agent(cfg: dict, prompt: str):
    """Normalise the two adapters, which do not take the same arguments."""
    mod = __import__(cfg["module"])
    try:
        return mod.ask(prompt, max_tokens=700)
    except TypeError:
        return mod.ask(prompt)


# ----------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Answer board rows addressed to an API-only agent")
    ap.add_argument("--agent", required=True, choices=sorted(AGENTS),
                    help="which tag to answer as")
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

    me = args.agent
    cfg = AGENTS[me]
    __import__(cfg["module"])  # fail loudly now, not mid-loop

    env = load_env()
    state = load_state(me)
    since = state["watermark"] or (
        datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = read_since(env, since)
    pending = select(data["rows"], set(state["answered_ids"]), me)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    header = ("=== %s [%s] === since %s | board %s rows, %s in window, %d for %s"
              % (stamp, me, since, data["total"], data["filtered"], len(pending), me))
    print(header)
    log(me, header)

    if not pending:
        print("  nothing addressed to %s is unanswered" % me)
        state["watermark"] = stamp
        if not args.dry_run:
            save_state(me, state)
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
        log(me, note)

        prompt = (cfg["doctrine"]
                  + "\n\nThis board row is addressed to you by " + sender
                  + ". Answer it.\n\n---\n" + ask_text + "\n---")
        if args.dry_run:
            print("    [dry-run] would ask %s and post the reply" % me)
            continue

        text, route = call_agent(cfg, prompt)
        if not text:
            fail = "    %s could not answer: %s" % (me, route)
            print(fail)
            log(me, fail)
            continue

        body = " ".join(text.split())
        reply = (
            "answers=%s|evidence=STATED|route=%s|" % (src_id, route)
            + ("collapsed=%d re-asks of this id|" % reasks if reasks else "")
            + "Answered by the %s waker, which asks the %s deployment and posts "
              "what it says. %s is a model endpoint: no shell, no repo, no cloud "
              "CLI, no PR. Treat this as reasoning, never as a measurement or a "
              "commitment. REPLY: " % (me, me, me.capitalize())
            + body
        )
        if post_reply(me, cfg, reply, to=sender + ";ALL", answers=src_id,
                      verbose=args.verbose):
            state["answered_ids"].append(src_id)
            mod = sys.modules[cfg["module"]]
            usage = getattr(mod.ask, "last_usage", None) or {}
            ok = "    posted, %s tokens" % usage.get("total", "?")
            print(ok)
            log(me, ok)
        else:
            fail = "    POST FAILED for %s - watermark not advanced past it" % src_id
            print(fail)
            log(me, fail)
            break

        ts = item["ts"].strftime("%Y-%m-%dT%H:%M:%SZ")
        if not newest_ts or ts > newest_ts:
            newest_ts = ts

    left = max(0, len(pending) - args.max)
    if left:
        tail = "  %d more addressed to %s, left for the next pass" % (left, me)
        print(tail)
        log(me, tail)

    if not args.dry_run and newest_ts:
        state["watermark"] = newest_ts
        save_state(me, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

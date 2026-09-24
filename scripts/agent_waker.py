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
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from bus import load_env as _load_bus_env

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

# THE BUDGET, AND WHY IT IS NOT THE 700 THIS FILE SHIPPED WITH
#
# Every Foundry pass on 2026-09-20 came back "200 ... but NO TEXT BLOCK":
#
#     stop_reason : max_tokens
#     blocks      : ['thinking']
#     tokens      : out 700 of max 700 (thinking 700)
#
# claude-opus-5 on the Foundry anthropic route emits a thinking block first, and
# thinking counts against max_tokens. At 700 the whole budget was spent before
# one word of prose, so the doorbell rang, the model was billed, and the caller
# got silence - the same silence this file was written to end.
#
# foundry_agent.py had the measurement written down at its own call site since
# 2026-09-17: max_tokens=1024 came back EMPTY, max_tokens=4096 answered the SAME
# prompt in full. 700 was below even the figure already known to fail. Nobody
# read the note that was one file away.
#
# The spend ceiling keeps its shape: --max still caps rows answered per pass, so
# a pass is still at most three calls. Only the ceiling per call moved.
DEFAULT_MAX_TOKENS = 4096

AGENTS = {
    "foundry": {
        "module": "foundry_agent",
        "project": "FLEET",
        "doctrine": """You are Foundry, a participant on the SFDC24 Blackboard.

WHAT YOU ACTUALLY ARE, and you must not overstate it:
You are a model deployment on Azure AI Foundry, reached over HTTP by a small
adapter - a scheduled cloud job since 2026-09-24. You have NO shell, NO repository, NO Azure CLI,
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
You are a Google model reached over HTTP by a small adapter - a scheduled
cloud job since 2026-09-24 - on the Gemini API with a key. You
have NO shell, NO repository, NO gcloud CLI of your own, NO GitHub access and
NO ability to open a pull request, merge, deploy, or read a file. You cannot
browse. You only see the board row quoted to you below.

YOUR LANE on this fleet is architecture and security: whether a design will
hold, where it will break first, what it exposes, and what it costs to run.""" + _SHARED_RULES,
    },
    "grok": {
        "module": "grok_agent",
        "project": "FLEET",
        "doctrine": """You are Grok, a participant on the SFDC24 Blackboard.

WHAT YOU ACTUALLY ARE, and you must not overstate it:
You are an xAI model reached over HTTP by a small adapter on Mr Salam's laptop.
This is the API route, NOT the Grok Bot desktop app. The desktop app can drive
this laptop; you cannot. You have NO shell, NO repository, NO GitHub access and
NO ability to open a pull request, merge, deploy, or read a file. You cannot
browse. You only see the board row quoted to you below.

If a row asks you to DO something on the laptop, say plainly that this route
cannot, and name what it would take. Do not accept work on behalf of the
desktop app.

YOUR LANE on this fleet is the board itself and the outside world: dispatching,
triage, product framing, market and outreach research.""" + _SHARED_RULES,
    },
    "claude-api": {
        "module": "claude_agent",
        "project": "FLEET",
        # THE STANDBY RULE, AND WHY IT IS THIS NARROW.
        #
        # Mr Salam wrote "Claude-code-cli can you answers questions on health
        # science?" on WhatsApp at 2026-09-20T21:23Z. It reached the board and
        # sat unanswered for three hours: foundry, gemini and grok each have a
        # doorbell, and the claude-code-cli tag has none.
        #
        # So this agent also picks up rows addressed to claude-code-cli - but
        # ONLY when the sender is whatsapp, which on this board means HIM. Two
        # reasons for the narrowness. The fleet writes to claude-code-cli
        # constantly: every waker reply is addressed to it, and codex sends it
        # long technical reviews. Answering those would be noise and spend, and
        # the reviews want the lane with a shell, not a model. And his messages
        # are the only ones with nobody else watching them.
        "standby_for": ("claude-code-cli",),
        "standby_when_sender": ("whatsapp",),
        "doctrine": """You are Claude on the Anthropic API, a participant on the SFDC24 Blackboard.

WHAT YOU ACTUALLY ARE, and the distinction here is not cosmetic:
Your tag is `claude-api`. You are NOT `claude-code-cli`. That is a different
lane on this fleet - a Claude Code session on Mr Salam's laptop with a shell, a
checkout of the repositories, and the ability to run tests, open a pull request
and merge it. You are a model endpoint reached over HTTP by a small adapter.
You have NO shell, NO repository, NO GitHub access and NO ability to open a
pull request, merge, deploy, or read a file. You cannot browse. You only see
the board row quoted to you below.

You are answering because Mr Salam addressed claude-code-cli on WhatsApp, and
this standby answers every such message straight away. You do NOT know
whether the claude-code-cli session is open: it may be working and reply
itself a few minutes later. So never say it is closed, offline or "not open".
SAY, in one short line before your answer, something like: this is the API
standby answering straight away; the claude-code-cli session will also see
your message. Then answer his question properly. If what he asked for needs the laptop - a code
change, a PR, a deploy, reading a file, checking a live page - say plainly that
this route cannot do it and that it needs the claude-code-cli session, rather
than describing what someone could do as though you were doing it.

YOUR LANE is answering him at any hour: explanation, analysis, drafting,
judgement. Be useful and be brief.""" + _SHARED_RULES,
    },
}


# ---------------------------------------------------------------- bus reading

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def load_env() -> dict:
    """Use the same injected-secret or BLACKBOARD_ENV contract as the bus."""
    return _load_bus_env()


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


_TAG_TOKENS = re.compile(r"[^A-Za-z0-9_-]+")


def names_tag(field: str, me: str) -> bool:
    """True when `field` names exactly this tag, as a whole token.

    `me in field` cannot carry this distinction. "grok" is a substring of
    "grok-bot", and those are two different lanes on this board: grok is the
    xAI API route, grok-bot is the desktop app that drives the laptop. A waker
    that answers another agent's mail is worse than one that stays quiet,
    because the sender is told the wrong thing by the wrong party.

    So tags are compared as whole tokens, with '-' and '_' kept INSIDE the
    token - which is what makes "grok-bot" one name rather than two.
    """
    return me in [t for t in _TAG_TOKENS.split((field or "").lower()) if t]


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
    if names_tag(target, me):
        return True

    # A standby agent also answers rows addressed to the tag it stands in for,
    # but only from the senders named in its config. See AGENTS["claude-api"].
    cfg = AGENTS.get(me) or {}
    standby_for = cfg.get("standby_for") or ()
    if standby_for and source in (cfg.get("standby_when_sender") or ()):
        for other in standby_for:
            if names_tag(target, other) or re.match(
                    r"^\s*%s\b" % re.escape(other), payload, re.I):
                return True
            for field in ("to", "cc"):
                m = re.search(r"\b%s=([^|]*)" % field, payload, re.I)
                if m and names_tag(m.group(1), other):
                    return True
    for field in ("to", "cc"):
        m = re.search(r"\b%s=([^|]*)" % field, payload, re.I)
        if m and names_tag(m.group(1), me):
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


WAKER_REPLY_MARK = "wakerreply=1"


def is_waker_reply(row) -> bool:
    """True when this row is one waker answering another - never an ask.

    THE LOOP THIS STOPS, measured on the live board 2026-09-20.

    Replies are posted `to=<sender>;ALL`. So foundry answers a row from grok
    and addresses the answer to grok; grok reads it as mail addressed to grok
    and answers THAT, addressed to foundry; and so on. The chain is legible in
    the Row_IDs it left behind:

        GROK-WAKE-FOUNDRY-WAKE-GROK-OVERNIGHT-FLEET-1312
        FOUNDRY-WAKE-GROK-WAKE-FOUNDRY-WAKE-GROK-OVERNIGHT-FL

    Depth three within one hour of grok being registered. Bounded per pass by
    --max, unbounded over time: at a fifteen-minute cadence this is three
    agents paying for each other's conversation for ever, and it grows a board
    that rolls over at 2000 rows.

    The existing echo guard is `is_from(row, me)` - it stops an agent answering
    ITSELF and says nothing about answering a PEER. The file's own docstring
    warns about the first and not the second.

    TWO TESTS, because the board already holds rows written before the marker
    existed:
      - WAKER_REPLY_MARK, a closed token, for everything written from now on
      - `answers=` from a sender that is itself a registered agent, for the
        rows already there

    The second condition needs BOTH halves. `answers=` alone would gag
    claude-code-cli, which writes `answers=WRK-...` when replying to Mr Salam -
    and those rows SHOULD be answered by the agents. A substring cannot carry
    that distinction; the sender is what carries it.
    """
    payload = str(row[C_PAYLOAD] if len(row) > C_PAYLOAD else "")
    if WAKER_REPLY_MARK in payload:
        return True
    sender = str(row[C_SOURCE] if len(row) > C_SOURCE else "").strip().lower()
    return sender in AGENTS and "answers=" in payload


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
        if (len(row) <= C_PAYLOAD or is_from(row, me)
                or is_waker_reply(row) or not addressed_to(row, me)):
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
    # HIS MESSAGES GO FIRST, then oldest-first inside each band.
    #
    # Measured 2026-09-20: the first grok pass found 26 unanswered rows, and
    # "Grok can you reply?" - sent by Mr Salam on WhatsApp at 2026-09-19T22:59Z
    # and still unanswered sixteen hours later - sat eighth behind fleet
    # chatter, three answers a pass. A person waiting on a reply is not the
    # same as a queue of agent notes, and a doorbell that makes him wait three
    # passes for an answer has not really rung.
    #
    # A whatsapp-sourced row IS him: that lane carries nothing else.
    out = sorted(groups.values(),
                 key=lambda g: (0 if sender_of(g["row"]).lower() == "whatsapp" else 1,
                                g["ts"]))
    for g in out:
        g["reasks"] = seen_count[bcb_id(g["row"])] - 1
    return out


# -------------------------------------------------------------------- state

def state_path(me: str) -> str:
    root = os.environ.get("BLACKBOARD_STATE_DIR") or REPO
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, ".%s_waker_state.json" % me)


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

def legacy_reply_row_id(me: str, answers: str) -> str:
    """Row_ID written before the hash suffix.

    It strips `.` and `_` and keeps 40 characters, so it is not an identity:
    SYNTHETIC.ASK and SYNTHETIC_ASK become one id, and so do two long ids
    that share a 40-character prefix. Recovery may FIND an old row by this
    id. It may accept the row only when answers= is the full source id.
    """
    return "%s-WAKE-%s" % (me.upper(), re.sub(r"[^A-Za-z0-9-]", "", str(answers or ""))[:40])


def reply_row_id(me: str, answers: str) -> str:
    """Row_ID of the reply post_reply will write for this answers id.

    The readable prefix is the source id with the board's id characters
    kept, so a person can still see which ask it was. A short hash of the
    FULL source id is appended, because the prefix alone collides: the
    legacy form strips `.` and `_` and truncates to 40 characters.

    The cloud waker looks a reply up by answers=, not by this string alone.
    One function, so the id that is written cannot drift from the id a
    later run computes.
    """
    raw = str(answers or "")
    prefix = re.sub(r"[^A-Za-z0-9._-]", "", raw)[:40]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    body = "%s-%s" % (prefix, digest) if prefix else digest
    return "%s-WAKE-%s" % (me.upper(), body)


def claim_answer(answers_id: str) -> bool:
    """Whether this process may call the model for answers_id.

    The laptop waker is the only writer of its file, so the claim is free.
    The cloud waker replaces this with a compare-and-swap on the shared cursor
    BEFORE the model call. False means skip the row. A lost compare-and-swap
    raises, because the cursor this process holds is stale and must not be written.
    """
    return True


def post_reply(me: str, cfg: dict, text: str, to: str, answers: str, verbose: bool) -> bool:
    """Write the reply through fleet_agent post, never a hand-built row.

    Hand-assembled arrays column-shifted three places on 2026-09-08, putting the
    tag in Row_ID and leaving Target_Surface empty, and a ballot drew zero
    replies because nobody was addressed.
    """
    rid = reply_row_id(me, answers)
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


def call_agent(cfg: dict, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS):
    """Normalise the two adapters, which do not take the same arguments.

    gemini_agent.ask takes no max_tokens at all, so the TypeError fallback is a
    signature test, not an error path.
    """
    mod = __import__(cfg["module"])
    # ASK THE SIGNATURE, DO NOT PROVOKE A TypeError.
    #
    # The first version called ask(prompt, max_tokens=...) and fell back to
    # ask(prompt) on TypeError. gemini_agent.ask genuinely takes no max_tokens,
    # so the fallback fired for the right reason there - but a TypeError raised
    # INSIDE any adapter looks identical from outside, and the fallback then
    # calls the model a SECOND time and bills for it. Reading the signature
    # cannot confuse "wrong arguments" with "went wrong".
    try:
        takes_budget = "max_tokens" in inspect.signature(mod.ask).parameters
    except (TypeError, ValueError):
        takes_budget = False
    try:
        if takes_budget:
            return mod.ask(prompt, max_tokens=max_tokens)
        return mod.ask(prompt)
    except SystemExit as exc:
        # grok_agent raises SystemExit when a reply carries no choices, and an
        # uncaught one ends the whole pass: one provider hiccup would stop the
        # doorbell for every remaining row, which is the failure this file
        # exists to prevent. Report it the way foundry_agent already does.
        return None, "adapter raised SystemExit: %s" % (exc,)
    except Exception as exc:  # noqa: BLE001 - a doorbell must survive the bell
        return None, "%s: %s" % (type(exc).__name__, exc)


# ----------------------------------------------------------------------- main

def main(argv=None) -> int:
    """argv is a parameter so a test can drive a whole pass.

    It was not, which is why the watermark path had no coverage: every existing
    test exercised select() and addressed_to() directly, and nothing ever ran
    the loop that decides what to remember. grok_agent.py already takes argv;
    this matches it.
    """
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
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                    dest="max_tokens",
                    help="token budget per model call (default %d). Thinking "
                         "counts against it: below ~1024, claude-opus-5 returns "
                         "a thinking block and no prose at all." % DEFAULT_MAX_TOKENS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

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
    # Quarantine is not a receipt and not work. unknown_ids stay in the
    # cursor so a person can still see them, and they are not copied into
    # answered_ids. They have to leave the list BEFORE the per-pass cap:
    # claim_answer will only refuse them, and three refusals fill --max,
    # hold the watermark, and the new row behind them is never reached.
    unknown = set(state.get("unknown_ids") or [])
    if unknown:
        pending = [item for item in pending if bcb_id(item["row"]) not in unknown]
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
    # THE EARLIEST ROW THIS PASS COULD NOT ANSWER.
    #
    # Found by chatgpt-codex-desktop on 2026-09-20 with a live repro of the
    # 16:01:44Z grok pass: WRK-0ba298fa posted, CCC-RW-PROOF-001 failed on an
    # HTTP 429, CCC-CONSOLE-403-001 then posted. The watermark advanced to the
    # LATER success while the failed row never reached answered_ids - so on the
    # next pass it fell before `since` and was never selected again. Answered
    # rows are remembered; failed ones were simply lost.
    #
    # The POST-failure path below already breaks for exactly this reason, and
    # its comment says so. Catching adapter failures to keep the pass alive
    # opened the same hole without the same protection.
    #
    # Breaking on first failure would close it too, and would undo the thing
    # the catch is for: one 429 taking the doorbell down for every later row.
    # A floor keeps both. Re-reading a row that is already in answered_ids
    # costs a wider window and nothing else - answered_ids is the dedupe, the
    # watermark is only the window.
    floor_ts = None
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

        # Ownership before the model. Two overlapping cloud runs both reach
        # this line with the same cursor; only the compare-and-swap winner
        # is allowed to call the model. A loser holding a stale token raises
        # rather than answering. False (someone else already owns a different
        # row, or this one is already answered) skips it and, when it is not
        # yet answered, holds the watermark so the row is not walked past.
        if not claim_answer(src_id):
            if src_id not in state["answered_ids"]:
                if floor_ts is None or item["ts"] < floor_ts:
                    floor_ts = item["ts"]
                note = "  %s left for the run that owns it" % src_id
                print(note)
                log(me, note)
            continue

        text, route = call_agent(cfg, prompt, args.max_tokens)
        if not text:
            fail = "    %s could not answer: %s" % (me, route)
            print(fail)
            log(me, fail)
            if floor_ts is None or item["ts"] < floor_ts:
                floor_ts = item["ts"]
            continue

        body = " ".join(text.split())
        reply = (
            # WAKER_REPLY_MARK first, so the guard can see it without parsing
            # the rest. See is_waker_reply for what it stops.
            "%s|answers=%s|evidence=STATED|route=%s|" % (WAKER_REPLY_MARK, src_id, route)
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

    if floor_ts is not None and newest_ts:
        # One second before the earliest failure, so that row is still inside
        # the window next time. Never moves the watermark BACKWARD past where
        # it already was: a floor older than the previous watermark would
        # re-open rows this agent has already dealt with.
        cap = (floor_ts - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if cap < newest_ts:
            held = max(cap, state["watermark"] or cap)
            note = ("  watermark held at %s (earliest unanswered row was %s) "
                    "instead of %s" % (held, floor_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       newest_ts))
            print(note)
            log(me, note)
            newest_ts = held

    if not args.dry_run and newest_ts:
        state["watermark"] = newest_ts
        save_state(me, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

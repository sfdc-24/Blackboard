#!/usr/bin/env python3
"""Grok (xAI) as a fleet agent, for design, architecture and outreach research.

WHY THIS EXISTS
---------------
Asked for on 2026-09-18: "I want you to use this API to interact with Grok, get
design feedback and use it for architectural and market outreach research
needs". That is a standing instruction rather than a one-off question, so it
gets a script with a stable contract instead of a throwaway curl.

It deliberately mirrors scripts/foundry_agent.py so the fleet has ONE shape:

    import grok_agent as ga
    text, model = ga.ask("what is wrong with this page", system="be blunt")

WHAT IT FIXES FROM THE FOUNDRY EQUIVALENT
foundry_agent.py has no --system flag, which meant every call that needed a
system prompt had to be made in-process. This takes --system on the command
line as well, because most of the research use above is "ask one sharp question
with one sharp framing".

CREDENTIALS
XAI_API_KEY is read from .env at run time. Never argv, never logged, never
printed - not even truncated. D-18.

THE FAILURE THIS SCRIPT IS BUILT TO EXPLAIN
As of 2026-09-18 the API answers 403 with:

    {"code":"permission-denied",
     "error":"Your newly created team doesn't have any credits or licenses yet."}

That is NOT a bad key. It was proven by contrast: with no Authorization header
the API returns 401 unauthenticated, and with this key it returns 403 forbidden
- so the key is parsed and accepted, and the team behind it simply has no
credits. SuperGrok is a consumer subscription and is billed separately from the
API; buying it did not change this response.

So `explain()` below prints the provider message verbatim rather than a generic
"request failed". A failure that hides the reason costs more than the request
did.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

BASE = "https://api.x.ai/v1"
CHAT = BASE + "/chat/completions"
MODELS = BASE + "/models"

# A browser-shaped agent. The API sits behind Cloudflare, and a thin or absent
# User-Agent is a known way to collect a bodyless 403 from the edge rather than
# an answer from the service - that cost real time on Groq once already.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# Narrow models are filtered out of automatic selection so a media or embedding
# model is never handed a text question.
#
# THE FIRST VERSION OF THIS LIST DID NOT WORK, and it is worth writing down why.
# It excluded "image" - but the media models are named grok-imagine-image and
# grok-imagine-video, and "image" is NOT a substring of "imagine". So none of
# them was filtered, and the only thing that stopped a text prompt going to a
# video generator was the accident of lexical ordering putting grok-4.x first.
# A filter that silently matches nothing is worse than no filter, because it
# reads as protection.
EXCLUDE = ("imagine", "vision", "embed", "video", "build")

# Reasoning variants are preferred for the work this channel exists to do -
# design critique, architecture review, outreach research - where the thinking
# matters more than the latency.
#
# NO VERSION RANKING IS ATTEMPTED between grok-4.20 and grok-4.6. Lexically
# 4.20 sorts first; numerically (4,20) beats (4,6); and neither tells you which
# xAI actually shipped later, because 4.20-0309 looks like a dated build and 4.6
# looks like a release line. Guessing produced the worst available Grok-4 on the
# first real call. So selection prefers reasoning, then takes the longest match
# on the family, and anything more specific is the caller's decision - set
# XAI_MODEL in .env or pass model= explicitly.
PREFER = ("grok-4", "grok-3", "grok")

_ENV_CACHE: Optional[dict] = None


def env(root: Optional[Path] = None) -> dict:
    """Parse .env once. Values are returned, never printed by this module."""
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    here = root or Path(__file__).resolve().parent.parent
    path = here / ".env"
    out: dict = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    # An environment variable wins, so a caller can override without editing .env.
    if os.environ.get("XAI_API_KEY"):
        out["XAI_API_KEY"] = os.environ["XAI_API_KEY"]
    _ENV_CACHE = out
    return out


def key() -> str:
    k = env().get("XAI_API_KEY", "")
    if not k:
        raise SystemExit(
            "XAI_API_KEY is not in .env and not in the environment. "
            "Get one from https://console.x.ai and add it to .env."
        )
    return k


def _headers() -> dict:
    return {
        "Authorization": "Bearer " + key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
    }


def explain(err: urllib.error.HTTPError) -> str:
    """Turn an HTTPError into something a person can act on.

    The status alone is not a diagnosis. 401 means no credential reached the
    service; 403 means one did and was refused - usually for billing. Printing
    the provider body is what separates those two, and an earlier version of
    this pattern printed only the exception class and hid the answer.
    """
    try:
        body = err.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    detail = body.strip()
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            detail = parsed.get("error") or parsed.get("message") or detail
            code = parsed.get("code")
            if code:
                detail = "%s: %s" % (code, detail)
    except Exception:
        pass
    hint = ""
    if err.code == 403 and "credit" in detail.lower():
        hint = (
            "\n  This is a billing state, not a bad key. SuperGrok is a consumer "
            "subscription and does not fund the API; API credits are bought "
            "separately against the team the key belongs to."
        )
    elif err.code == 401:
        hint = "\n  No usable credential reached the service - check XAI_API_KEY in .env."
    elif err.code == 429:
        hint = "\n  Rate limited. Back off and retry."
    return "HTTP %s - %s%s" % (err.code, detail or "(no body)", hint)


# MEASURED, NOT GUESSED. The first default here was 180 seconds and it was too
# short: grok-4.6 took 163.4s on a 2,919-token prompt, and a 13,781-character
# briefing timed out entirely. Reasoning models spend tokens you cannot see -
# that same call reported prompt=1209 completion=533 but total=5432 - so wall
# time scales with thinking rather than with the visible answer.
#
# A read timeout is also the worst failure to be wrong about, because the
# request HAS been sent: the work may be done and billed while the client throws
# away the answer. Erring long costs patience; erring short costs money.
def _post(url: str, payload: dict, timeout: int = 600) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def models() -> list:
    """Every model this key can see. Raises HTTPError with a readable body."""
    return sorted(m.get("id", "") for m in _get(MODELS).get("data", []) if m.get("id"))


def pick(available: Optional[list] = None) -> str:
    """Choose a text model by preference rather than hardcoding one that retires.

    An explicit XAI_MODEL in .env wins outright: this function guesses, and a
    caller who knows better should not have to argue with it.
    """
    override = env().get("XAI_MODEL", "").strip()
    if override:
        return override

    ids = available if available is not None else models()
    usable = [m for m in ids if not any(bad in m for bad in EXCLUDE)]
    if not usable:
        raise SystemExit("this key can see no usable text model")

    def rank(name: str):
        # Reasoning first, then multi-agent, then anything explicitly
        # non-reasoning last. Name is the final tiebreak so the choice is
        # stable across runs rather than depending on dict ordering.
        reasoning = 0 if "non-reasoning" in name else (2 if "reasoning" in name else 1)
        multi = 1 if "multi-agent" in name else 0
        return (-reasoning, -multi, name)

    for want in PREFER:
        hit = [m for m in usable if m.startswith(want)]
        if not hit:
            hit = [m for m in usable if want in m]
        if hit:
            return sorted(hit, key=rank)[0]
    return sorted(usable, key=rank)[0]


# Token usage from the most recent ask(), so spend is visible rather than
# invented. This is metered and billed per call; a client that does not report
# what it consumed leaves the only honest number out of the conversation. Kept
# off the return tuple deliberately, so ask() stays interchangeable with
# foundry_agent.ask rather than growing a third element the fleet has to unpack.
LAST_USAGE: dict = {}


def ask(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 1500,
    temperature: float = 0.7,
) -> Tuple[str, str]:
    """Ask Grok one question. Returns (text, model_used).

    Same shape as foundry_agent.ask so the two are interchangeable in the fleet.
    Token usage lands in LAST_USAGE.
    """
    global LAST_USAGE
    chosen = model or pick()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": chosen,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    body = _post(CHAT, payload)
    LAST_USAGE = body.get("usage") or {}
    # The fleet reads usage off the FUNCTION, because foundry_agent puts it
    # there and agent_waker.py reports `mod.ask.last_usage`. A module global
    # alone makes every grok reply log "? tokens" - a spend figure quietly
    # going missing rather than an error anyone would notice.
    ask.last_usage = {
        "in": LAST_USAGE.get("prompt_tokens"),
        "out": LAST_USAGE.get("completion_tokens"),
        "total": LAST_USAGE.get("total_tokens"),
    }
    choices = body.get("choices") or []
    if not choices:
        raise SystemExit("no choices in the response: %s" % json.dumps(body)[:300])
    text = (choices[0].get("message") or {}).get("content") or ""
    return text.strip(), chosen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ask Grok something, or list models.")
    sub = ap.add_subparsers(dest="cmd")

    say = sub.add_parser("say", help="ask one question")
    say.add_argument("prompt", help="the question (positional, like foundry_agent)")
    say.add_argument("--system", default=None, help="system framing")
    say.add_argument("--model", default=None, help="override model selection")
    say.add_argument("--max-tokens", type=int, default=1500)
    say.add_argument("--temperature", type=float, default=0.7)

    sub.add_parser("models", help="list models this key can see")
    sub.add_parser("check", help="report whether the API is reachable and why not")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2

    try:
        if args.cmd == "models":
            for m in models():
                print(m)
            return 0

        if args.cmd == "check":
            found = models()
            print("reachable: %d models, would use %s" % (len(found), pick(found)))
            return 0

        started = time.time()
        text, used = ask(
            args.prompt,
            system=args.system,
            model=args.model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
        )
        took = time.time() - started
        bits = []
        for label, field in (("prompt", "prompt_tokens"), ("completion", "completion_tokens"),
                             ("total", "total_tokens")):
            if LAST_USAGE.get(field) is not None:
                bits.append("%s=%s" % (label, LAST_USAGE[field]))
        usage = ("  " + " ".join(bits)) if bits else ""
        print("--- %s in %.1fs%s ---" % (used, took, usage))
        print(text)
        return 0

    except urllib.error.HTTPError as exc:
        print(explain(exc), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print("network error: %s" % exc.reason, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Claude on the Anthropic API, as a fleet agent. The standby for claude-code-cli.

WHY THIS EXISTS
---------------
Mr Salam sent "Claude-code-cli can you answers questions on health science?" on
WhatsApp at 2026-09-20T21:23Z. It reached the board and sat there for three
hours, because foundry, gemini and grok each have a doorbell that fires every
fifteen minutes and the claude-code-cli tag has none. If no session is open,
his message waits.

This is that doorbell - and it is deliberately NOT claude-code-cli. That tag
means the lane with a shell, a repository and the ability to open a pull
request. A model endpoint answering under that name would be the exact
false-capability claim every agent doctrine on this fleet exists to stop. So
the tag is `claude-api`, it says what it cannot do, and it stands in only for
HIS messages - see the standby rules in agent_waker.AGENTS.

WHY STDLIB AND NOT THE ANTHROPIC SDK
------------------------------------
The official SDK is the right default for a normal project. This is not one:
`anthropic` is not installed, there is no requirements.txt or pyproject.toml
covering scripts/, and the hosted suites run each file inside `unshare -n` with
no install step. foundry_agent, gemini_agent and grok_agent are all urllib and
share this shape. Adding a dependency for the fourth adapter would break the
offline CI and split the pattern. If a manifest ever arrives, move this to the
SDK first.

THE TRAP THIS FILE IS BUILT AROUND, AND IT COST A DAY
------------------------------------------------------
On 2026-09-20 every Foundry reply came back empty:

    stop_reason : max_tokens
    blocks      : ['thinking']
    tokens      : out 700 of max 700 (thinking 700)

claude-opus-5 THINKS BY DEFAULT and thinking counts against max_tokens, so a
small budget is spent before a word of prose. Two consequences, both encoded
below:

  1. max_tokens is generous by default, not thrifty.
  2. `content` is a LIST OF BLOCKS and the thinking block comes FIRST. Reading
     content[0].text returns a thinking block's absent text and looks like an
     empty answer. Every text-type block is joined instead, never indexed.

And one the API enforces: `thinking.budget_tokens` is REMOVED on claude-opus-5
and returns 400. So is `temperature`. Do not add either back.

CREDENTIALS
ANTHROPIC_API_KEY from .env at run time. Never argv, never logged, never
printed - not even truncated. D-18.

USAGE
    python scripts/claude_agent.py check
    python scripts/claude_agent.py say "what is a validation rule"
    python scripts/claude_agent.py say --file question.txt --max-tokens 8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5"

# Generous on purpose. The skill's guidance for a non-streaming request is
# ~16000, and the Foundry failure above is what a thrifty number looks like:
# thinking consumes the budget and the caller receives silence. A board reply
# is capped at 1500 characters downstream anyway, so this buys correctness, not
# length.
DEFAULT_MAX_TOKENS = 16000

KEY_NAME = "ANTHROPIC_API_KEY"
MODEL_ENV = "ANTHROPIC_MODEL"


def load_env(path: Path | None = None) -> dict:
    path = path or Path(os.environ.get("BLACKBOARD_ENV") or (REPO / ".env"))
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def api_key() -> str:
    env = load_env()
    return os.environ.get(KEY_NAME) or env.get(KEY_NAME, "")


def text_of(content) -> str:
    """Join every TEXT block. Never content[0].

    claude-opus-5 emits a thinking block first. Indexing returns that block,
    whose .text does not exist, and the caller reports an empty answer from a
    perfectly good 200. foundry_agent.py carries the same comment at its own
    call site for the same reason.
    """
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


def ask(prompt: str, model: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS,
        system: str | None = None):
    """Ask Claude once. Returns (text, route) or (None, reason).

    Mirrors foundry_agent.ask so the two are interchangeable in the fleet, and
    stashes usage on ask.last_usage because agent_waker reports mod.ask.last_usage.
    """
    ask.last_usage = None
    env = load_env()
    model = model or os.environ.get(MODEL_ENV) or env.get(MODEL_ENV) or DEFAULT_MODEL
    key = api_key()
    if not key:
        return None, "NO CREDENTIAL - %s absent from .env" % KEY_NAME

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    # NO thinking block, NO temperature. claude-opus-5 thinks by default, and
    # both `thinking.budget_tokens` and `temperature` return 400 on it.

    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "user-agent": "sfdc24-fleet/1.0",
        })
    route = "%s (anthropic/v1/messages, key %s)" % (model, KEY_NAME)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode("utf-8", "replace")[:400]
        return None, "HTTP %s on %s: %s" % (ex.code, route, detail)
    except Exception as ex:
        return None, "%s on %s: %s" % (type(ex).__name__, route, ex)

    u = body.get("usage") or {}
    ask.last_usage = {"in": u.get("input_tokens"), "out": u.get("output_tokens"),
                      "total": (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)}

    stop = body.get("stop_reason")
    text = text_of(body.get("content"))
    if text:
        return text, route

    # A 200 with no prose. Say WHY, the way foundry_agent learned to - a guard
    # that blocks the right call while withholding the reason just moves the
    # debugging somewhere else.
    blocks = [(b or {}).get("type") for b in (body.get("content") or [])]
    hint = ""
    if stop == "max_tokens":
        hint = ("the budget was spent before any prose was emitted (thinking "
                "counts against it): raise --max-tokens.")
    elif stop == "refusal":
        det = body.get("stop_details") or {}
        hint = ("the model declined. category=%s explanation=%s"
                % (det.get("category"), str(det.get("explanation"))[:160]))
    else:
        hint = "top-level keys: %s" % list(body)
    return None, ("200 on %s but NO TEXT BLOCK.\n"
                  "       stop_reason : %s\n"
                  "       blocks      : %s\n"
                  "       tokens      : out %s of max %s\n"
                  "       %s"
                  % (route, stop, blocks or "(none)", u.get("output_tokens"),
                     max_tokens, hint))


def read_prompt(args) -> str:
    """argv or a file, and never silently empty.

    consult.py filed a blank gemini consultation on 2026-09-20 because a long
    question went out as an argv element through a Windows command line. An
    adapter reachable only through argv is a trap for the next caller.
    """
    if getattr(args, "prompt_file", None):
        text = Path(args.prompt_file).read_text(encoding="utf-8")
    else:
        text = args.prompt or ""
    if not text.strip():
        raise SystemExit("empty question: pass text, or --file with a path that has some")
    return text


def cmd_check(args) -> int:
    key = api_key()
    print("key set: %s" % bool(key))
    if not key:
        print("  %s is absent from .env" % KEY_NAME)
        return 1
    text, route = ask("Reply with exactly: claude online", max_tokens=64)
    if not text:
        print("  no answer: %s" % route)
        return 1
    print("  %s" % route)
    print("  %s" % text)
    print("  tokens: %s" % (ask.last_usage or {}))
    return 0


def cmd_say(args) -> int:
    text, route = ask(read_prompt(args), args.model, args.max_tokens)
    if not text:
        print("  no answer: %s" % route)
        return 1
    print("  [%s]\n" % route)
    print(text)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="claude adapter: Anthropic Messages API, stdlib only")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.set_defaults(fn=cmd_check)
    s = sub.add_parser("say")
    s.add_argument("prompt", nargs="?", default=None)
    s.add_argument("--file", dest="prompt_file", default=None,
                   help="read the question from this path instead of argv")
    s.add_argument("--model", default=None)
    s.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, dest="max_tokens")
    s.set_defaults(fn=cmd_say)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

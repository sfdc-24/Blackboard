#!/usr/bin/env python3
"""Azure Foundry adapter. Two routes on one resource, and real token usage.

WHY THIS EXISTS AND WHY IT TOOK A DAY
  For a day this codebase carried the claim "Foundry 401s on every endpoint".
  It appeared in two commit messages and a docstring. It was wrong.

  The requests really did 401 — ten of them, on ten paths. But a failure that
  reproduces is still only evidence about the REQUEST. The key in .env was 32
  characters and belonged to a different resource entirely; the real key on
  `abdus-2123-resource` is 84. With it, the very first call returned HTTP 200.
  Three models had been deployed the whole time. The consistency of the failure
  is exactly what made it feel like a property of the service.

  So the first question about any 401 is not "which path?" — it is "which
  resource is this credential even from?"

TWO ROUTES, AND THE MODEL NAME PICKS ONE
  The same resource speaks two different protocols on two different paths with
  two different auth headers. Getting any one of the six wrong returns 401 or
  404 with no hint which of them was the problem:

  OpenAI-shaped  ->  POST /models/chat/completions?api-version=2024-05-01-preview
                     header  api-key: <key>
                     body    {"model": "...", "messages": [...]}
                     text at choices[0].message.content
                     serves  gpt-4o, text-embedding-3-large

  Anthropic-shaped -> POST /anthropic/v1/messages
                     headers x-api-key: <key>  +  anthropic-version: 2023-06-01
                     body    {"model","max_tokens","messages"}
                     text at the "text"-TYPE content block — NOT content[0]
                     serves  claude-opus-5

  The `api-key` header returns 401 on the anthropic path, and the anthropic path
  404s the OpenAI body. They are not interchangeable.

THE content[0] TRAP, WHICH I FELL INTO
  The first probe of claude-opus-5 got HTTP 200, reported 24 output tokens, and
  printed an EMPTY answer, because it read content[0].text. With thinking on,
  content[0] is a `thinking` block and the prose is in a later block. A 200 with
  empty text is the most dangerous shape there is: it looks like success and
  carries nothing. Hence `join text-type blocks`, never an index.

D-18: the key is read from .env at run time. Never argv, never logged, never
printed. `check` reports presence and length only.

USAGE
  python scripts/foundry_agent.py check
  python scripts/foundry_agent.py say "one line on why a reproduced 401 proves little"
  python scripts/foundry_agent.py say "..." --model gpt-4o
  python scripts/foundry_agent.py board "text to post as foundry"
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(REPO, ".env")

RESOURCE = "abdus-2123-resource"
RESOURCE_GROUP = "copilot-dev-rg"
HOST = "https://%s.services.ai.azure.com" % RESOURCE

OPENAI_PATH = "/models/chat/completions?api-version=2024-05-01-preview"
ANTHROPIC_PATH = "/anthropic/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_MODEL = "claude-opus-5"
OPENAI_MODEL = "gpt-4o"
KEY_NAMES = ["FOUNDRY_API_KEY", "AZURE_AI_API_KEY"]

# Deployments seen on the resource 2026-09-17 via
#   az cognitiveservices account deployment list -n <res> -g <rg>
KNOWN = ["claude-opus-5", "gpt-4o", "text-embedding-3-large"]


def load_env():
    kv = {}
    if os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8", errors="replace"):
            m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$", line)
            if m:
                kv[m.group(1)] = m.group(2)
    return kv


def api_key():
    kv = load_env()
    for n in KEY_NAMES:
        if kv.get(n):
            return n, kv[n]
        if os.environ.get(n):
            return n, os.environ[n]
    return None, None


def is_anthropic(model):
    return (model or "").lower().startswith("claude")


def _post(url, headers, payload, timeout=90):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers=dict(headers, **{"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, str(exc)


def extract_anthropic(d):
    """Join every text-TYPE block. Never an index — content[0] is often thinking.

    Returns None rather than "" so a 200-with-no-prose fails loudly instead of
    being reported as a successful empty answer.
    """
    parts = []
    for b in (d or {}).get("content") or []:
        if (b or {}).get("type") == "text":
            t = b.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t)
    return "\n".join(parts) if parts else None


def extract_openai(d):
    try:
        t = d["choices"][0]["message"]["content"]
        return t if isinstance(t, str) and t.strip() else None
    except Exception:
        return None


def usage_of(d, anthropic):
    """Normalise both shapes to one dict. This is the per-agent token number the
    site's meters were always meant to show, and it is free on every call."""
    u = (d or {}).get("usage") or {}
    if anthropic:
        det = u.get("output_tokens_details") or {}
        i, o = u.get("input_tokens"), u.get("output_tokens")
        return {"in": i, "out": o,
                "total": (i or 0) + (o or 0) if (i is not None or o is not None) else None,
                "thought": det.get("thinking_tokens"),
                "cache_read": u.get("cache_read_input_tokens"),
                "cache_write": u.get("cache_creation_input_tokens")}
    return {"in": u.get("prompt_tokens"), "out": u.get("completion_tokens"),
            "total": u.get("total_tokens"), "thought": None,
            "cache_read": None, "cache_write": None}


def ask(prompt, model=None, max_tokens=1024):
    """Ask one Foundry deployment. Returns (text, route) or (None, reason).

    Stashes the normalised usage dict on ask.last_usage for the caller.
    """
    ask.last_usage = None
    model = model or DEFAULT_MODEL
    name, key = api_key()
    if not key:
        return None, "NO CREDENTIAL — %s absent from .env" % "/".join(KEY_NAMES)

    anthropic = is_anthropic(model)
    if anthropic:
        url = HOST + ANTHROPIC_PATH
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": prompt}]}
    else:
        url = HOST + OPENAI_PATH
        headers = {"api-key": key}
        payload = {"model": model,
                   "messages": [{"role": "user", "content": prompt}]}

    status, body = _post(url, headers, payload)
    route = "%s (%s, key %s)" % (model, "anthropic/v1/messages" if anthropic
                                else "models/chat/completions", name)
    if status != 200:
        return None, "HTTP %s on %s: %s" % (status, route, body[:220])
    try:
        d = json.loads(body)
    except Exception:
        return None, "200 on %s but unparseable JSON" % route

    ask.last_usage = usage_of(d, anthropic)
    text = extract_anthropic(d) if anthropic else extract_openai(d)
    if not text:
        # A 200 carrying no prose. Say so; do not hand back an empty string.
        return None, ("200 on %s but NO TEXT BLOCK — top-level keys %s. "
                      "If this is an anthropic response, the prose block is "
                      "missing, not merely at another index."
                      % (route, list(d.keys())))
    return text, route


def deployments():
    """Ask Azure what is actually deployed. Never fabricate this list."""
    try:
        out = subprocess.run(
            ["az", "cognitiveservices", "account", "deployment", "list",
             "--name", RESOURCE, "--resource-group", RESOURCE_GROUP,
             "--query", "[].name", "-o", "tsv"],
            capture_output=True, text=True, timeout=180, shell=True)
        got = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
        return got or None
    except Exception:
        return None


def cmd_check(args):
    name, key = api_key()
    print("  resource     : %s (rg %s)" % (RESOURCE, RESOURCE_GROUP))
    print("  API key      : %s" % (("present as %s (%d chars)" % (name, len(key)))
                                   if key else "ABSENT"))
    if key and len(key) < 40:
        print("                 ^ SHORT. The real key on this resource is 84 chars.")
        print("                   A short key here is the bug that cost a day —")
        print("                   it is a valid key for a DIFFERENT resource.")
    live = deployments()
    print("  deployments  : %s" % (", ".join(live) if live
                                   else "could not read from azure (using known list)"))
    print()
    if not key:
        print("  => foundry CANNOT speak. Read the key with:")
        print("     az cognitiveservices account keys list --name %s --resource-group %s"
              % (RESOURCE, RESOURCE_GROUP))
        return 1

    print("  --- live probe, BOTH routes ---")
    ok = 0
    for m in (DEFAULT_MODEL, OPENAI_MODEL):
        text, route = ask("Reply with exactly: foundry online", m, max_tokens=64)
        if text:
            ok += 1
            u = getattr(ask, "last_usage", None) or {}
            print("  %-16s OK   said %r" % (m, text.strip()[:60]))
            print("  %-16s      tokens %s total (in %s, out %s, thinking %s)"
                  % ("", u.get("total"), u.get("in"), u.get("out"), u.get("thought")))
        else:
            print("  %-16s FAIL %s" % (m, route))
    print()
    if ok == 2:
        print("  => foundry is REACHABLE on both routes, with per-call token usage.")
        return 0
    if ok:
        print("  => PARTIAL: %d of 2 routes answered. Do not record foundry as live." % ok)
        return 1
    print("  => a credential exists and nothing answered. Present is not working.")
    return 1


def cmd_say(args):
    text, route = ask(args.prompt, args.model, args.max_tokens)
    if not text:
        print("  no answer: %s" % route)
        return 1
    print("  [%s]\n" % route)
    print(text.strip())
    u = getattr(ask, "last_usage", None) or {}
    print("\n  tokens: %s total (in %s, out %s, thinking %s)"
          % (u.get("total"), u.get("in"), u.get("out"), u.get("thought")))
    return 0


def cmd_board(args):
    """Have foundry answer, then post it to the board correctly shaped.

    Posting goes through fleet_agent.py `post` for one reason: hand-built rows
    column-shifted three times, putting the tag in Row_ID and leaving
    Target_Surface empty — which is why a vote ballot drew zero replies.
    """
    text, route = ask(args.prompt, args.model, args.max_tokens)
    if not text:
        print("  not posting — foundry could not answer: %s" % route)
        return 1
    print("  [%s] %s\n" % (route, text.strip()[:160]))
    fa = os.path.join(REPO, "scripts", "fleet_agent.py")
    out = subprocess.run(
        [sys.executable, fa, "post", text.strip()[:1200],
         "--tag", args.tag, "--to", args.to, "--phase", "OPEN",
         "--klass", "NOTE", "--project", "SITE",
         "--prefix", "FOUNDRY-" + (args.prefix or "SAYS"),
         "--gist", text.strip()[:170]],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    print((out.stdout or out.stderr).strip()[:600])
    return out.returncode


def main():
    p = argparse.ArgumentParser(
        description="Azure Foundry adapter: two routes on one resource, loud failures")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.set_defaults(fn=cmd_check)
    s = sub.add_parser("say")
    s.add_argument("prompt")
    s.add_argument("--model", default=None, help="default %s; gpt-4o for the OpenAI route" % DEFAULT_MODEL)
    s.add_argument("--max-tokens", type=int, default=1024, dest="max_tokens")
    s.set_defaults(fn=cmd_say)
    b = sub.add_parser("board")
    b.add_argument("prompt")
    b.add_argument("--model", default=None)
    b.add_argument("--max-tokens", type=int, default=1024, dest="max_tokens")
    b.add_argument("--tag", default="foundry")
    b.add_argument("--to", default="claude-code-cli;ALL")
    b.add_argument("--prefix", default=None)
    b.set_defaults(fn=cmd_board)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()

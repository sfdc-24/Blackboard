#!/usr/bin/env python3
"""Bring gemini onboard. Two routes, one adapter, and a loud failure when neither is open.

WHY THIS EXISTS
  gemini is REAL on this fleet: 82 rows on the board up to 2026-09-08, then
  silence when its credential went away. It is not a new agent to introduce —
  it is an existing one that cannot currently speak. sfdc24.com shows it as
  "needs a key" rather than pretending otherwise.

  This is the adapter so that the moment a credential exists, it speaks.

TWO ROUTES, CHECKED IN ORDER
  1. API KEY  -> https://generativelanguage.googleapis.com/v1beta/interactions
     The Interactions API became the DEFAULT in June 2026; generateContent is
     legacy. Key goes in an `x-goog-api-key` HEADER, not a query parameter.
     Body is {"model": ..., "input": ...}; the answer is at `output_text`.
     Confirmed against the live docs 2026-09-17, NOT from recall — the shape
     changed after this model's training cutoff and a remembered version would
     have targeted the deprecated path.

  2. VERTEX via gcloud ADC -> <loc>-aiplatform.googleapis.com/.../models/<m>:generateContent
     aiplatform.googleapis.com is already enabled on project sfdc24 and gcloud
     is authenticated, so this route may need NO new key at all.

D-18: the key is read from .env at run time. Never argv, never logged, never
printed. `check` reports presence and length only.

USAGE
  python scripts/gemini_agent.py check
  python scripts/gemini_agent.py say "one sentence on why read-back beats trust"
  python scripts/gemini_agent.py board "text to post as gemini"
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

INTERACTIONS = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "gemini-3.8-flash"
VERTEX_MODEL = "gemini-2.0-flash"
KEY_NAMES = ["GEMINI_API_KEY", "GOOGLE_AI_API_KEY", "GOOGLE_API_KEY"]


def load_env():
    # `with` — the bare open() leaked a file handle on every call. Same fix as
    # foundry_agent.load_env; these two were copied from one another, so a bug
    # in one is a bug in both.
    kv = {}
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8", errors="replace") as fh:
            for line in fh:
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


def adc_token():
    """An access token from gcloud ADC, or None. Never printed."""
    try:
        out = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True, timeout=60, shell=True)
        tok = (out.stdout or "").strip().splitlines()
        tok = tok[0] if tok else ""
        return tok if len(tok) > 40 and "ERROR" not in tok else None
    except Exception:
        return None


def gcloud_project():
    try:
        out = subprocess.run(["gcloud", "config", "get-value", "project"],
                             capture_output=True, text=True, timeout=45, shell=True)
        p = (out.stdout or "").strip().splitlines()
        return p[0] if p else ""
    except Exception:
        return ""


def _post(url, headers, payload, timeout=60):
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


def extract_text(d):
    """Find the generated text in an Interactions response.

    THE DOCS AND THE WIRE DISAGREE, AND THE WIRE WINS. The published shape says
    the answer is at `output_text`. What actually came back on 2026-09-17 was an
    envelope — id / status / usage / created / updated / service_tier / steps /
    object / model — with the text at:

        steps[1].content[0].text

    The first version trusted `output_text`, missed, and fell back to dumping
    the whole envelope as the "answer". It printed 400 characters of JSON and
    called it a success. So: try the documented path, then the observed path,
    then walk the structure — and return None rather than hand back an envelope
    pretending to be prose.
    """
    if not isinstance(d, dict):
        return None
    if isinstance(d.get("output_text"), str) and d["output_text"].strip():
        return d["output_text"]
    for step in d.get("steps") or []:
        for part in (step or {}).get("content") or []:
            t = (part or {}).get("text")
            if isinstance(t, str) and t.strip():
                return t
    # Last resort: the deepest plausible string, never the envelope itself.
    best = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("signature", "id", "model", "object", "status"):
                    continue
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and len(o.strip()) > 2:
            best.append(o)

    walk(d.get("steps") or {})
    return best[0] if best else None


def usage_of(d):
    """Per-call token usage — every Gemini call carries it for free.

    An earlier version of this docstring said Foundry "401s on every endpoint".
    That was wrong, and it was wrong in a way worth leaving a note about: the
    requests really did 401, but the cause was a key in .env belonging to a
    DIFFERENT resource, not a broken service. Corrected 2026-09-17 after the
    real key produced HTTP 200 on the first try. A failure I have reproduced
    many times is still only evidence about my request."""
    u = (d or {}).get("usage") or {}
    return {
        "total": u.get("total_tokens"),
        "in": u.get("total_input_tokens"),
        "out": u.get("total_output_tokens"),
        "thought": u.get("total_thought_tokens"),
    }


def ask(prompt, model=None):
    """Try the key route, then ADC. Returns (text, route) or (None, reason).

    Also stashes the last usage dict on ask.last_usage for the caller.
    """
    ask.last_usage = None
    name, key = api_key()
    if key:
        status, body = _post(INTERACTIONS,
                             {"x-goog-api-key": key},
                             {"model": model or DEFAULT_MODEL, "input": prompt})
        if status == 200:
            try:
                d = json.loads(body)
            except Exception:
                return None, "key route returned 200 but unparseable JSON"
            ask.last_usage = usage_of(d)
            text = extract_text(d)
            if not text:
                return None, ("key route returned 200 but no text found; "
                              "top-level keys were: %s" % list(d.keys()))
            return text, "api-key (%s)" % name
        return None, "key route HTTP %s: %s" % (status, body[:200])

    tok = adc_token()
    if tok:
        proj = gcloud_project()
        loc = "us-central1"
        url = ("https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s"
               "/publishers/google/models/%s:generateContent" % (loc, proj, loc, model or VERTEX_MODEL))
        status, body = _post(url, {"Authorization": "Bearer " + tok},
                             {"contents": [{"role": "user", "parts": [{"text": prompt}]}]})
        if status == 200:
            try:
                d = json.loads(body)
                return d["candidates"][0]["content"]["parts"][0]["text"], "vertex-adc (%s)" % proj
            except Exception:
                return None, "vertex returned 200 but an unexpected shape"
        return None, "vertex HTTP %s: %s" % (status, body[:200])

    return None, "NO CREDENTIAL"


def cmd_check(args):
    name, key = api_key()
    print("  API key      : %s" % (("present as %s (%d chars)" % (name, len(key))) if key else "ABSENT"))
    tok = adc_token()
    print("  gcloud ADC   : %s" % ("token available" if tok else "no token"))
    if tok:
        print("  project      : %s" % gcloud_project())
    print()
    if not key and not tok:
        print("  => gemini CANNOT speak. Neither route is open.")
        return 1
    print("  --- live probe ---")
    text, route = ask("Reply with exactly: gemini online")
    if text:
        print("  ROUTE  : %s" % route)
        print("  SAID   : %s" % text.strip()[:200])
        u = getattr(ask, "last_usage", None)
        if u:
            print("  TOKENS : %s total  (in %s, out %s, thought %s)"
                  % (u.get("total"), u.get("in"), u.get("out"), u.get("thought")))
            print("           per-agent token usage, free on every call.")
        print("\n  => gemini is REACHABLE. Flip it live on the site and in voices.json.")
        return 0
    print("  FAILED : %s" % route)
    print("\n  => a credential exists but the call did not succeed. Present is not working —")
    print("     the same trap FOUNDRY_API_KEY fell into: a real key, for the wrong resource.")
    return 1


def read_prompt(args) -> str:
    """argv or a file, exactly one, and never silently empty."""
    if getattr(args, "prompt_file", None):
        with open(args.prompt_file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = args.prompt or ""
    if not text.strip():
        raise SystemExit("empty question: pass text, or --file with a path that has some")
    return text


def cmd_say(args):
    text, route = ask(read_prompt(args), args.model)
    if not text:
        print("  no answer: %s" % route)
        return 1
    print("  [%s]\n" % route)
    print(text.strip())
    return 0


def cmd_board(args):
    """Have gemini answer, then post it to the board AS gemini, correctly shaped."""
    text, route = ask(args.prompt, args.model)
    if not text:
        print("  not posting — gemini could not answer: %s" % route)
        return 1
    print("  [%s] %s\n" % (route, text.strip()[:160]))
    fa = os.path.join(REPO, "scripts", "fleet_agent.py")
    out = subprocess.run(
        [sys.executable, fa, "post", text.strip()[:1200],
         "--tag", "gemini", "--to", args.to, "--phase", "OPEN",
         "--klass", "NOTE", "--project", "SITE",
         "--prefix", "GEMINI-" + (args.prefix or "SAYS"),
         "--gist", text.strip()[:170]],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    print((out.stdout or out.stderr).strip()[:600])
    return out.returncode


def main():
    p = argparse.ArgumentParser(description="gemini adapter: key route, then ADC, then a loud failure")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.set_defaults(fn=cmd_check)
    s = sub.add_parser("say")
    # A long question MUST be able to arrive as a path. Passing prose through
    # argv is how consult.py filed a blank gemini consultation on 2026-09-20:
    # a 2,979-byte question went through the Windows command line, the adapter
    # printed its route header and no body, and the record was written anyway.
    # foundry_agent and grok_thread both take --file already.
    s.add_argument("prompt", nargs="?", default=None)
    s.add_argument("--file", dest="prompt_file", default=None,
                   help="read the question from this path instead of argv")
    s.add_argument("--model", default=None)
    s.set_defaults(fn=cmd_say)
    b = sub.add_parser("board"); b.add_argument("prompt"); b.add_argument("--model", default=None)
    b.add_argument("--to", default="claude-code-cli;ALL"); b.add_argument("--prefix", default=None)
    b.set_defaults(fn=cmd_board)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()

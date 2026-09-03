#!/usr/bin/env python3
"""
SFDC24 — Groq offload client
claude-code-cli, 2026-09-03.

WHY
    Groq is effectively free for us, so bulk work belongs there rather than on
    metered frontier tokens. Standing rule from Mr. Salam: offload what can be
    offloaded.

WHAT GOES HERE, AND WHAT DOES NOT
    Send: first-pass drafting, summarising, classification, parsing,
    adversarial critique, anything where a second opinion is worth more than a
    perfect one.
    Do NOT send: decisions about what is safe to deploy, anything touching live
    systems, or a claim that will be acted on without verification. Cheap
    inference is a reason to ask more questions, not a reason to trust answers.

A NAMING CORRECTION, RECORDED ONCE
    Groq is an independent inference company running its own hardware. It is not
    Meta's API. It serves open-weight models from several vendors, and the model
    this project has actually been using through the gateway is
    `openai/gpt-oss-120b` -- an OpenAI open-weights model that self-reports as
    ChatGPT. There is no Meta model in the fleet.

SETUP
    Add to .env (the value, never pasted into a chat transcript):
        GROQ_API_KEY=...

USAGE
    python scripts/ask_groq.py --file prompts/whatever.md
    python scripts/ask_groq.py --prompt "..." --model openai/gpt-oss-120b
    python scripts/ask_groq.py --models          # what this key can reach
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
API = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"


def load_key() -> str | None:
    if not ENV.exists():
        return None
    for line in ENV.read_bytes().decode("utf-8", "replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if "GROQ" in k.strip().upper():
            v = v.strip().strip('"').strip("'")
            return v or None
    return None


def call(url: str, key: str, payload: dict | None = None, timeout: int = 180):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description="Offload a prompt to Groq.")
    ap.add_argument("--prompt")
    ap.add_argument("--file", type=pathlib.Path)
    ap.add_argument("--system", default="You are a sharp, specific analyst. You "
                    "disagree when you have reason to, you cite what you are unsure "
                    "of, and you never pad. Short and concrete beats long and hedged.")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--models", action="store_true", help="list reachable models")
    args = ap.parse_args()

    key = load_key()
    if not key:
        print("No GROQ_API_KEY in .env.\n"
              "Add the line yourself (do not paste the value into a chat):\n"
              f"    GROQ_API_KEY=...\n  in {ENV}", file=sys.stderr)
        return 2

    if args.models:
        try:
            ms = call(f"{API}/models", key)
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}", file=sys.stderr)
            return 1
        rows = sorted(ms.get("data", []), key=lambda m: m.get("id", ""))
        print(f"{len(rows)} models reachable:\n")
        for m in rows:
            ctx = m.get("context_window", "?")
            print(f"  {m.get('id','?'):46} ctx={ctx}")
        return 0

    text = args.prompt
    if args.file:
        text = args.file.read_text(encoding="utf-8")
    if not text:
        ap.error("give --prompt or --file")

    payload = {
        "model": args.model,
        "temperature": args.temperature,
        "messages": [
            {"role": "system", "content": args.system},
            {"role": "user", "content": text},
        ],
    }

    t0 = time.time()
    try:
        res = call(f"{API}/chat/completions", key, payload)
    except urllib.error.HTTPError as e:
        body = e.read()[:500].decode(errors="replace")
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    ms = int((time.time() - t0) * 1000)

    out = res["choices"][0]["message"]["content"]
    u = res.get("usage", {}) or {}
    print(out)
    print(f"\n--- groq: {args.model} | {ms}ms | "
          f"in {u.get('prompt_tokens','?')} out {u.get('completion_tokens','?')} "
          f"| these tokens did not come off the Claude budget ---", file=sys.stderr)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(out, encoding="utf-8")
        print(f"written: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

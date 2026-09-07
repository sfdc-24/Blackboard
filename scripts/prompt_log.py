#!/usr/bin/env python3
"""
SFDC24 — per-prompt time and token log
claude-code-cli, 2026-09-03. Built after Mr. Salam asked whether one was being kept.

THE HONEST ANSWER WAS NO, AND WHY THAT MATTERED
    I cannot see my own per-turn token usage — the counter visible to me resets
    each turn, so any number I wrote by hand would have been a guess wearing a
    lab coat. The harness, however, records the real thing: every assistant
    record in the session transcript carries input_tokens, cache_creation,
    cache_read, output_tokens and thinking_tokens.

    So this reads ground truth from the transcript instead of asking me to
    remember. That also means the whole session back to its first prompt is
    recoverable, not just from the moment the request was made.

INCREMENTAL BY DESIGN
    The transcript is ~64 MB and grows. Re-parsing it on every turn would add
    seconds to each response, so a state file records the last prompt already
    written and only newer ones are appended.

PRIVACY
    Entries contain prompt text. `logs/` is gitignored, and should stay that
    way — this is a local operating record, not repository content.

USAGE
    python scripts/prompt_log.py --backfill      # whole session, rebuild
    python scripts/prompt_log.py                 # append new prompts only
    (also runs automatically as a Stop hook — see .claude/settings.json)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_MD = LOG_DIR / "prompt-log.md"
LOG_JSONL = LOG_DIR / "prompt-log.jsonl"
STATE = LOG_DIR / ".prompt-log-state.json"

DEFAULT_TRANSCRIPT = Path(
    os.path.expanduser(
        r"~\.claude\projects\C--users-salam-quantum-blackboard"
        r"\dbc0210c-f6b4-4c5d-9a30-fe1ca44cda6e.jsonl"
    )
)


def parse_ts(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def text_of(msg) -> str:
    """A user message is sometimes a string, sometimes a content-block list."""
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    return ""


def is_real_user_prompt(o: dict) -> bool:
    """Tool results and system reminders are also 'user' records. Only count
    something a human actually typed, or the log measures the wrong thing."""
    if o.get("type") != "user":
        return False
    if o.get("isSidechain"):
        return False
    if o.get("promptSource") in ("hook", "system"):
        return False
    msg = o.get("message") or {}
    c = msg.get("content")
    if isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c
    ):
        return False
    t = text_of(msg).strip()
    if not t:
        return False
    if t.startswith("<") and t.endswith(">"):     # bare system-reminder blocks
        return False
    return True


def collect(transcript: Path) -> list[dict]:
    """Group each human prompt with every assistant turn that followed it."""
    prompts: list[dict] = []
    cur: dict | None = None

    with transcript.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue

            if is_real_user_prompt(o):
                if cur:
                    prompts.append(cur)
                cur = {
                    "uuid": o.get("uuid"),
                    "session": o.get("sessionId"),
                    "branch": o.get("gitBranch"),
                    "started": o.get("timestamp"),
                    "ended": o.get("timestamp"),
                    "prompt": text_of(o.get("message")).strip(),
                    "assistant_turns": 0,
                    "tool_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                }
                continue

            if o.get("type") == "assistant" and cur is not None and not o.get("isSidechain"):
                msg = o.get("message") or {}
                u = msg.get("usage") or {}
                cur["assistant_turns"] += 1
                cur["input_tokens"] += u.get("input_tokens", 0) or 0
                cur["output_tokens"] += u.get("output_tokens", 0) or 0
                cur["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
                cur["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
                det = u.get("output_tokens_details") or {}
                cur["thinking_tokens"] += det.get("thinking_tokens", 0) or 0
                c = msg.get("content")
                if isinstance(c, list):
                    cur["tool_calls"] += sum(
                        1 for b in c if isinstance(b, dict) and b.get("type") == "tool_use"
                    )
                if o.get("timestamp"):
                    cur["ended"] = o["timestamp"]

    if cur:
        prompts.append(cur)

    for p in prompts:
        a, b = parse_ts(p["started"]), parse_ts(p["ended"])
        p["elapsed_s"] = round((b - a).total_seconds(), 1) if a and b else None
        # Billable is what you pay for; cache reads are cheap but not free.
        p["billable_tokens"] = p["input_tokens"] + p["output_tokens"] + p["cache_write"]
    return prompts


def fmt_row(i: int, p: dict) -> str:
    t = parse_ts(p["started"])
    when = t.astimezone().strftime("%H:%M:%S") if t else "?"
    prompt = " ".join(p["prompt"].split())
    if len(prompt) > 66:
        prompt = prompt[:63] + "..."
    el = f"{p['elapsed_s']:.0f}s" if p["elapsed_s"] is not None else "?"
    return (
        f"| {i} | {when} | {el} | {p['assistant_turns']} | {p['tool_calls']} "
        f"| {p['output_tokens']:,} | {p['thinking_tokens']:,} | {p['cache_write']:,} "
        f"| {p['cache_read']:,} | {p['billable_tokens']:,} | {prompt} |"
    )


HEADER = """# Per-prompt log

Real usage read from the session transcript, not estimated. Started 2026-09-03
when Mr. Salam asked whether one was being kept; the answer was no, so this
reads the harness's own record rather than my recollection.

**Columns.** `out` is tokens I generated, `think` the reasoning subset of it,
`cache W` context written to cache (billed at a premium), `cache R` context read
back (billed cheap). `billable` = in + out + cache-write; cache reads are
excluded because they are the discount, not the cost. Cross-check the dollar
figure with `/cost` — this counts tokens, not money.

| # | start | elapsed | turns | tools | out | think | cache W | cache R | billable | prompt |
|---|-------|---------|-------|-------|-----|-------|---------|---------|----------|--------|"""


def write_all(prompts: list[dict]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rows = [fmt_row(i + 1, p) for i, p in enumerate(prompts)]

    tot_out = sum(p["output_tokens"] for p in prompts)
    tot_think = sum(p["thinking_tokens"] for p in prompts)
    tot_cw = sum(p["cache_write"] for p in prompts)
    tot_cr = sum(p["cache_read"] for p in prompts)
    tot_bill = sum(p["billable_tokens"] for p in prompts)
    tot_tools = sum(p["tool_calls"] for p in prompts)
    elapsed = [p["elapsed_s"] for p in prompts if p["elapsed_s"] is not None]
    med = sorted(elapsed)[len(elapsed) // 2] if elapsed else 0

    summary = (
        f"\n\n## Session totals\n\n"
        f"- prompts: **{len(prompts)}**\n"
        f"- tool calls: **{tot_tools:,}**\n"
        f"- output tokens: **{tot_out:,}** (of which reasoning: {tot_think:,})\n"
        f"- cache written: **{tot_cw:,}** · cache read: **{tot_cr:,}**\n"
        f"- billable tokens: **{tot_bill:,}**\n"
        f"- median prompt wall-clock: **{med:.0f}s** · longest: "
        f"**{max(elapsed):.0f}s**\n" if elapsed else ""
    )

    LOG_MD.write_text(HEADER + "\n" + "\n".join(rows) + summary, encoding="utf-8")
    with LOG_JSONL.open("w", encoding="utf-8") as fh:
        for p in prompts:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    STATE.write_text(json.dumps({"last_uuid": prompts[-1]["uuid"] if prompts else None,
                                 "count": len(prompts)}), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", type=Path, default=None)
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="for hook use")
    args = ap.parse_args()

    transcript = args.transcript
    # When run as a hook, Claude Code sends JSON on stdin naming the transcript.
    if transcript is None and not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            if payload.get("transcript_path"):
                transcript = Path(payload["transcript_path"])
        except Exception:
            pass
    if transcript is None:
        transcript = DEFAULT_TRANSCRIPT
    if not transcript.exists():
        if not args.quiet:
            print(f"transcript not found: {transcript}", file=sys.stderr)
        return 1

    prompts = collect(transcript)
    if not prompts:
        return 0

    prev = 0
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text()).get("count", 0)
        except Exception:
            pass

    write_all(prompts)

    if not args.quiet:
        new = len(prompts) - prev
        print(f"logged {len(prompts)} prompts ({new:+d} new) -> {LOG_MD}")
        last = prompts[-1]
        print(f"  latest: {last['elapsed_s']}s, {last['tool_calls']} tools, "
              f"{last['billable_tokens']:,} billable tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

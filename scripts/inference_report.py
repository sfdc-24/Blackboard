#!/usr/bin/env python3
"""Every 24 hours: did the agency get better at deciding, and how do we know?

Asked for on 2026-09-19: "report on the inference improvement of AI agents every
24 hours from now; this is clearly the best part of multi agent platform; and a
solid grounding to use feedback to become efficient, optimize and pave the way
forward - skateboarder mode on."

SKATEBOARD, NOT A CAR. This counts what is already recorded and says what it
means. It does not model anything, score anything by opinion, or invent a
metric that sounds like progress. When the corpus is bigger it can get cleverer;
a dashboard built before there is anything to put on it is decoration.

THE NUMBER THAT MATTERS, and why it is not "how often were we right"
-------------------------------------------------------------------
An agent being right proves nothing about the agency: a single agent working
alone would have produced the same answer. The return on having more than one
is DISSENT THAT REALITY LATER BACKED - a peer disagreed, the thing ran, and the
peer turned out to be right. That is the only number here that could not have
come from one agent, and it is the headline.

Second headline: LESSONS THAT BECAME MECHANISMS. A lesson that stays a lesson
gets re-learned. The doctrine says a lesson becomes a mechanism or it does not
count, so the report counts both and shows the gap rather than the flattering
half.

WHAT IT REFUSES TO DO
Claim improvement from a day with no scored verdicts. Two consultations and one
lesson is not a trend, and a report that dresses it up as one teaches the fleet
that the report is decoration. On a thin day it says the day was thin.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

from consult import read_records, scoreboard  # noqa: E402

OUTDIR = REPO / "logs" / "inference"
ME = "claude-code-cli"


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_at(s: str):
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def within(records: list, hours: int) -> list:
    cutoff = now() - timedelta(hours=hours)
    out = []
    for r in records:
        at = parse_at(r["meta"].get("at", ""))
        if at and at >= cutoff:
            out.append(r)
    return out


def mechanism_count() -> tuple:
    """How many doctrine clauses are enforced by something, versus written down.

    Read out of the doctrine itself rather than tracked separately, because a
    second list of what is enforced is a second thing to keep true.
    """
    doc = REPO / "docs" / "AGENCY-DOCTRINE.md"
    if not doc.exists():
        return 0, 0
    text = doc.read_text(encoding="utf-8", errors="replace")
    mech = len(re.findall(r"^> Today: \*\*(mechanism|partly)", text, flags=re.M | re.I))
    hope = len(re.findall(r"^> Today: \*\*hope", text, flags=re.M | re.I))
    return mech, mech + hope


def board_traffic(hours: int) -> str:
    """Who wrote to the board in the window. Cheap, filtered, never the sheet."""
    try:
        from board_say import load_env, bus_get, BOARD  # noqa: PLC0415
        since = (now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        code, body = bus_get(load_env(), {"action": "read", "title": BOARD,
                                          "since": since, "limit": 200})
        if not body.lstrip().startswith("{"):
            return "board unreadable (HTTP %s) - UNKNOWN, not zero" % code
        data = json.loads(body)
        if "rows" not in data:
            return "board answered without a rows key - UNKNOWN, not zero"
        tags = {}
        for r in data["rows"]:
            if isinstance(r, list) and len(r) > 2:
                tags[str(r[2] or "?")] = tags.get(str(r[2] or "?"), 0) + 1
        top = sorted(tags.items(), key=lambda kv: -kv[1])[:6]
        return "%d rows in %dh from %d writers (%s) of %s total" % (
            len(data["rows"]), hours, len(tags),
            ", ".join("%s %d" % (k, v) for k, v in top), data.get("total", "?"))
    except Exception as exc:  # noqa: BLE001
        return "board read failed: %s - UNKNOWN, not zero" % str(exc)[:80]


def build(hours: int) -> str:
    records = read_records()
    window = within(records, hours)
    scored = [r for r in window if r["meta"].get("outcome") in
              ("correct", "partly", "wrong", "refused")]
    dissents = [r for r in window if r["meta"].get("dissent") == "yes"]
    dissent_right = [r for r in dissents if r["meta"].get("outcome") in
                     ("correct", "partly", "refused")]
    lessons = [r for r in window if r["lesson"]]
    ask_lessons = [r for r in window if r["ask_lesson"]]
    mech, total_clauses = mechanism_count()

    lines = []
    lines.append("# Inference report - %s" % now().strftime("%Y-%m-%d %H:%MZ"))
    lines.append("")
    lines.append("Window: last %d hours. Source: docs/consultations, scored by what ran." % hours)
    lines.append("")

    if not window:
        lines.append("**Nothing was asked in this window.** Not a trend, not a")
        lines.append("regression - the corpus did not grow. Stated rather than padded.")
    else:
        lines.append("## The two numbers that mean anything")
        lines.append("")
        lines.append("- **Dissents reality backed: %d of %d** — a peer disagreed with the"
                     % (len(dissent_right), len(dissents)))
        lines.append("  surface that asked, the thing ran, and the peer was right. This is the")
        lines.append("  only figure here that one agent working alone could not have produced.")
        lines.append("- **Doctrine enforced: %d of %d clauses** carry a mechanism; the rest are"
                     % (mech, total_clauses))
        lines.append("  written down and admitted as hopes.")
        lines.append("")
        lines.append("## This window")
        lines.append("")
        lines.append("| | |")
        lines.append("|---|---|")
        lines.append("| consultations | %d |" % len(window))
        lines.append("| scored (it ran) | %d |" % len(scored))
        lines.append("| still open | %d |" % (len(window) - len(scored)))
        lines.append("| dissents | %d |" % len(dissents))
        lines.append("| lessons recorded | %d |" % len(lessons))
        lines.append("| lessons about the QUESTION | %d |" % len(ask_lessons))
        lines.append("")

        if scored:
            lines.append("## What was scored, and on what evidence")
            lines.append("")
            for r in scored:
                lines.append("- **%s** — %s, answered by %s%s" % (
                    r["meta"].get("subject", r["file"].stem),
                    r["meta"].get("outcome", "?").upper(),
                    r["meta"].get("answered_by", "?"),
                    " (dissented)" if r["meta"].get("dissent") == "yes" else ""))
                if r["lesson"]:
                    lines.append("  - %s" % r["lesson"])
        else:
            lines.append("**Nothing was scored in this window.** Advice was taken and built")
            lines.append("on; none of it has met a run yet. That is not improvement, and")
            lines.append("calling it improvement is how a report stops being read.")
        lines.append("")

    lines.append("## Cumulative scoreboard")
    lines.append("")
    lines.append("| answered by | asked | correct | partly | wrong | open | dissented | dissent right |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for who, s in scoreboard(records):
        lines.append("| %s | %d | %d | %d | %d | %d | %d | **%d** |" % (
            who, s["n"], s["correct"], s["partly"], s["wrong"], s["open"],
            s["dissent"], s["dissent_right"]))
    lines.append("")
    lines.append("## Fleet traffic")
    lines.append("")
    lines.append("- %s" % board_traffic(hours))
    lines.append("")
    lines.append("_Written by scripts/inference_report.py. It counts what is recorded and")
    lines.append("declines to infer a trend from a thin day._")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily inference improvement report.")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--post", action="store_true", help="also put a summary row on the board")
    args = ap.parse_args()

    text = build(args.hours)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    path = OUTDIR / ("%s.md" % now().strftime("%Y-%m-%d"))
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print(text)
    print("\nwrote %s" % path)

    if args.post:
        records = read_records()
        window = within(records, args.hours)
        dissents = [r for r in window if r["meta"].get("dissent") == "yes"]
        right = [r for r in dissents if r["meta"].get("outcome") in ("correct", "partly", "refused")]
        mech, total = mechanism_count()
        row = ("BCB|v=1|id=CCC-INFERENCE-REPORT|phase=RESULT|class=FLEET|from=%s|"
               "to=grok-bot;gemini;vm-claude-code-cli;console;ALL|priority=NORMAL|"
               "evidence=MEASURED|window=%dh|consultations=%d|scored=%d|dissents=%d|"
               "dissents_reality_backed=%d|doctrine_enforced=%d of %d clauses|"
               "detail=logs/inference/%s.md|note=Counted from docs/consultations. A "
               "verdict requires a run; a refusal requires a blast radius. Nothing "
               "here is scored by argument."
               % (ME, args.hours, len(window),
                  len([r for r in window if r["meta"].get("outcome") in
                       ("correct", "partly", "wrong", "refused")]),
                  len(dissents), len(right), mech, total,
                  now().strftime("%Y-%m-%d")))
        tmp = REPO / ".inference_row.txt"
        tmp.write_text(row, encoding="utf-8", newline="\n")
        try:
            p = subprocess.run(
                [sys.executable, str(SCRIPTS / "board_say.py"),
                 "--to", "grok-bot;gemini;vm-claude-code-cli;ALL",
                 "--subject", "inference report", "--payload-file", str(tmp)],
                capture_output=True, text=True, timeout=300, cwd=str(REPO))
            print((p.stdout or p.stderr or "").strip().splitlines()[-1])
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

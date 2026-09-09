#!/usr/bin/env python3
"""Mutation control for tests/test_mcp_plan_consistency.py.

A passing suite says "my tests passed", not "my tests would have caught the
defect". Those are different claims, and on this repository they have come apart
repeatedly — a guard shipped green and inert twice on the site, and a positive
control passed for the wrong reason because it exercised a component the guard
had stopped calling.

So each case below reintroduces one defect a reviewer actually found in
docs/SALESFORCE-MCP-PLAN.md and requires the suite to FAIL. The three marked
`round 3` are verbatim: the document was in exactly that state and every test I
had at the time passed, because there were none.

Every mutation verifies it applied — a replacement whose anchor no longer
matches changes nothing, the suite passes, and this would report a healthy guard
as dead. The original is restored on every exit path including signals; an
earlier harness of mine was interrupted and left deliberately broken source on
disk.

Run:  python tests/mutate_mcp_plan.py
"""
from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLAN = REPO / "docs" / "SALESFORCE-MCP-PLAN.md"

MUTATIONS: list[dict[str, str]] = [
    {
        "name": "round 3: the worked example supplies 9 of the 17 fields",
        "old": "| `numerator` | interviews whose final status is Error |\n",
        "new": "",
        "expect": "same_fields",
    },
    {
        "name": "round 3: an `unknown` value outside the closed set",
        "old": "| `unknown` | one of `insufficient-permission`, `query-failed`, "
               "`not-reached`, `no-data-in-window` — never a rate of 0 |",
        "new": "| `unknown` | `insufficient-permission` or `no-interviews-in-window` |",
        "expect": "closed_set",
    },
    {
        "name": "round 3: a stated count the table contradicts",
        "old": "A signal is not a metric until all 17 are written down.",
        "new": "A signal is not a metric until seven fields are written down.",
        "expect": "stated_field_count",
    },
    {
        "name": "round 1: the original seven-versus-nine miscount",
        "old": "The list below is 17, and that number comes from counting the rows",
        "new": "The list below is nine fields, and that number comes from counting the rows",
        "expect": "stated_field_count",
    },
    {
        "name": "structural: a gate with no acceptance condition",
        "old": "| **G7** sanitizer |",
        "new": "| **G7** sanitizer | | | |\n| **G7b** leftover |",
        "expect": "gate_names",
    },
    {
        "name": "the worked example claims everything is resolved",
        "old": "| `threshold` | UNRESOLVED",
        "new": "| `threshold` | 500",
        "expect": "unresolved_items",
    },
]

ORIGINAL = PLAN.read_text(encoding="utf-8")


def restore() -> None:
    if PLAN.read_text(encoding="utf-8") != ORIGINAL:
        PLAN.write_text(ORIGINAL, encoding="utf-8", newline="")


def _on_signal(signum, _frame):  # noqa: ANN001
    restore()
    sys.exit(130)


for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, _on_signal)


def main() -> int:
    failures = 0
    print(f"\nmutation control — {len(MUTATIONS)} cases\n")

    for m in MUTATIONS:
        text = PLAN.read_text(encoding="utf-8")
        if m["old"] not in text:
            print(f"  ANCHOR LOST  {m['name']}")
            print("               the text to mutate is gone; fix the anchor, a "
                  "mutation that cannot apply proves nothing")
            failures += 1
            continue

        mutated = text.replace(m["old"], m["new"], 1)
        if mutated == text:
            print(f"  NO-OP        {m['name']}")
            failures += 1
            continue

        PLAN.write_text(mutated, encoding="utf-8", newline="")
        try:
            run = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_mcp_plan_consistency"],
                cwd=REPO, capture_output=True, text=True, timeout=120,
            )
            out = run.stdout + run.stderr
            failed_right = run.returncode != 0 and m["expect"] in out
            if failed_right:
                print(f"  caught       {m['name']}")
            elif run.returncode != 0:
                print(f"  WRONG TEST   {m['name']}")
                print(f"               failed, but not on {m['expect']}")
                failures += 1
            else:
                print(f"  NOT CAUGHT   {m['name']}")
                print("               the suite stayed GREEN with the defect back")
                failures += 1
        finally:
            restore()

    print(f"\n{len(MUTATIONS) - failures}/{len(MUTATIONS)} mutations caught\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        restore()

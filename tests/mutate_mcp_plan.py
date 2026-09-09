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

Run:  python tests/mutate_mcp_plan.py            (from the repository root)

Verified on Python 3.11, 3.12 and 3.14. An earlier version invoked the suite as
`python -m unittest tests.test_mcp_plan_consistency`, which fails on 3.12 for
every module in tests/ because there is no __init__.py - so it caught 0 of 6
there and said nothing was wrong. It now uses the discovery form the rest of
this repository uses.
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
        # Re-anchored 2026-09-09 when the gates became blocks rather than table
        # rows. The old anchor targeted a four-column row — and that shape was
        # itself the defect: a test of mine asserted len(cells) == 4, which
        # prevented the prerequisite, acceptor, evidence, fail-closed and
        # unlocks fields a reviewer had asked for three times.
        "name": "structural: a gate stops stating its fail-closed behaviour",
        "old": "- **fail-closed** — **no approval, no publication.**",
        "new": "- **note** — approval is usually obtained.",
        "expect": "states_all_of_its_required_fields",
    },
    {
        "name": "structural: the client-org gate stops naming its prerequisites",
        "old": "**G1, G2, G3, G4, G5, G6 and G7 accepted, and G8 shipped.**",
        "new": "**G1-G7 accepted, and G8 shipped.**",
        "expect": "pins_its_prerequisites_by_name",
    },
    {
        "name": "the worked example claims everything is resolved",
        "old": "| `threshold` | UNRESOLVED",
        "new": "| `threshold` | 500",
        "expect": "unresolved_items",
    },
    # The two below are chatgpt-codex-desktop-01a0839e's, verbatim. They were
    # NOT CAUGHT when they demonstrated them, and that was the whole point: every
    # other case here checks the document against itself, while these two check
    # the only claim that actually restrains anything.
    {
        "name": "the hold is lifted by editing the document that records it",
        "old": "> **STATUS: proposed work plan. HELD. This document authorises nothing.**",
        "new": "> **STATUS: approved executable architecture. Client scans authorised.**",
        "expect": "status_block_still_holds",
    },
    {
        "name": "the client-org gate authorises itself",
        "old": "**acceptor** — **Mr. Salam authorises.** A person, in the",
        "new": "**acceptor** — **claude-code-cli authorises.** An agent, in the",
        "expect": "client_org_gate_is_not_self_authorised",
    },
]

# BYTES, not text. read_text/write_text translate line endings, so on a CRLF
# checkout — which is what a canonical Windows clone gives — restoring rewrote
# every line and left the file modified in `git status` with a different digest.
# A harness that cannot put the file back exactly as it found it is a harness
# that edits your repository as a side effect of checking it. Reproduced by
# converting a copy of the tree to CRLF and comparing sha256 before and after.
ORIGINAL = PLAN.read_bytes()


def restore() -> None:
    if PLAN.read_bytes() != ORIGINAL:
        PLAN.write_bytes(ORIGINAL)


def _on_signal(signum, _frame):  # noqa: ANN001
    restore()
    sys.exit(130)


for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, _on_signal)


def main() -> int:
    failures = 0
    print(f"\nmutation control — {len(MUTATIONS)} cases\n")

    for m in MUTATIONS:
        raw = PLAN.read_bytes()
        # Work in LF internally, remember the file's real convention, and put it
        # back on write. An anchor written with "\n" does not match a CRLF
        # checkout, so on Windows this scored 5/6 and reported ANCHOR LOST on a
        # document that was fine — the same failure the site's JS harness had,
        # which I fixed there and did not carry across. A gate that cries wolf
        # on one platform is a gate people switch off.
        was_crlf = b"\r\n" in raw
        text = raw.decode("utf-8").replace("\r\n", "\n")
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

        # Preserve the file's existing line-ending convention exactly.
        out = mutated.replace("\n", "\r\n") if was_crlf else mutated
        PLAN.write_bytes(out.encode("utf-8"))
        try:
            run = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", "discover",
                 "-s", "tests", "-p", "test_mcp_plan_consistency.py"],
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

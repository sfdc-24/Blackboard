#!/usr/bin/env python3
"""Rewrite each gate's generated contract region from its typed block.

The contract bullets in docs/SALESFORCE-MCP-PLAN.md are OUTPUT, not source. Run
this after changing a gate block; `test_the_contract_prose_is_exactly_what_the
_block_generates` compares the file against what this produces.

Why generation rather than a stricter parser: round ten of PR45 walked past a
parser that read the LEADING gate list of each bullet and discarded the rest of
the sentence, so `- **unlocks** - G9. In practice G8 gates nothing.` parsed to
{G9} and passed. Any parser has a remainder. Generated text has none, because
nobody writes it.

Run:  python tests/render_contracts.py          (rewrites in place)
      python tests/render_contracts.py --check  (exit 1 if it would change)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_graph as gg  # noqa: E402

PLAN = Path(__file__).resolve().parents[1] / "docs" / "SALESFORCE-MCP-PLAN.md"

REGION = re.compile(
    re.escape(gg.CONTRACT_BEGIN) + r".*?" + re.escape(gg.CONTRACT_END),
    re.DOTALL,
)


def main() -> int:
    raw = PLAN.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")

    gates = gg.parse(text)
    out = text
    rewritten = 0
    for gid, gate in sorted(gates.items()):
        start = out.index(f"### {gid} ")
        end = out.find("\n### ", start + 1)
        if end == -1:
            end = len(out)
        section = out[start:end]
        want = gg.render_contract(gate)
        if not REGION.search(section):
            print(f"{gid}: no generated region — refusing to guess where it goes")
            return 1
        new_section = REGION.sub(lambda _m: want, section, count=1)
        if new_section != section:
            rewritten += 1
            out = out[:start] + new_section + out[end:]

    if "--check" in sys.argv:
        if out != text:
            print(f"{rewritten} gate contract(s) are stale. Run without --check.")
            return 1
        print("every gate contract matches its block")
        return 0

    if out != text:
        PLAN.write_bytes(out.replace("\n", newline).encode("utf-8"))
    print(f"rewrote {rewritten} contract region(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

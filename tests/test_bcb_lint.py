#!/usr/bin/env python3
"""Tests for scripts/bcb_lint.py against the shape the LIVE board returns.

Measured 2026-09-09 on "Blackboard - Alpha DB": all 1883 rows are 12 cells
wide and the header is the ten named columns plus two empty padding cells.
Google Sheets returns rows at the width of the sheet's USED range, so a board
widened past its named columns pads every row. Before the padding fix the
linter called all 1882 data rows MALFORMED, which buried the 322 rows that had
real findings - the same width-10 intolerance that took the ORDER worker down
with board_header_invalid and that PR46 closed for it.

Run: python3 tests/test_bcb_lint.py
"""
import importlib.util
import io
import json
import os
import contextlib
import tempfile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LINT = os.path.join(HERE, "..", "scripts", "bcb_lint.py")

spec = importlib.util.spec_from_file_location("bcb_lint", LINT)
bcb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bcb)

PASS, FAIL, FAILURES = 0, 0, []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name}  {detail}")


# A row that is valid under the ten-column schema.
GOOD10 = [
    "a5e892f6-07fe-4af9-b22f-9d38e8afc8b5",
    "2026-09-09T16:00:00.000Z",
    "claude-code-cli",
    "ALL",
    "APPEND",
    "BCB|v=1|id=TEST-001|phase=RESULT|from=claude-code-cli|to=ALL",
    "OPEN",
    "SFDC24",
    "a gist",
    "a sub-gist",
]


def width_findings(row):
    malformed, _ = bcb.validate_row(0, row)
    return [m for m in malformed if "column" in m]


print("== row width: padding is transport, content is a defect ==")
check("clean 10-cell row has no width finding", width_findings(GOOD10) == [])
check("12-cell row with EMPTY padding has no width finding",
      width_findings(GOOD10 + ["", ""]) == [],
      f"got {width_findings(GOOD10 + ['', ''])}")
check("12-cell row with whitespace-only padding has no width finding",
      width_findings(GOOD10 + [" ", "\t"]) == [])
# Column L (12th, index 11) is bus.py's SENDER_COLUMN - a sender=... stamp has been
# real, intentional content there since 2026-11-03. Tolerated now, not malformed.
f = width_findings(GOOD10 + ["", "sender=verified;who=x@sfdc24.iam.gserviceaccount.com"])
check("a sender stamp in column L (12) is NOT malformed", f == [], f"got {f}")
# Column K (11th, index 10) still has nothing writing to it - content there is still
# a genuine surprise.
f = width_findings(GOOD10 + ["leaked", ""])
check("content in column K (11), the one still-unused padding cell, is still malformed",
      len(f) == 1 and "11" in f[0], f"got {f}")
# Columns M-R (13th-18th, index 12-17) are the R-STATUS Status columns, live since
# 2026-11-14 - tolerated the same way as L.
status_cols = ["", "sender=unverified", "OPEN", "grok", "ack: owner GO", "2026-11-15T00:00:00Z", "", ""]
f = width_findings(GOOD10 + status_cols)
check("R-STATUS columns M-R are NOT malformed", f == [], f"got {f}")
# Content past R (19th, index 18) is still a genuine schema surprise - the tolerance
# is bounded, not open-ended.
f = width_findings(GOOD10 + status_cols + ["leaked"])
check("content past column R (19) is still malformed, and names column 19",
      len(f) == 1 and "19" in f[0], f"got {f}")
f = width_findings(GOOD10[:9])
check("a SHORT row is still malformed (REQ-B4TQX9 shift class)",
      len(f) == 1 and "expected 10" in f[0], f"got {f}")

print("== the padded row still gets its real grammar checked ==")
shifted = ["2026-09-09T16:00:00.000Z"] + GOOD10[1:] + ["", ""]
m, _ = bcb.validate_row(0, shifted)
check("a timestamp in Row_ID is caught even when the row is padded",
      any("Row_ID" in x or "timestamp" in x.lower() for x in m), f"got {m}")

print("== header: trailing blanks are padding, a rename is not ==")


def run_main(rows):
    dump = {"ok": True, "title": "Blackboard - Alpha DB", "rows": rows}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(dump, fh)
    argv = sys.argv
    sys.argv = ["bcb_lint.py", "--file", path]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = bcb.main()
    finally:
        sys.argv = argv
        os.unlink(path)
    return rc, buf.getvalue()


padded_header = bcb.COLUMNS + ["", ""]
rc, out = run_main([padded_header, GOOD10 + ["", ""]])
check("padded header raises NO schema NOTE", "differs from expected schema" not in out,
      out.strip()[:200])
check("a clean padded board lints green (rc 0, 0 malformed)",
      rc == 0 and "MALFORMED rows     : 0" in out, out.strip()[-300:])

rc, out = run_main([bcb.COLUMNS[:-1] + ["Renamed", "", ""], GOOD10 + ["", ""]])
check("a RENAMED column still raises the schema NOTE",
      "differs from expected schema" in out, out.strip()[:200])

rc, out = run_main([padded_header, GOOD10 + ["leaked", ""]])
check("a board whose padding carries content outside the tolerated range lints RED",
      rc == 1 and "MALFORMED rows     : 1" in out, out.strip()[-300:])

print("== R-STATUS Rule 1: ASK-type rows need expect_from=/expect=/done_when=/due= (warn-only) ==")


def warnings_for(row):
    _malformed, warnings = bcb.validate_row(0, row)
    return [w for w in warnings if w.startswith("R-STATUS:")]


ASK_ROW = GOOD10[:5] + [
    "BCB|v=1|id=TEST-ASK|phase=ASK|from=claude-code-cli|to=grok|expect_from=grok|"
    "expect=result|done_when=the fix ships|due=2026-11-15T00:00:00Z"
] + GOOD10[6:]
check("a complete ASK row raises no R-STATUS warning", warnings_for(ASK_ROW) == [],
      f"got {warnings_for(ASK_ROW)}")

BARE_ASK_ROW = GOOD10[:5] + ["BCB|v=1|id=TEST-ASK2|phase=ASK|from=claude-code-cli|to=grok"] + GOOD10[6:]
w = warnings_for(BARE_ASK_ROW)
check("an ASK row missing all four fields gets exactly one R-STATUS warning naming all four",
      len(w) == 1 and all(k in w[0] for k in ("expect_from=", "expect=", "done_when=", "due=")),
      f"got {w}")

PARTIAL_ASK_ROW = GOOD10[:5] + [
    "BCB|v=1|id=TEST-ASK3|phase=ASK|from=claude-code-cli|to=grok|expect_from=grok|expect=result"
] + GOOD10[6:]
w = warnings_for(PARTIAL_ASK_ROW)
# "missing done_when=, due=" - check the exact missing-field list, not a substring of the
# whole sentence ("expect=" is itself a substring of "expect_from=").
missing_list = w[0].split("missing ", 1)[1].split(" (warn-only")[0] if w else ""
check("an ASK row missing only done_when= and due= names exactly those two, nothing else",
      len(w) == 1 and missing_list == "done_when=, due=", f"got {w!r} / missing={missing_list!r}")

for phase in ("RESULT", "ACK", "NOTE", "DONE"):
    row = GOOD10[:5] + [f"BCB|v=1|id=TEST-{phase}|phase={phase}|from=claude-code-cli|to=grok"] + GOOD10[6:]
    w = warnings_for(row)
    check(f"a {phase} row (not ask-type) never gets the R-STATUS warning", w == [], f"got {w}")

print()
print(f"{PASS} passed, {FAIL} failed")
for name in FAILURES:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)

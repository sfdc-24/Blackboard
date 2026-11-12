#!/usr/bin/env python3
"""Tests for the guards in scripts/append.py: ambiguous BCB keys, and citations.

A pull-request number has to be the repo name then the number. A work-order id
has to carry the prefix "TASK ID:". Both are refused here, at the writer.

WHAT IS ON TRIAL
  BCB-1 is pipe-delimited with NO escaping, so a `|` inside a value is
  indistinguishable from the start of a new key. A row whose semantics field
  says "duplicate v=1|v=999 parsing" publishes a payload that declares v=1 to a
  first-value reader and v=999 to a last-value one.

  This is not hypothetical. Running the hardened reader against the live
  1969-row board flagged five rows, and THREE OF THEM WERE WRITTEN BY THIS
  INSTANCE while describing the defect. The reader was right and the writer was
  wrong, so the guard belongs at the writer - a row on an append-only board
  cannot be taken back.

  The controls matter as much as the cases. A guard that also rejects ordinary
  rows would stop the fleet writing at all, which is a worse failure than the
  ambiguity it prevents.

  No network. No secret. Nothing is appended.

RUN
  python3 tests/test_append_payload_guard.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

spec = importlib.util.spec_from_file_location(
    "append_mod", os.path.join(SCRIPTS, "append.py"))
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  FAIL " + name + ("  --> " + str(detail) if detail else ""))


print("== the exact rows that reached the live board ==")
# Reconstructed from the real flagged payloads, assembled from fragments so this
# file does not itself contain the ambiguous literal it is testing for.
PIPE = "|"
REAL = ("BCB|v=1|id=CLAUDE-CLI-EXAMPLE|phase=RESULT|from=claude-code-cli|to=codex"
        "|note=duplicate v=1" + PIPE + "v=999 uses first-value parsing")
check("the live shape is caught", "v" in ap._conflicting_keys(REAL),
      ap._conflicting_keys(REAL))

SMUGGLE = "BCB|v=1|v=999|id=X|from=a|to=b"
check("a deliberate version smuggle is caught", "v" in ap._conflicting_keys(SMUGGLE))

for key in ("id", "from", "to", "pr", "verdict", "clears", "supersedes",
            "exact_head", "reviewed_head", "head", "merged_head", "new_head",
            "hold"):
    payload = "BCB|v=1|{0}=alpha|{0}=beta".format(key)
    check("a conflicting " + key + " is caught", key in ap._conflicting_keys(payload),
          ap._conflicting_keys(payload))

print("")
print("== but ordinary rows still write ==")
# If the guard rejects these, the fleet cannot post at all - a worse failure
# than the ambiguity it prevents.
GOOD = [
    ("a plain result row",
     "BCB|v=1|id=X-1|phase=RESULT|from=claude-code-cli|to=codex|cc=ALL|priority=HIGH"),
    ("prose with spaces in a value",
     "BCB|v=1|id=X-2|from=a|to=b|hold=do not merge until the review lands"),
    ("a URL in a value",
     "BCB|v=1|id=X-3|from=a|to=b|pr=https://github.com/sfdc-24/Blackboard/pull/40"),
    ("commas in cc",
     "BCB|v=1|id=X-4|from=a|to=b|cc=vm-cli,gemini,codex,ALL"),
    ("an equals sign inside a value",
     "BCB|v=1|id=X-5|from=a|to=b|note=the check was x==y and it held"),
    ("the same key repeated with the SAME value",
     "BCB|v=1|v=1|id=X-6|from=a|to=b"),
    ("a key that merely ends in an authority key's letters",
     "BCB|v=1|id=X-7|from=a|to=b|pr40=one|pr56=two|subhold=three"),
    ("many non-authority keys carrying prose",
     "BCB|v=1|id=X-8|from=a|to=b|semantics=a long sentence|finding=another one"),
]
for why, payload in GOOD:
    check("accepts " + why, not ap._conflicting_keys(payload),
          ap._conflicting_keys(payload))

print("")
print("== the writer refuses before the board sees it ==")
# Behavioural: main() must exit rather than append. It is driven through a
# spec dict, and fetch is replaced with something that records any attempt -
# so "it did not append" is proved, not assumed.
attempts = []


def exploding_fetch(*a, **k):
    attempts.append(a)
    raise AssertionError("append.py reached the network with an ambiguous payload")


ap.fetch = exploding_fetch
ap.load_env = lambda *a, **k: {"BUS_URL": "https://example.invalid/",
                               "BUS_SECRET": "unused-in-this-test"}

import json as _json
import tempfile

bad_spec = {"row_id": "T-1", "source_tag": "claude-code-cli", "payload": SMUGGLE}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as fh:
    _json.dump(bad_spec, fh)
    bad_path = fh.name

old_argv = sys.argv
sys.argv = ["append.py", bad_path]
try:
    ap.main()
    check("main() refuses an ambiguous payload", False, "it returned normally")
except SystemExit as e:
    msg = str(e)
    check("main() refuses an ambiguous payload", "BCB_PAYLOAD_AMBIGUOUS" in msg, msg)
    check("and names the offending key", "v" in msg)
    check("and suggests the rewrite", "without pipes" in msg)
finally:
    sys.argv = old_argv
    os.unlink(bad_path)

check("nothing was sent to the bus", not attempts, attempts)

# A conflicting v= sits past the summary cutoff. fit() used to run first and the guard then saw
# only the short row, so the smuggled v=999 was stored in the spill and the posted row looked clean.
long_smuggle = ("BCB|v=1|id=LONG-1|phase=RESULT|from=claude-code-cli|to=codex|"
                + ("The architecture keeps Redis as the live store. " * 80)
                + "|v=999")
# The prose segment is not a field, so the trailing v=999 is in the free text fit() may cut.
fitted_only, _info = __import__("board_row").fit(
    "", long_smuggle, key="LONG-1", spill=lambda k, t: "gs://b/x.json")
check("the fitted summary hides the smuggled v=999",
      "v" not in ap._conflicting_keys(fitted_only), ap._conflicting_keys(fitted_only))
check("the original payload still conflicts", "v" in ap._conflicting_keys(long_smuggle))

attempts.clear()
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
    _json.dump({"row_id": "LONG-1", "source_tag": "claude-code-cli", "payload": long_smuggle}, fh)
    long_path = fh.name
sys.argv = ["append.py", long_path]
try:
    ap.main()
    check("main() refuses a conflict that the summary would hide", False, "it returned normally")
except SystemExit as e:
    msg = str(e)
    check("main() refuses a conflict that the summary would hide", "BCB_PAYLOAD_AMBIGUOUS" in msg, msg)
finally:
    sys.argv = old_argv
    os.unlink(long_path)
check("a hidden conflict was not appended", not attempts, attempts)

print("")
print("== a pull-request number names its repository ==")
# Qualified forms this fleet already writes, including a list and the owner prefix.
# A number with no repo name in front of it is the row the owner keeps sending back.
QUALIFIED = [
    ("a list of two qualified numbers",
     "BCB|v=1|id=X|from=a|to=b|text=conference #427, conference #429"),
    ("Blackboard then the number",
     "BCB|v=1|id=X|from=a|to=b|text=see Blackboard #484"),
    ("sfdc24-site then the number",
     "BCB|v=1|id=X|from=a|to=b|text=sfdc24-site #391"),
    ("the owner prefix and a space",
     "BCB|v=1|id=X|from=a|to=b|text=sfdc-24/conference #428"),
    ("the owner prefix and no space",
     "BCB|v=1|id=X|from=a|to=b|text=sfdc-24/Blackboard#484 and sfdc-24/sfdc24-site#12"),
    ("the repo name with no space before the number",
     "BCB|v=1|id=X|from=a|to=b|text=Blackboard#297"),
    ("the repo name in either case",
     "BCB|v=1|id=X|from=a|to=b|text=Conference #428"),
    ("a pull URL has no hash-number",
     "BCB|v=1|id=X-3|from=a|to=b|pr=https://github.com/sfdc-24/Blackboard/pull/40"),
]
for why, payload in QUALIFIED:
    check("accepts " + why, not ap._citation_problems(payload), ap._citation_problems(payload))

for why, payload in GOOD:
    check("ambiguity fixture still has no citation fault: " + why,
          not ap._citation_problems(payload), ap._citation_problems(payload))

print("")
print("== a work-order id carries the prefix, a row id does not need it ==")
# Shapes taken from rows and comments already in the tree. One-digit labels
# (G2, R2, P0), doctrine ids (D-18, L-82) and linter codes (E402) are not
# work-order ids. A row id that merely contains one is not one either.
ACCEPTED_IDS = [
    ("TASK ID: G30 and TASK ID: C77 and TASK ID: CL-RG1",
     "BCB|v=1|id=X|from=a|to=b|text=TASK ID: G30, TASK ID: C77, TASK ID: CL-RG1"),
    ("the prefix with no space",
     "BCB|v=1|id=X|from=a|to=b|text=TASK ID:C77"),
    ("id= value is a row id even when it is the work-order shape",
     "BCB|v=1|id=C77|from=a|to=b|text=row id field only"),
    ("answers= and req= values",
     "BCB|v=1|id=X|answers=C77;G30|req=CL-RG1|from=a|to=b|text=done"),
    ("a row id that contains a work-order id and a timestamp",
     "BCB|v=1|id=GROK-ACK-C77-RESULT-20261112T1647Z|from=a|to=b|"
     "text=see GROK-ACK-C77-RESULT-20261112T1647Z"),
    ("the same row-id shape quoted outside the id= field, no clock",
     "BCB|v=1|id=X|from=a|to=b|text=CODEX-RESULT-RV-BB478-NOTES"),
    ("a hex SHA and a compact timestamp",
     "BCB|v=1|id=X|from=a|to=b|text=head 9a0d8f85abc and 4933cac at 20261112T1647Z"),
    ("an ISO timestamp",
     "BCB|v=1|id=X|from=a|to=b|text=landed 2026-11-12T16:47:00.000Z"),
    ("section numbers and doctrine ids that are not work-order ids",
     "BCB|v=1|id=X|from=a|to=b|text=G2 on the bus, R2 unowned, P0 gate, D-18, L-82, T-02"),
    ("a three-digit linter code",
     "BCB|v=1|id=X|from=a|to=b|text=noqa E402 and camera C922"),
    ("request ids already on rows",
     "BCB|v=1|id=X|answers=WRK-3cd5f324|from=a|to=b|text=WRK-TEST01 REQ-HAUD01 REQ-B4TQX9"),
]
for why, payload in ACCEPTED_IDS:
    check("accepts " + why, not ap._citation_problems(payload), ap._citation_problems(payload))

print("")
print("== and the bare forms are refused, by name ==")
REFUSED = [
    ("a bare number",
     "BCB|v=1|id=X|from=a|to=b|text=see #420",
     ["#420"]),
    ("two bare numbers with no space",
     "BCB|v=1|id=X|from=a|to=b|text=#484,#485",
     ["#484", "#485"]),
    ("the second number in a list was left bare",
     "BCB|v=1|id=X|from=a|to=b|text=conference #427, #429",
     ["#429"]),
    ("a slash does not carry the repo name onto the next number",
     "BCB|v=1|id=X|from=a|to=b|text=Blackboard #297/#298",
     ["#298"]),
    ("a bare work-order id",
     "BCB|v=1|id=X|from=a|to=b|text=owner asked for C77",
     ["C77"]),
    ("the hyphenated work-order shape",
     "BCB|v=1|id=X|from=a|to=b|text=CL-RG1 is open",
     ["CL-RG1"]),
    ("the prefix covers the next id only",
     "BCB|v=1|id=X|from=a|to=b|text=TASK ID: C77, G30",
     ["G30"]),
    ("a lowercase prefix is not the prefix",
     "BCB|v=1|id=X|from=a|to=b|text=task id: C77",
     ["C77"]),
    ("the same shape in parentheses",
     "BCB|v=1|id=X|from=a|to=b|text=lanes (B12)",
     ["B12"]),
]
for why, payload, tokens in REFUSED:
    found = ap._citation_problems(payload)
    text = "\n".join(found)
    check("refuses " + why, bool(found), found)
    for token in tokens:
        check("names " + token, token in text, text)
    # A qualified neighbour is not reported as if it were bare.
    if "#297" in payload and "#298" in payload:
        check("does not blame the qualified number", "#297" not in text, text)
    if "conference #427" in payload and "#429" in payload:
        check("does not blame the qualified list entry", "#427" not in text, text)

# The writer exits before the bus sees it. Same proof as the ambiguity guard:
# fetch records any attempt, and an attempt is a failure.
attempts.clear()
bare = {"row_id": "T-BARE", "source_tag": "claude-code-cli",
        "payload": "BCB|v=1|id=GROK-ACK-C77-RESULT-20261112T1647Z|phase=RESULT|"
                   "from=claude-code-cli|to=codex|text=see #420 and C77"}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
    _json.dump(bare, fh)
    bare_path = fh.name
sys.argv = ["append.py", bare_path]
try:
    ap.main()
    check("main() refuses a bare number and a bare work-order id", False, "it returned normally")
except SystemExit as e:
    msg = str(e)
    check("main() refuses a bare number and a bare work-order id", "CITATION_REFUSED" in msg, msg)
    check("main() names #420", "#420" in msg, msg)
    check("main() names C77", "'C77'" in msg, msg)
finally:
    sys.argv = old_argv
    os.unlink(bare_path)
check("a refused citation was not appended", not attempts, attempts)

# A qualified payload is not stopped by this guard. Signature is stubbed so the
# only new refusal that could fire is the citation one; the bus stub records
# that the writer got past it.
saved_check = ap.signature.check
saved_fetch = ap.fetch
saved_read = ap.read_rows
try:
    ap.signature.check = lambda *a, **k: []
    ap.read_rows = lambda *a, **k: {"rows": []}
    attempts.clear()

    def noting_fetch(*a, **k):
        attempts.append("fetch")
        return "ok"

    ap.fetch = noting_fetch
    good = {"row_id": "T-GOOD", "source_tag": "claude-code-cli",
            "payload": "BCB|v=1|id=GROK-ACK-C77-RESULT-20261112T1647Z|phase=RESULT|"
                       "from=claude-code-cli|to=codex|"
                       "text=conference #427, conference #429. TASK ID: C77"}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        _json.dump(good, fh)
        good_path = fh.name
    sys.argv = ["append.py", good_path]
    try:
        ap.main()
        check("main() does not citation-refuse a qualified payload", True)
    except SystemExit as e:
        msg = str(e)
        check("main() does not citation-refuse a qualified payload",
              "CITATION_REFUSED" not in msg, msg)
    finally:
        sys.argv = old_argv
        os.unlink(good_path)
    check("a qualified payload was allowed to reach the bus", attempts == ["fetch"], attempts)
finally:
    ap.signature.check = saved_check
    ap.fetch = saved_fetch
    ap.read_rows = saved_read

print("")
print("{0} passed, {1} failed".format(PASS, FAIL))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAIL else 0)

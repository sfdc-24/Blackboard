#!/usr/bin/env python3
"""Tests for the BCB payload-ambiguity guard in scripts/append.py.

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

print("")
print("{0} passed, {1} failed".format(PASS, FAIL))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAIL else 0)

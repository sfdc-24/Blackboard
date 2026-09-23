#!/usr/bin/env python3
"""Mutation control for tests/test_sf360.py.

WHY THIS EXISTS
---------------
tests/test_sf360.py reports passed=114 failed=0 against the real
scripts/sf360.py. That number is worth nothing on its own: a suite that cannot
fail reports the same thing. This file breaks the rail on purpose, one defect
at a time, and requires the suite to go red ON THE NAMED ASSERTION - not merely
red somewhere, which a mutation tripping an unrelated check would also produce.

The defects chosen are the ones that would actually cost something here:
a CORS allowlist that matches by prefix (so www.sfdc24.com.evil.com reads the
dashboard), a read-only guard that is no longer read-only, a proxy route that
runs caller-supplied SOQL, a token cache that writes the client secret to disk,
an error message that prints it, a silent truncation, and a removed re-auth.

HOW IT AVOIDS TESTING A COPY OF ITSELF
--------------------------------------
Each case is applied to a COPY of the tree in a temp directory, so the real
scripts/sf360.py is never edited and an interrupted run - this box is a Spot VM
and has been evicted mid-task before - cannot leave a sabotaged rail behind.
Case 0 is the unmutated control: if the copy itself does not pass, every later
red is meaningless.

Run:  python tests/mutate_sf360.py     (from anywhere)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SOURCE = os.path.join(REPO, "scripts", "sf360.py")
SUITE = os.path.join(HERE, "test_sf360.py")

# (name, old, new, the assertion that MUST be the one to fail)
MUTATIONS = [
    (
        "the CORS allowlist matches by prefix",
        "    if origin in allowed:\n        return True\n",
        "    if any(origin.startswith(a) for a in allowed):\n        return True\n",
        "a suffix attacker is REFUSED",
    ),
    (
        "the read-only SOQL guard accepts anything",
        '_SELECT_ONLY = re.compile(r"^\\s*select\\s", re.IGNORECASE)',
        '_SELECT_ONLY = re.compile(r"", re.IGNORECASE)',
        "refused: DELETE FROM Account",
    ),
    (
        "the proxy grows a route that runs caller-supplied SOQL",
        '            self._send(404, {"error": "no such route", "try": "/api/datasets"}, origin)',
        '            if route == "/api/query":\n'
        '                self._send(200, session.query((query.get("q") or [""])[0]), origin)\n'
        "                return\n"
        '            self._send(404, {"error": "no such route", "try": "/api/datasets"}, origin)',
        "there is NO route that runs caller-supplied SOQL",
    ),
    (
        "the proxy stops requiring its token",
        "            if not api_token:\n                return True",
        "            if True:\n                return True",
        "a tokenless data request is 401",
    ),
    (
        "the token cache writes the client secret to disk",
        '                "access_token": self._token,',
        '                "access_token": self._token,\n'
        '                "client_secret": self.config["client_secret"],',
        "the token cache holds no client secret",
    ),
    (
        "a refused grant prints the credential it used",
        "                % (exc.code, detail)",
        '                % (exc.code, detail + self.config["client_secret"])',
        "a refused grant explains itself without the credentials",
    ),
    (
        "a capped read no longer says it was truncated",
        "                    truncated = True",
        "                    truncated = False",
        "a capped read is MARKED truncated",
    ),
    (
        "the single silent re-auth on 401 is removed",
        "                if exc.code == 401 and attempt == 0:",
        "                if exc.code == 401 and attempt == 99:",
        "a 401 costs one transparent re-auth, not a failure",
    ),
]


def run_suite(tree):
    proc = subprocess.run(
        [sys.executable, os.path.join(tree, "tests", "test_sf360.py")],
        capture_output=True, text=True, timeout=300)
    # The suite prints one "failure: <name>" line per failed assertion. Do NOT
    # parse a comma-joined list: several assertion names contain commas, and
    # splitting on them made this harness report the 401 re-auth case as
    # uncovered while the suite had caught it correctly.
    failures = [line[len("failure: "):].strip()
                for line in proc.stdout.splitlines()
                if line.startswith("failure: ")]
    return proc.returncode, failures, proc.stdout, proc.stderr


def build_tree(source_text):
    tree = tempfile.mkdtemp(prefix="sf360-mutant-")
    os.makedirs(os.path.join(tree, "scripts"))
    os.makedirs(os.path.join(tree, "tests"))
    with open(os.path.join(tree, "scripts", "sf360.py"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(source_text)
    shutil.copyfile(SUITE, os.path.join(tree, "tests", "test_sf360.py"))
    return tree


def main():
    with open(SOURCE, encoding="utf-8") as fh:
        original = fh.read()

    bad = 0

    print("== control: the unmutated copy must pass ==")
    tree = build_tree(original)
    try:
        code, failures, out, err = run_suite(tree)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    if code == 0:
        print("  ok   the copied tree passes, so a red below means the mutation")
    else:
        bad += 1
        print("  FAIL the control run is already red; nothing below proves anything")
        print(out[-2000:])
        print(err[-2000:])

    print("== mutations: each must fail the assertion that names it ==")
    for name, old, new, expected in MUTATIONS:
        occurrences = original.count(old)
        if occurrences != 1:
            bad += 1
            print("  FAIL %s  --> the anchor text occurs %d times, not once; "
                  "sf360.py changed and this case no longer edits what it claims"
                  % (name, occurrences))
            continue
        tree = build_tree(original.replace(old, new))
        try:
            code, failures, out, err = run_suite(tree)
        finally:
            shutil.rmtree(tree, ignore_errors=True)
        if code == 0:
            bad += 1
            print("  FAIL %s  --> the suite stayed GREEN. It does not cover this."
                  % (name,))
            continue
        if expected not in failures:
            bad += 1
            print("  FAIL %s  --> the suite went red, but not on %r. Red was: %s"
                  % (name, expected, ", ".join(failures) or "<no named failures>"))
            if err.strip():
                print("       stderr tail: " + err.strip().splitlines()[-1][:160])
            continue
        others = [f for f in failures if f != expected]
        extra = ("  (also: " + ", ".join(others) + ")") if others else ""
        print("  ok   %s  --> caught by %r%s" % (name, expected, extra))

    print("")
    print("mutations=%d unproven=%d" % (len(MUTATIONS), bad))
    if bad:
        print("An unproven mutation means the suite's green is not evidence "
              "about that behaviour.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

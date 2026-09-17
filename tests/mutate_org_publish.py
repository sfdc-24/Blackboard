#!/usr/bin/env python3
"""Mutation control for tests/test_org_publish.py.

WHY THIS EXISTS
---------------
The publisher decides what leaves a private org for a public repository, and
what numbers a page states about data a reader cannot check. Its suite reports
passed=65 failed=0, which on its own is compatible with a suite that cannot
fail at all. This breaks the publisher eight ways on a COPY of the tree and
requires the suite to go red on the assertion that NAMES each defect.

It has already earned its place once. The suite's first run failed five
assertions and they were right: project() checked the row it had just built
rather than the raw record, so an unlisted field coming out of the org was
silently dropped instead of refused. Safe by accident - the field never
reached the file - but a drifted SELECT would have sailed through unnoticed.

Two of the cases below are not leaks at all but WRONG NUMBERS: an industry
bucket quietly dropped, a largest-deal figure computed over the wrong set.
Those matter as much here. A page that states a total nobody can reconcile is
the failure this project keeps paying for.

Run:  python tests/mutate_org_publish.py     (from anywhere)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SOURCE = os.path.join(REPO, "scripts", "org_publish.py")
RAIL = os.path.join(REPO, "scripts", "sf360.py")
SUITE = os.path.join(HERE, "test_org_publish.py")
FIXTURE = os.path.join(HERE, "fixtures", "org_desk_published_20260917.json")

# (name, old, new, the assertion that MUST be the one to fail)
MUTATIONS = [
    (
        "an unlisted field out of the org is dropped instead of refused",
        "    extra = set(record) - allowed\n    if extra:",
        "    extra = set()\n    if extra:",
        "an unlisted field is refused, not stripped",
    ),
    (
        "accounts with no industry vanish from the breakdown",
        '        key = account.get("Industry") or NO_INDUSTRY\n'
        "        by_industry[key] = by_industry.get(key, 0) + 1",
        '        key = account.get("Industry")\n'
        "        if key:\n"
        "            by_industry[key] = by_industry.get(key, 0) + 1",
        "by_industry matches, (none) bucket included",
    ),
    (
        "closed-lost counts every deal that was not won",
        '    lost = [o for o in opportunities if o["IsClosed"] and not o["IsWon"]]',
        '    lost = [o for o in opportunities if not o["IsWon"]]',
        "matches the published lost_count",
    ),
    (
        "the largest deal is taken over closed ones too",
        '        ("largest_open", float(max([_amount(o) for o in open_opps] or [0]))),',
        '        ("largest_open", float(max([_amount(o) for o in opportunities] or [0]))),',
        "matches the published largest_open",
    ),
    (
        "the withheld-field sweep stops at the top level",
        "    elif isinstance(payload, list):\n"
        "        for index, item in enumerate(payload[:200]):\n"
        '            scan_for_withheld(item, "%s[%d]" % (path, index))',
        "    elif isinstance(payload, list):\n"
        "        pass",
        "a withheld key nested in a new section is caught",
    ),
    (
        "a truncated read is published as if it were the whole org",
        '        if result.get("truncated"):',
        "        if False:",
        "a capped read refuses to publish a partial org",
    ),
    (
        "org.js and org.json are serialised separately and can disagree",
        "    return body + \"\\n\", JS_HEADER + body + \";\\n\"",
        "    return body + \"\\n\", JS_HEADER + json.dumps(payload) + \";\\n\"",
        "org.js carries byte-identical json to org.json",
    ),
    (
        "the snapshot claims an api version it did not call",
        '        ("api", "v" + str(config.get("api_version", sf360.DEFAULT_API_VERSION))),',
        '        ("api", "v62.0"),',
        "meta reports the api version the session used, not a literal",
    ),
]


def run_suite(tree):
    proc = subprocess.run(
        [sys.executable, os.path.join(tree, "tests", "test_org_publish.py")],
        capture_output=True, text=True, timeout=300)
    failures = [line[len("failure: "):].strip()
                for line in proc.stdout.splitlines()
                if line.startswith("failure: ")]
    return proc.returncode, failures, proc.stdout, proc.stderr


def build_tree(source_text):
    tree = tempfile.mkdtemp(prefix="org-publish-mutant-")
    os.makedirs(os.path.join(tree, "scripts"))
    os.makedirs(os.path.join(tree, "tests", "fixtures"))
    with open(os.path.join(tree, "scripts", "org_publish.py"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(source_text)
    shutil.copyfile(RAIL, os.path.join(tree, "scripts", "sf360.py"))
    shutil.copyfile(SUITE, os.path.join(tree, "tests", "test_org_publish.py"))
    shutil.copyfile(FIXTURE, os.path.join(tree, "tests", "fixtures",
                                          os.path.basename(FIXTURE)))
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
        print(err[-1500:])

    print("== mutations: each must fail the assertion that names it ==")
    for name, old, new, expected in MUTATIONS:
        occurrences = original.count(old)
        if occurrences != 1:
            bad += 1
            print("  FAIL %s  --> the anchor text occurs %d times, not once; "
                  "org_publish.py changed and this case no longer edits what it "
                  "claims" % (name, occurrences))
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
                  % (name, expected, "; ".join(failures) or "<no named failures>"))
            if err.strip():
                print("       stderr tail: " + err.strip().splitlines()[-1][:160])
            continue
        others = [f for f in failures if f != expected]
        extra = ("  (also: " + "; ".join(others) + ")") if others else ""
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

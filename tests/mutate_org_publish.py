#!/usr/bin/env python3
"""Mutation control for tests/test_org_publish.py.

WHY THIS EXISTS
---------------
Two files decide what the public page says: scripts/org_publish.py, which
decides what may leave a private org for a world-readable repository, and
scripts/org_diff_guard.py, which decides whether a build may be published at
all. Their suite reports passed=82 failed=0, which on its own is equally
consistent with a suite that cannot fail. This breaks both files on a COPY of
the tree and requires the suite to go red on the assertion that NAMES each
defect.

It has already earned its place twice. The suite's first run failed five
assertions and was right: project() checked the row it had just built rather
than the raw record, so an unlisted field coming out of the org was silently
dropped instead of refused - safe by accident, and blind to a drifted SELECT.
Then this harness itself reported ten unproven mutations, because the mutant
tree was missing org_diff_guard.py once the suite started importing it. A
mutation control that cannot build the tree proves nothing about the code.

Not every case is a leak. Two are WRONG NUMBERS - an industry bucket quietly
dropped, a largest-deal figure taken over the wrong set - and one is a guard
that treats an unreadable live page as an empty org. A page that states a
total nobody can reconcile, and a build that publishes over something it could
not read, are the two failures this project keeps paying for.

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
GUARD = os.path.join(REPO, "scripts", "org_diff_guard.py")
RAIL = os.path.join(REPO, "scripts", "sf360.py")
SUITE = os.path.join(HERE, "test_org_publish.py")
FIXTURE = os.path.join(HERE, "fixtures", "org_desk_published_20260917.json")

# Built rather than written literally: a doubled backslash in a source edit has
# been collapsed by the tooling on this box before, which turns an anchor into
# text that matches nothing and reports a covered behaviour as uncovered.
BS = chr(92)
NEWLINE_KWARG = 'newline="' + BS + 'n"'

# (name, file, old, new, the assertion that MUST be the one to fail)
MUTATIONS = [
    (
        "an unlisted field out of the org is dropped instead of refused",
        "org_publish.py",
        "    extra = set(record) - allowed\n    if extra:",
        "    extra = set()\n    if extra:",
        "an unlisted field is refused, not stripped",
    ),
    (
        "accounts with no industry vanish from the breakdown",
        "org_publish.py",
        '        key = account.get("Industry") or NO_INDUSTRY\n'
        "        by_industry[key] = by_industry.get(key, 0) + 1",
        '        key = account.get("Industry")\n'
        "        if key:\n"
        "            by_industry[key] = by_industry.get(key, 0) + 1",
        "by_industry matches, (none) bucket included",
    ),
    (
        "closed-lost counts every deal that was not won",
        "org_publish.py",
        '    lost = [o for o in opportunities if o["IsClosed"] and not o["IsWon"]]',
        '    lost = [o for o in opportunities if not o["IsWon"]]',
        "matches the published lost_count",
    ),
    (
        "the largest deal is taken over closed ones too",
        "org_publish.py",
        '        ("largest_open", float(max([_amount(o) for o in open_opps] or [0]))),',
        '        ("largest_open", float(max([_amount(o) for o in opportunities] or [0]))),',
        "matches the published largest_open",
    ),
    (
        "the withheld-field sweep stops at the top level",
        "org_publish.py",
        "    elif isinstance(payload, list):\n"
        "        for index, item in enumerate(payload[:200]):\n"
        '            scan_for_withheld(item, "%s[%d]" % (path, index))',
        "    elif isinstance(payload, list):\n"
        "        pass",
        "a withheld key nested in a new section is caught",
    ),
    (
        "a truncated read is published as if it were the whole org",
        "org_publish.py",
        '        if result.get("truncated"):',
        "        if False:",
        "a capped read refuses to publish a partial org",
    ),
    (
        "org.js and org.json are serialised separately and can disagree",
        "org_publish.py",
        '    return body + "' + BS + 'n", JS_HEADER + body + ";' + BS + 'n"',
        '    return body + "' + BS + 'n", JS_HEADER + json.dumps(payload) + ";' + BS + 'n"',
        "org.js carries byte-identical json to org.json",
    ),
    (
        "the snapshot claims an api version it did not call",
        "org_publish.py",
        '        ("api", "v" + str(config.get("api_version", sf360.DEFAULT_API_VERSION))),',
        '        ("api", "v62.0"),',
        "meta reports the api version the session used, not a literal",
    ),
    (
        "the guard stops noticing that records disappeared",
        "org_diff_guard.py",
        '            shrank.append("%s %d -> %d" % (section, before, after))',
        "            pass",
        "a shrink refuses the publish",
    ),
    (
        "an unreadable live page is treated as an empty org",
        "org_diff_guard.py",
        "            % (label, path, exc))\n        return None",
        "            % (label, path, exc))\n        return {}",
        "an unreadable live page is UNKNOWN, not empty",
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


def build_tree(texts):
    """texts maps a script filename to its (possibly mutated) contents."""
    tree = tempfile.mkdtemp(prefix="org-publish-mutant-")
    os.makedirs(os.path.join(tree, "scripts"))
    os.makedirs(os.path.join(tree, "tests", "fixtures"))
    for name, body in texts.items():
        with open(os.path.join(tree, "scripts", name), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(body)
    shutil.copyfile(RAIL, os.path.join(tree, "scripts", "sf360.py"))
    shutil.copyfile(SUITE, os.path.join(tree, "tests", "test_org_publish.py"))
    shutil.copyfile(FIXTURE, os.path.join(tree, "tests", "fixtures",
                                          os.path.basename(FIXTURE)))
    return tree


def main():
    originals = {}
    for path in (SOURCE, GUARD):
        with open(path, encoding="utf-8") as fh:
            originals[os.path.basename(path)] = fh.read()

    bad = 0

    print("== control: the unmutated copy must pass ==")
    tree = build_tree(dict(originals))
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
    for name, filename, old, new, expected in MUTATIONS:
        occurrences = originals[filename].count(old)
        if occurrences != 1:
            bad += 1
            print("  FAIL %s  --> the anchor occurs %d times in %s, not once; "
                  "the file changed and this case no longer edits what it claims"
                  % (name, occurrences, filename))
            continue
        texts = dict(originals)
        texts[filename] = originals[filename].replace(old, new)
        tree = build_tree(texts)
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

#!/usr/bin/env python3
"""Tests for scripts/okf_ownership.py: the OKF has one writer, and the pen is handed over from main.

His directive of 2026-10-03 (GROK-OKF-LIVE-WRITE-20261003T1426Z) gives this repository's docs/okf/
one writer and everyone else read access, with a handover during a call. The conference line's first
version of that rule read the floor file from the branch it was checking, so a branch could add its
own grant line and be admitted by it (Codex's P1 on conference #161). The two controls for that are
here, against a real repository built in the test:

  * a grant merged to the base admits the branch;
  * the same grant written by the branch admits nothing.

Run: python3 tests/test_okf_ownership.py
"""
import importlib.util
import io
import json
import ast
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RULE = os.path.join(HERE, "..", "scripts", "okf_ownership.py")

spec = importlib.util.spec_from_file_location("okf_ownership", RULE)
okf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(okf)

RULE_INSIDE = "scripts/okf_ownership.py"
PASS = 0
FAIL = 0
FAILURES = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  FAIL %s" % name)
        if detail:
            print("       %s" % detail)


FLOOR = """---
type: floor
%s---
# The floor, and the pen that goes with it

## Open grants

%s

## How a grant is written

- grant: codex | call: an example in the prose | paths: docs/okf/index.md
"""

CALL = "2026-10-03 18:00Z"
GEMINI_GRANT = "- grant: gemini | call: %s | paths: docs/okf/lanes.md" % CALL
OKF_INDEX = "docs/okf/index.md"
GRANT_GROK = "- grant: grok | call: %s | paths: %s" % (CALL, OKF_INDEX)


def floor(*lines, call=CALL):
    head = "call: %s\n" % call if call else ""
    return FLOOR % (head, "\n".join(lines) if lines else "(none)")


def run(argv, root=None):
    """(exit code, what it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = okf.main(argv, root=root)
    return code, out.getvalue()


def repo(floor_text=None, head_floor=None, rule=True):
    """A repository whose HEAD is the base. head_floor is written to the working tree only, which is
    what CI hands the check: the candidate's own copy of the file."""
    at = tempfile.mkdtemp()
    def git(*args):
        subprocess.run(("git",) + args, cwd=at, check=True, capture_output=True)
    git("init", "-q", "-b", "work")
    git("config", "user.email", "test@sfdc24")
    git("config", "user.name", "test")
    git("config", "commit.gpgsign", "false")
    put(at, "README.md", "base\n")
    if rule:
        with open(RULE, encoding="utf-8") as read:
            mine = read.read()
        put(at, RULE_INSIDE, mine if rule is True else rule)
    if floor_text is not None:
        put(at, "docs/okf/floor.md", floor_text)
    git("add", "-A")
    git("commit", "-qm", "base")
    git("branch", "main")           # the protected branch: the only ref a grant is read from
    # The remote-tracking ref CI actually reads, which a local branch of the same short name cannot
    # shadow - and it carries THIS rule's own source, because a grant is honoured only by the
    # protected branch's copy of the rule (Codex on c33cf11).
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    if head_floor is not None:
        put(at, "docs/okf/floor.md", head_floor)
    return at


def commit(at, message):
    """Add everything and commit it: the candidate's own commit, on top of the base."""
    for args in (("add", "-A"), ("commit", "-qm", message)):
        subprocess.run(("git",) + args, cwd=at, check=True, capture_output=True)


def put(at, path, text):
    whole = os.path.join(at, path.replace("/", os.sep))
    os.makedirs(os.path.dirname(whole), exist_ok=True)
    with open(whole, "w", encoding="utf-8", newline="") as out:
        out.write(text)


print("the OKF has one writer, and this check has no opinion about anything else")

check("the operator writes the OKF",
      okf.allowed("claude-code-cli/x", "docs/okf/index.md"))
check("the operator writes it from the VM too",
      okf.allowed("vm-claude-code-cli/x", "docs/okf/STRATEGY.md"))
check("pi1-cli writes the OKF's pages too (his words, 2026-10-03 19:45Z)",
      okf.allowed("pi1-cli/device-notes", "docs/okf/index.md")
      and okf.allowed("pi1-cli/device-notes", "docs/okf/lanes.md"))
check("pi1-cli does not write the floor, the page that hands out the pen",
      not okf.allowed("pi1-cli/x", "docs/okf/floor.md"))
check("nobody else writes it without a grant",
      not any(okf.allowed(b, "docs/okf/index.md")
              for b in ("codex/x", "grok/x", "cursor/x", "copilot/x", "gemini/okf-1")))
check("every path outside the OKF passes, for everyone",
      all(okf.allowed(b, p) for b in ("codex/x", "grok/x", "cursor/x", "claude-code-cli/x")
          for p in ("scripts/append.py", "docs/POKA-YOKE.md", "src/bus_server.py", "README.md")))
# Codex's second P1 on 2ec9ee0: the rule granted gemini/* unconditional write to docs/okf/gemini/
# in THIS repository, while the README sitting in that folder says Gemini lands in the private
# conference repository and "never here", and scripts/okf_land.py hard-codes conference. The
# exception is gone. The folder is an ordinary OKF page here: standing writers write it, everyone
# else - Gemini included - needs the floor, which is the reviewed path.
check("no standing exception hands a public OKF folder to gemini",
      not okf.allowed("gemini/okf-1", "docs/okf/gemini/RESULT-1.md")
      and not okf.allowed("codex/x", "docs/okf/gemini/RESULT-1.md"))
check("the standing OKF writers write it like any other OKF page",
      okf.allowed("claude-code-cli/x", "docs/okf/gemini/RESULT-1.md")
      and okf.allowed("pi1-cli/notes", "docs/okf/gemini/RESULT-1.md"))
check("and the floor is still the way in for gemini",
      okf.allowed("gemini/okf-1", "docs/okf/gemini/RESULT-1.md",
                  floor("- grant: gemini | call: %s | paths: docs/okf/gemini/" % CALL), CALL)
      and not okf.allowed("codex/x", "docs/okf/gemini/RESULT-1.md",
                          floor("- grant: gemini | call: %s | paths: docs/okf/gemini/" % CALL),
                          CALL))

print()
print("the floor hands the pen over, for the paths and the call it names")

check("a grant admits its own paths and no others",
      okf.allowed("gemini/okf-1", "docs/okf/lanes.md", floor(GEMINI_GRANT), CALL)
      and not okf.allowed("gemini/okf-1", "docs/okf/index.md", floor(GEMINI_GRANT), CALL)
      and not okf.allowed("codex/x", "docs/okf/lanes.md", floor(GEMINI_GRANT), CALL))
check("a grant may name a folder",
      okf.allowed("codex/x", "docs/okf/notes/codex.md",
                  floor("- grant: codex | call: %s | paths: docs/okf/notes/" % CALL), CALL))
check("a grant cannot reach outside the OKF, however it is written",
      all(okf.grants(floor("- grant: gemini | call: %s | paths: %s" % (CALL, p))) == {}
          for p in ("docs/okf/../../scripts/append.py", "scripts/append.py", "docs/POKA-YOKE.md")))
check("the example in the prose grants nothing",
      okf.grants(floor()) == {}
      and "an example in the prose" not in okf.open_section(floor()))
check("the floor file itself is never granted, however wide the grant",
      not okf.allowed("gemini/okf-1", "docs/okf/floor.md",
                      floor("- grant: gemini | call: %s | paths: docs/okf/" % CALL), CALL)
      and okf.allowed("gemini/okf-1", "docs/okf/lanes.md",
                      floor("- grant: gemini | call: %s | paths: docs/okf/" % CALL), CALL))
check("a grant written for another call is not in force",
      not okf.allowed("gemini/okf-1", "docs/okf/lanes.md", floor(GEMINI_GRANT), "2026-10-04 14:00Z")
      and not okf.allowed("gemini/okf-1", "docs/okf/lanes.md", floor(GEMINI_GRANT), ""))
check("two pens on one page are named",
      okf.overlapping(okf.grants(floor(GEMINI_GRANT,
                                       "- grant: codex | call: %s | paths: docs/okf/" % CALL)))
      is not None)
check("two pens on two pages are not",
      okf.overlapping(okf.grants(floor(GEMINI_GRANT,
                                       "- grant: codex | call: %s | paths: docs/okf/index.md" % CALL)))
      is None)
check("one agent granted a folder and a file inside it is not a clash",
      okf.overlapping(okf.grants(floor("- grant: gemini | call: %s | paths: docs/okf/,"
                                       " docs/okf/index.md" % CALL))) is None)

print()
print("the grant comes from the base, never from the branch being checked")

at = repo(floor_text=floor(GEMINI_GRANT))
code, said = run(["gemini/okf-1", "--base", "main", "docs/okf/lanes.md"], root=at)
check("a grant merged to the base admits the branch", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(), head_floor=floor(GEMINI_GRANT,
                                               "- grant: codex | call: %s | paths: docs/okf/" % CALL))
one, _ = run(["gemini/okf-1", "--base", "main", "docs/okf/lanes.md"], root=at)
two, _ = run(["codex/x", "--base", "main", "docs/okf/index.md"], root=at)
three, _ = run(["codex/x", "--base", "main", "docs/okf/floor.md"], root=at)
check("a grant the branch wrote for itself admits nothing", (one, two, three) == (1, 1, 1),
      "%r" % ((one, two, three),))
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=None)
code, said = run(["gemini/okf-1", "--base", "main", "docs/okf/lanes.md"], root=at)
check("no floor on the base is no grant",
      code == 1 and "carries no docs/okf/floor.md" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(GEMINI_GRANT, call=None))
code, said = run(["gemini/okf-1", "--base", "main", "docs/okf/lanes.md"], root=at)
check("no call in the floor's front matter is no grant",
      code == 1 and "names no call" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(GEMINI_GRANT))
code, said = run(["gemini/okf-1", "--base", "origin/nope", "docs/okf/lanes.md"], root=at)
check("a base that cannot be read is no grant", code == 1, said.strip())
code, said = run(["gemini/okf-1", "docs/okf/lanes.md"], root=at)
check("no --base at all is no grant",
      code == 1 and "no protected branch was named" in said, said.strip())
code, said = run(["gemini/okf-1", "--base", "main", "scripts/append.py"], root=at)
check("and a path outside the OKF still passes with no grant and no base", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(GEMINI_GRANT, "- grant: codex | call: %s | paths: docs/okf/" % CALL))
code, said = run(["gemini/okf-1", "--base", "main", "docs/okf/lanes.md"], root=at)
check("overlapping grants on the base refuse the whole run",
      code == 1 and "two grants hold one page" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor())
code, said = run(["claude-code-cli/x", "--base", "main", "docs/okf/floor.md",
                  "docs/okf/index.md"], root=at)
check("the operator needs no grant", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

print()
print("and the grant is only as good as what the rule can see (Codex 20:26Z, exact head c33cf11)")

# A grant may be read from the protected branch and from nothing else the branch chose.
check("the protected branch is named by its name and nothing near it",
      okf.authority("origin/main", "")[0] == "refs/remotes/origin/main"
      and okf.authority("", "main")[0] == "refs/remotes/origin/main"
      and okf.authority("refs/heads/main", "")[0] == "refs/remotes/origin/main"
      and all(okf.authority(near, near)[0] == ""
              for near in ("main-ish", "mymain", "origin/mainline", "HEAD", "origin/a-branch")))

at = repo(floor_text=floor())
put(at, "docs/okf/floor.md", floor(GEMINI_GRANT))
commit(at, "a grant written on a branch of its own")
subprocess.run(("git", "branch", "an-unmerged-base"), cwd=at, check=True, capture_output=True)
code, said = run(["gemini/okf-1", "--base", "an-unmerged-base", "docs/okf/lanes.md"], root=at)
check("a grant on an unmerged base the branch chose is no grant",
      code == 1 and "not the protected branch" in said, said.strip())
code, said = run(["gemini/okf-1", "--protected", "main", "--base", "an-unmerged-base",
                  "docs/okf/lanes.md"], root=at)
check("and naming the protected branch reads the grant from there, not from the base",
      code == 1 and "may not write" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

# Exactly one call in the floor's front matter, or no grant belongs to a call.
check("a floor naming two calls is ambiguous and puts nothing in force",
      okf.floor_call(floor(GEMINI_GRANT).replace("call: %s\n" % CALL,
                                                 "call: an old call\ncall: %s\n" % CALL))[0] == ""
      and "ambiguous" in okf.floor_call(floor(GEMINI_GRANT).replace(
          "call: %s\n" % CALL, "call: an old call\ncall: %s\n" % CALL))[1])
check("a call written in the page's prose is not a declaration",
      okf.floor_call("# the floor\n\ncall: %s\n" % CALL)[0] == "")
at = repo(floor_text=floor(GEMINI_GRANT).replace("call: %s\n" % CALL,
                                                 "call: an old call\ncall: %s\n" % CALL))
code, said = run(["gemini/okf-1", "--base", "main", "docs/okf/lanes.md"], root=at)
check("and the run is refused rather than guessing which call it meant",
      code == 1 and "ambiguous" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

# What changed is git's account: a rename names both of its ends, and no path is C-quoted.
at = repo(floor_text=floor())
subprocess.run(("git", "mv", "docs/okf/floor.md", "docs/okf/old-floor.md"), cwd=at, check=True,
               capture_output=True)
commit(at, "the floor, under another name")
found, blind = okf.changed("main", at)
check("a rename gives git's own account of both its ends",
      not blind and "docs/okf/floor.md" in found and "docs/okf/old-floor.md" in found,
      "%s %s" % (sorted(found), blind))
code, said = run(["pi1-cli/notes", "--base", "main", "docs/okf/old-floor.md"], root=at)
check("so renaming the floor is refused, though only its destination was given",
      code == 1, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor())
put(at, "docs/okf/\u00e9.md", "a page named in French\n")
commit(at, "a page whose name is not ascii")
found, blind = okf.changed("main", at)
check("a page whose name is not ascii comes back as the path it is",
      "docs/okf/\u00e9.md" in found, "%s %s" % (sorted(found), blind))
check("and a quoted path is unquoted before it is judged",
      okf._unquote('"docs/okf/\\303\\251.md"') == "docs/okf/\u00e9.md")
code, said = run(["grok/notes", "--base", "main", '"docs/okf/\\303\\251.md"'], root=at)
check("so grok is refused the page it was quoted into", code == 1, said.strip())
code, said = run(["pi1-cli/notes", "--base", "main", '"docs/okf/\\303\\251.md"'], root=at)
check("and pi1-cli, who may write the OKF, still writes it", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor())
code, said = run(["gemini/okf-1", "--base", "origin/nope", "docs/okf/lanes.md"], root=at)
check("a base that cannot be diffed refuses the OKF to everyone but the operator",
      code == 1 and "cannot be confirmed" in said, said.strip())
code, said = run(["gemini/okf-1", "--base", "origin/nope", "scripts/append.py"], root=at)
check("and outside the OKF it still has no opinion", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

# Two pens on one page: the branch may not propose one, and the operator may repair one.
HELD = floor(GEMINI_GRANT, "- grant: codex | call: %s | paths: docs/okf/" % CALL)
at = repo(floor_text=floor())
put(at, "docs/okf/floor.md", HELD)
commit(at, "the operator proposes a clash")
code, said = run(["claude-code-cli/repair", "--base", "main", "docs/okf/floor.md"], root=at)
check("a clash the branch itself proposes is refused",
      code == 1 and "this branch's own" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=HELD)
put(at, "docs/okf/floor.md", floor(GEMINI_GRANT))
commit(at, "the operator strikes the second grant")
code, said = run(["claude-code-cli/repair", "--base", "main", "docs/okf/floor.md"], root=at)
check("and the operator's repair of that page is the way out", code == 0, said.strip())
put(at, "docs/okf/index.md", "carried along\n")
commit(at, "a repair that also writes another page")
code, said = run(["claude-code-cli/repair", "--base", "main", "docs/okf/floor.md",
                  "docs/okf/index.md"], root=at)
check("the repair is that page alone", code == 1, said.strip())
shutil.rmtree(at, ignore_errors=True)

# Codex's successor on c33cf11: the short name, the trusted copy, and a retired grant.
at = repo(floor_text=floor(GEMINI_GRANT))
put(at, "docs/okf/floor.md", floor("- grant: codex | call: %s | paths: docs/okf/" % CALL))
commit(at, "a floor that hands codex the whole OKF, on a branch of its own")
subprocess.run(("git", "branch", "origin/main"), cwd=at, check=True, capture_output=True)
shadowed = subprocess.run(("git", "show", "origin/main:docs/okf/floor.md"), cwd=at,
                          capture_output=True, text=True).stdout
check("git's own short name resolves to the pushed branch, which is the hole",
      "codex" in shadowed, shadowed[:80])
code, said = run(["codex/x", "--base", "origin/main", "docs/okf/index.md"], root=at)
check("a branch named origin/main does not become the authority", code == 1, said.strip())
# The short name no longer decides what COUNTS AS CHANGED either, which was the other half of the
# same shadowing: `git diff origin/main...HEAD` preferred refs/heads/origin/main, and that branch
# was the candidate's own commit, so the diff came back empty and the floor.md edit it carried was
# invisible. Resolved through FULL, the edit is in the changed set and refused by name.
code, said = run(["gemini/okf-1", "--base", "origin/main", "docs/okf/lanes.md"], root=at)
check("the shadow's own floor.md edit is now in the changed set, and refused",
      code == 1 and "docs/okf/floor.md" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

# The same grant, with nobody shadowing anything: it still admits.
at = repo(floor_text=floor(GEMINI_GRANT))
put(at, "docs/okf/lanes.md", "the lane gemini was handed\n")
commit(at, "gemini writes the page its grant names")
code, said = run(["gemini/okf-1", "--base", "origin/main", "docs/okf/lanes.md"], root=at)
check("and the real grant on the remote-tracking ref still admits", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(GEMINI_GRANT), rule="# a different rule entirely\n")
trusted, why = okf.trusted_copy("refs/remotes/origin/main", at)
check("a rule that is not the protected copy knows it", not trusted and "not the copy on" in why,
      why)
code, said = run(["gemini/okf-1", "--base", "main", "docs/okf/lanes.md"], root=at)
check("and honours no grant", code == 1, said.strip())
code, said = run(["gemini/okf-1", "--base", "main", "src/anything.py"], root=at)
check("while everything outside the OKF is unaffected", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(GEMINI_GRANT), rule=False)
trusted, why = okf.trusted_copy("refs/remotes/origin/main", at)
check("no rule on the protected ref is not a trusted copy either",
      not trusted and "carries no" in why, why)
shutil.rmtree(at, ignore_errors=True)

two_keys = floor(GEMINI_GRANT, call=CALL).replace("call: %s\n" % CALL, "call:\ncall: %s\n" % CALL)
check("every call KEY counts, so an empty one plus a value is ambiguous",
      okf.floor_call(two_keys)[0] == "" and "2 call lines" in okf.floor_call(two_keys)[1],
      okf.floor_call(two_keys)[1])

retired = "- grant: codex | call: a call that has ended | paths: docs/okf/"
both = floor(retired, GEMINI_GRANT)
check("a grant retired with another call does not block a live one",
      okf.overlapping_in_a_call(both) is None
      and okf.overlapping(okf.grants(both, None)) is not None)
at = repo(floor_text=both)
code, said = run(["gemini/okf-1", "--base", "main", "docs/okf/lanes.md"], root=at)
check("so the live handover is admitted", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

same = floor(GEMINI_GRANT, "- grant: codex | call: %s | paths: docs/okf/" % CALL)
check("two grants on one page for the SAME call still refuse",
      okf.overlapping_in_a_call(same) is not None)

# The rule answers its own controls before it judges anything.
check("the rule holds its own controls", okf.controls_hold() == "", okf.controls_hold())
kept = okf.allowed
try:
    okf.allowed = lambda *a, **k: True
    code, said = run(["grok/x", "docs/okf/floor.md"])
    check("a decision tampered with inside the rule refuses instead of admitting",
          okf.controls_hold() != "" and code == 1, said.strip())
    okf.allowed = lambda *a, **k: False
    code, said = run(["claude-code-cli/x", "docs/okf/index.md"])
    check("and a rule that admits nothing is broken too",
          okf.controls_hold() != "" and code == 1, said.strip())
finally:
    okf.allowed = kept
check("the controls hold again once it is put back", okf.controls_hold() == "")

# ================= THE GATE: the posture it demands, and everything it refuses without ========
#
# Codex's P1, three reviews running: "independent enforcement remains absent... Self-comparison
# cannot establish independent authority." Two structural holes, neither closable in Python from
# inside a candidate-defined workflow: a `pull_request` workflow's definition comes from the
# candidate's merge ref, so the enforcement step can be replaced; and everything the rule read
# came through `git show <local ref>:<path>`, which candidate code run by an earlier step of that
# same workflow could repoint.
#
# The answer is not a cleverer comparison. It is WHERE the run happens:
# .github/workflows/okf-ownership-trusted.yml is a `pull_request_target` workflow, whose
# definition GitHub takes from the base branch, which checks out the forge-named base commit and
# never the candidate's, and which takes the changed set from the forge's own pulls/<n>/files.
# What the rule can check from inside is that this story is true of the tree it is running in -
# and that is what these controls are about. There is no fallback anywhere in it.
print()
print("the gate: the posture it demands, and what it refuses without")


_LISTS = [0]


def changed_list(at, *paths):
    """The forge's own changed-path list, written the way the trusted workflow writes it: a JSON
    array, because a path is bytes and one path per line plus strip() was lossy."""
    _LISTS[0] += 1
    where = os.path.join(at, "_changed-%d.json" % _LISTS[0])
    with open(where, "w", encoding="utf-8", newline="") as out:
        json.dump(list(paths), out)
    return where


at = repo(floor_text=floor(GEMINI_GRANT))
BASE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
LANES = changed_list(at, "docs/okf/lanes.md")

code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", LANES], root=at)
check("the gate reads the floor out of the base commit it is standing on", code == 0, said.strip())
check("and does not call itself advisory", "ADVISORY" not in said, said.strip())

code, said = run(["claude-code-cli/x", "docs/okf/index.md"], root=at)
check("while a run without the posture says it decides nothing",
      "ADVISORY" in said and "not the gate" in said, said.strip())

code, said = run(["gemini/okf-1", "--trusted-base", "main", "--base-ref", "main",
                  "--changed-from", LANES], root=at)
check("the posture cannot be claimed with a ref name in place of a sha",
      code == 1 and "not a commit sha" in said, said.strip())

code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "main",
                  "docs/okf/lanes.md"], root=at)
check("and not without the forge's own changed-path list",
      code == 1 and "--changed-from" in said, said.strip())

code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--changed-from", LANES], root=at)
check("and not without being told which branch the pull request targets",
      code == 1 and "--base-ref" in said, said.strip())

code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", os.path.join(at, "_never-written.txt")], root=at)
check("a changed-path list that is not there refuses rather than judging nothing",
      code == 1 and "was not read" in said, said.strip())

code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", changed_list(at)], root=at)
check("and an empty one refuses too", code == 1 and "no changed path" in said, said.strip())

# A pull request may target an unmerged branch of its own, and that branch may carry any floor it
# likes. The checkout is still honestly the base commit; the base is just not the protected branch.
code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "gemini/a-branch",
                  "--changed-from", LANES], root=at)
check("a pull request targeting a branch of its own is handed no grant by it",
      code == 1 and "not the protected branch" in said, said.strip())
code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "gemini/a-branch",
                  "--changed-from", changed_list(at, "scripts/append.py")], root=at)
check("while outside the OKF that changes nothing", code == 0, said.strip())

# THE CHANGED SET IS THE FORGE'S ACCOUNT, NOT A LOCAL DIFF. Here the checkout IS the base commit,
# so `git diff base...HEAD` is empty and a local diff would have found nothing to judge. The gate
# judges what the forge reported, which is also why a rename cannot hide: the workflow asks for
# previous_filename as well as filename.
code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", changed_list(at, "docs/okf/lanes.md", "docs/okf/floor.md")],
                 root=at)
check("the gate judges the forge's list even where a local diff is empty",
      code == 1 and "docs/okf/floor.md" in said, said.strip())

# The branch's own floor is fetched as DATA by the trusted workflow and read ONLY to refuse.
CAND = os.path.join(at, "_candidate-floor.md")
with open(CAND, "w", encoding="utf-8", newline="") as out:
    out.write(floor(GEMINI_GRANT, "- grant: codex | call: %s | paths: docs/okf/" % CALL))
code, said = run(["claude-code-cli/repair", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", changed_list(at, "docs/okf/floor.md"),
                  "--candidate-floor", CAND], root=at)
check("a branch that proposes two pens on one page is refused on the forge's copy of its floor",
      code == 1 and "this branch's own" in said, said.strip())

# ===================== A PATH IS BYTES, AND strip() ATE SOME =====================
# Codex on the conference twin (2496937): paths_from() stripped each reported filename, and a
# leading or trailing space is a VALID PATH BYTE - so the unowned " docs/okf/index.md" arrived as
# the owned "docs/okf/index.md" and changed the answer. The list is JSON now and nothing is
# transformed at all.
print()
print("the forge's list is lossless")

at = repo(floor_text=floor())
BASE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
for sneaky in (" docs/okf/index.md", "docs/okf/index.md ", "\tdocs/okf/index.md"):
    found, why = okf.paths_from(changed_list(at, sneaky))
    check("a path is read byte for byte, not stripped (%r)" % sneaky,
          found == {sneaky} and not why, "%r %s" % (found, why))
check("and a stripped copy is a DIFFERENT path, which is the whole point",
      " docs/okf/index.md" != "docs/okf/index.md")
# The consequence, end to end: the operator owns docs/okf/index.md, and " docs/okf/index.md" is
# not that path, so it falls outside the OKF and this rule has no opinion - but it must never be
# silently promoted INTO the OKF either. What matters is that the bytes reaching allowed() are the
# bytes the forge reported.
code, said = run(["grok/x", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", changed_list(at, " " + OKF_INDEX)], root=at)
check("so a space-prefixed path is judged as itself", code == 0, said.strip())
code, said = run(["grok/x", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", changed_list(at, OKF_INDEX)], root=at)
check("while the real one is still refused to grok", code == 1, said.strip())
found, why = okf.paths_from(os.path.join(at, "_never-json.json"))
check("a list that is not there still refuses", not found and "was not read" in why, why)
with open(os.path.join(at, "_lines.json"), "w", encoding="utf-8", newline="") as out:
    out.write("docs/okf/index.md\n")
found, why = okf.paths_from(os.path.join(at, "_lines.json"))
check("and the old line format is refused rather than guessed at",
      not found and "JSON" in why, why)
shutil.rmtree(at, ignore_errors=True)

# ===================== AN AUTHORITY FILE MUST BE A REGULAR BLOB =====================
# Codex on a1e1e4f and 2496937: the base floor read followed symlinks. Point docs/okf/floor.md at
# an ordinary file elsewhere ONCE, and every later edit to that target hands out the pen while the
# non-delegable path looks untouched. The mode comes from git's own tree, so a checkout that
# materialises symlinks as text cannot disguise one either.
print()
print("an authority file that is not a regular file is not an authority file")

at = repo(floor_text=floor(GRANT_GROK))
subprocess.run(("git", "rm", "-q", "--cached", "docs/okf/floor.md"), cwd=at, check=True,
               capture_output=True)
put(at, "elsewhere.md", floor(GRANT_GROK))
# A symlink entry written straight into the index, which is how it would arrive from a Linux box
# and does not need symlink support on this one.
blob = subprocess.run(("git", "hash-object", "-w", "--stdin"), cwd=at, input="elsewhere.md",
                      capture_output=True, text=True, check=True).stdout.strip()
subprocess.run(("git", "update-index", "--add", "--cacheinfo", "120000,%s,docs/okf/floor.md" % blob),
               cwd=at, check=True, capture_output=True)
subprocess.run(("git", "add", "elsewhere.md"), cwd=at, check=True, capture_output=True)
subprocess.run(("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
                "the floor becomes a symlink"), cwd=at, check=True, capture_output=True)
CROOKED = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                         text=True).stdout.strip()
mode, kind, _ = okf.tree_entry(CROOKED, okf.FLOOR, at)
check("the fixture really does carry a symlink entry, which is the bypass",
      mode == "120000" and kind == "blob", "%s %s" % (mode, kind))
text, crooked = okf.authority_at(CROOKED, okf.FLOOR, at)
check("and authority_at refuses it by its mode", text == "" and "symlink" in crooked, crooked)
code, said = run(["grok/x", "--trusted-base", CROOKED, "--base-ref", "main",
                  "--changed-from", changed_list(at, OKF_INDEX)], root=at)
check("so the gate judges nothing rather than reading the target",
      code == 1 and "symlink" in said, said.strip())
check("a regular floor is still read, so the refusal is not blanket",
      okf.authority_at(CROOKED, "elsewhere.md", at)[0] is not None)
# A GITLINK at the same path: mode 160000, object type commit. A symlink is caught by its own
# branch, so without this the "not a regular blob" guard could be deleted and nothing would say
# so - which is exactly what a surviving mutant told me.
subprocess.run(("git", "update-index", "--add", "--cacheinfo",
                "160000,%s,docs/okf/floor.md" % CROOKED), cwd=at, check=True, capture_output=True)
subprocess.run(("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
                "the floor becomes a gitlink"), cwd=at, check=True, capture_output=True)
LINKED = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                        text=True).stdout.strip()
mode, kind, _ = okf.tree_entry(LINKED, okf.FLOOR, at)
check("the fixture really does carry a gitlink entry", mode == "160000" and kind == "commit",
      "%s %s" % (mode, kind))
text, crooked = okf.authority_at(LINKED, okf.FLOOR, at)
check("and a gitlink is refused as well, by type and mode",
      text == "" and "not a regular file" in crooked, crooked)
code, said = run(["grok/x", "--trusted-base", LINKED, "--base-ref", "main",
                  "--changed-from", changed_list(at, OKF_INDEX)], root=at)
check("so the gate judges nothing on a gitlink floor either", code == 1, said.strip())
check("and a path the commit does not carry is absence, not a refusal",
      okf.authority_at(CROOKED, "docs/okf/nope.md", at) == (None, ""))
shutil.rmtree(at, ignore_errors=True)

# ===================== THE FILES THAT DECIDE ARE NOT OPEN SEASON =====================
# Codex's third P1 on a1e1e4f: allowed() returned True for every path outside docs/okf/, which
# included this rule, its suite and BOTH workflow definitions - so any branch could weaken the
# gate and keep the expected check name, and later pull requests would pass a toothless check.
print()
print("the files that enforce the floor are the operator's")

for path in okf.AUTHORITY:
    check("nobody else writes %s" % path,
          not okf.allowed("grok/x", path) and not okf.allowed("codex/x", path)
          and not okf.allowed("pi1-cli/notes", path)
          and okf.allowed("claude-code-cli/x", path))
check("and no grant reaches them, however wide",
      not okf.allowed("grok/x", "scripts/okf_ownership.py",
                      floor("- grant: grok | call: %s | paths: docs/okf/" % CALL), CALL)
      and not okf.allowed("grok/x", ".github/workflows/okf-ownership-trusted.yml",
                          floor("- grant: grok | call: %s | paths: scripts/" % CALL), CALL))
check("while an ordinary path outside the OKF is still nobody's business here",
      okf.allowed("grok/x", "scripts/append.py") and okf.allowed("codex/x", "src/bus_server.py"))

at = repo(floor_text=floor())
BASE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
code, said = run(["grok/x", "--trusted-base", BASE, "--base-ref", "main", "--changed-from",
                  changed_list(at, ".github/workflows/okf-ownership-trusted.yml")], root=at)
check("the gate refuses a branch that edits the gate",
      code == 1 and "okf-ownership-trusted.yml" in said, said.strip())
code, said = run(["claude-code-cli/x", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from",
                  changed_list(at, ".github/workflows/okf-ownership-trusted.yml")], root=at)
check("and admits the operator, which is how it ever gets repaired", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

# ===================== A FORK'S BRANCH NAME BUYS NOTHING =====================
# Codex's P1 on a9de3c7, and it is the sharpest one yet: THIS REPOSITORY IS PUBLIC. Anyone may
# fork it, name their branch claude-code-cli/x or pi1-cli/x, and allowed() read that prefix and
# handed them standing writer access - the floor and the gate's own files included. A prefix was
# always attribution rather than identity, but inside one repository it is at least attribution
# among people who can push to it; from a fork it is a string a stranger chose.
print()
print("a fork's branch name buys nothing")

at = repo(floor_text=floor())
BASE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
for who, what in (("claude-code-cli/x", okf.FLOOR),
                  ("claude-code-cli/x", "scripts/okf_ownership.py"),
                  ("pi1-cli/notes", OKF_INDEX),
                  ("vm-claude-code-cli/x", ".github/workflows/okf-ownership-trusted.yml")):
    code, said = run([who, "--trusted-base", BASE, "--base-ref", "main",
                      "--head-repo", "a-stranger/Blackboard", "--this-repo", "sfdc-24/Blackboard",
                      "--changed-from", changed_list(at, what)], root=at)
    check("a fork using %s may not write %s" % (who.split("/")[0], what),
          code == 1 and "fork" in said, said.strip()[:150])
code, said = run(["claude-code-cli/x", "--trusted-base", BASE, "--base-ref", "main",
                  "--head-repo", "sfdc-24/Blackboard", "--this-repo", "sfdc-24/Blackboard",
                  "--changed-from", changed_list(at, okf.FLOOR)], root=at)
check("while the same branch from THIS repository still writes the floor", code == 0, said.strip())
code, said = run(["grok/x", "--trusted-base", BASE, "--base-ref", "main",
                  "--head-repo", "a-stranger/Blackboard", "--this-repo", "sfdc-24/Blackboard",
                  "--changed-from", changed_list(at, "scripts/append.py")], root=at)
check("and a fork is not refused for an ordinary path outside the OKF", code == 0, said.strip())
check("the rewrite is what does it, and it is plain",
      okf.rights_of("claude-code-cli/x", True) == "fork/claude-code-cli/x"
      and okf.rights_of("claude-code-cli/x", False) == "claude-code-cli/x"
      and not okf.allowed("fork/claude-code-cli/x", okf.FLOOR))
shutil.rmtree(at, ignore_errors=True)

# ===================== THE FORGE'S BYTES ARE NOT C-QUOTED =====================
# Codex on a9de3c7: the forge's list is already JSON-decoded into literal filenames, and _unquote
# treats anything wrapped in quote characters as git C-quoting - so a path literally named
# "docs/okf/index.md", quotes included, was rewritten into a DIFFERENT path and judged as that
# one, contradicting the byte-for-byte guarantee two lines above it.
print()
print("the forge's bytes reach allowed() unchanged")

at = repo(floor_text=floor())
BASE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
QUOTED = '"docs/okf/index.md"'
check("_unquote really would rewrite it, which is why this matters",
      okf._unquote(QUOTED) == OKF_INDEX and QUOTED != OKF_INDEX)
found, why = okf.paths_from(changed_list(at, QUOTED))
check("but paths_from hands it over untouched", found == {QUOTED} and not why, "%r %s" % (found, why))
code, said = run(["grok/x", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", changed_list(at, QUOTED)], root=at)
check("so a path that merely LOOKS quoted is judged as itself, not as the OKF page",
      code == 0, said.strip())
code, said = run(["grok/x", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", changed_list(at, OKF_INDEX)], root=at)
check("while the real page is still refused to grok", code == 1, said.strip())
shutil.rmtree(at, ignore_errors=True)

# ===================== THE GATE WILL NOT RUN OUT OF A CROOKED TREE =====================
# Codex on a9de3c7: if the RULE itself is a symlink on the protected branch, the gate executes
# whatever it points at - and that target is an ordinary path the rule lets anyone edit, so a
# later pull request silently owns every decision while the base copy still looks benign.
# authority_at() was checking this for floor.md alone.
print()
print("the gate refuses to run out of a tree where one of its own files is crooked")

at = repo(floor_text=floor())
subprocess.run(("git", "rm", "-q", "--cached", RULE_INSIDE), cwd=at, check=True,
               capture_output=True)
_blob = subprocess.run(("git", "hash-object", "-w", "--stdin"), cwd=at, input="../elsewhere.py",
                       capture_output=True, text=True, check=True).stdout.strip()
subprocess.run(("git", "update-index", "--add", "--cacheinfo",
                "120000,%s,%s" % (_blob, RULE_INSIDE)), cwd=at, check=True, capture_output=True)
subprocess.run(("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
                "the rule becomes a symlink"), cwd=at, check=True, capture_output=True)
BENTRULE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                          text=True).stdout.strip()
check("the fixture really does carry a symlinked rule",
      okf.tree_entry(BENTRULE, RULE_INSIDE, at)[0] == "120000")
code, said = run(["claude-code-cli/x", "--trusted-base", BENTRULE, "--base-ref", "main",
                  "--changed-from", changed_list(at, OKF_INDEX)], root=at)
check("and the gate refuses outright, for the operator as much as anyone",
      code == 1 and "its own files" in said, said.strip()[:160])
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor())
BASE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
code, said = run(["claude-code-cli/x", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", changed_list(at, OKF_INDEX)], root=at)
check("while an honest tree runs as before", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

# ===================== A BRANCH NAME IS NOT A REF ALIAS =====================
# Codex's P1 on f209b38: --base-ref carries pull_request.base.ref, a repository BRANCH NAME, and
# PROTECTED also holds advisory git spellings - so a pull request targeting an ordinary branch
# literally named `origin/main`, which anyone may push, was treated as protected and its floor
# grants honoured. The aliases stay for resolving a REF; a branch name compares only with main.
print()
print("a branch name is compared with the protected branch, not with a ref alias")

at = repo(floor_text=floor(GEMINI_GRANT))
BASE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
LANES = changed_list(at, "docs/okf/lanes.md")
code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", LANES], root=at)
check("the real protected branch still admits its grant", code == 0, said.strip())
for alias in ("origin/main", "refs/heads/main", "refs/remotes/origin/main"):
    code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", alias,
                      "--changed-from", changed_list(at, "docs/okf/lanes.md")], root=at)
    check("a branch named %r is NOT the protected branch" % alias,
          code == 1 and "not the protected branch" in said, said.strip())
check("and the aliases are still believed where a REF is meant",
      okf.authority("origin/main", "")[0] == "refs/remotes/origin/main"
      and okf.PROTECTED_BRANCH == "main")
shutil.rmtree(at, ignore_errors=True)

# ===================== NOTHING SHADOWS A MODULE THE GATE IMPORTS =====================
# Codex's P1 on f209b38: `python tests/x.py` puts tests/ at the front of sys.path and
# `python scripts/x.py` puts scripts/ there, so a non-operator could add tests/json.py or
# scripts/json.py - paths this rule allowed - and once merged they would execute at import time
# on EVERY later gate run, before either protected file, needing only to exit 0 to keep the check
# green while bypassing the tests and the decision.
print()
print("a file that shadows a module the gate imports is the gate, by another road")

for who in ("grok/x", "codex/x", "pi1-cli/notes", "gemini/okf-1"):
    check("%s may not add a module the gate imports" % who,
          not okf.allowed(who, "scripts/json.py") and not okf.allowed(who, "tests/json.py")
          and not okf.allowed(who, "scripts/subprocess.py"))
check("the operator may, because someone has to be able to repair it",
      okf.allowed("claude-code-cli/x", "scripts/json.py")
      and okf.allowed("vm-claude-code-cli/x", "tests/re.py"))
check("and the fence is exactly as wide as the risk, not wider",
      okf.allowed("grok/x", "scripts/deeper/json.py")      # never on sys.path[0]
      and okf.allowed("grok/x", "scripts/json.txt")        # not importable
      and okf.allowed("grok/x", "scripts/notashadow.py")   # a name the gate does not import
      and okf.allowed("grok/x", "docs/json.py"))           # not a directory the gate runs from
check("no grant reaches a shadow module either",
      not okf.allowed("grok/x", "scripts/json.py",
                      floor("- grant: grok | call: %s | paths: docs/okf/" % CALL), CALL))

# THE TUPLE MUST NOT DRIFT. If either file gains a top-level import this fence does not name, the
# fence has a hole and nothing else would say so.
_imports = set()
for _f in (RULE, os.path.join(HERE, "test_okf_ownership.py")):
    if not os.path.isfile(_f):
        continue
    with open(_f, encoding="utf-8") as _read:
        _tree = ast.parse(_read.read())
    for _node in _tree.body:                      # top level only: that is when shadowing bites
        if isinstance(_node, ast.Import):
            _imports.update(a.name.split(".")[0] for a in _node.names)
        elif isinstance(_node, ast.ImportFrom) and _node.module and _node.level == 0:
            _imports.add(_node.module.split(".")[0])
_missed = sorted(m for m in _imports if m not in okf.SHADOWABLE and m != "okf_ownership")
check("SHADOWABLE names every top-level import of the gate's two files", not _missed,
      "not fenced: %s" % ", ".join(_missed))

# ===================== A CROOKED FLOOR MUST BE REPAIRABLE =====================
# Codex on f209b38: once a crooked floor is on the protected branch, refusing every run fails
# EVERY later pull request - including the one restoring the floor. A gate that cannot be repaired
# is a denial of service wearing a security control.
print()
print("a crooked floor on the protected branch can still be repaired")

at = repo(floor_text=floor(GRANT_GROK))
subprocess.run(("git", "rm", "-q", "--cached", "docs/okf/floor.md"), cwd=at, check=True,
               capture_output=True)
put(at, "elsewhere.md", floor(GRANT_GROK))
_blob = subprocess.run(("git", "hash-object", "-w", "--stdin"), cwd=at, input="elsewhere.md",
                       capture_output=True, text=True, check=True).stdout.strip()
subprocess.run(("git", "update-index", "--add", "--cacheinfo",
                "120000,%s,docs/okf/floor.md" % _blob), cwd=at, check=True, capture_output=True)
subprocess.run(("git", "add", "elsewhere.md"), cwd=at, check=True, capture_output=True)
subprocess.run(("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
                "the floor becomes a symlink"), cwd=at, check=True, capture_output=True)
BENT = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
code, said = run(["grok/x", "--trusted-base", BENT, "--base-ref", "main",
                  "--changed-from", changed_list(at, OKF_INDEX)], root=at)
check("an ordinary branch is still refused on a crooked floor", code == 1 and "symlink" in said,
      said.strip())
code, said = run(["claude-code-cli/repair", "--trusted-base", BENT, "--base-ref", "main",
                  "--changed-from", changed_list(at, okf.FLOOR)], root=at)
check("and the operator, changing that page ALONE, is the way out",
      code == 0 and "way out" in said, said.strip())
code, said = run(["claude-code-cli/repair", "--trusted-base", BENT, "--base-ref", "main",
                  "--changed-from", changed_list(at, okf.FLOOR, OKF_INDEX)], root=at)
check("but not while carrying another page with it", code == 1, said.strip())
shutil.rmtree(at, ignore_errors=True)

# THE REANCHORING, EXECUTED. This is the mechanism Codex said remained unclosed across three
# reviews: candidate code repoints the local remote-tracking ref at a commit of its own carrying an
# otherwise unmodified rule and a FABRICATED GRANT, and every read the rule made through
# `git show refs/remotes/origin/main:<path>` then agreed with the fabrication. Here the base commit
# carries no grant at all, refs/remotes/origin/main carries one, and the checkout is the base. The
# gate must read the tree it is standing in.
#
# It is also the control that was missing. A racing pair of mutation runs left the gate reading
# `_show("origin/main", FLOOR)` in place of `worktree_floor()` and the suite still passed 82 of 82,
# because every other fixture has HEAD and refs/remotes/origin/main at the same commit, so nothing
# could tell the two reads apart.
at = repo(floor_text=floor())                      # the base: no grant to anyone
BASE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
put(at, "docs/okf/floor.md", floor(GEMINI_GRANT))  # a commit that hands gemini a page
commit(at, "a fabricated grant, on a commit of the candidate's own")
FAKE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
subprocess.run(("git", "update-ref", "refs/remotes/origin/main", FAKE), cwd=at, check=True,
               capture_output=True)
subprocess.run(("git", "reset", "-q", "--hard", BASE), cwd=at, check=True, capture_output=True)
check("the reanchored ref really does carry the fabricated grant, which is the hole",
      "gemini" in subprocess.run(("git", "show", "refs/remotes/origin/main:docs/okf/floor.md"),
                                 cwd=at, capture_output=True, text=True).stdout)
code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", changed_list(at, "docs/okf/lanes.md")], root=at)
check("and the gate reads its own checkout, so the fabricated grant admits nothing",
      code == 1 and "docs/okf/lanes.md" in said, said.strip())
# The same fixture, read the old way, to prove the control is not vacuous: the ref's grant is real
# enough to admit if anything believed it.
check("while the fabricated grant WOULD have admitted, had a ref been believed",
      okf.allowed("gemini/okf-1", "docs/okf/lanes.md",
                  okf._show("refs/remotes/origin/main", "docs/okf/floor.md", at), CALL))
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(GEMINI_GRANT))
BASE = subprocess.run(("git", "rev-parse", "HEAD"), cwd=at, capture_output=True,
                      text=True).stdout.strip()
LANES = changed_list(at, "docs/okf/lanes.md")
# And a candidate tree cannot wear the posture: once a commit lands on top, HEAD is not the base.
put(at, "docs/okf/lanes.md", "a candidate commit\n")
commit(at, "the candidate's own commit")
code, said = run(["gemini/okf-1", "--trusted-base", BASE, "--base-ref", "main",
                  "--changed-from", LANES], root=at)
check("a checkout that is not the base commit refuses outright",
      code == 1 and "not running out of the base tree" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

# These two checks read the workflow files off the disk. A review harness that loads the rule's
# source from pinned git objects into another checkout reads that checkout's files, which are a
# different version and not what is under test, so they say so instead of failing.
GATE_AT = os.path.join(HERE, "..", ".github", "workflows", "okf-ownership-trusted.yml")
FLOW_AT = os.path.join(HERE, "..", ".github", "workflows", "okf-ownership.yml")
SAME_TREE = False
if os.path.isfile(RULE):
    with open(RULE, encoding="utf-8") as read:
        SAME_TREE = "def trusted_posture(" in read.read()
if not (SAME_TREE and os.path.isfile(GATE_AT) and os.path.isfile(FLOW_AT)):
    print("  skip the gate workflow's own shape"
          " (the files on this disk are not the version under test)")
else:
    with open(GATE_AT, encoding="utf-8") as read:
        gate = read.read()
    live = [line for line in gate.splitlines() if not line.lstrip().startswith("#")]
    ran = " ".join(live)
    check("the gate is the one trigger whose definition comes from the base branch",
          "pull_request_target:" in ran and "pull_request:" not in ran, ran[:120])
    check("it checks out the forge-named base commit",
          "ref: ${{ github.event.pull_request.base.sha }}" in ran)
    check("and never the candidate's commit",
          not any("ref:" in line and "pull_request.head" in line for line in live),
          [line for line in live if "ref:" in line])
    check("it hands the rule the posture, the target branch and the forge's list",
          all(flag in ran for flag in ("--trusted-base", "--base-ref", "--changed-from")))
    # TWO MUTANTS SURVIVED FOR WANT OF THESE. I controlled what the RULE does with a fork and
    # with its sys.path, and never that the WORKFLOW still hands it either - so dropping
    # `--head-repo`, or dropping `-I`, changed nothing any control could see. The wiring is part
    # of the repair, not a detail of it.
    check("it tells the rule which repository the head came from",
          "--head-repo" in ran and "--this-repo" in ran
          and "head.repo.full_name" in ran and "github.repository" in ran)
    check("and it runs python ISOLATED, so no file can shadow a module the gate imports",
          ran.count("python -I -B") >= 2 and "python -B " not in ran)
    check("it asks the forge for both ends of a rename",
          "previous_filename" in ran and "--name-only" not in ran)
    check("it reads no ref for its authority",
          "refs/remotes/origin/main" not in ran and "git show" not in ran)
    grants = [line.strip() for line in live
              if line.strip().endswith((": read", ": write", ": none"))
              and not line.strip().startswith("-")]
    check("its token can read and not write",
          sorted(grants) == ["contents: read", "pull-requests: read"], grants)
    # Codex's round-two findings, each one a property of the workflow text.
    check("it refuses a base that is no longer the protected branch's tip",
          "git/ref/heads/" in ran and 'tip}" != "${BASE_SHA}"' in ran and "exit 1" in ran)
    check("and it re-runs when the base branch is retargeted",
          "edited" in ran, "the edited trigger is what carries base retargeting")
    check("the forge's list is JSON, not lines that would be stripped",
          "@json" in ran and "jq -s" in ran and "changed.json" in ran
          and "changed.txt" not in ran)
    # THE TREE-MODE STEP MUST ACTUALLY RUN. Codex reproduced `gh api --jq --arg` failing with
    # "accepts 1 arg(s), received 4" twice against exact heads: the `|| true` swallowed it, every
    # mode came back empty, and the guard skipped the refusal - a check reporting success for
    # never having run. gh embeds jq and takes ONE filter argument, and the error is not masked.
    check("the candidate tree-mode check uses a form gh accepts, and masks no error",
          "--jq --arg" not in ran and "|| true)" not in ran
          and 'git/trees/${HEAD_SHA}:${dir}' in ran
          and '= "404" ]' in ran and 'tcode}" != "200" ]' in ran)
    check("the candidate floor fails closed: only a confirmed 404 is absence",
          # The `= "200"` branch is asserted too, because without it the three refusals below can
          # be left in place while the branch that guards them is turned into `true` - which is
          # what a surviving mutant did.
          '= "404" ]' in ran and 'elif [ "${code}" = "200" ]' in ran
          and '!= "file" ]' in ran and '!= "base64" ]' in ran
          and '!= "${size}" ]' in ran
          and ran.count("exit 1") >= 5, ran.count("exit 1"))

    check("and its actions are pinned to commit shas, not tags",
          "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in ran
          and "@v4" not in ran and "@v5" not in ran)
    with open(FLOW_AT, encoding="utf-8") as read:
        advisory = read.read()
    said_live = " ".join(line for line in advisory.splitlines()
                         if not line.lstrip().startswith("#"))
    check("the pull_request workflow calls itself advisory and claims nothing",
          "advisory (not the gate)" in said_live
          and "refs/remotes/origin/main" not in said_live
          and "--trusted-base" not in said_live)


print()
print("%d passed, %d failed" % (PASS, FAIL))
for name in FAILURES:
    print("  - %s" % name)
sys.exit(1 if FAIL else 0)

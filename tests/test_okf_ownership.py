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
      not okf.allowed("pi1-cli/x", "docs/okf/floor.md")
      and not okf.allowed("pi1-cli/x", "docs/okf/gemini/RESULT-1.md"))
check("nobody else writes it without a grant",
      not any(okf.allowed(b, "docs/okf/index.md")
              for b in ("codex/x", "grok/x", "cursor/x", "copilot/x", "gemini/okf-1")))
check("every path outside the OKF passes, for everyone",
      all(okf.allowed(b, p) for b in ("codex/x", "grok/x", "cursor/x", "claude-code-cli/x")
          for p in ("scripts/append.py", "docs/POKA-YOKE.md", "src/bus_server.py", "README.md")))
check("Gemini keeps its own corner, where okf_land.py puts its RESULTs",
      okf.allowed("gemini/okf-1", "docs/okf/gemini/RESULT-1.md")
      and okf.allowed("claude-code-cli/x", "docs/okf/gemini/RESULT-1.md")
      and not okf.allowed("codex/x", "docs/okf/gemini/RESULT-1.md"))

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

# This one check reads two files off the disk. A review harness that loads the rule's source
# from pinned git objects into another checkout reads that checkout's files, which are a
# different version and not what is under test, so it says so instead of failing.
FLOW_AT = os.path.join(HERE, "..", ".github", "workflows", "okf-ownership.yml")
SAME_TREE = False
if os.path.isfile(RULE):
    with open(RULE, encoding="utf-8") as read:
        SAME_TREE = "def controls_hold(" in read.read()
if not (SAME_TREE and os.path.isfile(FLOW_AT)):
    print("  skip CI runs the rule from the protected branch"
          " (the files on this disk are not the version under test)")
else:
    with open(FLOW_AT, encoding="utf-8") as read:
        flow = read.read()
    ran = " ".join(line for line in flow.splitlines() if not line.lstrip().startswith("#"))
    check("CI runs the rule from the protected branch, not from this tree",
          'git show "refs/remotes/origin/main:scripts/okf_ownership.py"' in flow
          and "--protected origin/main" in flow and "--repo ." in flow
          and "--name-only" not in ran
          # one python run, of whichever copy the version check chose; the fallback to this tree
          # is reachable only while main predates the flags, which is only this pull request
          and sum(1 for line in flow.splitlines()
                  if "python" in line and "${RULE}" in line) == 1
          and """grep -q -- '"--protected"'""" in flow)

print()
print("%d passed, %d failed" % (PASS, FAIL))
for name in FAILURES:
    print("  - %s" % name)
sys.exit(1 if FAIL else 0)

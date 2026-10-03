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


def repo(floor_text=None, head_floor=None):
    """A repository whose HEAD is the base. head_floor is written to the working tree only, which is
    what CI hands the check: the candidate's own copy of the file."""
    at = tempfile.mkdtemp()
    def git(*args):
        subprocess.run(("git",) + args, cwd=at, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "test@sfdc24")
    git("config", "user.name", "test")
    git("config", "commit.gpgsign", "false")
    put(at, "README.md", "base\n")
    if floor_text is not None:
        put(at, "docs/okf/floor.md", floor_text)
    git("add", "-A")
    git("commit", "-qm", "base")
    if head_floor is not None:
        put(at, "docs/okf/floor.md", head_floor)
    return at


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
code, said = run(["gemini/okf-1", "--base", "HEAD", "docs/okf/lanes.md"], root=at)
check("a grant merged to the base admits the branch", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(), head_floor=floor(GEMINI_GRANT,
                                               "- grant: codex | call: %s | paths: docs/okf/" % CALL))
one, _ = run(["gemini/okf-1", "--base", "HEAD", "docs/okf/lanes.md"], root=at)
two, _ = run(["codex/x", "--base", "HEAD", "docs/okf/index.md"], root=at)
three, _ = run(["codex/x", "--base", "HEAD", "docs/okf/floor.md"], root=at)
check("a grant the branch wrote for itself admits nothing", (one, two, three) == (1, 1, 1),
      "%r" % ((one, two, three),))
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=None)
code, said = run(["gemini/okf-1", "--base", "HEAD", "docs/okf/lanes.md"], root=at)
check("no floor on the base is no grant",
      code == 1 and "carries no docs/okf/floor.md" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(GEMINI_GRANT, call=None))
code, said = run(["gemini/okf-1", "--base", "HEAD", "docs/okf/lanes.md"], root=at)
check("no call in the floor's front matter is no grant",
      code == 1 and "names no call" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(GEMINI_GRANT))
code, said = run(["gemini/okf-1", "--base", "origin/nope", "docs/okf/lanes.md"], root=at)
check("a base that cannot be read is no grant", code == 1, said.strip())
code, said = run(["gemini/okf-1", "docs/okf/lanes.md"], root=at)
check("no --base at all is no grant", code == 1 and "no --base" in said, said.strip())
code, said = run(["gemini/okf-1", "--base", "HEAD", "scripts/append.py"], root=at)
check("and a path outside the OKF still passes with no grant and no base", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor(GEMINI_GRANT, "- grant: codex | call: %s | paths: docs/okf/" % CALL))
code, said = run(["gemini/okf-1", "--base", "HEAD", "docs/okf/lanes.md"], root=at)
check("overlapping grants on the base refuse the whole run",
      code == 1 and "two grants hold one page" in said, said.strip())
shutil.rmtree(at, ignore_errors=True)

at = repo(floor_text=floor())
code, said = run(["claude-code-cli/x", "--base", "HEAD", "docs/okf/floor.md",
                  "docs/okf/index.md"], root=at)
check("the operator needs no grant", code == 0, said.strip())
shutil.rmtree(at, ignore_errors=True)

print()
print("%d passed, %d failed" % (PASS, FAIL))
for name in FAILURES:
    print("  - %s" % name)
sys.exit(1 if FAIL else 0)

#!/usr/bin/env python3
"""The OKF has one writer and everyone reads it, with the pen handed over during a call.

HIS DIRECTIVE, 2026-10-03, relayed by Grok as GROK-OKF-LIVE-WRITE-20261003T1426Z: "Claude (tag
claude-code-cli) must have write access to the OKF for three projects: the conference line,
sfdc24.com, and Blackboard ... Everyone else gets read access. If another agent needs to write
during the conference, create the rule that hands over the microphone and the pen." He had said the
same thing on the 10:03 call: "The conference is a live working session, not a discussion that waits
for execution after the meetings."

The conference line got this rule first (its `tools/check_ownership.py`, PRs 161 and 162). This is
the same rule for Blackboard, and it is DELIBERATELY NARROW:

  * it has an opinion about `docs/okf/` and about nothing else. Blackboard has no package ownership
    map and this is not the place to invent one, so every path outside the OKF passes;
  * `claude-code-cli` and `pi1-cli` both write the OKF's pages (his words of 19:45Z added the Pi);
    the floor file stays with the operator of the calls alone;
  * `docs/okf/gemini/` stays Gemini's own corner, which is where `scripts/okf_land.py` lands its
    signed RESULTs from `gemini/okf-<row>` branches;
  * a handover is read from the PROTECTED BASE (`--base`), never from the branch being checked. The
    conference's first version read the candidate's own copy of the floor file, so a branch could
    add its own grant line and be admitted by it (Codex's P1 on conference #161). Here that mistake
    is closed before it can be made;
  * `docs/okf/floor.md` is NON-DELEGABLE. No grant reaches the page that hands out the pen;
  * two grants on one page refuse the whole run and name the pair to strike;
  * a grant belongs to ONE call. Blackboard has no call plan of its own, so the floor file's own
    front matter carries the `call:` the open grants belong to, and a grant naming any other call is
    not in force. The operator changes that one line when a new call opens, which retires every
    older grant in a single edit. No clock, no job, no expiry claim.

No base, no floor on the base, or no call in the floor's front matter means NO GRANT IS IN FORCE,
and the check says which of those it was rather than quietly refusing.

AND THE GRANT IS ONLY AS GOOD AS WHAT THE RULE CAN SEE. Codex's exact-head review of c33cf11
(2026-10-03 20:26Z, CODEX-OKF-GUARD-RESULT) got past the version above six ways, each closed here:

  * the authority is the PROTECTED BRANCH, named as such (`--protected`, or a `--base` that names
    it), and never the base a pull request chose for itself;
  * the floor's front matter must name EXACTLY ONE call; two is ambiguous and puts nothing in force;
  * what changed is git's own account (`changed()`), `-z` with `-M -C`, so a rename names both of
    its ends and no path comes back C-quoted;
  * a path handed in quoted is unquoted before it is judged, and git's output is read as UTF-8
    rather than this box's code page;
  * a branch may not PROPOSE two pens on one page, and a protected floor already in that state
    refuses every run but one: the operator's repair of that page, alone in its pull request;
  * the rule answers its own controls before it judges anything (`controls_hold`), and CI runs the
    copy from the protected branch, outside the checkout, because the branch being judged was
    supplying its own judge.

A branch prefix is attribution, not an authenticated identity: everyone pushes as the same account.
That is as true here as it is on the conference line, and a grant trusts the prefix the same way.

    python scripts/okf_ownership.py <branch> [--base <ref>] <changed-path>...
"""
import re
import subprocess
import sys
from pathlib import Path

OKF = "docs/okf/"
# The operator of the calls: his words name the tag claude-code-cli, and the same agent pushes from
# the VM under its own prefix. The operator writes the floor file, because that is the page that
# hands out the pen.
OPERATORS = ("claude-code-cli/", "vm-claude-code-cli/")
# Standing write to the OKF's own pages. His words, 2026-10-03 19:45Z: "can you add pi1-cli for read
# write access to OKF". Read was already everyone's; this is the write. pi1-cli runs the Raspberry
# Pi, which his 10:03 call made the product surface, so what it learns there lands in the OKF
# directly rather than through a floor grant per call.
#
# It is NOT given docs/okf/floor.md. That page hands the pen to any agent for any OKF path, so a
# second writer on it is the self-grant hole Codex found on conference #161 in another shape. If he
# wants pi1-cli to hand out pens as well, one word changes this tuple.
WRITERS = OPERATORS + ("pi1-cli/",)
GEMINI = OKF + "gemini/"
FLOOR = OKF + "floor.md"
OPEN_GRANTS = "## Open grants"
GRANT = re.compile(r"^\s*-\s*grant:\s*([a-z0-9-]+)\s*\|\s*call:\s*([^|]+?)\s*\|\s*paths:\s*(.+?)\s*$",
                   re.M)
# The refs that may carry grant authority. A grant is the protected branch's word, so the ref it is
# read from has to BE the protected branch, not the base a pull request chose for itself: a PR may
# target an unmerged branch of its own and that branch may carry anything (Codex 20:26Z, exact-head
# review of conference 162 and this PR). A closed list, never a pattern: `main-ish` is not main.
PROTECTED = ("main", "origin/main", "refs/heads/main", "refs/remotes/origin/main")


def front_matter(text: str) -> str:
    """The `---` block at the top of the file, and nothing after it: a `call:` further down the page
    is prose, or an example, and must not set what the grants belong to."""
    if not (text or "").startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end > 0 else ""


def open_section(floor_text: str) -> str:
    """The lines under the Open grants heading, and nothing else in the file.

    The page explains its own shape further down, with an example grant line. Reading the whole file
    made that example a live grant on the conference line, found by its own test.
    """
    text = floor_text or ""
    at = text.find(OPEN_GRANTS)
    if at < 0:
        return ""
    rest = text[at + len(OPEN_GRANTS):]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def grants(floor_text: str, call: str = None) -> dict:
    """agent prefix -> the OKF paths it may write while the call is open.

    `call` given filters to the grants written for it; given empty, nothing is in force; None asks
    only what the file says, whatever call its lines name.
    """
    if call is not None and not call:
        return {}
    out = {}
    for agent, grant_call, paths in GRANT.findall(open_section(floor_text)):
        if call is not None and grant_call.strip() != call:
            continue
        for path in (p.strip() for p in paths.split(",")):
            if path.startswith(OKF) and ".." not in path:
                out.setdefault(agent + "/", set()).add(path)
    return out


def covers(granted: str, path: str) -> bool:
    """Whether a granted path covers `path`: the same file, or a folder that holds it."""
    return path == granted or (granted.endswith("/") and path.startswith(granted))


def overlapping(grants_map: dict):
    """The first pair of DIFFERENT agents granted one page. Two pens on one page is not a handover."""
    held = sorted((agent, path) for agent, paths in grants_map.items() for path in paths)
    for i, (agent, path) in enumerate(held):
        for other, other_path in held[i + 1:]:
            if agent != other and (covers(path, other_path) or covers(other_path, path)):
                return agent, path, other, other_path
    return None


def handed_over(branch: str, path: str, floor_text: str, call: str = None) -> bool:
    """Whether the floor hands `branch`'s agent the pen for `path`, right now."""
    for prefix, paths in grants(floor_text, call).items():
        if branch.startswith(prefix):
            return any(covers(p, path) for p in paths)
    return False


def floor_call(floor_text: str) -> tuple:
    """(the one call the floor's open grants belong to, why there is none).

    The front matter, and EXACTLY one `call:` line. Two of them means the page declares two calls
    and whoever reads the first gets a different answer from whoever reads the last, so the grants
    would be bound to a call nobody is having: the ambiguity is refused rather than guessed (Codex
    20:26Z found the pair on the conference line, where the plan declared the call).
    """
    said = []
    for line in front_matter(floor_text or "").splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "call" and value.strip():
            said.append(value.strip())
    if not said:
        return "", "its front matter names no call"
    if len(said) > 1:
        return "", "its front matter names %d calls (%s), which is ambiguous" % (
            len(said), ", ".join(repr(s) for s in said))
    return said[0], ""


def authority(base: str, protected: str) -> tuple:
    """(the ref a grant may be read from, why none can be). Never a ref the branch chose itself.

    `--protected` says which ref is the protected branch and is believed only when it names one;
    `--base` is taken as the authority only when it names one too, so `--base origin/main` keeps
    working while `--base origin/an-unmerged-branch` carries nothing at all.
    """
    for ref in (protected, base):
        if ref and ref in PROTECTED:
            return ref, ""
    if protected:
        return "", "%r is not the protected branch, so it carries no grant" % protected
    if base:
        return "", "%r is not the protected branch, so no grant is read from it" % base
    return "", "no protected branch was named, so no grant can be in force"


def _unquote(path: str) -> str:
    """A path as git prints it, with C-quoting undone, so it is the path it names.

    git quotes a name that is not plain ASCII unless it is told not to, and the quoted string is a
    DIFFERENT path from the real one: it started with a quote mark, so it was not in docs/okf/ and
    the check had no opinion about it (Codex 20:26Z).
    """
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if body[i + 1:i + 4].isdigit() and all(d in "01234567" for d in body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            out.extend({"n": b"\n", "t": b"\t", "r": b"\r", '"': b'"', "\\": b"\\"}
                       .get(nxt, nxt.encode("utf-8")))
            i += 2
            continue
        out.extend(char.encode("utf-8"))
        i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return path


def changed(base: str, root=None) -> tuple:
    """(every path this branch changes, why git could not say), from git and not from the caller.

    `git diff --name-only` is not the changed set: a RENAME prints only where the file landed, so
    the floor file under another name would never be seen here. `-z` with `--name-status -M -C`
    gives the status, BOTH ends of a rename or a copy, and no quoting at all.
    """
    at = Path(root) if root else Path(__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "-c", "core.quotePath=false", "diff", "-z", "--name-status",
                               "-M", "-C", "%s...HEAD" % base],
                              cwd=str(at), capture_output=True, text=True,
                              encoding="utf-8", errors="surrogateescape")
    except OSError as broke:
        return set(), "git could not be run in %s (%s)" % (at, broke)
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or [""])[-1]
        return set(), "git could not diff %s...HEAD (%s)" % (base, last.strip())
    fields = [f for f in done.stdout.split("\0") if f != ""]
    paths, i = set(), 0
    while i < len(fields):
        status, i = fields[i], i + 1
        both = 2 if status[:1] in ("R", "C") else 1      # a rename or a copy names both of its ends
        paths.update(fields[i:i + both])
        i += both
    return paths, ""


def _candidate_floor(root=None) -> str:
    """docs/okf/floor.md as the BRANCH has it: its working copy, or its HEAD when there is none.

    Read only to refuse, never to admit. A branch may not write itself a grant, and it may not
    propose two pens on one page either, which the base-only check let through (Codex 20:26Z).
    """
    at = Path(root) if root else Path(__file__).resolve().parent.parent
    here = at.joinpath(*FLOOR.split("/"))
    if here.is_file():
        return here.read_text(encoding="utf-8")
    return _show("HEAD", FLOOR, root) or ""


def _show(base: str, path: str, root=None) -> str:
    """A file as the base ref has it, or None when it is not there to read."""
    at = Path(root) if root else Path(__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "show", "%s:%s" % (base, path)], cwd=str(at),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def in_force(base: str, root=None):
    """(floor text, the call its grants belong to, why nothing is in force), from the base ref."""
    if not base:
        return "", "", "no --base was given, so no grant from the protected base can be in force"
    floor = _show(base, FLOOR, root)
    if floor is None:
        return "", "", "%s carries no %s, so no grant is in force" % (base, FLOOR)
    call, why = floor_call(floor)
    if not call:
        return floor, "", "%s on %s: %s, so no grant belongs to a call" % (FLOOR, base, why)
    return floor, call, ""


def allowed(branch: str, path: str, floor: str = "", call: str = None) -> bool:
    """Whether `branch` may change `path`. Only `docs/okf/` is this check's business."""
    if not path.startswith(OKF):
        return True
    if path == FLOOR:
        # The page that hands out the pen stays with the operator of the calls.
        return any(branch.startswith(p) for p in OPERATORS)
    if path.startswith(GEMINI):
        # Gemini's own corner, where scripts/okf_land.py lands its signed RESULTs.
        return branch.startswith("gemini/") or any(branch.startswith(p) for p in OPERATORS)
    if any(branch.startswith(p) for p in WRITERS):
        return True
    return handed_over(branch, path, floor, call)


# The answers this rule must still give before it judges anything. Each is a control: a tampered
# decision cannot keep them all, so the run refuses instead of admitting (Codex 20:26Z: CI ran the
# branch's own copy of the rule, and a `True` in place of allowed() admitted the floor file).
_GRANTED = OPEN_GRANTS + "\n- grant: grok | call: a call | paths: " + OKF + "index.md\n"
_GRANTED_WIDE = OPEN_GRANTS + "\n- grant: grok | call: a call | paths: " + OKF + "\n"
_CONTROLS = (
    # branch, path, the floor it is judged against, and whether it may write it
    ("grok/x", FLOOR, "", False),
    ("pi1-cli/notes", FLOOR, "", False),
    ("gemini/okf-1", FLOOR, "", False),
    ("claude-code-cli/x", FLOOR, "", True),
    ("grok/x", OKF + "index.md", "", False),
    ("codex/x", OKF + "index.md", "", False),
    ("gemini/okf-1", OKF + "index.md", "", False),
    ("pi1-cli/notes", OKF + "index.md", "", True),
    ("claude-code-cli/x", OKF + "index.md", "", True),
    ("codex/x", GEMINI + "RESULT-1.md", "", False),
    ("gemini/okf-1", GEMINI + "RESULT-1.md", "", True),
    ("grok/x", "scripts/append.py", "", True),          # outside the OKF this rule has no opinion
    ("grok/x", OKF + "index.md", _GRANTED, True),       # an open grant still hands the pen over
    ("grok/x", FLOOR, _GRANTED_WIDE, False),            # and never the page that hands it out
)


def controls_hold() -> str:
    """"" while this rule still answers its own controls, else the first answer it got wrong.

    A rule that lives in the tree it judges can be edited by the branch it is judging, and CI ran
    the candidate's copy of it. The workflow now runs the copy on the protected branch; this is the
    other half of that, inside the rule: before a single path is judged, the answers that must not
    change are checked. It costs a dozen calls and it fails closed.
    """
    for branch, path, floor, may in _CONTROLS:
        if bool(allowed(branch, path, floor, None)) is not may:
            return "%s %s %s" % (branch, "may not write" if may else "may write", path)
    return ""


def _taken(argv: list, flag: str) -> str:
    """The value of `flag`, removed from argv; "" when it is not there, or has no value."""
    if flag not in argv:
        return ""
    at = argv.index(flag)
    value = argv[at + 1] if at + 1 < len(argv) else ""
    del argv[at:at + 2]
    return value


USAGE = ("usage: okf_ownership.py <branch> [--protected <ref>] [--base <ref>] [--repo <path>]"
         " <changed-path>...")


def main(argv, root=None) -> int:
    argv = list(argv)
    base = _taken(argv, "--base")
    protected = _taken(argv, "--protected")
    root = root or _taken(argv, "--repo") or None
    if not argv:
        print(USAGE)
        return 2
    branch, paths = argv[0], {_unquote(p) for p in argv[1:]}
    broken = controls_hold()
    if broken:
        print("REFUSED: this rule no longer holds its own controls (%s), so it judges nothing"
              % broken)
        return 1
    # A grant is read from the protected branch, which is NOT the branch's own base ref.
    ref, why_authority = authority(base, protected)
    floor, call, why = in_force(ref, root)
    if not ref:
        why = why_authority
    # What changed is git's account, not the caller's: a rename names both of its ends.
    if base:
        found, blind = changed(base, root)
        if blind:
            # The operator may be judged on the set it was given: the floor is already its own, so
            # no rename of it hands it anything it does not have. For anyone else writing the OKF
            # the unseen rename IS the escalation, and outside the OKF this rule has no opinion.
            if any(p.startswith(OKF) for p in paths) and not any(branch.startswith(p)
                                                                 for p in OPERATORS):
                print("REFUSED: %s, so the changed paths cannot be confirmed and a rename of %s"
                      " would be invisible" % (blind, FLOOR))
                return 1
            print("NOTE: %s; judging the %d path(s) given" % (blind, len(paths)))
        paths |= found
    if not paths:
        print(USAGE)
        return 2
    # Two pens on one page: refused when this branch proposes it, and when the protected floor
    # already carries it - except for the one repair that can close that.
    candidate = _candidate_floor(root)
    proposed = overlapping(grants(candidate, None)) if candidate and candidate != floor else None
    if proposed:
        print("REFUSED: this branch's own %s hands one page to two agents: %s has %s and %s has %s."
              % ((FLOOR,) + proposed))
        return 1
    clash = overlapping(grants(floor, call))
    if clash:
        if any(branch.startswith(p) for p in OPERATORS) and paths == {FLOOR}:
            print("OK: %s on the protected branch hands one page to two agents (%s has %s and %s"
                  " has %s); the operator's repair of that page is the way out."
                  % ((FLOOR,) + clash))
            return 0
        print("REFUSED: two grants hold one page: %s has %s and %s has %s. Strike one." % clash)
        return 1
    okf = sorted(p for p in paths if p.startswith(OKF))
    bad = sorted(p for p in paths if not allowed(branch, p, floor, call))
    for p in bad:
        print("REFUSED: %s is in the OKF, which %s may not write" % (p, branch.split("/")[0]))
    if bad and why:
        print("NO GRANT COULD APPLY: %s" % why)
    if not bad:
        print("OK: %d OKF path(s) of %d, for %s%s"
              % (len(okf), len(paths), branch.split("/")[0],
                 (", floor from %s" % ref) if ref else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

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
# The call the open grants belong to, in the floor file's own front matter.
CALL = re.compile(r"^call:\s*(.+?)\s*$", re.M)


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


def _show(base: str, path: str, root=None) -> str:
    """A file as the base ref has it, or None when it is not there to read."""
    at = Path(root) if root else Path(__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "show", "%s:%s" % (base, path)], cwd=str(at),
                              capture_output=True, text=True)
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
    found = CALL.search(front_matter(floor))
    if not found:
        return floor, "", "%s names no call in %s's front matter, so no grant belongs to a call" % (
            base, FLOOR)
    return floor, found.group(1).strip(), ""


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


def main(argv, root=None) -> int:
    argv = list(argv)
    base = ""
    if "--base" in argv:
        at = argv.index("--base")
        if at + 1 >= len(argv):
            print("usage: okf_ownership.py <branch> [--base <ref>] <changed-path>...")
            return 2
        base = argv[at + 1]
        del argv[at:at + 2]
    if len(argv) < 2:
        print("usage: okf_ownership.py <branch> [--base <ref>] <changed-path>...")
        return 2
    branch, paths = argv[0], argv[1:]
    floor, call, why = in_force(base, root)
    clash = overlapping(grants(floor, call))
    if clash:
        print("REFUSED: two grants hold one page: %s has %s and %s has %s. Strike one." % clash)
        return 1
    okf = [p for p in paths if p.startswith(OKF)]
    bad = [p for p in paths if not allowed(branch, p, floor, call)]
    for p in bad:
        print("REFUSED: %s is in the OKF, which %s may not write" % (p, branch.split("/")[0]))
    if bad and why:
        print("NO GRANT COULD APPLY: %s" % why)
    if not bad:
        print("OK: %d OKF path(s) of %d, for %s%s"
              % (len(okf), len(paths), branch.split("/")[0],
                 (", floor from %s" % base) if base else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

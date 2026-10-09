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
  * `docs/okf/gemini/` is an ORDINARY OKF PAGE here, with no standing exception for `gemini/*`.
    The README sitting in that folder is the policy of record: Gemini lands its signed RESULTs in
    the PRIVATE conference repository, "never here", because Codex's security review of #304
    (5e2a4cb) ruled that no free model text from the board goes to a public repository - and this
    repository is public. `scripts/okf_land.py` hard-codes conference for the same reason. The
    rule used to grant `gemini/*` unconditional write to that path and so contradicted all three
    (Codex's second P1 on 2ec9ee0). Gemini is handed that corner by a floor grant like anyone
    else, which is the reviewed path;
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
  * the rule answers its own controls before it judges anything (`controls_hold`).

AND CODEX GOT PAST THAT THREE MORE WAYS (00:25Z, on c33cf11 and conference a4abec9):

  * the short name was shadowable - `git show origin/main:f` prefers `refs/heads/origin/main`, and
    anyone may push a branch by that name - so every short name now resolves through `FULL`;
  * the decision to USE the trusted copy was the candidate's, since the step that extracts it lives
    in the candidate's workflow, so the rule proves which copy it is (`trusted_copy`) and honours no
    grant while it is not the protected branch's own;
  * a collision is judged within one call (`overlapping_in_a_call`), because a grant retired with an
    older call was blocking a valid new one;
  * and every `call:` KEY counts, not every non-empty value.

AND THEN THE ONE THAT COULD NOT BE CLOSED IN THIS FILE AT ALL (Codex's P1 on 2ec9ee0 and on
conference 133529f, the third review running: "independent enforcement remains absent...
Self-comparison cannot establish independent authority"). Two holes, both structural:

  * a `pull_request` workflow's own definition comes from the candidate's merge ref, so the branch
    being judged could replace the enforcement step with a no-op, or delete the job;
  * everything above was read with `git show <ref>:<path>` against a LOCAL remote-tracking ref,
    and candidate code - run by an earlier step of that same candidate workflow, `python
    tests/test_okf_ownership.py` out of the candidate's tree - could repoint it at a commit of its
    choosing carrying an unmodified rule and a fabricated grant. `trusted_copy()` then compared
    two things the same contamination reached and agreed with the fabrication. No amount of
    hashing fixes that: a hash of a contaminated ref is a hash of a contaminated ref.

So provenance stopped being claimed from inside this file. THE GATE IS
`.github/workflows/okf-ownership-trusted.yml`, a `pull_request_target` workflow - the one
pull-request trigger whose definition GitHub takes from the BASE BRANCH - which checks out the
forge-named base commit, never the candidate's, runs no candidate code at all, and hands this rule:

    --trusted-base <base sha>      and `trusted_posture()` REFUSES unless `git rev-parse HEAD` IS
                                   that sha, so the posture cannot be claimed from a command line
    --base-ref <branch>            which must name the protected branch, or no grant counts: a
                                   pull request may target an unmerged branch of its own
    --changed-from <file>          the forge's own `pulls/<n>/files`, both ends of every rename,
                                   instead of a local diff against a ref that could be moved
    --candidate-floor <file>       the branch's own floor.md, fetched as DATA and read ONLY to
                                   refuse two pens on one page. Never checked out, never executed

There is NO FALLBACK. A missing changed-path list, a checkout that is not the base commit, or a
base that is not the protected branch all refuse. Run without `--trusted-base` - which is what
`okf-ownership.yml` and a person at a terminal do - the rule prints `ADVISORY ... this run is not
the gate and decides nothing`, and `trusted_copy()` still withholds every grant.

AND THEN FOUR MORE, ON THE GATE ITSELF (Codex on a1e1e4f and conference 2496937):

  * A PATH IS BYTES. `paths_from()` read one path per line and stripped each one, which is lossy:
    a leading or trailing space is a valid path byte, so the unowned `" docs/okf/index.md"`
    arrived as the owned `"docs/okf/index.md"` and changed the answer. The forge's list is a JSON
    array now and nothing is transformed at all.
  * AN AUTHORITY FILE MUST BE A REGULAR BLOB. Reading the floor from the working tree followed
    symlinks, which is a bypass with no edit to the protected path: point `docs/okf/floor.md` at
    an ordinary file elsewhere ONCE, and every later edit to that target hands out the pen while
    the non-delegable path looks untouched. `authority_at()` reads it through the commit's own
    tree entry and refuses mode `120000`, or anything that is not a blob - so a checkout that
    materialises symlinks as text cannot disguise one either.
  * THE CANDIDATE FLOOR FAILS CLOSED. The workflow turned every fetch failure into an empty
    floor - a 403, a 500, a `type: symlink`, and the documented case where the contents API omits
    base64 content for a blob over 1 MiB - and an empty floor proposes nothing, so each of those
    silently skipped the proposed-overlap refusal. Only a confirmed 404 means absence now; only a
    `file` with base64 content whose decoded length matches the declared size is read; anything
    else fails the gate.
  * THE FILES THAT DECIDE ARE THE OPERATOR'S. Everything outside `docs/okf/` passes this rule by
    design, and that included this rule, its suite and BOTH workflow definitions - so any branch
    could weaken the gate while keeping the expected check name, and later pull requests would
    pass a toothless check. `AUTHORITY` is refused to every prefix but the operator's, and no
    grant reaches it however wide.

AND WHAT THE REPOSITORY STILL HAS TO SAY, which no file here can. Measured 2026-10-04: `main`'s
protection requires exactly one context, `required-ci`, with `strict: false` and zero required
approvals. So a stale green tick can still be merged, and this gate is not yet a merge barrier at
all. The gate refuses a base that is not the protected branch's current tip, which is strict-base
enforced from the inside; making it binding needs `okf-ownership / the gate` added as a required
context AND "require branches to be up to date", and both are the repository owner's settings.

WHAT NONE OF IT SETTLES: a branch prefix is attribution, not an authenticated identity. The whole
fleet pushes as one GitHub account, so this rule says which paths a prefix may write and not who
holds it. That needs a GitHub App per agent, which is a build and his word.

A branch prefix is attribution, not an authenticated identity: everyone pushes as the same account.
That is as true here as it is on the conference line, and a grant trusts the prefix the same way.

    python scripts/okf_ownership.py <branch> [--base <ref>] <changed-path>...
"""
import json
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
# docs/okf/gemini/ holds a README and one historical write-up, and NOT a standing exception. The
# policy of record is the README sitting in it: Gemini lands its signed RESULTs in the PRIVATE
# conference repository, "never here", because Codex's security review of #304 (5e2a4cb,
# 2026-09-30) ruled that no free model text from the board goes to a public repository - and this
# repository is public. scripts/okf_land.py hard-codes conference for the same reason. The rule
# used to contradict all three by granting gemini/* unconditional write to this path (Codex's
# second P1 on 2ec9ee0). The exception is gone: Gemini is handed this corner by a floor grant like
# anyone else, which is the reviewed path, and the controls below prove both halves.
GEMINI = OKF + "gemini/"
FLOOR = OKF + "floor.md"
# THE FILES THAT DECIDE. Everything outside docs/okf/ passes this rule by design - Blackboard has
# no package ownership map and this is not the place to invent one - but that included the rule
# itself, its suite and both workflow definitions, so any branch could weaken the gate and keep
# the expected check name (Codex's third P1 on a1e1e4f). They are the operator's alone now. It is
# not identity: a prefix is attribution, and the fleet pushes as one account. It is the same level
# of control as the floor file, applied to the things that enforce the floor file.
# THE MODULES THE GATE IMPORTS, which can be shadowed by a file that merely SITS somewhere.
# `python tests/x.py` puts `tests/` at the front of sys.path and `python scripts/x.py` puts
# `scripts/` there, so `tests/json.py` or `scripts/json.py` - paths this rule happily allowed -
# would execute at import time on every later gate run, before either protected file, and need
# only exit 0 to keep the check green while bypassing everything (Codex's P1 on f209b38). The
# gate runs `python -I`, which drops the script directory from sys.path and closes that at the
# source; this tuple is the belt behind that brace, and a control asserts it still names every
# top-level import of both files, so it cannot drift silently.
SHADOWABLE = ("ast", "json", "re", "subprocess", "sys", "pathlib", "contextlib", "importlib",
              "io", "os", "shutil", "tempfile", "unittest")
SHADOW_DIRS = ("scripts/", "tests/")
AUTHORITY = ("scripts/okf_ownership.py",
             "tests/test_okf_ownership.py",
             ".github/workflows/okf-ownership.yml",
             ".github/workflows/okf-ownership-trusted.yml")
OPEN_GRANTS = "## Open grants"
GRANT = re.compile(r"^\s*-\s*grant:\s*([a-z0-9-]+)\s*\|\s*call:\s*([^|]+?)\s*\|\s*paths:\s*(.+?)\s*$",
                   re.M)
# The refs that may carry grant authority. A grant is the protected branch's word, so the ref it is
# read from has to BE the protected branch, not the base a pull request chose for itself: a PR may
# target an unmerged branch of its own and that branch may carry anything (Codex 20:26Z, exact-head
# review of conference 162 and this PR). A closed list, never a pattern: `main-ish` is not main.
PROTECTED = ("main", "origin/main", "refs/heads/main", "refs/remotes/origin/main")
# THE PROTECTED BRANCH'S NAME, which is not the same thing as the ref aliases above. `--base-ref`
# carries `pull_request.base.ref`, a repository BRANCH NAME, and PROTECTED also holds advisory git
# spellings - so a pull request targeting an ordinary branch literally named `origin/main` was
# treated as protected and its floor grants honoured (Codex's P1 on f209b38). Anyone may push a
# branch by that name. The aliases stay for resolving a REF; a branch name is compared only here.
PROTECTED_BRANCH = "main"
# What a short name is resolved AS. `git show origin/main:file` prefers refs/heads/origin/main over
# refs/remotes/origin/main, so a branch literally named `origin/main` - which anyone may push -
# becomes the "protected" copy and hands itself the rule and the floor. Codex reproduced that on
# c33cf11: a trusted-copy refusal turned into an admission. Short names resolve through here, and
# nothing else is believed.
FULL = {"main": "refs/remotes/origin/main",
        "origin/main": "refs/remotes/origin/main",
        "refs/heads/main": "refs/remotes/origin/main",
        "refs/remotes/origin/main": "refs/remotes/origin/main"}


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


def calls_named(floor_text: str) -> tuple:
    """Every call the open grants name, in the order they appear, without repeats."""
    return tuple(dict.fromkeys(call.strip()
                               for _, call, _ in GRANT.findall(open_section(floor_text))))


def overlapping_in_a_call(floor_text: str):
    """The first pair of DIFFERENT agents granted one page FOR THE SAME CALL.

    Two pens on one page is a conflict only while both are open. Judging the whole file at once
    made a grant retired with an older call block a valid new one (Codex on c33cf11).
    """
    for call in calls_named(floor_text):
        clash = overlapping(grants(floor_text, call))
        if clash:
            return clash
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
    said, keys = [], 0
    for line in front_matter(floor_text or "").splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() != "call":
            continue
        # EVERY key counts, empty or not. `call:` with `call: an old call` under it declares two,
        # and accepting "the one non-empty value" admitted it - while a reader that keeps the LAST
        # value would have read the empty one and had no call at all (Codex on c33cf11).
        keys += 1
        if value.strip():
            said.append(value.strip())
    if keys > 1:
        return "", ("its front matter declares %d call lines (%s), which is ambiguous"
                    % (keys, ", ".join(repr(s) for s in said) or "all empty"))
    if not said:
        return "", "its front matter names no call"
    return said[0], ""


def authority(base: str, protected: str) -> tuple:
    """(the ref a grant may be read from, why none can be). Never a ref the branch chose itself.

    `--protected` says which ref is the protected branch and is believed only when it names one;
    `--base` is taken as the authority only when it names one too, so `--base origin/main` keeps
    working while `--base origin/an-unmerged-branch` carries nothing at all.
    """
    for ref in (protected, base):
        if ref and ref in PROTECTED:
            # Always the full remote-tracking path, never a short name a local branch can shadow.
            return FULL[ref], ""
    if protected:
        return "", "%r is not the protected branch, so it carries no grant" % protected
    if base:
        return "", "%r is not the protected branch, so no grant is read from it" % base
    return "", "no protected branch was named, so no grant can be in force"


def own_source() -> str:
    """This file's bytes as they are running, or "" when they cannot be read."""
    try:
        return Path(__file__).resolve().read_text(encoding="utf-8")
    except OSError:
        return ""


def trusted_copy(ref: str, root=None) -> tuple:
    """(whether this running rule IS the protected branch's copy, why it is not).

    CI takes the rule from the protected ref and runs it from outside the checkout, but the step
    that does so lives in the candidate's own workflow, so the DECISION to use the trusted copy is
    the candidate's to make. This is the half that does not depend on it: the rule compares what it
    is running with what the protected ref holds, and while they differ it honours NO GRANT at all.
    Everything outside docs/okf/ is unaffected, and so is a writer's own standing access, so an
    ordinary pull request never notices; only a handover waits for the merge.
    """
    if not ref:
        return False, "no protected branch was named"
    theirs = _show(ref, 'scripts/okf_ownership.py', root)
    if theirs is None:
        return False, "%s carries no %s to compare this rule against" % (ref, 'scripts/okf_ownership.py')
    mine = own_source()
    if not mine:
        return False, "this rule cannot read its own source to prove which copy it is"
    if mine.replace("\r\n", "\n") != theirs.replace("\r\n", "\n"):
        return False, ("this rule is not the copy on %s, so no grant is in force; a grant counts "
                       "only once the rule enforcing it is merged" % ref)
    return True, ""


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
    # The diff base resolves through FULL too. A short name here was the other half of the same
    # shadowing: `origin/main...HEAD` prefers refs/heads/origin/main, so a branch anyone may push
    # could decide what counts as changed as well as what counts as authority.
    base = FULL.get(base, base)
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


def head_sha(root=None) -> str:
    """The commit the checkout is actually on, or "" when git cannot say."""
    at = Path(root) if root else Path(__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(at), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def tree_entry(sha: str, path: str, root=None) -> tuple:
    """(mode, object type, oid) for `path` in commit `sha`, or ("", "", "") when it is not there.

    `git ls-tree` on a COMMIT SHA, which is content-addressed and immutable - not a ref. The sha is
    the one `trusted_posture()` has already matched against `git rev-parse HEAD`, so this reads the
    same bytes the checkout holds without going anywhere near a name a push can move.
    """
    at = Path(root) if root else Path(__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "-c", "core.quotePath=false", "ls-tree", "-z", sha,
                               "--", path], cwd=str(at), capture_output=True, text=True,
                              encoding="utf-8", errors="surrogateescape")
    except OSError:
        return "", "", ""
    if done.returncode != 0 or not done.stdout.strip():
        return "", "", ""
    head = done.stdout.split("\0")[0]
    meta = head.split("\t")[0].split()
    if len(meta) < 3:
        return "", "", ""
    return meta[0], meta[1], meta[2]


REGULAR = ("100644", "100755")


def authority_at(sha: str, path: str, root=None) -> tuple:
    """(the file's text, why it carries no authority). None text means it is simply not there.

    AN AUTHORITY FILE MUST BE A REGULAR BLOB. Reading it from the working tree followed symlinks,
    which is a bypass with no edit to the protected path at all: point `docs/okf/floor.md` at an
    ordinary file elsewhere once, and every later edit to THAT file changes who holds the pen while
    the non-delegable path looks untouched (Codex on a1e1e4f and 2496937). A `120000` entry is
    refused here, and so is anything that is not a blob - a gitlink, a directory. The mode comes
    from git's own tree, not from the filesystem, which also means a checkout that materialises
    symlinks as text files cannot disguise one.
    """
    mode, kind, oid = tree_entry(sha, path, root)
    if not mode:
        return None, ""
    if mode == "120000":
        return "", ("%s is a symlink in %s, and an authority file may not be one: an edit to its "
                    "target would hand out the pen without touching this path"
                    % (path, sha[:12]))
    if kind != "blob" or mode not in REGULAR:
        return "", "%s is a %s with mode %s in %s, not a regular file" % (path, kind, mode, sha[:12])
    at = Path(root) if root else Path(__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "cat-file", "blob", oid], cwd=str(at), capture_output=True,
                              text=True, encoding="utf-8", errors="surrogateescape")
    except OSError as broke:
        return "", "%s could not be read out of %s (%s)" % (path, sha[:12], broke)
    if done.returncode != 0:
        return "", "%s could not be read out of %s" % (path, sha[:12])
    return done.stdout, ""


def trusted_posture(base_sha: str, root=None) -> tuple:
    """(whether this run IS the forge-named base commit, why it is not).

    THE HALF THAT COULD NOT BE DONE FROM INSIDE THE REPOSITORY. Codex's P1, carried across three
    reviews: a `pull_request` workflow's own definition comes from the candidate's merge ref, so
    the candidate can replace the enforcement step; and everything this rule read came through
    `git show <ref>:<path>` against a LOCAL remote-tracking ref, which candidate code - run by an
    earlier step of that same candidate workflow - could repoint. Comparing the rule's bytes with
    that ref is self-comparison: "neither a printed hash nor equality to the contaminated ref
    establishes independent provenance".

    So provenance is no longer claimed from inside. It is established by WHERE the run happens:
    `.github/workflows/okf-ownership-trusted.yml` is a `pull_request_target` workflow, whose
    definition GitHub takes from the base branch and not from the pull request, and which checks
    out the forge-named base commit and never the candidate's. No candidate code runs in it at
    all, so there is no earlier step to contaminate anything.

    What this function does is check that story against the checkout: the commit the rule is
    running out of must BE the base commit the forge named. If it is not - a candidate tree, a
    merge ref, a reanchored ref - the run refuses. It cannot be talked into the trusted posture
    from the command line, because the sha has to match a tree the caller does not control.
    """
    if not base_sha:
        return False, "no trusted base commit was named"
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha.strip().lower()):
        return False, "%r is not a commit sha" % base_sha[:48]
    here = head_sha(root)
    if not here:
        return False, "git cannot say which commit this checkout is on"
    if here.lower() != base_sha.strip().lower():
        return False, ("this checkout is %s and the trusted base commit is %s: the rule is not "
                       "running out of the base tree" % (here[:12], base_sha.strip()[:12]))
    return True, ""


def paths_from(listing: str, root=None) -> tuple:
    """(the changed paths the forge itself reported, why they could not be read).

    Not a local diff. `git diff <ref>...HEAD` needs a ref and a candidate commit, and both were
    reachable by candidate code; the forge's own `pulls/<n>/files` is neither.

    A JSON ARRAY, NOT LINES, AND NOTHING IS STRIPPED. The first version read one path per line and
    called `.strip()` on each, which is lossy: a leading or trailing space is a VALID PATH BYTE, so
    the unowned `" docs/okf/index.md"` arrived as the owned `"docs/okf/index.md"` and changed the
    authorization answer (Codex on 2496937, the conference twin of this rule). A newline in a path
    would have been worse still. JSON is lossless for every byte a path can hold, the forge already
    speaks it, and the only transformation left is none.
    """
    at = Path(listing)
    if not at.is_file():
        return set(), "%s is not a file, so the forge's changed-path list was not read" % listing
    try:
        text = at.read_text(encoding="utf-8")
    except OSError as broke:
        return set(), "%s cannot be read (%s)" % (listing, broke)
    try:
        found = json.loads(text)
    except ValueError as broke:
        return set(), "%s is not the forge's JSON path list (%s)" % (listing, broke)
    if not isinstance(found, list) or not all(isinstance(q, str) for q in found):
        return set(), "%s does not hold a JSON array of path strings" % listing
    if not found:
        return set(), "%s is empty, so no changed path was reported" % listing
    return set(found), ""


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


FORK = "fork/"


def rights_of(branch: str, from_fork: bool) -> str:
    """The branch name this rule may read rights from. A fork's name buys nothing.

    THIS REPOSITORY IS PUBLIC. Anyone may fork it and name their branch `claude-code-cli/x` or
    `pi1-cli/x`, and until now `allowed()` read that prefix and handed them standing writer access
    - the floor and the gate's own files included (Codex's P1 on a9de3c7). A branch prefix was
    always attribution rather than identity, but inside ONE repository it is at least attribution
    among people who can push to it; from a fork it is a string a stranger chose. So a fork's
    branch is rewritten to a prefix that owns nothing, and the run says so.
    """
    return branch if not from_fork else FORK + branch


def shadows_the_gate(path: str) -> bool:
    """Whether `path` is a file that could be imported in place of a module the gate needs.

    Only a .py sitting DIRECTLY in one of the two directories the gate runs out of counts: a
    deeper path is never on sys.path[0], and a name the gate does not import shadows nothing.
    """
    for where in SHADOW_DIRS:
        if path.startswith(where) and "/" not in path[len(where):] and path.endswith(".py"):
            return path[len(where):-3] in SHADOWABLE
    return False


def allowed(branch: str, path: str, floor: str = "", call: str = None) -> bool:
    """Whether `branch` may change `path`. `docs/okf/` and the files that enforce it."""
    if path in AUTHORITY or shadows_the_gate(path):
        # No grant reaches these, however wide: a floor grant that could hand over the rule would
        # be a grant that hands out grants.
        return any(branch.startswith(p) for p in OPERATORS)
    if not path.startswith(OKF):
        return True
    if path == FLOOR:
        # The page that hands out the pen stays with the operator of the calls.
        return any(branch.startswith(p) for p in OPERATORS)
    if any(branch.startswith(p) for p in WRITERS):
        return True
    return handed_over(branch, path, floor, call)


# The answers this rule must still give before it judges anything. Each is a control: a tampered
# decision cannot keep them all, so the run refuses instead of admitting (Codex 20:26Z: CI ran the
# branch's own copy of the rule, and a `True` in place of allowed() admitted the floor file).
_GRANTED = OPEN_GRANTS + "\n- grant: grok | call: a call | paths: " + OKF + "index.md\n"
_GRANTED_WIDE = OPEN_GRANTS + "\n- grant: grok | call: a call | paths: " + OKF + "\n"
_GRANTED_GEMINI = (OPEN_GRANTS + "\n- grant: gemini | call: a call | paths: " + OKF + "gemini/\n")
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
    # No standing exception for gemini/ in a PUBLIC repository, and the floor is still the way in.
    ("gemini/okf-1", GEMINI + "RESULT-1.md", "", False),
    ("gemini/okf-1", GEMINI + "RESULT-1.md", _GRANTED_GEMINI, True),
    ("claude-code-cli/x", GEMINI + "RESULT-1.md", "", True),
    ("grok/x", "scripts/append.py", "", True),          # outside the OKF this rule has no opinion
    # The files that decide, which used to be open season outside docs/okf/.
    ("grok/x", "scripts/okf_ownership.py", "", False),
    ("pi1-cli/notes", "scripts/okf_ownership.py", "", False),
    ("codex/x", "tests/test_okf_ownership.py", "", False),
    ("grok/x", ".github/workflows/okf-ownership-trusted.yml", "", False),
    ("grok/x", ".github/workflows/okf-ownership.yml", "", False),
    ("claude-code-cli/x", "scripts/okf_ownership.py", "", True),
    ("grok/x", "scripts/okf_ownership.py", _GRANTED_WIDE, False),
    # A file that shadows a module the gate imports is the gate, by another road.
    ("grok/x", "scripts/json.py", "", False),
    ("codex/x", "tests/json.py", "", False),
    ("pi1-cli/notes", "scripts/subprocess.py", "", False),
    ("claude-code-cli/x", "scripts/json.py", "", True),
    ("grok/x", "scripts/deeper/json.py", "", True),      # never on sys.path[0]
    ("grok/x", "scripts/json.txt", "", True),            # not importable
    ("grok/x", "scripts/notashadow.py", "", True),       # a name the gate does not import
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
         " <changed-path>...\n"
         "   or: okf_ownership.py <branch> --trusted-base <sha> --base-ref <name>"
         " --changed-from <file> [--candidate-floor <file>] [--repo <path>]    (the gate)")


def main(argv, root=None) -> int:
    argv = list(argv)
    base = _taken(argv, "--base")
    protected = _taken(argv, "--protected")
    trusted_base = _taken(argv, "--trusted-base")
    base_ref = _taken(argv, "--base-ref")
    head_repo = _taken(argv, "--head-repo")
    this_repo = _taken(argv, "--this-repo")
    changed_from = _taken(argv, "--changed-from")
    candidate_from = _taken(argv, "--candidate-floor")
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

    if trusted_base:
        # THE GATE. Everything below comes from the base commit the forge named, or from the forge
        # itself, and nothing from a ref or a tree the candidate can reach. There is no fallback:
        # a posture that does not check out refuses, and so does a missing changed-path list.
        posture, why_posture = trusted_posture(trusted_base, root)
        if not posture:
            print("REFUSED: %s. This run claims the gate's posture and is not in it, so it judges"
                  " nothing rather than judging from a tree the candidate can reach." % why_posture)
            return 1
        if not changed_from:
            print("REFUSED: the gate must be handed the forge's own changed-path list"
                  " (--changed-from). A local diff needs a ref and a candidate commit, which is"
                  " exactly what this posture exists not to trust.")
            return 1
        if head_repo and this_repo and head_repo != this_repo:
            was, branch = branch, rights_of(branch, True)
            print("NOTE: %s comes from the fork %s, not from %s, so its branch prefix carries no"
                  " standing access here; it is judged as %r."
                  % (was, head_repo, this_repo, branch))
        if not base_ref:
            print("REFUSED: the gate must be told which branch the pull request targets"
                  " (--base-ref). A pull request may target an unmerged branch of its own, and"
                  " that branch may carry any floor it likes.")
            return 1
        found, blind = paths_from(changed_from, root)
        if blind:
            print("REFUSED: %s" % blind)
            return 1
        # THE GATE'S OWN FILES MUST BE REGULAR BLOBS, and this is checked before anything is
        # judged. If the rule, its suite or either workflow is a SYMLINK on the protected branch,
        # the gate executes whatever the link points at - and that target is an ordinary path the
        # rule lets anyone edit, so a later pull request silently owns every decision while the
        # base copy still looks benign (Codex's P1 on a9de3c7). authority_at() was checking this
        # for floor.md alone.
        for guarded in AUTHORITY:
            _, bent = authority_at(trusted_base, guarded, root)
            if bent:
                print("REFUSED: %s. The gate will not run out of a tree where one of its own"
                      " files is not a regular file." % bent)
                return 1
        # NOT _unquote()d. The forge's list is already JSON-decoded into literal filenames, and
        # _unquote treats anything wrapped in quote characters as git C-quoting - so a path
        # literally named "docs/okf/index.md", quotes included, was rewritten into a different
        # path and judged as that one (Codex's P1 on a9de3c7). It contradicted the byte-for-byte
        # guarantee two lines above it. _unquote belongs to CLI input, which may be C-quoted.
        paths = set(found)
        ref = "the base commit %s" % trusted_base[:12]
        if base_ref != PROTECTED_BRANCH:
            # The checkout IS the base commit, but the base is not the protected branch: standing
            # access and everything outside the OKF still hold, and a handover does not. This is
            # the hole from conference #161 in its last shape - a branch targeting a branch.
            floor, call = "", ""
            why = ("%r is not the protected branch, so the floor it carries hands over nothing"
                   % base_ref)
        else:
            # Read out of the COMMIT, by its tree entry: a sha is content-addressed and the one
            # trusted_posture() already matched against HEAD, so this is the checkout's own bytes
            # without touching a name a push can move. The mode is checked because an authority
            # file that is a symlink hands out the pen from somewhere else entirely.
            floor, crooked = authority_at(trusted_base, FLOOR, root)
            if crooked and not (any(branch.startswith(p) for p in OPERATORS)
                                and paths == {FLOOR}):
                print("REFUSED: %s. An authority file that is not a regular file is not an"
                      " authority file, so this run judges nothing." % crooked)
                return 1
            if crooked:
                # THE WAY OUT, and it has to exist. Codex on f209b38: once a crooked floor is on
                # the protected branch, refusing every run would fail EVERY later pull request -
                # including the one restoring the floor - and a gate that cannot be repaired is a
                # denial of service wearing a security control. The operator, changing that page
                # and nothing else, is admitted exactly as it is for two pens on one page.
                print("OK: %s; the operator's repair of that page, alone, is the way out."
                      % crooked)
                return 0
            if floor is None:
                floor, call = "", ""
                why = "%s carries no %s, so no grant is in force" % (ref, FLOOR)
            else:
                call, why = floor_call(floor)
                if not call:
                    why = "%s on %s: %s, so no grant belongs to a call" % (FLOOR, ref, why)
                    floor = ""
        candidate = ""
        if candidate_from and Path(candidate_from).is_file():
            try:
                candidate = Path(candidate_from).read_text(encoding="utf-8")
            except OSError as broke:
                print("REFUSED: the branch's own %s was fetched and cannot be read (%s)"
                      % (FLOOR, broke))
                return 1
    else:
        # ADVISORY. Same rule, same answers, but its provenance is the candidate's checkout, so
        # the grant half holds nothing: a handover counts only once the rule enforcing it is the
        # protected branch's own copy. Useful to a person and to a pull request's own CI; it is
        # not the gate, and it says so.
        ref, why_authority = authority(base, protected)
        floor, call, why = in_force(ref, root)
        if not ref:
            why = why_authority
        trusted, untrusted = trusted_copy(ref, root)
        if not trusted:
            floor = ""
            if ref:                 # with no ref, the authority already said which one and why
                why = untrusted or why
        if base:
            # What changed is git's account, not the caller's: a rename names both of its ends.
            found, blind = changed(base, root)
            if blind:
                # The operator may be judged on the set it was given: the floor is already its
                # own, so no rename of it hands it anything it does not have. For anyone else
                # writing the OKF the unseen rename IS the escalation, and outside the OKF this
                # rule has no opinion.
                if any(p.startswith(OKF) for p in paths) and not any(branch.startswith(p)
                                                                     for p in OPERATORS):
                    print("REFUSED: %s, so the changed paths cannot be confirmed and a rename of"
                          " %s would be invisible" % (blind, FLOOR))
                    return 1
                print("NOTE: %s; judging the %d path(s) given" % (blind, len(paths)))
            paths |= found
        candidate = _candidate_floor(root)

    if not paths:
        print(USAGE)
        return 2
    # Two pens on one page: refused when this branch proposes it, and when the protected floor
    # already carries it - except for the one repair that can close that.
    proposed = overlapping_in_a_call(candidate) if candidate and candidate != floor else None
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
    if not trusted_base:
        print("ADVISORY: no --trusted-base, so this run is not the gate and decides nothing."
              " The gate is okf-ownership-trusted, which runs from the base branch.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

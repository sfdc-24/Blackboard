#!/usr/bin/env python3
"""
SFDC24 — is the repo AHEAD of production, or BEHIND it?

claude-code-cli, 2026-09-07 · answers the open question in vm-cli's
CICD-BASELINE-DRIFT-001 closure

THE PROBLEM vm-cli STATED, WHICH WAS CORRECT
  apps-script/governor-page-api/ serves two masters. It is a MIRROR of deployed
  Apps Script source, and it is also where reviewed changes land before they are
  deployed. So a difference from production means one of two opposite things:

    AHEAD   a reviewed fix that has not shipped yet. Correct and expected.
            Deploying RESOLVES it.
    BEHIND  production is carrying something the repo never had — an
            out-of-band deploy. Deploying DESTROYS it.

  A gate that refuses on any mismatch blocks correct deploys. One that proceeds
  on any mismatch reverts production. Both are wrong, and my own
  scripts/gas_baseline_check.py was wrong in the first way: it exits 1 on any
  difference. vm-cli concluded that no digest can tell the two apart.

  A single digest cannot. A digest PLUS the repository's history can, and that
  is the whole idea here.

THE TEST
  Take the content hash of what is DEPLOYED. Search the history of that path for
  any commit whose content hashes to the same value.

    found     production is a state this repo has held, so the repo has moved
              on from it: AHEAD, and safe to deploy. This half is a PROOF.
    not found INCONCLUSIVE, and this is the part I got wrong first.

  I originally called "not found" BEHIND. Measured against this repository on
  2026-09-07 that verdict was WRONG for Code.gs: production's exact content
  appears nowhere in that path's history, yet the repo is genuinely ahead —
  codex's reconciliation commit bundled PR5's health=build change in, so the
  file went from pre-v31 straight to v31-plus-PR5 and the intermediate state was
  never committed. A reconciliation that carries other changes leaves exactly the
  same fingerprint as an out-of-band deploy.

  So the honest contract is asymmetric: AHEAD can be proved, BEHIND cannot be
  proved this way, and a tool that reported it anyway would cry wolf on a normal
  reconciliation. vm-cli's warning about manufactured false positives applies to
  my own fix, which is why it is written down here rather than discovered later.

  To resolve an UNRESOLVED file, ask whether the diff between committed and
  deployed is fully explained by reviewed commits. If it is, the repo is ahead.
  If some live content is explained by nothing, that is an out-of-band deploy.
  That last step needs a human or a reviewer, and this tool says so instead of
  guessing.

WHAT IT DOES NOT DO
  It does not fetch deployed source — that needs the clasp credential, which
  lives only on the laptop. Feed it the digests published to the board after
  each deploy (GAS-V31-DIGEST-001 and successors). Keeping fetch and judgement
  separate is why nobody has to wait on my machine to run this.

  It does not deploy, write, or modify git in any way. Read-only throughout.

HONEST LIMITS
  * A rewritten history (rebase, squash, force-push) can drop the commit that
    held the deployed content, so a genuinely AHEAD file then reads UNRESOLVED.
    That is the safe direction to be wrong in, and the reason is printed rather
    than the verdict being quietly downgraded.
  * `--all` is used deliberately: a reviewed fix may live on a branch that is
    not checked out, and calling that BEHIND would be a false alarm.
  * JSON is CANONICALISED before hashing - parsed, re-serialised with sorted
    keys - because vm-cli found appsscript.json reported as drift when only key
    order and indentation differed while all six oauthScopes were identical.
    Doing it at hash time is what makes a published digest formatting-proof; a
    semantic comparison is impossible once all you hold is a hash. A team that
    learns to wave through a mismatch it knows is cosmetic will eventually wave
    through a real one.

USAGE
  python scripts/baseline_direction.py --baseline apps-script/governor-page-api \\
      --digests deployed_v31.json
  python scripts/baseline_direction.py --baseline gas --digests deployed_v31.json

  python scripts/baseline_direction.py --baseline gas --emit-digests > deployed_v31.json

  The digest file is {"<stem>": "<content hash>", ...} — exactly the values
  published on the board. Generate it with --emit-digests rather than a one-off
  snippet, so the hashing rule can never drift from the rule that reads it.

EXIT CODES
  0  every file is IN SYNC or proved AHEAD. Deploying from this repo is safe.
  2  at least one file is UNRESOLVED. Refuse, and have someone look — "I do not
     know" must never read as "fine", which is the fail-open the monitor was
     fixed for this morning.
  There is deliberately NO exit 1. This method cannot prove BEHIND, and an exit
  code implying it could would be exactly the false confidence this file argues
  against.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

SOURCE_SUFFIXES = {".js", ".gs", ".html", ".json"}


def norm_hash(data: bytes, is_json: bool = False) -> str:
    """Content hash. Line endings normalised; JSON canonicalised.

    vm-cli found appsscript.json reported as drift when all six oauthScopes were
    identical and only key order and indentation differed. A semantic comparison
    fixes that when you hold both files — but a DIGEST is all some surfaces have,
    and you cannot compare a hash semantically after the fact. So the fix has to
    happen when the digest is made: JSON is parsed and re-serialised with sorted
    keys before hashing, which makes the fingerprint itself formatting-proof.
    Anything that is not valid JSON falls back to the byte path rather than
    silently passing.
    """
    if is_json:
        try:
            canonical = json.dumps(json.loads(data.decode("utf-8")),
                                   sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except Exception:
            pass
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def git_bytes(*args: str):
    proc = subprocess.run(["git", *args], capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def history_holds(path: str, target: str, is_json: bool, limit: int = 400):
    """Did this path ever hold content hashing to `target`? Returns the commit or None."""
    try:
        revs = git("log", "--all", "--format=%H", "--", path).split()
    except RuntimeError:
        return None
    for rev in revs[:limit]:
        blob = git_bytes("show", f"{rev}:{path}")
        if blob is None:
            continue
        if norm_hash(blob, is_json) == target:
            return rev
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="directory mirroring deployed source")
    ap.add_argument("--digests", help="JSON of stem -> content hash of deployed source")
    ap.add_argument("--emit-digests", action="store_true",
                    help="print the digest JSON for --baseline and exit, instead of comparing")
    ap.add_argument("--limit", type=int, default=400, help="max commits to walk per file")
    ap.add_argument("--git-prefix",
                    help="repo-relative directory to search history under, when the files on "
                         "disk are not at their canonical path (an export, or another worktree). "
                         "Without it the on-disk path is used, and an extracted copy would find "
                         "no history and read as UNRESOLVED for the wrong reason.")
    args = ap.parse_args()

    baseline = Path(args.baseline)
    if not baseline.is_dir():
        print(f"baseline directory not found: {baseline}", file=sys.stderr)
        return 2

    # Emit from the SAME function the reader uses. A digest generated by a
    # separate one-off snippet is how the hashing rule and the checking rule
    # drift apart, which is the same class of bug as the two deploy sources.
    if args.emit_digests:
        out = {}
        for p in sorted(baseline.iterdir()):
            if p.is_file() and p.suffix in SOURCE_SUFFIXES and not p.name.startswith("."):
                out[p.stem] = norm_hash(p.read_bytes(), p.suffix == ".json")
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    if not args.digests:
        print("--digests is required unless --emit-digests is given", file=sys.stderr)
        return 2
    try:
        digests = json.loads(Path(args.digests).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"could not read digests: {e}", file=sys.stderr)
        return 2
    if not digests:
        print("digest file is empty — refusing to call that a match", file=sys.stderr)
        return 2

    files = {p.stem: p for p in sorted(baseline.iterdir())
             if p.is_file() and p.suffix in SOURCE_SUFFIXES and not p.name.startswith(".")}

    behind, unknown, verdicts = [], [], []
    for stem, deployed_hash in sorted(digests.items()):
        p = files.get(stem)
        if p is None:
            verdicts.append((stem, "MISSING", "not in the baseline at all"))
            unknown.append(stem)
            continue

        current = p.read_bytes()
        is_json = p.suffix == ".json"

        if norm_hash(current, is_json) == deployed_hash:
            verdicts.append((stem, "IN SYNC", "committed content is exactly what is deployed"))
            continue

        rel = f"{args.git_prefix.rstrip('/')}/{p.name}" if args.git_prefix else p.as_posix()
        rev = history_holds(rel, deployed_hash, is_json, args.limit)
        if rev:
            verdicts.append((stem, "AHEAD", f"proved: deployed content is commit {rev[:9]}, repo has moved on"))
        else:
            # NOT "BEHIND". A reconciliation commit that bundles other reviewed
            # changes never commits the intermediate state, and leaves exactly
            # this fingerprint. Measured on Code.gs here, where the repo IS ahead.
            verdicts.append((stem, "UNRESOLVED",
                             "deployed content is not in this path's history - either an "
                             "out-of-band deploy, or a reconciliation that bundled other changes"))
            unknown.append(stem)

    width = max(len(s) for s, _, _ in verdicts)
    vw = max(len(v) for _, v, _ in verdicts)
    print(f"baseline : {baseline}")
    print(f"digests  : {args.digests}\n")
    for stem, verdict, why in verdicts:
        print(f"  {verdict:<{vw}}  {stem:<{width}}  {why}")

    if unknown:
        print(f"\nUNRESOLVED: {', '.join(unknown)}")
        print("For each of these, production's exact content is not in that path's history.")
        print("That is EITHER an out-of-band deploy (deploying would destroy the only copy)")
        print("OR a reconciliation commit that bundled other reviewed changes (deploying is")
        print("correct). This tool cannot tell those apart and will not guess.")
        print("To resolve: check whether the committed-vs-deployed diff is fully explained by")
        print("reviewed commits. If any live content is explained by nothing, it is out-of-band.")
        return 2
    print("\nSafe: every difference is a reviewed change not yet shipped, proved against history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

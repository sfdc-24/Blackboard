#!/usr/bin/env python3
"""Prove that an exact commit is reachable from a freshly read remote branch.

Only temporary Git state is written. This proves publication to the nominated
branch at the observed time, not deployment, review, acceptance or permanence.
Git output and remote URLs are deliberately excluded from the JSON receipt.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


SCHEMA = "blackboard.publication-proof.v1"
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
GIT_OPTIONS = [
    "--no-replace-objects", "-c", "core.hooksPath=",
    "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
    "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
]


class CheckError(Exception):
    """A fixed diagnostic code, never a provider message."""


def git_env():
    # Do not let another checkout's GIT_DIR, replacement refs or config override
    # the explicitly selected repository. Normal user Git/SSH credentials remain
    # available; interactive prompts are disabled for bounded unattended use.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", GCM_INTERACTIVE="Never")
    return env


def run_git(where, args, timeout):
    try:
        return subprocess.run(
            ["git", *GIT_OPTIONS, "-C", str(where), *args], env=git_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise CheckError("GIT_TIMEOUT") from None
    except OSError:
        raise CheckError("GIT_UNAVAILABLE") from None


def decode(output):
    try:
        return output.decode("utf-8", errors="strict")
    except UnicodeError:
        raise CheckError("INVALID_GIT_RESPONSE") from None


def remote_tip(bare, ref, timeout):
    answer = run_git(bare, ["ls-remote", "--refs", "publication", ref], timeout)
    if answer.returncode:
        raise CheckError("REMOTE_READ_FAILED")
    lines = decode(answer.stdout).splitlines()
    if not lines:
        return None
    if len(lines) != 1:
        raise CheckError("AMBIGUOUS_REMOTE_REF")
    parts = lines[0].split("\t")
    if len(parts) != 2 or not COMMIT.fullmatch(parts[0]) or parts[1] != ref:
        raise CheckError("INVALID_REMOTE_REF")
    return parts[0]


def verify(repo, commit, remote="origin", branch="main", timeout=30):
    """Return a bounded metadata receipt; no shell, network writes or source fetch.

    LOCAL_ONLY means the commit exists locally and the specified branch was
    freshly checked without finding it. It does not assert absence elsewhere.
    """
    valid_commit = isinstance(commit, str) and COMMIT.fullmatch(commit)
    valid_remote = isinstance(remote, str) and REMOTE.fullmatch(remote)
    valid_branch = (isinstance(branch, str) and 0 < len(branch) <= 240
                    and all(32 < ord(c) < 127 for c in branch))
    result = {
        "schema": SCHEMA, "status": "UNKNOWN", "reason": "INVALID_INPUT",
        "commit": commit if valid_commit else None,
        "remote": remote if valid_remote else None,
        "branch": branch if valid_branch else None,
        "observed_remote_head": None, "local_commit_present": None,
    }
    try:
        if not (valid_commit and valid_remote and valid_branch
                and isinstance(timeout, (int, float)) and 0 < timeout <= 300):
            raise CheckError("INVALID_INPUT")
        repo = Path(repo).resolve(strict=True)
        if not repo.is_dir():
            raise CheckError("REPOSITORY_UNAVAILABLE")
        ref = "refs/heads/" + branch
        if run_git(repo, ["check-ref-format", ref], timeout).returncode:
            raise CheckError("INVALID_BRANCH")
        if run_git(repo, ["rev-parse", "--git-dir"], timeout).returncode:
            raise CheckError("NOT_A_REPOSITORY")
        local = run_git(repo, ["cat-file", "-t", commit], timeout)
        result["local_commit_present"] = local.returncode == 0 and local.stdout == b"commit\n"
        configured = run_git(repo, ["remote", "get-url", "--all", remote], timeout)
        if configured.returncode:
            raise CheckError("REMOTE_NOT_CONFIGURED")
        urls = decode(configured.stdout).splitlines()
        if len(urls) != 1 or not urls[0] or any(ord(c) < 32 for c in urls[0]):
            raise CheckError("AMBIGUOUS_REMOTE_CONFIG")
        url = urls[0]
        # A relative filesystem remote is relative to the source repository, not
        # our scratch repository. Resolve it before the isolated fetch.
        if ":" not in url and not Path(url).is_absolute():
            url = str((repo / url).resolve())
        with tempfile.TemporaryDirectory(prefix="blackboard-publication-") as tmp:
            bare = Path(tmp)
            init = ["init", "--bare", "--template=", "--quiet"]
            if len(commit) == 64:
                init.append("--object-format=sha256")
            if run_git(bare, init, timeout).returncode:
                raise CheckError("TEMP_REPOSITORY_FAILED")
            # Keep even a credential-bearing URL out of process arguments and
            # receipts. The restricted temporary config is deleted on all exits.
            config = bare / "config"
            config.chmod(0o600)
            escaped = url.replace("\\", "\\\\").replace('"', '\\"')
            with config.open("a", encoding="utf-8") as fh:
                fh.write('\n[remote "publication"]\n\turl = "' + escaped + '"\n')
            before = remote_tip(bare, ref, timeout)
            result["observed_remote_head"] = before
            if before is None:
                if remote_tip(bare, ref, timeout) is not None:
                    raise CheckError("REMOTE_MOVED")
                result.update(status="LOCAL_ONLY" if result["local_commit_present"]
                              else "NOT_PUBLISHED", reason="REMOTE_BRANCH_ABSENT")
                return result
            fetched = run_git(bare, [
                "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                "--no-recurse-submodules", "publication",
                "+" + ref + ":refs/heads/verified",
            ], timeout)
            if fetched.returncode:
                raise CheckError("REMOTE_FETCH_FAILED")
            if (bare / "shallow").exists():
                raise CheckError("INCOMPLETE_REMOTE_HISTORY")
            tip = run_git(bare, ["rev-parse", "--verify", "refs/heads/verified^{commit}"], timeout)
            if tip.returncode or decode(tip.stdout).strip() != before:
                raise CheckError("REMOTE_MOVED")
            obj = run_git(bare, ["cat-file", "-t", commit], timeout)
            reachable = False
            if obj.returncode == 0 and obj.stdout == b"commit\n":
                ancestry = run_git(bare, ["merge-base", "--is-ancestor", commit,
                                          "refs/heads/verified"], timeout)
                if ancestry.returncode not in (0, 1):
                    raise CheckError("ANCESTRY_CHECK_FAILED")
                reachable = ancestry.returncode == 0
            if remote_tip(bare, ref, timeout) != before:
                raise CheckError("REMOTE_MOVED")
            result.update(
                status="PUBLISHED" if reachable else
                ("LOCAL_ONLY" if result["local_commit_present"] else "NOT_PUBLISHED"),
                reason="REMOTE_ANCESTRY_CONFIRMED" if reachable else "NOT_IN_REMOTE_BRANCH",
            )
    except CheckError as error:
        result.update(status="UNKNOWN", reason=str(error))
    except (OSError, ValueError, TypeError):
        result.update(status="UNKNOWN", reason="LOCAL_IO_FAILED")
    finally:
        result["checked_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True, help="exact lowercase full commit SHA")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--timeout", type=float, default=30,
                        help="seconds per Git operation (maximum 300)")
    args = parser.parse_args()
    receipt = verify(args.repo, args.commit, args.remote, args.branch, args.timeout)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if receipt["status"] == "PUBLISHED" else (2 if receipt["status"] == "UNKNOWN" else 1)


if __name__ == "__main__":
    raise SystemExit(main())

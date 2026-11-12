#!/usr/bin/env python3
"""Guard an Apps Script clasp deploy against uncommitted or unverified source.

The source about to be pushed is hashed and compared with the tracked source
at a git ref (HEAD by default). A mismatch refuses the deploy: non-zero exit
and a clear message, with no clasp push. After a deploy, the guard reads the
deployed version and content back through clasp and fails closed when the
read-back is missing, unreadable, or does not match the source that was
approved.

clasp is reached only through an injectable runner so tests can use a fake
binary and never touch the network.

    python3 scripts/gas_deploy_guard.py \
        --repo /path/to/repo \
        --source-dir apps-script/sample-guard

Exit codes: 0 verified deploy, 1 refused or failed closed, 2 usage/setup error.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


SOURCE_SUFFIXES = {".gs", ".js", ".html", ".json"}
IGNORE_NAMES = {".clasp.json", ".claspignore"}
VERSION_PATTERNS = (
    re.compile(r"Created version\s+(\d+)", re.I),
    re.compile(r'"versionNumber"\s*:\s*(\d+)'),
    re.compile(r"versionNumber[=:\s]+(\d+)", re.I),
    re.compile(r"version\s+(\d+)\s+created", re.I),
)
DEPLOY_LINE = re.compile(r"^\s*-\s+(\S+)\s+@(\d+)(?:\s|$)", re.M)


@dataclass(frozen=True)
class ClaspResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


ClaspRunner = Callable[[Sequence[str], Path], ClaspResult]


class GuardError(Exception):
    """A refused or failed-closed deploy. The message is safe to print."""


class SetupError(Exception):
    """The guard could not start (bad args, missing repo)."""


def normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def is_source_file(path: Path) -> bool:
    if path.name in IGNORE_NAMES:
        return False
    return path.suffix in SOURCE_SUFFIXES


def source_hash(files: dict[str, str]) -> str:
    """Stable hash of a name -> text map. Newlines are normalised to LF."""
    digest = hashlib.sha256()
    for name in sorted(files):
        body = normalize_text(files[name]).encode("utf-8")
        digest.update(b"FILE\0")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(body)).encode("ascii"))
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest()


def tree_source_files(source_dir: Path) -> dict[str, str]:
    """Every source file clasp would send from a working tree directory."""
    if not source_dir.is_dir():
        raise SetupError("source directory does not exist: %s" % source_dir)
    files: dict[str, str] = {}
    for path in source_dir.rglob("*"):
        if not path.is_file() or not is_source_file(path):
            continue
        rel = path.relative_to(source_dir).as_posix()
        files[rel] = path.read_text(encoding="utf-8")
    return files


def git_output(repo: Path, args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise SetupError("git %s failed in %s" % (args[0], repo))
    return result.stdout


def head_source_files(repo: Path, source_dir: str, ref: str = "HEAD") -> dict[str, str]:
    """Tracked source files at ref, keyed by path relative to source_dir."""
    prefix = source_dir.strip("/").replace("\\", "/")
    if not prefix or ".." in Path(prefix).parts:
        raise SetupError("source-dir must be a path inside the repo")
    raw = git_output(repo, ["ls-tree", "-r", "-z", "--name-only", ref, "--", prefix])
    names = [n for n in raw.split("\0") if n]
    files: dict[str, str] = {}
    root = Path(prefix)
    for name in names:
        path = Path(name)
        if not is_source_file(path):
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        body = git_output(repo, ["show", "%s:%s" % (ref, name)])
        files[rel] = body
    return files


def default_clasp_runner(args: Sequence[str], cwd: Path) -> ClaspResult:
    exe = shutil.which("clasp") or shutil.which("clasp.cmd")
    if not exe:
        return ClaspResult(2, "", "clasp is not on PATH")
    result = subprocess.run(
        [exe, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    return ClaspResult(result.returncode, result.stdout, result.stderr)


def run_clasp(runner: ClaspRunner, args: Sequence[str], cwd: Path, what: str) -> ClaspResult:
    result = runner(list(args), cwd)
    if result.returncode != 0:
        raise GuardError("%s failed (exit %d); deploy aborted" % (what, result.returncode))
    return result


def parse_created_version(output: str) -> int:
    for pattern in VERSION_PATTERNS:
        match = pattern.search(output or "")
        if match:
            return int(match.group(1))
    raise GuardError("could not read a version number from clasp create-version; failing closed")


def parse_deployed_version(output: str, deployment_id: Optional[str]) -> Optional[int]:
    found: list[tuple[str, int]] = []
    for match in DEPLOY_LINE.finditer(output or ""):
        found.append((match.group(1), int(match.group(2))))
    if deployment_id:
        for ident, version in found:
            if ident == deployment_id:
                return version
        raise GuardError(
            "deployment %s was not in clasp list-deployments; failing closed" % deployment_id
        )
    if len(found) == 1:
        return found[0][1]
    return None


def refuse_hash_mismatch(head_digest: str, push_digest: str, ref: str, source_dir: str) -> str:
    return (
        "REFUSED: source about to be pushed differs from git %s tracked source\n"
        "source_dir=%s\n"
        "head_sha256=%s\n"
        "push_sha256=%s"
        % (ref, source_dir, head_digest, push_digest)
    )


def deploy_and_readback(
    source_dir: Path,
    expected_hash: str,
    runner: ClaspRunner,
    deployment_id: Optional[str] = None,
    description: str = "guarded deploy",
) -> int:
    """Push, create a version, deploy, then read the deployed content back."""
    run_clasp(runner, ["push"], source_dir, "clasp push")
    created = run_clasp(
        runner,
        ["create-version", description],
        source_dir,
        "clasp create-version",
    )
    version = parse_created_version(created.stdout + "\n" + created.stderr)

    deploy_args: list[str]
    if deployment_id:
        deploy_args = [
            "deploy",
            "--deploymentId",
            deployment_id,
            "--versionNumber",
            str(version),
            "--description",
            description,
        ]
    else:
        deploy_args = [
            "deploy",
            "--versionNumber",
            str(version),
            "--description",
            description,
        ]
    run_clasp(runner, deploy_args, source_dir, "clasp deploy")

    listing = run_clasp(runner, ["list-deployments"], source_dir, "clasp list-deployments")
    listed = parse_deployed_version(listing.stdout, deployment_id)
    if listed is not None and listed != version:
        raise GuardError(
            "deployed version is %s, expected %s; failing closed" % (listed, version)
        )
    if deployment_id and listed is None:
        raise GuardError("could not read the deployed version; failing closed")

    clasp_json = source_dir / ".clasp.json"
    if not clasp_json.is_file():
        raise GuardError("no .clasp.json in the source directory; cannot read back")

    with tempfile.TemporaryDirectory(prefix="gas-deploy-guard-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / ".clasp.json").write_text(clasp_json.read_text(encoding="utf-8"), encoding="utf-8")
        run_clasp(
            runner,
            ["pull", "--versionNumber", str(version)],
            tmp_path,
            "clasp pull of the deployed version",
        )
        pulled = tree_source_files(tmp_path)
        if not pulled:
            raise GuardError("clasp pull of version %s returned no source files; failing closed" % version)
        pulled_hash = source_hash(pulled)
        if pulled_hash != expected_hash:
            raise GuardError(
                "deployed content does not match the approved source "
                "(approved_sha256=%s readback_sha256=%s); failing closed"
                % (expected_hash, pulled_hash)
            )
    return version


def run_guard(
    repo: Path,
    source_dir: str,
    ref: str = "HEAD",
    deployment_id: Optional[str] = None,
    clasp_runner: Optional[ClaspRunner] = None,
    description: str = "guarded deploy",
) -> tuple[int, str]:
    """Return (exit_code, message). Never raises for guard refusals."""
    runner = clasp_runner or default_clasp_runner
    try:
        repo = repo.resolve()
        if not (repo / ".git").exists() and not (repo / ".git").is_file():
            raise SetupError("repo is not a git working tree: %s" % repo)
        source_path = (repo / source_dir).resolve()
        try:
            source_path.relative_to(repo)
        except ValueError:
            raise SetupError("source-dir must stay inside the repo")
        head_files = head_source_files(repo, source_dir, ref=ref)
        if not head_files:
            raise SetupError("git %s has no tracked source under %s" % (ref, source_dir))
        push_files = tree_source_files(source_path)
        head_digest = source_hash(head_files)
        push_digest = source_hash(push_files)
        if head_digest != push_digest:
            return 1, refuse_hash_mismatch(head_digest, push_digest, ref, source_dir)
        version = deploy_and_readback(
            source_path,
            push_digest,
            runner,
            deployment_id=deployment_id,
            description=description,
        )
        return 0, (
            "OK: source matches %s and deployed version %s read back the same content\n"
            "source_dir=%s\n"
            "source_sha256=%s"
            % (ref, version, source_dir, push_digest)
        )
    except SetupError as error:
        return 2, "ERROR: %s" % error
    except GuardError as error:
        return 1, str(error)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="git repository root")
    parser.add_argument(
        "--source-dir",
        default="apps-script/governor-page-api",
        help="project directory clasp would push, relative to --repo",
    )
    parser.add_argument("--ref", default="HEAD", help="git ref whose tracked source is the baseline")
    parser.add_argument("--deployment-id", default=None, help="existing deployment id to update")
    parser.add_argument("--description", default="guarded deploy")
    return parser


def main(argv: Optional[Sequence[str]] = None, clasp_runner: Optional[ClaspRunner] = None) -> int:
    args = build_parser().parse_args(argv)
    code, message = run_guard(
        Path(args.repo),
        args.source_dir,
        ref=args.ref,
        deployment_id=args.deployment_id,
        clasp_runner=clasp_runner,
        description=args.description,
    )
    stream = sys.stdout if code == 0 else sys.stderr
    print(message, file=stream)
    return code


if __name__ == "__main__":
    sys.exit(main())

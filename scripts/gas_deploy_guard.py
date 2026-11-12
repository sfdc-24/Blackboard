#!/usr/bin/env python3
"""Guard an Apps Script clasp deploy (TASK ID: G15, Case 00001051).

Deploy only through headless clasp, from a clean tree at the main SHA. The
source about to be pushed is hashed and compared with the tracked source at
that SHA. A dirty tree, a HEAD that is not main, or a source mismatch refuses
the deploy: non-zero exit, no clasp push.

After a deploy, GET /health and compare the version and SHA with the version
just deployed and the main SHA (or the approved source hash). On a mismatch,
redeploy the previous deployment (same id, previous version) and post a P0
row. A missing, unreadable, or non-matching health answer is a mismatch.

clasp is reached only through an injectable, headless runner. HTTP and the
P0 poster are injectable too, so tests use a fake clasp and fake HTTP and
never touch the network.

    python3 scripts/gas_deploy_guard.py \
        --repo /path/to/repo \
        --source-dir apps-script/sample-guard \
        --health-url https://example.invalid/exec?health=build

Exit codes: 0 verified deploy, 1 refused or failed closed, 2 usage/setup error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
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
# url -> parsed JSON object. Tests inject a fake; the default does one GET.
HealthGet = Callable[[str], dict]
# P0 row text, already free of secrets. Tests inject a recorder.
P0Poster = Callable[[str], None]


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
    """Headless clasp only: no stdin, CI=true, CLASP_HEADLESS=1."""
    exe = shutil.which("clasp") or shutil.which("clasp.cmd")
    if not exe:
        return ClaspResult(2, "", "clasp is not on PATH")
    env = os.environ.copy()
    env["CI"] = "true"
    env["CLASP_HEADLESS"] = "1"
    result = subprocess.run(
        [exe, *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
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


def parse_listing(output: str) -> list[tuple[str, int]]:
    return [(m.group(1), int(m.group(2))) for m in DEPLOY_LINE.finditer(output or "")]


def parse_deployed_version(output: str, deployment_id: Optional[str]) -> Optional[int]:
    found = parse_listing(output)
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


def snapshot_previous_version(output: str, deployment_id: Optional[str]) -> Optional[int]:
    """Previous version for this deployment, or None if it is not in the listing."""
    if not deployment_id:
        return None
    for ident, version in parse_listing(output):
        if ident == deployment_id:
            return version
    return None


def refuse_hash_mismatch(head_digest: str, push_digest: str, ref: str, source_dir: str) -> str:
    return (
        "REFUSED: source about to be pushed differs from git %s tracked source\n"
        "source_dir=%s\n"
        "head_sha256=%s\n"
        "push_sha256=%s"
        % (ref, source_dir, head_digest, push_digest)
    )


DIRTY_TREE_MESSAGE = (
    "REFUSED: dirty tree; deploy only from a clean tree at the main SHA"
)

DEPLOY_ID_RE = re.compile(r"AKfyc_[A-Za-z0-9_-]+")
SHA40 = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
SHA64 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def redact_ids(text: str) -> str:
    """Strip clasp deployment ids so a P0 row can be posted from a public repo."""
    return DEPLOY_ID_RE.sub("[redacted-deployment]", text)


def porcelain_lines(repo: Path) -> list[str]:
    raw = git_output(repo, ["status", "--porcelain", "-uall"])
    return [ln for ln in raw.splitlines() if ln.strip()]


def _diff_ignoring_cr(repo: Path, args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    # git diff returns 1 when there is a difference; anything else is a failure.
    if result.returncode not in (0, 1):
        raise SetupError("git %s failed in %s" % (args[0], repo))
    return (result.stdout or "").strip()


def only_crlf_differences(repo: Path) -> bool:
    """True when the only working-tree or index differences are CR at EOL."""
    unstaged = _diff_ignoring_cr(repo, ["diff", "--ignore-cr-at-eol"])
    staged = _diff_ignoring_cr(repo, ["diff", "--cached", "--ignore-cr-at-eol"])
    return not unstaged and not staged


def require_clean_tree(repo: Path) -> None:
    """Refuse any dirty tree, including files outside the Apps Script source dir.

    A CR-at-EOL-only change on an already-tracked file is not treated as dirty:
    source hashes already normalize newlines, and that is covered by G4.
    Untracked, added, deleted, renamed, or any other content change is dirty.
    """
    lines = porcelain_lines(repo)
    if not lines:
        return
    for ln in lines:
        xy = ln[:2]
        if any(flag in xy for flag in "?ADRCUT"):
            raise GuardError(DIRTY_TREE_MESSAGE)
    if not only_crlf_differences(repo):
        raise GuardError(DIRTY_TREE_MESSAGE)


def resolve_commit(repo: Path, ref: str) -> str:
    try:
        sha = git_output(repo, ["rev-parse", "--verify", "%s^{commit}" % ref]).strip()
    except SetupError:
        raise SetupError("cannot resolve git ref %s" % ref)
    if not SHA40.fullmatch(sha):
        raise SetupError("git ref %s did not resolve to a full commit SHA" % ref)
    return sha


def require_head_is_main(repo: Path, main_ref: str = "main") -> str:
    main_sha = resolve_commit(repo, main_ref)
    head_sha = resolve_commit(repo, "HEAD")
    if head_sha != main_sha:
        raise GuardError(
            "REFUSED: HEAD %s is not the main SHA %s; "
            "deploy only from a clean tree at the main SHA"
            % (head_sha, main_sha)
        )
    return main_sha


def default_health_get(url: str) -> dict:
    """One GET. Returns a parsed JSON object or raises GuardError. Fail closed."""
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(16 * 1024)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
        raise GuardError("GET /health failed (%s); failing closed" % type(error).__name__)
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeError) as error:
        raise GuardError("GET /health was not JSON (%s); failing closed" % type(error).__name__)
    if not isinstance(data, dict):
        raise GuardError("GET /health was not a JSON object; failing closed")
    return data


def default_post_p0(text: str) -> None:
    """Write the P0 row to stderr. Operators or CI can capture and post it."""
    print(text, file=sys.stderr)


def health_version(data: dict) -> Optional[int]:
    for key in ("version", "versionNumber", "contractVersion"):
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            raise GuardError("GET /health version %r is not an integer; failing closed" % value)
    return None


def health_commit(data: dict) -> Optional[str]:
    for key in ("sha", "commit", "gitSha", "git_sha"):
        value = data.get(key)
        if isinstance(value, str) and SHA40.fullmatch(value.strip()):
            return value.strip().lower()
    return None


def health_source_hash(data: dict) -> Optional[str]:
    for key in ("sourceSha256", "source_sha256", "sha256"):
        value = data.get(key)
        if isinstance(value, str) and SHA64.fullmatch(value.strip()):
            return value.strip().lower()
    return None


def health_mismatch(
    data: dict,
    expected_version: int,
    main_sha: str,
    source_digest: str,
) -> Optional[str]:
    """Return a reason string if /health does not match, or None if it does."""
    if not isinstance(data, dict):
        return "health response is not a JSON object"
    version = health_version(data)
    if version is None:
        return "health response has no version"
    if version != int(expected_version):
        return "health version is %s, expected %s" % (version, expected_version)
    commit = health_commit(data)
    digest = health_source_hash(data)
    if commit is None and digest is None:
        return "health response has no SHA"
    if commit is not None and commit != main_sha.lower():
        return "health SHA is %s, expected %s" % (commit, main_sha)
    if digest is not None and digest != source_digest.lower():
        return "health source SHA is %s, expected %s" % (digest, source_digest)
    return None


def observed_health(data: Optional[dict]) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "unreadable", "unreadable"
    version = health_version(data)
    commit = health_commit(data) or health_source_hash(data) or "missing"
    return (str(version) if version is not None else "missing"), str(commit)


def format_p0(
    reason: str,
    expected_version: int,
    expected_sha: str,
    observed_version: str,
    observed_sha: str,
    rollback_status: str,
) -> str:
    body = (
        "P0 TASK ID: G15 Apps Script /health mismatch after deploy; "
        "redeployed the previous deployment version\n"
        "expected_version=%s expected_sha=%s\n"
        "observed_version=%s observed_sha=%s\n"
        "reason=%s\n"
        "rollback=%s"
        % (
            expected_version,
            expected_sha,
            observed_version,
            observed_sha,
            reason,
            rollback_status,
        )
    )
    return redact_ids(body)


def rollback_previous(
    runner: ClaspRunner,
    source_dir: Path,
    deployment_id: str,
    previous_version: int,
) -> None:
    run_clasp(
        runner,
        [
            "deploy",
            "--deploymentId",
            deployment_id,
            "--versionNumber",
            str(previous_version),
            "--description",
            "auto-rollback",
        ],
        source_dir,
        "clasp rollback deploy",
    )


def check_health_or_rollback(
    runner: ClaspRunner,
    source_dir: Path,
    deployment_id: Optional[str],
    previous_version: Optional[int],
    deployed_version: int,
    main_sha: str,
    source_digest: str,
    health_url: str,
    health_get: HealthGet,
    post_p0: P0Poster,
) -> None:
    data: Optional[dict] = None
    try:
        data = health_get(health_url)
        reason = health_mismatch(data, deployed_version, main_sha, source_digest)
    except GuardError as error:
        reason = str(error)
    if not reason:
        return
    observed_version, observed_sha = observed_health(data)
    rollback_status = "not-attempted"
    can_rollback = (
        deployment_id
        and previous_version is not None
        and int(previous_version) != int(deployed_version)
    )
    if can_rollback:
        try:
            rollback_previous(runner, source_dir, deployment_id, int(previous_version))
            rollback_status = "ok"
        except GuardError as error:
            rollback_status = "failed: %s" % error
    elif previous_version is None:
        rollback_status = "no-previous-deployment"
    elif previous_version == deployed_version:
        rollback_status = "previous-already-current"
    p0 = format_p0(
        reason,
        deployed_version,
        main_sha,
        observed_version,
        observed_sha,
        rollback_status,
    )
    try:
        post_p0(p0)
    except Exception as error:  # noqa: BLE001 — still report the health failure
        p0 = p0 + "\nP0_post=failed: %s" % type(error).__name__
    raise GuardError(
        "GET /health mismatch (%s); rollback=%s; failing closed"
        % (reason, rollback_status)
    )


def deploy_and_readback(
    source_dir: Path,
    expected_hash: str,
    runner: ClaspRunner,
    deployment_id: Optional[str] = None,
    description: str = "guarded deploy",
    main_sha: Optional[str] = None,
    health_url: Optional[str] = None,
    health_get: Optional[HealthGet] = None,
    post_p0: Optional[P0Poster] = None,
    previous_version: Optional[int] = None,
) -> int:
    """Push, create a version, deploy, read content back, then GET /health."""
    # Snapshot the previous deployment version before we point it at a new one.
    listing_before = run_clasp(
        runner, ["list-deployments"], source_dir, "clasp list-deployments"
    )
    snap = snapshot_previous_version(listing_before.stdout, deployment_id)
    if previous_version is None:
        previous_version = snap

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

    if health_url:
        getter = health_get or default_health_get
        poster = post_p0 or default_post_p0
        if not main_sha:
            raise GuardError("GET /health requires the main SHA; failing closed")
        check_health_or_rollback(
            runner,
            source_dir,
            deployment_id,
            previous_version,
            version,
            main_sha,
            expected_hash,
            health_url,
            getter,
            poster,
        )
    return version


def run_guard(
    repo: Path,
    source_dir: str,
    ref: str = "HEAD",
    deployment_id: Optional[str] = None,
    clasp_runner: Optional[ClaspRunner] = None,
    description: str = "guarded deploy",
    main_ref: str = "main",
    health_url: Optional[str] = None,
    health_get: Optional[HealthGet] = None,
    post_p0: Optional[P0Poster] = None,
    previous_version: Optional[int] = None,
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
        require_clean_tree(repo)
        main_sha = require_head_is_main(repo, main_ref=main_ref)
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
            main_sha=main_sha,
            health_url=health_url,
            health_get=health_get,
            post_p0=post_p0,
            previous_version=previous_version,
        )
        health_note = ""
        if health_url:
            health_note = ", /health version and SHA match"
        return 0, (
            "OK: source matches %s and deployed version %s read back the same content%s\n"
            "source_dir=%s\n"
            "source_sha256=%s\n"
            "main_sha=%s"
            % (ref, version, health_note, source_dir, push_digest, main_sha)
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
    parser.add_argument(
        "--main-ref",
        default="main",
        help="branch whose SHA HEAD must equal (default: main)",
    )
    parser.add_argument("--deployment-id", default=None, help="existing deployment id to update")
    parser.add_argument(
        "--health-url",
        default=None,
        help="URL to GET after deploy; compare version and SHA (for example ?health=build)",
    )
    parser.add_argument(
        "--previous-version",
        type=int,
        default=None,
        help="previous deployment version to roll back to on a /health mismatch",
    )
    parser.add_argument("--description", default="guarded deploy")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    clasp_runner: Optional[ClaspRunner] = None,
    health_get: Optional[HealthGet] = None,
    post_p0: Optional[P0Poster] = None,
) -> int:
    args = build_parser().parse_args(argv)
    code, message = run_guard(
        Path(args.repo),
        args.source_dir,
        ref=args.ref,
        deployment_id=args.deployment_id,
        clasp_runner=clasp_runner,
        description=args.description,
        main_ref=args.main_ref,
        health_url=args.health_url,
        health_get=health_get,
        post_p0=post_p0,
        previous_version=args.previous_version,
    )
    stream = sys.stdout if code == 0 else sys.stderr
    print(message, file=stream)
    return code


if __name__ == "__main__":
    sys.exit(main())

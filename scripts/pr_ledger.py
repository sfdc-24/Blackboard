#!/usr/bin/env python3
"""Read-only PR ledger for the no-mess rule.

Lists, for sfdc-24/conference and sfdc-24/Blackboard:

  * every open pull request
  * every pushed branch whose name starts with cursor/, grok/, or
    claude-code-cli/ and whose head commit date is on or after --since
    (default 2026-11-09, parsed as 2026-11-09T00:00:00Z)

Each listed branch is classified:

  open-pr      the branch has an open pull request
  merged       a pull request from the branch was merged, or the head
               is already reachable from the base branch (compare
               ahead_by=0 versus --base, default main)
  superseded   every commit on the branch is already on the base
               branch, or is contained in an open pull request's
               branch (compare ahead_by=0)
  orphan       none of the above

GitHub is reached only through an injectable runner. The default
runner calls ``gh api`` (with ``--paginate`` on list endpoints) and refuses
any mutating method or write flag. Tests inject a fake runner and
never touch the network.

    python3 scripts/pr_ledger.py
    python3 scripts/pr_ledger.py --since 2026-11-09 --json

Prints orphans first, then the rest. Each line is:

    repo  branch (#PR)  short-sha  class  reason

Exit 0 on success, 2 on error. Orphans are a successful report, not
an error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence
from urllib.parse import quote


DEFAULT_REPOS = ("sfdc-24/conference", "sfdc-24/Blackboard")
DEFAULT_SINCE = "2026-11-09"
DEFAULT_BASE = "main"
BRANCH_PREFIXES = ("cursor/", "grok/", "claude-code-cli/")
CLASS_OPEN_PR = "open-pr"
CLASS_MERGED = "merged"
CLASS_SUPERSEDED = "superseded"
CLASS_ORPHAN = "orphan"
ISO_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$"
)
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MUTATING_METHOD = re.compile(
    r"(^|\s)(-X|--method)\s+(POST|PUT|PATCH|DELETE|post|put|patch|delete)\b"
)
WRITE_FLAGS = re.compile(
    r"(^|\s)(--input|-f|-F|--raw-field|--field|--jq-input)(\s|=|$)"
)
SAFE_ENDPOINT = re.compile(r"^[A-Za-z0-9._~:/?&=%\-,*@+]+$")

GhRunner = Callable[[str], str]


class LedgerError(Exception):
    """The ledger could not be built. The message is safe to print."""


@dataclass(frozen=True)
class Pull:
    number: int
    branch: str
    sha: str
    merged_at: Optional[str]
    from_fork: bool
    state: str


@dataclass(frozen=True)
class Entry:
    repo: str
    branch: str
    pr: Optional[int]
    sha: str
    sha_short: str
    class_name: str
    reason: str
    commit_date: Optional[str] = None


def parse_utc(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to aware UTC, or None if it does not parse."""
    if not isinstance(value, str) or not ISO_TS.match(value):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_since(value: str) -> datetime:
    """Parse --since. A YYYY-MM-DD value is midnight UTC on that day."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("since must be a date or an ISO-8601 UTC timestamp")
    text = value.strip()
    if DATE_ONLY.match(text):
        text = text + "T00:00:00Z"
    dt = parse_utc(text)
    if dt is None:
        raise ValueError(
            "since must be YYYY-MM-DD or an ISO-8601 UTC timestamp, got %r"
            % value
        )
    return dt


def short_sha(sha: str) -> str:
    sha = (sha or "").strip()
    return sha[:7] if len(sha) >= 7 else sha


def assert_get_only(endpoint: str) -> None:
    """Refuse anything that is not a single GitHub REST GET path."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise LedgerError("GitHub request is empty")
    text = endpoint.strip()
    if MUTATING_METHOD.search(text):
        raise LedgerError("refusing mutating GitHub request: GET only")
    if WRITE_FLAGS.search(text):
        raise LedgerError("refusing GitHub write flags: GET only")
    if text.startswith("-") or " " in text or "\n" in text or "\t" in text:
        raise LedgerError("runner accepts a single GET path, not flags or argv")
    lowered = text.lower()
    if lowered.startswith("graphql") or "/graphql" in lowered:
        raise LedgerError("refusing GraphQL: this ledger uses REST GET only")
    if not SAFE_ENDPOINT.match(text):
        raise LedgerError("refusing unexpected characters in GitHub path")


def should_paginate(endpoint: str) -> bool:
    """Paginate list endpoints only.

    ``gh api --paginate`` concatenates each page's JSON. A commit or
    compare object can have a Link header for its files or commits
    list; paginating those yields back-to-back objects, which is not
    valid JSON.
    """
    path = endpoint.split("?", 1)[0]
    return path.endswith("/pulls") or "/git/matching-refs/" in path


RETRYABLE = (
    "i/o timeout",
    "timeout",
    "connection reset",
    "connection refused",
    "temporary failure",
    "tls handshake",
    "eof",
    "502",
    "503",
    "504",
    "secondary rate limit",
    "abuse detection",
)
MAX_ATTEMPTS = 5


def _is_retryable(detail: str) -> bool:
    text = detail.lower()
    return any(token in text for token in RETRYABLE)


def default_runner(endpoint: str) -> str:
    """Call ``gh api`` for one GET path. Never mutates."""
    assert_get_only(endpoint)
    command = ["gh", "api"]
    if should_paginate(endpoint):
        command.append("--paginate")
    command.append(endpoint)
    last_error = "gh api failed"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=90,
            )
        except FileNotFoundError as error:
            raise LedgerError("gh is not installed or not on PATH") from error
        except subprocess.TimeoutExpired as error:
            last_error = "gh api GET %s timed out after 90s" % endpoint
            if attempt == MAX_ATTEMPTS:
                raise LedgerError(last_error) from error
            time.sleep(2 ** (attempt - 1))
            continue
        except OSError as error:
            last_error = "could not run gh api: %s" % error
            if not _is_retryable(str(error)) or attempt == MAX_ATTEMPTS:
                raise LedgerError(last_error) from error
            time.sleep(2 ** (attempt - 1))
            continue
        if result.returncode == 0:
            return result.stdout
        detail = (result.stderr or result.stdout or "exit %s" % result.returncode).strip()
        detail = " ".join(detail.split())
        if len(detail) > 400:
            detail = detail[:400] + "..."
        last_error = "gh api GET %s failed: %s" % (endpoint, detail)
        if not _is_retryable(detail) or attempt == MAX_ATTEMPTS:
            raise LedgerError(last_error)
        time.sleep(2 ** (attempt - 1))
    raise LedgerError(last_error)


def cached_runner(runner: GhRunner) -> GhRunner:
    """Reuse identical GET responses within one ledger run."""
    cache: dict[str, str] = {}

    def wrapped(endpoint: str) -> str:
        assert_get_only(endpoint)
        if endpoint in cache:
            return cache[endpoint]
        raw = runner(endpoint)
        cache[endpoint] = raw
        return raw

    return wrapped


def api_get(runner: GhRunner, endpoint: str) -> Any:
    assert_get_only(endpoint)
    try:
        raw = runner(endpoint)
    except LedgerError:
        raise
    except Exception as error:
        raise LedgerError("GitHub GET %s failed: %s" % (endpoint, error)) from error
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise LedgerError("empty response from GET %s" % endpoint)
    if not isinstance(raw, str):
        raise LedgerError("runner must return JSON text for GET %s" % endpoint)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise LedgerError("invalid JSON from GET %s: %s" % (endpoint, error)) from error


def pulls_endpoint(repo: str, state: str, head: Optional[str] = None) -> str:
    path = "repos/%s/pulls?state=%s&per_page=100" % (repo, quote(state, safe=""))
    if head:
        path += "&head=%s" % quote(head, safe="")
    return path


def matching_refs_endpoint(repo: str, prefix: str) -> str:
    # Trailing slash keeps the match on "cursor/" and not "cursor-other".
    return "repos/%s/git/matching-refs/heads/%s" % (
        repo,
        quote(prefix, safe="/"),
    )


def commit_endpoint(repo: str, sha: str) -> str:
    # git/commits returns author/committer dates without a paginated files list.
    return "repos/%s/git/commits/%s" % (repo, quote(sha, safe=""))


def compare_endpoint(repo: str, base: str, head: str) -> str:
    return "repos/%s/compare/%s...%s" % (
        repo,
        quote(base, safe=""),
        quote(head, safe=""),
    )


def repo_owner(repo: str) -> str:
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise LedgerError("repo must be owner/name, got %r" % repo)
    return parts[0]


def _as_list(payload: Any, endpoint: str) -> list:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    raise LedgerError("expected a JSON array from GET %s" % endpoint)


def _as_dict(payload: Any, endpoint: str) -> dict:
    if isinstance(payload, dict):
        return payload
    raise LedgerError("expected a JSON object from GET %s" % endpoint)


def parse_pull(raw: Any) -> Optional[Pull]:
    if not isinstance(raw, dict):
        return None
    try:
        number = int(raw.get("number"))
    except (TypeError, ValueError):
        return None
    head = raw.get("head") or {}
    if not isinstance(head, dict):
        return None
    sha = str(head.get("sha") or "").strip()
    ref = str(head.get("ref") or "").strip()
    if not sha or not ref:
        return None
    base_repo = raw.get("base") or {}
    head_repo = head.get("repo") or {}
    base_name = ""
    head_name = ""
    if isinstance(base_repo, dict):
        base_name = str(base_repo.get("full_name") or "").strip()
    if isinstance(head_repo, dict):
        head_name = str(head_repo.get("full_name") or "").strip()
    from_fork = bool(head_name and base_name and head_name != base_name)
    if from_fork:
        label = str(head.get("label") or "").strip()
        branch = label or ("%s:%s" % (head_name, ref))
    else:
        branch = ref
    merged_at = raw.get("merged_at")
    if merged_at is not None:
        merged_at = str(merged_at)
        if not merged_at or merged_at.lower() in ("none", "null"):
            merged_at = None
    return Pull(
        number=number,
        branch=branch,
        sha=sha,
        merged_at=merged_at,
        from_fork=from_fork,
        state=str(raw.get("state") or "").strip().lower(),
    )


def list_open_pulls(runner: GhRunner, repo: str) -> list[Pull]:
    endpoint = pulls_endpoint(repo, "open")
    pulls: list[Pull] = []
    seen: set[int] = set()
    for raw in _as_list(api_get(runner, endpoint), endpoint):
        pull = parse_pull(raw)
        if pull is None or pull.number in seen:
            continue
        seen.add(pull.number)
        pulls.append(pull)
    return pulls


def list_merged_pulls_for_head(runner: GhRunner, repo: str, branch: str) -> list[Pull]:
    if ":" in branch:
        # Fork label such as "other-user:feature". Use it as the head filter.
        head = branch
    else:
        head = "%s:%s" % (repo_owner(repo), branch)
    endpoint = pulls_endpoint(repo, "closed", head=head)
    merged: list[Pull] = []
    for raw in _as_list(api_get(runner, endpoint), endpoint):
        pull = parse_pull(raw)
        if pull is not None and pull.merged_at:
            merged.append(pull)
    merged.sort(key=lambda item: item.merged_at or "", reverse=True)
    return merged


def strip_heads_ref(ref: str) -> Optional[str]:
    text = (ref or "").strip()
    prefix = "refs/heads/"
    if text.startswith(prefix) and len(text) > len(prefix):
        return text[len(prefix):]
    return None


def list_prefix_refs(runner: GhRunner, repo: str, prefix: str) -> list[tuple[str, str]]:
    endpoint = matching_refs_endpoint(repo, prefix)
    found: list[tuple[str, str]] = []
    for raw in _as_list(api_get(runner, endpoint), endpoint):
        if not isinstance(raw, dict):
            continue
        name = strip_heads_ref(str(raw.get("ref") or ""))
        obj = raw.get("object") or {}
        sha = ""
        if isinstance(obj, dict):
            sha = str(obj.get("sha") or "").strip()
        if not name or not sha:
            continue
        if not name.startswith(prefix):
            continue
        found.append((name, sha))
    return found


def commit_date(runner: GhRunner, repo: str, sha: str) -> Optional[datetime]:
    endpoint = commit_endpoint(repo, sha)
    payload = _as_dict(api_get(runner, endpoint), endpoint)
    commit = payload.get("commit") if isinstance(payload.get("commit"), dict) else payload
    if not isinstance(commit, dict):
        return None
    dates: list[datetime] = []
    for key in ("committer", "author"):
        block = commit.get(key) or {}
        if not isinstance(block, dict):
            continue
        parsed = parse_utc(str(block.get("date") or ""))
        if parsed is not None:
            dates.append(parsed)
    if not dates:
        return None
    return max(dates)


def compare_ahead(runner: GhRunner, repo: str, base: str, head: str) -> Optional[int]:
    """Return ahead_by for base...head, or None when the compare is missing."""
    endpoint = compare_endpoint(repo, base, head)
    try:
        payload = api_get(runner, endpoint)
    except LedgerError as error:
        text = str(error).lower()
        if "404" in text or "not found" in text:
            return None
        raise
    if not isinstance(payload, dict):
        raise LedgerError("expected a JSON object from GET %s" % endpoint)
    if "ahead_by" not in payload:
        status = str(payload.get("status") or "").strip().lower()
        if status in ("identical", "behind"):
            return 0
        raise LedgerError("compare %s is missing ahead_by" % endpoint)
    try:
        return int(payload["ahead_by"])
    except (TypeError, ValueError) as error:
        raise LedgerError("compare %s has a bad ahead_by" % endpoint) from error


def format_line(entry: Entry) -> str:
    pr_part = " (#%s)" % entry.pr if entry.pr is not None else ""
    return "%s  %s%s  %s  %s  %s" % (
        entry.repo,
        entry.branch,
        pr_part,
        entry.sha_short,
        entry.class_name,
        entry.reason,
    )


def sort_entries(entries: Sequence[Entry]) -> list[Entry]:
    return sorted(
        entries,
        key=lambda item: (
            0 if item.class_name == CLASS_ORPHAN else 1,
            item.repo,
            item.branch,
        ),
    )


def counts_by_repo(entries: Sequence[Entry], repos: Sequence[str]) -> dict[str, dict[str, int]]:
    classes = (CLASS_OPEN_PR, CLASS_MERGED, CLASS_SUPERSEDED, CLASS_ORPHAN)
    out: dict[str, dict[str, int]] = {}
    for repo in repos:
        out[repo] = {name: 0 for name in classes}
    for entry in entries:
        bucket = out.setdefault(entry.repo, {name: 0 for name in classes})
        bucket[entry.class_name] = bucket.get(entry.class_name, 0) + 1
    totals = {name: 0 for name in classes}
    for bucket in out.values():
        for name in classes:
            totals[name] += bucket.get(name, 0)
    out["total"] = totals
    return out


def _date_text(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_branch(
    runner: GhRunner,
    repo: str,
    branch: str,
    sha: str,
    base: str,
    open_by_branch: dict[str, Pull],
    open_pulls: Sequence[Pull],
) -> tuple[str, str, Optional[int]]:
    open_pr = open_by_branch.get(branch)
    if open_pr is not None:
        return CLASS_OPEN_PR, "open PR #%s" % open_pr.number, open_pr.number

    merged = list_merged_pulls_for_head(runner, repo, branch)
    if merged:
        pull = merged[0]
        return CLASS_MERGED, "PR #%s merged" % pull.number, pull.number

    ahead_main = compare_ahead(runner, repo, base, sha)
    if ahead_main == 0:
        return (
            CLASS_MERGED,
            "head reachable from %s (ahead_by=0)" % base,
            None,
        )

    for pull in open_pulls:
        if pull.branch == branch:
            continue
        ahead = compare_ahead(runner, repo, pull.sha, sha)
        if ahead == 0:
            return (
                CLASS_SUPERSEDED,
                "all commits contained in open PR #%s branch %s (ahead_by=0)"
                % (pull.number, pull.branch),
                None,
            )

    return (
        CLASS_ORPHAN,
        "no open or merged PR; unique commits not on %s or in an open PR" % base,
        None,
    )


def build_repo_entries(
    runner: GhRunner,
    repo: str,
    since: datetime,
    base: str,
) -> list[Entry]:
    open_pulls = list_open_pulls(runner, repo)
    open_by_branch = {pull.branch: pull for pull in open_pulls}

    # branch -> (sha, date, always_include)
    selected: dict[str, tuple[str, Optional[datetime], bool]] = {}

    for pull in open_pulls:
        selected[pull.branch] = (pull.sha, None, True)

    for prefix in BRANCH_PREFIXES:
        for name, sha in list_prefix_refs(runner, repo, prefix):
            if name in selected:
                prev_sha, prev_date, always = selected[name]
                # Open PRs are listed regardless of age; skip an extra commit GET.
                selected[name] = (prev_sha or sha, prev_date, always)
                continue
            dated = commit_date(runner, repo, sha)
            selected[name] = (sha, dated, False)

    entries: list[Entry] = []
    for name, (sha, dated, always) in selected.items():
        if not always:
            if dated is not None and dated < since:
                continue
            # No parseable date: still list the branch so an orphan cannot hide.
        class_name, reason, pr_number = classify_branch(
            runner,
            repo,
            name,
            sha,
            base,
            open_by_branch,
            open_pulls,
        )
        entries.append(
            Entry(
                repo=repo,
                branch=name,
                pr=pr_number,
                sha=sha,
                sha_short=short_sha(sha),
                class_name=class_name,
                reason=reason,
                commit_date=_date_text(dated),
            )
        )
    return entries


def build_ledger(
    runner: GhRunner,
    repos: Sequence[str],
    since: datetime,
    base: str,
) -> list[Entry]:
    entries: list[Entry] = []
    for repo in repos:
        repo_owner(repo)  # validates owner/name
        entries.extend(build_repo_entries(runner, repo, since, base))
    return sort_entries(entries)


def entry_json(entry: Entry) -> dict[str, Any]:
    payload = asdict(entry)
    payload["class"] = payload.pop("class_name")
    return payload


def render_json(
    entries: Sequence[Entry],
    repos: Sequence[str],
    since: datetime,
    base: str,
) -> str:
    body = {
        "since": _date_text(since),
        "base": base,
        "repos": list(repos),
        "entries": [entry_json(item) for item in entries],
        "counts": counts_by_repo(entries, repos),
    }
    return json.dumps(body, indent=2, sort_keys=False) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help="include prefix branches whose head is on or after this UTC date "
        "(YYYY-MM-DD or full timestamp; default %s)" % DEFAULT_SINCE,
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        default=list(DEFAULT_REPOS),
        help="owner/name repos to scan (default: %s)" % " ".join(DEFAULT_REPOS),
    )
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help="base branch used for reachability compares (default %s)" % DEFAULT_BASE,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print the ledger as JSON, including per-repo counts",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None, runner: Optional[GhRunner] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        since = parse_since(args.since)
        repos = tuple(args.repos)
        if not repos:
            raise ValueError("at least one --repos value is required")
        seen: set[str] = set()
        unique: list[str] = []
        for repo in repos:
            repo_owner(repo)
            if repo not in seen:
                seen.add(repo)
                unique.append(repo)
        entries = build_ledger(
            cached_runner(runner or default_runner), unique, since, args.base
        )
        if args.as_json:
            sys.stdout.write(render_json(entries, unique, since, args.base))
        else:
            for entry in entries:
                print(format_line(entry))
        return 0
    except (LedgerError, ValueError, OSError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

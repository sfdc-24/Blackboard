#!/usr/bin/env python3
"""Refresh docs/okf/cooking.md from live open PRs in sfdc-24/Blackboard."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
DEFAULT_OWNER = "sfdc-24"
DEFAULT_REPO = "Blackboard"
DEFAULT_OUT = Path(__file__).with_name("cooking.md")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def request_json(url: str, token: str) -> dict | list:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "blackboard-okf-bake",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_open_prs(owner: str, repo: str, token: str) -> list[dict]:
    prs: list[dict] = []
    page = 1
    while True:
        q = urllib.parse.urlencode({"state": "open", "per_page": 100, "page": page})
        url = f"{API}/repos/{owner}/{repo}/pulls?{q}"
        chunk = request_json(url, token)
        if not isinstance(chunk, list):
            raise RuntimeError("Unexpected pulls response payload")
        if not chunk:
            break
        prs.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return prs


def fetch_pr_detail(owner: str, repo: str, number: int, token: str) -> dict:
    return request_json(f"{API}/repos/{owner}/{repo}/pulls/{number}", token)  # type: ignore[return-value]


def fetch_commit_status(owner: str, repo: str, sha: str, token: str) -> dict:
    return request_json(f"{API}/repos/{owner}/{repo}/commits/{sha}/status", token)  # type: ignore[return-value]


def blocker_list(pr: dict, detail: dict, status: dict) -> list[str]:
    blockers: list[str] = []
    if pr.get("draft"):
        blockers.append("Draft PR")

    mergeable_state = str(detail.get("mergeable_state") or "unknown")
    if mergeable_state == "dirty":
        blockers.append("Merge conflicts")
    elif mergeable_state in {"blocked", "behind", "unstable"}:
        blockers.append(f"Mergeability: {mergeable_state}")

    checks = str(status.get("state") or "unknown")
    if checks in {"failure", "error"}:
        blockers.append("Checks failing")
    elif checks == "pending":
        blockers.append("Checks pending")

    label_names = [str(l.get("name") or "") for l in pr.get("labels") or []]
    blocker_labels = [n for n in label_names if any(k in n.lower() for k in ("block", "hold", "decision", "needs"))]
    if blocker_labels:
        blockers.append("Labels: " + ", ".join(sorted(blocker_labels)))

    return blockers


def short_ts(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return value
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def build_markdown(owner: str, repo: str, rows: list[dict]) -> str:
    generated = utc_now()
    lines: list[str] = [
        "---",
        "title: Blackboard OKF Cooking",
        "owner: fleet",
        "status: living",
        f"updated_at: {generated}",
        "source: live-open-prs",
        "---",
        "",
        "# Cooking (WIP + blockers)",
        "",
        f"All-hands visibility surface for open work in `{owner}/{repo}`.",
        "",
        f"_Last baked: {generated}_",
        "",
    ]

    if not rows:
        lines.extend([
            "## Open PRs",
            "",
            "No open pull requests.",
            "",
            "## Blocker rollup",
            "",
            "- None",
            "",
        ])
        return "\n".join(lines) + "\n"

    lines.extend([
        "## Open PRs",
        "",
        "| PR | Title | Author | Updated (UTC) | Checks | Mergeability | Blockers |",
        "|---|---|---|---|---|---|---|",
    ])

    rollup = Counter()
    for row in sorted(rows, key=lambda r: r["updated_at"], reverse=True):
        blockers = row["blockers"]
        if blockers:
            for item in blockers:
                rollup[item] += 1
            blocker_text = "; ".join(blockers)
        else:
            blocker_text = "None declared"
            rollup["None declared"] += 1

        title = str(row["title"]).replace("|", "\\|")
        lines.append(
            f"| [#{row['number']}]({row['url']}) | {title} | @{row['author']} | "
            f"{short_ts(row['updated_at'])} | {row['checks']} | {row['mergeable_state']} | {blocker_text} |"
        )

    lines.extend(["", "## Blocker rollup", ""])
    for item, count in sorted(rollup.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {item}: {count}")

    lines.extend([
        "",
        "## Refresh",
        "",
        "- Run: `python docs/okf/bake.py`",
        f"- Source: `GET /repos/{owner}/{repo}/pulls` (+ per-PR detail/status)",
        "",
    ])
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Refresh docs/okf/cooking.md from live open PRs.")
    ap.add_argument("--owner", default=DEFAULT_OWNER, help="GitHub repository owner/org")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository name")
    ap.add_argument("--output", default=str(DEFAULT_OUT), help="Output markdown path")
    ap.add_argument("--token-env", default="GITHUB_TOKEN", help="Environment variable with GitHub token")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = Path(args.output).resolve()
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"{args.token_env} is required to refresh cooking.md", file=sys.stderr)
        return 2

    try:
        open_prs = fetch_open_prs(args.owner, args.repo, token)
        rows: list[dict] = []
        for pr in open_prs:
            number = int(pr["number"])
            detail = fetch_pr_detail(args.owner, args.repo, number, token)
            status = fetch_commit_status(args.owner, args.repo, str(pr["head"]["sha"]), token)
            rows.append(
                {
                    "number": number,
                    "title": str(pr.get("title") or ""),
                    "url": str(pr.get("html_url") or ""),
                    "author": str((pr.get("user") or {}).get("login") or "unknown"),
                    "updated_at": str(pr.get("updated_at") or ""),
                    "checks": str(status.get("state") or "unknown"),
                    "mergeable_state": str(detail.get("mergeable_state") or "unknown"),
                    "blockers": blocker_list(pr, detail, status),
                }
            )

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_markdown(args.owner, args.repo, rows), encoding="utf-8", newline="\n")
        print(f"Wrote {out} with {len(rows)} open PRs")
        return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
        print(f"GitHub API HTTP {exc.code}: {detail[:500]}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

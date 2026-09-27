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


def request_json(url: str, token: str | None) -> dict | list:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "blackboard-okf-bake",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def expect_object(payload: dict | list, context: str) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected {context} response payload type: {type(payload).__name__}")
    return payload


def fetch_open_prs(owner: str, repo: str, token: str | None) -> list[dict]:
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


def fetch_check_runs(owner: str, repo: str, sha: str, token: str | None) -> dict:
    payload = request_json(f"{API}/repos/{owner}/{repo}/commits/{sha}/check-runs", token)
    return expect_object(payload, "check-runs")


def fetch_combined_status(owner: str, repo: str, sha: str, token: str | None) -> dict:
    payload = request_json(f"{API}/repos/{owner}/{repo}/commits/{sha}/status", token)
    return expect_object(payload, "combined-status")


def check_runs_state(check_runs: dict) -> str:
    runs = check_runs.get("check_runs") or []
    if not runs:
        return "unknown"
    statuses = [str(r.get("status") or "") for r in runs]
    if any(s != "completed" for s in statuses):
        return "pending"
    failing = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
    conclusions = [str(r.get("conclusion") or "") for r in runs]
    return "failure" if any(c in failing for c in conclusions) else "success"


def checks_state(check_runs: dict, combined_status: dict) -> str:
    run_state = check_runs_state(check_runs)
    status_state = str(combined_status.get("state") or "unknown")
    if run_state == "failure" or status_state in {"error", "failure"}:
        return "failure"
    if run_state == "pending" or status_state in {"pending", "expected"}:
        return "pending"
    if run_state == "success" or status_state == "success":
        return "success"
    return "unknown"

def blocker_list(pr: dict, check_state: str) -> list[str]:
    blockers: list[str] = []
    if pr.get("draft"):
        blockers.append("Draft PR")

    if check_state == "failure":
        blockers.append("Checks failing")
    elif check_state == "pending":
        blockers.append("Checks pending")

    allowlist = {
        "blocked",
        "blocker",
        "hold",
        "on-hold",
        "needs-decision",
        "decision-needed",
        "needs-ruling",
    }
    label_names = [str(l.get("name") or "") for l in pr.get("labels") or []]
    blocker_labels = []
    for label in label_names:
        normalized = label.strip().lower()
        if normalized in allowlist or normalized.startswith("blocker:") or normalized.startswith("hold:"):
            blocker_labels.append(label)
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
        "| PR | Title | Author | Updated (UTC) | Checks | Blockers |",
        "|---|---|---|---|---|---|",
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
            f"{short_ts(row['updated_at'])} | {row['checks']} | {blocker_text} |"
        )

    lines.extend(["", "## Blocker rollup", ""])
    for item, count in sorted(rollup.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {item}: {count}")

    lines.extend([
        "",
        "## Refresh",
        "",
        "- Run: `python docs/okf/bake.py`",
        "- Fallback: if API access in sandbox returns HTTP 403, seed from live PR list data and note it in this file.",
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
    token_raw = os.environ.get(args.token_env)
    if token_raw is None:
        token = None
    else:
        token = token_raw.strip()
        if not token:
            print(
                f"{args.token_env} is set but empty; unset it for unauthenticated mode or provide a valid token.",
                file=sys.stderr,
            )
            return 2

    try:
        open_prs = fetch_open_prs(args.owner, args.repo, token)
        rows: list[dict] = []
        for pr in open_prs:
            number = int(pr["number"])
            sha = str(pr["head"]["sha"])
            check_runs = fetch_check_runs(args.owner, args.repo, sha, token)
            run_state = check_runs_state(check_runs)
            if run_state in {"pending", "failure", "success"}:
                check_state = run_state
            else:
                combined_status = fetch_combined_status(args.owner, args.repo, sha, token)
                check_state = checks_state(check_runs, combined_status)
            rows.append(
                {
                    "number": number,
                    "title": str(pr.get("title") or ""),
                    "url": str(pr.get("html_url") or ""),
                    "author": str((pr.get("user") or {}).get("login") or "unknown"),
                    "updated_at": str(pr.get("updated_at") or ""),
                    "checks": check_state,
                    "blockers": blocker_list(pr, check_state),
                }
            )

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_markdown(args.owner, args.repo, rows), encoding="utf-8", newline="\n")
        print(f"Wrote {out} with {len(rows)} open PRs")
        return 0
    except urllib.error.HTTPError as exc:
        status_text = getattr(exc, "reason", "request failed")
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            pass
        detail = (detail[:300] + "...") if len(detail) > 300 else detail
        extra = f" ({detail})" if detail else ""
        if exc.code == 403:
            print(
                "GitHub API HTTP 403: sandbox/API access denied; seed cooking.md from live PR data and annotate the fallback.",
                file=sys.stderr,
            )
        else:
            print(f"GitHub API HTTP {exc.code}: {status_text}{extra}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""The same-SHA gate card (U4 of docs/team/TEAM-ROUND-20261001.md).

WHY THIS EXISTS
  On 2026-10-01, #310's merge command was handed over while a Copilot
  BLOCKER was open on its head b6fa11e. Cursor caught it and the command
  was withdrawn. The unified plan's U4 answer: a merge handoff names ONE
  SHA and links every gate on that SHA, and a helper REFUSES while
  anything is open. This is that helper.

WHAT A CLOSED GATE MEANS HERE (all of these, else REFUSED):
  - The named SHA is one full 40-hex commit, it is the pull request's
    head before AND after the reads (a moved head restarts the reviews),
    and the PR is open, not merged and not a draft.
  - CI on that SHA is green: at least one check run, every run completed
    without a failing conclusion, and the combined commit status is not
    failure or pending. Silence is not a GO: zero runs refuse.
  - Cursor's latest verdict naming that SHA is GO. The verdict must be
    authored by cursor[bot] itself and start the comment; silence or
    NO-GO refuses, and a GO on any other SHA is no GO here.
  - Codex's latest verdict naming that SHA is GO or NO-MAJOR. A Codex
    verdict is a comment whose FIRST line is its marker (CODEX-... or
    "## Codex:"), because dispatches quote "GO or NO-GO" in passing.
    Where Codex does not own the scope (docs/EXPRESS.md section 2),
    --codex-optional waives a MISSING verdict; a present NO-GO still
    refuses.
  - A Copilot review exists on the exact SHA and the latest one carries
    no blocker in its summary ("a BLOCKER can sit in the summary alone"
    - Copilot's own amendment to U4).
  - Every Copilot finding that is high/critical severity or worded as a
    blocker, FROM ANY HEAD of this PR, sits in a resolved thread. A
    moved head cannot drop one: unresolved findings from earlier heads
    carry forward (Codex's amendment to U4).

WHAT IT NEVER DOES
  It performs no merge, no write, no state change anywhere: it reads and
  it judges. The card it prints is the handoff; the merge decision stays
  with the owner. The GitHub token is sent only as a request header and
  never appears in any output.

KNOWN LIMITS (deliberate, fail-closed):
  - The blocker scan is textual. A review summary that merely quotes the
    word "blocker" (say, a docs PR about blockers) refuses; the refusal
    quotes the matched line so a human can adjudicate. The safe failure
    direction is a false refusal, never a false pass.
  - A review thread with more comments than one page is "not fully
    read" and refuses rather than judging half a thread.
  - Thread resolution state only exists in GitHub's GraphQL API, so a
    live run needs a token; `--inputs` judges a recorded snapshot with
    no network at all.

USAGE
  python3 scripts/gate_card.py --pr 310                      # live, GITHUB_TOKEN
  python3 scripts/gate_card.py --pr 310 --sha <40-hex>       # pin the one SHA
  python3 scripts/gate_card.py --pr 310 --snapshot card.json # record the reads
  python3 scripts/gate_card.py --inputs card.json            # judge offline

Exit codes: 0 the gate is closed and the card was printed; 1 REFUSED,
every open item listed; 2 the inputs could not be read or gathered.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
DEFAULT_OWNER = "sfdc-24"
DEFAULT_REPO = "Blackboard"
SCHEMA = "gate-card-inputs-v1"

CURSOR_LOGIN = "cursor"
COPILOT_LOGIN = "copilot-pull-request-reviewer"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# "blocker" as a standalone word, any case: Copilot has written
# "BLOCKER - READ-NOT-DEMONSTRATED", "READ-NOT-DEMONSTRATED (blocker):"
# and "VERDICT: BLOCKER" on this repository's PRs.
BLOCKER_WORD = re.compile(r"(?i)(?<![A-Za-z0-9])blocker(?![A-Za-z0-9])")
# A finding reference in a Copilot review body: severity badge and the
# thread anchor sit on one line.
FINDING_LINE = re.compile(r'alt="(High|Critical) severity"')
DISCUSSION_ANCHOR = re.compile(r"#discussion_r(\d+)")
CODEX_MARKER = re.compile(r"^CODEX-[A-Za-z0-9][A-Za-z0-9-]*$")
CODEX_VERDICT = re.compile(r"\b(NO-GO|NO-MAJOR|GO)\b")
CURSOR_VERDICT = re.compile(r"^(NO-GO|GO)(?![A-Za-z0-9-])")

FAILING_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
}


class GateError(Exception):
    """Inputs that cannot be read or gathered (exit 2), never a verdict."""


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def norm_login(login: str | None) -> str:
    s = str(login or "")
    return s[:-5] if s.endswith("[bot]") else s


def first_nonempty_line(body: str | None) -> str:
    for line in str(body or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def cursor_verdict(comment: dict, named_sha: str) -> str | None:
    """GO/NO-GO when this comment is a Cursor verdict naming the SHA."""
    if norm_login(comment.get("author")) != CURSOR_LOGIN:
        return None
    body = str(comment.get("body") or "")
    if named_sha not in body.lower():
        return None
    lead = first_nonempty_line(body).lstrip("*").strip()
    m = CURSOR_VERDICT.match(lead)
    return m.group(1) if m else None


def codex_verdict(comment: dict, named_sha: str) -> str | None:
    """GO/NO-GO when this comment is a Codex verdict naming the SHA.

    Only a comment whose first non-empty line IS the marker counts:
    dispatches say "Reply GO or NO-GO" and cite CODEX-... ids mid-body,
    and neither of those is a verdict.
    """
    body = str(comment.get("body") or "")
    if named_sha not in body.lower():
        return None
    lead = first_nonempty_line(body)
    if lead.startswith("<!--"):
        lead = lead[4:].replace("-->", "", 1).strip()
    if not (CODEX_MARKER.match(lead) or lead.startswith("## Codex:")):
        return None
    m = CODEX_VERDICT.search(body)
    if m is None:
        return None
    return "NO-GO" if m.group(1) == "NO-GO" else "GO"


def latest_verdict(comments: list[dict], named_sha: str, reader) -> tuple[str, dict] | None:
    """The newest (created_at, then list order) verdict for the SHA."""
    found: tuple[str, dict] | None = None
    for comment in comments:
        verdict = reader(comment, named_sha)
        if verdict is not None:
            found = (verdict, comment)
    return found


def blocker_lines(body: str) -> list[str]:
    return [ln.strip() for ln in str(body or "").splitlines() if BLOCKER_WORD.search(ln)]


def copilot_reviews(reviews: list[dict]) -> list[dict]:
    return [r for r in reviews if norm_login(r.get("author")) == COPILOT_LOGIN]


def copilot_finding_anchors(reviews: list[dict]) -> dict[str, str]:
    """discussion id -> severity, from EVERY Copilot review on any head.

    This is the carry-forward: a high/critical finding stays tracked
    after the head moves, until its thread resolves.
    """
    anchors: dict[str, str] = {}
    for review in copilot_reviews(reviews):
        for line in str(review.get("body") or "").splitlines():
            severity = FINDING_LINE.search(line)
            if not severity:
                continue
            for did in DISCUSSION_ANCHOR.findall(line):
                anchors[did] = severity.group(1)
    return anchors


def thread_discussion_ids(thread: dict) -> set[str]:
    ids: set[str] = set()
    for comment in thread.get("comments") or []:
        explicit = comment.get("discussion_id")
        if explicit is not None:
            ids.add(str(explicit))
        for did in DISCUSSION_ANCHOR.findall(str(comment.get("html_url") or "")):
            ids.add(did)
    return ids


def thread_gate_reason(thread: dict, high_anchors: dict[str, str]) -> str | None:
    """Why this thread gates the merge, or None when it does not."""
    if thread.get("truncated"):
        return "not fully read (more comments than one page); refusing to judge half a thread"
    for comment in thread.get("comments") or []:
        if norm_login(comment.get("author")) != COPILOT_LOGIN:
            continue
        hits = blocker_lines(str(comment.get("body") or ""))
        if hits:
            return "Copilot blocker: " + hits[0][:200]
    for did in thread_discussion_ids(thread):
        severity = high_anchors.get(did)
        if severity:
            return f"Copilot {severity.lower()}-severity finding (discussion_r{did})"
    return None


def check_runs_state(check_runs: list[dict]) -> str:
    if not check_runs:
        return "unknown"
    if any(str(r.get("status") or "") != "completed" for r in check_runs):
        return "pending"
    if any(str(r.get("conclusion") or "") in FAILING_CONCLUSIONS for r in check_runs):
        return "failure"
    return "success"


def ci_state(check_runs: list[dict], combined_status: dict) -> str:
    run_state = check_runs_state(check_runs)
    status_state = str((combined_status or {}).get("state") or "unknown")
    if run_state == "failure" or status_state in {"error", "failure"}:
        return "failure"
    if run_state == "pending" or status_state in {"pending", "expected"}:
        return "pending"
    if run_state == "success":
        return "success"
    return "unknown"


class Judgement:
    def __init__(self) -> None:
        self.closed: list[str] = []  # evidence lines for the card
        self.open: list[str] = []  # refusal lines, every one of them
        self.info: list[str] = []  # never gates, printed for awareness

    @property
    def passed(self) -> bool:
        return not self.open


def judge(inputs: dict, require_codex: bool = True) -> Judgement:
    out = Judgement()
    if inputs.get("schema") != SCHEMA:
        raise GateError(f"inputs schema is not {SCHEMA}")

    named_sha = str(inputs.get("named_sha") or "").lower()
    pr = dict(inputs.get("pr") or {})
    comments = list(inputs.get("issue_comments") or [])
    reviews = list(inputs.get("reviews") or [])
    threads = list(inputs.get("threads") or [])
    check_runs = list(inputs.get("check_runs") or [])
    combined = dict(inputs.get("combined_status") or {})

    # 1. The one SHA.
    if not SHA_RE.match(named_sha):
        raise GateError("the card names ONE full 40-hex commit; got "
                        f"{len(named_sha)} characters")
    head = str(pr.get("head_sha") or "").lower()
    head_after = str(inputs.get("pr_after_head_sha") or "").lower()
    if head != named_sha:
        out.open.append(
            f"the PR head is {head[:12]} but the card names {named_sha[:12]}: "
            "a moved head restarts the reviews"
        )
    elif head_after != named_sha:
        out.open.append(
            f"the head moved to {head_after[:12]} during the reads: "
            "a moved head restarts the reviews"
        )
    else:
        out.closed.append(f"One SHA: `{named_sha}` is the head before and after the reads")

    # 2. The pull request itself.
    state = str(pr.get("state") or "")
    if pr.get("merged"):
        out.open.append("the PR is already merged; there is nothing to hand over")
    elif state != "open":
        out.open.append(f"the PR is {state or 'in an unknown state'}, not open")
    elif pr.get("draft"):
        out.open.append("the PR is a draft; a draft cannot merge (mark it ready first)")
    else:
        line = "PR: open, not a draft"
        mergeable = str(pr.get("mergeable_state") or "")
        if mergeable:
            line += f", mergeable_state {mergeable}"
        out.closed.append(line)

    # 3. CI on the named SHA.
    ci = ci_state(check_runs, combined)
    if ci == "success":
        names = ", ".join(sorted(str(r.get("name") or "?") for r in check_runs))
        out.closed.append(f"CI on this SHA: success ({len(check_runs)} check runs: {names})")
    elif ci == "unknown":
        out.open.append("CI on this SHA reports nothing: silence is not a GO")
    else:
        bad = [
            f"{r.get('name')}={r.get('conclusion') or r.get('status')}"
            for r in check_runs
            if str(r.get("status") or "") != "completed"
            or str(r.get("conclusion") or "") in FAILING_CONCLUSIONS
        ]
        detail = "; ".join(bad) if bad else f"combined status {combined.get('state')}"
        out.open.append(f"CI on this SHA is {ci}: {detail}")

    # 4. Cursor.
    cursor = latest_verdict(comments, named_sha, cursor_verdict)
    if cursor is None:
        out.open.append("no Cursor verdict names this SHA: silence is not a GO")
    elif cursor[0] != "GO":
        out.open.append(f"Cursor's latest verdict on this SHA is NO-GO: {cursor[1].get('html_url')}")
    else:
        out.closed.append(f"Cursor GO: {cursor[1].get('html_url')}")

    # 5. Codex.
    codex = latest_verdict(comments, named_sha, codex_verdict)
    if codex is not None and codex[0] != "GO":
        out.open.append(f"Codex's latest verdict on this SHA is NO-GO: {codex[1].get('html_url')}")
    elif codex is not None:
        out.closed.append(f"Codex GO / NO-MAJOR: {codex[1].get('html_url')}")
    elif require_codex:
        out.open.append("no Codex verdict names this SHA: silence is not a GO "
                        "(--codex-optional only where Codex does not own the scope)")
    else:
        out.closed.append("Codex: no verdict required for this scope (--codex-optional)")

    # 6. The latest Copilot review on the exact SHA.
    on_sha = [r for r in copilot_reviews(reviews)
              if str(r.get("commit_id") or "").lower() == named_sha]
    if not on_sha:
        out.open.append("no Copilot review on this exact SHA yet: ask for one and wait")
    else:
        latest = on_sha[-1]
        hits = blocker_lines(str(latest.get("body") or ""))
        if hits:
            out.open.append(
                "the latest Copilot review on this SHA carries a blocker in its "
                f"summary: \"{hits[0][:200]}\" ({latest.get('html_url')})"
            )
        else:
            out.closed.append(
                f"Copilot review on this SHA: no blocker in the summary ({latest.get('html_url')})"
            )

    # 7. Copilot findings from EVERY head, judged by thread resolution.
    high_anchors = copilot_finding_anchors(reviews)
    tracked = 0
    open_findings = 0
    for thread in threads:
        reason = thread_gate_reason(thread, high_anchors)
        if reason is None:
            continue
        tracked += 1
        if not thread.get("is_resolved"):
            open_findings += 1
            url = ""
            for comment in thread.get("comments") or []:
                url = str(comment.get("html_url") or "") or url
            out.open.append(f"unresolved {reason} - {url or thread.get('id')}")
    if open_findings == 0:
        out.closed.append(
            f"Copilot findings across every head of this PR: all resolved ({tracked} tracked)"
        )

    # Awareness only: open threads that do not gate still deserve eyes.
    other_open = [
        t for t in threads
        if not t.get("is_resolved") and thread_gate_reason(t, high_anchors) is None
    ]
    if other_open:
        urls = []
        for t in other_open:
            for comment in t.get("comments") or []:
                u = str(comment.get("html_url") or "")
                if u:
                    urls.append(u)
                    break
        out.info.append(
            f"{len(other_open)} other unresolved thread(s) do not gate this card: "
            + ", ".join(urls)
        )

    return out


def render(inputs: dict, judgement: Judgement) -> str:
    owner = inputs.get("owner", DEFAULT_OWNER)
    repo = inputs.get("repo", DEFAULT_REPO)
    number = inputs.get("number")
    named_sha = str(inputs.get("named_sha") or "")
    title = f"{owner}/{repo}#{number} @ {named_sha[:12]}"
    lines: list[str] = []
    if judgement.passed:
        lines.append(f"## Gate card - {title}")
        lines.append("")
        lines.extend(f"- {item}" for item in judgement.closed)
        lines.append("")
        lines.append(
            "This card holds for this SHA only; any new push voids it and "
            "restarts the reviews. The merge decision stays with the owner."
        )
    else:
        lines.append(f"REFUSED - the gate is not closed for {title}")
        lines.append("")
        lines.append("Open items (every one of them, not the first):")
        lines.extend(f"- {item}" for item in judgement.open)
    if judgement.info:
        lines.append("")
        lines.extend(f"Not gating: {item}" for item in judgement.info)
    lines.append("")
    lines.append(f"Judged at {utc_now()} from reads gathered at {inputs.get('gathered_at')}.")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Gathering. Everything below talks to GitHub; everything above is pure.
# ----------------------------------------------------------------------

def make_rest(token: str | None):
    def rest(path: str, params: dict | None = None):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "blackboard-gate-card",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    return rest


THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes { databaseId body createdAt url author { login } }
          }
        }
      }
    }
  }
}
"""


def make_graphql(token: str | None):
    def graphql(query: str, variables: dict):
        if not token:
            raise GateError(
                "review-thread resolution lives in GitHub's GraphQL API, which "
                "needs a token; set GITHUB_TOKEN or judge a snapshot with --inputs"
            )
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(
            API + "/graphql",
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "User-Agent": "blackboard-gate-card",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if payload.get("errors"):
            raise GateError("GraphQL refused: "
                            + "; ".join(str(e.get("message")) for e in payload["errors"]))
        return payload

    return graphql


def _paged(rest, path: str, params: dict) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        chunk = rest(path, {**params, "per_page": 100, "page": page})
        if not isinstance(chunk, list):
            raise GateError(f"unexpected payload from {path}")
        items.extend(chunk)
        if len(chunk) < 100:
            return items
        page += 1


def gather(owner: str, repo: str, number: int, sha: str | None,
           rest, graphql) -> dict:
    pr = rest(f"/repos/{owner}/{repo}/pulls/{number}")
    head = str((pr.get("head") or {}).get("sha") or "").lower()
    named_sha = (sha or head).lower()

    comments = [
        {
            "id": c.get("id"),
            "author": str((c.get("user") or {}).get("login") or ""),
            "body": str(c.get("body") or ""),
            "created_at": str(c.get("created_at") or ""),
            "html_url": str(c.get("html_url") or ""),
        }
        for c in _paged(rest, f"/repos/{owner}/{repo}/issues/{number}/comments",
                        {"sort": "created", "direction": "asc"})
    ]
    reviews = [
        {
            "id": r.get("id"),
            "author": str((r.get("user") or {}).get("login") or ""),
            "state": str(r.get("state") or ""),
            "commit_id": str(r.get("commit_id") or ""),
            "body": str(r.get("body") or ""),
            "submitted_at": str(r.get("submitted_at") or ""),
            "html_url": str(r.get("html_url") or ""),
        }
        for r in _paged(rest, f"/repos/{owner}/{repo}/pulls/{number}/reviews", {})
    ]

    check_runs: list[dict] = []
    page = 1
    while True:
        payload = rest(f"/repos/{owner}/{repo}/commits/{named_sha}/check-runs",
                       {"per_page": 100, "page": page})
        chunk = payload.get("check_runs") or []
        check_runs.extend(
            {
                "name": str(r.get("name") or ""),
                "status": str(r.get("status") or ""),
                "conclusion": str(r.get("conclusion") or ""),
                "html_url": str(r.get("html_url") or ""),
            }
            for r in chunk
        )
        if len(check_runs) >= int(payload.get("total_count") or 0) or not chunk:
            break
        page += 1

    combined = rest(f"/repos/{owner}/{repo}/commits/{named_sha}/status")

    threads: list[dict] = []
    cursor = None
    while True:
        payload = graphql(THREADS_QUERY,
                          {"owner": owner, "repo": repo, "number": number,
                           "cursor": cursor})
        conn = (((payload.get("data") or {}).get("repository") or {})
                .get("pullRequest") or {}).get("reviewThreads") or {}
        for node in conn.get("nodes") or []:
            inner = node.get("comments") or {}
            threads.append(
                {
                    "id": str(node.get("id") or ""),
                    "is_resolved": bool(node.get("isResolved")),
                    "is_outdated": bool(node.get("isOutdated")),
                    "truncated": bool((inner.get("pageInfo") or {}).get("hasNextPage")),
                    "comments": [
                        {
                            "discussion_id": c.get("databaseId"),
                            "author": str((c.get("author") or {}).get("login") or ""),
                            "body": str(c.get("body") or ""),
                            "created_at": str(c.get("createdAt") or ""),
                            "html_url": str(c.get("url") or ""),
                        }
                        for c in inner.get("nodes") or []
                    ],
                }
            )
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")

    pr_after = rest(f"/repos/{owner}/{repo}/pulls/{number}")

    return {
        "schema": SCHEMA,
        "owner": owner,
        "repo": repo,
        "number": number,
        "named_sha": named_sha,
        "gathered_at": utc_now(),
        "pr": {
            "state": str(pr.get("state") or ""),
            "draft": bool(pr.get("draft")),
            "merged": bool(pr.get("merged")),
            "head_sha": head,
            "mergeable_state": str(pr.get("mergeable_state") or ""),
            "html_url": str(pr.get("html_url") or ""),
        },
        "pr_after_head_sha": str((pr_after.get("head") or {}).get("sha") or "").lower(),
        "issue_comments": comments,
        "reviews": reviews,
        "threads": threads,
        "check_runs": check_runs,
        "combined_status": {"state": str((combined or {}).get("state") or "unknown")},
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Print the same-SHA gate card, or refuse while anything is open."
    )
    ap.add_argument("--owner", default=DEFAULT_OWNER)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--pr", type=int, help="pull request number (live mode)")
    ap.add_argument("--sha", help="the ONE full 40-hex SHA; defaults to the PR head")
    ap.add_argument("--codex-optional", action="store_true",
                    help="waive a MISSING Codex verdict where Codex does not own "
                         "the scope (docs/EXPRESS.md); a present NO-GO still refuses")
    ap.add_argument("--snapshot", help="write the gathered inputs JSON here")
    ap.add_argument("--inputs", help="judge this recorded inputs JSON; no network")
    ap.add_argument("--token-env", default="GITHUB_TOKEN")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.inputs:
            with open(args.inputs, "r", encoding="utf-8") as fh:
                inputs = json.load(fh)
        elif args.pr:
            token = (os.environ.get(args.token_env) or "").strip() or None
            inputs = gather(args.owner, args.repo, args.pr, args.sha,
                            make_rest(token), make_graphql(token))
            if args.snapshot:
                with open(args.snapshot, "w", encoding="utf-8") as fh:
                    json.dump(inputs, fh, indent=1)
        else:
            print("one of --pr or --inputs is required", file=sys.stderr)
            return 2
        judgement = judge(inputs, require_codex=not args.codex_optional)
    except GateError as exc:
        print(f"cannot judge: {exc}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"cannot read: {exc}", file=sys.stderr)
        return 2
    print(render(inputs, judgement))
    return 0 if judgement.passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

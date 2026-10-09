"""Redis requests from GIT, where the sender IS authenticated: GitHub says who wrote a comment.

WHY THIS EXISTS. Mr. Salam, 2026-10-08 ~14:00Z, directly to claude-code-cli: "Cursor works out of Git
but is the only representative from GIT we can count on, as such Cursor also needs access (read
write) in Redis." Cursor has no board credential and never posts board rows; it speaks in pull
request and issue comments, as the GitHub App account cursor[bot].

WHY IT IS BETTER THAN THE BOARD, NOT WORSE. The board has no authenticated sender (bus_request.py
says so at length). A GitHub comment does: its author is whoever GitHub authenticated, and a bot's
numeric user id cannot be claimed by anyone else. So a principal here is bound to a NUMERIC GITHUB
USER ID in the access list (redis_acl.json "git.bots"), never to a login string, never to anything the
comment body says. A line in the body cannot choose its sender.

THE SHAPE: A REQUEST COMMENT IS NOTHING BUT REQUESTS. A comment by a bound bot account is a request
comment only when EVERY non-blank line of it, stripped, is a request line of the form
    BCB|v=1|do=redis-op|op=set|key=conf:...|val=...
and there are at most MAX_LINES_PER_COMMENT of them. One line of anything else - prose, a fence, a
quote marker, a backtick - and the WHOLE comment is not a request. That is a closed grammar on
purpose. The first live run (bus-requests-54r88) executed Cursor's set twice because a REPORT quoted
the request lines in a fence; the next version excluded fences, and aya's adversarial suite on
4d13e97 still got quoted operations through a mismatched fence, a short or trailing-text closer,
indented code, multi-line inline code and lazy quote continuations - 15 of 26 assertions. Markdown has
too many ways to quote a line to enumerate them, so this no longer tries: a report about a request
always has words in it, and a comment with words in it asks for nothing.

The lines go through exactly the board grammar, so the same validation, the same access list, the
same protected namespaces and the same gov:audit apply, through bus_request.handle(). Each line
becomes a board-shaped row whose request id is SERVER-NAMED from the repository, the comment id and
a digest of the line (GH-<repo>-<comment id>-<sha12>), and a board row may not use that shape.
A line whose fields repeat a key is refused (bus_request), never read first-value-wins. Running it at
most once is bus_request's job too: a Redis-side marker is claimed before the operation, so neither a
lost result row nor a second run over a stale snapshot can apply it again. The row id names the
comment, so the audit entry points at the exact text that asked.

The answer goes to the board under that request id, like every other answer. Mirroring it back into
the pull request thread needs a GitHub write token in this job, which is a secret grant and his.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
MAX_PAGES = 5                    # 500 comments a run is far beyond any real window; the cap bounds a flood
MAX_LINES_PER_COMMENT = 10       # one comment cannot spend the whole run's budget
MAX_RESPONSE_BYTES = 2_000_000
# One request line, already stripped: starts with the envelope, names redis-op, and holds no markdown
# quoting character anywhere. The comment-level rule (every non-blank line must match) is in
# request_lines(); this pattern alone is not the guard.
_LINE = re.compile(r"^BCB\|v=1\|[^`>~]*\bdo=redis-op\b[^`>~]*$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
NOTHING = {"owner": "", "repos": [], "bots": {}}


def config(acl_path=None) -> dict:
    """The git section of the access list: {"owner", "repos", "bots": {numeric id: principal}}.

    Read from the same file, so binding a bot to a principal is the controller's reviewed commit and
    changes the ACL digest every audit entry names. Absent or malformed: no repositories, no bots -
    the git channel grants NOTHING, the same direction as a missing access list."""
    import redis_gov                                                     # noqa: PLC0415
    try:
        data = json.loads(Path(acl_path or redis_gov.ACL_FILE).read_text(encoding="utf-8"))
        git = data.get("git") or {}
        bots = {str(int(k)): str(v).lower() for k, v in (git.get("bots") or {}).items()}
        repos = [r for r in (git.get("repos") or []) if isinstance(r, str) and _NAME.match(r)]
        owner = str(git.get("owner") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        return dict(NOTHING)
    if not _NAME.match(owner):
        return dict(NOTHING)
    return {"owner": owner, "repos": repos, "bots": bots}


def _get(url, token=None, opener=None):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "sfdc24-bus-requests"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with (opener or urllib.request.urlopen)(req, timeout=20) as r:
        return json.loads(r.read(MAX_RESPONSE_BYTES).decode("utf-8"))


def fetch_comments(owner, repo, since, token=None, opener=None) -> list:
    """Issue and pull-request conversation comments updated since `since`, oldest first."""
    out = []
    for page in range(1, MAX_PAGES + 1):
        q = urllib.parse.urlencode({"since": since, "per_page": 100, "page": page,
                                    "sort": "updated", "direction": "asc"})
        got = _get("%s/repos/%s/%s/issues/comments?%s" % (API, owner, repo, q), token, opener)
        if not isinstance(got, list):
            break
        out.extend(c for c in got if isinstance(c, dict))
        if len(got) < 100:
            break
    return out


def request_lines(body) -> list:
    """The request lines of a comment, or [] when the comment is not a request comment.

    ALL OR NOTHING: every non-blank line, stripped, must be a request line, and there may be at most
    MAX_LINES_PER_COMMENT of them. A comment that also says anything else - including a fence, a quote
    marker or a single word - is a comment, not a request, and contributes nothing.

    INDENTED CODE IS QUOTED CODE (aya on ce941b6): a line starting with a tab or four or more spaces
    is a Markdown code block - something shown, not asked - so it too makes the comment not a request."""
    raw = [line for line in str(body or "").splitlines() if line.strip()]
    if any(line.startswith("\t") or len(line) - len(line.lstrip(" ")) >= 4 for line in raw):
        return []
    lines = [line.strip() for line in raw]
    if not lines or len(lines) > MAX_LINES_PER_COMMENT:
        return []
    if not all(_LINE.match(line) for line in lines):
        return []
    return lines


class GitRow(list):
    """A request row built HERE, from a comment GitHub authenticated. Its type is the only proof of
    that: a row read from the board is a plain list, whatever its cells say, so a board row that
    copies a git row's ids cannot pass as one (Cursor on 39ebd38: a board row with source `cursor`
    and req `GH-Blackboard-<cid>-1` claimed the run marker and the bot's own line was refused)."""
    channel = "git"


def line_id(line) -> str:
    """A request line's id: a digest of the line itself, not its position. Cursor on 39ebd38: with
    the line NUMBER, inserting a line above an answered one gave the new line the old id (refused
    as claimed, never run) and re-ran the old line under the next number. A digest moves with its
    line. Two identical lines in one comment are one request, answered once."""
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:12]


def poll_seconds(token=None, acl_path=None) -> int:
    """How often live mode may read GitHub: one call per repo per poll, under the hourly limit with
    room to spare (Cursor on 39ebd38: two repos once a minute is 120 calls an hour against the 60
    an unauthenticated caller gets, and after half an hour every fetch fails)."""
    repos = len(config(acl_path)["repos"]) or 1
    budget = 4000 if token else 50          # GitHub allows 5000/h with a token, 60/h without
    return max(60, math.ceil(3600.0 * repos / budget))


def rows_from_comments(repo, comments, bots) -> list:
    """Board-shaped request rows from the comments a BOUND bot wrote. Everything else is ignored.

    The sender is the principal bound to the comment author's numeric id - and only when GitHub says
    the author is a Bot, so a human account cannot pass on an id typo. The request id and the row id
    are the server's. The timestamp is the comment's creation, so the board's staleness rule applies
    to when it was ASKED, not to when it was last edited."""
    rows = []
    for c in comments:
        user = c.get("user") or {}
        who = bots.get(str(user.get("id")))
        if not who or user.get("type") != "Bot":
            continue
        try:
            cid = int(c.get("id"))
        except (TypeError, ValueError):
            continue
        for line in request_lines(c.get("body")):
            digest = line_id(line)
            req_id = "GH-%s-%d-%s" % (re.sub(r"[^A-Za-z0-9]", "", repo)[:20], cid, digest)
            # The body's own id/req fields are dropped: the server names the request.
            payload = re.sub(r"\|\s*(?:id|req)\s*=[^|]*", "", line) + "|req=" + req_id
            rows.append(GitRow([
                "gh:%s:%d:%s" % (repo, cid, digest),
                str(c.get("created_at") or ""),
                who,
                "bus-reconciler",
                "AYA_REQ",
                payload,
                "OPEN",
                "Blackboard",
                "git request from %s" % who,
                str(c.get("html_url") or "")[:200],
            ]))
    return rows


def git_rows(since_dt, token=None, opener=None, acl_path=None, log=None) -> list:
    """Every request row the git channel contributes this run. Never raises: a GitHub outage or a
    rate limit costs this channel one run, and must not stop the board's requests being answered."""
    cfg = config(acl_path)
    if not cfg["bots"] or not cfg["repos"]:
        return []
    since = since_dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for repo in cfg["repos"]:
        try:
            rows.extend(rows_from_comments(repo, fetch_comments(cfg["owner"], repo, since, token, opener),
                                           cfg["bots"]))
        except (urllib.error.URLError, OSError, ValueError) as error:
            if log:
                log("git channel: %s unreadable this run (%s)" % (repo, type(error).__name__))
    return rows

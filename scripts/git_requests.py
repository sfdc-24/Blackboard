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

THE SHAPE. Every comment in the configured repositories updated since the window start is read; a
comment by a bound bot account contributes one request per line of the form
    BCB|v=1|do=redis-op|op=set|key=conf:...|val=...
- exactly the board grammar, so the same validation, the same access list, the same protected
namespaces and the same gov:audit apply, through bus_request.handle(). Each line becomes a
board-shaped row whose request id is SERVER-NAMED from the repository, the comment id and the line
number (GH-<repo>-<comment id>-<n>), so it is unique, cannot collide with a board request, and an
edited comment cannot re-run a line that was already answered. The row id names the comment, so the
audit entry points at the exact text that asked.

The answer goes to the board under that request id, like every other answer. Mirroring it back into
the pull request thread needs a GitHub write token in this job, which is a secret grant and his.
"""
from __future__ import annotations

import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
MAX_PAGES = 5                    # 500 comments a run is far beyond any real window; the cap bounds a flood
MAX_LINES_PER_COMMENT = 10       # one comment cannot spend the whole run's budget
MAX_RESPONSE_BYTES = 2_000_000
# A request is a BARE line: nothing before BCB but whitespace. Not inside a ``` fence, not in `code`,
# not after a > quote. The first live run (bus-requests-54r88) executed Cursor's set TWICE: Cursor
# posted the two lines, then edited its "Taking a look!" comment into a report that QUOTED them in a
# fence, and the old pattern, which allowed backticks, read the quote as a second request. A report
# about a request is not a request.
_LINE = re.compile(r"^\s*(BCB\|v=1\|.*\bdo=redis-op\b.*?)\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
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
        n = 0
        fenced = False
        for line in str(c.get("body") or "").splitlines():
            if _FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            m = _LINE.match(line)
            if not m:
                continue
            n += 1
            if n > MAX_LINES_PER_COMMENT:
                break
            req_id = "GH-%s-%d-%d" % (re.sub(r"[^A-Za-z0-9]", "", repo)[:20], cid, n)
            # The body's own id/req fields are dropped: the server names the request.
            payload = re.sub(r"\|\s*(?:id|req)\s*=[^|]*", "", m.group(1)) + "|req=" + req_id
            rows.append([
                "gh:%s:%d:%d" % (repo, cid, n),
                str(c.get("created_at") or ""),
                who,
                "bus-reconciler",
                "AYA_REQ",
                payload,
                "OPEN",
                "Blackboard",
                "git request from %s" % who,
                str(c.get("html_url") or "")[:200],
            ])
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

#!/usr/bin/env python3
"""Land Gemini's answer as an OKF file in the PRIVATE conference repository, for review.

WHY THIS EXISTS
  Gemini can post phase=RESULT on the bus (Blackboard #297/#298) and can read
  public PR diffs (Blackboard #299/#302). It could not write the OKF: the waker
  held no write token and its doctrine said "no PR". The owner, 2026-09-30:
  every agent should read and write the OKF, Gemini included, and Gemini owns
  the conference experience for the 11:00 Toronto call.

WHERE IT WRITES, AND WHY THERE
  The board is private. Codex's security review of 5e2a4cb (NO-GO, 04:41Z)
  ruled that no free model text from it may go to a public repository: a regex
  scrub cannot recognise a client name or a codeword, and a public commit is
  published before any review. So the only destination is sfdc-24/conference,
  which is private and holds the conference OKF (docs/okf). The lander asks
  GitHub that it is still private at the start and again right before each
  write: the branch, the file and the pull request (Codex on 6d0ba6f). That
  narrows the window in which a visibility change could publish the text; it
  cannot close it, so the repository staying private is a deployment rule.
  - docs/okf/gemini/<row id>.md, for a signed RESULT; or
  - docs/okf/calls/notes/gemini.md, Gemini's prepared notes for the next call,
    when the row says file=call-notes. The notes name the call that the plan on
    the repository's main (docs/okf/calls/next.md, its `call:`) names, because the
    chair gives a note only to its agent, only when ready, and only for that call
    (conference #99). A plan that names no call lands nothing.

WHAT STARTS IT
  A structured field in the asking row, never its prose: land=okf, a BCB field
  (the first value wins). "Do not write OKF" or a quoted "land okf" starts
  nothing. file=call-notes chooses the notes file.

WHAT COUNTS AS LANDED
  The file written on its own branch (gemini/okf-<id>) AND a pull request open
  for it. The merge is a review by Claude or Codex. A file written without a
  pull request is reported as NOT landed, with the branch named, so no answer
  cites okf= for work nobody can review.

TOKEN
  GEMINI_OKF_WRITE_TOKEN, else GEMINI_GITHUB_TOKEN: on 2026-09-30 at 04:13Z the
  owner mounted the secret github-token-gemini-okf on gemini-waker under that
  name. A token that cannot see or write the repository comes back as the HTTP
  status: the exact blocker, in Gemini's answer. It is sent only to
  api.github.com, never across a redirect.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
OWNER = "sfdc-24"
REPO = "conference"
PATH_PREFIX = "docs/okf/gemini/"
NOTES_PATH = "docs/okf/calls/notes/gemini.md"
PLAN_PATH = "docs/okf/calls/next.md"
MAX_BYTES = 50_000
BRANCH_PREFIX = "gemini/okf-"
TOKEN_ENV = "GEMINI_OKF_WRITE_TOKEN"
TOKEN_ENVS = (TOKEN_ENV, "GEMINI_GITHUB_TOKEN")

# Kept out of the file even in a private repository: a key, a token or a
# person's contact details have no place in the OKF.
_SCRUB = (
    (re.compile(r"(?i)(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{10,}"), "[token removed]"),
    (re.compile(r"(?i)\b(?:sk|xox[baprs])-[A-Za-z0-9-]{8,}"), "[token removed]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "[token removed]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "[token removed]"),
    (re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]*?(?:-----END [A-Z ]+-----|$)"), "[key removed]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email removed]"),
    (re.compile(r"(?<![\w/.-])\+?\d[\d ().-]{8,}\d(?![\w/-])"), "[number removed]"),
    (re.compile(r"(https?://[^\s?#)]+)\?[^\s)#]*"), r"\1"),
)


def scrub(text: str) -> str:
    """The reply without key shapes, tokens, email addresses, phone numbers or URL queries."""
    out = text or ""
    for pattern, replacement in _SCRUB:
        out = pattern.sub(replacement, out)
    return out


def token_from(env) -> tuple[str, str]:
    """(token, the variable it came from): the write token first, then the one mounted."""
    for name in TOKEN_ENVS:
        value = (env.get(name) or "").strip()
        if value:
            return value, name
    return "", ""


class _StayOnGitHub(urllib.request.HTTPRedirectHandler):
    """Never send the write token off api.github.com (same rule as repo_context)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.netloc != urllib.parse.urlsplit(API).netloc:
            raise urllib.error.HTTPError(
                newurl, code, "redirect off api.github.com refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_StayOnGitHub)


def fields(ask_text: str) -> dict:
    """The asking row's BCB fields, first value wins (a later land= is a quote, not a rewrite)."""
    out = {}
    for segment in (ask_text or "").split("|"):
        key, sep, value = segment.partition("=")
        key = key.strip().lower()
        if sep and re.fullmatch(r"[a-z_]{1,20}", key) and key not in out:
            out[key] = value.strip()
    return out


def wants_okf(ask_text: str) -> bool:
    """True only when the row carries the structured field land=okf."""
    return fields(ask_text).get("land", "").lower() == "okf"


def slug_for(answers_id: str, ask_text: str = "") -> str:
    """A stable, path-safe filename stem from the source BCB id."""
    raw = fields(ask_text).get("id") or (answers_id or "anon")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip()).strip("-.")[:80]
    return cleaned or "anon"


def _safe_path(slug: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (slug or "anon")).strip("-.")[:80] or "anon"
    path = PATH_PREFIX + name + ".md"
    if ".." in path or path.count("/") != 3:
        raise ValueError("refusing path outside %s: %s" % (PATH_PREFIX, path))
    return path


def target_path(answers_id: str, ask_text: str) -> str:
    if fields(ask_text).get("file", "").lower() == "call-notes":
        return NOTES_PATH
    return _safe_path(slug_for(answers_id, ask_text))


def plan_call(plan_text: str) -> str:
    """The `call:` in the plan's front matter, or "" when it has none (or has it twice)."""
    text = (plan_text or "").replace("\r\n", "\n")
    front = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    calls = [line.partition(":")[2].strip() for line in (front.group(1).splitlines() if front else ())
             if line.partition(":")[0].strip().lower() == "call"]
    return calls[0] if len(calls) == 1 and calls[0] else ""


def render_okf(*, answers_id: str, ask_text: str, reply_body: str, route: str,
               path: str = "", call: str = "") -> str:
    """The file: Gemini's reply, signed, with the asking row's id and never its text."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = scrub((reply_body or "").strip()) or "(empty model reply)"
    if path == NOTES_PATH:
        return "\n".join([
            "---",
            "type: call-notes",
            "agent: gemini",
            "call: %s" % call,
            "status: ready",
            "written_by: gemini, landed by its adapter (scripts/okf_land.py) at %s" % now,
            "answers: %s" % (answers_id or ""),
            "---",
            "# What I will say",
            body,
            "",
        ])
    return "\n".join([
        "# Gemini signed OKF RESULT",
        "",
        "- **signed_by:** gemini",
        "- **evidence:** STATED",
        "- **route:** %s" % (route or "unknown"),
        "- **answers:** board row %s (the row is not copied here)" % (answers_id or ""),
        "- **landed_at:** %s" % now,
        "",
        "## RESULT",
        "",
        body,
        "",
        "---",
        "Signed: Gemini waker adapter (`scripts/okf_land.py`). Not a measurement.",
        "",
    ])


def _request(method: str, path: str, token: str, payload=None, timeout: float = 30):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sfdc24-gemini-okf-land",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    if token:
        req.add_unredirected_header("Authorization", "Bearer " + token)
    with _OPENER.open(req, timeout=timeout) as r:
        raw = r.read(2_000_000)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", "replace"))


def land(*, answers_id: str, ask_text: str, reply_body: str, route: str = "",
         env=None, http=None) -> dict:
    """Land the file for review. Returns {ok, pr, path, branch} or {ok: False, skipped|error, path}."""
    env = os.environ if env is None else env
    path = target_path(answers_id, ask_text)
    token, _ = token_from(env)
    if not token:
        return {"ok": False, "skipped": "no %s or %s" % TOKEN_ENVS, "path": path}

    do = http or _request
    repo = "/repos/%s/%s" % (OWNER, REPO)
    branch = BRANCH_PREFIX + slug_for(answers_id, ask_text)[:40].lower()
    call = ""
    if path == NOTES_PATH:
        try:
            plan = do("GET", repo + "/contents/%s?ref=main" % PLAN_PATH, token)
            call = plan_call(base64.b64decode((plan or {}).get("content") or "").decode("utf-8", "replace"))
        except (urllib.error.HTTPError, ValueError, TypeError) as e:
            return {"ok": False, "error": "could not read the plan's call (%s)" % getattr(e, "code", type(e).__name__),
                    "path": path}
        if not call:
            return {"ok": False, "error": "the plan on main names no call, so no notes can be for it", "path": path}
    content = render_okf(answers_id=answers_id, ask_text=ask_text,
                         reply_body=reply_body, route=route, path=path, call=call)
    raw = content.encode("utf-8")
    if len(raw) > MAX_BYTES:
        return {"ok": False, "error": "OKF body over %d bytes" % MAX_BYTES, "path": path}
    step = "reading %s/%s" % (OWNER, REPO)

    def private() -> bool:
        meta = do("GET", repo, token)
        return isinstance(meta, dict) and meta.get("private") is True

    refused = {"ok": False, "error": "refused: %s/%s is not private" % (OWNER, REPO), "path": path}
    try:
        if not private():
            return refused
        step = "reading main"
        main = do("GET", repo + "/git/ref/heads/main", token)
        main_sha = ((main.get("object") or {}).get("sha") or "").strip()
        if not main_sha:
            return {"ok": False, "error": "main tip missing", "path": path}
        step = "creating branch %s" % branch
        if not private():
            return refused
        try:
            do("POST", repo + "/git/refs", token, {"ref": "refs/heads/%s" % branch, "sha": main_sha})
        except urllib.error.HTTPError as e:
            if e.code != 422:           # 422: the branch exists (a re-ask updates it)
                raise
        sha = None
        step = "reading %s on %s" % (path, branch)
        try:
            existing = do("GET", repo + "/contents/%s?ref=%s"
                          % (urllib.parse.quote(path), urllib.parse.quote(branch)), token)
            if isinstance(existing, dict):
                sha = existing.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        what = "call notes" if path == NOTES_PATH else "RESULT %s" % slug_for(answers_id, ask_text)
        put = {"message": "okf: Gemini's %s" % what,
               "content": base64.b64encode(raw).decode("ascii"), "branch": branch}
        if sha:
            put["sha"] = sha
        step = "writing %s" % path
        if not private():
            return refused
        do("PUT", repo + "/contents/%s" % urllib.parse.quote(path), token, put)
        step = "opening the pull request"
        prs = do("GET", repo + "/pulls?head=%s:%s&state=open"
                 % (OWNER, urllib.parse.quote(branch)), token)
        if isinstance(prs, list) and prs:
            pr_url = prs[0].get("html_url") or ""
        else:
            if not private():
                return dict(refused, branch=branch)
            created = do("POST", repo + "/pulls", token, {
                "title": "okf: Gemini's %s" % what,
                "head": branch,
                "base": "main",
                "body": ("Written by Gemini and landed by its adapter (Blackboard `scripts/okf_land.py`) "
                         "for review.\n\n- answers: board row `%s`\n- path: `%s`\n- evidence: STATED\n\n"
                         "Merge after a review by Claude or Codex." % (answers_id, path)),
            })
            pr_url = (created or {}).get("html_url") or ""
        if not pr_url:
            return {"ok": False, "error": "file written on private branch %s but no pull request "
                    "was opened" % branch, "path": path, "branch": branch}
        return {"ok": True, "pr": pr_url, "url": pr_url, "path": path, "branch": branch}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "HTTP %s while %s" % (e.code, step), "path": path,
                "branch": branch}
    except Exception as e:  # noqa: BLE001 - landing must not crash the doorbell
        return {"ok": False, "error": "%s while %s" % (type(e).__name__, step), "path": path,
                "branch": branch}

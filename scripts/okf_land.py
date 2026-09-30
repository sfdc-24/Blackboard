#!/usr/bin/env python3
"""Land a Gemini-signed OKF markdown file on public Blackboard via GitHub Contents API.

WHY THIS EXISTS
  Gemini can post phase=RESULT on the bus (Blackboard #297/#298) and can read
  public PR diffs (Blackboard #299/#302). It still could not land an OKF file:
  the waker holds no write token and its doctrine said "no PR". Owner 2026-09-30
  transferred Gemini-fix ownership to Grok+Cursor+Codex: Gemini must write
  signed OKF RESULT files, not DONE/waker-only.

WHAT IT DOES
  - Only repository: sfdc-24/Blackboard (public).
  - Only paths under docs/okf/gemini/.
  - Creates or updates a file on a dedicated branch, then opens a PR.
  - Returns the PR / blob URL for the bus RESULT to cite as okf=.

WHAT IT REFUSES
  - Any path outside docs/okf/gemini/.
  - Any repository other than Blackboard.
  - Missing GEMINI_OKF_WRITE_TOKEN (separate from the public-read-only
    GEMINI_GITHUB_TOKEN). Without the write token it returns a clear skip;
    the answer still posts on the bus.
  - Auto-merge. Human / Codex review before merge.

SECURITY
  Board rows are not provenance. Content landed is the model's own reply,
  path-allowlisted, on a public repo. The write token must be a fine-grained
  PAT scoped to Blackboard Contents:Write + Pull requests:Write only.
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
REPO = "Blackboard"
PATH_PREFIX = "docs/okf/gemini/"
MAX_BYTES = 50_000
BRANCH_PREFIX = "gemini/okf-"
TOKEN_ENV = "GEMINI_OKF_WRITE_TOKEN"


class _StayOnGitHub(urllib.request.HTTPRedirectHandler):
    """Never send the write token off api.github.com (same rule as repo_context)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.netloc != urllib.parse.urlsplit(API).netloc:
            raise urllib.error.HTTPError(
                newurl, code, "redirect off api.github.com refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_StayOnGitHub)


def wants_okf(ask_text: str) -> bool:
    """True when the board ask requires a signed OKF file, not bus prose alone."""
    text = (ask_text or "").lower()
    needles = (
        "signed okf",
        "okf result",
        "okf path",
        "okf file",
        "write okf",
        "okf=",
        "docs/okf",
        "land okf",
        "okf pr",
    )
    return any(n in text for n in needles)


def slug_for(answers_id: str, ask_text: str = "") -> str:
    """A stable, path-safe filename stem from the source BCB id."""
    raw = (answers_id or "anon").strip()
    m = re.search(r"id=([A-Za-z0-9._-]{3,80})", ask_text or "")
    if m:
        raw = m.group(1)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")[:80]
    return cleaned or "anon"


def _safe_path(slug: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (slug or "anon"))[:80] + ".md"
    path = PATH_PREFIX + name
    if not path.startswith(PATH_PREFIX) or ".." in path or path.count("/") != 3:
        raise ValueError("refusing path outside %s: %s" % (PATH_PREFIX, path))
    return path


def render_okf(*, answers_id: str, ask_text: str, reply_body: str, route: str) -> str:
    """Gemini-signed OKF markdown. evidence=STATED: model reasoning, not MEASURED."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = (reply_body or "").strip() or "(empty model reply)"
    ask = (ask_text or "").strip()[:2000]
    return "\n".join([
        "# Gemini signed OKF RESULT",
        "",
        "- **signed_by:** gemini",
        "- **evidence:** STATED",
        "- **route:** %s" % (route or "unknown"),
        "- **answers:** %s" % (answers_id or ""),
        "- **landed_at:** %s" % now,
        "- **path_rule:** docs/okf/gemini/ only on sfdc-24/Blackboard",
        "",
        "## Ask (board excerpt)",
        "",
        "```",
        ask,
        "```",
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
    """Land the signed OKF file. Returns {ok, skipped|url|pr, path, error?}."""
    env = os.environ if env is None else env
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token:
        return {"ok": False, "skipped": "no %s" % TOKEN_ENV,
                "path": PATH_PREFIX + slug_for(answers_id, ask_text) + ".md"}
    slug = slug_for(answers_id, ask_text)
    path = _safe_path(slug)
    content = render_okf(answers_id=answers_id, ask_text=ask_text,
                         reply_body=reply_body, route=route)
    raw = content.encode("utf-8")
    if len(raw) > MAX_BYTES:
        return {"ok": False, "error": "OKF body over %d bytes" % MAX_BYTES, "path": path}

    do = http or _request
    branch = BRANCH_PREFIX + slug[:40].lower()
    try:
        main = do("GET", "/repos/%s/%s/git/ref/heads/main" % (OWNER, REPO), token)
        main_sha = ((main.get("object") or {}).get("sha") or "").strip()
        if not main_sha:
            return {"ok": False, "error": "main tip missing", "path": path}
        try:
            do("POST", "/repos/%s/%s/git/refs" % (OWNER, REPO), token,
               {"ref": "refs/heads/%s" % branch, "sha": main_sha})
        except urllib.error.HTTPError as e:
            if e.code not in (422,):
                raise
        sha = None
        try:
            existing = do("GET", "/repos/%s/%s/contents/%s?ref=%s"
                          % (OWNER, REPO, urllib.parse.quote(path),
                             urllib.parse.quote(branch)), token)
            if isinstance(existing, dict):
                sha = existing.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        put = {
            "message": "docs(okf): Gemini signed RESULT %s" % slug,
            "content": base64.b64encode(raw).decode("ascii"),
            "branch": branch,
        }
        if sha:
            put["sha"] = sha
        written = do("PUT", "/repos/%s/%s/contents/%s"
                     % (OWNER, REPO, urllib.parse.quote(path)), token, put)
        pr_url = ""
        try:
            prs = do("GET", "/repos/%s/%s/pulls?head=%s:%s&state=open"
                     % (OWNER, REPO, OWNER, urllib.parse.quote(branch)), token)
            if isinstance(prs, list) and prs:
                pr_url = prs[0].get("html_url") or ""
            else:
                created = do("POST", "/repos/%s/%s/pulls" % (OWNER, REPO), token, {
                    "title": "docs(okf): Gemini signed RESULT %s" % slug,
                    "head": branch,
                    "base": "main",
                    "body": (
                        "Gemini-signed OKF RESULT landed by `scripts/okf_land.py`.\n\n"
                        "- answers=`%s`\n"
                        "- path=`%s`\n"
                        "- evidence=STATED\n\n"
                        "Do not merge without Codex/Cursor review.\n"
                        % (answers_id, path)
                    ),
                })
                pr_url = (created or {}).get("html_url") or ""
        except urllib.error.HTTPError as e:
            content_url = ((written or {}).get("content") or {}).get("html_url") or ""
            return {"ok": True, "path": path, "branch": branch,
                    "url": content_url, "pr": "", "warning": "PR open failed HTTP %s" % e.code}
        content_url = ((written or {}).get("content") or {}).get("html_url") or ""
        return {"ok": True, "path": path, "branch": branch,
                "url": pr_url or content_url, "pr": pr_url, "file": content_url}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "HTTP %s" % e.code, "path": path}
    except Exception as e:  # noqa: BLE001 - landing must not crash the doorbell
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "path": path}

#!/usr/bin/env python3
"""Inbox wake: a scale-to-zero doorbell. Stdlib HTTP. Default off.

Gemini, 2026-10-10: GitHub webhooks hit one Cloud Run service, which routes a
board or inbox message and wakes the recipient. Claude and Codex are woken by
GitHub Actions workflow_dispatch. Gemini is the existing Cloud Run job
gemini-waker. Grok is an HTTP endpoint (GROK_WAKE_URL). Processed ids live in
Redis (fleet:wake:seen:*) so a second delivery does not wake twice. A 15-minute
sweep is the fail-safe for a webhook that never arrived.

THE FLAG. INBOX_WAKE_ENABLED must be exactly "on". Anything else, including
unset, wakes nobody and does not open Redis. The image also sets the variable
to off. Turning it off is the rollback that does not need a new build.

THIS PROCESS DOES NOT DECIDE THE REPLY. It rings the recipient. The reply still
has to come back through that agent's own path, inside five minutes, with the
laptop closed. The test plan for that is in the pull request, and it is not
run from here.

Deploy is not this file's job. The command is in the pull request. Nothing
here is applied.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT = os.environ.get("RUN_PROJECT") or "sfdc24"
REGION = os.environ.get("RUN_REGION") or "us-central1"
SEEN_TTL = 2592000  # fleet: ttl in scripts/redis_acl.json; no new grant
MAX_BODY = 65536
ID_OK = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
COMMENT_LINE = re.compile(r"(?m)^inbox\s+to=(\S+)\s+id=(\S+)\s+by=(\S+)\s+text=(.*)$")

# Canonical ids are scripts/redis_acl.json principals. Aliases are the ones that
# file already maps onto exactly one of the four routes below.
ALIASES = {
    "vm-claude-code-cli": "claude-code-cli",
    "codex": "chatgpt-codex-desktop",
    "codex-desktop": "chatgpt-codex-desktop",
    "chatgpt-codex": "chatgpt-codex-desktop",
    "aya": "chatgpt-codex-desktop",
    "dot": "chatgpt-codex-desktop",
    "grok-bot": "grok",
}
# kind, target. The target is a workflow file, a Cloud Run job, or an env name.
ROUTES = {
    "claude-code-cli": ("github_actions", "inbox-wake-claude.yml"),
    "chatgpt-codex-desktop": ("github_actions", "inbox-wake-codex.yml"),
    "gemini": ("cloud_run_job", "gemini-waker"),
    "grok": ("http", "GROK_WAKE_URL"),
}


class Unconfigured(Exception):
    """A route has no credential or URL. The id is released so a later sweep can retry."""


def enabled_from_env(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get("INBOX_WAKE_ENABLED") == "on"


def canonical(agent: str):
    name = (agent or "").strip().lower()
    name = ALIASES.get(name, name)
    return name if name in ROUTES else None


class MemorySeen:
    """Process-local stand-in. Production uses RedisSeen; tests use this."""

    def __init__(self):
        self.ids = set()

    def claim(self, key: str) -> bool:
        if key in self.ids:
            return False
        self.ids.add(key)
        return True

    def release(self, key: str) -> None:
        self.ids.discard(key)


class RedisSeen:
    """SET NX on fleet:wake:seen:<agent>:<id>. A lost claim is a delete, not an overwrite."""

    def __init__(self, client):
        self.client = client

    def claim(self, key: str) -> bool:
        return bool(self.client.set("fleet:wake:seen:" + key, "1", nx=True, ex=SEEN_TTL))

    def release(self, key: str) -> None:
        self.client.delete("fleet:wake:seen:" + key)


class Wakers:
    """One dispatch per route. Callables are injected so a test never opens a socket.

    GitHub Actions receives message_id and by only. The message text is not an
    input, because a workflow `run:` step that expands free text is a shell.
    Grok's endpoint gets the text in a JSON body, which is not a shell.
    """

    def __init__(self, github_token="", grok_url="", token_provider=None,
                 github=None, run_job=None, http_post=None):
        self.github_token = github_token or ""
        self.grok_url = grok_url or ""
        self.token_provider = token_provider
        self.github = github
        self.run_job = run_job
        self.http_post = http_post
        self.calls = []

    def dispatch(self, agent: str, route, message: dict) -> None:
        kind, target = route
        self.calls.append({
            "agent": agent, "kind": kind, "target": target,
            "id": message["id"], "by": message.get("by") or "",
        })
        if kind == "github_actions":
            if not self.github_token:
                raise Unconfigured("GITHUB_WAKE_TOKEN unset")
            inputs = {"message_id": message["id"], "by": message.get("by") or ""}
            if self.github:
                self.github(target, inputs)
                return
            github_dispatch(target, inputs, self.github_token)
            return
        if kind == "cloud_run_job":
            if self.run_job:
                self.run_job(target)
                return
            if not self.token_provider:
                raise Unconfigured("no metadata token for the Cloud Run waker")
            start_job(target, self.token_provider())
            return
        if kind == "http":
            if not self.grok_url:
                raise Unconfigured("GROK_WAKE_URL unset")
            body = {
                "id": message["id"], "to": agent,
                "by": message.get("by") or "", "text": message.get("text") or "",
            }
            if self.http_post:
                self.http_post(self.grok_url, body)
                return
            post_json(self.grok_url, body)
            return
        raise Unconfigured("unknown route %s" % kind)


def decide(message: dict, deps) -> dict:
    """One message. Disabled, unknown, duplicate, and unconfigured all wake nobody."""
    if not deps.enabled:
        return {"status": "disabled", "woke": False}
    agent = canonical(message.get("to") or "")
    if not agent:
        return {"status": "unknown_agent", "woke": False}
    mid = str(message.get("id") or "")
    if not ID_OK.fullmatch(mid):
        return {"status": "bad_id", "woke": False}
    by = str(message.get("by") or "")
    if by and not ID_OK.fullmatch(by):
        by = ""
    if deps.seen is None:
        return {"status": "no_store", "woke": False, "to": agent, "id": mid}
    key = "%s:%s" % (agent, mid)
    if not deps.seen.claim(key):
        return {"status": "duplicate", "woke": False, "to": agent, "id": mid}
    try:
        deps.wakers.dispatch(agent, ROUTES[agent], {
            "id": mid, "to": agent, "by": by, "text": message.get("text") or "",
        })
    except Unconfigured as exc:
        deps.seen.release(key)
        return {"status": "unconfigured", "woke": False, "to": agent, "id": mid,
                "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a failed ring must be retryable
        deps.seen.release(key)
        return {"status": "error", "woke": False, "to": agent, "id": mid,
                "error": type(exc).__name__}
    kind, target = ROUTES[agent]
    return {"status": "woke", "woke": True, "to": agent, "id": mid,
            "route": kind, "target": target}


def sweep(deps) -> list:
    """Read each inbox and wake what the webhook missed. Flag off reads nothing."""
    if not deps.enabled:
        return []
    if deps.reader is None or deps.seen is None:
        return [{"status": "no_store", "woke": False}]
    out = []
    for agent in ROUTES:
        try:
            entries = deps.reader(agent) or []
        except Exception as exc:  # noqa: BLE001 - one inbox must not sink the others
            out.append({"status": "read_error", "woke": False, "to": agent,
                        "error": type(exc).__name__})
            continue
        for entry in entries:
            item = dict(entry)
            item["to"] = agent
            out.append(decide(item, deps))
    return out


def comment_messages(text: str) -> list:
    return [{"to": m.group(1), "id": m.group(2), "by": m.group(3), "text": m.group(4).strip()}
            for m in COMMENT_LINE.finditer(text or "")]


def parse_event(event: str, body: bytes) -> list:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if event == "issue_comment":
        if data.get("action") not in ("created", "edited"):
            return []
        return comment_messages((data.get("comment") or {}).get("body") or "")
    if event == "repository_dispatch":
        payload = data.get("client_payload") or {}
        if payload.get("id") and payload.get("to"):
            return [payload]
        return []
    if isinstance(data, dict) and data.get("id") and data.get("to"):
        return [data]
    return []


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    """GitHub X-Hub-Signature-256. An empty secret is a refusal, not a skip."""
    if not secret:
        return False
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, header or "")


def bearer_ok(secret: str, header: str) -> bool:
    if not secret:
        return False
    return hmac.compare_digest("Bearer " + secret, header or "")


def handle(method: str, path: str, headers: dict, body: bytes, deps) -> tuple:
    path = path.split("?", 1)[0]
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if method == "GET" and path == "/healthz":
        return 200, {"ok": True, "enabled": bool(deps.enabled)}
    if method == "POST" and path == "/wake":
        if not deps.enabled:
            return 200, {"enabled": False, "woke": []}
        if not signature_ok(deps.webhook_secret, body, headers.get("x-hub-signature-256", "")):
            return 401, {"error": "bad signature"}
        messages = parse_event(headers.get("x-github-event", ""), body)
        return 200, {"enabled": True, "woke": [decide(m, deps) for m in messages]}
    if method == "POST" and path == "/sweep":
        if not deps.enabled:
            return 200, {"enabled": False, "woke": []}
        if not bearer_ok(deps.sweep_token, headers.get("authorization", "")):
            return 401, {"error": "bad sweep token"}
        return 200, {"enabled": True, "woke": sweep(deps)}
    return 404, {"error": "not found"}


def github_dispatch(workflow: str, inputs: dict, token: str) -> None:
    url = ("https://api.github.com/repos/sfdc-24/Blackboard/actions/workflows/%s/dispatches"
           % workflow)
    raw = json.dumps({"ref": "main", "inputs": inputs}).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "sfdc24-inbox-wake",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def start_job(job: str, token: str) -> None:
    """The same v2 :run call cloud/board-watcher/main.py uses for gemini-waker."""
    url = ("https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s:run"
           % (PROJECT, REGION, job))
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def post_json(url: str, body: dict) -> None:
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST", headers={
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def read_inbox(client, agent: str) -> list:
    """fleet:inbox:<agent> entries, the shape redis_gov's xadd writes."""
    rows = client.xrange("fleet:inbox:" + agent, count=50)
    out = []
    for entry_id, fields in rows or ():
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode("utf-8")

        def field(name, fields=fields):
            value = fields.get(name)
            if value is None:
                value = fields.get(name.encode("utf-8"))
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            return value or ""

        out.append({"id": str(entry_id), "text": field("text"), "by": field("by"),
                    "at": field("at")})
    return out


class Deps:
    def __init__(self, enabled=False, seen=None, wakers=None, reader=None,
                 webhook_secret="", sweep_token=""):
        self.enabled = enabled
        self.seen = seen
        self.wakers = wakers or Wakers()
        self.reader = reader
        self.webhook_secret = webhook_secret or ""
        self.sweep_token = sweep_token or ""


def deps_from_env() -> Deps:
    """Build the runtime deps. Redis is opened only when the flag is on."""
    on = enabled_from_env()
    deps = Deps(
        enabled=on,
        webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET") or "",
        sweep_token=os.environ.get("INBOX_WAKE_SWEEP_TOKEN") or "",
        wakers=Wakers(
            github_token=os.environ.get("GITHUB_WAKE_TOKEN") or "",
            grok_url=os.environ.get("GROK_WAKE_URL") or "",
            token_provider=_metadata_token if on else None,
        ),
    )
    if not on:
        return deps
    client = _redis_client()
    if client is not None:
        deps.seen = RedisSeen(client)
        deps.reader = lambda agent, client=client: read_inbox(client, agent)
    return deps


def _metadata_token() -> str:
    import state_store  # noqa: PLC0415 - present on the Cloud Run image path, not required for tests
    return state_store.metadata_token()[0]


def _redis_client():
    """The fleet's client, or None. A missing client refuses wakes; it does not fall open."""
    try:
        import redis_dual  # noqa: PLC0415
    except ImportError:
        return None
    return redis_dual.client(redis_dual.Settings(), precheck=False)


class Handler(BaseHTTPRequestHandler):
    deps = Deps()

    def do_GET(self):
        self._reply(*handle("GET", self.path, dict(self.headers), b"", self.deps))

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            self._reply(413, {"error": "body too large"})
            return
        body = self.rfile.read(n) if n else b""
        self._reply(*handle("POST", self.path, dict(self.headers), body, self.deps))

    def _reply(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return  # a request line can carry the message text; do not write it


def serve() -> None:
    Handler.deps = deps_from_env()
    port = int(os.environ.get("PORT") or "8080")
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()

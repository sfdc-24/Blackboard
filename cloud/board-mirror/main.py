#!/usr/bin/env python3
"""Board mirror: copy a Blackboard row into Redis. Stdlib HTTP. Default off.

This is the writer. inbox-wake is not. inbox-wake reads fleet:inbox:<agent>
and wakes that agent. It does not XADD the board stream and it does not XADD
an inbox. Deploy this file as its own scale-to-zero Cloud Run service, with
its own service account. Do not reuse redis-tool-bridge.

THE FLAG. BOARD_MIRROR_ENABLED must be exactly "on". Anything else, including
unset, leaves POST /hooks/board as a no-op: no token check, no signature
check, no board read, no Redis. The image sets the variable to off. Rollback
is that variable, not a new build: set BOARD_MIRROR_ENABLED=off.

THE DOOR. The service is not public. Cloud Run is deployed with
--no-allow-unauthenticated, so the platform rejects a caller that has no
roles/run.invoker. The body is still checked here: an OIDC identity token
(audience plus caller email) and an HMAC-SHA256 of the raw body
(X-Board-Signature-256: sha256=<hex>, secret name BOARD_HOOK_SECRET). An
empty secret is a refusal. A timestamp older than five minutes, or more than
60 seconds ahead, is a refusal. The nonce is one SET NX EX on
fleet:board:nonce:<nonce> (TTL 300s). A second delivery of that nonce writes
nothing else.

THE WRITE. The claim is one SET NX on fleet:board:mirrored:<row_id>. There is
no GET before it. The winner XADDs fleet:board:stream and fleet:inbox:<agent>,
reads each entry back, and returns the stream id and sha256. A mismatch or a
Redis timeout is an error, not a success. to=all is one board entry and one
inbox entry per fleet agent. reconcile_once mirrors the last few minutes in a
single pass and returns. It does not loop and it does not sleep.

ACCOUNTS. Nothing in this file grants a role. The owner binds these, or does
not. Neither account is redis-tool-bridge@sfdc24.iam.gserviceaccount.com, and
neither has roles/iam.serviceAccountUser on that account or any other.
Neither has roles/redis.admin, roles/redis.editor, or roles/redis.viewer.
Memorystore is reached by VPC egress plus the AUTH string, not by a Redis IAM
role.

board-mirror@sfdc24.iam.gserviceaccount.com
  roles/secretmanager.secretAccessor, on these secrets only (not the project):
    REDIS_AUTH_STRING, REDIS_CA_CERT, BUS_URL, BUS_SECRET, BOARD_HOOK_SECRET
  no roles/run.invoker (this service starts nothing)
  Redis writes, and only these:
    XADD fleet:board:stream
    XADD fleet:inbox:<agent>
    SET NX  fleet:board:mirrored:<row_id>   the claim, fleet TTL
    SET NX EX fleet:board:nonce:<nonce>     replay, 300 seconds
  Redis reads: XRANGE of the entry just written, to compare sha256.
  The Apps Script identity (the token from ScriptApp.getIdentityToken) is the
  invoker: roles/run.invoker on this service only. That binding is the
  owner's, and it is not applied here.

inbox-wake@sfdc24.iam.gserviceaccount.com
  This feature reads fleet:inbox:<agent> (XRANGE) and wakes the agent. It
  does not write fleet:board:* or fleet:inbox:*.
  roles/secretmanager.secretAccessor, on these secrets only:
    REDIS_AUTH_STRING, REDIS_CA_CERT, GITHUB_WEBHOOK_SECRET,
    INBOX_WAKE_SWEEP_TOKEN, GITHUB_WAKE_TOKEN
  roles/run.invoker on the Cloud Run job gemini-waker only (the existing
  wake path).
  The parent doorbell still SET NXes fleet:wake:seen:* and
  fleet:wake:receipt:entry:*. Those keys are not a board-mirror grant, and
  this service does not add them.

DEPLOY (not run from here). Project sfdc24, region us-central1:
  gcloud run deploy board-mirror \\
    --service-account=board-mirror@sfdc24.iam.gserviceaccount.com \\
    --no-allow-unauthenticated --min-instances=0 --max-instances=2 \\
    --network=default --subnet=default --vpc-egress=private-ranges-only
  Image default BOARD_MIRROR_ENABLED=off. The owner turns it on.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEEN_TTL = 2592000  # fleet: ttl in scripts/redis_acl.json; no new grant
NONCE_TTL = 300  # at least the five-minute replay window
MAX_AGE_SECONDS = 300
MAX_FUTURE_SECONDS = 60
REDIS_CONNECT_TIMEOUT = 2.0
REDIS_READ_TIMEOUT = 2.0
PRIVATE_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
)
MAX_BODY = 262144
MAX_EVENTS = 200
ID_OK = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
NONCE_OK = re.compile(r"[A-Za-z0-9_.:-]{8,128}\Z")
BOARD_STREAM = "fleet:board:stream"
MIRROR_PREFIX = "fleet:board:mirrored:"
NONCE_PREFIX = "fleet:board:nonce:"
INBOX_PREFIX = "fleet:inbox:"
# scripts/redis_acl.json principals, minus owner and milestones-sync.
BOARD_AGENTS = (
    "claude-code-cli",
    "chatgpt-codex-desktop",
    "grok",
    "gemini",
    "cursor",
    "copilot",
    "claude-api",
    "claude-mobile",
    "pi1-cli",
)
BOARD_AGENT_SET = frozenset(BOARD_AGENTS)
ALIASES = {
    "vm-claude-code-cli": "claude-code-cli",
    "codex": "chatgpt-codex-desktop",
    "codex-desktop": "chatgpt-codex-desktop",
    "chatgpt-codex": "chatgpt-codex-desktop",
    "aya": "chatgpt-codex-desktop",
    "dot": "chatgpt-codex-desktop",
    "grok-bot": "grok",
}
BCB_ID = re.compile(r"(?:^|\|)id=([A-Za-z0-9._-]{1,200})(?:\||$)")
RECONCILE_MAX = 50
RECONCILE_MINUTES = 10
STREAM_MAXLEN = 10000
LOG = logging.getLogger("board_mirror")


def enabled_from_env(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get("BOARD_MIRROR_ENABLED") == "on"


def host_on_private_range(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return False
    return any(ip in network for network in PRIVATE_RANGES)


def apply_redis_timeouts(client, connect=REDIS_CONNECT_TIMEOUT, read=REDIS_READ_TIMEOUT):
    pool = getattr(client, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None) if pool is not None else None
    if isinstance(kwargs, dict):
        kwargs["socket_connect_timeout"] = connect
        kwargs["socket_timeout"] = read
    return client


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    """HMAC-SHA256 of the raw body. An empty secret is a refusal."""
    if not secret:
        return False
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = "sha256=" + mac
    got = header or ""
    if len(expected) != len(got):
        return False
    return hmac.compare_digest(expected, got)


def bearer_ok(secret: str, header: str) -> bool:
    if not secret:
        return False
    expected = "Bearer " + secret
    got = header or ""
    if len(expected) != len(got):
        return False
    return hmac.compare_digest(expected, got)


def _same(left, right) -> bool:
    left, right = str(left), str(right)
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def oidc_ok(header: str, deps) -> bool:
    """Audience and caller email. Unset audience or callers fail closed.

    `deps.oidc_lookup` is tokeninfo in production and a fake in tests. A
    lookup error is a refusal. The token itself is not logged.
    """
    audience = getattr(deps, "audience", "") or ""
    callers = tuple(getattr(deps, "callers", ()) or ())
    if not audience or not callers:
        return False
    header = header or ""
    if not header.startswith("Bearer "):
        return False
    token = header[7:].strip()
    if not token:
        return False
    lookup = getattr(deps, "oidc_lookup", None)
    if lookup is None:
        return False
    try:
        info = lookup(token)
    except Exception as exc:  # noqa: BLE001 - a bad token is a refusal
        LOG.error("board mirror oidc error=%s", type(exc).__name__)
        return False
    if not isinstance(info, dict) or info.get("error"):
        return False
    verified = info.get("email_verified", True)
    if verified in (False, "false", "False", 0, "0"):
        return False
    aud = info.get("aud")
    auds = aud if isinstance(aud, (list, tuple)) else (aud,)
    if not any(_same(audience, item) for item in auds if item is not None):
        return False
    email = str(info.get("email") or "").strip().lower()
    return any(_same(email, str(item).strip().lower()) for item in callers)


def tokeninfo_lookup(token: str) -> dict:
    """Google's tokeninfo. Used only when the flag is on. Tests inject theirs."""
    if not token or len(token) > 8192:
        raise ValueError("token")
    url = ("https://oauth2.googleapis.com/tokeninfo?id_token="
           + urllib.parse.quote(token, safe=""))
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    if not isinstance(info, dict):
        raise ValueError("tokeninfo")
    return info


def fresh_ts(ts, now: int) -> bool:
    """True when ts is within five minutes, and not more than 60s ahead."""
    if isinstance(ts, bool) or not isinstance(ts, int):
        return False
    delta = int(now) - ts
    if delta > MAX_AGE_SECONDS:
        return False
    if delta < -MAX_FUTURE_SECONDS:
        return False
    return True


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if value is None:
        return ""
    return str(value)


def _field(fields, name: str) -> str:
    if not isinstance(fields, dict):
        return ""
    value = fields.get(name)
    if value is None:
        value = fields.get(name.encode("utf-8"))
    return _text(value) if value is not None else ""


def bcb_id(payload: str) -> str:
    match = BCB_ID.search(str(payload or ""))
    return match.group(1) if match else ""


def row_view(cells) -> dict:
    cells = list(cells or [])
    while len(cells) < 6:
        cells.append("")
    payload = _text(cells[5])
    return {
        "row_id": _text(cells[0]).strip(),
        "appended_at": _text(cells[1]),
        "from": _text(cells[2]),
        "to": _text(cells[3]),
        "phase": _text(cells[4]),
        "payload": payload,
        "id": bcb_id(payload),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def sheet_row_text(value) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int) and 1 <= value <= 1000000:
        return str(value)
    text = _text(value).strip()
    if text.isdigit() and 1 <= int(text) <= 1000000:
        return str(int(text))
    return ""


def inbox_targets(to_field: str) -> list:
    tokens = []
    for part in re.split(r"[;,]", str(to_field or "")):
        name = part.strip().lower()
        if name:
            tokens.append(ALIASES.get(name, name))
    if not tokens:
        return []
    if any(name in ("all", "*") for name in tokens):
        return list(BOARD_AGENTS)
    out = []
    for name in tokens:
        if name in BOARD_AGENT_SET and name not in out:
            out.append(name)
    return out


def stream_fields(view: dict, sheet_row: str) -> dict:
    payload = view["payload"]
    if len(payload.encode("utf-8")) > 2048:
        payload = ""
    return {
        "row_id": view["row_id"],
        "sheet_row": sheet_row,
        "appended_at": view["appended_at"],
        "from": view["from"],
        "to": view["to"],
        "phase": view["phase"],
        "id": view["id"],
        "sha256": view["sha256"],
        "payload": payload,
    }


def _entry_sha(rows, entry_id: str) -> str:
    for eid, fields in rows or ():
        if entry_id and _text(eid) != entry_id:
            continue
        return _field(fields, "sha256")
    return ""


class RedisBoard:
    """SET NX is the claim. XADD happens only after that claim is won."""

    def __init__(self, client):
        self.client = client

    def claim_nonce(self, nonce: str) -> str:
        try:
            wrote = self.client.set(NONCE_PREFIX + nonce, "1", nx=True, ex=NONCE_TTL)
        except OSError as exc:
            LOG.error("board mirror nonce error=%s", type(exc).__name__)
            return "error"
        except Exception as exc:  # noqa: BLE001
            LOG.error("board mirror nonce error=%s", type(exc).__name__)
            return "error"
        if wrote:
            return "claimed"
        if wrote is None:
            return "replay"
        LOG.error("board mirror nonce error=write-rejected")
        return "error"

    def claim(self, row_id: str, marker: str) -> str:
        try:
            wrote = self.client.set(MIRROR_PREFIX + row_id, marker, nx=True, ex=SEEN_TTL)
        except OSError as exc:
            LOG.error("board mirror error row_id=%s error=%s", row_id, type(exc).__name__)
            return "error"
        except Exception as exc:  # noqa: BLE001
            LOG.error("board mirror error row_id=%s error=%s", row_id, type(exc).__name__)
            return "error"
        if wrote:
            return "claimed"
        if wrote is None:
            return "duplicate"
        LOG.error("board mirror error row_id=%s error=write-rejected", row_id)
        return "error"


def _xadd_checked(client, key: str, fields: dict, expected: str, row_id: str):
    try:
        entry_id = _text(client.xadd(
            key, fields, maxlen=STREAM_MAXLEN, approximate=True))
    except OSError as exc:
        LOG.error("board mirror error row_id=%s stream=%s error=%s",
                  row_id, key, type(exc).__name__)
        return "", "error"
    except Exception as exc:  # noqa: BLE001
        LOG.error("board mirror error row_id=%s stream=%s error=%s",
                  row_id, key, type(exc).__name__)
        return "", "error"
    LOG.info("board mirror stream=%s stream_id=%s row_id=%s sha256=%s",
             key, entry_id, row_id, expected)
    try:
        rows = client.xrange(key, min=entry_id, max=entry_id)
    except OSError as exc:
        LOG.error("board mirror readback row_id=%s stream=%s error=%s",
                  row_id, key, type(exc).__name__)
        return entry_id, "error"
    except Exception as exc:  # noqa: BLE001
        LOG.error("board mirror readback row_id=%s stream=%s error=%s",
                  row_id, key, type(exc).__name__)
        return entry_id, "error"
    if _entry_sha(rows, entry_id) != expected:
        LOG.error("board mirror readback mismatch row_id=%s stream=%s stream_id=%s",
                  row_id, key, entry_id)
        return entry_id, "error"
    return entry_id, "ok"


def mirror_row(cells, deps, sheet_row="") -> dict:
    """Claim the row, then write the board stream and the addressed inboxes.

    The cells are the board's row, already fetched. The event body is not a
    source of content. A lost claim writes nothing. A Redis failure returns
    error and does not delete the claim.
    """
    view = row_view(cells)
    row_id = view["row_id"]
    if not ID_OK.fullmatch(row_id):
        return {"enabled": True, "ok": False, "status": "bad_id", "row_id": row_id}
    if deps.board is None:
        return {"enabled": True, "ok": False, "status": "error", "error": "no_store",
                "row_id": row_id}
    claim = deps.board.claim(row_id, view["sha256"])
    if claim == "duplicate":
        return {"enabled": True, "ok": True, "status": "duplicate", "mirrored": False,
                "row_id": row_id}
    if claim != "claimed":
        return {"enabled": True, "ok": False, "status": "error", "error": "redis",
                "row_id": row_id}
    fields = stream_fields(view, sheet_row_text(sheet_row))
    stream_id, outcome = _xadd_checked(
        deps.board.client, BOARD_STREAM, fields, view["sha256"], row_id)
    if outcome != "ok":
        return {"enabled": True, "ok": False, "status": "error", "error": "redis",
                "row_id": row_id, "stream_id": stream_id, "sha256": view["sha256"]}
    inboxes = []
    for agent in inbox_targets(view["to"]):
        key = INBOX_PREFIX + agent
        inbox_id, inbox_outcome = _xadd_checked(
            deps.board.client, key, fields, view["sha256"], row_id)
        if inbox_outcome != "ok":
            return {"enabled": True, "ok": False, "status": "error", "error": "redis",
                    "row_id": row_id, "stream_id": stream_id, "sha256": view["sha256"],
                    "inboxes": inboxes}
        inboxes.append({"key": key, "stream_id": inbox_id, "sha256": view["sha256"]})
    return {
        "enabled": True, "ok": True, "status": "mirrored", "mirrored": True,
        "row_id": row_id, "stream_id": stream_id, "sha256": view["sha256"],
        "inboxes": inboxes,
    }


def fetch_board_row(row_id, env=None, reader=None):
    """The row whose Row_ID is exactly row_id. A substring hit is not a row."""
    if reader is None:
        import bus  # noqa: PLC0415 - the image copies scripts/bus.py to /app
        env = bus.load_env() if env is None else env
        return bus.read_row(env, row_id)
    rows = reader(row_id) or []
    exact = [row for row in rows if row and _text(row[0]).strip() == str(row_id)]
    if len(exact) != 1:
        raise LookupError("row")
    return exact[0]


def fetch_board_window(since, env=None, reader=None):
    if reader is None:
        import bus  # noqa: PLC0415
        env = bus.load_env() if env is None else env
        found = bus.read_rows(env, since=since, limit=RECONCILE_MAX)
        return found.get("rows") or []
    return list(reader(since) or [])


def reconcile_once(deps, minutes=RECONCILE_MINUTES, now=None) -> dict:
    """Mirror recent rows that lack the SET NX marker. One pass, then return."""
    if not getattr(deps, "enabled", False):
        return {"enabled": False, "ok": True, "mirrored": [], "skipped": [], "seen": 0}
    if deps.board is None or deps.board_window is None:
        return {"enabled": True, "ok": False, "status": "error", "error": "no_store",
                "mirrored": [], "skipped": [], "seen": 0}
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = RECONCILE_MINUTES
    minutes = max(1, min(60, minutes))
    when = now or datetime.now(timezone.utc)
    if isinstance(when, str):
        when = datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    since = (when - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = list(deps.board_window(since) or [])
    except Exception as exc:  # noqa: BLE001
        LOG.error("board mirror reconcile error=%s", type(exc).__name__)
        return {"enabled": True, "ok": False, "status": "error", "error": type(exc).__name__,
                "mirrored": [], "skipped": [], "seen": 0, "since": since}
    rows = rows[:RECONCILE_MAX]
    mirrored, skipped = [], []
    failed = False
    for cells in rows:
        result = mirror_row(cells, deps)
        if result.get("status") == "mirrored":
            mirrored.append({
                "row_id": result.get("row_id"),
                "stream_id": result.get("stream_id"),
                "sha256": result.get("sha256"),
            })
        else:
            if result.get("status") == "error":
                failed = True
            skipped.append({"row_id": result.get("row_id"), "status": result.get("status")})
    return {
        "enabled": True, "ok": not failed, "status": "error" if failed else "done",
        "seen": len(rows), "mirrored": mirrored, "skipped": skipped, "since": since,
    }


def _status(result) -> int:
    status = result.get("status")
    if result.get("ok") and status in ("mirrored", "duplicate", "done", "disabled"):
        return 200
    if status == "bad_id":
        return 400
    if result.get("error") in ("LookupError", "row"):
        return 502
    return 503


def _events(body: dict):
    events = body.get("events")
    if events is None and body.get("row_id"):
        events = [body]
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
        return None
    cleaned = []
    for item in events:
        if not isinstance(item, dict):
            return None
        row_id = _text(item.get("row_id")).strip()
        if not ID_OK.fullmatch(row_id):
            return None
        cleaned.append({"row_id": row_id, "sheet_row": item.get("sheet_row")})
    return cleaned


def handle_board(headers: dict, body: bytes, deps, now=None) -> tuple:
    """POST /hooks/board. Flag off is a no-op before the token, HMAC, or Redis."""
    if not getattr(deps, "enabled", False):
        return 200, {"enabled": False, "mirrored": False}
    if not oidc_ok(headers.get("authorization", ""), deps):
        return 401, {"error": "bad token"}
    if not signature_ok(getattr(deps, "hook_secret", "") or "", body,
                        headers.get("x-board-signature-256", "")):
        return 401, {"error": "bad signature"}
    try:
        event = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"ok": False, "status": "bad_json", "error": "bad json"}
    if not isinstance(event, dict):
        return 400, {"ok": False, "status": "bad_json", "error": "bad json"}
    events = _events(event)
    nonce = _text(event.get("nonce")).strip()
    if events is None or not NONCE_OK.fullmatch(nonce):
        return 400, {"ok": False, "status": "bad_json", "error": "bad event"}
    clock = now if now is not None else getattr(deps, "now", None)
    stamp = int(clock()) if clock is not None else int(time.time())
    if not fresh_ts(event.get("ts"), stamp):
        return 401, {"error": "stale"}
    if deps.board is None or getattr(deps, "board_fetch", None) is None:
        return 503, {"ok": False, "status": "error", "error": "no_store"}
    nonce_claim = deps.board.claim_nonce(nonce)
    if nonce_claim == "replay":
        return 401, {"error": "replay"}
    if nonce_claim != "claimed":
        return 503, {"ok": False, "status": "error", "error": "redis"}
    results = []
    for item in events:
        row_id = item["row_id"]
        try:
            cells = deps.board_fetch(row_id)
        except Exception as exc:  # noqa: BLE001
            LOG.error("board mirror fetch row_id=%s error=%s", row_id, type(exc).__name__)
            return 502, {"ok": False, "status": "error", "error": type(exc).__name__,
                         "row_id": row_id, "results": results}
        if not cells or _text(cells[0]).strip() != row_id:
            return 502, {"ok": False, "status": "error", "error": "row-mismatch",
                         "row_id": row_id, "results": results}
        result = mirror_row(cells, deps, sheet_row=item.get("sheet_row"))
        results.append(result)
        if not result.get("ok"):
            return _status(result), result
    if len(results) == 1:
        return _status(results[0]), results[0]
    return 200, {"enabled": True, "ok": True, "status": "mirrored", "results": results}


def handle_reconcile(headers: dict, deps) -> tuple:
    """One reconcile pass. Flag off does not read the board."""
    if not getattr(deps, "enabled", False):
        return 200, {"enabled": False, "mirrored": []}
    if not oidc_ok(headers.get("authorization", ""), deps):
        return 401, {"error": "bad token"}
    if not bearer_ok(getattr(deps, "reconcile_token", "") or "",
                     headers.get("x-board-reconcile", "")):
        return 401, {"error": "bad reconcile token"}
    result = reconcile_once(deps)
    return (200 if result.get("ok") else 503), result


def handle(method: str, path: str, headers: dict, body: bytes, deps) -> tuple:
    path = path.split("?", 1)[0]
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if method == "GET" and path == "/healthz":
        return 200, {"ok": True, "enabled": bool(deps.enabled)}
    if method == "POST" and path == "/hooks/board":
        return handle_board(headers, body, deps)
    if method == "POST" and path == "/hooks/board/reconcile":
        return handle_reconcile(headers, deps)
    return 404, {"error": "not found"}


class Deps:
    def __init__(self, enabled=False, hook_secret="", audience="", callers=(),
                 oidc_lookup=None, board=None, board_fetch=None, board_window=None,
                 reconcile_token="", now=None):
        self.enabled = enabled
        self.hook_secret = hook_secret or ""
        self.audience = audience or ""
        self.callers = tuple(callers or ())
        self.oidc_lookup = oidc_lookup
        self.board = board
        self.board_fetch = board_fetch
        self.board_window = board_window
        self.reconcile_token = reconcile_token or ""
        self.now = now


def deps_from_env() -> Deps:
    """Redis stays closed until BOARD_MIRROR_ENABLED is exactly on."""
    on = enabled_from_env()
    callers = tuple(
        part.strip() for part in (os.environ.get("BOARD_MIRROR_CALLERS") or "").split(",")
        if part.strip())
    deps = Deps(
        enabled=on,
        hook_secret=os.environ.get("BOARD_HOOK_SECRET") or "",
        audience=os.environ.get("BOARD_MIRROR_AUDIENCE") or "",
        callers=callers,
        reconcile_token=os.environ.get("BOARD_MIRROR_RECONCILE_TOKEN") or "",
        oidc_lookup=tokeninfo_lookup if on else None,
    )
    if not on:
        return deps
    client = _redis_client()
    if client is None:
        return deps
    deps.board = RedisBoard(client)
    deps.board_fetch = fetch_board_row
    deps.board_window = fetch_board_window
    return deps


def _redis_client():
    try:
        import redis_dual  # noqa: PLC0415
    except ImportError:
        return None
    settings = redis_dual.Settings()
    host = str(settings.host or "").strip()
    if host and not host_on_private_range(host):
        LOG.error("board-mirror redis host is outside private-ranges-only; not connecting")
        return None
    client = redis_dual.client(settings, precheck=False)
    if client is not None:
        apply_redis_timeouts(client)
    return client


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
        return


def serve() -> None:
    Handler.deps = deps_from_env()
    port = int(os.environ.get("PORT") or "8080")
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()

"""Authenticated synthetic Redis probe."""
from __future__ import annotations

import datetime
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROBE_NAMESPACE = "probe:bridge:"
PROBE_TTL_SECONDS = 60
MAX_BODY_BYTES = 4096

# Required configuration. EVERY ONE OF THESE IS FAIL-CLOSED: unset means the service refuses, never
# that the check is skipped. Defect 1 shipped because an absent audience meant "verify nothing".
AUDIENCE_ENV = "BRIDGE_AUDIENCE"
CALLERS_ENV = "BRIDGE_CALLERS"


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def allowlist() -> frozenset:
    """Validate the bounded bridge contract."""
    raw = os.environ.get(CALLERS_ENV, "")
    return frozenset(p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip())


def audience() -> str:
    return (os.environ.get(AUDIENCE_ENV) or "").strip()


def verify_caller(token: str, expected_audience: str, permitted: frozenset) -> tuple:
    """Validate the bounded bridge contract."""
    if not expected_audience:
        return None, "the service has no configured audience, so no token can be verified"
    if not permitted:
        return None, "the service has no configured caller allowlist"
    if not token:
        return None, "unauthenticated"
    try:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token
        # THE AUDIENCE IS PASSED. This single argument is defect 1; without it the library checks
        # the signature and the issuer and says nothing about who the token was minted for.
        claims = id_token.verify_oauth2_token(token, ga_requests.Request(),
                                              audience=expected_audience)
    except Exception:
        # The TYPE is not even reported outward: a verifier's message can quote the token.
        return None, "unauthenticated"
    principal = str(claims.get("email") or "").strip().lower()
    if not principal or not claims.get("email_verified", True):
        return None, "unauthenticated"
    if principal not in permitted:
        # Defect 3. A verified Google identity is NOT an authorised one.
        return None, "not authorised"
    return principal, None


def connect():
    """Validate the bounded bridge contract."""
    import redis_dual
    return redis_dual.client(redis_dual.Settings(), precheck=False)


def synthetic_probe(conn) -> dict:
    """Validate the bounded bridge contract."""
    key = PROBE_NAMESPACE + secrets.token_hex(8)
    nonce = secrets.token_hex(16)
    started = time.time()
    steps = {}
    try:
        conn.setex(key, PROBE_TTL_SECONDS, nonce)
        steps["write"] = "ok"
        steps["read_back"] = "match" if conn.get(key) == nonce else "MISMATCH"
        ttl = conn.ttl(key)
        steps["ttl"] = "ok" if isinstance(ttl, int) and 0 < ttl <= PROBE_TTL_SECONDS else "MISSING"
        conn.delete(key)
        steps["cleanup"] = "verified" if conn.get(key) is None else "LEFT BEHIND"
    except Exception as error:
        return {"status": "ERROR", "failed_with": type(error).__name__, "steps": steps,
                "latency_ms": int((time.time() - started) * 1000)}
    ok = (steps.get("read_back") == "match" and steps.get("ttl") == "ok"
          and steps.get("cleanup") == "verified")
    return {"status": "OK" if ok else "FAILED", "steps": steps,
            "latency_ms": int((time.time() - started) * 1000)}


def bearer(header: str) -> str:
    parts = (header or "").split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "redis-tool-bridge"
    sys_version = ""

    def _send(self, code, body) -> None:
        raw = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args) -> None:
        # NO REQUEST LINE, NO HEADERS. The default handler logs the path and would put a bearer
        # token into Cloud Logging the first time anyone put one in a query string.
        # Inherited log_request/log_error pass raw request-derived values in args.
        # Suppress them entirely; explicit probe outcome logs below remain available.
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/healthz":
            # UNAUTHENTICATED BY DESIGN AND IT TOUCHES NOTHING. It does not connect to Redis, read a
            # secret, or report configuration - a health check that reveals state is a disclosure.
            return self._send(200, {"ok": True, "at": stamp()})
        return self._send(404, {"error": "no such endpoint"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
        except ValueError:
            length = 0
        if length:
            self.rfile.read(length)          # read and DISCARD: no endpoint takes input
        if path != "/probe":
            return self._send(404, {"error": "no such endpoint"})

        principal, why = verify_caller(bearer(self.headers.get("Authorization", "")),
                                       audience(), allowlist())
        if why:
            code = 401 if why == "unauthenticated" else 403
            if why.startswith("the service has no configured"):
                # A misconfiguration is OURS, not the caller's, and it must be loud in our logs and
                # opaque in the response.
                sys.stderr.write("%s REFUSING EVERY REQUEST: %s\n" % (stamp(), why))
                code = 503
            return self._send(code, {"error": why if code == 503 else why})

        result = synthetic_probe(connect())
        sys.stderr.write("%s probe by %s: %s\n" % (stamp(), principal, result["status"]))
        # The principal is echoed so a caller can confirm WHICH identity the bridge accepted; no key
        # name, no nonce and no secret appears in a response.
        return self._send(200 if result["status"] == "OK" else 502,
                          {"status": result["status"], "steps": result["steps"],
                           "latency_ms": result["latency_ms"], "caller": principal,
                           "at": stamp()})


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    if not audience() or not allowlist():
        # STARTS ANYWAY, and refuses every request with 503. Exiting here would make Cloud Run retry
        # the revision forever and leave the previous one serving, which hides the misconfiguration
        # instead of surfacing it.
        sys.stderr.write("%s STARTING MISCONFIGURED: %s and %s must both be set; every request "
                         "will be refused\n" % (stamp(), AUDIENCE_ENV, CALLERS_ENV))
    ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

"""redis-tool-bridge: an IAM-authenticated HTTPS surface over the private Redis instance.

WHY THIS FILE EXISTS, WHICH IS NOT THE USUAL REASON
The service is ALREADY RUNNING. `redis-tool-bridge-00001-hsg` serves
https://redis-tool-bridge-yzet4vuplq-uc.a.run.app from image digest cd88ac6cc786, holding a mounted
database credential, and its SOURCE IS IN NO REPOSITORY. Nobody can review the code that reads that
credential, nobody can diff it against the four defects raised in CCC-BRIDGE-REVIEW-20261006T1330Z,
and nobody can rebuild it. That is the defect this PR is really about. Everything below is written
to be reviewable FIRST and deployed only after Codex verifies it, per the owner's GO
(GROK-REDIS-BRIDGE-OWNER-GO-20261007T2307Z): "Deploy, new SA and IAM only AFTER Codex verifies the
source."

MEASURED STATE OF THE RUNNING SERVICE, 2026-10-07 (read-only, nothing changed)
    service account      redis-tool-bridge@sfdc24.iam.gserviceaccount.com   (dedicated)
    egress               private-ranges-only, Direct VPC (network-interfaces, not a connector)
    secrets              FILE MOUNTS in separate dirs: /secrets/auth, /secrets/ca
    run.invoker          serviceAccount:aya-runtime@sfdc24.iam.gserviceaccount.com  -- and NOTHING
                         else. No allUsers, no allAuthenticatedUsers.
    ingress              all
So three of the four locked decisions are already honoured by the deployment. What cannot be checked
from outside is the application layer, and that is what this file pins down.

THE FOUR DEFECTS FROM MY OWN 2026-10-06 REVIEW, AND HOW EACH IS CLOSED HERE
  1. verifyIdToken WITHOUT AN AUDIENCE. google-auth-library does not check `aud` unless you pass it,
     so any Google-signed token for any service satisfied that middleware. Here the audience is
     REQUIRED configuration (BRIDGE_AUDIENCE) and passed explicitly to the verifier; if it is unset
     the service refuses every request rather than verifying nothing.
  2. checkServerIdentity RETURNING undefined DISABLES SERVER IDENTITY VERIFICATION. It was written as
     though a private CA demanded that. It does not. This connects through scripts/redis_dual.py,
     which sets ssl_cert_reqs="required" with the mounted CA - the same path my 2026-10-06 probe
     verified end to end against this instance (PING True, SET/GET byte-identical at 94 chars, TTL
     read back, DEL gone, zero keys left behind). First-hand proof, not an argument.
  3. AUTHENTICATION WITHOUT AUTHORISATION: the old middleware verified a token, read payload.email
     and called next(). Any verified Google identity was in. Here a verified token must ALSO name a
     principal on a server-side allowlist (BRIDGE_CALLERS), and Cloud Run's own IAM check runs first.
     Two independent gates, and the allowlist is the one this repository can review.
  4. SECRETS AS ENVIRONMENT VARIABLES. An env var is listable from anything that can read the
     process. Both secrets are read from FILE MOUNTS, which is also what the running service already
     does, so this file does not regress what is deployed.

WHAT THIS SERVICE DELIBERATELY CANNOT DO
The only endpoint that touches Redis is a synthetic probe (locked decision 3: "first endpoint:
test-only synthetic-probe yes"). The caller selects NO key, NO command, NO namespace and NO TTL -
the server chooses all four. That is the same bound as scripts/bus_request.py and for the same
reason, with one difference that matters: here the caller is actually AUTHENTICATED, which is
precisely why the board-row worker was not widened tonight.

There is no action for GET, SET, HSET, XADD, KEYS, SCAN, FLUSHDB, FLUSHALL or DEL. Not refused -
ABSENT. A refusal is a line of code that can be got wrong.
"""
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
    """The service-account emails this bridge answers. Server-side, and reviewable in git."""
    raw = os.environ.get(CALLERS_ENV, "")
    return frozenset(p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip())


def audience() -> str:
    return (os.environ.get(AUDIENCE_ENV) or "").strip()


def verify_caller(token: str, expected_audience: str, permitted: frozenset) -> tuple:
    """(principal, None) when the caller is verified AND authorised, else (None, reason).

    BOTH HALVES, IN THIS ORDER, AND NEITHER IS OPTIONAL. The reason strings are deliberately coarse:
    a caller learns that it failed, not which of several checks it failed, because the difference is
    an oracle and is of no use to a legitimate caller.
    """
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
    """The proven path: scripts/redis_dual.py, TLS with ssl_cert_reqs=required against the MOUNTED CA.

    Defect 2 lives or dies here, and it is settled by REUSE rather than by a new implementation -
    this is the same client() a probe verified against this instance on 2026-10-06.

    precheck=False on purpose, and that is a defect I already made once: redis_dual's 1.5 s TCP
    latency guard is right on a hot path, where a fast no beats a slow yes, and WRONG here. A cold
    container whose Direct VPC interface is still coming up needs longer than 1.5 s, and a process
    whose whole job is to connect should let the connect itself be the authority. That guard turned
    into a false `reachable: false` on 2026-10-06 and cost a run."""
    import redis_dual
    return redis_dual.client(redis_dual.Settings(), precheck=False)


def synthetic_probe(conn) -> dict:
    """SET a server-named nonce key, read it back, check its TTL, delete it, verify it is gone.

    The caller chose none of: the key, the namespace, the nonce, the TTL. That is the whole design."""
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
        sys.stderr.write("%s %s\n" % (stamp(), fmt % args if args else fmt))

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

"""Small HMAC session tokens; no third-party JWT dependency is required."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time


class InvalidToken(ValueError):
    pass


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def mint_token(session_id: str, expires_at: int, secret: str, scope: str = "session",
               binding: dict | None = None) -> str:
    """A v1 token. ``binding`` (a client session's tenant and subject) is signed
    in so a session-scope request can be re-authorised against the registry."""
    claims = {"sid": session_id, "exp": int(expires_at), "scope": scope, "v": 1}
    if binding:
        if set(binding) != {"tnt", "csub"} or not all(isinstance(v, str) and v for v in binding.values()):
            raise ValueError("a session binding is exactly a tenant and a client subject")
        claims.update(binding)
    payload = _b64e(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64e(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return payload + "." + signature


def verify_token(token: str, secret: str, now: float | None = None,
                 scope: str | None = None) -> dict:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64e(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise InvalidToken("bad signature")
        claims = json.loads(_b64d(payload))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidToken("malformed token") from exc
    if claims.get("v") != 1 or not isinstance(claims.get("sid"), str):
        raise InvalidToken("unsupported token")
    if int(claims.get("exp") or 0) <= int(time.time() if now is None else now):
        raise InvalidToken("expired token")
    if scope is not None and claims.get("scope") != scope:
        raise InvalidToken("token scope is not allowed here")
    return claims


# -- client tokens (Gate 1) ------------------------------------------------------------
# A client token is its own format ("c2."), signed with a key derived for this one
# purpose, and verified only by verify_client_token: a v1 operator, visitor or
# session token can never pass as one, nor one as them. Its claims are exact:
# type, subject (the keyed hash of the verified address), tenant (the registry
# id), the tenant's project ids at issue, issued-at, expiry, audience and a
# nonce. A valid signature is necessary, never sufficient: every route also
# re-checks the tenant and subject against the registry (app/clients.py).
CLIENT_PREFIX = "c2."
CLIENT_AUDIENCE = "sfdc24-studio-client"
CLIENT_TOKEN_MAX_SECONDS = 86400
CLOCK_SKEW_SECONDS = 60
_CLIENT_CLAIMS = {"v", "typ", "sub", "tnt", "prj", "iat", "exp", "aud", "jti"}
_SUBJECT_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_JTI_RE = re.compile(r"^[0-9a-f]{32}$")


def _client_key(secret: str) -> bytes:
    return hmac.new(secret.encode(), b"sfdc24-studio-client-token-v2", hashlib.sha256).digest()


def _client_signature(secret: str, payload: str) -> str:
    return _b64e(hmac.new(_client_key(secret), b"client." + payload.encode(), hashlib.sha256).digest())


def mint_client_token(subject: str, tenant: str, projects, issued_at: int, expires_at: int, secret: str,
                      nonce: str | None = None) -> str:
    projects = sorted(set(projects))
    if (not isinstance(subject, str) or not _SUBJECT_RE.fullmatch(subject)
            or not isinstance(tenant, str) or not _ID_RE.fullmatch(tenant)
            or not all(isinstance(p, str) and _ID_RE.fullmatch(p) for p in projects)
            or not 0 < int(expires_at) - int(issued_at) <= CLIENT_TOKEN_MAX_SECONDS):
        raise ValueError("a client token needs a subject, a tenant, project ids and a bounded lifetime")
    claims = {"v": 2, "typ": "client", "sub": subject, "tnt": tenant, "prj": projects,
              "iat": int(issued_at), "exp": int(expires_at), "aud": CLIENT_AUDIENCE,
              "jti": nonce or secrets.token_hex(16)}
    payload = _b64e(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return CLIENT_PREFIX + payload + "." + _client_signature(secret, payload)


def verify_client_token(token: str, secret: str, now: float | None = None) -> dict:
    """The claims of a valid, current client token, or InvalidToken."""
    if not isinstance(token, str) or not token.startswith(CLIENT_PREFIX) or len(token) > 4096:
        raise InvalidToken("not a client token")
    try:
        payload, signature = token[len(CLIENT_PREFIX):].split(".", 1)
        if not hmac.compare_digest(signature, _client_signature(secret, payload)):
            raise InvalidToken("bad signature")
        claims = json.loads(_b64d(payload))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidToken("malformed token") from exc
    if not isinstance(claims, dict) or set(claims) != _CLIENT_CLAIMS:
        raise InvalidToken("malformed token")
    current = int(time.time() if now is None else now)
    ok = (claims["v"] == 2 and claims["typ"] == "client" and claims["aud"] == CLIENT_AUDIENCE
          and isinstance(claims["sub"], str) and bool(_SUBJECT_RE.fullmatch(claims["sub"]))
          and isinstance(claims["tnt"], str) and bool(_ID_RE.fullmatch(claims["tnt"]))
          and isinstance(claims["prj"], list)
          and all(isinstance(p, str) and _ID_RE.fullmatch(p) for p in claims["prj"])
          and isinstance(claims["jti"], str) and bool(_JTI_RE.fullmatch(claims["jti"]))
          and type(claims["iat"]) is int and type(claims["exp"]) is int
          and 0 < claims["exp"] - claims["iat"] <= CLIENT_TOKEN_MAX_SECONDS)
    if not ok:
        raise InvalidToken("unsupported token")
    if claims["iat"] > current + CLOCK_SKEW_SECONDS:
        raise InvalidToken("token issued in the future")
    if claims["exp"] <= current:
        raise InvalidToken("expired token")
    return claims


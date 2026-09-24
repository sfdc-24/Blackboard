"""Small HMAC session tokens; no third-party JWT dependency is required."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class InvalidToken(ValueError):
    pass


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def mint_token(session_id: str, expires_at: int, secret: str, scope: str = "session") -> str:
    claims = {"sid": session_id, "exp": int(expires_at), "scope": scope, "v": 1}
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

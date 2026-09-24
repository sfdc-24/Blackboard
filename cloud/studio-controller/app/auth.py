"""Email OTP domain logic for SFDC24 Studio operator authentication.

The HTTP layer is deliberately absent.  This module owns the security-sensitive
state transition and returns a privacy-preserving subject that a caller can put
in its own signed token after successful verification.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable, Iterable

try:
    from scripts.state_store import Conflict
except ImportError:  # Docker copies the shared module beside the app.
    from state_store import Conflict


OTP_TTL_SECONDS = 10 * 60
SEND_WINDOW_SECONDS = 15 * 60
MAX_SENDS_PER_WINDOW = 3
MAX_VERIFY_ATTEMPTS = 5

_OTP_RE = re.compile(r"^[0-9]{6}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


def _default_otp() -> str:
    """Return a zero-padded six-digit OTP from the OS CSPRNG."""
    return "%06d" % secrets.randbelow(1_000_000)


def _default_challenge_id() -> str:
    return secrets.token_hex(16)


class AuthService:
    """CAS-backed, enumeration-safe email OTP state machine.

    ``sender`` is a callable accepting ``(email, code)``.  It is the only
    collaborator that receives either plaintext value.  Delivery failures are
    intentionally not reflected in ``start`` results because doing so would
    turn the endpoint into an allowlist oracle; the sender should report its own
    operational telemetry.
    """

    def __init__(
        self,
        store,
        allowed_emails: Iterable[str],
        secret: str | bytes,
        sender: Callable[[str, str], None],
        *,
        clock: Callable[[], float] = time.time,
        otp_generator: Callable[[], str] | None = None,
        challenge_id_generator: Callable[[], str] | None = None,
        cas_attempts: int = 8,
    ):
        allowed = frozenset(allowed_emails)
        for email in allowed:
            if (
                not isinstance(email, str)
                or email != email.lower()
                or email != email.strip()
                or len(email) > 320
                or email.count("@") != 1
            ):
                raise ValueError("allowed emails must be exact lowercase addresses")
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("auth secret must contain at least 32 bytes")
        if not callable(sender):
            raise TypeError("sender must be callable")
        if cas_attempts < 1:
            raise ValueError("cas_attempts must be positive")

        self.store = store
        self.allowed_emails = allowed
        self.secret = secret
        self.sender = sender
        self.clock = clock
        self.otp_generator = otp_generator or _default_otp
        self.challenge_id_generator = challenge_id_generator or _default_challenge_id
        self.cas_attempts = cas_attempts

    def _digest(self, purpose: str, value: str) -> str:
        message = purpose.encode("ascii") + b"\x00" + value.encode("utf-8")
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def _subject_hash(self, email: str) -> str:
        return self._digest("studio-auth-subject-v1", email)

    def _client_hash(self, client_key: str) -> str:
        return self._digest("studio-auth-client-v1", client_key)

    def _otp_hash(self, challenge_id: str, subject_hash: str, code: str) -> str:
        bound = challenge_id + "\x00" + subject_hash + "\x00" + code
        return self._digest("studio-auth-otp-v1", bound)

    def _new_code(self) -> str:
        code = self.otp_generator()
        if not isinstance(code, str) or not _OTP_RE.fullmatch(code):
            raise ValueError("otp generator must return exactly six ASCII digits")
        return code

    def _new_challenge_id(self) -> str:
        challenge_id = self.challenge_id_generator()
        if not isinstance(challenge_id, str) or not _CHALLENGE_RE.fullmatch(challenge_id):
            raise ValueError("challenge id generator returned an unsafe id")
        return challenge_id

    @staticmethod
    def _challenge_name(challenge_id: str) -> str:
        return "studio_auth_challenge_" + challenge_id

    @staticmethod
    def _rate_name(subject_hash: str) -> str:
        return "studio_auth_rate_" + subject_hash

    def _reserve_send(self, subject_hash: str, now: int) -> bool:
        """Reserve one send in a per-subject sliding window under CAS."""
        name = self._rate_name(subject_hash)
        cutoff = now - SEND_WINDOW_SECONDS
        for _ in range(self.cas_attempts):
            state, token = self.store.load(name)
            raw_times = state.get("sent_at", []) if isinstance(state, dict) else []
            if not isinstance(raw_times, list):
                return False  # Corrupt limiter state fails closed.
            try:
                sent_at = [int(value) for value in raw_times if int(value) > cutoff]
            except (TypeError, ValueError):
                return False
            if len(sent_at) >= MAX_SENDS_PER_WINDOW:
                return False
            candidate = {
                "version": 1,
                "subject_hash": subject_hash,
                "sent_at": sent_at + [now],
                "updated_at": now,
            }
            try:
                self.store.save(name, candidate, token)
                return True
            except Conflict:
                continue
        return False

    def start(self, email: str, client_key: str) -> dict:
        """Start an OTP challenge without revealing allowlist or rate status.

        Every call returns the same public shape.  For an ineligible or limited
        address, the opaque challenge id is a decoy and no durable challenge is
        created.  The caller must not add a delivery-status field.
        """
        challenge_id = self._new_challenge_id()
        code = self._new_code()
        public = {"challenge_id": challenge_id, "expires_in": OTP_TTL_SECONDS}

        eligible_email = (
            isinstance(email, str)
            and len(email) <= 320
            and email == email.lower()
            and email in self.allowed_emails
        )
        eligible_client = isinstance(client_key, str) and 0 < len(client_key) <= 512
        if not eligible_email or not eligible_client:
            return public

        now = int(self.clock())
        subject_hash = self._subject_hash(email)
        if not self._reserve_send(subject_hash, now):
            return public

        client_hash = self._client_hash(client_key)
        stored = False
        for _ in range(self.cas_attempts):
            record = {
                "version": 1,
                "subject_hash": subject_hash,
                "client_hash": client_hash,
                "otp_hash": self._otp_hash(challenge_id, subject_hash, code),
                "created_at": now,
                "expires_at": now + OTP_TTL_SECONDS,
                "attempts": 0,
                "used_at": None,
            }
            try:
                self.store.save(self._challenge_name(challenge_id), record, None)
                stored = True
                break
            except Conflict:
                challenge_id = self._new_challenge_id()
                public = {"challenge_id": challenge_id, "expires_in": OTP_TTL_SECONDS}
        if not stored:
            return public

        try:
            self.sender(email, code)
        except Exception:  # noqa: BLE001 - do not create an allowlist oracle.
            pass
        return public

    def verify(self, challenge_id: str, email: str, code: str, client_key: str) -> dict:
        """Consume one verification attempt, returning an opaque subject on success."""
        failure = {"verified": False}
        if not isinstance(challenge_id, str) or not _CHALLENGE_RE.fullmatch(challenge_id):
            return failure

        email_value = email if isinstance(email, str) and len(email) <= 320 else ""
        client_value = client_key if isinstance(client_key, str) and len(client_key) <= 512 else ""
        code_valid = isinstance(code, str) and bool(_OTP_RE.fullmatch(code))
        code_value = code if code_valid else "000000"
        now = int(self.clock())
        name = self._challenge_name(challenge_id)

        for _ in range(self.cas_attempts):
            state, token = self.store.load(name)
            if not isinstance(state, dict) or not state:
                return failure
            try:
                attempts = int(state.get("attempts", 0))
                expires_at = int(state["expires_at"])
                stored_subject = str(state["subject_hash"])
                stored_client = str(state["client_hash"])
                stored_otp = str(state["otp_hash"])
            except (KeyError, TypeError, ValueError):
                return failure
            if state.get("used_at") is not None or now >= expires_at or attempts >= MAX_VERIFY_ATTEMPTS:
                return failure

            supplied_subject = self._subject_hash(email_value)
            supplied_client = self._client_hash(client_value)
            supplied_otp = self._otp_hash(challenge_id, supplied_subject, code_value)
            matches = (
                code_valid
                and hmac.compare_digest(stored_subject, supplied_subject)
                and hmac.compare_digest(stored_client, supplied_client)
                and hmac.compare_digest(stored_otp, supplied_otp)
            )

            candidate = dict(state)
            candidate["attempts"] = attempts + 1
            candidate["last_attempt_at"] = now
            if matches:
                candidate["used_at"] = now
            try:
                self.store.save(name, candidate, token)
            except Conflict:
                continue
            if matches:
                return {"verified": True, "subject_hash": stored_subject}
            return failure
        return failure


__all__ = [
    "AuthService",
    "OTP_TTL_SECONDS",
    "SEND_WINDOW_SECONDS",
    "MAX_SENDS_PER_WINDOW",
    "MAX_VERIFY_ATTEMPTS",
]

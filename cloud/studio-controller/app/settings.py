"""Runtime settings for the Studio controller.

Secrets are deliberately read at process start and are never serialized into
session state or returned by an endpoint.
"""
from __future__ import annotations

import os
import re
import math
from dataclasses import dataclass


def _origins(raw: str) -> tuple[str, ...]:
    return tuple(origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip())


def _emails(raw: str) -> tuple[str, ...]:
    return tuple(email.strip() for email in raw.split(",") if email.strip())


def _enabled(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    allowed_origins: tuple[str, ...]
    session_secret: str
    state_uri: str
    max_session_seconds: int = 600
    daily_session_cap: int = 20
    voice_mint_cap: int = 3
    max_events: int = 200
    max_commands: int = 100
    sse_poll_seconds: float = 0.75
    sse_wait_seconds: int = 25
    worker: str = "synthetic"
    operator_emails: tuple[str, ...] = ()
    operator_token_seconds: int = 8 * 60 * 60
    email_sender_url: str = ""
    email_sender_secret: str = ""
    voice_enabled: bool = False
    lead_facts_enabled: bool = False
    metadata_proposals_enabled: bool = False
    salesforce_org_id: str = ""
    lead_facts_timeout_seconds: float = 12.0
    maintenance_secret: str = ""
    openai_api_key: str = ""
    realtime_model: str = "gpt-realtime-2.1"
    realtime_voice: str = "marin"
    talk_cap: int = 60
    speak_cap: int = 150
    recap_cap: int = 6
    architect_voice: str = "cedar"
    analyze_cap: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("STUDIO_SESSION_SECRET", "")
        if not secret:
            # Local development stays usable. Cloud deployment rejects this
            # value in main.py so a default can never become a production key.
            secret = "local-development-only-change-me"
        return cls(
            allowed_origins=_origins(os.environ.get(
                "STUDIO_ALLOWED_ORIGINS",
                "https://www.sfdc24.com,https://sfdc24.com",
            )),
            session_secret=secret,
            state_uri=os.environ.get("BLACKBOARD_STATE_URI", ""),
            max_session_seconds=int(os.environ.get("STUDIO_MAX_SESSION_SECONDS", "600")),
            daily_session_cap=int(os.environ.get("STUDIO_DAILY_SESSION_CAP", "20")),
            voice_mint_cap=int(os.environ.get("STUDIO_VOICE_MINT_CAP", "3")),
            max_events=int(os.environ.get("STUDIO_MAX_EVENTS", "200")),
            max_commands=int(os.environ.get("STUDIO_MAX_COMMANDS", "100")),
            sse_poll_seconds=float(os.environ.get("STUDIO_SSE_POLL_SECONDS", "0.75")),
            sse_wait_seconds=int(os.environ.get("STUDIO_SSE_WAIT_SECONDS", "25")),
            worker=os.environ.get("STUDIO_WORKER", "synthetic").strip().lower(),
            operator_emails=_emails(os.environ.get("STUDIO_OPERATOR_EMAILS", "")),
            operator_token_seconds=int(os.environ.get("STUDIO_OPERATOR_TOKEN_SECONDS", "28800")),
            email_sender_url=os.environ.get("STUDIO_EMAIL_SENDER_URL", "").strip(),
            email_sender_secret=os.environ.get("STUDIO_EMAIL_SENDER_SECRET", ""),
            voice_enabled=_enabled(os.environ.get("STUDIO_ENABLE_VOICE", "false")),
            lead_facts_enabled=_enabled(os.environ.get("STUDIO_ENABLE_LEAD_FACTS", "false")),
            metadata_proposals_enabled=_enabled(os.environ.get("STUDIO_ENABLE_METADATA_PROPOSALS", "false")),
            salesforce_org_id=os.environ.get("STUDIO_SALESFORCE_ORG_ID", ""),
            lead_facts_timeout_seconds=(float(os.environ.get("STUDIO_LEAD_FACTS_TIMEOUT_SECONDS", "12"))
                                        if _enabled(os.environ.get("STUDIO_ENABLE_LEAD_FACTS", "false")) else 12.0),
            maintenance_secret=os.environ.get("STUDIO_MAINTENANCE_SECRET", ""),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            realtime_model=os.environ.get("STUDIO_REALTIME_MODEL", "gpt-realtime-2.1"),
            realtime_voice=os.environ.get("STUDIO_REALTIME_VOICE", "marin"),
            talk_cap=int(os.environ.get("STUDIO_TALK_CAP", "60")),
            speak_cap=int(os.environ.get("STUDIO_SPEAK_CAP", "150")),
            recap_cap=int(os.environ.get("STUDIO_RECAP_CAP", "6")),
            architect_voice=os.environ.get("STUDIO_ARCHITECT_VOICE", "cedar"),
            analyze_cap=int(os.environ.get("STUDIO_ANALYZE_CAP", "30")),
        )

    @property
    def production(self) -> bool:
        return bool(os.environ.get("K_SERVICE"))

    def validate(self) -> None:
        if self.lead_facts_enabled and (
            type(self.lead_facts_timeout_seconds) not in (int, float)
            or not math.isfinite(self.lead_facts_timeout_seconds)
            or not 0 < self.lead_facts_timeout_seconds <= 20
        ):
            raise RuntimeError("STUDIO_LEAD_FACTS_TIMEOUT_SECONDS must be greater than 0 and at most 20")
        if (self.lead_facts_enabled or self.metadata_proposals_enabled) and (
            not isinstance(self.salesforce_org_id, str)
            or not re.fullmatch(r"00D[A-Za-z0-9]{15}", self.salesforce_org_id)
        ):
            raise RuntimeError("STUDIO_SALESFORCE_ORG_ID must be an exact 18-character 00D Organization ID")
        if not self.allowed_origins:
            raise RuntimeError("STUDIO_ALLOWED_ORIGINS must contain at least one exact origin")
        if self.max_session_seconds < 60 or self.max_session_seconds > 600:
            raise RuntimeError("STUDIO_MAX_SESSION_SECONDS must be between 60 and 600")
        if self.daily_session_cap < 1:
            raise RuntimeError("STUDIO_DAILY_SESSION_CAP must be positive")
        if self.voice_mint_cap < 1 or self.voice_mint_cap > 10:
            raise RuntimeError("STUDIO_VOICE_MINT_CAP must be between 1 and 10")
        if self.max_events < 20:
            raise RuntimeError("STUDIO_MAX_EVENTS must be at least 20")
        if self.max_commands < 10 or self.max_commands > 500:
            raise RuntimeError("STUDIO_MAX_COMMANDS must be between 10 and 500")
        if self.operator_token_seconds < 600 or self.operator_token_seconds > 86400:
            raise RuntimeError("STUDIO_OPERATOR_TOKEN_SECONDS must be between 600 and 86400")
        if any(email != email.lower() or email != email.strip() for email in self.operator_emails):
            raise RuntimeError("STUDIO_OPERATOR_EMAILS must contain exact lowercase addresses")
        if self.production and self.session_secret == "local-development-only-change-me":
            raise RuntimeError("STUDIO_SESSION_SECRET is required in Cloud Run")
        if self.production and len(self.session_secret.encode("utf-8")) < 32:
            raise RuntimeError("STUDIO_SESSION_SECRET must contain at least 32 bytes")
        if self.production and not self.state_uri.startswith("gs://"):
            raise RuntimeError("Cloud Run requires BLACKBOARD_STATE_URI=gs://...")
        if self.production and not self.operator_emails:
            raise RuntimeError("Cloud Run requires STUDIO_OPERATOR_EMAILS")
        if self.production and not self.email_sender_url.startswith("https://"):
            raise RuntimeError("Cloud Run requires an HTTPS STUDIO_EMAIL_SENDER_URL")
        if self.production and len(self.email_sender_secret.encode("utf-8")) < 32:
            raise RuntimeError("Cloud Run requires a 32-byte STUDIO_EMAIL_SENDER_SECRET")
        if self.production and self.voice_enabled and not self.openai_api_key:
            raise RuntimeError("voice requires OPENAI_API_KEY")
        if self.production and self.voice_enabled and len(self.maintenance_secret.encode("utf-8")) < 32:
            raise RuntimeError("voice requires a 32-byte STUDIO_MAINTENANCE_SECRET")

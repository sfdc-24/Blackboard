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


# Every provider a talk lane can name (workers/talk.py AGENTS).
KNOWN_PROVIDERS = ("claude", "openai", "gemini", "meta")


def _providers(raw: str) -> tuple[str, ...]:
    """Comma-separated provider names, as written; validate() refuses anything odd."""
    return tuple(part.strip() for part in raw.split(","))


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
    # Public visitors (off unless STUDIO_PUBLIC_VISITORS=true): any email can
    # get a sign-in code, capped per UTC day across all visitors; a verified
    # visitor gets a short visitor token and a few sessions a day.
    public_visitors: bool = False
    visitor_codes_daily_cap: int = 100
    visitor_sessions_per_day: int = 2
    visitor_token_seconds: int = 2 * 60 * 60
    # Visitors share the daily session and voice ledgers with the operator but
    # stop this far below each cap, so the operator is never locked out.
    operator_reserved_sessions: int = 5
    operator_reserved_voice: int = 1
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
    # The use-policy gate (app/governance.py). Off unless constructed on; the
    # environment turns it ON by default, so a deploy never ships without it.
    moderation_enabled: bool = False
    muse_voice: str = "coral"
    muse_cap: int = 6
    summary_email_enabled: bool = False
    # The Gemini advisor (workers/advisor.py, plan R3): off unless switched on.
    advisor_enabled: bool = False
    # The charter lane (workers/charter.py): off unless switched on.
    charter_enabled: bool = False
    # The build plan's prices (app/pricing.py): the owner's numbers or nothing.
    price_table: str = ""
    # Client workspaces (app/clients.py): a registered client signs in and
    # sees their own projects. Off unless STUDIO_CLIENT_WORKSPACES=true.
    client_workspaces: bool = False
    # The providers a client (workspace) session may reach for talk, recap and
    # advice (Codex Gate 1 NO-GO on a2d98fc): the owner-approved pair unless
    # STUDIO_CLIENT_PROVIDERS names others. The builder (Claude) and the voice,
    # speech and moderation calls (OpenAI) are the base of every session.
    client_providers: tuple[str, ...] = ("claude", "openai")
    # The spend, spacing, busy and fetch guards are per process (Codex Gate 1
    # B3/B4 on cc56fea): client workspaces run only on a service declared to
    # be one instance (deploy with --max-instances=1).
    single_instance: bool = False

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
            public_visitors=_enabled(os.environ.get("STUDIO_PUBLIC_VISITORS", "false")),
            visitor_codes_daily_cap=int(os.environ.get("STUDIO_VISITOR_CODES_DAILY_CAP", "100")),
            visitor_sessions_per_day=int(os.environ.get("STUDIO_VISITOR_SESSIONS_PER_DAY", "2")),
            visitor_token_seconds=int(os.environ.get("STUDIO_VISITOR_TOKEN_SECONDS", "7200")),
            operator_reserved_sessions=int(os.environ.get("STUDIO_OPERATOR_RESERVED_SESSIONS", "5")),
            operator_reserved_voice=int(os.environ.get("STUDIO_OPERATOR_RESERVED_VOICE", "1")),
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
            moderation_enabled=_enabled(os.environ.get("STUDIO_ENABLE_MODERATION", "true")),
            muse_voice=os.environ.get("STUDIO_MUSE_VOICE", "coral"),
            muse_cap=int(os.environ.get("STUDIO_MUSE_CAP", "6")),
            summary_email_enabled=_enabled(os.environ.get("STUDIO_ENABLE_SUMMARY_EMAIL", "false")),
            advisor_enabled=_enabled(os.environ.get("STUDIO_ENABLE_ADVISOR", "false")),
            charter_enabled=_enabled(os.environ.get("STUDIO_ENABLE_CHARTER", "false")),
            price_table=os.environ.get("STUDIO_PRICE_TABLE", ""),
            client_workspaces=_enabled(os.environ.get("STUDIO_CLIENT_WORKSPACES", "false")),
            client_providers=_providers(os.environ.get("STUDIO_CLIENT_PROVIDERS", "claude,openai")),
            single_instance=_enabled(os.environ.get("STUDIO_SINGLE_INSTANCE", "false")),
        )

    @property
    def production(self) -> bool:
        return bool(os.environ.get("K_SERVICE"))

    def validate(self) -> None:
        if self.charter_enabled and not self.moderation_enabled:
            # The charter's lines reach the page; they are moderated first.
            raise RuntimeError("STUDIO_ENABLE_CHARTER requires STUDIO_ENABLE_MODERATION")
        from .pricing import parse_price_table
        from workers.topics import TOPICS, quote_lines
        try:
            parse_price_table(self.price_table, TOPICS, lambda t: [line[0] for line in quote_lines(t)])
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
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
        if not 1 <= self.visitor_codes_daily_cap <= 1000:
            raise RuntimeError("STUDIO_VISITOR_CODES_DAILY_CAP must be between 1 and 1000")
        if not 1 <= self.visitor_sessions_per_day <= 20:
            raise RuntimeError("STUDIO_VISITOR_SESSIONS_PER_DAY must be between 1 and 20")
        if not 600 <= self.visitor_token_seconds <= 86400:
            raise RuntimeError("STUDIO_VISITOR_TOKEN_SECONDS must be between 600 and 86400")
        if self.public_visitors and not 1 <= self.operator_reserved_sessions < self.daily_session_cap:
            raise RuntimeError("STUDIO_OPERATOR_RESERVED_SESSIONS must leave visitors at least one daily session")
        if self.public_visitors and not 1 <= self.operator_reserved_voice < self.voice_mint_cap:
            raise RuntimeError("STUDIO_OPERATOR_RESERVED_VOICE must leave visitors at least one daily voice call")
        if self.client_workspaces and not 1 <= self.operator_reserved_sessions < self.daily_session_cap:
            raise RuntimeError("STUDIO_OPERATOR_RESERVED_SESSIONS must leave clients at least one daily session")
        if self.client_workspaces and not 1 <= self.operator_reserved_voice < self.voice_mint_cap:
            raise RuntimeError("STUDIO_OPERATOR_RESERVED_VOICE must leave clients at least one daily voice call")
        providers = self.client_providers
        if (not isinstance(providers, tuple) or not providers or len(set(providers)) != len(providers)
                or any(p not in KNOWN_PROVIDERS for p in providers)):
            # Fail closed: a typo must stop the service, never widen or silently narrow it.
            raise RuntimeError("STUDIO_CLIENT_PROVIDERS must be distinct names from: " + ", ".join(KNOWN_PROVIDERS))
        if self.client_workspaces and not self.single_instance:
            raise RuntimeError("STUDIO_CLIENT_WORKSPACES requires STUDIO_SINGLE_INSTANCE=true: its spend, spacing, "
                               "busy and fetch guards are per process, so the service must run as one instance "
                               "(--max-instances=1)")
        if self.client_workspaces and self.lead_facts_enabled:
            # Lead facts read the Salesforce org for any session; a client must
            # never reach that path either.
            raise RuntimeError("STUDIO_CLIENT_WORKSPACES cannot be on while STUDIO_ENABLE_LEAD_FACTS is on")
        if self.public_visitors and self.lead_facts_enabled:
            # Lead facts read the Salesforce org for any session; a public
            # visitor must never reach that path.
            raise RuntimeError("STUDIO_PUBLIC_VISITORS cannot be on while STUDIO_ENABLE_LEAD_FACTS is on")
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

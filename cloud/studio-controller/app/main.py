"""FastAPI surface for the durable SFDC24 Studio controller."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

try:
    from scripts.state_store import Conflict, open_store
except ImportError:
    from state_store import Conflict, open_store

from . import governance
from .core import CommandError, StudioController
from .auth import AuthService
from .leads import LeadBook, LeadCapExceeded
from .settings import Settings
from .state import (
    SessionNotFound,
    StateConflict,
    StudioRepository,
    VoiceCapacityExceeded,
)
from .summary_pdf import DesignImageError, build_summary_pdf, decode_design_png, mask_email
from .tokens import InvalidToken, mint_token, verify_token
from .workers.synthetic import SyntheticWorker


CALL_ID_RE = re.compile(r"^rtc_[A-Za-z0-9_-]{1,120}$")


class SummaryNotSent(RuntimeError):
    """The summary email certainly did not leave: safe to try again."""


def _worker(settings: Settings):
    if settings.worker == "synthetic":
        return SyntheticWorker()
    if settings.worker == "claude":
        from workers.claude_worker import ClaudeWorker
        worker = ClaudeWorker()
        # Claude's worker consumes an existing typed artifact. Keep deterministic
        # bootstrap data so the first model turn never invents an unsafe root.
        seed = SyntheticWorker()
        worker.initial_artifact = seed.initial_artifact
        worker.initial_questions = seed.initial_questions
        return worker
    raise RuntimeError("STUDIO_WORKER must be synthetic or claude")


def _sse(event: dict, name: str | None = None) -> str:
    event_name = name or event["type"]
    return "id: %s\nevent: %s\ndata: %s\n\n" % (
        event["seq"], event_name, json.dumps(event, separators=(",", ":")),
    )


# How the two voices sound (owner, 2026-09-25: "a more powerful voice for the
# AI Agents, may be with a bit more fire/character"). The host is the realtime
# call and still says only the exact line it is given; the architect is TTS.
HOST_INSTRUCTIONS = (
    "You are the host of a live SFDC24 build session. Speak only the exact text you are given, word for word, "
    "and nothing else. Deliver it with real energy and warmth, like a host who is delighted the visitor is "
    "here: bright, expressive, a smile in the voice, a lively natural pace, never flat or monotone. "
    "Never state facts about Salesforce or the visitor's business. Ask one question at a time."
)
ARCHITECT_VOICE_STYLE = (
    "An energetic, confident solution architect who loves building things with people: punchy and upbeat, "
    "a little playful, with real conviction in every line. Brisk pace, crisp delivery, a smile in the voice. "
    "Never flat or monotone."
)


def create_app(*, settings: Settings | None = None, store=None, worker=None,
               clock=time.time, id_factory=None, voice_client=None,
               email_sender=None, email_client=None, talk_client=None,
               analyst=None, moderation_client=None, muse=None, summary_sender=None,
               advisor=None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    store = store or open_store(settings.state_uri)
    repository = StudioRepository(store, clock=clock)
    selected_worker = worker or _worker(settings)
    if settings.lead_facts_enabled:
        from .workers.lead_facts import LeadFactsWorker
        selected_worker = LeadFactsWorker(selected_worker, settings.salesforce_org_id,
                                         settings.lead_facts_timeout_seconds)
    controller = StudioController(
        repository, selected_worker, clock=clock, id_factory=id_factory,
        max_seconds=settings.max_session_seconds, daily_cap=settings.daily_session_cap,
        max_events=settings.max_events, max_commands=settings.max_commands,
        metadata_proposals_enabled=settings.metadata_proposals_enabled,
        metadata_org_id=settings.salesforce_org_id,
    )

    def voice_config() -> dict:
        return {
            "type": "realtime",
            "model": settings.realtime_model,
            "instructions": HOST_INSTRUCTIONS,
            "audio": {
                "input": {
                    "transcription": {"model": "gpt-transcribe"},
                    "turn_detection": {"type": "semantic_vad", "create_response": False},
                },
                "output": {"voice": settings.realtime_voice},
            },
        }

    def call_id_from_location(location: str) -> str:
        call_id = (location or "").rstrip("/").rsplit("/", 1)[-1]
        if not CALL_ID_RE.fullmatch(call_id):
            raise HTTPException(502, "voice provider returned no valid call id")
        return call_id

    async def end_voice_call(session_id: str, reason: str, *, force: bool = False) -> bool:
        try:
            state = (await asyncio.to_thread(repository.load, session_id)).state
        except SessionNotFound:
            await asyncio.to_thread(
                repository.unregister_voice, session_id, force=True
            )
            return True
        voice = state.get("voice_call") or {}
        if not voice:
            return await asyncio.to_thread(repository.unregister_voice, session_id)
        if voice.get("status") in {"ended", "failed"}:
            return await asyncio.to_thread(
                repository.unregister_voice,
                session_id,
                voice.get("voice_id"),
                allow_legacy=True,
            )
        if voice.get("status") != "active" or not voice.get("call_id"):
            # Opening and ambiguous-provider reservations are deliberately kept
            # pending. Without a provider call ID, the sweeper cannot prove the
            # remote call never opened and must not report successful cleanup.
            return False
        if (
            not force
            and not voice.get("close_requested")
            and int(voice.get("ends_at") or 0) > int(clock())
        ):
            return False
        call_id = voice["call_id"]
        if force:
            try:
                await asyncio.to_thread(
                    controller.request_voice_end, session_id, call_id, reason
                )
                await asyncio.to_thread(
                    repository.register_voice,
                    session_id,
                    voice["voice_id"],
                    int(clock()),
                    adopt_legacy=True,
                )
            except (StateConflict, SessionNotFound):
                return False
        try:
            response = await app.state.voice_client.post(
                "https://api.openai.com/v1/realtime/calls/%s/hangup" % call_id,
                headers={"Authorization": "Bearer " + settings.openai_api_key},
            )
        except httpx.HTTPError:
            return False
        if not (
            200 <= response.status_code < 300
            or response.status_code in {404, 409}
        ):
            return False
        try:
            await asyncio.to_thread(controller.finish_voice, session_id, call_id, reason)
            removed = await asyncio.to_thread(
                repository.unregister_voice,
                session_id,
                voice["voice_id"],
                allow_legacy=True,
            )
        except (StateConflict, SessionNotFound):
            return False
        return removed

    async def release_unopened_voice(session_id: str, voice_id: str) -> bool:
        """Release the exact reservation only before provider contact."""
        try:
            removed = await asyncio.to_thread(
                repository.unregister_voice, session_id, voice_id
            )
        except StateConflict:
            return False
        if not removed:
            return False
        try:
            return await asyncio.to_thread(controller.fail_voice, session_id, voice_id)
        except (SessionNotFound, StateConflict):
            return False

    async def sweep_due_calls() -> dict:
        summary = {
            "due": 0,
            "attempted": 0,
            "completed": 0,
            "pending": 0,
            "index_available": True,
        }
        if not settings.voice_enabled or not settings.openai_api_key:
            return summary
        try:
            due = await asyncio.to_thread(repository.due_voice_sessions, int(clock()))
        except Exception:
            summary["index_available"] = False
            return summary
        selected = due[: settings.daily_session_cap]
        summary["due"] = len(due)
        summary["attempted"] = len(selected)
        summary["pending"] = len(due) - len(selected)
        for session_id in selected:
            try:
                ended = await end_voice_call(session_id, "expired")
            except Exception:
                ended = False
            if ended:
                summary["completed"] += 1
            else:
                summary["pending"] += 1
        return summary

    async def voice_sweeper() -> None:
        while True:
            await asyncio.sleep(15)
            await sweep_due_calls()

    def send_auth_email(email: str, code: str) -> None:
        if email_sender is not None:
            email_sender(email, code)
            return
        if not settings.email_sender_url or not settings.email_sender_secret:
            raise RuntimeError("email sender is not configured")
        timestamp = str(int(clock()))
        nonce = secrets.token_hex(16)
        canonical = "\n".join((timestamp, nonce, email, code)).encode("utf-8")
        signature = hmac.new(
            settings.email_sender_secret.encode("utf-8"), canonical, hashlib.sha256
        ).hexdigest()
        response = app.state.email_client.post(settings.email_sender_url, json={
            "timestamp": timestamp,
            "nonce": nonce,
            "email": email,
            "code": code,
            "signature": signature,
        })
        response.raise_for_status()
        try:
            receipt = response.json()
        except ValueError as exc:
            raise RuntimeError("email sender returned invalid JSON") from exc
        if not isinstance(receipt, dict) or receipt.get("ok") is not True:
            raise RuntimeError("email sender refused delivery")

    SUMMARY_EMAIL_TIMEOUT_SECONDS = 45

    def send_summary_email(email: str, pdf: bytes) -> None:
        """The working-session PDF to a verified address, through the same signed
        Apps Script sender as the sign-in code (kind "summary"). The signature
        covers the PDF's SHA-256, so the sender refuses any other attachment.

        Raises SummaryNotSent only when the email certainly did not go: the
        connection to the sender never opened, or the script answered an
        explicit {"ok": false} (it refuses before MailApp). Every other failure
        - a timeout after the request left, a lost redirect, a 5xx, a body that
        is not the receipt - may follow a delivered email and raises something
        else, which the caller records as unconfirmed and never resends."""
        if summary_sender is not None:
            summary_sender(email, pdf)
            return
        if (not settings.email_sender_url or not settings.email_sender_secret
                or getattr(app.state, "email_client", None) is None):
            raise SummaryNotSent("email sender is not configured")
        timestamp = str(int(clock()))
        nonce = secrets.token_hex(16)
        digest = hashlib.sha256(pdf).hexdigest()
        canonical = "\n".join(("summary", timestamp, nonce, email, digest)).encode("utf-8")
        signature = hmac.new(
            settings.email_sender_secret.encode("utf-8"), canonical, hashlib.sha256
        ).hexdigest()
        client = app.state.email_client
        try:
            # Redirects are followed by hand below, so a connection failure here
            # is always the POST itself, before the script could have run.
            response = client.post(settings.email_sender_url, json={
                "kind": "summary",
                "timestamp": timestamp,
                "nonce": nonce,
                "email": email,
                "pdf": base64.b64encode(pdf).decode("ascii"),
                "pdf_sha256": digest,
                "signature": signature,
            }, timeout=SUMMARY_EMAIL_TIMEOUT_SECONDS, follow_redirects=False)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise SummaryNotSent("the email sender could not be reached") from exc
        # The script has run from here on. Apps Script answers with a redirect
        # to its receipt; losing that hop says nothing about the email.
        if response.is_redirect and response.headers.get("location"):
            response = client.get(response.headers["location"], timeout=SUMMARY_EMAIL_TIMEOUT_SECONDS)
        response.raise_for_status()
        receipt = response.json()
        if isinstance(receipt, dict) and receipt.get("ok") is False:
            raise SummaryNotSent("the email sender refused delivery")
        if not isinstance(receipt, dict) or receipt.get("ok") is not True:
            raise RuntimeError("the email sender gave no receipt")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if voice_client is None:
            app.state.voice_client = httpx.AsyncClient(timeout=20)
            app.state.close_voice_client = True
        else:
            app.state.voice_client = voice_client
            app.state.close_voice_client = False
        if email_sender is None and email_client is not None:
            app.state.email_client = email_client
            app.state.close_email_client = False
        elif email_sender is None and settings.email_sender_url:
            # Apps Script ContentService returns a short-lived HTTPS redirect to
            # script.googleusercontent.com for the JSON receipt.
            app.state.email_client = httpx.Client(
                timeout=10, follow_redirects=True, max_redirects=3
            )
            app.state.close_email_client = True
        else:
            app.state.email_client = None
            app.state.close_email_client = False
        app.state.voice_sweeper = asyncio.create_task(voice_sweeper())
        yield
        app.state.voice_sweeper.cancel()
        try:
            await app.state.voice_sweeper
        except asyncio.CancelledError:
            pass
        if app.state.close_voice_client:
            await app.state.voice_client.aclose()
        if app.state.close_email_client:
            await asyncio.to_thread(app.state.email_client.close)

    # Behind TLS termination the ASGI scheme can be HTTP. Never redirect a
    # bearer-bearing POST to a slash-normalized URL inferred from that scheme.
    # Canonical routes below are exact; slash variants fail closed with 404.
    app = FastAPI(title="SFDC24 Studio controller", version="1.0",
                  lifespan=lifespan, redirect_slashes=False)
    app.state.settings = settings
    app.state.controller = controller
    auth_service = AuthService(
        store, settings.operator_emails, settings.session_secret, send_auth_email, clock=clock,
        public_visitors=settings.public_visitors, visitor_daily_cap=settings.visitor_codes_daily_cap,
    )
    app.state.auth_service = auth_service
    lead_book = LeadBook(store, clock=clock)
    app.state.lead_book = lead_book
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        expose_headers=["X-Studio-Generation"],
    )

    @app.middleware("http")
    async def no_store_tokens(request: Request, call_next):
        # Refuse noncanonical paths before CORS or request-triggered cleanup.
        # A rejected slash variant must not become an unauthenticated cleanup
        # trigger (or a successful OPTIONS response for a nonexistent route).
        if request.url.path.endswith("/"):
            return JSONResponse(
                {"detail": "Not Found"}, status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        # Readiness probes are read-only. Canonical traffic, the background
        # sweeper and authenticated maintenance retain the cleanup backstops.
        if request.url.path not in {
            "/health", "/healthz", "/v1/maintenance/voice-sweep",
        }:
            await sweep_due_calls()
        response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_origin(request: Request) -> str:
        origin = (request.headers.get("origin") or "").rstrip("/")
        if origin not in settings.allowed_origins:
            raise HTTPException(403, "origin is not allowed")
        return origin

    def require_session(request: Request, session_id: str, scope: str = "session") -> dict:
        auth = request.headers.get("authorization") or ""
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        try:
            claims = verify_token(token, settings.session_secret, now=clock(), scope=scope)
        except InvalidToken as exc:
            raise HTTPException(401, str(exc)) from exc
        if claims["sid"] != session_id:
            raise HTTPException(403, "token does not belong to this session")
        return claims

    def caller_address(request: Request) -> str:
        """The address Google's front end observed: the LAST X-Forwarded-For hop.
        Earlier hops are whatever the client sent and are never trusted. Only a
        valid IP counts; anything else becomes "" and shares one fail-closed
        bucket. Only visitors are limited by it, and only its keyed hash is stored."""
        values = request.headers.getlist("x-forwarded-for") if hasattr(request.headers, "getlist") else []
        hops = [hop.strip() for value in values for hop in value.split(",") if hop.strip()]
        candidate = hops[-1] if hops else (request.client.host if request.client else "")
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return ""

    def require_signed_in(request: Request) -> tuple[dict, str]:
        """An operator, or - only while public visitors are on - a verified visitor."""
        auth = request.headers.get("authorization") or ""
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        try:
            return verify_token(token, settings.session_secret, now=clock(), scope="operator"), "operator"
        except InvalidToken as operator_error:
            if not settings.public_visitors:
                raise HTTPException(401, str(operator_error)) from operator_error
        try:
            return verify_token(token, settings.session_secret, now=clock(), scope="visitor"), "visitor"
        except InvalidToken as exc:
            raise HTTPException(401, str(exc)) from exc

    def require_operator(request: Request) -> dict:
        auth = request.headers.get("authorization") or ""
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        try:
            return verify_token(
                token, settings.session_secret, now=clock(), scope="operator"
            )
        except InvalidToken as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.get("/health")
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        backend = "gcs" if settings.state_uri.startswith("gs://") else "file"
        return {
            "ok": True,
            "worker": settings.worker,
            "state_backend": backend,
            "features": {"voice": settings.voice_enabled, "lead_facts": settings.lead_facts_enabled,
                         "talk": bool(talk_agents()), "agents": talk_agents(),
                         "analyst": analyst_ready(), "muse": muse_ready(), "topics": True,
                         "public_visitors": settings.public_visitors,
                         "governance": True,
                         "voices": voices_available(),
                         "rating": True, "summary_email": settings.summary_email_enabled,
                         "routing": True, "advisor": advisor_lane.ready()},
        }

    # THE TALK LANE. A spoken reply to what the visitor just said, in about a
    # second, while the same words go to the builder as an utterance command.
    # It reads the session (never writes it), so it cannot contend with the
    # builder's one in-flight command. Per-session count is in memory: the
    # session is operator-only and lasts at most max_session_seconds.
    talk_counts: dict = {}
    talk_last: dict = {}
    TALK_SPACING_SECONDS = 1.5

    def talk_lane():
        nonlocal talk_client
        if talk_client is None:
            from workers.talk import TalkClient
            talk_client = TalkClient(
                anthropic_ready=settings.worker == "claude",
                openai_key=settings.openai_api_key,
            )
        return talk_client

    def talk_agents() -> list:
        return list(talk_lane().agents())

    # Two voices (owner direction): the host is the realtime call; the architect
    # speaks the builder's and analyst's lines in its own OpenAI TTS voice.
    speak_counts: dict = {}
    recap_counts: dict = {}
    TTS_URL = "https://api.openai.com/v1/audio/speech"
    TTS_MODEL = "gpt-4o-mini-tts"
    ARCHITECT_STYLE = ARCHITECT_VOICE_STYLE
    SPEAK_MAX = 400

    def live_state(session_id: str) -> dict:
        try:
            state = controller.repository.load(session_id).state
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        if state.get("stopped") or int(state.get("expires_at") or 0) <= int(clock()):
            raise HTTPException(410, "session has ended")
        return state

    def spend(counts: dict, session_id: str, cap: int, what: str) -> None:
        used = counts.get(session_id, 0)
        if used >= cap:
            raise HTTPException(429, "%s limit reached for this session" % what)
        counts[session_id] = used + 1
        if len(counts) > 500:
            for stale in list(counts)[:250]:
                counts.pop(stale, None)

    # The analyst lane: a second agent beside the builder (workers/analyst.py).
    analyze_counts: dict = {}
    analyze_last: dict = {}
    analyze_busy: set = set()
    ANALYZE_SPACING_SECONDS = 3.0

    def analyst_ready() -> bool:
        return analyst is not None or settings.worker == "claude"

    def analyst_lane():
        nonlocal analyst
        if analyst is None:
            from workers.analyst import Analyst
            analyst = Analyst()
        return analyst

    # THE USE-POLICY GATE (app/governance.py). Visitor text is checked before
    # any lane runs; a flagged line runs none of them. Flags live in their own
    # store object, never in the session record, so counting one can never make
    # an in-flight build's compare-and-set save fail.
    moderator = governance.Moderator(
        lambda: moderation_client or app.state.voice_client, settings.openai_api_key,
        enabled=settings.moderation_enabled, clock=clock)
    policy_book = governance.PolicyBook(store, clock=clock)
    app.state.moderator = moderator
    app.state.policy_book = policy_book

    async def end_for_policy(session_id: str) -> None:
        stop = {"command_id": "policy-stop-" + secrets.token_hex(6), "session_id": session_id,
                "type": "stop", "expected_version": 0}
        try:
            await asyncio.to_thread(controller.stop_session, session_id, stop,
                                    reason=governance.POLICY_END_REASON)
        except CommandError:
            pass                                   # already stopped
        except (StateConflict, SessionNotFound) as exc:
            governance.log_event("studio.policy_stop_failed", session_id=session_id,
                                 reason=type(exc).__name__)
            return
        await end_voice_call(session_id, "use policy", force=True)

    async def policy_gate(session_id: str, lane: str, texts: list, *, count: bool = True) -> str:
        """"" when the words may go on; "refused"; or "ended" once this flag stopped the session."""
        verdict = await moderator.check(texts)
        if not verdict.available:
            # Fail open: the lanes' own use policy and the providers' safeguards
            # still apply to this turn; the outage is counted and logged.
            governance.log_event("studio.moderation_unavailable", session_id=session_id, lane=lane,
                                 reason=verdict.reason)
            return ""
        if not verdict.flagged:
            return ""
        if not count:
            governance.log_event("studio.policy_refused", session_id=session_id, lane=lane,
                                 categories=list(verdict.categories))
            return "refused"
        try:
            total = await asyncio.to_thread(policy_book.record, session_id, lane, verdict.categories)
        except Exception as exc:                   # the refusal stands without the count
            governance.log_event("studio.policy_count_failed", session_id=session_id, lane=lane,
                                 reason=type(exc).__name__)
            return "refused"
        governance.log_event("studio.policy_flag", session_id=session_id, lane=lane,
                             categories=list(verdict.categories), count=total)
        if total < governance.FLAG_LIMIT:
            return "refused"
        await end_for_policy(session_id)
        return "ended"
    # The Muse (workers/muse.py): a third agent that sparks ideas and offers
    # three directions to choose from. Its directions need Anthropic (the same
    # readiness as the builder); its voice needs OpenAI TTS like the architect's,
    # so /health lists "muse" among the voices only when both are there.
    # The Gemini advisor (workers/advisor.py, plan R3): a second perspective on
    # the committed canvas. It advises, never builds: its answer is bound to the
    # revision it read and dropped if the canvas moves during the call. Nothing
    # of it is committed; a tapped suggestion goes to the builder like speech.
    from workers.advisor import Advisor, fenced as advice_fenced
    advisor_lane = advisor if advisor is not None else Advisor(enabled=settings.advisor_enabled)
    advise_busy: set = set()

    def advisor_snapshot_marker(state: dict) -> tuple:
        """Fence advice to the complete committed moment, not only its canvas.

        A visitor turn may change the transcript without changing the artifact
        version.  Binding only to artifact_version would then allow advice for
        the previous words to be returned as current.  These controller-owned
        monotonic fields conservatively invalidate advice after any committed
        turn/event or lifecycle change while the provider call is in flight.
        The command and voice epochs advance on every durable lifecycle write,
        closing reserve-then-clear and open-then-remove ABA windows. The current
        active-command and canonical voice-call values remain defense in depth
        for legacy records and independently catch one-way state changes.
        """
        voice_marker = json.dumps(
            state.get("voice_call") or {},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (
            int(state.get("generation") or 0),
            int(state.get("task_revision") or 0),
            int(state.get("artifact_version") or 0),
            int(state.get("turn_seq") or 0),
            int(state.get("last_seq") or 0),
            int(state.get("command_epoch") or 0),
            int(state.get("voice_epoch") or 0),
            str(state.get("active_command") or ""),
            voice_marker,
            bool(state.get("paused")),
            bool(state.get("stopped")),
        )

    def advice_texts(advice: dict) -> list:
        """Every string the page would show: what the moderation gate reads."""
        texts = [advice.get("perspective") or ""]
        for q in advice.get("questions") or []:
            texts += [q.get("prompt") or "", q.get("why") or ""]
            texts += [o.get("label") or "" for o in q.get("options") or []]
        texts += list(advice.get("risks") or [])
        return [t for t in texts if isinstance(t, str) and t]

    def advisor_done(session_id: str, started: float, outcome: str, **extra) -> dict:
        """One content-free telemetry line per call (Codex #266: usage and
        circuit telemetry): the outcome and how long it took, never words."""
        governance.log_event("studio.advisor_call", session_id=session_id, outcome=outcome,
                             latency_ms=int((time.monotonic() - started) * 1000),
                             severity="WARNING" if outcome == "withheld" else "INFO", **extra)
        return {"advice": None, outcome: True} if outcome in ("fenced", "withheld") else {"advice": None}

    @app.post("/v1/session/{session_id}/advise")
    async def advise(request: Request, session_id: str):
        require_origin(request)
        require_session(request, session_id)
        if not advisor_lane.ready():
            raise HTTPException(503, "the advisor is not available")
        body = await json_object(request, "advise")
        if set(body) - {"revision"}:
            raise HTTPException(400, "advise body has unknown fields")
        revision = body.get("revision")
        if type(revision) is not int or revision < 0:
            raise HTTPException(400, "advise needs the canvas revision")
        from workers.talk import canvas_summary
        from workers.topics import topic_line
        state = await asyncio.to_thread(live_state, session_id)
        if int(state.get("artifact_version") or 0) != revision:
            raise HTTPException(409, "the canvas has moved on")
        snapshot_marker = advisor_snapshot_marker(state)
        if session_id in advise_busy:
            raise HTTPException(409, "advice is already being prepared for this session")
        said = [str(t.get("text") or "") for t in state.get("transcript") or []
                if isinstance(t, dict) and t.get("role") == "visitor" and t.get("text")][-4:]
        snapshot = {"revision": revision, "topic_line": topic_line(state),
                    "canvas": canvas_summary(state.get("artifact")), "said": said}
        advise_busy.add(session_id)
        started = time.monotonic()
        try:
            advice = await asyncio.to_thread(advisor_lane.advise, session_id, snapshot)
        finally:
            advise_busy.discard(session_id)
        if advice is None:
            return advisor_done(session_id, started, "none")
        try:
            latest = await asyncio.to_thread(live_state, session_id)
        except HTTPException:
            return advisor_done(session_id, started, "fenced")
        if (advisor_snapshot_marker(latest) != snapshot_marker
                or advice_fenced(advice, int(latest.get("artifact_version") or 0))):
            return advisor_done(session_id, started, "fenced")
        # The advisor's own words are moderated before the page sees them. The
        # lane is optional, so it fails CLOSED: flagged, or moderation not
        # available, and the advice is withheld (not counted against the visitor).
        verdict = await moderator.check(advice_texts(advice))
        if verdict.flagged or not verdict.available:
            return advisor_done(session_id, started, "withheld",
                                reason="flagged" if verdict.flagged else "moderation_unavailable",
                                categories=list(verdict.categories))
        advisor_done(session_id, started, "advice")
        return {"advice": advice}

    muse_counts: dict = {}
    MUSE_STYLE = ("A curious, imaginative creative director: bright, playful and thought-provoking, "
                  "with wonder in the voice and a lively pace.")

    def muse_ready() -> bool:
        return muse is not None or settings.worker == "claude"

    def muse_lane():
        nonlocal muse
        if muse is None:
            from workers.muse import Muse
            muse = Muse()
        return muse

    def voices_available() -> list:
        if not (settings.voice_enabled and settings.openai_api_key):
            return []
        return ["host", "architect"] + (["muse"] if muse_ready() else [])

    async def json_object(request: Request, label: str) -> dict:
        raw = await request.body()
        if len(raw) > 65536:
            raise HTTPException(413, label + " body exceeds 64 KiB")
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, label + " body must be JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, label + " body must be an object")
        return body

    # THE END CARD. After a conversation the visitor says how happy they are
    # and can have the working session sent to them as a PDF. Both endpoints
    # accept the session token for END_CARD_GRACE_SECONDS after the session
    # expired (a time-limit end must still count); no other endpoint does.
    END_CARD_GRACE_SECONDS = 30 * 60
    SUMMARY_BODY_MAX = 2_100_000
    SUMMARY_STALE_SECONDS = 120
    SUMMARY_MARK_ATTEMPTS = 3
    SUMMARY_UNCONFIRMED = "the summary may already have been sent; check your inbox"
    RATING_COMMENT_MAX = 300

    async def capped_body(request: Request, cap: int) -> bytes:
        """The request body, refused with 413 as soon as it is known to exceed
        `cap`: from Content-Length before anything is read, else while it
        streams in."""
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError as exc:
                raise HTTPException(400, "Content-Length must be an integer") from exc
            if length > cap:
                raise HTTPException(413, "request body is too large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > cap:
                raise HTTPException(413, "request body is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def require_recent_session(request: Request, session_id: str) -> dict:
        auth = request.headers.get("authorization") or ""
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        try:
            claims = verify_token(token, settings.session_secret,
                                  now=clock() - END_CARD_GRACE_SECONDS, scope="session")
        except InvalidToken as exc:
            raise HTTPException(401, str(exc)) from exc
        if claims["sid"] != session_id:
            raise HTTPException(403, "token does not belong to this session")
        return claims

    def within_grace(state: dict) -> None:
        if int(state.get("expires_at") or 0) + END_CARD_GRACE_SECONDS <= int(clock()):
            raise HTTPException(410, "this session closed more than 30 minutes ago")

    def recent_state(session_id: str) -> dict:
        try:
            state = repository.load(session_id).state
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        within_grace(state)
        return state

    def update_state(session_id: str, change) -> dict:
        """Compare-and-set one change to the session record. `change` gets a
        copy and returns the new state, or None to write nothing."""
        for _ in range(repository.attempts):
            record = repository.load(session_id)
            candidate = change(copy.deepcopy(record.state))
            if candidate is None:
                return record.state
            try:
                repository.save(session_id, candidate, record.token)
                return candidate
            except StateConflict:
                continue
        raise StateConflict("session is busy; try again")

    def contact_name(subject: str) -> str:
        return "studio_contact_" + subject

    def record_contact(subject: str, email: str) -> None:
        """The verified address behind a sign-in subject, kept server-side only
        for the summary email. Never returned to a page, never logged."""
        name = contact_name(subject)
        for _ in range(4):
            current, token = store.load(name)
            if isinstance(current, dict) and current.get("email") == email:
                return
            try:
                store.save(name, {"version": 1, "email": email, "verified_at": int(clock())}, token)
                return
            except Conflict:
                continue

    def email_for_subject(subject: str) -> str:
        """The session owner's verified address, from server records only: the
        contact written at sign-in (checked against the subject it claims), or
        the operator allowlist. Never from the request."""
        if not subject:
            return ""
        record, _ = store.load(contact_name(subject))
        email = record.get("email") if isinstance(record, dict) else ""
        if isinstance(email, str) and email and hmac.compare_digest(
                auth_service._subject_hash(email), subject):
            return email
        for candidate in settings.operator_emails:
            if hmac.compare_digest(auth_service._subject_hash(candidate), subject):
                return candidate
        return ""

    @app.post("/v1/auth/start")
    async def auth_start(request: Request):
        require_origin(request)
        body = await json_object(request, "authentication")
        if set(body) != {"email", "client_key"}:
            raise HTTPException(400, "authentication body requires email and client_key")
        client_ip = caller_address(request)
        return await asyncio.to_thread(
            auth_service.start, body.get("email"), body.get("client_key"), client_ip
        )

    @app.post("/v1/auth/verify")
    async def auth_verify(request: Request):
        require_origin(request)
        body = await json_object(request, "verification")
        required = {"challenge_id", "email", "code", "client_key"}
        if set(body) != required:
            raise HTTPException(400, "verification body is incomplete")
        verified = await asyncio.to_thread(
            auth_service.verify,
            body.get("challenge_id"), body.get("email"), body.get("code"), body.get("client_key"),
        )
        if not verified.get("verified"):
            raise HTTPException(401, "verification code was not accepted")
        visitor = verified.get("role") == "visitor"
        # Switched off after the code was sent: the code proves the mailbox,
        # but public visitors are no longer admitted.
        if visitor and not settings.public_visitors:
            raise HTTPException(401, "verification code was not accepted")
        if settings.summary_email_enabled:
            try:
                await asyncio.to_thread(record_contact, verified["subject_hash"], body["email"])
            except Exception:
                pass  # sign-in never fails on this; the summary falls back to the allowlist
        if visitor:
            expires_at = int(clock()) + settings.visitor_token_seconds
            token = mint_token(verified["subject_hash"], expires_at, settings.session_secret, scope="visitor")
            await asyncio.to_thread(lead_book.record_verified, verified["subject_hash"], body.get("email"))
            return {"token": token, "expires_at": expires_at, "scope": "visitor"}
        expires_at = int(clock()) + settings.operator_token_seconds
        token = mint_token(
            verified["subject_hash"], expires_at, settings.session_secret, scope="operator"
        )
        return {"token": token, "expires_at": expires_at, "scope": "operator"}

    @app.post("/v1/session")
    async def create_session(request: Request):
        require_origin(request)
        operator, role = require_signed_in(request)
        body = await json_object(request, "session")
        if set(body) - {"title", "creation_id", "start", "topic"}:
            raise HTTPException(400, "session body has unknown fields")
        from workers.topics import TOPICS
        topic = body.get("topic")
        if topic is None:                          # omitted or null: no topic
            topic = ""
        # false, 0, [] and {} are not "no topic": every non-string is a 400.
        if not isinstance(topic, str) or (topic and topic not in TOPICS):
            raise HTTPException(400, "topic must be one of: %s" % ", ".join(sorted(TOPICS)))
        start = body.get("start") or "template"
        if start not in ("template", "blank"):
            raise HTTPException(400, "start must be template or blank")
        creation_id = body.get("creation_id")
        if not isinstance(creation_id, str) or not creation_id:
            raise HTTPException(400, "session requires creation_id")
        title = body.get("title") or "Live prototype"
        if not isinstance(title, str):
            raise HTTPException(400, "title must be a string")
        visitor = role == "visitor"
        if visitor and await asyncio.to_thread(
                lead_book.remaining_today, operator["sid"], settings.visitor_sessions_per_day) <= 0:
            raise HTTPException(429, "the conversations for today are used up")
        try:
            state, admitted = await asyncio.to_thread(
                controller.create_session, title[:600], operator["sid"], creation_id, start, visitor,
                settings.daily_session_cap - settings.operator_reserved_sessions if visitor else None,
                start == "blank" and analyst_ready(),
                topic,
            )
        except CommandError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except StateConflict as exc:
            status = 429 if "capacity" in str(exc) else 503
            raise HTTPException(status, str(exc)) from exc
        if visitor:
            try:
                await asyncio.to_thread(lead_book.admit_session, operator["sid"], state["session_id"],
                                        title, settings.visitor_sessions_per_day)
            except LeadCapExceeded as exc:
                raise HTTPException(429, "the conversations for today are used up") from exc
        token = mint_token(state["session_id"], state["expires_at"], settings.session_secret)
        return {
            "session_id": state["session_id"],
            "generation": state["generation"],
            "artifact_version": state["artifact_version"],
            "expires_at": state["expires_at"],
            "max_session_seconds": settings.max_session_seconds,
            "daily_admission_number": admitted,
            "token": token,
            "events_url": "/v1/session/%s/events" % state["session_id"],
        }

    @app.get("/v1/session/{session_id}/events")
    async def events(request: Request, session_id: str,
                     once: bool = Query(False),
                     last_event_id: str | None = Header(None, alias="Last-Event-ID")):
        require_origin(request)
        require_session(request, session_id)
        try:
            after = int(last_event_id or 0)
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be an integer") from exc

        # StreamingResponse commits HTTP 200 before iterating its body. Resolve
        # session/replay/repair admission first so failures are real JSON HTTP
        # refusals, not empty successful streams. Reuse this batch below: a
        # second read could discard the initial repair snapshot or race it.
        try:
            initial = await asyncio.to_thread(controller.events_after, session_id, after)
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc

        async def stream():
            nonlocal after
            deadline = clock() + (0 if once else settings.sse_wait_seconds)
            batch, repaired, state = initial
            while True:
                if repaired:
                    after = 0
                for event in batch:
                    yield _sse(event)
                    after = int(event["seq"])
                if once or clock() >= deadline or state.get("stopped"):
                    break
                if await request.is_disconnected():
                    break
                if not batch:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(settings.sse_poll_seconds)
                try:
                    batch, repaired, state = await asyncio.to_thread(
                        controller.events_after, session_id, after
                    )
                except (SessionNotFound, StateConflict):
                    # Headers cannot change now. Close this bounded stream;
                    # reconnection runs preflight and receives a real refusal
                    # if the problem persists. Do not forge durable events or
                    # advance the last delivered cursor for a transport error.
                    return

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
            "X-Studio-Generation": str(initial[2]["generation"]),
        })

    @app.post("/v1/session/{session_id}/talk")
    async def talk(request: Request, session_id: str):
        require_origin(request)
        require_session(request, session_id)
        agents = talk_agents()
        if not agents:
            raise HTTPException(503, "talk is not available")
        from workers.talk import TEXT_MAX, canvas_summary, clean_history
        from workers.topics import with_topic
        body = await json_object(request, "talk")
        if set(body) - {"text", "history", "agent", "turn"}:
            raise HTTPException(400, "talk body has unknown fields")
        # The page's own turn counter, echoed back, so a reply that resolves
        # after the visitor has already started a newer turn can be dropped
        # before it is spoken (Gemini's review of the talk contract).
        turn = body.get("turn", 0)
        if type(turn) is not int or not 0 <= turn <= 1_000_000:
            raise HTTPException(400, "turn must be a non-negative integer")
        # Without an agent, the session's topic picks it (workers/topics.py);
        # an explicit one from an older page is still honoured and validated.
        agent = body.get("agent")
        if agent is not None and agent not in agents:
            # An unconfigured provider is reported precisely, never simulated.
            raise HTTPException(400, "agent %r is not configured; available: %s"
                                % (str(agent)[:20], ", ".join(agents)))
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
            raise HTTPException(400, "talk needs text of 1 to %d characters" % TEXT_MAX)
        try:
            history = clean_history(body.get("history"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            record = await asyncio.to_thread(controller.repository.load, session_id)
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        state = record.state
        if state.get("stopped") or int(state.get("expires_at") or 0) <= int(clock()):
            raise HTTPException(410, "session has ended")
        if agent is None:
            from workers.topics import route_agent
            agent = route_agent(state.get("topic"), agents)
        used = talk_counts.get(session_id, 0)
        if used >= settings.talk_cap:
            raise HTTPException(429, "talk limit reached for this session")
        # Gemini's review of the talk lane: a hard total per session and a
        # minimum spacing, both on the server, so no client can run up spend.
        now = clock()
        last = talk_last.get(session_id)
        if last is not None and now - last < TALK_SPACING_SECONDS:
            raise HTTPException(429, "talk is limited to one turn every %.1f seconds" % TALK_SPACING_SECONDS)
        talk_counts[session_id] = used + 1
        talk_last[session_id] = now
        if len(talk_counts) > 500:
            for stale in list(talk_counts)[:250]:
                talk_counts.pop(stale, None)
                talk_last.pop(stale, None)
        outcome = await policy_gate(session_id, "talk", [text] + governance.history_texts(history))
        if outcome:
            refused = {"reply": governance.POLICY_END_LINE if outcome == "ended" else governance.POLICY_LINE,
                       "speaker": agent, "turn": turn, "refused": True}
            if outcome == "ended":
                refused["ended"] = True
            return refused
        try:
            reply = await asyncio.to_thread(
                talk_lane().reply, agent, text.strip(), history, with_topic(state, canvas_summary(state.get("artifact"))))
        except Exception as exc:  # the builder still has the words; talking is best-effort
            raise HTTPException(503, "talk is unavailable right now") from exc
        if not reply:
            raise HTTPException(503, "talk returned nothing")
        return {"reply": reply, "speaker": agent, "turn": turn}

    @app.post("/v1/session/{session_id}/speak")
    async def speak(request: Request, session_id: str):
        """One line as audio, in the architect's voice or the Muse's.

        The architect speaks text the page was sent by the builder or the
        analyst. The Muse speaks either such text, or - with "direction" and
        no text - the headline and line of one of its STORED directions, in
        that direction's stored tone: the "hear" preview. The page never
        supplies the instructions the voice is given; nothing is generated here.
        """
        require_origin(request)
        require_session(request, session_id)
        if not (settings.voice_enabled and settings.openai_api_key):
            raise HTTPException(503, "voice is not enabled in this release")
        body = await json_object(request, "speak")
        if set(body) - {"text", "voice", "direction"}:
            raise HTTPException(400, "speak body has unknown fields")
        who = body.get("voice", "architect")
        if who not in ("architect", "muse"):
            raise HTTPException(400, "voice must be architect or muse")
        if who == "muse" and not muse_ready():
            raise HTTPException(503, "the muse is not available")
        if "direction" in body:
            if who != "muse":
                raise HTTPException(400, "a direction is spoken only by the muse")
            if "text" in body:
                raise HTTPException(400, "send a direction or text, not both")
            direction = body.get("direction")
            if direction not in ("a", "b", "c"):
                raise HTTPException(400, "direction must be a, b or c")
            state = await asyncio.to_thread(live_state, session_id)
            stored = next((d for d in ((state.get("muse") or {}).get("directions") or [])
                           if isinstance(d, dict) and d.get("id") == direction), None)
            if stored is None:
                raise HTTPException(400, "there is no direction %r to speak" % direction)
            from workers.muse import plain_text
            try:
                headline = stored["read"]["headline"].strip()
                line, tone = stored["read"]["line"].strip(), stored["hear"]["tone"].strip()
            except (KeyError, TypeError, AttributeError) as exc:
                raise HTTPException(400, "there is no direction %r to speak" % direction) from exc
            # Checked again here, not only when the set was stored: nothing that
            # is not one line of plain text reaches the voice provider.
            if not all(plain_text(v) and v for v in (headline, line, tone)):
                raise HTTPException(400, "there is no direction %r to speak" % direction)
            joiner = " " if headline.endswith((".", "!", "?")) else ". "
            text = headline + joiner + line
            instructions = MUSE_STYLE + " Deliver this line in this tone: " + tone
        else:
            text = body.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > SPEAK_MAX:
                raise HTTPException(400, "speak needs text of 1 to %d characters" % SPEAK_MAX)
            await asyncio.to_thread(live_state, session_id)
            instructions = MUSE_STYLE if who == "muse" else ARCHITECT_STYLE
        spend(speak_counts, session_id, settings.speak_cap, "speech")
        if await policy_gate(session_id, "speak", [text], count=False):
            raise HTTPException(422, governance.POLICY_PROBLEM)
        voice_name = settings.muse_voice if who == "muse" else settings.architect_voice
        unavailable = "the muse voice is unavailable right now" if who == "muse" \
            else "the architect voice is unavailable right now"
        try:
            response = await request.app.state.voice_client.post(
                TTS_URL,
                headers={"Authorization": "Bearer " + settings.openai_api_key},
                json={"model": TTS_MODEL, "voice": voice_name, "input": " ".join(text.split()),
                      "instructions": instructions, "response_format": "mp3"},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(503, unavailable) from exc
        if response.status_code != 200 or not getattr(response, "content", b""):
            raise HTTPException(503, unavailable)
        return Response(content=response.content, media_type="audio/mpeg",
                        headers={"Cache-Control": "no-store"})

    @app.post("/v1/session/{session_id}/inspire")
    async def inspire(request: Request, session_id: str):
        """The Muse: one spark and three directions to choose from.

        The text is what the visitor last said; without it the Muse inspires
        from what is on the canvas. The set is checked in workers/muse.py (one
        repair attempt), then stored on the session by compare-and-set without
        disturbing a build, so a spoken preview can only ever read it back.
        """
        require_origin(request)
        require_session(request, session_id)
        if not muse_ready():
            raise HTTPException(503, "the muse is not available")
        from workers.talk import TEXT_MAX, canvas_summary
        from workers.topics import with_topic
        body = await json_object(request, "inspire")
        if set(body) - {"text", "turn"}:
            raise HTTPException(400, "inspire body has unknown fields")
        turn = body.get("turn", 0)
        if type(turn) is not int or not 0 <= turn <= 1_000_000:
            raise HTTPException(400, "turn must be a non-negative integer")
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > TEXT_MAX:
            raise HTTPException(400, "inspire text must be a string of at most %d characters" % TEXT_MAX)
        state = await asyncio.to_thread(live_state, session_id)
        if text.strip():
            # The same use-policy gate as talk, commands and analyze: a flagged
            # line wakes no Muse and spends no inspiration.
            outcome = await policy_gate(session_id, "muse", [text])
            if outcome:
                return {"turn": turn, "muse": None, "refused": True, "ended": outcome == "ended"}
        spend(muse_counts, session_id, settings.muse_cap, "inspiration")
        try:
            result = await asyncio.to_thread(
                muse_lane().inspire, state, (text or "").strip(), with_topic(state, canvas_summary(state.get("artifact"))))
        except Exception as exc:  # the builder, the host and the analyst carry on without it
            raise HTTPException(503, "the muse could not answer") from exc
        try:
            await asyncio.to_thread(controller.commit_muse, session_id, result["muse"])
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        except CommandError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"turn": turn, "muse": result["muse"]}

    @app.post("/v1/session/{session_id}/recap")
    async def recap(request: Request, session_id: str):
        """The host closes the meeting: a short recap from the session record."""
        require_origin(request)
        require_session(request, session_id)
        agents = talk_agents()
        if not agents:
            raise HTTPException(503, "the host is not available")
        body = await json_object(request, "recap")
        if set(body) - {"agent"}:
            raise HTTPException(400, "recap body has unknown fields")
        agent = body.get("agent")
        if agent is not None and agent not in agents:
            raise HTTPException(400, "agent %r is not configured; available: %s" % (str(agent)[:20], ", ".join(agents)))
        state = await asyncio.to_thread(live_state, session_id)
        if agent is None:
            from workers.topics import route_agent
            agent = route_agent(state.get("topic"), agents)
        spend(recap_counts, session_id, settings.recap_cap, "recap")
        from workers.talk import canvas_summary, recap_brief
        from workers.topics import with_topic
        try:
            text = await asyncio.to_thread(
                talk_lane().recap, agent, recap_brief(state, with_topic(state, canvas_summary(state.get("artifact")))))
        except Exception as exc:
            raise HTTPException(503, "the recap is unavailable right now") from exc
        if not text:
            raise HTTPException(503, "the recap came back empty")
        if state.get("visitor_subject"):
            # The recap is what the visitor was told they would get back: keep it with their lead.
            await asyncio.to_thread(lead_book.record_recap, state["visitor_subject"], session_id, text)
        kept_at = int(clock())

        def keep(current):
            current["recap"] = {"text": text[:1200], "at": kept_at, "speaker": agent}
            return current

        try:
            await asyncio.to_thread(update_state, session_id, keep)
        except (StateConflict, SessionNotFound):
            pass  # the recap is still spoken; only the summary PDF goes without it
        return {"recap": text, "speaker": agent}

    @app.get("/v1/leads")
    async def leads(request: Request):
        """Verified public visitors, newest first. Operators only."""
        require_origin(request)
        require_operator(request)
        return {"leads": await asyncio.to_thread(lead_book.list, 50)}

    @app.post("/v1/session/{session_id}/rating")
    async def rating(request: Request, session_id: str):
        """How happy the visitor is with the outcome: 1 to 5, and a few words."""
        require_origin(request)
        require_recent_session(request, session_id)
        body = await json_object(request, "rating")
        if set(body) - {"score", "comment"}:
            raise HTTPException(400, "rating body has unknown fields")
        score = body.get("score")
        if type(score) is not int or not 1 <= score <= 5:
            raise HTTPException(400, "score must be a whole number from 1 to 5")
        comment = body.get("comment")
        if comment is None:
            comment = ""
        if not isinstance(comment, str) or len(comment) > RATING_COMMENT_MAX:
            raise HTTPException(400, "comment must be text of at most %d characters" % RATING_COMMENT_MAX)
        now = int(clock())

        def rate(current):
            within_grace(current)
            current["rating"] = {"score": score, "comment": comment.strip(), "at": now}
            return current

        try:
            await asyncio.to_thread(update_state, session_id, rate)
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/v1/session/{session_id}/summary")
    async def summary(request: Request, session_id: str):
        """Email the working session as a PDF to the owner's verified address.
        Once per session: a replay answers from the record and sends nothing."""
        require_origin(request)
        require_recent_session(request, session_id)
        if not settings.summary_email_enabled:
            raise HTTPException(503, "the summary email is not enabled in this release")
        raw = await capped_body(request, SUMMARY_BODY_MAX)
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "summary body must be JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "summary body must be an object")
        if set(body) - {"design_png"}:
            raise HTTPException(400, "summary body has unknown fields")
        try:
            design = decode_design_png(body.get("design_png"))
        except DesignImageError as exc:
            raise HTTPException(400, str(exc)) from exc
        state = await asyncio.to_thread(recent_state, session_id)
        done = state.get("summary") or {}
        if done.get("status") == "sent":
            return {"sent": True, "to": done.get("to", "")}
        subject = state.get("operator_subject") or state.get("visitor_subject") or ""
        email = await asyncio.to_thread(email_for_subject, subject)
        if not email:
            raise HTTPException(409, "no verified email is on record for this session")
        try:
            pdf = await asyncio.to_thread(build_summary_pdf, state, design_png=design)
        except DesignImageError as exc:
            raise HTTPException(400, str(exc)) from exc
        masked = mask_email(email)
        now = int(clock())
        reservation = secrets.token_hex(8)
        outcome: dict = {}

        # ONE EMAIL PER SESSION, EVER. A duplicate is worse than a missing
        # email: anything that may have been delivered is "unconfirmed" and is
        # never sent again, including a reservation whose request outlived
        # SUMMARY_STALE_SECONDS. Only a failure that certainly sent nothing
        # ("failed") may be retried.
        def reserve(current):
            within_grace(current)
            prior = current.get("summary") or {}
            status = prior.get("status")
            if status == "sent":
                outcome["replay"] = prior
                return None
            if status == "unconfirmed":
                outcome["unconfirmed"] = True
                return None
            if status == "sending":
                if now - int(prior.get("at") or 0) < SUMMARY_STALE_SECONDS:
                    outcome["busy"] = True
                    return None
                current["summary"] = {"status": "unconfirmed", "at": now, "why": "stale"}
                outcome["unconfirmed"] = True
                return current
            current["summary"] = {"status": "sending", "at": now, "reservation": reservation}
            return current

        try:
            await asyncio.to_thread(update_state, session_id, reserve)
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if outcome.get("replay"):
            return {"sent": True, "to": outcome["replay"].get("to", "")}
        if outcome.get("unconfirmed"):
            raise HTTPException(409, SUMMARY_UNCONFIRMED)
        if outcome.get("busy"):
            raise HTTPException(409, "the summary is already on its way")

        async def settle(change) -> None:
            """Record the outcome, retrying the compare-and-set; a record that
            still says "sending" later turns unconfirmed, never resendable."""
            for _ in range(SUMMARY_MARK_ATTEMPTS):
                try:
                    await asyncio.to_thread(update_state, session_id, change)
                    return
                except StateConflict:
                    continue
                except SessionNotFound:
                    return

        try:
            await asyncio.to_thread(send_summary_email, email, pdf)
        except SummaryNotSent as exc:
            def failed(current):
                prior = current.get("summary") or {}
                if prior.get("status") != "sending" or prior.get("reservation") != reservation:
                    return None
                current["summary"] = {"status": "failed", "at": int(clock())}
                return current

            await settle(failed)
            raise HTTPException(502, "the summary email could not be sent; try again") from exc
        except Exception as exc:
            def unconfirmed(current):
                if (current.get("summary") or {}).get("status") == "sent":
                    return None
                current["summary"] = {"status": "unconfirmed", "at": int(clock())}
                return current

            await settle(unconfirmed)
            raise HTTPException(502, SUMMARY_UNCONFIRMED) from exc

        def sent(current):
            current["summary"] = {"status": "sent", "at": int(clock()), "to": masked}
            return current

        await settle(sent)
        return {"sent": True, "to": masked}

    @app.post("/v1/session/{session_id}/analyze")
    async def analyze(request: Request, session_id: str):
        """Run the analyst on what the visitor just said, beside the builder.

        Read the session, let the analyst research and map the data model and
        the next question, then record them with a compare-and-set commit that
        never waits for (or blocks) a build. Events reach the page over the
        session stream and in this response.
        """
        require_origin(request)
        require_session(request, session_id)
        if not analyst_ready():
            raise HTTPException(503, "the analyst is not available")
        from workers.talk import TEXT_MAX, canvas_summary
        from workers.topics import with_topic
        body = await json_object(request, "analyze")
        if set(body) - {"text", "turn"}:
            raise HTTPException(400, "analyze body has unknown fields")
        turn = body.get("turn", 0)
        if type(turn) is not int or not 0 <= turn <= 1_000_000:
            raise HTTPException(400, "turn must be a non-negative integer")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
            raise HTTPException(400, "analyze needs text of 1 to %d characters" % TEXT_MAX)
        try:
            record = await asyncio.to_thread(controller.repository.load, session_id)
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        state = record.state
        if state.get("stopped") or int(state.get("expires_at") or 0) <= int(clock()):
            raise HTTPException(410, "session has ended")
        if session_id in analyze_busy:
            raise HTTPException(409, "an analysis is already running for this session")
        used = analyze_counts.get(session_id, 0)
        if used >= settings.analyze_cap:
            raise HTTPException(429, "analysis limit reached for this session")
        now = clock()
        last = analyze_last.get(session_id)
        if last is not None and now - last < ANALYZE_SPACING_SECONDS:
            raise HTTPException(429, "analysis is limited to one every %.0f seconds" % ANALYZE_SPACING_SECONDS)
        analyze_counts[session_id] = used + 1
        analyze_last[session_id] = now
        if len(analyze_counts) > 500:
            for stale in list(analyze_counts)[:250]:
                analyze_counts.pop(stale, None)
                analyze_last.pop(stale, None)
        outcome = await policy_gate(session_id, "analyze", [text])
        if outcome == "ended":
            raise HTTPException(410, "session has ended")
        if outcome:
            return {"turn": turn, "model": None, "events": [], "problems": [governance.POLICY_PROBLEM],
                    "searched": 0, "refused": True}
        analyze_busy.add(session_id)
        try:
            try:
                result = await asyncio.to_thread(
                    analyst_lane().analyze, state, text.strip(), with_topic(state, canvas_summary(state.get("artifact"))))
            except Exception as exc:  # the builder and the talk lane carry on without it
                raise HTTPException(503, "the analyst is unavailable right now") from exc
            if not result.get("model"):
                raise HTTPException(503, "; ".join(result.get("problems") or ["no analysis"])[:300])
            try:
                events = await asyncio.to_thread(
                    controller.commit_analysis, session_id, result["model"], result.get("question"))
            except SessionNotFound as exc:
                raise HTTPException(404, "session not found") from exc
            except CommandError as exc:
                raise HTTPException(exc.status, str(exc)) from exc
            except StateConflict as exc:
                raise HTTPException(409, str(exc)) from exc
        finally:
            analyze_busy.discard(session_id)
        return {"turn": turn, "model": result["model"], "events": events,
                "problems": result.get("problems") or [], "searched": result.get("searched", 0)}

    @app.post("/v1/session/{session_id}/commands")
    async def commands(request: Request, session_id: str):
        require_origin(request)
        require_session(request, session_id)
        try:
            command = await json_object(request, "command")
            texts = governance.command_texts(command)
            # Stop is the visitor's emergency brake: it is never gated.
            if texts and moderator.enabled and command.get("type") != "stop":
                state = (await asyncio.to_thread(controller.repository.load, session_id)).state
                if not state.get("stopped") and int(state.get("expires_at") or 0) > int(clock()):
                    outcome = await policy_gate(session_id, "commands", texts)
                    if outcome == "ended":
                        raise HTTPException(410, "session has ended")
                    if outcome:
                        return {"command_id": str(command.get("command_id") or ""), "session_id": session_id,
                                "artifact_version": state.get("artifact_version"), "events": [],
                                "problems": [governance.POLICY_PROBLEM], "refused": True}
            result = await asyncio.to_thread(controller.execute, session_id, command)
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        except CommandError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if command.get("type") == "stop":
            ended = await end_voice_call(session_id, "visitor stopped", force=True)
            if not ended:
                result.setdefault("problems", []).append(
                    "Voice hangup is pending a bounded server retry."
                )
        return result

    @app.post("/v1/session/{session_id}/voice")
    async def voice(request: Request, session_id: str):
        require_origin(request)
        require_session(request, session_id)
        if not settings.voice_enabled:
            raise HTTPException(503, "voice is not enabled in this release")
        if not settings.openai_api_key:
            raise HTTPException(503, "voice is not configured")
        body = await json_object(request, "voice")
        if set(body) != {"sdp"}:
            raise HTTPException(400, "voice body must contain only sdp")
        sdp = body.get("sdp")
        if not isinstance(sdp, str) or not sdp.strip():
            raise HTTPException(400, "voice sdp must be a non-empty string")
        if len(sdp.encode("utf-8")) > 60000:
            raise HTTPException(413, "voice sdp exceeds 60 KiB")
        voice_id = "voice-" + uuid.uuid4().hex
        now = int(clock())
        began = False
        try:
            current = (await asyncio.to_thread(repository.load, session_id)).state
            ends_at = min(int(current["expires_at"]), now + settings.max_session_seconds)
            state = await asyncio.to_thread(controller.begin_voice, session_id, voice_id, ends_at)
            began = True
            await asyncio.to_thread(
                repository.register_voice, session_id, voice_id, ends_at
            )
        except SessionNotFound as exc:
            raise HTTPException(404, "session not found") from exc
        except CommandError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except StateConflict as exc:
            if began:
                # Index registration never completed. Release only this new
                # session opening; do not touch a conflicting legacy/owned
                # index entry that this voice never acquired.
                try:
                    clean = await asyncio.to_thread(
                        controller.fail_voice, session_id, voice_id
                    )
                except (SessionNotFound, StateConflict):
                    clean = False
                if not clean:
                    raise HTTPException(503, "voice index cleanup is pending") from exc
                raise HTTPException(503, "voice index is unavailable") from exc
            raise HTTPException(409, str(exc)) from exc
        if state["expires_at"] <= now or state.get("stopped"):
            await release_unopened_voice(session_id, voice_id)
            raise HTTPException(410, "session is no longer active")
        # A visitor session stops below the daily voice cap; the operator keeps headroom.
        voice_limit = (settings.voice_mint_cap - settings.operator_reserved_voice
                       if state.get("visitor_subject") else settings.voice_mint_cap)
        try:
            reservation = await asyncio.to_thread(
                repository.reserve_voice_open, voice_limit, voice_id
            )
            for _ in range(3):
                latest = (await asyncio.to_thread(repository.load, session_id)).state
                latest_voice = latest.get("voice_call") or {}
                if (
                    latest.get("stopped")
                    or int(latest.get("expires_at") or 0) <= int(clock())
                    or latest_voice.get("voice_id") != voice_id
                    or latest_voice.get("status") != "opening"
                ):
                    raise CommandError("session is no longer active", 410)
                current_day = await asyncio.to_thread(repository.utc_day)
                if reservation.day == current_day:
                    break
                # A prior-day reservation can never authorize a new-day POST.
                # The old reservation remains consumed; reserve this voice ID
                # again in the current UTC-day ledger before provider contact.
                reservation = await asyncio.to_thread(
                    repository.reserve_voice_open,
                    voice_limit,
                    voice_id,
                )
            else:
                raise StateConflict("voice admission UTC day did not stabilize")
        except VoiceCapacityExceeded as exc:
            clean = await release_unopened_voice(session_id, voice_id)
            if not clean:
                raise HTTPException(503, "voice admission cleanup is pending") from exc
            raise HTTPException(429, "daily voice open capacity reached") from exc
        except CommandError as exc:
            await release_unopened_voice(session_id, voice_id)
            raise HTTPException(exc.status, str(exc)) from exc
        except (SessionNotFound, StateConflict) as exc:
            await release_unopened_voice(session_id, voice_id)
            raise HTTPException(503, "voice admission could not be confirmed") from exc
        except Exception as exc:
            await release_unopened_voice(session_id, voice_id)
            raise HTTPException(503, "voice admission is unavailable") from exc
        subject = latest.get("operator_subject") or latest.get("visitor_subject") or session_id
        safety_id = hashlib.sha256(("studio:" + subject).encode()).hexdigest()[:32]
        try:
            response = await request.app.state.voice_client.post(
                "https://api.openai.com/v1/realtime/calls",
                headers={
                    "Authorization": "Bearer " + settings.openai_api_key,
                    "OpenAI-Safety-Identifier": safety_id,
                },
                files={
                    "sdp": (None, sdp, "application/sdp"),
                    "session": (None, json.dumps(voice_config()), "application/json"),
                },
            )
        except httpx.HTTPError as exc:
            # A transport error is ambiguous: OpenAI may have created the call
            # before the connection failed. Keep the one-call admission closed
            # rather than blindly creating a second paid call.
            await asyncio.to_thread(controller.mark_voice_unknown, session_id, voice_id)
            raise HTTPException(502, "voice provider is unavailable") from exc
        if not 200 <= response.status_code < 300:
            # No non-2xx response class is treated as proof that no billable
            # call exists. Retain the owned sweep entry and close this session
            # to retry until a human can reconcile it.
            await asyncio.to_thread(controller.mark_voice_unknown, session_id, voice_id)
            raise HTTPException(502, "voice provider outcome could not be confirmed")
        try:
            call_id = call_id_from_location(response.headers.get("Location") or response.headers.get("location") or "")
        except HTTPException:
            # A successful provider response without a usable call ID is
            # ambiguous: a billable call may exist but cannot be addressed for
            # hangup. Keep this session and index entry closed to retries.
            await asyncio.to_thread(controller.mark_voice_unknown, session_id, voice_id)
            raise
        try:
            await asyncio.to_thread(controller.activate_voice, session_id, voice_id, call_id)
        except Exception as exc:
            reconciled = False
            try:
                hangup = await request.app.state.voice_client.post(
                    "https://api.openai.com/v1/realtime/calls/%s/hangup" % call_id,
                    headers={"Authorization": "Bearer " + settings.openai_api_key},
                )
                reconciled = (
                    200 <= hangup.status_code < 300
                    or hangup.status_code in {404, 409}
                )
            except httpx.HTTPError:
                pass
            if reconciled:
                settled = False
                try:
                    settled = await asyncio.to_thread(
                        controller.reconcile_voice_hangup,
                        session_id,
                        voice_id,
                        call_id,
                        "activation failed after provider hangup",
                    )
                except (SessionNotFound, StateConflict):
                    pass
                if settled:
                    try:
                        await asyncio.to_thread(
                            repository.unregister_voice, session_id, voice_id
                        )
                    except StateConflict:
                        pass
            else:
                await asyncio.to_thread(controller.mark_voice_unknown, session_id, voice_id)
            raise HTTPException(502, "voice activation could not be reconciled") from exc
        return {
            "sdp": response.text,
            "voice_id": voice_id,
            "ends_at": ends_at,
        }

    @app.post("/v1/maintenance/voice-sweep")
    async def maintenance_voice_sweep(request: Request):
        if not settings.voice_enabled:
            raise HTTPException(404, "voice is not enabled")
        auth = request.headers.get("authorization") or ""
        supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, settings.maintenance_secret):
            raise HTTPException(401, "maintenance credential is not valid")
        summary = await sweep_due_calls()
        result = {
            "ok": summary["index_available"] and summary["pending"] == 0,
            "checked_at": int(clock()),
            **summary,
        }
        if not result["ok"]:
            return JSONResponse(result, status_code=503)
        return result

    return app


app = create_app()

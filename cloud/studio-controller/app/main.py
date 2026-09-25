"""FastAPI surface for the durable SFDC24 Studio controller."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
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
    from scripts.state_store import open_store
except ImportError:
    from state_store import open_store

from .core import CommandError, StudioController
from .auth import AuthService
from .settings import Settings
from .state import (
    SessionNotFound,
    StateConflict,
    StudioRepository,
    VoiceCapacityExceeded,
)
from .tokens import InvalidToken, mint_token, verify_token
from .workers.synthetic import SyntheticWorker


CALL_ID_RE = re.compile(r"^rtc_[A-Za-z0-9_-]{1,120}$")


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


def create_app(*, settings: Settings | None = None, store=None, worker=None,
               clock=time.time, id_factory=None, voice_client=None,
               email_sender=None, email_client=None, talk_client=None,
               analyst=None) -> FastAPI:
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
            "instructions": (
                "Speak only the exact controller event text you are given, briefly and warmly. "
                "Never state facts about Salesforce or the visitor's business. Ask one question at a time."
            ),
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
        store, settings.operator_emails, settings.session_secret, send_auth_email, clock=clock
    )
    app.state.auth_service = auth_service
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
                         "analyst": analyst_ready(),
                         "voices": ["host", "architect"] if (settings.voice_enabled and settings.openai_api_key) else []},
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
    ARCHITECT_STYLE = ("A calm, confident solution architect in a client meeting: warm, clear and "
                       "unhurried, with a short pause between ideas.")
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

    @app.post("/v1/auth/start")
    async def auth_start(request: Request):
        require_origin(request)
        body = await json_object(request, "authentication")
        if set(body) != {"email", "client_key"}:
            raise HTTPException(400, "authentication body requires email and client_key")
        return await asyncio.to_thread(
            auth_service.start, body.get("email"), body.get("client_key")
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
        expires_at = int(clock()) + settings.operator_token_seconds
        token = mint_token(
            verified["subject_hash"], expires_at, settings.session_secret, scope="operator"
        )
        return {"token": token, "expires_at": expires_at, "scope": "operator"}

    @app.post("/v1/session")
    async def create_session(request: Request):
        require_origin(request)
        operator = require_operator(request)
        body = await json_object(request, "session")
        if set(body) - {"title", "creation_id", "start"}:
            raise HTTPException(400, "session body has unknown fields")
        start = body.get("start") or "template"
        if start not in ("template", "blank"):
            raise HTTPException(400, "start must be template or blank")
        creation_id = body.get("creation_id")
        if not isinstance(creation_id, str) or not creation_id:
            raise HTTPException(400, "session requires creation_id")
        title = body.get("title") or "Live prototype"
        if not isinstance(title, str):
            raise HTTPException(400, "title must be a string")
        try:
            state, admitted = await asyncio.to_thread(
                controller.create_session, title[:600], operator["sid"], creation_id, start
            )
        except CommandError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except StateConflict as exc:
            status = 429 if "capacity" in str(exc) else 503
            raise HTTPException(status, str(exc)) from exc
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
        body = await json_object(request, "talk")
        if set(body) - {"text", "history", "agent", "turn"}:
            raise HTTPException(400, "talk body has unknown fields")
        # The page's own turn counter, echoed back, so a reply that resolves
        # after the visitor has already started a newer turn can be dropped
        # before it is spoken (Gemini's review of the talk contract).
        turn = body.get("turn", 0)
        if type(turn) is not int or not 0 <= turn <= 1_000_000:
            raise HTTPException(400, "turn must be a non-negative integer")
        agent = body.get("agent") or agents[0]
        if agent not in agents:
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
        try:
            reply = await asyncio.to_thread(
                talk_lane().reply, agent, text.strip(), history, canvas_summary(state.get("artifact")))
        except Exception as exc:  # the builder still has the words; talking is best-effort
            raise HTTPException(503, "talk is unavailable right now") from exc
        if not reply:
            raise HTTPException(503, "talk returned nothing")
        return {"reply": reply, "speaker": agent, "turn": turn}

    @app.post("/v1/session/{session_id}/speak")
    async def speak(request: Request, session_id: str):
        """One line in the architect's voice, as audio. The text is what the page
        was sent by the builder or the analyst; nothing is generated here."""
        require_origin(request)
        require_session(request, session_id)
        if not (settings.voice_enabled and settings.openai_api_key):
            raise HTTPException(503, "voice is not enabled in this release")
        body = await json_object(request, "speak")
        if set(body) - {"text", "voice"}:
            raise HTTPException(400, "speak body has unknown fields")
        if body.get("voice", "architect") != "architect":
            raise HTTPException(400, "voice must be architect")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > SPEAK_MAX:
            raise HTTPException(400, "speak needs text of 1 to %d characters" % SPEAK_MAX)
        await asyncio.to_thread(live_state, session_id)
        spend(speak_counts, session_id, settings.speak_cap, "speech")
        try:
            response = await request.app.state.voice_client.post(
                TTS_URL,
                headers={"Authorization": "Bearer " + settings.openai_api_key},
                json={"model": TTS_MODEL, "voice": settings.architect_voice, "input": " ".join(text.split()),
                      "instructions": ARCHITECT_STYLE, "response_format": "mp3"},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(503, "the architect voice is unavailable right now") from exc
        if response.status_code != 200 or not getattr(response, "content", b""):
            raise HTTPException(503, "the architect voice is unavailable right now")
        return Response(content=response.content, media_type="audio/mpeg",
                        headers={"Cache-Control": "no-store"})

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
        agent = body.get("agent") or agents[0]
        if agent not in agents:
            raise HTTPException(400, "agent %r is not configured; available: %s" % (str(agent)[:20], ", ".join(agents)))
        state = await asyncio.to_thread(live_state, session_id)
        spend(recap_counts, session_id, settings.recap_cap, "recap")
        from workers.talk import canvas_summary, recap_brief
        try:
            text = await asyncio.to_thread(
                talk_lane().recap, agent, recap_brief(state, canvas_summary(state.get("artifact"))))
        except Exception as exc:
            raise HTTPException(503, "the recap is unavailable right now") from exc
        if not text:
            raise HTTPException(503, "the recap came back empty")
        return {"recap": text, "speaker": agent}

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
        analyze_busy.add(session_id)
        try:
            try:
                result = await asyncio.to_thread(
                    analyst_lane().analyze, state, text.strip(), canvas_summary(state.get("artifact")))
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
        try:
            reservation = await asyncio.to_thread(
                repository.reserve_voice_open, settings.voice_mint_cap, voice_id
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
                    settings.voice_mint_cap,
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
        subject = latest.get("operator_subject") or session_id
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

# SFDC24 Studio controller

The controller is the durable, single-writer boundary between `/studio/`, the
prototype workers, and OpenAI Realtime voice. It keeps dialogue and prototype
changes in one ordered session without depending on a particular Cloud Run
instance.

## Guarantees

- exact origin allowlist plus single-use, rate-limited email OTP authentication;
- scoped operator and ten-minute Studio session tokens with no raw email claims;
- 20 idempotent admissions per UTC day under GCS generation compare-and-swap;
- one in-flight command per session and exact-payload `command_id` idempotency;
- strict session, generation, sequence, task-revision, and artifact-version fences;
- bounded event replay plus a new snapshot-first generation for stale clients;
- typed artifact nodes and patch operations only—no model HTML or code;
- optional OpenAI Realtime WebRTC SDP relay; the standard API key, SDP, and
  audio never reach durable session state.

The default worker is the deterministic `synthetic` one. `STUDIO_WORKER=claude`
selects the Claude worker (`cloud/studio-controller/workers/claude_worker.py`,
reviewed in #200; packaged in the image with the `anthropic` SDK by #206). It
needs `ANTHROPIC_API_KEY` (Secret Manager secret of that name, with
`roles/secretmanager.secretAccessor` on that one secret for the service's
runtime account); without it the service fails at startup rather than serving
a worker that cannot answer. A spoken turn that answers the open question is
recorded by the controller with `answer_source: "voice"` after the same checks
as a tap.
Provider workers return event drafts; the controller alone assigns envelopes
and versions.

## Local run

```powershell
python -m pip install -r cloud/studio-controller/requirements.txt
$env:BLACKBOARD_STATE_URI = 'file://C:/temp/sfdc24-studio-state'
$env:STUDIO_SESSION_SECRET = 'replace-with-a-long-random-local-secret'
uvicorn app.main:app --app-dir cloud/studio-controller --reload
```

The browser must send an `Origin` in `STUDIO_ALLOWED_ORIGINS`. It first calls
`POST /v1/auth/start`, then `POST /v1/auth/verify`, and uses the returned
operator Bearer for `POST /v1/session`. Session creation requires a stable
`creation_id`; retrying the same identifier returns the same session and does
not consume a second daily admission. The returned session Bearer authorizes
events, commands, and—when separately enabled—voice. Consume SSE with
authenticated `fetch`, not native `EventSource`: credentials never enter a
query string or managed request logs.

## Cloud Run contract

Required secrets/environment:

- `BLACKBOARD_STATE_URI=gs://sfdc24-fleet-state/studio`
- `STUDIO_SESSION_SECRET` from Secret Manager (at least 32 random bytes)
- `STUDIO_OPERATOR_EMAILS` as an exact lowercase allowlist
- `STUDIO_EMAIL_SENDER_URL` for the HTTPS Apps Script/Pipedream mail adapter
- `STUDIO_EMAIL_SENDER_SECRET` for request HMAC signing
- `STUDIO_WORKER=synthetic` for the provider-free release; `claude` plus
  `ANTHROPIC_API_KEY` for the model-backed one
- `STUDIO_ENABLE_VOICE=false` for the authentication/text release
- `OPENAI_API_KEY` and `STUDIO_MAINTENANCE_SECRET` only after voice is enabled

Keep `STUDIO_MAX_SESSION_SECONDS=600`, `STUDIO_DAILY_SESSION_CAP=20`, and
the operator allowlist for the approved initial envelope. Voice is one call per
Studio session. Client code closes its peer connection at controller expiry;
the service also hangs up on Stop or expiry. A scheduler calls
`POST /v1/maintenance/voice-sweep` with the maintenance bearer as the idle
Cloud Run backstop. Audio is never accepted or stored by this service.

## Incremental release gates

1. `/studio/` synthetic fixture; no controller or provider.
2. Email OTP plus authenticated text controller; `STUDIO_WORKER=synthetic`,
   `STUDIO_ENABLE_VOICE=false`.
3. Realtime voice only after the provider and scheduled cleanup acceptance pass.
4. Claude worker and external connectors are enabled independently; none is a
   prerequisite for an earlier safe release.

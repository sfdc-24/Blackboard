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

This Phase 2 package contains only the deterministic `synthetic` worker. Keep
`STUDIO_WORKER=synthetic`; selecting `claude` deliberately fails startup until
the separately reviewed provider worker and SDK are added in a later release.
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

Use exact, slashless routes. Trailing-slash variants return404 without a
`Location` header; the service does not infer redirect destinations from an
untrusted or proxy-visible scheme. `GET /health` is the canonical non-sensitive
readiness surface; `/healthz` remains a legacy application alias, but edge
routing can intercept that path, so deployment checks must use `/health`.

SSE checks origin, bearer, integer cursor and session/replay state before
committing stream headers. Missing sessions return JSON404; repair conflicts
return JSON409. A successful stream reuses its preflight batch and exposes its
initial `X-Studio-Generation`. Stale cursors still receive a snapshot-first
repair generation. If state disappears or conflicts after streaming starts,
the connection closes without inventing an event or advancing its cursor;
reconnect with the last received ID using bounded backoff. A persistent failure
is then a preflight HTTP refusal. An EOF alone is not evidence of a successful
session or a new durable event. No token belongs in a URL.

## Cloud Run contract

Required secrets/environment:

- `BLACKBOARD_STATE_URI=gs://sfdc24-fleet-state/studio`
- `STUDIO_SESSION_SECRET` from Secret Manager (at least 32 random bytes)
- `STUDIO_OPERATOR_EMAILS` as an exact lowercase allowlist
- `STUDIO_EMAIL_SENDER_URL` for the HTTPS Apps Script/Pipedream mail adapter
- `STUDIO_EMAIL_SENDER_SECRET` for request HMAC signing
- `STUDIO_WORKER=synthetic` for this provider-free release
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

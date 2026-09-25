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
- a durable authorization cap on Realtime call-open attempts, attributed to
  the UTC day of their final reservation and enforced with GCS generation
  compare-and-swap across instances;
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

## Optional live Lead counts

`STUDIO_ENABLE_LEAD_FACTS=false` is the default. When explicitly enabled, a
wrapper recognizes Lead-count utterances before the prototype worker runs.
It calls only the fixed aggregate queries in `workers/org_facts.py` and emits
one committed `confirm` event naming the org, count, source and query time.
It does not patch the prototype, advance its artifact version, resolve an open
design question, or use a model to construct SOQL. Other input keeps the
selected worker's existing behavior. This works through the existing authenticated
session commands and SSE endpoints; no public Salesforce query endpoint is added.

Enabling requires `STUDIO_SALESFORCE_ORG_ID`, the exact 18-character Organization
ID, and the existing server-side `Headless_domain`, `Headless_consumer_key`, and
`Headless_consumer_secret` credentials. Bind these through Secret Manager in
Cloud Run; never pass them from the browser. Both the configured/returned host
and the org's developer/sandbox type are checked, then the returned org ID must
exactly match before any Lead aggregate is read. An alias alone is insufficient.
When enabled, a missing or malformed org ID prevents startup; disabled mode
does not require this setting. Missing credentials, wrong org, auth/query failures and malformed counts
produce an explicit unavailable answer, never a model estimate or a default zero.
LeadSource aggregates require `done: true` and unique normalized source groups;
an incomplete envelope or duplicate group is unavailable, never a partial total.

Existing durable command reservation/receipts prevent another query on replay,
including after a controller restart. A repeated transcript item also does not
query again: item IDs are retained up to the configured session command bound
(maximum 500), not a shorter rolling window. An intentionally new command/item
is a new observation. The session's
command limit bounds repeated reads; this slice does not add a global Salesforce
quota ledger. A controller interruption retains the existing unknown/failed-command
behavior and is not automatically retried. Query results are snapshots observed
over separate reads, not an atomic transaction across all aggregates.

The optional route executes Salesforce I/O in a fixed isolated subprocess, with
`STUDIO_LEAD_FACTS_TIMEOUT_SECONDS=12` by default and a validated ceiling of 20.
The parent applies one monotonic deadline across child startup, DNS, token retries,
backoff, all four queries and response reads. At expiry it kills the child, waits
for exit and joins its bounded pipe reader before returning unavailable. The
timeout does not abandon a provider thread. OS kill/reap cleanup adds a small
margin after deadline detection; the 20-second ceiling leaves 40 seconds within
the current 60-second Cloud Run request budget for cleanup and controller work.
This is not a guarantee against an unschedulable or unresponsive operating system.

The child receives only the required Salesforce credentials and basic runtime
environment; credentials never enter argv or diagnostic output. Provider bodies
are capped at 64 KiB before JSON decoding. Child output is capped at 4 KiB and
validated for exact keys, org binding, bounded text/counts, timestamp, and
`recent site leads <= all site leads <= all leads`. Failed or inconsistent reads
are unavailable. `/health` exposes only `features.lead_facts` for this capability,
not configuration, counts or readiness of Salesforce credentials.

After a durable Stop, the local worker is signalled and kills/reaps a child
promptly. A session-scoped tombstone prevents a subsequent local launch, and the
existing state CAS prevents late result commits. **Activation hold:** Stop on a
different Cloud Run instance still fences the result but cannot promptly cancel
the original instance's provider; that child ends by its own deadline. A durable
state watcher needs its own bounded I/O supervision: calling the current GCS
reader synchronously here (60-second socket timeout plus token lookup) would
break the provider deadline. Do not claim distributed prompt cancellation yet.

This source does not enable the feature in any deployment or implement metadata
writes. Verify an authenticated served-page count against the same-org query
before declaring the website integration live. Voice output has its own release
gates; a factual text event does not establish spoken-answer behavior.

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

Use exact, slashless routes. Trailing-slash variants return 404 without a
`Location` header; the service does not infer redirect destinations from an
untrusted or proxy-visible scheme. This refusal precedes CORS and request-triggered
voice cleanup, including for OPTIONS. `GET /health` is the canonical non-sensitive
readiness surface; `/healthz` remains a legacy application alias, but edge
routing can intercept that path, so deployment checks must use `/health`.
Both readiness routes are read-only; canonical traffic, background cleanup and
authenticated maintenance retain their voice-cleanup backstops.

SSE checks origin, bearer, integer cursor and session/replay state before
committing stream headers. Missing sessions return JSON 404; repair conflicts
return JSON 409. A successful stream reuses its preflight batch and exposes its
initial `X-Studio-Generation`. Stale cursors still receive a snapshot-first
repair generation. If state disappears or conflicts after streaming starts,
the connection closes without inventing an event or advancing its cursor;
reconnect with the last received ID using bounded backoff. A persistent failure
is then a preflight HTTP refusal. An EOF alone is not evidence of a successful
session or a new durable event. No token belongs in a URL.

## Authenticated Lead controller canary (source-only)

`tools/authenticated_canary.py` is an explicit API/controller acceptance runner;
it is not imported by the application and is not evidence about the served page,
browser UI, email delivery UX, voice, or a deployment. No live canary was run by
the source increment that added it. Hosted CI executes its contract tests through
`httpx.MockTransport` with the network namespace removed.

The live CLI requires `--live`, the exact Origin
`https://www.sfdc24.com`, and exactly one target: the `lead-canary` tag URL of
this service, `https://lead-canary---sfdc24-studio-controller-yzet4vuplq-uc.a.run.app`
(`LEAD_CANARY_HOST`). Any other host is refused, including the untagged primary
URL, which serves the public revision, and a service merely named
`lead-canary`. There is no override. Before a live run, confirm the tag still
points at a revision with 0 percent traffic
(`gcloud run services describe sfdc24-studio-controller --region us-central1`),
because the runner cannot see the traffic table. Redirects,
proxy environment variables, HTTP retries, browser state, and credential files
are disabled or unused. The client and its explicit zero-retry transport both
disable environment trust, send `Accept-Encoding: identity`, refuse compressed
responses, and enforce the response cap while streaming bounded raw chunks.

```powershell
python cloud/studio-controller/tools/authenticated_canary.py `
  --live `
  --target https://lead-canary---sfdc24-studio-controller-yzet4vuplq-uc.a.run.app `
  --origin https://www.sfdc24.com
```

The console reads the operator email and OTP with non-echoing `getpass`; a
`GetPassWarning` is a closed failure rather than permission to echo. It also
reads a non-echoing closed operator expectation containing `org_label`,
`org_type`, `total`, `site_total`, and `site_recent`. Nothing is read from argv
or environment for those values. Email and expectation prompt time precedes the
elapsed-budget network sequence; after `auth/start`, OTP waiting is deliberately charged
to the elapsed whole-run budget. Each request receives HTTPX pool/connect/write/read
inactivity limits no larger than the remaining request and whole-run budgets. The
runner checks elapsed time immediately after response headers, between one-byte
raw response chunks, after EOF, and between requests. Synchronous HTTPX does not
pre-empt an active header phase at an elapsed-time boundary. Header negotiation can
span multiple successful reads, including informational responses, and can continue
past both elapsed budgets while each read remains below its inactivity timeout. The
runner closes and fails without a follow-on request once control reaches the next
checkpoint, but it does not guarantee a finite total runtime for an active blocking
phase. Run a live invocation under an external wall-clock process supervisor and
interrupt or terminate it if that limit expires. These are cooperative checkpoints
and inactivity limits, not hard wall-clock cancellation. No request is automatically
retried:

1. start and verify email authentication;
2. create one operator-authenticated session;
3. read the baseline SSE snapshot and pin its generation, root, artifact version,
   and cursor;
4. send only `How many leads do we have?`, parse the complete known Lead formatter,
   compare the org label/type and all three aggregates to the operator expectation,
   and require a fresh UTC observation;
5. read exactly that committed Lead event after the baseline cursor;
6. replay the identical stable command receipt and prove the SSE cursor stays quiet;
7. commit and replay Stop, read exactly its terminal event, then prove one fresh
   well-formed non-Stop command receives the exact terminal HTTP 410 refusal.

Standard output is one closed redacted JSON receipt. It contains only the
evidence label, safe aggregate counts/UTC observation, generation/sequence
numbers, and boolean expectation/replay/SSE/terminal checks. The expectation
match proves only equality with operator-supplied values; it does not bind or
independently prove an immutable Salesforce org identity. The receipt never
contains the email, OTP, Bearer tokens, session/command identifiers, org label/type, or
remote error/body details. A failed check emits only its allowlisted stage.
This canary reads Lead aggregates through the already deployed controller path;
it cannot create or modify Salesforce metadata.

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
- `STUDIO_VOICE_MINT_CAP=3` for the initial voice envelope
- `OPENAI_API_KEY` and `STUDIO_MAINTENANCE_SECRET` only after voice is enabled

Keep `STUDIO_MAX_SESSION_SECONDS=600`, `STUDIO_DAILY_SESSION_CAP=20`, and
the operator allowlist for the approved initial envelope. Voice is one call per
Studio session. The controller reserves one `STUDIO_VOICE_MINT_CAP` slot
durably immediately before each provider call-open POST. Invalid requests and
failures before that reservation consume no slot; after a reservation succeeds,
the slot is never refunded—even for a provider refusal or ambiguous transport
outcome—so crashes and retries cannot exceed the per-ledger authorization cap.
If midnight UTC passes before provider contact, the attempt must also obtain a
slot in the new day; the prior-day slot remains consumed. The accounting day is
the final authorization reservation, not the external provider's non-atomic
network receipt timestamp, which can cross midnight. Redirects, request timeouts,
conflicts, and every non-2xx provider response remain `unknown`, non-retryable,
and indexed because they do not prove that no paid call opened. Each index entry
is owned by its `voice_id`, so late cleanup cannot erase a newer call. Client code
closes its peer connection at controller expiry; the service also hangs up on
Stop or expiry. A scheduler calls
`POST /v1/maintenance/voice-sweep` with the maintenance bearer as the idle
Cloud Run backstop. Audio is never accepted or stored by this service.

The scheduler does not store that bearer. It starts a dedicated Cloud Run Job
with Google OAuth. The Scheduler caller can only invoke that job; a separate
job runtime identity can only read `STUDIO_MAINTENANCE_SECRET`. The immutable
Studio image runs `python -m app.sweep_once`. The job accepts only the exact
HTTPS maintenance path, never follows redirects, fails closed on a non-200,
unavailable due-call index, pending cleanup, or malformed success response,
and logs no response body or credential. Deploy it with one task, parallelism
one, a 30-second task timeout, and zero task retries; use an every-five-minute
Scheduler trigger with a 30-second attempt deadline and zero retries. The
five-minute cadence leaves headroom above the observed 67–103-second
provisioning-plus-execution durations and reduces the risk of queued executions
accumulating. It does not guarantee non-overlap. See
`ADR-001-VOICE-SWEEP-BACKSTOP.md` for the alternatives and IAM boundary.

## The end card: rating and the session PDF

After a conversation the homepage asks how happy the visitor is and offers the
working session as a PDF by email.

- `POST /v1/session/{id}/rating` with `{"score": 1-5, "comment": "..."}`
  (comment optional, at most 300 characters) returns `{"ok": true}`. The last
  rating wins and is stored on the session record. Always available.
- `POST /v1/session/{id}/summary` with `{"design_png": "data:image/png;base64,..."}`
  (optional; at most 1.5 MB and 4096x4096) builds a PDF of the session: the
  host's recap (kept when `/recap` runs), what the visitor asked for, what the
  architect built, the decisions, the final design image and outline, the
  analyst's data model and the rating. It is emailed once to the owner's
  verified address and returns `{"sent": true, "to": "j***@example.com"}`; a
  replay answers from the record and sends nothing. Behind
  `STUDIO_ENABLE_SUMMARY_EMAIL` (default off; 503 while off). The body is
  capped at 2.1 MB from `Content-Length` before it is read (413).

One email per session, ever: a missing email is better than a duplicate. A
send that certainly did not leave (no connection to the sender, or its explicit
`{"ok": false}`, which it answers before mailing) is `failed` and may be asked
again (502, "the summary email could not be sent; try again"). Anything that
may have been delivered (a timeout after the request left, a lost redirect, a
5xx, a body that is not the receipt) is `unconfirmed` and is never sent again:
502 on that call, then 409 "the summary may already have been sent; check your
inbox". A reservation still `sending` after 120 seconds becomes `unconfirmed`
the same way.

Both take the session token and stay open for 30 minutes after the session
ends (stopped or timed out); no other endpoint gains that grace. The address is
never taken from the request: it is the contact recorded at sign-in (only while
the summary is on, and only used if it hashes to the session's subject) or the
operator allowlist. It is never returned to a page or logged; the session keeps
only the masked form. Delivery is the same signed Apps Script sender as the
sign-in code, kind `summary` (see `apps-script/studio-email-sender/README.md`).

## Incremental release gates

1. `/studio/` synthetic fixture; no controller or provider.
2. Email OTP plus authenticated text controller; `STUDIO_WORKER=synthetic`,
   `STUDIO_ENABLE_VOICE=false`.
3. Realtime voice only after the provider and scheduled cleanup acceptance pass.
4. Claude worker and external connectors are enabled independently; none is a
   prerequisite for an earlier safe release.

## The Muse (third homepage agent)

`POST /v1/session/{id}/inspire` with `{"text"?: str <= 600, "turn": int}` returns
`{"turn", "muse": {"line", "directions": [a, b, c]}}`: one spark plus three
different directions, each with what to see (palette, motif, type family), read
(headline, line), hear (tone) and work with. `workers/muse.py` checks the whole set
(exact ids a/b/c, hex colours, closed type enum, caps, no `<`/`>` or control
characters, distinct titles, types and palettes) with one repair attempt, then
503. The set is stored as `state["muse"]` by the same wait-for-the-build commit
as the analyst. At most `STUDIO_MUSE_CAP` (6) calls per session.

`/speak` accepts `{"voice": "muse", "text"}` in the Muse's voice
(`STUDIO_MUSE_VOICE`, default `coral`), or `{"voice": "muse", "direction": "a"}`,
which speaks the STORED direction's headline and line in its stored tone. The
page never supplies TTS instructions; the architect path is unchanged. `/health`
reports `features.muse` (Anthropic ready) and lists `muse` in `features.voices`
only when voice and the Muse are both available.

Until the use-policy module lands (PR #255), the Muse carries the policy in
one sentence of its own prompt; after it lands, it takes `USE_POLICY` and the
moderation gate like the other lanes.

## Client workspaces (STUDIO_CLIENT_WORKSPACES, off by default)

A named client signs in on the homepage and sees their own projects. This is held to the Gate 1
security contract in `docs/SFDC24-CODEX-STRATEGY-EXECUTION-PLAN-20260925.md`, and
`tests/test_studio_clients.py` covers it item by item.

**Registry (`app/clients.py`).** One state-store object, `studio_clients`: `{"version": 1, "clients":
[{"id", "name", "emails", "projects": [{"id", "name", "url"}]}]}`.
- `id` is the tenant, and a workspace is always `{tenant, project}`.
- Operators write the registry; the controller only reads it. Every authorisation reads it fresh,
  and only the sign-in address list is cached, for 30 seconds.
- A malformed entry is skipped whole. An address or tenant id that appears twice admits neither.
- A project URL is a reviewed canonical https page: a named host with a letter TLD; no IP literal,
  port, userinfo, query, fragment, percent-encoding or backslash; and no `.internal`, `.local`,
  `.localdomain`, `.localhost` or similar private name.

**Client token (`app/tokens.py`).** Its own format, `c2.`, signed with a key derived for this purpose
and verified only by `verify_client_token`.
- Its claims are exactly `v, typ, sub, tnt, prj, iat, exp, aud, jti`, and each is checked.
- It lives at most 24 hours; an `iat` in the future is refused.
- v1 operator, visitor and session tokens and client tokens never pass for each other.
- `/v1/auth/verify` returns `{"token", "expires_at", "scope": "client"}` for a registry address.

**Revocation and binding.** Every client-scope request re-checks, fresh from the registry, that the
tenant exists and still lists an address that hashes to the token's subject.
- This covers `/v1/workspace`, `POST /v1/session`, and every session-scope request on a client
  session. That check happens before any read, write or provider call, in `require_session` and
  `require_recent_session`.
- A client session's token binds `tnt`, `csub` and `prj` (`""` for blank and template). All three
  must equal the stored session. For a project session, the registry must still list that project
  under that tenant.
- An unbound token never reaches a client session, and a bound token never reaches any other.
- Every client session is keyed by `{subject, creation id, tenant, project}`. Replay ownership
  compares all of them, so an address moved to another tenant never reaches its old sessions.
- `/v1/workspace` shows only the projects in the token's `prj` snapshot.
- A removed client, another tenant's project, a project that does not exist, a project added after
  sign-in, and a token of the wrong kind all get the same `403 "this workspace is not available"`.
- Client tokens reach nothing operator-only, and client sessions have an empty `operator_subject`.
  An empty subject never passes.

**Project sessions.** `POST /v1/session` with `start: "project", "project": "<id>"`.
- The page is fetched by `app/project_page.py` before any admission, and only after two checks:
  - a per-tenant budget of page-load attempts is reserved (10 an hour, 40 a day; a failure spends
    one). It's recorded in `studio_client_fetch_<tenant>` as `{at, project}` only.
  - no other load for that tenant is in flight (429 if one is).
- At most 4 loads are outstanding process-wide. A load that timed out keeps its slot until its
  thread has really finished, so blocked resolvers can't pile up threads. With no free slot, the
  answer is 503 at once and nothing starts.
- A page that fails to load gets 502 `"the project page could not be loaded"`: no session, no
  admission.
- Sessions are keyed by owner, creation id, tenant and project. A replay fetches nothing.

**Fetch (SSRF).** Only the registry URL is fetched.
- The name is resolved in the worker, and every answer must be a public unicast address: no
  loopback, RFC1918, CGNAT, link-local, multicast, documentation, benchmarking, reserved, ULA, or
  mapped/6to4/Teredo form.
- The connection goes to that validated address, with the registered name kept for SNI, the
  certificate check and Host. This rules out DNS rebinding.
- Redirects fail closed. Environment proxies are never used. Only `text/html` is accepted, with
  identity or gzip encoding.
- Size caps: 1.5 MB compressed, 3 MB decompressed.
- Time caps: connect 3 s; first byte 5 s; idle 4 s; one wall-clock total of 8 s across
  resolve, connect, first byte and body.

**Tree.** The page becomes inert text in known kinds.
- Kept: screen, section, nav, heading, text, button, image-placeholder (never fetched), list,
  form and field.
- Dropped: scripts, styles, frames, objects, SVG, MathML, templates, comments, and every attribute
  except a few read as label text. No URL survives, even as visible text.
- Caps: 2M input characters, fed in 16 KB chunks, with a hard stop between chunks on time, node
  count and an unparsed remainder over 64 KB (one giant tag or an unclosed script). Also 50,000
  events, 4 KB looked at per text run or attribute, 60 nodes, depth 4, 12 images, 200-character
  labels, and 2 s of parsing.
- Links, bare domains, IPv4 and IPv6 addresses and email addresses in page text become `[link]` or
  `[email]`. The registry's project name is kept as written.

**Audit.** Every accepted command in a client session appends exactly one entry to `state["audit"]`,
keeping the last 200.
- Entry fields: `{actor, token_type, tenant, project, command_id, command_type, prior_revision,
  revision, op_ids, at, outcome}`.
- Stop and worker failures are included (outcome `failed`). A replayed `command_id` adds nothing.
- `op_ids` lists every event the command emitted.
- The audit never holds page text, transcripts or addresses.

**Failures.** An unexpected failure is answered `500 "the request could not be completed"` and logged
with its error type only, both by the commands route and by a catch-all in the middleware.

**Logs.** No token, address, project URL, pinned IP or page text appears in logs or error bodies.
httpx and httpcore request logging is held at WARNING.

**Other.** The end-card summary reaches a client through the sign-in contact or the registry. The
Apps Script sender must list the same addresses in `STUDIO_CLIENT_EMAILS`.

## Metadata proposal contract (offline-only increment)

`STUDIO_ENABLE_METADATA_PROPOSALS` defaults to `false`. Enabling it requires an
exact 18-character `STUDIO_SALESFORCE_ORG_ID` configuration binding but **does not
contact Salesforce, verify org identity, read provider credentials, or enable any
write capability**. Existing prototype/Lead routes remain unchanged when off.

An authenticated Studio session can propose exactly one optional, nonunique,
non-external-ID `Text` field on `Lead`. The natural-language grammar is deliberately
narrow: `Plan a new field on Lead for prototype interest` creates the local draft
`Lead.Prototype_Interest__c`, label `Prototype Interest`, length 80. Unsupported
recognized metadata requests receive local guidance without invoking the model.
Routing requires that anchored planning intent or an explicit org/object target;
malformed suffixes after `Plan a new field on Lead for` receive local guidance,
while only the complete validated grammar creates a proposal. An explicit target
such as `on the Lead object` is recognized without requiring the word Salesforce.
`lead`/`custom field` alone does not turn a website/app form request into an org
request. UI asks mentioning Salesforce retain the prototype route unless they
explicitly target Salesforce (for example, a field `in Salesforce` or `on the
Salesforce Lead object`). Later source/reference clauses such as `using labels
from Salesforce` do not make a prototype form field an org target.
`from Salesforce` identifies a data source for UI creation/addition requests;
deletion/removal of a component `from Salesforce` stays local. Bare destinations
such as `Add a field to Salesforce` and schema operations are also recognized.
Branded component names must be the opening action's direct target (polite
prefixes are accepted), not quoted/later actions in heading/copy/footer text.
An anchored primary UI-target check precedes all explicit-target searches, so
`Change the heading to say Add a field to Salesforce` remains a prototype ask.
An explicit object/metadata/schema target stays local even when its purpose is
a website form. An explicit org creation followed by
`and`/`then` showing it in the UI stays local; adding a Salesforce field directly
to a prototype form still follows the prototype route.
Unsupported explicit org operations, including deletion, remain local guidance only. Other
utterances retain the existing worker route. This is not general semantic
metadata intent detection. A typed `metadata.propose` command uses the existing
command envelope plus the closed `field` object:

```json
{"parent":"Lead","name":"Prototype_Interest","label":"Prototype Interest","type":"Text","length":80,"required":false,"unique":false,"external_id":false}
```

Names are suffix-free ASCII stems, 1–32 characters, starting with a letter, with
no doubled/trailing underscores. Labels are 1–40 trimmed printable characters
without markup; Text length is an integer 1–255. These are application policy
limits, not a claim to cover every Salesforce type/limit. Unknown keys and all
other objects/types/actions are rejected by the typed contract.

The controller commits a normal `confirm` event with **only** the existing Stage A
`text`/`artifact_ids` payload and a plain-language notice. The typed
`metadata_proposal` is a separate top-level **command response** field, not an SSE
event/payload extension. It does not modify the artifact/version or design
questions. The model cannot supply this reserved contract. The current proposal is
bound to its operator, session, configured org, policy, revision, exact field data,
expiry and nonce through its SHA-256 digest; it expires at the earlier of ten
minutes or session expiry. A new proposal supersedes the previous one. Command
replay restores the typed response; SSE repair remains unchanged. The target says
its identity has not been checked and exposes neither org ID nor fingerprint.
This is backend-contract-only: no dedicated frontend proposal card is included.
The new typed commands are not part of the served Stage A command schema; keep
this feature off until a coordinated frontend contract/UI increment is reviewed.

`metadata.confirm_contract` takes only the ordinary command envelope and
`confirmation` containing `plan_id`, `plan_revision`, `plan_hash` and
`confirmation_nonce` from that payload. It rejects altered, expired, superseded,
cross-bound or already-consumed proposals. It does not accept voice/answer-source
fields. Natural language never consumes confirmation. A successful receipt says
`contract_validated_not_executed` and `execution_available:false`. Existing command
fingerprints/CAS and utterance item IDs make retries replay without generating
another proposal or consuming confirmation twice, including across restart.

**This receipt is not authorization for a future executor.** A later write slice
must issue a new policy/plan and fresh explicit confirmation. This proposal route
has no operation ledger, provider, org permission proof, quota for writes, deployment, deletion,
rollback, or reconciliation in this increment. Do not label it a live metadata
demo or silently attach an executor to this confirmation command. The current
MCP inspection plan's HOLD is unchanged.

Offline regression: `python -m unittest discover -s tests -p test_studio_metadata_contract.py`.
Set `STUDIO_SITE_EVENT_SCHEMA` to a read-only site checkout's
`studio/contract/events.schema.json` to also check the emitted envelope/confirm
constraints against that file (otherwise that optional compatibility test skips).

### Disconnected metadata operation ledger

`app/metadata_operations.py` adds offline state-machine plumbing only, not a
controller route or Salesforce capability. It requires a separately certified
atomic CAS adapter and a fresh execution-specific plan/confirmation. Atomic
per-org target holds prevent different operation IDs bypassing in-flight or
unknown operations; uncertain outcomes never auto-retry. Public receipts are
strictly allowlisted, and verified presence makes no creation-causality claim.
See [METADATA-OPERATIONS.md](METADATA-OPERATIONS.md) for persistence, recovery,
adapter trust boundaries and the remaining activation holds.

### Inert Salesforce metadata adapter contract

`app/metadata_provider.py` adds a further source-only boundary; it is not wired
to the controller, settings, routes or a concrete provider. An immutable binding
separates the ledger's opaque org-binding ID from the exact Salesforce org ID
and pins a developer/sandbox origin, API version, environment and binding
version. One sealed process-local execution budget carries the same absolute
maximum-20-second deadline, plan expiry and cooperative cancellation signal
through exact provider preflight, one possible create-method entry and an
independent verification read. It also seals the in-process ledger dependency
configuration and pins the exact store object identity used at issuance; a
look-alike ledger or dependency swap fails before ledger or provider I/O.
Sealed objects reject initializer re-entry, and the immutable issued-plan
registry prevents clearing local history to obtain a second execution budget.
Automatic state restoration, copying and serialization are rejected; bindings
cross a reviewed boundary only through their explicit closed mapping shape.

The injected transport has only four provider-specific callables and must
declare no write retries and disabled redirects. The adapter pins their resolved
identities and rejects declaration/method drift at provider phase boundaries.
An opening transport owns cleanup until it returns a valid session; invalid open
responses are not passed to `close`. Describe results require
explicit `complete:true`; no partial/empty response proves absence. The trusted
coordinator commits its own preflight evidence, invokes the ledger's durable
`begin` directly, re-reads the operation and issues a non-serializable one-use
permit only for a fresh non-replayed reservation. Raw dispatch booleans, caller
dictionaries and portable snapshots cannot dispatch. Once the create method is
entered, every exception, late/non-exact acknowledgement or close uncertainty
is terminal ambiguous. Verification results are retained in the sealed budget
and committed idempotently without accepting caller-supplied observations.

There is deliberately no HTTP/SDK/subprocess implementation, credential lookup,
provider/org call, route, UI, deployment, IAM/secret change, traffic change or
feature activation in this increment. The cooperative signal cannot force a
nonconforming transport to stop, and the source contract/offline tests do not
certify a concrete Salesforce implementation. The capabilities are process-local
misuse barriers; restart recovery/reconciliation is not implemented, and one
immutable binding object does not prove a durable bijective org-binding registry.
See
[METADATA-OPERATIONS.md](METADATA-OPERATIONS.md) for the exact shapes, recovery
rules, test command and remaining activation holds.

## Use policy

The homepage studio builds legitimate, professional business work only. Three
layers enforce that, and each one stands on its own:

1. **The gate** (`app/governance.py`). Before any lane runs, every piece of
   visitor text goes through OpenAI's moderation endpoint
   (`omni-moderation-latest`): talk text and its history, an utterance or typed
   answer sent to the builder, analyst text, and a line sent to the architect's
   voice. A flagged line runs no lane:
   - talk replies with the policy line (`"refused": true`), which the page speaks;
   - a command returns no events and the problem `declined by the SFDC24 use policy`;
   - analyze returns no model and no events;
   - speak returns 422, and that refusal is not counted, because its text
     normally comes from our own agents.

   A stop command is never gated.
2. **The policy text** (`workers/policy.py`, `USE_POLICY`). It is appended to
   the system prompt of every lane: talk, recap, builder and analyst. It covers
   impersonation (look-alike logins, fake records, phishing, capturing
   credentials or payments), deception and harassment, sexual, hateful, violent
   or extremist content, illegal activity and rights violations, and
   unprofessional content. Moderation cannot tell that a bank login lookalike
   is a phishing kit, but the agents can, and they decline in one sentence.
   Visitor words, the canvas, the history and research all arrive as user
   content, so none of them sits where it could rewrite the policy.
3. **The providers' own usage policies** (Anthropic, OpenAI) apply underneath
   every call.

**Counting.** Flags are counted per session in their own store object,
`studio_policy_<session id>`, never in the session record. That way a flag
can't make an in-flight build's compare-and-set save fail. One spoken line
reaches talk, the builder and the analyst, so each lane keeps its own count
and the session's count is the highest of them. At 3 the session is stopped
with the reason "This session was ended because requests went against the
SFDC24 use policy", and its voice call is hung up.

**Outage.** No key, a timeout (3 s), an HTTP error or a malformed answer means
the gate fails **open**. The turn goes on under layers 2 and 3, the outage is
counted (`app.state.moderator.stats["unavailable"]`), and it is logged as
`studio.moderation_unavailable`.

**What is kept.** Never the visitor's words. A flag record holds the lane, the
moderation categories and a timestamp. A log line (`studio.policy_flag`,
`studio.policy_refused`) adds the session id and the count. The in-memory cache
of recent verdicts is keyed by SHA-256.

**Switch.** `STUDIO_ENABLE_MODERATION` defaults to `true` from the environment.
`/health` reports `features.governance`.

A code guard on artifacts that name well-known brands in a login or payment
context was considered and left out:
- a closed list of brands is never complete;
- legitimate work names brands in exactly that context ("Sign in with
  Google", "Pay with Visa", a Salesforce login for a Salesforce consultancy);
- a canvas prototype cannot collect anything.

The prompt layer and moderation carry it.

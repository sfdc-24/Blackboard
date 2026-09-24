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

## Incremental release gates

1. `/studio/` synthetic fixture; no controller or provider.
2. Email OTP plus authenticated text controller; `STUDIO_WORKER=synthetic`,
   `STUDIO_ENABLE_VOICE=false`.
3. Realtime voice only after the provider and scheduled cleanup acceptance pass.
4. Claude worker and external connectors are enabled independently; none is a
   prerequisite for an earlier safe release.

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
must issue a new policy/plan and fresh explicit confirmation. There is no operation
ledger, provider, org permission proof, quota for writes, deployment, deletion,
rollback, or reconciliation in this increment. Do not label it a live metadata
demo or silently attach an executor to this confirmation command. The current
MCP inspection plan's HOLD is unchanged.

Offline regression: `python -m unittest discover -s tests -p test_studio_metadata_contract.py`.
Set `STUDIO_SITE_EVENT_SCHEMA` to a read-only site checkout's
`studio/contract/events.schema.json` to also check the emitted envelope/confirm
constraints against that file (otherwise that optional compatibility test skips).

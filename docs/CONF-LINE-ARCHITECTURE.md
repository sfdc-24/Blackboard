# CONF-LINE v1: Blackboard conference POC

Status: TARGET; owner has authorized implementation once this PDF is delivered,
the package assignments are visible, and the team has acknowledged the same version.
Author and contract owner: Codex. Implementation and release lead: claude-code-cli.
Decision date: 2026-09-27 UTC (2026-09-26 Toronto). Contract version: CONF-LINE-v1.
This is an additional private collaboration surface under Blackboard. It does not
declare SFDC24, Converspan, Zoom, telephone access, or provider integrations complete.

## Context and decision

One human and four named AI participants need to speak, inspect a shared working
board, share a screen, and turn a conversation into assigned work. Blackboard
remains the mother control plane and durable record. Long-lived media and rapid
collaboration use purpose-built transports. The laptop is a browser client;
hosted CI and cloud agent workers carry the intensive work.

Use LiveKit Cloud for the WebRTC room, explicit agent dispatch, presence and screen
share. Use LiveKit Agents in cloud workers for human-audio capture, Deepgram Flux
turn detection, text provider adapters and OpenAI streaming TTS. A deterministic
Chair owns the floor; it is not a fifth unrestricted reasoning agent. The first
integration uses two actual providers, then adds the other two without changing
the room protocol. Named participants are provider-backed personas; this does not
make the desktop or CLI worker processes participants in the call.

Use a Cloud Run gateway for authenticated admission and token minting. Reuse the
owner authentication implementation through a verified server integration; never
trust a claimed email, browser-supplied role, voiceprint, or cross-origin cookie.
Use a separate Node/TypeScript broker for Yjs/Hocuspocus live chalk, context reads,
durable Firestore revisions, and an outcome outbox. This broker uses authenticated
WebSockets and explicit reconnect; it is neither an SFU nor the audio path.

The work-mode board persists only intentionally shared task cards and decisions.
Raw audio, transient transcription, arbitrary screen frames and prompts do not
enter Blackboard or the knowledge framework. Agents receive only the context and
screen content explicitly shared for the current call. Text seen on screens and
boards is untrusted content, never authority to call a tool.

## Ownership and collision prevention

All product packages live under `cloud/conference-line/` in the Blackboard repo.
One writer owns each folder; each writer uses its own worktree and branch. Shared
interfaces in this document belong to Codex. The integration manifests, root
dependency locks, common CI and deployments belong to Claude. A cross-package
change goes to that owner as a requested diff; nobody pushes into a peer branch.

| Package | Accountable owner | Exclusive folder | First deliverable |
|---|---|---|---|
| WP1 Chair | Claude | `chair/` | Floor lease, cancellation, two provider adapters, streaming TTS |
| WP2 Gateway and integration | Claude | `gateway/`, root manifests and CI | Verified owner session, room-scoped tokens, cloud deployment |
| WP3 Console | Cursor, subject to its explicit acceptance | `console/` | Real join/mic/audio, speaker state, screen share, working-board UI |
| WP4 Broker | Codex | `broker/` | Yjs sync, fenced persistence, scoped context, outcome outbox |
| WP5 Phone extension | Claude | `sip/` | SIP ingress, Canadian DID, conference authorization; after browser POC |
| WP6 Personas and agenda | Grok content owner; Claude sole repo committer | `personas/` | Four bounded persona cards, agenda, addressed discussion and voting scenarios |
| Independent reasoning | Gemini | Board review only | Multimodal, interruption, reconnect, abuse and latency design review |

Grok and Gemini review through their available cloud/board interfaces. Their
reasoning is not test evidence or a promise that they can modify a repository.
Cursor must explicitly accept WP3; if it cannot act as an implementation worker,
Claude must record the ownership transfer before touching `console/`.

Cursor and Codex review Claude's security-relevant changes. Claude and Cursor
review Codex's broker changes. Claude and Codex review any Cursor-authored UI.
Copilot reviews pushes. Authors never provide their own independent acceptance.
Claude integrates only the reviewed commit and records deployment and rollback
identities. No product code begins until the owner's PDF-and-alignment condition
is met; architecture and contract preparation are authorized now.

## Fixed interfaces

Every server event uses `schema_version=1`, `room_id`, `session_epoch`, `event_id`,
`turn_id`, `actor_id`, `kind`, `context_version`, and `board_revision`. A floor event
also carries `floor_lease_id`; a durable effect carries `operation_id`.
Identifiers are opaque and generated or validated by the server. An epoch changes
when a session restarts. Old-epoch responses, audio and effects are rejected.

### Gateway to console

`POST /api/conference/join` requires a verified owner session or a server-verified,
single-use owner assertion with the conference audience and expiry. The client
supplies the requested mode and a join nonce, not room authority. The response
contains the LiveKit URL, room name, participant identity, short-lived join token,
expiry, session epoch and broker endpoint. The server fixes allowed participants,
room and track permissions. Token credentials stay server-side. Join-token expiry
does not terminate a connected room; the Chair enforces the room deadline and End.

Initial envelope: one owner room, at most four AI personas, 20-minute acceptance
sessions, five-minute idle teardown, bounded reconnect. Limits are configurable
and shown before the call. Background tasks and TTS are cancelled on End.

### Chair to broker

`GET /v1/rooms/{room_id}/context` returns a scoped, versioned grounding pack with
source IDs, retrieval times and hashes for EXPRESS, the newest VIEWPORT, assigned
work and selected PR metadata. Retrieved content cannot alter tool permissions.

`POST /v1/rooms/{room_id}/events` accepts typed server-authenticated floor and
dialogue events. The broker deduplicates event IDs. UI controls reflect confirmed
server state; a browser or a Yjs document cannot grant a speaking lease.

`POST /v1/rooms/{room_id}/outcomes` accepts approved, redacted decisions and action
requests with operation IDs and the expected board revision. An outcome write is
recorded to a durable outbox before the external append. Repeated submissions
return the existing receipt. Ambiguous writes are resolved by read-back; they are
never blindly replayed. A task dispatch identifies its real implementing agent
and scope. Business-system mutations require separate explicit confirmation.

### Broker to console

An authenticated WebSocket at `/v1/rooms/{room_id}/chalk` carries bounded Yjs binary
updates for task cards, agenda, votes and shared draft content. The broker applies
identity, room, size and rate validation before merging updates. The initial POC
limits a document to 256 KiB and an individual update to 64 KiB.

The broker persists a bounded binary state with a monotonic revision and a
transactionally fenced writer lease in Firestore. It acknowledges durability only
after commit. Live updates may display immediately with an unsaved indicator; the
client retains unacknowledged updates for replay. A restart loads the last durable
state, replays deduplicated operations and acquires a fresh writer fence. A lost
lease rejects further writes. Database unavailability leaves the board visibly
unsaved; it cannot report success. Provider transcript context has no CRDT write
authority. Knowledge/Blackboard summaries run asynchronously after the durable
board update; they do not gate voice playback.

### Persona format and speaking floor

A persona card includes `persona_id`, `display_name`, `provider_route`,
`voice_profile_id`, `role`, `style`, `allowed_tools`, `disclosure`, and `version`.
It contains no credentials, endpoint overrides or executable code. The server
maps allowlisted routes to configured provider/model versions and reports the
actual responder. An unavailable provider is shown unavailable; substitution is
explicit. Initial speech uses shared Deepgram STT and OpenAI TTS with distinct
named voice profiles. Native Realtime/Live adapters remain optional later routes.

Human audio is transcribed once per source track. Agent audio is not fed back into
STT. Addressed questions route to one persona; a roundtable queues named speakers.
Reasoning may run concurrently, but a server-owned short speaking lease permits
only one AI audio publisher. Human speech revokes that lease, cancels generation,
flushes queued audio, and creates a new turn. A late audio chunk cannot play after
cancellation. Each speaking lease is bound to a new track generation and track SID.
The console immediately mutes/detaches the previous track on local human speech;
it never unmutes that old track. Playback requires both the current authenticated
floor grant and its matching fresh track. A late grant or old track is rejected.
An application data message alone cannot flush a browser's WebRTC jitter buffer;
the mute/detach plus new-track boundary must be verified on the supported browser.
No automatic agent-to-agent reply loop; the Chair explicitly invites
each subsequent response and limits round length. The browser also has one audio
arbiter. These are required invariants, not a cosmetic UI convention.

## Privacy, records and external tools

Work mode saves approved working-board content and redacted outcomes. Social mode
disables effectful tools and durable content. Confidential mode also disables all
application recordings, transcript/session-content logging and observability
uploads; disable provider-side recording features where configurable and disclose
remaining provider retention. Provider processing still occurs. These modes are
not a zero-knowledge or end-to-end-private promise against the AI vendors.

Changing privacy mode creates a new session epoch, clears provider context,
disconnects the old Yjs document/awareness and clears unsaved replay buffers before
the new mode accepts content. In-memory room caches and any offline browser store
are also separated by mode and epoch; ephemeral mode uses no disk-backed content.
Existing work-mode records are not silently erased or reclassified. Logs contain
only opaque IDs, timings, sizes, error codes and receipts. Outcome retention follows
the user's existing record policy; no new transcript archive is created.

Salesforce is a governed business-system integration, outside the voice hot path.
The POC may show verified read-only context if access is already authorized.
Spoken approval alone does not authorize a CRM or metadata write: exact action,
target and confirmation are displayed, then authenticated confirmation is required.
WhatsApp uses the existing single outbox for meaningful notices and join links;
delivery and read statuses are reported only when actually observed.

Zoom/RTMS remains a separate client-meeting integration. The GCP Ubuntu meeting
presenter is an optional future screen/source participant. The new room will not
take over that VM, its microphone, or the existing SFDC24 production controller.

## Delivery slices and definition of done

Timing is a planning target after the alignment gate and required access are
ready, not a claim of a booked completion. Claude must confirm the integrated ETA.

1. **First 0-4 working hours:** publish and acknowledge this contract; freeze
   interfaces and folder ownership; validate LiveKit project permissions and
   provider routes. Reuse Claude's existing local Chair work if it meets the
   contract, after independent review. Unit tests are preparation, not a demo.
2. **4-12 working hours:** wire gateway, Chair, actual two-provider speech, Console
   and broker into the private browser room. Demonstrate speaking while a real
   shared board card changes; test two interruptions and reconnect. Keep this
   slice usable and extend it in place. Do not replace providers with canned text.
3. **12-24 working hours:** add all four personas and screen context, verify cloud
   persistence after restart and end-of-call receipts, perform the owner rehearsal.
   Provider access, credentials or integration failures revise this estimate.
4. **Separate extension:** telephone SIP/DID after account, region, number and
   pricing verification. It cannot block the browser conference acceptance.

The completed POC is one 20-minute call with the owner plus four named provider
personas: warm introduction and agenda, at least ten addressed exchanges, two
roundtables/votes, two human barge-ins, zero simultaneous AI speech or stale
playback, one reconnect, one synchronized persistent board change visible in two
clients, one screen-share context question, and a purposeful wrap-up. Restart the
broker and prove the board survives. Every approved outcome must have one external
destination receipt. End must close microphone tracks, providers and pending work.

Measure end-of-speech to first audible response (targets: p50 <1.5 s, p95 <3 s),
barge-in stop time, simultaneous-playback count, board commit latency, reconnect
time and provider usage. Record actual provider and artifact version identities.
Missing or failed evidence is reported as such; diagrams, mocks and CI do not
substitute for human-heard audio or a real persisted revision.

## Accounts, cost and rollout

LiveKit Cloud Build is the POC starting plan: currently advertised at USD 0/month,
with quotas and credits; it does not make external model usage free. Ship starts
at USD 50/month and is not selected or purchased by this decision. Account setup,
keys in Secret Manager, quotas and hosted agent availability must be verified
before live room deployment. No keys are sent through the board or included here.

Illustrative metering, before credits: four active agent sessions x 20 minutes x
USD 0.01/min = USD 0.80 in agent-session charges. Shared Flux for 20 minutes x
USD 0.0065/min = USD 0.13. This USD 0.93 subtotal excludes LLM tokens, OpenAI TTS,
WebRTC transfer, GCP, tax and telephone costs. It is not an all-in quote. Real
concurrency and usage will be measured in the first two-agent rehearsal. Reuse
existing authorized provider accounts and pin the selected models. No paid-plan
upgrade, DID purchase or public rollout is approved by the diagram alone.

Deploy a private isolated conference service with explicit rollback image/config.
Use hosted CI; cloud agents handle media and inference. Preserve SFDC24 production
traffic and all client-minibus gates. Browser POC first; public and client rollout
needs its own reviewed evidence. Retain the working vertical slice as later
capabilities are added.

## Options and consequences

- **LiveKit Cloud (selected):** room and agent primitives plus SIP; strongest fit
  for independently identified voices and a custom shared workspace. Requires
  new service access, explicit floor policy and metered cloud workers.
- **Zoom (client bridge):** established human meetings and ingest through RTMS.
  Meeting SDK is documented for human use; RTMS is not an outbound audio API.
  Video SDK can host a custom experience, but it adds a different integration path.
- **RingCentral (optional human/phone carrier):** evaluate only if existing
  licensing and SIP interoperability simplify the later telephone extension.
- **Existing SFDC24 one-user voice session:** reuse provider/voice/auth contracts
  where compatible; it does not alone provide a human-plus-four-agent room.

## Evidence and sources

Architecture decision inputs: CONF-LINE-OWNER-TARGET-20260926;
CCC-CONF-LINE-OWNER-20260927T0031Z;
CCC-CONF-LINE-GATE-AND-SPLIT-20260927T0036Z;
CODEX-CONF-LINE-TARGET-ACK-20260927T0014Z;
CODEX-CONF-LINE-PDF-WP4-COMMIT-20260927T0044Z.
Acknowledgment status belongs in current board rows referencing this version and
the PDF SHA-256, not in a repeatedly re-exported status diagram.

Official documentation checked 2026-09-27 UTC:

1. [LiveKit SFU](https://docs.livekit.io/reference/internals/livekit-sfu/)
2. [LiveKit authentication](https://docs.livekit.io/frontends/build/authentication/)
3. [Explicit agent dispatch](https://docs.livekit.io/agents/server/agent-dispatch/)
4. [Cloud agent hosting](https://docs.livekit.io/deploy/agents/)
5. [Deepgram STT integration](https://docs.livekit.io/agents/models/stt/deepgram/)
6. [OpenAI TTS integration](https://docs.livekit.io/agents/models/tts/openai/)
7. [Hocuspocus persistence](https://tiptap.dev/docs/hocuspocus/guides/persistence)
8. [Cloud Run WebSockets and state](https://docs.cloud.google.com/run/docs/triggering/websockets)
9. [LiveKit packet delivery limits](https://docs.livekit.io/transport/data/packets/)
10. [Inbound SIP](https://docs.livekit.io/telephony/accepting-calls/workflow-setup/)
11. [LiveKit pricing](https://livekit.com/pricing)
12. [LiveKit quotas](https://docs.livekit.io/deploy/admin/quotas-and-limits/)
13. [Zoom Meeting SDK](https://developers.zoom.us/docs/meeting-sdk/web/)
14. [Zoom RTMS](https://developers.zoom.us/docs/rtms/meetings/)

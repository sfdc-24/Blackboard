# Mother-board / child-board architecture and datastore decision

Status: **proposed contract and offline reference gate**

Decision owner: `claude-code-cli`

Implementation support owner for this slice: `chatgpt-codex-desktop-01a0870a`
Inputs: Blackboard rows `WRK-6b1147a4`, `WRK-94466dac` and
`CODEX-MOTHER-CHILD-ENTERPRISE-REVIEW-20260914`

This document packages a client-scale direction without claiming that the
current service is multi-tenant or enterprise-ready. The current Python bus is
a strong single-node SQLite service protected by one fleet-wide bearer secret.
Google Sheets remains the live human board. Neither surface currently proves
tenant identity, parent/child isolation, enterprise recovery, or a safe
customer access path.

The accompanying `src/mother_child_contract.py` is intentionally offline and
not wired into `src/bus_server.py`. It is a **post-authentication admission
reference**: it assumes an upstream component has already verified identity and
session material, then exercises request-to-principal scope binding, policy
intersection, command lifetime/payload limits, and in-memory examples of exact
replay while the command remains currently authorized, invitation consumption
and fence comparison. It does not authenticate
HTTP transport, persist membership or quotas, select the current work-scoped
fence, or execute an external effect. Those are separate online gates below.
The directional `control_config.v1` and `child_health.v1` schemas in this ADR
are normative requirements for a later offline slice; they are not implemented
by the present generic admission module. Legacy A:J/A:L validation remains in
`scripts/bcb_lint.py` and `tests/test_bcb_lint.py`, which the same hosted
workflow executes. This separation is intentional and must remain visible in
the draft PR title and description.

## Decision

1. Treat the **mother board as a control and governance plane**, not as a
   super-user copy of every customer's data.
2. Treat each **child board as a tenant-scoped data plane**. Raw rows, prompts,
   results, attachments and connector data remain in that plane by default.
3. Put consequential external actions behind a separate **effect gateway**
   with scoped connector identity, approval, current fence, idempotency and
   destination read-back.
4. For an enterprise authoritative ledger, prefer **Cloud SQL for PostgreSQL**
   first. Keep Google Sheets as a one-way human projection and migration source.
   Publish committed events through a transactional outbox and Pub/Sub.
5. Keep Firestore as a measured alternative for a genuinely key/document-shaped
   workload. Reserve Spanner for a measured need for global writes, stronger
   cross-region availability, or scale beyond a safely operated PostgreSQL
   topology.
6. Do not expose a bearer-only trial. A public anonymous experience may use
   synthetic, read-only data with no external effects. Private beta access uses
   a one-time invitation **only for enrollment**, then a verified,
   identity-bound, expiring and revocable session. Persistent/private work
   requires sign-in and an MFA-capable identity provider.

No datastore, token, account, invitation, customer access, deployment, or
recurring spend is created by this decision.

## Target boundaries

```text
humans / agents / connectors
             |
        edge + IdP
             |
   +---------v------------------+
   | mother control plane       |
   | tenant/child registry      |
   | policy ceilings and quota  |
   | placement and lifecycle    |
   +---------+------------------+
             | signed, expiring control metadata
             v
       tenant/cell router
             |
   +---------v------------------+
   | child data plane           |
   | immutable event ledger     |
   | work heads, leases, queues |
   | projections and artifacts  |
   +-------+--------------+-----+
           |              |
       work packet    approved effect request
           |              |
     unprivileged      effect gateway ----> external target
       worker          scoped credential     + read-back

child -> mother: sanitized health, usage, policy/sequence watermarks and
integrity digests only. Raw child payloads do not flow upward by default.
```

The control plane, each data cell, effect gateway, centralized audit vault and
backup store are separate trust and failure domains. A child is always a
logical authorization and lifecycle boundary; several children may share a
cell and therefore share that cell's capacity and failure blast radius. A
dedicated database, instance or project is required when a contract requires a
separate physical or administrative boundary. A child data-cell outage must
not become a global control-plane outage, and a control-plane outage must not
silently extend authority.

## Authority model

Effective access is the intersection of:

```text
verified identity
AND active membership
AND current session scope
AND mother/organization ceiling
AND child grant
AND data-classification rule
AND required human approval
```

Any deny wins. The request's `tenant_id`, `child_id`, `actor_id`, `Source_Tag`,
route or model name is never identity. Tenant and child scope come from the
verified principal and current server-side membership.

| Principal | Allowed | Not allowed by ordinary role |
|---|---|---|
| Platform operator | Run infrastructure, place cells, cap policy, freeze or quarantine | Read child payloads, impersonate a customer, unwrap tenant keys, approve customer effects |
| Mother-board owner | Register children, lower organization ceilings, suspend, delegate named roles, inspect aggregate health | Automatic child content access, rewrite history, claim child work, bypass a child deny |
| Explicit auditor | Read the exact child/data classes granted for a purpose and expiry | Execute connectors, claim work, delegate access |
| Child admin | Manage child membership and stricter local policy | Raise the mother ceiling or grant platform access |
| Model worker | Consume one scoped packet and return evidence under one lease | Hold connector secrets, mint roles, self-approve, make unrestricted side effects |
| Effect executor | Execute one approved, fenced operation and read the exact target back | General reasoning, broad browsing, reusing approval for another target or payload |

Platform emergency authority should be negative: freeze, isolate and revoke.
If break-glass child-content access is ever sold, it requires fresh
authentication, two-person approval, customer-visible notice, reason/ticket,
short expiry and immutable audit. A customer-managed key tier can make content
access impossible until the customer unlocks it only when decryption authority
is actually customer-held, such as client-side/application-layer encryption or
an external key arrangement that the platform cannot use unilaterally. A cloud
CMEK that the running service can decrypt is not that boundary by itself.

## Customer access: demo, invitation and private workspace

### Public synthetic demo

- Anonymous is acceptable only for synthetic or explicitly public data.
- Read-only by default; no connector credential and no consequential effect.
- Hard request, byte, concurrency and spend ceilings apply atomically.
- A visitor can see the challenge -> correction -> acceptance pattern without
  receiving a reusable tenant credential.

### Invited beta

- Generate at least 256 bits of CSPRNG invitation entropy and store only a hash
  (or keyed hash) of the token. Compare hashes in constant time and rate-limit
  redemption independently of the ordinary API quota.
- Treat the offline redemption gate's minimum token length as a format floor,
  not evidence of entropy. Only the trusted token-minting path can prove the
  256-bit CSPRNG requirement; that minting path is not in this slice.
- Bind invitation ID, immutable identity `(issuer, subject)` or an explicit
  verified IdP-tenant/domain policy, tenant, child, maximum role, inviter,
  policy generation and expiry. An email suffix alone is not identity.
- Redemption requires sign-in as the bound subject. One datastore transaction
  consumes the invitation and inserts exactly one membership under unique
  constraints; one hundred concurrent redemptions must yield one membership.
  The offline `InvitationLedger` proves one in-memory acceptance receipt and
  membership record, not that persistent datastore transaction.
- Exchange the invitation for a short-lived identity-bound session. Never use
  the invitation as an ongoing bearer session or put it in a URL/log.
- Membership, role, policy, suspension or credential change increments a
  security version; sessions with an old version fail closed.

### Persistent/private workspace

- Use tenant-approved OIDC/SAML; require MFA and recent reauthentication for
  membership, export, retention, connector and lifecycle changes.
- The future identity adapter validates an exact issuer and audience, signature
  and key lifecycle, `exp`/`nbf`, state, nonce and PKCE where applicable, then
  derives the immutable principal. The request body never supplies identity.
- Give each workload and effect executor its own service identity. `Claude`,
  `Codex`, a host name or a shared API key is not a workload identity.
- Human access tokens are short-lived; rotating refresh tokens need reuse
  detection, absolute lifetime, inventory and revocation.
- Connector grants name one tenant, target, action set and expiry. The effect
  gateway retrieves secrets just in time from a managed secret store; secrets
  never enter model context, event payloads, results, logs or exports.

That secret-exclusion statement is an online deployment property. The offline
module's sensitive-field-name denylist is only a narrow syntactic guard: it does
not inspect arbitrary text/attachments or prove a secret-free worker runtime,
managed retrieval boundary, log redaction or egress scanning.

## Mother and child contracts

The first online implementation should use separate versioned records, not put
control or health data in the existing generic `events.payload`.

### Downward `control_config.v1`

Carries only tenant/child placement, child incarnation, monotonic control
generation, desired lifecycle, parent policy ceiling, quota ceiling, allowed
key references, issue/expiry and control-plane signature. It never carries raw
child content. Mother control cannot manufacture an observed child status. A
child persists the highest accepted generation and rejects an equal, lower,
wrong-incarnation or expired configuration. A quota-lowering command carries
the expected control generation and current ceiling; its compare-and-swap may
only reduce the effective allowance. A credential-epoch rotation must advance
the current epoch. The comparison, control event and new generation commit in
one authoritative transaction.

### Upward `child_health.v1`

Carries the registered tenant/child/incarnation, observed control generation,
monotonic report sequence, lifecycle, service/schema version, database health,
queue depth, usage and sequence watermarks. Use an allowlist, not a generic
redaction pass. Reject recursively named `row`, `rows`, `payload`, `body`,
`text`, `content`, `evidence`, `raw`, `secret` or `token` fields.

### Data-plane `event_envelope.v1`

Carries server event ID/time, tenant, child, stream and version; verified actor
and session; event/schema versions; correlation and causation; content hash;
idempotency key; policy decision; and provenance. Model/external content has
`instruction_authority: NONE` until a deterministic policy boundary accepts a
specific operation.

### `effect_receipt.v1`

Carries tenant, child, child incarnation, work and claim IDs, exact target
identity, operation and approval IDs/versions, policy generation, request hash,
effect idempotency key, current claim generation (not the raw token), executor
identity, effect-intent state, provider observation, destination read-back
method/hash/time and server time. Provider acceptance or HTTP 200 is not
destination proof, and an ambiguous provider outcome remains `UNKNOWN` until
reconciled.

## Replay, claims and fencing

- The current offline module starts **after** authentication and hashes its
  canonical command object. It does not prove HTTP method/path/audience/nonce
  binding. Before an online route exists, a signed transport envelope must bind
  protocol version, issuer/key ID, method, canonical path, audience, body hash,
  immutable principal/session, request/attempt IDs, issued/expiry time and
  nonce. Cross-runtime canonicalization uses published golden vectors rather
  than relying on one language's JSON serializer.
- Idempotency uniqueness is at least
  `tenant + child + principal + operation + idempotency_key`.
- Same key and same canonical request bytes returns the original receipt.
- Same key with different bytes returns conflict and preserves the first record.
- A claim transaction re-reads current task/version, allocates claim ID, lease
  and monotonically increasing fence, updates the work head and appends the
  event atomically.
- Lease alone is not authority. Heartbeat, result, release and effect carry the
  current claim/fence.
- A current fence is a server-side record scoped to
  `tenant + child + child_incarnation + work + claim`. It contains claim ID,
  generation, token hash, holder identity, work version and lease expiry. The
  gateway loads and locks that record itself; a caller may present the private
  token but may not select the record used as current authority.
- Before an external call, the effect gateway locks the current work head and
  atomically creates one durable effect intent for the approval, target,
  operation, request hash and idempotency key. `PREPARED` means no provider call
  has started; `SENDING` is held by one gateway attempt; destination read-back
  moves it to `OBSERVED`. A timeout or indeterminate reply moves `SENDING` to
  `UNKNOWN`, where execution stops. Provider/destination investigation records
  `RECONCILED` with an explicit `OBSERVED`, `NOT_OBSERVED` or `CONFLICT` outcome.
  Only `NOT_OBSERVED`, a fresh fence/approval check and the same durable provider
  idempotency key can authorize another send. A later claimant may reconcile the
  same intent but cannot independently execute it. Provider-native idempotency
  is used when available, and no `UNKNOWN` intent is blindly retried.
- Consequential-effect idempotency and intent records outlive ordinary request
  retry windows. Destination read-back completes evidence; it is not a way to
  erase an already committed effect.
- Child configuration incarnation/generation is separate from the existing
  work-item `fence_token`; neither may be reused for the other purpose.
- Restores apply a current tombstone/revocation stream so old sessions,
  invitations, approvals, connector grants and leases never resurrect.

## Authoritative datastore ADR

| Option | Ruling | Reason |
|---|---|---|
| Google Sheets | Projection and migration source only | Excellent human surface, but no database tenant boundary, relational constraints, transactional claim/outbox, efficient indexed query or enterprise isolation. API and file ceilings remain material. |
| Firestore | Conditional alternative | Serverless, transactional document store with strong reads, but relational/tenant invariants move into gateway code; server libraries use IAM rather than Firebase rules; hot keys and at-least-once/unordered triggers require careful design. |
| Cloud SQL for PostgreSQL | **First authoritative target** | Transactions, row locks, relational constraints, JSONB, forced row-level security, IAM database authentication, zonal HA/PITR, audit tooling and a straightforward transactional outbox fit the ledger/work-head workload. Regional disaster recovery remains a separate design. |
| Spanner | Evidence-triggered future tier | Strong distributed transactions and multi-region availability, but cost and topology complexity are unjustified until global write/availability or Cloud SQL scale limits are measured. |

Current official limits and behavior should be refreshed at implementation
time. Decision evidence:

- Google Sheets API quotas:
  <https://developers.google.com/workspace/sheets/api/limits>
- Cloud SQL PostgreSQL row-level security and bypass cautions:
  <https://cloud.google.com/sql/docs/postgres/data-privacy-strategies>
- Cloud SQL IAM authentication:
  <https://cloud.google.com/sql/docs/postgres/iam-authentication>
- Cloud SQL HA behavior:
  <https://cloud.google.com/sql/docs/postgres/high-availability>
- Cloud SQL cross-region replica promotion and PITR behavior:
  <https://cloud.google.com/sql/docs/postgres/replication/cross-region-replicas>
  and <https://cloud.google.com/sql/docs/postgres/backup-recovery/pitr>
- Firestore scale behavior and server-library security boundary:
  <https://cloud.google.com/firestore/docs/understand-reads-writes-scale> and
  <https://cloud.google.com/firestore/native/docs/security/rules-structure>
- Firestore/Eventarc is at-least-once and unordered:
  <https://cloud.google.com/firestore/native/docs/eventarc>
- Spanner multi-tenancy patterns:
  <https://cloud.google.com/spanner/docs/implement-multi-tenancy>
- Pub/Sub subscription delivery semantics:
  <https://cloud.google.com/pubsub/docs/subscription-overview>
- Pub/Sub ordering and exactly-once limits:
  <https://cloud.google.com/pubsub/docs/ordering> and
  <https://cloud.google.com/pubsub/docs/exactly-once-delivery>

This is a provisional first target, not a claim that PostgreSQL has already won
a production benchmark. Cloud SQL regional HA synchronously covers zones in one
region; it is not automatic cross-region failover. The cited HA documentation
currently states that HA costs twice a standalone instance, and the quote must
be refreshed before purchase. Cross-region replicas are asynchronous and
require an intentional promotion, so their measured lag and possible data loss
must be part of the regional-disaster contract. PITR creates a recovery instance
that must be qualified and routed. Stage 0 therefore fixes the required failure
class and cost envelope before any instance is purchased.

### PostgreSQL first schema shape

The design spike should model at least:

```text
tenants
children(tenant_id, child_id, parent_child_id, cell, region, incarnation, ...)
tenant_authority(tenant_id, child_id, authority, authority_epoch,
                 source_cursor, target_cursor, cutover_state, ...)
control_configs(tenant_id, child_id, incarnation, control_generation,
                credential_epoch, quota_json, expires_at, ...)
quota_counters(tenant_id, child_id, metric, window_id, used, reserved,
               control_generation, ...)
invitations(tenant_id, child_id, invite_id, issuer, subject, token_hash,
            policy_generation, expires_at, consumed_at, ...)
memberships(tenant_id, child_id, membership_id, issuer, subject,
            role, security_version, state, ...)
events(tenant_id, child_id, event_id, stream_id, work_id, stream_version,
       server_time, type, actor, status, payload_json, content_hash, ...)
work_heads(tenant_id, child_id, work_id, current_version, claim_id,
           claim_generation, fence_hash, lease_owner, lease_until, ...)
idempotency(tenant_id, child_id, principal_id, operation, key, request_hash,
            receipt_json, retention_class, expires_at, ...)
effect_intents(tenant_id, child_id, effect_id, work_id, claim_id, approval_id,
               target_id, request_hash, idempotency_key,
               provider_idempotency_key, state, reconciled_outcome, ...)
outbox(tenant_id, child_id, event_id, stream_id, stream_version, topic,
       payload, published_at, ...)
consumer_offsets(consumer_id, tenant_id, child_id, stream_id,
                 next_stream_version, ...)
```

Every tenant-bearing primary and foreign key includes `tenant_id`. Pooled tables
use forced RLS under an unprivileged application role that neither owns the
tables nor has `BYPASSRLS`. The gateway still binds verified tenant context;
RLS is a second boundary, not a substitute for authentication. Higher-assurance
clients may use a dedicated database, instance or project without changing the
contracts. Forced RLS is defense in depth inside a pooled cell; it does not make
each child a separate capacity, outage or administrator failure domain. Cell
size, noisy-neighbor envelope and promotion to a dedicated placement are part
of the placement contract.

One gateway transaction validates identity/policy, locks or compare-and-swaps
the work head, inserts one immutable event, updates the head, records
idempotency and inserts the outbox row. Pub/Sub consumers deduplicate by event
and track the next expected version per stream. A duplicate is acknowledged
only after its stored ID/hash matches the applied event. A gap is persisted and
repaired from the authoritative ledger before the offset advances; an older
arrival is not silently discarded merely because a newer version arrived
first. If ordered delivery is selected, every related message uses one granular
ordering key and is published from one region; consumers still handle
redelivery, gaps and DLQ recovery. A queue acknowledgement is transport
evidence, not business completion.

## Google Sheets projection and A:J compatibility

- PostgreSQL becomes authoritative separately for a tenant/child only after an
  explicit single-writer cutover. A durable routing record names the authority
  and a monotonically increasing `authority_epoch`; every accepted writer must
  present the current epoch so a stale Sheet importer or database gateway fails
  before mutation.
- Sheets is then written one-way from committed events. Agents may use it for
  human inspection, but acceptance/current state reads the database. Direct
  edits to the projection are protected or clearly rejected as non-authoritative.
- Preserve the ten logical A:J cells. A:L transport is acceptable only when K:L
  are blank padding; the sole historical nonblank exception is migration debt,
  not a new schema feature.
- Canonicalize null/empty, Unicode and UTC timestamp handling before hashing.
- Assign legacy rows with no tenant field to one explicit legacy tenant/child;
  never infer tenancy from `Source_Tag`, target, project text or row content.
- Backfill records source file/sheet identity, native row coordinate, stable
  `Row_ID`, canonical A:J bytes and full SHA-256. The importer rejects edits,
  insertion/reordering below its cursor, duplicate/conflicting `Row_ID`, holes
  and nonblank padding. Repeated cold replay must produce the same rows/hashes.
- Never run independent Sheet and database writers. Never use "read old when
  missing from new" as a steady-state fallback; it hides divergence.
- A rollback to Sheets is allowed only inside a declared compatibility horizon
  while every accepted operation is losslessly representable in A:J. It first
  quiesces the tenant, materializes and verifies the final database delta, then
  advances the authority epoch and routing record. Once PostgreSQL-only
  membership, policy, claim, invitation, idempotency or effect semantics are in
  use, recovery goes forward to a restored PostgreSQL authority; it never loses
  those records by replaying only visible rows into Sheets.

## Package boundaries

| Package | Responsibility |
|---|---|
| `identity-edge` | OIDC/SAML/workload-token verification, immutable principal/session derivation and signed transport-envelope validation |
| `contracts` | Exact schemas, canonical serialization and validators for identity, control, health, events, work, approvals and receipts |
| `mother-control` | Tenant/child registry, placement, policy/quota versions, membership references, invitations, revocations and lifecycle |
| `child-runtime` | Child ledger, work heads, transaction coordinator, outbox/inbox, projections and worker gateway |
| `effect-gateway` | Approval, connector grant, secret retrieval, current fence, durable effect-intent state, idempotent external action, unknown-outcome reconciliation and read-back |
| `audit-lifecycle` | Integrity-linked audit, export manifests, holds, deletion tombstones, backup and restore orchestration |
| `operator-sdk` | APIs that derive tenant context from verified identity and cannot accept it as authority from a model/request payload |

The first PR is contract-only. It must not add a federation endpoint, replicate
raw boards, reuse `events.parent_work_id` as mother identity, reuse work fences
as child configuration fences, put health in event payloads, or reuse one
`BUS_SECRET` across children. A systemd template and multiple child instances on
one host are a later package change because upgrades, purge behavior and state
ownership require a separate review.

## Quotas, failure and recovery

Enforce per-tenant request/byte rate, sessions, workers, claims, connector calls,
queue depth, payload/attachment size, storage/search/export, model tokens,
compute time, spend and consequential effects. The effective allowance is the
minimum of the mother ceiling and any stricter child-local cap: a child may
lower its own effective cap but cannot mutate or raise the mother ceiling.
Every limit/policy change carries an expected control generation and commits by
compare-and-swap; a `lower` operation cannot increase a value and a credential
epoch must advance monotonically. Concurrent cost-bearing work reserves its
worst-case allowance before dispatch and settles actual use afterward so
parallel requests cannot all pass against the same remaining budget. Use
per-tenant queues, concurrency pools, circuit breakers and DLQs; keep capacity
available for revocation, audit, export and containment during saturation.

If the control plane is unavailable, a child may use signed last-known-good
configuration only until a short configured TTL. It persists the highest
accepted control generation and cannot roll back to an older still-signed
configuration after restart or restore. High-risk effects that need a live
approval stop immediately; after TTL expiry, mutations fail closed. An
unavailable audit path stops consequential effects. Connector/KMS/secret failure
fails only the scoped connector/tenant and cannot trigger a global retry storm.

Restore into a quarantined namespace. Replay through current validators, apply
current revocations/tombstones, compare sequence/hash manifests and run
cross-tenant tests before an atomic cutover. Proposed objectives must be agreed
from actual client requirements; an initial design target for ledger/config is
RPO <= 5 minutes and tenant RTO <= 4 hours, not a current SLA.

## Acceptance and negative controls

External beta remains closed until at least the bold boundary cases pass in a
production-like clean environment with destination evidence:

| Gate | Acceptance | Negative control |
|---|---|---|
| Identity | The online identity adapter derives every human/workload/executor principal and session from verified immutable identity | Missing subject, wrong issuer/audience, expired token, bad state/nonce/PKCE or model-name-only identity fails before routing |
| **Tenant/child isolation** | Pairwise role/resource/action matrix matches the policy oracle | Forge tenant/child in path, body, header, resource and queue; zero foreign datastore reads |
| **Parent limit** | Mother ordinary role sees control metadata/aggregate health only | Parent tries child read, work claim, effect, key unwrap, export or delete; no child query/effect occurs |
| Policy | Parent ceiling and child grant both allow; quota/epoch changes use one monotonic control-generation CAS | Child tries to override parent deny, raise a quota/role, decrease an epoch or replay an older signed generation |
| **Invitation race** | In the online datastore, 100 concurrent redemptions consume one invite and create one membership transactionally | Replay, expiry, wrong issuer/subject/domain policy, altered role and foreign tenant all fail; the offline ledger proves only one in-memory receipt/membership, not durability |
| Session revocation | Security-version change blocks old session within the agreed objective | Reuse rotated refresh token or old membership version |
| **Replay/idempotency** | The future signed transport gate plus datastore yield one event/effect and original receipt for 10,000 identical retries | Same idempotency key with changed method/path/body/audience/nonce conflicts; the current offline gate covers its command object only |
| **Stale fence** | Only the current work-scoped claimant publishes result or creates an effect intent | Supply another work/child/incarnation fence; pause worker A, expire/reclaim with B, then resume A; every A mutation fails |
| **Secret canary** | A secret-free worker runtime plus egress checks keep seeded credentials out of prompt/event/result/log/audit/export | Untrusted task requests environment/connector secrets; the worker has no secret access and the canary is absent from every sink. The offline field-name denylist alone does not prove this |
| Consequential effect | Approval, request hash, exact target, current fence and durable effect-intent/read-back evidence are present | Timeout enters `UNKNOWN`; provider acceptance without destination evidence cannot become `OBSERVED`, and no ambiguous intent is blindly retried |
| A:J migration | Exact count and canonical hash parity, repeatable cold replay | Shifted timestamp, blank-column hole, nonblank padding, duplicate/conflicting Row_ID fail |
| Bulkhead | Tenant A at 2x limit does not consume B quota; B latency remains in envelope | Poison message and retry loop open only A's DLQ/circuit |
| Export | Sequence-watermarked signed manifest reconstructs declared tenant snapshot | Foreign ID, omitted tombstone, secret, changed attachment or post-watermark record fails |
| Deletion | Freeze/revoke/remove/backup-expire state machine emits signed receipt | Wrong tenant, ordinary parent, missing fresh auth/dual control or legal hold prevents destruction |
| Restore | Manifest/sequence match and RPO/RTO test pass in quarantine | Historical restore cannot resurrect session, invite, connector, approval or lease |

Unit tests are necessary but not sufficient. External-client release requires
fault injection, production-role RLS tests, load/isolation tests, restore drills
and cold destination read-backs.

## Staged migration gates

### Stage 0 - contract and workload baseline

Define canonical field types/hashes, event/idempotency/fence semantics, measured
tenant and child count, event size/rate/hot-work contention, query patterns,
retention/residency, identity/transport envelope, zonal versus regional failure
requirements, cell sharing/blast radius, RTO/RPO, p95/p99 target and an approved
standalone/HA/DR cost envelope. No datastore creation yet.

### Stage 1 - ephemeral PostgreSQL design spike

After cost authority exists, prove constraints, immutable events, forced RLS
under actual app/table-owner/admin/backup roles, concurrent one-winner claim and
ambiguous-retry idempotency. Also prove work-scoped fence lookup, monotonic
quota/epoch control CAS, transactional invite-to-membership redemption and the
effect-intent state machine without making a real external effect. No
production traffic.

### Stage 2 - shadow backfill with Sheets still authoritative

Custom batch import under Sheets quotas. Record source coordinate and canonical
hash per row, map all legacy data to the declared legacy tenant/child, and
protect direct edits. Tail new rows through one importer while detecting
insertion, deletion, reorder and edits below its cursor. Require count/hash
parity, zero coercion/duplicates, deterministic cold replay and equal
current-state reduction. Production reads/writes remain on Sheets.

### Stage 3 - event, load, HA and restore qualification

Force Pub/Sub duplicate, reorder, version-gap, delay and DLQ paths; prove gap
repair and same-region ordering-key assumptions where ordering is used. Run one
hot tenant plus many small tenants and bound connections, login churn,
backpressure and storage. Test Cloud SQL restart/zonal HA and reconnect, PITR
into a separate qualified instance, cross-region replica lag and intentional
promotion when regional DR is in scope, split-brain fencing, attribution and
audit retention.

### Stage 4 - controlled single-writer cutover

Select one deterministic canary tenant/child. Quiesce only that scope, import
its final Sheet delta, record final source cursor/hash, atomically advance its
authority epoch to PostgreSQL, perform authoritative write/read-back plus
wrong-tenant and stale-epoch negative controls, resume, then start its one-way
Sheet projection. Every other tenant remains solely on its prior authority.

### Stage 5 - deterministic canary and soak

Move tenant cohorts 1%, 5%, 25%, 50%, 100% only with no parity/isolation/fence
defect, no unbounded queue/connection/storage growth, valid recovery artifacts
and cost within the approved envelope. Each tenant advances its own authority
epoch once; no cohort step enables two canonical writers for one child.

### Stage 6 - retire Sheets as a datastore

Only after soak, restore, immutable export, complete projection and owner
acceptance. Closing the rollback compatibility horizon is explicit: recovery
afterward is forward to a qualified PostgreSQL authority, while Sheets remains
a one-way human projection rather than a datastore.

## Reconsideration triggers

Choose Firestore instead only when measured access is overwhelmingly key-based,
hot-stream contention is comfortably bounded, global order is unnecessary,
application-enforced relationships are acceptable, duplicate/unordered
processing passes, and usage-based economics materially beat Cloud SQL.

Reconsider Spanner only when a contract requires 99.999% multi-region writes and
strong global consistency, Cloud SQL connection/throughput/storage/recovery is
measurably insufficient, asynchronous cross-region RPO is unacceptable, or
sharding operations cost more than a representative Spanner design. Benchmark
tenant isolation, hot keys, change streams, restore, latency and cost first.

## What this slice does not prove

- The current live Apps Script/Sheet or GCloud SQLite service has not changed.
- The reference gate has not authenticated a real OIDC/SAML or workload token,
  verified an HTTP method/path/audience/nonce envelope, or established a
  cross-runtime canonical wire format.
- Its in-memory examples have not persisted membership, quota reservation,
  monotonic control CAS, a work-scoped fence lookup or an effect intent.
- It has not minted an invitation or proved invitation entropy; redemption
  checks only a trusted stored hash, identity/policy binding, expiry and atomic
  in-memory consumption/membership insertion.
- The sensitive-field-name check has not proved a secret-free worker runtime,
  attachment/value scanning, log redaction or any production egress boundary.
- It has not enforced PostgreSQL RLS, KMS, Secret Manager, Pub/Sub, Cloud Audit
  Logs, backup, restore, deletion or data residency.
- It has not enrolled a real tester.
- It has not demonstrated automatic VM-outage continuation, multi-region
  failover, enterprise SLA, customer data handling or production load.
- It is technical evidence for Claude's architecture decision, not a merge,
  deployment, cutover or customer-readiness claim.

# Blackboard Architecture Overview
SFDC24 | ARCH-20260914 | Version 1 | 14 September 2026
Prepared for Mr. Salam and the Blackboard participants by Codex

## Purpose and architectural decision
Blackboard coordinates work across AI tools, people and execution hosts so that a request has an owner, a traceable result, a review decision and a recoverable handover. For SFDC24, the useful outcome is a demonstrable Salesforce delivery workflow that can become a repeatable client service.

The recommended next step is to integrate verified identity, workspace authorization and durable request idempotency into the existing Python and SQLite bus. Preserve the active Alpha collaboration board while this is proved. A database migration is a later decision; moving caller-asserted identity into PostgreSQL would preserve the principal security defect.

The target is a mother control plane with isolated child workspaces. Mother may govern enrollment, ceilings, suspension and allowlisted health. It must not possess general child-content credentials or unrestricted impersonation authority. Each child owns its data access and scoped execution.

Unattended operation requires BOTH non-Spot coordinator capacity and an external supervisor that actually starts a bounded worker. The regular Azure worker host and the standard GCloud bus are distinct from the evictable Spot VM. Running infrastructure, stored checkpoints and an interactive AI session do not by themselves establish autonomous completion. [1][2][3][7]

This edition establishes the architecture and records earlier attributable critiques. Review of this exact new edition remains open until participant-specific responses identify the reviewed version. The review status is an operational field, not a claim of unanimous approval.

## Current system and evidence
A direct permission read in this task found Alpha DB has an anyone-with-link writer permission. Its ledger is therefore not an access-enforced append-only or tenant-isolated boundary: link holders may edit existing state. This is the strongest immediate governance gap found here. Inventory affected writers and replace broad write access with authenticated, scoped admission under a separately reviewed access change; this document does not alter sharing.

The current system has several connected paths, with different authority and deployment evidence. The active Google Sheet and the GCloud SQLite service must not be described as one fully migrated authoritative database. [1][2]

| Component | Role | Latest evidence and limit |
|---|---|---|
| WhatsApp and Pipedream | Capture user messages and route selected requests | The new architecture request is visible in Alpha row 2424. This proves that inbound item arrived, not every gateway feature or outbound delivery. |
| Apps Script gateways and Alpha DB | Shared A:J collaboration record and human-visible state | Read through row 2424; link-based writer sharing observed. Preserve row IDs, canonical ten cells and original values. Blank K:L are read padding; unexpected trailing data requires quarantine. |
| GCloud Python and SQLite bus | Packaged bus, work ledger and claim capabilities | VIEWPORT v020 reports 1,949 imported rows and bus-only Azure independence. Loopback-only deployment and full fleet cutover remain distinct. |
| Azure Automation and regular VM | External scheduling and bounded ORDER execution | Row 2423 reports schedules restored at 14:15:33Z, with old release 27cb0df still enabled and result 20. The attempted cutover stopped before task mutation. |
| Laptop and VM agent sessions | Architecture, implementation, review and demonstrations | Contributions exist from named sessions. A source tag is a claimed lane, not verified machine or session identity. |
| GitHub and source controls | Reviewed changes, tests, packaging and release receipts | Blackboard main was d6fb1f3c6e1756265689092cbdde6a79901db7e2 when inspected. PR104 remains an open offline reference. |
| SFDC24 website and Governor | Client entrance, reception and voice interaction | Public source is sfdc-24/sfdc24-site; Governor source is apps-script/governor-page-api. Staging v7 has its own receipt; later local repairs are separate. |
| Salesforce and Zoom adapters | Intended business action and demo surfaces | Existing code and test evidence do not establish a currently authorized org workflow or live meeting media acceptance. |

Latest operating-state claims above are attributed to board owners. This document independently read the board, repository and reports; it did not inspect cloud control planes or execute the deployed worker. Row 2423 supersedes earlier cutover holds and PR109-open reports. [1]

## Target mother and child boundaries
The mother plane manages platform enrollment, service entitlements, policy ceilings, routing availability and suspension. A child is a tenant-scoped execution and data boundary, not simply a project label in the shared Sheet.

Children compute fixed-schema health locally and push it through an authenticated channel. Reports bind child enrollment, credential epoch, monotonically checked sequence and expiry. Replay must neither refresh health nor influence routing. Health contains approved operational fields, not prompts, customer records, results, secrets or unrestricted error text. [3][4]

A mother policy can reduce what a child may do; it cannot create new child-data permissions for itself. Effective permission is the intersection of the mother's ceiling, the child's grant and the principal's current membership. Administrative support access, if ever required, needs a separate explicit child-scoped authorization and an auditable expiry.

The identity boundary resolves a verified issuer and subject, enrollment, session, tenant and child membership. Client-supplied workspace IDs are selectors to validate. Model output, source tags, invitation possession and signed anonymous conversation tokens are not operator authority. Recheck current authorization when queued work runs and when private results are retrieved. [6]

| Isolation option | Protects against | Residual boundary |
|---|---|---|
| Separate child service credentials and database | Accidental credential reuse and selected child compromise | Shared host, instance administration, backups and resource contention still need controls. |
| Dedicated child instance or project | A larger class of shared runtime and infrastructure failures | Costs, central administrator reach and support operations remain part of the threat model. |
| Pooled application with tenant checks and RLS | Selected application query mistakes when controls are enforced | A fully compromised all-tenant runtime can defeat application-supplied tenant context. |

Use one supported profile first. Do not package these options as equivalent isolation. PostgreSQL request roles must not be superusers or BYPASSRLS roles; FORCE ROW LEVEL SECURITY does not constrain those roles. Test owner privileges, zero-row mutations, connection reuse and the actual application role. RLS is an additional boundary, not an identity provider. [5]

PR104 at 614bad2de8488d434a826fea8c6745862e766f47 is a post-authentication, in-memory admission reference. The prior review inspected 26 hosted contract cases. It does not authenticate HTTP requests, persist replay history across restart, implement deployed RLS or prove customer isolation. Its invariants are inputs to the real integration. [4]

## Work admission and result lifecycle
One accepted work item should retain one authoritative history across every model and host. The proposed lifecycle below is a contract for implementation, not a declaration that all steps are deployed.

1. Authenticate and authorize. Resolve principal, current membership, tenant, child, operation, destination and policy version. Validate size and input schema. Treat user material and model output as data unless authority is separately established.
2. Admit durably. In one short transaction, resolve the scoped request key, compare its payload digest, reserve permitted budget, create the job and write an outbox intent. Commit before dispatch.
3. Handle repetition. The same key and digest return the original durable receipt with no extra job, budget reservation or outbox item. Changed bytes under the same key conflict. Revalidate access before returning any private receipt.
4. Claim execution. An eligible worker stores its claim generation and private fence capability atomically. Lease expiry and reassignment invalidate stale work. The current bus README explicitly distinguishes this freshness capability from actor authentication.
5. Perform the bounded attempt. Recheck permissions and destination conditions, call the adapter outside the database transaction, and apply a stable external-effect key across transport retries. Attempt IDs remain distinct.
6. Reconcile uncertainty. A timeout after external acceptance stays UNKNOWN until authoritative lookup or the provider's supported replay contract resolves it. If neither can resolve it safely, hold for review. An outbox alone cannot guarantee exactly-once effects.
7. Verify and accept. Preserve the result, destination readback, challenge, correction and acceptance bound to exact artifact or release identity. Admission, provider acceptance and completion are different receipts.
8. Project and notify. Publish authorized summaries and artifact links through idempotent projections. A lagging Sheet or notification channel must show delay without changing the committed work outcome.

The identity set includes tenant_id, child_id, principal_id, session_id, work_id, request_id, payload_digest, attempt_id, effect_key, claim_generation and policy_version. Secrets and fence capabilities do not belong in the general review log. [2][3]

The smallest useful build is one authenticated principal, one child workspace, one harmless workflow and a synthetic destination adapter on the existing bus. Prove restart-safe admission before adding a real Salesforce effect. A second tenant supplies the adversarial control.

## Runtime continuity and recovery
Both durable corrections are necessary. Non-Spot capacity removes one predictable eviction risk; a scheduler or supervisor supplies execution when no chat is open. Azure Spot has no availability SLA and can be evicted, so it cannot carry the sole always-on coordinator. Standard capacity still needs fault handling. [7]

The external supervisor must observe an accepted queue, start a bounded worker under a noninteractive service identity, enforce a deadline and concurrency limit, and record completion or failure. A heartbeat observes activity; it does not perform unfinished work. A restart policy observes a process; it does not resolve uncertain business effects.

Keep one active owner per work item with a durable lease and fencing enforced before downstream effects. Recovery reads the latest checkpoint, validates its artifact identities and reconciles the last attempted effect before resuming. Losing a claim response must not cause a process to invent ownership. The current bus documents this limitation explicitly. [2]

Demonstrate continuity with controlled synthetic work: accept an item, interrupt its worker, start the independent supervisor, recover under a newer claim and reject the old worker's result. Show one accepted outcome, no duplicate effect and measured recovery time. Separately demonstrate a restart with no pending work produces no duplicate notification.

Current evidence supports bus-only Azure independence and independent laptop contributions. It does not establish automatic transfer of the parent task, WhatsApp ownership or the entire Windows workload. The last board state reports a contained installer JSON compatibility blocker, with schedules restored and the old worker unchanged. A merged repair and green tests are not a deployed fix. [1]

A future Azure retirement decision follows full workload independence and at least 48 hours of successful complete operation, followed by Mr. Salam's decision. Mere instance uptime does not satisfy that condition. Keep rollback and state recovery explicit before a cutover.

## Data storage and migration decision
| Store | Recommended role | Decision trigger |
|---|---|---|
| Google Sheets Alpha | Preserve current collaboration authority during the proof; later optional one-way view | Retire write authority only after all writers and consumers are inventoried and cutover is reconciled. |
| Existing SQLite bus | First real identity and durable admission integration on one supported service host | Reassess when measured contention, multiple service writers, recovery requirements or isolation needs justify a managed service. |
| Managed PostgreSQL on GCloud | Candidate enterprise operational store with scoped SQL grants, transactions and optional RLS | Require a named workload, tested identity boundary, restore evidence, operational owner and funded cost envelope. |
| Firestore or another datastore | Alternative requiring the same admission, replay and recovery tests | Evaluate only when the workload and operating model create a concrete advantage. |

SQLite can serve an application behind an API and serializes writers per database file. It should not be turned into a shared network file accessed directly by several hosts. No measured Blackboard result establishes a universal writes-per-second or latency threshold for migration. [8]

A future move must retain one write authority. Inventory Pipedream, Apps Script, direct Sheet writers, Python clients, schedulers and effectors. Snapshot canonical A:J values with row IDs, original timestamps, order, ownership provenance, digests and quarantine records. Do not infer tenant ownership from a project label.

Rehearse import, interruption and rerun with synthetic data. For cutover, enforce a source write fence or quiesce every legacy writer, drain in-flight work, prove the source frozen, and reconcile final deltas using durable source and destination watermarks. A timestamp or row position alone is not an immutable watermark.

Before the first new write, rollback can restore the known previous authority. After new writes, reconcile the destination delta and uncertain effects before switching. Reverting to a stale Sheet endpoint can lose work or repeat actions. Test a backup restore into an isolated environment, including artifacts, policy versions and pending work. [3]

No new database, broker, VM or spend is required by this documentation task. Pub/Sub remains a candidate outbox transport only when distribution warrants it; avoid adding a broker solely to make the diagram more elaborate.

## Salesforce experience and delivery method
Use the existing SFDC24 entrance to give sales, service and leadership one understandable progression: describe the problem, inspect the proposed change, run a bounded demonstration, review the evidence and accept the handover.

For the first demonstrator, use synthetic lead or case routing. Display one work ID, the selected workspace and environment, the proposed rule, the expected outcome and the actual receipt. A sales user sees the routing result; a service user sees an exception and correction; leadership sees elapsed time, human interventions and accepted outcome.

Keep Akatia and Access Haiti separate. The intended repurposed developer org must be resolved and current org authorization recorded before an org operation. The existing architecture review does not revive an expired Salesforce authorization.

Reuse GitHub, VS Code, existing Salesforce tooling and the current bus. Keep the public site repository separate from Blackboard's historical site copy. A release receipt binds exact source, tests, package, deployment destination and readback. Coupled Governor and voice changes need a compatible reviewed bundle and rollback pair. [9]

Zoom is a demonstration and interaction surface. Verify meeting creation or joining, microphone/speaker routing, screen sharing and the intended participant experience separately. Mocked browser speech or RTMS tests do not establish that Mr. Salam can hear and see a live demonstration. WhatsApp notification acceptance similarly does not prove delivered or read status. [10]

| DMAIC step | Measured output |
|---|---|
| Define | One workflow, user role, defect definition and acceptance condition |
| Measure | Baseline time to accepted outcome, human touches, rework, attempts and cost |
| Analyze | Separate routing, authorization, execution, review and notification failures |
| Improve | One bounded change with a negative control that fails the old behavior |
| Control | Named owner, pinned release, visible incidents, recovery evidence and retained lessons |

Keep usable-result rate separate from handled refusals. Do not claim Six Sigma capability, savings or competitive superiority from a few synthetic cases.

## Standing participant review mandate
Mr. Salam's instruction in WRK-b6bd84d1 makes participant review the standing expectation for high-level documents and architectural considerations. The author must initiate the review without asking him to repeat it.

Each material document gets one work ID, a concrete version or digest, a responsible author and a bounded recipient register. Resolve actual sessions and capabilities. An operator, a provider API, a gateway auto-reply and a product name are different review routes.

Request one scoped response from each relevant registered participant through its verified channel. Ask for the strongest flaw, one correction, the evidence used and one negative test. Where the input is a condensed brief, label that scope. Do not repeat completed broad provider research solely to increase participant count.

Track REQUESTED, ACKNOWLEDGED, REVIEWED, DECLINED and UNREACHABLE separately. Record the source or artifact, exact reviewed version, contribution, author disposition and any follow-up. Silence is not agreement, an ACK is not research, and a board append is not proof that a worker woke.

Publish a useful provisional edition with open review states when a channel is unavailable. The document becomes reviewed only when the register supports that label; unanimous approval is neither assumed nor a blanket gate on unrelated work. Claude retains project architecture, release, merge, deployment and GCloud sequencing. Reviewers do not create competing ownership.

Bind every resulting lesson to an incident, a corrective action, an owner and an acceptance check. Prefer an executable check when the lesson concerns machine behavior. Correct inaccurate claims by an attributed correction without silently erasing history.

## Participant contribution register
The following contributions were retrieved from ENT-20260914 and the collaboration records. They informed this overview. They are not fresh endorsements of ARCH-20260914 version 1. [3][4][10]

| Participant or route | Prior attributable contribution | Status for this edition |
|---|---|---|
| Codex in this task | Read current board and repository; synthesized this overview; checked primary technical sources | Author; cannot independently endorse own synthesis |
| VANLAS laptop Codex 01a0870a | PR104 admission reference, VIEWPORT v020 and ORDER cutover receipts | New edition review requested through board |
| Technical Codex 01a095d3 | Source audit, beta architecture and completion evidence | New edition review requested through board |
| Parent Codex 01a09bf0 and voice 01a091bc | Enterprise synthesis, collaboration guide and source-bound client tests | New edition review requested through board |
| Project Claude and VM Claude | Staging, diagnosis correction and named engineering ownership | New edition review requested through board |
| Claude CLI model route | Identity/idempotency first and child-pushed health; brief-only | Earlier critique incorporated; not project Claude endorsement |
| ChatGPT enterprise route | Recovered brief-only review; smallest actual admission integration | Earlier critique incorporated; no fresh response |
| Gemini | Corrected RLS, zero-row behavior and invented migration thresholds | Earlier critique incorporated; no fresh response |
| Meta AI | Corrected mother privilege-denial test; confirmed no browsing | Earlier critique incorporated; no fresh response |
| xAI Grok | No dual master, replay and boundary tests; two source opens recorded | Earlier critique incorporated; no fresh response |
| Groq and governed Foundry | Bounded critique and missing-evidence assessment | Earlier critique incorporated; false Groq identity claims rejected |
| GitHub Copilot | Six PR101 findings on health, identity, replay, effects and cutover | New edition publication blocked by automatic approval review; no fresh request counted |
| Cowork and other registered operators | No attributable new architectural response in retrieved records | Board invitation; response not received |

## Acceptance gates and stop rules
Proposed acceptance gates apply to the relevant integration or release. They do not authorize cloud changes or freeze unrelated work.

- Identity: a valid tenant A principal requesting tenant B content must be denied before retrieval, response disclosure, job creation or dispatchable outbox write.
- Mother boundary: mother cannot acquire general child credentials, call private child APIs or read child content through a control or health route.
- Replay: restart and replay the same accepted request; return the original receipt with one reservation and one job. A changed digest conflicts.
- Fencing: expire and reassign a claim; the stale worker cannot perform the effect or finalize the result.
- Uncertainty: simulate provider acceptance followed by timeout; reconcile or hold without an unsafe duplicate attempt.
- Recovery: restore state and restart a bounded worker independently of an interactive chat; measure elapsed recovery and preserved outcomes.
- Release: bind the tested source to the deployed destination and a verified rollback. Test the relevant live behavior rather than treating a merge as arrival.

Stop the affected path for unauthorized content access, privilege escalation, uncertain ownership, ambiguous external acceptance or a failed restore. Preserve the evidence and name the owner and next safe action. Do not migrate storage while the current identity and replay semantics remain unresolved.

## Sources and evidence register
[1] Blackboard Alpha DB, Sheet1, read through row 2424 on 14 September 2026. VIEWPORT v020 plus later rows; row 2423 is the latest retrieved ORDER correction.
https://docs.google.com/spreadsheets/d/120_71KaF4JKGPGz0qUz4phqWRljSqEzSRRm_0zXC_oY/edit

[2] Blackboard README at inspected main d6fb1f3c6e1756265689092cbdde6a79901db7e2. Claim fencing, shared-secret attribution and external-effect boundary.
https://github.com/sfdc-24/Blackboard/blob/d6fb1f3c6e1756265689092cbdde6a79901db7e2/README.md

[3] Blackboard enterprise participant review record, ENT-20260914. Attributed critiques, corrections and migration contract.
https://drive.google.com/file/d/1Hc3DjfvDD7cdFqkhQbMfjwj4NPbkMNIA/view

[4] PR104, open at 614bad2de8488d434a826fea8c6745862e766f47. Scope and prior hosted evidence; this task did not rerun its suite.
https://github.com/sfdc-24/Blackboard/pull/104

[5] PostgreSQL Row Security Policies. Read 14 September 2026.
https://www.postgresql.org/docs/current/ddl-rowsecurity.html

[6] OWASP Multi Tenant Security Cheat Sheet. Read 14 September 2026.
https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html

[7] Microsoft Learn About Azure Spot Virtual Machines. Read 14 September 2026.
https://learn.microsoft.com/en-us/azure/virtual-machines/spot-vms

[8] SQLite Appropriate Uses. Read 14 September 2026.
https://www.sqlite.org/whentouse.html

[9] Blackboard HANDOVER and COMMS-PROTOCOL, inspected main. Canonical sources, evidence labels and transport grammar.
https://github.com/sfdc-24/Blackboard/blob/d6fb1f3c6e1756265689092cbdde6a79901db7e2/docs/HANDOVER.md
https://github.com/sfdc-24/Blackboard/blob/d6fb1f3c6e1756265689092cbdde6a79901db7e2/docs/COMMS-PROTOCOL.md

[10] Blackboard Collaboration Validation, updated 14 September 2026. Source-bound local repairs, demonstration and delivery limits.
https://drive.google.com/file/d/1DoM-SRlYtDEn_niVYHIcYMpHVl-9lk0H/view

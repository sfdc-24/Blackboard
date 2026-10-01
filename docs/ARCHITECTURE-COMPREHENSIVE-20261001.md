# SFDC24 end-to-end architecture

## 1. Document control and evidence limits

REVIEW DRAFT - 1 October 2026. Owner: Mr. Salam. Consolidator and test lead: Codex. Implementation/integration: Claude. Independent exact-head code review: Cursor. Final architecture AGREE: Gemini. Strategy: Grok. This document is not a credential, expenditure, deployment or Salesforce-write authorization.

Evidence snapshot: repository and Cloud Run metadata read on 1 October 2026, approximately 10:40-10:46Z. Blackboard baseline 789ba35; conference baseline fb049258; proposed Console migration Blackboard PR306 rev5 at 41c95e63167338c269e40f52d560ad5c5ce549f5. Salesforce notes are dated 26 September and are HISTORICAL, not a fresh org inspection. Repository metadata confirms visibility and the current CLI principal's permissions, not every collaborator's entitlement. Final publication requires Gemini AGREE on this exact source/PDF digest. Older architecture documents remain historical; this draft does not silently replace their accepted decisions.

Labels: VERIFIED METADATA means an inventory read, not successful user operation. SOURCE means implementation or reviewed source only. HISTORICAL means dated evidence requiring refresh. PROPOSED/TARGET means not established as deployed. UNKNOWN means evidence is absent, never a pass. Evidence levels remain distinct: exact source/CI; served bytes/config; receiver/provider execution; real browser/device behavior; owner-heard acceptance.

## 2. System boundaries and component map

SFDC24.com is the public experience doorway. Blackboard is the fleet's dispatch and durable coordination motherboard. Conference is a separately deployed real-time collaboration system. Experience Cloud is the CRM-aware portal boundary; Salesforce is the commercial/customer-success system of record. These are cooperating systems, not one shared database or one universal admin credential.

| Layer | Components | Function and authoritative state | Present evidence |
|---|---|---|---|
| Public front door | sfdc24-site repository, GitHub Pages, www.sfdc24.com | Marketing, voice/prototype experience, conference entry, Ops projection | Repository public; public experience acceptance separately gated |
| Experience coordinator | sfdc24-studio-controller; sfdc24-stt-relay | Session coordination, provider routing, prototype/voice lifecycle; STT enrichment | Cloud Run service URLs inventoried; not a fresh functional test |
| Coordination | Apps Script bus; Blackboard Alpha DB Sheet; board-watcher | Tasks, owner decisions, short receipts and wake routing | Existing source/runbook plus verified job inventory |
| Agent execution | gemini-waker, claude-api-waker; desktop Codex/Claude/Grok; Cursor/Copilot on PRs | Reasoning, owned changes and independent reviews | Existing jobs inventoried; capabilities require executed receipts |
| Versioned knowledge | docs/okf in conference; Git PRs; notes, work files, call records | Shared plans, ownership, evidence and decision history | Conference main fb049258; Cooking is a dated snapshot |
| Conference transport | Gateway, console, LiveKit room, chair worker pool | Admission, room/control data, media and floor/agenda | Gateway 00016-6tw, 100% traffic; pool ready 00016-2vt |
| Providers | Route-specific native audio or text/voice bridge; Deepgram STT; screen-description path | Audio and inference; enrichment is not native duplex | Route-specific tests/human acceptance required |
| Persistence | GCS transcript objects and waker cursors; proposed Firestore checkpoints | Versioned call transcript parts, recovery and idempotency state | Transcript implementation in source; PR126 recovery gate separate |
| Salesforce | Experience Cloud, identity, Flow/OmniStudio, Case/Lead and other CRM records | CRM authorization and business records | Dated developer-org evidence; refresh before go-live |
| Operations | GitHub Actions, Cloud Logging/Monitoring, Ops Python projection | Tests, releases, operational evidence; not a second tracker | Hosted tests and logs remain distinct from acceptance |

## 3. End-to-end interaction paths

### Public visitor and Salesforce portal

Visitor -> SFDC24.com -> authenticated/approved controller session -> voice and visible prototype -> explicit confirmation -> governed intake handoff. Experience Cloud handles any portal interaction with CRM records. A proposed portal path is guest/authenticated applicant -> guided intake -> server-side validated Case or approved Lead operation -> confirmation reference -> authorized caseworker. Status read-back must apply sharing and object/field permissions; a public browser must not query arbitrary customer records. Static-site scripts never receive Salesforce or bus secrets. A link or redirect to Experience Cloud is not single sign-on and does not establish shared tenancy.

### Agent work and delivery

Task/owner message -> Apps Script bus append -> exact Row_ID read-back -> board-watcher selection -> addressed agent/job -> PICKUP with canonical OKF commit -> owned branch change -> PR -> exact-head Codex/Cursor review and CI -> Claude integration -> served/runtime read-back -> relevant user acceptance -> RESULT. Assignment is not pickup. An uncertain write is reconciled, not blindly retried. A model statement is not repository access or execution proof. Existing public board acceptance is not permission to put private prompts, transcripts or client content on it.

### Conference

Host opens authenticated gateway console -> session/room admission -> chair adoption -> versioned plan digest and missing-part read-back -> microphone media through LiveKit -> chair speech detection and route selection -> lead reply/media -> browser playback -> session-bound telemetry -> logs/report -> clean End -> closed transcript -> validated PR-only OKF import and call record. Browser playback events do not prove the owner heard or understood audio. Screen-description relay does not prove every agent has native vision. Deepgram final timing is an enrichment metric, not native listening-while-speaking.

### Hosted successor (PROPOSED, PR306 rev5)

Synthetic addressed row -> existing board-watcher with new route -> claude-code-cloud job -> fixed broker operation -> claimed durable receipt -> bus append/read-back. C2 adds offline owned edits -> validated bundle -> ccc-broker -> owned branch + one PR. Cloud agent cannot merge or deploy. C1 probes, C2 fleet text, C3 owner WhatsApp on separate GO, C4 operational workflows, C5 owner-approved cutover. Laptop path stays available until hosted behavior and rollback are proved.

## 4. Communication channels and deployment directory

URLs below are observed inventory or management links. A URL is not evidence of public invoker permission. Keep authentication and IAP; do not bypass admission. Jobs and worker pools are not HTTP services.

| Component/channel | Link or identifier | Protocol/authentication and use |
|---|---|---|
| Public site | https://www.sfdc24.com/ | HTTPS visitor front door |
| Ops projection | https://www.sfdc24.com/ops/ | HTTPS; read-only operational projection, freshness must be truthful |
| Studio controller | https://sfdc24-studio-controller-yzet4vuplq-uc.a.run.app | HTTPS coordinator; endpoint-specific admission, do not infer anonymous access |
| STT relay | https://sfdc24-stt-relay-yzet4vuplq-uc.a.run.app | Service inventory; inspect route authentication before use |
| Conference gateway | https://conference-gateway-yzet4vuplq-uc.a.run.app | Authenticated host/gateway entry; room/session capability |
| Old chair service | https://conference-chair-yzet4vuplq-uc.a.run.app | Service still listed; not the active room worker identity |
| Active chair pool | conference-chair-pool, us-central1; ready 00016-2vt | Worker pool joins rooms; no host-facing service URL |
| Cloud Run management | https://console.cloud.google.com/run?project=sfdc24 | Google-authenticated operator console; services/jobs/pools |
| Bus | Blackboard Alpha DB through Apps Script gateway | Signed/secret-controlled bus operations; configured URL omitted because access-bearing query strings must not leak |
| WhatsApp inbound/outbound | Current gateway -> bus; wa-outbox job -> provider | Addressed owner messages; outbox/provider acceptance is not delivered/read proof |
| LiveKit | Configured project/room endpoint | WebRTC media and SDK control/data channels; scoped expiring room tokens |
| Provider APIs | OpenAI, Anthropic, Gemini, xAI, Deepgram as selected | Server-held provider credentials; authorization/spend per route |
| Salesforce portal | Salesforce Experience Cloud domain; actual production URL UNKNOWN | Salesforce identity and guest boundary; developer portal research not production proof |
| Salesforce agent API | Hosted MCP or approved REST integration | OAuth-scoped identity; read versus mutation separately gated |

Existing jobs inventoried: board-watcher, board-probe, gemini-waker, claude-api-waker, wa-outbox, waker-shadow, studio-voice-sweep. A scheduler wakes jobs; each has its own execution identity/state. Inventory does not prove schedule reliability. claude-code-cloud and ccc-broker were not present in this inventory and remain PROPOSED.

## 5. GitHub repositories and access levels

| Repository | Visibility verified | Current CLI principal | Desired runtime access and write boundary |
|---|---|---|---|
| sfdc-24/Blackboard | Public | admin, maintain, push, triage, pull | Public/source read where appropriate; proposed clone credential contents-read only; broker owned-branch PR writes |
| sfdc-24/conference | Private | admin, maintain, push, triage, pull | Authenticated private read; per-agent owned notes/work lanes; no direct canonical transcript/main edits |
| sfdc-24/sfdc24-site | Public | admin, maintain, push, triage, pull | Public source; authorized deployment pipeline; not included in proposed PR306 broker repo allowlist |
| Experience Cloud source package | Local applicant-portal package identified | Remote entitlement not inventoried | Separate Salesforce metadata/repo lane; deployment permission is not supplied by GitHub access |

The observed administrator permission belongs to the local authenticated principal. It is not an intended cloud-agent permission or proof that Gemini/Grok/Claude each hold it. Complete collaborator, GitHub App installation, token scope, ruleset-bypass and Actions credential inventories remain UNKNOWN until owner-authorized metadata receipts establish them. Public read and private write are different capabilities.

Owned lanes: Claude chair/gateway/console/root integrations; Codex broker/docs/architecture and test gates; each agent only its own notes unless calls operator authorized. Every moving PR head restarts review. Merge authority belongs to integration owner under exact-head gates, not to a broker. CI permissions, branch protections and runtime token restrictions must reinforce ownership; a naming convention alone is not enforcement.

## 6. Architecture considerations: access and read-write matrix

P = PROPOSED in PR306; E = existing component; H = historical Salesforce design. Storage entries name credential classes, never values. Revocation owner is the responsible authority, not a claim that an agent has permission to revoke now. Where scope is unverified it must be read back before implementation.

| Component | Resource | Read or write | Credential | Where stored | Who revokes |
|---|---|---|---|---|---|
| P claude-code-cloud | Anthropic Console workspace fleet-claude-cloud | Inference requests; no account administration | Workspace-scoped API key | Proposed Secret Manager, runtime injection | Owner/Console workspace administrator |
| P clone harness | Blackboard and private conference source | Contents read only, before agent starts | Read-only GitHub credential | Proposed Secret Manager; removed from child environment/disk/remote after clone | Owner/GitHub credential administrator; GCP admin can revoke accessor |
| P cloud agent | Offline owned working copy | Read/edit local allowed files; no GitHub remote writes | No GitHub write credential | Isolated job filesystem | Harness/runtime owner disables task and broker capability |
| P cloud agent job | GCP metadata/logs | Scoped describe/list/log read; no deployment/IAM/scheduler writes | Job service-account identity | Metadata-issued short-lived tokens; IAM binding | Owner/GCP IAM administrator |
| P job -> broker | ccc-broker fixed operations | post_receipt, post_result, open_pr only | Audience-bound Google OIDC ID token | Short-lived token; no shared client secret | GCP IAM administrator revokes invoker/identity |
| P ccc-broker | Two allowed GitHub repos | New/fast-forward owned branch plus PR creation; no merge/delete/PR edits | Fine-grained write token in pinned rev5; GitHub App is a separate proposal in owner brief | Secret Manager accessible only to broker identity | Owner/GitHub token or App installation administrator |
| P ccc-broker | Bus append endpoint | Template/validated receipt and result append + read-back | BUS_SECRET | Secret Manager; not exposed to agent job | Owner/bus credential administrator; rotation requires owner gate |
| P ccc-broker | Firestore receipts/work_id | Atomic create/read/reconcile receipt; no replay of ambiguous effect | Broker service-account identity | IAM and short-lived metadata tokens | GCP IAM administrator |
| P hosted memory sync | Dedicated GCS memory prefix | Read/write CAS only within owned prefix | Scoped service-account identity | IAM; durable state in GCS, not container-only | GCP IAM/storage administrator |
| E board-watcher | Bus, GCS cursor, job starts | Read routing rows; CAS cursor; invoke selected jobs | Existing configured bus/GCP credentials; exact IAM scope pending read-back | Secret Manager and service-account IAM per runbook | Owner/bus admin and GCP IAM administrator |
| E gemini-waker | Provider; allowed conference OKF route | Reasoning and explicitly authorized PR-only contribution | Mounted Gemini/provider and GitHub credentials; scope must be read back | Secret Manager/runtime mount; never public board | Owner/provider/GitHub admin and GCP IAM administrator |
| E claude-api-waker | Addressed bus rows and provider | Answer selected rows; not equivalent to Console implementation worker | Provider key, bus capability, service identity | Secret Manager/runtime; exact inventory pending | Owner/provider/bus/GCP administrators |
| E wa-outbox | WhatsApp provider | Send authorized outbox item; receipt reconciliation | Provider credential plus bus read/state identity | Server-side secrets; no phone/token in PDF | Owner/provider business administrator |
| E gateway | Host admission, room tokens and control | Validate session, issue limited room capability; control telemetry | Service identity and LiveKit signing material | Server configuration/Secret Manager; browser gets limited token only | Owner/GCP/LiveKit administrator |
| E chair worker | Provider routes, session state, transcript prefix | Listen/reply; controlled state and transcript writes | Service/provider identities; conditioned storage permission | Runtime secrets and IAM; GCS transcript objects | Owner/provider/GCP/storage administrators |
| P PR126 checkpoint | Firestore session checkpoint | Claim/save/recover with CAS lease; off by default | Scoped chair service identity | IAM; Firestore durable document | GCP IAM administrator; operator disables feature |
| E browser console | Own admitted room and telemetry | Media send/receive; session-bound events; no server-secret access | Expiring room/session token | Browser memory/session; never provider/admin key | Gateway/LiveKit admission authority; token expiry/session End |
| E integration lane | GitHub main and releases | Reviewed merge, build/deploy under gate | Operator/CI identity; actual scopes not fully inventoried | GitHub credential store/Actions/OIDC as configured | Owner/GitHub/GCP administrator |
| H Experience Cloud guest | One approved intake flow | Execute validated create path; no general record read/API | Salesforce guest identity; scoped profile/flow permission | Salesforce site guest profile/permission settings | Salesforce administrator disables site/flow permissions |
| H Experience Cloud member/caseworker | Authorized CRM records | CRUD/FLS/sharing according to business role | Salesforce authenticated user/session | Salesforce identity/permission sets/sharing | Salesforce identity administrator |
| H Salesforce integration | Hosted MCP/API in approved scope | Read first; writes/deletes behind separate approval | OAuth External Client App and scoped integration user | Salesforce credential store; server secret store where applicable | Salesforce admin revokes app/token/user permission |
| P Salesforce -> Blackboard | Approved bus operation | Controlled outbound integration, never embedded bus UI | Named Credential + External Credential | Salesforce credential store | Salesforce and bus administrators |

Important source discrepancy: pinned PR306 rev5 calls for fine-grained tokens, whereas the 10:40Z brief proposes a GitHub App with short-lived installation tokens and token fallback. The PDF records both but does not silently accept either as deployed. An App reduces standing token exposure but still needs an installation scope, permitted repos and broker policy. No credential is created by this document.

## 7. Experience Cloud integration and security

Dated source research identified Digital Experiences enabled in a developer org, a sample site behind login, an applicant-intake package, and an OmniStudio Lead path. Its README status and research are not fresh production evidence. Screenshots/local source alone do not prove today's deployment or guest submission. Actual site/domain, template, activated metadata and guest policy must be re-read before claims are upgraded.

Separate audiences: anonymous visitor, authenticated external member, caseworker, integration user and administrator. For each define authentication, object CRUD, field-level security, sharing, consent purpose and retention. No real customer/citizen data until least privilege, guest isolation, session/MFA policy and audit are accepted. System-context Flow requires input validation, anti-abuse controls, minimal output and review of its elevated write path; no object permission does not itself make a system-context flow safe.

Salesforce-owned data remains in Salesforce. Blackboard receives minimal task references and redacted outcomes, not raw applications. Use correlation IDs across sessions, jobs and Salesforce records without exposing identities publicly. Named/External Credentials are the proposed outbound boundary; OAuth scopes and runtime user permissions both matter. A developer-admin integration identity is not a production-ready unattended identity. Custom domain or DNS redirect is a separate owner gate. SFDC24.com and Experience Cloud need explicit navigation, consent and logout/session behavior; cross-origin links are not implicit SSO.

## 8. Durable state, isolation and failure recovery

Alpha DB remains canonical for dispatch until explicit cutover. Git OKF is the versioned decision/evidence ledger. GCS holds cursor and transcript artifacts. Firestore checkpoint and broker receipt state have separate purposes. Proposed Redis/Memorystore is hot coordination/cache, not a new authority or replacement for durable records. Redis has no verified deployment in this inventory.

One writer per lane and CAS do not prove exactly-once external effects. Define pending/committed/unknown receipt states, settle transport ambiguity by destination read-back and never replay an uncertain append. Idempotency scope must include work identity, operation and attempt semantics. A transcript is imported only after closed state, consistency/held-stamp validation and exclusive copy checks; canonical record mutation is PR-only.

Conference replacement recovery needs durable agenda snapshot, single active audible chair, separate transcript parts and clean End. A write lease is not a speech fence. In-flight actions or unfinished steps can repeat; save lag and ambiguous claim commits need explicit acceptance limits. PR126 SOURCE GO at 78a4811 is not production Firestore/IAM or owner-heard approval. Keep the noncheckpoint working route as rollback.

Tenant/session isolation: admit only scoped room/session access; bind telemetry to its originating session and versions; cap queues and timeouts; distinguish refused/lost from unanswered/unconfirmed. Page-exit flushing can leave events unreported. Gaps prove only absence of received telemetry, not their cause. Browser/device evidence and owner-heard judgment remain independent.

## 9. Delivery, operations and experiment acceptance

Source changes require exact-head independent review and hosted CI. Public releases additionally require served identity, desktop and narrow-screen behavior, authenticated device tests and rollback. Ops is a read-only projection of real receipts: it must not refresh observed_at from polling or metadata that is not a source event, display fake utilization, or conceal stale data. Latest earlier browser checks showed stale source observation; no fresh Ops success claimed here.

Keep laptop heavy work behind free RAM >=4 GB and CPU <75%, one lane. Never kill/suspend interactive agents. Move one bounded workflow at a time, with destination read-back and laptop-off proof before decommission. Console API/Cloud Run execution differs from subscription routines; OpenAI API successor work needs its own scope, credential/spend gates and receipts.

Conference tests: real session-bound browser telemetry; all agents read the same plan digest; each executes allowed owned PR write or BLOCKED; stale-base/conflict/denial/read-back controls; genuine native listening-while-speaking per route; device-level intentional barge-ins; complete replies; clean End; replacement/recovery; no duplicate board effects. Hosted receiver tests are not owner-heard acceptance.

JMP pilot: 24 assigned dev-only runs (stimulus, minimum speech duration, activation threshold, noise). Record false cuts, command-cut delay, premature finals, answer completion, first browser audio, telemetry loss/unconfirmed, chair/console versions and room. Missing stays missing. Owner preference is fewer false cuts first, speed second. Real call observations inform stimuli and failure cases, not fabricated completed DOE rows. Choice design compares candidate experiences only after safety gates; no production threshold change from a single attractive run.

## 10. Promotion checklist and remaining decisions

| Gate | Evidence needed | Owner/reviewer |
|---|---|---|
| Final architecture publication | Frozen source + PDF hash, Gemini final AGREE, Drive upload/download hash read-back | Codex consolidates; Gemini final architecture gate |
| Full access inventory | Actual collaborator/App/token scope, ruleset bypass, IAM and secret-access metadata, no values | Claude inventory; Codex security; owner credential authority |
| Console C1 | Single-tool startup, denied read/shell/delegation, one exact receipt, bounded cost | Claude implements; Codex tests; Cursor code gate |
| Console C2 | Broker owned-branch/path/fast-forward controls, no job write credential, malicious bundle/command denial | Same independent gates |
| Private text/WhatsApp | Redaction, admission, purpose/retention and explicit owner GO | Owner + Codex security |
| Salesforce portal | Fresh site/flow/guest CRUD/FLS/sharing read-back, end-to-end guest test, audit and least privilege | Salesforce administrator + Codex acceptance |
| Native audio/recovery | Same reversible side-by-side route gates, scripted receiver and owner-device acceptance separately | Claude, Codex, Cursor, owner |
| Laptop cutover | Hosted run with laptop off, real task/PR receipt, rollback proof, owner decommission GO | Owner |

## 11. Evidence directory

Sources: Blackboard docs/EXPRESS.md, docs/CLOUD-FLEET-RUNBOOK.md, docs/CLOUD-CREDENTIAL-CONTRACT.md, docs/OPENAI-CLOUD-MIGRATION.md and accepted ADR-20260925-BLACKBOARD-MINIBUS-MULTIAGENT-CONTROL-PLANE.md (its embedded runtime snapshot is historical). Conference docs/okf/HOW-WE-WORK.md, lanes.md, dated cooking.md, call plan/notes and exact-head PR126. Salesforce local package README.md and RESEARCH-PORTAL-SEC-ARCH.md, dated 26 September. Drive roadmap draft 1cUhqIH7AiWfRvqrIpgxKdMID5sP_M4V1ARkeC_t8RWA is a proposal, not runtime authority. Board ask CCC-ARCH-ACCESS-ASK-20261001T1040Z and owner lock ARCH-PDF-GEMINI-GATE supply the publication mandate.

Repository URLs: https://github.com/sfdc-24/Blackboard ; https://github.com/sfdc-24/conference ; https://github.com/sfdc-24/sfdc24-site . Private access is required where indicated. No recorded conversation, customer data, raw prompt, phone/email or secret value is included. Every unknown item is a follow-up inventory gate rather than an invented capability.

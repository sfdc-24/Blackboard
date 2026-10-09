# SFDC24 creator minibus offer and customer-experience roadmap

**Status:** Proposed strategy; implementation, pricing, payment-provider, and
creator-onboarding decisions remain gated  
**Strategy owner:** Codex  
**Decision owner:** Mr. Salam  
**Prepared:** 2026-09-25  
**Answers:** `CCC-OWNER-DIRECTION-BUILDPLAN-MINIBUS-20260925T1725Z`  
**Architecture dependency:**
`ADR-20260925-BLACKBOARD-MINIBUS-MULTIAGENT-CONTROL-PLANE.md`

## Decision

Build the owner's new product direction as two customer-experience slices and
one later creator pilot:

1. **CX1 / `DISCOVERY-CHARTER-001` - Charter Readiness and Structured
   Discovery** captures confirmed
   project context while the live prototype continues to evolve.
2. **CX2 / `BUILD-PLAN-PDF-001` - Build Plan and Commercial Handoff** packages one accepted charter
   and artifact revision into a watermarked, human-reviewed build-plan PDF.
3. **CO1 - Creator Minibus Pilot** lets one invited creator deliver that
   experience from the shared SFDC24 framework under a distinct tenant,
   project-level client grants, quotas, evidence, and a kill switch.

Do not call the new product slices unqualified `G3` and `G4` in durable
artifacts. The accepted minibus ADR already defines architecture G3 as the PR
#260 client-isolation gate and architecture G4 as the PR #261
durable-publication gate. Reusing those identifiers would make release and
security decisions ambiguous.

The context score is a **readiness measure, not a judgment of the client**. It
must never change price, eligibility, or service quality. The creator offer is
a managed service on the common minibus framework, not a source-code licence,
unrestricted white-label chatbot, or per-customer fork.

## Release sequence

| Order | Gate or slice | Work allowed now | Exit evidence | What remains prohibited |
|---:|---|---|---|---|
| 1 | Architecture G1 run 2 | Execute the owner's accepted checklist against the immutable site #208 baseline | One completed session with the required Website path, turns, barge-ins, visible revisions, natural End, provider/audio/artifact receipts, and destination read-back | Deploying site #209, changing the G1 acceptance object, or substituting transcript-only evidence |
| 2 | Site #209 guided-flow acceptance | Keep exact head `0447b279544dbce43f539e88f712ce61cd806561` held; after G1, refresh review and prove the TTS deadline and guided UX | Current exact-head review, green checks, served-byte identity, and a short human UX receipt | Treating a green CI run or an older review as deployment authority |
| 3 | Architecture G2 | Provider deadlines, fallback, circuit state, stale-turn fences, and bounded telemetry | Failure-path tests and real-provider receipts that correctly identify the responding provider | Silent or mislabeled fallback |
| 4 | Architecture G3 / PR #260 | Rebase and independently review the exact isolation head; deploy disabled and at zero traffic | Subject-tenant-project binding, revocation, pre-admission fetch, parser/resource bounds, audit redaction, cross-tenant negative controls, and rollback | Real creator/client data or external onboarding |
| 5 | Architecture G4 / PR #261 | Rebase only after G3 acceptance; close durable publication | Crash-after-admission recovery, exactly one publication, same replay receipt, destination read-back, independent exact-head review, disabled deployment | Publication claims based only on an ACK, merge, process state, or HTTP response |
| 6 | CX1 operator canary | Schema, pure reducer, validation, synthetic fixtures, prompt-injection tests, and UI mock may be developed source-only before this point; real integration waits for G3/G4 | Bound state and revision receipts, persisted reload, conflict reopening, redaction, resource/cost bounds, and an operator-only experience receipt | Public visitors, creator clients, or source changes stacked onto a moving PR #260/#261 head |
| 7 | CX2 operator canary | Typed PDF schema and inert template may be designed now; functional generation follows accepted CX1 state | Deterministic artifact, immutable ID and digest, authorized read-back, human approval, idempotent regeneration, and rollback | Invented prices, a live payment link, or sending an unapproved quote |
| 8 | CO1 creator pilot | Strategy, contracts, schemas, threat model, and non-production design may proceed now | G1-G4 accepted; CX1/CX2 accepted; one creator tenant; one end-client project; quotas, isolation, suspension, rollback, support and delivery receipts | Public self-service, paid acquisition, broad creator onboarding, regulated data, marketplace settlement, or arbitrary provider/tool access |

CX1 and CX2 should be separately releasable, default-off changes. They must not
be added to the moving PR #260 or #261 reviews. Site #209 is the likely
presentation base after its own acceptance; it is not permission to merge or
serve either product slice.

## CX1 / `DISCOVERY-CHARTER-001` - Charter Readiness and Structured Discovery

### Product promise

The visitor can talk naturally and see useful work immediately. In parallel,
the system makes the emerging project charter visible, shows which decisions
are still open, and asks the single most valuable next question. It must not
turn the experience into a questionnaire that delays the first prototype.

### The six owner dimensions

Retain the owner's six top-level dimensions:

1. **Scope** - website type, pages, components, content ownership,
   integrations, constraints, and exclusions.
2. **Objectives** - intended audience, visitor need, desired outcome, and the
   business, person, or entity the site represents.
3. **Design sense** - tone, references, visual attributes, accessibility, and
   meaningful constraints.
4. **Refinement** - prototype changes, rejected directions, confirmed choices,
   and unresolved conflicts.
5. **Timeline** - desired dates, dependencies, milestones, and assumptions;
   never an invented commitment.
6. **Closing expectations** - acceptance, handover, ownership, support choice,
   commercial next step, and open decisions.

Website discovery starts with type choices such as ecommerce, blog/social, and
company page, then asks who visits and why, learns the represented business or
person, proposes clearly labelled best-practice recommendations, establishes
design direction, and closes with a build plan.

### Readiness model

Use the same bounded scale for every dimension:

- `0` - not yet asked or unknown;
- `1` - mentioned, but ambiguous or unsupported;
- `2` - sufficient for a preliminary build plan;
- `3` - explicitly confirmed by the authorized participant.

The score is derived from dimension state. Model prose cannot set the score
directly. Completed dimensions may shrink visually, as requested, but must
remain inspectable and editable. A conflicting or changed answer reopens the
dimension instead of silently overwriting history.

Each dimension stores only:

- bounded captured facts;
- readiness level and confirmation state;
- committed transcript, canvas event, and artifact revision references;
- conflicts, assumptions, and exclusions;
- one recommended next question.

Recommendations must be visibly distinct from client facts. Raw transcript or
arbitrary canvas HTML is not charter state.

### Authority and security contract

The controller processes the newest committed transcript/canvas delta and
returns an untrusted, bounded patch. Trusted code must validate:

- authenticated subject, tenant, project, session, turn, and artifact revision;
- an allowlist of dimensions and state transitions;
- current evidence references and compare-and-set revision;
- size, field, resource, and provider-budget limits;
- redaction of secrets, email, private hosts, and unnecessary personal data;
- prompt-injection and cross-tenant denial controls;
- cancellation of stale, late, conflicting, or revoked work.

No model may write a charter directly, mark a fact confirmed, choose a price,
or create a commercial effect. Audit receipts contain identifiers, revision,
outcome, provider and bounded timing/usage data, not raw client content.

### CX1 acceptance

CX1 is accepted only when an operator-only session proves all of the following:

- the prototype remains visible and continues to update while discovery runs;
- the newest committed delta updates only the intended charter revision;
- a stale or cross-project patch is rejected;
- a changed answer reopens the affected dimension;
- a completed dimension stays inspectable;
- prompt injection cannot forge evidence, confirmation, authority, or score;
- restart/reload reconstructs the same charter from durable state;
- no raw transcript, secret, or unnecessary personal data appears in logs or
  Blackboard receipts;
- latency, token, and provider-call ceilings are measured and enforced.

## CX2 / `BUILD-PLAN-PDF-001` - Build Plan and Commercial Handoff

### Artifact contract

CX2 generates from one explicitly confirmed charter revision and one exact
prototype artifact revision, never directly from a raw transcript. The PDF is
watermarked and contains:

- document identity, version, issue date, validity policy, and digest;
- minimum necessary customer and project identity;
- confirmed context, audience, objectives, and design direction;
- proposed pages, components, content/data needs, and integrations;
- inclusions, exclusions, assumptions, dependencies, and open decisions;
- delivery stages and timeline assumptions;
- acceptance, revision, handover, export, and ownership rules;
- owner-approved price, currency, and commercial version;
- the owner's explicit `50% to begin build and test` and `50% upon complete
  delivery and handover` terms;
- the selected support option: subscription or on demand;
- human reviewer identity and the next authorized action.

The document must distinguish confirmed client facts, SFDC24 recommendations,
assumptions, and binding commercial terms.

### PDF safety and delivery

Use a deterministic renderer over a typed and escaped document model. Visitor
or model content may not supply HTML, filesystem paths, URLs, renderer options,
payment parameters, or template instructions. If an HTML renderer is ever
used, it must have no network or local-file access and must enforce document,
image, font, CPU, memory, and time bounds.

Acceptance requires byte-stable regeneration for the same inputs, an immutable
artifact ID and digest, tenant/project authorization on download, a human
approval step, idempotent email/download commands, destination read-back, and a
known rollback or revocation path. A generated file, HTTP 200, or accepted
email command is not proof of delivery.

### Payment-link ruling

A payment link is a separate governed financial effect, not ordinary PDF text.
Until Mr. Salam chooses prices, currency, provider, seller identity, support
terms, tax/refund/dispute policy, and quote validity, CX2 may create a draft PDF
but must not create, include, or send a live payment link.

When authorized later, the minimum boundary is:

- a human-approved immutable quote and commercial revision;
- a server-created provider-hosted checkout bound to opaque quote, tenant,
  project, amount, currency, purpose, and expiry;
- no card data handled or stored by SFDC24;
- no amount, recipient, seller, return URL, or terms controlled by model or
  client input;
- webhook signature verification, replay protection, deduplication,
  reconciliation, and authoritative provider read-back;
- an idempotent delivery command to the authorized contact with destination
  receipt;
- two distinct payment states: deposit, then final payment only after accepted
  delivery/handover evidence.

Email delivery, a click, checkout acceptance, or webhook acknowledgement is not
settled-payment proof.

## CO1 - Creator Minibus Pilot

### Positioning

The initial offer is one invite-only, fixed-scope design-partner pilot:

> Lead a natural client conversation while SFDC24 builds a visible prototype,
> captures a confirmed project charter, and produces a human-reviewed build
> plan. SFDC24 operates the shared voice, model-routing, evidence, security,
> rollback, and platform framework; the creator retains the craft and client
> relationship.

The likely first customer is an independent designer, developer, educator, or
specialist with a repeatable service, a small audience, low concurrency,
non-regulated content, and willingness to use bounded templates, providers,
quotas, and evidence rules.

Do not publish a tier menu before pilot data exists. Use one package with four
phases:

1. **Plan** - fit/risk screening, one service template, the creator's own
   charter/prototype experience, accepted scope/exclusions, and support choice.
2. **Build and test** - after the signed plan and confirmed first 50% payment,
   configure one tenant/minibus from the common framework, bounded branding,
   roles, grants, quotas, retention, support routing, tests, and rollback.
3. **Delivery and handover** - the final 50% becomes due only against the
   accepted revision, one complete rehearsal, training, operating guide,
   support/escalation route, export/deletion/suspension procedure, tested
   rollback, and handover receipt.
4. **Operate** - either bounded subscription support or separately authorized
   on-demand work. Neither option creates promises not written into the
   accepted support contract.

Co-brand the experience as `Creator name - Powered by SFDC24`. Do not imply
SFDC24 is the seller of the creator's end-client service when it is not.

### Commercial boundary for the first pilot

The recommended first model is:

- SFDC24 contracts with and invoices the creator for the minibus service;
- the creator contracts with and invoices the creator's end clients outside
  the platform;
- SFDC24 does not act as marketplace, payment splitter, or merchant of record
  for creator/end-client transactions.

Connected accounts, revenue sharing, creator-customer checkout, and centralized
50/50 settlement are out of scope until the owner explicitly chooses that
business model and accepts its tax, refund, dispute, identity, and compliance
obligations.

### Responsibility split

| SFDC24 control plane | Creator tenant | Human gates |
|---|---|---|
| Enrollment, leases, capability allowlists, provider routing, budgets, quotas, framework updates, platform security, health/usage receipts, suspension and kill switch | Client relationship, content rights, project grants, bounded branding, first-line support, client approvals, creative promises, and acceptance of delivered work | Prices, payment provider, seller/merchant model, tax/refund terms, support promise, production promotion, spend, and each pilot onboarding |

The control plane stores policy, health, usage, and receipts, not unrestricted
client content. Internal Blackboard must remain mechanically isolated from
creator and end-client surfaces.

### Required tenancy additions

PR #260's tenant registry is a foundation, not the full B2B2C model. Before an
external creator pilot, add and verify:

- explicit creator-admin, creator-staff, and end-client roles;
- project-level subject grants; tenant membership alone cannot expose every
  client project;
- end-client tokens restricted to the intended project/session;
- tenant/project-separated state, artifacts, audit, caches, prompts,
  notifications, backups, restore, retention, export, and deletion;
- creator- and project-level budgets, concurrency, provider-egress grants, and
  data-class policy;
- suspension and lease expiry that stop new private reads, provider dispatch,
  artifact commits, payments, and notifications;
- denial tests across live state, stale tokens, logs, caches, provider prompts,
  email, notifications, backups, and restored data.

### Pilot non-goals

- public anonymous signup or self-service onboarding;
- regulated or high-sensitivity workloads;
- arbitrary code execution, live-site mutation, or custom provider keys;
- unrestricted white-labeling or creator-modified governance;
- a separate code fork for every creator or end client;
- marketplace, revenue splitting, or SFDC24 merchant-of-record service;
- capabilities such as Salesforce writes, WhatsApp, Zoom, or Meta inference
  before each has independently passed its own gate;
- paid acquisition before delivery and unit economics are proven.

### Pilot acceptance and metrics

Hard acceptance requires zero cross-tenant/project disclosure; complete
tenant/project/session/revision binding for every provider call and artifact
commit; one immutable charter and artifact revision per build plan; no secret,
raw transcript, or unnecessary personal-data leakage; reconciled payment
receipts; and tested rollback, suspension, export, deletion, and restore.

Measure:

- time to first useful spoken response and visible artifact;
- questions before first visible value;
- charter completion and human correction rate;
- time from discovery to accepted build plan;
- build plans needing manual repair;
- deposit-to-handover completion and post-deposit scope changes;
- operator interventions and support minutes per project;
- provider/platform cost and contribution per completed project;
- support selection, renewal, and second-project rate.

Do not invent conversion targets before pilot evidence exists. The first
commercial acceptance event is one unrelated creator paying under approved
terms, completing one real end-client project within the promised scope and
human-effort boundary, delivering the agreed result, and leaving no unresolved
security, payment, support, rollback, or destination receipt.

## Ownership

- **Codex:** offer, sequencing, scope, commercial/acceptance contracts, and
  payment/tenant boundary rulings.
- **Claude:** bounded implementation and source/runtime delivery evidence.
- **Cursor:** exact-head implementation and security review.
- **Gemini:** adversarial UX, prompt-injection, abuse, and unit-economics
  challenge; reasoning only, with no artifact or execution authority.
- **Meta/WhatsApp:** later consented channel experiments only after the offer
  and delivery path pass their gates.
- **Mr. Salam:** prices, currency, payment provider, terms, support promise,
  production promotion, spend, and creator-onboarding decisions.

## Decisions required from Mr. Salam before a live commercial handoff

1. Price by direct website/service type.
2. Creator-minibus setup and recurring commercial model.
3. Included usage and overage treatment.
4. Subscription-support scope, response commitment, and price.
5. On-demand support rate and authorization rule.
6. Currency, tax, cancellation, refund, chargeback, and quote-validity policy.
7. Payment provider and legal seller identity.
8. Whether the 50/50 terms apply to direct SFDC24 work, creator onboarding, or
   both.
9. Whether SFDC24 ever intends to become merchant of record for creator
   transactions; the recommended v1 answer is no.

No account creation, price invention, payment-link generation, quote send,
charge, provider spend, production promotion, or creator onboarding is
authorized by this document.

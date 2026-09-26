# Converspan readiness - 2026-09-26

**Snapshot:** 2026-09-26 02:40 UTC, with a 03:00 UTC delta for the site row. Facts are dated observations, not acceptances. Prepared overnight on the owner's instruction "get ready to start on Converspan".

**Status:** strategy and documentation only. ADR G9 permits this before its gate. Nothing here builds, deploys, points
DNS or spends.

**Evidence labels:** each fact names its source.
- `repo` means it is in this repository.
- `board` means a board row, named by its id.
- `session` means it was observed by claude-code-cli in the 2026-09-25/26 session and is not recorded in this repository.

PR numbers carry their repository.

## The governing rule

`docs/ADR-20260925-BLACKBOARD-MINIBUS-MULTIAGENT-CONTROL-PLANE.md`, G9, reads (`repo`):

> The non-waivable foundation gate is: accepted SFDC24 continuous-audio and live-prototype evidence from G1;
> provider fallback/deadline/circuit/telemetry controls from G2; exact-head PR260 tenant-isolation acceptance
> from G3; PR261 durable publication, restart, and replay acceptance from G4; immutable source-to-runtime
> receipts; and a tested rollback on production `main`.
>
> Strategy, documentation, and non-production design may continue before that gate. Converspan production
> implementation, deployment, and client onboarding may not begin before it passes.

G9 also requires, after the gate:
- a distinct tenant/minibus with an expiring capability lease, separate budgets, a separate data boundary and a Blackboard kill switch;
- a zero/low-traffic canary, a rollback drill, and a 24/48-hour soak before broader client onboarding.

## What Converspan is, and what is still undecided

**The owner's direction** (`session`, 2026-09-25):
- The design work (websites, logos, apps) belongs to Converspan.
- sfdc24.com stays focused on Salesforce.
- Creators can be offered the sfdc24.com architecture as part of the minibus family, on our framework, at low cost.

**Two drafts describe the commercial model, and they differ:**
- **The domain roadmap** `docs/DOMAIN-PORTFOLIO-ROI-ROADMAP-20260925.md` makes Converspan the commercial parent, moving checkout and the legal seller identity there.
  - It exists only in the owner's local checkout: commits dc68c99, 6ed0546, 937f69b and 87e0ffd on local `main`, not pushed (`session`).
- **Blackboard #268**, Codex's creator minibus roadmap (open, unmerged): SFDC24 contracts with and invoices the creator. Seller identity, payment provider and prices are decisions for the owner.
- This document does not settle the difference. It is decision 3 below.

## What exists today

| Asset | State | Source |
|---|---|---|
| `converspan.com`, `.org`, `.ca` | Registered with NameSilo DNS (`ns1`/`ns2`/`ns3.dnsowl.com`), A records 172.232.24.161, 172.232.24.235 and 172.234.25.42. No site is served: HTTP returns `502 Bad Gateway` (Pingora) and HTTPS fails with a TLS alert, as reported by the Cursor review on this PR; from the owner's laptop the connection is refused at once (02:48 UTC) | `session` DNS checks via 8.8.8.8 and 1.1.1.1; Cursor review comment on this PR |
| `sfdc-24/converspan` | An OpenAI Sites (vinext) starter the owner created on 2026-09-25. One commit, `a1953af`. The local checkout also has uncommitted starter files, left untouched. The repository is private and not visible to review bots | `session` (local checkout) |
| SFDC24 homepage voice and live canvas | LIVE at 03:00 UTC: sfdc24-site main `e538ecf` (sfdc24-site #209 guided meeting, #216 host nudge, #221 nudge lifecycle, #223 builder question context). After merging #223, Codex withdrew its GO, finding an unheard-question lifecycle race and a question-span cut. A fix-forward is in progress | sfdc24-site repo; `board` CODEX-SITE223-89E3ABC-NOGO-CORRECTION-20260926T024146Z and ...-NOGO-ADDENDUM-20260926T024635Z; served-byte checks in `session` |
| Studio controller | Serving `r5b-0895605` at 100% since 2026-09-25 18:37Z: the same image as `r5-0895605-g`, with one added operator. The ADR contract's "r5-0895605-g at 100%" line is stale | `session` (Cloud Run traffic); `board` CCC-SITE209-MOVED-HEAD-454B60E-20260925T2030Z |
| Build plan and quote PDF | Merged dark (Blackboard #269). `STUDIO_ENABLE_CHARTER` defaults false and `STUDIO_PRICE_TABLE` defaults empty. Payment terms: 50% to start build and test, 50% on delivery and handover | `repo` (`app/summary_pdf.py`, #269) |

## G9 measured against current evidence

| G9 component | State | Evidence | What closes it |
|---|---|---|---|
| G1 continuous audio | PARTIAL | See below | See below |
| G1 live prototype | PARTIAL | See below | See below |
| G2 provider controls | OPEN | See below | See below |
| G3 tenant isolation | OPEN | See below | See below |
| G4 durable publication | OPEN | See below | See below |
| Immutable source-to-runtime receipts | PARTIAL | See below | See below |
| Tested rollback on production `main` | OPEN | See below | See below |

**G1: continuous audio (PARTIAL)**
- Evidence:
  - ADR acceptance matrix: physical and human-heard audio pending (`repo`).
  - The owner's run at 2026-09-26 01:00–01:10Z completed a session, but the architect was silent about 3 minutes: a builder 504 at Cloud Run's 60 s, then 409s (`session`; Blackboard #272 body).
- What closes it: G1's ten minutes, five turns and two barge-ins, run by the owner, after the builder is bounded.

**G1: live prototype (PARTIAL)**
- Evidence: multiple visible revisions observed; ten-minute and replay acceptance pending (`repo` ADR matrix).
- What closes it: three visible revisions, and the final artifact reproduced from the event ledger.

**G2: provider controls (OPEN)**
- Evidence: Blackboard #272 (builder bounded) has Cursor GO at `8a9349e` and a Codex NO-GO (`board` CODEX-PR272-8A9349E-NOGO-20260926T020927Z, CODEX-R5D-8A9349E-NOGO-20260926T0231Z): no total wall-clock deadline, and permanent 4xx errors classed as timeouts.
- What closes it: the complete ADR G2 controls, not #272 alone: definite-error fallback, provider circuit state, stale-turn fences, hard deadlines, bounded response bodies, and provider/model/latency/outcome receipts. Each needs failure-case evidence (timeout, quota, auth, malformed, refusal, empty, recap failure) and a Codex GO.

**G3: tenant isolation (OPEN)**
- Evidence: Blackboard #260 is open at `ecee267`. Cursor GO 02:05Z; Codex Gate 1 NO-GO (`board` CODEX-PR260-ECEE267-GATE1-NOGO-20260926T021501Z).
- What closes it: Codex exact-head GO, a disabled zero-traffic deploy, and cross-tenant and revocation negative controls.

**G4: durable publication (OPEN)**
- Evidence: Blackboard #261 is open, stacked on #260. Starting #261 is not closure.
- What closes it (ADR G4):
  - rebase onto the accepted #260/main base;
  - crash-after-commit recovery with exactly one publication and a durable replay receipt;
  - a separate exact-head review;
  - an accepted disabled zero-traffic deploy.

**Immutable source-to-runtime receipts (PARTIAL)**
- Evidence:
  - Site: served files matched main by hash after each release (`session`; `board` rows above).
  - Controller: revisions run fixed images, but no receipt is committed per revision.
- What closes it: a committed receipt per controller revision (commit, tree hash, image digest).

**Tested rollback on production `main` (OPEN)**
- Evidence: a prior Cloud Run revision exists, but no rollback drill is evidenced.
- What closes it: one timed drill: shift to the prior revision and back, with health and served-byte checks.

## What may proceed now (G9 permits it)

- **D0: this readiness document**, critiqued by Codex, Gemini and Grok.
- **D1: non-production design.**
  - What: the Converspan landing and conversation design as a design artifact.
  - Where: in the owner's Sites repo on a branch, or as a Blackboard design doc.
  - Constraints: not published, not pointed at a domain, and no voice or controller connection.
- **D2: the minibus contract drafts.** Aligned with Blackboard #268, they must define:
  - **Enrollment:** the Converspan tenant manifest, an expiring capability lease, budgets, and a scoped kill switch (G9).
  - **Authorization:** server-enforced subject and tenant binding. CORS is origin control, not authorization.
  - **Microphone:** affirmative activation and guaranteed teardown.
  - **Data:** retention and deletion for raw audio and transcripts, and provider egress permissions per provider.
  - **The return path to the motherboard:** schema-limited, carrying only health, usage, policy and release versions, plus redacted acceptance evidence. It never automatically returns transcripts, canvases, personal data, credentials, provider bodies or unapproved learnings.
- **Not before G9 passes:** any Converspan deployment, holding page on a live domain, controller origin change, onboarding, checkout, or paid acquisition.

## Decisions that are the owner's

1. **Hosting for the public Converspan site, once G9 passes.**
   - Recommendation: the OpenAI Sites repo the owner created as the public shell. Voice and canvas come from the same controller *implementation* as SFDC24, deployed as Converspan's own instance.
   - Consequence: the code and its tests are shared, but the runtimes are not. Converspan needs its own enrollment, server-side auth and tenant negative controls, provider permissions, budget limits, release receipt, canary, rollback and scoped kill switch. It is not an extra origin on SFDC24's controller.
2. **DNS** for `converspan.com`, once G9 passes. DNS is outside standing permission.
3. **The commercial model.**
   - The options: the domain roadmap (Converspan is the seller) or Blackboard #268 (SFDC24 invoices the creator).
   - Either way, the legal seller identity and payment provider must be chosen before any checkout.
4. **The price list.** It is shared with the quote PDF, and CX2 must not invent prices (Blackboard #268).

## Consultation

- Posted to Codex (the G9 reading and the D0–D2 scope), Gemini (hosting and architecture) and Grok (positioning challenge) on the board: `board` CCC-CONVERSPAN-READINESS-REVIEW-20260926T0230Z.
- Their answers change this document only through a dated addendum.

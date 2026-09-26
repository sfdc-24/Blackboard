# Converspan readiness - 2026-09-26

**Snapshot:** 2026-09-26 02:27 UTC. Prepared overnight on the owner's instruction "get ready to start on Converspan".

**Status:** readiness only. Nothing here builds, deploys, points DNS or spends. Every line below is either
verified tonight or quoted from the named source.

## What Converspan is

**The owner's direction, 2026-09-25:**
- The design work (websites, logos, apps) belongs to Converspan. sfdc24.com stays focused on Salesforce.
- Creators and service providers can be offered the sfdc24.com architecture, meaning streaming audio plus the
  live build experience, as part of the minibus family, maintained on our framework at low cost.

**The domain roadmap** (`docs/DOMAIN-PORTFOLIO-ROI-ROADMAP-20260925.md`):
- Converspan is the working commercial parent for "conversation that turns into visible, retained work".
- Website prototyping and the other verticals start as routes under Converspan.
- Checkout and the legal seller identity move to Converspan.
- sfdc24.com remains the Salesforce acquisition surface.

**The architecture ADR** (`docs/ADR-20260925-BLACKBOARD-MINIBUS-MULTIAGENT-CONTROL-PLANE.md`, page 2 of the
architecture PDF):
- Converspan is a minibus on the Blackboard motherboard.
- Its production state is HELD behind a non-waivable launch gate.

## What exists today (verified 2026-09-26 02:25 UTC)

| Asset | State | Evidence |
|---|---|---|
| `converspan.com`, `converspan.org`, `converspan.ca` | Registered; resolving to NameSilo parking | DNS via 8.8.8.8: NS `dnsowl.com`, A 172.232.24.235 / 172.234.25.42. No site is pointed yet |
| `sfdc-24/converspan` repo | OpenAI Sites starter (vinext on Cloudflare Workers; optional D1/R2; Sign in with ChatGPT helpers) | One commit `a1953af` (2026-09-25 10:54 UTC); `.openai/hosting.json` project `appgprj_...`; the local checkout has uncommitted starter files, left untouched |
| Reusable SFDC24 pieces | LIVE on sfdc24.com | Site main 88b2419 served byte-for-byte: guided meeting, speaker cast, clock, host nudge, nudge lifecycle, voice + live canvas, creative directions |
| Studio controller | LIVE r5b-0895605 | Cloud Run, per-origin CORS allowlist, operator email codes, topic routing, builder / analyst / creative lanes |
| Quote PDF (build plan and quote) | DARK in main (#269) | `STUDIO_ENABLE_CHARTER=false`; price table empty; Codex's R7 repairs outstanding |

## The launch gate, measured against tonight's evidence

The ADR's non-waivable gate reads: "No Converspan production onboarding until SFDC24 proves continuous audio,
artifact replay, tenant isolation, durable effects, source-to-runtime identity and rollback."

| Gate | Where SFDC24 stands | What closes it |
|---|---|---|
| **Continuous audio** | PARTIAL | See below |
| **Artifact replay** | CURRENT | See below |
| **Tenant isolation** | HELD | See below |
| **Durable effects** | PARTIAL | See below |
| **Source-to-runtime identity** | CURRENT (site); PARTIAL (controller) | See below |
| **Rollback** | CURRENT | See below |

**Continuous audio (PARTIAL)**
- Where it stands:
  - Owner-run G1 is partial.
  - The host nudge and its lifecycle repairs are live (#216, #221).
  - The owner's 01:00Z run had a 3-minute architect silence: a builder 504 at Cloud Run's 60 s, then a 409 lease lockout.
- What closes it:
  - #272 (builder bounded and filled) ships as the r5d hotfix.
  - The owner's rehearsal then shows five genuine exchanges, two barge-ins, ten uninterrupted minutes and a clean End.

**Artifact replay (CURRENT)**
- Where it stands: GCS CAS session state, ordered SSE, and snapshot repair.
- What closes it: a reconnect-and-replay acceptance during a live session.

**Tenant isolation (HELD)**
- Where it stands: #260 client workspaces, Codex Gate 1 round 12 NO-GO at ecee267, on four adversarial windows.
- What closes it: Codex Gate 1 GO on #260, then #261, then a Nav pilot.

**Durable effects (PARTIAL)**
- Where it stands:
  - Outcome durability was accepted in #260 round 12.
  - WhatsApp delivery is unverified (acceptance only).
  - Quote and email effects are dark.
- What closes it:
  - Delivery read-back for one channel.
  - #269 repairs, then CX1 and CX2 canaries.

**Source-to-runtime identity (CURRENT for the site, PARTIAL for the controller)**
- Where it stands:
  - Site served bytes match main (two samples per release).
  - Controller revisions are image-pinned.
- What closes it: record image digest plus tree hash per revision; r5d is the first.

**Rollback (CURRENT)**
- Where it stands: Cloud Run keeps the prior revision (r5-0895605-g); a site revert goes through the merge parent.
- What closes it: one timed rollback drill on a tag.

## Increments (smallest reversible first)

Every increment carries Cursor exact-SHA plus Codex review, and each ships with a before/after snapshot and a
release-rail entry.

| # | Increment | Needs | Spend |
|---|---|---|---|
| **C0** | This plan, plus Grok, Gemini and Codex critique | Nothing | None |
| **C1** | An honest Converspan holding page: what we do, one conversation entry, the seller identity once settled. It is built in the existing Sites repo, with no voice yet | Owner decisions 1-2 below | None |
| **C2** | An operator-only design preview on Converspan: the SFDC24 voice and canvas with the design topics (logo, website, app) | See below | Provider usage only |
| **C3** | Quote and checkout under the Converspan seller identity: 50% to start build and test, 50% on delivery, plus a support plan | See below | Payment-provider fees |
| **C4** | The creators' minibus family: tenant-isolated minibuses on the framework | Tenant isolation gate (#260 and #261), the minibus factory, and Blackboard signed leases | Per tenant |

**C2 needs:**
- the same controller, with `converspan.com` added to its origin allowlist (a Codex-gated env change);
- the gates above, apart from tenant isolation, because the preview is operator-only.

**C3 needs:**
- the #269 repairs and CX1 and CX2 canaries;
- the owner's price list;
- a payment provider.

## Decisions that are the owner's

1. **Hosting for the public Converspan site.**
   - Recommendation: keep the OpenAI Sites repo the owner created for the public shell. It talks to the existing studio controller, which is host-agnostic behind its CORS allowlist, for voice and canvas.
   - Consequence: one controller and one set of gates serve both brands, and there is no second runtime to harden.
2. **DNS.** Pointing `converspan.com` at the chosen host. DNS is outside standing permission, so this is the owner's click or command.
3. **Legal seller identity and footer.** This must be settled before any checkout (roadmap).
4. **The price list.** It is shared with the SFDC24 quote PDF, where an unpriced quote is not acceptable.

## Consultation

- The plan is posted to Codex (release sequence and gates), Gemini (architecture and visual review) and Grok (market and positioning challenge).
- Their answers are recorded on the board. Any change to an increment or gate is made here with a dated addendum.

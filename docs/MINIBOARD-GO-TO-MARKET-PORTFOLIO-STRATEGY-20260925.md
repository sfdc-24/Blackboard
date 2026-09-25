# Miniboard Go-to-Market Portfolio Strategy

**Version:** 0.2.1 — controlled-launch operating strategy  
**Date:** 2026-09-25  
**Sponsor:** Mr. Salam  
**Author:** Codex  
**Independent reviews completed:** Claude Sonnet 5 and Gemini 3.8 Flash  
**Distribution reviews pending:** Meta and Cursor; no alignment is attributed to them  
**Status:** CONTROLLED GO — fixed-scope B2B services only; paid acquisition remains gated

## Strategic decision

Use Blackboard as the governed control plane for a portfolio of focused,
customer-facing miniboards. Each miniboard serves one clear customer job through
conversation, live visual work, retained context, specialist tools, and a next
action. Domains are acquisition surfaces and market experiments; they do not
become separate technology stacks.

This extends, rather than replaces, the product thesis in `docs/PRODUCT.md`:
the sale happens after the visitor has a useful working result in hand. For a
design customer that result is a prototype. For a home buyer it is a live,
explained shortlist. For a learner it is a completed exercise and a retained
practice artifact.

## Current commercial launch decision

The Monday, September 28 launch is a **professional-services revenue test**, not
the public launch of an autonomous miniboard subscription. Blackboard and the
agents accelerate research, prototyping, analysis, quality control, campaign
operations, and customer follow-up behind the scenes. Every paid deliverable is
human-reviewed. Capabilities that have not passed exact end-to-end verification
are excluded from the promise.

| Priority | Offer | Founder price | Fixed result | Capacity |
|---|---|---:|---|---:|
| 1 | **Salesforce Org Opportunity Scan** | CAD 1,950 | Authorized read-only assessment, ten-item roadmap, effort bands, and 60-minute review within five business days | 1/week |
| 2 | **Website Conversion Prototype Sprint** | CAD 895 | Up-to-five-page review, homepage copy, one static responsive concept, recommendations, and walkthrough within three business days | 2/week |
| 3 | **Applied-AI Workflow Clinic** | CAD 399 | Ninety-minute role-specific clinic, three workflows, reusable process pack, and limited follow-up | 3/week |
| Upsell | **Salesforce Architecture & Prototype Sprint** | From CAD 3,000 | Separately scoped sandbox-first architecture and prototype work after the Opportunity Scan | Case-by-case |

The offer ladder reconciles the two independent reviews. Claude favored a
lower-friction CAD 1,950 Salesforce diagnostic and CAD 895 one-concept website
sprint. Gemini favored a CAD 3,000 architecture sprint and CAD 1,800 three-page
prototype. The launch uses the smaller fixed offer to earn trust, then makes the
larger work an explicit, separately scoped upsell.

### Monday acceptance test

The launch succeeds when an unrelated customer pays, the promised result is
delivered inside its time cap, owner time is measured, and the customer reaches
a useful outcome without a material complaint. Clicks, impressions, form fills,
pipeline, proposals, deposits, and collected cash are reported separately.

## Commercial category

**Conceptual category:** conversational creation, decision, and practice
platform.

**Plain-language promise:**

> Talk through what you need. See the work take shape. Keep the result.

**Working platform brand:** Converspan, subject to trademark clearance and
final registration checks. SFDC24 remains the Salesforce specialist brand.

## Portfolio architecture

```text
                              BLACKBOARD
                   governance, identity, permissions,
                 agent routing, integrations, observability
                                   |
                     SHARED CONVERSATIONAL PLATFORM
                  realtime voice + dialogue + live workspace
                                   |
       +---------------------------+---------------------------+
       |                           |                           |
   Find and buy              Learn and practice          Create and launch
 homes, gaming PCs,          AI, language, math,         websites, brands,
 vehicles, software          cyber, Salesforce           applications
       |                           |                           |
       +---------------------------+---------------------------+
                                   |
                         Operate and improve
                     Salesforce, sales, support,
                         business automation
```

One account, tenant boundary, billing layer, artifact model, and deployment
system should serve the portfolio. A new domain should normally be a tenant
manifest and specialist tool configuration, not a forked application.

## Future-state opportunity portfolio

The list below is a research backlog, not the Monday catalogue. Real estate,
gaming, education, language, math, and cybersecurity remain no-builds until the
service launch proves demand, delivery discipline, payment, privacy, and the
shared platform. Their ordering remains a hypothesis rather than a commitment.

| Hypothesis | Customer job | Working result delivered in the first session | Likely commercial model | Principal risk |
|---|---|---|---|---|
| **1. Real-estate buyer concierge** | Describe the home and life situation in natural language; avoid repetitive filter-driven search | Explained shortlist with fit reasons, compromises, saved preferences, and a licensed next step | Brokerage subscription or white label, qualified referral, premium concierge, adjacent-service referrals | Listing-data rights, regional coverage, trust, and regulated activities |
| **2. Live website and brand studio** | Turn an idea or an outdated presence into something credible without a long discovery cycle | Visible page, copy, and identity directions revised during the conversation | Fixed-price project, hosting, maintenance, revision plan | Delivery effort can exceed the inexpensive first-session promise |
| **3. Applied AI work-skills coach** | Learn AI by completing useful work rather than watching generic lessons | Finished role-specific task, reusable prompt or process, and verification checklist | Individual packs, team onboarding, subscription | Crowded category and weak retention if tasks are generic |
| **4. Gaming-PC buyer agent** | Translate games, budget, performance expectations, and upgrade plans into a trustworthy purchase | Ranked live offers with component trade-offs, compatibility warnings, and retailer links | Affiliate revenue, retailer lead fees, premium advice | Price/feed freshness, biased commissions, and thin margins |
| **5. Workplace language practice** | Rehearse an interview, meeting, customer call, or difficult explanation | Completed role-play, corrections, alternate phrasing, and a personal phrase set | Subscription, scenario packs, employer or newcomer-program seats | Crowded language market; speech quality and narrow positioning matter |
| **6. Salesforce specialist service** | Build, change, explain, or operate Salesforce with visible progress and governed actions | Prototype, configuration plan, tested change, or grounded org answer | Existing SFDC24 consulting, managed service, training | Authentication, authorization, change control, and enterprise trust |
| **7. Math reasoning studio** | Explain thinking aloud and see equations, diagrams, or graphs respond | Worked problem, error pattern, visual explanation, and repeat attempt | Topic packs, family or tutor subscription | Accuracy, curriculum scope, child safety, and acquisition cost |
| **8. Cybersecurity practice** | Rehearse everyday security decisions or incident response safely | Controlled scenario, decision timeline, consequence map, and response checklist | Team workshops, recurring practice, role packs | Safety boundaries, credibility, curriculum upkeep, and misuse risk |
| **9. Sales and support rehearsal** | Practise objections, discovery, escalation, or difficult customer conversations | Scored rehearsal and reusable talk track tied to the customer context | Team subscription and onboarding packages | Evaluation quality and manager adoption |
| **10. Additional active buyer agents** | Reduce research burden for vehicles, business software, travel, contractors, or other complex purchases | Compared shortlist with reasons, costs, trade-offs, and next action | Affiliate, referral, lead fee, buyer subscription, supplier SaaS | Category-specific data access, commercial bias, and consumer-protection duties |

## Real-estate wedge

Do not begin by rebuilding an MLS portal. Begin with **intent to shortlist**:

1. The buyer describes the desired home, household, budget, commute, tolerance
   for renovation, and subjective preferences.
2. The miniboard makes those preferences visible and asks only high-value
   clarifying questions.
3. Authorized listing sources populate a ranked shortlist.
4. Every property shows why it fits and what compromise it requires.
5. The buyer refines conversationally, for example: "more like the second home,
   but with a larger lot."
6. The miniboard remembers, monitors, and alerts.
7. Viewings, representation, advice, negotiation, and offers move through a
   properly licensed partner where required.

The defensible asset is not a filter or a chatbot. It is the persistent buyer
model, explained trade-offs, accumulated decisions, and coordinated handoff.

## Demand-led launch system

The portfolio is an experiment system, not permission to build ten businesses
at once.

1. Choose one narrow customer job and audience.
2. Launch one complete conversation-to-result journey.
3. Route a small amount of qualified traffic to it.
4. Measure conversation start, first useful result, qualified intent, repeat
   use, willingness to pay, delivery cost, and trust failures.
5. Expand, reconfigure, combine, or stop the miniboard based on evidence.

Controlled sequencing:

- **First:** Salesforce Org Opportunity Scan through owned, organic, and
  professional channels.
- **Second:** Website Conversion Prototype Sprint through visual proof and
  local-business discovery channels.
- **Third:** Applied-AI Workflow Clinic as a waitlist/pilot, not the centre of
  paid acquisition.
- **Later:** one buyer-agent or learning vertical only after its data rights,
  regulatory boundary, acquisition economics, and repeat-use hypothesis have
  been independently verified.

## Domain and brand policy

- Keep `sfdc24.com` only as a legacy/descriptive Salesforce-specialist surface
  while its trademark risk is assessed. Do not claim Salesforce affiliation,
  partnership, authorization, or certification without current documentation,
  and do not use Salesforce logos.
- Contract, invoice, and collect payment under the verified legal seller or a
  neutral parent brand. **Converspan** remains the working parent candidate and
  **Page & Mark** the working creative-studio candidate, both subject to final
  registry price, renewal, corporate-name, trademark, and conflict checks.
- Use a parent platform brand only for trust, accounts, shared capabilities,
  and "Powered by" endorsement.
- Give a vertical its own customer-facing domain when the buyer, promise, and
  acquisition channel are distinct.
- Use one shared runtime and deployment path beneath the domains.
- Do not present a name as cleared because a registry currently has no record.
  Domain availability, trademark clearance, corporate-name searches, and
  commercial-conflict screening are separate gates.
- Do not buy a portfolio of speculative domains. Register one parent or launch
  domain only after the exact registrar total and renewal are shown and
  approved. Additional names graduate from the experiment system only after
  attributable demand exists.

## Review questions

Each reviewer should respond independently and bind the response to **version
0.1 dated 2026-09-25**.

1. Rank the top three launch opportunities and explain the ranking.
2. Name the strongest flaw in the overall portfolio strategy.
3. For each top-three opportunity, identify the assumption most likely to kill
   it and the cheapest ethical experiment that would test that assumption.
4. Identify missing opportunities that fit the shared architecture better than
   one of the listed candidates.
5. State what should explicitly not be built yet.
6. Flag data rights, regulatory, safety, trust, distribution, or unit-economics
   constraints that materially alter the sequence.
7. Provide a clear disposition: endorse, endorse with corrections, or do not
   endorse.

### Assigned review lenses

- **Claude:** architecture fit, operational feasibility, sequencing, and hidden
  delivery cost.
- **Gemini:** market structure, differentiation, adoption, and the evidence
  needed before prioritization.
- **Meta:** accessible social-demand signals, audience formation, distribution,
  and creator/community dynamics. No review may be attributed to Meta unless an
  actual Meta model or authorized Meta dataset was used.
- **Cursor:** fastest testable increments, reusable versus vertical-specific
  implementation, and ways the portfolio could create codebase fragmentation.

## Review register

| Reviewer | Route | State | Reviewed version | Contribution |
|---|---|---|---|---|
| Claude | Local Claude Code CLI 2.1.281.0, Sonnet 5, high effort, safe mode, no session persistence | REVIEWED | 0.1 plus monetization brief | **CONDITIONAL GO** for a CAD 1,950 read-only Salesforce scan, CAD 895 website concept sprint, and CAD 399 workflow clinic; count owner time; keep at least CAD 2,000 in reserve; do not promise unverified platform capabilities |
| Gemini | Google Generative Language Interactions API, Gemini 3.8 Flash | REVIEWED | 0.1 plus monetization brief | **GO WITH CONDITIONS**; use narrow fixed-price services, a CAD 3,000 Salesforce architecture upsell, gated CAD 300–600 search validation, and no paid expansion before checkout, attribution, and delivery are proven |
| Meta | `META_TOKEN` verified as a Graph/WhatsApp credential; no connected business, Facebook Page, or Instagram account is exposed; authenticated Meta AI web review is ready but not submitted | PARTIAL_ROUTE_ONLY | 0.1 pending | Credential proves WhatsApp operations, not social-demand intelligence; review pending explicit browser-message confirmation |
| Cursor | Installed desktop command lacks the separate headless `cursor-agent`; authenticated Cursor web review is ready but not submitted | ROUTE_READY | 0.1 pending | Review pending explicit browser-message confirmation; no substitute model used |
| Grok | Not requested because sponsor reported no remaining usage credit | DECLINED_BY_SCOPE | — | None |

Silence is not agreement. A credential is not a review. A response without an
identifiable route and reviewed version remains unverified.

## Current demand evidence

The evidence supports a narrow B2B service test; it does not prove that any
specific offer will sell.

| Signal | Current evidence | Commercial implication |
|---|---|---|
| Canadian AI adoption is rising but still uneven | Statistics Canada reports 19.2% of businesses used AI in Q2 2026, up from 12.2% in 2025 and 6.1% in 2024; privacy/security and cost remain barriers | Sell a concrete workflow or business result, not generic AI access |
| Digital presence is broad but maturity is shallow | BDC reports 96% of Canadian SMEs use at least one digital technology, three in ten use generative AI, and only 23% report high or very high digital maturity | Guided assessment and implementation support are more credible than a self-serve platform promise |
| Websites remain a core small-business channel | CFIB reports 90% of surveyed SMEs used at least one digital channel; websites were 78%, Facebook 59%, Google Business/Maps 52%, and Instagram 41% | A fixed, visual website-conversion sprint has a large addressable problem, but price and demand still require paid-order validation |
| Salesforce AI adoption is expanding | Salesforce's 2026 vendor survey of 4,050 sales professionals reports 87% of sales organizations use AI and nearly nine in ten plan agent use by 2027 | Existing Salesforce customers are a better Monday audience than net-new platform buyers; vendor data is directional, not Canada-only proof |
| Canadian small-business base is large | ISED reports 1.08 million small employer businesses in Canada, including 410,154 in Ontario | The addressable base is real; reach is not. Acquisition must still be proven one attributable order at a time |

## Channel ladder and Salesforce campaign controls

| Salesforce Campaign | ID | State | Budget ceiling | Release rule |
|---|---|---|---:|---|
| FY2026 SFDC24 / Converspan Monetization Launch | `701fj00001ynVKYAA2` | Active parent | CAD 3,000 | Governance envelope; a ceiling is not authorization to spend |
| Free Listings & Directories — Demand Test | `701fj00001ynaX7AAI` | Active | CAD 0 | LinkedIn Service/organic, owned site, and permitted communities; unique source token and UTMs |
| Owned / Organic / Opt-in — Monday Launch | `701fj00001ynaX8AAI` | Active | CAD 0 | Consent-grounded referrals, demos, public owned content, and opt-in follow-up |
| Kijiji Services — Conditional Listing Test | `701fj00001ynNutAAE` | Planned, inactive | Exact price not approved | Kijiji Services currently charges an insertion fee; read exact price and term before a separate purchase decision |
| Google Search — Salesforce Opportunity Scan | `701fj00001ynaX9AAI` | Planned, inactive | CAD 600 | First release at most CAD 300 after one paid delivery and verified checkout/tracking |
| Meta / Instagram — Website Prototype Sprint | `701fj00001ynaXAAAY` | Planned, inactive | CAD 300 | Requires Page/business/ad assets, tracking, privacy, landing page, and an organic delivery |
| ChatGPT Ads Beta — Contextual B2B Test | `701fj00001ynaXBAAY` | Planned, inactive | CAD 200 | Requires account approval, billing, destination, measurement, and a better expected test than the organic baseline |
| YouTube Demo / Retargeting — Conditional | `701fj00001ynaXCAAY` | Planned, inactive | CAD 0 | Organic demo first; paid retargeting only after a real audience and proven conversion |

### What is genuinely free

- A LinkedIn Company or Service Page can be created without a paid Page plan;
  a Service Page makes the offer discoverable and allows members to request a
  proposal.
- The owned website, public portfolio/demo assets, permissioned referrals, and
  opt-in email or WhatsApp follow-up have no media fee. They still consume time
  and must follow CASL and the platform's terms.
- Relevant Facebook Pages or groups can be tested only when their rules permit
  business-service posts. Facebook Marketplace is not used to list these
  services; Meta describes Marketplace around goods and says it is still
  exploring small-business listing capability.
- Google Business Profile is free but is not a shortcut for an online-only
  consultancy or lead-generation company. It is used only if the seller has a
  legitimate storefront/service-area business that meets Google's in-person
  contact and owner-verification rules.
- Kijiji has a real Services audience, but it is not free: its current Services
  posting policy applies an insertion fee. It stays inactive until the exact
  price is visible and the organic test supplies a rational comparison.

### Zero-cost experiment design

Each placement receives a unique experiment token plus raw `utm_source`,
`utm_medium`, `utm_campaign`, and `utm_content`. The token is the authoritative
join across the website event, Salesforce Campaign/CampaignMember, Lead,
Opportunity, deposit, and delivery Case. Raw UTMs remain evidence and are not
overwritten during conversion.

The first checkpoint is seven days or 20 attributable enquiries, whichever
comes first. Any cohort below 20 is labelled **ANECDOTE**. A channel graduates
only on a paid order and completed delivery, not on impressions or messages.

## CAD 3,000 capital plan

| Gate | Cash released | Evidence required | Stop condition |
|---|---:|---|---|
| 0 — launch readiness | Up to CAD 200 | Legal seller and tax status known; fixed scope/SOW/refund/privacy live; payment and refund tested; analytics and Salesforce attribution verified; sample deliverables ready | Any unresolved seller, payment, privacy, delivery, or measurement failure |
| 1 — zero-cost demand discovery | CAD 0 media spend | Attributable owned/organic/free listings; qualified conversations; owner time measured | No qualified conversation after the defined checkpoint, or buyer feedback rejects the offer/problem |
| 2 — first paid search test | Additional CAD 300 maximum | One unrelated paid order delivered; positive contribution; Google billing and conversion tracking verified | CAD 200 without a qualified conversation; no defensible progress by CAD 300 |
| 3 — controlled scale | Cumulative paid media up to CAD 900 | Positive CAC and contribution; delivery inside cap; no material complaint | CAC exceeds the contribution ceiling, delivery slips, or channel quality falls |
| Reserve | At least CAD 2,000 | Three paid orders, at least two unrelated customers, two completed deliveries, and repeatable delivery evidence before release | Hold by default |

Google promotional credits are never counted until they post to the account.
Meta, YouTube, and ChatGPT Ads receive no Monday cash allocation.

## Unit economics and ROI

These are planning estimates, not promises. Owner time is a real cost.

| Offer | Price | Planning assumptions | Estimated contribution before acquisition, fixed overhead, and tax |
|---|---:|---|---:|
| Salesforce Org Opportunity Scan | CAD 1,950 | 8 owner hours; CAD 100/hour opportunity cost; approximately CAD 50 compute; Stripe domestic-card fee | About CAD 1,043; about CAD 643 if owner time is valued at CAD 150/hour |
| Website Conversion Prototype Sprint | CAD 895 | 4 owner hours at CAD 75/hour; approximately CAD 30 compute; Stripe fee | About CAD 539 |
| Applied-AI Workflow Clinic | CAD 399 | 2 owner hours at CAD 75/hour; approximately CAD 5 compute; Stripe fee | About CAD 232 |

The governing formula is:

> ROI = (collected revenue − payment fees − owner labour − model/runtime cost − refunds − ad spend) / ad spend

At the conservative planning margins, spending the entire CAD 3,000 on
acquisition would require approximately three Salesforce scans, six website
sprints, or thirteen clinics merely to recover that acquisition spend. A mixed
two-scan/two-website-sprint outcome produces about CAD 3,164 of contribution at
the lower Salesforce owner-time assumption. This is why capital releases are
incremental.

## Salesforce customer-success architecture

```text
Campaign + experiment token
            |
            v
Lead --qualify--> Account + Contact + Opportunity + Product/Price Book
                                      |
                                verified deposit
                                      |
                                      v
                        Onboarding / Delivery Case
                                      |
                       Tasks, Events, Email/WhatsApp
                                      |
                                      v
                    Deliverable + review + CSAT + support
                                      |
                               renewal / referral
```

| Salesforce capability | Operating role |
|---|---|
| Case | Executive business case, onboarding/delivery, support, escalation, and change control |
| Campaign / CampaignMember | Channel budget, activation gate, experiment token, raw attribution, and response state |
| Lead | Web, voice, WhatsApp, referral, directory, and ad intake before qualification |
| Account / Contact | Verified customer identity and relationship history |
| Opportunity | Scope, stage, proposal, deposit, collected revenue, probability, and close outcome |
| Product2 / PricebookEntry / OpportunityLineItem | The fixed service catalogue and quoted offer |
| Task / Event / EmailMessage | Follow-up, meetings, delivery milestones, and customer communication history |
| Case hierarchy | Parent engagement with child delivery, support, incident, or change-request Cases |
| Reports / dashboards | Spend, leads, qualified conversations, proposals, deposits, cash, CAC, contribution, response/resolution time, CSAT, and repeat business |

The first increment uses standard objects. Custom Payment, Engagement,
Artifact, Experiment, or Miniboard records are introduced only when the standard
model creates a measured operational gap. Entitlements, Omni-Channel,
Knowledge, and advanced automation are enabled only after the org's license and
delivery workflow are verified.

### Salesforce records created for this decision

- Executive Case **00001027**, ID `500fj00002YiMjNAAV`, status **Working**,
  priority **High**.
- One active parent Campaign with a CAD 3,000 control ceiling.
- Two active zero-cash Campaigns.
- Five inactive paid/conditional Campaigns, including the separately tracked
  Kijiji test.
- The org default currency was independently read as CAD. The Standard Price
  Book is active, with four active Products and Pricebook Entries:
  `SFSCAN-1950` at CAD 1,950, `WEBSPRINT-895` at CAD 895,
  `AIWORKFLOW-399` at CAD 399, and `SFARCH-3000` at CAD 3,000. These are
  quoteable catalogue prices; they do not authorize delivery outside the Case
  scope and capacity controls.

## Payment and account readiness

Stripe Payment Links is the preferred checkout because the Canadian standard
plan has no setup or monthly platform fee and charges 2.9% + CAD 0.30 for a
successful domestic-card payment. A new account must not assume immediate first
payout. PayPal or Square Payment Links can be fallbacks, but activating more
than one processor before demand exists adds reconciliation work.

The agents can prepare product copy, checkout metadata, webhook mapping,
Salesforce attribution, test cases, pages, and reconciliation. The legal account
owner must personally provide or attest to identity, entity type, beneficial
ownership, bank ownership, tax status, and financial-service terms. A public
post, account creation, payment, domain purchase, or message is a final-action
approval point after all fields and costs are visible.

## Execution calendar to Monday, September 28

| When | Agents execute | Legal-account-owner action only when required | Evidence gate |
|---|---|---|---|
| Friday Sep 25 | Finalize offer/SOW/privacy/refund copy; create Salesforce control records; prepare tracked landing and listing variants; validate seller-domain choices | Confirm legal seller and current GST/HST registration status; complete payment KYC/bank verification if prompted | Case and Campaign read-back; all public claims trace to a tested capability |
| Saturday Sep 26 | Build sample Salesforce scan and website prototype; wire source tokens, UTMs, analytics, Lead/Campaign mapping, checkout-success event and delivery Case path | Confirm any regulated identity or financial attestation | Full dry run from source token to Lead/Opportunity/payment event/Case |
| Sunday Sep 27 | QA mobile/accessibility/performance; prepare LinkedIn, owned-site, community, and opt-in copy; run payment/refund test; reconcile dashboard | Approve the exact public posts/messages and any real charge immediately before submission | No broken path; no unreviewed claim; test transaction reconciles |
| Monday Sep 28 | Publish approved zero-cost surfaces; monitor responses; qualify into Salesforce; enforce capacity and stop rules; report cash separately from pipeline | Respond only to identity/contract decisions that legally require the owner | First attributable conversations; no paid Campaign activation |

## Source register

- [Statistics Canada — AI use by businesses, Q2 2026](https://www150.statcan.gc.ca/n1/pub/11-621-m/11-621-m2026010-eng.htm)
- [BDC — Digital transformation of SMEs in the age of AI](https://www.bdc.ca/en/about/analysis-research/digital-transformation-of-smes-in-the-age-of-artificial-intelligence)
- [CFIB — SME digital presence](https://www.cfib-fcei.ca/en/research-economic-analysis/sme-digital-presence?hs_amp=true)
- [ISED — Key Small Business Statistics 2025](https://ised-isde.canada.ca/site/sme-research-statistics/en/key-small-business-statistics/key-small-business-statistics-2025)
- [Salesforce — State of Sales 2026](https://www.salesforce.com/news/stories/state-of-sales-report-announcement-2026/)
- [LinkedIn — Page and Service Page capabilities](https://www.linkedin.com/help/linkedin/answer/a727893)
- [Kijiji — Services listing fee policy](https://help.kijiji.ca/helpdesk/basics/services-listing-fee)
- [Meta — Marketplace and small-business listing direction](https://about.fb.com/news/2026/07/connecting-real-people-on-facebook/amp/)
- [Google — Business Profile eligibility](https://support.google.com/business/answer/13763036?hl=en)
- [CRTC — CASL compliance guidance](https://crtc.gc.ca/eng/com500/guide.htm)
- [Stripe Canada — pricing](https://stripe.com/en-ca/pricing)
- [Stripe — Canadian account verification](https://support.stripe.com/questions/what-do-i-need-to-do-to-verify-my-stripe-account?locale=en-CA)
- [OpenAI — Ads Manager availability](https://help.openai.com/en/articles/20001245-ads-manager-availability)
- [Salesforce — trademark usage guidelines](https://www.salesforce.com/en-us/wp-content/uploads/sites/4/documents/legal/Terms%20of%20Service/salesforce-trademark-usage-guidelines.pdf)

# Monday Zero-Cost Demand Test

**Version:** 0.1  
**Launch date:** 2026-09-28  
**Control Case:** Salesforce Case 00001027 (`500fj00002YiMjNAAV`)  
**Free-channel Campaign:** `701fj00001ynaX7AAI`  
**Owned/opt-in Campaign:** `701fj00001ynaX8AAI`  
**Status:** Prepared, not posted

## Operating rule

Publish only to a destination whose current rules allow the service and whose
account owner has approved the exact final post. No fake scarcity, ordinary
price comparison, performance guarantee, affiliation claim, scraped audience,
or unsolicited bulk message. Every response must be attributable in
Salesforce. Kijiji is not a free channel and Facebook Marketplace is not used
for service listings.

## Tracking contract

Each surface receives a unique experiment token. Preserve the token and raw
UTMs from first visit through Lead, CampaignMember, Opportunity, payment, and
delivery Case.

| Surface / offer | Experiment token | UTM source | UTM medium | UTM campaign | UTM content |
|---|---|---|---|---|---|
| LinkedIn Service Page — Salesforce Scan | `LI-SFSCAN-A-20260928` | `linkedin` | `service-page` | `monday-launch-20260928` | `sfscan-a` |
| LinkedIn organic — Salesforce Scan | `LI-SFSCAN-B-20260928` | `linkedin` | `organic` | `monday-launch-20260928` | `sfscan-b` |
| LinkedIn organic — Website Sprint | `LI-WEBSPRINT-A-20260928` | `linkedin` | `organic` | `monday-launch-20260928` | `websprint-a` |
| Facebook Page/permitted group — Website Sprint | `FBG-WEBSPRINT-A-20260928` | `facebook` | `organic-community` | `monday-launch-20260928` | `websprint-a` |
| Owned site — Salesforce Scan | `OWN-SFSCAN-A-20260928` | `sfdc24` | `owned` | `monday-launch-20260928` | `sfscan-a` |
| Opt-in WhatsApp/email — Salesforce Scan | `WARM-SFSCAN-A-20260928` | `warm-network` | `opt-in` | `monday-launch-20260928` | `sfscan-a` |
| Kijiji conditional paid listing — Website Sprint | `KIJ-WEBSPRINT-A-20260928` | `kijiji` | `paid-listing` | `monday-launch-20260928` | `websprint-a` |

Canonical URL pattern:

```text
{{LANDING_URL}}?exp={{EXPERIMENT_TOKEN}}&utm_source={{SOURCE}}&utm_medium={{MEDIUM}}&utm_campaign=monday-launch-20260928&utm_content={{CONTENT}}
```

Do not publish the URL until the landing page, analytics event, Salesforce Lead
mapping, and Campaign association have passed a full dry run.

## Offer 1 — Salesforce Org Opportunity Scan

### LinkedIn Service Page copy

**Service name**  
Salesforce Org Opportunity Scan

**Price**  
CAD 1,950 — founder batch of three engagements

**Description**  
Get a practical, prioritized view of where your Salesforce org can work harder
for the business—without making production changes.

For one authorized Salesforce org, the Scan includes:

- a read-only review of metadata, automation, permissions, limits, dashboards,
  and aggregated counts;
- ten prioritized improvement opportunities with effort bands;
- a written roadmap and a 60-minute review;
- delivery targeted within five business days.

It does not include production changes, data cleanup or migration, credential
storage, penetration testing, CPQ or Marketing Cloud deep dives, or a security
certification. Record-level personal data is not sent to general-purpose AI
models. The service is independent and is not represented as sponsored by or
affiliated with Salesforce.

**Call to action**  
Send your role, company size, Salesforce edition, and the business problem you
most want to fix. We will confirm fit and the exact access boundary before any
payment.

### LinkedIn organic post

Many Salesforce teams know the org could be doing more, but the backlog mixes
real business opportunities with cleanup that may not matter.

I am opening a founder batch of three **Salesforce Org Opportunity Scans** at
CAD 1,950 each. The engagement is read-only: one authorized org, a review of
metadata and operating signals, a ten-item roadmap with effort bands, and a
60-minute walkthrough. No production changes and no record-level personal data
sent to general-purpose AI models.

If your team wants a clear answer to “what should we improve first?”, reply or
send a message with your role, Salesforce edition, and the one process causing
the most friction. I will confirm fit before asking for payment.

Independent service; no Salesforce affiliation is claimed.

## Offer 2 — Website Conversion Prototype Sprint

### LinkedIn organic post

If your website looks respectable but does not make the next step obvious, a
full rebuild may be the wrong first purchase.

The **Website Conversion Prototype Sprint** turns one existing website into a
concrete direction in three business days:

- review of the site and up to five important pages;
- rewritten homepage copy;
- one static, responsive homepage concept;
- prioritized calls-to-action and conversion recommendations;
- basic automated accessibility and performance observations;
- a walkthrough.

Founder price: **CAD 895**. This is a prototype and decision package, not a full
site implementation, hosting plan, brand identity, compliance certification,
or traffic/conversion guarantee.

Send the current website URL and the one action you most want a visitor to
take. I will reply with three specific observations and confirm whether the
sprint is a fit before payment.

### Facebook Page or permitted local-business group post

Local business owners: is your current website clear about what you do, who it
is for, and what a visitor should do next?

I am testing a fixed **Website Conversion Prototype Sprint** for established
service businesses. For CAD 895, it includes an up-to-five-page review,
homepage copy rewrite, one mobile-responsive homepage concept, prioritized
conversion recommendations, and a walkthrough. Target delivery is three
business days.

It is not a full rebuild and there is no SEO, traffic, accessibility-compliance,
or conversion guarantee. The purpose is to give you a working direction before
you commit to a larger implementation.

If the group permits service posts, comment or message with your website and
the main action you want visitors to take. I will confirm fit before payment.

### Kijiji Services listing — conditional, not free

**Title**  
See a Responsive Homepage Direction Before Funding a Full Rebuild

**Price**  
CAD 895

**Body**  
For an established small business with an existing website, this fixed sprint
delivers an up-to-five-page review, rewritten homepage copy, one static
mobile-responsive homepage concept, prioritized conversion recommendations,
and a walkthrough. Target delivery: three business days.

This is a prototype and decision package. It does not include full-site
development, hosting, a complete brand identity, SEO rankings, formal
accessibility certification, or a promise of more sales.

Send the website URL, business type, city or service area, and the single most
important visitor action. Fit and scope are confirmed before payment.

**Activation rule**  
Kijiji currently charges an insertion fee for Services listings. Read the exact
fee, term, taxes, and renewal/boost state at the final screen. Do not submit or
pay until that exact total is approved. Do not buy a boost or package.

## Warm opt-in message — Salesforce Scan

Use only where there is consent, an existing business relationship, or the
recipient has explicitly asked about Salesforce help.

> I am opening a small founder batch for a read-only Salesforce Org Opportunity
> Scan. It produces a ten-item improvement roadmap and walkthrough without
> making production changes. The fixed price is CAD 1,950. If that is relevant,
> I can send the exact scope and access boundary; if not, no action is needed.

Every commercial electronic message must identify the sender, provide current
contact information, and include a working unsubscribe path where CASL requires
it.

## Qualification questions

Ask only what is needed to determine fit.

### Salesforce Scan

1. What is your role and organization type?
2. Which Salesforce edition and clouds are in scope?
3. What business process is causing the most friction?
4. Is the org authorized to provide read-only access or a metadata export?
5. Does the org contain health, financial, child, or other highly sensitive
   record-level data? If yes, stop and design a safer boundary before sale.
6. Who can approve scope and payment?
7. What decision must the roadmap support within 30 days?

### Website Sprint

1. What is the current URL?
2. What single visitor action matters most?
3. Who is the primary buyer/customer?
4. Which five pages matter most?
5. Are the existing brand assets and copy owned or licensed for reuse?
6. Who approves the prototype?
7. Is implementation expected? If yes, make clear that it requires a separate
   SOW.

## Salesforce routing

1. Create or match the Lead; never create duplicates blindly.
2. Set Lead Source to the closest approved standard value and preserve the raw
   source and experiment token in the agreed attribution fields or intake
   payload.
3. Add the Lead or Contact to the correct Campaign with a response state.
4. Record the first response, qualification answers, consent basis, and next
   action as a Task/Activity.
5. Convert only after identity and buying intent are credible; create Account,
   Contact, and Opportunity.
6. Add the exact Pricebook Entry to the Opportunity:

   - `SFSCAN-1950` — CAD 1,950
   - `WEBSPRINT-895` — CAD 895
   - `AIWORKFLOW-399` — CAD 399
   - `SFARCH-3000` — CAD 3,000 starting price

7. Treat a proposal, deposit, collected payment, and completed delivery as four
   separate states.
8. A verified deposit creates an onboarding/delivery Case linked to the
   customer and Opportunity.

## Daily scorecard

| Metric | Report separately |
|---|---|
| Exposure | Post status, views/reach where supplied by platform |
| Interest | Messages and landing-page starts |
| Qualification | Qualified conversations and disqualifying reasons |
| Commercial progress | Proposals, deposits, collected cash |
| Delivery | Starts, completed deliverables, owner hours, exceptions |
| Economics | Fees, compute, owner cost, refunds, contribution, CAC |
| Customer success | First-response time, time to first useful result, resolution time, CSAT, repeat/referral |

No channel is called successful from impressions or one friendly response.
Fewer than 20 attributable enquiries is reported as **ANECDOTE**.

## Final pre-publish checklist

- The destination's current rules allow this exact service post.
- The legal seller and contact identity are accurate.
- The landing URL and source token have passed an end-to-end test.
- Scope, price, tax treatment, timing, exclusions, refund/cancellation terms,
  privacy notice, and AI/human-review disclosure are consistent.
- No unsupported affiliation, certification, testimonial, urgency, discount,
  or outcome claim is present.
- Salesforce Campaign, Product, routing, and capacity controls are ready.
- The exact final post or message and target audience have been approved
  immediately before submission.

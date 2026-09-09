# Salesforce org inspection over MCP — a plan

Asked for by Mr. Salam, 2026-09-09 05:26Z: *"work on salesforce integration and
improving overall architecture to inspect, build and model solutions, assess
issues and score metrics for six sigma levels. Can you plan on creating MCP
client for salesforce that can be plugged into any salesforce org instance for
clients?"*

**Revised 06:00Z after checking whether a Salesforce MCP server already exists.
It does — three of them, two official. That check changed the plan and cut the
build by most of its size.** The first version of this document is preserved in
the PR history; what follows replaces it.

---

## 1. He said "client". He was right, and I corrected him wrongly

The first draft of this plan told him the correct term was an MCP *server*. That
would be true if nothing existed. It does:

| what | who | shape |
|---|---|---|
| **Hosted MCP Server** | Salesforce, GA April 2026 | Salesforce-managed endpoint. Records, flows, Apex invocable actions, `@AuraEnabled` methods, Named Queries. **Per-user OAuth 2.0 + PKCE** — the agent acts inside the requesting user's own permissions, so CRUD, FLS and sharing all still apply. Enterprise Edition and above. Read **and** write. |
| **Salesforce DX MCP Server** | Salesforce, open source, `github.com/salesforcecli/mcp` | Self-hosted, TypeScript. Org listing, org records, **metadata retrieve/deploy**. Developer-preview since May 2025. |
| community servers | several | SOQL/SOSL, metadata, Tooling API, Apex REST. |

So the server side is solved, twice over by Salesforce themselves. **What we
build is a client.** His word was the accurate one and mine was not.

---

## 2. What this leaves us building — and it is much smaller

```
Salesforce org
     │  Hosted MCP Server (theirs) or DX MCP Server (theirs, open source)
     ▼
  our MCP CLIENT  ──►  scoring engine  ──►  xray-score-v1  ──►  /xray/
     (small)            (the product)        (contract, live)   (live)
```

`/xray/` shipped on 2026-09-09 and already consumes `xray-score-v1`:

```
overall  { score, belt, sigma, dpmo }
pillars  SECURITY | OPERABILITY | WASTE | REDUNDANCY
         each { score, belt, sigma, dpmo, defects, opportunities }
findings [ { id, pillar, severity, title, opportunities, defects, dpmo,
             sigma, effort, priority, frame, rec } ]
```

That is a real six-sigma model — defects per million opportunities → sigma →
belt — and its presentation layer is built and reviewed.

**So neither end is missing. The scoring engine in the middle is.** Nothing today
turns org facts into an `xray-score-v1`. That middle is also the only part a
competitor cannot copy in an afternoon.

---

## 3. Which of their servers to use, and why it matters

**Prefer the DX MCP Server for inspection**, at least first:

- it is **open source**, so we can read exactly what it does before pointing it
  at a client's org — a claim we can make to a security review and support;
- it exposes **metadata**, which is where the org-health signals actually live
  (triggers, flows, permission sets, profiles). The Hosted server is oriented at
  records and actions;
- it is **self-hosted**, so it works against any org including the Developer
  Edition we already control — the Hosted server needs Enterprise Edition and
  above, which our DE test org may not have. That alone decides the first sprint.

**Use the Hosted server where a client already has Enterprise+ and prefers a
Salesforce-managed endpoint.** Their per-user OAuth with PKCE is a *better*
security story than the JWT service account the first draft proposed: a scan
cannot see more than the person who authorised it, enforced by Salesforce rather
than by our good intentions. Withdrawing my own suggestion in favour of theirs.

---

## 4. What each pillar needs from the org

All read-only. All available through metadata + Tooling API + SOQL, which the
existing servers already expose.

| pillar | signals |
|---|---|
| SECURITY | Health Check score, profiles/perm sets granting Modify All Data, org-wide defaults, session and password policy, guest user access |
| OPERABILITY | Apex test coverage, failed scheduled jobs, API volume against limits, flow error rates, triggers per object |
| WASTE | fields never populated, users inactive 90+ days holding licences, reports and dashboards never run, unused permission sets |
| REDUNDANCY | multiple active triggers on one object, overlapping flows on one event, duplicate permission sets, near-identical record types |

**The scoring is the intellectual property, not the queries.** Anyone can count
triggers. Deciding that two active triggers on one object is a defect *against a
defined opportunity set* — and that the set is "objects with automation" rather
than "all objects" — is the judgement a client pays for. It is already encoded in
the findings on `/xray/`.

---

## 5. Read-only, and say so out loud

The Hosted server can write. We should not.

Writing into a client's production org is a different liability class from
reading it. An inspection tool that *cannot* write is one a security review
approves in an afternoon; one that can is held for a quarter. And "build and
model solutions" is better served by the **prototype publisher we already have**:
generate the proposed design, publish it as a page the client can open, let them
implement it in their own org. They see the artefact before anyone touches
anything, which is a better sales motion as well as a safer one.

---

## 6. Governor limits are a feature

An inspection that burns a client's daily API quota is one they disable. The
client must read `/limits` first, refuse a scan that would consume more than a
configured share of what remains, report what it spent, and cache — org metadata
does not change between two scans an hour apart.

---

## 7. First sprint, now that the server is not ours to build

1. Stand up the **DX MCP Server** against the Developer Edition org
   (`omnistudio-sandbox`) we already control. No client involved.
2. Write the **scoring engine** against `xray-score-v1`, which is already
   specified and already has a consumer. Test it with recorded org fixtures and
   no live org at all — the same shape as the 590-case corpus that settled the
   `/xray/` guard.
3. Produce the **first real `xray-score-v1`** from our own DE org. That is the
   honest replacement for the synthetic data `/xray/` currently carries and
   correctly labels as fake in four places.
4. Only then talk to a client org.

---

## 8. What this is worth, honestly

The MCP plumbing is not the differentiator and now demonstrably never was —
Salesforce ship it, twice, free.

The differentiator is the scoring model, a presentation layer that already
exists, and a review culture that rejects unevidenced claims — where a client can
read the whole argument afterwards. See `docs/POKA-YOKE.md`.

---

## Decisions that are Mr. Salam's

1. **Read-only permanently?** I recommend yes, with "build" served by the
   prototype publisher.
2. **Which server first** — DX (open source, works on our DE org) or Hosted
   (Salesforce-managed, needs Enterprise+)? I recommend DX for sprint one.
3. **Does the first real org readout go public on `/xray/`,** or stay internal? A
   real org's readout is client data even when the client is us.
4. **Does this jump the six open Zoom blockers?** Smaller than the first draft
   implied, but still larger than anything currently in flight.

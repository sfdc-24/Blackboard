# Salesforce org inspection over MCP — a plan

Asked for by Mr. Salam, 2026-09-09 05:26Z: *"work on salesforce integration and
improving overall architecture to inspect, build and model solutions, assess
issues and score metrics for six sigma levels. Can you plan on creating MCP
client for salesforce that can be plugged into any salesforce org instance for
clients?"*

This is a plan, not an implementation. It ends with the decisions that are his.

---

## 1. One terminology correction, because it changes what gets built

He asked for an **MCP client**. What plugs into a Salesforce org and exposes it
to an AI is an **MCP server**: the org is the resource, the server surfaces it,
and Claude/Codex/any MCP-capable agent is the client. The distinction is not
pedantry — it decides who runs the process, where credentials live, and who the
customer is.

Everything below describes an **MCP server for a Salesforce org**.

---

## 2. The thing that makes this cheap: we already own the contract

`/xray/` shipped to production on 2026-09-09 and already consumes a scoring
document, `xray-score-v1`:

```
overall  { score, belt, sigma, dpmo }
pillars  SECURITY | OPERABILITY | WASTE | REDUNDANCY
         each { score, belt, sigma, dpmo, defects, opportunities }
findings [ { id, pillar, severity, title, opportunities, defects, dpmo,
             sigma, effort, priority, frame, rec } ]
```

That is a real six-sigma model — defects per million opportunities, sigma level,
belt grade — and the presentation layer for it is **already live and reviewed**.

**So the six-sigma scoring he asked for is not the missing piece. The
_collector_ is.** Today `/xray/` is fed synthetic data and its own page says so.
Nothing exists that connects to a real org and emits a real `xray-score-v1`.

That gives the whole project a clean seam:

```
Salesforce org  ──►  MCP server  ──►  xray-score-v1  ──►  /xray/  (exists)
                     (to build)        (the contract,      (exists)
                                        already defined)
```

The server never learns about the web page; the page never learns about
Salesforce. Either can be replaced without touching the other, and the contract
is testable on its own with no org at all.

---

## 3. What each pillar actually costs to compute

Every one of these is answerable from the Tooling API, the Metadata API or
plain SOQL. None of it needs write access.

| pillar | signals | source |
|---|---|---|
| SECURITY | Health Check score, profiles/perm sets granting Modify All Data, org-wide sharing defaults, session and password settings, guest user access | Health Check, `PermissionSet`, `Profile`, `Organization` |
| OPERABILITY | Apex test coverage, failed scheduled jobs, API request volume against limits, flow error rates, triggers per object | `ApexCodeCoverageAggregate`, `AsyncApexJob`, `/limits`, `FlowDefinitionView` |
| WASTE | fields never populated, users inactive 90+ days holding licences, reports and dashboards never run, unused permission sets | field usage sampling, `User`, `Report`, `Dashboard` |
| REDUNDANCY | multiple active triggers on one object, overlapping flows on the same event, duplicate permission sets, near-identical record types | `ApexTrigger`, `FlowDefinitionView`, `PermissionSet` |

**The scoring is the intellectual property, not the queries.** Anyone can count
triggers. Deciding that two active triggers on one object is a *defect against a
defined opportunity set* — and that the opportunity set is "objects with
automation" rather than "all objects" — is the judgement a client is paying for.
That judgement is already encoded in `xray-score-v1`'s findings.

---

## 4. Authentication — the part that decides whether this is sellable

**Connected App + OAuth, per client org. Never a password, never ours.**

- **JWT bearer flow** for headless inspection: the client's admin authorises our
  Connected App once, we hold a certificate rather than a credential, and the
  client can revoke it from their own setup at any time without contacting us.
  This is the only model in which "plugged into any org" is honest.
- **Read-only scopes.** `api`, `refresh_token`, and nothing that writes.
- **Per-org isolation.** One org's token cannot read another org. This is not a
  configuration detail; it is the whole product's liability position.
- Credentials live in the machine's local `.env` or a secret store, never in the
  repo, never on a command line. (D-18, and `.gitignore` already carries the
  scars of that lesson.)

---

## 5. Read-only, and say so out loud

The instruction says *inspect, build and model solutions*. Only "inspect" is in
this plan, deliberately.

Writing to a client's production org is a different liability class from reading
it, and it should be a different product with a different conversation. An
inspection tool that cannot write is one a client's security review can approve
in an afternoon. One that can write is one their CISO will hold for a quarter.

"Build and model solutions" is well served by the **prototype publisher we
already have** — generate a proposed design, publish it as a page the client can
open, and let *them* implement it in their own org. That keeps our hands off
their production data and is a better sales motion anyway: they see the artefact
before anyone touches anything.

---

## 6. Governor limits are a feature, not an obstacle

An inspection that burns a client's daily API quota is one they will disable.
The server must:

- read `/limits` **first** and refuse to run a scan that would consume more than
  a configured share of the remaining quota;
- report what it spent in the scan output, as a line the client can see;
- cache aggressively — org metadata does not change between two scans an hour
  apart.

Handled well this is a differentiator to say out loud in a pitch. Handled badly
it is the reason the pilot ends.

---

## 7. Before writing any code

1. **Check whether a Salesforce MCP server already exists** — Salesforce Labs,
   the MCP community registry, or an npm package. Building what already ships
   would be a poor use of a week. *This has not been checked and must not be
   assumed either way.*
2. **Decide host.** The VM, not the laptop: an inspection service a client
   depends on cannot live on a machine that closes at night.
3. **Test target already exists.** The Developer Edition org
   (`omnistudio-sandbox`) is a real org we control. First scan goes there, and
   the first `xray-score-v1` it produces is the honest replacement for the
   synthetic data on `/xray/` — which the page currently, correctly, labels as
   fake in four places.

---

## 8. What this is worth, honestly

The differentiator is not the MCP server. Anyone can wrap an API.

It is that we already have a **scoring model, a presentation layer, and a review
culture that rejects unevidenced claims** — and a client can read the whole
argument afterwards. See `docs/POKA-YOKE.md`. The server is the missing input to
a machine that already runs.

---

## Decisions that are Mr. Salam's, not mine

1. **Read-only, or read-write later?** I recommend read-only, permanently, with
   "build" served by the prototype publisher instead.
2. **Who hosts it** — us as a service, or shipped for the client to run inside
   their own perimeter? The second is easier to sell to a security team and
   harder to charge for.
3. **Does the first real scan go to `/xray/` publicly**, or stay internal? A
   real org's readout is client data even when the client is us.
4. **Is this ahead of, or behind, the five open Zoom blockers?** It is a bigger
   piece of work than anything currently in flight.

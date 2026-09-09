# Salesforce org inspection over MCP — a plan

Asked for by Mr. Salam, 2026-09-09 05:26Z: *"work on salesforce integration and
improving overall architecture to inspect, build and model solutions, assess
issues and score metrics for six sigma levels. Can you plan on creating MCP
client for salesforce that can be plugged into any salesforce org instance for
clients?"*

**Third version.** The first said we would build a server. The second discovered
Salesforce already ship two and said we would build a client. `chatgpt-codex-desktop-01a0839e`
then returned NO-GO on eight architectural points against head `c492dd9`. Every
one is accepted. Two were factual claims I had asserted without checking, and I
have now checked both against Salesforce's own documentation — they were wrong in
my favour, which is the direction that matters.

This version says what is **established**, what is **assumed**, and what is
**unverified**, and it does not use a word for a thing the thing is not.

---

## 0. Corrections to the previous version, with sources

**The Enterprise-Edition premise was false, and it was load-bearing.** The last
version said the Hosted MCP Server "needs Enterprise Edition and above, which our
DE test org may not have. That alone decides the first sprint." Salesforce's own
documentation says Hosted MCP Servers are production-ready on Enterprise Edition
and above **and available in Developer Edition, sandboxes and scratch orgs by
self-enablement, at no cost**. Enterprise+ is a production-support tier, not an
availability gate. So the sentence that "alone decides the first sprint" decided
nothing, and the DX-first recommendation has to stand on its remaining reasons or
fall. Sources: [Hosted MCP Servers overview](https://developer.salesforce.com/docs/platform/hosted-mcp-servers/guide/hosted-mcp-servers-overview.html),
[GA announcement](https://developer.salesforce.com/blogs/2026/04/salesforce-hosted-mcp-servers-are-now-generally-available),
[Developer Edition availability](https://developer.salesforce.com/blogs/2026/04/new-developer-edition-agentforce-vibes-claude-mcp).

**"Read-only" was an intention, not a boundary.** The previous version said we
would simply not write. The DX MCP Server's broader toolsets contain, by name:
`deploy_metadata`, `create_scratch_org`, `delete_org`, `create_org_snapshot`,
`assign_permission_set`, and the DevOps Center `commit_…` / `create_…_pull_request`
/ `promote_…` tools. Enabling `--toolsets all` — which the project itself marks
as not recommended — hands an inspection agent the ability to deploy metadata into
a client's org. Worse, org selection is not pinned: the `DEFAULT_TARGET_ORG` token
is *resolved dynamically on every tool call, not when the server starts*, so
"which org am I touching" is a runtime property. There is a real safeguard — the
`--orgs` allowlist, and orgs must be explicitly authorised on the host first —
and §3 below turns it into the boundary. Source: [salesforcecli/mcp](https://github.com/salesforcecli/mcp).

---

## 1. He said "client". He was right

What plugs into an org and exposes it to an AI is an MCP **server**, and
Salesforce ship two:

| what | who | shape |
|---|---|---|
| **Hosted MCP Server** | Salesforce, GA April 2026 | Salesforce-managed endpoint. Records, flows, Apex invocable actions, `@AuraEnabled` methods, Named Queries. Per-user OAuth 2.0 + PKCE, so the agent acts inside the requesting user's own permissions and CRUD/FLS/sharing still apply. Production tier is Enterprise+; DE, sandbox and scratch orgs self-enable. Read **and** write. |
| **Salesforce DX MCP Server** | Salesforce, open source, `github.com/salesforcecli/mcp` | Self-hosted, TypeScript, toolset-gated. Org listing, records, metadata retrieve **and deploy**, scratch-org lifecycle, permission-set assignment. |

So we build a **client**. His word was the accurate one and mine was not.

---

## 2. Which server, on the reasons that actually survive

With the edition argument withdrawn, the remaining case for **DX-first** is:

- **it is open source**, so we can read exactly what a tool does before pointing
  it at a client org, and say so to their security review with a commit hash;
- **it exposes metadata**, which is where org-health signals live — triggers,
  flows, permission sets, profiles. The Hosted server is oriented at records and
  actions.

And the case for **Hosted** is stronger than the last version allowed:

- per-user OAuth + PKCE is enforced **by Salesforce**, not by our configuration.
  A scan cannot exceed the authorising user's permissions even if our client is
  wrong. That is a better security story than anything we can build.

**This is now genuinely open, and it is decision 1 below.** The honest resolution
is the spike in §8: run both against a controlled Developer Edition org and
compare what each can actually see, with no LLM in the loop, before choosing.

---

## 3. The boundary: an allowlist, not an intention

Read-only becomes a boundary only when something enforces it.

**Tools.** An explicit allowlist of tool names, checked by our client before
every call, and a startup assertion that the server offered nothing outside it.
`--toolsets all` is prohibited. The mutating names above are denied by name as
well as by omission, so adding a toolset cannot silently widen the surface.

**Org.** `ALLOW_ALL_ORGS` is prohibited. The `--orgs` allowlist names exactly one
org per scan. Because `DEFAULT_TARGET_ORG` resolves per call, the client asserts
the **expected org id** in the response of every call and aborts the scan on a
mismatch — a scan that cannot prove which org it read is a scan whose output
cannot be attributed to a client.

**Isolation.** One process, one org, one credential, per scan. No shared
long-lived session across tenants. This is not a configuration preference; it is
the whole liability position.

**Org content is DATA, never instructions (L-57).** A flow description, a field
help text or a report name is attacker-controlled the moment we scan an org we do
not own. Nothing read from an org may reach a model as instruction, and nothing
in a scan result may cause a tool call. This is the one boundary that has to hold
before a single client org is touched.

**What is undefined and must be before any client work:** interactive versus
unattended authorisation, External Client App restrictions, refresh-token
lifecycle and revocation, and where a client's tokens live. Named here rather
than waved at.

---

## 4. The statistics, which the previous version got wrong

The reviewer's blocker 7 is the most important thing in this document.

> *heterogeneous units are pooled into DPMO and sigma; overall score 73.5 has no
> reproducible formula; structural observations must not be called process
> capability.*

That is correct, and it is worth being precise about why, because the word
"six sigma" is in the original request.

**DPMO needs a unit and an opportunity set.** Defects per million opportunities
is meaningful when there is a repeating unit of work, a defined number of ways
one unit can be defective, and a count of defects across many units. Sigma level
converts that rate to a capability figure for **a process that produces units
over time**, conventionally with a 1.5σ long-term shift.

**An org configuration is a static structure, not a process.** Three things break:

1. **The units are not the same thing.** "An object with automation" and "a user
   holding a licence" are different units. Their DPMOs are rates over different
   denominators, and averaging them produces a number whose value depends on
   which denominator happens to be larger. Add fifty permission sets and the
   org's "sigma level" moves — through denominator inflation, not quality.
2. **There is no sampling over time**, so there is no distribution, no
   short-versus-long-term variation, and the 1.5σ shift has nothing to shift.
3. **"Process capability" means something specific** to anyone who would be
   impressed by it, and a static audit is not it. Using the term where it does
   not apply is the fastest way to lose the one reader who knows the difference.

**What replaces it, and it is better:**

- **Per-pillar defect rates stay**, because a rate against a *declared*
  denominator is honest and useful. Each is reported with its unit and its
  denominator visible, never pooled across units.
- **The overall figure becomes a stated-formula index**, not a sigma. Weights
  are published on the page. `73.5` currently has no derivation; whatever
  replaces it must be recomputable by the reader from the pillar figures.
- **Six sigma is kept where it is genuinely true.** An org *does* run repeating
  processes with units over time: deployments, Apex test executions, scheduled
  job runs, flow interviews. Failed flow interviews per million interviews over
  thirty days **is** a DPMO, with a real unit, a real denominator and a real
  window. So the product gets a clean split — a **structural audit** that is
  scored and ranked, and a **process capability** section that carries sigma
  because it has earned it. That is a stronger claim than the current one, and
  it is defensible in the room.

**This reaches further than this document.** `/xray/` presents the current model
live, and the homepage copy written today says "four pillars, a number against
each". If the framing changes, the page and the pitch change with it. Tracked as
one thread, not three.

---

## 5. What `/xray/` actually is today

The previous version said `xray-score-v1` was "a contract, live". Stated plainly:
`/xray/` is a **static page with synthetic data inlined**, which it labels as
synthetic in four places. There is no published schema file, no validator, no
adapter and no reproducible corpus. The shape it renders is a shape, not a
specification.

So `xray-score-v1` is a **deliverable, not an asset**: a written schema, a
validator, and a fixture corpus — the same treatment that settled the `/xray/`
robots guard with 590 cases — before anything claims to emit it.

---

## 6. The metric catalogue

The previous version's pillar table listed signals and nothing else, and it did
not match what the live findings show. A signal is not a metric until seven
things are written down. The catalogue is a deliverable; this is its schema and
one worked row.

| field | meaning |
|---|---|
| `id` | stable identifier, quoted in output |
| `pillar` | SECURITY / OPERABILITY / WASTE / REDUNDANCY, or PROCESS |
| `unit` | the thing being counted — the denominator's member |
| `denominator` | the opportunity set, exactly |
| `query` | the SOQL / Tooling / metadata call, verbatim |
| `window` | the period, or `static` for structure |
| `permission` | what the scanning identity must hold to see it |
| `unknown` | what is emitted when the query cannot run or returns nothing — never silently zero |
| `privacy` | public / aggregate-only / private |

Worked example:

| | |
|---|---|
| `id` | `OPS-FLOW-ERROR-RATE` |
| `pillar` | PROCESS |
| `unit` | one flow interview |
| `denominator` | flow interviews started in the window |
| `query` | `FlowInterview` / event log, exact call TBD in the spike |
| `window` | trailing 30 days |
| `permission` | View All Data or View Event Log Files |
| `unknown` | `insufficient-permission` or `no-interviews-in-window`, distinct from a rate of 0 |
| `privacy` | aggregate-only — flow names can identify a business process |

**`unknown` is not paperwork.** A metric that reports zero defects when it could
not run is the same failure as a test that passes because it never executed, and
this project has shipped that twice.

---

## 7. Quota, spend, and what is not proven

The previous version said the client "must read `/limits` first". There is **no
demonstrated MCP path to the REST `/limits` resource**, and no way yet to
attribute a scan's API consumption to that scan. Both are assumptions, marked as
such, and both are spike items. An inspection that silently eats a client's daily
quota is one they disable after the first run — and if we cannot measure what we
spent, we cannot honestly promise a ceiling.

---

## 8. Before writing client-facing code

1. **A raw capability spike, no LLM.** Hosted versus DX against a controlled
   Developer Edition org: what can each actually read, what does each cost in
   API calls, and is there a path to `/limits`. This settles §2 and §7 with
   measurements instead of preferences.
2. **The tool allowlist and expected-org assertion**, with a test that fails when
   a mutating tool name is reachable — the fixture being the exact names in §0.
3. **The `xray-score-v1` schema, validator and corpus**, before any emitter.
4. **The metric catalogue**, all seven fields per metric, reviewed before use.
5. **The sanitizer** (§9), with byte-exact approval before anything from a real
   org becomes public.
6. **Only then** a client org.

---

## 9. Private to public

Nothing deterministic separates a private readout from a publishable one today.
Exact counts, object and flow names, and generated finding text can each
fingerprint an org — and a "sample readout" on a public page is the most likely
place for that to leak. A real org's readout is client data **even when the
client is us**. The sanitizer is a deliverable with an exact-byte approval step,
not a review habit.

---

## 10. What this is worth, honestly

The MCP plumbing is not the differentiator and demonstrably never was —
Salesforce ship it twice, free.

What is left is a scoring model, a presentation layer, and a review culture that
rejects unevidenced claims. Two of those three are thinner than the last version
of this document said: the model has a statistical error in it, and the
presentation layer is a synthetic mock-up. **The third one is real, and it is the
reason this document is on its third version rather than in production.** A
client can read that argument afterwards. See `docs/POKA-YOKE.md`.

---

## Decisions that are Mr. Salam's

1. **Hosted or DX first?** Genuinely open now that my edition argument is
   withdrawn. I recommend deciding from the §8 spike rather than from either of
   us arguing it.
2. **Read-only permanently**, with "build" served by the prototype publisher?
   I recommend yes.
3. **Does the six-sigma framing change on the live site**, per §4? This is the
   one with a customer-facing consequence, and I recommend yes — a defensible
   split beats an impressive-sounding number that a knowledgeable buyer can take
   apart.
4. **Does the first real org readout go public on `/xray/`,** or stay internal?
5. **Does this jump the open Zoom blockers?** Larger than the last version
   implied, because §5 and §6 are build, not paperwork.

# Salesforce org inspection over MCP — a proposed work plan, under HOLD

> **STATUS: proposed work plan. HELD. This document authorises nothing.**
>
> It is not executable architecture, it does not authorise a scan of any
> Salesforce org, and no gate in §8 is passed. The architecture is what G1–G7
> produce; this says what those artefacts must contain and who accepts them.
>
> That wording is `chatgpt-codex-desktop-01a0839e`'s, from the fourth review of
> this file, and adopting it settles what I had been treating as a
> disagreement. Their eight blockers ask for executable architecture. They are
> right that this is not it — and right that it should not be read as it. It was
> never meant to be, and now says so at the top instead of on page nine.
>
> **The one thing that does not wait is G8**: the live `/xray/` and homepage
> six-sigma wording. That corrects a claim a visitor can read today, so it is
> scheduled ahead of everything else here and is not gated on any of it.

```status
state: HOLD
authorises: nothing
execution: forbidden
lifted_by: human:salam
```

*The block above is not decoration. `tests/test_mcp_gate_semantics.py` reads it,
and while `state` is `HOLD` **every gate's `passed` field must be `no`** — so
the hold cannot be lifted by rewriting the prose around it, which is what the
eighth review demonstrated was possible.*




Asked for by Mr. Salam, 2026-09-09 05:26Z: *"work on salesforce integration and
improving overall architecture to inspect, build and model solutions, assess
issues and score metrics for six sigma levels. Can you plan on creating MCP
client for salesforce that can be plugged into any salesforce org instance for
clients?"*

**Fifth version.** The first said we would build a server. The second discovered
Salesforce already ship two and said we would build a client. `chatgpt-codex-desktop-01a0839e`
returned NO-GO on eight architectural points against `c492dd9`, and NO-GO again
on eight more against `2839641`. All accepted. Two of the first eight were facts
I had asserted without checking; I checked both against Salesforce's own
documentation and both were wrong in my favour, which is the direction that
matters. Two of the second eight were errors made *inside the corrections* — a
miscount in the section about writing things down precisely, and an over-claim
about capability in the paragraph withdrawing an over-claim.

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
- **Six sigma is a candidate only where there is a real process**, and the last
  version over-claimed here too. It said failed flow interviews per million
  interviews over thirty days "**is** a DPMO" and that a section built on it
  would carry sigma "because it has earned it." A defect **rate** is not
  capability, and I reached that conclusion inside the very paragraph correcting
  an over-claim.

  A rate over a window becomes capability only with all of: a **homogeneous
  unit**, evidence the process is **stable** over that window rather than
  drifting or shifting, a **sample** large enough for the rate to mean anything,
  **stratification** where the population is not one population (three release
  trains are not one process), a stated **uncertainty interval**, an explicit
  **transform** from rate to sigma, and a declared **shift convention** rather
  than an unexamined 1.5σ. None of those is established for any org signal
  today.

  So: the product ships a **structural audit** with declared rates now, and a
  **process** section that reports rates *as rates* with their windows and
  intervals. Sigma appears only after the seven conditions above are
  demonstrated for a specific metric, in writing, per metric. That is a weaker
  claim than the last version made and a stronger one than the page makes.

**This reaches further than this document, and it is not optional.** `/xray/`
presents the current model live and the homepage copy written today says "four
pillars, a number against each". A live page presenting a pooled figure as a
sigma level is making a claim that does not hold, so correcting it is a required
follow-on, not a decision to be weighed. What is Mr. Salam's is the *wording*
and the timing, not whether an unsound claim stays up.

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
not match what the live findings show.

**A correction first: the last version said "seven things" and then listed
nine.** A miscount in a section about writing things down precisely, and the
second time in two days I have stated a count from memory instead of counting —
the Zoom blockers were "five" when they were six. The list below is 17, and that number comes from counting the rows
rather than from me.

A signal is not a metric until all 17 are written down. The catalogue is a
deliverable; this is its schema and one worked row.

| field | meaning |
|---|---|
| `id` | stable identifier, quoted in output |
| `pillar` | SECURITY / OPERABILITY / WASTE / REDUNDANCY, or PROCESS |
| `unit` | the thing being counted — the denominator's member |
| `numerator` | what is counted as present, stated separately from what makes it a defect |
| `denominator` | the opportunity set, exactly |
| `defect` | the rule that turns a counted thing into a defect |
| `formula` | how numerator and denominator combine into the reported figure |
| `direction` | whether higher is better or worse — never left to the reader |
| `threshold` | the value at which it becomes a finding, and where that value came from |
| `exclusions` | what is deliberately not counted, and why |
| `completeness` | how much of the denominator the query actually reached |
| `query` | the SOQL / Tooling / metadata call, verbatim |
| `window` | the period, or `static` for structure |
| `permission` | what the scanning identity must hold to see it |
| `unknown` | the closed set of non-numeric outcomes — `insufficient-permission`, `query-failed`, `not-reached`, `no-data-in-window` — never silently zero, and never merged with a real value |
| `cost` | API calls consumed, so a scan's spend is attributable per metric |
| `privacy` | public / aggregate-only / private |

Worked example. **The last version of this example filled in nine of the
seventeen and used an `unknown` value that was not in the closed set** — a
worked example that does not satisfy its own schema teaches the wrong thing
twice. All seventeen, with the unresolved ones marked as unresolved rather than
omitted:

| field | value |
|---|---|
| `id` | `OPS-FLOW-ERROR-RATE` |
| `pillar` | PROCESS |
| `unit` | one flow interview |
| `numerator` | interviews whose final status is Error |
| `denominator` | interviews *started* in the window, including those still running at its close |
| `defect` | final status Error. A cancelled or paused interview is not a defect |
| `formula` | `numerator / denominator`, reported as a rate per 10⁶ with its interval — **not** converted to sigma until §4's seven conditions are met for this metric |
| `direction` | lower is better |
| `threshold` | UNRESOLVED — no defensible value yet; must come from observed distribution across ≥3 orgs, not from a round number |
| `exclusions` | interviews from the scanning identity's own actions; screen flows abandoned by a user, which are not automation failures |
| `completeness` | interviews the query reached ÷ interviews the org reports started; below 0.95 the metric emits `not-reached` rather than a rate |
| `query` | UNRESOLVED — `FlowInterview` versus Event Log Files, decided by the §8 spike |
| `window` | trailing 30 days |
| `permission` | View All Data, or View Event Log Files for the ELF path |
| `unknown` | one of `insufficient-permission`, `query-failed`, `not-reached`, `no-data-in-window` — never a rate of 0 |
| `cost` | UNRESOLVED — measured in the spike; ELF and SOQL paths differ by roughly an order of magnitude |
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

## 8. The ordered gates before a client org is touched

**These were four-column table rows until 2026-09-09, and my own test was the
thing keeping them that way.** `chatgpt-codex-desktop-01a0839e` asked across
three rounds for prerequisite, independent acceptor, evidence, fail-closed
behaviour and unlock. I argued those belonged in the artefacts the gates
produce — while a test I had written asserted `len(cells) == 4`, which
*actively prevented anyone adding them.* I could not honestly hold a position
that my own guard was enforcing into the codebase. The columns are here now.

Each gate below states seven things in prose. A gate that cannot be failed, or
whose failure has no defined consequence, is not a gate.

**Each also carries a `gate` block of 10 typed fields, and that block is what
the tests read.** It exists because the eighth review of this file ran **eight
semantic inversions of the plan and every one left the suite green** — the
document made to say G8 did not unlock G9, that an unknown-cost scan could
start, that G9's authorisation need not name a scope or an expiry. The tests
were matching substrings, and a substring cannot carry a negation:
`assertIn("G9", line)` is satisfied by a line saying G9 is *not* unlocked.

The typed fields have a closed grammar, so an inversion either parses to a
different value and breaks the graph, or fails to parse at all. `requires` and
`unlocks` are checked for **reciprocity in both directions**, and the set of
gates that must precede G9 is **computed from the graph** rather than read from
the sentence that claims it.

**Nothing in G1–G7 requires a client org.** That is the point: everything that
can be wrong is wrong before a client is exposed to it.

---

### G1 · capability spike

```gate
id: G1
requires: none
unlocks: G2, G3, G5
owner: claude-code-cli
acceptor: agent:independent
human_precondition: yes
fail_closed: yes
fail_closed_rule: refuse_unconfirmed_claim
evidence_must_name: named_authoriser, de_org_identifier, authorisation_scope, authorisation_expiry, tool_listing, version_output, commit_hash, no_connect_failure_mode
passed: no
```

- **prerequisite** — **none**: no gate precedes G1. What it does need is a
  Developer Edition org we control **and a separate, human authorisation to
  read it** (see below) — a real-world precondition, which is why it is
  carried as `human_precondition` above rather than as a gate edge. Note which section is
  provisional: **§0 is the verified part**, checked against Salesforce's own
  documentation with sources, and it is not in question here. **§1's
  capability table is the unqualified one** and is what this gate confirms
  or strikes. The previous version had these the wrong way round.
- **artefact** — `docs/mcp-capability-matrix.md`: exact server ids, endpoints,
  version and maturity for Hosted and DX, **measured, not quoted**.
- **owner** — claude-code-cli.
- **independent acceptor** — any agent that did not write it.
- **evidence** — the raw tool listing and version output from both servers,
  committed, with the commit hash recorded — **and the provenance of the
  authorisation that produced them**: who authorised it by name, which
  Developer Edition org by identifier, how far the authorisation reaches, and
  when it lapses. A receipt that proves a connection happened but not *whose
  permission it happened under* is a receipt for the wrong thing.
  **The receipt is immutable**: it is the committed output at a recorded hash,
  not a summary written afterwards.
- **evidence, when it does not connect** — a failure is a result and is
  recorded as one. `no_connect_failure_mode` names what was attempted, what the
  org returned verbatim, and which of the two servers it was. This is not
  hypothetical: the first contact with the DE org on 2026-09-09 returned
  `invalid_grant / no client credentials user enabled`, and a **wrong secret
  returns the identical error**, so that attempt confirms the org recognises
  the connected app and confirms nothing about the secret. A gate that only
  has a shape for success quietly promotes that to a pass.
- **fail-closed** — if a claim in §1's table cannot be confirmed with a version,
  it is **struck from the table**, not softened.
- **unlocks** — G2, G3, G5. G5's field inventory comes out of the same
  spike, and pretending otherwise left G5 with no stated source.

### G2 · quota and spend

```gate
id: G2
requires: G1
unlocks: G9
owner: claude-code-cli
acceptor: agent:independent
human_precondition: no
fail_closed: yes
fail_closed_rule: refuse_when_cost_unknown
evidence_must_name: scan_envelope, limits_before, limits_after, paging_budget, retry_budget
passed: no
```

- **prerequisite** — G1 accepted.
- **artefact** — folded into G1's matrix: measured API cost of a scan of a
  **named, enumerated object set fixed at G1 time** — `scan_envelope`. "A full
  scan" was undefined at this point in the order, because what a scan covers is
  not settled until G5's schemas and G6's catalogue exist; a cost measured
  against an undefined envelope is a number with no unit. The envelope is
  stated explicitly here, and **when G5 or G6 changes it, G2's measurement is
  re-taken** rather than inherited.
- **owner** — claude-code-cli. **acceptor** — as G1.
- **evidence** — before/after `/limits` readings around a full scan, or a
  written demonstration that `/limits` is unreachable over MCP.
- **fail-closed** — a scan does not start when it *would exceed* the
  configured share of remaining quota **or when its cost is not yet known**.
  Unknown is not permission: the previous wording blocked only the measured
  excess, so an unmeasured scan ran. Paging and retry budgets are stated as
  numbers or the gate fails.
- **unlocks** — G9's spend argument.

### G3 · enforcement policy

```gate
id: G3
requires: G1
unlocks: G4
owner: claude-code-cli
acceptor: agent:independent
human_precondition: no
fail_closed: yes
fail_closed_rule: deny_by_default
evidence_must_name: mutating_tool_unreachable_test, unexpected_org_id_test
passed: no
```

- **prerequisite** — G1's tool listing.
- **artefact** — `docs/mcp-enforcement-policy.md`: the literal tool allowlist,
  the org allowlist rule, the telemetry position, the non-GA rule.
- **owner** — claude-code-cli. **acceptor** — an agent that did not write it.
- **evidence** — a test that fails when any name in §0's mutating list is
  reachable, and a test that fails when a response carries an unexpected org id.
- **fail-closed** — **deny by default**: a tool not on the allowlist is refused
  even if the server offers it. **If the per-response org id proves
  unobtainable, G3 fails and the DX path is abandoned** — load-bearing, not
  decorative.
- **unlocks** — G4.

### G4 · auth and tenancy

```gate
id: G4
requires: G3
unlocks: G9
owner: claude-code-cli
acceptor: human:salam-or-reviewer
human_precondition: no
fail_closed: yes
fail_closed_rule: block_until_demonstrated
evidence_must_name: attended_vs_unattended, eca_constraints, token_storage, token_rotation, revocation, tenant_isolation
passed: no
```

- **prerequisite** — G3's org boundary.
- **artefact** — `docs/mcp-auth-lifecycle.md`, answering **six** concerns:
  attended vs unattended authorisation, ECA constraints, token storage, token
  rotation, revocation, per-tenant isolation. *(The previous version said
  "each of the five" while listing six — a count stated from memory, the fourth
  such error in two days. It now says six because they were counted.)*
- **owner** — claude-code-cli. **acceptor** — Mr. Salam or a reviewer; **this
  one does not pass on my say-so**, because it is the gate that decides where a
  client's credentials live.
- **evidence** — an executable demonstration per concern, not prose: a token
  refresh, a revocation taking effect, a second tenant's scan failing to see
  the first's data.
- **fail-closed** — any concern without a demonstration blocks G9.
- **unlocks** — G9.

### G5 · schemas

```gate
id: G5
requires: G1
unlocks: G6, G7
owner: claude-code-cli
acceptor: agent:independent
human_precondition: no
fail_closed: yes
fail_closed_rule: refuse_unvalidated_emitter
evidence_must_name: per_field_mutation, unexpected_field_mutation, pinned_corpus_digest
passed: no
```

- **prerequisite** — G1's field inventory.
- **artefact** — `xray-score-v1` schema, validator, and a fixture corpus with a
  **pinned digest**; plus versioned collection-manifest, private-facts and
  public-export schemas.
- **owner** — claude-code-cli. **acceptor** — an agent that did not write it.
- **evidence** — the validator rejects a corpus mutated in **each** field, the
  way `tests/mutate_positioning.cjs` does for the site, including a mutation
  that adds an **unexpected field** rather than only removing known ones.
- **fail-closed** — an emitter that cannot validate does not publish.
- **unlocks** — G6, G7. The sanitizer needs the public-export schema
  directly, not only by way of the catalogue.

### G6 · catalogue and statistics

```gate
id: G6
requires: G5
unlocks: G7
owner: claude-code-cli
acceptor: human:salam-or-reviewer
human_precondition: no
fail_closed: yes
fail_closed_rule: report_rate_not_sigma
evidence_must_name: seventeen_fields_no_unresolved, stability, sample, stratification, uncertainty
passed: no
```

- **prerequisite** — G5's schemas.
- **artefact** — the 17-field catalogue, and for any metric proposed to carry
  sigma, a written demonstration of §4's seven conditions.
- **owner** — claude-code-cli proposes. **acceptor** — **Mr. Salam or a
  reviewer**; I should not be the one who decides my own model is sound.
- **evidence** — every metric carries all 17 fields with no `UNRESOLVED`; each
  sigma claim carries its stability, sample, stratification and uncertainty
  working.
- **fail-closed** — a metric without its demonstration reports a **rate**, not
  a sigma. It is not dropped and it is not promoted.
- **unlocks** — G7.

### G7 · sanitizer

```gate
id: G7
requires: G5, G6
unlocks: G9
owner: claude-code-cli
acceptor: human:salam
human_precondition: no
fail_closed: yes
fail_closed_rule: refuse_without_approval
evidence_must_name: stable_export_digest, leakage_mutation, approved_exact_bytes
passed: no
```

- **prerequisite** — G5, G6 — the public-export schema and the catalogue.
  A sanitizer cannot know what to redact until the fields are defined.
- **artefact** — `docs/mcp-sanitizer.md` and the tool.
- **owner** — claude-code-cli builds. **acceptor** — **Mr. Salam approves the
  exact bytes** of anything that becomes public.
- **evidence** — a fixture of private facts produces a public export with a
  **stable digest**, plus a leakage mutation: an org name injected into a
  finding must not survive to the export.
- **fail-closed** — **no approval, no publication.** Absence of approval is not
  permission.
- **unlocks** — G9.

### G8 · live correction — *out of order, and first*

```gate
id: G8
requires: none
unlocks: G9
owner: claude-code-cli
acceptor: human:salam
human_precondition: no
fail_closed: yes
fail_closed_rule: refuse_until_live_readback_matches
evidence_must_name: merge_commit, deployment_run_id, live_page_readback
passed: no
```

- **prerequisite** — none. This is why it is out of order.
- **artefact** — a source PR against `sfdc24-site` correcting the `/xray/` and
  homepage six-sigma wording per §4.
- **owner** — claude-code-cli. **acceptor** — Mr. Salam chooses the wording;
  a reviewer accepts the change.
- **evidence** — the merge commit, the deployment run id, and a **read-back of
  the live page body** showing the corrected wording and no forbidden claim.
- **fail-closed** — if the live read-back still shows the old wording, the gate
  is not passed however green CI was.
- **unlocks** — G9.

*G8 has no prerequisites and one dependent, and that asymmetry is the point:
it corrects a claim a visitor can read today, so nothing may hold it up. An
earlier version wrote "it gates nothing and nothing gates it", which
contradicted G9's own prerequisite list two blocks below.*

### G9 · first client org

```gate
id: G9
requires: G2, G4, G7, G8
unlocks: none
owner: none
acceptor: human:salam
human_precondition: yes
fail_closed: yes
fail_closed_rule: refuse_without_authorisation
evidence_must_name: human_channel_row, org_identifier, scope, expiry
passed: no
```

- **prerequisite** — G2, G4, G7, G8 **directly**, and therefore G1, G3, G5
  and G6 **transitively** — the whole of G1–G8, with nothing reaching G9 by
  another route. "Mostly done" cannot pass for done because the test
  **computes that closure from the typed blocks** instead of trusting this
  sentence, which is what an earlier version asked you to do.
- **artefact** — none. This is an authorisation, not a document.
- **owner** — n/a. **acceptor** — **Mr. Salam authorises.** A person, in the
  open. Not an agent, and not this document.
- **evidence** — a board row **from Mr. Salam's own channel**, naming the
  **exact org identifier** to be read, its scope, and an expiry. An agent's
  note that it is fine is not evidence, and neither is an authorisation
  that does not say which org.
- **fail-closed** — no authorisation, no scan. Silence is not a yes.
- **unlocks** — none. G9 is terminal: what it opens is the first scan of an
  org we do not own, and that is an authorisation, not another gate.

---

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
3. **How and when the live site's six-sigma wording is corrected**, per §4.
   *Whether* is not a decision — a page presenting a pooled figure as a sigma
   level is making a claim that does not hold. The wording and the timing
   relative to launch are yours.
4. **Does the first real org readout go public on `/xray/`,** or stay internal?
5. **Does this jump the open Zoom blockers?** Larger than the last version
   implied, because §5 and §6 are build, not paperwork.

---

## What this document is, and where it stops

A reviewer can keep asking a plan for more specification, and each ask can be
individually reasonable while the document is the wrong place for the answer.
So, plainly:

**This is a plan. It commits to producing four specifications and gates client
work behind them** — the tool/org enforcement policy (§3), the
`xray-score-v1` schema with validator and corpus (§5), the metric catalogue
(§6), and the sanitizer (§9). Each is a separate artefact with its own review.

**What belongs in those artefacts and not here:** an exact versioned capability
matrix per server release, the literal enforceable allowlist and telemetry
policy, the attended/unattended auth and token-lifecycle design, the quota
reserve/retry/paging budget, and the formal versioned schemas for collection
manifest, private facts, internal score and public export. Naming them here as
required is the plan doing its job; writing them here would produce a document
nobody can review and a specification nobody can version.

**Settled, rather than argued.** The fourth review named this "a useful
proposed work plan under HOLD, not executable architecture and no client-org
scan authorization." That is exactly right, and it is now the status line at the
top of the file. The eight architecture blockers are requirements on G1-G7's
artefacts, which is where they were always going; what was missing was this
document saying plainly what it is, so a reader could not mistake it for the
architecture. It said so on page nine and now says so first.

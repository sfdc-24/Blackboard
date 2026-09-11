# Client-demo readiness — workplan to Wednesday 2026-09-16

Directive from Mr. Salam, 2026-09-11: clean up ways of working, projects and
activities so clients/prospects can try **blackboarding** — during demo calls,
in meetings, or on their own — without muddying the water we work in. Status
updates every few hours in his format
(`<Workstream> - <NN>% / Capability built - ... / Pending - ...`).

This is the organizing plan. It rides the same branch as
`BUS-SECURITY-HARDENING.md` because the two are one push: **you cannot invite
outsiders onto the bus before per-tenant isolation exists**, and that is the
security roadmap's H2 by another name.

---

## 1. The no-muddy-water principle (the design constraint everything obeys)

**Clients never touch the working board. Ever.** The internal `Blackboard -
Alpha DB` carries operations, strategy, credentials discussions, and agent
coordination. It is the kitchen; clients eat in the dining room.

Mechanism, not discipline: a **demo tenant** is a separate bus instance with
its own store, its own secret(s), its own front page, and a **reset switch**
that restores a seeded state in seconds. `src/bus_server.py` makes this cheap
by design — single file, stdlib-only, SQLite store — so a tenant is one
systemd unit + one DB file + one Caddy route:

```
bus.sfdc24.com          -> internal (migration target, per H2 keys)
demo.sfdc24.com/<slug>  -> per-client demo tenants (disposable, seeded)
```

- **Seed script**: creates the tenant DB from a fixture board that tells a
  story (a small project mid-flight: DISPATCH → CLAIM → PROGRESS → FINDING →
  RESULT), so a prospect sees coordination happening, not an empty sheet.
- **Reset switch**: drop DB, re-run seed. A demo can be burned and relit
  between two calls.
- **Demo agents**: one or two fleet agents subscribe to demo tenants with
  demo-scoped keys only. An agent holding a demo key **cannot** write to the
  internal board with it (scopes per key — H2 item 2).
- The internal board's secret never enters a demo context, a client browser,
  or a screen-share.

## 2. The bus that governs busses (SPINE — control plane)

Mr. Salam's instinct scales the same way clouds do: many tenant busses, one
**control plane** that provisions, watches, and audits them but never carries
tenant traffic itself. Phased so Wednesday doesn't wait on architecture:

- **Now (by Wed):** the control plane is a *registry + a provisioning script*
  — `tenants.json` on the VM (slug, port, key generation, created, state) and
  `provision_tenant.sh` / `reset_tenant.sh`. Governor-only. That is enough to
  run five clean demos in parallel.
- **Next:** the registry becomes a bus itself (the governor bus): tenant
  lifecycle events, key issuance/revocation, aggregated health, cross-tenant
  audit roll-up. This is where the hardening roadmap's H3 hash-chained ledger
  naturally lands first — the *governor's* ledger is the audit trail we sell.
- **Salesforce exploration (his call-out):** the tenant registry's system of
  record belongs in Salesforce — tenants as Accounts, demo sessions as
  activities, provisioning via Flow calling the control plane. That is
  dogfooding: the demo we show *is* a Salesforce-governed multi-agent system,
  which is the practice we sell. Salesforce's own **Hosted MCP Server** (GA
  Apr 2026, Enterprise+) exposes org flows/Apex actions to MCP clients —
  worth exploring as the bridge in both directions. Filed as exploration, not
  on the Wednesday path.

## 3. Workstreams and baseline (percentages are baselines, expected to move)

### W1 — Demo tenancy & provisioning — 25%
- **Built:** self-hosted bus server + Debian packaging + hardened unit + GCP
  runbook (`DEPLOY-GCP.md`); import/export CLI for seeding from dumps.
- **Pending:** GCP VM provisioned; multi-instance layout (per-tenant port/DB/
  unit template); seed fixture board; reset script; Caddy routes; the
  registry. **Needs from Mr. Salam: approval to create the GCP VM (cost).**

### W2 — Security / isolation — 20%
- **Built:** roadmap approved-pending-review (PR #75); rotation window
  tooling; hardened unit; constant-time auth.
- **Pending:** H1 rotation executed; **per-tenant/per-agent keys with scopes
  (H2) — the demo gate**; auth-failure alarming. Sequenced so demo tenants
  get keyed auth first (new code path, no migration risk to the live board).

### W3 — Client-facing front end — 40%
- **Built:** sfdc24.com live on GitHub Pages (moved off Google Sites 09-04;
  the "Sites is obsolete" concern is already answered for the public site);
  voice page live; reception chat with caps + quarantine.
- **Pending:** a per-tenant demo page (board view + post box against the
  tenant bus — `web/console.html` and `site/` pages are the starting
  material); demo landing explaining what the prospect is seeing.
- **CMS question (his ask):** researched, recommendation in §5 — short
  version: **no CMS adoption before Wednesday; WordPress not recommended as
  default; decide after first client feedback.**

### W4 — Zoom / meetings — 40% (of what a client call needs)
- **Built (PR #40, mergeable, CI green at its head, 16 review rounds):**
  wake-word live assistance mid-call — transcript excerpt → SFDC24
  `reception()` (same persona, caps, quarantined logging) → answer to the
  consultant's phone via WhatsApp; app-credential event WebSocket (no user
  token needed at cold start); honest delivery accounting; transcript
  logging off by default.
- **Held on purpose:** post-call wrap-up (`WRAP_UP_ENABLED=false`) until the
  backend exposes a trusted summarization route — the reception persona
  rightly refuses embedded commands.
- **Pending:** merge decision on PR #40 (held per the PM ruling; it blocks
  the consultation-demo issue #42); **Zoom Developer Pack credits (money —
  Mr. Salam)**; `ZOOM_WS_ENDPOINT` from Marketplace; a Linux host
  (`@zoom/rtms` is native, refuses Windows — the demo VM can double for it);
  first live end-to-end call; client-consent line in the demo script ("this
  call is AI-assisted"). Transcript lines only arrive when Zoom
  captions/transcription are active on the authorizing host account.
- **Honest capability line for demos (survey §7):** today the agent
  **listens and assists** — it cannot *present*. Plan calls accordingly: a
  human (or the phone voice page) presents; the agent's visible magic is
  answering wake-word questions from live call context.

### W5 — Salesforce admin/dev, headless ("360") — 15%
- **Built:** CLI-session auth pattern (acatia POC); org deploy manifests;
  permission-set discipline.
- **Direction change from research:** Salesforce ships an **official DX MCP
  server** (`salesforcecli/mcp`, 60+ tools: deploys, scratch orgs, tests,
  org auth). Adopt it as the base; build custom MCP tools only for the gaps
  (org-config admin actions, our conventions). That converts "build a custom
  MCP server" into "wire + extend", which is days not weeks.
- **Pending:** DX MCP wired to a dev org from a fleet host; scripted
  scratch-org spin-up as a demo beat ("watch the agent build in Salesforce
  live"); guardrails (which org, which permset, never prod).

### W6 — Knowledge & learnings (llm-wiki) — 30%
- **Built:** llm-wiki v0.24.4 installed (laptop), `archive`/`checkpoint`/
  `ingest`/`librarian`; the fleet's L-numbers and doctrine live in memory +
  docs.
- **Pending:** ingestion pipeline from board FINDING/RESULT rows into the
  wiki so learnings survive session churn; fix the known vault issue (Drive
  `.gdoc` pointers index as empty — point at `Claude Archive/` real
  markdown); a curated public-safe subset for demo storytelling.

### W7 — Ways-of-working cleanup — 35%
- **Built:** strong doc discipline (HANDOVER, ONBOARDING, POKA-YOKE, BCB-1
  grammar + lint); evidence culture.
- **Pending:** burn down the 8-PR open queue to a clean main (ORDER Linux
  lane is converging; #70 verified GO on Linux); **GitHub Actions billing
  unblocked (Mr. Salam — runs die in ~5s account-wide)**; retire stale
  branches; refresh ONBOARDING for demo-tenant world; consolidate the
  duplicate site copies story (repo `site/` is reference-only — already
  documented, enforce in demo materials).

## 4. Research questions to the fleet (dispatch when bus reachable)

Three DISPATCH rows, one per question, answers as RESULT rows tagged
`DEMO-READY-RQ1..3`; synthesis lands back in this doc:

1. **RQ1 — workflow optimization:** where does your loop lose the most time
   on the current bus (read patterns, duplicate-write discipline, stale
   reads, PR round-trips)? Name the top two with measurements if you have
   them.
2. **RQ2 — speed with quality:** what would let you execute faster *without*
   losing the evidence/mutation-test discipline — and what learning-capture
   step should become automatic rather than manual?
3. **RQ3 — capability expansion:** what one tool/system/process would most
   expand what blackboarding can do for a paying client (dynamic to client
   needs), and what's the smallest demo-able slice of it?

(Until egress to `script.google.com` opens from this environment, these can
be posted by any credentialed instance — payloads above are the contract.)

## 5. CMS decision (researched 2026-09-11)

**Recommendation: adopt no CMS before Wednesday. Default to what's live.**

- The public site already left Google Sites for GitHub Pages — fast, free,
  reviewed-by-PR, zero patch surface. The demo needs *tenant pages served by
  the bus VM* (Caddy static + JSONP/API to the tenant bus), not a CMS.
- **WordPress** (Mr. Salam's suggestion): viable open-source on our Linux
  instance and unmatched for non-technical editing (~63% market share), but
  it drags a PHP/MySQL patch treadmill — Patchstack recorded ~8k WordPress
  vulnerabilities in 2024, +68% YoY, mostly plugins — directly against the
  hardening roadmap we just wrote. Choose it only if non-technical content
  editing becomes the real bottleneck, and then prefer managed hosting over
  our VM.
- **If/when a CMS is warranted:** self-hosted headless — **Strapi** (leading
  open-source, self-hostable) or **Payload** (TypeScript-first) — fits our
  ops posture better; content via API into the same tenant pages.
- Decision point: revisit after the first three prospect demos, with actual
  content-editing pain as the evidence.

Sources: [InMotion — Best CMS 2026](https://www.inmotionhosting.com/blog/best-cms-platforms-2026/),
[Naturaily — WordPress alternatives 2026](https://naturaily.com/blog/best-alternatives-to-wordpress),
[UnfoldCMS — self-hosted platforms 2026](https://unfoldcms.com/blog/self-hosted-cms-platforms-developer-guide-2026),
[Salesforce — DX MCP server](https://developer.salesforce.com/blogs/2025/06/level-up-your-developer-tools-with-salesforce-dx-mcp),
[Salesforce — MCP support across Salesforce](https://developer.salesforce.com/blogs/2025/06/introducing-mcp-support-across-salesforce).

## 6. Timeline to Wednesday

| Day | Deliverable |
|---|---|
| Thu 9/11 | This plan; status cadence armed; PR #70 verified; unblock list to Mr. Salam |
| Fri 9/12 | PR queue triage to mergeable set; tenant provisioning scripts drafted; RQ1–3 dispatched to fleet |
| Sat–Sun 9/13–14 | GCP VM (on approval) + first demo tenant live with keyed auth; seed fixture; demo page v1 |
| Mon 9/15 | End-to-end dry run: provision → demo → reset; Zoom live validation if credits; fleet RQ synthesis |
| Tue 9/16 eve | Demo script + consent line; second dry run with a cold audience (an agent playing prospect) |
| **Wed 9/17** | **Client-ready: invite-able demo tenants + rehearsed call flow** |

*(Note: "next week Wednesday" read as 2026-09-16/17 boundary — plan targets
Tue night ready, Wed buffer.)*

## 7. Survey addendum — meeting-agent & screenshare (surveyed 2026-09-11)

**`sfdc-24/sfdc24-meeting-agent` is an empty repository.** Created
2026-08-24, zero commits, zero refs on the remote — a reserved name, not a
codebase. The only meeting agent that exists is the Zoom agent on Blackboard
PR #40 (`claude-code-cli/zoom-agent-live`, not yet on `main`). Decision
needed: populate that repo as the Zoom agent's future home post-merge, or
archive the name.

**Zoom agent capability truth-table** (verified in PR #40 code, head
`353dc8e`):

| Capability | State |
|---|---|
| Receive meeting events, join live media stream (RTMS) | BUILT |
| Listen / transcribe (Zoom's transcript stream) | BUILT |
| Wake-word Q&A from live context → consultant's phone (WhatsApp) | BUILT |
| Board write with read-back verification | BUILT |
| Post-call summary | BUILT but **shipped disabled** (needs trusted backend summarization route) |
| Screenshare **ingest** | PARTIAL — frames counted, nothing decoded |
| In-meeting chat | read-only |
| Create/schedule/invite to a meeting | **MISSING** — no code path |
| Appear as a participant | **MISSING** — RTMS is a server-side subscription; and Zoom's Linux Meeting SDK excludes bot participants |
| Speak into the call / post chat / present a screen | **MISSING** — only outbound channel is WhatsApp |
| Any live-path execution (Zoom, Graph, Apps Script, bus) | **NEVER exercised** — all 51 tests offline |

**Consequence for "improving zoom voice + screensharing":** voice *into*
calls and screenshare *out* are new capability lanes, not improvements —
RTMS cannot do them, and Zoom's Linux SDK excludes bot participants. Realistic
routes to scope after Wednesday: (a) present from a browser-automation
participant (its own project), or (b) demo shape that plays to what's built:
human presents, agent listens and answers — which is also the more credible
sell ("your consultant, augmented", not "a bot runs your meeting").

## 8. Unblock queue (each surfaced one at a time, per L-86)

1. **GitHub Actions billing** — every CI run account-wide dies in ~5s; the
   whole PR queue is hand-verified until this is fixed.
2. **Network egress for this cloud environment** — allow `script.google.com`
   + `script.googleusercontent.com` (board access), and Meta Graph API
   domain when WhatsApp goes live.
3. **WhatsApp credentials** (`WA_TOKEN`/`META_TOKEN`, `WA_PHONE_NUMBER_ID`,
   `WA_TO`) for the status cadence — until then, updates arrive as push
   notifications + session messages. Note Meta's 24-hour session window
   (error 131047): messaging the business number once a day keeps the
   free-form window open; otherwise updates need an approved template.
4. **GCP VM approval** for the demo bus host (also the Linux host PR #40
   needs).
5. **Zoom Developer Pack credits** — without them the Zoom agent authorizes
   but never receives a stream.
6. **PR #40 merge ruling** — mergeable and green; held only by the active-
   session conflict ruling.

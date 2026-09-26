# DOC-REGISTER — which document to trust for what

Companion to [`EXPRESS.md`](EXPRESS.md). This register classifies every
ways-of-working document across the three places they live. It was built on
2026-09-26 from three read-only inventories: Blackboard `origin/main` at
`d2256e0`, sfdc24-site `origin/main` at `dee1139`, and the Drive folder
"SFDC 24 - Claude". Line numbers are as of those commits; a file that now
carries a status banner is shifted down by two lines.

**What the statuses mean**

- **CANON**: authoritative. EXPRESS points here for detail.
- **REFERENCE**: accurate and useful, but not a source of rules.
- **STALE**: mostly right; the listed lines are wrong.
- **SUPERSEDED**: replaced by the named document. Do not follow it.
- **OBSOLETE**: describes a world that no longer exists: Azure, Foundry, the laptop lane, Google Sites.
- **HISTORICAL**: a dated record. Keep it, never follow it.

**Handling rules**

- Nothing is deleted.
- Drive documents are never renamed. The bus finds documents by exact title, so a rename silently breaks every writer.

## 1 · Blackboard repo (`docs/` unless noted)

| Document | Status | Notes |
|---|---|---|
| ADR-20260925-…-CONTROL-PLANE.md | CANON | The architecture and gates G1–G9. Its snapshot tables are dated (R5/R5b); read live state instead. |
| AGENCY-DOCTRINE.md | CANON | Decision rights (clause 4 lists six carve-outs) and evidence. Stale: the "Today:" notes that name the laptop `board_waker.py` (lines 61–63, 188–190) and the `inference_report.py` step (line 232), both laptop tasks. |
| PRODUCT.md | CANON | The four beats (sheet L-90). |
| POKA-YOKE.md | CANON | Repo lessons L-91 onward. Stale lines: 19 ("highest was L-90") and 34–42. L-91 is now enforced on main, and site #13 merged. |
| ACCEPTANCE-CHECKLIST.md | CANON | Client-gating checklist. |
| CLOUD-FLEET-RUNBOOK.md | CANON, STALE | What runs where. Stale lines: 50–66 and 76–77 (the r3 rollback chain), 141 (row count). Cloud Run traffic read 2026-09-26 11:55Z: `sfdc24-studio-controller-r6-d2256e0` at 100%, promoted 04:21Z (board row `CODEX-R6-D2256E0-PROMOTED-20260926T0421Z`); the ADR addendum (03:23Z) and the Converspan note predate that and name r5b. |
| CLOUD-CREDENTIAL-CONTRACT.md | CANON, STALE | Secret Manager and closed rotation. Stale lines: 123–127 and 382–385 (the laptop waker and `.env` still in use). |
| COMMS-PROTOCOL.md | CANON §2–§9, STALE | Identity, asking him, read-back, row grammar. Stale: BOOT as the state source (13–16); the vm-* roster (44–59, 196–214); the laptop outbox (440–450, a double-send hazard); the `claude`/`gemini` tag note (75–81). |
| board-protocol.md | CANON (bus I/O), STALE | Stale: the "Grok↔Claude" framing (1–4); line 136 names a script not on main; BOOT wake (167); a duplicated block (177–183). The `to=` delimiter at 124 disagrees with COMMS. |
| APPS-SCRIPT-DEPLOY.md, gas-bus/README.md | CANON | Apps Script deploys. The production path conflicts with CICD.md 12–14; resolve it in APPS-SCRIPT-DEPLOY. |
| DELIVERY-OPERATING-MODEL.md | STALE | M5–M8 still hold. Roles (4–6, 46–52) and "auto-merge is his" (81–97) are superseded by EXPRESS §2 and §7. |
| TRUE-NORTH.md | SUPERSEDED by EXPRESS | Its merge-rulings pointers (§6) are restated in EXPRESS §3. |
| ONBOARDING.md | SUPERSEDED by EXPRESS | §4 grammar and §5 rules still hold. Foundry, the BOOT wake and the ORDER profile are obsolete. |
| INTEGRATION-CHECKLIST.md | SUPERSEDED by EXPRESS | Line 60 ("decisions in GitHub Issues only") is wrong. |
| .github/copilot-instructions.md | CANON for Copilot, STALE | Copilot reads only this file. Stale: Azure/VM (11–15), secrets (80–83), merge bar (151–158), site PR #12 (203–206). Fix it in the same PR series as EXPRESS. |
| README.md | STALE | Frames the repo as the Ubuntu bus package. |
| CONVERSPAN-READINESS-20260926.md | REFERENCE (dated) | What may proceed before G9. |
| BUS-SECURITY-HARDENING.md | STALE | Its rotation cadence (65–70, 107–111, 201–202) contradicts the closed decision. |
| HANDOVER.md | HISTORICAL | Still cited as current by COMMS line 14 and copilot line 64; those citations are wrong. Its site-source-of-truth and single-Governor-deploy-source sections belong in APPS-SCRIPT-DEPLOY. |
| ARCHITECTURE-OVERVIEW-20260914.md, BLACKBOARD-REEVALUATION-20260915.md, GOVERNANCE-FOUR-ON-ONE-PAGE.md, PR-BACKLOG-TRIAGE-20260923.md, FLEET-EFFICIENCY.md, ISSUES.md, SALESFORCE-MCP-PLAN.md | HISTORICAL | ISSUES.md numbers (ISS-###) collide with the Drive Issue Journal (ISSUE ###). Cite with the source. |
| claude-cli-vs-console.md, CONSOLE-MIGRATION-READINESS.md, OPENAI-CLOUD-MIGRATION.md, waker.md | SUPERSEDED | By CLOUD-FLEET-RUNBOOK and CLOUD-CREDENTIAL-CONTRACT. |
| GCLOUD-MIGRATION.md, GCLOUD-LINUX-MIGRATION.md, FOUNDRY_WORKER_CONTRACT.md, ORDER-*.md, LINUX-READINESS-ORDER-SUITES.md, infra/azure/*, DEPLOY.md, DEPLOY-GCP.md | OBSOLETE | Azure, Foundry, the ORDER lane, the first bus. GCLOUD-LINUX line 59 carries a wrong `BUS_URL`. |
| CODEX-PM-QUESTIONS.md | OBSOLETE (hazard) | Lines 256–259 give a wrong `BUS_URL`; lines 278–282 say to edit the sheet by hand. |
| CICD.md, CICD-BUILD-IDENTITY.md | REFERENCE | CICD.md line 27 says v31; the Governor is at v49 or later. |
| CICD-GOVERNOR-V30/V31, CICD-STAGING-*, CODEX-CI-ACCEPTANCE.md, MULTITENANT-READINESS.md, SITE-P0-TTS-CANARY.md, governor_conversation.patch.md, consensus-attack-findings.md, DOMAINS-AND-HOSTING.md, SERVICENOW_SETUP.md, homepage-copy.md, website-copy.md, LIVE-CASE-DEMO-PATH.md | HISTORICAL | — |
| WA-OUTBOX.md, PIPEDREAM-CONNECT.md, PIPEDREAM-WA-PREFIX-GATE.md, WHATSAPP-GATEWAY-REPAIR.md | STALE | The WA_SEND grammar is current. Every instruction to arm or use the laptop outbox is wrong: the cloud wa-outbox is the only sender. |
| MIND-MAP.md, `_md_inventory.json`, `_search_doctrain_pokayoke.json`, blackboard-architecture.html, web/*.gs.txt | OBSOLETE | The `.gs.txt` files are stale copies of Apps Script code; do not read them as current. |
| blockers-page.md, lessons/, scripts/README-board-clients.md, cloud/studio-controller/README.md, ADR-001-VOICE-SWEEP-BACKSTOP.md, DEMO.md, METADATA-OPERATIONS.md, zoom-agent/README.md | REFERENCE | The studio-controller README's `/studio/` framing (lines 3, 316) predates the homepage-only rule. |
| consultations/, peer-notes/ | HISTORICAL | Records. |
| **Not in the repo:** Codex's `SFDC24-CODEX-STRATEGY-EXECUTION-PLAN-20260925.md` | CANON, **local only** | It exists only as an untracked file in the owner's checkout. Codex should publish it. |

## 2 · sfdc24-site repo

**Required checks, read from ruleset 23679990:**
- Changes go through a PR; `required_approving_review_count` is 0, and unattributed changes need an extra approval.
- Five strict required checks: `prototype-publisher-test / test`, `site-positioning-test / test`, `homepage-recovery-test / test`, `intake-contract / intake`, `xray-page-test / test`.
- No bypass, and Copilot reviews every push.
- `honesty-dom-test` is **not** required. The site's `copilot-instructions.md` (lines 159–162) lists it as required, and `site-doctrine.md` (line 9) says "six required checks".

| Document | Status | Notes |
|---|---|---|
| tests/capabilities.json, tests/site_positioning.cjs, tests/honesty.spec.cjs, tests/test_triage_no_names.py | CANON | What the site may claim, how it speaks, and no names. capabilities.json content is dated 09-09: 21 of its 26 homepage entries no longer appear on the page. |
| docs/PROTOTYPE-PUBLISHER.md | CANON | `/p/<uuid>/` prototypes and the exact-SHA Pages read-back. |
| studio/contract/README.md (+ events.schema.json) | CANON (the controller contract), STALE | Renderer rules 1–10 hold. Its `/studio/` framing is outdated, and the API table lacks `/talk`. |
| .github/copilot-instructions.md | CANON for Copilot, STALE | Stale: honesty-dom is not required (159–162); canvas #24 is not "no go" (203–204); P0s #1, #2 and #12 are closed (198–199). |
| docs/site-doctrine.md | STALE | The visitor-copy rules hold. Stale: "six required checks" (L9), Foundry (L7), SPEED unowned and Grok reserved (L106), "no weekday date" (L5, L31), "no sitemap" (L17), and the IP rule contradicting itself (L66 vs L110). |
| docs/STT-STREAM.md, docs/python-offload.md, tests/fixtures/README.md, data/next-release.json, data/site-manifest.json | REFERENCE | STT-STREAM lines 87–93 are stale: relayUrl is now set. The release rail must never sit expired. |
| docs/lessons-log.md | STALE (dormant) | The ETA logging stopped on 09-19. |
| docs/STAGING.md | STALE (dormant) | Line 33 names Foundry. |
| docs/claude-cli-vs-console.md | SUPERSEDED | The Blackboard copy is the one kept. |
| docs/AB-TEST-HOMEPAGE.md | OBSOLETE | The A/B is gone, and the variant headline is now banned. |
| docs/reviews/CODEX-REVIEW-001-2026-09-18.md, data/sfdc24_model_bench_expanded.md | HISTORICAL | S2 (replace-all) and S3 (`git()` ignores the exit code) are still in `tools/site_edit_router.py`; S1, the sitemap-versus-doctrine conflict, is unresolved. |

**Open site-side decisions:**
- `sitemap.xml` lists `/studio/`, and `/studio/` has no noindex, which conflicts with homepage-only.
- The noscript line "That reaches the same person" is an implied personal promise.

## 3 · Drive folder "SFDC 24 - Claude"

**Live and canonical:**
- `Blackboard - Alpha DB`: the board.
- `Blackboard - Alpha DB - ARCHIVE to 2026-09-18`: rows before 2026-09-19, verbatim, at the same row numbers.
- The dated architecture PDF of 2026-09-26.
- The 10-Hour Corrective Action order, until it is closed.
- The AkatiaVM retirement manifest.
- `SFDC24 — Inbox · claude-code-cli`, a live append target.

| Document | Status | Notes |
|---|---|---|
| SFDC24 — DOCTRINE (read first, every session) | CANON as an **id registry**, frozen | D-1 to D-36. Several are stale in substance: D-11–D-14 (the email sweep), D-33 (the ring and footer), and §4's date. |
| SFDC24 — LEARNINGS (Rules Sheet) | CANON as an **id registry**, frozen | L-0 to L-97 and the COLLAB rows. Add no new L-rows: the next one would collide with repo L-98. Stale in substance: L-30, L-58, L-69, L-70, and L-75 (superseded by L-86 but never marked). |
| SFDC24 — READ DOC (BOOT) | SUPERSEDED by EXPRESS | Stale since 09-03. |
| DOCTRINE V2; DOCTRINE · VENDOR ROUTING TABLE; READ ME FIRST; both Project Instructions; BOOT PACK; Continuity & Autonomy Protocol; TEAM OPERATING MODEL v1; Strategy, Doctrine & Execution Plan v1; VALIDATION PROTOCOL v1; MULTI-SURFACE EVENT BUS V3.1; LEDGER SCHEMA v1; ARCHITECTURE.md; ARCHITECTURE.STATUS-RELATED.md; WAKE-CARD-001; DIRECTION Web/Portal; CI/CD RUNBOOK; CONTEXT PACK | SUPERSEDED by EXPRESS | Between them they give five different authority maps and seven boot orders. DOCTRINE V2 reuses D-24–D-28 with different meanings. |
| LEARNINGS (Poka-Yoke Ledger) | SUPERSEDED | Six post-freeze learnings never reached the sheet: the host-naming rule, one owner step per message, WA-MEDIA audio, the bus `text` key, context budget, and a naming rule. Their live content is folded into EXPRESS. |
| BCB-1 benchmark | STALE | The row grammar holds (see EXPRESS §5); the channel registry is obsolete. |
| Dispatch (Work Queue) | HISTORICAL | ORDERs 001–049 are closed by supersession. The principles that survive are in EXPRESS. |
| LIVE QUEUE; Sign-In Register; check-in sheet; EMAIL DIGEST; HURDLE REGISTER | OBSOLETE | The Blockers page replaces the hurdle register. |
| Foundry Mission Inbox; SPEC FOUNDRY-WA-001; Board to Foundry Shipper runbooks; Bus v1.1 PATCH RUNBOOK | OBSOLETE | Foundry and Azure are out. |
| Sales Doctrine: Facts Only; Doctrine v2: Value First | **Owner review** | Commercial rules are his to confirm. The two conflict with each other on pricing. |
| SFDC24 — Issue Journal; Charter; PUDDING; Master Log; reports, results and handbacks | HISTORICAL | — |
| All SESSION STATE and Inbox docs except claude-code-cli | STALE or OBSOLETE | The grok-bot pair stays until Grok says it no longer uses them. |

**Credential-shaped values found in Drive documents.** No value is reproduced
here. Cleaning or rotating them is Mr. Salam's decision:
- "SFDC24 — Strategy": the bus URL and a per-vendor secret.
- "MULTI-SURFACE EVENT BUS … V3.1": an older bus secret.
- "SFDC24 — Dispatch (Work Queue)", ORDER 043: a Salesforce keystore password.

## 4 · Identifier collisions: always cite with the source

- **L-91 to L-97** mean different things in the Drive sheet and in the repo's POKA-YOKE.md. Write `sheet:L-9x` or `repo:L-9x`.
- **D-24 to D-28** differ between DOCTRINE and DOCTRINE V2. V2 is superseded.
- **ISS-###** (repo ISSUES.md) and **ISSUE ###** (Drive Issue Journal) are two separate ledgers.
- **"G1"** has four meanings:
  - an ADR release gate;
  - the Salesforce MCP plan gate (the Sep 9 Salesforce hold);
  - a BUS-SECURITY gap;
  - the Codex plan's "Gate 1", which is ADR G3 for #260.
- **PR numbers** always need their repository.

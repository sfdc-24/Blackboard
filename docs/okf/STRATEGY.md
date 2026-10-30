---
title: Full Strategy Pack — Codex architecture mapped
id: FULL-STRATEGY-BUILD-20260927
owner: grok (PM)
authority: Codex PDF SFDC24-Blackboard-Architecture-Current-Future-2026-09-26
status: living
updated_at: 2026-09-28T13:20:00Z
public_site_rule: never put the word OKF on www.sfdc24.com public pages
---

# Full strategy — implement Codex architecture (do not invent a parallel)

**Canon diagram:** Drive `1QX0wpWTuWRtsm5C2QKcmmv2r-_X3OgPq` — *SFDC24 - Blackboard Architecture - Current and Future State - 2026-09-26* (Codex).
**Local:** `/workspace/briefs/SFDC24-Blackboard-Architecture-Current-Future-2026-09-26-CODEX.pdf`
**Companion stitch (CONF-LINE overlay only):** `/workspace/briefs/conf-line-architecture-stitched.md` — LiveKit media plane *on top of* this motherboard; never a replacement.

This pack is HOW-WE-WORK–adjacent. The execution model is [HOW-WE-WORK](./HOW-WE-WORK.md). This pack and Cooking show the field. The bus carries doorbells and milestones. EXPRESS is how we behave.

## 1) Vision (from Codex FUTURE page)

| Pillar | Meaning | Status language |
|---|---|---|
| Motherboard command | Blackboard = registry, leases, policies, budgets, evidence, pause/kill | PARTIAL now → TARGET signed leases + kill ≤60s (G9) |
| Seamless present moment | Audio + dialogue cards + live prototype share **one turn timeline** | PARTIAL (R5b LIVE; builder/analyze 60s defects open) |
| Multi-agent, single commit | Claude builds; Gemini advises; **controller alone** validates and commits | PARTIAL |
| SFDC24 first | Converspan / client minibuses wait on foundation gates | HELD until G9 |

**Evidence vocabulary (Codex CURRENT):** LIVE · CURRENT · PARTIAL · NO-GO · DARK · HELD · TARGET. Never blur them.

## 2) Current operating picture (Codex CURRENT page — short)

- **User/channel surfaces:** sfdc24.com owner session LIVE · WhatsApp PARTIAL · Zoom+Ubuntu HELD · Salesforce observation PARTIAL/gated
- **Live browser session:** OpenAI Realtime PARTIAL · browser audio arbiter LIVE (#221 @ 7a2685a Codex GO) · Studio Controller R5b LIVE · OpenAI TTS PARTIAL
- **Bounded work:** TALK/RECAP PARTIAL · Claude builder PARTIAL · Analyst+Muse PARTIAL · ordered artifact+event ledger CURRENT
- **Durable:** Blackboard+Apps Script CURRENT · Pipedream PARTIAL · GCS CAS CURRENT · GitHub+public site LIVE

Held/dark (do not auto-promote): Zoom RTMS+Ubuntu presenter; Salesforce writes (BLK-059); Converspan; Nav/steelworkson (#260/#261 gates); charter+quote PDF (#269) DARK; Gemini advisor DARK.

## 3) Promotion order overnight / next (Codex FUTURE "Next promotions, in order")

1. **#272** repaired on a **fresh exact head**, independent acceptance, newly verified image — each step needs **Codex GO**. Do not promote r5d on a NO-GO head.
2. **Deploy, traffic, owner rehearsal** (targeted **G1** close) are **gated separately**.
3. Complete **G2** and the **#260** gate, then **#261**.
4. Repair/review **#269**; **CX1** charter-only canary (PDF+price effects off); **CX2** after owner's commercial gates. Traffic is a separate GO.
5. Then ADR acceptance matrix items that remain open (edition-2 PDF dropped blanket "Then ADR G5–G9" as a free phrase — promote by **named gate**, not slogan).

**Converspan launch (non-waivable):** ADR **G9** in full (includes G2 + own enrollment). Soak criteria set by Codex/owner.

## 4) Lanes (fleet) — living roles on HOW-WE-WORK

Delivery assignment refines on [HOW-WE-WORK](./HOW-WE-WORK.md) (`EXECUTION-MODEL-20260928`). This table matches that page. Codex remains quality and test lead. Promotion order in §3 is unchanged.

| Role | Owner | Notes |
|---|---|---|
| Delivery and strategy lead | **Grok Bot** | This pack; overnight GO triage; alignment on the project OKF |
| Quality and test lead | **Codex** (`chatgpt-codex-desktop`) | Exact-head GO/NO-GO; fleet tests; broker/ and docs/ quality |
| Data and security engineer | **Claude** (`claude-code-cli`) | Data handling and security engineering, with evidence |
| Heavy PM and Build and PR execution | **Cursor** | The build and the PR, including conference packages when that is the change |
| Admin and analyst | **Gemini** | Admin surfaces and analysis written back to the OKF |
| GitHub DevOps and repo reviewer | **Copilot Agents** | CI, repo review, Cooking / HOW-WE-WORK / session pack refresh via PR |
| Human decision / commercial / irreversible | **Mr. Salam** | His six; CX pricing; paid plans; live keys |

Conference folder ownership stays in `conference/docs/okf/lanes.md`. Blackboard lane map stays in `./lanes.md`. Role edits land on HOW-WE-WORK.

## 5) HITL — bake gates into the plan (not after)

Every gate needs five fields (see HOW-WE-WORK): **Who · What · When · How · Timeout**.

Standing gates from Codex PDF:

| Gate | Who | What | When | How | Timeout |
|---|---|---|---|---|---|
| Exact-head review | Codex (+ Cursor on Claude pkgs) | PR + head SHA | before merge | board GO/NO-GO | silence = hold |
| G1 continuous audio + live prototype | Codex + owner rehearsal | 10 min / 5 turns / 2 barge-ins | before traffic claim | board + evidence | hold |
| G2 provider reliability | Codex | deadlines, cancel, fallback, fences | before minibus promote | board | hold |
| G3/G4 workspaces | Codex | #260 isolation / #261 durability | before client on | board | hold |
| G7 outbox | Codex | dispatch-once + rollback | before Salesforce mutations | board | hold |
| G9 enrollment | Codex + owner | kill switch ≤60s + soak | before Converspan | board | **non-waivable** |
| Spike A heard-call | Codex Test lead | owner-heard duplex proof | before claiming Spike A done | board | hold |
| Public `/conference` publish | Mr. Salam or Cursor GO | customer-visible page | before Pages promote | board | hold |

Agents do **not** invent a silent bypass.

## 6) Utilization metrics (fleet health)

Track in Cooking + Ops snapshot (public Ops shows numbers, never internal jargon):

| Metric | Definition | Target overnight |
|---|---|---|
| Agent utilization | % of roster with RESULT or Cooking update in last 60m | ≥80% non-idle |
| PR cycle | open → green checks → exact-head GO → merge | density+test green = squash-merge eligible |
| Evidence hygiene | every RESULT names evidence level | 100% |
| Gate debt | open NO-GO / pending verdict heads | triage; no silent re-ask |
| Doorbell→docs lag | DISPATCH or RESULT that changes the field → Cooking/HOW-WE-WORK refresh | <15m (Copilot Agents) |
| Test lead coverage | Codex-led suite plan posted for conference | FLEET-TEST-SUITE-CODEX-20260927 |

## 7) Doorbell → living docs

1. Bus DISPATCH wakes the idle agent and carries the `okf=` URL.
2. The agent updates **Cooking / session pack / gates** on that OKF page.
3. Copilot Agents keep HOW-WE-WORK / Cooking / session packs current across Blackboard · conference · site knowledge packs, via the PR.
4. Public **Ops** (`https://www.sfdc24.com/ops/`) shows the living hub, the fleet roles, and release NOW. Snapshot, not a motherboard dump. Public pages do not print the word OKF.

## 8) Spike A → sfdc24.com `/conference` page

- Spike A lives in `sfdc-24/conference` (LiveKit keys via env only; D-18).
- Public surface is **`/conference/`** on sfdc24-site: owner-facing status of the conference line POC — media plane, rooms, next demo — **without** the word OKF, without secrets, without motherboard internals.
- Internal durable wiki remains `docs/okf/` in private/working repos.

## 9) OPS — living hub, roles, release NOW

- URL: https://www.sfdc24.com/ops/
- Shows three things: the living hub (this OKF, in plain words), the fleet roles, and **release NOW** (Cooking now / next release). Sprint lanes on the page: Next release · In sprint · Backlog.
- Public copy does not print the word OKF.
- Overnight GO: squash-merge **green density + test** PRs on conference / site / Blackboard that are docs-or-test-safe and exact-head GO'd.
- Hold: anything touching live keys, traffic %, Salesforce writes, paid plans, or a Codex NO-GO head.

## 10) Overnight GO policy (Grok Bot, delivery lead)

**GO = squash-merge** when all true:

1. Required checks green on exact head.
2. Ownership check green (conference).
3. Exact-head GO from required reviewers (Codex Test lead where named).
4. Scope is density/docs/test/living-pack — not irreversible prod traffic.

**HOLD** otherwise; post the reason on the bus; keep the fleet on the next green item.

## 11) Pack index (inline links for board NOTE)

- This file: `docs/okf/STRATEGY.md`
- Execution model: `okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/HOW-WE-WORK.md`
- Project OKF: `okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/index.md`
- Lanes: `docs/okf/lanes.md`
- Cooking: `docs/okf/cooking.md`
- Utilization: `docs/okf/utilization.md`
- Doorbell→docs: `docs/okf/doorbell-living-docs.md`
- Conference pack: https://github.com/sfdc-24/conference/tree/main/docs/okf
- Conference strategy mirror: `docs/okf/STRATEGY.md` (conference repo)
- Public Ops board: https://www.sfdc24.com/ops/
- Public conference page (Spike A surface): https://www.sfdc24.com/conference/
- Codex PDF: Drive `1QX0wpWTuWRtsm5C2QKcmmv2r-_X3OgPq`

id=`FULL-STRATEGY-BUILD-20260927`

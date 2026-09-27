---
title: Full Strategy Pack — Codex architecture mapped
id: FULL-STRATEGY-BUILD-20260927
owner: grok (PM)
authority: Codex PDF SFDC24-Blackboard-Architecture-Current-Future-2026-09-26
status: living
updated_at: 2026-09-27T04:21:00Z
public_site_rule: never put the word OKF on www.sfdc24.com public pages
---

# Full strategy — implement Codex architecture (do not invent a parallel)

**Canon diagram:** Drive `1QX0wpWTuWRtsm5C2QKcmmv2r-_X3OgPq` — *SFDC24 - Blackboard Architecture - Current and Future State - 2026-09-26* (Codex).
**Local:** `/workspace/briefs/SFDC24-Blackboard-Architecture-Current-Future-2026-09-26-CODEX.pdf`
**Companion stitch (CONF-LINE overlay only):** `/workspace/briefs/conf-line-architecture-stitched.md` — LiveKit media plane *on top of* this motherboard; never a replacement.

This pack is HOW-WE-WORK–adjacent. Board moves work; this pack + Cooking show the field; EXPRESS is how we behave.

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

## 4) Lanes (fleet) — Test lead = Codex

| Lane | Owner | Notes |
|---|---|---|
| Strategy / track / stitch | **grok** (PM) | This pack; overnight GO triage; board NOTE ALL |
| CHAIR / Spike A–B / conference packages | **claude-code-cli** | chair/, gateway/, console/, personas/ |
| Contract + broker + **Test lead** | **chatgpt-codex-desktop (Codex)** | broker/, docs/; exact-head GO/NO-GO; fleet test suite lead |
| Adversarial / multimodal limits | **gemini** | Reasoning only unless given hands |
| Living docs ops (Cooking/HOW-WE-WORK/session packs) | **copilot** | Doorbell → refresh living docs; no motherboard redesign |
| Human decision / commercial / irreversible | **Mr. Salam** | His six; CX pricing; paid plans; live keys |

Conference folder ownership stays in `conference/docs/okf/lanes.md`. Blackboard lane map stays in `./lanes.md`. This table is the **cross-repo** strategy view.

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
| Doorbell→docs lag | board NOTE that changes field → Cooking/HOW-WE-WORK refresh | <15m (Copilot lane) |
| Test lead coverage | Codex-led suite plan posted for conference | FLEET-TEST-SUITE-CODEX-20260927 |

## 7) Doorbell → living docs

1. Board row wakes the idle agent (doorbell).
2. Agent updates **Cooking / session pack / gates** in-repo (living docs).
3. Copilot Agents keep HOW-WE-WORK / Cooking / session packs current across Blackboard · conference · site knowledge packs.
4. Public **Ops** (`https://www.sfdc24.com/ops/`) is the client-safe living **project board** — snapshot, not motherboard dump. **Never print "OKF" on the public site.**

## 8) Spike A → sfdc24.com `/conference` page

- Spike A lives in `sfdc-24/conference` (LiveKit keys via env only; D-18).
- Public surface is **`/conference/`** on sfdc24-site: owner-facing status of the conference line POC — media plane, rooms, next demo — **without** the word OKF, without secrets, without motherboard internals.
- Internal durable wiki remains `docs/okf/` in private/working repos.

## 9) OPS living project board

- URL: `/ops/` (already densified toward sprint lanes: Next release · In sprint · Backlog).
- Overnight GO: squash-merge **green density + test** PRs on conference / site / Blackboard that are docs-or-test-safe and exact-head GO'd.
- Hold: anything touching live keys, traffic %, Salesforce writes, paid plans, or a Codex NO-GO head.

## 10) Overnight GO policy (PM-Grok)

**GO = squash-merge** when all true:

1. Required checks green on exact head.
2. Ownership check green (conference).
3. Exact-head GO from required reviewers (Codex Test lead where named).
4. Scope is density/docs/test/living-pack — not irreversible prod traffic.

**HOLD** otherwise; board the reason; keep fleet busy on the next green item.

## 11) Pack index (inline links for board NOTE)

- This file: `docs/okf/STRATEGY.md`
- HOW-WE-WORK: `docs/okf/HOW-WE-WORK.md`
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

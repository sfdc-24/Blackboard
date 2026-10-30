---
type: guide
title: Board and OKF — how we work
id: EXECUTION-MODEL-20260928
updated: 2026-09-28T13:20:00Z
tags: blackboard, okf, fleet, express, execution-model
canon_express: https://github.com/sfdc-24/Blackboard/blob/main/docs/EXPRESS.md
drive_copy: https://docs.google.com/document/d/1UP3yEsSPkCctf6j9pDlw4u2b9adyqQ9An5U_j2_-Fso/edit
okf: https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/HOW-WE-WORK.md
---

# Board and OKF — how we work

Everyone on the fleet reads this before grabbing all-hands work.

This page is the Blackboard ops and execution standard. Living fleet roles refine here, on this same OKF.

**Project OKF (alignment):** [`docs/okf/`](./index.md)

`okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/index.md`

**Execution standard (this page):**

`okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/HOW-WE-WORK.md`

## Required in every OKF

Every living OKF pack (every repo that has one) **must** include:

1. **EXPRESS pointer** — link live [`docs/EXPRESS.md`](../EXPRESS.md) (how this fleet behaves: his six, evidence, shipping). Do **not** paste EXPRESS into the OKF; this repo file is the source. The [Drive copy](https://docs.google.com/document/d/1UP3yEsSPkCctf6j9pDlw4u2b9adyqQ9An5U_j2_-Fso/edit) is read-only convenience.
2. **Execution model** — this page. One shared project OKF, bus doorbells and milestones, Ops window, living fleet roles.
3. **Lanes / ownership** for that repo, matching the living roles below.
4. **Cooking / WIP** (open PRs, blockers, next merge target).
5. **Session pack** when a room or all-hands is running (attendees, topic, agenda, objectives).

Repo-specific pages (gates, duplex, architecture) sit beside those, not instead of them.

## Execution model

### 1. OKF — the execution surface

**OKF** is the standard execution surface for all delivery.

- One shared project OKF for alignment. Blackboard’s is this pack: `docs/okf/`. Conference keeps its own pack in `sfdc-24/conference` → `docs/okf/`. A second Blackboard pack, a side doc, or a private scratch file is not alignment.
- Agents work from `okf=` URLs. Open the URL, read it, and write the spec, the plan, the acceptance notes, and the role refinements on that page (by PR).
- Chat is the transcript of a wake. The Google Sheet stores bus rows. Long work, drafts, and the current picture live in the OKF.

`okf=` form, used in doorbells and in handoffs:

```
okf=https://github.com/sfdc-24/<repo>/blob/main/docs/okf/<page>.md
```

Repo-relative path (stable inside a PR):

```
okf=docs/okf/<page>.md
```

A DISPATCH names the page the work executes against. The first reply repeats that same `okf=` URL.

### 2. Blackboard BUS — doorbells and milestones

**Blackboard BUS** is the dispatch bus. It carries doorbells and milestone / larger events only:

| Event | When to post | What the row holds |
|---|---|---|
| **DISPATCH** | Wake someone, or start a named unit of work | who, what, `okf=` URL, RESULT expected |
| **RESULT** | A milestone shipped or state changed | what shipped, PR/SHA, evidence level, what’s next |
| **DONE** | The `okf=` item is finished | PR/SHA and the OKF page that flipped out of Cooking |
| **STUCK** | Paused mid-lane; someone else may resume | why, what’s safe to resume, `okf=` URL |
| **NEED_HELP** | One ask that needs another role or the owner | the ask, what yes does, what waiting costs, `okf=` URL |
| **Gate** | GO / NO-GO / hold | verdict, head SHA, the five gate fields |

Use the bus for those events: owner decisions and holds, cross-repo sequencing, and ANDON (live break, wrong evidence level, stop shipping).

Specs, design drafts, review threads, file-by-file notes, CI green spam, and Cooking refreshes stay on the OKF or the PR. The sheet is the bus, not the workspace.

Every DISPATCH puts `okf=` inline in the subject or payload. Grammar (no literal pipes inside values):

```
BCB|v=1|id=<TAG>-<SUBJECT>-<YYYYMMDDTHHMMZ>|phase=DISPATCH|from=<tag>|to=<tag>|okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/<page>.md|
```

### 3. OPS — the public window

**OPS** is [www.sfdc24.com/ops](https://www.sfdc24.com/ops/). It shows three things:

1. **Living hub** — the public view of this OKF (Cooking, how we work, session packs).
2. **Roles** — the living fleet roles below, as roles, not live presence.
3. **Release NOW** — what is shipping now (Cooking now / next release).

Ops is a snapshot for people outside the repo. It is not a second workspace and not a motherboard dump. Public pages use plain words. They do not print the word OKF (`/ops/`, `/conference/`).

### 4. Living fleet roles

Refine these on this same OKF. When a role changes, edit this table in a PR. Do not keep a second role map in chat or on the sheet.

| Role | Tags | Owns |
|---|---|---|
| **Grok Bot** — Delivery and strategy lead | `grok-bot`, `grok` | Delivery plan, strategy, fleet alignment on this OKF |
| **Codex** — Quality and test lead | `codex`, `chatgpt-codex-desktop`, `CODEX-DESKTOP` | Quality bar, fleet tests, exact-head GO / NO-GO |
| **Claude** — Data and security engineer | `claude-code-cli` | Data handling and security engineering, with evidence |
| **Cursor** — Heavy PM and Build and PR execution | `cursor` | Heavy project management, the build, and the PR |
| **Gemini** — Admin and analyst | `gemini` | Admin surfaces and analysis, written back to this OKF |
| **Copilot Agents** — GitHub DevOps and repo reviewer | `copilot` | GitHub DevOps, CI, and repository review (the PR is the channel) |

**Mr. Salam** remains the human owner: product, priority, his six, and every irreversible or client-visible gate. He is not a fleet tag.

One owner per lane. The author of a change does not issue that change’s quality verdict (Codex). Cursor does not approve a moved head it authored. Copilot Agents do not read the bus; a board-capable agent relays. Gemini does not hold the quality gate.

EXPRESS remains how we behave (his six, evidence, shipping). Delivery assignment is this table. [`docs/EXPRESS.md`](../EXPRESS.md) §2 points here.

## How a unit of work moves

1. Open the project OKF (`okf=` hub). Read this page, Lanes, and Cooking. Read EXPRESS once per session.
2. Write the spec, the plan, and the named HITL gates on the OKF before anyone runs.
3. **DISPATCH** a short bus row: who, what, `okf=` URL, RESULT expected.
4. Build on a PR (Cursor). Copilot Agents review the repo. Codex issues the quality verdict on the exact head.
5. Post **RESULT**, then **DONE**, **STUCK**, or **NEED_HELP**. Post the gate verdict the same way. Update Cooking in the same beat.
6. Ops shows the living hub, the roles, and release NOW. It does not collect the spec.

## Human in the loop (HITL) — bake gates into the plan

Speed without planned review just ships risk faster. When work moves lightning-fast, **name the approvals before agents run**. Gates are part of the plan, not a chat afterthought.

### Write every gate with five fields

1. **Who** — one named reviewer (owner, Cursor, lane lead, or all-hands). Not "someone."
2. **What** — exact artifact (PR + head SHA, demo URL, CONFIG change, merge, live keys).
3. **When** — before merge, before deploy, before live keys, before customer-facing.
4. **How** — GO / NO-GO on the bus (doorbell), or Cooking marked `waiting:<gate>`. Reviewers read the bus row, the PR, and the `okf=` page. Chat alone is not a verdict.
5. **Timeout** — if silence past the named window, **hold**. Do not auto-advance on lightning work.

### What always gets a gate

Anything irreversible, customer-visible, spendy, identity-bearing, or live-key / production. Parallel work is fine; **crossing a gate is not**.

### Where it lives

- Put named gates in the plan / Cooking / `gates.md` **before** the DISPATCH.
- Post the **verdict** on the bus (GO / NO-GO / hold + head SHA). Keep Cooking current.
- Spike and repo checklists (e.g. Conference Spike A) are instances of this rule, not exceptions.

Owner and lead agents plan HITL together. Agents do not invent a silent bypass.

## Where the living OKF lives

- This repo: `docs/okf/` — `okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/index.md`
- This standard: `okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/HOW-WE-WORK.md`
- Conference Line: `sfdc-24/conference` → `docs/okf/`
- EXPRESS: [`docs/EXPRESS.md`](../EXPRESS.md)
- Ops: https://www.sfdc24.com/ops/

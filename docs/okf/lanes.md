---
title: Blackboard OKF Lanes
owner: fleet
status: living
updated_at: 2026-09-28T13:20:00Z
strategy: FULL-STRATEGY-BUILD-20260927
roles: EXECUTION-MODEL-20260928
okf: https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/lanes.md
---

# Lanes and ownership (Blackboard-touching work)

Living fleet roles are defined in [HOW-WE-WORK](./HOW-WE-WORK.md) (`EXECUTION-MODEL-20260928`). Refine them there. This page is the lane each role runs.

`okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/lanes.md`

## Lane map

| Lane | Primary owner | Typical work in Blackboard | Inputs | Exit signal |
|---|---|---|---|---|
| Delivery and strategy | **Grok Bot** (`grok-bot`, `grok`) | Delivery plan, STRATEGY pack, role refinements on this OKF, merge triage | Project OKF + owner priorities | OKF updated; bus carries DISPATCH / RESULT only |
| Quality and test | **Codex** (`chatgpt-codex-desktop`) | Fleet tests, exact-head GO/NO-GO, architecture PDF fidelity | PR diffs + `okf=` page | Explicit GO/NO-GO on the bus |
| Data and security | **Claude** (`claude-code-cli`) | Data handling, security engineering, evidence for those controls | Data-path and security changes | Finding or control written on the OKF; gate if irreversible |
| Heavy PM, build, and PR | **Cursor** | Implementation, PR execution, heavy project management of the change | `okf=` spec | Open PR at an exact head SHA |
| Admin and analyst | **Gemini** (`gemini`) | Admin surfaces, analysis, ops readouts | Directed questions, snapshots | Analysis captured on the OKF |
| GitHub DevOps and repo review | **Copilot Agents** (`copilot`) | Workflows, CI, Cooking / HOW-WE-WORK / session pack refresh, PR review | PR events | Review on the PR; docs lag ≤15m |
| Human decision gate | **Mr. Salam** | Product, priority, irreversible/external-impact decisions | One NEED_HELP with a recommendation | Ruling issued and logged |

## Ownership rules

- One owner per lane at a time. Name the lane on the `okf=` page before deep work.
- Work from the `okf=` URL. The bus is DISPATCH, RESULT, DONE, STUCK, NEED_HELP, and gates.
- Keep Cooking current so all lanes see blockers in one place.
- If a blocker needs another lane, record it as a blocker and name the target role.
- Execute Codex PDF CURRENT/FUTURE + promotion order (see STRATEGY.md). Role changes land on HOW-WE-WORK, not a parallel map.

## Lane handoff checklist

- Problem stated in one sentence.
- Evidence linked (PR/check/log/doc line).
- Clear next owner.
- Explicit unblock condition.

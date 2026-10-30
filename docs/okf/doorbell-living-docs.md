---
title: Doorbell → living docs
id: FULL-STRATEGY-BUILD-20260927
owner: grok-bot + copilot
updated_at: 2026-09-28T13:20:00Z
okf: https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/doorbell-living-docs.md
---

# Doorbell → living docs

Standard: [HOW-WE-WORK](./HOW-WE-WORK.md) execution model.

`okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/doorbell-living-docs.md`

## Loop

1. **Doorbell (bus)** — DISPATCH, RESULT, DONE, STUCK, NEED_HELP, or a gate verdict. The row names `to=` and the `okf=` URL.
2. **Living docs (repo)** — the `okf=` page: Cooking, HOW-WE-WORK, session pack, gates, STRATEGY. Specs and long work land here.
3. **Public Ops** — https://www.sfdc24.com/ops/ shows the living hub, the fleet roles, and release NOW. Plain words on the public page.
4. **Public `/conference/`** — Spike A status surface for humans; no secrets; no internal jargon label.

## Who refreshes what

| Surface | Refresher | Trigger |
|---|---|---|
| Blackboard `docs/okf/cooking.md` | baker / Copilot Agents | open PR change, merge, blocker |
| Blackboard role table | Grok Bot (delivery lead), by PR | role refinement |
| Conference `docs/okf/*` | that project's OKF owners; Copilot Agents may refresh via PR | Spike A / WP merges |
| Site `knowledge/` packs | Grok Bot / Copilot Agents | session start |
| `/ops/` snapshot (hub, roles, release NOW) | site bake job | every ~5m |
| `/conference/` | site PR (density+copy) | Spike A status change |

## Anti-patterns

- Drafting the spec, or parking long work, on the bus.
- Reconstructing the task from chat instead of opening the `okf=` URL.
- Using the sheet as the workspace.
- Expecting the OKF alone to wake an idle agent (DISPATCH on the bus).
- Printing the word OKF on www.sfdc24.com.

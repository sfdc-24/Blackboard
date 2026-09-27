---
title: Doorbell → living docs
id: FULL-STRATEGY-BUILD-20260927
owner: grok + copilot
updated_at: 2026-09-27T04:21:00Z
---

# Doorbell → living docs

## Loop

1. **Doorbell (board)** — short NOTE / TASK / RESULT / GO wakes the right tag (`to=`).
2. **Living docs (repo)** — Cooking, HOW-WE-WORK, session pack, gates, STRATEGY updated in the owning repo.
3. **Public Ops** — client-safe project board at `/ops/` refreshed from snapshot (no internal wiki word on public pages).
4. **Public `/conference/`** — Spike A status surface for humans; no secrets; no internal jargon label.

## Who refreshes what

| Surface | Refresher | Trigger |
|---|---|---|
| Blackboard `docs/okf/cooking.md` | baker / copilot | open PR change, merge, blocker |
| Conference `docs/okf/*` | Codex owns docs/; Copilot may refresh ops | Spike A / WP merges |
| Site `knowledge/` packs | grok/copilot | session start |
| `/ops/` snapshot | site bake job | every ~5m |
| `/conference/` | site PR (density+copy) | Spike A status change |

## Anti-patterns

- Board play-by-play of file edits (use Cooking).
- Expecting OKF alone to wake an idle agent (use board doorbell).
- Printing internal wiki names on www.sfdc24.com.

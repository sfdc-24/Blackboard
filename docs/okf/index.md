---
title: Blackboard OKF Hub
owner: fleet
status: living
updated_at: 2026-09-27T04:21:00Z
---

# Blackboard OKF

This is the living OKF surface for work in `sfdc-24/Blackboard`.

## Start here

- [Board vs OKF + HITL (HOW-WE-WORK)](./HOW-WE-WORK.md) ← doctrine first
- [**Full strategy (Codex PDF mapped)**](./STRATEGY.md) ← overnight PM pack `FULL-STRATEGY-BUILD-20260927`
- [Cooking (WIP + blockers)](./cooking.md) ← all-hands visibility surface
- [Lanes and ownership](./lanes.md)
- [Utilization metrics](./utilization.md)
- [Doorbell → living docs](./doorbell-living-docs.md)
- [Session pack (fluid)](./session-pack.md)
- [Real-time work during a call (side conversation in Redis)](./realtime.md)
- EXPRESS: [`docs/EXPRESS.md`](../EXPRESS.md)

## Operating rhythm

1. Read HOW-WE-WORK (Board vs OKF + HITL gates).
2. Read STRATEGY (Codex CURRENT/FUTURE + promotion order) before inventing work.
3. Refresh cooking from live open PRs:
   - `python docs/okf/bake.py`
   - If sandbox/API access returns HTTP 403, seed `cooking.md` from live PR data and add a short fallback note.
4. Read blockers first, then lane updates.
5. Update session pack for the current working block.

## Scope boundary

- This OKF is for Blackboard-repo work only.
- Conference OKF remains in `sfdc-24/conference` and is not duplicated here.
- Public site must not display the word OKF (`/ops/`, `/conference/`).

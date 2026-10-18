---
title: Blackboard OKF Hub
owner: fleet
status: living
updated_at: 2026-09-28T13:20:00Z
okf: https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/index.md
---

# Blackboard OKF

This is the one shared project OKF for work in `sfdc-24/Blackboard`. Agents align here and work from `okf=` URLs.

`okf=https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/index.md`

## Start here

- [Execution model (HOW-WE-WORK)](./HOW-WE-WORK.md) ← ops and execution standard (`EXECUTION-MODEL-20260928`)
- [**Full strategy (Codex PDF mapped)**](./STRATEGY.md) ← overnight PM pack `FULL-STRATEGY-BUILD-20260927`
- [Cooking (WIP + blockers)](./cooking.md) ← all-hands visibility surface
- [Lanes and ownership](./lanes.md) ← living fleet roles
- [Utilization metrics](./utilization.md)
- [Doorbell → living docs](./doorbell-living-docs.md)
- [Session pack (fluid)](./session-pack.md)
- EXPRESS: [`docs/EXPRESS.md`](../EXPRESS.md)
- Ops (living hub, roles, release NOW): https://www.sfdc24.com/ops/

## Operating rhythm

1. Open the `okf=` URL for this hub. Read the execution model (OKF surface, bus doorbells and milestones, Ops, living roles).
2. Read STRATEGY (Codex CURRENT/FUTURE + promotion order) before inventing work. Delivery roles stay on HOW-WE-WORK.
3. Refresh cooking from live open PRs:
   - `python docs/okf/bake.py`
   - If sandbox/API access returns HTTP 403, seed `cooking.md` from live PR data and add a short fallback note.
4. Read blockers first, then lane updates.
5. Update session pack for the current working block.
6. DISPATCH on the bus only to wake, and include this `okf=` URL. Specs stay in the pack.

## Scope boundary

- This OKF is for Blackboard-repo work only.
- Conference OKF remains in `sfdc-24/conference` and is not duplicated here.
- Public site must not display the word OKF (`/ops/`, `/conference/`).

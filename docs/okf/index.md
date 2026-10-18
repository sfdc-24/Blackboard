---
title: Blackboard OKF Hub
owner: fleet
status: living
updated_at: 2026-09-27T04:45:00Z
---

# Blackboard OKF

This is the living OKF surface for work in `sfdc-24/Blackboard`.

## Start here

- [Board vs OKF + HITL (HOW-WE-WORK)](./HOW-WE-WORK.md) ← doctrine first
- [**Test matrix**](./tests.md) ← **SoT for test status** (Codex authors; agents check off)
- [**Full strategy (Codex PDF mapped)**](./STRATEGY.md) ← overnight PM pack `FULL-STRATEGY-BUILD-20260927`
- [Cooking (WIP + blockers)](./cooking.md) ← all-hands visibility surface (not test SoT)
- [Lanes and ownership](./lanes.md)
- [Utilization metrics](./utilization.md)
- [Doorbell → living docs](./doorbell-living-docs.md)
- [Session pack (fluid)](./session-pack.md)
- EXPRESS: [`docs/EXPRESS.md`](../EXPRESS.md)

## Operating rhythm

1. Read HOW-WE-WORK (Board vs OKF + HITL gates).
2. Read STRATEGY (Codex CURRENT/FUTURE + promotion order) before inventing work.
3. For test status → open [`tests.md`](./tests.md) (conference Spike A → conference matrix).
4. Refresh cooking from live open PRs:
   - `python docs/okf/bake.py`
   - If sandbox/API access returns HTTP 403, seed `cooking.md` from live PR data and add a short fallback note.
5. Read blockers first, then lane updates.
6. Update session pack for the current working block.

## Scope boundary

- This OKF is for Blackboard-repo work only.
- Conference OKF remains in `sfdc-24/conference` and is not duplicated here (Spike A scenarios stay on conference `tests.md`).
- Public site must not display the word OKF (`/ops/`, `/conference/`).

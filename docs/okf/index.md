---
title: Blackboard OKF Hub
owner: fleet
status: living
updated_at: 2026-09-27
---

# Blackboard OKF

This is the living OKF surface for work in `sfdc-24/Blackboard`.

## Start here

- [Board vs OKF + HITL (HOW-WE-WORK)](./HOW-WE-WORK.md) ← doctrine first
- [Cooking (WIP + blockers)](./cooking.md) ← all-hands visibility surface
- [Lanes and ownership](./lanes.md)
- [Session pack (fluid)](./session-pack.md)
- EXPRESS: [`docs/EXPRESS.md`](../EXPRESS.md)

## Operating rhythm

1. Read HOW-WE-WORK (Board vs OKF + HITL gates).
2. Refresh cooking from live open PRs:
   - `python docs/okf/bake.py`
   - If sandbox/API access returns HTTP 403, seed `cooking.md` from live PR data and add a short fallback note.
3. Read blockers first, then lane updates.
4. Update session pack for the current working block.

## Scope boundary

- This OKF is for Blackboard-repo work only.
- Conference OKF remains in `sfdc-24/conference` and is not duplicated here.

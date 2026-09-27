---
title: Blackboard OKF Cooking
owner: fleet
status: living
updated_at: 2026-09-27T04:45:00Z
source: live-open-prs
---

# Cooking (WIP + blockers)

**Test status SoT:** [`tests.md`](./tests.md) (fleet pattern) · Conference Spike A: [conference `tests.md`](https://github.com/sfdc-24/conference/blob/main/docs/okf/tests.md). Cooking is WIP/PRs only — do not use Cooking to answer “who tested.”

All-hands visibility surface for open work in `sfdc-24/Blackboard`.

_Last baked: 2026-09-27T02:47:00Z — re-run `python docs/okf/bake.py` for live PR table._

## Refresh

- Run: `python docs/okf/bake.py`
- Source: `GET /repos/sfdc-24/Blackboard/pulls`

## Status notify → Grok PM

On Cooking state flip, board `NOTE`/`RESULT` with `okf=` + `status=DONE|STUCK|NEED_HELP` (`need=` for NEED_HELP). **NO NAPS:** after DONE, doorbell Grok, else run open `tests.md` scenarios, else NEED_HELP/help.

---
title: Fleet test matrix (source of truth pattern)
id: OKF-TESTS-MATRIX-20260927
owner: chatgpt-codex-desktop (Test lead)
status: living
updated_at: 2026-09-27T04:45:00Z
conference_canon: https://github.com/sfdc-24/conference/blob/main/docs/okf/tests.md
---

# Fleet test matrix (Blackboard mirror)

**Pattern:** Codex (Test lead) authors scenarios; each agent checks off **own cell** only; board `RESULT` includes `okf=` URL.

Conference Spike A / line scenarios live in the **conference** matrix (do not duplicate scenario text here):

→ https://github.com/sfdc-24/conference/blob/main/docs/okf/tests.md

This file holds **Blackboard-repo** suite rows and the shared check-off protocol so the fleet uses one pattern everywhere.

## Check-off protocol (fleet)

1. **Codex authors** the Scenario row.
2. Runner marks **own cell** (`PASS` / `FAIL` / `BLOCKED` / `SKIP`) + Status + Evidence link.
3. Board `RESULT` with `okf=` to this file or the conference matrix URL (row anchor preferred).
4. No "did X test?" — **look at the matrix**.

Cell legend: `—` not run · `PASS` · `FAIL` · `BLOCKED` · `SKIP`

## Status notify (tests)
**NO NAPS:** after DONE, doorbell Grok for next work, else run another open scenario, else NEED_HELP/help. Idle = violation.

After marking a cell, board `RESULT` with `okf=` to this matrix (row anchor preferred) and `status=DONE|STUCK|NEED_HELP`. If `NEED_HELP`, set `need=<writer-tag>`. Grok PM watches and reassigns. Do not treat the board as the test log — the matrix is SoT.

## Blackboard matrix

| Scenario | Owner-author (Codex) | Claude | Codex | Gemini | Grok | Status | Evidence link |
|---|---|---|---|---|---|---|---|
| BB-BUS-01 Board append + readback (D-4) | Codex — *author pending* | — | — | — | — | OPEN | — |
| BB-COOK-01 Cooking bake from open PRs | Codex — *author pending* | — | — | — | — | OPEN | — |
| BB-HITL-01 Exact-head GO/NO-GO posted on board | Codex — *author pending* | — | — | — | — | OPEN | — |
| BB-DOORBELL-01 DISPATCH includes living-docs `okf=` URL | Codex — *author pending* | — | — | — | — | OPEN | — |

## Cross-link

| Surface | URL |
|---|---|
| Conference test SoT | https://github.com/sfdc-24/conference/blob/main/docs/okf/tests.md |
| This mirror | https://github.com/sfdc-24/Blackboard/blob/main/docs/okf/tests.md |
| HOW-WE-WORK | [HOW-WE-WORK.md](./HOW-WE-WORK.md) |
| Cooking (WIP only) | [cooking.md](./cooking.md) |

id=`OKF-TESTS-MATRIX-20260927`

---
title: Blackboard OKF Cooking
owner: fleet
status: living
updated_at: 2026-09-28T13:17:45Z
source: live-open-prs
---

# Cooking (WIP + blockers)

All-hands visibility surface for open work in `sfdc-24/Blackboard`.

_Last baked: 2026-09-28T13:17:45Z_

## Open PRs

| PR | Title | Author | Updated (UTC) | Checks | Blockers |
|---|---|---|---|---|---|
| [#291](https://github.com/sfdc-24/Blackboard/pull/291) | docs(okf): tests.md matrix mirror — Codex Test lead + check-off (OKF-TESTS-MATRIX-20260927) | @sfdc-24 | 2026-09-27 04:40Z | success | None declared |
| [#280](https://github.com/sfdc-24/Blackboard/pull/280) | Gate heavy local agent work behind host headroom | @sfdc-24 | 2026-09-27 01:06Z | success | None declared |
| [#260](https://github.com/sfdc-24/Blackboard/pull/260) | Studio: client workspaces behind STUDIO_CLIENT_WORKSPACES | @sfdc-24 | 2026-09-26 23:52Z | success | None declared |
| [#275](https://github.com/sfdc-24/Blackboard/pull/275) | Quote PDF: a designed build plan and quote; DRAFT CAD price list for review (dark) | @sfdc-24 | 2026-09-26 06:44Z | success | None declared |
| [#276](https://github.com/sfdc-24/Blackboard/pull/276) | Architecture PDF edition 2: one evidence cutoff; verdicts read from the board | @sfdc-24 | 2026-09-26 04:21Z | success | None declared |
| [#261](https://github.com/sfdc-24/Blackboard/pull/261) | Workspaces: a project keeps its design between visits | @sfdc-24 | 2026-09-26 02:44Z | success | None declared |
| [#268](https://github.com/sfdc-24/Blackboard/pull/268) | docs: define creator minibus offer and CX gates | @sfdc-24 | 2026-09-25 17:36Z | success | None declared |
| [#267](https://github.com/sfdc-24/Blackboard/pull/267) | Advisor: output moderation, telemetry, moderation interlock (still off) | @sfdc-24 | 2026-09-25 15:45Z | success | None declared |
| [#213](https://github.com/sfdc-24/Blackboard/pull/213) | feat(studio): add disabled Governor email adapter | @sfdc-24 | 2026-09-24 08:48Z | success | Draft PR |
| [#91](https://github.com/sfdc-24/Blackboard/pull/91) | Advisory: validate dialogue gaps and fail safely on pause errors | @sfdc-24 | 2026-09-23 18:09Z | success | Draft PR |
| [#96](https://github.com/sfdc-24/Blackboard/pull/96) | Fail closed on invalid BUS read responses | @sfdc-24 | 2026-09-23 18:09Z | success | Draft PR |
| [#137](https://github.com/sfdc-24/Blackboard/pull/137) | A read-only rail into the dev org, and the Actions publisher that replaces the proxy | @sfdc-24 | 2026-09-18 07:01Z | success | None declared |
| [#85](https://github.com/sfdc-24/Blackboard/pull/85) | Give the rehearsal two voices, and refuse to half-play it | @sfdc-24 | 2026-09-17 13:48Z | success | None declared |
| [#116](https://github.com/sfdc-24/Blackboard/pull/116) | Write down what has to be up for the fleet to work | @sfdc-24 | 2026-09-15 23:42Z | pending | Draft PR; Checks pending |
| [#104](https://github.com/sfdc-24/Blackboard/pull/104) | Add a post-auth mother/child admission reference | @sfdc-24 | 2026-09-14 02:39Z | success | Draft PR |
| [#80](https://github.com/sfdc-24/Blackboard/pull/80) | Fail closed before presenter playback | @sfdc-24 | 2026-09-12 05:36Z | success | None declared |
| [#72](https://github.com/sfdc-24/Blackboard/pull/72) | Keep local voice microphone capture explicitly opt-in | @sfdc-24 | 2026-09-11 17:55Z | failure | Checks failing |
| [#66](https://github.com/sfdc-24/Blackboard/pull/66) | The bus-client suite runs on Linux, and the leak detector fails open there | @sfdc-24 | 2026-09-11 02:57Z | failure | Checks failing |
| [#36](https://github.com/sfdc-24/Blackboard/pull/36) | The X-Ray console: verified, and held out of the release path | @sfdc-24 | 2026-09-11 00:52Z | pending | Checks pending |
| [#54](https://github.com/sfdc-24/Blackboard/pull/54) | Verify remote publication before agent handoffs | @sfdc-24 | 2026-09-10 17:06Z | success | None declared |
| [#25](https://github.com/sfdc-24/Blackboard/pull/25) | Keep mirrored voice turns sequential while a reply is pending | @sfdc-24 | 2026-09-07 17:07Z | success | Draft PR |
| [#3](https://github.com/sfdc-24/Blackboard/pull/3) | SFDC-BACKEND-001: sfdc24.com Web-to-Lead intake (split out of the CI/CD PR) | @sfdc-24 | 2026-09-07 05:53Z | success | Draft PR |

## Blocker rollup

- None declared: 12
- Draft PR: 7
- Checks failing: 2
- Checks pending: 2

## Refresh

- Run: `python docs/okf/bake.py`
- Fallback: if API access in sandbox returns HTTP 403, seed from live PR list data and note it in this file.
- Source: `GET /repos/sfdc-24/Blackboard/pulls` (+ per-PR detail/status)


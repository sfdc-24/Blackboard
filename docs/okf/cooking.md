---
title: Blackboard OKF Cooking
owner: fleet
status: living
updated_at: 2026-09-27T02:45:00Z
source: mcp-seed
---

# Cooking (WIP + blockers)

All-hands visibility surface for open work in `sfdc-24/Blackboard`.

_Last seeded: 2026-09-27T02:45:00Z (from GitHub MCP open PRs)._

> Canonical refresh path is `python docs/okf/bake.py` with `GITHUB_TOKEN`.

## Open PRs

| PR | Title | Updated (UTC) | Blockers |
|---|---|---|---|
| [#284](https://github.com/sfdc-24/Blackboard/pull/284) | docs: Board vs OKF — how we work | 2026-09-27T02:30:42Z | None declared |
| [#280](https://github.com/sfdc-24/Blackboard/pull/280) | Gate heavy local agent work behind host headroom | 2026-09-27T01:06:00Z | None declared |
| [#278](https://github.com/sfdc-24/Blackboard/pull/278) | Poka-yoke L-100 to L-113: stacked conflicts, claimed kills, verdict lanes, release races, overnight coordination | 2026-09-26T15:23:44Z | None declared |
| [#276](https://github.com/sfdc-24/Blackboard/pull/276) | Architecture PDF edition 2: one evidence cutoff; verdicts read from the board | 2026-09-26T04:21:54Z | None declared |
| [#275](https://github.com/sfdc-24/Blackboard/pull/275) | Quote PDF: a designed build plan and quote; DRAFT CAD price list for review (dark) | 2026-09-26T06:44:31Z | None declared |
| [#268](https://github.com/sfdc-24/Blackboard/pull/268) | docs: define creator minibus offer and CX gates | 2026-09-25T17:36:19Z | None declared |
| [#267](https://github.com/sfdc-24/Blackboard/pull/267) | Advisor: output moderation, telemetry, moderation interlock (still off) | 2026-09-25T15:45:39Z | None declared |
| [#261](https://github.com/sfdc-24/Blackboard/pull/261) | Workspaces: a project keeps its design between visits | 2026-09-26T02:44:00Z | None declared |
| [#260](https://github.com/sfdc-24/Blackboard/pull/260) | Studio: client workspaces behind STUDIO_CLIENT_WORKSPACES | 2026-09-26T23:52:32Z | None declared |
| [#213](https://github.com/sfdc-24/Blackboard/pull/213) | feat(studio): add disabled Governor email adapter | 2026-09-24T08:48:42Z | Draft PR |
| [#137](https://github.com/sfdc-24/Blackboard/pull/137) | A read-only rail into the dev org, and the Actions publisher that replaces the proxy | 2026-09-18T07:01:42Z | Aging backlog |
| [#116](https://github.com/sfdc-24/Blackboard/pull/116) | Write down what has to be up for the fleet to work | 2026-09-15T23:42:22Z | Draft PR; Aging backlog |
| [#104](https://github.com/sfdc-24/Blackboard/pull/104) | Add a post-auth mother/child admission reference | 2026-09-14T02:39:35Z | Draft PR; Aging backlog |
| [#96](https://github.com/sfdc-24/Blackboard/pull/96) | Fail closed on invalid BUS read responses | 2026-09-23T18:09:08Z | Draft PR |
| [#91](https://github.com/sfdc-24/Blackboard/pull/91) | Advisory: validate dialogue gaps and fail safely on pause errors | 2026-09-23T18:09:10Z | Draft PR |
| [#85](https://github.com/sfdc-24/Blackboard/pull/85) | Give the rehearsal two voices, and refuse to half-play it | 2026-09-17T13:48:04Z | Aging backlog |
| [#80](https://github.com/sfdc-24/Blackboard/pull/80) | Fail closed before presenter playback | 2026-09-12T05:36:08Z | Aging backlog |
| [#72](https://github.com/sfdc-24/Blackboard/pull/72) | Keep local voice microphone capture explicitly opt-in | 2026-09-11T17:55:24Z | Aging backlog |
| [#66](https://github.com/sfdc-24/Blackboard/pull/66) | The bus-client suite runs on Linux, and the leak detector fails open there | 2026-09-11T02:57:41Z | Aging backlog |
| [#54](https://github.com/sfdc-24/Blackboard/pull/54) | Verify remote publication before agent handoffs | 2026-09-10T17:06:11Z | Aging backlog |
| [#36](https://github.com/sfdc-24/Blackboard/pull/36) | The X-Ray console: verified, and held out of the release path | 2026-09-11T00:52:43Z | Aging backlog |
| [#25](https://github.com/sfdc-24/Blackboard/pull/25) | Keep mirrored voice turns sequential while a reply is pending | 2026-09-07T17:07:01Z | Draft PR; Aging backlog |
| [#3](https://github.com/sfdc-24/Blackboard/pull/3) | SFDC-BACKEND-001: sfdc24.com Web-to-Lead intake (split out of the CI/CD PR) | 2026-09-07T05:53:13Z | Draft PR; Aging backlog |

## Blocker rollup

- Draft PR: 7
- Aging backlog (older and likely needs explicit triage): 10
- None declared: 13

## Refresh

- Run: `python docs/okf/bake.py`
- Required env: `GITHUB_TOKEN`
- Target API: `GET /repos/sfdc-24/Blackboard/pulls`

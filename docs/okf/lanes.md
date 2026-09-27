---
title: Blackboard OKF Lanes
owner: fleet
status: living
updated_at: 2026-09-27T04:21:00Z
strategy: FULL-STRATEGY-BUILD-20260927
---

# Lanes and ownership (Blackboard-touching work)

## Lane map

| Lane | Primary owner | Typical work in Blackboard | Inputs | Exit signal |
|---|---|---|---|---|
| Release/CI | `claude-code-cli` | Workflows, gates, deployment safety, release checks | Open PR checks, workflow runs | Required checks green + reviewer verdict |
| Controller/runtime | `claude-code-cli` | `scripts/`, `src/`, runtime contracts, guard rails | Bug reports, acceptance findings | Repro closed with evidence |
| **Test lead + strategy/security gate** | `chatgpt-codex-desktop` (**Codex**) | Exact-head GO/NO-GO, architecture PDF fidelity, fleet test suite lead, sequencing rulings | PR diffs + board dispatches + Codex PDF | Explicit GO/NO-GO verdict |
| Strategy / overnight PM | `grok` | STRATEGY pack, merge triage (density+test), board NOTE ALL | Owner sleep brief | Pack live + green merges |
| Adversarial reasoning | `gemini`, `grok-bot` | Risk probes, positioning, dissent framing | Directed questions / board dispatches | Actionable recommendation captured |
| Living docs ops | `copilot` | Cooking / HOW-WE-WORK / session pack refresh | Doorbell + PR events | Docs lag ≤15m |
| Auto PR review | `copilot` | PR-level review findings in thread | Open PR head | Findings resolved or dispositioned |
| Human decision gate | `Mr. Salam` | Product, priority, irreversible/external-impact decisions | Condensed asks with recommendation | Ruling issued and logged |

## Ownership rules

- One owner per lane at a time; announce lane before deep work.
- Keep Cooking current so all lanes see blockers in one place.
- If a blocker needs a decision from another lane, record it as a blocker and name the target lane.
- **Do not invent a parallel architecture** — execute Codex PDF CURRENT/FUTURE + promotion order (see STRATEGY.md).

## Lane handoff checklist

- Problem stated in one sentence.
- Evidence linked (PR/check/log/doc line).
- Clear next owner.
- Explicit unblock condition.

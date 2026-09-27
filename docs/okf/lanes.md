---
title: Blackboard OKF Lanes
owner: fleet
status: living
updated_at: 2026-09-27
---

# Lanes and ownership (Blackboard-touching work)

## Lane map

| Lane | Primary owner | Typical work in Blackboard | Inputs | Exit signal |
|---|---|---|---|---|
| Release/CI | `claude-code-cli` | Workflows, gates, deployment safety, release checks | Open PR checks, workflow runs | Required checks green + reviewer verdict |
| Controller/runtime | `claude-code-cli` | `scripts/`, `src/`, runtime contracts, guard rails | Bug reports, acceptance findings | Repro closed with evidence |
| Test lead (fleet) | `chatgpt-codex-desktop` (Codex) | Voice/chair tests, mutant gates, flake triage for conference/fleet | Test failures, flake reports, gate CI | Gate green or flake dispositioned |
| Strategy & security gate | `grok` / `grok-bot` | Gate verdicts, architecture/security review, sequencing rulings | PR diffs + board dispatches | Explicit GO/NO-GO verdict |
| Adversarial reasoning | `gemini`, `grok-bot` | Risk probes, positioning, dissent framing, challenge assumptions | Directed questions / board dispatches | Actionable recommendation captured |
| Auto PR review | `copilot` | PR-level review findings in thread | Open PR head | Findings resolved or dispositioned |
| Human decision gate | `Mr. Salam` | Product, priority, irreversible/external-impact decisions | Condensed asks with recommendation | Ruling issued and logged |

## Ownership rules

- One owner per lane at a time; announce lane before deep work.
- Keep Cooking current so all lanes see blockers in one place.
- If a blocker needs a decision from another lane, record it as a blocker and name the target lane.

## Lane handoff checklist

- Problem stated in one sentence.
- Evidence linked (PR/check/log/doc line).
- Clear next owner.
- Explicit unblock condition.

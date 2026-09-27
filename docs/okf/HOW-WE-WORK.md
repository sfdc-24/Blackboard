---
type: guide
title: Board and OKF — how we work
updated: 2026-09-27T02:30:00Z
tags: blackboard, okf, fleet
---

# Board and OKF — how we work

Everyone on the fleet reads this before grabbing all-hands work.

## Simple rule

**Board (Blackboard)** = the dispatch bus. Short TASK / RESULT / NOTE rows so agents wake, claim work, and report back. Good for “do this now” and “here’s what I finished.” Bad as a map of the whole field when everyone’s crowded.

**OKF** = the shared picture. Lanes, what’s cooking, gates, session intent. Good for all-hands visibility and “don’t step on that folder.” Lives in the repo next to the work, so PRs and ownership stay honest. Bad as a pager — don’t put every micro-task only in OKF and expect a wake.

## How they fit together

1. Before grabbing a lane → read OKF Cooking + Lanes.
2. Starting real work → board a short TASK (who, what, RESULT expected).
3. While working → keep OKF Cooking current (baker or a quick edit).
4. Done → RESULT on the board; OKF flips that item out of Cooking.
5. Conference / any room → session OKF is the gate; board is still how follow-up work lands after the call.

Board moves work. OKF shows the field. Use both; don’t make either do the other’s job.

## Where living OKF packs live

- This repo: `docs/okf/` (fleet / Blackboard view — Copilot session may be seeding it)
- Conference Line: `sfdc-24/conference` → `docs/okf/` (PR #8)

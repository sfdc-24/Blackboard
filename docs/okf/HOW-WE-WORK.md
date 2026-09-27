---
type: guide
title: Board and OKF — how we work
updated: 2026-09-27T02:35:00Z
tags: blackboard, okf, fleet, express
canon_express: https://github.com/sfdc-24/Blackboard/blob/main/docs/EXPRESS.md
drive_copy: https://docs.google.com/document/d/1UP3yEsSPkCctf6j9pDlw4u2b9adyqQ9An5U_j2_-Fso/edit
---

# Board and OKF — how we work

Everyone on the fleet reads this before grabbing all-hands work.

## Required in every OKF

Every living OKF pack **must** include:

1. **EXPRESS pointer** — link live [`docs/EXPRESS.md`](../EXPRESS.md) (how this fleet works). Do **not** paste EXPRESS into the OKF; this repo file is the source. The [Drive copy](https://docs.google.com/document/d/1UP3yEsSPkCctf6j9pDlw4u2b9adyqQ9An5U_j2_-Fso/edit) is read-only convenience.
2. **Board vs OKF** — the simple rule below.
3. **Lanes / ownership** for that repo.
4. **Cooking / WIP** (open PRs, blockers, next merge target).
5. **Session pack** when a room or all-hands is running.

Repo-specific pages sit beside those, not instead of them.

## Simple rule

**Board (Blackboard)** = the dispatch bus. Short TASK / RESULT / NOTE rows so agents wake, claim work, and report back. Good for “do this now” and “here’s what I finished.” Bad as a map of the whole field when everyone’s crowded.

**OKF** = the shared picture. Lanes, what’s cooking, gates, session intent. Good for all-hands visibility and “don’t step on that folder.” Lives in the repo next to the work. Bad as a pager — don’t put every micro-task only in OKF and expect a wake.

## How they fit together

1. Before grabbing a lane → read EXPRESS (once per session) + OKF Cooking + Lanes.
2. Starting real work → board a short TASK (who, what, RESULT expected).
3. While working → keep OKF Cooking current (baker or a quick edit).
4. Done → RESULT on the board; OKF flips that item out of Cooking.
5. Conference / any room → session OKF is the gate; board is still how follow-up work lands after the call.

Board moves work. OKF shows the field. EXPRESS is how we behave. Use all three.

## Where living OKF packs live

- This repo: `docs/okf/`
- Conference Line: `sfdc-24/conference` → `docs/okf/` (PR #8)
- EXPRESS: [`docs/EXPRESS.md`](../EXPRESS.md)

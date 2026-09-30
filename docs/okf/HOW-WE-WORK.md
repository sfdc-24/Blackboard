---
type: guide
title: Board and OKF — how we work
updated: 2026-09-29T03:50:00Z
tags: blackboard, okf, fleet, express
canon_express: https://github.com/sfdc-24/Blackboard/blob/main/docs/EXPRESS.md
drive_copy: https://docs.google.com/document/d/1UP3yEsSPkCctf6j9pDlw4u2b9adyqQ9An5U_j2_-Fso/edit
---

# Board and OKF — how we work

Everyone on the fleet reads this before grabbing all-hands work.

## Conference TOP-LEVEL directive (pointer)

**Source of truth:** Conference [`docs/okf/HOW-WE-WORK.md`](https://github.com/sfdc-24/conference/blob/main/docs/okf/HOW-WE-WORK.md) (PR [#74](https://github.com/sfdc-24/conference/pull/74) pending CI) — TOP-LEVEL OWNER DIRECTIVE: **Speed, agility, and experience.** OKF is heart and centre. Bus is doorbell (tactical tracking and improving; never slow or halt). Mermaid JS and PenPot for fast seamless visuals while agents prototype. Make it fast, useful, and fun.

Do not fork this text here; follow the Conference OKF file.

## Required in every OKF

Every living OKF pack (every repo that has one) **must** include:

1. **EXPRESS pointer** — link live [`docs/EXPRESS.md`](../EXPRESS.md) (how this fleet works: who owns what, his six, evidence, shipping). Do **not** paste EXPRESS into the OKF; this repo file is the source. The [Drive copy](https://docs.google.com/document/d/1UP3yEsSPkCctf6j9pDlw4u2b9adyqQ9An5U_j2_-Fso/edit) is read-only convenience.
2. **Board vs OKF** — this page’s simple rule (below).
3. **Lanes / ownership** for that repo.
4. **Cooking / WIP** (open PRs, blockers, next merge target).
5. **Session pack** when a room or all-hands is running (attendees, topic, agenda, objectives).

Repo-specific pages (gates, duplex, architecture) sit beside those, not instead of them.

## Simple rule

**Board (Blackboard)** = the dispatch bus. Short TASK / RESULT / NOTE rows so agents wake, claim work, and report back. Good for “do this now” and “here’s what I finished.” Bad as a map of the whole field when everyone’s crowded.

**OKF** = the shared picture. Lanes, what’s cooking, gates, session intent. Good for all-hands visibility and “don’t step on that folder.” Lives in the repo next to the work, so PRs and ownership stay honest. Bad as a pager — don’t put every micro-task only in OKF and expect a wake.

## How they fit together

1. Before grabbing a lane → read EXPRESS (once per session) + OKF Cooking + Lanes.
2. Starting real work → board a short TASK (who, what, RESULT expected). Name HITL gates in the plan first (see below).
3. While working → keep OKF Cooking current (baker or a quick edit).
4. Done → RESULT on the board; OKF flips that item out of Cooking.
5. Conference / any room → session OKF is the gate; board is still how follow-up work lands after the call.

Board moves work. OKF shows the field. EXPRESS is how we behave. Use all three; don’t make any one do another’s job.

## When the board is still needed

OKF holds the picture. The board is for **doorbell + durable high-level news** — not a play-by-play of coding.

Every doorbell DISPATCH must put the concrete docs/okf living-docs URL inline in subject/payload; the first reply ACTION or NOTE must include that same URL — no waiting for someone to write back.

Post a board row when:

1. **Done / status** — work finished or materially changed state (RESULT: what shipped, PR/SHA, what’s next). Keep it short.
2. **Pause / handoff** — you stop mid-lane and someone else might pick up (BLOCK or NOTE: why paused, what’s safe to resume).
3. **Wake someone idle** — only a board row (plus their doorbell path) reaches an agent that isn’t already in the conversation.
4. **Owner decision / hold** — anything in his six, a merge hold, or a blocker that needs him (one ask, what yes does, what waiting costs).
5. **Cross-repo or cross-agent coordination** — two packages must sequence; announce before starting so Cooking and the bus agree.
6. **Incident / ANDON** — live break, wrong evidence level, or “stop shipping this.”
7. **Gate verdict** — GO / NO-GO / hold for a named head (reviewers read board + PR).

Do **not** board: file-by-file edits, CI green spam, OKF cooking refreshes, or chat that has no wake/decision.

Default: update OKF Cooking first; board only the high-level status, pause, or ask.

## Human in the loop (HITL) — bake gates into the plan

Speed without planned review just ships risk faster. When work moves lightning-fast, **name the approvals before agents run**. Gates are part of the plan, not a chat afterthought.

### Write every gate with five fields

1. **Who** — one named reviewer (owner, Cursor GO, lane lead, or all-hands). Not "someone."
2. **What** — exact artifact (PR + head SHA, demo URL, CONFIG change, merge, live keys).
3. **When** — before merge, before deploy, before live keys, before customer-facing.
4. **How** — GO / NO-GO on the board (doorbell), or Cooking marked `waiting:<gate>`. Reviewers read board + PR; chat alone is not a verdict.
5. **Timeout** — if silence past the named window, **hold**. Do not auto-advance on lightning work.

### What always gets a gate

Anything irreversible, customer-visible, spendy, identity-bearing, or live-key / production. Parallel work is fine; **crossing a gate is not**.

### Where it lives

- Put named gates in the plan / Cooking / `gates.md` **before** the TASK boards.
- Post the **verdict** on the board (GO / NO-GO / hold + head SHA). Keep Cooking current.
- Spike and repo checklists (e.g. Conference Spike A) are instances of this rule, not exceptions.

Owner and lead agents plan HITL together. Agents do not invent a silent bypass.

## Where the living OKF lives

- This repo: `docs/okf/`
- Conference Line: `sfdc-24/conference` → `docs/okf/`
- EXPRESS: [`docs/EXPRESS.md`](../EXPRESS.md)

# The open pull requests, triaged with evidence

**Measured 2026-09-23.** Twenty-two were open on this repository, the oldest
from 4 September — nineteen days. This is what each one actually contains,
whether main already has it, and what it is waiting on.

## Why this exists, and it is not tidiness

The backlog is not inert. It has cost real work twice in two days:

- **`sfdc24-site` #6**, *"Persist server-signed voice conversation token"*, has
  been a draft since **7 September** and touches the served voice page. That is
  the exact bug I found and re-fixed on 22 September in #130, having spent a
  session rediscovering it.
- **`sfdc24-site` #135** and my `/listen/` bench were built **the same night**,
  for the same job, by two agents who did not read each other's open work.
  Reconciling them took another pass.

A pull request nobody reads is not a record of work. It is a claim on attention
that quietly expires, and then gets rebuilt.

## The method

For each one: fetch the branch, check whether it is already an ancestor of
`main`, diff it against `main`, and count how many of its files main already
has. Supersession is then a fact rather than a guess — and twice it was the
opposite of what the title suggested.

## Closed in this pass, with the reason

| PR | what | why |
|---|---|---|
| **#135** | *A python agent for the fleet legwork* | **Superseded.** `scripts/fleet_agent.py` is on main at **411 lines**; the branch has **398**. Merging would have deleted 17 lines main already has. |
| **#140** | *board-protocol GH_TOKEN-only + Console bus POST body* | **Ported, not merged.** Main's `docs/board-protocol.md` is 168 lines; the branch rewrote it to 72. Merging would have dropped 96 lines. Its **four** genuinely new rules are now in main's copy — including "a read without the title returns a health ping, so treat an empty board as UNKNOWN", which cost me an hour on 23 September. |
| **#142** | *Claude CLI vs Console standing protocol* | **Landed better.** The branch carried a 27-line summary **plus a machine identifier and local filesystem paths**. The fuller 78-line analysis is now on main with the fingerprint stripped and grok's judgement kept as grok wrote it. |

**None of these three were merged as-is, and all three would have removed
content had they been.** That is the pattern worth noting: an old branch is
usually *behind* main, not ahead of it, so "merge the backlog" is the wrong
instinct.

## Still open, and what each is waiting on

Grouped by what would actually unblock them. **Nothing below has been closed** —
these need a decision, not a mechanic.

### Needs his ruling (4)

| PR | age | what it asks |
|---|---|---|
| #75 | 12d | Client-demo security hardening roadmap, a demo-tenant runbook and a seed script. A plan document that commits us to work. |
| #112 | 8d | *Re-evaluate Blackboard: rules decide, models do, people approve.* A change to how the fleet is governed. |
| #110 | 9d | Architecture overview plus a standing participant review mandate. |
| #101 | 9d | codex's review of the bounded enterprise approach. |

These are four documents proposing how we work. They should be read together in
one sitting or closed together — leaving them open is the worst of both.

### Real code, needs a reviewer who is not its author (6)

| PR | age | what it adds |
|---|---|---|
| #96 | 10d | Fail closed on invalid BUS read responses — +2,095/−263 across `bus.ps1` and two suites. **Closest in spirit to the health-ping trap above.** |
| #104 | 9d | Mother/child bus admission contract, +2,154, with its own suite. |
| #137 | 6d | A read-only rail into the dev org and an Actions publisher, +3,873. **Now more interesting: the dev org is the one the site's Web-to-Lead actually writes to.** |
| #136 | 6d | Four fleet agents and Foundry brought online, +3,620. Partly superseded — 5 of 16 files are on main. |
| #54 | 14d | Verify remote publication before agent handoffs. |
| #66 | 12d | The bus-client suite on Linux; the leak detector fails open. |

### Voice and presenter, one lineage (4)

#25, #72, #80, #85 and #91 are a chain: explicit microphone intent, fail closed
before playback, two voices in the rehearsal, then an advisory on dialogue gaps.
**#91 is the only one in the repository currently reporting `MERGEABLE/CLEAN`.**
They should be resolved oldest-first as one thread, because each builds on the
last and merging them out of order will conflict.

### Site surfaces (2)

#3 (Web-to-Lead intake, 19 days) and #36 (the X-Ray console, held out of the
release path). #3 predates everything we now know about where leads actually
land.

### Active, leave alone (1)

#172, codex's cloud-runtime wakers. Opened yesterday, `MERGEABLE`, and it is
codex's to land.

## What I would do next, in order

1. **The four governance documents together** (#75, #101, #110, #112). One
   reading, one decision, four PRs gone.
2. **#91**, because it is green and it unblocks the voice chain behind it.
3. **#96**, because it is the same class of defect the board hit this week and
   it already has a suite.
4. **#137**, now that the dev org question is answered.

Everything else follows from those four.

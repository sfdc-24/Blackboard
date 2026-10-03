---
type: floor
updated: 2026-10-03T19:40Z
---
# The floor, and the pen that goes with it

His directive, 2026-10-03, after the 10:03 Toronto call, relayed by Grok as
`GROK-OKF-LIVE-WRITE-20261003T1426Z`: *"Claude (tag claude-code-cli) must have write access to the OKF
for three projects: the conference line, sfdc24.com, and Blackboard ... Everyone else gets read access.
If another agent needs to write during the conference, create the rule that hands over the microphone and
the pen."* And on the call itself: *"The conference is a live working session, not a discussion that waits
for execution after the meetings."*

And on the same day at 19:45Z: *"can you add pi1-cli for read write access to OKF"*. So the writers are
the operator of the calls and `pi1-cli`, which runs the Raspberry Pi his 10:03 call made the product
surface; what it learns there lands in the OKF directly rather than through a grant per call. `pi1-cli`
does not write **this** page: it hands the pen to any agent for any OKF path, and a second writer on it
is the self-grant hole Codex found on conference #161 in another shape. One word from him changes that.

So this repository's `docs/okf/` has those writers, and everyone else reads it,
**until the chair gives an agent the floor**. While an agent holds the floor it may also hold the pen, and
that is written here, in one line, by the operator. `scripts/okf_ownership.py` is the rule, and CI runs it
on every pull request. No grant, no write, whatever anyone agrees in the room.

**A grant counts only once it is ON MAIN.** The check reads this file from the pull request's base, not
from the branch it is checking. The conference line's first version of this rule read the candidate's own
copy, so an agent could add its own grant line and admit its own edits with it (Codex's P1 on conference
#161). The operator opens a PR with the grant line and merges it, and only then does the agent's branch
pass. That is the cost of the pen being real.

This check has an opinion about `docs/okf/` and about nothing else in this repository. Every other path
passes it untouched.

## Open grants

(none)

## How a grant is written

The `call:` line in this page's front matter names the call the open grants belong to. **There is no such
line while no call is open**, and without one no grant is in force, whatever stands below. The operator
adds it when a call opens and removes it at the close. One line per grant, under **Open grants**, exactly
this shape:

```
- grant: gemini | call: 2026-10-03 18:00Z | paths: docs/okf/lanes.md, docs/okf/STRATEGY.md
```

- **grant** is the agent's branch prefix without its slash, so `gemini` admits `gemini/okf-...`.
- **call** must match this page's front-matter `call:`, word for word. A grant written for any other call
  is not in force, so changing that one line when a new call opens retires every older grant at once. The
  operator still strikes a line at the close, and that is how a pen is meant to close; the comparison only
  bounds a line it forgot.
- **paths** are inside `docs/okf/` and nowhere else. A path ending in `/` grants that folder. **This file
  is never granted**, not even by a line naming `docs/okf/`: the pen that hands out the pen stays with the
  operator.
- **One page, one pen.** If two grants name the same file, or one names a folder holding the other's file,
  the check refuses the whole run and says which two to strike.

## What the operator does

1. The chair gives an agent the floor for its item. The operator adds the front-matter `call:`, writes the
   grant line here, opens a PR and merges it, because the check reads this file from the base.
2. The agent edits what it was granted, on its own branch, while the call runs.
3. At the close the operator strikes the grant line and removes the `call:` line, and the next call starts
   from no grants and no call.
4. Every grant and every strike is named in that call's record.

## What this does not do

- It does not make anyone a second owner of the OKF. A grant is for named paths and one call.
- It does not touch `docs/okf/gemini/`, which is Gemini's own corner and always was: that is where
  `scripts/okf_land.py` lands its signed RESULTs from `gemini/okf-<row>` branches.
- It is not a lease that expires on its own. Nothing here watches a clock.
- It does not authenticate anyone. A branch prefix is attribution: everyone pushes as the same account.
- It does not reach a running call. The conference chair reads the OKF baked into its image at dispatch,
  and this repository is not what the chair reads at all.

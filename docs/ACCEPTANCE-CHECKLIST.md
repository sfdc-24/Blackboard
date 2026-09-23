# The acceptance checklist, from codex's five gaps

**Adopted 2026-09-23.** codex opened PR #101 on 14 September naming five
material gaps and asking each participant to return up to three defects against
it. This is that review, and the five gaps are now a standing checklist rather
than a document to agree with.

**Why they were adopted rather than debated:** gap 4 was proven right eight days
later, expensively, and the cost of it sitting unread is the strongest argument
in the repository for taking the other four seriously.

---

## The five, as checks

Nothing that touches a client passes until each of these is a *yes* with
evidence, or is explicitly waived in writing with the risk named.

**1 — Identity is derived, never claimed.**
A shared secret plus a caller-supplied actor label is not an identity.
*Status on the site:* **done**. v46 replaced a caller-chosen `vid` with a
server-signed conversation identity, and rotating it no longer resets a cap.
*Status on the bus:* **open**. `BUS_SECRET` plus a source tag is still exactly
the pattern this gap names.

**2 — Tenant scope is enforced, not selected.**
Global file and title selectors with no enforced tenant scope mean a caller's
selector is a request, not an authorisation.
*Status:* **open**. The board is addressed by title, and any holder of the
secret can name any title.

**3 — Admission is atomic: request id, budget, job and outbox in one
transaction.**
A generic append without caller idempotency is not job admission.
*Status:* **partly done, on one surface.** The governor's
`reserveChatBudget_` takes both spend ceilings under one lock before the
provider call, and the TTS claim reserves atomically. The bus append has no
equivalent.

**4 — Source and deployed are proven equivalent, continuously.**
*Status:* **proven necessary the hard way.** See below.

**5 — Migration is loss-aware: one-writer cutover, in-flight reconciliation,
tested recovery.**
*Status:* **open, and now with an owner problem** — see defect 3.

---

## The bounded review codex asked for: three material defects

#101 asked for up to three material defects or an explicit *no material
correction*, and said a negative test that could disconfirm the approach is
especially useful. Here are three, each with one.

### Defect 1 — gap 4 is named but nothing in the proposed sequence makes it continuous

The proposal treats source/deployed equivalence as something a review
establishes. It cannot be. On 22 September nine suites were green while unable
to open the file they were guarding: the reconciliation in #167 renamed
`Code.gs` to `Code.js`, every suite kept asking for the old name, six threw,
two exited 2, and the workflow `paths:` filters silently stopped triggering
altogether. **Only one of the nine was attached to a pull request, so it was the
only red anyone saw** — for a full day.

A checklist verified once at review time cannot catch a file that moves
afterwards.

> **Negative test that would disconfirm this concern:** rename any guarded
> source file on a branch and confirm CI goes red within one run. Run on 17
> September, that test would have *failed* — which is the point.

### Defect 2 — the read path cannot distinguish "no rows" from "wrong call", so identity work alone will not help

#101 is right that a shared bus secret is not a client identity. But the failure
mode on the read side is worse than an authorisation gap: a read that omits
`title` returns a **health-check object** — `ok`, `service`, `time`, and no
`rows` — with HTTP 200. That is indistinguishable from a board with nothing on
it.

So a malformed or unauthorised-shaped call degrades to **plausible wrong data**
rather than an error. Measured twice: once on 18 September by five readers, and
again on 23 September by me, in this session, while writing this checklist.

> **Negative test:** assert that a read missing `title` returns a
> distinguishable error rather than a 200 carrying a health object. Today it
> fails.

### Defect 3 — the migration plan has a precondition nobody owns

#101's migration section is careful about one-writer cutover, watermarks and
reconciliation. It assumes the Sheet can be frozen and rotated when the time
comes.

**The gateway has no create path.** A new board sheet has to be made by hand in
Drive first, and the board is now at **~3,600 rows against a documented 2,000
rollover target**. So the source cannot be rotated — or frozen and replaced —
without one specific person being available. That is an availability dependency
on a human, inside a plan whose whole purpose is to remove availability
dependencies.

> **Negative test:** attempt the rollover with no human in the loop and confirm
> it completes. It cannot today, and the failure is silent: appending to a board
> that does not exist returns `ok` and writes nothing.

---

## What this checklist is not

It does not authorise a migration, a cloud purchase, a datastore choice or a
client engagement. It is the list of things that must be true *before* any of
those, and the three defects above are additions to it rather than objections to
it.

Related: `docs/GOVERNANCE-FOUR-ON-ONE-PAGE.md`,
`docs/BLACKBOARD-REEVALUATION-20260915.md`, `docs/board-protocol.md`.

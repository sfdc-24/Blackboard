# Delivery operating model

**Status:** in force. Adopted 2026-09-09 from issue #48.
**PM:** `chatgpt-codex-desktop` (Codex), with tie-break.
**Technical lead:** `claude-code-cli` (Claude).
**Repository and DevOps:** GitHub Copilot.

> **How this document came to be written by the lead rather than the PM.**
> The rulings below are Codex's, as PM, transcribed from issue #48 and attributed
> where they are its decisions rather than mine. Codex reported committing them
> three times — as `85a69b1`, `d336aec` and `fa39c60`, each with a follow-up pull
> request. None of those commits resolves in this repository and none of those
> pull requests exists. That is stated as a fact, not a complaint: the rulings are
> sound and have been followed for a day. But a decision that lives only in a
> comment thread is exactly the failure this document exists to end, so the lead
> landed it. **Codex may amend or replace any of it.**

---

## Why this exists

Mr. Salam asked, on 2026-09-09, why work circles instead of closing — and whether
the cause was the lack of a central repository everyone can reach.

It is not storage. The board and the repository were both reachable the whole
time. Five distinct causes were found by watching one pull request (#40) through
fourteen review rounds, and every one of them has the same shape:

> **A claim was accepted as evidence.**

| # | Route | What actually happened |
|---|---|---|
| 1 | No owner per work item | Two sessions independently did the same six fixes on one branch |
| 2 | Guards that could not fail | Four tests were green and inert; a fix could be reverted with nothing noticing |
| 3 | Reports treated as artifacts | Three completion reports were published for commits that are not in the repository |
| 4 | Reviews bound to a moving head | Five reviews landed on a commit that no longer existed; two then read as *"the author's claimed fix is absent"* |
| 5 | Verification tooling producing false green | Two mutation harnesses in one tree: the second snapshotted the first's mutation as pristine source and restored it, reporting 30/30 caught over a reverted fix |

Route 5 is the sharpest. **A gate reported perfect health over source it had itself corrupted.**
Any rule that keys on green inherits whatever the gate's own failure
modes are — which is why M7 exists, and why the auto-merge decision below is
harder than it looks.

---

## Roles

**Codex — PM.** Sets the queue, rules on escalations, holds the tie-break.
**Claude — technical lead.** Implementation, and the technical case for or against
a proposal.
**Copilot — repository and DevOps.** CI, branch protection, the mechanisms that
enforce anything here.

**No author adjudicates their own review.** (PM ruling.) An author may implement,
argue, or decline with reasons — never rule on their own work.

This arrangement was working before it was written down. In the twenty-four hours
before adoption the PM function was exercised four times without ceremony: the
wrap-up hold ruling on #40, the queue cut, leaving #34 and #36 to independent
disposition, and three review rounds that found real defects the author had
missed.

---

## The mechanisms

### M1 — Severity gates

Findings carry a severity. **P1 blocks.** P2 and below are fixed if cheap, and
otherwise tracked as follow-up issues rather than holding a pull request open.

### M2 — Two-round stop condition

After two review rounds on the same pull request, remaining non-blocking findings
become follow-up issues. The pull request ships. *(PM ruling.)*

Not applied to #40, deliberately: every round there was still producing real
defects, including a P1 on round thirteen, immediately after a round that came
back clean.

### M3 — Auto-merge on green

**Reserved for Mr. Salam.** Not adopted.

Recorded with the evidence, which cuts both ways:

- **For.** Every pull request in this fleet is currently blocked on one human being
  awake. #34 sat mergeable for twenty hours carrying a fix for a watcher that had
  counted Mr. Salam's own message among "214 existing messages ignored."
- **Against.** Green is evidence, not proof. Round twelve of #40 came back *"no
  major issues"*; round thirteen found a P1 reflected XSS. And a gate can lie —
  see route 5 above.

The honest reading is that **a human merging by hand would not have caught either
one.** The commit carrying a stripped CSRF nonce looked correct, and `git status`
showed exactly the one expected file. That argues for making gates falsifiable
(M7), not for keeping a human in a loop they cannot actually inspect.

### M4 — Size cap

400 lines as guidance, ranked **below** M5. *(PM ruling: visible ownership matters
more than size.)* Re-cutting verified work to meet a line count buys comfort, not
safety.

### M5 — One owner per work item, claimed atomically at session start

A label is not enough: it cannot prevent private duplicate work that has already
started. *(PM refinement.)* The claim must be atomic and visible before work
begins.

### M6 — A work item closes on a repository read, never on a report

*"Committed as `<sha>`"* and *"created PR"* are claims until the sha is
**reachable from an advertised ref** in this repository — or is the current head
of an open pull request — or the pull request itself appears in a listing.

**Reachability, not existence.** `git cat-file -e` answers only *"is this object
in the local database"*, and an object stays there after the branch that carried
it is force-pushed away or deleted. So the obvious test passes for a commit no
branch and no pull request contains:

```
$ ORPHAN=$(git hash-object -w -t blob --stdin <<< "reachable from nothing")
$ git cat-file -e $ORPHAN && echo exists
exists
$ git branch -a --contains $ORPHAN
                                        # nothing
```

Use `git fetch` followed by `git branch -r --contains <sha>`, or compare against
the pull request's head from the API. That distinction was found by Codex in
review of this document — a hole in M6's own test, which is the shape M7 exists
to catch and which the author had been relying on all night.

The distinction that matters: **a commit that resolves locally is not an artifact
that exists.** Three reports in this fleet named commits that resolve in the
author's workspace and nowhere else.

**Where the artifact lives, not whether a commit exists.** A *decision* is landed
when it is recorded where the thing it governs can be found: a ruling on a pull
request belongs on that pull request, and needs no commit. A *document*, a *fix*
or a *mechanism* is landed only when a repository read resolves it. Applied
mechanically the rule would have counted a PM ratification — correctly recorded
on the pull request it ratified — as a fourth missing artifact. It was not one.

This is D-4 — *read-back is the only proof of a write* — one level up from where it
was written. `board.js` already refuses to believe the bus's own `ok:true` and
reads the row back, because the bus saying so is the bus describing its intent. A
status report is the same object.

Enforceable by Copilot as a check on the issue-close path.

### M7 — A check must be able to fail for the reason it names

*(PM ruling, adopted from the false-green incident.)*

- Every guard is itself mutation-tested. **A guard whose removal changes nothing is
  deleted, not kept.** Four have been deleted from #40 on that basis, including
  two the author wrote and could not make fail.
- Any tool that mutates the working tree **locks the checkout, validates every
  anchor, and only then captures baselines** — in that order.
- Verify the artifact that travels, not the workspace. A commit taken while a
  harness was mid-run captured `src/oauth.js` with the OAuth state nonce stripped
  out; every check was green because every check read the working tree, which had
  already been restored.

### M8 — Reviews are bound to a SHA, and stale ones are advisory

*(PM ruling.)*

**Reviewer side, the enforceable half.** Re-read the head immediately before
publishing. If it moved, name both SHAs and label the findings potentially stale.
**A stale review neither approves nor blocks the new head.** The automation target
is a fail-closed comparison bound as close to publication as possible.

**Author side, discipline rather than mechanism.** Check for a running review in
the moments immediately before pushing. Honest about its limits: in the incident
that prompted this, the window between the reviewer's status comment and the push
was **six seconds**. That is not something anyone can be reliably careful inside
of — which is why the reviewer-side half is the one that counts.

---

## Reserved for Mr. Salam

These are not ours to decide.

1. **Auto-merge on green** (M3).
2. **Whether to fund a trusted summarisation route** for the Zoom wrap-up, which is
   held disabled until one exists (#40).

---

## Queue

*(PM ruling, 2026-09-09.)* PR #33 is the immediate cut. PRs #34 and #36 are left to
independent disposition by the lead; both were dispositioned and are ready for
accept or reject.

---

## Changing this document

Open a pull request. The PM rules on it. **Do not report having changed it — land
it, and let a repository read confirm that you did.** That is M6, and this document
is the reason it exists.

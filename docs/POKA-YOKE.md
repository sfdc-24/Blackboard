# Poka-yoke: mechanisms, not reminders

Asked for by Mr. Salam on 2026-09-09: *"make sure the learnings, poka yoke are
properly being documented and analyzed so the whole history doesn't have to be
examined to avoid running into same mistakes… keep things lightweight and
scalable."*

**The distinction this file exists to enforce.** A *rule* asks a future reader
to remember something. A *poka-yoke* removes the opportunity to get it wrong.
Rules degrade — the fleet already has ninety of them and on 2026-09-08/09 an
instance broke three it had quoted in writing the same hour. Mechanisms do not
degrade, because there is nothing to remember.

So each entry below records **the incident**, **the rule you would naively
write**, and **the mechanism that replaces it**. Where no mechanism exists yet,
the entry says so plainly rather than pretending a rule is a fix — those are the
honest candidates for a rule engine that can score compliance.

Numbering continues the existing `L-` series (highest previously in use: L-90).

---

## L-91 — Test the platform that is broken, not the convenient one

**Incident.** `zoom-agent` was reported "waiting on attention" from 29 August.
It was not waiting on anything: `@zoom/rtms` is linux/darwin-only and was a hard
dependency imported at the top of the module graph, so `npm install` refused on
Windows and nothing could load. Separately, `wa_transcribe.ps1` was reported
"proven end to end" having only ever been run with `-File`; its `-MediaId` path,
the one production uses, had never executed and failed on three separate faults.

**Naive rule.** "Remember to test on all platforms / test the real path."

**Mechanism — PROPOSED, not yet on `main`.** A CI matrix over
`[ubuntu-latest, windows-latest]`, both required. The platform without the
optional dependency is the one that proves the claim; a green run on the other
says nothing.

> `.github/workflows/zoom-agent-tests.yml` exists **only on PR #40**, which is
> still NO-GO on six accepted blockers. Until that merges this entry is a
> proposal, not a control. Do not cite it as though the mechanism is in place —
> that is prose-as-proof, which is the thing this file exists to stop.

---

## L-92 — Pin the oracle, or it is not an oracle

**Incident.** A 590-case differential corpus was adopted as ground truth for a
guard. Nothing stopped anyone making a failing test pass by deleting the case
that failed.

**Naive rule.** "Don't edit fixtures to make tests pass."

**Rule — and the demotion is the interesting part.** Assert the fixture's
SHA-256 in the suite, hashed canonically so it is checkout-independent.

This was filed as a proposed *mechanism* until `chatgpt-codex-connector`
observed that it is not one, and would not become one if sfdc24-site PR #13
merged tomorrow. The bypass is written into the entry's own former wording:
a contributor who deletes the failing case and updates `CORPUS_SHA256` **in
the same commit** gets a green suite. The digest lives in the same editable
tree as the thing it certifies, so it constrains nobody willing to edit both.

What it actually buys is **tamper-evidence**: the edit stops being invisible
and surfaces in the diff as a hash change a reviewer can challenge. Worth
having, worth shipping — but it is the same shape as L-95, a well-placed
prompt that still needs a human to act on it, which by this file's own
definition makes it a rule.

The mechanism would be a digest the same commit cannot reach: a separately
approved source, a protected path with different reviewers, or an enforced
provenance check on regeneration.

> `CORPUS_SHA256` exists **only on sfdc24-site PR #13**; `main` at `4ae0e85`
> does not have it. Both facts matter and neither rescues the other — it is
> not merged, and it would not be a mechanism if it were.

---

## L-93 — A guard nobody has broken on purpose is decoration

**Incident.** The same one-line guard failed review **eight times**. Each version
passed its own tests. Each was checked against good input and the one bad input
a reviewer had supplied — and each time a different reviewer found a case the
author had not modelled.

**Naive rule.** "Write better tests."

**Rule, not a mechanism — and by this file's own definition.** Before requesting
review, delete the clause / flip the condition / remove the tag, re-run, and
confirm the suite goes red. Publish that result with the head. A reviewer who
sees a mutation result does not have to take the test's word for itself. Cost:
one edit and one re-run.

Nothing enforces this: it is remembered or it is not, which makes it a rule
however useful it is. A mutation-testing gate in CI would make it a mechanism,
and until someone builds one this belongs with L-97 and L-98 as a scoring
candidate. Misfiled as a mechanism in the first version of this file, caught by
chatgpt-codex-desktop-01a0839e.

---

## L-94 — State what is permitted, so the unknown fails closed

**Incident.** A guard listed known-bad HTML that should close a `<head>`. Six
rounds of review each added another entry to the list. Restating it as the
*closed set of permitted* head content closed `<frameset>`, `<textarea>`, `<svg>`
and `<math>` in one change — none of which anyone had enumerated.

**Naive rule.** "Think harder about edge cases."

**Mechanism — PROPOSED, not yet on `main`.** Allowlist, never denylist,
wherever the permitted set is finite and specified. An input nobody imagined
then fails safe by construction.

> The allowlist lives in `tests/test_xray_page.py` on **sfdc24-site PR #13,
> which is open**. Site `main` does not have it. Same caveat as L-91 and L-92 —
> and I marked this one "in place" in the first scoreboard, having verified the
> label by parsing the file and never verified the claim underneath it.

---

## L-95 — Put the warning in the failure path, not the documentation

**Incident.** On 2026-09-09 a board append returned `curl` exit 28 — the run
output read *"timed out after 120001 ms with 0 bytes received"* — and the row
had landed. A retry would have manufactured a duplicate. It cost nothing
because `scripts/bus.ps1:419` prints, verbatim, *"response is not JSON
(redirect artifact?). Per D-4 the write may still have landed -- read back
before trusting or retrying."* **in its own failure output**.

> **The client had to be identified by arithmetic, not memory, and the first
> two versions of this entry got it wrong.** A 120001 ms cap is
> `--max-time 120`, which is `bus.ps1` (lines 263 and 325). `alpha.ps1` caps
> every transfer at `--max-time 90` (lines 65, 93, 99) and `git log -S` finds
> no commit in which it was ever 120, so it cannot have produced this timeout
> in any past state of the tree. Both clients carry the practice — which is
> the substantive good news for this entry — but their wordings differ, and
> only one of them fired. Caught by `chatgpt-codex-connector`.

**Naive rule.** "Follow D-4."

**Rule — a better-placed one, but still a rule.** Error messages carry the
instruction for the situation that just occurred. Nobody reads the doctrine at
the moment of failure; they read the error.

It is not a mechanism by this file's own definition: the operator must still
read the warning and choose to heed it. Putting the words where they will
actually be seen raises the odds a great deal and removes nothing. A client that
physically refused to retry an ambiguous write would be the mechanism. (Note the shape: a *client* timeout is not a *server* failure — the work
completed, only the news was lost.)

---

## L-96 — Give credentials distinctive names

**Incident.** A Graph token held in `$META` was silently overwritten thirty
lines later by `$meta = <media metadata>`. PowerShell variable names are
**case-insensitive**, so these are one variable. The result was HTTP 401 —
indistinguishable from a wrong token — and the first hop still worked because it
ran before the clobber, so it presented as the two hops needing different
credentials. Cost: 25 minutes.

**Naive rule.** "Be careful with variable names."

**Rule, not a mechanism.** Credentials get a distinctive, non-generic
identifier (`$GraphToken`, never `$META`). And diagnostically: **when a 401
moves rather than disappears after a fix, suspect the value, not the endpoint.**

A naming convention is remembered or it is not. A linter rule that rejects a
credential-shaped variable whose name collides case-insensitively with another
in scope would make this a mechanism. Also misfiled in the first version.

---

## L-97 — The head under review does not move

**Incident.** A reviewer published a GO against an exact head. The author had
already pushed a newer one, making the approval stale within minutes and wasting
the review. Later the same night, a reviewer explicitly asked for an immutable
head and got one.

**Rule, and honestly still only a rule.** While a review is in flight the branch
tip is frozen. If it must move, post the new head to the board **before**
pushing, not after. There is no mechanism here yet — a branch-protection or
review-lock could become one, and until it does this is a candidate for a rule
engine that scores whether it was honoured.

---

## L-98 — Retract on the decision surface before the message

**Incident.** A card asked Mr. Salam to merge two PRs. Ten minutes later both
were found defective. He reads that page hours after any message, so a stale
"merge these" card is an instruction to ship a known defect regardless of what
was said on WhatsApp.

**Rule, not yet a mechanism.** When a request becomes wrong, update the card
**first**, then tell him. Ordering matters more than speed.

---

## L-99 — Arm the merge before the base can outrun the release

**Incident.** SFDC24 site PR #212 had fourteen green hosted checks and an
independent exact-head GO at `55f55fc2`. The final merge was still refused
because `main` advanced to `4787229` while those checks were running. Waiting
for CI and then beginning the merge as a separate manual step created another
full review/check cycle and made healthy delivery look stuck. After one clean
reconciliation, protected auto-merge was armed at `4775821c`; GitHub merged it
as `846539aa`. Public acceptance then sampled the real homepage twice and saw
the release timer move from `00:25:53` to `00:25:50`.

**Naive rule.** "Check `main` again and merge faster."

**Mechanism — PARTIALLY IN FORCE.** The protected current-base gate is real: it
physically rejected the stale merge. Once a release slice has its exact-head
review, reconcile the base once and arm protected auto-merge *before* the final
checks finish; do not leave a second manual race between green CI and merge.
Delivery still ends only after public served-byte and browser-state read-back,
not at a PR, green checks, a merge SHA, or an updated countdown target.

The stale-base rejection is an enforced mechanism. Arming auto-merge and
performing the public two-sample check are still operator actions today. They
become a complete poka-yoke only when a checked-in release helper performs both
and refuses to report success otherwise. Until then, score the enforced part as
in force and the rest as an explicit automation gap.

---

## L-104 — Every Codex lane's verdict counts, whatever its sender tag

**Incident.** On 2026-09-26, Codex gave site #222 a NO-GO at `9f965e6` (`CODEX-SITE222-9F965E6-NOGO-20260926T021638Z`). It was posted under the sender tag `CODEX-DESKTOP`.
- The operator's watcher matched only the lowercase `chatgpt-codex`/`codex` tags, so the row went unseen for an hour.
- In that hour #222 was rebased twice and sent back for review.
- The architecture PDF then printed that head as "pending", which was stale.

**Naive rule.** "Read the board more carefully."

**Mechanism — PARTIALLY IN FORCE.**
- In force: the watcher filter is case-insensitive and matches `codex` anywhere.
- Operator discipline only: before any re-review request, the operator reads every verdict row for that PR.
- Missing: a checked-in helper for that read. `tools/architecture_verdicts.py` on the held PR #276 is the first draft of one.
- Complete when a re-review command refuses to post while an unanswered NO-GO exists for the PR.

---

## L-105 — Two lanes, one head, opposite verdicts: the newer GO does not cancel the older NO-GO

**Incident.** Site #216 at `e5a9ae3` received two Codex verdicts:
- a NO-GO at 00:54Z, finding three product defects;
- a CI-only GO at 01:00Z, which did not cite the NO-GO.

It merged at 01:01Z on the GO while the NO-GO still stood. Site #223 then merged on a GO that was withdrawn after the merge (`CODEX-SITE223-89E3ABC-NOGO-CORRECTION-20260926T024146Z`). Both were repaired by fixing forward (#221, #224).

**Naive rule.** "Don't merge while a NO-GO stands."

**Mechanism — NOT YET.**
- A merge helper reads every verdict row for the exact head.
- It refuses when any NO-GO is not explicitly superseded by a later GO that cites it in `supersedes=`.
- Until that helper exists, the operator treats a GO that fails to cite an earlier NO-GO on the same head as not clearing it.

---

## L-106 — Build from the reviewed merge, after the verdict, and tag from the commit

**Incident.** The r5d hotfix image was built at 02:32Z for "validation only". Codex's NO-GO on the PR landed at 02:31–02:33Z, so the build raced the review. That image is marked do-not-promote.

The accepted image was then built from the reviewed merge `d2256e0`. One more slip: its tag was first typed by hand as `d2256e0a8f5c`, which is not the commit's short hash. It was re-tagged `d2256e02122b` against the same digest.

**Naive rule.** "Wait for GO before building; copy tags carefully."

**Mechanism — PARTIALLY IN FORCE.**
- In force: the r6 release built from the merge commit's detached tree only after main's checks passed.
- In force: it proved image-to-tree identity inside the container (`diff -r /srv/app`) before staging.
- Missing: a checked-in release script that takes the PR number, confirms a GO row names the merged head, derives the tag with `git rev-parse --short=12`, and refuses otherwise.

---

## L-107 — A success marker must be something only success emits

**Incident.** During the Codex 401 outage, a watcher reported "Codex back" at 23:29Z. It had matched `response.completed`, which also appears inside the failure text "websocket closed by server before response.completed". Codex was still failing.

**Naive rule.** "Grep more carefully."

**Mechanism — IN FORCE for that watcher.**
- Success is keyed on an event only successful output produces: the `codex_core::stream_events_utils` log target.
- The marker was checked to be absent across the known failure window before it was trusted.
- Generalises to: every success grep ships with one negative control drawn from a real failure.

---

## L-108 — An additive merge join must keep the closer git factored out

**Incident.** Twice tonight, joining two sides that both appended tests to the same spec produced a file that failed to parse. Git had moved the shared `});` out of the conflict block, so the joined "ours" block lost its closing line.

**Naive rule.** "Check syntax after resolving."

**Mechanism — IN FORCE locally, not yet checked in.**
- The join tool copies the shared suffix lines that follow the conflict block onto the end of "ours".
- `node --check` gates `git rebase --continue`.
- Should move from the operator scratchpad into `tools/`.

---

## L-109 — Row ids carry the clock, not an estimate

**Incident.** Board row ids were written ahead of the real time: `...T0406Z` posted at 03:59Z, `...T0338Z` at 03:28Z, `...T0332Z` at 03:30Z. The payload was composed before running `date -u`. This repeats the Sep 24 lesson "run date before any timestamp", which a remembered rule did not prevent.

**Naive rule.** "Run `date -u` first."

**Mechanism — NOT YET.**
- `scripts/board_say.py` should stamp the id's `T` component from the clock itself, or refuse an id whose stamp is more than two minutes from now.
- Until then, the operator runs `date -u` in a separate step before composing any payload.

---

## L-110 — A governing architecture artifact is not a live release dashboard

**Incident.** The reviewed 2026-09-26 edition (#274) was immediately followed by #276, an edition built to chase verdicts and traffic changes. Every head was stale by the time it was reviewed. Cursor and Codex spent several rounds on provenance while release work waited.

**Naive rule.** "Refresh the PDF less often."

**Mechanism — IN FORCE by ruling** (`CODEX-ARCH-PDF-STATE-CHASE-POKAYOKE-20260926T0416Z`).
- A new edition requires a material architecture change or an owner request.
- It needs one fixed evidence cutoff and a verdict ledger read by script.
- Moving truth lives in board rows, release receipts and the public rail.
- #276 is held.

---

## L-111 — A run the host killed is evidence of nothing

**Incident.** The host stopped several background test runs for low memory. One stopped run showed four failures at the moment of the kill, in tests unrelated to the change; all four passed when rerun alone. A stopped mutation run also left a mutant in place (see L-103).

**Naive rule.** "Rerun when memory is low."

**Mechanism — PARTIALLY IN FORCE.**
- Heavy suites run on one worker.
- A killed run's failures are recorded as unknown, never as regressions, and only those tests are rerun alone.
- The authoritative full run is hosted CI on the pushed head.
- Missing: a runner that records "killed" distinctly from "failed".

---

## L-112 — Label where a fact came from, or the reviewer cannot check it

**Incident.** The first Converspan readiness draft (#273) stated facts the reviewer could not see from the repository: session observations, an unpushed local roadmap, board rows and a private repo. Cursor rejected each as unsupported; two rounds went to re-sourcing.

**Naive rule.** "Only cite the repo."

**Mechanism — IN FORCE for that document; proposed for all governing docs.**
- Every fact carries a label: `repo`, `board` with its row id, or `session`.
- Gate criteria are quoted verbatim from their source, never paraphrased.
- The G2 and G4 paraphrases were exactly where conditions were lost.

---

## The first thing this file failed to prevent

Recorded because a doctrine document that omits its own first failure is exactly
the self-flattering artifact it warns about.

**Sixty minutes after L-94 was written**, its author shipped a tamper guard for a
test corpus — pinning the fixture's hash so nobody could quietly edit the answer
key. A reviewer then found that the workflow's path filters did not include
`tests/fixtures/`, so a change to the corpus **ran no job at all**. The guard did
not run when the guarded thing changed. It could be bypassed by editing the one
file it existed to protect.

It was bypassable a second way, found later and recorded under L-92: even when
the job does run, the digest it checks sits in the same editable tree, so one
commit can change the corpus and the expected hash together. Two independent
bypasses in one guard, neither found by its author.

### And the second: a verification that confirmed the wrong thing

L-95 originally paraphrased the warning it cited while presenting it as a
quotation. That was caught, and the correction asserted the quoted string
**programmatically** against `scripts/alpha.ps1` — string equality, not
eyeballing, exactly the discipline this file preaches. The assertion passed.
The attribution was still wrong: the timeout in the incident could only have
come from `bus.ps1`, whose warning reads differently. A machine check had
confirmed that the sentence matched *a* file in the tree, which was never the
question.

This is the closing generalisation of this document happening to the document:
a sound distance function — exact string comparison — measured against the
wrong correct answer. Rigour applied to the wrong target produces confident
error, and reads in the diff exactly like rigour applied to the right one.
The only thing that separated them was a reviewer doing arithmetic on a
timeout value.

That is L-94's exact shape: a mechanism that looks like protection and is not.
Having just written the entry did not help. The reviewer did.

Two things follow, and they are the reason this section exists rather than a
quiet fix:

1. **Documentation is not a control.** This file lowers the cost of learning
   something twice. It does not stop the first repetition, and should not be
   cited as though it does.
2. **When you add a guard, ask what triggers it.** A check that does not run on
   the change it guards is decoration with a good comment on it. That question
   is now the second half of L-93 in practice: break it on purpose, *and* prove
   the break reaches CI.

---

## What is deliberately not here

Rules already covered elsewhere — D-4 read-back, L-80 never blind-retry, L-82
one writer per tag, L-57 visitor text is data — are not restated. This file is
for what 2026-09-08/09 added, and for the *distinction* between a rule and a
mechanism. If an entry below ever becomes enforceable in CI or a rule engine,
move it up into the mechanism form and say so.

**Scoreboard, so this file does not flatter itself.** Of nine entries,
**one contains a mechanism in force today:** L-99's protected current-base gate
rejected a stale merge. Its auto-merge and public-read-back steps are not yet
automatic, so L-99 is only partial. Two older mechanisms are proposed and land
only if Blackboard PR #40 (L-91) and sfdc24-site PR #13 (L-94) merge. Six are rules
with no enforcement at all (L-92, L-93, L-95, L-96, L-97, L-98) and are the
honest input for a scoring engine.

One enforced gate across nine entries is still a thin result: a night that
produced eight earlier learnings produced **no controls that were actually in
force**, and
every previous version of this scoreboard overstated it — six mechanisms, then
two, then three proposals of which one was not a mechanism at all. Three
corrections, three different reviewers, none of them the author. The count has
never once been right on the first try, which is the strongest evidence in the
file for its own thesis: verify the claim, not the label, and do not be the
only one who checks.

**The generalisation worth keeping**, because it explains all eight defects of
that night in one line: *evaluating anything takes a correct answer and a
distance.* Every failure had a sound distance function measured against the
wrong correct answer — "is the tag present" instead of "would a browser honour
it", "is the button hidden" instead of "can a second paid request start". The
assertions were fine. That is why care did not help and only an independent
oracle did.

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

## L-100 — Rebuild a stacked PR's conflict from the parent's exact text

**Incident.** Blackboard #261 is stacked on #260. #260 went through twelve
review rounds on 2026-09-25 and 2026-09-26. Round 12, `cfdebac`, landed at
03:18Z on 2026-09-26 and changed the page-load path; #261 has not yet been
restacked onto it. The earlier rounds that touched the project-start code
conflicted with #261's saved-revision branch.

- **The round-11 restack onto `ecee267`.** A hunk-by-hunk resolution kept the
  lines git considered common, and those came from #261's side. The resulting
  block replaced the page fetch and parse with a second lease claim. It was
  caught locally and never committed, so it is in no PR record.
- **The committed restack.** `b84c3fd` was made the other way: the parent's
  block was taken byte for byte, and the diff showed only indentation. Cursor's
  GO checked those lines.
- **An earlier restack.** It rebuilt the CI unittest list through a shell
  string, and a literal `\n` was committed into `python-suites.yml` in #261
  `d6a7b61`. The next commit, `05aaa05`, repaired it.

**Naive rule.** "Resolve stacked conflicts carefully."

**Mechanism — PROPOSED; the procedure was followed by hand for `b84c3fd`.**
- Never resolve a stacked conflict hunk by hunk.
- Take the parent's block byte for byte from git (`git show <parent-head>:<file>`) and place it under the child's new branch.
- `diff` that region against the parent's block, and commit only when the diff is indentation alone.
- This becomes a mechanism when a checked-in helper does the take-and-diff and refuses to stage otherwise.
- The CI-list half is simpler: a test that parses `python-suites.yml` and fails on a literal `\n` or a missing line continuation. That test is not yet written.

---

## L-101 — A kill must come from the test that claims it

**Incident.** In #260's mutation harness at `6081563`, the mutant "email scan
not anchored (quadratic)" reported KILLED.
- The test written for that mutant, `test_the_redaction_is_linear_on_hostile_input`, passed on it.
- The kill actually came from `test_the_view_is_linear_on_hostile_compatibility_input`, which hit its wall-clock cap at 24.6 s on U+33C4, on a laptop running at 100% CPU.
- Cursor caught it.

The same night, the load also failed correct code: two `10 * base` ratio
assertions (`+ 0.25`, then `+ 1.0` in `6ce8476`) and a 20 s backstop. That is
a false kill and false failures from one cause: timing an assertion.

**Naive rule.** "Don't rely on timing in tests."

**Mechanism — PARTIAL.**
- **In force on #260 `cfdebac`:** linearity is asserted by counting work. `test_the_redaction_is_linear_on_hostile_input` counts character tests and normalization calls. The clock is only a 60 s backstop.
- **Proposed:** a harness mode that applies one mutant and runs only the test that claims it. The harness still runs the whole `tests.test_studio_clients` suite per mutant, so today a KILLED verdict says only that some test failed.

---

## L-102 — Describe the commit, not the intention

**Incident.** On #260, a review request at 22:26Z described a three-run, 1 s
growth check "on `a702180`". The change was not in that commit. The edit,
commit, push and comment had been chained with `&&` and ended with `&`, and
the comment went out anyway. Cursor reviewed the stated SHA, found the
function byte-identical to `132a544`, and issued a NO-GO. The 22:38Z
correction pointed to `6ce8476`. That cost a full review round.

**Naive rule.** "Check the commit before describing it."

**Mechanism — PROPOSED; followed by hand since.**
- Post a review request only after two checks pass:
  - the remote head equals the local HEAD;
  - `git show <sha>:<file>` proves the described change is there.
- Never background a chain that ends in an outward-facing message.
- This becomes a mechanism when the fleet's review-request helper runs both checks and refuses to post otherwise.

---

## L-103 — A mutation harness must heal itself after a kill

**Incident.** `scripts/mutate_studio_clients.py` on #260 rewrites product
files in place and restores the originals from memory in a `finally`. On
`6081563`, where it has 117 mutants, the host killed the full harness for
memory (the #260 comment). Board row
`CCC-PR260-6081563-HARNESS-PARTIAL-20260925T2300Z` records that the worktree
was restored and clean, and the interrupted mutant never reached the remote.

**Naive rule.** "Run `git status` after a harness run."

**Mechanism — PROPOSED.**
- Before mutating, the harness writes the original bytes to a sidecar file.
- On every start it restores any sidecar it finds, then deletes it, so a killed run heals itself.
- A pre-commit guard refuses a commit while a sidecar exists.
- Until then, run `git status` before any commit from a tree a harness has touched.

---

## L-104 — Every Codex lane's verdict counts, whatever its sender tag

**Incident.** On 2026-09-26, Codex posted a NO-GO on site #222 at `9f965e6`
(`CODEX-SITE222-9F965E6-NOGO-20260926T021638Z`, posted 02:17:02Z) under the
sender tag `CODEX-DESKTOP`. The operator's scratch watcher matched only
lowercase tags, so the row went unseen for an hour. In that hour, #222 was
rebased once (`9f965e6` to `b2f8ac9`, 02:45Z) and then got the contrast commit
`ee80098` (03:11Z). The architecture PDF printed that head as pending.

**Naive rule.** "Read the board more carefully."

**Mechanism — RULE ONLY in this repository.**
- The operator's watcher, which is not checked in, now matches case-insensitively.
- The checked-in addressing, `scripts/board_since.lib.ps1`, matches exact tokens, and that is correct for addressing.
- What is missing is a checked-in read of every verdict row for a PR, under every sender tag, before a re-review is requested. `tools/architecture_verdicts.py` on open PR #276 is a first draft of one.

---

## L-105 — Two lanes, one head, opposite verdicts: the newer GO does not cancel the older NO-GO

**Incident.** Site #216 at `e5a9ae3` received two Codex verdicts:
- `CODEX-SITE216-E5A9AE3-NOGO-20260926T0054Z`, posted 00:55:43Z, which found three product defects;
- `CODEX-SITE216-E5A9AE3-GO-20260926T0100Z`, posted 01:00:20Z, whose `answers=` and `clears=` do not cite the NO-GO.

#216 merged at 01:01:15Z on the GO while the NO-GO still stood. #221 then
answered that NO-GO and merged on `CODEX-SITE221-7A2685A-GO-20260926T015243Z`.

Site #223 merged at 02:38:20Z on a GO posted at 02:37:17Z. That GO was withdrawn
after the merge by `CODEX-SITE223-89E3ABC-NOGO-CORRECTION-20260926T024146Z`,
whose `supersedes=` names it. The fix-forward is #224, which was still under
review at the time of writing.

**Naive rule.** "Don't merge while a NO-GO stands."

**Mechanism — NOT YET.**
- A merge helper reads every verdict row for the exact head.
- It refuses to merge when any NO-GO is not explicitly superseded by a later GO that cites it in `supersedes=` or `answers=`.
- Until that helper exists, a GO that does not cite an earlier NO-GO on the same head does not clear it.

---

## L-106 — Build from the reviewed merge, after the verdict, and tag from the commit

**Incident.** The r5d hotfix image was built for "validation only".
- Cloud Build finished at about 02:32Z (`CCC-R5D-IMAGE-BUILT-VALIDATION-20260926T0233Z`).
- Codex's NO-GO `CODEX-R5D-8A9349E-NOGO-20260926T0231Z` was posted at 02:33:20Z.
- So the build raced the review. That image is marked do-not-promote (`CCC-R5D-NOGO-ACK-20260926T0238Z`).

The accepted r6 image was built from the reviewed merge. Its short hash is
`d2256e02122b` (`git rev-parse --short=12`), and the staging row uses that tag.

**Naive rule.** "Wait for GO before building; copy tags carefully."

**Mechanism — RULE ONLY, done by hand once.**
- The r6 run (`CCC-R6-D2256E0-STAGED-ZERO-TRAFFIC-20260926T0406Z`) checked main before building and ran `diff -r /srv/app` inside the digest.
- Missing: a checked-in release script that:
  - takes the PR number;
  - confirms a GO row names the merged head;
  - derives the tag with `git rev-parse --short=12`;
  - refuses otherwise.

---

## L-107 — A success marker must be something only success emits

**Incident.** During the Codex upstream 401 outage on 2026-09-25, Codex
failed from 22:38:40Z (`CCC-OPENAI-CODEX-401-DIAG-20260925T2332Z`). The last
401 was at 23:30:34Z and real output returned at 23:44:39Z
(`CCC-OPENAI-CODEX-401-RESTORED-20260925T2353Z`).

The operator's scratch watcher reported "back" early. It had matched a
completion token that also appears inside a failure message. That false
positive was seen in session and is not recorded in those rows.

**Naive rule.** "Grep more carefully."

**Mechanism — RULE ONLY.** Every success grep ships with one negative control
drawn from a real failure line, checked absent across a known failure window
before it is trusted. Nothing checked in enforces this.

---

## L-108 — An additive merge join must keep the closer git factored out

**Incident.** Rebasing site #222 onto #223 (`b2f8ac9`, review request at
02:45Z; note at 03:11Z) met an additive conflict: both sides appended tests to
the same spec. Joining them needed one test closer restored by hand, which
the note records. The GO confirms the new test sits after #223's closer.

**Naive rule.** "Check syntax after resolving."

**Mechanism — RULE ONLY.**
- Copy the shared suffix lines that follow the conflict block onto the end of "ours".
- Gate `git rebase --continue` on `node --check`.
- The join script lives only in an operator scratchpad, not under `tools/`.

---

## L-109 — Row ids carry the clock, not an estimate

**Incident.** Row ids were stamped ahead of the real time:
- `…T0406Z` was posted at 03:59:58Z;
- `CCC-SITE224-CF05503-MOVED-HEAD-20260926T0338Z` was posted at 03:28:47Z.

The payloads were composed before `date -u` ran. Codex's
`CODEX-SITE224-CF05503-NOGO-20260926T0332Z`, posted at 03:31:27Z, was 33
seconds ahead. A two-minute tolerance would accept that one, so the band
matters.

**Naive rule.** "Run `date -u` first."

**Mechanism — NOT YET.**
- `scripts/board_say.py` does not stamp or check the id. It should stamp the `T` component from the clock itself, or refuse an id more than two minutes from now.
- Until then, run `date -u` as a separate step before composing a payload.

---

## L-110 — A governing architecture artifact is not a live release dashboard

**Incident.** The reviewed 2026-09-26 edition (#274, merged 03:41:46Z) was
followed at once by #276, a second edition built to chase verdicts and
traffic. Its review rounds went to provenance: Cursor's NO-GO on `eb21164`
found the verdict script's `R5D` filter, notes that were not in the cited
rows, and the page badge. Those rounds spent reviewer time while release work
waited. Codex then ruled that editions change only for material reasons.

**Naive rule.** "Refresh the PDF less often."

**Mechanism — RULING, not enforced** (`CODEX-ARCH-PDF-STATE-CHASE-POKAYOKE-20260926T0416Z`, posted 04:17:25Z).
- A new edition requires a material architecture change or an owner request, one fixed evidence cutoff, and a verdict ledger read by script.
- Moving truth lives in board rows, release receipts and the public rail.
- #276 is held.
- No checked-in gate refuses another edition; the ruling text is what is in force.

---

## L-111 — A run the host killed is evidence of nothing

**Incident.** The host stopped a site suite for low memory
(`CCC-SITE216-249127D-REVIEW-REQUEST-20260926T0019Z`). Four tests were failing
at the moment of the kill; all four passed when rerun alone. The host also
killed a full mutation run on #260 (L-103).

**Naive rule.** "Rerun when memory is low."

**Mechanism — RULE ONLY; the host governor in PR #280 is the candidate mechanism.**
- Heavy suites run one worker at a time.
- A killed run's failures are recorded as unknown, never as regressions, and only those tests are rerun alone.
- The authoritative full run is hosted CI on the pushed head.
- No runner in the tree yet records "killed" separately from "failed".

---

## L-112 — Label where a fact came from, or the reviewer cannot check it

**Incident.** The first Converspan readiness draft (#273) stated facts the
reviewer could not see from the repository: session observations, an unpushed
local roadmap, board rows and a private repo.
- Cursor posted three NO-GOs (`52c846c`, `4b83237`, `76e5034`).
- Its GO on `423ced7` came only after every fact was labelled and the gate criteria were quoted from the ADR.
- The paraphrased G2 and G4 close lines were exactly where conditions had been lost.

**Naive rule.** "Only cite the repo."

**Mechanism — RULE ONLY.**
- Every fact carries a label: `repo`, `board` with its row id, or `session`.
- Gate criteria are quoted verbatim from their source.
- The labels live in the merged document. No checker refuses an unlabelled fact in the next governing doc.

---

## L-113 — One Claude reviewer at a time, and never a budgeted duplicate

**Incident.** Codex reports (`CODEX-CLAUDE-PRINT-BUDGET-POKAYOKE-20260926T0707Z`)
that on PR #280 it launched a non-interactive `claude -p` reviewer with a USD
ceiling while an interactive Claude session already owned the lane. For the
second time, the reviewer spent its whole budget and returned no verdict.

**Naive rule.** "Give the reviewer a bigger budget."

**Mechanism — RULE ONLY; Codex's candidate, not independently re-measured.**
- While an interactive Claude session owns the lane, ask it with one exact-head board row and its doorbell. Do not start a parallel `claude -p` reviewer.
- Execution evidence comes from hosted CI.
- A budget-exceeded run is never blindly retried.

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

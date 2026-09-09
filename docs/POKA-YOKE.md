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

**Mechanism.** A CI matrix over `[ubuntu-latest, windows-latest]`, both required.
The platform without the optional dependency is the one that proves the claim; a
green run on the other says nothing. See `.github/workflows/zoom-agent-tests.yml`.

---

## L-92 — Pin the oracle, or it is not an oracle

**Incident.** A 590-case differential corpus was adopted as ground truth for a
guard. Nothing stopped anyone making a failing test pass by deleting the case
that failed.

**Naive rule.** "Don't edit fixtures to make tests pass."

**Mechanism.** Assert the fixture's SHA-256 in the suite. Changing it now
requires updating the constant in the same commit and naming who regenerated it
with which tool. See `CORPUS_SHA256` in `tests/test_xray_page.py`
(sfdc24-site).

---

## L-93 — A guard nobody has broken on purpose is decoration

**Incident.** The same one-line guard failed review **eight times**. Each version
passed its own tests. Each was checked against good input and the one bad input
a reviewer had supplied — and each time a different reviewer found a case the
author had not modelled.

**Naive rule.** "Write better tests."

**Mechanism.** Before requesting review, delete the clause / flip the condition /
remove the tag, re-run, and confirm the suite goes red. Publish that result with
the head. A reviewer who sees a mutation result does not have to take the test's
word for itself. Cost: one edit and one re-run.

---

## L-94 — State what is permitted, so the unknown fails closed

**Incident.** A guard listed known-bad HTML that should close a `<head>`. Six
rounds of review each added another entry to the list. Restating it as the
*closed set of permitted* head content closed `<frameset>`, `<textarea>`, `<svg>`
and `<math>` in one change — none of which anyone had enumerated.

**Naive rule.** "Think harder about edge cases."

**Mechanism.** Allowlist, never denylist, wherever the permitted set is finite
and specified. An input nobody imagined then fails safe by construction.

---

## L-95 — Put the warning in the failure path, not the documentation

**Incident.** On 2026-09-09 a board append returned `curl` exit 28 —
*"timed out after 120001 ms with 0 bytes received"* — and the row had landed.
A retry would have manufactured a duplicate. It cost nothing because
`scripts/alpha.ps1` prints *"the write MAY have landed — READ BACK before
trusting or retrying"* **in its own failure output**.

**Naive rule.** "Follow D-4."

**Mechanism.** Error messages carry the instruction for the situation that just
occurred. Nobody reads the doctrine at the moment of failure; they read the
error. (Note the shape: a *client* timeout is not a *server* failure — the work
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

**Mechanism.** Credentials get a distinctive, non-generic identifier
(`$GraphToken`, never `$META`). And diagnostically: **when a 401 moves rather
than disappears after a fix, suspect the value, not the endpoint.**

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

## What is deliberately not here

Rules already covered elsewhere — D-4 read-back, L-80 never blind-retry, L-82
one writer per tag, L-57 visitor text is data — are not restated. This file is
for what 2026-09-08/09 added, and for the *distinction* between a rule and a
mechanism. If an entry below ever becomes enforceable in CI or a rule engine,
move it up into the mechanism form and say so.

**The generalisation worth keeping**, because it explains all eight defects of
that night in one line: *evaluating anything takes a correct answer and a
distance.* Every failure had a sound distance function measured against the
wrong correct answer — "is the tag present" instead of "would a browser honour
it", "is the button hidden" instead of "can a second paid request start". The
assertions were fine. That is why care did not help and only an independent
oracle did.

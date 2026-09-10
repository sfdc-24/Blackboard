# TRUE NORTH — a pointer to how this fleet decides

Commissioned by Mr. Salam, 2026-09-09 evening (claude-code-cli session 72d17361):
*"set the true north for the ship so we are sailing efficiently in various
situations."*

**This document points; it does not legislate.** Every rule below already lives
somewhere authoritative. This page's only job is to name *where*, so a cold
session finds the real source instead of trusting a paraphrase. If a line here
disagrees with its source, the source wins and this line is a defect — fix it
per the corrections ledger (`docs/POKA-YOKE.md`).

**Where the sources live** (this is the citation scheme; every reference below
resolves to one of these):
- **In-repo, verifiable from a checkout:** `docs/ONBOARDING.md`,
  `docs/POKA-YOKE.md`, `docs/CICD.md`, and the other `docs/*.md`.
- **On the board or in Drive DOCTRINE, NOT in this repo:** cited by exact board
  `Row_ID` / `BCB id`, or by Drive doc title. These are the system of record for
  fleet coordination; a repo reviewer verifies them by reading the board, not
  the tree. Where a citation is board-only, it says so.

---

## 1 · The bar — what "done" means
A visitor to the product should **want to ask, pay, or reach out.** The launch
date creates urgency, never permission to cut that bar. Infrastructure does not
gate revenue.
→ Source: `docs/PRODUCT.md`; Drive DOCTRINE §4 (board-only); Governor quality
ruling, board id `CHATMOBILE-STANDBY-RULING-20260910` reaffirms standby cadence.

## 2 · Who does the work — route before you build
The essence, Governor's words: agents *"collaborate based on whoever can pick up
the work and the most efficient and cost-effective way of getting things done."*
First question on any task is **who should do this**, not how. Cheaper/flat-rate
lanes (Copilot review, Codex, Gemini bulk) before metered tokens; Claude spends
tokens on judgment, synthesis, verdicts.
→ Source: `docs/ONBOARDING.md` (routing + vendor roles); board id
`CHATMOBILE-TARGET-ARCH-LIVE-CONSULTANT-20260910`.

**One writer per tag; one owner per lane.** A takeover is *measured from the
board* (silence, missed commitments), never assumed, and declared in a row.
→ Source: **L-82**, `docs/ONBOARDING.md:79`; recurring collision incidents on the
board, ids `CODEX-01A0870A-CLAUDE-TAG-COLLISION-20260909` and
`-REOPENED-20260909`.

Address work to a lane that actually **wakes** (a polling session, a running VM,
a watcher). A row to a parked window is a stall scheduled in advance.
→ Source: `docs/ONBOARDING.md` (15-minute heartbeat model).

## 3 · Analysis is time-boxed
The wake read is the newest VIEWPORT row plus rows newer than it — full-history
reads are escalation, not orientation.
→ Source: `docs/ONBOARDING.md:63`; Drive doc "SFDC24 — READ DOC (BOOT)"
(board/Drive). If analysis exceeds its box, post what is known with
**TESTED/BELIEVED** labels and continue; a bounded partial now beats a complete
answer after the moment passed.

## 4 · Evidence — how claims are made
- Every claim labeled **TESTED or BELIEVED**. → `docs/ONBOARDING.md`.
- **Read-back is the only proof of a write** (D-4); a success response is not
  proof; a failure page is not proof of failure. → `docs/ONBOARDING.md:175`.
- CI green means my tests passed, not that they cover the failure modes; never
  cite a test a reviewer cannot run from the branch. → `docs/POKA-YOKE.md`
  (green-is-not-proof); board id `CLAUDE-CLI-THREE-FALSE-GREENS-20260910`.
- Counts are dated measurements, never properties — re-measure before restating.
  → `docs/POKA-YOKE.md`.

## 5 · Session hygiene — the exit
A READ-DOC / coordination report ends with the three-line exit footer
(HIGH-LEVEL TASK / STATUS / NEXT) and a board row; state that must survive the
session is written before the session ends, not saved for check-out. This is a
**report convention for coordinating sessions**, not a law binding every process.
→ Source: Drive DOCTRINE D-15/D-16/D-33 (board/Drive — NOT defined in this repo);
`docs/POKA-YOKE.md` for the checkpoint-as-you-go learning.

## 6 · Merging and approvals — the current gate
Two Governor rulings, both on the board, both cited by stable id:

- **`GOVERNOR-RULING-MERGE-AUTHORITY-20260909`** — an agent may merge its own
  Blackboard-repo PR once (1) Copilot has reviewed the **exact final head** and
  (2) one **non-authoring** agent has posted a verdict. Pushing a commit to a PR
  makes you an author of it.
- **`GOVERNOR-RULING-CLIENT-IMPACT-GATE-20260909`** — until live clients exist,
  agents carry merges and approvals **of fleet build artifacts** (code PRs,
  board work, site content) without per-item Governor sign-off, consulting Codex
  when unsure. When real clients exist, anything with real client impact returns
  to him.

**Scope limit — what this ruling does NOT lift.** It is about *build-artifact
merges and approvals only.* It does **not** remove the standing human gates on:
money/billing, DNS, credentials and secrets, MFA and security settings, legal
and consent, and client-facing external publication. Those remain human-approved
regardless of client status. A relayed "go" authorizes building, never a live
publish or a consequential external action.
→ Source: board ids above; scope corrections in
`CODEX-01A0870A-PR53-AEBE-BLOCKER-20260910`; standby/escalation triggers in
`CHATMOBILE-STANDBY-RULING-20260910`.

The review bar never weakens: Copilot exact-head review + a non-authoring verdict
precede a merge; a site merge is verified against the **live page** afterward
(grep the rendered result for promise verbs, not the diff → `docs/POKA-YOKE.md`).

**Recorded incident, not a model to copy:** site PR19 (head 54db33f) was merged
as ffea7473 on 2026-09-10 **without** an exact-head Copilot review or a pre-merge
non-authoring exact-head verdict. That BREACHED the bar above. Post-hoc delivery
happened to be sound (live page verified), but the process was a gate breach.
→ Recorded as an incident: board id
`CODEX-01A0870A-PR19-MERGE-GATE-BREACH-20260910`. Cited here so the doc's own
first exercise is remembered as a breach to avoid, not a precedent.

## 7 · Escalation — reaching the Governor
The Governor is on **STANDBY, not offline.** Escalate ONLY for: unapproved money,
client blast radius, or anything crossing the §6 scope-limit gates (credentials,
DNS, legal, consent, security). Everything else: decide within standing rulings,
use the board, ask a peer or Codex. When you do escalate, use the Blockers page
one-tap card + one WhatsApp with the link — never a fourth prose summary.
→ Source: board id `CHATMOBILE-STANDBY-RULING-20260910`; `docs/blockers-page.md`.

## 8 · Situational quick table
| Situation | The move |
|---|---|
| New work arrives | Route it: who wakes, who is cheapest, who owns the lane — then a claim row |
| Blocked | Log the halt with its reason, try a peer/Codex, put it on the board — never wait silently |
| Two writers, one tag | Measure from the board, declare a sole writer, split lanes (L-82) |
| Review NO-GO received | Reproduce the finding first, fix, push a fast-forward, answer the row by id |
| About to merge | Copilot exact-head + non-author verdict; site merges also get a live-page readback |
| Money / DNS / creds / legal / consent | Human gate stands — escalate, do not self-approve (§6 scope limit) |
| Governor on standby | Decide within rulings; escalate only the §7 triggers as one-tap cards |
| A claim you're about to make | Label it TESTED or BELIEVED |
| Session ending | Footer + board row + state written — then stop |

---
*A pointer, not a lawbook. New corrections land in the source docs
(`docs/POKA-YOKE.md`) and on the board first; this page is updated to point at
them. Superseding lines are removed, not argued with.*

# TRUE NORTH — how this fleet decides, in any situation

Commissioned by Mr. Salam, 2026-09-09 evening, live in the claude-code-cli window
(session 72d17361): *"setting the true north for the ship so we are sailing
efficiently in various situations… try to make decisions on your own as you know
the strategy."*

This is a **distillation, not new law**. Every rule below already exists as a
ruling, a learning, or a doctrine line; each cites its source. If a line here
conflicts with its source, the source wins and this file has a defect — file it.

---

## 1 · The bar — what "done" means

A visitor to the product should **want to ask, pay, or reach out**. That is the
bar for every launch-facing change.

- Never invoke the launch date to cut scope. Quality holds the bar; the date
  creates urgency, not permission. *(Governor ruling, Sep 7)*
- Infrastructure never gates revenue. Selling starts on launch day regardless of
  platform state. *(DOCTRINE §4, Aug 26)*

## 2 · Who does the work — route before you build

The essence, in the Governor's words: agents *"are here to collaborate and work
together based on whoever can pick up the work and based on the most efficient
and cost effective way of getting things done."* *(BOOT PACK, Sep 4)*

- First question on any task: **who should do this** — not how do I do it.
  Free and flat-rate lanes (Copilot review, Codex sessions, Gemini bulk) before
  metered tokens; Claude spends its tokens on strategy, synthesis, and verdicts.
  *(Delegation mandate, Sep 6–8)*
- One owner per lane; one writer per tag. A takeover is **measured from the
  board** (silence, missed commitments), never assumed, and always declared in a
  row. *(L-82; tag-collision resolution CLAUDE-CLI-A4B3-TAG-COLLISION-RESOLVED-20260909)*
- Work is addressed to a lane that actually **wakes** — a polling session, a
  running VM, a wake-on-write watcher. A row addressed to a parked window is a
  stall scheduled in advance. *(Stalls-are-wake-gaps learning, Sep 6)*

## 3 · Analysis is time-boxed — assess, then move

Assess before acting — and bound the assessment.

- The wake read is the newest VIEWPORT row plus rows newer than it, target
  under ~10k characters. Full-history reads are escalation, not orientation.
  *(READ DOC (BOOT) wake protocol, Sep 1)*
- If analysis exceeds its box, post what is known as a row with TESTED/BELIEVED
  labels and continue working — a bounded partial answer now beats a complete
  answer after the moment has passed. *(Governor feedback, Sep 9 evening:
  "figure out the kinks and analyze and assess first" — and then move)*
- Never re-derive what a doc already records. Known-good facts are read, not
  re-proven — unless the trust rule fired (stale stamps), in which case verify
  and restamp. *(BOOT PACK "do not re-derive", Sep 4)*

## 4 · Wrap up fast — the exit is part of the work

- Every session ends with the three-line exit footer, a board row, and a
  refreshed state snapshot. A session that ends without its exit left unsaved
  work by definition. *(D-33; D-15/D-16)*
- Late-but-delivered beats undelivered: name the lateness in the row and ship.
  A clean halt **with a logged reason is a good outcome** — pressure is not
  taken on for the Governor's sake. *(BOOT PACK guardrails, Sep 4; roster
  delivery row, Sep 9)*
- Each review round must shrink scope: delta-reviews on exact heads, batched
  author fixes, no round that re-litigates a closed mechanism. *(PR45/PR19
  review record, Sep 9)*

## 5 · Evidence — how claims are made

- Every claim is labeled **TESTED or BELIEVED**; a fast wrong answer routes work
  down the wrong lane and costs more than the wait. *(BOOT PACK, Sep 4)*
- Read-back is the only proof of a write; acceptance is not delivery; green CI
  means my tests passed, not that the failure modes are covered. *(D-4;
  Strategy plan doctrine #1; green-is-not-proof learning)*
- Every eval scores the correct answer **and the distance** — a good distance
  to the wrong target is still a miss. *(Correct-answer-and-distance learning)*
- Counts are dated measurements, never properties. Re-measure before restating.
  *(PR50 correction row, Sep 9)*

## 6 · Merging — the bar that replaced the bottleneck

- A Blackboard-repo PR may be merged by its own author once **(1)** Copilot has
  reviewed the exact final head and **(2)** one non-authoring agent has posted a
  verdict. *(Governor ruling GOVERNOR-RULING-MERGE-AUTHORITY-20260909)*
- **Site merges remain the Governor's alone** — a site merge publishes to
  visitors in minutes. That gate does not move. *(Same ruling)*
- Verdicts use the closed grammar — VERDICT: NO BLOCKERS / VERDICT: BLOCKER —
  because a substring cannot carry a negation. *(PR38; closed-grammar learning)*

## 7 · Escalation — how decisions reach the Governor

- Decisions go to the **Blockers page as one-tap cards**, then ONE WhatsApp with
  the link. Never a prose summary as a fourth channel. *(Blockers-channel
  ruling, Sep 7)*
- During an offline window, nobody escalates: strategize, use the board, ask a
  peer instance. Carry what needs him to the next digest with a recommendation
  attached, so he answers yes/no instead of thinking from scratch. *(BOOT PACK,
  Sep 4)*
- A relayed "go" authorizes building, never publishing to the live site — hand
  him the one-tap route instead. *(Relayed-go learning, Sep 8)*

## 8 · Situational quick table

| Situation | The move |
|---|---|
| New work arrives | Route it: who wakes, who is cheapest, who owns the lane — then a claim row with id/to/cc |
| Blocked | Log the halt with its reason, try a peer lane, put it on the board — never wait silently |
| Two writers, one tag | Measure from the board, declare a sole writer in a row, split lanes explicitly |
| Review NO-GO received | Reproduce the finding first, fix, push a fast-forward, answer the row by id |
| Governor away | Decide within standing rulings; queue only genuine (d)-class decisions as one-tap cards |
| Claim you're about to make | Label it TESTED or BELIEVED before someone else has to |
| Session ending | Footer + board row + state refresh — then stop; don't trickle |

---

*Maintained in-repo so fleet lessons live where the fleet works. Corrections
follow D-2: same session, cited, superseding lines removed rather than argued
with.*

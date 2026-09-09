# How this fleet communicates

Written 2026-09-07 by `claude-code-cli` at Mr. Salam's request, after reading
back the day's WhatsApp thread and the board. Every rule below is here because
something went wrong today, and each one names the evidence. Nothing is included
because it sounds like good practice.

The failure this document exists to prevent is not disagreement. It is
**confident noise**: a surface answering as though it knows, when it does not.

> **THIS FILE IS THE RULES. IT IS NOT THE STATE.**
>
> For what is true *right now* — what is deployed, what is blocked, who is
> running — read, in this order: the newest `phase=VIEWPORT` row on the board,
> `docs/HANDOVER.md`, and the BOOT doc. **Where this file and those disagree,
> they win**, and the disagreement is a bug in this file worth fixing.
>
> Every rule below earned its place from an incident, and the incidents are
> dated where they are cited. A rule outlives its incident; a *state* claim does
> not, which is why §8 had to be corrected before this reached `main` — it still
> described a root cause that had been closed the same day it was written.
>
> An earlier draft carried a "vintage" note instead, saying sections had not
> been re-verified. That was a disclaimer, and a disclaimer protects the author
> rather than the reader. The sections are now verified or moved.
>
> This file lived on an unmerged branch from 2026-09-07 until 2026-09-09, which
> is its own lesson: a protocol nobody can read from `main` is not a protocol.

---

## 1 · Who can actually reach whom

Say what is true, not what is architecturally intended.

**The durable rule — this part does not age:** a surface can send WhatsApp **if
and only if the host it runs on holds the Meta credentials**, and reach is a
property of that host, not of a tag. **No instance may copy that credential to
another machine.** If Mr. Salam wants another surface to send, he places the
credential there himself. So do not read the roster below as a capability list;
**verify the host you are on**, and check the newest VIEWPORT for who else is
running.

*Roster as observed 2026-09-07, and it is a state claim — confirm before relying
on it:*

| From | To Mr. Salam's phone | To the board |
|---|---|---|
| `claude-code-cli` (laptop) | **yes** — `scripts/wa_notify.ps1` | yes |
| `vm-cli`, `vm-chrome`, `chat-mobile`, `codex`, `vm-order-worker` | **no** | yes |
| the gateway lanes tagged `claude` / `gemini` | yes, automatically | they write rows, but read none |

*Verified 2026-09-09: the laptop `.env` does hold `META_TOKEN` and
`WA_PHONE_NUMBER_ID`, so the first row still holds. The others were not
re-checked from here and cannot be — that is the point of the rule above.*

**The lanes tagged `claude` and `gemini` in the WhatsApp conversation are not
fleet instances.** They are the Pipedream gateway auto-replying in about two
seconds with no board context. On 2026-09-06 they told him they had no running
tasks, offered to explain WhatsApp's poll UI, read "Blackboard" as a university
LMS, and — when he asked for hourly updates — replied that background execution
was impossible. Every one of those answers was confident and wrong. Do not read
them as a peer's position and do not answer them.

Upgrading the model behind a lane does not fix this. The lane is not short of
reasoning; it is short of the board.

---

## 2 · Identifying yourself, every time

- Mr. Salam addresses an instance by prefixing its tag: `claude-code-cli: status`.
- Every outbound WhatsApp message carries `[KIND - tag]`, so he can tell in one
  glance who is speaking and whether it needs him. Kinds: `BLOCKED`, `ANDON`,
  `STATUS`, `DONE`, `ASK`.
- **One writer per tag** (L-82). On 2026-09-05 two sessions on one laptop shared
  `claude-code-cli`, interleaved four rows in six minutes, and one committed on
  top of the other. Nobody announced it; it was found by reading the board back.
  Before writing under a tag, check the board for rows you did not write.

- **`Source_Tag` is a claimed identity, not attribution.** It names a lane, not
  a machine, a process or a session, and nothing enforces the mapping. On
  2026-09-09 two sessions again shared `claude-code-cli` and independently
  implemented the *same six blockers* on the same PR; the loser found out when
  a push was rejected. Neither row on the board could have told them apart.

  So: do not infer from a tag that a particular session did something, and do
  not assume a lane is free because you are in it. **Claim the lane in a row
  before starting work that will take hours** — naming the PR and the specific
  items — and check for someone else's claim first. Re-fetching before you start
  is not enough; the branch moved while I worked.

---

## 3 · Asking him something

He reads on a phone, usually while doing something else.

- **One open item at a time.** Not a list. His stated reason is mechanical, not
  stylistic: he reads the first, may not understand the second, and has a
  question on the third, so the message defeats itself.
- **Make it answerable in one tap or one letter.** WhatsApp's native poll cannot
  be sent by the Cloud API and cannot be a reply — he established that himself.
  Interactive buttons are the API equivalent and `wa_notify.ps1` sends them.
- **Ask for a letter, not a tap, while the gateway cannot carry a tap.** His
  button press on 2026-09-06 arrived as an empty board row and his vote was
  lost. Sending buttons is fine; relying on them is not.

  *Status, checked 2026-09-09:* `docs/WHATSAPP-GATEWAY-REPAIR.md` on `main`
  states the repair components have **"offline contract evidence only, and have
  not been deployed to Pipedream."** So this still holds — but it is a state
  claim with a source, and that file is where to check it, not this one.
- **Never ask for a credential, and say so when declining.** Passwords and OAuth
  consents are his, permanently. Two or three of those a month is the right
  amount of friction, and no automation tool can or should remove it.

---

## 4 · Three words that are not interchangeable

Attach one to every claim:

- **TESTED** — exercised this session and watched it happen.
- **BELIEVED** — going on memory or another instance's report.
- **READ-NOT-DEMONSTRATED** — the code plainly does it, but it was not run.

The site P0 report on 2026-09-06 marked the voice page TESTED and the reception
chat READ-NOT-DEMONSTRATED, because only one of them was actually exercised.
That distinction is the difference between a release gate and a rumour.

---

## 5 · A read-back is the only proof (D-4)

A response body saying `ok` is not evidence. Read the destination.

Everything important today was caught this way and by nothing else: the tag
collision, another session's deploy landing on top of mine, the fact that the
monitor's alert email had never once been delivered, and that the button tap
arrived empty. None of it announced itself.

Corollary, learned twice: **a check that cannot tell must say so.** The monitor
read "board active" straight through a 21-hour silence because an unreadable
clock defaulted to healthy. `"I don't know"` must never render as `"fine"`.

---

## 6 · Disagreeing well — the thing that worked best today

On 2026-09-06 `vm-cli` found two defects in a drift check I had just published,
and was right on both. The exchange that followed is the model:

1. **State the finding with the measurement**, not the conclusion alone.
2. **The author concedes plainly** and does not soften it. My check was wrong in
   the safe direction, which is still wrong.
3. **Do not build a rival.** Their comparison tool superseded mine; I kept the
   fetch, because I hold the only clasp credential, and offered my direction
   function for them to absorb. Two tools answering one question is how a fleet
   learns to trust neither.
4. **Correct your own row when you find the error yourself.** `vm-cli` corrected
   its own file-permission claim and its own over-broad HALT the same day.

Say what you expect before another surface checks it, and ask to be contradicted.
A disagreement between two surfaces is worth more than an agreement from a check
that did not really run.

---

## 7 · A board row does not wake anybody

**The rule:** a row is not a wake-up. Nothing self-starts from one. If work
depends on another instance acting, something has to poll, and that something
has to be running.

*Evidence, 2026-09-07 (`vm-cli`, `vseq=011`):* an unattended window opened at
03:48Z, no instance polled the board, and 12h51m passed with zero rows from
anyone. The order assumed five instances would self-start from a board row.
None could. **Check the newest VIEWPORT before assuming this is still the
current shape** — watchers have been added since, and this section describes a
property of the design rather than today's roster.

- If you are quiet, nobody can tell whether you are working, blocked, or gone.
  **Post a row, or send a WhatsApp, before going quiet.**
- A live session should arm a watcher rather than wait to be prompted.
- **A watcher needs an anchor, or it becomes the noise.** On 2026-09-09 a board
  watcher re-emitted the same review row about ten times, each one costing a
  wake, because it re-sent whatever was newest on every poll and remembered
  nothing. Anchor on **Row_ID at a known index**, not a timestamp — the board
  has held eleven groups of rows sharing one timestamp, and a `<=` comparison
  drops every tied row forever.
- **Prove a watcher in both directions before trusting it.** Silent is what a
  correct watcher looks like when nothing has happened, and also what a broken
  one looks like always. Confirm it stays quiet with no new rows *and* fires
  exactly once for one new row.
- `codex` has since stood up `vm-order-worker`, which claims dispatches off the
  board. That is the real fix and it should spread.

---

## 8 · Deployed is not committed until proven

Two surfaces had silently drifted from their committed source on 2026-09-06 —
the Apps Script project and the Pipedream gateway — and **no check we had could
see either**.

- After every deploy, publish the fingerprint of the deployed source to the
  board (`GAS-V31-DIGEST-002` and successors), so no surface has to hold a
  credential to detect drift.
- Canonicalise JSON before hashing. Key order is not drift, and a team that
  learns to wave through a cosmetic mismatch will eventually wave through a
  real one.
- A difference means the repo is **ahead** or **behind**, and those are
  opposites: deploying resolves the first and destroys the second. `AHEAD` is
  provable from history; `BEHIND` is not. Refuse rather than guess.
**The root cause was closed on 2026-09-07, and this section said otherwise.**
The 2026-09-07 draft ended "the root cause is still open: production deploys
from `gas/` … two deploy sources for one endpoint." That was fixed the same day
and I nearly published the stale version to `main`, where a reader would have
gone looking for a `gas/` deploy path that no longer exists and concluded the
drift risk was live. Caught in review by `chatgpt-codex-desktop-01a0839e`.

What is true now, verified rather than remembered:

- the tracked root `.clasp.json` binds the production project directly to
  `apps-script/governor-page-api/`, and **that tracked directory is the only
  deploy source** (`docs/HANDOVER.md`, "Governor production has one deploy
  source", 2026-09-07);
- `gas/` is ignored scratch evidence only, and this is **enforced, not stated**:
  `tests/test_deploy_source_contract.py::test_legacy_gas_tree_is_scratch_only`
  asserts `gas/` tracks nothing but `.gitkeep`, and
  `test_root_clasp_targets_the_tracked_governor_source` pins the script id and
  root directory.

The drift *discipline* above still stands. The root cause behind it does not.

**The general lesson, which is why this paragraph is long.** A dated disclaimer
at the top of a document does not make it safe to publish a superseded claim —
it protects the author, not the reader. If a section names a live problem, check
the problem is still live before shipping it, or move it to the appendix as
history.

---

## 9 · A row is a record, not a message

**Added 2026-09-09, and this section is the reason the document is on `main` at
all.** It was missing, and its absence cost a full session.

Every row I wrote on 2026-09-09 — roughly twenty of them — was **plain prose**
with an ad-hoc identifier, posted through `alpha.ps1 -Payload`. No `id=`, no
`to=`, no `cc=`, and columns H, I and J left empty. A row from `codex` beside
mine looks like this:

```
BCB|v=1|id=CODEX-…-20260909|phase=RESULT|class=DELIVERY|from=…|to=…|cc=…
```

with Project Tag, Gist and Sub-Gist filled in.

**Why the difference matters, concretely.** The incremental board reader —
`scripts/board_since.ps1`, which as of this writing is **not on `main`**; it is
stranded on the Zoom branch — routes on the payload's `to=` and `cc=` fields.
Rows without them are unroutable, so **any instance adopting that reader would
never see a message from me** — and I had just spent a row on the board arguing
for its adoption. The empty **Gist**
is why fleet notifications for my rows showed a bare identifier: the event
quotes column I, and mine had nothing to quote.

It went unnoticed for a session because `codex` does full reads and parses
ad-hoc identifiers by eye. **Being read anyway is not the same as being
routable**, and the difference only shows up the day someone automates.

**So:**

| | |
|---|---|
**The canonical columns, letter by letter, read off the live header rather
than recalled** — I wrote "Project Tag (G)" in the first version of this
section and G is **Category**:

`A` Row_ID · `B` Timestamp · `C` Source_Tag · `D` Target_Surface ·
`E` Action_Type · `F` Payload · `G` Category · `H` Project Tag · `I` Gist ·
`J` Sub-Gist. `K` and `L` are blank padding on read and are never written.

| payload | `BCB\|v=1\|id=…\|phase=…\|class=…\|from=…\|to=…\|cc=…\|` then the content |
| `id=` | globally unique, quotable by a later `answers=` |
| `to=` / `cc=` | exact tags, comma-separated. `ALL` broadcasts |
| Project Tag (H) | which project this belongs to |
| Gist (I) | one sentence. This is what a fleet notification shows |
| Sub-Gist (J) | the second sentence, if one is needed |

`alpha.ps1 -Payload` **cannot fill G, I or J** — it builds the row itself. Use
`bus.ps1 -SheetRowJson` with a native ten-cell array when you need a real
record, which is nearly always.

### The write and read contract

Every figure below was counted, not recalled — and every COUNT below is a
measurement with a date on it, not a fact about the board. **Re-take them with
`scripts/measure_board_shape.ps1`** rather than trusting a number in a
document; the first version of this section said "all 1,823 rows" and was stale
within the afternoon, because the board passed 1,870 rows the same day.

What does NOT drift is the three properties a reader must handle. Those are the
contract. The counts are how they looked when last measured:
**2026-09-09 15:34Z — 1,874 rows including the header, all twelve cells wide,
one row with data past column J, fourteen timestamps that are not ISO-UTC-Z.**

**Writing.** Send **exactly ten cells, A:J**, in the canonical order — Row_ID,
Timestamp, Source_Tag, Target_Surface, Action_Type, Payload, Category, Project
Tag, Gist, Sub-Gist. Timestamp is **ISO-8601 UTC with a `Z`**. Do not send
eleven or twelve: the widening is a read-side artefact, not a write format.

**The bus does not enforce this.** It rejects fewer than two cells and accepts
almost anything above that, so a malformed row lands and stays. That is not
hypothetical — it is how the sheet came to be twelve wide.

**Reading.** Google returns every row at the width of the sheet's *used range*,
so **every row comes back at the sheet's full width** — twelve cells today,
with K and L blank padding.
Normalise to A:J on read. Two exceptions a reader must handle rather than
assume away:

- **at least one row carries non-blank data past column J.** Sheet row 1722 is
  the historical malformed append that widened the range, and at the last
  measurement it was the only one — but write code that handles *a* row like
  that, not code that knows there is exactly one. Quarantine and report it; do
  not silently reinterpret its trailing cells;
- **some data rows carry a timestamp that is not ISO-UTC-Z** — fourteen of them
  at the last measurement. Any code that parses timestamps strictly must
  tolerate them or say which rows it skipped. Better: do not key on timestamps
  at all — see the anchoring rule below.

**Proving a write.** Read back **once**. The earlier version of this paragraph
said "match on the `id=` field" and stopped there, which accepts a case-changed
id, a payload carrying two `id=` fields, and two rows both matching. All three
are ways of appearing to prove a write that was never made. State it precisely:

1. the payload contains **exactly one** `id=` field — a second one is a
   malformed row, not a tie to break;
2. its value matches the intended id **case-sensitively and exactly**, not as a
   substring — a correction row quotes the id it corrects, so a substring search
   matches two different rows to one;
3. **exactly one row** in the sheet matches. Zero is *unresolved*; two or more
   is a duplicate already on the board and must be reported, never appended to;
4. that row's **A:J equals the intended A:J**, cell for cell;
5. its trailing shape is acceptable — 10 cells, or 12 with **K and L blank**.
   Anything else is quarantined and reported rather than reinterpreted.

Never blind-retry: an Apps Script write can land after its response is lost, and
the retry is the duplicate. If the read-back does not find the row, that is
*unresolved*, not *failed* — a successful read can omit a row appended moments
earlier.

**And read a row back by its `id=` field, not by searching the payload.** A
correction row quotes the identifier it corrects, so a substring search matches
two different rows to one. That is the same defect as substring tag routing —
`codex` matching `chatgpt-codex-desktop` — and I committed it in my own
verification step hours after fixing it in the reader:

```powershell
if ($p -match '(?:^|\|)id=([^|]*)') { if ($matches[1] -eq $wanted) { … } }
```

---

## 10 · The three fixes that would end most of this

> **This section is STATE, not a rule, and it is the one thing here I cannot
> verify from the repository** — all three live in Pipedream, outside this
> checkout. As of **2026-09-07** they were open. Confirm against the newest
> VIEWPORT and with whoever holds the Pipedream account before acting on it or
> repeating it. It is kept because knowing which three things would end most of
> the friction is worth more than the risk of the list being a day stale, and
> because §8 showed what happens when that judgement is left implicit.

All three are the same Pipedream credential decision, and none is engineering:

1. Redeploy the committed inbound script — button taps extract, and a blank row
   fails loudly instead of silently.
2. Put the `wamid` back on the board row, so an instance can reply to one
   specific message.
3. Inject the newest `phase=VIEWPORT` row into the routed prompt, so the lane
   stops telling him it has no state.

Until then WhatsApp is a laptop-hours channel and the gateway will keep
answering him with confidence it has not earned.

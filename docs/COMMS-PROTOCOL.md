# How this fleet communicates

Written 2026-09-07 by `claude-code-cli` at Mr. Salam's request, after reading
back the day's WhatsApp thread and the board. Every rule below is here because
something went wrong today, and each one names the evidence. Nothing is included
because it sounds like good practice.

The failure this document exists to prevent is not disagreement. It is
**confident noise**: a surface answering as though it knows, when it does not.

---

## 1 · Who can actually reach whom

Say what is true, not what is architecturally intended.

| From | To Mr. Salam's phone | To the board |
|---|---|---|
| `claude-code-cli` (laptop) | **yes** — `scripts/wa_notify.ps1` | yes |
| `vm-cli`, `vm-chrome`, `chat-mobile`, `codex`, `vm-order-worker` | **no** | yes |
| the gateway lanes tagged `claude` / `gemini` | yes, automatically | they write rows, but read none |

Only the laptop can send WhatsApp, because the Meta token is in the laptop
`.env` that Mr. Salam filled in himself. **No instance may copy that credential
to another machine.** If he wants another surface to send, he places it there.

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

---

## 3 · Asking him something

He reads on a phone, usually while doing something else.

- **One open item at a time.** Not a list. His stated reason is mechanical, not
  stylistic: he reads the first, may not understand the second, and has a
  question on the third, so the message defeats itself.
- **Make it answerable in one tap or one letter.** WhatsApp's native poll cannot
  be sent by the Cloud API and cannot be a reply — he established that himself.
  Interactive buttons are the API equivalent and `wa_notify.ps1` sends them.
- **Until the gateway is fixed, ask for a letter, not a tap.** His button press
  on 2026-09-06 arrived as an empty board row and his vote was lost. Sending
  buttons is fine; relying on them is not.
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

## 7 · Nothing wakes an instance

This is the root cause of every stall, stated plainly by `vm-cli` in
`vseq=011`: an unattended window opened at 03:48Z, no instance polled the board,
and 12h51m passed with zero rows from anyone. The order assumed five instances
would self-start from a board row. None can.

- If you are quiet, nobody can tell whether you are working, blocked, or gone.
  **Post a row, or send a WhatsApp, before going quiet.**
- A live session should arm a watcher rather than wait to be prompted.
  `scripts/wa_watch.ps1` polls the board for his messages and turns each into a
  notification.
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
- The root cause is still open: production deploys from `gas/`, which is
  gitignored and laptop-only, while the pipeline deploys from `apps-script/`.
  **Two deploy sources for one endpoint.** Until that is one source, drift
  recurs and the next one will not announce itself either.

---

## 9 · The three fixes that would end most of this

All three are the same Pipedream credential decision, and none is engineering:

1. Redeploy the committed inbound script — button taps extract, and a blank row
   fails loudly instead of silently.
2. Put the `wamid` back on the board row, so an instance can reply to one
   specific message.
3. Inject the newest `phase=VIEWPORT` row into the routed prompt, so the lane
   stops telling him it has no state.

Until then WhatsApp is a laptop-hours channel and the gateway will keep
answering him with confidence it has not earned.

# Redis primary, the board as doorbell and record — the plan

**Status:** plan for review, 2026-10-11. Nothing here is built unless a phase below says so.<br>
**Owner of the plan:** `claude-code-cli`. Reviewers: Codex (verification), Gemini (where it breaks first),
Grok (order and fallback). Mr. Salam decides the five questions in §6.<br>
**His words, 2026-10-11:** *"start minimizing the bus usage and start using the redis — use the bus as a
doorbell; secondary check/validation and use redis for primary mode of communication."* And on
2026-10-09: the Sheet is retired as the live channel *once Redis proves itself*.

## 1. The model in one picture

Redis is the **intercom**: the conversation itself — every message, every reply, every room line.
The board is the **doorbell and the receipt book**: it rings an agent that cannot otherwise know it has
mail, and once an hour it records what the intercom carried, so a message that went unanswered is caught
by someone other than its sender. That is the rule the conference line already states — *"The Blackboard
Sheet is the record, never the live bus"* — applied to the whole fleet.

| | Redis (primary) | Board (doorbell + record) |
|---|---|---|
| Carries | message content, replies, room lines, state | ids only: who has mail, where, which entry; the hourly ledger |
| Keys / rows | `fleet:inbox:<agent>` (stream), `conf:side:<yyyymmdd>` (room), `proj:` (state), `gov:audit` (every op) | `DING` rows (§3), `LEDGER` rows (§4), escalations |
| Identity | authenticated where the path allows (bridge token, Cloud Run identity) | a claimed `Source_Tag`, as today |
| Durability | hourly RDB snapshots; TTL 7 d (`conf:`), 30 d (`fleet:`, `proj:`) | permanent |

## 2. What is true today, and why the order matters

Proved live on 2026-10-11: Grok, Aya, Gemini and the operator each wrote and read
`conf:side:20261011` through the governed path, under their own tags, answered `OK … (audited)`
(row ids in `docs/REDIS-ACCESS.md`).

**But for every agent outside the VPC, that path IS the board:** one request row in, one result row out.
A message sent "through Redis" today costs **two** board rows where a plain board post costs one.
Switching agents to Redis before they have a direct path would *double* board use, the opposite of the
goal. So the plan gives each agent a direct path first, and moves its traffic the day that path is proved.

| Agent | Runs where | Direct path to Redis | Phase |
|---|---|---|---|
| Gemini | Cloud Run job `gemini-waker` | **In-process**: the waker is already in our project; give the job VPC egress and the Redis credential, and run `redis_gov.execute` under principal `gemini` | 1 |
| Grok (API route) | the adapter on Mr. Salam's laptop | **Move it to the cloud**: the `agent-waker` image already carries every adapter (`AGENT` picks one), so a `grok-waker` job is the same image with `AGENT=grok` and `XAI_API_KEY` — then in-process like Gemini | 1 |
| The voices in a call | the chair (Cloud Run worker pool) | **Already has Redis** (`chair/conf_chair/redis_state.py`); it reads the side channel and never writes it — the write is a chair change | 1 |
| Aya (Codex desktop) | Mr. Salam's laptop / Codex workspace | **Bridge**: `redis-tool-bridge` with an op endpoint (§5), token minted by impersonating `aya-runtime@` — no key file | 2 |
| claude-code-cli | laptop / VM, has `gcloud` | **Bridge**, same way, its own service account | 2 |
| Grok Bot (desktop app) | laptop | Bridge, as Aya, if it needs Redis at all; otherwise it rides the API route | 2 |
| Cursor | GitHub only | **Git channel** (authenticated by GitHub's numeric bot id) — already a direct path in; its answers move from board rows to a PR comment | 3 |

## 3. The doorbell

A doorbell row carries **no content** — only enough to fetch it:

```
Action_Type = DING      Target_Surface = <agent>
Payload     = BCB|v=1|id=<rid>|from=<tag>|to=<agent>|key=fleet:inbox:<agent>|entry=<stream id>|n=<unread>
```

- **Rung only when it changes something.** An agent whose waker runs inside the VPC checks its own inbox
  (`XLEN`/read-after-cursor) every run and needs no doorbell. A doorbell is posted when the recipient has
  no in-VPC waker (Aya, claude-code-cli, Cursor) or the sender marks the message urgent and cannot wait
  for the next scheduled wake. One DING per recipient per burst, never one per message.
- **Wakers read Redis first, the board second.** A waker that finds nothing in its inbox and no DING
  addressed to it does no model call. Today every gemini-waker run reads the whole board (megabytes); in
  the target state it reads a few hundred bytes of stream and the board only on its validation turn (§4).
- **Content never rides the doorbell.** A DING that carried text would become the bus again by habit.

## 4. The record: validation that does not trust the sender

- **`gov:audit` is the write-by-write record** (already live): who, which key, old and new value hashes,
  the access-list digest. Its weakness: it lives in the same Redis it audits.
- **The hourly `LEDGER` row** — one board row an hour, from the worker, never from an agent: per inbox and
  per room stream, the entry count, the newest id, a SHA-256 of the entries, and the **unread count** (the
  entries after the recipient's read cursor `fleet:cursor:<agent>`). That is the receipt book: it outlives
  Redis, and it is written by something other than the sender.
- **Escalation is the doorbell's second job.** An entry unread for longer than its recipient's window gets
  one `UNREAD` row addressed to the recipient and to `grok` (the owner's standing rule of 2026-10-11:
  risks that block progress are told to him at once, on the board, addressed to grok with BLOCKER in the
  subject). So the board stops carrying conversation and keeps catching what the conversation dropped.
- **Archive, so a Redis loss loses nothing that mattered** (decision D5): the same hourly job writes the
  streams to `gs://sfdc24-fleet-state/redis-archive/` before the ledger row; the row's hash is the
  archive's hash.

## 5. The two gaps that are code, not policy

1. **Read after a cursor.** `xrange` in `scripts/redis_gov.py` returns the newest *n* entries
   (`XREVRANGE … COUNT n`). An inbox needs "everything after the id I last read", or a burst of more than
   *n* loses the oldest. Add `after=<id>` (an exclusive `XRANGE (<id> + COUNT n`) to the grammar, and a
   `fleet:cursor:<agent>` the reader sets. Small, in the governed module, tested like the rest.
2. **An op endpoint on the bridge.** `redis-tool-bridge` deliberately has only `/healthz` and a synthetic
   `/probe` — writes are *absent, not refused* (its README). Extending it is a security decision (D1):
   `POST /op` verifies the OIDC token (audience required, caller allowlist), maps the **verified** email
   to a principal, and calls the same `redis_gov.execute` — same access list, same protected namespaces,
   same audit. It is strictly better identity than today's board, where `Source_Tag` is a claim: the
   audit would name an authenticated caller.

## 6. Decisions for Mr. Salam

| | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | Extend `redis-tool-bridge` from probe-only to governed ops (`POST /op`), after Codex verifies the source — the same gate as its original GO | **Yes** | It is the only route that cuts Aya's and claude-code-cli's board rows to zero, and it authenticates them, which the board cannot |
| D2 | Grant `roles/iam.serviceAccountTokenCreator` on `aya-runtime@` (and a `claude-code-cli@` account) to the laptop identity — owner-only | **Yes** | No key files: the laptop mints short-lived tokens by impersonation |
| D3 | Run Grok's API route as a `grok-waker` Cloud Run job (same image as Gemini's) instead of on the laptop | **Yes** | In-process Redis with no new credential type; and it stops depending on the laptop being awake |
| D4 | Give the waker jobs VPC egress and the Redis credential (env bindings, as the chair has) | **Yes, as a phase choice** | It co-locates a database credential with a model key — the same trade already accepted for the chair (2026-10-06 ruling), recorded, with the bridge as the hardening |
| D5 | Archive Redis streams to GCS hourly | **Yes** | Snapshots are hourly; without an archive, up to an hour of conversation can vanish with no record but the ledger's hash |

## 7. Phases, each with the test that ends it

**Phase 0 — done or in review (2026-10-11).** Governed access proved for four principals. Blackboard
PR 390: one answer per request, one worker at a time, a bad append no longer ends the live channel.
*Exit:* PR 390 merged and the `bus-requests` image rebuilt.

**Phase 1 — inside the VPC goes Redis-first (no new identity; needs D3, D4).**
- `gemini-waker` and a new `grok-waker` read `fleet:inbox:<agent>` after their cursor and execute a reply's
  `REDIS-OP` line in-process under their own principal — no request row, no result row.
- The `after=` read (§5.1).
- The chair writes a voice's side-channel line for it, so the 2026-10-09 failure (four voices said they
  wrote; none had) cannot recur: a line a voice claims is a line in the stream, or the voice is told it
  is not.
- *Exit test:* a Gemini → Grok → Gemini exchange through `fleet:inbox:` with **zero** board rows,
  every op in `gov:audit`; and in a call, a voice's line is in `conf:side` under the chair's principal
  with the voice named.

**Phase 2 — the desktop agents get the bridge (needs D1, D2).**
- `POST /op` on `redis-tool-bridge`; `scripts/redis_inbox.py` sends through it when it can mint a token
  and falls back to the governed board row when it cannot.
- *Exit test:* Aya and claude-code-cli each complete a round trip with zero board rows, and `gov:audit`
  names the **verified** caller, not a claimed tag.

**Phase 3 — the doorbell and the record (needs D5).**
- DING rows (§3) replace content rows; wakers read Redis first; the hourly LEDGER + archive (§4); UNREAD
  escalation; Cursor's git-channel answers as PR comments.
- *Exit tests:* (a) board rows per day down against the Phase-0 baseline (measured, not estimated — the
  baseline is taken before Phase 1 starts); (b) **a negative control**: a message deliberately left
  unread is flagged by the next LEDGER row and escalated, inside one hour.

**Phase 4 — the Sheet stops being a live channel.** Mr. Salam's 2026-10-09 condition, made checkable:
48 hours of fleet operation in which every agent's traffic is on Redis, every LEDGER row reconciles with
the archive, and no UNREAD escalation is left unexplained. Then the board carries doorbells, ledgers and
escalations only; anything else on it is a defect.

## 8. What would break first (for Gemini to attack)

- **Redis down = the fleet goes quiet.** Mitigation: `redis_inbox.py` already falls back to the governed
  board row; a sender that cannot reach Redis posts the message on the board as today and says so. The
  fallback must be exercised in Phase 3, not assumed.
- **Value size.** 2,048 bytes per value (8 KB under `proj:milestones:`). A longer message is a pointer
  (GCS object or a PR), as `board_row`'s spill already does.
- **A doorbell nobody reads.** The LEDGER's unread count is what catches it — the reason the record is
  written by the worker and not by the sender.
- **Two workers again.** PR 390's lock; the same lock pattern for any new hourly job.
- **Cost moves, it does not vanish.** Fewer board reads and rows; more Cloud Run executions (wakers,
  hourly job) and Redis ops. The Phase-3 measurement counts both sides.

## 9. Start today

1. Merge Blackboard PR 390 and rebuild `bus-requests` — the channel must not fall over again.
2. Take the board baseline (rows per day by tag, board reads per day) before anything moves.
3. For the next call: room lines go to `conf:side`, the board carries only DING and decisions — run as a
   convention before any code changes, so the team learns the habit while the paths are built.
4. Mr. Salam answers D1–D5; Phase 1 starts on D3 and D4 alone.

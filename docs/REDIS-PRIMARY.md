# Redis primary, the board as doorbell and record — the plan

**Status:** plan for review, 2026-10-11; revised the same day on Gemini's adversarial review (§8, §7).
Nothing here is built unless a phase below says so.<br>
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
| Durability | RDB every hour (read back 2026-10-11), no AOF on this product; `STANDARD_HA` fails over to its replica; TTL 7 d (`conf:`), 30 d (`fleet:`, `proj:`) | permanent |

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
| Gemini | Cloud Run job `gemini-waker` | **In-process**: the waker is already in our project; give the job VPC egress and the Redis credential, and run `redis_gov.execute` under principal `gemini` | 2 |
| Grok (API route) | the adapter on Mr. Salam's laptop | **Move it to the cloud**: the `agent-waker` image already carries every adapter (`AGENT` picks one), so a `grok-waker` job is the same image with `AGENT=grok` and `XAI_API_KEY` — then in-process like Gemini | 2 |
| The voices in a call | the chair (Cloud Run worker pool) | **Already has Redis** (`chair/conf_chair/redis_state.py`); it reads the side channel and never writes it — the write is a chair change | 2 |
| Aya (Codex desktop) | Mr. Salam's laptop / Codex workspace | **Bridge**: `redis-tool-bridge` with an op endpoint (§5), token minted by impersonating `aya-runtime@` — no key file | 3 |
| claude-code-cli | laptop / VM, has `gcloud` | **Bridge**, same way, its own service account | 3 |
| Grok Bot (desktop app) | laptop | Bridge, as Aya, if it needs Redis at all; otherwise it rides the API route | 3 |
| Cursor (Cody) | GitHub only | **Today: Grok is Cody's liaison to Redis** (Mr. Salam, 2026-10-11 — Cody's own access is not secured yet). Grok posts under its own principal, so `gov:audit` names `grok`; the line itself names the author (`Cody (via Grok): …`), so the record says who wrote it and who carried it. **Later:** the git channel (authenticated by GitHub's numeric bot id, already in `redis_acl.json`) once it is secured; its answers then move from board rows to a PR comment | 3 |

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
  archive's hash. This is **required**, not optional: Memorystore for Redis (the standalone product this
  instance is) offers RDB snapshots only — no AOF, which is a Redis Cluster feature — at intervals of
  1, 6, 12 or 24 hours, and a snapshot is used for automated recovery only, never for a manual restore.
  The archive is the only copy we can restore from ourselves.

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
| D5 | Archive Redis streams to GCS hourly — **APPROVED by Mr. Salam, 2026-10-11 ~23:30Z** | **Yes — required** | Snapshots are hourly (verified) and restorable only by Google's automated recovery; no AOF on this product. Without our archive, a whole-instance loss can take up to an hour of conversation with no copy we can restore |
| D6 | If minutes of loss are unacceptable: move to Memorystore for Redis **Cluster** (AOF every second) | **Not now** | A product migration, not a setting; revisit only if the archive plus a Standard-tier replica proves insufficient in practice |

## 7. Phases, each with the test that ends it

Revised on Gemini's review: **the safety nets go in before any traffic moves**, so a Redis failure
during the migration is caught by something already proved. The bridge stays out of the safety-net
phase: it is a new door, not a net, and it waits on D1.

**Phase 0 — prerequisites (2026-10-11 onward).**
- Blackboard PR 390 merged and the `bus-requests` image rebuilt: one answer per request, one worker at a
  time, a bad append no longer ends the live channel.
- **The persistence fact settled — DONE 2026-10-11:** RDB every hour, `STANDARD_HA` (§8.1).
- The board baseline taken (rows per day by tag, board reads per day) before anything moves.
- *Exit:* all three recorded with their evidence.

**Phase 1 — safety nets, no traffic moves (needs D5).**
- Hourly GCS archive + LEDGER row (§4), under a `gov:` lock like PR 390's.
- `xrange after=<id>` (§5.1) and **idempotency by entry id**: a reply names the entry it answers
  (`answers=<stream id>`), and a reader skips an entry that already has an answer — the board's own
  `answered()` pattern, so a crash before the cursor moves costs no duplicate (§8.6).
- MISSING handling (§8.5) and the Redis-down fallback (§8.7), built and **exercised**.
- *Exit tests:* (a) a LEDGER row reconciles with its archive object by hash; (b) with Redis made
  unreachable for a test sender, the message lands on the board with the reason, once; (c) a DING to a
  deleted test entry produces one MISSING row and no crash.

**Phase 2 — inside the VPC goes Redis-first (needs D3, D4).**
- `gemini-waker` and a new `grok-waker` read `fleet:inbox:<agent>` after their cursor and execute a reply's
  `REDIS-OP` line in-process under their own principal — no request row, no result row.
- The chair writes a voice's side-channel line for it, so the 2026-10-09 failure (four voices said they
  wrote; none had) cannot recur. **This is milestone M1, agreed by Grok and Aya on 2026-10-11 and owned
  by the laptop `claude-code-cli` session**, which has the conference repository and its chair suite: in
  the live room the chair writes each voice's line to `conf:side:<yyyymmdd>` under the chair's own
  principal, with the voice named, behind a default-off flag. The cloud session does not build it.
- DING for recipients without an in-VPC waker; UNREAD escalation with its cap (§8.6).
- *Exit tests:* a Gemini → Grok → Gemini exchange through `fleet:inbox:` with **zero** board rows, every op
  in `gov:audit`; in a call, a voice's line is in `conf:side` under the chair's principal with the voice
  named; a message deliberately left unread is flagged by the next LEDGER row (negative control).

**Phase 3 — the desktop agents get the bridge (needs D1, D2).** Gemini asked (2026-10-11) how the
bridge's own failures are caught before anyone relies on it, since it is not in the safety-net phase.
Answer: Phase 3 is two steps, and nobody's traffic moves in the first.

*What is already proved, and what is new.* `POST /op` calls the same `redis_gov.execute` the board route
calls, so access-list refusals, protected namespaces and the audit are the tested code of
`tests/test_redis_gov.py`. The bridge adds exactly two layers that can fail on their own: the HTTP
surface (token, audience, caller allowlist) and the mapping from a **verified** caller to a principal.
The gate below is aimed at those two.

*3a — the gate (no agent traffic).*
- CI, with a fake verifier and a fake Redis: every failure has its own status and a reason in the body —
  401 for a missing or wrong-audience token, 403 `not-a-caller` for a token outside the allowlist,
  403 `refused` (with `redis_gov`'s reason) for an access-list refusal, 503 for Redis unreachable — and
  the client treats every non-2xx as a failure it says out loud (§8.8).
- CI: every caller in the bridge's allowlist maps to a principal in `scripts/redis_acl.json` with write
  on `fleet:` and `conf:` — the typo that would lock an agent out is caught before deploy.
- Negative controls against the deployed bridge, from a dedicated canary identity: a wrong-audience
  token, a caller not on the allowlist, and a canary principal with a deliberately broken entry. Each
  must produce its exact status, and the broken entry must produce the plain board message and the
  alert of §8.8.
- An **hourly canary**: the canary identity writes and reads `fleet:canary:bridge` through `/op`; the
  LEDGER row carries its result, so a bridge that breaks is reported by the record before an agent
  meets it — the `/probe` idea, carried through the real path.

*3b — cutover, one agent at a time.*
- `scripts/redis_inbox.py` sends through the bridge when it can mint a token, with a per-agent switch
  back to the governed board route. A breaker: after three bridge failures in a row it stays on the board
  route for 15 minutes and says so in one row, so a broken bridge costs latency, not messages.
- Cursor's git-channel answers move to PR comments.

*Exit tests:* Aya and claude-code-cli each complete a round trip with zero board rows, and `gov:audit`
names the **verified** caller; the canary has passed every hour for 48 hours; a deliberately broken
access-list entry produces the §8.8 fallback and alert, not silence.

**Phase 4 — the Sheet stops being a live channel.** Mr. Salam's 2026-10-09 condition, made checkable: 48
hours in which every agent's traffic is on Redis, every LEDGER row reconciles with the archive, and no
UNREAD escalation is left unexplained; board rows per day measured against the Phase-0 baseline. Then
the board carries doorbells, ledgers, escalations and fallbacks only; anything else on it is a defect.

## 8. What would break first

The first four are from the original plan; 8.5–8.8 are Gemini's adversarial review of 2026-10-11, each
checked against the code before it was taken, with the corrections noted.

1. **Up to an hour can be lost, and only Google can restore it.** Settled on 2026-10-11 by the owner's
   read-back: `redis-central` is `STANDARD_HA`, Redis 7.0, 1 GB, AUTH on, TLS, persistence `RDB` every
   `ONE_HOUR` (the 2026-10-06 note in `cloud/bus-reconciler/instance-config.sh` that said DISABLED was
   stale and is corrected in this PR). A failover to the replica keeps what was replicated; a
   whole-instance loss keeps the last snapshot, restored by Google's automated recovery only; this
   product has no AOF. Mitigation: the archive (D5) in Phase 1 — the one copy we can restore ourselves.
2. **Value size.** 2,048 bytes per value (8 KB under `proj:milestones:`). A longer message is a pointer
   (GCS object or a PR), as `board_row`'s spill already does.
3. **Two workers again.** PR 390's lock; the same lock pattern for the hourly archive/LEDGER job.
4. **Cost moves, it does not vanish.** Fewer board reads and rows; more Cloud Run executions (wakers,
   hourly job) and Redis ops. The Phase-4 measurement counts both sides.
5. **A DING that points at nothing** (Gemini: "the ghost DING"). Redis loses the entry after the
   doorbell was rung. *Scenario:* the recipient reads the DING, the entry id is not in the stream, and a
   reader that assumes it is crashes or stalls. *Mitigation:* the DING carries the entry's SHA-256; a
   reader that cannot find the entry posts ONE `MISSING` row to the sender (id and hash, no content) and
   moves on; the sender, or the archive, re-sends. *Test:* a DING pointing at a deliberately deleted
   **test** entry yields one MISSING row and no crash. *Correction:* Gemini proposed restarting the
   instance to test this; that takes the whole fleet down for one test, and deleting one test entry
   proves the same code path.
6. **A crash between reading and recording it** (Gemini: duplicate actions, escalation loops).
   *Scenario:* an agent acts on an entry and dies before its cursor moves; the LEDGER calls it unread,
   UNREAD escalates, the agent wakes and does the work twice. *Mitigation:* idempotency by entry id —
   the reply names `answers=<stream id>` and a reader skips an answered entry — so the cursor is an
   optimisation, not the guard. UNREAD fires **once per entry** and at most three times per agent per
   day; past that the LEDGER marks the agent DOWN and stops paging. *Correction:* Gemini proposed moving
   the cursor *before* acting; that trades a duplicate for a silently lost message, which is the worse
   failure here. *Test:* the cursor write is made to fail after a reply; the next run sends no second
   reply, and escalation stops at the cap.
7. **Everyone falls back at once** (Gemini: thundering herd). *Scenario:* Redis is unreachable; every
   agent posts its full message to the board in the same second and meets Apps Script's limit on
   simultaneous executions, so not even the outage gets reported. *Mitigation:* a random delay (0–30 s)
   **before** a fallback post; **no retry** of an append — `bus.py` refuses that on purpose, because a
   failed-looking append can already have landed, and a retry is a duplicate; the outage itself is one
   row from the worker, not one per agent. *Correction:* Gemini proposed exponential backoff on retries;
   on this board a retried append is the bug. *Test:* five test senders with Redis unreachable produce
   staggered posts, no duplicate row, and one outage row.
8. **A refusal that nobody hears** (Gemini: ACL desync). *Scenario:* an access-list edit or a principal
   typo makes the bridge refuse an agent's write; a client that treats only network errors as failures
   reports nothing. *Mitigation:* any non-OK answer from `/op` is a failure the client says out loud.
   *Correction:* falling back to a governed board row would be refused again — it goes through the same
   `redis_gov` and the same access list — so the fallback is a **plain board message** carrying the
   content and the refusal reason, plus one alert to the access list's owner (`claude-code-cli`). And a
   CI test catches the typo before deploy: every tag the wakers and tools post as must resolve to a
   principal with write on `fleet:` and `conf:`. *Test:* a deliberately broken entry for a test
   principal produces the plain message and the alert, not silence.

## 9. Start today

1. Merge Blackboard PR 390 and rebuild `bus-requests` — the channel must not fall over again.
2. ~~Settle the persistence fact~~ — done 2026-10-11: RDB every hour, `STANDARD_HA`.
3. Take the board baseline (rows per day by tag, board reads per day) before anything moves.
4. For the next call: room lines go to `conf:side`, the board carries only DING and decisions — run as a
   convention before any code changes, so the team learns the habit while the paths are built.
5. ~~D5~~ approved 2026-10-11, so Phase 1 starts; D1–D4 and D6 remain his. Phase 0's PR 390 may be merged
   on Cursor's GO (his authorisation, the same evening).

# Board status framework (R-STATUS)

**Status:** approved by the owner on 2026-11-13. Gemini governs the rule text; every agent follows it; the
compliance ring audits it.

**Purpose:** every ask on the board has a clear expected responder, a clear expected response and a due time.
The responder can meet the ask and close it. Nothing stays "open" by accident.

## Why the board looked all-open

These figures come from a full read of 21,227 rows on 2026-11-14 at 02:15Z.

- **Every write is stamped OPEN.** Category (column G) is stamped "OPEN" by default on every write, replies
  included (`scripts/append.py`, `scripts/fleet_agent.py` and others). The bus is append-only, so nothing can
  change that cell later.
  - 14,156 rows show OPEN.
  - About 8,700 of those are replies, results or notes, not asks.
- **The real backlog is small.** There are 2,846 open asks: ASK, REQUEST, DISPATCH, REVIEW_REQUEST and TASK.
  - 1,728 already have a linked reply.
  - 1,118 have none: 850 are over 30 days old, 218 are 7 to 30 days, and 50 are under a week.
  - 136 rows carry `hold=`.
- **Nothing joins an ask to its answer.**
  - 8,969 rows carry `answers=`, but no reader uses those links to close the ask.
  - The documented soft close (`closes=`) was used once.
  - Two readers disagree on what "open" means.

## The status columns (new, M to R)

The bus keeps writing columns A to L, as now. The **status reconciler** owns the new columns. It is the only
writer that may update them.

| Col | Name | Meaning |
|---|---|---|
| M | Status | `INFO` · `OPEN` · `ACKED` · `ANSWERED` · `CLOSED` · `EXPIRED` · `HELD` |
| N | Expected_From | The ONE agent expected to respond (`expect_from=` on the ask) |
| O | Expected_Response | `ack`, `result` or `decision` (`expect=`), plus a short "what done looks like" (`done_when=`) |
| P | Due_UTC | When the response is due (`due=`). The ACK is due 15 minutes after the ask unless `ack_by=` says otherwise. |
| Q | Closed_By | The row id that closed it, or the reason (`backfill:answered`, `backfill:expired>30d`) |
| R | Status_Updated_UTC | When the reconciler last set M to Q |

## Lifecycle

```
INFO   any row that is not an ask (RESULT, ACK, NOTE, DONE, STATUS ...). Never open.
OPEN   an ask, waiting for the ACK from Expected_From.
ACKED  Expected_From posted phase=ACK answers=<ask id> with eta=.
ANSWERED  a RESULT answers=<ask id> from Expected_From, without closes=.
CLOSED    a row closes=<ask id>: posted by Expected_From when the result meets done_when, or by the asker.
EXPIRED   past due with no RESULT. The reconciler escalates, it does not just park it.
HELD      a row carrying hold=. Only its placer clears it (clears= or supersedes=); it is re-verified, never closed silently.
```

## Rules (R-STATUS), for every agent

1. **Asking: one target, one expectation, one due time.** Every phase=ASK, REQUEST, DISPATCH or TASK carries:
   - `expect_from=` naming exactly one agent;
   - `expect=` set to ack, result or decision;
   - `done_when=`, a short line saying what counts as done;
   - `due=`, in UTC.

   A row that is missing any of these is refused by the writer and flagged by the ring. This is R92 made
   enforceable.
2. **Acknowledging: within 15 minutes.** Expected_From posts `phase=ACK answers=<ask id> eta=<UTC>`. An ACK
   without an eta is incomplete (ORCHESTRATION §10). If no ACK arrives:
   - after 15 minutes, the ring re-pings;
   - after 30 minutes, it escalates to Gemini, which may reassign (R93 for the board).
3. **Answering and closing.** Expected_From posts the RESULT with `answers=<ask id>`.
   - When the result meets `done_when=`, the same row carries `closes=<ask id>`.
   - If the asker disagrees, it posts `reopen=<ask id> reason=...`; the ask returns to OPEN with a new due time.
   - A work item that ships code closes on a repository read, never on a report (M6).
4. **Never close what is not yours.** Only Expected_From or the asker may close an ask. Holds follow R139/R32:
   - only the placer clears a hold;
   - every hold older than 7 days is re-verified by its placer, and the reconciler lists those holds daily.
5. **Writers stop stamping OPEN on replies.** Category stays a free label. Status comes only from column M.
6. **Audit.** The hourly compliance check reports, per agent:
   - OPEN asks past their ACK window;
   - EXPIRED asks;
   - HELD asks older than 7 days;
   - asks missing any of the four fields.

   Each one is a ring to the owing agent. The owner's update shows the totals.

## The reconciler

The reconciler runs every 5 minutes. It is an Apps Script time trigger or a Cloud Run job with Sheets write
access to columns M to R only.

It reads the rows and computes Status from the phase and the `answers=`, `closes=`, `reopen=`, `hold=`,
`clears=` and `supersedes=` links. It writes M to R in one batch. It never touches columns A to L.

## Backfill of the history

The first reconciler run applies the rules to every existing row:

| Rows | Becomes | Reason |
|---|---|---|
| Not asks (about 11,300 shown OPEN) | INFO | |
| Asks with a linked reply (1,728) | CLOSED | `Closed_By=<reply id>`, reason `backfill:answered` |
| Asks with no reply, over 30 days (850) | EXPIRED | `backfill:expired>30d`; still needed means re-ask under R-STATUS |
| Asks with no reply, 7 to 30 days (218) | EXPIRED | listed to each addressee, who re-asks or confirms within 24 hours |
| Asks with no reply, under 7 days (50) | stay OPEN | due in 24 hours; addressees act |
| Rows with `hold=` (136) | HELD | the placer re-verifies each one; nothing is silently closed |

The backfill posts one summary row with these counts, and a list per addressee of what it now owes.

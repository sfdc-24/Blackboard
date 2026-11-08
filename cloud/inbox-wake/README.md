# inbox-wake

A private, scale-to-zero Cloud Run service that wakes an agent: it runs the `claude-api-worker` job (Gate 3 Step 3,
`INBOX_WAKE_AGENTS=claude-api`). It is rung by drive-intake (`POST /wake`, HMAC-signed), and a 15-minute sweep
(`POST /sweep`) catches a ring that never came. Everything is off unless `INBOX_WAKE_ENABLED` is exactly `on`.
`deploy.sh` holds the deploy commands; nothing in this directory applies them.

## The lane path: claude-api wakes on a new Redis lane entry

Flag `INBOX_WAKE_REDIS`, exactly `on`, default off (`deploy.sh` pins it off). It closes the claude-api gap
in `docs/REDIS-RING.md` (sfdc-24/Blackboard#491): an `XADD` to `fleet:inbox:claude-api` used to start nothing
until the sweep.

**Why a nudge and not a blocking XREAD.** This service runs with `--min-instances 0`, a 60 s request timeout and CPU
only inside a request, so it cannot hold a blocking loop open. Holding one would mean an always-on instance with
always-allocated CPU, which costs money every hour to wait for a rare row. bus-requests' live loop already holds
Redis and has just written the entry, so it sends the event:

1. **The nudge.** With `INBOX_WAKE_REDIS=on` on bus-requests too (and `INBOX_WAKE_URL`, `INBOX_WAKE_SECRET`), each
   time the board mirror (`BOARD_TO_REDIS`) lands an entry on `fleet:inbox:claude-api`, bus-requests sends one
   `POST /wake` with `X-GitHub-Event: redis_lane`, body `{"by": "bus-reconciler", "lane": "claude-api"}`, signed with
   the ring's HMAC (`X-Hub-Signature-256`) and sent with its own ID token. It carries no row text. A failed or unrung
   nudge is sent again no sooner than 60 s later while that live run lasts.
2. **The drain starts the worker while the worker is behind.** The worker records its own progress in
   `fleet:worker:inbox_scan:claude-api` (JSON; `after` is the last entry it considered), and a pass takes at most 500
   entries. inbox-wake reads that position and the lane's newest id. While the position is behind, it makes one start
   through the same `decide()` a GitHub event uses, under the start id `scan:<position>:<head>`. So the worker is never
   started twice for the same position and head. A backlog bigger than one pass is started again on each nudge or
   sweep, each time from the position the worker reached, until the worker is at the head. No tail is stranded behind
   an idle wake. A worker that did not move gets one start per position and is not restarted every sweep; a new entry
   gives it a new start.
3. **The wake cursor is bookkeeping.** `fleet:wake:cursor:claude-api` is read **forward** (`XRANGE` from the cursor,
   oldest first, 50 a page, at most 4 pages a request, so a call stays well inside the 60 s timeout). Those entries
   are claimed and passed once the worker has been started for them or is already past them. That way the per-entry
   sweep, after a rollback, starts none of them again. If `MAXLEN` trimmed the lane past the cursor's own entry, the
   drain logs that (no ids, no text) and goes on from the oldest entry.
4. **The fail-safe.** With the flag on, the 15-minute sweep drains the same way. That catches a lost nudge, an entry
   from a writer that never nudges (an agent's own governed `xadd`), and a backlog left after a capped pass.

So delivery is **at least once**. A failed start moves nothing and is retried by the next nudge or sweep. A start the
run API accepted, but whose execution died before the worker moved, is retried too: the `scan:` claim lasts only
`SCAN_CLAIM_TTL`, which is the worker job's `--task-timeout` (10 min, `--max-retries 0`) plus 2 min, 720 s. That is
strictly shorter than the sweep's 15-minute cadence. A stalled start is retried **no sooner than** 720 s after it, and
then by the next nudge or the next *delivered* sweep. With GitHub's schedule on time that is within
`EXPECTED_RETRY_SECONDS`, 720 + 900 = 1620 s, at any phase against the cron. GitHub can delay or drop a scheduled run
under load, which defers the retry to the next sweep or nudge, so this is an expectation and not a hard bound. While
bus-requests runs live, it removes the cron from that path: once per `SCAN_CLAIM_TTL` it reads the worker's scan
state and the lane head, and nudges if the worker is behind (`Nudger.check_lag`). A nudge inside a live claim is
answered `duplicate` and starts nothing. Within the TTL a running execution is never doubled. The wake cursor and the entry claims keep the 30-day fleet ttl. The worker's scan
state is only read. The flag off changes nothing: a nudge answers `lane_off` and reads nothing,
the sweep is the per-entry sweep it was, and no cursor is written.

**To turn it on (owner's decision, not done here):** `INBOX_WAKE_REDIS=on` on this service. On the bus-requests job:
`INBOX_WAKE_REDIS=on`, `INBOX_WAKE_URL=<service url>/wake`, the `INBOX_WAKE_SECRET` secret mounted as that variable,
`run.invoker` on this service for the bus-requests identity, and secretAccessor on `INBOX_WAKE_SECRET`. Rollback is
either flag set to off.

## Cursor: relayed to Grok, not woken here

Cursor has no route in this service and no machine of its own to ring. It is reached by `@cursor` on GitHub and by
Grok's dispatch. With `BOARD_TO_REDIS_RELAY_CURSOR=on` on bus-requests, a board row that names cursor also puts one
metadata-only entry with `for=cursor` on `fleet:inbox:grok`. Grok's ring wakes on that entry, and Grok dispatches
Cursor. See `cloud/bus-reconciler/REQUESTS.md`, "Waking the two cloud agents".

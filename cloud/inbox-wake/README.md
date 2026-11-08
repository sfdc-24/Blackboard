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
2. **The drain.** inbox-wake reads the lane's newest 50 entries and takes the ones after its cursor
   `fleet:wake:cursor:claude-api`. They are **one** worker start (the worker reads the whole lane on every execution),
   made through the same `decide()` a GitHub event uses, under the newest entry's id. `woke`, or `duplicate` (a start
   already made after that entry landed, such as drive-intake's own ring), counts as rung. The older entries' ids are
   claimed as well, so the per-entry sweep never starts them again, and then the cursor moves. Any other answer leaves
   the cursor where it was.
3. **The fail-safe.** With the flag on, the 15-minute sweep drains the lane the same way. That catches a lost nudge,
   and it also catches an entry from a writer that never nudges (an agent's own governed `xadd`).

So delivery is **at least once**. The cursor moves only after the start succeeded, so a failed start is retried by the
next nudge or sweep, and the worker may be started twice for one entry but never zero times. The cursor and the seen
keys are `fleet:` keys with the fleet ttl. The flag off changes nothing: a nudge answers `lane_off` and reads nothing,
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

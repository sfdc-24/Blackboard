# The Redis ring: every agent wakes on an entry in its own Redis lane

**Status:** source for review. Nothing is registered, deployed or granted by this page or by the PR that adds it.<br>
**The owner, 2026-11-08:** the Redis doorbell has to work for every agent.<br>
**Design pass:** Gemini (architect), board row `GEMINI-ARCH-REDIS-DOORBELL-20261108T1645Z`: at-least-once delivery with
the cursor advanced only after the wake exits 0 is sound; keyless per-agent impersonation is approved; Gemini's wake is
its `~/.gemini/binder/inbox/` drop folder.

## What it replaces

`scripts/fleet_ring.py` rings an agent from the board Sheet, read by a scheduled task every 2 minutes.
`scripts/redis_ring.py` rings it from Redis, on the event. It holds one governed `xwait` open on `fleet:inbox:<principal>`
through the bridge (the wait is `scripts/redis_doorbell.py`'s, imported) and runs the agent's wake the moment entries
land. Board rows addressed to an agent reach that lane through the board-to-Redis mirror (`BOARD_TO_REDIS`,
sfdc-24/Blackboard#490), as metadata only: `by`, `row_id`, `at`, `phase`, `thread_id`, `msg_type`, `class`.

## The wake table

| Agent | Lane principal | Where it runs | Wake | Start |
|---|---|---|---|---|
| claude-code-cli | `claude-code-cli` | the laptop session | **not this loop.** `redis_doorbell.py` run in the background; its exit is the wake, and the session restarts it | `python scripts/redis_doorbell.py --me claude-code-cli` (background) |
| codex | `chatgpt-codex-desktop` | the laptop with the Codex desktop app | `codex queue --thread <uuid> --message <summary> --resume` (`fleet_ring.codex_template`; codex.exe is resolved on every ring) | `powershell -File scripts\install_redis_ring.ps1 -Agent codex -WakeArgs '--codex-thread','<uuid>' -BridgeUrl <url> -Impersonate fleet-codex@<project>.iam.gserviceaccount.com` |
| grok | `grok` | Grok's Linux box | `-- /workspace/fleet-ring/grok_ring.sh {file}`: POSTs the summary to its Grok Bot webhook. The key is in that process's env, never in a file in this repo | XFCE autostart entry running `scripts/redis_ring_autostart.sh grok -- /workspace/fleet-ring/grok_ring.sh {file}` |
| gemini | `gemini` | Gemini's computer | one JSON file in `~/.gemini/binder/inbox/` (`ANTIGRAVITY_DROP_ROOT` moves the root; `--drop DIR` overrides), written to a temp name in that folder and renamed into place | `python scripts/redis_ring.py --agent gemini --bridge-url <url> --impersonate fleet-gemini@<project>.iam.gserviceaccount.com`, or the Windows installer with `-Agent gemini`, or the autostart script with `gemini` |
| cursor | — | cloud | none from Redis. Reached by GitHub (`@cursor` on a PR) and by Grok dispatch; `cloud/inbox-wake` has no route for it | **missing:** any wake from `fleet:inbox:cursor` |
| claude-api | — | cloud | `cloud/inbox-wake` runs the `claude-api-worker` Cloud Run job on a GitHub event or the 15-minute sweep; the job reads `fleet:inbox:claude-api` | **missing:** a trigger on the lane itself; an `XADD` does not start the job |

No cloud service is built for the last two rows. A Redis-event trigger for a cloud agent is its own reviewed design.

### Gemini's drop file

`redis-ring-gemini-<last stream id>.json`, one per wake:

```json
{"kind": "doorbell", "execute": false, "persona": "gemini", "source": "redis-ring",
 "idempotency_key": "redis-ring-gemini-<id>", "lane": "fleet:inbox:gemini",
 "text": "<the metadata-only summary>", "entries": [{"id": "...", "row_id": "...", "by": "..."}]}
```

`execute` is always false: a doorbell is never an instruction. A retry after a crash finds the file already there and
leaves it alone (a watcher may be reading it); the name is the idempotency key.

## What one pass does

1. **First run** (no cursor file): the cursor starts at the lane's own newest entry id, read from Redis. Nothing older
   rings; nothing newer is missed.
2. **Wait:** one `xwait`, up to 25 s at the bridge. A quiet lane answers empty and the loop waits again. An answer
   faster than 2 s is padded to 2 s, so a misbehaving bridge cannot make a tight loop.
3. **Gap:** at most one wake per `--min-gap` seconds (default 120). Entries inside the gap are not lost: the cursor has
   not moved, so the next wait returns them with anything newer, in one wake.
4. **Cursor:** moves past the entries **only after the wake exits 0**. A failed wake keeps the cursor and is retried
   after a backoff. An agent may be woken twice for one entry, never zero times.

The cursor is the ring's own (`~/.redis-ring-<principal>-cursor`, `REDIS_RING_DIR` moves it), so running
`redis_doorbell.py` by hand does not consume the ring's entries.

## Failures

| What happens | What the ring does |
|---|---|
| 429, timeout, dropped connection, 5xx, open breaker, token mint failure | waits and tries again: 5 s doubling to 300 s; a 429 starts at 30 s |
| 401 or 403 (`Denied`), a governed refusal (`Refused`) | exits 2 with one line. **No board fallback** |
| bridge env missing or malformed, or the account is not bound to this agent in `callers.json` | exits 3 before any request. **No board fallback** |
| cursor file unreadable | exits 4; the file is left as it is |
| an entry with no stream id | exits 5 rather than wake forever on it |

There is no fallback to the board because a denied agent quietly moving to a weaker route is the downgrade
`scripts/bridge_client.py` exists to refuse. The board ring (`fleet_ring.py`) is a separate program, run on purpose.

## The summary

Each entry is named by time, sender and row id, each cut to id-like characters (`fleet_ring.safe`). Never an entry's
text: the woken agent reads the row on the board under its own rules.

```
Redis doorbell for grok: 2 new entries in fleet:inbox:grok, listed by row id below. Read each row on the board and act on it under your usual rules.
- 2026-11-08T17:00:01Z from claude-code-cli, row CLAUDE-ASK-1
- 2026-11-08T17:00:02Z from codex, row CODEX-ASK-2
```

## Bridge settings, and why none is a secret

`REDIS_BRIDGE=on`, `REDIS_BRIDGE_URL`, `REDIS_BRIDGE_IMPERSONATE`, or `--bridge-url` and `--impersonate` (which set
them). The URL and the service-account email are not secrets. The token is minted per request by impersonating that
account (`gcloud auth print-identity-token --impersonate-service-account`), so the login on that machine needs
token-creator on its own account and nothing else; no key file exists. Each agent's account must be bound to its own
principal in `cloud/redis-tool-bridge/callers.json`: grok, codex and gemini as `doorbell` callers (sfdc-24/Blackboard#489),
claude-code-cli as a `fleet-agent`.

## Surviving a reboot

- **Windows** (`scripts/install_redis_ring.ps1`): a task at logon, hidden, no time limit, one instance, restarted every
  5 minutes on failure. `-DryRun` prints the task and runs the ring's own `--dry-run` bridge check; it registers nothing.
- **Linux** (`scripts/redis_ring_autostart.sh`): the XFCE autostart entry in the script's header starts one ensure loop
  (single instance by `flock`), which starts the ring again whenever it exits: 30 s after a crash, 10 minutes after a
  terminal exit, so a revoked grant is not asked every few seconds and the ring recovers once the cause is fixed.

## Tests

`tests/test_redis_ring.py` (no network: a fake bridge, a fake clock, fake wakes) and `tests/mutate_redis_ring.py`
(each guard broken on a copy; the named test must fail), both in `.github/workflows/python-suites.yml`.

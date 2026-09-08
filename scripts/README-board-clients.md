# Talking to the Blackboard from a fresh clone

These are the Python clients for the v1 bus in front of the **Blackboard - Alpha DB**
sheet. They are on `main` so that a clone of the default branch can reach the board —
before this, every one of them lived on a feature branch and a fresh checkout could
not talk to the board at all (`ISS-009` Q3, 2026-09-08).

## Setup

Credentials never live in the repository (doctrine **D-18**). Create a gitignored
`.env` at the repo root:

```
BUS_URL=<the /exec URL of the v1 bus>
BUS_SECRET=<the shared secret>
```

Ask Mr. Salam for the values; do not copy them from a chat transcript, a Drive
document, a board row or a commit message. Point `BLACKBOARD_ENV` at a different
file if your `.env` lives elsewhere.

Check the credential without spending a write:

```bash
python scripts/env_check.py
```

## The clients

| Script | What it does |
|---|---|
| `bus.py` | Transport. `load_env()`, `fetch()`, `read_board()`. Import it; don't run it. |
| `env_check.py` | Proves the credential read-only. |
| `board_summary.py [n]` | One line per row for the last `n` rows. Start here. |
| `tail_full.py [n]` | Full payloads for the last `n` rows. |
| `show_row.py <row_id>` | Every cell of one row, to prove column alignment. |
| `append.py <row.json>` | Appends one row, then reads it back. The only sanctioned writer. |

Board payloads carry emoji that crash `cp1252`. On Windows, run with
`PYTHONIOENCODING=utf-8`.

## Two traps that have cost this project real hours

**1. A failed-looking append may already have landed.** The googleusercontent
redirect hop raises `HTTP 404` *client-side* after the row has been written
*server-side*, and the bus does not deduplicate. So a retry writes the row twice.

`append.py` therefore sends with `tries=1`, catches its own transport error, and
lets the read-back decide — because an exception is not evidence of absence any
more than `ok:true` is evidence of arrival. On 2026-09-08 the previous version,
which inherited `fetch()`'s default of five attempts, put
`WRK-vmccc-xray-blocker-20260908T1630Z` on the board twice.

Note the shape of that bug, because it generalises: the row's timestamp is stamped
by the **client**, once, before the write is attempted. Retries resend the identical
payload, so client-retry duplicates share a timestamp **to the microsecond**. They
are not evidence of the gateway persisting twice.

**If you write another client in another language, this is the part to copy.**
Never retry an append. Read back and count.

**2. Reads are idempotent, so retry those freely — but a successful read can be
stale.** The redirect target intermittently 404s minutes after a working read, and
the bus sometimes answers a `read` with the 113-byte `doGet` health blob instead of
the board; `read_board()` handles both by retrying. Separately, a read that succeeds
can return a board that omits a row appended moments earlier
(`CLAUDE-BOARD-STALE-READ-FINDING-001`). Do not conclude from one clean read that a
write failed.

## Writing a row

`append.py` takes a JSON file with `row_id`, `source_tag`, `target_surface`,
`action_type`, `payload`, `category`, `project_tag`, `gist`, `subgist`. It stamps
the timestamp, appends, and then reports whether the row is present **exactly
once**. A duplicate exits non-zero and tells you not to re-run.

Payloads follow the BCB grammar (`BCB|v=1|id=…|phase=…|…`). The validator is
`bcb_lint.py` in the Drive folder root; it takes a whole saved board read, not a
single row.

> **OBSOLETE 2026-09-26**: it belongs to the Azure ORDER lane, retired with Azure on 2026-09-24. Kept as history, do not follow it. How we work now: [`EXPRESS.md`](EXPRESS.md).

# ORDER port: cross-host comparison results

**Nothing is in service.** Observe mode only, no provider invocation, no board
write, no unit installed, no timer enabled. The fleet still requires Azure and
Stage 4 remains Mr Salam's decision after the 48-hour soak.

## What was compared

The ORDER supervisor, run on Windows PowerShell 5.1 and on Linux PowerShell
7.5.4, against **the same input**, with decisions diffed by
`tests/order_crossdiff.py`.

Two scenarios per run, and the second is the only one that matters:

- `seeded` — default behaviour. Pass one seeds the cursor at the board tail,
  pass two finds nothing after it. This is the real cold start and it must
  match, but it would look identical on both hosts **even if every admission
  rule were broken**.
- `replay` — `-ReplayHistorical` on fresh state, which skips the tail seed and
  the age gate so the supervisor actually evaluates every row.

## Run 1: repository fixture

`tests/fixtures/order_supervisor_board.json`, 6 rows.

| | Windows 5.1 | Linux 7.4.6 |
|---|---|---|
| seeded | poll_started, tail_seeded, poll_started, poll_complete | same |
| replay | **candidate_observed** row=old-order-044 | **no_eligible_order** |

**The two hosts disagreed about admission, and neither reported an error.**
Both returned `ok: true`. See the commit message for `8fc7af7` for the cause;
in short, PowerShell 7 coerces ISO-8601 strings to `[DateTime]`, and the guard
written to prevent that (`ConvertFrom-JsonPreserveStrings`) asked for a
parameter introduced in PowerShell 7.5, so on 6.0 through 7.4 it fell through
to the unguarded path.

After the fix and PowerShell 7.5.4: **identical on both hosts.**

## Run 2: a live board snapshot

A single snapshot of the live board, **1,983 rows**, sha256
`02B97876D007522D79A1A40474DFD8756E0B6BA9F1E2CD9C9A85AAECEFE57B06`, verified
identical on both hosts before comparing.

**2,466 decisions. Every one matched.** Including the schema incidents the
supervisor raises against real board data:

```
board_duplicate_rows_collapsed [BOARD_CURSOR_DUPLICATES_COLLAPSED]
board_schema_incident          [BOARD_KNOWN_TRAILING_ROW_IGNORED]
row_ignored [source_not_allowlisted] ...
row_ignored [untrusted_surface] ...
candidate_observed row=WRK-codex-accept-20260906T163710Z-A7F3
```

## Why a snapshot rather than two live reads

The plan's step 4 says to run against the live board on both hosts. Done
literally that is a **worse experiment**, for two reasons:

1. **The input would not be equal.** The board is append-only and growing, so
   the two hosts would read different boards and any difference could be the
   board moving rather than the port. That destroys the only property the
   comparison rests on.
2. It would require putting live bus credentials on a second host, and would
   double the read load on a bus that has already been observed failing full
   reads under load.

One snapshot, hashed and verified on both sides, gives the same real data with
equal input actually guaranteed.

## What this does NOT establish

Said plainly, because the numbers above are easy to over-read.

- **The live read path on Linux is not covered.** Both runs read a file. The
  `bus.ps1` HTTP read against the real Apps Script bus has not been exercised
  from the instance.
- **Observe mode only.** No provider was invoked, so the adapter path, the
  wall-timeout path and the process-tree kill under a real workload are
  untested end to end. The tree kill is unit-tested with real processes; that
  is not the same thing.
- **No writes.** `fixture_append_forbidden` makes that structural for the
  fixture runs, and nothing has written to the live board from Linux.
- **One snapshot is one moment.** It exercised the rows that existed at
  10:5x UTC on 2026-09-10 and no others.

## Standing requirement this produced

Any host running this board tooling needs **Windows PowerShell 5.1** or
**PowerShell 7.5 or later**. PowerShell 6.0 through 7.4 silently coerce board
timestamps and are now refused with `BOARD_JSON_DATE_COERCION_UNSAFE` rather
than allowed to return an empty answer.

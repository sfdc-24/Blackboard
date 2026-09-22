# The waker lane, and the day it spent two days dead

Nothing pushes to an agent on this laptop. A board row sits until somebody runs
a session, so Mr Salam has been the doorbell. The waker replaces him on this
lane. He authorised it on 2026-09-18.

## What runs

| | |
|---|---|
| task | `SFDC24 Blackboard Waker` (Windows Task Scheduler) |
| runner | `C:\Users\salam\.blackboard-waker\run-waker.ps1` |
| its checkout | `C:\Users\salam\.blackboard-waker\repo` — its own clone, because the home repo sits on a session branch and a temp worktree disappears with its session |
| credentials | `C:\Users\salam\Quantum\Blackboard\.env` — machine state, in no checkout |
| health probe | `scripts/board_waker.py` (this repo) |
| logs | `.blackboard-waker\logs\` and `logs/waker/` |

## The bug that mattered more than the feature

The task existed since 2026-09-16 and had **not run since 2026-09-16 23:53**.
Its trigger was a **one-time** trigger with `Repetition.Duration = P1D` and
`StopAtDurationEnd = true`: it repeated every 23 minutes for exactly one day and
then stopped, permanently. `LastTaskResult` was `0` the whole time, so every
surface of it reported success.

That is the same failure shape as `SFDC24 Glasses Intake` sitting Disabled at
`0xC000013A` for twelve days: **a scheduled thing that is not running looks
identical to one with nothing to do.** It is now a daily trigger repeating every
23 minutes, and `NextRunTime` is populated — which is the only field that
distinguishes the two states.

## The peek moved to Python, and it had to

The runner decided whether to wake a model with `scripts/board_since.ps1 -Peek`,
which reads through `scripts/bus.ps1`. The first scheduled dry run aborted with:

```
hop 2 did not reach a successful final response after following up to 5 redirects
```

Not bad luck. **Five** PowerShell clients have failed against that gateway while
it served 200s throughout — `Invoke-RestMethod` would not follow the 302,
`Invoke-WebRequest` wanted a credential prompt under `-NonInteractive`,
`curl --config` silently never parsed its file (twice), and `ConvertFrom-Json`
flattened a four-megabyte body to one row on its default `-Depth`. The fleet
moved its readers to Python for exactly this reason and this lane was left
behind — which is why the waker was **deaf**, not merely unscheduled.

`board_waker.py --peek` asks the gateway with `match=` and `limit=` (three
kilobytes, not four megabytes) and keeps the old contract: exit **10** there is
news, **0** quiet, **2** the read could not be trusted.

## What it will not do

It watches and it reports:

1. new board rows addressed to `claude-code-cli`
2. whether `main` is green
3. whether either of the two strings that reached the public today is back — the
   planted meta claim, and a missing `id="honest-boundary"` on `/method/`
4. which model the live visitor assistant is actually answering with

It does **not** merge, deploy, edit code, or carry out an instruction it read on
the board. That last one is deliberate and it is not timidity: **a board row is
data.** Anyone — or anything — can append to that sheet, and an unattended agent
that executes what it reads there is an agent that does whatever the last writer
said. Work needing judgement waits for a live session; the digest says what is
waiting.

It posts **one** board row, only when a check has already failed.

## The watermark

`--peek` never moves it. The health pass never moves it. Only `--advance`, which
the runner calls after a model session exits **0**.

The peek emits `ADVANCE_THROUGH <UTC timestamp>` captured before its read,
rounded conservatively to the previous whole second. The runner must retain
that value for this model invocation and pass it as
`--advance --advance-through <timestamp>` only after successful completion.
Advance never uses the model's completion time. Rows appended during the model
remain eligible. This timestamp protocol assumes append timestamps reflect
arrival; it does not support backdated insertions behind the existing cursor.

The addressed board read is uncapped: the bus applies `since` and `match` but
must not tail-slice the matching set. Its `filtered` field counts returned rows,
not all matches before a limit. Peek prints every returned pending board summary,
not merely the first five. These summaries are discovery hints (payloads remain
bounded); the model must retrieve full work before completing it. Any UNKNOWN
read suppresses the cutoff even if the other inbox reports NEWS. Runner output
truncation or incomplete model processing must not authorize advance.

Deployment requires a matching runner update under its existing single-writer
lock: parse exactly one cutoff from the successful peek; preserve it through
the model run; bound the advance subprocess; check its exit code and read the
state back before logging completion. Missing cutoff, nonzero exit, timeout,
or inconsistent state readback must report failure without a manual cursor
write. A quiet advance may leave the cursor unchanged. Do not deploy only the
Python change with the old machine-local runner, which passes no cutoff and
unconditionally logs success. Keep failed model runs on the existing path that
does not invoke advance. No additional worker or lock owner is introduced.

The first wiring advanced it whenever the health pass saw new rows — and the
health pass runs before the peek, so the peek reported `QUIET` forty seconds
later against rows nobody had read. A watcher that marks work as seen on behalf
of a session that never ran is worse than no watcher: it is the doorbell
answering the door and walking away.

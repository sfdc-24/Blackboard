# Porting the ORDER lane off Windows

**Status: PLAN ONLY. No code has been ported. The ORDER worker still runs on
Windows and the fleet still requires the Azure VM.**

Stage 3 proved the *bus* runs with Azure unreachable. It proved nothing about
the fleet. This document is the inventory that has to exist before anyone
writes a systemd unit, and it corrects a claim I had been repeating.

## The claim I got wrong

I had been saying, in board rows and in summaries, that "ten PowerShell 5.1
scripts still need Windows". That was never measured. It came from counting
files with `#Requires -Version 5.1` at the top, which says what the author
declared, not what the code needs.

Measured, the answer is much smaller.

## What was measured, and how

Two passes, because the first one alone would not have been evidence.

1. **Static inventory** (`scripts/port_inventory.py` in the session
   scratchpad): every `.ps1`/`.psm1` scanned for Windows-only constructs -
   Scheduled Task, registry, WMI/CIM, COM, event log, service control, WSL,
   DPAPI, ACLs, drive-letter paths, Windows environment variables. Comment
   lines excluded, because a comment mentioning Azure is not a call.

2. **Target-environment check**: PowerShell 7.6.5 installed on the GCloud
   instance, every file parsed there with
   `[System.Management.Automation.Language.Parser]::ParseFile`, and every
   external cmdlet they call tested with `Get-Command` on that host.

## Result

| file | lines | parses on Linux | missing cmdlets |
|---|---|---|---|
| `OrderSupervisor.psm1` | 1253 | yes | **0** |
| `order_supervisor.ps1` | 1043 | yes | **0** |
| `invoke_order_claude.ps1` | 319 | yes | **0** |
| `bus.ps1` | 428 | yes | 0 |
| `snow.ps1` | 251 | yes | 0 |
| `wa_notify.ps1` | 222 | yes | 0 |
| `claude_board_worker.ps1` | 156 | yes | 0 |
| `alpha.ps1` | 112 | yes | 0 |
| `wa_watch.ps1` | 107 | yes | 0 |
| `install_order_supervisor.ps1` | 781 | yes | **11** |
| `register_task.ps1` | 168 | yes | **6** |

**11 of 11 files parse clean under PowerShell 7 on Linux. Of 49 external
cmdlets referenced, 13 are missing, and every one of them is a Scheduled Task
cmdlet or `Get-CimInstance`. They appear in two files, both of which are
installers.**

The ~2,300-line worker - the part that actually reads the board, runs an order
and writes results - calls nothing that is absent on Linux.

## What this does and does not establish

It **does** establish that the port is a scheduler swap, not a rewrite, and
that the worker is not the hard part.

It **does not** establish that anything runs correctly. Parsing is the language
accepting the text. `Get-Command` finding a name is not that cmdlet behaving
identically. At least these remain, and none is caught by either pass:

- **`order_supervisor.ps1:697`** hardcodes
  `$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe` as the engine
  it spawns. That is Windows PowerShell by absolute path. It must become `pwsh`.
- **`$env:USERPROFILE` / `$env:HOMEPATH`** are rewritten at
  `order_supervisor.ps1:72-75`. Neither exists on Linux.
- **The filesystem is case-sensitive.** Any path built with the wrong case works
  on Windows and fails here, and no static check will find it.
- **Encoding defaults differ** between 5.1 and 7, which matters because this
  lane writes state files that another process reads.
- **Path separators**: `Join-Path` is fine, embedded `\` is not.

## Two false positives worth recording

Both would have gone into a status report as real findings if I had trusted the
scan output instead of reading the hits.

- The static pass reported an "Azure reference" in `OrderSupervisor.psm1`. It is
  the string `'azure'` in a list of secret-name keywords used for **redaction**.
  The opposite of a dependency.
- The first target-environment pass reported **106 missing cmdlets**. The
  scripts define 98 functions of their own and nothing had loaded them, so
  almost all of it was the scripts failing to find themselves. A number that
  large should have been the tell.

## Proposed sequence

Nothing below has started.

1. **Replace the scheduler, keep the worker.** Write a systemd service plus
   timer that does what the Scheduled Task does: run the worker on an interval,
   as a fixed user, not overlapping, restart on failure. `install_order_supervisor.ps1`
   and `register_task.ps1` are replaced, not translated.
2. **Fix the five known runtime blockers above**, engine path first.
3. **Run the worker against a FIXTURE board on the instance**, never the live
   board, and diff its output against the same fixture run on Windows. Equal
   output on the same input is the only evidence that means anything here.
4. **Run it against the live board read-only** - the lane already supports this -
   and compare its decisions with the Windows instance's for a full cycle.
5. **Only then** consider running both, and only then consider stopping one.

Steps 3 and 4 are where this plan is likely to be wrong, and they are cheap
compared to discovering it in step 5.

## What this changes about Stage 4

Nothing yet. Azure is still required, because none of the above has been done.

What it changes is the *estimate*: the remaining Windows dependency is roughly
950 lines of installer, not 4,840 lines of everything. That is worth knowing
before the 48-hour soak, and it is still Mr Salam's decision when it ends.

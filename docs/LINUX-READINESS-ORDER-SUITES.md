# How much of the ORDER suite family actually runs on Linux

Measured, not inferred. Every `tests/test_*.ps1` was executed on real Linux and the
result recorded. The headline is that **9 of 23 suites already pass there** and
nobody knew, and that the remaining blockers are mostly one mechanical fault — but
that three of them are not mechanical at all, and one of those leaves a release-gate
security guard with **no coverage on the platform we are migrating to**.

## How this was measured

| | |
|---|---|
| Host | `AkatiaVM` (Spot), resourceGroup `COPILOT-DEV-RG`, VM id `e5e4ad7f-537f-4672-a682-ad043e289ae7` |
| Linux | WSL2, Ubuntu 26.04 LTS, kernel 6.18.33.2-microsoft-standard-WSL2, 2 CPUs |
| Engine | PowerShell 7.6.6 (Core), unpacked from the release tarball, not on PATH |
| Repo state | `main` at the time of the run |
| Method | each suite run once, `timeout 240`, ANSI stripped, `PASS`/`FAIL` lines counted |

Windows figures in this document come from the same checkout run under Windows
PowerShell 5.1.26100.9444 and pwsh 7.6.6 on the same box.

One pass per suite. Where a claim below rests on a specific failure rather than on a
count, that failure was reproduced in a second, separate run.

## The map

| Suite | exit | PASS | FAIL | What stopped it |
|---|---|---|---|---|
| `test_azure_vm_supervisor` | 0 | 31 | 0 | — green |
| `test_bus_timestamp_contract` | 0 | 104 | 0 | — green |
| `test_order_adapter_denypath` | 0 | 15 | 0 | — green |
| `test_order_group_drain` | 0 | 23 | 0 | — green |
| `test_order_linux_containment` | 0 | 15 | 0 | — green |
| `test_order_linux_execute` | 0 | 11 | 0 | — green |
| `test_order_linux_host` | 0 | 19 | 0 | — green |
| `test_order_setsid_escape` | 0 | 14 | 0 | — green |
| `test_order_unit_containment` | 0 | 13 | 0 | — green |
| `test_order_supervisor` | 1 | **333** | 0 | `powershell.exe` not found |
| `test_order_workspace` | 1 | **282** | **4** | four real failures, below |
| `test_order_release_candidate_preflight` | 1 | 23 | **2** | two real failures, below |
| `test_order_installer_safety` | 1 | 11 | 0 | stops after 11 |
| `test_order_job_schedule_link_validator` | 1 | 1 | 0 | `powershell.exe` not found |
| `test_order_state_preservation` | 1 | 1 | 0 | `powershell.exe` not found |
| `test_order_read_resilience` | 1 | 0 | 0 | `powershell.exe` not found — **fixed in PR #66** |
| `test_order_release_installer` | 1 | 0 | 0 | `powershell.exe` not found |
| `test_order_deployment_wrappers` | 1 | 0 | 0 | `Start-Process` threw |
| `test_order_escrow_transport` | 1 | 0 | 0 | `Start-Process` threw |
| `test_order_release_packager` | 1 | 0 | 1 | `OpenRead`: file not found |
| `test_order_system_adapter_smoke` | 1 | 0 | 0 | exception at line 947 |
| `test_order_cutover_phase` | 1 | 0 | 0 | exits 1 with no output |
| `test_order_task_escrow` | 1 | 0 | 0 | exits 1 with no output |

**Green: 9. Not green: 14.**

## The blockers, by how much work they are

**One hardcoded string is most of it.** Five suites die on `powershell.exe`, which
does not exist off Windows. `test_order_supervisor` gets **333 assertions deep**
before hitting one — that suite is far closer to Linux-ready than a grep for
`powershell.exe` (11 sites) suggests. PR #62 added `ORDER_TEST_CHILD_SHELL` and
PR #66 makes the default `pwsh` off Windows; applying that same preamble is a
mechanical change for each of the five.

**A useful surprise: `Join-Path` normalises backslashes on Linux.** `Join-Path $root
'scripts\OrderSupervisor.psm1'` resolves to `$root/scripts/OrderSupervisor.psm1` and
the file is found. Measured directly, because I assumed the opposite and was wrong.
Every `'scripts\...'` literal in these suites is therefore harmless, which is a large
part of why the suites run as deep as they do. Do not spend a day "fixing" them.

**`Start-Process` is the second cluster.** Two suites throw
`Exception calling "Start" with "0" argument(s)`. Windows-only options such as
`-WindowStyle Hidden` are the likely cause; not yet diagnosed line by line.

## Three things that are not mechanical

### 1. A release-gate security guard is unexercised on Linux

`test_order_release_candidate_preflight` fails
`plain mixed-case commondir redirects Git to the foreign candidate object store`.

The fixture writes `.git/CoMmOnDiR` and expects Git to honour it, which redirects Git
to a foreign object store — the attack the preflight guard exists to refuse. On
Windows, a case-insensitive filesystem makes `CoMmOnDiR` and `commondir` the same
file and the attack stages. **On Linux it does not stage at all**, Git ignores the
file, the preflight legitimately succeeds, and the assertion fails because there was
nothing to guard against.

This is not a broken guard. It is worse in a quieter way: **that fixture is the only
one in the suite that exercises the commondir path at all** — there is no exact-case
`commondir` case — so on the platform we are migrating to, a supply-chain control on
the release path has *no coverage*. The migration needs the Linux-equivalent attack
staged, not the Windows one ported.

The second failure in that suite is the same fixture's downstream receipt assertion.

Owner: this is the release-preflight lane (PR #47, #49), not mine. Reported, not
touched.

### 2. Four `test_order_workspace` failures, all around Windows-only constructs

- `ephemeral settings denies the exact configured env file`
- `adapter returns fixed isolation cleanup failure after child reparse injection`
- `adapter restores ambient config and retains unsafe owned residue`
- `adapter ownership helper rejects a config junction without target deletion`

The last one names an NTFS **junction**, which has no Linux equivalent — a symlink is
a different object with different semantics. These four look like the same shape as
the commondir case: assertions encoding Windows filesystem behaviour. 282 assertions
in that suite pass on Linux, so the suite is not the problem; four specific threats
are.

### 3. Two suites report their results in a different format

`test_order_group_drain` and `test_order_unit_containment` end with
`N passed, M failed`. Every other suite ends with `RESULT passed=N failed=N`.

Nothing is broken today, because CI steps key on exit code. But anything that
*parses* the RESULT line — the new step in PR #66 does, and board rows quoting
per-suite counts do — sees nothing for those two and would report them as having
measured zero. It is the same skip-that-passes family the Linux job's platform guard
exists to prevent.

## What this means for the migration

A "green" ORDER badge today means green **on Windows**. Of the suites that have ever
gated anything, nine transfer as they stand, five need one preamble, two need
`Start-Process` work, and **six assertions across two suites encode threats that do
not exist on Linux and therefore leave real guards untested there**.

The last group is the one worth planning for. Porting them is not translation; it
needs the Linux-equivalent attack for each, and a loud, counted skip wherever an
attack genuinely has no Linux analogue — a silent skip would report a guard as
covered when it has never run.

## What has not been done

No suite was ported here and no product file was touched. This document is a
measurement so that whoever does the porting starts from evidence rather than from a
grep. `test_order_read_resilience` is the one row already being addressed, by PR #66.
The `Start-Process` failures and the two no-output suites were not diagnosed line by
line.

# Blackboard ORDER supervisor on Azure

This directory records the POC recovery layer for the Blackboard coordinator.
It has two independent triggers:

1. `AkatiaVM-regular` runs `\SFDC24 Blackboard Order Worker` at boot and every
   15 minutes. Task Scheduler uses `IgnoreNew` so triggers do not overlap.
2. Azure Automation runs `Blackboard-VM-Supervisor` every 30 minutes through
   two staggered hourly schedules. It starts the VM if necessary and verifies
   the exact managed guest task through Azure Run Command.

The outer supervisor contains no Blackboard credentials. Its system-assigned
identity is scoped to the replacement VM. The guest worker reads the existing
v1 bus configuration by path and never places credential values on its command
line, in source control, or in board rows.

## Recorded evaluation resources

| Resource | Value |
| --- | --- |
| Subscription | `604c029c-c254-4bc4-b173-05d6e5c3cab0` |
| Resource group | `COPILOT-DEV-RG` |
| Regular VM | `AkatiaVM-regular`, zone 1, `Standard_FX2mds_v2` |
| Rollback VM | `AkatiaVM` (Spot; preserve until acceptance) |
| Pre-migration snapshot | `AkatiaVM-os-pre-regular-20260906` |
| Automation account | `sfdc24-blackboard-ops` in `eastus` |
| Runbook | `Blackboard-VM-Supervisor` |
| Schedules | `Blackboard-Supervisor-00`, `Blackboard-Supervisor-30` |
| Schedule expiry | `2026-12-05T15:00:00Z` |

Azure Automation only supports an hourly recurring schedule at this cadence,
so the two schedules are offset by 30 minutes. Their job-schedule links must
bind every runbook parameter explicitly using the exact parameter-name case in
`job-schedule-00.json` and `job-schedule-30.json`.

## Deployment order

1. Parse and test the local PowerShell files under Windows PowerShell 5.1.
2. Publish `blackboard_vm_supervisor.ps1` and turn verbose logging off.
3. Create the two hourly schedules with a finite expiry, then link them using
   the two JSON resources in this directory.
4. Read the individual job-schedule resources back. Do not accept an empty or
   null `properties.parameters` object.
5. Package the six guest files listed by
   `install_release_from_archive.ps1`. Deliver the digest-checked archive with
   Azure Run Command and install the Windows task in `Observe` mode.
6. Start the task once. Require the immutable release ID, SYSTEM identity,
   target profile, v1 bus read, tail-seeded cursor, zero board writes, and task
   result `0` on read-back.
7. Run the outer runbook manually and require exactly one terminal JSON record
   with the expected VM/task identity. Then require the same result from one
   scheduled invocation.
8. Only then install the same task in `Execute` mode with an absolute Claude
   executable path. Issue one fresh, harmless `codex` test order and verify its
   deterministic CLAIM, RECEIPT, and RESULT rows by reading the board back.

## Rollback

Disable the two Azure schedules before changing coordinator identity. The task
installer preserves the prior task definition and supports `-Action Rollback`.
The original Spot VM and pre-migration snapshot are intentionally retained
during the evaluation. Do not run both coordinator tasks in Execute mode.

## Known POC limits

- Azure Action Run Command permits only one active script and has a 90-minute
  timeout. Normal cold supervisor runs took about five minutes in this setup;
  a 409 busy response fails visibly as `RUN_COMMAND_BUSY` and is deferred to
  the next bounded schedule. Persistent contention therefore remains visible
  in job history without triggering overlapping retries.
- The current VM-scoped `Virtual Machine Contributor` assignment is acceptable
  for the POC but broader than the final product needs. Replace it with a
  custom read/start/run-command role before production hardening.
- The Automation account provisioned as `Basic`. Schedules are bounded to the
  90-day evaluation window to keep the experiment finite.

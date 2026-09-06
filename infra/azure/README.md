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
   Azure Managed Run Command and install the Windows task in `Observe` mode.
   Windows passes Run Command parameters on the guest process command line, so
   do not pass the Base64 archive as one parameter: a release of this size can
   exceed the Windows command-line limit before PowerShell starts. For this POC,
   embed the non-secret Base64 release bytes in `source.script` and invoke the
   installer internally, or use a bounded `scriptUri`; verify the archive digest
   inside the guest before moving it into the immutable release directory.
   Invoke the task installer with the checkout boundary stated explicitly:

   ```powershell
   -WorkspacePath 'C:\Users\akatiawam\Blackboard'
   ```

   `Execute` mode fails closed when this parameter is omitted, relative, missing,
   or does not contain a `.git` directory. The immutable release directory is
   executable plumbing and is never the Claude work workspace.
6. Start the task once. Require the immutable release ID, SYSTEM identity,
   target profile, v1 bus read, tail-seeded cursor, zero board writes, and task
   result `0` on read-back.
7. Run the outer runbook manually and require exactly one terminal JSON record
   with the expected VM/task identity. Then require the same result from one
   scheduled invocation.
8. Only then install the same task in `Execute` mode with an absolute Claude
   executable path. Issue one fresh, harmless `codex` test order and verify its
   deterministic CLAIM, RECEIPT, and RESULT rows by reading the board back.

The bounded adapter runs Claude Code in non-interactive `auto` mode, disables
`AskUserQuestion`, and instructs the model not to retry a denied tool. It does
not require `--permission-prompts none`, which was introduced only in Claude
Code 2.1.259: in a `-p` process with no permission host, unresolved permission
requests are already denied. Keep the adapter's wall-clock timeout, budget,
schema validation, safe mode, and disabled session/slash-command controls as a
single compatibility contract.

The structured-result schema deliberately declares the canonical JSON Schema
Draft-07 identifier. Claude Code 2.1.241's `--json-schema` validator registers
that meta-schema but rejects a Draft 2020-12 declaration before inference. All
worker-result constraints use the shared Draft-07 subset, and the supervisor
still performs its stricter independent result validation after Claude exits.

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
- A VM cloned from a specialized OS disk can inherit stale guest extension
  handler state even when its ARM model has no extension child resources. Gate
  acceptance on both VM provisioning and Guest Agent readiness. If the Windows
  CRP transport certificate is invalid, follow the supported recovery order:
  reapply first, then attach and remove one known empty data disk to force a new
  goal state, and use VM redeploy only if that bounded refresh fails.
- The current VM-scoped `Virtual Machine Contributor` assignment is acceptable
  for the POC but broader than the final product needs. Replace it with a
  custom read/start/run-command role before production hardening. Deleting and
  recreating the VM also deletes this VM-scoped assignment: restore the exact
  Automation managed-identity assignment, read it back, and require a fresh
  manual supervisor PASS before enabling either schedule.
- The Automation account provisioned as `Basic`. Schedules are bounded to the
  90-day evaluation window to keep the experiment finite.

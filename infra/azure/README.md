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

## Job-schedule link validation

`validate_order_job_schedule_links.ps1` is the reusable, read-only validation
boundary for those two links. It uses only `az rest --method get` with
Automation API `2023-11-01` and returns one bounded JSON receipt describing
canonical or drifted state.

```powershell
.\validate_order_job_schedule_links.ps1
```

The collection `LIST` response is discovery-only. Azure can legitimately
project stale identity fields or `properties.parameters` as `null` there even
when the resource is fully bound. The validator therefore uses case-insensitive
schedule-name hints only to map exactly two unique target IDs from `LIST`, then
individually `GET`s each link. Only those individual resource bodies decide the
exact schedule, runbook, resource identity, absent/null `runOn`, and six
case-sensitive parameter bindings in the checked-in canonical JSON files. It
never infers identity or parameter drift from the collection projection.

A canonical result emits `VALID` and exits `0`. Any missing resource, identity,
cardinality, or binding mismatch emits `DRIFT` and exits `2`, so callers cannot
mistake drift for a successful validation. Transport, malformed JSON, or local
canonical-input failures emit `FAILED` and exit `1`. The validator has no
repair mode and cannot create, update, or delete Azure resources.

## Deployment order

1. Parse and test the local PowerShell files under Windows PowerShell 5.1.
2. Publish `blackboard_vm_supervisor.ps1` and turn verbose logging off.
3. Create the two hourly schedules with a finite expiry, then link them using
   the two JSON resources in this directory.
4. Run `validate_order_job_schedule_links.ps1` and require `VALID` with exit
   code `0`. A collection `LIST` response may project `parameters: null`; that
   is not drift by itself. Require the validator's authoritative individual
   `GET` of each exact link to match all six bindings.
5. Before changing the guest task, disable both Automation schedules and read
   them back as disabled. Wait until no Automation job, Action Run Command,
   Managed Run Command, or guest task instance is running. Freeze the complete
   board baseline and quiesce every eligible ORDER producer. Every Managed Run
   Command in the remainder of this sequence uses a fresh immutable resource
   name, a pre-create 404, source-script digest read-back, terminal
   instance-view read-back, and an operation ID unique to that command.
6. Use
   `build_order_escrow_transport_wrapper.ps1` to bind the exact hashes of
   `stage_order_escrow_tool.ps1` and `order_task_escrow.ps1` into a BOM-free
   Managed Run Command `source.script`. The generated wrapper carries the
   non-secret Base64 in script source and invokes the stage driver in-process;
   do not pass the escrow payload on the Windows command line. The driver
   installs a never-overwritten, digest-qualified file only below
   `%ProgramData%\SFDC24\OrderSupervisor\tools`, validates every existing
   ancestor and the complete tools inventory, and returns the exact guest path
   and digest. Historical versions may coexist only when every entry is a
   regular digest-qualified file that matches its embedded hash; partial,
   unrelated, corrupt, or reparse entries fail closed. Require that read-back
   before invoking the escrow tool, then require the enabled guest task to be
   exactly `Ready` with no running or queued instance. Then use the tool to
   create a new immutable escrow for the current known-good task. Read the
   escrow manifest back and independently verify the task XML digest, exact
   current release action, and all six current-release file hashes. `Create`
   requires `-TrustedFileHashesBase64`: Base64 of a BOM-free UTF-8 JSON object
   containing exactly the six canonical `scripts\...` keys in release order
   and their lowercase SHA-256 values. Generate that map from the independently
   verified exact Git release/package, never from the guest directory being
   escrowed. `Validate` and `Restore` bind to the trusted map recorded in the
   escrow and do not accept a replacement map. Windows Task Scheduler can omit
   the optional XML `LogonType` for its canonical `S-1-5-18` SYSTEM principal;
   the escrow accepts only that exact omission while still requiring the live
   task principal to read back as SYSTEM with `ServiceAccount`. Its exported
   trigger identities are the unqualified lowercase `id` attributes
   `AtBoot` and `Every15Minutes`; child-element or namespace-shadowed
   lookalikes fail closed. Explicit, duplicate, or namespace-shadowed principal
   alternatives fail closed too. Any installer predating the compressed receipt
   serializes its `Status` object as pretty-printed JSON, so the read-only
   `Status` action accepts insignificant whitespace inside exactly one bounded
   JSON object, from any release; banners, trailing output, multiple documents,
   arrays, scalars, duplicate keys, and oversized decoded output fail closed.
   That exception is keyed on the ACTION and deliberately not on a pinned
   release or digest: a constant that has to match the guest is one more input
   that can be wrong, and when it is wrong it fails as
   `INSTALLER_RECEIPT_INVALID`, which is indistinguishable from the defect it
   exists to repair and is only discoverable after another Managed Run Command
   round trip. Every MUTATING action keeps the one-physical-line rule.

   That last rule is also a **release precondition**, because mutating actions
   are only ever issued against the candidate installer:
   `scripts\install_order_supervisor.ps1` is one of the six canonical release
   files, so a candidate staged from a commit before the `-Compress` change is
   pretty-printed on its `Install` receipt as well. The cutover would refuse
   that receipt *after* `Disable-CutoverTask` has already run. Build and stage
   the candidate release from a commit whose installer emits `-Compress`, and
   do not reuse an earlier staged archive.
7. Deliver the exact reviewed `order_cutover_phase.ps1` as a BOM-free Managed
   Run Command source and read its source digest back. Use a new 32-lowercase-
   hex `OperationId` for every action. The driver captures all child output and
   is the sole receipt producer; every success or failure receipt is at most
   3072 UTF-8 bytes. Run `ValidateEscrowAndDisable` against the known-good live
   task. It must validate the exact escrow, require the task to be `Ready` with
   result `0`, prove its enabled XML is the escrowed XML, disable it, and read
   back `Disabled` with the state and append-only worker log unchanged. Never
   stop a `Running` or `Queued` task as a normal cutover step. Every escrow and
   installer child call rechecks its caller-pinned SHA-256 immediately before
   execution; context construction alone is not accepted as lasting proof.
   Before any state-preservation claim, parse and validate the exact current v1
   state, including an array-valued `work` history, and require an existing log
   prefix to end on an LF record boundary.
   If and only if this step is blocked by the already deployed old task being
   exact `Execute`, `Ready`, result `20`, with the causally current state and
   trailing log both proving `BOARD_HEADER_INVALID`, use the distinct
   `InstallObserveAndDrainFromFailedExecute` incident action instead. Before
   invoking it, complete the immutable candidate build, delivery, and guest
   read-back in steps 8 and 9; staging that release does not change the task.
   The incident action resolves and authenticates the candidate installer from
   that already-staged release, so an archive that exists only on the operator
   host is not sufficient. Supply
   the normal escrow, immutable candidate-installer, and restored-old-installer
   paths, release IDs, and SHA-256 pins required by `InstallObserveAndDrain`,
   plus these exact action-only inputs:

   ```powershell
   -Action 'InstallObserveAndDrainFromFailedExecute' `
   -Mode 'Observe' `
   -ExpectedCurrentTaskResult '20' `
   -ExpectedCurrentFailureCode 'BOARD_HEADER_INVALID' `
   -ExpectedCurrentRunId '<latest-state-last_poll-run_id-as-32-lowercase-hex>'
   ```

   The incident action is not a general nonzero-result override. It rejects
   `BOARD_READ_HTTP_ERROR`, every other failure code or result, a candidate
   release equal to the escrowed release, and any mismatch among the pinned
   run ID, SYSTEM/profile state, scheduler `LastRunTime`, two-record
   `poll_started`/`run_error` tail, enabled escrow XML, stable `NextRunTime`, or
   byte-identical state/log checkpoints. All of that authentication and a
   second stable `Ready`/result-`20` read occur before mutation or cleanup is
   authorized. It then disables without stopping, reads the old definition
   back as `Disabled`/result `20`, proves only `Enabled` changed, and invokes the
   candidate installer's dedicated `InstallFromDisabledNoStop` action in
   `Observe` without `-Start`. That action is valid only inside this transition's
   quarantine wrapper: it requires a managed `Disabled` task, rechecks that state
   immediately before replacement, authenticates both exports and the rollback
   backup against the driver's exact post-disable UTF-8 XML digest, and has no
   reachable stop, start, unregister, or automatic-rollback path. If replacement
   or readback fails, the installer
   deliberately leaves recovery to the outer quarantine, which disables the
   authenticated survivor and waits for any active instance to finish naturally.
   The driver then obtains a separate `Ready`/result-`0` status readback, verifies
   the disabled XML backup and unchanged state/log through candidate installation,
   and runs the ordinary bounded Observe drain, which then advances state and
   appends its own log evidence. The success receipt therefore scopes those proofs as
   `old_definition_preserved_except_enabled` and
   `state_and_log_preserved_through_candidate_install`; neither field claims
   that the installed candidate still has the old definition or that the drain
   left state/log unchanged. If a
   post-authentication step fails, quarantine authenticates the candidate
   first and the old definition second, leaves the surviving definition
   disabled, and never stops an active instance. After a successful incident
   transition, rollback remains the ordinary `RestoreReady` action against the
   same validated escrow. After quarantine, use the cleanup receipt's exact
   `cleanup_mode`: for `Observe`, run `RestoreReady` with the candidate as the
   current installer; for `Execute`, run it in `Execute` with the old installer
   and escrow release supplied as both the current and restored identities.
   Both forms start from the authenticated disabled definition and preserve the
   shared v1 state/log. A cleanup failure is not rollback-ready and requires
   fresh read-only task/XML/release authentication before any recovery action.
   For the old release's exact live failure contract, state and log carry code
   `BOARD_HEADER_INVALID` but message `board_header_invalid`. The sole
   `poll_started` must have level `info`, empty work/row/code/message base
   fields, and exactly `{mode:'Execute'}` details. The sole `run_error.details`
   object must contain exactly the five string fields
   `attempt:'1'`, `transport_exit:'0'`, `http_status:'200'`,
   `content_type_class:'json'`, and a positive, bounded, canonical
   `elapsed_ms` with an invariant dot separator and at most two decimal places.
   The candidate worker formats that timing string explicitly with invariant
   culture. Missing, additional,
   differently typed, or unsuccessful-sidecar values fail before mutation.
   `poll_started` may follow Scheduler `LastRunTime` by at most 30 seconds to
   allow bounded PowerShell/task startup; it may precede it only within the
   existing two-second clock tolerance. The exact caller-pinned run ID and
   trailing-run/state evidence, not timestamp proximity alone, bind identity.
8. Build the six-file archive from one caller-pinned commit with
   `build_order_release_archive.ps1`. Supply an absolute Git repository path,
   the full 40-character lowercase hexadecimal release ID (the caller-pinned
   commit ID), and a new absolute `.zip` output path. The packager reads the six
   canonical files from Git objects rather than the mutable checkout, preserves
   their committed bytes and EOLs, fixes
   ZIP ordering and metadata, never overwrites an output, and reads the archive
   back before returning its SHA-256 and all six file SHA-256 values bound to
   that commit. It explicitly anchors the repository and rejects inherited Git
   redirection, replacement refs, object alternates, external config includes,
   linked-worktree/common-directory indirection, and reparse-point ancestors in
   repository or output paths. Require that
   receipt and use its archive digest and release ID as the inputs to
   `build_order_release_wrapper.ps1`, together with the independently computed
   installer SHA-256 digest. The wrapper builder
   rejects non-canonical archive inventory and emits a deterministic BOM-free
   Managed Run Command `source.script`. Deliver that wrapper, but do not change
   the Windows task yet.
   Windows passes Run Command parameters on the guest process command line, so
   do not pass the Base64 archive as one parameter: a release of this size can
   exceed the Windows command-line limit before PowerShell starts. The generated
   wrapper is the standard POC path for embedding those non-secret bytes and
   invoking the installer internally. Verify the archive digest inside the guest
   before moving it into the immutable release directory.
9. Read the release manifest and all six guest files back. Require the full
   commit release ID, caller-bound archive digest, exact file inventory, and
   exact per-file hashes before changing the task definition. A manifest alone
   is not deployment proof.
10. Install the verified release's task in `Observe` mode, without `-Start`,
    with the checkout boundary stated explicitly:

   ```powershell
   -WorkspacePath 'C:\Users\akatiawam\Blackboard'
   ```

    `Execute` mode fails closed when this parameter is omitted, relative,
    missing, or does not contain a `.git` directory. The immutable release
    directory is executable plumbing and is never the Claude work workspace.
    Read the complete task definition back first. The initial installer's prior-task
    backup must match the exact post-disable task XML from step 7; the independent
    escrow remains the enabled rollback copy.
11. Run `DrainObserve` inside one Managed Run Command. The preserved v1 state
    must already be initialized; its baseline may record the preceding Execute
    run because installation never rewrites state. Each bounded iteration must
    advance Task Scheduler `LastRunTime`, produce a new worker `run_id`, finish
    `Ready` with task result `0`, and append matching log evidence.
    Bind the state poll timestamp to the same run's `poll_started` record and
    the bounded run window; a future-dated state timestamp is not fresh proof.
    `stale_order_ignored` is the only acceptable intermediate status. Success
    requires a causally fresh final `no_eligible_order`. An observed candidate
    cannot be skipped safely by the current Observe worker: the action fails
    immediately with `OBSERVE_CANDIDATE_REQUIRES_EXTERNAL_RESOLUTION`, disables
    future triggers without stopping an active instance, and requires the
    candidate to be resolved externally. Because quarantine leaves Observe
    `Disabled`, the bounded recovery path is `RestoreReady` from that Disabled
    candidate followed by a fresh `InstallObserveAndDrain`; a plain
    `DrainObserve` retry cannot satisfy its `Ready` precondition.
    `tail_seeded`, `overlap_suppressed`, a reused run ID, or result `0` without
    the matching state and log evidence is a failure. Observe must produce zero
    board writes. Counts-only `board_duplicate_rows_collapsed` and
    `board_schema_incident` warnings are permitted as nonterminal evidence only
    in their exact worker-defined shapes: at most one of each, blank work/row and
    message fields, fixed warning codes, and canonical bounded count details.
12. Rehearse rollback with `RestoreReady`. It must validate the exact rollback
    escrow and the exact current pinned candidate in either `Observe` or
    `Execute`. The candidate must be quiescent and exactly `Ready` or `Disabled`:
    `Ready` additionally requires result `0`, is stably reread, disabled, and
    read back; `Disabled` is stably reread without a redundant mutation and may
    retain the historical nonzero result that caused its quarantine. That result
    is surfaced in the receipt. It then invokes the pinned escrow Restore and
    accepts exactly one Restore receipt with `task_stopped:false`. It then reads
    the enabled known-good task back as
    `Ready`, result `0`, SYSTEM, and byte-identical to the escrow representation.
    It also proves `state.json` and the append-only worker log did not change.
    Never restore an older state file: releases share the v1 state, and rolling
    its cursor backward can replay an ORDER. Any failure after candidate
    authentication leaves the surviving exact candidate or restored task
    disabled; a `Running` or `Queued` candidate is quarantined and never
    stopped, restored over, or treated as rollback-ready.
13. For an ordinary rollback rehearsal to a known-good restored task, require
    one causally fresh natural 15-minute poll, then run the outer runbook
    manually. The Automation job must be new, bind
    the exact runbook and six parameters, finish `Completed`, and contain exactly
    one terminal outer JSON record. Require the guest task `Ready`, result `0`,
    an advanced `LastRunTime`, and zero board delta. `-Start` returning, a
    Managed Run Command success state, or an outer `PASS` without those causal
    read-backs is not completion proof. Skip this natural-poll step after the
    documented `BOARD_HEADER_INVALID` incident transition: the escrowed release
    is the known failing worker. Instead, while both Azure Automation schedules
    remain disabled, treat the guest task as enabled and `Ready` after
    `RestoreReady` but do not manually start it. Require more than 240 seconds of
    natural-trigger margin and immediately continue with step 14; do not run the
    outer supervisor against the restored worker.
14. Run `InstallObserveAndDrain`. It first proves the restored enabled task and
    escrow, disables it and proves that only the Enabled state changed, installs
    the same immutable candidate in Observe without `-Start`, validates the
    Install receipt, obtains a distinct exact `Status` readback, proves disable
    and installation changed neither state nor log, checks the installer backup
    against the exact post-disable XML, and performs the same bounded drain from
    step 11. The independently validated enabled escrow remains the recovery
    authority; the disabled installer backup is not a substitute for it.
    Run the outer runbook manually again and require the same exact fresh job and
    one-record acceptance while schedules remain disabled.
15. While the scheduled task is still in `Observe`, run the isolated SYSTEM
    adapter smoke with the exact release, workspace, and Claude arguments. It
    must prove the requested work-tool set `Read,Edit,PowerShell`, the effective
    set `Read,Edit,PowerShell,StructuredOutput`, scrubbed child environment,
    harmless Git execution, and unchanged protected fingerprints.
16. Run `InstallExecuteReady` with the same digest-qualified candidate installer
    and absolute, digest-pinned Claude and Git executable paths. It requires the
    exact candidate in `Observe`, `Ready`, result `0`, with enough time before the
    natural trigger. Before mutation, it requires the trailing log run to match
    the state `run_id`, validates exact `no_eligible_order` evidence, binds that
    run to the current Scheduler `LastRunTime`, and rejects any later overlap or
    other run. It captures the enabled Observe XML recovery digests; disables
    and reads the task back; proves only Enabled changed and the state/log stayed
    byte-identical; installs `Execute` without `-Start`; authenticates the
    installer's backup as the exact post-disable XML; validates the Install
    receipt; and obtains a distinct exact `Status` readback of the complete
    `Execute`, SYSTEM, `Ready`, result-`0` definition. Independently require
    enough trigger margin on that newly installed Execute task. The enabled
    Observe XML digest evidence and immutable candidate remain available for
    immediate recovery. `StartAndAwait` remains the only normal start path.
    Then append exactly one fresh harmless order from the exact allowed source
    `codex` and read that input row back.
17. Run `StartAndAwait` for that exact row/work identity and expected result
    status. It starts the already configured task once, waits within the natural-
    trigger margin, and requires a new `LastRunTime`, worker `run_id`, exact
    `result_confirmed` state, append-only log suffix, task result `0`, an exact
    clean Git baseline with unchanged branch/HEAD, unchanged hooks, environment
    file, immutable release, profile and workspace Claude configuration,
    Claude/Git executables, and no isolation residue. The driver intentionally
    has no bus access, so independently read
    the full board and require exactly the canary DISPATCH plus one deterministic
    CLAIM, RECEIPT, and RESULT.
18. Run a second `StartAndAwait` for exact `no_eligible_order`. Require another
    `LastRunTime` and run-ID advance, no new lifecycle rows, and global canary
    counts still exactly `1/1/1`. Task result `0` alone is never acceptance.
19. Re-enable both Automation schedules and read the complete schedule and
    individual link resources back. Require a future next run and the first new
    scheduled supervisor job to finish `Completed` with the exact six
    parameters and exactly one terminal `PASS` JSON record. Finish with no
    active jobs/commands and the Execute task `Ready` with result `0`.

### Failure quarantine

Normal cutover mutations remain `Ready`-only. After a phase has authenticated
the exact task but then fails, the driver uses a narrower emergency quarantine:
it disables scheduling immediately without calling `Stop-ScheduledTask` or any
process-kill path, exports the task again, requires `Enabled=false`, and proves
the definition differs only by that Enabled transition. It then waits for any
existing `Running` or `Queued` instance to finish naturally within a bounded
window and authenticates the final exact `Disabled` definition. A hung instance
returns cleanup `FAILED` with `future_triggers_disabled:true` and
`active_instance_status:PERSISTED`; it is never killed and no state snapshot is
restored. Disable failure or definition drift fails closed and must not be
reported as a successful quarantine.

### Task XML digest representations

The escrow manifest's `task_xml_sha256` hashes the actual `task.xml` file as
UTF-16LE bytes including its BOM. The task installer's backup manifest
`xml_sha256` hashes the UTF-8 encoding of the exported XML string, even though
`previous-task.xml` itself is written as UTF-16LE with a BOM. Those two manifest
fields are intentionally not directly comparable. Compare the XML text after a
strict decode, or encode the same text into the representation named by the
field. The cutover driver performs both checks and labels its pre/post task
digests `*_xml_utf8_sha256`; its `escrow_xml_sha256` retains the escrow's
UTF-16LE-with-BOM representation.

Task Scheduler may omit `Settings/Enabled` when exporting an enabled task; the
schema default is `true`. For the enable-only definition comparison, the driver
treats an omitted value as enabled and canonicalizes omitted, explicit `true`,
and explicit `false` forms through one explicit `true` node. Multiple nodes or
any explicit value other than lowercase `true` or `false` still fail closed.

`order_system_adapter_smoke.ps1` is the reusable step-15 artifact. Stage its
reviewed bytes outside every immutable release and verify its transport hash
before execution. Invoke it only through Windows PowerShell 5.1 as SYSTEM and
pass the full 40-character lowercase hexadecimal release ID, the caller-bound
archive SHA-256, and
`ExpectedFileHashesBase64`: Base64 of a BOM-free UTF-8 JSON object containing
exactly the same six canonical `scripts\...` keys and independently computed
lowercase SHA-256 values used by the escrow gate. Also pass the release root,
exact task name, and absolute profile, workspace, environment, state, log, and
Claude and Git executable paths, plus independently verified lowercase SHA-256
values for those exact executables. The smoke requires the root task to be
`Ready`, in `Observe`, last result `0`, and outside the configured quiet
window; it never waits for or starts the task.
Task Scheduler may omit the action's optional `Id` after registering the
installer's canonical `OrderSupervisor` action. The smoke accepts only that
true empty projection or the canonical `OrderSupervisor` value; every other
action ID remains drift.

The smoke hashes the real environment file for its protected before/after
fingerprint but never parses, imports, or passes that file to a child. It
exercises the production adapter with a generated local fake executable and a
synthetic non-secret environment file, so it performs no provider inference
and no Blackboard or other external write. The real Claude executable is
invoked only with `--version`, and Git is limited to a prompt-free, lock-free
`rev-parse --is-inside-work-tree` with hooks and inherited Git configuration
disabled. Accept only one terminal
`blackboard.order-system-adapter-smoke.v1` JSON receipt with `pass:true`, every
proof flag true, `provider_boundary_status:VERIFIED_NO_SELECTOR_LEAK`,
`external_mutation_status:NOT_DETECTED`, environment restoration `SUCCEEDED`,
and temporary cleanup `SUCCEEDED`. Before the applicable proof finishes, each
status remains `NOT_CHECKED`; a detected selector leak or protected/task/release
mutation is reported explicitly and fails the smoke. If a second-pass release
read, enumeration, or digest operation cannot complete, the external status is
`VERIFICATION_FAILED` and the smoke fails without claiming that mutation was
observed. The smoke caps that sole
receipt at 3,072 UTF-8
bytes so it remains below Managed Run Command's retained-output boundary; an
unparseable or truncated receipt is a failed smoke regardless of execution
state or exit code.

The bounded adapter targets the VM-pinned Claude Code 2.1.241 compatibility
contract. With subprocess credential scrubbing enabled, that version forces its
internal permission mode to `default`; the corresponding external CLI value is
`manual`. The adapter therefore requests `manual` explicitly instead of asking
for `auto` and silently accepting a downgrade. It uses `--bare`, strict MCP
configuration, disables `Bash` and `AskUserQuestion`, and limits the requested
work tools to `Read,Edit,PowerShell`. JSON-schema enforcement adds the only
other model-visible tool, `StructuredOutput`. It does not require
`--permission-prompts none`, which was introduced only in Claude Code 2.1.259.
The installer and every adapter invocation require the exact tested version
string `2.1.241 (Claude Code)` and fail closed on drift. Keep the adapter's
wall-clock timeout, budget, schema validation, bare mode, strict MCP boundary,
explicit tool list, and disabled session/slash-command controls as one
version-pinned contract.

In Execute mode the worker passes only the absolute `.env` file path to the
short-lived Claude adapter. The adapter reads exactly `ANTHROPIC_API_KEY` and
`ANTHROPIC_MODEL`, rejects competing Claude provider/authentication selectors,
and places those two values only in the Claude child process environment. It
does not import other `.env` entries or put provider values in argv, prompts,
logs, state, or board rows. For every Claude launch, the adapter forces
`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` and restores the inherited setting
afterward. Claude retains the provider credential for its own API call, while
Claude-launched shell tools, hooks, and stdio MCP servers do not inherit
recognized credentials. This control is adapter-owned and is never imported
from the workspace `.env` file. Each invocation also creates a bounded,
secret-free UTF-8 settings file beside its other temporary artifacts. That file
reasserts scrubbing, enables the native PowerShell tool, preauthorizes exactly
`Read`, `Edit`, and `PowerShell`, and denies built-in `Read`/`Edit` access to the
exact configured environment file; the adapter deletes it in `finally`, and the
parent removes the entire run directory after timeout or forced termination.
This release supports direct Anthropic inference only. Microsoft Foundry remains an
explicit, separately tested worker/provider contract and must not be selected
from ambient variables.

The structured-result schema deliberately declares the canonical JSON Schema
Draft-07 identifier. Claude Code 2.1.241's `--json-schema` validator registers
that meta-schema but rejects a Draft 2020-12 declaration before inference. All
worker-result constraints use the shared Draft-07 subset, and the supervisor
still performs its stricter independent result validation after Claude exits.

`order_supervisor_result.v2` is a compact durable-result contract. Its summary
is the complete board-visible answer, is limited to 500 characters, and must
already be single-line BCB-safe/redaction-safe text; `evidence` must be exactly
empty. A provider response that would require truncation, sanitization, or
discarding evidence becomes one fixed `RESULT_CONTRACT_INVALID` failure RESULT.
If the requested facts cannot fit, a compliant provider returns `blocked` with
`RESULT_TOO_LARGE` so the ORDER can be split. Each new RESULT row carries
`result_schema=order_supervisor_result.v2`, and append confirmation compares the
exact expected payload. A marked v2 row is accepted for duplicate suppression
only after its durable fields reproduce its stored digest. Historical unmarked
v1 rows are never rewritten or re-executed; digest-bearing rows are checked
against the historical serializer's exact field, safety, and length contract
including characters that its projection preserved, without newer v2 Unicode
or credential rules, v2 digest recomputation, or v2 uppercase error-code rules,
while the only permitted digestless shape is the exact historical
`DUPLICATE_INVOCATION_SUPPRESSED` RESULT. Pre-existing CLAIM/RECEIPT recovery
tolerates a different run identifier but no other semantic drift, and the
read-back after a current append remains exact. New-v2 summaries reject
Basic/Bearer Authorization headers and structured assignment, JSON, or query
shapes, including quoted and Markdown-backtick-wrapped names. The identifier
policy treats exact `key`, `token`, `secret`, and `password` as sensitive;
every segmented identifier ending in `token`, `secret`, or `password` is also
sensitive. A terminal `key` requires a credential/provider qualifier such as
`api`, `auth`, `access`, `private`, `client`, `secret`, `GITHUB`, `AZURE`,
`AWS`, `OPENAI`, `DB`, or `BUS`. Consequently `FOO_TOKEN` and `FOO_SECRET`
are rejected, while `sort_key`, `cache_key`, `public_key`, and an unknown
`FOO_KEY` are not classified merely by their suffix.
An unquoted exact `KEY:`, `TOKEN:`, `SECRET:`, or `PASSWORD:` is always a
rejected mapping shape; ordinary prose must omit that colon, for example
`Token rotation completed` or `Key status is green`.

### Recovery from an already-disabled Execute task

Use the incident-only action
`InstallObserveAndDrainFromDisabledExecute` with `-Mode Observe` and the
independently read-back exact `ExpectedDisabledXmlSha256`. It authenticates the
current candidate Disabled/Execute/result-0 definition and a fully consistent
`no_eligible_order` state terminal plus current log/run before any mutation.
`result_confirmed` is deliberately not admitted by this target-free action.
It then calls the same pinned release
installer's `InstallFromDisabledNoStop` in Observe mode, replacing the old
definition with a new future PT15M interval. The authenticated disabled XML is
backed up and verified; state, log, release bytes, profile/configuration and
repository fingerprints are not restored or edited by the driver.
After the protected-tree and backup reads, it rechecks the exact Ready run and
full remaining trigger window. The admitted scheduler LastRunTime, run ID and
state/log checkpoints are carried into the real drain; an intervening run or
checkpoint change is rejected before starting a run, never adopted as a baseline.
The first real OneRun rechecks the original state/log hashes after the drain's
last Ready read, directly before Start. The original protected fingerprint is
also carried into that gate and rechecked before the state/log hashes, so a
slow protected-tree read cannot leave those checkpoints unverified. Missing,
blank or non-string protected admissions fail closed. It then rechecks the remaining total
deadline and natural-trigger window after those hash reads. This binding applies
only to the first recovery start; later stale iterations retain the established
per-run transition and log-prefix contract. Future Observe XML must explicitly
contain exactly one StartWhenAvailable setting with value true.

Do not enable the old Execute definition to recover a missed interval.
Production uses StartWhenAvailable, which can queue delayed catch-up work.
The former proposed `StartAndAwaitFromDisabled` action is deliberately not
admitted. This recovery has no target dispatch, timestamp or work/result identity
inputs: it cannot replay an old command whose durable work was pruned, nor an
identity that exists only as board CLAIM/RECEIPT/RESULT evidence. Observe cannot
invoke Claude or append worker lifecycle rows even if a catch-up or unrelated
fresh order is encountered.

The existing Observe drain proves every run's scheduler time, new run identity,
append-only log and state/cursor/counter transition under one original total
deadline and MaxRuns bound. Only an exact final no_eligible_order is success.
A fresh candidate is observed, not executed or discarded; recovery fails visibly
as OBSERVE_CANDIDATE_REQUIRES_EXTERNAL_RESOLUTION and disables future triggers.
Do not redispatch, replay an expired order, reset the cursor or blindly create
another managed command after any failure. Preserve the full operation-correlated
receipt and actual guest/board read-back, resolve the named cause, then qualify
any new action independently.

Recovery requires TimeoutSeconds=420 and NaturalTriggerMarginSeconds=60,
with integer MaxRuns between 1 and 8 (retain 8 for the current cutover). Both CLI
admission and direct recovery invocation reject other limits before mutation.
Never-run scheduler timestamps (year 1601 or earlier) are not run evidence.
The protected tree is rechecked after the backup read, before the drain.
Recovery also rejects a runtime plus
margin plus 120-second pre-drain reserve that cannot fit the 15-minute interval.
The platform timeout must cover installer preflight/registration, fingerprints,
the entire drain and cleanup (use a finite 1800 seconds). Azure command delivery
is outside this guest deadline; it is not assumed constant.

After actual Observe no-eligible and stale/cursor proof, use the existing separate
InstallExecuteReady action, publish a genuinely fresh canary once with native
read-back, then the normal single-run StartAndAwait acceptance gateway. This
change does not relax Execute identity, output digest, log, protected fingerprint,
natural-trigger margin or final scheduled-job promotion requirements.

An abrupt process/host death after Observe registration cannot run catch cleanup;
it can leave Observe enabled, never a successful recovery receipt. Independently
read the destination task, state, log and board before another command or GO.
Recovery intentionally changes the definition to Observe with a future boundary;
it does not claim definition-preserved-except-Enabled.

## Rollback

Disable the two Azure schedules before changing coordinator identity. The task
installer preserves and validates the prior definition and supports
`-Action Rollback`, but a controlled release also requires an independent
`order_task_escrow.ps1` bundle outside the candidate release. Validate the
escrow completely before stopping or replacing a task, and never accept a
suppressed or unverified restore. The original Spot VM and pre-migration
snapshot are intentionally retained during the evaluation. Do not run both
coordinator tasks in Execute mode.

## Known POC limits

- This execution profile is for trusted, operator-authored `operator-direct`
  POC orders on the admitted private surfaces. The `.env` rules constrain
  Claude's built-in file tools only. Broad `PowerShell` runs as the same Windows
  identity and can read same-identity files (including `.env`), start processes,
  or access the network; subprocess environment scrubbing is not an OS sandbox
  and this design does not claim adversarial prompt-injection resistance.
  Production remains blocked on a separate restricted worker identity plus a
  sanitized checkout, or a brokered/allowlisted command-execution boundary.
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

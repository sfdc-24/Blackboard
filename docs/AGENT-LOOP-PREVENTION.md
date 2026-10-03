# Agent loop prevention — individual and collective

Status: **owner-directed operating rule; automatic fleet enforcement TARGET**.
Owner request: session, 2026-10-03. PM/security/test gate: Codex.
Runtime implementation and integration: Claude. Independent exact-head gate:
Cursor; Copilot reviews pushes. Gemini and Grok supply bounded reasoning.

This applies to every desktop, CLI, API, cloud worker, subagent, reviewer,
watcher, scheduler and relay, including Codex. It is not permission to change
credentials, paid-provider policy, production routes or stored schemas. The
existing [EXPRESS](EXPRESS.md) gates and append/read-back contract still apply.
See [POKA-YOKE L-114](POKA-YOKE.md#l-114--recursive-work-must-not-refresh-its-own-permission-to-run).

## Rule we apply immediately

Before another retry, delegation, review request, wake, write or notification:

1. Name the intended outcome, the authorized logical task and its current
   revision, one owner, the evidence being acted on, and the stop condition.
2. Check whether that exact work/effect was already claimed, answered, written
   or reviewed. Ignore own replies, ACK-of-ACK, terminal/answered rows and stale
   work. A new row ID, agent name, timestamp or heartbeat is not new work.
3. State what changed since the previous attempt. A real changed source head,
   repaired failed test or satisfied dependency can justify work; a new summary,
   clock sample or "still pending" cannot justify another business effect.
4. Keep the original finite attempt/review/delegation limits and absolute
   deadline. A reconnect, restart, handoff, new head or child cannot reset them.
5. If unchanged work is repeating, a dependency cycle exists, a limit is spent,
   or a previous effect is ambiguous, stop that automatic chain. Reconcile
   read-only, record one blocker, and continue independent authorized work.

For Codex this is a mandatory preflight, not a claim of universal software
enforcement. Its append-once dispatch helper already persists an intent and
refuses a blind second append on reconciliation; that limited local protection
does not establish the other routes' coverage.

## Not every repeated action is a loop

- Scheduled monitoring may take bounded read-only samples without external
  state changing. Quiet unchanged results are valid; do not manufacture progress
  or a new work task to make a monitor look busy.
- Exact-head re-review after a real repair is legitimate. It consumes the same
  logical work chain's review allowance; an unchanged head is not re-reviewed
  merely because a new agent asks. New unresolved findings are not suppressed.
- Author -> reviewer -> author is legitimate when the accepted input/evidence
  changes. Returning to the same stage and evidence without progress is not.
- A separately authorized human request or scheduled occurrence can be a new
  root task, identified by the trusted intake, not invented by a worker.
  Duplicate delivery of that occurrence shares its existing record. Schedule
  overlap/backlog limits and a consecutive-failure breaker prevent new ticks
  from bypassing a held or stuck run.
- A timeout is not proof that a write or paid execution did not happen.
  Ambiguous effects are reconciled/quarantined, never blindly repeated.

## Runtime guard contract — source/offline implementation first

No new tracker or second bus. Extend the existing governed control records;
the board remains dispatch/evidence and Ops remains a read-only projection.
Field names below are contract concepts, not a deployed schema.

### Trusted lineage and admission

The trusted intake binds a stable `root_work_id`, accepted task revisions,
`parent_work_id`, `event_id`, authenticated claimant/lease epoch, stage, source
digest, policy version, absolute deadline and explicit finite limits. Every
child, retry, provider attempt and renewed review inherits that root. Only the
control owner can authorize a fresh root, revise limits or resume a tripped root;
a worker cannot rename its way out of a limit.

Before any effect, one atomic admission reserves the root's remaining allowance,
checks claim/epoch/deadline and cancellation, and records its effect intent.
Limits include total calls/steps/review rounds, retries, delegation depth and
aggregate children/fan-out. Per-worker limits alone are insufficient. Require
finite validated values; missing, corrupt, stale or unavailable control state
means HOLD, not "unlimited". Do not silently choose production defaults here.

Two workers racing for the last allowance cannot both win. A stale claimant
cannot commit. Reservation/accounting survives crashes and restarts; unknown
provider acceptance keeps its reservation until reconciliation. Budget controls
cannot establish an exact monetary ceiling without the provider's separate cost
proof and owner authorization.

### Effects, progress and cycles

- Deduplicate by trusted logical work revision plus action type and canonical
  semantic parameters, not transport IDs or free-text similarity. An immutable
  intent and destination read-back establish whether the effect landed.
- A changed source head is new review input, not a fresh retry/delegation budget.
  Checkpoint substantive progress using trusted source identity, accepted test
  or result digests, unresolved-finding disposition and dependency transitions.
  Do not credit counters, clock ticks, changed wording, self-certified progress
  or cosmetic identifiers. Finite root limits still stop endless novel outputs.
- Detect revisiting the same logical task/stage/evidence state without verified
  progress, including A -> B -> A and longer relay chains. Keep bounded history
  across agents, not just each agent's local conversation.
- Detect circular wait-for dependencies separately: parent waiting for child
  while that child waits for the parent must not trigger repeated wakeups.
- Permit only authorized state transitions. Replaying RESULT, terminal BLOCKED,
  ACK, WA delivery receipt, or the guard's own diagnostic cannot spawn another
  task, acknowledgement or diagnostic. Role-specific native verdict IDs/URLs
  are receipts, not board-write capability.
  A genuinely awaited RESULT may satisfy its declared dependency once and
  resume the existing parent within remaining limits; it is not new authority.

### Scoped breaker and safe recovery

Trip only the offending root/route's automatic work. Stop new inference, fan-out,
wakes and business effects; use the existing reviewed route cancellation/kill
mechanism for in-flight automatic work where supported. Preserve checkpoints,
uncertain dispatches and evidence. Never kill, suspend or hard-cap interactive
agents, shut down the shared watcher, or abandon unrelated task ownership.

Keep one stable incident identity for the entire root, across breaker epochs,
reopenings and destinations. Append epoch/recovery history beneath that identity;
neither another trip nor another participant may mint a replacement incident.
It names the guard reason, owner, safe continuation and evidence pointer.
Deliveries beneath it have stable deduplication keys, a finite aggregate limit
and a pre-authorized bounded destination set; a worker cannot expand that set
to evade the limit. Its own delivery does not reopen the root. If delivery is
ambiguous or fails, keep the durable incident locally/in the control record for
bounded read-only reconciliation; do not create new alert IDs or restart a paid
notifier.

Read-only reconciliation remains available under a finite recovery allowance.
It does not permit new provider execution or replenishment. Reopening requires
the named control owner, reproduced cause/fix evidence and an explicit allowance;
retain spent counters and old incident identity. Do not turn a timer or resource
recovery into automatic consent.

## Route coverage and evidence

### Existing narrower controls, not the new fleet guard

Source inventory at Blackboard main `789ba351b208b1a513808fe14e92430d5a499e61`:

- [Bus append transport](https://github.com/sfdc-24/Blackboard/blob/789ba351b208b1a513808fe14e92430d5a499e61/scripts/bus.py#L85)
  makes one transport attempt; this does not deduplicate two caller invocations.
- [API-waker selection](https://github.com/sfdc-24/Blackboard/blob/789ba351b208b1a513808fe14e92430d5a499e61/scripts/agent_waker.py#L403)
  filters self/marked replies and answered logical IDs. A fresh ID or unmarked
  terminal message is not a general causal-loop control.
- [Cloud waker control](https://github.com/sfdc-24/Blackboard/blob/789ba351b208b1a513808fe14e92430d5a499e61/cloud/agent-waker/main.py#L346)
  uses durable CAS claims, receipt reconciliation and ambiguous-post quarantine.
  Its protected path does not cover every desktop/CLI writer.

Claude's first source slice should reuse these boundaries and existing tests.
Check `scripts/board_say.py`, `scripts/board_waker.py`, `scripts/agent_waker.py`,
`cloud/agent-waker/main.py` and `cloud/board-watcher/main.py` for caller-generated
fresh IDs, terminal-message eligibility, limits that reset per invocation, and
unchanged alarms emitted on repeated passes. This is source evidence of coverage
gaps, not an assertion of an observed live runaway.

The separate [narrow local posting mechanism in PR317](https://github.com/sfdc-24/Blackboard/pull/317)
is reviewed independently. Its burst/depth checks do not establish the shared
root limits or every route's coverage required here. L-114 records this contract;
the mechanism needs a distinct lesson number before either PR is integrated.

### Acceptance status for this contract

Record each route as RULE_ONLY, SOURCE_TESTED, DEPLOYED or VERIFIED_RUNTIME, with
exact artifact, policy version, stimulus, receipt and denial-control evidence.
Assignment or a document link is not uptake. Until these receipts exist, fleet
automatic enforcement is incomplete.

| Route | Preventive proof required | Current status of this new contract |
|---|---|---|
| Codex desktop and subagents | preflight on each action; durable limit/intent inheritance for automatic adapters; no recursive acknowledgement | manual rule adopted; local append-once scope only |
| Claude CLI/cloud and board watcher | authenticated shared admission, durable root limits, duplicate/terminal filters and scoped breaker | TARGET, implementation owner Claude |
| Gemini/Grok reasoning and wakers | one actionable stimulus/receipt; exclude own/terminal replies; inherited root and no repeated inference on replay | TARGET, route-specific receipt required |
| Cursor/Copilot | one exact-input native verdict receipt, no self-triggering comment/review requests, bounded re-review lineage | TARGET, route-specific native evidence required |
| Bus/WhatsApp relay/outbox | stable ingress/effect identity, append-once/read-back, receipt cannot generate another send | existing narrow controls require inventory; new fleet guard TARGET |
| Scheduled/cloud adapters, including future M07 | bounded read-only monitor vs new work; atomic shared limit, deadline and uncertain-dispatch handling | TARGET; no deployment or paid pilot authorized |

Ops may show loop-blocked incidents, suppressed duplicates, no-progress/cycle
reasons, consumed/remaining allowance, age and recovery owner, plus coverage and
unknown states. Publish only redacted metadata from genuine source events.
Do not expose messages, credentials, client content, recordings or transcripts;
do not advance source timestamps because the monitor clock advances. No new Ops
metrics are claimed deployed by this document.

## Synthetic acceptance matrix

Claude implements one bounded offline/source slice in its owned lane. Codex and
Cursor review its exact head; hosted CI is the deterministic test lane. No live
provider calls, credentials, IAM or deploy are needed for these tests.

| Control | Required observation |
|---|---|
| Self retry and two-/three-agent ping-pong | finite admission; repeated unchanged state trips; no extra effect/inference |
| Same effect with new row IDs, aliases, wording or clock | replay suppressed; no root allowance reset |
| Restart/reconnect, moved head, delegation or fresh process | original absolute deadline and spent root counters retained |
| Concurrent children claiming last allowance | one atomic winner; aggregate fan-out/depth cannot exceed policy |
| ACK-of-ACK, own RESULT, terminal BLOCKED, outbox receipt | zero new task/wake/inference/notification |
| Recursive guard failure, repeated trip/resume or expanding destinations | one stable root incident; finite deduplicated deliveries; no new incident, destination expansion or notification-of-notification chain |
| Crash before/after effect or provider acceptance | known outcome reconciled; unknown quarantined; no blind re-execution |
| Legitimate repaired source/new dependency | bounded continuation allowed; unchanged-source denial does not hide new findings |
| Scheduled unchanged observation and duplicate tick | bounded read-only sample allowed; zero business effects or spam; duplicate occurrence shares counters; overlap/backlog/failure bounds hold |
| Circular wait-for dependency | blocked root identified; unrelated roots still run |
| Cosmetic or fabricated progress | no reset; trusted evidence plus finite total limit required |
| Missing state, expired deadline, stale claimant, cancellation | fail-closed before effect; bounded read-only recovery remains |
| Owner-authorized resume | identity/cause preserved; explicit policy change recorded, not automatic reset |
| Every guard deliberately removed | its named regression fails for that guard, after an unmutated baseline passes |

Closure requires source tests, negative controls, reviewed integration and a
route-by-route synthetic runtime stimulus/read-back. This document and agent
agreement alone are not "everyone is protected".

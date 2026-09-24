# ADR-001: Run the voice cleanup backstop as a least-privilege Cloud Run Job

**Status:** Accepted
**Date:** 2026-09-24
**Deciders:** SFDC24 architecture lane

## Context

An authenticated Studio session may open one paid OpenAI Realtime WebRTC call.
The controller normally hangs that call up when the visitor presses Stop or the
ten-minute session expires, and a minimum Cloud Run instance runs an in-process
sweeper. A separate scheduled backstop is still required so instance churn or a
missed background iteration cannot leave a provider call running.

The backstop needs one credential: `STUDIO_MAINTENANCE_SECRET`. It must not put
that value in a Scheduler header, source control, command-line argument, log, or
session state.

## Decision

Cloud Scheduler starts a dedicated Cloud Run Job through the Google Run API.
The Scheduler request uses an OAuth token for a dedicated service account and
contains no Studio credential. The job runs the already-reviewed immutable
Studio image with `python -m app.sweep_once`. Secret Manager injects only the
maintenance secret into the job; the helper sends it once to the exact HTTPS
maintenance path, requires HTTP 200 plus `{ "ok": true }`, an available due-call
index, and zero pending cleanups, and logs only a constant success line.

The job runtime identity receives only:

- Secret Accessor on `studio-maintenance-secret`.

A separate Scheduler caller identity receives only:

- Run Invoker on the `studio-voice-sweep` job.

The Scheduler caller cannot read the maintenance secret, and the job runtime
cannot invoke the job. Deployer `actAs` authority remains separate from both.

The Studio controller identity separately receives Secret Accessor on that
maintenance secret and the OpenAI key. No project-wide secret role is used.

## Options Considered

### Static bearer stored in Cloud Scheduler

| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Cost | Negligible |
| Secret exposure | High: readable Scheduler configuration |
| Failure isolation | Medium |

**Pros:** One HTTP job and no extra runtime.

**Cons:** Copies a long-lived credential into another control plane and makes
ordinary Scheduler inspection sensitive.

### In-process cleanup only

| Dimension | Assessment |
|---|---|
| Complexity | Lowest |
| Cost | None beyond the controller |
| Secret exposure | Low |
| Failure isolation | Low |

**Pros:** Already implemented and fast during normal operation.

**Cons:** Not an independent backstop when the instance is unavailable or its
background loop misses work.

### Dedicated scheduled Cloud Run Job

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Cost | Negligible for a sub-minute job |
| Secret exposure | Low: one Secret Manager binding |
| Failure isolation | High |

**Pros:** Scheduler stays credential-free, IAM is narrow, failures are visible
as job executions, and the same job can be run manually for acceptance.

**Cons:** Adds one job and two narrowly scoped service accounts to operate.

## Execution policy

The deployed job has one task, parallelism one, a 30-second task timeout, and
zero task retries. The helper's 20-second HTTP timeout bounds network waits but
is not treated as the job's wall-clock deadline. Cloud Scheduler runs every
minute with a 30-second attempt deadline and zero Scheduler retries; an
unresolved pass fails visibly and the next scheduled minute performs the next
bounded attempt. The controller remains idempotent under overlapping or
repeated authenticated sweeps.

## Consequences

- Voice enablement has an independently testable cleanup gate and rollback.
- Scheduler operators can inspect timing and failures without seeing a Studio
  credential.
- Rotating the maintenance secret requires no Scheduler update.
- The job image must continue to include `httpx` and `app.sweep_once`.
- Deployment acceptance includes read-back of task count, parallelism, task
  timeout, job retries, Scheduler attempt deadline, and Scheduler retries.
- Provider voice remains off until the job, Stop hangup, and real WebRTC path
  all pass against the same controller revision.

## Action Items

1. Create the maintenance secret, job runtime identity, and separate Scheduler
   caller identity.
2. Deploy the job with the immutable Studio image at zero voice traffic.
3. Enable voice on a tagged controller revision and manually verify the job.
4. Create the every-minute Scheduler trigger with the explicit no-retry policy.
5. Verify one genuine microphone turn, Stop, provider hangup, durable cleanup,
   and the public page before promoting the voice revision.

# The Aya diagnostic chain: configured, missing, and the deploy that has not been run

**Status:** Reviewable. **Nothing deployed, no IAM applied, no grant widened.** Dual-run off,
production mailboxes untouched.<br>
**Date:** 2026-10-06 · Claude (`claude-code-cli`), Redis control lead<br>
**For review by:** Codex (quality), Gemini (architecture)

## The chain, and why it has no HTTPS bridge in it

```
Aya posts AYA_REQ row               the board, which is Aya's only tool
  -> worker validates               allowlisted claim, allowlisted action, age, rate, idempotency
  -> AYA_RECEIPT row                carries the request id, so the ask is visibly owned
  -> TLS Redis synthetic probe      server-named key, server nonce, server TTL, read back, deleted
  -> AYA_RESULT row                 carries the SAME request id
  -> Aya reads it back              the board again
```

A bridge was proposed so Aya could call Redis over HTTPS. **Measured against the live project, the
bridge is not needed for this chain**: the `bus-reconciler` runtime already holds the board
credential, `REDIS_AUTH_STRING`, `REDIS_CA_CERT` and `private-ranges-only` egress, and contains no
model, so routing costs no paid wake. A bridge would add a public-ingress service, a second identity,
an invoker grant and an OIDC path to connect two things that already share a transport.

## Configured versus missing

| Component | State | Evidence |
|---|---|---|
| Aya writes a bounded request | **configured** | Aya posted `WA-AYA-CONF167-BLOCKER-20261005T164034Z` on 2026-10-05 |
| Aya reads a recorded result | **configured** | same tool |
| A deterministic runtime that reaches Redis over TLS | **configured** | `bus-reconciler`, egress `private-ranges-only` on `default/default`, both secrets mounted as files, own least-privilege SA |
| Read/write on `redis-central` proven | **configured** | `redis-probe` execution: PING, SET, byte-for-byte GET, TTL, DEL, zero keys left |
| Request handling (validate, receipt, probe, result) | **built here, NOT deployed** | `scripts/bus_request.py`, 28 tests |
| Entrypoint | **built here, NOT deployed** | `cloud/bus-reconciler/serve_requests.py` |
| A trigger | **ruled, not built** — a scheduler on this job | Gemini 2026-10-06 15:21:43Z; see below |
| HTTPS bridge, `aya-runtime` grants, invoker role | **missing and argued unnecessary** | above |

## What `aya-runtime` is, exactly

`projects/sfdc24/serviceAccounts/aya-runtime@sfdc24.iam.gserviceaccount.com`

**Nothing runs as it.** No Cloud Run job or service references it; no user-managed key exists; no
project role; no IAM policy on the account, so nobody can impersonate it. Permissions granted: none.
**The name does not connect it to Aya's voice session** — it is a label, not a binding, and this
fleet has already been bitten once by reading a name as an identity.

Nothing in this chain uses it. It stays empty until a consumer exists that actually runs as it.

## The security truth about the board, stated rather than papered over

**The board has no authenticated sender.** `source_tag` is a column the caller supplies, so anybody
who can append can claim to be Aya. No allowlist changes that, and a sender allowlist must not be
described as "verified origin".

So the defence is **consequence, not identity**. The one requestable action writes a key the *server*
names, in a namespace the *server* owns, with a nonce the *server* generates and a TTL the *server*
sets; reads it back; deletes it; verifies it is gone; and returns a receipt with no secret, no nonce
and no key name in it. A forged request buys one synthetic probe against a key the forger cannot name
or read. Tests prove each of those: a request carrying `key=bus:row:…`, `ttl=999999` or
`do=FLUSHALL` changes nothing.

The remaining bounds exist so a forged flood is not free: a request older than 30 minutes is refused,
one stamped in the future is refused, a request id already carrying a RESULT does nothing, at most
three are answered per run, oldest first — and **a malformed flood produces no refusal rows at all**,
because otherwise it is the same denial of service with our name on the rows.

**When a request can ever do something that matters, it needs a real identity first — not a longer
allowlist.**

## Idempotency

Keyed on the request id, and read **off the board**, not off local state: a Cloud Run job keeps no
disk that survives it, and a watermark that can be lost is a watermark that re-answers. A request id
with an `AYA_RESULT` row already on the board is skipped without a Redis call.

Writes go through `scripts/append.py` — the one path on this fleet that sends exactly **one**
transport attempt and then reads the row back by `Row_ID`. Its non-zero exit is logged as
`APPEND UNCONFIRMED` and never swallowed, because "the row may or may not be there" is the one thing
worth knowing about a board write.

## The trigger — RULED

**Gemini, architect lead, 2026-10-06 15:21:43Z: a scheduler on this job, and it pays for the empty
polls.**

> *"Stand your ground. Do not widen the default compute service account. The financial cost of an
> empty container start is fractions of a cent. The blast radius of a compromised identity with
> project-wide secretAccessor is catastrophic. Keep the trigger on bus-reconciler, paying for the
> empty polls, until board-watcher is migrated to a dedicated, least-privilege identity."*

I had recommended the other option — `board-watcher` already polls every two minutes, is
deterministic, and would wake this job only when a request exists, so nothing would pay for silence.
I flagged the constraint beside it rather than burying it: **`board-watcher` runs as the default
compute service account**, which our access inventory records as able to read *every* secret in the
project across five runtimes.

**Gemini weighted my constraint above my preference**, and the arithmetic is not close — an empty
container start costs fractions of a cent, a compromised project-wide `secretAccessor` identity costs
everything. So the trigger is a scheduler here and **`board-watcher` gets no new grant at all**: not
`run.invoker`, not Redis, not egress. The decision reopens only if it is ever given a least-privilege
identity of its own.

Neither is built. **A scheduler that executes a job which connects to Redis IS a connect**, and the
dual-run is held.

## Deploy steps, for review only — none of this has been run

```bash
# 1. Build (the image gains bus_request.py, serve_requests.py and append.py; nothing else changes)
GCLOUD=<gcloud> REDIS_HOST=<private address> bash cloud/bus-reconciler/deploy.sh build

# 2. A SECOND job on the same image, with the request entrypoint. Note the RELATIVE path:
#    WORKDIR is /app, and an absolute one is rewritten by Git Bash before gcloud sees it.
gcloud run jobs create bus-requests --project=sfdc24 --region=us-central1 \
  --image us-central1-docker.pkg.dev/sfdc24/cloud-run-source-deploy/bus-reconciler:v1 \
  --service-account bus-reconciler@sfdc24.iam.gserviceaccount.com \
  --command python --args=-B,serve_requests.py \
  --set-secrets "BUS_URL=BUS_URL:latest,BUS_SECRET=BUS_SECRET:latest,/secrets/ca/redis-ca.pem=REDIS_CA_CERT:latest,/secrets/auth/redis-auth=REDIS_AUTH_STRING:latest" \
  --set-env-vars "REDIS_CONNECT=true,REDIS_HOST=<private address>,REDIS_PORT=6378" \
  --network default --subnet default --vpc-egress private-ranges-only \
  --max-retries 0 --task-timeout 5m --cpu 1 --memory 512Mi

# 2b. LIVE MODE for a meeting (serve_requests.py loops while LOOP_UNTIL is ahead). The 5m timeout
#     above is for ONE pass; Cloud Run kills an execution at its task timeout whatever the loop
#     intends, so a live run MUST pass a timeout that covers LOOP_UNTIL (Cursor on 39ebd38). This is
#     the exact command run for the 2026-10-08 10 PM ET call (execution bus-requests-rk5zr):
gcloud run jobs execute bus-requests --region=us-central1 --project=sfdc24   --update-env-vars=LOOP_UNTIL=2026-10-09T03:30:00Z,LOOP_INTERVAL=10,LOOP_MAX_MINUTES=340   --task-timeout=6h --async
#     Without GITHUB_READ_TOKEN the git channel is read every 144 s (two repos under GitHub's 60
#     unauthenticated calls an hour); with it, once a minute.
#     KEEPALIVE, NOT APPLIED (2026-10-10): a past LOOP_UNTIL runs zero passes. The free
#     definition that sets a fresh LOOP_UNTIL every 3 hours, default off, is
#     .github/workflows/bus-requests-keepalive.yml. The command the owner runs is in
#     docs/CLOUD-FLEET-RUNBOOK.md under "bus-requests keepalive". No IAM in that file.

# 3. READ BACK before trusting any of it. Three of this job's four defects were found this way.
gcloud run jobs describe bus-requests --project=sfdc24 --region=us-central1 --format=json
#    Check: no env value rewritten into a C:/Program Files/... path; both secret mounts present;
#    vpc-access-egress private-ranges-only; the service account is bus-reconciler and not default.

# 4. Execute once, with a real request row already on the board, and read the log.
gcloud run jobs execute bus-requests --project=sfdc24 --region=us-central1 --wait
```

No new secret, no new service account, no new role, no public ingress. **And after the trigger
ruling there is no permission question left at all**: `board-watcher` gets nothing, so the scheduler
runs as this job's own identity and grants nobody anything new.

## The two switches, and which one this job needs

`REDIS_CONNECT`, not `REDIS_DUAL_ENABLED`. They were one setting until Copilot found that
`REDIS_DUAL_ENABLED=true` in a job's environment beat a settings file saying `enabled=false` - so
the documented off-switch did not work on any deployed job, this one included. Separating them
showed why it had to: `enabled` means *the dual-run is on*, `connect` means *this process may open
a socket*. A request worker only ever wanted the second, and asserting the first to get it is
exactly what let the dual-run's off-switch be bypassed.

The file wins on both. `scripts/redis_dual.settings.json` ships `enabled=false, connect=true`, and
neither can be switched back on from the environment; a missing or malformed file reads as both off.
`bash cloud/bus-reconciler/deploy.sh off` sets both to false, and needs no address to do it.

## Rollback

```bash
gcloud run jobs delete bus-requests --project=sfdc24 --region=us-central1 --quiet
```

Nothing else to undo: no IAM was granted, no existing job or service was modified, and the image is
additive — `bus-reconciler` and `redis-probe` are unchanged by it.

## What a receipt from this will and will not prove

A `PING` or `SET` receipt from a runtime proves **that runtime's** path and nothing else. The
`redis-probe` PASS proves `bus-reconciler`'s path to `redis-central`. It does **not** prove Aya's
chain, and a real receipt for that requires a genuine `AYA_REQ` row posted by Aya's own tool and an
`AYA_RESULT` read back by Aya. Until that exists, this document claims only what the two deployed
executions have shown.

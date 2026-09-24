# Where credentials live, and how a cloud runtime gets them

**Decided by Mr Salam, 2026-09-23.** This closes dependency 2 of
`docs/OPENAI-CLOUD-MIGRATION.md` — *"the live bus and provider credentials are
held in the laptop's `.env`"* — which was the gate on the rest of that migration.

**The answer: Google Secret Manager in project `sfdc24`, injected into Cloud Run
as environment variables.** Four options were put to him; this one won because it
was the only one that was **already in production**.

## It was not a greenfield decision

`sfdc24-stt-relay`, the live speech relay behind the streaming page, already runs
on Cloud Run and already reads its credentials this way:

```yaml
env:
- name: DEEPGRAM_API_KEY
  valueFrom:
    secretKeyRef: { name: DEEPGRAM_API_KEY, key: latest }
```

So this is an existing, working pattern being extended to the fleet — not a new
plane being stood up. The alternatives each lost on a concrete fact rather than a
preference:

| option | why not |
|---|---|
| Azure Key Vault | The subscription is live but **completely empty** — no vault, no VMs. It would mean a new plane for secrets alone, with the runtime either moving to Azure or reaching across clouds on every read. |
| GitHub Actions secrets | **Both `Blackboard` and `sfdc24-site` are PUBLIC repos.** Every credential would sit behind workflow-file review permanently. It also cannot serve the site's request path, which is the latency objection Mr Salam raised himself earlier the same day. |
| One encrypted file via `BLACKBOARD_ENV` | Cheapest, and already supported — it is how the live waker works today. But getting that file onto a cloud runtime securely is the unsolved part, and it does not survive a rebuilt box. A stepping stone, not a destination. |

## The runtime contract

Unchanged from what #172 landed, which is why no application code moved:

1. **Injected wins.** If `BUS_URL` and `BUS_SECRET` are both in the process
   environment, `bus.load_env()` returns them and never touches the filesystem.
   This is the cloud path.
2. **Otherwise a file**, at `<repo>/.env` or wherever `BLACKBOARD_ENV` points.
   This is the laptop path, and the scheduled waker still uses it.
3. **A partial injection fails closed** — one of the two present is an error, not
   a silent fall-through to a stale file.

`tests/test_cloud_runtime_contract.py` pins all three, plus the resolution order.

## Proven, not asserted

Run on 2026-09-23 against the live board, with `BLACKBOARD_ENV` deliberately
pointed at a path that does not exist, so a fallback to any file would fail
loudly rather than succeed and prove nothing:

```
1. pulling the bus pair out of Secret Manager
   BUS_URL      114 chars retrieved
   BUS_SECRET   31 chars retrieved
2. BLACKBOARD_ENV -> definitely-not-a-real-env-file.env   (exists: False)
3. bus.load_env resolved both values from the process environment
   live board read OK: 3617 rows
```

A Cloud Run container holding those two secrets can do the fleet's work with no
credential file on disk.

## The tool

`scripts/gcp_secrets_sync.py`. Two rules it is built around:

- **No value ever reaches a command line.** Arguments are visible in the process
  list to every process on the box and land in shell history. Values go to
  `gcloud` over **stdin** via `--data-file=-`, and there is no code path that puts
  one in `argv`. Enforced by parsing the module, not by reading it.
- **No value ever reaches stdout.** Every line printed is a name, a length or an
  outcome, because this output goes into session transcripts and waker logs.

It is **idempotent**: an existing secret gets a new version only when the value
actually differs from `latest`, compared on a SHA-256 digest so the existing
value is fetched but never displayed. Verified — a second run wrote nothing and
every secret still has exactly one version.

```
python scripts/gcp_secrets_sync.py --dry-run
python scripts/gcp_secrets_sync.py --only BUS_URL,BUS_SECRET
python scripts/gcp_secrets_sync.py --grant-accessor <service-account>
```

**Config is not a credential.** `MODEL_PROVIDER`, `ANTHROPIC_MODEL`,
`OPENAI_MODEL` and `WA_TO` are excluded by name. Putting a model choice in a
secret store means changing models requires a secret rotation.

## Deliberately not done

**No blanket IAM grant.** Only the bus pair is granted to the runtime service
account, because every unattended runner needs it. The provider keys are created
with **no accessor binding at all** — each service gets
`roles/secretmanager.secretAccessor` on the specific secrets it uses, at deploy
time. A single grant of all 27 to one service account would mean any Cloud Run
service in the project can read every key the business holds.

## Rotation of `BUS_SECRET`: measured, declined, closed

The value in Secret Manager is the same 31-character secret `BUS-ROTATE-001` has
been trying to retire since 3 September. **Mr Salam ruled on 2026-09-23 that it
stays**, and the reasoning is his:

> *"The work we are doing here is preliminary and not client based. As such the
> bus secret will not benefit anyone; is not worth changing now. proceed without
> rotating this."*

That is a correct reading of the exposure. Nothing on this board is client data,
the surfaces holding the secret are all ours, and rotation briefly breaks every
holder at once for no gain today. **This is not an open item and should not be
raised again as one.**

**What would change it, and the check already exists.** The decision is scoped to
*preliminary, non-client* work. The moment a client is invited onto any surface
that reaches this bus, `docs/ACCEPTANCE-CHECKLIST.md` gap 1 and PR #75's
principle — *clients never touch the working board* — both bite, and rotation
becomes part of that work rather than a standing chore. Until then it is settled.

## Still open, and it is his

**The laptop `.env` is still a second source of truth.** Secret Manager is now
authoritative for anything running in the cloud; the laptop lane still reads the
file. They agree today because the sync copied one into the other. Making the
file derive from Secret Manager, rather than the reverse, is the next step and
needs the scheduled tasks to move first.

**A note on the identity the relay runs as:** `sfdc24-stt-relay` uses the default
compute service account, which is broadly privileged. A dedicated per-service
account is the right shape, but changing the identity of a service that is on the
live demo path is not a change to make quietly. Flagged, not done.

## The unattended trigger, and why it needed no elevated shell

**Approved by Mr Salam, 2026-09-23.** Dependency 1 of the migration is *"`SFDC24
Blackboard Waker` is a Windows Task Scheduler job"*, and it had been recorded as
waiting on his elevated PowerShell. It was not actually blocked on that: a
trigger that lives in GCP needs no laptop at all.

`scripts/gcp_scheduler_bootstrap.py` provisions it, idempotently. **Cloud
Scheduler -> Cloud Run job**, hourly at :15 Toronto, running as a dedicated
service account that can do exactly one thing: run one job. No secret access, no
write role, and `run.invoker` bound **on the job** rather than on the project.

### Proven, end to end

Forced a fire and checked the execution count actually grew, rather than assuming
a created job works:

```
lastAttemptTime: 2026-09-23T21:13:14.708793Z
status: {}                                    <- empty means success

NAME               SUCCEEDED_COUNT  FAILED_COUNT
board-probe-2wpbb  1                              <- created 21:13:14, same second

FINGERPRINT rows_compared=2305 digest=068234f3b64c27e7 where=cloud-run cred=injected
```

Same digest as the laptop. Scheduler to Cloud Run to Secret Manager to the live
board, with no laptop anywhere in the path.

### Two things that fail SILENTLY, both of which cost time here

**1. The Cloud Scheduler service agent must be able to act as the invoker.** A
job created with `--oauth-service-account-email` sits at `status: code: -1` with
**no `lastAttemptTime` and no logs at all** until
`service-96522051727@gcp-sa-cloudscheduler.iam.gserviceaccount.com` holds
`roles/iam.serviceAccountTokenCreator` **on the invoker account**. The invoker had
no IAM policy bindings whatsoever and nothing said so.

**A missing `lastAttemptTime` means it never attempted**, which looks identical
to "attempted and the target refused" unless you go and check. That distinction
is the whole diagnosis.

**2. Use the v2 run endpoint, not v1 `namespaces`.** `roles/run.invoker` grants
`run.jobs.run`, which is exactly what v2 `:run` needs. The v1 form also reads the
job, which `run.invoker` does not grant — so v1 forces the role up to
`run.developer` for no benefit.

## Durable state, and why compare-and-swap is the point

**Dependency 3**, *"waker watermarks are local JSON files"*. A cursor on one
laptop's disk is a cursor the fleet loses when that laptop is rebuilt, and after
#187 and #189 it was the last thing pinning the unattended lane here.

`scripts/state_store.py`, stdlib only, two backends behind one interface:

| `BLACKBOARD_STATE_URI` | backend | token |
|---|---|---|
| unset | file, as today | mtime + size |
| `gs://bucket/prefix` | GCS | object **generation** |

**Durability is the easy half.** The half that has cost this fleet real incidents
is two writers touching one cursor — two surfaces under one tag is the collision
class behind three of them. So every `save` carries the token its `load`
returned, and a stale token is **refused, not merged and not overwritten**: the
loser is told it lost and can re-read. GCS gives this natively with
`ifGenerationMatch`, and `ifGenerationMatch=0` makes a first write safe against
two processes both creating it.

`advance()` holds the rule in one place: a cursor may move **forward or stay,
never back**. Backwards re-answers rows; forward past unread work loses them
silently, which is worse and is what #168 was written to prevent.

### Proven on the real thing, not a fake

Two Cloud Run executions are two containers, so two runs are a restart:

```
board-probe-62qvw  22:16:57   changed=True   value=2026-09-23T21:32:33  previous=None
board-probe-mt88k  22:19:27   changed=False  value=2026-09-23T21:32:33  reason=not newer

gs://sfdc24-fleet-state/wakers/board_probe.json
  {"watermark": "2026-09-23T21:32:33"}
  39 bytes, generation #1790201817265979, ONE version
```

The second container read back exactly what the first wrote — had state not
persisted it would have reported `changed=True, previous=None` like the first. And
**one generation** means the second run genuinely declined to rewrite rather than
writing the same value again.

The bucket has uniform access, public access prevention **enforced**, and
versioning on, so a bad cursor write is recoverable.

## Shadow mode: the cloud decides, and cannot act

**Cutover step 5**, *"Run cloud and laptop in shadow mode with cloud writes
disabled."* `cloud/waker-shadow/` runs the **live** waker's decision logic -
`board_waker.check_board` and `check_whatsapp`, the same file the laptop runs -
and compares verdicts.

### Writes are disabled by absence of capability, not by a flag

A flag is a promise. The job is deployed with **only** `BUS_URL` and `BUS_SECRET`:

- no `META_TOKEN`, so it **cannot** send Mr Salam a WhatsApp message
- no `GH_TOKEN`, so it **cannot** touch a repository

On top of that an AST guard refuses to run if the module names any waker write
path, against an **allowlist of two** - so a new writer added to `board_waker`
tomorrow fails the guard without anyone maintaining a denylist. The verdict output
records `can_send_whatsapp` and `can_reach_github` so a deploy that accidentally
injected a token would show up in the result rather than being invisible.

### Agreement, three runs each side, same cursor

| | board | whatsapp | combined | would_exit | fresh |
|---|---|---|---|---|---|
| laptop ×3 | NEWS | QUIET | NEWS | 10 | 13 / 0 |
| cloud ×3 | NEWS | QUIET | NEWS | 10 | 13 / 0 |

Identical on every field, including the row count. Both sides take their cursor
from `SHADOW_SINCE` — run with different cursors, two verdicts differ for a reason
that has nothing to do with where they ran.

The shadow keeps its **own** cursor, never the live waker's; writing the document
the doorbell depends on would make it a second writer to it. Three containers
appended to it under compare-and-swap:

```
gs://sfdc24-fleet-state/wakers/waker_shadow.json   3 generations
  runs: [00:00:34 NEWS exit=10, 00:02:33 NEWS exit=10, 00:04:08 NEWS exit=10]
```

### One finding, and it is smaller than it first looked

The shadow's first local run returned `UNKNOWN — board read failed (HTTP 404)`.
`check_board` reads through `board_say.bus_get`, which does **not** retry the
documented redirect flap, and the launcher aborts on any exit code but 0 or 10 —
so that read failing means a tick where no model starts.

**Measured before drawing a conclusion: 10 of 10 subsequent reads succeeded.** So
that 404 was a one-off, not the common case, and adding retry to `check_board`
would be hardening rather than a fix. Latency varied 3–47s across the ten, which
is worth knowing separately. Not changed here: `bus_get` is shared with the live
doorbell, and one change at a time.

## Step 6: the first cloud write, and the 404 that proved the design

**Cutover step 6**, *"Enable one allowlisted cloud acknowledgement with exact
row-ID read-back."* `cloud/ack-once/` is the **only** cloud job permitted to
change the board.

### What actually happened on its first real run

```
post_status : 404
post_body   : <!DOCTYPE html><html lang="en">...
readback    : found=true, matched_rows=1
verdict     : LANDED
```

**The POST returned 404 and the row landed.** A job that trusted the POST status
would have called that a failure and resent it — and on an append-only board that
is exactly how one `GROK-ZOOM-HYPERSONIC-001` became four. The read-back by
Row_ID is the only thing that settles it, which the fleet already knew and this
run demonstrated again from a new direction.

A second execution then reported `wrote: false`, recognised the prior attempt from
durable state, and re-verified the same Row_ID. **The live board carries exactly
one matching row.**

### The four properties, each enforced rather than promised

| property | how |
|---|---|
| **exactly one append** | an AST guard counts the append call sites and refuses unless there is exactly one — zero would exit 0 having proved nothing |
| **allowlisted** | recipient must be in `ACK_ALLOW_TO`, payload id must start with `ACK_REQUIRE_ID_PREFIX`; **both default empty, and empty refuses everything** |
| **never resent** | the POST is not retried, and an unreadable board is reported as `UNKNOWN — do NOT resend`, never as a missing row |
| **idempotent across runs** | the Row_ID is written to durable state **before** the POST, so a container killed mid-write leaves the id a later run must look for. `BLACKBOARD_STATE_URI` is mandatory: without it the job cannot promise it has not already run, so it refuses |

### What it writes, stated plainly

One row, `phase=RESULT class=CUTOVER`, addressed to `claude-code-cli` — the tag
this session already owns — and marked prunable in its own payload. **It is a
cutover probe, not an acknowledgement of another agent's work.** Acking real work
from the cloud is a larger change, because another agent may act on an ACK.

The allowlist is what makes that a configuration rather than a rewrite: point
`ACK_ALLOW_TO` and `ACK_REQUIRE_ID_PREFIX` at something real and this becomes a
real acknowledgement with no code change. A test pins that claim.

### One limit, recorded rather than papered over

An AST append-count **cannot see a loop around one call site** — one call site,
many runtime appends. The test says so out loud. What actually bounds repeat
writes is the durable idempotency record, which has its own tests.

### What this does and does not replace

It replaces Task Scheduler **for this lane**. The probe is read-only and
stateless, so it needed nothing else. **The waker is a different matter**: it
holds a watermark, and moving that to durable storage is dependency 3 and still
open. Until then the scheduled waker stays on the laptop.

Related: `docs/OPENAI-CLOUD-MIGRATION.md`, `docs/ACCEPTANCE-CHECKLIST.md` (gap 1,
identity is derived and never claimed), `docs/GCLOUD-MIGRATION.md`.

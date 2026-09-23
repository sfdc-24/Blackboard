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

## Still open, and both are his

1. **`BUS_SECRET` has not been rotated.** The value now in Secret Manager is the
   same 31-character secret that `BUS-ROTATE-001` has been trying to retire since
   3 September — vm-chrome held it in session, and it is recorded as exposed.
   **Putting an exposed secret in a vault does not un-expose it.** Rotation
   means a new value in Apps Script Script Properties and a new version here, and
   it is his call because it briefly breaks every holder at once.
2. **The laptop `.env` is still a second source of truth.** Secret Manager is now
   authoritative for anything running in the cloud; the laptop lane still reads
   the file. They agree today because the sync copied one into the other. Making
   the file derive from Secret Manager, rather than the reverse, is the next step
   and needs the scheduled tasks to move first — which needs his elevated shell.

**A note on the identity the relay runs as:** `sfdc24-stt-relay` uses the default
compute service account, which is broadly privileged. A dedicated per-service
account is the right shape, but changing the identity of a service that is on the
live demo path is not a change to make quietly. Flagged, not done.

Related: `docs/OPENAI-CLOUD-MIGRATION.md`, `docs/ACCEPTANCE-CHECKLIST.md` (gap 1,
identity is derived and never claimed), `docs/GCLOUD-MIGRATION.md`.

> **OBSOLETE 2026-09-26**: it installs the self-hosted bus, which is not the live board. Kept as history, do not follow it. How we work now: [`EXPRESS.md`](EXPRESS.md).

# blackboard-bus — Deploying to an Ubuntu VM on Google Cloud

The package carries the Blackboard bus architecture on a VM you control:
the v1 Apps Script bus contract (clients port by changing `BUS_URL`), the
filed fixes (REPLACE, tail read, schema-aware append, honest status codes,
write echoes), and the proposed LEDGER SCHEMA v1 event ledger.

## 0. What you need

- A GCP project with billing, `gcloud` authenticated.
- The `.deb` (built by `packaging/build-deb.sh` on any Ubuntu, including WSL).
- A bus secret. Generate fresh — do not reuse the Apps Script one, it has
  leaked into source history before (ISSUE 025 / GW-DIAG-003):
  `openssl rand -base64 24 | tr -d '/+=' | cut -c1-31`

## 1. Create the VM

```bash
gcloud compute instances create blackboard-bus \
  --zone=us-east1-b \
  --machine-type=e2-micro \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --tags=blackboard-bus
```

`e2-micro` is enough: the service is stdlib Python + SQLite; the whole
Alpha DB board today is ~750 rows / ~400 KB.

## 2. Install

```bash
gcloud compute scp blackboard-bus_1.1.0_all.deb blackboard-bus:~ --zone=us-east1-b
gcloud compute ssh blackboard-bus --zone=us-east1-b
# on the VM:
sudo apt-get update && sudo apt-get install -y ./blackboard-bus_1.1.0_all.deb
sudoedit /etc/blackboard-bus/env         # set BUS_SECRET=<generated value>
sudo systemctl enable --now blackboard-bus
curl -s http://127.0.0.1:8787/           # {"ok":true,"service":"sfdc24-blackboard-bus",...}
```

## 3. HTTPS — pick ONE of these before any fleet client connects

The bus binds `127.0.0.1:8787` by default on purpose. The secret rides in
request bodies, so plaintext HTTP across the internet is not acceptable.

**Option A (recommended): Caddy reverse proxy with automatic TLS.**

```bash
sudo apt-get install -y caddy
# /etc/caddy/Caddyfile — needs a DNS A record, e.g. bus.sfdc24.com -> VM IP:
#   bus.sfdc24.com {
#       reverse_proxy 127.0.0.1:8787
#   }
sudo systemctl reload caddy
```

Open only 80/443:

```bash
gcloud compute firewall-rules create allow-bus-https \
  --target-tags=blackboard-bus --allow=tcp:80,tcp:443
```

Clients then use `BUS_URL=https://bus.sfdc24.com/`. Note the DNS charter
lesson (Sep 2-3 outage): adding the A record via NameSilo can silently drop
domain forwarding — re-verify apex forwarding after the change.

**Option B: IAP TCP tunnel, no public exposure at all.**
IAP forwards to the VM's internal NIC, not to loopback — so the bus must
bind all interfaces, protected by a firewall rule that admits ONLY Google's
IAP range:

```bash
# on the VM: set BUS_BIND=0.0.0.0 in /etc/blackboard-bus/env, then
#   sudo systemctl restart blackboard-bus
gcloud compute firewall-rules create allow-bus-iap \
  --target-tags=blackboard-bus --allow=tcp:8787 \
  --source-ranges=35.235.240.0/20
# from a client machine:
gcloud compute start-iap-tunnel blackboard-bus 8787 \
  --local-host-port=localhost:8787 --zone=us-east1-b
```

Clients then use `BUS_URL=http://localhost:8787/` through the tunnel.
Good for a Claude/laptop-only bus; useless for Pipedream.

## 4. Seed from the existing board

From any machine that holds the old bus URL + secret (or a saved dump):

```bash
# live pull from the Apps Script bus:
BUS_URL='https://script.google.com/macros/s/<deployment>/exec' \
BUS_SECRET_SOURCE='<apps-script-secret>' \
blackboard-bus --db /var/lib/blackboard-bus/bus.db import-bus --title 'Blackboard - Alpha DB'

# or from a saved read dump:
blackboard-bus --db /var/lib/blackboard-bus/bus.db import-file board_read.json
```

Run imports as root or the `blackboard-bus` user (`sudo -u blackboard-bus ...`)
so the DB stays owned by the service. Stop the service during an import of a
sheet it already serves; imports refuse to overwrite an existing title.

## 5. Client migration

Any v1 client (bus.ps1, bcb_lint.py, Pipedream steps) ports by pointing
`BUS_URL` at the new host. Two behavior changes to know:

1. **Honest status codes.** Auth failures are real 401s, malformed requests
   real 400s (body still carries `_httpStatus` + the familiar `error` text).
   Clients that treated HTTP≠200 as transport failure must read the body.
2. **No redirect dance.** Responses come straight back — the Apps Script
   302/echo-layer flake and its retry rule do not apply here.

New capabilities: `replace` (docs), `read` with `limit`, `create`, `list`,
and the proposed v2 ledger (`event` / `inbox` / `work`) — see the source
docstring for the contract. The v2 surface implements LEDGER SCHEMA v1
as PROPOSED; using it in anger is Mr. Salam's ratification call.

### v1.1 fenced-claim cutover

Stop every older bus process that can write the SQLite database before starting
v1.1. Do not run mixed binaries during the cutover. The additive migration keeps
legacy rows unchanged, and database triggers reject old-style unfenced writes
after a work item's first v1.1 CLAIM, but an old reader does not understand the
new state reduction and a never-yet-fenced item remains legacy-compatible.

Every successful CLAIM returns a private `fence_token` and monotonically
increasing `claim_generation`. Persist both atomically with `work_id` and
`actor_tag` before doing work. Carry the token on every PROGRESS, BLOCK, RELEASE,
COMPLETE, or CANCEL. A live same-actor CLAIM is a renewal and must carry the
current token; a tokenless same-actor process receives 409. Never let a delayed
lower-generation receipt overwrite a higher locally persisted generation.

Use `CLAIM`/`CLAIMED` for every new fenced claim and the canonical lifecycle
pairs `BLOCK`/`BLOCKED`, `RELEASE`/`OPEN`, `COMPLETE`/`DONE`, and
`CANCEL`/`CANCELLED`. Once a work item is fenced, contradictory pairs fail
before storage rather than leaving the previous claim deceptively active;
untouched legacy items retain their v1 behavior.
Prefer UTC `Z` for leases. Numeric offsets may be coloned or compact but must
have an absolute value below 15 hours so Python and SQLite enforce the same time.
Every new CLAIM and subsequent fenced state-bearing lease is capped at four hours
from acceptance, including a lease refreshed by `PROGRESS`; `NOTE`/`FINDING`
lease fields never affect a hold. Untouched legacy items keep their v1 rules.

The `inbox` and `work` read APIs expose generations but redact tokens, including
the token for a public CLAIM event id. A lost or ambiguous CLAIM response cannot
be recovered through reads in v1.1: do no work, recover the process's own atomic
receipt if possible, or wait for lease expiry and make a fresh tokenless CLAIM.
An exact COMPLETE replay remains receipt-idempotent and is checked before current
authorization; a changed stale result fails closed.

The token proves freshness, not actor identity under the fleet-wide shared
`BUS_SECRET`. Any downstream system that performs an irreversible side effect
must persist the highest accepted generation per work item and reject lower
generations before performing the effect.

## 6. Operations

- Logs: `journalctl -u blackboard-bus -f` (bodies are never logged; the
  secret appears nowhere).
- Backup: `sqlite3 /var/lib/blackboard-bus/bus.db ".backup /root/bus-$(date +%F).db"`
  in a daily cron; the DB is a single file.
- Rotation: set the new value in `BUS_SECRET`, move the old one to
  `BUS_PREVIOUS_SECRET`, `systemctl restart blackboard-bus`, migrate
  clients, then delete `BUS_PREVIOUS_SECRET` and restart again.
- Uninstall: `apt-get remove blackboard-bus` (keeps data);
  `apt-get purge` deletes `/var/lib/blackboard-bus` and the env file.

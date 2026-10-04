# pi1-cli: the doorbell, the credential, and what it can do with them

His instruction, 2026-10-04 00:40Z: *"can you help pi1-cli with the setup that's blocking it"*, then
*"give it direct access and have Gemini support the configuration and setup for GCP and anything
needed for github"*. This is what was blocking it, what is now unblocked, and the one-minute install
on the Pi.

pi1-cli shadows the operator of the conference line (his direction of 2026-10-03; the job itself is
written down in conference `docs/okf/operator.md`). Two things stood between shadowing and working:
**nothing woke it**, and **it held no cloud credential**. The first is this page. The second he did
himself.

---

## 1. The doorbell: the Pi pulls, because nothing can push to it

`cloud/board-watcher/main.py` carries three routes and each one **starts a Cloud Run job**. The Pi is
not a Cloud Run job: it is on home Wi-Fi behind NAT with no inbound path, so no watcher tick, no
scheduler and no job can reach it. A fourth route would have had nothing to start. His own ruling of
2026-09-24 already said which way this goes — *"shouldn't things work based on pull system"* — so
`scripts/pi1_wake.py` runs **on the Pi** and pulls.

It needs no Cloud Run job, no Cloud Scheduler, no GCP credential and no new paid resource: only the
bus pair the Pi already holds, which is how it has been posting its own rows all along.

Each pass it reads only the rows new since its own cursor, keeps the ones addressed to `pi1-cli` or
`ALL` by the fleet's own predicate (`agent_waker.addressed_to`, so there is no second opinion about
what "addressed to" means), drops its own rows and waker replies, appends each new row to an inbox
file as JSON lines, and runs the wake command **once for the pass**.

**Four refusals Codex's review put there** (NO-GO on head `0a440cc`,
`CODEX-PI-WA-QC-RESULT-20261004T012631Z`), each with its own control:

* **It never skips an arrival.** The watermark is the **read's own moment**, stamped before the
  read, so a row that lands while the wake command runs is the next pass's business. The old code
  stamped the clock *after* the work, so a five-minute wake moved the cursor past everything that
  arrived during it.
* **A capped read is refused, not held.** Holding the cursor on a full read was the first
  version, and it was not a repair: `limit` on this bus means the **newest n rows**, so the next
  read returns the same newest n and the older addressed rows the cap left out are never reachable
  — the bus has no `until` to page back with. So `--limit` is accepted **only with `--dry-run`**,
  and any pass that could ring or move the cursor reads uncapped (exit 2 otherwise). The held
  cursor stays in the code as a belt behind that refusal.
* **A corrupt cursor fails closed.** Missing means first run, which primes; **unreadable means
  refuse** — exit 2, nothing read, rung or written. Treating a truncated file as "no history" is
  how a doorbell primes away everything pending. The cursor is also written atomically now, so a
  kill mid-write leaves the old file rather than half a new one. **A cursor that is a string but
  not a timestamp counts as unreadable**, and is checked before anything is read: it used to fail
  open in both directions at once — the read lost its `since` and went out over the whole board,
  while the watermark was still truthy so priming was skipped — which would have rung for every
  row in the board's history inside the age window.
* **One row is one inbox entry**, whether the wake failed, succeeded, or was killed between the
  two. What has been *written down* is recorded separately from what has been *handled* — and
  **the inbox file is the journal**, not just the state. The inbox is appended and fsynced *before*
  the state records that it was, because the other order loses the notification outright; so a kill
  landing between those two writes used to put the row in the inbox twice on restart. Recovery now
  reads the inbox's own Row_IDs, and a **torn final line** — a kill *during* the append — is
  truncated away so the row is written once, whole. The notification itself is at-least-once by
  design: after a kill the Pi is woken again, and the inbox still holds one entry.
* **A row to ALL is a row to this Pi.** `agent_waker.addressed_to` answers for a *named* tag, so a
  genuine `ALL` row — target `ALL`, payload `to=ALL`, naming nobody — was missed, and the test that
  was supposed to cover it had been passing only because its fixture also carried `to=pi1-cli`.
  `ALL`, `everyone` and `fleet` are explicit now, in `to=`, `cc=`, `cast=` and the target column.

And two from the night it was written:

* **It primes on first run.** With no cursor it records what is on the board as seen and wakes
  nothing. Hours earlier, one EOD send put nineteen WhatsApp messages on his phone because a queue
  with no drainer had collected eighteen rows since Sep 21 and nothing had primed. A doorbell's
  first ring must not be a fortnight of news.
* **An old row does not ring.** Past `--max-age-hours` (default 24) a row is recorded as seen and
  the wake does not run for it. A timestamp that cannot be read counts as old, because this board
  has rows whose timestamp column holds a BCB payload.

And one from the Pi's own hardware:

* **The cursor never moves backwards.** This Pi has no clock battery. A boot that reads the board
  before `time-sync.target` lands carries whatever hour the clock believes in, and a cursor that
  followed it down would re-ring everything since. The pass says `CLOCK WENT BACKWARDS`, holds the
  cursor and keeps going. It is the same dead battery that opened the 2 PM TFT an hour late, which
  is also why the timer unit carries `After=time-sync.target` and `Persistent=false`.

And a failed wake **holds the watermark**, so the next pass rings again rather than forgetting —
the watcher's rule, for the watcher's reason.

**The suite runs in CI.** `tests/test_pi1_wake.py` (45 controls) is wired into
`.github/workflows/python-suites.yml`, in an empty network namespace, on every change to it or to
`scripts/pi1_wake.py`. Until that was wired, the green tick on the pull request came from the MCP
and semantic checks and this suite was the author's word for its own claims.

**It is a notification, never a task.** A row's content never reaches a shell: the wake command is
the Pi's own script, and all it is told is how many rows arrived (`PI1_WAKE_ROWS`) and where they
are written (`PI1_WAKE_INBOX`). That is Codex's R2 — a notice is not task or reply authority — and
pi1-cli's first hook is deliberately a log line plus a notice on the TFT, with no session start.

### Install it (one minute)

```bash
# 1. the bus pair the Pi already has, wherever it keeps it
export BLACKBOARD_ENV=/home/pi/.blackboard.env

# 2. prime: record what is already on the board, ring nothing
python3 scripts/pi1_wake.py --prime

# 3. what a ring should do. Yours to write - it is how a session starts on this box.
cat > /home/pi/wake-session.sh <<'SH'
#!/usr/bin/env bash
# $PI1_WAKE_ROWS new rows; each is a JSON line in $PI1_WAKE_INBOX, newest last.
set -euo pipefail
logger -t pi1-wake "doorbell: ${PI1_WAKE_ROWS} row(s) in ${PI1_WAKE_INBOX}"
# start the session, or arm the TFT, or both
SH
chmod +x /home/pi/wake-session.sh

# 4. one pass, to see it work
python3 scripts/pi1_wake.py --dry-run
python3 scripts/pi1_wake.py --cmd /home/pi/wake-session.sh
```

Then keep it running. Either the loop:

```bash
python3 scripts/pi1_wake.py --loop 60 --cmd /home/pi/wake-session.sh
```

or a systemd **user** service and timer, which survives a reboot and needs no root:

```ini
# ~/.config/systemd/user/pi1-wake.service
[Unit]
Description=pi1-cli board doorbell (one pass)
After=network-online.target time-sync.target

[Service]
Type=oneshot
Environment=BLACKBOARD_ENV=/home/pi/.blackboard.env
WorkingDirectory=/home/pi/Blackboard
ExecStart=/usr/bin/python3 scripts/pi1_wake.py --cmd /home/pi/wake-session.sh
```

```ini
# ~/.config/systemd/user/pi1-wake.timer
[Unit]
Description=ring the pi1-cli doorbell every minute

[Timer]
OnBootSec=90
OnUnitActiveSec=60
AccuracySec=5s
Persistent=false

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now pi1-wake.timer
loginctl enable-linger pi1          # so the timer runs with nobody logged in
journalctl --user -u pi1-wake -f
```

**`After=time-sync.target`, and `Persistent=false`, both on purpose.** This Pi has no clock battery:
after a reboot its clock is stale until NTP syncs, which is what opened the 2 PM call on the TFT an
hour late on 2026-10-03. A doorbell that rings on a stale clock rings for the wrong hour, and a
`Persistent=true` timer would fire every missed minute at once.

Worst-case latency with the timer above is about 60 s plus the read — better than the fleet's own
wake path, which is a 180 s watcher plus a job start.

---

## 2. The credential: he did it himself

Read back from the project at 2026-10-04 00:20Z, not taken on trust:

| What | Where it is bound | Why |
|---|---|---|
| `roles/run.developer` | project | deploy a revision of the `conference-chair-pool` worker pool |
| `roles/cloudbuild.builds.editor` | project | `gcloud builds submit` with `chair/cloudbuild.yaml` |
| `roles/serviceusage.serviceUsageConsumer` | project | the API calls the SDK makes on its behalf |
| `roles/logging.viewer` | project | the chair's own log window, for `tools/call_report.py` |
| `roles/iam.serviceAccountUser` | **on `conference-chair@` only** | a pool deploy runs as the chair's service account |
| `roles/artifactregistry.reader` | **on the `cloud-run-source-deploy` repository only** | read the image digest a deploy pins |
| `roles/storage.objectViewer` | **on `gs://sfdc24-fleet-state`, conditioned** | read the call transcripts |

He created the service account `pi1-cli@sfdc24.iam.gserviceaccount.com` at 00:07:11Z and a
user-managed key at 00:18:32Z. Two of those bindings are tighter than project-wide, which is the
right shape: the service-account-user grant is on the chair's identity alone, not on every identity
in the project.

### The two gaps, written down rather than discovered later

1. **`iam.serviceAccountUser` on the chair's service account is a real blast radius, and it is
   unavoidable for a deploy.** That service account holds the chair's 8 secrets. A deploy is "run
   this image as that identity", so an agent that can deploy can run code as the chair and read what
   the chair can read. The alternative is a deploy-only Cloud Run job the Pi may merely *invoke*,
   holding no path to the chair's identity — a new resource, so his word, and Gemini has the design
   question (`CCC-GEMINI-PI1-ACCESS-20261004T0045Z`, Q1 and Q2).
   **Gemini's design, asked for by him and answered at 00:18Z** (`GEMINI-WAKE-CCC-GEMINI-PI1-ACCESS-20261004T0045Z-6f6f7a5de1`):
   build a Cloud Run **deployer or broker** that holds the GCP roles, and grant the Pi only
   `roles/run.invoker` on it — or let the Pi sign its request with the bus pair it already has and
   have the broker verify and act. Either way no GCP credential leaves the cloud boundary. That is
   the same shape as the alternative above, so it is now the design of record for duty 4, and it is
   a new resource, so it waits for his word. One correction, because Gemini is a model endpoint and
   its answer is reasoning rather than a measurement: it named `roles/run.admin` for the deploy. The
   pool deploy in `chair/deploy.sh` needs `roles/run.developer`, which is what he granted and which
   is the narrower of the two.

2. **The bucket condition permits listing the whole bucket, not only the transcripts.** It reads
   `resource.type == "storage.googleapis.com/Bucket" || resource.name.startsWith(".../objects/conference/transcripts/")`.
   Object **bytes** are restricted to the transcripts prefix, which is what matters; but
   `storage.objects.list` is checked against the *bucket* resource, so the first half of that `||`
   lets the Pi see the **names** of everything in `gs://sfdc24-fleet-state` — fleet cursors, soak
   data, state objects. Dropping that disjunct would also stop the Pi listing the transcripts
   prefix at all, which is survivable (the operator gets the exact object path from the chair's own
   log line and the call's record). Left as he set it, named here, his to tighten.

3. **The key exists, and Gemini says it should not.** Its answer: *"Do not put a long-lived GCP
   JSON key on the Pi."* He created one at 00:18:32Z, which is what makes duties 1 to 3 possible
   tonight, so the honest position is a sequence rather than a contradiction: keep the key while the
   Pi files records, plans and floor grants; build the broker for the deploy; then disable the key
   and rotate nothing because there is nothing left to rotate. Until then the key is a secret on a
   device in his house, and that is a known, written-down exposure rather than an oversight.

### Verify it from the Pi, first-hand

Each line is a capability above, and each should be run on the Pi with its own key active:

```bash
gcloud auth activate-service-account --key-file=/home/pi/.pi1-cli-sa.json
gcloud config set project sfdc24

gcloud run worker-pools describe conference-chair-pool --region us-central1 \
  --format='value(metadata.name,status.latestReadyRevisionName)'          # run.developer
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/sfdc24/cloud-run-source-deploy/conference-chair \
  --limit 1 --format='value(version)'                                      # artifactregistry.reader
gcloud logging read 'resource.type="cloud_run_revision"' --freshness=1h --limit 1 \
  --format='value(timestamp)'                                              # logging.viewer
gcloud storage ls gs://sfdc24-fleet-state/conference/transcripts/ | head -3  # storage, conditioned
gcloud builds list --limit 1 --format='value(id,status)'                   # cloudbuild.builds.editor
```

A deploy itself is **not** on that list on purpose: the first real deploy pi1-cli runs is duty 4 of
the handover, after duties 1 to 3 have each passed twice, and never while a call is live.

---

## 3. GitHub

`docs/okf/` admits `pi1-cli/` branches in all three repositories — conference
`tools/check_ownership.py`, Blackboard `scripts/okf_ownership.py`, site `tools/okf_ownership.py` —
minus `docs/okf/floor.md`, which hands out the pen, and `docs/okf/gemini/`.

**That admission is not on `main` yet.** It lands with conference #162, Blackboard #319 and site
#277; until they merge, CI judges a `pi1-cli/` branch by the rule that shipped in #161, which does
not know the prefix, and refuses its OKF edits. So the first thing a merge unblocks is pi1-cli
writing the OKF at all.

The push credential is the other half. Gemini's answer names the shape — *"a fine-grained Personal
Access Token scoped strictly to the required repositories and actions"* — which for this fleet means
contents and pull-requests write on `sfdc-24/conference`, `sfdc-24/Blackboard` and
`sfdc-24/sfdc24-site`, and nothing else: no workflow scope, no admin, no other repository. Creating
that token is his tap, like every credential here.

It did not answer the second half of that question, so it stays open and it matters: a fine-grained
token cannot tell CI **which** agent pushed, because the whole fleet pushes as one user and the
branch prefix is attribution, not an identity. A `pi1-cli` token pushing a `claude-code-cli/` branch
would be admitted by the ownership rule today. The honest options are a GitHub App installation per
agent (a real identity, and a build), or accepting the prefix as a convention and saying so. Until
one is chosen, the Pi keeps its read-only clone for anything outside `docs/okf/` and hands its diffs
to the operator.

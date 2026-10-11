# Cloud fleet runbook: what runs where, and how to change it safely

One page for anyone about to touch the unattended half of the fleet. The
evidence for how it got here is in `docs/CLOUD-CREDENTIAL-CONTRACT.md` and
`docs/OPENAI-CLOUD-MIGRATION.md`; this page is the current state and the
procedure.

**Read live, then trust this page.** The job table was read from GCP on
2026-09-24 between 22:00 and 22:30 UTC; the studio controller table on
2026-09-25 at 04:25 UTC. Images move with every rollout, so run the three commands
in *Read the live state* before acting on a row here.

## The shape

- GCP project `sfdc24`, region `us-central1`. Credentials live in Secret
  Manager and are injected into the jobs; nothing reads a `.env` in the cloud.
- The **board** is the Google Sheet behind the Apps Script bus gateway. Six of
  the seven jobs below read it through that gateway; `studio-voice-sweep` talks
  only to the studio controller.
- **board-watcher** runs every minute, reads the rows new since its cursor, and
  starts the job each row is for. It also starts a route that still has pending
  work from an earlier read, so a quiet minute can still start a job; with
  nothing new and nothing pending it starts nothing.
- Each started job keeps its own cursor in `gs://sfdc24-fleet-state/wakers`,
  written compare-and-swap, so two runs cannot answer the same row twice.

## Jobs

| job | what it does | started by | image (2026-09-24) | rollback | task timeout |
|---|---|---|---|---|---|
| `board-watcher` | reads new rows, starts the routes below | `board-watcher-2min` (`* * * * *`, every minute despite the name) | `board-watcher:e6f9e5a` | `board-watcher:2f73072125bc` | 100s |
| `gemini-waker` | answers rows addressed to `gemini` (reasoning only: no shell, no repo) | board-watcher | `agent-waker:697e190` | `agent-waker:e6f9e5a` | 840s |
| `claude-api-waker` | answers rows addressed to `claude-api`, and stands by for WhatsApp messages addressed to `claude-code-cli` (`agent_waker.addressed_to`) | board-watcher | `agent-waker:697e190` | `agent-waker:e6f9e5a` | 840s |
| `wa-outbox` | sends WhatsApp requests found on the board | board-watcher | `wa-outbox:2f73072125bc` | - | 300s |
| `board-probe` | hourly health read of the board; its cursor must only move forward | `board-probe-hourly` (`15 * * * *`) | `board-probe:e6f9e5a` | `board-probe:v4` | 1800s |
| `waker-shadow` | runs the laptop waker's decision logic read-only, to prove the cloud can replace it | `waker-shadow-hourly` (`45 * * * *`) | `waker-shadow:9fa38c1c9499` (#242, rolled 2026-09-24 ~23:25Z) | `waker-shadow:675f6fe166a9` | 1800s |
| `studio-voice-sweep` | backstop that hangs up any studio voice call the controller missed, so none keeps billing (`cloud/studio-controller/ADR-001-VOICE-SWEEP-BACKSTOP.md`) | `studio-voice-sweep-every-minute` (`*/5 * * * *`, every five minutes despite the name) | `studio-controller@sha256:1736bc4f…` (the reviewed studio image, pinned by digest) | the previous digest | 30s |

Images are in `us-central1-docker.pkg.dev/sfdc24/cloud-run-source-deploy/`,
tagged with the first 7 or 12 characters of the `main` commit they were built
from. Each is built from `cloud/<dir>/cloudbuild.yaml`, and the directory is not
always the job name: `gemini-waker` and `claude-api-waker` both build from
`cloud/agent-waker/`, and `studio-voice-sweep` runs the image built from
`cloud/studio-controller/`. The rest build from the directory of their own name.

## The studio controller service

`sfdc24-studio-controller` is a Cloud Run **service**, not a job.

As of 2026-09-25 04:25Z, after three homepage releases that night. The
rollback chain is ONLY `r3-d961048` -> `r2-44ed5d7` -> `talk-293d248` ->
`p3-claude-6fd0dc4`. The two canary rows are never rollback targets: they ran at
0% beside production, and `lead-test-bb68838-r3` carries Salesforce lead facts
and the `Headless_*` credentials - sending it 100% would publish that on the
production URL.

| revision | traffic | tag | what it is |
|---|---|---|---|
| `sfdc24-studio-controller-r3-d961048` | **100%** | `r3-live` | main d961048: talk lane, analyst, scene grammar, two voices (`/speak`, `/recap`) |
| `sfdc24-studio-controller-r2-44ed5d7` | 0% | `r2-live` | main 44ed5d7: talk lane, analyst, scene grammar |
| `sfdc24-studio-controller-talk-293d248` | 0% | `talk-live` | main 293d248: talk lane, blank start |
| `sfdc24-studio-controller-p3-claude-6fd0dc4` | 0% | `claude-text` | before voice was on |
| `sfdc24-studio-controller-lead-test-bb68838-r3` | 0% | `lead-canary` | Salesforce lead facts canary - NOT a rollback target |
| `sfdc24-studio-controller-voice-cap-b04762d` | 0% | `voice-canary` | old voice canary - NOT a rollback target; `studio-voice-sweep` still calls this tag URL (shared GCS state, so it works) |

Production env on r3: `STUDIO_ENABLE_VOICE=true`, `STUDIO_ENABLE_LEAD_FACTS=false`
(and no `Headless_*` / org-id secrets), `STUDIO_VOICE_MINT_CAP=10`,
`STUDIO_WORKER_MODEL=claude-sonnet-5` (the blueprint builder; the analyst stays on
claude-opus-5). The service template carried the lead-canary settings once;
every release sets these explicitly.

A release: build from a merged main SHA in a detached worktree, deploy with
`--no-traffic --tag <name>`, read `/health` on the tag URL (voice, talk,
analyst, voices), then `gcloud run services update-traffic ... --to-revisions=<rev>=100`
as a command of its own. `python scripts/acceptance_current_state.py --only studio_controller`
confirms production afterwards. It requires voice, talk, analyst and both voices,
so only r3 passes (`ok; voice/talk/analyst on; voices ['host', 'architect']`). After
a rollback its FAIL line names exactly what that revision lacks - expected, not a
new fault:

| rolled back to | expected `studio_controller` line |
|---|---|
| `r2-44ed5d7` | `ok=True, off: voice:host, voice:architect` |
| `talk-293d248` | `ok=True, off: analyst, voice:host, voice:architect` |
| `p3-claude-6fd0dc4` (voice off) | `ok=True, off: voice, talk, analyst, voice:host, voice:architect` |

The traffic column decides what the untagged `*.a.run.app` URL serves: that
URL is production. A tag URL always reaches its own revision, whatever the
split says. So a canary run stays off the production revision for two reasons
together: the authenticated canary runner
(`cloud/studio-controller/tools/authenticated_canary.py`) will only ever call
the `lead-canary` tag URL, never the untagged one (#231); and that tag points at
a revision with 0% of the traffic. Moving the tag onto the 100% revision would
defeat the second, so check the table before a live canary run.

## Read the live state

`gcloud` is installed but not on PATH on the laptop; prefix
`PATH="$PATH:/c/Users/salam/AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin"`.

```bash
gcloud run jobs list --project sfdc24 --region us-central1 \
  --format="table(name,spec.template.spec.template.spec.containers[0].image)"
gcloud scheduler jobs list --project sfdc24 --location us-central1 \
  --format="table(name,schedule,state,lastAttemptTime)"
gcloud run services describe sfdc24-studio-controller --project sfdc24 \
  --region us-central1 --format="json(status.traffic)"
```

A scheduler with no `lastAttemptTime` never attempted anything; an ENABLED
state proves nothing on its own.

## Is it healthy?

- **`python scripts/soak_report.py`** judges the cutover soak: PASS, FAIL or
  NOT YET. It checks hourly cadence of the probe and the shadow, a cursor that
  never goes back, exactly one board row for the ack target, and that every run
  was in the cloud with injected credentials. Each UNKNOWN shadow run is listed
  with its time and cause (#234). Run from a worktree with
  `BLACKBOARD_ENV` pointing at the laptop's `.env`.
- **`python scripts/governor_live_check.py`** compares the Apps Script version
  the public deployment serves with `origin/main`: exit 0 match, 1 differ,
  2 unknown. A DIFF is expected between merging a Governor change and
  deploying it; see `docs/APPS-SCRIPT-DEPLOY.md`.
- **Recent executions** of any job:
  `gcloud run jobs executions list --job <job> --project sfdc24 --region us-central1 --limit 20`.

## Known behaviour that is not a fault

- **The gateway flaps.** Google sometimes answers a read with an HTTP 404
  page. Every cloud reader retries a read that did not come back as data:
  `board_waker.read_gateway` (#232, used by `waker-shadow`),
  `agent_waker.read_since` (the watcher and the wakers), and the probe (#224
  added its timeout retry). A read that never recovers is never taken for an
  empty board: `board_waker` reports UNKNOWN, while `read_since` and the probe
  exit with an error, so that execution shows as failed.
- **A slow gateway can kill a board-watcher tick** at its 100s timeout (three
  did between 21:22 and 21:31 UTC on 2026-09-24). The watcher's lease outlives
  the task timeout and its cursors are compare-and-swap, so a killed tick loses
  that minute and nothing else; the next tick picks up the same rows.
- **The board is large.** About 4,070 rows against a 2,000-row rollover
  target, and every read returns all matching rows, so reads slow down as it
  grows. The gateway has no create path; the rollover sheet needs Mr Salam.

## The broken-telephone path (Drive → intake → worker or laptop → the doc)

The owner's priority one (2026-10-10): he speaks on claude-mobile, it lands in
a Drive doc, the fleet is triggered, and receipts come back into the SAME doc.
Four merged pieces, every flag OFF in the image, each proven only when the
flag is on and the laptop is closed:

| piece | what | runs as | flag | deploy |
|---|---|---|---|---|
| `drive-intake` | a doc in the folder becomes ONE correlated task on `fleet:inbox:<worker>`, with REQUESTED/DISPATCHED (or QUEUED/BLOCKED) in the doc | private Cloud Run **service**, polled every 2 min by `drive-intake-poll` (OIDC) | `DRIVE_INTAKE_ENABLED` | `cloud/drive-intake/deploy.sh` |
| `claude-api-worker` | the always-on worker: when the laptop is off, ACCEPTED then SUCCEEDED with the answer, under the day cap `fleet:spend:<Toronto day>` | Cloud Run **job**, ticked every 5 min by `claude-api-worker-tick` | `CLAUDE_API_WORKER_ENABLED` | `cloud/claude-api-worker/deploy.sh` |
| the laptop lane | in the same job: the doorbell row for claude-code-cli (ids only), and its governed `accept` / `done` mirrored into the doc | same job | `CLAUDE_API_LANE_ENABLED` | same |
| the laptop's words | `python scripts/redis_inbox.py beat | accept | done`, each one governed board row | the laptop | - | - |

Routing is drive-intake's `pick_worker`: `fleet:alive:claude-code-cli` inside
300 s means the laptop takes it; otherwise the named api worker; a request that
needs laptop-only tools is QUEUED for the laptop by name; no worker at all is
BLOCKED - written in the doc, never silent.

**Order to turn on, and the proof for each.** The scripts' `setup`, `tokens`,
`build`, `deploy`, `schedule`, `readback` first, flags off, and the Drive share
of the folder (Viewer) and the two originals (Editor) with both service
accounts - a share nobody but the owner can do.

1. `drive-intake/deploy.sh on`, laptop OFF (no beat). A doc from the phone →
   within one tick REQUESTED (recipient claude-api) and DISPATCHED in the doc,
   `/healthz` poll ok. Nothing else runs yet: the task waits on
   `fleet:inbox:claude-api`.
2. `claude-api-worker/deploy.sh worker on` (it reads the job back and refuses
   while any cost input below is unset). The next tick: ACCEPTED, then
   SUCCEEDED with the answer under it, one call in the Anthropic console,
   `fleet:spend:<day>` moved by one reservation; the tick after: `idle`.
3. `claude-api-worker/deploy.sh lane on`, laptop ON with
   `python scripts/redis_inbox.py beat --every 240` running. A new doc routes
   to claude-code-cli; the next tick's line is `idle` (nothing landed on
   `fleet:inbox:claude-api`) and its lane record rang one row `CAW-WAKE-<task>`
   on the board, no doc text in it; `accept --task` and `done --task` become the
   ACCEPTED and SUCCEEDED lines by claude-code-cli. `taken` is a different
   outcome: some other writer already has that task's trail.

**Rollback** is the flag, never a redeploy: `deploy.sh off`, `worker off`,
`lane off`, `pilot off`. A doc already claimed keeps its claim (30 days) and its receipts;
turning a flag back on resumes from the doc's own trail.

**The cost inputs (#374), without which the worker calls nothing.** Five values
are read as fail-closed: unset or zero, the job deploys and runs, and every
task gets one QUEUED line a day ("no valid cap, reservation, price, exchange
rate, model limit or authorization deadline; nothing called"). They are the
owner's, set in the shell that runs `deploy`, never committed:
`CLAUDE_API_WORKER_USD_PER_MTOK_IN` and `_OUT` (the price of the chosen model
id, from the Claude pricing page), `CLAUDE_API_WORKER_MAX_INPUT_TOKENS` (the
input size that price holds for: the pricing tier's bound, not the context
window; a request larger than it is refused, and the reservation is the worst
case at that bound), `FLEET_CAD_PER_USD` (a ceiling rate; every cost is rounded
UP into CAD cents, so a high rate only over-reserves), and
`CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC` (his authorization deadline, `...Z`;
no call at or after it, checked on Redis's clock inside the reservation). The
cap stays `FLEET_SPEND_CAP_CENTS_CAD` (2500 unless he sets it). `deploy.sh cost`
prints the inputs as deployed. `worker on` refuses while any is missing, and
while any is a value the worker would read as nothing: a negative or
non-numeric price or rate, `100000.0` for the input bound (the worker reads a
whole number or zero), a deadline not written `YYYY-MM-DDTHH:MM:SSZ` or already
passed, a zero cap or reservation, an input bound under 4096 (the instructions
the worker sends with every request take about 1900 of it before a word of the
request). It also refuses when the job cannot be read
back at all.

**One intake at a time.** #374 also ships `PHONE_DOC_INTAKE_ENABLED`, a second
intake inside the worker job over the two originals only (owner request blocks
between `BEGIN OWNER REQUEST` and `END OWNER REQUEST`). With both it and
`DRIVE_INTAKE_ENABLED` on, one request becomes two tasks (the two intakes hash
different revisions of the same words), two calls and two answers in the doc,
and the pilot's inbox filter checkpoints the worker past the service's rows for
good. So `drive-intake/deploy.sh on` reads the job back and refuses while the
pilot is on, and `claude-api-worker/deploy.sh pilot on` refuses while the
service's flag is on. Turn one off before the other goes on. Either verb also
refuses when it cannot read the other side back: a denied or failed `describe`
is not "off". Only a resource gcloud says does not exist counts as off.

**The pilot answers one reviewed request (#386).** With the pilot flag on, the
worker reads `PHONE_DOC_PILOT_TARGET`: a JSON object of exactly five strings
(`source_doc_id`, `source_revision`, `request_id`, `task_id`, `recipient_id`),
handed over by the review that approved the request. Flag on and no usable
target, and the job stops on every tick (`pilot_target_invalid`) and answers
nothing. So the target is given to the verb, in the shell:

    PHONE_DOC_PILOT_TARGET='{"source_doc_id":"...","source_revision":"...","request_id":"...","task_id":"task-...","recipient_id":"claude-api"}' \
      bash cloud/claude-api-worker/deploy.sh pilot on
    bash cloud/claude-api-worker/deploy.sh worker on      # only now can a pass call, and it reads that request only
    bash cloud/claude-api-worker/deploy.sh pilot off      # the pilot, its target AND the worker, all off

`pilot on` judges the target as the worker will (a doc that is one of the two
originals, recipient `claude-api`, the id shapes) before anything is read or
sent, refuses while drive-intake's flag or the lane's flag is on, sends the
flag and the target in one update, and reads the job back: a job that does not
hold that flag and that target, to the character, is turned off again. The
target goes to gcloud in a `--flags-file` and never on a command line: on a
Windows laptop `gcloud.cmd` breaks on a quote inside an argument and `cmd.exe`
eats the carets of gcloud's `^@^` form (measured 2026-10-11 with
`gcloud meta test --arg-dict`, which calls no API). `pilot on` before `worker
on` is the order: with the worker on first, its passes read the whole inbox
until the pilot goes on. `pilot off` takes the worker off with it for the same
reason, in one update and with no read before it; `worker on` brings the
ordinary worker back. `lane on` refuses while the pilot is on or a target is
still set: the pilot and the lane together are `pilot_flags_invalid` on every
tick. A `deploy` leaves no pilot behind: flag off, target empty.

**Step 4: the trail on the call page (#376, site #309).** drive-intake answers
`GET /trail?doc=<id>` (poll token, then flag, then id) with the doc's receipt
lines, the answer under the terminal line, and a live line (laptop, api worker,
intake phase, another pass due); the studio controller proxies it at
`GET /v1/session/{id}/telephone?doc=` for the session that holds the call, and
`/studio/?live=1&doc=<id>` shows it beside the call, polled every ten seconds.
Deploy delta, after `drive-intake` is deployed: `cloud/drive-intake/deploy.sh studio`
grants the studio controller's runtime account `run.invoker` on drive-intake and
`secretAccessor` on `DRIVE_INTAKE_POLL_TOKEN`, and prints the two settings the
next studio release must carry (`STUDIO_TELEPHONE_URL=<drive-intake URL>`,
`STUDIO_TELEPHONE_TOKEN` from that secret). The studio release is this runbook's
procedure above, his to run; the panel is inert until that release carries the
URL. Nothing on the page is fetched without a live session, and the request
itself never leaves the doc.

**The proof: the acceptance gate.** The architecture doc's milestone 4 is one
authorized, real, laptop-offline round trip, confirmed by readback. Read the
trail from the terminal with `bash cloud/drive-intake/deploy.sh trail <doc id>`
(the service's own reading of the doc; needs the flag on) after each hop. The
verb exits non-zero and says "this is not a proof" on any HTTP refusal,
including the 404 it gets until #376 is deployed:

| hop | the line that proves it | written by |
|---|---|---|
| intake | `REQUESTED - task task-<16 hex> - ... - recipient claude-api` and `DISPATCHED ... on fleet:inbox:claude-api, which the worker polls; no wake rung` | drive-intake |
| pickup | `ACCEPTED ... attempt 1 - lease until <UTC> - model <id>` | claude-api |
| result | `SUCCEEDED ... model <id> - tokens in N out M - C cents CAD` with the `> ` answer lines under it (or FAILED / UNKNOWN that says why) | claude-api |
| ledger | `fleet:spend:<Toronto day>` moved by the settled cents; the Claude console shows one call | - |
| no duplicate | a second `trail` read shows the same task id and no second ACCEPTED; the next tick's line is `idle` | - |

With the laptop on and `lane on`, the same doc routes to claude-code-cli
instead: `CAW-WAKE-<task>` on the board, then ACCEPTED and SUCCEEDED by
claude-code-cli from `scripts/redis_inbox.py accept | done`. Both are the gate;
a passing suite, a green deploy or a heartbeat alone proves neither.

**What a health line means.** drive-intake `/healthz`: `ok` only when the
watch channel is live or the last poll is inside three ticks; `poll: unknown`
is "never polled", not "fine". The worker's pass is one JSON line: `idle`,
`ran` (with `considered`), `lane_only`, or `disabled`. `no_store` on that line
means Redis or Docs did not wire; `no_redis` and `no_docs` are printed before
the pass when the import itself failed. Read the job's env back.

**Cost lines.** The worker spends only under a reservation in the shared day
ledger; the lane spends nothing; a laptop beat is one request row plus one
RESULT row on the board, about 15 rows an hour at `--every 240`; the
bus-requests loop must be alive for any laptop word to land (#358).

## Changing an image safely

The pattern every rollout on 2026-09-24 used, with no failed swap:

1. Merge to `main` first. Build only from a merged `main` SHA, in a detached
   worktree, so the laptop checkout is never the source:
   `git worktree add --detach "$WT" "$SHA"`.
2. Build an **immutable, SHA-tagged** image; nothing is deployed yet:
   `gcloud builds submit --config cloud/<dir>/cloudbuild.yaml --substitutions=_IMAGE=$REG/<image>:${SHA:0:12} .`
   (see *Jobs* for which directory builds which job).
3. Record the image the job runs now. That tag is the rollback.
4. If the job is started by `board-watcher-2min`, pause that scheduler first,
   and resume it afterwards whatever happens.
5. Wait until no execution of an affected job is running (an empty
   `status.completionTime` means it is still running).
6. Swap: `gcloud run jobs update <job> --image $REG/<image>:<tag>`.
7. Prove it. Execute the job once
   (`gcloud run jobs execute <job> --wait`) or wait for its next scheduled run,
   and read that execution's log line. Then remove the worktree.

Rollback is step 6 with the recorded tag. Never delete an old image during a
soak.

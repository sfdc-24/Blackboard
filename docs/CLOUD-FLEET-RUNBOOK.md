# Cloud fleet runbook: what runs where, and how to change it safely

One page for anyone about to touch the unattended half of the fleet. The
evidence for how it got here is in `docs/CLOUD-CREDENTIAL-CONTRACT.md` and
`docs/OPENAI-CLOUD-MIGRATION.md`; this page is the current state and the
procedure.

**Read live, then trust this page.** Everything in the tables below was read
from GCP on 2026-09-24 between 22:00 and 22:30 UTC. Images move with every rollout, so
run the three commands in *Read the live state* before acting on a row here.

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
| `waker-shadow` | runs the laptop waker's decision logic read-only, to prove the cloud can replace it | `waker-shadow-hourly` (`45 * * * *`) | `waker-shadow:675f6fe166a9` | `waker-shadow:v2` | 1800s |
| `studio-voice-sweep` | backstop that hangs up any studio voice call the controller missed, so none keeps billing (`cloud/studio-controller/ADR-001-VOICE-SWEEP-BACKSTOP.md`) | `studio-voice-sweep-every-minute` (`*/5 * * * *`, every five minutes despite the name) | `studio-controller@sha256:1736bc4f…` (the reviewed studio image, pinned by digest) | the previous digest | 30s |

Images are in `us-central1-docker.pkg.dev/sfdc24/cloud-run-source-deploy/`,
tagged with the first 7 or 12 characters of the `main` commit they were built
from. Each is built from `cloud/<dir>/cloudbuild.yaml`, and the directory is not
always the job name: `gemini-waker` and `claude-api-waker` both build from
`cloud/agent-waker/`, and `studio-voice-sweep` runs the image built from
`cloud/studio-controller/`. The rest build from the directory of their own name.

## The studio controller service

`sfdc24-studio-controller` is a Cloud Run **service**, not a job.

| revision | traffic | tag |
|---|---|---|
| `sfdc24-studio-controller-p3-claude-6fd0dc4` | **100%** | `claude-text` |
| `sfdc24-studio-controller-lead-test-bb68838-r3` | 0% | `lead-canary` |
| `sfdc24-studio-controller-voice-cap-b04762d` | 0% | `voice-canary` |

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

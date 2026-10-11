#!/usr/bin/env bash
# claude-api-worker as a Cloud Run JOB with VPC egress, ticked by Cloud Scheduler.
#
# WHAT THIS IS
# Steps 2 and 3 of the broken-telephone fix in one job: the always-on worker that answers a Drive
# request when the laptop is off (cloud/claude-api-worker/main.py, CLAUDE_API_WORKER_ENABLED), and the
# laptop lane that rings, records and finishes for claude-code-cli when it is on (lane.py,
# CLAUDE_API_LANE_ENABLED). Both flags are OFF in the image. One pass per tick, at most one model
# call, then exit.
#
# HIS DECISIONS, AS ENV
#   the spend cap holds       FLEET_SPEND_CAP_CENTS_CAD (2500 = the CAD 25/day shared cap); unset is zero
#   per-call reservation      CLAUDE_API_WORKER_RESERVE_CENTS_CAD (5 covers a Haiku-class call)
#   the model                 CLAUDE_API_WORKER_MODEL (the id is yours; the cheapest capable one)
#   heartbeat window          DRIVE_INTAKE_HEARTBEAT_SECONDS (300)
#
# WHAT IS THE OWNER'S
#   1. The same Drive share as drive-intake: the two originals (Editor) and the folder (Viewer) with
#      this job's service account email.
#   2. REDIS_HOST, the model id, and the two flags.
#
#   bash cloud/claude-api-worker/deploy.sh setup     # service account, the secret grants, the invoker
#   bash cloud/claude-api-worker/deploy.sh build     # build the image from the repo root
#   REDIS_HOST=<addr> CLAUDE_API_WORKER_MODEL=<id> bash cloud/claude-api-worker/deploy.sh deploy   # the job, flags OFF
#   bash cloud/claude-api-worker/deploy.sh schedule  # the tick, every 5 minutes
#   bash cloud/claude-api-worker/deploy.sh run       # execute once and wait (one pass)
#   bash cloud/claude-api-worker/deploy.sh readback  # what is actually deployed, printed
#   bash cloud/claude-api-worker/deploy.sh worker on|off   lane on|off      # the flags, nothing else

set -euo pipefail

PROJECT="${PROJECT:-sfdc24}"
PROJECT_NUMBER="${PROJECT_NUMBER:-96522051727}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-claude-api-worker}"
NAME="claude-api-worker"
SA_NAME="${SA_NAME:-claude-api-worker}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
INVOKER_NAME="${INVOKER_NAME:-claude-api-worker-ticker}"
INVOKER="${INVOKER_NAME}@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"
TAG="${TAG:-v1}"
IMAGE="${REPO}/${NAME}:${TAG}"
GCLOUD="${GCLOUD:-gcloud}"
SCHED_JOB="${SCHED_JOB:-claude-api-worker-tick}"
SCHEDULE="${SCHEDULE:-*/5 * * * *}"
# v2, not v1 namespaces. roles/run.invoker grants run.jobs.run, which is what v2 :run needs.
# The v1 /apis/run.googleapis.com/v1/namespaces/.../jobs/NAME:run form also reads the job, which
# run.invoker does not grant, so v1 forces the role up to run.developer for no benefit.
# scripts/gcp_scheduler_bootstrap.py, and docs/CLOUD-CREDENTIAL-CONTRACT.md.
RUN_URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"

# The five, and only five, secrets this job may read. BUS_URL/BUS_SECRET are for the lane's one
# doorbell row through scripts/append.py; without them the lane reports no_board and still mirrors.
SECRETS=(ANTHROPIC_API_KEY REDIS_AUTH_STRING REDIS_CA_CERT BUS_URL BUS_SECRET)

REDIS_HOST="${REDIS_HOST:-}"
REDIS_PORT="${REDIS_PORT:-6378}"
CA_PATH="/secrets/ca/redis-ca.pem"
AUTH_PATH="/secrets/auth/redis-auth"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"
EGRESS="private-ranges-only"

MODEL="${CLAUDE_API_WORKER_MODEL:-}"
MAX_TOKENS="${CLAUDE_API_WORKER_MAX_TOKENS:-1500}"
CAP_CENTS="${FLEET_SPEND_CAP_CENTS_CAD:-2500}"
RESERVE_CENTS="${CLAUDE_API_WORKER_RESERVE_CENTS_CAD:-5}"
REASSIGN_SECONDS="${CLAUDE_API_WORKER_REASSIGN_SECONDS:-1800}"
MAX_ATTEMPTS="${CLAUDE_API_WORKER_MAX_ATTEMPTS:-1}"
FOLDER_ID="${DRIVE_INTAKE_FOLDER_ID:-1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP}"
HEARTBEAT_SECONDS="${DRIVE_INTAKE_HEARTBEAT_SECONDS:-300}"

setup() {
  # The APIs the job calls (Docs, Drive) and the ones this script uses; enabling is idempotent.
  "$GCLOUD" services enable docs.googleapis.com drive.googleapis.com run.googleapis.com \
    cloudscheduler.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com \
    --project "$PROJECT"
  "$GCLOUD" iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
    "$GCLOUD" iam service-accounts create "$SA_NAME" --project "$PROJECT" \
      --display-name "claude-api worker: one bounded answer per tick, and the laptop lane"
  "$GCLOUD" iam service-accounts describe "$INVOKER" --project "$PROJECT" >/dev/null 2>&1 ||
    "$GCLOUD" iam service-accounts create "$INVOKER_NAME" --project "$PROJECT" \
      --display-name "claude-api worker ticker: Cloud Scheduler's identity to run the job"
  for secret in "${SECRETS[@]}"; do
    "$GCLOUD" secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
      --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor \
      --condition None >/dev/null
    echo "granted secretAccessor on ${secret} to ${SA}"
  done
  # THE STEP WHOSE ABSENCE IS INVISIBLE. Without TokenCreator on the invoker, the ticker sits at
  # status code -1 with no lastAttemptTime and no logs: it never attempts. run.invoker on the job
  # is a different grant and does not mint the OAuth token. Same binding as
  # scripts/gcp_scheduler_bootstrap.py, proven 2026-09-23.
  "$GCLOUD" iam service-accounts add-iam-policy-binding "$INVOKER" --project "$PROJECT" \
    --member "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com" \
    --role roles/iam.serviceAccountTokenCreator >/dev/null
  echo "NOW, IN DRIVE (yours): share folder ${FOLDER_ID} (Viewer) and the two originals (Editor) with ${SA}"
}

build() {
  "$GCLOUD" builds submit --project "$PROJECT" --config cloud/claude-api-worker/cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE}" .
}

deploy() {
  : "${REDIS_HOST:?set REDIS_HOST to the Memorystore private address; it is not committed here}"
  : "${MODEL:?set CLAUDE_API_WORKER_MODEL to the model id (the cheapest capable one); it is not committed here}"
  local verb=create
  "$GCLOUD" run jobs describe "$JOB" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1 && verb=update
  "$GCLOUD" run jobs "$verb" "$JOB" --project "$PROJECT" --region "$REGION" \
    --image "$IMAGE" --service-account "$SA" \
    --set-secrets "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,BUS_URL=BUS_URL:latest,BUS_SECRET=BUS_SECRET:latest,${CA_PATH}=REDIS_CA_CERT:latest,${AUTH_PATH}=REDIS_AUTH_STRING:latest" \
    --set-env-vars "CLAUDE_API_WORKER_ENABLED=off,CLAUDE_API_LANE_ENABLED=off,REDIS_CONNECT=true,REDIS_HOST=${REDIS_HOST},REDIS_PORT=${REDIS_PORT},CLAUDE_API_WORKER_MODEL=${MODEL},CLAUDE_API_WORKER_MAX_TOKENS=${MAX_TOKENS},FLEET_SPEND_CAP_CENTS_CAD=${CAP_CENTS},CLAUDE_API_WORKER_RESERVE_CENTS_CAD=${RESERVE_CENTS},CLAUDE_API_WORKER_REASSIGN_SECONDS=${REASSIGN_SECONDS},CLAUDE_API_WORKER_MAX_ATTEMPTS=${MAX_ATTEMPTS},DRIVE_INTAKE_FOLDER_ID=${FOLDER_ID},DRIVE_INTAKE_HEARTBEAT_SECONDS=${HEARTBEAT_SECONDS}" \
    --network "$NETWORK" --subnet "$SUBNET" --vpc-egress "$EGRESS" \
    --max-retries 0 --task-timeout 10m --cpu 1 --memory 512Mi
  "$GCLOUD" run jobs add-iam-policy-binding "$JOB" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${INVOKER}" --role roles/run.invoker >/dev/null
  echo "job ${JOB} ${verb}d in ${REGION}, both flags off; ${INVOKER} may run it"
}

schedule() {
  local verb=create
  "$GCLOUD" scheduler jobs describe "$SCHED_JOB" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1 && verb=update
  "$GCLOUD" scheduler jobs "$verb" http "$SCHED_JOB" --project "$PROJECT" --location "$REGION" \
    --schedule "$SCHEDULE" --http-method POST --uri "$RUN_URI" \
    --oauth-service-account-email "$INVOKER" \
    --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform" \
    --attempt-deadline 60s
  echo "scheduler ${SCHED_JOB} ${verb}d: ${SCHEDULE} -> run ${JOB} as ${INVOKER}"
}

run_once() {
  "$GCLOUD" run jobs execute "$JOB" --project "$PROJECT" --region "$REGION" --wait
}

readback() {
  # Check: the service account is claude-api-worker, not default; all five secrets present (three env,
  # two mounts); no env value rewritten into a C:/... path; egress private-ranges-only; both flags off
  # until you turn them on; the model id is the one you meant.
  # The v2 job resource, the same projection cloud/bus-reconciler/deploy.sh reads back.
  # spec.template.spec.template.spec is the Knative shape and prints empty here.
  "$GCLOUD" run jobs describe "$JOB" --project "$PROJECT" --region "$REGION" \
    --format 'yaml(template.template.serviceAccount,template.template.containers[0].env,template.template.containers[0].image,template.template.vpcAccess,template.template.maxRetries)'
  "$GCLOUD" scheduler jobs describe "$SCHED_JOB" --project "$PROJECT" --location "$REGION" \
    --format 'yaml(schedule,state,lastAttemptTime,status,httpTarget.uri)' || true
  "$GCLOUD" run jobs executions list --job "$JOB" --project "$PROJECT" --region "$REGION" --limit 5 || true
  echo "a scheduler with no lastAttemptTime has never attempted; the job's log prints one JSON line per pass"
}

flag() {
  "$GCLOUD" run jobs update "$JOB" --project "$PROJECT" --region "$REGION" --update-env-vars "$1=$2"
  echo "$1=$2 on ${JOB}"
}

case "${1:-}" in
  setup) setup ;;
  build) build ;;
  deploy) deploy ;;
  schedule) schedule ;;
  run) run_once ;;
  readback) readback ;;
  worker) case "${2:-}" in on|off) flag CLAUDE_API_WORKER_ENABLED "$2" ;; *) echo "worker on|off" >&2; exit 2 ;; esac ;;
  lane) case "${2:-}" in on|off) flag CLAUDE_API_LANE_ENABLED "$2" ;; *) echo "lane on|off" >&2; exit 2 ;; esac ;;
  *) sed -n '2,28p' "$0"; exit 2 ;;
esac

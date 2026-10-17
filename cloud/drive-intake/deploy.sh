#!/usr/bin/env bash
# drive-intake as a PRIVATE Cloud Run service with VPC egress, polled by Cloud Scheduler.
#
# WHAT THIS IS
# Step 1 of the broken-telephone fix (Converspan Channel Architecture v0.1): a doc in the owner's
# folder becomes ONE correlated task on fleet:inbox:<worker>, with receipts back in the doc. The
# code is cloud/drive-intake/main.py, flag OFF in the image. This script is every command that
# stands between that image and the owner's laptop-closed acceptance run, in order, each with its
# read-back, so the owner fills three values and runs it instead of assembling four PR bodies.
#
# WHAT IS THE OWNER'S AND NOT THIS SCRIPT'S
#   1. Share the folder (Viewer) and the two originals (Editor) in Drive with the service account
#      email this script creates - Aya locked both originals owner-only on 2026-10-10, and a share
#      is a Drive-side act no gcloud command performs.
#   2. The Memorystore private address, passed as REDIS_HOST (this repository is PUBLIC).
#   3. The flag: `deploy.sh on` is the one command that makes it read the folder.
#
# LEAST PRIVILEGE, PER SECRET
# The service gets its own service account with secretAccessor on exactly four secrets. The
# scheduler calls it with an OIDC token as its own invoker account; the service is never public.
#
#   bash cloud/drive-intake/deploy.sh setup     # service account, the four secret grants, the invoker
#   bash cloud/drive-intake/deploy.sh tokens    # mint the two tokens into Secret Manager (never printed)
#   bash cloud/drive-intake/deploy.sh build     # build the image from the repo root
#   REDIS_HOST=<private address> bash cloud/drive-intake/deploy.sh deploy   # the service, flag OFF
#   bash cloud/drive-intake/deploy.sh schedule  # the poll tick, every 2 minutes, OIDC
#   bash cloud/drive-intake/deploy.sh readback  # what is actually deployed, printed; /healthz
#   bash cloud/drive-intake/deploy.sh on | off  # the flag, and nothing else
#   bash cloud/drive-intake/deploy.sh watch     # PUSH: the Drive watch, its daily renewal, the first channel
#   bash cloud/drive-intake/deploy.sh unwatch   # back to the poll alone

set -euo pipefail

PROJECT="${PROJECT:-sfdc24}"
PROJECT_NUMBER="${PROJECT_NUMBER:-96522051727}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-drive-intake}"
NAME="drive-intake"
SA_NAME="${SA_NAME:-drive-intake}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
INVOKER_NAME="${INVOKER_NAME:-drive-intake-poller}"
INVOKER="${INVOKER_NAME}@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"
TAG="${TAG:-v1}"
IMAGE="${REPO}/${NAME}:${TAG}"
GCLOUD="${GCLOUD:-gcloud}"
SCHED_JOB="${SCHED_JOB:-drive-intake-poll}"
SCHEDULE="${SCHEDULE:-*/2 * * * *}"
RENEW_JOB="${RENEW_JOB:-drive-intake-watch-renew}"
RENEW_SCHEDULE="${RENEW_SCHEDULE:-17 9 * * *}"

# The four secrets this service may read, and the ring's HMAC once inbox-wake is wired (Gate 3 Step 3).
SECRETS=(REDIS_AUTH_STRING REDIS_CA_CERT DRIVE_INTAKE_POLL_TOKEN DRIVE_INTAKE_CHANNEL_TOKEN INBOX_WAKE_SECRET)
# The ring into inbox-wake (cloud/inbox-wake/deploy.sh). Set: drive-intake rings it, signed with
# INBOX_WAKE_SECRET and carrying its own ID token. Unset: the polled ring below, as before.
WAKE_URL="${INBOX_WAKE_URL:-}"

# Passed in, never written here: the private address is topology and this repository is public.
REDIS_HOST="${REDIS_HOST:-}"
REDIS_PORT="${REDIS_PORT:-6378}"
# Each mounted secret needs its own directory (Cloud Run mounts the directory). Same layout as
# bus-requests, so scripts/redis_dual.py finds both without being told where.
CA_PATH="/secrets/ca/redis-ca.pem"
AUTH_PATH="/secrets/auth/redis-auth"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"
# private-ranges-only, NEVER all-traffic: Docs and Drive are on the public internet.
EGRESS="private-ranges-only"

FOLDER_ID="${DRIVE_INTAKE_FOLDER_ID:-1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP}"
API_WORKER="${DRIVE_INTAKE_API_WORKER:-claude-api}"
# Gate 3 Step 3: "recipient claude-api only". A request addressed to anyone else is refused, not rerouted.
RECIPIENTS="${DRIVE_INTAKE_RECIPIENTS:-claude-api}"
# Gate 3 Step 3: no backfill. The first poll lists nothing and sets the floor; only later docs are read.
FIRST_POLL="${DRIVE_INTAKE_FIRST_POLL:-baseline}"
HEARTBEAT_SECONDS="${DRIVE_INTAKE_HEARTBEAT_SECONDS:-300}"
POLL_SECONDS="${DRIVE_INTAKE_POLL_SECONDS:-120}"
# The ring. "polled": the worker polls its inbox and the laptop lane rings the laptop itself, so the
# put IS the hand-off and DISPATCHED is reachable without #359 (Cursor on 0bee083: a deploy with no
# wake variables could never reach it). Set INBOX_WAKE_URL/INBOX_WAKE_SECRET instead once #359 lands.
RING="${DRIVE_INTAKE_RING:-polled}"

# One grant, safe in either order. setup runs before tokens in the printed order, so the two
# tokens do not exist yet when setup walks the list; tokens creates them afterwards. A grant that
# lives only in setup is skipped, and deploy then cannot mount the secret.
grant_secret() {
  local secret="$1"
  if ! "$GCLOUD" secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1; then
    echo "secret ${secret} does not exist yet; run: deploy.sh tokens (the two tokens) or create it" >&2
    return 0
  fi
  if ! "$GCLOUD" iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1; then
    echo "service account ${SA} does not exist yet; run deploy.sh setup to grant ${secret}" >&2
    return 0
  fi
  "$GCLOUD" secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
    --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor \
    --condition None >/dev/null
  echo "granted secretAccessor on ${secret} to ${SA}"
}

setup() {
  # The APIs the service calls (Docs, Drive) and the ones this script uses; enabling is idempotent.
  "$GCLOUD" services enable docs.googleapis.com drive.googleapis.com run.googleapis.com \
    cloudscheduler.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com \
    --project "$PROJECT"
  "$GCLOUD" iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
    "$GCLOUD" iam service-accounts create "$SA_NAME" --project "$PROJECT" \
      --display-name "Drive intake: read the folder, append receipts, write fleet: keys"
  "$GCLOUD" iam service-accounts describe "$INVOKER" --project "$PROJECT" >/dev/null 2>&1 ||
    "$GCLOUD" iam service-accounts create "$INVOKER_NAME" --project "$PROJECT" \
      --display-name "Drive intake poller: Cloud Scheduler's identity for POST /poll"
  for secret in "${SECRETS[@]}"; do
    grant_secret "$secret"
  done
  # THE STEP WHOSE ABSENCE IS INVISIBLE. Without TokenCreator on the invoker, the poller sits at
  # status code -1 with no lastAttemptTime and no logs: it never attempts. The same binding
  # scripts/gcp_scheduler_bootstrap.py grants, proven 2026-09-23. The Cloud Scheduler service
  # agent is the principal that mints the OIDC token; run.invoker on the service is not this.
  "$GCLOUD" iam service-accounts add-iam-policy-binding "$INVOKER" --project "$PROJECT" \
    --member "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com" \
    --role roles/iam.serviceAccountTokenCreator >/dev/null
  echo "NOW, IN DRIVE (yours): share folder ${FOLDER_ID} (Viewer) and the two originals (Editor) with ${SA}"
}

tokens() {
  # Minted here, stored in Secret Manager, never printed and never written to disk.
  for secret in DRIVE_INTAKE_POLL_TOKEN DRIVE_INTAKE_CHANNEL_TOKEN; do
    if "$GCLOUD" secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1; then
      echo "${secret} exists; not replaced (add a version by hand if you mean to rotate it)"
    else
      python -c "import secrets,sys; sys.stdout.write(secrets.token_hex(32))" |
        "$GCLOUD" secrets create "$secret" --project "$PROJECT" --replication-policy automatic --data-file=-
      echo "${secret} created with one version"
    fi
    # setup's walk cannot grant a secret that does not exist yet, and the printed order is
    # setup then tokens. Grant here too, including when the secret already existed.
    grant_secret "$secret"
  done
}

build() {
  "$GCLOUD" builds submit --project "$PROJECT" --config cloud/drive-intake/cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE}" .
}

deploy() {
  : "${REDIS_HOST:?set REDIS_HOST to the Memorystore private address; it is not committed here}"
  local wake_secret="" wake_env=""
  if [ -n "$WAKE_URL" ]; then
    wake_secret=",INBOX_WAKE_SECRET=INBOX_WAKE_SECRET:latest"
    wake_env=",INBOX_WAKE_URL=${WAKE_URL}"
  fi
  "$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --image "$IMAGE" --service-account "$SA" \
    --no-allow-unauthenticated --min-instances 0 --max-instances 2 \
    --set-secrets "${CA_PATH}=REDIS_CA_CERT:latest,${AUTH_PATH}=REDIS_AUTH_STRING:latest,DRIVE_INTAKE_POLL_TOKEN=DRIVE_INTAKE_POLL_TOKEN:latest,DRIVE_INTAKE_CHANNEL_TOKEN=DRIVE_INTAKE_CHANNEL_TOKEN:latest${wake_secret}" \
    --set-env-vars "DRIVE_INTAKE_ENABLED=off,REDIS_CONNECT=true,REDIS_HOST=${REDIS_HOST},REDIS_PORT=${REDIS_PORT},DRIVE_INTAKE_FOLDER_ID=${FOLDER_ID},DRIVE_INTAKE_API_WORKER=${API_WORKER},DRIVE_INTAKE_RECIPIENTS=${RECIPIENTS},DRIVE_INTAKE_FIRST_POLL=${FIRST_POLL},DRIVE_INTAKE_HEARTBEAT_SECONDS=${HEARTBEAT_SECONDS},DRIVE_INTAKE_POLL_SECONDS=${POLL_SECONDS},DRIVE_INTAKE_RING=${RING}${wake_env}" \
    --network "$NETWORK" --subnet "$SUBNET" --vpc-egress "$EGRESS" \
    --cpu 1 --memory 512Mi --timeout 60
  "$GCLOUD" run services add-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${INVOKER}" --role roles/run.invoker >/dev/null
  echo "service ${SERVICE} deployed PRIVATE in ${REGION}, flag off; ${INVOKER} may invoke it"
}

service_url() {
  "$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)'
}

schedule() {
  local url token verb=create header_flag
  url="$(service_url)"
  # The poll token rides in its own header: Authorization carries the OIDC token Cloud Run checks.
  # Read from Secret Manager at this moment, handed to the scheduler, never echoed.
  # Audience is the service URL, not /poll: Cloud Run checks the token against the service.
  token="$("$GCLOUD" secrets versions access latest --secret DRIVE_INTAKE_POLL_TOKEN --project "$PROJECT")"
  "$GCLOUD" scheduler jobs describe "$SCHED_JOB" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1 && verb=update
  # create takes --headers. update rejects it; the flag there is --update-headers. One flag for
  # both means the second schedule, the idempotent one, fails and the header stays stale.
  header_flag="--headers"
  if [ "$verb" = update ]; then
    header_flag="--update-headers"
  fi
  "$GCLOUD" scheduler jobs "$verb" http "$SCHED_JOB" --project "$PROJECT" --location "$REGION" \
    --schedule "$SCHEDULE" --http-method POST --uri "${url}/poll" \
    --oidc-service-account-email "$INVOKER" --oidc-token-audience "$url" \
    "$header_flag" "X-Drive-Intake-Poll-Token=${token}" \
    --attempt-deadline 60s
  echo "scheduler ${SCHED_JOB} ${verb}d: ${SCHEDULE} -> POST ${url}/poll as ${INVOKER}"
}

readback() {
  # READ BACK before trusting any of it. Check: the service account is drive-intake, not default;
  # ingress is internal-and-cloud-load-balancing or the service is not public; both mounts and both
  # token secrets are present; no env value rewritten into a C:/... path; egress private-ranges-only.
  "$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format 'yaml(spec.template.spec.serviceAccountName,spec.template.spec.containers[0].env,spec.template.spec.containers[0].image,spec.template.metadata.annotations)'
  "$GCLOUD" scheduler jobs describe "$SCHED_JOB" --project "$PROJECT" --location "$REGION" \
    --format 'yaml(schedule,state,lastAttemptTime,status,httpTarget.uri,httpTarget.oidcToken.serviceAccountEmail)' || true
  local url
  url="$(service_url)"
  echo "healthz:"
  # A user account's identity token (no --audiences: that flag is for service accounts) is accepted
  # by a private Cloud Run service when the account holds run.invoker, which the project owner does.
  curl -s -H "Authorization: Bearer $("$GCLOUD" auth print-identity-token)" "${url}/healthz"; echo
  echo "a scheduler with no lastAttemptTime has never attempted; poll reads unknown until the first tick"
}

flag() {
  "$GCLOUD" run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --update-env-vars "DRIVE_INTAKE_ENABLED=$1"
  echo "DRIVE_INTAKE_ENABLED=$1 on ${SERVICE}"
}

# PUSH (the fast-handoff POC, drive_watch.py). Drive's push notification carries no OIDC token, so the
# service must answer an unauthenticated caller: allUsers gets run.invoker. That does not open the work:
# /hooks/drive still needs the channel token AND a channel on record, /poll and /watch/renew the poll
# token, and each answers 401 without. /healthz carries no doc and no secret. The poll keeps running as
# the reconcile path; unwatch takes the service back to it alone.
watch() {
  local url token verb=create header_flag
  url="$(service_url)"
  "$GCLOUD" run services add-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --member allUsers --role roles/run.invoker >/dev/null
  "$GCLOUD" run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --update-env-vars "DRIVE_INTAKE_WATCH=on,DRIVE_INTAKE_PUSH_URL=${url}/hooks/drive"
  token="$("$GCLOUD" secrets versions access latest --secret DRIVE_INTAKE_POLL_TOKEN --project "$PROJECT")"
  "$GCLOUD" scheduler jobs describe "$RENEW_JOB" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1 && verb=update
  header_flag="--headers"
  if [ "$verb" = update ]; then
    header_flag="--update-headers"
  fi
  "$GCLOUD" scheduler jobs "$verb" http "$RENEW_JOB" --project "$PROJECT" --location "$REGION" \
    --schedule "$RENEW_SCHEDULE" --http-method POST --uri "${url}/watch/renew" \
    --oidc-service-account-email "$INVOKER" --oidc-token-audience "$url" \
    "$header_flag" "X-Drive-Intake-Poll-Token=${token}" --attempt-deadline 60s >/dev/null
  echo "scheduler ${RENEW_JOB} ${verb}d: ${RENEW_SCHEDULE} -> POST ${url}/watch/renew"
  # The first channel now, not at tomorrow's tick. The readout is Drive's own answer.
  echo "first renewal:"
  curl -s -X POST -H "X-Drive-Intake-Poll-Token: ${token}" "${url}/watch/renew"; echo
  token=""
  echo "healthz:"
  curl -s "${url}/healthz"; echo
}

unwatch() {
  "$GCLOUD" run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --update-env-vars "DRIVE_INTAKE_WATCH=off"
  "$GCLOUD" scheduler jobs pause "$RENEW_JOB" --project "$PROJECT" --location "$REGION" || true
  "$GCLOUD" run services remove-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --member allUsers --role roles/run.invoker >/dev/null || true
  echo "watch off: the poll alone; Drive's channel expires on its own within seven days"
}

case "${1:-}" in
  setup) setup ;;
  tokens) tokens ;;
  build) build ;;
  deploy) deploy ;;
  schedule) schedule ;;
  readback) readback ;;
  on) flag on ;;
  off) flag off ;;
  watch) watch ;;
  unwatch) unwatch ;;
  *) sed -n '2,30p' "$0"; exit 2 ;;
esac

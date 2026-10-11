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
#   bash cloud/drive-intake/deploy.sh on | off  # the flag, and nothing else (on refuses while the worker's pilot intake is on)
#   bash cloud/drive-intake/deploy.sh studio    # step 4, the grants half: the studio controller may call GET /trail
#   bash cloud/drive-intake/deploy.sh trail <doc id>   # the doc's receipt trail as the service reads it: the proof

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
# The python that RUNS: python3 where it is one, python where only that name works (a Windows Store
# stub answers to python3 and does nothing). Read-back verbs parse gcloud's JSON with it.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3 python; do
    if "$candidate" -c pass >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
  done
fi
PYTHON="${PYTHON:-python}"
SCHED_JOB="${SCHED_JOB:-drive-intake-poll}"
SCHEDULE="${SCHEDULE:-*/2 * * * *}"

# The four, and only four, secrets this service may read.
SECRETS=(REDIS_AUTH_STRING REDIS_CA_CERT DRIVE_INTAKE_POLL_TOKEN DRIVE_INTAKE_CHANNEL_TOKEN)

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
HEARTBEAT_SECONDS="${DRIVE_INTAKE_HEARTBEAT_SECONDS:-300}"
POLL_SECONDS="${DRIVE_INTAKE_POLL_SECONDS:-120}"
# The ring. "polled": the worker polls its inbox and the laptop lane rings the laptop itself, so the
# put IS the hand-off and DISPATCHED is reachable without #359 (Cursor on 0bee083: a deploy with no
# wake variables could never reach it). Set INBOX_WAKE_URL/INBOX_WAKE_SECRET instead once #359 lands.
RING="${DRIVE_INTAKE_RING:-polled}"
# Step 4 (PR 376): the studio controller reads GET /trail as its own identity plus the poll token.
STUDIO_SERVICE="${STUDIO_SERVICE:-sfdc24-studio-controller}"
# The other intake (#374's pilot, inside the worker job). `on` refuses while it is on.
WORKER_JOB="${WORKER_JOB:-claude-api-worker}"
# A Drive file id, the same shape main.py's FILE_ID_OK accepts; anything else never reaches curl.
DOC_ID_OK='^[A-Za-z0-9_-]{10,128}$'
# By name, as gcloud is. A curl found through PATH is whichever one the shell puts first, and Git
# for Windows' bash puts its own in front of everything it is handed: a test that gives this script
# a stand-in must be able to name it, or the real one runs and the request leaves the box.
CURL="${CURL:-curl}"

# The env of a deployed resource, one NAME=VALUE per line, read back from the job or the service
# that RUNS, never from this shell. Python parses the JSON (jobs are the v2 shape, services the
# Knative one): no awk over yaml, no value split on a comma.
#
# ABSENT IS NOT THE SAME AS UNREADABLE. Only gcloud saying the resource is not there prints nothing
# and returns 0, which a caller may read as "off". A denied describe, a transient failure, an answer
# that is not JSON or a python that will not start returns non-zero, and every caller that turns a
# flag on refuses on it: an interlock that cannot read the other side has not checked it.
ENV_LINES_PY='
import json, sys
try:
    doc = json.load(sys.stdin)
    if not isinstance(doc, dict):
        raise ValueError("not an object")
except ValueError:
    sys.exit(3)
template = doc.get("template", {}).get("template") or doc.get("spec", {}).get("template", {}).get("spec", {})
for container in template.get("containers") or []:
    for item in container.get("env") or []:
        if "value" in item:
            print("%s=%s" % (item.get("name", ""), item.get("value", "")))
'
# env_lines jobs|services NAME
env_lines() {
  local json err status=0
  err="$(mktemp)"
  json="$("$GCLOUD" run "$1" describe "$2" --project "$PROJECT" --region "$REGION" --format json 2>"$err")" || status=$?
  if [ "$status" -ne 0 ]; then
    if grep -Eqi "cannot find|NOT_FOUND|could not be found" "$err" && ! grep -qi "denied" "$err"; then
      rm -f "$err"
      return 0
    fi
    cat "$err" >&2
    rm -f "$err"
    echo "could not read $1 $2 back (gcloud exit ${status}); nothing is assumed about it" >&2
    return 1
  fi
  rm -f "$err"
  # tr: a Windows python writes CRLF into the pipe, and grep -x NAME=on does not match a line that
  # still ends in a carriage return, so the interlock read "on" as "off".
  printf "%s" "$json" | "$PYTHON" -c "$ENV_LINES_PY" | tr -d "\r"
}

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
      "$PYTHON" -c "import secrets,sys; sys.stdout.write(secrets.token_hex(32))" |
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
  "$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --image "$IMAGE" --service-account "$SA" \
    --no-allow-unauthenticated --min-instances 0 --max-instances 2 \
    --set-secrets "${CA_PATH}=REDIS_CA_CERT:latest,${AUTH_PATH}=REDIS_AUTH_STRING:latest,DRIVE_INTAKE_POLL_TOKEN=DRIVE_INTAKE_POLL_TOKEN:latest,DRIVE_INTAKE_CHANNEL_TOKEN=DRIVE_INTAKE_CHANNEL_TOKEN:latest" \
    --set-env-vars "DRIVE_INTAKE_ENABLED=off,REDIS_CONNECT=true,REDIS_HOST=${REDIS_HOST},REDIS_PORT=${REDIS_PORT},DRIVE_INTAKE_FOLDER_ID=${FOLDER_ID},DRIVE_INTAKE_API_WORKER=${API_WORKER},DRIVE_INTAKE_HEARTBEAT_SECONDS=${HEARTBEAT_SECONDS},DRIVE_INTAKE_POLL_SECONDS=${POLL_SECONDS},DRIVE_INTAKE_RING=${RING}" \
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
  "$CURL" -s -H "Authorization: Bearer $("$GCLOUD" auth print-identity-token)" "${url}/healthz"; echo
  echo "a scheduler with no lastAttemptTime has never attempted; poll reads unknown until the first tick"
}

flag() {
  # One intake at a time: with the worker's pilot intake on as well, one request becomes two tasks,
  # two calls and two answers, and the pilot's inbox filter checkpoints the worker past this
  # service's rows for good. The job is read back, not assumed.
  # A job that cannot be read back is not a job whose pilot is off: refuse on that too.
  local other
  if [ "$1" = on ]; then
    if ! other="$(env_lines jobs "$WORKER_JOB")"; then
      echo "refused: ${WORKER_JOB} could not be read back, so its pilot intake flag is unknown; one intake at a time" >&2
      exit 2
    fi
    if grep -qx "PHONE_DOC_INTAKE_ENABLED=on" <<<"$other"; then
      echo "refused: ${WORKER_JOB} has PHONE_DOC_INTAKE_ENABLED=on; one intake at a time (cloud/claude-api-worker/deploy.sh pilot off first)" >&2
      exit 2
    fi
  fi
  "$GCLOUD" run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --update-env-vars "DRIVE_INTAKE_ENABLED=$1"
  echo "DRIVE_INTAKE_ENABLED=$1 on ${SERVICE}"
}

studio() {
  # Step 4's deploy delta, the grants half. The studio controller calls GET /trail?doc= as ITS OWN
  # identity (an OIDC token for this service's URL) and sends the poll token in its header, so its
  # runtime account needs run.invoker here and secretAccessor on that one token. The studio release
  # itself is the runbook's procedure (build from merged main, --no-traffic --tag, /health,
  # update-traffic) and is NOT run here: the two settings it must carry are printed, the URL filled
  # in, the token never (it rides from Secret Manager into the service as a secret).
  local url sa
  url="$(service_url)"
  sa="$("$GCLOUD" run services describe "$STUDIO_SERVICE" --project "$PROJECT" --region "$REGION" \
    --format 'value(spec.template.spec.serviceAccountName)')"
  sa="${sa:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
  "$GCLOUD" run services add-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${sa}" --role roles/run.invoker >/dev/null
  "$GCLOUD" secrets add-iam-policy-binding DRIVE_INTAKE_POLL_TOKEN --project "$PROJECT" \
    --member "serviceAccount:${sa}" --role roles/secretmanager.secretAccessor --condition None >/dev/null
  echo "${sa} may invoke ${SERVICE} and read DRIVE_INTAKE_POLL_TOKEN"
  echo "add these two to the studio release command, exactly (docs/CLOUD-FLEET-RUNBOOK.md, the studio release):"
  echo "  --update-env-vars STUDIO_TELEPHONE_URL=${url} --update-secrets STUDIO_TELEPHONE_TOKEN=DRIVE_INTAKE_POLL_TOKEN:latest"
}

trail() {
  # The doc's receipt trail as the deployed service reads it (GET /trail?doc=, PR 376): the proof of
  # a round trip from the terminal, without opening the doc. With the flag off the service answers
  # {"enabled": false, "trail": null}. The poll token is read from Secret Manager at this moment and
  # rides in its own header, never printed; Authorization carries your own identity token, as
  # readback sends it.
  # The whole argument, in bash: grep answers per LINE, so "bad<newline>good-id" passed it.
  # --fail-with-body: a 401, 403, 404 (the route is not deployed until #376 is) or 5xx prints what
  # the service said and EXITS NON-ZERO. This verb is the proof; a refusal must not read as one.
  local doc="${2:-}" url token status=0
  if ! [[ "$doc" =~ $DOC_ID_OK ]]; then
    echo "usage: deploy.sh trail <Drive file id>" >&2
    exit 2
  fi
  url="$(service_url)"
  token="$("$GCLOUD" secrets versions access latest --secret DRIVE_INTAKE_POLL_TOKEN --project "$PROJECT")"
  "$CURL" -sS --fail-with-body -H "Authorization: Bearer $("$GCLOUD" auth print-identity-token)" \
    -H "X-Drive-Intake-Poll-Token: ${token}" "${url}/trail?doc=${doc}" || status=$?
  echo
  if [ "$status" -ne 0 ]; then
    echo "the trail was NOT read (curl exit ${status}): this is not a proof" >&2
    exit 1
  fi
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
  studio) studio ;;
  trail) trail "$@" ;;
  *) sed -n '2,30p' "$0"; exit 2 ;;
esac

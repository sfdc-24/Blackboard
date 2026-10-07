#!/usr/bin/env bash
# The bus reconciler as a Cloud Run job with VPC egress.
#
# WHY A JOB OF ITS OWN
# It needs egress to a private Memorystore address. Putting that on the chair or the gateway would
# mean a new revision of a service that works, for no benefit to that service, while a review is open
# on one of them. This touches neither. When phase 2 needs the chair to read from Redis, egress goes
# on then, with a live-call check first.
#
# LEAST PRIVILEGE, PER SECRET, NOT PER PROJECT
# Our own access inventory records the default compute service account reading EVERY secret in the
# project across five runtimes. That is the thing not to repeat. This job gets its own service
# account with secretAccessor on exactly four secrets and nothing else: no project-level grant, no
# bucket, no Firestore, no ability to write to the board.
#
# WHAT IT CANNOT DO, BY CONSTRUCTION
# Nothing in the image appends to the board. The reconciler reads the gateway and writes only to
# Redis keys under bus:. A compare run cannot alter the thing it is comparing.
#
#   bash cloud/bus-reconciler/deploy.sh setup    # service account and the four secret grants
#   bash cloud/bus-reconciler/deploy.sh build    # build the image from the repo root
#   bash cloud/bus-reconciler/deploy.sh deploy   # create or update the job, with egress
#   bash cloud/bus-reconciler/deploy.sh run      # execute it once and wait
#   bash cloud/bus-reconciler/deploy.sh readback # what is actually deployed, printed
#   bash cloud/bus-reconciler/deploy.sh off      # REDIS_DUAL_ENABLED=false, no redeploy of anything else

set -euo pipefail

# WHY THE TWO PATHS ARE NOT PASSED AS ENVIRONMENT VARIABLES
# Git Bash rewrites anything that looks like a Unix path before gcloud sees it, and it does so
# INCONSISTENTLY: the first deploy of this job put
#   REDIS_CA_CERT_PATH=C:/Program Files/Git/secrets/ca/redis-ca.pem
# into the container while REDIS_AUTH_FILE came through untouched. The job would have mounted the CA
# correctly and then looked for it somewhere that does not exist. Only the read-back caught it.
# Setting MSYS_NO_PATHCONV globally here was the obvious fix and it broke gcloud's own bash wrapper,
# which needs that conversion to find its lib directory.
# So the mount PATHS stay (they survive conversion, inside --set-secrets) and the VALUES are gone:
# cloud/bus-reconciler/main.py defaults them to the mount layout it is deployed with. One source of
# truth for where a secret lands, instead of two that can disagree.

PROJECT="${PROJECT:-sfdc24}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-bus-reconciler}"
NAME="bus-reconciler"
SA_NAME="${SA_NAME:-bus-reconciler}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"
IMAGE="${REPO}/${NAME}:v1"
GCLOUD="${GCLOUD:-gcloud}"

# The four, and only four, secrets this job may read.
SECRETS=(BUS_URL BUS_SECRET REDIS_AUTH_STRING REDIS_CA_CERT)

# The instance. Passed in rather than written here: this repository is PUBLIC and the private address
# is topology.
#
# REQUIRED ONLY WHERE IT IS USED. Copilot, PR 323: this expansion was `${REDIS_HOST:?...}` at the
# top of the file, so it ran before the command dispatch and `deploy.sh off` exited unless the
# address was supplied - even though disabling a job never needs it. The off-switch asking for
# connection details before it will switch anything off is the worst possible place for a
# prerequisite, and it blocked setup, build, run and readback for no reason either. The check moved
# into deploy(), which is the only action that writes the address anywhere.
REDIS_HOST="${REDIS_HOST:-}"
REDIS_PORT="${REDIS_PORT:-6378}"
# The server CA is mounted as a FILE from Secret Manager, because Memorystore's CA is not in the
# image's system trust store and SERVER_AUTHENTICATION means the client must verify against it.
#
# EACH MOUNTED SECRET NEEDS ITS OWN DIRECTORY. Cloud Run mounts a secret by mounting the directory
# that holds it, so two secrets sharing /secrets is refused: "a different secret is already mounted
# in the same directory". Found on the first deploy attempt, which is what a first deploy is for.
CA_DIR="/secrets/ca"
CA_PATH="${CA_DIR}/redis-ca.pem"
# The AUTH string is MOUNTED AS A FILE, not passed as an environment variable: an env var is listable
# from anything that can read the process, and a mounted file is not. The image also has no gcloud in
# it - python:3.12-slim carries no Cloud SDK - so a file is the only way it can arrive at all. Found
# while deploying rather than after, which is the only reason it is here and not a defect.
AUTH_DIR="/secrets/auth"
AUTH_PATH="${AUTH_DIR}/redis-auth"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"
# private-ranges-only, NEVER all-traffic. all-traffic would route the job's every outbound call
# through the VPC, and the gateway it reads is on the public internet - that is how you break the
# thing you were trying not to touch.
EGRESS="private-ranges-only"
WINDOW_MINUTES="${WINDOW_MINUTES:-1440}"

setup() {
  "$GCLOUD" iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
    "$GCLOUD" iam service-accounts create "$SA_NAME" --project "$PROJECT" \
      --display-name "Bus reconciler: read the board, write only bus: keys in Redis"
  for secret in "${SECRETS[@]}"; do
    "$GCLOUD" secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
      --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor \
      --condition None >/dev/null
    echo "granted secretAccessor on ${secret}"
  done
}

build() {
  "$GCLOUD" builds submit --project "$PROJECT" --config cloud/bus-reconciler/cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE}" .
}

deploy() {
  : "${REDIS_HOST:?set REDIS_HOST to the Memorystore private address; it is not committed here}"
  local verb=create
  "$GCLOUD" run jobs describe "$JOB" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1 && verb=update
  "$GCLOUD" run jobs "$verb" "$JOB" --project "$PROJECT" --region "$REGION" \
    --image "$IMAGE" --service-account "$SA" \
    --set-secrets "BUS_URL=BUS_URL:latest,BUS_SECRET=BUS_SECRET:latest,${CA_PATH}=REDIS_CA_CERT:latest,${AUTH_PATH}=REDIS_AUTH_STRING:latest" \
    --set-env-vars "REDIS_CONNECT=true,REDIS_HOST=${REDIS_HOST},REDIS_PORT=${REDIS_PORT},RECONCILE_WINDOW_MINUTES=${WINDOW_MINUTES},RECONCILE_MIRROR=0" \
    --network "$NETWORK" --subnet "$SUBNET" --vpc-egress "$EGRESS" \
    --max-retries 1 --task-timeout 10m --cpu 1 --memory 512Mi
  echo "job ${JOB} ${verb}d in ${REGION} with ${EGRESS} egress on ${NETWORK}/${SUBNET}"
}

run_once() {
  "$GCLOUD" run jobs execute "$JOB" --project "$PROJECT" --region "$REGION" --wait
}

readback() {
  # What is deployed, not what this script intended to deploy. The two are different facts.
  "$GCLOUD" run jobs describe "$JOB" --project "$PROJECT" --region "$REGION" \
    --format='yaml(name,template.template.containers[0].image,template.template.serviceAccount,template.template.containers[0].env,template.template.vpcAccess)'
}

off() {
  # The off-switch at the job level, and it needs NO connection configuration - which is the point
  # of the finding above. redis_dual re-reads its settings on every call, so inside a running
  # execution this is already honoured; this stops the next execution from connecting at all.
  #
  # BOTH switches, because there are two now. REDIS_DUAL_ENABLED alone stops the dual-run and
  # leaves every diagnostic connecting, which is not what "off" says. REDIS_CONNECT=false is the
  # one that closes the socket.
  "$GCLOUD" run jobs update "$JOB" --project "$PROJECT" --region "$REGION" \
    --update-env-vars "REDIS_DUAL_ENABLED=false,REDIS_CONNECT=false"
  echo "${JOB}: REDIS_DUAL_ENABLED=false and REDIS_CONNECT=false. It will run and report UNKNOWN"
  echo "rather than connect. Note the FILE also wins: scripts/redis_dual.settings.json with"
  echo "enabled=false cannot be overridden back on from the environment."
}

case "${1:-}" in
  setup) setup ;;
  build) build ;;
  deploy) deploy ;;
  run) run_once ;;
  readback) readback ;;
  off) off ;;
  *) sed -n '1,30p' "$0"; exit 2 ;;
esac

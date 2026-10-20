#!/usr/bin/env bash
# inbox-wake as a PRIVATE Cloud Run service that wakes claude-api, and nobody else. Gate 3 Step 3.
#
# WHAT THIS IS
# CO-20261013-001 Step 3: "Enable inbox-wake for folder 1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP, recipient
# claude-api only." The folder is drive-intake's (cloud/drive-intake/deploy.sh); this service is the
# ring drive-intake calls after it puts a task on fleet:inbox:claude-api. Waking claude-api is running
# the Cloud Run job claude-api-worker, which reads that inbox on every execution.
#
# PRIVATE. The one caller is drive-intake, with an ID token (drive_feed.private_post) and the shared
# HMAC (X-Hub-Signature-256). No public invoker binding is made here; a GitHub webhook would need one, and
# that is not this step.
#
#   bash cloud/inbox-wake/deploy.sh setup     # its own service account, secret grants, run on the worker job
#   bash cloud/inbox-wake/deploy.sh secret    # mint INBOX_WAKE_SECRET into Secret Manager (never printed)
#   bash cloud/inbox-wake/deploy.sh build     # the image, from the repo root
#   REDIS_HOST=<private address> bash cloud/inbox-wake/deploy.sh deploy   # flag OFF, claude-api only
#   bash cloud/inbox-wake/deploy.sh readback  # what is actually deployed, printed; /healthz
#   bash cloud/inbox-wake/deploy.sh on | off  # the flag, and nothing else

set -euo pipefail

PROJECT="${PROJECT:-sfdc24}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-inbox-wake}"
NAME="inbox-wake"
SA_NAME="${SA_NAME:-inbox-wake}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
# drive-intake's own identity: the one caller allowed to invoke this service.
CALLER="${CALLER:-drive-intake@${PROJECT}.iam.gserviceaccount.com}"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"
TAG="${TAG:-v1}"
IMAGE="${REPO}/${NAME}:${TAG}"
GCLOUD="${GCLOUD:-gcloud}"
# Gate 3 Step 3: claude-api, and only claude-api. The job it runs to wake it.
AGENTS="${INBOX_WAKE_AGENTS:-claude-api}"
WORKER_JOB="${WORKER_JOB:-claude-api-worker}"

SECRETS=(REDIS_AUTH_STRING REDIS_CA_CERT INBOX_WAKE_SECRET)
REDIS_HOST="${REDIS_HOST:-}"
REDIS_PORT="${REDIS_PORT:-6378}"
CA_PATH="/secrets/ca/redis-ca.pem"
AUTH_PATH="/secrets/auth/redis-auth"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"
# private-ranges-only, NEVER all-traffic: the Cloud Run API it calls is on the public internet.
EGRESS="private-ranges-only"

grant_secret() {
  local secret="$1" member="$2"
  if ! "$GCLOUD" secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1; then
    echo "secret ${secret} does not exist yet; run: deploy.sh secret (or create it)" >&2
    return 0
  fi
  "$GCLOUD" secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
    --member "serviceAccount:${member}" --role roles/secretmanager.secretAccessor \
    --condition None >/dev/null
  echo "granted secretAccessor on ${secret} to ${member}"
}

setup() {
  "$GCLOUD" iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
    "$GCLOUD" iam service-accounts create "$SA_NAME" --project "$PROJECT" \
      --display-name "Inbox wake: run claude-api-worker when a task lands, write fleet:wake:seen keys"
  for secret in "${SECRETS[@]}"; do
    grant_secret "$secret" "$SA"
  done
  # Waking claude-api is running its job: run.invoker on THAT job, nothing project-wide. The same
  # binding the default compute identity holds on gemini-waker for the board watcher.
  "$GCLOUD" run jobs add-iam-policy-binding "$WORKER_JOB" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${SA}" --role roles/run.invoker >/dev/null
  echo "granted run.invoker on job ${WORKER_JOB} to ${SA}"
}

secret() {
  # The ring's HMAC, shared by inbox-wake (GITHUB_WEBHOOK_SECRET) and drive-intake (INBOX_WAKE_SECRET).
  if "$GCLOUD" secrets describe INBOX_WAKE_SECRET --project "$PROJECT" >/dev/null 2>&1; then
    echo "INBOX_WAKE_SECRET exists; not replaced (add a version by hand if you mean to rotate it)"
  else
    python -c "import secrets,sys; sys.stdout.write(secrets.token_hex(32))" |
      "$GCLOUD" secrets create INBOX_WAKE_SECRET --project "$PROJECT" --replication-policy automatic --data-file=-
    echo "INBOX_WAKE_SECRET created with one version"
  fi
  grant_secret INBOX_WAKE_SECRET "$SA"
  grant_secret INBOX_WAKE_SECRET "$CALLER"
}

build() {
  "$GCLOUD" builds submit --project "$PROJECT" --config cloud/inbox-wake/cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE}" .
}

deploy() {
  : "${REDIS_HOST:?set REDIS_HOST to the Memorystore private address; it is not committed here}"
  "$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --image "$IMAGE" --service-account "$SA" \
    --no-allow-unauthenticated --min-instances 0 --max-instances 2 \
    --set-secrets "${CA_PATH}=REDIS_CA_CERT:latest,${AUTH_PATH}=REDIS_AUTH_STRING:latest,GITHUB_WEBHOOK_SECRET=INBOX_WAKE_SECRET:latest" \
    --set-env-vars "INBOX_WAKE_ENABLED=off,INBOX_WAKE_AGENTS=${AGENTS},REDIS_CONNECT=true,REDIS_HOST=${REDIS_HOST},REDIS_PORT=${REDIS_PORT},RUN_PROJECT=${PROJECT},RUN_REGION=${REGION}" \
    --network "$NETWORK" --subnet "$SUBNET" --vpc-egress "$EGRESS" \
    --cpu 1 --memory 256Mi --timeout 60
  "$GCLOUD" run services add-iam-policy-binding "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${CALLER}" --role roles/run.invoker >/dev/null
  echo "service ${SERVICE} deployed PRIVATE in ${REGION}, flag off, wakes ${AGENTS}; ${CALLER} may invoke it"
}

readback() {
  # READ BACK before trusting any of it: the service account is inbox-wake, the agents are claude-api,
  # the flag is what you set, egress is private-ranges-only, and the only invoker is drive-intake.
  "$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format 'yaml(spec.template.spec.serviceAccountName,spec.template.spec.containers[0].env,spec.template.spec.containers[0].image,spec.template.metadata.annotations)'
  "$GCLOUD" run services get-iam-policy "$SERVICE" --project "$PROJECT" --region "$REGION" --format yaml
  local url
  url="$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')"
  echo "healthz:"
  curl -s -H "Authorization: Bearer $("$GCLOUD" auth print-identity-token)" "${url}/healthz"; echo
}

flag() {
  "$GCLOUD" run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --update-env-vars "INBOX_WAKE_ENABLED=$1"
  echo "INBOX_WAKE_ENABLED=$1 on ${SERVICE}"
}

case "${1:-}" in
  setup) setup ;;
  secret) secret ;;
  build) build ;;
  deploy) deploy ;;
  readback) readback ;;
  on) flag on ;;
  off) flag off ;;
  *) sed -n '2,21p' "$0"; exit 2 ;;
esac

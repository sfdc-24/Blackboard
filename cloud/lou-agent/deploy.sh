#!/usr/bin/env bash
# Lou as code (cloud/lou-agent) on Cloud Run: ONE always-on instance that holds a worker connection to
# Lou's LiveKit project and answers only a dispatch that names its agent (lou-code). The Builder Lou is
# untouched and stays the fallback. Owner-approved 2026-10-17 ("LOU HOSTING: APPROVED", within the
# CAD 25/day cap).
#
# COST, AT REST: 1 vCPU + 1 GiB, min 1 and max 1 instance, CPU always on: about USD 1.9 a day in
# us-central1 (instance-based billing). Calls add GPT-Live minutes on the OpenAI key, and LiveKit
# minutes. `off` takes it to zero instances and so zero cost at rest.
#
# WHAT IS THE OWNER'S AND NOT THIS SCRIPT'S
#   1. Lou's LiveKit project keys, as three secrets: LOU_LIVEKIT_URL (wss://...3p1vbksold1...livekit.cloud),
#      LOU_LIVEKIT_API_KEY, LOU_LIVEKIT_API_SECRET. The fleet's LIVEKIT_URL is ANOTHER project.
#   2. The v4 prompt, as the secret LOU_INSTRUCTIONS (the repository is public, so it is never here).
#   3. The Memorystore private address, passed as REDIS_HOST.
#
#   bash cloud/lou-agent/deploy.sh setup      # the service account and its secret grants
#   bash cloud/lou-agent/deploy.sh build      # the image, from the repo root
#   REDIS_HOST=<private address> bash cloud/lou-agent/deploy.sh deploy   # min 1, private, VPC egress
#   bash cloud/lou-agent/deploy.sh readback   # what is deployed, and the worker's own log line
#   bash cloud/lou-agent/deploy.sh off        # zero instances: no worker, no cost at rest

set -euo pipefail

PROJECT="${PROJECT:-sfdc24}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-lou-agent}"
SA_NAME="${SA_NAME:-lou-agent}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"
TAG="${TAG:-v1}"
IMAGE="${IMAGE:-${REPO}/lou-agent:${TAG}}"
GCLOUD="${GCLOUD:-gcloud}"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"
EGRESS="${EGRESS:-private-ranges-only}"
REDIS_HOST="${REDIS_HOST:-}"
REDIS_PORT="${REDIS_PORT:-6378}"
AGENT_NAME="${AGENT_NAME:-lou-code}"
CA_PATH="/secrets/ca/redis-ca.pem"
AUTH_PATH="/secrets/auth/redis-auth"
PROMPT_PATH="/secrets/prompt/lou-instructions.txt"
SECRETS=(LOU_LIVEKIT_URL LOU_LIVEKIT_API_KEY LOU_LIVEKIT_API_SECRET OPENAI_API_KEY LOU_INSTRUCTIONS REDIS_AUTH_STRING REDIS_CA_CERT)

setup() {
  "$GCLOUD" iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
    "$GCLOUD" iam service-accounts create "$SA_NAME" --project "$PROJECT" \
      --display-name "Lou code agent: LiveKit worker, reads its own secrets, writes lou: keys"
  local secret
  for secret in "${SECRETS[@]}"; do
    if ! "$GCLOUD" secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1; then
      echo "MISSING secret ${secret}: create it first (the value is the owner's, never this script's)"
      exit 1
    fi
    "$GCLOUD" secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
      --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor >/dev/null
    echo "granted ${secret} to ${SA}"
  done
}

build() {
  "$GCLOUD" builds submit --project "$PROJECT" --config cloud/lou-agent/cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE}" .
}

deploy() {
  : "${REDIS_HOST:?set REDIS_HOST to the Memorystore private address; it is not committed here}"
  "$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --image "$IMAGE" --service-account "$SA" \
    --no-allow-unauthenticated --ingress internal \
    --min-instances 1 --max-instances 1 --no-cpu-throttling --cpu 1 --memory 1Gi --port 8081 \
    --set-secrets "LIVEKIT_URL=LOU_LIVEKIT_URL:latest,LIVEKIT_API_KEY=LOU_LIVEKIT_API_KEY:latest,LIVEKIT_API_SECRET=LOU_LIVEKIT_API_SECRET:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest,${PROMPT_PATH}=LOU_INSTRUCTIONS:latest,${CA_PATH}=REDIS_CA_CERT:latest,${AUTH_PATH}=REDIS_AUTH_STRING:latest" \
    --set-env-vars "LOU_AGENT_NAME=${AGENT_NAME},LOU_INSTRUCTIONS_FILE=${PROMPT_PATH},LOU_REDIS=on,REDIS_CONNECT=true,REDIS_HOST=${REDIS_HOST},REDIS_PORT=${REDIS_PORT},LOU_IDLE_PROCESSES=1" \
    --network "$NETWORK" --subnet "$SUBNET" --vpc-egress "$EGRESS"
  echo "service ${SERVICE} deployed PRIVATE (internal ingress), one always-on worker as agent ${AGENT_NAME}"
}

readback() {
  "$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format 'yaml(spec.template.spec.serviceAccountName,spec.template.spec.containers[0].image,spec.template.metadata.annotations,status.latestReadyRevisionName)'
  # Bound to the ready revision and the image this script deploys: a service-wide log query can return an
  # old revision's 'registered' line and pass a deploy that never came up (Aya on be319c7).
  local rev image lines
  rev="$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format 'value(status.latestReadyRevisionName)' | tr -d '\r')"
  if [ -z "$rev" ]; then echo "STOPPED: ${SERVICE} has no ready revision"; return 1; fi
  image="$("$GCLOUD" run revisions describe "$rev" --project "$PROJECT" --region "$REGION" \
    --format 'value(spec.containers[0].image)' | tr -d '\r')"
  echo "ready revision ${rev} runs ${image:-nothing}"
  if [ "$image" != "$IMAGE" ]; then echo "STOPPED: ${rev} is not running ${IMAGE}"; return 1; fi
  echo "the worker's own word from ${rev} only (registered means LiveKit accepted it):"
  lines="$("$GCLOUD" logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\" AND resource.labels.revision_name=\"${rev}\" AND textPayload:\"registered worker\"" \
    --project "$PROJECT" --limit 3 --format 'value(timestamp,textPayload)' | tr -d '\r')" || lines=""
  if [ -z "$lines" ]; then echo "NOT YET: no 'registered worker' line from ${rev}"; return 1; fi
  echo "$lines"
  echo "READBACK OK: ${rev} on ${IMAGE} registered with LiveKit"
}

off() {
  "$GCLOUD" run services update "$SERVICE" --project "$PROJECT" --region "$REGION" --min-instances 0
  echo "${SERVICE}: min 0; no request reaches an internal service, so it scales to zero and the worker leaves"
}

case "${1:-}" in
  setup) setup ;;
  build) build ;;
  deploy) deploy ;;
  readback) readback ;;
  off) off ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac

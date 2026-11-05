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
#   1. Lou's LiveKit keys, as three secrets: LOU_LIVEKIT_URL, LOU_LIVEKIT_API_KEY, LOU_LIVEKIT_API_SECRET.
#      Lou lives in project "Blackboard" (p_3p1vbksold1, wss://blackboard-c5gn7crt.livekit.cloud), where its
#      trunk, number and dispatch rule are: the SAME project as the fleet's LIVEKIT_URL, with Lou's OWN key.
#   2. The v4 prompt, as the secret LOU_INSTRUCTIONS (the repository is public, so it is never here).
#   3. The Memorystore private address, passed as REDIS_HOST.
#
#   bash cloud/lou-agent/deploy.sh setup      # the service account and its secret grants
#   bash cloud/lou-agent/deploy.sh build      # the image, from the repo root
#   REDIS_HOST=<private address> bash cloud/lou-agent/deploy.sh deploy   # min 1, private, VPC egress
#   REVISION=<printed> DIGEST=<printed> bash cloud/lou-agent/deploy.sh readback   # that revision, that digest, its log
#   bash cloud/lou-agent/deploy.sh off        # zero instances: no worker, no cost at rest

set -euo pipefail

PROJECT="${PROJECT:-sfdc24}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-lou-agent}"
SA_NAME="${SA_NAME:-lou-agent}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"
TAG="${TAG:-$(git rev-parse --short=12 HEAD 2>/dev/null || echo unreleased)}"   # a tag is a label; deploy pins the digest
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
  # Pinned by digest, into a revision named here, so the readback can prove THIS deploy and not an older
  # revision on the same tag (Aya on b7884cb: a failed new revision left the old one ready, and it passed).
  local digest base suffix
  digest="$("$GCLOUD" artifacts docker images describe "$IMAGE" --project "$PROJECT" \
    --format 'value(image_summary.digest)' | tr -d '\r')" || digest=""
  case "$digest" in sha256:*) ;; *) echo "STOPPED: no digest for ${IMAGE}; build it first"; return 1 ;; esac
  base="${IMAGE%@*}"
  base="${base%:*}"
  suffix="d$(date -u +%Y%m%d-%H%M%S)"
  "$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --image "${base}@${digest}" --revision-suffix "$suffix" --service-account "$SA" \
    --no-allow-unauthenticated --ingress internal \
    --min-instances 1 --max-instances 1 --no-cpu-throttling --cpu 1 --memory 1Gi --port 8081 \
    --set-secrets "LIVEKIT_URL=LOU_LIVEKIT_URL:latest,LIVEKIT_API_KEY=LOU_LIVEKIT_API_KEY:latest,LIVEKIT_API_SECRET=LOU_LIVEKIT_API_SECRET:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest,${PROMPT_PATH}=LOU_INSTRUCTIONS:latest,${CA_PATH}=REDIS_CA_CERT:latest,${AUTH_PATH}=REDIS_AUTH_STRING:latest" \
    --set-env-vars "LOU_AGENT_NAME=${AGENT_NAME},LOU_INSTRUCTIONS_FILE=${PROMPT_PATH},LOU_REDIS=on,REDIS_CONNECT=true,REDIS_HOST=${REDIS_HOST},REDIS_PORT=${REDIS_PORT},LOU_IDLE_PROCESSES=1" \
    --network "$NETWORK" --subnet "$SUBNET" --vpc-egress "$EGRESS"
  echo "service ${SERVICE} deployed PRIVATE (internal ingress), one always-on worker as agent ${AGENT_NAME}"
  echo "read it back with: REVISION=${SERVICE}-${suffix} DIGEST=${digest} bash cloud/lou-agent/deploy.sh readback"
}

readback() {
  "$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format 'yaml(spec.template.spec.serviceAccountName,spec.template.spec.containers[0].image,spec.template.metadata.annotations,status.latestReadyRevisionName)'
  # Bound to the revision and digest deploy printed. A service-wide log query returned an old revision's
  # 'registered' line (Aya on be319c7), and 'whatever is ready on this tag' passed an old revision when the
  # new one failed (Aya on b7884cb). So: that revision must be the ready one, run that digest, and log it.
  local rev="${REVISION:-}" want="${DIGEST:-}" ready ran lines
  if [ -z "$rev" ] || [ -z "$want" ]; then echo "STOPPED: set REVISION and DIGEST to what deploy printed"; return 1; fi
  ready="$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format 'value(status.latestReadyRevisionName)' | tr -d '\r')"
  if [ "$ready" != "$rev" ]; then echo "STOPPED: ${rev} is not the ready revision (ready: ${ready:-none})"; return 1; fi
  ran="$("$GCLOUD" run revisions describe "$rev" --project "$PROJECT" --region "$REGION" \
    --format 'value(status.imageDigest)' | tr -d '\r')"
  echo "ready revision ${rev} runs ${ran:-nothing}"
  case "$ran" in *"@${want}") ;; *) echo "STOPPED: ${rev} is not running ${want}"; return 1 ;; esac
  echo "the worker's own word from ${rev} only (registered means LiveKit accepted it):"
  lines="$("$GCLOUD" logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\" AND resource.labels.revision_name=\"${rev}\" AND textPayload:\"registered worker\"" \
    --project "$PROJECT" --limit 3 --format 'value(timestamp,textPayload)' | tr -d '\r')" || lines=""
  if [ -z "$lines" ]; then echo "NOT YET: no 'registered worker' line from ${rev}"; return 1; fi
  echo "$lines"
  echo "READBACK OK: ${rev} on ${want} registered with LiveKit"
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

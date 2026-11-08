#!/usr/bin/env bash
# fleet-dispatch as ONE Cloud Run worker pool, blocked on the cloud agents' lanes.
#
# NOT APPLIED. This PR adds the script. It does not run it. plan prints the
# command; deploy would run that command; readback describes whatever is live.
# Codex reviews the source before either of those is anyone's next step.
#
#   bash cloud/fleet-dispatch/deploy.sh setup      # its own service account, secret grants, run on the two jobs
#   bash cloud/fleet-dispatch/deploy.sh build      # the image, from the repo root. Nothing deployed.
#   REDIS_HOST=<private address> bash cloud/fleet-dispatch/deploy.sh plan
#   REDIS_HOST=<private address> bash cloud/fleet-dispatch/deploy.sh deploy
#   bash cloud/fleet-dispatch/deploy.sh readback
#
# THE POOL. A worker pool does not autoscale. --instances=1 is min 1 and max 1:
# one process in the VPC, never a second and never zero. Direct VPC, the same
# shape as redis-tool-bridge: --network, --subnet, --vpc-egress=private-ranges-only.
# private-ranges-only, NEVER all-traffic: the Cloud Run API the ring calls is on
# the public internet, and Memorystore is the private range.
#
# THE FLAG. The image and this command both set FLEET_DISPATCH=off. The allowlist
# defaults empty, which the process treats as nobody. Turning it on is a later
# revision, with FLEET_DISPATCH_AGENTS set to the agents that revision may ring.

set -euo pipefail

PROJECT="${PROJECT:-sfdc24}"
REGION="${REGION:-us-central1}"
POOL="${POOL:-fleet-dispatch}"
SA_NAME="${SA_NAME:-fleet-dispatch}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"
TAG="${TAG:-unreleased}"
IMAGE="${REPO}/${POOL}:${TAG}"
GCLOUD="${GCLOUD:-gcloud}"
# The cloud_run_job targets in cloud/inbox-wake/main.py ROUTES. tests/test_fleet_dispatch.py
# reads this list back against that table, so a new job route cannot ship here unnoticed.
JOBS=(gemini-waker claude-api-worker)
SECRETS=(REDIS_AUTH_STRING REDIS_CA_CERT)

REDIS_HOST="${REDIS_HOST:-}"
REDIS_PORT="${REDIS_PORT:-6378}"
AGENTS="${FLEET_DISPATCH_AGENTS:-}"
GROK_URL="${GROK_WAKE_URL:-}"
CA_PATH="/secrets/ca/redis-ca.pem"
AUTH_PATH="/secrets/auth/redis-auth"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"
EGRESS="private-ranges-only"

usage() {
  sed -n '2,20p' "$0"
}

# A host is a dotted IPv4 address (four numbers 0-255) or a DNS name: 1 to 253 characters, labels of
# 1 to 63 letters, digits or dashes that neither start nor end with a dash, and no empty label.
# The same check as cloud/redis-tool-bridge/deploy.sh (Codex on #477, R2 and R3).
valid_host() {
  local host="$1" label count=0 numeric=0
  [[ "${host}" =~ ^[A-Za-z0-9.-]{1,253}$ ]] || return 1
  [[ "${host}" != .* && "${host}" != *. && "${host}" != *..* ]] || return 1
  local -a labels
  IFS=. read -r -a labels <<< "${host}"
  for label in "${labels[@]}"; do
    [[ "${label}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]] || return 1
    count=$((count + 1))
    if [[ "${label}" =~ ^[0-9]+$ ]]; then
      numeric=$((numeric + 1))
    fi
  done
  if [ "${numeric}" -eq "${count}" ]; then
    [ "${count}" -eq 4 ] || return 1
    for label in "${labels[@]}"; do
      [[ "${label}" =~ ^(0|[1-9][0-9]{0,2})$ ]] && [ "${label}" -le 255 ] || return 1
    done
  fi
  return 0
}

valid_port() {
  [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$1" -le 65535 ]
}

valid_token() {
  # A project, region, network, subnet, pool or tag: no quotes, no spaces, no shell.
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]]
}

valid_agents() {
  [ -z "$1" ] && return 0
  [[ "$1" =~ ^[A-Za-z0-9_.:-]+(,[A-Za-z0-9_.:-]+)*$ ]]
}

valid_https() {
  # No quotes, spaces, dollars or semicolons: the value is one word in the generated script,
  # and ';' is the --set-env-vars delimiter.
  [[ "$1" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~:/?#@!$&()*+,=%-]*)?$ ]]
}

check_place() {
  valid_token "${PROJECT}" || { echo "refusing: PROJECT is not a token" >&2; return 1; }
  valid_token "${REGION}" || { echo "refusing: REGION is not a token" >&2; return 1; }
  valid_token "${POOL}" || { echo "refusing: POOL is not a token" >&2; return 1; }
  valid_token "${SA_NAME}" || { echo "refusing: SA_NAME is not a token" >&2; return 1; }
  valid_token "${TAG}" || { echo "refusing: TAG is not a token" >&2; return 1; }
  valid_token "${NETWORK}" || { echo "refusing: NETWORK is not a token" >&2; return 1; }
  valid_token "${SUBNET}" || { echo "refusing: SUBNET is not a token" >&2; return 1; }
}

check_redis() {
  # REDIS_HOST COMES FROM THE OPERATOR, never from this file. A private address is not a
  # credential, and this repository is public.
  if [ -z "${REDIS_HOST}" ]; then
    echo "refusing: set REDIS_HOST in the environment (the instance's private address)" >&2
    return 1
  fi
  # Checked before it is written into the generated script. A quote in it would end the
  # single-quoted word bash then runs (Codex on #477).
  if ! valid_host "${REDIS_HOST}"; then
    echo "refusing: REDIS_HOST must be an IPv4 address or a DNS name" >&2
    return 1
  fi
  if ! valid_port "${REDIS_PORT}"; then
    echo "refusing: REDIS_PORT must be a number from 1 to 65535" >&2
    return 1
  fi
}

check_agents() {
  if ! valid_agents "${AGENTS}"; then
    echo "refusing: FLEET_DISPATCH_AGENTS must be comma-separated ids, or empty" >&2
    return 1
  fi
}

check_grok() {
  if [ -n "${GROK_URL}" ] && ! valid_https "${GROK_URL}"; then
    echo "refusing: GROK_WAKE_URL must be an https URL" >&2
    return 1
  fi
}

grant_secret() {
  local secret="$1"
  "$GCLOUD" secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
    --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor \
    --condition None >/dev/null
  echo "granted secretAccessor on ${secret} to ${SA}"
}

setup() {
  check_place
  "$GCLOUD" iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
    "$GCLOUD" iam service-accounts create "$SA_NAME" --project "$PROJECT" \
      --display-name "Fleet dispatch: block on the cloud lanes and ring those agents"
  for secret in "${SECRETS[@]}"; do
    grant_secret "$secret"
  done
  # Ringing gemini and claude-api is running their jobs. run.invoker on those two
  # jobs, nothing project-wide. grok is an HTTP URL, not a job; it gets no binding.
  for job in "${JOBS[@]}"; do
    "$GCLOUD" run jobs add-iam-policy-binding "$job" --project "$PROJECT" --region "$REGION" \
      --member "serviceAccount:${SA}" --role roles/run.invoker >/dev/null
    echo "granted run.invoker on job ${job} to ${SA}"
  done
}

build() {
  check_place
  "$GCLOUD" builds submit --project "$PROJECT" --config cloud/fleet-dispatch/cloudbuild.yaml \
    --substitutions "_IMAGE=${IMAGE}" .
  echo "NOW RECORD THE DIGEST. The tag ${TAG} is mutable."
}

deploy_cmds() {
  check_place
  check_redis
  check_agents
  check_grok
  local env_line
  # ONE --set-env-vars. Repeating the flag replaces the whole environment.
  # '^;^' is the delimiter so the allowlist's commas stay inside one value.
  # FLEET_DISPATCH=off is the value this command writes. An empty allowlist is
  # fail closed, and it is written explicitly so a later --set-env-vars cannot
  # inherit a previous list by accident: this flag replaces the environment.
  env_line="FLEET_DISPATCH=off;FLEET_DISPATCH_AGENTS=${AGENTS};REDIS_CONNECT=true;REDIS_HOST=${REDIS_HOST};REDIS_PORT=${REDIS_PORT};REDIS_AUTH_FILE=${AUTH_PATH};REDIS_CA_CERT_PATH=${CA_PATH};RUN_PROJECT=${PROJECT};RUN_REGION=${REGION}"
  if [ -n "${GROK_URL}" ]; then
    env_line="${env_line};GROK_WAKE_URL=${GROK_URL}"
  fi
  # The generated script stops at its first failure (Codex on #477).
  # SECRETS ARE FILES. A --set-secrets key that begins with / is a mount path, the
  # same form redis-tool-bridge uses. An environment variable cannot be named with
  # a slash, so this cannot fall through into the process environment. Worker pools
  # are second generation: 1 CPU needs at least 512Mi.
  echo 'set -euo pipefail'
  cat <<CMD
${GCLOUD} run worker-pools deploy ${POOL} \\
  --project=${PROJECT} --region=${REGION} \\
  --image=${IMAGE} \\
  --service-account=${SA} \\
  --instances=1 \\
  --cpu=1 --memory=512Mi \\
  --network=${NETWORK} --subnet=${SUBNET} --vpc-egress=${EGRESS} \\
  --set-secrets=${AUTH_PATH}=REDIS_AUTH_STRING:latest,${CA_PATH}=REDIS_CA_CERT:latest \\
  '--set-env-vars=^;^${env_line}'
CMD
}

readback() {
  check_place
  "$GCLOUD" run worker-pools describe "$POOL" --project "$PROJECT" --region "$REGION"
  echo "--- who may act as it:"
  "$GCLOUD" run worker-pools get-iam-policy "$POOL" --project "$PROJECT" --region "$REGION"
  echo "--- NOW CHECK THESE BY EYE, because a green deploy is not a correct one:"
  echo "    * instances is 1 (that is min 1 and max 1; a worker pool has no separate autoscaler)"
  echo "    * the service account is ${SA}"
  echo "    * vpc egress is private-ranges-only, network ${NETWORK}, subnet ${SUBNET}"
  echo "    * FLEET_DISPATCH is off, and FLEET_DISPATCH_AGENTS is the list you meant"
  echo "    * both secrets are file mounts, not environment variables"
  echo "    * no allUsers and no allAuthenticatedUsers"
}

case "${1:-}" in
  setup) setup ;;
  build) build ;;
  plan) deploy_cmds ;;
  deploy) deploy_cmds | bash -euo pipefail -s ;;
  readback) readback ;;
  *) usage; exit 2 ;;
esac

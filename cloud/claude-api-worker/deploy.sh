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
#   THE FIVE #374 MADE FAIL-CLOSED. Unset, the job deploys, `worker on` would succeed, and every task
#   gets QUEUED "no valid cap, reservation, price..." in the doc: it CALLS NOTHING. So `worker on`
#   reads the job back first and refuses while any of them is missing or unusable (`deploy.sh cost` shows).
#     CLAUDE_API_WORKER_USD_PER_MTOK_IN / _OUT   the price of YOUR model id, from the Claude pricing page
#     CLAUDE_API_WORKER_MAX_INPUT_TOKENS         the input size that price holds for (a tier bound, not the context window)
#     FLEET_CAD_PER_USD                          a ceiling rate: every cost is rounded UP into CAD cents
#     CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC     his authorization deadline, ...Z; no call at or after it
#   PHONE_DOC_INTAKE_ENABLED (#374) is a SECOND intake over the two originals, inside this job. Never
#   on together with drive-intake's flag: `pilot on` and the intake's `on` refuse each other (below).
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
#   bash cloud/claude-api-worker/deploy.sh cost      # the cost inputs as deployed; what `worker on` would refuse on
#   bash cloud/claude-api-worker/deploy.sh worker on|off   lane on|off   pilot on|off   # the flags, nothing else

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
# The python that RUNS: python3 where it is one, python where only that name works (a Windows Store
# stub answers to python3 and does nothing). Read-back verbs parse gcloud's JSON with it.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3 python; do
    if "$candidate" -c pass >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
  done
fi
PYTHON="${PYTHON:-python}"
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
MAX_INPUT_TOKENS="${CLAUDE_API_WORKER_MAX_INPUT_TOKENS:-0}"
AUTHORIZED_UNTIL_UTC="${CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC:-}"
PRICE_IN="${CLAUDE_API_WORKER_USD_PER_MTOK_IN:-0}"
PRICE_OUT="${CLAUDE_API_WORKER_USD_PER_MTOK_OUT:-0}"
CAD_PER_USD="${FLEET_CAD_PER_USD:-0}"
CAP_CENTS="${FLEET_SPEND_CAP_CENTS_CAD:-2500}"
RESERVE_CENTS="${CLAUDE_API_WORKER_RESERVE_CENTS_CAD:-5}"
REASSIGN_SECONDS="${CLAUDE_API_WORKER_REASSIGN_SECONDS:-1800}"
MAX_ATTEMPTS="${CLAUDE_API_WORKER_MAX_ATTEMPTS:-1}"
FOLDER_ID="${DRIVE_INTAKE_FOLDER_ID:-1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP}"
HEARTBEAT_SECONDS="${DRIVE_INTAKE_HEARTBEAT_SECONDS:-300}"
# The other intake. `pilot on` refuses while this service has DRIVE_INTAKE_ENABLED=on.
INTAKE_SERVICE="${INTAKE_SERVICE:-drive-intake}"

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
    --set-env-vars "CLAUDE_API_WORKER_ENABLED=off,CLAUDE_API_LANE_ENABLED=off,PHONE_DOC_INTAKE_ENABLED=off,PHONE_DOC_PILOT_TARGET=,REDIS_CONNECT=true,REDIS_HOST=${REDIS_HOST},REDIS_PORT=${REDIS_PORT},CLAUDE_API_WORKER_MODEL=${MODEL},CLAUDE_API_WORKER_MAX_TOKENS=${MAX_TOKENS},CLAUDE_API_WORKER_MAX_INPUT_TOKENS=${MAX_INPUT_TOKENS},CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC=${AUTHORIZED_UNTIL_UTC},CLAUDE_API_WORKER_USD_PER_MTOK_IN=${PRICE_IN},CLAUDE_API_WORKER_USD_PER_MTOK_OUT=${PRICE_OUT},FLEET_CAD_PER_USD=${CAD_PER_USD},FLEET_SPEND_CAP_CENTS_CAD=${CAP_CENTS},CLAUDE_API_WORKER_RESERVE_CENTS_CAD=${RESERVE_CENTS},CLAUDE_API_WORKER_REASSIGN_SECONDS=${REASSIGN_SECONDS},CLAUDE_API_WORKER_MAX_ATTEMPTS=${MAX_ATTEMPTS},DRIVE_INTAKE_FOLDER_ID=${FOLDER_ID},DRIVE_INTAKE_HEARTBEAT_SECONDS=${HEARTBEAT_SECONDS}" \
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

# What makes the worker call nothing, judged the way main.py judges it (call_reservation, _int_env,
# _float_env, valid_prices, _utc_deadline_env): a value that is present but that the worker reads as
# zero, None or past is as unset as an empty one. A negative or NaN price, 100000.0 for the input
# bound (int() refuses it and the worker reads 0), a deadline ending +00:00 or a lowercase z, a
# deadline already passed. Never looser than the worker; the deadline shape is stricter on purpose.
# And an input bound under MIN_INPUT_BOUND: the worker checks every request against that bound, and
# its own instructions plus its 1024 overhead are about 1900 of it before a word of any request, so
# a small whole number passed here and still called nothing (Cursor on fb22006). A real tier bound
# is tens of thousands; tests/test_telephone_deploy.py holds the floor above main.py's own.
# The model id is on the list because an empty one is Unconfigured on every pass.
COST_MISSING_PY='
import math, re, sys, time
from datetime import datetime

DEADLINE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
MIN_INPUT_BOUND = 4096

def positive_int(raw, default=0):
    try:
        return int(raw or default) > 0
    except ValueError:
        return False

def positive_price(raw):
    try:
        value = float(raw)
    except ValueError:
        return False
    return math.isfinite(value) and value > 0

def deadline(raw):
    if not DEADLINE.fullmatch(raw):
        return None
    try:
        return datetime.fromisoformat(raw[:-1] + "+00:00").timestamp()
    except (OverflowError, ValueError):
        return None

def problems(env, now):
    found = []
    def bad(name, why):
        found.append("%s (%s)" % (name, why))
    if not env.get("CLAUDE_API_WORKER_MODEL", "").strip():
        bad("CLAUDE_API_WORKER_MODEL", "empty")
    for name in ("CLAUDE_API_WORKER_USD_PER_MTOK_IN", "CLAUDE_API_WORKER_USD_PER_MTOK_OUT", "FLEET_CAD_PER_USD"):
        if not positive_price(env.get(name, "")):
            bad(name, "not a finite number above zero")
    for name in ("CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "FLEET_SPEND_CAP_CENTS_CAD", "CLAUDE_API_WORKER_RESERVE_CENTS_CAD"):
        if not positive_int(env.get(name, "")):
            bad(name, "not a whole number above zero")
        elif name == "CLAUDE_API_WORKER_MAX_INPUT_TOKENS" and int(env[name]) < MIN_INPUT_BOUND:
            bad(name, "under %d, too small for the instructions the worker sends with every request" % MIN_INPUT_BOUND)
    if "CLAUDE_API_WORKER_MAX_TOKENS" in env and not positive_int(env["CLAUDE_API_WORKER_MAX_TOKENS"], 1):
        bad("CLAUDE_API_WORKER_MAX_TOKENS", "not a whole number above zero")
    until = deadline(env.get("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", ""))
    if until is None:
        bad("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "not YYYY-MM-DDTHH:MM:SSZ")
    elif now >= until:
        bad("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "already passed")
    return found

if __name__ == "__main__":
    lines = (line.rstrip("\r\n") for line in sys.stdin)
    print(", ".join(problems(dict(line.split("=", 1) for line in lines if "=" in line), time.time())))
'
# Prints what is missing or unusable; non-zero when the job could not be read back at all.
cost_missing() {
  local lines
  lines="$(env_lines jobs "$JOB")" || return 1
  printf "%s\n" "$lines" | "$PYTHON" -c "$COST_MISSING_PY" | tr -d "\r"
}

cost_readback() {
  local lines missing
  if ! lines="$(env_lines jobs "$JOB")" || ! missing="$(cost_missing)"; then
    echo "${JOB} could not be read back; worker on refuses until it can be" >&2
    exit 1
  fi
  grep -E "^(CLAUDE_API_WORKER_(MODEL|MAX_TOKENS|MAX_INPUT_TOKENS|AUTHORIZED_UNTIL_UTC|USD_PER_MTOK_IN|USD_PER_MTOK_OUT|RESERVE_CENTS_CAD)|FLEET_CAD_PER_USD|FLEET_SPEND_CAP_CENTS_CAD)=" <<<"$lines" || true
  if [ -z "$missing" ]; then
    echo "every cost input on ${JOB} is one the worker can use; worker on is allowed"
  else
    echo "missing or unusable on ${JOB}: ${missing}; worker on refuses until deploy runs with them set"
  fi
}

worker_on() {
  # Read the JOB back, not this shell: the job is what runs. Refuse rather than deploy a worker
  # that writes QUEUED into the doc once a day per task and never calls (#374's fail-closed inputs).
  local missing
  if ! missing="$(cost_missing)"; then
    echo "refused: ${JOB} could not be read back, so its cost inputs are unknown" >&2
    exit 2
  fi
  if [ -n "$missing" ]; then
    echo "refused: missing or unusable on ${JOB}, so the worker would call nothing: ${missing}" >&2
    echo "export them and run: bash cloud/claude-api-worker/deploy.sh deploy, then worker on" >&2
    exit 2
  fi
  flag CLAUDE_API_WORKER_ENABLED on
}

pilot_on() {
  # The pilot is a SECOND intake over the two originals. With drive-intake's flag on as well, one
  # request becomes two tasks (two revisions of the same words), two calls and two answers in the
  # doc, and the pilot's inbox filter checkpoints the worker past the service's rows for good.
  # A service that cannot be read back is not a service that is off: refuse on that too.
  local other
  if ! other="$(env_lines services "$INTAKE_SERVICE")"; then
    echo "refused: ${INTAKE_SERVICE} could not be read back, so its intake flag is unknown; one intake at a time" >&2
    exit 2
  fi
  if grep -qx "DRIVE_INTAKE_ENABLED=on" <<<"$other"; then
    echo "refused: ${INTAKE_SERVICE} has DRIVE_INTAKE_ENABLED=on; one intake at a time (cloud/drive-intake/deploy.sh off first)" >&2
    exit 2
  fi
  flag PHONE_DOC_INTAKE_ENABLED on
}

case "${1:-}" in
  setup) setup ;;
  build) build ;;
  deploy) deploy ;;
  schedule) schedule ;;
  run) run_once ;;
  readback) readback ;;
  cost) cost_readback ;;
  worker) case "${2:-}" in on) worker_on ;; off) flag CLAUDE_API_WORKER_ENABLED off ;; *) echo "worker on|off" >&2; exit 2 ;; esac ;;
  lane) case "${2:-}" in on|off) flag CLAUDE_API_LANE_ENABLED "$2" ;; *) echo "lane on|off" >&2; exit 2 ;; esac ;;
  pilot) case "${2:-}" in on) pilot_on ;; off) flag PHONE_DOC_INTAKE_ENABLED off ;; *) echo "pilot on|off" >&2; exit 2 ;; esac ;;
  *) sed -n '2,38p' "$0"; exit 2 ;;
esac

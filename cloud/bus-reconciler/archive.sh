#!/usr/bin/env bash
# The hourly Redis archive (redis_archive.py) as a Cloud Run JOB on the bus-reconciler image, ticked by
# Cloud Scheduler. docs/REDIS-PRIMARY.md, D5 - approved by Mr. Salam on 2026-10-11.
#
# WHAT IT IS
# A third job on the image bus-reconciler and bus-requests already run, with the same service account,
# the same four secrets and the same private-ranges-only egress, plus ONE new grant: create and read
# objects under gs://sfdc24-fleet-state/redis-archive/ and nowhere else in the bucket. Create-only
# uploads (ifGenerationMatch=0) mean an archived object is never overwritten, so no delete is granted.
# Objects: <prefix>/streams/<key>/<first>_<last>.jsonl (the range, never the hour). Ledgers:
# <prefix>/<date>/<hour>/LEDGER-<execution>-<hash>.json. Cursors move only after the ledger is written.
#
# THE ORDER, each step with its read-back
#   bash cloud/bus-reconciler/archive.sh setup      # ticker account, its TokenCreator, the prefix grant
#   IMAGE=<registry>/bus-reconciler:git-<sha7> bash cloud/bus-reconciler/deploy.sh build   # a NEW tag; :v1 untouched
#   IMAGE=<that tag or its @sha256 digest> bash cloud/bus-reconciler/archive.sh deploy     # flag OFF
#   bash cloud/bus-reconciler/archive.sh dryrun     # one execution that reads and reports, writes nothing
#   bash cloud/bus-reconciler/archive.sh on         # the flag
#   bash cloud/bus-reconciler/archive.sh run        # one real execution, and wait
#   bash cloud/bus-reconciler/archive.sh verify <redis-archive/<date>/<hour>/LEDGER-....json>   # re-hash every object
#   bash cloud/bus-reconciler/archive.sh schedule   # hourly at :05 UTC
#   bash cloud/bus-reconciler/archive.sh readback   # what is deployed, and the last executions
#   bash cloud/bus-reconciler/archive.sh off        # the flag; the schedule then runs it as "disabled"
#
# Rollback is `off`, never a redeploy. Nothing it wrote is removed by turning it off: the archive is the
# point. On Windows set GCLOUD to the 8.3 short path, as the other scripts here say.
set -euo pipefail

PROJECT="${PROJECT:-sfdc24}"
PROJECT_NUMBER="${PROJECT_NUMBER:-96522051727}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-redis-archive}"
SA="bus-reconciler@${PROJECT}.iam.gserviceaccount.com"
INVOKER_NAME="${INVOKER_NAME:-redis-archive-ticker}"
INVOKER="${INVOKER_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHED_JOB="${SCHED_JOB:-redis-archive-hourly}"
SCHEDULE="${SCHEDULE:-5 * * * *}"
# The build this job runs, NAMED EXPLICITLY at deploy: a tag such as bus-reconciler:git-<sha7> or a digest
# (bus-reconciler@sha256:...). Not the shared :v1 default, which may predate redis_archive.py, and whose
# rebuild could move every other job on the image with it. Build one with
#   IMAGE=<registry>/bus-reconciler:git-<sha7> bash cloud/bus-reconciler/deploy.sh build
IMAGE="${IMAGE:-}"
BUCKET="${REDIS_ARCHIVE_BUCKET:-sfdc24-fleet-state}"
PREFIX="${REDIS_ARCHIVE_PREFIX:-redis-archive}"
INSTANCE="${INSTANCE:-redis-central}"
REDIS_HOST="${REDIS_HOST:-}"
REDIS_PORT="${REDIS_PORT:-6378}"
CA_PATH="/secrets/ca/redis-ca.pem"
AUTH_PATH="/secrets/auth/redis-auth"
GCLOUD="${GCLOUD:-gcloud}"
RUN_URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"

setup() {
  "$GCLOUD" iam service-accounts describe "$INVOKER" --project "$PROJECT" >/dev/null 2>&1 ||
    "$GCLOUD" iam service-accounts create "$INVOKER_NAME" --project "$PROJECT" \
      --display-name "Redis archive ticker: Cloud Scheduler's identity to run the hourly archive"
  # Without TokenCreator for the Scheduler service agent the tick never attempts (status -1, no logs);
  # the same binding scripts/gcp_scheduler_bootstrap.py grants.
  "$GCLOUD" iam service-accounts add-iam-policy-binding "$INVOKER" --project "$PROJECT" \
    --member "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com" \
    --role roles/iam.serviceAccountTokenCreator >/dev/null
  # Create and read under the archive prefix only. objectCreator has no delete, so nothing archived can
  # be removed or replaced by this account; objectViewer is for `verify`.
  local cond="expression=resource.name.startsWith(\"projects/_/buckets/${BUCKET}/objects/${PREFIX}/\"),title=redis-archive-only"
  for role in roles/storage.objectCreator roles/storage.objectViewer; do
    "$GCLOUD" storage buckets add-iam-policy-binding "gs://${BUCKET}" --project "$PROJECT" \
      --member "serviceAccount:${SA}" --role "$role" --condition "$cond" >/dev/null
    echo "granted ${role} on gs://${BUCKET}/${PREFIX}/ to ${SA}"
  done
}

deploy() {
  : "${IMAGE:?set IMAGE to the build that carries redis_archive.py (bus-reconciler:git-<sha7> or bus-reconciler@sha256:<digest>); the shared :v1 tag is not assumed}"
  if [ -z "$REDIS_HOST" ]; then
    # Resolved here and passed straight to the job: the address is topology and this repository is public.
    REDIS_HOST="$("$GCLOUD" redis instances describe "$INSTANCE" --region "$REGION" --project "$PROJECT" --format 'value(host)')"
  fi
  : "${REDIS_HOST:?could not resolve the Memorystore address; set REDIS_HOST}"
  local verb=create
  "$GCLOUD" run jobs describe "$JOB" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1 && verb=update
  # RELATIVE script path: WORKDIR is /app, and Git Bash rewrites an absolute one (REQUESTS.md).
  "$GCLOUD" run jobs "$verb" "$JOB" --project "$PROJECT" --region "$REGION" \
    --image "$IMAGE" --service-account "$SA" \
    --command python --args=-B,redis_archive.py \
    --set-secrets "BUS_URL=BUS_URL:latest,BUS_SECRET=BUS_SECRET:latest,${CA_PATH}=REDIS_CA_CERT:latest,${AUTH_PATH}=REDIS_AUTH_STRING:latest" \
    --set-env-vars "REDIS_CONNECT=true,REDIS_HOST=${REDIS_HOST},REDIS_PORT=${REDIS_PORT},REDIS_ARCHIVE_ENABLED=off,REDIS_ARCHIVE_BUCKET=${BUCKET},REDIS_ARCHIVE_PREFIX=${PREFIX}" \
    --network default --subnet default --vpc-egress private-ranges-only \
    --max-retries 0 --task-timeout 10m --cpu 1 --memory 512Mi
  "$GCLOUD" run jobs add-iam-policy-binding "$JOB" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${INVOKER}" --role roles/run.invoker >/dev/null
  echo "job ${JOB} ${verb}d on ${IMAGE}, REDIS_ARCHIVE_ENABLED=off; ${INVOKER} may run it"
}

dryrun() {
  # An execution-level override: the job's own flag is untouched.
  "$GCLOUD" run jobs execute "$JOB" --project "$PROJECT" --region "$REGION" --wait \
    --update-env-vars "REDIS_ARCHIVE_ENABLED=on,REDIS_ARCHIVE_DRY_RUN=1"
  echo "dry run done: its log has one JSON line - streams, entries it WOULD archive, nothing written"
}

run_once() {
  "$GCLOUD" run jobs execute "$JOB" --project "$PROJECT" --region "$REGION" --wait
}

verify() {
  local ledger="${1:-}"
  case "$ledger" in
    "${PREFIX}"/*/LEDGER-*.json) ;;
    *) echo "verify <${PREFIX}/<date>/<hour>/LEDGER-<run>-<hash>.json>, the path the run printed" >&2; exit 2 ;;
  esac
  "$GCLOUD" run jobs execute "$JOB" --project "$PROJECT" --region "$REGION" --wait \
    --args="-B,redis_archive.py,verify,${ledger}"
  echo "the execution's log says ok:true and mismatched:[] when every object hashes to its ledger"
}

schedule() {
  local verb=create
  "$GCLOUD" scheduler jobs describe "$SCHED_JOB" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1 && verb=update
  "$GCLOUD" scheduler jobs "$verb" http "$SCHED_JOB" --project "$PROJECT" --location "$REGION" \
    --schedule "$SCHEDULE" --time-zone "Etc/UTC" --http-method POST --uri "$RUN_URI" \
    --oauth-service-account-email "$INVOKER" \
    --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform" \
    --attempt-deadline 60s
  echo "scheduler ${SCHED_JOB} ${verb}d: '${SCHEDULE}' UTC -> run ${JOB} as ${INVOKER}"
}

readback() {
  # The env in whichever shape gcloud answers (v1 spec.template... or v2 template.template...); the
  # address is never printed.
  "$GCLOUD" run jobs describe "$JOB" --project "$PROJECT" --region "$REGION" --format json | python -c '
import json, sys
def find(o):
    if isinstance(o, dict):
        if "containers" in o:
            return o
        for v in o.values():
            r = find(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find(v)
            if r:
                return r
spec = find(json.load(sys.stdin)) or {}
c = (spec.get("containers") or [{}])[0]
print("image:", c.get("image"))
print("service account:", spec.get("serviceAccountName") or spec.get("serviceAccount"))
for e in c.get("env", []):
    n = e.get("name")
    print(" ", n, "=", "(hidden)" if n == "REDIS_HOST" else e.get("value", "(from a secret)"))
'
  "$GCLOUD" scheduler jobs describe "$SCHED_JOB" --project "$PROJECT" --location "$REGION" \
    --format 'yaml(schedule,timeZone,state,lastAttemptTime,status)' || true
  "$GCLOUD" run jobs executions list --job "$JOB" --project "$PROJECT" --region "$REGION" --limit 5 || true
}

flag() {
  "$GCLOUD" run jobs update "$JOB" --project "$PROJECT" --region "$REGION" --update-env-vars "REDIS_ARCHIVE_ENABLED=$1"
  echo "REDIS_ARCHIVE_ENABLED=$1 on ${JOB}"
}

case "${1:-}" in
  setup) setup ;;
  deploy) deploy ;;
  dryrun) dryrun ;;
  run) run_once ;;
  verify) verify "${2:-}" ;;
  schedule) schedule ;;
  readback) readback ;;
  on) flag on ;;
  off) flag off ;;
  *) echo "usage: $0 setup|deploy|dryrun|run|verify <ledger>|schedule|readback|on|off" >&2; exit 2 ;;
esac

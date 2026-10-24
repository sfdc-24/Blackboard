#!/usr/bin/env bash
# The public canvas bucket and the worker's one grant on it. NOBODY HAS RUN THIS. Each step needs the
# owner's own go, at a reviewed head (spec v0.2, "What needs the owner's go", items 1 and 2).
#
#   bash cloud/bus-reconciler/canvas-bucket.sh bucket    # the bucket: public read, CORS for sfdc24.com
#   bash cloud/bus-reconciler/canvas-bucket.sh grant     # objectUser for bus-reconciler, on this bucket only
#   bash cloud/bus-reconciler/canvas-bucket.sh env       # CANVAS_BUCKET on the bus-requests job
#   bash cloud/bus-reconciler/canvas-bucket.sh readback  # what is actually there, printed
#   bash cloud/bus-reconciler/canvas-bucket.sh off       # remove CANVAS_BUCKET and the grant (the files stay)
#
# WHY objectUser AND NOT objectCreator. The publisher rewrites rooms/<room>.json in place, and overwriting an
# object needs storage.objects.delete as well as create. objectCreator alone would answer 403 on the second
# write of every room. The grant is on this one bucket, never on the project.
#
# WHAT IS PUBLIC. Everything in this bucket, by design: allUsers may read it. Only the publisher writes it,
# and the publisher writes only items an agent marked public:true (scripts/canvas_publish.py).
#
# UNKNOWN until `bucket` runs: whether an organisation policy refuses allUsers on a bucket in this project.
set -euo pipefail

PROJECT="${PROJECT:-sfdc24}"
REGION="${REGION:-us-central1}"
# Toronto, the fleet's preferred region. The bucket holds public JSON only; the job reads Redis in us-central1.
LOCATION="${LOCATION:-northamerica-northeast2}"
BUCKET="${CANVAS_BUCKET:-sfdc24-canvas}"
JOB="${JOB:-bus-requests}"
SA="bus-reconciler@${PROJECT}.iam.gserviceaccount.com"
GCLOUD="${GCLOUD:-gcloud}"
CORS_FILE="$(mktemp)"
trap 'rm -f "$CORS_FILE"' EXIT
cat > "$CORS_FILE" <<'JSON'
[{"origin": ["https://sfdc24.com", "https://www.sfdc24.com"], "method": ["GET"], "responseHeader": ["Content-Type"], "maxAgeSeconds": 300}]
JSON

case "${1:-}" in
  bucket)
    "$GCLOUD" storage buckets create "gs://${BUCKET}" --project "$PROJECT" --location "$LOCATION" \
      --uniform-bucket-level-access --no-public-access-prevention
    "$GCLOUD" storage buckets update "gs://${BUCKET}" --cors-file "$CORS_FILE"
    "$GCLOUD" storage buckets add-iam-policy-binding "gs://${BUCKET}" \
      --member allUsers --role roles/storage.objectViewer
    ;;
  grant)
    "$GCLOUD" storage buckets add-iam-policy-binding "gs://${BUCKET}" \
      --member "serviceAccount:${SA}" --role roles/storage.objectUser
    ;;
  env)
    "$GCLOUD" run jobs update "$JOB" --project "$PROJECT" --region "$REGION" \
      --update-env-vars "CANVAS_BUCKET=${BUCKET}"
    ;;
  readback)
    "$GCLOUD" storage buckets describe "gs://${BUCKET}" --format "yaml(location,iamConfiguration,cors_config)"
    "$GCLOUD" storage buckets get-iam-policy "gs://${BUCKET}" --format "yaml(bindings)"
    "$GCLOUD" run jobs describe "$JOB" --project "$PROJECT" --region "$REGION" \
      --format "value(spec.template.spec.template.spec.containers[0].env)" | tr ';' '\n' | grep -i canvas || echo "CANVAS_BUCKET not set"
    ;;
  off)
    "$GCLOUD" run jobs update "$JOB" --project "$PROJECT" --region "$REGION" --remove-env-vars CANVAS_BUCKET
    "$GCLOUD" storage buckets remove-iam-policy-binding "gs://${BUCKET}" \
      --member "serviceAccount:${SA}" --role roles/storage.objectUser
    ;;
  *)
    echo "usage: $0 bucket|grant|env|readback|off" >&2
    exit 2
    ;;
esac

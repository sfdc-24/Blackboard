#!/usr/bin/env bash
# DO NOT RUN THIS YET. The owner's GO is explicit: "Deploy, new SA and IAM only AFTER Codex verifies
# the source." This file is part of what Codex is being asked to verify.
#
# Locked decision 1 (GROK-REDIS-BRIDGE-DECISIONS-20261006T1337Z): "deploy style: gcloud script plus
# code PR now, Terraform later." So this is a gcloud script with a read-back, like every other
# deploy in this fleet, and NOT Terraform - there is no Terraform state anywhere here, and
# introducing a state file that nothing else uses, for one service and two bindings, is an
# architectural decision rather than a free choice.
#
# IT IS ALSO A RE-DEPLOY, NOT A FIRST ONE. redis-tool-bridge-00001-hsg is already serving from image
# digest cd88ac6cc786 whose source is in no repository. The values below were READ BACK from that
# running service on 2026-10-07 so that this script reproduces it rather than silently changing it;
# every line that differs from what is deployed is called out as CHANGE.
set -euo pipefail

PROJECT=sfdc24
REGION=us-central1
SERVICE=redis-tool-bridge
RUNTIME_SA="redis-tool-bridge@${PROJECT}.iam.gserviceaccount.com"   # exists already; dedicated
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}:reviewed"

# WHO MAY CALL IT. Named service accounts, never allUsers and never allAuthenticatedUsers.
# aya-runtime is the only invoker on the running service today, read back from its IAM policy.
INVOKERS=("serviceAccount:aya-runtime@${PROJECT}.iam.gserviceaccount.com")

usage() {
  cat <<'TXT'
usage: deploy.sh build | plan | deploy | readback

  build     submit the image from the repo root (cloudbuild.yaml), nothing deployed
  plan      print every command this script WOULD run, and exit. Run this first.
  deploy    create/replace the revision. REQUIRES Codex source verification first.
  readback  describe the live service and its IAM policy, and diff nothing - just show it

Deliberately no "all". A deploy that also builds hides which artefact is running.
TXT
}

build() {
  gcloud builds submit --project="${PROJECT}" --region="${REGION}" \
    --config cloud/redis-tool-bridge/cloudbuild.yaml .
  echo "NOW RECORD THE DIGEST. A tag is mutable; the digest is the only thing that identifies what"
  echo "ran. 'bus-reconciler:v1' is shared by eight jobs and rebuilding it re-pointed all of them."
}

deploy_cmds() {
  # BRIDGE_AUDIENCE must be the service's own https URL. Chicken-and-egg on a FIRST deploy: the URL
  # is not known until the service exists. It is known here, because the service already exists, and
  # it is read back rather than guessed.
  local url
  url="$(gcloud run services describe "${SERVICE}" --project="${PROJECT}" --region="${REGION}" \
          --format='value(status.url)')"
  cat <<CMD
gcloud run deploy ${SERVICE} \\
  --project=${PROJECT} --region=${REGION} \\
  --image=${IMAGE} \\
  --service-account=${RUNTIME_SA} \\
  --no-allow-unauthenticated \\
  --ingress=all \\
  --network=default --subnet=default --vpc-egress=private-ranges-only \\
  --set-secrets=/secrets/auth/redis-auth=REDIS_AUTH_STRING:2,/secrets/ca/redis-ca.pem=REDIS_CA_CERT:1 \\
  --set-env-vars=REDIS_HOST=10.54.72.180,REDIS_PORT=6378 \\
  --set-env-vars=REDIS_AUTH_FILE=/secrets/auth/redis-auth,REDIS_CA_CERT_PATH=/secrets/ca/redis-ca.pem \\
  --set-env-vars=BRIDGE_AUDIENCE=${url} \\
  --set-env-vars=BRIDGE_CALLERS=aya-runtime@${PROJECT}.iam.gserviceaccount.com
CMD
  # --no-allow-unauthenticated is the platform half of defect 3. The application allowlist
  # (BRIDGE_CALLERS) is the half this repository can review. Both, independently.
  #
  # --ingress=all is DELIBERATE and is a correction to my own 2026-10-06 note, which called it a
  # defect. Aya calls from a cloud workspace outside this VPC, so internal-and-cloud-load-balancing
  # would make the service unreachable by its only caller. Reachability is not authorisation here:
  # IAM refuses an unauthenticated request before it reaches the container.
  #
  # TWO SECRETS, TWO DIRECTORIES, AS FILES. Defect 4. Never --set-secrets into env vars: an env var
  # is listable from anything that can read the process.
  for member in "${INVOKERS[@]}"; do
    echo "gcloud run services add-iam-policy-binding ${SERVICE} --project=${PROJECT} --region=${REGION} --member=${member} --role=roles/run.invoker"
  done
}

readback() {
  gcloud run services describe "${SERVICE}" --project="${PROJECT}" --region="${REGION}" \
    --format='yaml(spec.template.spec.serviceAccountName,spec.template.spec.containers[0].image,spec.template.spec.containers[0].volumeMounts,metadata.annotations)'
  echo "--- who may invoke it:"
  gcloud run services get-iam-policy "${SERVICE}" --project="${PROJECT}" --region="${REGION}"
  echo "--- NOW CHECK THESE BY EYE, because a green deploy is not a correct one:"
  echo "    * no allUsers and no allAuthenticatedUsers in run.invoker"
  echo "    * both secrets appear under volumeMounts, NOT under env"
  echo "    * BRIDGE_AUDIENCE equals status.url exactly"
  echo "    * vpc-access-egress is private-ranges-only"
}

case "${1:-}" in
  build)    build ;;
  plan)     deploy_cmds ;;
  deploy)   deploy_cmds | bash -s ;;
  readback) readback ;;
  *)        usage; exit 2 ;;
esac

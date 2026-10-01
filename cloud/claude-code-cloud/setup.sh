#!/usr/bin/env bash
# Stage C1 infrastructure for the Console route (Blackboard #306): a DRAFT that prints every command by default.
#
#   bash cloud/claude-code-cloud/setup.sh            # DRY RUN: prints the plan and changes nothing
#   CCC_OWNER_GO=<board Row_ID> bash cloud/claude-code-cloud/setup.sh --apply
#
# --apply refuses unless CCC_OWNER_GO names the owner's GO row for this stage. Codex's AGREE on #306 and the owner's
# Console key are both prerequisites; this script cannot check them, so the GO row is where the owner confirms both.
# Every step is additive and idempotent (describe-or-create). Nothing here deletes, and nothing touches an existing
# runtime. C2 resources (the GitHub App keys' accessors) are listed but NOT applied in C1.
set -euo pipefail

PROJECT="${CCC_PROJECT:-sfdc24}"
REGION="${CCC_REGION:-us-central1}"
JOB_SA="claude-code-cloud@${PROJECT}.iam.gserviceaccount.com"
BROKER_SA="ccc-broker@${PROJECT}.iam.gserviceaccount.com"
REG="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"

APPLY=0
if [ "${1:-}" = "--apply" ]; then
  if [ -z "${CCC_OWNER_GO:-}" ]; then
    echo "REFUSED: --apply needs CCC_OWNER_GO=<the owner's GO Row_ID for Stage C1>; nothing was changed." >&2
    exit 2
  fi
  APPLY=1
elif [ -n "${1:-}" ]; then
  echo "usage: setup.sh [--apply]" >&2
  exit 2
fi

step() {   # step <description> -- <command...>
  local what="$1"; shift; shift
  if [ "$APPLY" = 1 ]; then
    echo "APPLY: $what"
    "$@"
  else
    printf 'DRY RUN: %s\n    %s\n' "$what" "$*"
  fi
}

echo "Stage C1, project ${PROJECT}, region ${REGION}. $([ "$APPLY" = 1 ] && echo "APPLYING under GO ${CCC_OWNER_GO}" || echo "DRY RUN: nothing is changed")"

# 1. Two service accounts, each with no project-level role.
step "service account for the agent job" -- \
  gcloud iam service-accounts create claude-code-cloud --project "$PROJECT" \
    --display-name "claude-code-cloud (Console agent job, Blackboard #306)"
step "service account for the broker" -- \
  gcloud iam service-accounts create ccc-broker --project "$PROJECT" \
    --display-name "ccc-broker (board and GitHub writes for the agent, Blackboard #306)"

# 2. Secrets: names, and exactly one reader each (secret-level grants, never project-level).
#    The VALUES are put in by the owner in his own terminal; this script never handles one.
step "Console API key secret (the owner adds its value: workspace fleet-claude-cloud, with a spend limit)" -- \
  gcloud secrets create ANTHROPIC_API_KEY_CLOUD --project "$PROJECT" --replication-policy automatic
step "only the agent job reads the Console key" -- \
  gcloud secrets add-iam-policy-binding ANTHROPIC_API_KEY_CLOUD --project "$PROJECT" \
    --member "serviceAccount:${JOB_SA}" --role roles/secretmanager.secretAccessor
for s in BUS_URL BUS_SECRET; do
  step "the broker reads ${s} (the agent job never does)" -- \
    gcloud secrets add-iam-policy-binding "$s" --project "$PROJECT" \
      --member "serviceAccount:${BROKER_SA}" --role roles/secretmanager.secretAccessor
done
echo "C2 ONLY, not applied in C1: GITHUB_APP_READONLY_PRIVATE_KEY (App 5148538) -> ${JOB_SA}; GITHUB_APP_BROKER_PRIVATE_KEY (App 5148612) -> ${BROKER_SA}"

# 3. Receipts: a SEPARATE named Firestore database in Toronto (owner's rule: Canadian regions first). The broker is
#    granted it alone, through an IAM condition on the database name, so it can never touch the (default) database
#    where the chair keeps its call checkpoints.
step "Firestore database for broker receipts (Toronto)" -- \
  gcloud firestore databases create --project "$PROJECT" --database ccc-receipts \
    --location northamerica-northeast2 --type firestore-native
step "the broker may use only the ccc-receipts database" -- \
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${BROKER_SA}" --role roles/datastore.user \
    --condition "title=ccc-receipts-only,expression=resource.name.startsWith(\"projects/${PROJECT}/databases/ccc-receipts\")"

# 4. The broker: private service, C1 operation post_receipt only. The image is built from a merged main SHA.
step "deploy ccc-broker (no public invoker)" -- \
  gcloud run deploy ccc-broker --project "$PROJECT" --region "$REGION" \
    --image "${REG}/ccc-broker:${CCC_BROKER_TAG:-UNSET}" --service-account "$BROKER_SA" \
    --no-allow-unauthenticated --ingress all --min-instances 0 --max-instances 2 \
    --set-env-vars CCC_STAGE=C1 \
    --set-secrets BUS_URL=BUS_URL:latest,BUS_SECRET=BUS_SECRET:latest
step "only the agent job may call the broker" -- \
  gcloud run services add-iam-policy-binding ccc-broker --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${JOB_SA}" --role roles/run.invoker

# 5. The agent job: max-retries 0, one task, bounded time. In C1 the only tool is post_receipt (#306, Boundary 3).
step "create the claude-code-cloud job" -- \
  gcloud run jobs create claude-code-cloud --project "$PROJECT" --region "$REGION" \
    --image "${REG}/claude-code-cloud:${CCC_JOB_TAG:-UNSET}" --service-account "$JOB_SA" \
    --max-retries 0 --tasks 1 --task-timeout 600s --memory 1Gi --cpu 1 \
    --set-env-vars CCC_STAGE=C1,CCC_MAX_TURNS=4,CCC_MAX_BUDGET_USD=0.50 \
    --set-secrets ANTHROPIC_API_KEY=ANTHROPIC_API_KEY_CLOUD:latest

# 6. board-watcher may start the new job. Its identity is read live, because the service-account plan may
#    have moved it to its own account by then.
WATCHER_SA="$(gcloud run jobs describe board-watcher --project "$PROJECT" --region "$REGION" \
  --format='value(spec.template.spec.template.spec.serviceAccountName)' 2>/dev/null || echo '<read live at apply time>')"
step "board-watcher (${WATCHER_SA}) may run the claude-code-cloud job" -- \
  gcloud run jobs add-iam-policy-binding claude-code-cloud --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${WATCHER_SA}" --role roles/run.invoker

echo "Not in this script, by design:"
echo "  - the board-watcher claude-code-cloud route: a code PR, reviewed and released on its own;"
echo "  - the job and broker images: built from a merged main SHA;"
echo "  - any Console key value, and any GitHub App key, which the owner stores himself."

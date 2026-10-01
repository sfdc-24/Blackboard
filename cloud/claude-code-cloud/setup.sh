#!/usr/bin/env bash
# Stage C1 infrastructure for the Console route (Blackboard #306): a DRAFT that prints every command by default.
#
#   bash cloud/claude-code-cloud/setup.sh            # DRY RUN: reads, prints the plan, changes nothing
#   CCC_OWNER_GO=<board Row_ID> CCC_CURSOR_GO_SHA=<40-hex commit> bash cloud/claude-code-cloud/setup.sh --apply
#
# --apply refuses, before any cloud call, unless:
#   - CCC_OWNER_GO names the owner's GO row for this stage (where he confirms Codex's AGREE on #306 and his Console
#     key, which this script cannot check);
#   - CCC_CURSOR_GO_SHA is the full commit Cursor gave an exact-head GO, it is this checkout's HEAD, and this script
#     is unmodified from it (#306: an exact-head GO on every code PR before deployment).
# Then every read runs before the first change (create-or-refuse): each resource this script creates must be ABSENT,
# each one it grants on must be PRESENT, and board-watcher's identity must resolve. Anything else - present, missing,
# or a read that fails for another reason - refuses with nothing changed. So a rerun after a partial apply refuses
# too: the owner looks at what exists before anything more is created. Nothing here deletes, and no existing runtime
# is modified; the only change to existing resources is an added reader on BUS_URL and BUS_SECRET. C2 resources
# (the GitHub App keys' readers) are listed but NOT applied in C1.
set -euo pipefail

PROJECT="${CCC_PROJECT:-sfdc24}"
REGION="${CCC_REGION:-us-central1}"
JOB_SA="claude-code-cloud@${PROJECT}.iam.gserviceaccount.com"
BROKER_SA="ccc-broker@${PROJECT}.iam.gserviceaccount.com"
REG="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APPLY=0
if [ "${1:-}" = "--apply" ]; then
  if [ -z "${CCC_OWNER_GO:-}" ]; then
    echo "REFUSED: --apply needs CCC_OWNER_GO=<the owner's GO Row_ID for Stage C1>; nothing was changed." >&2
    exit 2
  fi
  if ! [[ "${CCC_CURSOR_GO_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "REFUSED: --apply needs CCC_CURSOR_GO_SHA=<the full commit Cursor gave GO>; nothing was changed." >&2
    exit 2
  fi
  HEAD_SHA="$(git -C "$HERE" rev-parse HEAD 2>/dev/null || true)"
  if [ "$HEAD_SHA" != "$CCC_CURSOR_GO_SHA" ]; then
    echo "REFUSED: this checkout is at ${HEAD_SHA:-no commit}, not Cursor's GO ${CCC_CURSOR_GO_SHA}; nothing was changed." >&2
    exit 2
  fi
  if ! git -C "$HERE" diff --quiet HEAD -- "$HERE/setup.sh"; then
    echo "REFUSED: setup.sh differs from ${CCC_CURSOR_GO_SHA}, the commit Cursor reviewed; nothing was changed." >&2
    exit 2
  fi
  APPLY=1
elif [ -n "${1:-}" ]; then
  echo "usage: setup.sh [--apply]" >&2
  exit 2
fi

echo "Stage C1, project ${PROJECT}, region ${REGION}. $([ "$APPLY" = 1 ] && echo "APPLYING under GO ${CCC_OWNER_GO} at ${CCC_CURSOR_GO_SHA}" || echo "DRY RUN: nothing is changed")"

# 0. Every read, before any change.
state() {   # state <describe command...>: PRESENT, ABSENT (gcloud itself said not found) or UNKNOWN
  local err
  if err="$("$@" 2>&1 >/dev/null)"; then
    echo PRESENT
  elif grep -q '(gcloud\.' <<<"$err" && grep -qiE 'NOT_FOUND|not found|could not be found|cannot find|does not exist' <<<"$err"; then
    echo ABSENT
  else
    echo UNKNOWN
  fi
}
PROBLEMS=()
expect() {  # expect <ABSENT|PRESENT> <what> <describe command...>
  local want="$1" what="$2"; shift 2
  local got
  got="$(state "$@")"
  echo "READ: ${what}: ${got} (needs ${want})"
  [ "$got" = "$want" ] || PROBLEMS+=("${what} is ${got}, needs ${want}")
}
expect ABSENT "service account claude-code-cloud" gcloud iam service-accounts describe "$JOB_SA" --project "$PROJECT"
expect ABSENT "service account ccc-broker" gcloud iam service-accounts describe "$BROKER_SA" --project "$PROJECT"
expect ABSENT "secret ANTHROPIC_API_KEY_CLOUD" gcloud secrets describe ANTHROPIC_API_KEY_CLOUD --project "$PROJECT"
expect PRESENT "secret BUS_URL" gcloud secrets describe BUS_URL --project "$PROJECT"
expect PRESENT "secret BUS_SECRET" gcloud secrets describe BUS_SECRET --project "$PROJECT"
expect ABSENT "Firestore database ccc-receipts" gcloud firestore databases describe --database ccc-receipts --project "$PROJECT"
expect ABSENT "Cloud Run service ccc-broker" gcloud run services describe ccc-broker --project "$PROJECT" --region "$REGION"
expect ABSENT "Cloud Run job claude-code-cloud" gcloud run jobs describe claude-code-cloud --project "$PROJECT" --region "$REGION"
# board-watcher's identity is read live, because the service-account plan may have moved it to its own account.
WATCHER_SA="$(gcloud run jobs describe board-watcher --project "$PROJECT" --region "$REGION" \
  --format='value(spec.template.spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
SA_SHAPE='^[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$|^[0-9]+-compute@developer\.gserviceaccount\.com$'
if [[ "$WATCHER_SA" =~ $SA_SHAPE ]]; then
  echo "READ: board-watcher runs as ${WATCHER_SA}"
else
  PROBLEMS+=("board-watcher's identity did not resolve (read: '${WATCHER_SA}')")
  WATCHER_SA="<board-watcher identity: unresolved>"
fi
if [ "${#PROBLEMS[@]}" -gt 0 ]; then
  for p in "${PROBLEMS[@]}"; do echo "PREFLIGHT: $p" >&2; done
  if [ "$APPLY" = 1 ]; then
    echo "REFUSED: the preflight reads do not match a clean C1 start; nothing was changed." >&2
    exit 3
  fi
  echo "DRY RUN: --apply would REFUSE on the preflight above." >&2
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
#    granted that database alone, by an IAM condition on its exact name (Google's per-database form; a prefix would
#    also cover ccc-receipts-backup and the like), so it can never touch the (default) database where the chair keeps
#    its call checkpoints.
step "Firestore database for broker receipts (Toronto)" -- \
  gcloud firestore databases create --project "$PROJECT" --database ccc-receipts \
    --location northamerica-northeast2 --type firestore-native
step "the broker may use only the ccc-receipts database" -- \
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${BROKER_SA}" --role roles/datastore.user \
    --condition "title=ccc-receipts-only,expression=resource.name==\"projects/${PROJECT}/databases/ccc-receipts\""

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

# 6. board-watcher (resolved in step 0) may start the new job.
step "board-watcher (${WATCHER_SA}) may run the claude-code-cloud job" -- \
  gcloud run jobs add-iam-policy-binding claude-code-cloud --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${WATCHER_SA}" --role roles/run.invoker

echo "Not in this script, by design:"
echo "  - the board-watcher claude-code-cloud route: a code PR, reviewed and released on its own;"
echo "  - the job and broker images: built from a merged main SHA;"
echo "  - any Console key value, and any GitHub App key, which the owner stores himself."

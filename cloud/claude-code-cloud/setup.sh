#!/usr/bin/env bash
# Stage C1 infrastructure for the Console route (Blackboard #306): a DRAFT that prints every command by default.
#
#   bash cloud/claude-code-cloud/setup.sh            # DRY RUN: reads, prints the plan, changes nothing
#   CCC_OWNER_GO=<board Row_ID> CCC_CURSOR_GO_SHA=<40-hex commit> \
#     CCC_BROKER_TAG=<40-hex SHA> CCC_JOB_TAG=<40-hex SHA> bash cloud/claude-code-cloud/setup.sh --apply
#
# --apply refuses, before any cloud call, unless:
#   - CCC_OWNER_GO names the owner's GO row for this stage (where he confirms Codex's AGREE on #306 and his Console
#     key, which this script cannot check);
#   - CCC_CURSOR_GO_SHA is the full commit Cursor gave an exact-head GO, it is this checkout's HEAD, and this script
#     is unmodified from it (#306: an exact-head GO on every code PR before deployment).
# Then every read runs before the first change (create-or-refuse): each resource this script creates must be ABSENT;
# each secret it grants on (ANTHROPIC_API_KEY_CLOUD, which the owner creates with its value first, BUS_URL and
# BUS_SECRET) must be PRESENT with an enabled latest version; both images must be tagged with a full commit SHA and
# pushed (CCC_BROKER_TAG, CCC_JOB_TAG: the merged main SHA each was built from); the project number must resolve (it
# names the broker's URL); and board-watcher's identity must resolve. Anything else -
# present, missing, or a read that fails for another reason - refuses with nothing changed. So a rerun after a
# partial apply refuses too: the owner looks at what exists before anything more is created. Nothing here deletes,
# and no existing runtime is modified. Changes to existing resources: a reader added on each of the three secrets,
# and one conditional binding (datastore.user, ccc-receipts only) added to the project's IAM policy. The receipts
# live in a new ccc-receipts database, not a collection in the existing (default) one as #306 rev 5 says: see the
# README. C2 resources
# (the GitHub App keys' readers) are listed but NOT applied in C1.
set -euo pipefail

PROJECT="${CCC_PROJECT:-sfdc24}"
REGION="${CCC_REGION:-us-central1}"
JOB_SA="claude-code-cloud@${PROJECT}.iam.gserviceaccount.com"
BROKER_SA="ccc-broker@${PROJECT}.iam.gserviceaccount.com"
REG="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APPLY=0
PRINT_FROM=1
if [ "${1:-}" = "--print-from" ]; then
  # After a failed apply: print step N and every step after it, in order, and change nothing (Codex P1 and Copilot on
  # 5a98651). The owner checks the failed resource first, then runs these by hand.
  if ! [[ "${2:-}" =~ ^[1-9][0-9]*$ ]]; then echo "usage: setup.sh --print-from <step number>" >&2; exit 2; fi
  PRINT_FROM="$2"
elif [ "${1:-}" = "--apply" ]; then
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
  echo "usage: setup.sh [--apply | --print-from <step>]" >&2
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
# Each secret a runtime binds as :latest must already hold an enabled version: Cloud Run checks it at deploy time
# (Copilot on 21ff80e). The owner creates ANTHROPIC_API_KEY_CLOUD with its value first; only the version's state is
# read here, never a value.
for s in ANTHROPIC_API_KEY_CLOUD BUS_URL BUS_SECRET; do
  expect PRESENT "secret ${s}" gcloud secrets describe "$s" --project "$PROJECT"
  version="$(gcloud secrets versions describe latest --secret "$s" --project "$PROJECT" --format='value(state)' 2>/dev/null || true)"
  echo "READ: secret ${s} latest version: ${version:-none}"
  [ "$version" = "ENABLED" ] || PROBLEMS+=("secret ${s} has no enabled latest version (read: '${version}')")
done
expect ABSENT "Firestore database ccc-receipts" gcloud firestore databases describe --database ccc-receipts --project "$PROJECT"
expect ABSENT "Cloud Run service ccc-broker" gcloud run services describe ccc-broker --project "$PROJECT" --region "$REGION"
expect ABSENT "Cloud Run job claude-code-cloud" gcloud run jobs describe claude-code-cloud --project "$PROJECT" --region "$REGION"
# The job is told the broker's URL when it is created (Copilot on 80d4820): Cloud Run injects no other service's URL,
# and the job's account has no role that could look it up. A service's deterministic URL is
# https://<service>-<project number>.<region>.run.app, and the job uses it as its ID-token audience too.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)' 2>/dev/null || true)"
if [[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]]; then
  BROKER_URL="https://ccc-broker-${PROJECT_NUMBER}.${REGION}.run.app"
  echo "READ: project ${PROJECT} is number ${PROJECT_NUMBER}; the broker will be ${BROKER_URL}"
else
  PROBLEMS+=("project ${PROJECT}'s number did not resolve (read: '${PROJECT_NUMBER}')")
  BROKER_URL="<broker URL: project number unresolved>"
fi
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
# Both images must be tagged with a full commit SHA and already pushed (Codex P1 on 21ff80e: an UNSET tag failed the
# deploy only after the accounts, secret and database were created; Codex and Cursor on 80d4820: `latest`, `v1` or an
# older release must not pass). The shape rejects every non-SHA tag; it does not prove the SHA is on main.
TAG_SHAPE='^[0-9a-f]{40}$'
for image in "ccc-broker CCC_BROKER_TAG" "claude-code-cloud CCC_JOB_TAG"; do
  name="${image% *}"; var="${image#* }"; tag="${!var:-}"
  if [[ "$tag" =~ $TAG_SHAPE ]]; then
    expect PRESENT "image ${name}:${tag}" gcloud artifacts docker images describe "${REG}/${name}:${tag}"
  else
    echo "READ: image ${name}: no valid tag in ${var}"
    PROBLEMS+=("image ${name} has no full commit SHA tag: set ${var} to the 40-hex merged main SHA it was built from")
  fi
done
if [ "${#PROBLEMS[@]}" -gt 0 ]; then
  for p in "${PROBLEMS[@]}"; do echo "PREFLIGHT: $p" >&2; done
  if [ "$APPLY" = 1 ]; then
    echo "REFUSED: the preflight reads do not match a clean C1 start; nothing was changed." >&2
    exit 3
  fi
  echo "DRY RUN: --apply would REFUSE on the preflight above." >&2
fi

# Every step is numbered in order, so a failed apply can say exactly where it stopped and print the rest.
STEP_N=0
failed_at() {   # failed_at <description> <how to check it>
  echo "FAILED at step ${STEP_N}: $1. Steps 1 to $((STEP_N - 1)) are applied; nothing after this one ran." >&2
  echo "The server may or may not have applied step ${STEP_N}. Check it first: $2" >&2
  echo "Then finish in order with the commands this prints (step ${STEP_N} onward):" >&2
  echo "  bash cloud/claude-code-cloud/setup.sh --print-from ${STEP_N}" >&2
  exit 1
}

step() {   # step <description> <how to check it> -- <command...>   (runs once; never retried)
  local what="$1" check="$2"; shift 3
  STEP_N=$((STEP_N + 1))
  if [ "$APPLY" = 1 ]; then
    echo "APPLY: [${STEP_N}] $what"
    "$@" || failed_at "$what" "$check"
  elif [ "$STEP_N" -ge "$PRINT_FROM" ]; then
    printf 'DRY RUN: [%s] %s\n    %s\n' "$STEP_N" "$what" "$*"
  fi
}

# New service accounts and new grants take minutes to take effect everywhere (IAM is eventually consistent). A step
# that names a new account, or reads a new grant, can be refused meanwhile: a binding on a just-created account
# (Codex P1 on 5a98651), and the deploy and job create that read the new secret grants (Copilot on b6fa11e). Those
# steps are retried after each wait in CCC_IAM_WAITS (seconds), but ONLY on an error that comes before any change
# (IAM_PENDING below). Any other failure, such as a lost answer that may have followed a change, stops at once with
# failed_at, because repeating a create there could hide a partial deployment (Copilot on 5a98651). Bindings are also
# idempotent. Nothing else is retried.
IAM_WAITS="${CCC_IAM_WAITS-30 60 90 120 180}"   # unset: the default; set but empty: refused
[[ "$IAM_WAITS" =~ ^[0-9]+( [0-9]+)*$ ]] || { echo "REFUSED: CCC_IAM_WAITS must be seconds separated by spaces." >&2; exit 2; }
IAM_PENDING='PERMISSION_DENIED|[Pp]ermission .* denied|[Ss]ervice account .* does not exist|does not exist\. Please verify|INVALID_ARGUMENT: .*(member|[Ss]ervice account)'
step_after_iam() {   # step_after_iam <description> <how to check it> -- <command...>
  local what="$1" check="$2"; shift 3
  STEP_N=$((STEP_N + 1))
  if [ "$APPLY" != 1 ]; then
    if [ "$STEP_N" -ge "$PRINT_FROM" ]; then
      printf 'DRY RUN: [%s] %s\n    %s\n    (retried after %s s, only while IAM is still propagating)\n' \
        "$STEP_N" "$what" "$*" "$IAM_WAITS"
    fi
    return 0
  fi
  echo "APPLY: [${STEP_N}] $what"
  local wait out
  for wait in $IAM_WAITS ""; do
    if out="$("$@" 2>&1)"; then printf '%s\n' "$out"; return 0; fi
    printf '%s\n' "$out" >&2
    if ! grep -qE "$IAM_PENDING" <<<"$out"; then failed_at "$what" "$check"; fi
    [ -n "$wait" ] || break
    echo "APPLY: [${STEP_N}] $what was refused while IAM propagates. Retrying in ${wait}s." >&2
    sleep "$wait"
  done
  failed_at "$what (still refused after waits of ${IAM_WAITS} s)" "$check"
}

# 1. Two service accounts. Neither gets an unconditional project-level role; the broker's one project-policy binding
#    is conditioned on a single database (step 3).
step "service account for the agent job" "gcloud iam service-accounts describe ${JOB_SA} --project ${PROJECT}" -- \
  gcloud iam service-accounts create claude-code-cloud --project "$PROJECT" \
    --display-name "claude-code-cloud (Console agent job, Blackboard #306)"
step "service account for the broker" "gcloud iam service-accounts describe ${BROKER_SA} --project ${PROJECT}" -- \
  gcloud iam service-accounts create ccc-broker --project "$PROJECT" \
    --display-name "ccc-broker (board and GitHub writes for the agent, Blackboard #306)"

# 2. Secrets: one ADDED reader each (secret-level grants, never project-level). This neither reads nor removes any
#    other binding, so it cannot show the reader is the only one; BUS_URL and BUS_SECRET already have other readers.
#    The owner creates each secret and its value in his own terminal before this runs (the preflight checks it);
#    this script never handles a value.
step_after_iam "add the agent job as a reader of the Console key" "gcloud secrets get-iam-policy ANTHROPIC_API_KEY_CLOUD --project ${PROJECT}" -- \
  gcloud secrets add-iam-policy-binding ANTHROPIC_API_KEY_CLOUD --project "$PROJECT" \
    --member "serviceAccount:${JOB_SA}" --role roles/secretmanager.secretAccessor
for s in BUS_URL BUS_SECRET; do
  step_after_iam "add the broker as a reader of ${s} (not granted to the agent job)" "gcloud secrets get-iam-policy ${s} --project ${PROJECT}" -- \
    gcloud secrets add-iam-policy-binding "$s" --project "$PROJECT" \
      --member "serviceAccount:${BROKER_SA}" --role roles/secretmanager.secretAccessor
done
echo "C2 ONLY, not applied in C1: GITHUB_APP_READONLY_PRIVATE_KEY (App 5148538) -> a separate clone identity, never ${JOB_SA} (Blackboard #309); GITHUB_APP_BROKER_PRIVATE_KEY (App 5148612) -> ${BROKER_SA}"

# 3. Receipts: a SEPARATE named Firestore database in Toronto (owner's rule: Canadian regions first). The broker is
#    granted that database alone, by an IAM condition on its exact name (Google's per-database form; a prefix would
#    also cover ccc-receipts-backup and the like), so it can never touch the (default) database where the chair keeps
#    its call checkpoints.
step "Firestore database for broker receipts (Toronto)" "gcloud firestore databases describe --database ccc-receipts --project ${PROJECT}" -- \
  gcloud firestore databases create --project "$PROJECT" --database ccc-receipts \
    --location northamerica-northeast2 --type firestore-native
step_after_iam "the broker may use only the ccc-receipts database" "gcloud projects get-iam-policy ${PROJECT}" -- \
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${BROKER_SA}" --role roles/datastore.user \
    --condition "title=ccc-receipts-only,expression=resource.name==\"projects/${PROJECT}/databases/ccc-receipts\""

# 4. The broker: private service, C1 operation post_receipt only. The image is built from a merged main SHA.
step_after_iam "deploy ccc-broker (no public invoker)" "gcloud run services describe ccc-broker --project ${PROJECT} --region ${REGION}" -- \
  gcloud run deploy ccc-broker --project "$PROJECT" --region "$REGION" \
    --image "${REG}/ccc-broker:${CCC_BROKER_TAG:-UNSET}" --service-account "$BROKER_SA" \
    --no-allow-unauthenticated --ingress all --min-instances 0 --max-instances 2 \
    --set-env-vars CCC_STAGE=C1 \
    --set-secrets BUS_URL=BUS_URL:latest,BUS_SECRET=BUS_SECRET:latest
# This ADDS the job as an invoker. Allow policies are inherited, so a project-, folder- or organization-level
# run.invoker (and owner or editor) also reaches the broker: the binding does not make the job its only caller
# (Codex P2 on ee71826). The broker image must check the caller itself (README, "Who can call the broker").
step_after_iam "add the agent job as an invoker of the broker" "gcloud run services get-iam-policy ccc-broker --project ${PROJECT} --region ${REGION}" -- \
  gcloud run services add-iam-policy-binding ccc-broker --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${JOB_SA}" --role roles/run.invoker

# 5. The agent job: max-retries 0, one task, bounded time. In C1 the only tool is post_receipt (#306, Boundary 3),
#    which calls the broker at CCC_BROKER_URL with an ID token for that audience.
step_after_iam "create the claude-code-cloud job" "gcloud run jobs describe claude-code-cloud --project ${PROJECT} --region ${REGION}" -- \
  gcloud run jobs create claude-code-cloud --project "$PROJECT" --region "$REGION" \
    --image "${REG}/claude-code-cloud:${CCC_JOB_TAG:-UNSET}" --service-account "$JOB_SA" \
    --max-retries 0 --tasks 1 --task-timeout 600s --memory 1Gi --cpu 1 \
    --set-env-vars "CCC_STAGE=C1,CCC_MAX_TURNS=4,CCC_MAX_BUDGET_USD=0.50,CCC_BROKER_URL=${BROKER_URL},CCC_BROKER_AUDIENCE=${BROKER_URL}" \
    --set-secrets ANTHROPIC_API_KEY=ANTHROPIC_API_KEY_CLOUD:latest

# 6. board-watcher (resolved in step 0) may start the new job and pass each run its envelope. The envelope travels as
#    a per-execution override, which needs run.jobs.runWithOverrides; run.invoker has only run.jobs.run (Codex P1 on
#    80d4820). The role also holds run.executions.cancel, and is bound on this job alone.
step_after_iam "board-watcher (${WATCHER_SA}) may run the claude-code-cloud job with its envelope" "gcloud run jobs get-iam-policy claude-code-cloud --project ${PROJECT} --region ${REGION}" -- \
  gcloud run jobs add-iam-policy-binding claude-code-cloud --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${WATCHER_SA}" --role roles/run.jobsExecutorWithOverrides

echo "Not in this script, by design:"
echo "  - the board-watcher claude-code-cloud route: a code PR, reviewed and released on its own;"
echo "  - the job and broker images: built from a merged main SHA;"
echo "  - any Console key value, and any GitHub App key, which the owner stores himself."

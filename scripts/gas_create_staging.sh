#!/usr/bin/env bash
# VM-CICD-001 · STEP_2 helper — create the DEV and STAGING Apps Script copies
# for one project and print the ids the pipeline needs. Requires a working
# `clasp login` (see docs/CICD.md, blocker 1). Idempotence: this CREATES new
# projects — run once per project, record the ids, do not re-run casually.
#
# Usage: gas_create_staging.sh <project-dir-under-apps-script> "<Project Title>"
set -euo pipefail
DIR="apps-script/${1:?project dir}"
TITLE="${2:?project title}"
[ -d "$DIR" ] || { echo "no such dir: $DIR" >&2; exit 1; }

for ENV in DEV STAGING; do
  (
  WORK=$(mktemp -d)
  trap 'rm -rf -- "$WORK"' EXIT
  cp -a "$DIR/." "$WORK/"
  rm -f "$WORK/.clasp.json"
  cd "$WORK"
    clasp create-script --title "$TITLE - $ENV" --type standalone
    clasp push -f
    if [ "$ENV" = "STAGING" ]; then
      clasp create-version "staging baseline"
      clasp create-deployment -d "staging web app"
      echo "=== $ENV ids (record as repo variables, see docs/CICD.md) ==="
      cat .clasp.json
      clasp list-deployments
    else
      echo "=== $ENV created (deploy at @HEAD from the editor as needed) ==="
      cat .clasp.json
    fi
  )
done
echo "Remember: gh variable set STAGING_SCRIPT_ID_<KEY> / STAGING_DEPLOYMENT_ID_<KEY> / STAGING_EXEC_URL_<KEY>"

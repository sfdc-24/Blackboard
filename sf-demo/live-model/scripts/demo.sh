#!/usr/bin/env bash
# SAMPLE DATA ONLY. Create a scratch org, deploy Service Request, assign the
# perm set, import the tree, and open the Service Request list.
# Does not read credentials and does not target any org except the alias below.
#
# Usage, from anywhere:
#   sf-demo/live-model/scripts/demo.sh
#   sf-demo/live-model/scripts/demo.sh livemodel-b
set -euo pipefail

ALIAS="${1:-livemodel}"
if [[ "$ALIAS" != "livemodel" && "$ALIAS" != "livemodel-b" ]]; then
  echo "Alias must be livemodel or livemodel-b" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

run() {
  echo "+ $*"
  "$@"
}

run sf org list
run sf limits api display --target-org DevHub

if [[ "$ALIAS" == "livemodel" ]]; then
  run sf org create scratch \
    --target-dev-hub DevHub \
    --definition-file config/project-scratch-def.json \
    --alias livemodel \
    --set-default \
    --duration-days 7 \
    --wait 15
else
  run sf org create scratch \
    --target-dev-hub DevHub \
    --definition-file config/project-scratch-def.json \
    --alias livemodel-b \
    --duration-days 7 \
    --wait 15
fi

run sf project deploy start --target-org "$ALIAS"
run sf org assign permset --name Service_Request_Access --target-org "$ALIAS"
run sf data import tree --plan data/sample-data-plan.json --target-org "$ALIAS"
run sf org open --target-org "$ALIAS" --path /lightning/o/Service_Request__c/list

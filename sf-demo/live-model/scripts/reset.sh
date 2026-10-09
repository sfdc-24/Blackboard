#!/usr/bin/env bash
# Delete the scratch org and recreate it from the packaged model.
# Drops a live-change field if one was copied into force-app during rehearsal.
#
# Usage:
#   sf-demo/live-model/scripts/reset.sh
#   sf-demo/live-model/scripts/reset.sh livemodel-b
set -euo pipefail

ALIAS="${1:-livemodel}"
if [[ "$ALIAS" != "livemodel" && "$ALIAS" != "livemodel-b" ]]; then
  echo "Alias must be livemodel or livemodel-b" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FIELDS="$ROOT/force-app/main/default/objects/Service_Request__c/fields"
rm -f "$FIELDS/Days_Open__c.field-meta.xml" "$FIELDS/Due_Date__c.field-meta.xml"

echo "+ sf org delete scratch --target-org $ALIAS --no-prompt"
sf org delete scratch --target-org "$ALIAS" --no-prompt

exec "$ROOT/scripts/demo.sh" "$ALIAS"

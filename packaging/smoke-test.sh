#!/usr/bin/env bash
# Smoke-test a RUNNING blackboard-bus. Usage:
#   BUS_SECRET=... ./smoke-test.sh http://127.0.0.1:8787/
# Exercises health, auth, doc lifecycle, sheet schema-guard, and fenced v2 ledger.
set -euo pipefail
BASE="${1:-http://127.0.0.1:8787/}"
: "${BUS_SECRET:?BUS_SECRET must be set in the environment}"
response_dir="$(mktemp -d /tmp/blackboard-bus-smoke.XXXXXX)"
response_file="$response_dir/response.json"
cleanup() {
  rm -f -- "$response_file"
  rmdir -- "$response_dir" 2>/dev/null || true
}
trap cleanup EXIT

pass=0; fail=0
say() { printf '  %-55s %s\n' "$1" "$2"; }
show_response() {
  python3 -c 'import json,sys; d=json.load(sys.stdin); d.pop("fence_token", None); print(json.dumps(d, ensure_ascii=False))' \
    <"$response_file" 2>/dev/null || printf '%s\n' '<non-JSON response withheld>'
}
post() {
  printf '%s' "$1" | curl -sS -o "$response_file" -w '%{http_code}' \
    -H 'Content-Type: application/json' --data-binary @- "$BASE"
}
expect() { # name, want_code, got_code, [grep pattern in body]
  local name="$1" want="$2" got="$3" pat="${4:-}"
  if [ "$got" = "$want" ] && { [ -z "$pat" ] || grep -q "$pat" "$response_file"; }; then
    say "$name" "ok"; pass=$((pass+1))
  else
    say "$name" "FAIL (HTTP $got, wanted $want)"; show_response; fail=$((fail+1))
  fi
}

S="$BUS_SECRET"
TS="$(date -u +%Y%m%d%H%M%S)"
DOC="smoke doc $TS"
SHEET="smoke sheet $TS"

echo "== health/auth =="
code=$(curl -sS -o "$response_file" -w '%{http_code}' "$BASE")
expect "bare GET health" 200 "$code" '"ok": *true'
code=$(post '{"action":"time","secret":"wrong"}')
expect "wrong secret -> real 401" 401 "$code" 'Bad or missing secret'
code=$(post "{\"action\":\"time\",\"secret\":\"$S\"}")
expect "authenticated time" 200 "$code" '"iso"'

echo "== docs =="
code=$(post "{\"action\":\"create\",\"title\":\"$DOC\",\"kind\":\"doc\",\"secret\":\"$S\"}")
expect "create doc" 200 "$code"
code=$(post "{\"action\":\"append\",\"title\":\"$DOC\",\"text\":\"entry one\",\"secret\":\"$S\"}")
expect "append doc (echoes bytes)" 200 "$code" 'bytes_written'
code=$(post "{\"action\":\"replace\",\"title\":\"$DOC\",\"body\":\"replaced snapshot\",\"secret\":\"$S\"}")
expect "REPLACE doc (D-16)" 200 "$code"
code=$(post "{\"action\":\"read\",\"title\":\"$DOC\",\"secret\":\"$S\"}")
expect "read-back shows replace" 200 "$code" 'replaced snapshot'

echo "== sheet schema guard =="
code=$(post "{\"action\":\"create\",\"title\":\"$SHEET\",\"kind\":\"sheet\",\"header\":[\"Row_ID\",\"Timestamp\",\"Source_Tag\",\"Payload\"],\"secret\":\"$S\"}")
expect "create 4-col sheet" 200 "$code"
code=$(post "{\"action\":\"append\",\"title\":\"$SHEET\",\"sheetRow\":[\"vm-cli\",\"hello\"],\"secret\":\"$S\"}")
expect "content cells -> server ids" 200 "$code" 'row_id'
code=$(post "{\"action\":\"append\",\"title\":\"$SHEET\",\"sheetRow\":[\"2026-09-02T17:01:00Z\",\"shifted\",\"x\",\"y\"],\"secret\":\"$S\"}")
expect "shifted row -> 400 REQ-B4TQX9" 400 "$code" 'REQ-B4TQX9'
code=$(post "{\"action\":\"append\",\"title\":\"$SHEET\",\"secret\":\"$S\"}")
expect "empty append -> 400 REQ-V8QD7R" 400 "$code" 'REQ-V8QD7R'
code=$(post "{\"action\":\"read\",\"title\":\"$SHEET\",\"limit\":1,\"secret\":\"$S\"}")
expect "tail read with limit" 200 "$code" 'total_rows'

echo "== v2 ledger =="
W="WRK-SM$TS"
LEASE="$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)"
code=$(post "{\"action\":\"event\",\"work_id\":\"$W\",\"event_type\":\"CREATE\",\"actor_tag\":\"vm-cli\",\"assigned_to\":\"ANY\",\"status\":\"OPEN\",\"payload\":\"smoke item\",\"secret\":\"$S\"}")
expect "CREATE event" 200 "$code" 'payload_bytes'
code=$(post "{\"action\":\"event\",\"work_id\":\"$W\",\"event_type\":\"CLAIM\",\"actor_tag\":\"gemini\",\"status\":\"CLAIMED\",\"lease_until\":\"$LEASE\",\"payload\":\"claiming\",\"secret\":\"$S\"}")
expect "CLAIM returns private fence" 200 "$code" '"fence_token"'
FENCE="$(python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("claim_generation") == 1; print(d["fence_token"])' <"$response_file")"
code=$(post "{\"action\":\"event\",\"work_id\":\"$W\",\"event_type\":\"CLAIM\",\"actor_tag\":\"vm-cli\",\"status\":\"CLAIMED\",\"lease_until\":\"$LEASE\",\"payload\":\"stealing\",\"secret\":\"$S\"}")
expect "conflicting CLAIM -> 409" 409 "$code" 'holder'
code=$(post "{\"action\":\"inbox\",\"tag\":\"gemini\",\"secret\":\"$S\"}")
expect "inbox shows claimant's item" 200 "$code" "$W"
if grep -q 'fence_token' "$response_file"; then
  say "inbox redacts private fence" "FAIL"; fail=$((fail+1))
else
  say "inbox redacts private fence" "ok"; pass=$((pass+1))
fi
code=$(post "{\"action\":\"event\",\"work_id\":\"$W\",\"event_type\":\"COMPLETE\",\"actor_tag\":\"gemini\",\"status\":\"DONE\",\"payload\":\"smoke result\",\"secret\":\"$S\"}")
expect "COMPLETE without fence -> 409" 409 "$code" 'fence_token'
code=$(post "{\"action\":\"event\",\"work_id\":\"$W\",\"event_type\":\"COMPLETE\",\"actor_tag\":\"gemini\",\"status\":\"DONE\",\"payload\":\"smoke result\",\"fence_token\":\"$FENCE\",\"secret\":\"$S\"}")
expect "fenced COMPLETE" 200 "$code" '"claim_generation"'
code=$(post "{\"action\":\"event\",\"work_id\":\"$W\",\"event_type\":\"COMPLETE\",\"actor_tag\":\"gemini\",\"status\":\"DONE\",\"payload\":\"smoke result\",\"fence_token\":\"$FENCE\",\"secret\":\"$S\"}")
expect "exact fenced COMPLETE replay" 200 "$code" '"replayed": *true'

echo
echo "$pass passed, $fail failed"
exit $((fail > 0))

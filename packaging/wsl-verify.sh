#!/usr/bin/env bash
# Phase 2 verification against the INSTALLED, RUNNING service. Run as root.
set -euo pipefail
REPO=/mnt/c/users/akatiawam/blackboard
DUMP="$1"   # path to a saved v1 bus read dump (board_read.json)
export BUS_SECRET="$(cat /tmp/bus-test-secret)"

echo "===== A. smoke test against installed service ====="
bash "$REPO/packaging/smoke-test.sh" http://127.0.0.1:8787/

echo
echo "===== B. import the real board dump (as service user) ====="
runuser -u blackboard-bus -- /usr/bin/blackboard-bus --db /var/lib/blackboard-bus/bus.db \
  import-file "$DUMP" --title 'Blackboard - Alpha DB'

echo
echo "===== C. read it back over HTTP (limit=2) ====="
curl -s -H 'Content-Type: application/json' \
  -d "{\"action\":\"read\",\"title\":\"Blackboard - Alpha DB\",\"limit\":2,\"secret\":\"$BUS_SECRET\"}" \
  http://127.0.0.1:8787/ | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok:', d['ok'], '| total_rows:', d['total_rows'], '| returned:', len(d['rows']), '| header ok:', d['rows'][0][0]=='Row_ID')"

echo
echo "===== D. unmodified fleet client: bcb_lint.py --live against THIS bus ====="
BUS_URL=http://127.0.0.1:8787/ python3 "$REPO/scripts/bcb_lint.py" --live || true

echo
echo "===== E. full test suite on Linux (fresh scratch server) ====="
TEST_PORT=28787 python3 "$REPO/tests/test_bus.py" "$REPO/src/bus_server.py" | tail -3

echo
echo "===== F. service still healthy, owned files sane ====="
systemctl is-active blackboard-bus
ls -l /var/lib/blackboard-bus/ | sed 's/^/  /'
journalctl -u blackboard-bus --no-pager -n 3 | sed 's/^/  /'

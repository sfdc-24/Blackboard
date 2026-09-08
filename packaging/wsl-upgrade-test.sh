#!/usr/bin/env bash
# Upgrade-path test: build 1.0.1, upgrade over the running 1.0.0 install,
# verify the service restarted onto new code and the data survived. Run as root.
set -euo pipefail
REPO=/mnt/c/users/akatiawam/blackboard
export BUS_SECRET="$(cat /tmp/bus-test-secret)"

echo "== before: running version =="
curl -s http://127.0.0.1:8787/ | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])"

echo "== build 1.0.1 (as abdus so \$HOME/build stays consistent) =="
runuser -u abdus -- bash "$REPO/packaging/build-deb.sh" >/dev/null
ls -l /home/abdus/build/blackboard-bus_1.0.1_all.deb

echo "== upgrade in place =="
apt-get install -y /home/abdus/build/blackboard-bus_1.0.1_all.deb 2>&1 | tail -3

echo "== after: version, active, data intact =="
sleep 1
systemctl is-active blackboard-bus
curl -s http://127.0.0.1:8787/ | python3 -c "import json,sys; d=json.load(sys.stdin); print('version:', d['version'])"
curl -s -H 'Content-Type: application/json' \
  -d "{\"action\":\"read\",\"title\":\"Blackboard - Alpha DB\",\"limit\":1,\"secret\":\"$BUS_SECRET\"}" \
  http://127.0.0.1:8787/ | python3 -c "import json,sys; d=json.load(sys.stdin); print('board intact:', d['ok'] and d['total_rows'] == 744, '| total_rows:', d['total_rows'])"

echo "== hardening after upgrade =="
systemctl show blackboard-bus -p UMask -p LimitNOFILE -p TasksMax -p MemoryMax | sed 's/^/  /'
stat -c '%a %U %G %n' /var/lib/blackboard-bus /var/lib/blackboard-bus/bus.db /etc/blackboard-bus/env | sed 's/^/  /'

echo "== smoke test on upgraded service =="
bash "$REPO/packaging/smoke-test.sh" http://127.0.0.1:8787/ | tail -3

echo "== fleet client against upgraded service =="
BUS_URL=http://127.0.0.1:8787/ python3 "$REPO/scripts/bcb_lint.py" --live 2>/dev/null | head -4 || true

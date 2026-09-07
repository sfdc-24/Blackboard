#!/usr/bin/env bash
# One-shot: install the freshly built .deb, configure a test secret, start the
# systemd service, and verify health. Run inside Ubuntu with passwordless sudo.
set -euo pipefail

DEB="${1:-$HOME/build/blackboard-bus_1.0.0_all.deb}"

echo "== install =="
sudo apt-get install -y "$DEB" 2>&1 | tail -4

echo "== configure =="
SEC="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-31)"
sudo tee /etc/blackboard-bus/env >/dev/null <<EOF
BUS_SECRET=$SEC
BUS_BIND=127.0.0.1
BUS_PORT=8787
EOF
sudo chown root:blackboard-bus /etc/blackboard-bus/env
sudo chmod 640 /etc/blackboard-bus/env
# stash for the smoke test only - never printed
echo "$SEC" > /tmp/bus-test-secret
chmod 600 /tmp/bus-test-secret

echo "== start =="
sudo systemctl daemon-reload
sudo systemctl enable --now blackboard-bus
sleep 1
systemctl is-active blackboard-bus
echo "== health =="
curl -s http://127.0.0.1:8787/
echo
echo "== unit hardening view =="
systemctl show blackboard-bus -p User -p NoNewPrivileges -p ProtectSystem -p StateDirectory | sed 's/^/  /'

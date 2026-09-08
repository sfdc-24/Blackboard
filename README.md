# Blackboard

Shared bus for the SFDC24 multi-agent fleet.

The public website is maintained in
[`sfdc-24/sfdc24-site`](https://github.com/sfdc-24/sfdc24-site).
This repository's `site/` directory is an incomplete reference/test copy.
Use the complete website repository for page changes and releases; see
[the source and release guidance](docs/HANDOVER.md).

## blackboard-bus — self-hosted bus package

A stdlib-only Python service + Debian package that carries the Blackboard
bus architecture on an Ubuntu VM (target: Google Cloud). Implements the
v1 Apps Script bus contract so existing clients port by changing
`BUS_URL`, adds the fleet's filed fixes (REPLACE, tail read, schema-aware
append, honest HTTP status codes, write echoes), and ships the proposed
LEDGER SCHEMA v1 event ledger (`event` / `inbox` / `work`).

| Path | What |
|------|------|
| `src/bus_server.py` | the whole service (server + CLI: serve, import-file, import-bus, export) |
| `tests/test_bus.py` | end-to-end suite (38 checks) — spawns a real server |
| `packaging/build-deb.sh` | builds `blackboard-bus_<v>_all.deb` (run on any Ubuntu, incl. WSL) |
| `packaging/smoke-test.sh` | drives a *running* bus through the contract (16 checks) |
| `packaging/wsl-install-test.sh` / `wsl-verify.sh` | install + full verification used on WSL |
| `packaging/blackboard-bus.service` | hardened systemd unit (secret via `/etc/blackboard-bus/env`) |
| `docs/DEPLOY-GCP.md` | GCP runbook: create VM, install, HTTPS options, seed, migrate clients |
| `scripts/bcb_lint.py` | BCB board grammar validator (GPT-BCBLINT-001) — also works against this bus |

Quick start (Ubuntu):

```bash
packaging/build-deb.sh
sudo apt-get install -y ~/build/blackboard-bus_1.0.1_all.deb
sudoedit /etc/blackboard-bus/env     # set BUS_SECRET (never hardcode - D-18)
sudo systemctl enable --now blackboard-bus
curl -s http://127.0.0.1:8787/
```

Tests: `python3 tests/test_bus.py` (no install needed).

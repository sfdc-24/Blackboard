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
| `tests/test_bus.py` | self-checking end-to-end suite — spawns real server processes |
| `packaging/build-deb.sh` | builds `blackboard-bus_<v>_all.deb` (run on any Ubuntu, incl. WSL) |
| `packaging/smoke-test.sh` | drives a *running* bus through the installed contract |
| `packaging/wsl-install-test.sh` / `wsl-verify.sh` | install + full verification used on WSL |
| `packaging/blackboard-bus.service` | hardened systemd unit (secret via `/etc/blackboard-bus/env`) |
| `docs/DEPLOY-GCP.md` | GCP runbook: create VM, install, HTTPS options, seed, migrate clients |
| `scripts/bcb_lint.py` | BCB board grammar validator (GPT-BCBLINT-001) — also works against this bus |
| `src/mother_child_contract.py` | offline post-auth reference gate for identity-bound tenant/child commands, policy ceilings, trusted-store invitations, currently authorized replay and work-scoped fences; not wired into the live server |
| `tests/test_mother_child_contract.py` | adversarial, credential-free contract suite, including 100-way invitation and replay races |
| `docs/MOTHER-CHILD-BUS-ARCHITECTURE.md` | proposed control/data-plane boundary, Cloud SQL datastore ADR, migration gates and enterprise negative controls |

### Mother/child post-auth admission is proposed, not deployed

The current bus remains a single-node service authorized by one shared bearer
secret. Passing the offline mother/child suite does **not** make that service
multi-tenant. The proposed contract derives tenant and child scope from a
separately verified principal, prevents a mother/control identity from reading
or writing child data, requires the intersection of parent ceiling and child
grant, and makes a bounded post-auth subset of invitation, replay and
work-fence failures executable before an online integration exists. Directional
control/health protocols, identity transport, persistence and effects remain
future gates. See the
[architecture and datastore decision](docs/MOTHER-CHILD-BUS-ARCHITECTURE.md).

### Claim fencing contract

Each successful ledger `CLAIM` returns a server-issued `claim_generation` and
private, opaque `fence_token` that is independent of the public event id. The
claimant must carry that exact token on every
`PROGRESS`, `BLOCK`, `RELEASE`, and `COMPLETE`. `CANCEL` must also carry the
current token; possession of the private capability authorizes cancellation and
the supplied operator `actor_tag` remains audit attribution. While a claim lease
is live, a same-actor renewal must carry its current token before the server will
advance the per-work generation. A tokenless same-actor process therefore cannot
supersede the live holder merely by reusing its actor name. `NOTE` and `FINDING`
are observations: their recorded status, assignment, and lease do not reduce
effective work state.

New fenced claims require `CLAIM`/`CLAIMED`. Subsequent lifecycle transitions
are fail-closed status pairs: `BLOCK`/`BLOCKED`, `RELEASE`/`OPEN`,
`COMPLETE`/`DONE`, and `CANCEL`/`CANCELLED`. Contradictory pairs are rejected
before storage once a work item is fenced, so they cannot leave a stale fence
active; untouched legacy items retain their v1 behavior.

Clients must persist `{work_id, actor_tag, claim_generation, fence_token}` as one
atomic record before starting work. Concurrent responses can arrive out of
order: never let a lower generation overwrite a higher one. Read APIs return the
generation for observability but deliberately redact every token; even a CLAIM's
public `event_id` cannot be used as its token. If a CLAIM response is lost or
ambiguous, there is no read-back recovery in v1.1: the process must not perform
or publish work, and must either recover its own atomic receipt or wait for the
lease to expire before making a fresh tokenless claim. Do not blindly retry a
live tokenless CLAIM. A future idempotent claim nonce could remove this liveness
cost without exposing the capability. An exact `COMPLETE` retry can return its
original receipt with `replayed: true` even after displacement because that path
adds no transition; any changed stale result is rejected.

Use UTC `Z` for lease timestamps when possible. Numeric offsets with or without
a colon are accepted only when their absolute value is below 15 hours, matching
the Python validator and SQLite's durable time comparison. Every new `CLAIM` and
subsequent fenced state-bearing lease is capped at four hours from acceptance,
including a lease refreshed by `PROGRESS`; observation-only `NOTE`/`FINDING`
lease fields cannot affect a hold. Untouched legacy items keep their v1 rules.

The token is a freshness capability, not actor authentication. Every holder of
the shared `BUS_SECRET` can read ledger state and assert an `actor_tag`, but the
secret alone cannot recover or fabricate a live process's private token. Systems
that perform external side effects must independently persist the highest
generation per work item and reject lower generations *before* the effect; a bus
rejection after an external write cannot undo that write.

During upgrade, stop every old bus writer before starting the new binaries. The
SQLite migration preserves legacy rows and permits tokenless claimant events on
a work item only until its first fenced CLAIM. Database triggers prevent an old
binary from downgrading that item afterward, but cannot make a never-yet-fenced
legacy item safe while an old writer is still running.

Quick start (Ubuntu):

```bash
packaging/build-deb.sh
sudo apt-get install -y ~/build/blackboard-bus_1.1.0_all.deb
sudoedit /etc/blackboard-bus/env     # set BUS_SECRET (never hardcode - D-18)
sudo systemctl enable --now blackboard-bus
curl -s http://127.0.0.1:8787/
```

Tests: `python3 tests/test_bus.py` and
`python3 tests/test_mother_child_contract.py` (no install needed).

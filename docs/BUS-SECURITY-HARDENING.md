# Bus communication — security hardening roadmap

Requested by Mr. Salam, 2026-09-11: harden and optimize bus communication to a
standard that holds up to enterprise security review. This document is the
assessment and the plan. It deliberately describes **controls, not attack
walkthroughs** — detailed attack-path mapping of a live system stays local per
the 2026-09-03 precedent (`docs/security-review.md`, local-only).

Scope: the two carriers of the Blackboard bus —

- **v1 Apps Script bus** (live): Google Apps Script `/exec` in front of the
  `Blackboard - Alpha DB` sheet.
- **blackboard-bus** (migration target): `src/bus_server.py`, self-hosted on
  Ubuntu/GCP, v1-compatible plus the LEDGER SCHEMA v1 surface.

---

## 1. Current state, honestly

### What is already right (keep, and keep verifying)

| Control | Where | Evidence |
|---|---|---|
| Secret never in URLs, never in repo, never logged | both; doctrine D-18 | clients send it in POST bodies; `env_check.py` checks presence without printing |
| Constant-time secret comparison | `bus_server.py` `secret_ok()` | `hmac.compare_digest`, non-string input → 401 not 500 |
| Zero-downtime rotation window | both | `BUS_PREVIOUS_SECRET` on the server; `codegs_rotation_window.gs` for Apps Script |
| Refuses to start without a secret | `bus_server.py` `cmd_serve` | hard exit, points at `/etc/blackboard-bus/env` |
| Localhost bind by default; TLS by fronting proxy or IAP tunnel | `DEPLOY-GCP.md` §3 | secret never rides plaintext past the loopback |
| Hardened systemd unit | `packaging/blackboard-bus.service` | `NoNewPrivileges`, `ProtectSystem=strict`, empty capability set, syscall filter, `MemoryMax`/`TasksMax` so a flood exhausts the service, not the VM |
| Honest HTTP status codes | self-hosted | a 401 is a 401; clients can alarm on it |
| Schema-aware append | self-hosted | rejects mis-shaped rows (REQ-B4TQX9 class) instead of silently shifting columns |
| Ledger idempotency | v2 surface | `event_id UNIQUE` — a replayed event is a no-op, not a duplicate |
| Quarantine of visitor text | Apps Script | `PUBLIC_INBOX`, verified 2026-09-03; untrusted text never lands on the agent board |
| Bounded chat spend | Apps Script | session/daily caps, kill switch |

### The structural gaps

**G1 — One secret is one identity.** Every agent presents the same
`BUS_SECRET`. Consequences, in order of weight: no per-agent revocation
(cutting off one client means rotating the whole fleet); no authorization
tiers (whoever can read can also append and REPLACE); and audit attribution
rests entirely on the next gap.

**G2 — `Source_Tag` is self-asserted.** The writer names itself. Any holder
of the one secret can write rows attributed to any other agent — including
authority-bearing rows (`verdict=`, `hold=`, `clears=`). The 2026-09-03
`post_reset` incident (forged `by=Governor`) was this class; the endpoint got
guarded, the board write path did not get identity.

**G3 — The bearer secret is replayable forever.** No timestamp, no nonce, no
signature binding a request to a moment or a payload. Interception or
transcript exposure equals full read/write until the next manual rotation —
and rotation currently has no cadence, only a deferred queue item.
(2026-09-11: the secret transited a chat transcript; risk explicitly accepted
by Mr. Salam for now. That acceptance is the clock on H1 below.)

**G4 — No tamper evidence.** The board is a Google Sheet with UUIDs; the
self-hosted store is SQLite rows. An editor of the sheet, or root on the VM,
can rewrite history without trace. This is also a *product* gap, not just a
security one: the round-table strategy names the audit trail as a sellable
asset, and its own honest first step was "tamper-evident logging"
(`docs/HANDOVER.md`, Strategy).

**G5 — No per-credential throttling or auth-failure detection.** Apps Script
gives coarse platform quotas; the self-hosted unit caps total resources but
neither counts failed auth per source nor alerts on it. The monitor
(`monitorTick`) watches availability, not security signals.

**G6 — Transport duplicates are policed by convention, not protocol.** The
v1 append is not idempotent; the one-attempt-then-read-back discipline lives
in client code and README warnings. Every new client must re-learn it or
re-create the 2026-09-08 double-write.

**G7 — Secrets lifecycle is manual.** `.env` files provisioned by hand,
no central store, no rotation schedule, no record of who holds which secret
(vm-chrome's possibly-stale secret was an open queue item precisely because
holding is invisible).

---

## 2. Target architecture, in three horizons

The migration to the self-hosted bus is already fleet policy (UBUNTU-LEAD /
GCP runbook). Hardening rides that migration rather than fighting Apps
Script's ceiling: Apps Script cannot do per-client credentials, custom
headers on `/exec`, or response signing — the platform owns the HTTP layer.
So: **H1 tightens what exists, H2 lands identity with the migration, H3 makes
integrity a product.**

### H1 — Now, on the live v1 bus (days, no client rewrites)

1. **Rotate the shared secret** through the existing window
   (`codegs_rotation_window.gs` + `BUS_PREVIOUS_SECRET` pattern), and set a
   cadence — quarterly, plus on any suspected exposure. Owner: laptop
   instance (clasp auth). The 2026-09-11 transcript exposure makes the first
   rotation the natural test of the window.
2. **Alarm on auth failures.** Extend `monitorTick` to count `401`/bad-secret
   responses in the Apps Script execution log and alert on a burst (same
   state-change-only email discipline, same 12/day cap). Detection now, not
   after migration.
3. **Inventory secret holders.** One board row per instance confirming which
   secret generation it holds (generation number, never the value). Turns
   "is vm-chrome cut off?" from a mystery into a read.
4. **Write the client discipline into the contract.** The
   one-attempt/read-back rule and the never-in-URL rule move from README
   prose into BCB-1 as normative requirements, so a new client's conformance
   is checkable (`bcb_lint` already validates grammar; add these).

### H2 — With the GCP migration (the identity release)

The self-hosted server is stdlib-only and single-file by design; these fit
that shape:

1. **Per-agent keyed HMAC requests.** Each agent gets its own key
   (`key_id` + secret). A request carries `key_id`, an ISO timestamp, a
   nonce, and `HMAC-SHA256(key, timestamp + nonce + body)`. The server keeps
   a short nonce cache and rejects stale timestamps — replay dies, G3
   closes. `Source_Tag` is then **derived from `key_id`, not asserted** —
   G2 closes. Revoking one agent is deleting one key — G1's revocation half
   closes. Rollout is compatible: accept both auth forms during a window,
   exactly like `BUS_PREVIOUS_SECRET`, then retire the shared secret.
2. **Scopes per key**: `read`, `append`, `replace`, `admin`. The REPLACE
   action and any future reset/import verbs demand `admin`. A leaked
   read-scoped key can no longer write history — the rest of G1 closes.
3. **Per-key rate limits and lockout** (in-process token bucket keyed by
   `key_id` and by remote address for unauthenticated attempts), with
   failed-auth counters exposed on the health endpoint so the monitor can
   read them — G5 closes.
4. **Protocol-level idempotency for v1 append**: client supplies an
   `append_id` (UUID); server stores it unique and answers a duplicate with
   the original result, exactly as the v2 ledger already does with
   `event_id`. The read-back discipline becomes belt-and-braces instead of
   the only brace — G6 closes.
5. **Transport**: keep the localhost bind + Caddy/Let's Encrypt front (or
   IAP tunnel) from `DEPLOY-GCP.md`; add HSTS at the proxy; keep the
   firewall admitting only 443. Note the DNS charter (L-81) before any
   `bus.sfdc24.com` record: `www` must never carry an A record, and DNS
   changes are outside standing permission.
6. **Secrets at rest**: keys live in GCP Secret Manager, delivered to the VM
   at boot into `/etc/blackboard-bus/env` (root:blackboard-bus 0640, as
   packaged); laptop agents keep D-18 `.env` files but now hold *their own*
   key, so one laptop's exposure is one key's rotation — G7 mostly closes.

### H3 — Enterprise posture and the product thesis

1. **Hash-chained ledger.** Each event stores
   `prev_hash` and `hash = SHA256(prev_hash + canonical(event))`; the chain
   head is periodically anchored somewhere the VM cannot rewrite (a signed
   git tag, or a daily row in a separate account's store). Tampering is then
   *detectable by any reader* — G4 closes, and the "audit trail as product"
   claim becomes technically true instead of aspirational. This is the one
   item that is simultaneously a security control and a revenue direction;
   it deserves its own design review before code.
2. **Workload identity instead of static keys** where the runner allows it:
   GCP service-account OIDC tokens for VM-resident agents, verified
   server-side; static HMAC keys remain for laptops. mTLS at the Caddy layer
   is the heavier alternative — evaluate only if a customer audit demands it.
3. **Structured security log** (auth outcomes, key_id, action, scope
   decision — never payloads or secrets) shipped to Cloud Logging with a
   retention policy; alerting policy on failure bursts and on any `admin`
   scope use.
4. **Availability**: `litestream` (or plain `sqlite3 .backup` on a timer) to
   GCS for the bus DB; a documented restore drill. Tamper-evident and
   *recoverable* is the pair auditors ask for.
5. **Control mapping** one page mapping the above to SOC 2 CC-series /
   ISO 27001 Annex A families, written when the first external party asks —
   the controls above are chosen so that the mapping is a table, not a
   project.

---

## 3. What this deliberately does not do

- No change to the quarantine boundary: visitor text stays in
  `PUBLIC_INBOX`; identity for agents does not grant authority to visitors
  (`instruction_authority=NONE` holds through any sign-in work).
- No DNS action of any kind without explicit approval (L-81, ISS-015).
- No weakening of runtime guards to make cross-platform tests pass — the
  ORDER lane's rule applies to the bus too.
- No new dependency in `bus_server.py` for H2: HMAC, nonce cache, token
  bucket, and scopes are all stdlib-shaped, keeping the single-file,
  auditable character that is itself a security property.

## 4. Decisions queued for Mr. Salam (one at a time, per L-86)

1. Approve the first H1 rotation (the window tooling exists; this also
   retires the transcript-exposed secret).
2. Confirm H2 rides the GCP migration as its acceptance criteria — i.e. the
   migration is not "done" on feature parity alone, but on per-agent keys +
   scopes + idempotent append.
3. Rule whether the H3 hash-chained ledger becomes a product workstream or
   stays an internal control.

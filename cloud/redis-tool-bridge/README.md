# redis-tool-bridge

An IAM-authenticated HTTPS surface over the private Redis instance, so agents with no VPC reach can
exercise it without anyone handing out a database credential.

**Status: source for review. Nothing in this directory has been applied or deployed.** The owner's
GO is explicit — *"Deploy, new SA and IAM only AFTER Codex verifies the source"*
(`GROK-REDIS-BRIDGE-OWNER-GO-20261007T2307Z`).

## Why this PR exists, which is not the usual reason

**The service is already running.** `redis-tool-bridge-00001-hsg` serves
`https://redis-tool-bridge-yzet4vuplq-uc.a.run.app` from image digest `cd88ac6cc786…`, it holds a
mounted database credential, and **its source is in no repository.** Nobody can review the code that
reads that credential, diff it against the four defects raised in `CCC-BRIDGE-REVIEW-20261006T1330Z`,
or rebuild it. A service that handles a secret and cannot be reviewed is the finding here; the code
is the remedy.

## What was measured on 2026-10-07, read-only

| | running service | this source |
|---|---|---|
| service account | `redis-tool-bridge@sfdc24` (dedicated) | same |
| egress | Direct VPC, `private-ranges-only` | same |
| secrets | **file mounts**, separate dirs `/secrets/auth`, `/secrets/ca` | same |
| `run.invoker` | `aya-runtime@sfdc24` **only** — no `allUsers`, no `allAuthenticatedUsers` | same |
| ingress | `all` | same, and now justified below |
| **application code** | **unreviewable** | this directory |

Three of the four locked decisions were already honoured by the deployment. The application layer is
the part nobody could check, and it is the part these files pin down.

## The four defects, and where each is closed

From my own review of the original proposal, verdict CHANGES-REQUESTED:

1. **`verifyIdToken` called with no audience.** The library does not check `aud` unless you pass it,
   so any Google-signed token minted for any service satisfied the middleware. Here `BRIDGE_AUDIENCE`
   is **required configuration**, passed explicitly to the verifier, and if it is unset the service
   refuses **every** request rather than verifying nothing. Asserted against the source, because no
   behavioural test can see a missing argument without a real Google token.
2. **`checkServerIdentity` returning `undefined` disabled server identity verification,** written as
   though a private CA demanded it. It does not. This connects through `scripts/redis_dual.py`
   (`ssl_cert_reqs="required"`, mounted CA) — the same path a probe verified end to end against this
   instance on 2026-10-06: PING true, SET/GET byte-identical at 94 chars, TTL read back, DEL gone,
   zero keys left behind. The bridge implements no TLS of its own, and a test forbids it from doing so.
3. **Authentication without authorisation.** The old middleware verified a token, read
   `payload.email`, and called `next()`. Here a verified token must *also* name a principal on the
   server-side `BRIDGE_CALLERS` allowlist, and Cloud Run's own IAM check runs first. Two independent
   gates; the allowlist is the one this repository can review.
4. **Secrets as environment variables** — listable from anything that can read the process. Both
   arrive as file mounts in separate directories. A test parses `deploy.sh` and fails on any
   `--set-secrets` binding that is not a path, because the difference between a mount and an env var
   is one character.

**One correction to that review.** I called `ingress: all` a defect. It is not, and I was wrong:
Aya calls from a cloud workspace outside this VPC, so `internal-and-cloud-load-balancing` would make
the service unreachable by its only caller. Reachability is not authorisation — IAM refuses an
unauthenticated request before it reaches the container.

**And one fact that has changed since.** That review's decisive blocker was
*"aya-runtime@sfdc24.iam.gserviceaccount.com DOES NOT EXIST."* **It exists now**, and it holds
`run.invoker` on the bridge. Verified, not assumed.

## What it can do

One endpoint that touches Redis: `POST /probe`, a synthetic probe — the server chooses the key, the
namespace, the nonce and the TTL; the caller chooses none of them (locked decision 3). Plus
`GET /healthz`, which touches nothing at all, reads no secret and reports no configuration.

There is **no** action for `GET`, `SET`, `HSET`, `XADD`, `KEYS`, `SCAN`, `FLUSHDB`, `FLUSHALL` or
`DEL`. Not refused — **absent**. A refusal is a line of code that can be got wrong. A test pins the
Redis calls to exactly `setex`, `get`, `ttl`, `delete`, so a fifth is a test change and therefore a
review.

### `POST /op` — source for review, not approved, not deployed (2026-10-13)

Everything above describes what is **deployed** and what `main.py` itself does. This PR adds a second
endpoint as source, for decision D1 of `docs/REDIS-PRIMARY.md` ("extend the bridge from probe-only to
governed ops, after the source is verified"). **D1 has not been given. Nothing here deploys it, and
`deploy.sh` still says not to run it.**

- It lives in `op.py`, so the paragraph above stays true of `main.py`: its own Redis calls are still
  exactly `setex`, `get`, `ttl`, `delete`, and the same test still pins them.
- `op.py` makes **no** Redis call of its own. It hands one request to `scripts/redis_gov.execute`, the
  function the board route already calls, so the access list, the protected namespaces, the server-set
  TTL, the value cap and the audit are one tested implementation, not two.
- The caller is whoever `verify_caller` verified (signature, audience, allowlist), mapped to a principal
  by `callers.json`. The body cannot name who it acts as. `gov:audit` records `via = bridge:<email>`.
- A message through it costs no board row.

| status | meaning |
|---|---|
| 401 `unauthenticated` | no token, bad signature, wrong audience, unverified email |
| 403 `not authorised` | a verified identity that is not on `BRIDGE_CALLERS` |
| 400 `bad request` | the body is not the small JSON object `op.py` describes; `executed: false` |
| 413 `body too large` | over 16 KB; refused before it is read |
| 403 `principal mismatch` | the caller is bound to another principal than the client's `expect`; `executed: false` |
| 403 `refused` | `redis_gov` refused it; `reason` is its own; `audit` says whether the refusal was recorded; `executed: false` |
| 503 `redis unreachable` | no connection was made; always `executed: false` |
| 503 `service unavailable` | the bridge is misconfigured, including a caller with no principal; `executed: false` |
| 502 `failed` | a read raised inside Redis; the error type only |
| 502 `outcome unknown` | a **write** raised after the connection was made; it may have committed |
| 200 | `redis_gov`'s result |

**`executed: false` is a promise, and its absence is not one** (Aya's review of `919fd52`). A client may
send a write again by another route only when the first attempt provably did nothing. A write that
reached `redis_gov` and then raised is `outcome unknown`: a dropped connection can lose the reply to a
transaction that ran. The first version called that `redis unreachable`, the same words as a connection
never made.

**`expect` is a check, not a choice.** A body may carry `"expect": "<principal>"`. It can never select a
principal; it can only make the bridge refuse, before anything runs, when the caller is bound to a
different one. That is what stops a client configured with another agent's account from writing under
that agent's name.

**Not built here** (the rest of Phase 3a and all of 3b): the hourly canary, the negative controls
against a deployed bridge, the client (`scripts/redis_inbox.py` sending through the bridge with its
breaker), and any second caller. Adding a caller is one line in `callers.json` after its service
account exists and the owner has approved it (D2).

## Caller identity, per surface

The GO asks for this explicitly: *federation possible or not, and why.*

| surface | can it mint an OIDC token for this audience? | plan |
|---|---|---|
| **Aya / Codex cloud workspace** | **Yes, by impersonation.** `aya-runtime@` exists and holds `run.invoker` (both verified). | Locked decision 4: laptop ADC impersonates `aya-runtime`, **no downloadable keys**. Still needs `roles/iam.serviceAccountTokenCreator` on `aya-runtime`, which is **owner-only** and outstanding. |
| **Grok box** | **No.** The GO states the Grok box has no `gcloud`; I have not verified any OIDC issuer on that host, and I am not going to assume one. | **Relay, not a key.** Grok asks through a surface that already has an identity rather than holding a credential. The GO permits keys only if federation is impossible — but "impossible" should not become "a key on a laptop" while a relay exists. |
| **Cursor cloud agents** | **Unverified.** I have not confirmed that Cursor exposes an OIDC issuer for agent workloads, so I will not claim workload identity federation works there. | Same: relay first. If Cursor does publish an issuer, WIF is the answer and no key is needed — that is a fact to establish, not to guess. |
| **pi1-cli** | **Yes.** `pi1-cli@sfdc24` exists. | Grant `run.invoker` when it actually needs the bridge, not before. |

**The recommendation, plainly:** no downloadable service-account keys for any surface. Two of the
four have no verified federation path, and for those a relay through an identity that already exists
is strictly safer than a key file on a host we do not control.

## Deploying it, when that is authorised

`deploy.sh` is a gcloud script with a read-back, not Terraform — locked decision 1, and there is no
Terraform state anywhere in this fleet.

```
./deploy.sh plan        # print every command it WOULD run. Do this first, and read it.
./deploy.sh build       # build the image. Record the DIGEST; a tag is mutable.
./deploy.sh deploy      # only after Codex has verified this source
./deploy.sh readback    # describe the service and its IAM policy, and check it by eye
```

There is deliberately no `all`: a deploy that also builds hides which artefact is running. And
record the digest, not the tag — `bus-reconciler:v1` is shared by eight Cloud Run jobs, and
rebuilding it re-pointed every one of them.

## Tests

`python tests/test_redis_tool_bridge.py` — 27 tests, offline, no project, no credential, no network.
Each of the four defects has a test that fails if it comes back.

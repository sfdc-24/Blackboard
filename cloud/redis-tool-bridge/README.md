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

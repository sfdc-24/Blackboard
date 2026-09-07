# SITE-P0-TTS-001 — isolated provider-admission cardinality canary

Status: **OFFLINE HARNESS READY; LIVE CANARY ON HOLD.** No canary project,
version, deployment, Script Property, provider request, or production/staging
source has been created or changed by this work.

## What this can and cannot prove

The canary is designed to prove, in the Apps Script runtime, that the Governor
admits exactly one synthetic provider call for a valid one-use audio claim and
admits zero additional calls for replay or exhausted budgets. It includes a
barrier/overlap witness for two separate `scripts.run` executions racing on the
same server-side key.

The observer is a **credential-free synthetic admission counter**. It is not
OpenAI telemetry and cannot prove what OpenAI received. No paid provider key or
paid request is used. `SITE-P0-TTS-001` may close on this receipt only if the
gate is provider admission at the application boundary; an OpenAI-side
cardinality requirement remains open until independent provider telemetry
exists.

## Isolation contract

The harness may run only in a newly provisioned, dedicated standalone Apps
Script project. The project must be different from every production and
staging script ID, contain no Script Properties or triggers, and be used only
for this canary. `scripts/gas_tts_canary_target.json` deliberately remains
`UNPROVISIONED`; `gas_tts_canary.py prepare` fails closed until a reviewed ID is
recorded there and the `SITE_P0_TTS_CANARY_SCRIPT_ID` repository variable
matches it exactly.

The generated manifest has:

- one API-executable entry point with access `MYSELF`;
- no web-app entry point;
- no external-request, Sheets, Drive, mail, or trigger scope;
- only the minimal `userinfo.email` scope needed for an authenticated private
  execution contract (the harness never reads or returns the email).

The project must use a protected GitHub environment named `tts-canary`,
restricted to default-branch manual dispatch with a required reviewer. That
environment and its target variables do not exist today. Referencing an
unprotected automatically-created environment is not acceptable.

## Reviewed source construction

`scripts/gas_tts_canary.py` reads the selected full Git commit, not uncommitted
working-tree source. It builds a temporary bundle containing only the required
TTS constants, private control functions, and these canonical Governor
functions:

- `jsonp_`, `ttsVoice_`, `ttsConfigured_`, `ttsDay_`, `ttsSessionHash_`;
- `ttsLimit_`, `ttsBudget_`, `purgeExpiredTtsKeys_`, `mintTtsKey_`;
- `claimTts_`, `ttsAudio_`, and `TTS_STYLE_`.

`ttsBudget_`, `mintTtsKey_`, and `claimTts_` remain byte-for-byte identical to
the selected commit and receive individual hashes plus one combined digest.
The build fails if any selected function or constant is missing or duplicated,
or if an extra top-level function appears.

There is one deliberate test-only substitution in the copied `ttsAudio_`:

```text
UrlFetchApp.fetch(
  ->
siteP0CanaryProviderFetch_(
```

The build requires exactly one substitution and then rejects every remaining
`UrlFetchApp` reference. The adapter validates the canonical OpenAI URL,
request shape, closed-list voice, fixed non-credential authorization sentinel,
fixed server-generated synthetic phrase, model, format, and voice style before
incrementing the counter. It never accepts caller-supplied text and never logs
or returns the request, text, audio key, authorization sentinel, or fake audio
body. This overlay exists only in the dedicated canary project and cannot be
selected by the tracked production `.clasp.json`.

## Required live sequence after provisioning

Actual execution remains a second reviewed change because the account-level
prerequisites below are not yet proven. Its driver must perform this sequence:

1. Assert default-branch manual dispatch and the protected `tts-canary`
   environment.
2. Resolve the script ID through the reviewed inventory/variable equality
   check and reject every production/staging ID.
3. Read and hash the dedicated project HEAD, deployment inventory, and version
   inventory. Require no versioned deployment, no trigger, enough room below
   Apps Script's 200-version limit, and an empty Script Property keyset.
4. Generate the source from the exact dispatched commit into runner-temporary
   storage; push only from that directory.
5. Create one immutable version and one temporary API-executable deployment.
   Read both back and require the exact script ID, version, source digest,
   `EXECUTION_API` entry point, `MYSELF` access, and no `WEB_APP` entry point.
6. Invoke the deployment ID through `scripts.run` with `devMode=false`. Never
   run HEAD and never pass the Apps Script project ID as the execution target.
7. Require a clean property-key preflight immediately before `start`. The
   fixed `OPENAI_KEY` sentinel written after preflight is not a credential and
   exists only to exercise canonical `ttsConfigured_` and request construction.
8. Run the matrix below. Audio keys stay in server-side slots and never cross
   the API boundary.
9. Run cleanup and require zero properties, zero tracked cache entries, and
   zero tracked claim markers. Delete the temporary deployment and read back
   its absence. Restore and re-read the exact original HEAD.
10. Validate the allowlisted receipt with `gas_tts_canary.py validate-receipt`
    and required `--commit`, `--run-id`, `--version`, and `--deployment-id`
    values independently obtained from the dispatch and deployment read-back.
    The validator rebuilds the expected bundle and exact-matches every source
    digest. Suppress the PASS receipt if any identity, cleanup, or read-back
    check fails.

One immutable version remains after each run because the Apps Script REST API
does not provide version deletion. The driver must version-cap gate before
mutation and report the residual version as explicit manual-cleanup debt. A
dedicated disposable project avoids spending the Governor staging project's
version budget.

## Acceptance matrix

Each scenario resets its run state and independent counter first:

| Scenario | Preparation | Required provider-admission evidence |
|---|---|---|
| Valid | Mint one fixed-text, server-side claim | accepted; delta `+1` |
| Replay | Reuse that consumed claim | `expired`; delta `0` |
| Concurrent replay | Two `scripts.run` calls cross a two-party barrier on the same slot | one winner, one `expired`, total `1`, loser observed while adapter in flight |
| Session cap | Seed only run budget to effective session cap minus one; mint two same-session keys before consuming either | first delta `+1`; second `tts-session-cap`, delta `0` |
| Daily cap | Seed only run budget to effective daily cap minus one; mint two different-session keys before consuming either | first delta `+1`; second `tts-daily-cap`, delta `0` |

The canonical `TTS_SESSION_CAP` and `TTS_DAILY_CAP` property names remain
read-only. If either effective cap is zero, the canary returns HOLD rather than
changing it. The existing offline negative control deliberately substitutes a
legacy non-consuming claim and confirms that the same observer counts two
provider admissions; this proves the counter can detect the regression.

## Receipt and privacy contract

Only a strict `site-p0-tts-canary.v1` PASS object is accepted. It contains the
repository/commit and source digests, reviewed canary IDs/version, effective
caps, allowlisted scenario outcomes, privacy booleans, and cleanup booleans.
It must label the provider as `credential-free synthetic admission counter`.

The receipt must never contain OAuth material, provider credentials or
authorization strings, property values, email identities, input/generated
text, audio keys, request/response bodies, raw Apps Script errors, logs, or
base64 audio. Raw execution responses stay process-local. Failure output is a
generic gate error, not the remote response.

## Exact account-level blocker

The current `CLASPRC_JSON` history proves authenticated read, push, version,
deployment, and stable staging operations on existing projects. It does not
prove permission to create a new standalone project or invoke a private API
deployment. A private `scripts.run` deployment additionally requires a standard
Google Cloud project shared by the script and OAuth client, Apps Script API
enablement, suitable OAuth scopes/consent, and a deployment-ID execution
client. None is reviewed/configured in current `main`; there is no dedicated
script ID, protected `tts-canary` environment, or canary repository variable.

Therefore this PR is intentionally non-deploying. After it is reviewed and CI
is green, the owner may authorize provisioning one empty dedicated project and
a controlled no-secret capability probe. Until those prerequisites read back,
do not create a project, push the bundle, run `clasp`, create a deployment, or
claim live closure of `SITE-P0-TTS-001`.

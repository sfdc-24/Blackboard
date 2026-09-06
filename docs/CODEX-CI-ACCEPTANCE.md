# VM-CICD-001 acceptance repair for Claude Code

The staging validator could exit successfully for missing URLs, sign-in pages and error responses. The inventory audit could certify arbitrary HTML or pass after finding no deployments. This branch proposes code fixes and a non-deploying PR check for those concrete failures.

## Current validation

82 offline tests pass with no skips: 30 validator/audit tests, three staging-helper tests, 16 target/workflow tests, 15 immutable-build tests, eight health-route tests, four v30 preservation tests, four Google callback tests and two pinned-clasp JSON contract tests. Responses and inventories are mocked; network access is guarded in the validator/audit and build suites. The shell suite executes workflow blocks with a fake clasp in disposable fixtures, including failure before deploy/rollback mutation when source verification fails. The contract tests execute clasp 3.4.1's actual output formatters with fake project methods. No Google token exchange or deployment runs during tests.

From a checkout:

```powershell
$env:GAS_VERSION_ASSERT_PATH = (Resolve-Path scripts/gas_version_assert.py).Path
$env:GAS_DEPLOYMENT_AUDIT_PATH = (Resolve-Path scripts/gas_deployment_audit.py).Path
python -B -m unittest discover -s tests -p 'test_gas_*_acceptance.py' -v
python -B -m unittest discover -s tests -p 'test_staging_*.py' -v
python -B -m unittest discover -s tests -p 'test_build_identity.py' -v
npm ci --prefix tooling/clasp --ignore-scripts --no-audit --no-fund
node --test tests/test_governor_auth.cjs tests/test_clasp_json_contract.cjs tests/test_build_health.cjs tests/test_governor_v30.cjs
```

The new `.github/workflows/ci-acceptance.yml` runs these suites on `pull_request`, uses read-only contents permission, disables persisted checkout credentials, pins its Actions to exact commits, and references no repository deployment secrets. Its result is an offline code check, never a deployment receipt. Node 22 and Python 3.12 are used in CI. Windows shell tests use Git Bash: set `TEST_BASH` to its `bash.exe` path if necessary.

## Exact staging configuration and tool contract

Both staging workflows install clasp 3.4.1 from `tooling/clasp/package-lock.json` with `npm ci --ignore-scripts`, then verify its version. The lock includes transitive dependencies and registry integrity hashes. `npm audit --omit=dev` reported zero vulnerabilities on September 6, 2026. Local actionlint 1.7.12 validation passed for the three changed workflows (external shellcheck/pyflakes integrations disabled).

`scripts/gas_staging_targets.json` records the three staging script IDs from the existing STEP_2 inventory and deployment IDs read from GitHub repository variables on September 6. All nine current variables matched this configuration. This configuration read is not a live Apps Script ownership or health receipt. Each workflow performs its own read-only `clasp --json list-deployments <scriptId>` before any push/redeploy and requires exactly one matching immutable deployment. Staging replacements require a reviewed inventory change plus matching repository variables; variables alone cannot redirect a run.

The exec URL must be exactly `https://script.google.com/macros/s/<reviewed-deployment-id>/exec`, without query, fragment, credentials or a `/dev` substitution. Deployment read-back compares structured IDs and integer versions instead of finding substrings in human-readable descriptions. Version creation uses clasp's JSON output. Rollback validates its free-text input as a canonical positive integer before use; raw input is supplied through an environment variable, never inserted into shell code. Deploy and rollback use the same per-project concurrency group, including push-matrix jobs, and stop after 15 minutes.

The JSON shapes are verified against the [clasp 3.4.1 package](https://www.npmjs.com/package/@google/clasp/v/3.4.1). The authoritative relationship checked before mutation is the [deployment list belonging to the selected script project](https://developers.google.com/apps-script/api/reference/rest/v1/projects.deployments/list).

All seven original Copilot findings on PR #2 are addressed: full checkout history and explicit initial/missing-SHA handling; exact Google issuer allowlist; affirmative boolean email verification; staging helper cleanup with nested/dotfile copying; both PublicInbox.gs references; and the holistic wording correction. Both deployment workflows require the exec URL before mutating staging. Auth checks follow [Google's issuer and claim reference](https://developers.google.com/identity/openid-connect/reference).

## Application and build identity remain distinct

The estate audit requires a positive reviewed JSON health signature. Its existing root-URL probe recognizes the glasses uploader (`sfdc24-glasses-uploader`); it still cannot certify Governor HTML or the sweeper placeholder. The deployment workflows now use the separate [build identity protocol](CICD-BUILD-IDENTITY.md) for the Governor and glasses applications. That protocol checks all committed source at an immutable Apps Script version plus the live `?health=build` marker and fresh nonce. The copied sweeper has no doGet and is explicitly rejected by the builder.

A matching response version is still not proof of a deployed commit. The legacy field validator remains for compatibility and its original regression tests; deployment workflows no longer call it or compare application contract versions with deployment numbers. The new source/build protocol is implemented and tested offline. Current source/scope evidence, complete estate coverage, owner review and actual staging deploy/rollback receipts remain open. These changes do not authorize a public deployment, OAuth consent, Salesforce mutation or production promotion.

Development /dev URLs are inventoried separately and excluded from anonymous /exec readiness. Empty or incomplete versioned inventories fail the overall audit. API/list pagination failures invalidate that project's inventory instead of leaving a partial successful result.

## Scope and review

The earlier acceptance branch began at 8c170db. This follow-up integrates it into `f82eca47fd6c0ad2d93113c0f49e40f8519d58a0` in an isolated checkout, preserving the Governor @29 baseline history. Auth.gs and Code.gs now contain proposed review fixes and are no longer byte-identical to that historical deployment. Historical hash tables are not current source evidence. Integrate through a reviewed PR into session/vm-cicd; do not overwrite the active shared checkout.

The consent packet now prohibits arbitrary function execution and explicitly calls out sweepDraftsToEndpointV2's writes/deletions. An explicitly reviewed no-op and actual staging source/version binding remain owner work. The 403 cause remains a hypothesis until confirmed in the signed-in authorization/error state.

## Historical Foundry evidence

`examples/foundry/vm-cicd-001-resume.work_packet.v1.json` remains a historical packet pinned to 8c170db: 12 evidence items, canonical SHA-256 `44037384093665359db70c6f8ccfb507d48d76b788651a6eefd38a92729f6793`. Its old red-test transcript is evidence of the original defect, not the current repaired result.

It passed local schema/evidence-hash validation only. No live Foundry invocation or assessment occurred. Readiness still requires the configured endpoint identity and the existing governed seam's prerequisite checks and controlled non-production identity proof. Regenerate a new packet for a future review; do not silently rewrite this historical evidence.

References: [Google web apps](https://developers.google.com/apps-script/guides/web?hl=en), [GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

## Current Governor source

The immutable production v30 source was read back and its Code.gs/Monitor.gs changes reconciled without dropping the proposed fixes. See [CICD-GOVERNOR-V30.md](CICD-GOVERNOR-V30.md) for all seven baseline hashes and regression evidence. This removes the stale-v29 baseline gap for PR #5; it does not establish live staging acceptance.

# VM-CICD-001 acceptance repair for Claude Code

The staging validator could exit successfully for missing URLs, sign-in pages and error responses. The inventory audit could certify arbitrary HTML or pass after finding no deployments. This branch proposes code fixes and a non-deploying PR check for those concrete failures.

## Current validation

37 offline tests pass with no skips: 30 validator/audit tests, three staging-helper tests and four Google callback tests. Responses and inventories are mocked; network access is guarded in the validator/audit suite. The shell suite uses a fake clasp in disposable fixtures. No Google token exchange or deployment runs. Coverage includes missing/partial inventories, API errors, pagination, development URLs, oversized/truncated bodies, unknown HTML, wrong services, invalid version JSON, issuer lookalikes, absent/non-boolean email verification, nested/hidden source copying, failure cleanup and initial/missing push history.

From a checkout:

```powershell
$env:GAS_VERSION_ASSERT_PATH = (Resolve-Path scripts/gas_version_assert.py).Path
$env:GAS_DEPLOYMENT_AUDIT_PATH = (Resolve-Path scripts/gas_deployment_audit.py).Path
python -B -m unittest discover -s tests -p 'test_gas_*_acceptance.py' -v
python -B -m unittest discover -s tests -p 'test_staging_helpers.py' -v
node --test tests/test_governor_auth.cjs
```

The new `.github/workflows/ci-acceptance.yml` runs these suites on `pull_request`, uses read-only contents permission, disables persisted checkout credentials, pins both Actions to exact commits, and references no repository deployment secrets. Its result is an offline code check, never a deployment receipt. Windows shell tests use Git Bash: set `TEST_BASH` to its `bash.exe` path if necessary.

All seven original Copilot findings on PR #2 are addressed: full checkout history and explicit initial/missing-SHA handling; exact Google issuer allowlist; affirmative boolean email verification; staging helper cleanup with nested/dotfile copying; both PublicInbox.gs references; and the holistic wording correction. Both deployment workflows require the exec URL before mutating staging. Auth checks follow [Google's issuer and claim reference](https://developers.google.com/identity/openid-connect/reference).

## Application and build identity remain distinct

The audit now needs a positive reviewed JSON health signature. Only the glasses uploader has an existing reviewed service signature (`sfdc24-glasses-uploader`). Governor HTML and the drafts-sweeper placeholder deliberately remain unverified until their owners establish suitable health contracts. The copied sweeper has no doGet.

A matching response version is still not proof of a deployed commit. CI-stamped build identity, exact pipeline-target binding, the complete estate scope, independently verified staging source, and real staging deploy/rollback receipts remain open. These changes do not authorize a public deployment, OAuth consent, Salesforce mutation or production promotion.

Development /dev URLs are inventoried separately and excluded from anonymous /exec readiness. Empty or incomplete versioned inventories fail the overall audit. API/list pagination failures invalidate that project's inventory instead of leaving a partial successful result.

## Scope and review

The earlier acceptance branch began at 8c170db. This follow-up integrates it into `f82eca47fd6c0ad2d93113c0f49e40f8519d58a0` in an isolated checkout, preserving the Governor @29 baseline history. Auth.gs and Code.gs now contain proposed review fixes and are no longer byte-identical to that historical deployment. Historical hash tables are not current source evidence. Integrate through a reviewed PR into session/vm-cicd; do not overwrite the active shared checkout.

The consent packet now prohibits arbitrary function execution and explicitly calls out sweepDraftsToEndpointV2's writes/deletions. An explicitly reviewed no-op and actual staging source/version binding remain owner work. The 403 cause remains a hypothesis until confirmed in the signed-in authorization/error state.

## Historical Foundry evidence

`examples/foundry/vm-cicd-001-resume.work_packet.v1.json` remains a historical packet pinned to 8c170db: 12 evidence items, canonical SHA-256 `44037384093665359db70c6f8ccfb507d48d76b788651a6eefd38a92729f6793`. Its old red-test transcript is evidence of the original defect, not the current repaired result.

It passed local schema/evidence-hash validation only. No live Foundry invocation or assessment occurred. Readiness still requires the configured endpoint identity and the existing governed seam's prerequisite checks and controlled non-production identity proof. Regenerate a new packet for a future review; do not silently rewrite this historical evidence.

References: [Google web apps](https://developers.google.com/apps-script/guides/web?hl=en), [GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

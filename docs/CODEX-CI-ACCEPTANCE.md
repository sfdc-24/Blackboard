# VM-CICD-001 acceptance repair for Claude Code

The staging validator could exit successfully for missing URLs, sign-in pages and error responses. The inventory audit could certify arbitrary HTML or pass after finding no deployments. This branch proposes code fixes and a non-deploying PR check for those concrete failures.

## Current validation

30 offline tests pass with no skips. Responses and inventories are mocked; network access is guarded. Coverage includes missing/partial inventories, API errors, pagination, editor-only development URLs, oversized or truncated bodies, unknown HTML, wrong services, HTTP failures and invalid version JSON. Both valid-response controls and failure controls are included.

From a checkout:

```powershell
$env:GAS_VERSION_ASSERT_PATH = (Resolve-Path scripts/gas_version_assert.py).Path
$env:GAS_DEPLOYMENT_AUDIT_PATH = (Resolve-Path scripts/gas_deployment_audit.py).Path
python -B -m unittest discover -s tests -p 'test_gas_*_acceptance.py' -v
```

The new `.github/workflows/ci-acceptance.yml` runs this suite on `pull_request`, uses read-only contents permission, disables persisted checkout credentials, pins both Actions to exact commits, and references no repository deployment secrets. Its result is an offline code check, never a deployment receipt.

## Application and build identity remain distinct

The audit now needs a positive reviewed JSON health signature. Only the glasses uploader has an existing reviewed service signature (`sfdc24-glasses-uploader`). Governor HTML and the drafts-sweeper placeholder deliberately remain unverified until their owners establish suitable health contracts. The copied sweeper has no doGet.

A matching response version is still not proof of a deployed commit. CI-stamped build identity, exact pipeline-target binding, the complete estate scope, independently verified staging source, and real staging deploy/rollback receipts remain open. These changes do not authorize a public deployment, OAuth consent, Salesforce mutation or production promotion.

Development /dev URLs are inventoried separately and excluded from anonymous /exec readiness. Empty or incomplete versioned inventories fail the overall audit. API/list pagination failures invalidate that project's inventory instead of leaving a partial successful result.

## Scope and review

This branch began at 8c170db. The latest reviewed CI/CD owner head is 1d091ee5cd65eb8670909369473e95f52b828569, which records the Governor @29 baseline. Its source reconciliation does not close the public-site P0s or validate staging. Integrate through a reviewed PR into session/vm-cicd; do not overwrite the active shared checkout.

Before a browser owner uses the consent packet, replace its generic function-run instruction with an explicitly reviewed no-op. Do not run sweepDraftsToEndpointV2: it posts from the real drafts folder and trashes files on HTTP 2xx. Bind any code change to the actual staging source/version. The 403 cause remains a hypothesis until confirmed in the signed-in authorization/error state.

## Historical Foundry evidence

`examples/foundry/vm-cicd-001-resume.work_packet.v1.json` remains a historical packet pinned to 8c170db: 12 evidence items, canonical SHA-256 `44037384093665359db70c6f8ccfb507d48d76b788651a6eefd38a92729f6793`. Its old red-test transcript is evidence of the original defect, not the current repaired result.

It passed local schema/evidence-hash validation only. No live Foundry invocation or assessment occurred. Readiness still requires the configured endpoint identity and the existing governed seam's prerequisite checks and controlled non-production identity proof. Regenerate a new packet for a future review; do not silently rewrite this historical evidence.

References: [Google web apps](https://developers.google.com/apps-script/guides/web?hl=en), [GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

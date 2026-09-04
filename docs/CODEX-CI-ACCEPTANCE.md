# VM-CICD-001 independent acceptance handoff

Prepared by chatgpt-codex-desktop after the machine restart. This branch starts at `8c170dbfe6e56145f03ae518b59b01ab4db7e400` and contains review fixtures and evidence only.

## Offline regression

`tests/test_gas_version_assert_acceptance.py` accepts the validator and optional deployment audit paths through environment variables. It blocks network and sleeps.

PowerShell example from a checkout of this branch:

```powershell
$env:GAS_VERSION_ASSERT_PATH = (Resolve-Path scripts/gas_version_assert.py).Path
$env:GAS_DEPLOYMENT_AUDIT_PATH = (Resolve-Path scripts/gas_deployment_audit.py).Path
python -B tests/test_gas_version_assert_acceptance.py -v
```

Historical run: 9 test methods, 11 failures including subtests, exit 1. The valid-version controls pass and wrong versions fail. Red tests demonstrate missing-URL success, wrapper/sign-in soft passes, versionless JSON soft passes, uncontrolled scalar JSON errors, and an audit marker hidden after byte 400. This is deliberately a failing acceptance suite against the historical code, not a green build claim. The audit snapshot matches the audit subsequently committed in 8c170db.

## Foundry

`examples/foundry/vm-cicd-001-resume.work_packet.v1.json` passed the existing adapter's `load_work_packet` validation. It has 12 evidence items and canonical packet SHA-256 `44037384093665359db70c6f8ccfb507d48d76b788651a6eefd38a92729f6793`.

No live Foundry invocation or Foundry assessment occurred. azd and a configured endpoint identity are unavailable in this task. The packet is ready for the existing governed Foundry seam after its normal prerequisites and controlled non-production identity verification; evidence is data with instruction_authority=NONE. The calling orchestrator remains responsible for validation and any routing.

Source evidence is pinned to 8c170db. The network observations in the scope packet are attributed to vm-cli, not independently repeated by this task. The three byte-hash mismatches for Index.html, PublicInbox.gs and Reception.html are explained by CRLF working files versus LF Git blobs. None establishes remote staging source identity.

## Acceptance corrections for Claude Code

- Keep the PR validation work moving while browser authorization is blocked. workflow_dispatch registration and pull_request checks are different triggers.
- Do not execute `sweepDraftsToEndpointV2` for consent: it reads the real drafts folder, posts to the existing endpoint, and trashes files on HTTP 2xx. The committed copy has no doGet. Consent cannot create a web entry point.
- Replace "run any harmless function" with an explicitly reviewed no-op; declare/hash any source change. Treat the proposed OAuth cause as a hypothesis until confirmed in the signed-in authorization/error state.
- Classify development /dev URLs separately from versioned /exec targets; /dev requires editor access according to Google.
- Make audit failure include missing deployments, API errors, and absent expected application identity. Blacklisting a few strings is not positive application proof.
- Require build identity, strict failures and independently checked staging source/deployment bindings before deploy/rollback acceptance.

References: [Google web apps](https://developers.google.com/apps-script/guides/web?hl=en), [GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

# STEP_2 — DEV / STAGING project ids (VM-CICD-001)

Created 2026-09-04 by vm-cli via Drive `files.copy` (no clasp OAuth required).
Each copy is a byte-exact duplicate of its production project's HEAD — the
Governor STAGING copy's `Code.gs` sha256 was verified equal to the committed
baseline (`afb4a45f…`). **These are Apps Script *projects* only.** Each still
needs a web-app *deployment* (a `/exec` URL), which is NOT a Drive operation —
it needs `clasp create-deployment` or the editor, i.e. the `CLASPRC_JSON`
unblock in [CICD.md](CICD.md).

| Project | Env | scriptId | Repo variable (STAGING) |
|---|---|---|---|
| governor-page-api | STAGING | `1S4LQE0SQwnE9kngxMhRngyAmSNGhFtDRxoYQqIxSSH0UoQ5u7T84MKU1` | `STAGING_SCRIPT_ID_GOVERNOR_PAGE_API` |
| governor-page-api | DEV | `1NBDw5Ya81Y8XlDME01N7GGM7UwproSvqIdtaeuMBu4iAhlGfDbYwmeoP` | — |
| blackboard-production | STAGING | `1IxPEx_5gT7HGEMwOFSB7wcO6qPUTMLr32qAzDykGYJ5KdrxscQeoZC1K` | `STAGING_SCRIPT_ID_BLACKBOARD_PRODUCTION` |
| blackboard-production | DEV | `1sOVS20USpkDZ7po8wFK-DgfujYI_1Vq-jgcGUEhJyCIGLZ0k0bRoMNAf` | — |
| glasses-intake-uploader | STAGING | `1ElMYYzbdCsKfnhN30Gxt47TUoSFZg7ixGNTEdZNJORolpsS9eXVaYWuI` | `STAGING_SCRIPT_ID_GLASSES_INTAKE_UPLOADER` |
| glasses-intake-uploader | DEV | `1rjnl6ianZEKilaWFZDE3CfZANmUCArAN9sHB8NR6RCpQDUDgrBiQ-uoD` | — |

## Remaining to finish STEP_2 (needs the OAuth-holder / a browser)

For each STAGING project, once clasp is authenticated:

```bash
cd apps-script/<project>
printf '{"scriptId":"<STAGING scriptId>","rootDir":"."}\n' > .clasp.json
clasp push -f                       # source already identical; makes it explicit
clasp create-deployment -d "staging web app"
clasp list-deployments             # copy the deployment id and the /exec URL
gh variable set STAGING_DEPLOYMENT_ID_<KEY> --body "<deployment id>"
gh variable set STAGING_EXEC_URL_<KEY>     --body "https://script.google.com/macros/s/<...>/exec"
```

`STAGING_SCRIPT_ID_*` are set as repo variables already (see the table). KEY =
the project dir upper-cased, `-` → `_`.

## Caveats

- **blackboard-production** copies duplicate a HEAD that is only the drafts
  sweeper, not the live V2 gateway (the deployed gateway code lives in a
  different, container-bound project). A staging copy of it is a placeholder
  until the real gateway source is baselined — don't treat it as the gateway.
- The two `Blackboard Production` copies landed in the shared-drive root, not
  the "SFDC 24 - Claude" folder, because the original lives there and the copy
  API keeps the source parent. Harmless; findable by title.
- Trashing any copy is one click if the layout changes — nothing depends on
  these ids except the repo variables above.

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

**This section is DONE — completed 2026-09-04 19:31Z**, after this file was
written. The commands below are kept because they are the recipe if a staging
project is ever rebuilt, but they no longer describe outstanding work:

```bash
cd apps-script/<project>
printf '{"scriptId":"<STAGING scriptId>","rootDir":"."}\n' > .clasp.json
clasp push -f                       # source already identical; makes it explicit
clasp create-deployment -d "staging web app"
clasp list-deployments             # copy the deployment id and the /exec URL
gh variable set STAGING_DEPLOYMENT_ID_<KEY> --body "<deployment id>"
gh variable set STAGING_EXEC_URL_<KEY>     --body "https://script.google.com/macros/s/<...>/exec"
```

All nine variables are set (`gh variable list`), and each staging project has
one `@1` web-app deployment described `staging web app (VM-CICD-001)`. KEY =
the project dir upper-cased, `-` → `_`.

## The deployments exist and still do not work — verified 2026-09-04 23:10Z

Creating the deployment was not the same as having a working endpoint, and the
repo variables cannot tell the difference. Measured with
`python scripts/gas_deployment_audit.py`:

| Project | @1 anonymous | @HEAD anonymous | API says |
|---|---|---|---|
| governor-page-api | **403** | 200 Google sign-in page | `ANYONE_ANONYMOUS` |
| blackboard-production | **403** | 200 Google sign-in page | `ANYONE_ANONYMOUS` |
| glasses-intake-uploader | **403** | 200 Google sign-in page | `ANYONE_ANONYMOUS` |

0 of 6 web-app deployments actually run. The same probe against the live v1 bus
and glasses production endpoints returns their real JSON, so the probe is
sound — these endpoints genuinely do not serve.

Re-probed with the **owner's** OAuth token: still 403. The access setting is
not the problem; the copied scripts have never been OAuth-authorized. A project
created by Drive `files.copy` brings its code and its manifest but not its
grant. Clearing it needs a browser once per project (see item 4 of
[CICD.md](CICD.md)) — it cannot be done from a headless lane.

Two traps worth keeping, both of which nearly produced a false "green" here:

- **HTTP 200 is not the app.** Google's sign-in page and its Drive notice page
  both arrive with 200. The first version of the audit counted three endpoints
  as anonymous-ready on status alone; all three were Google pages. The checker
  now requires that no Google interstitial appears in the body, and is
  regression-checked against two endpoints known to serve anonymously.
- **The manifest is not the deployment.** Every manifest here declares
  `ANYONE_ANONYMOUS` and every endpoint refuses callers. Read the deployment
  config from the API, then probe the URL, and treat disagreement as the finding.

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

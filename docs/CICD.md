# CI/CD for the Apps Script estate — VM-CICD-001

Built by vm-cli, 2026-09-04, under dispatch `WRK-cm-vmcicd-001` (chat-mobile,
authority Mr. Salam direct). **Staging only** — nothing in here touches a
production deployment id, DNS, or the theme.

## The environment model (Mr. Salam's three environments)

| Env | What it is | Deploys how |
|---|---|---|
| DEV | A separate Apps Script *copy* of each project. Anyone breaks it freely. | `clasp push` straight from a working tree, or the editor. No ceremony. |
| STAGING | A separate Apps Script copy per project with ONE stable web-app deployment. | **GitHub Actions only** — `.github/workflows/staging-deploy.yml` on push to the `staging` branch (or manual dispatch). |
| PROD | The existing live projects/deployments. | **Manual, by a human, unchanged tonight.** Automating prod is a separate ruling. |

The source of truth is `apps-script/<project>/` in this repo:

| Dir | Live project (scriptId) | Notes |
|---|---|---|
| `apps-script/governor-page-api/` | `1lTbqTZ3DBHI2WyJu19Lf1M0a4J01aEuH45c2VcT6Rzxy0vxE2S8FYUEp` | The site/governor endpoint. Prod deployment `AKfycbx0D-5DAn…` (v25/v26 era). |
| `apps-script/blackboard-production/` | `1XBE2qVMiIu8xOq5jks4T3BG3o6CRFx8HKsWXvbXIJ6sOefPUBN-bVOh-` | The V2/Alpha gateway. |
| `apps-script/glasses-intake-uploader/` | `1PBfO1sPQmGTXPHWrAAot2wCgSCPizO2uC8RUwUKQ5hn7A7dUq0U4_Q_2` | Already exposes `CONTRACT_VERSION` on GET — the model citizen. |
| *(v1 bus)* | container-bound to Blackboard - Alpha DB | **Not exportable by API without Apps Script API OAuth.** Repo root `Code.gs` is the 2026-08-25 paste; treat as UNVERIFIED against live until pulled via Sheet → Extensions → Apps Script. |

## Why the version assertion exists

On 2026-09-03 a security fix was "deployed" six times; the one deploy that
completed silently republished Version 12 — the unfixed code — while reporting
success. There is no outside way to tell what is live. So every staging deploy
here must pass TWO independent read-backs before it is called done:

1. `clasp list-deployments` shows the staging deployment id at the new version.
2. The live `/exec` GET reports that version (`scripts/gas_version_assert.py`).
   Projects that don't expose a version field yet soft-pass with a loud
   warning; adding `CONTRACT_VERSION` to each `doGet` is the first change that
   should ride this pipeline, after which the assertion goes `--strict`.

Rollback is `staging-rollback.yml` — repoint the deployment at a known-good
version, same two assertions. Written before it was needed, as ordered.

## What blocks full activation (the honest list)

1. **`CLASPRC_JSON` secret** — clasp OAuth. clasp is installed on the VM but
   `clasp login` needs a browser consent nobody can click while Mr. Salam is
   offline. Unblock paths, either works:
   - vm-chrome completes `clasp login --no-localhost` consent in its browser
     (account `abdus@sfdc24.com`, Apps Script API already enabled Sep 3);
   - the laptop (PARITY-001 credential equal) copies its working
     `~/.clasprc.json` into the repo secret:
     `gh secret set CLASPRC_JSON < ~/.clasprc.json` — one command.
2. **STEP_2 staging copies** — the DEV and STAGING projects per app, then repo
   variables `STAGING_SCRIPT_ID_<KEY>`, `STAGING_DEPLOYMENT_ID_<KEY>`,
   `STAGING_EXEC_URL_<KEY>` (KEY = dir name upper-cased, `-` → `_`).
   With clasp: `clasp create-script --title "<name> - STAGING"`, push the
   baseline, `clasp create-deployment`, record ids. Scriptable the moment
   item 1 clears; `scripts/` will grow a helper for it.
3. **The v1 bus baseline** — someone with a browser opens the Alpha DB sheet →
   Extensions → Apps Script and pastes the live `Code.gs` back into the repo
   (or clasp-clones it once OAuth exists — container-bound scripts have
   scriptIds too, visible in the editor's project settings).

Until item 1 clears, pushes to `staging` fail at the auth step with an explicit
error — that is intended behaviour, not breakage: a pipeline that silently
skipped deploys would be theatre.

## Guardrails carried from the dispatch

- No production deploys from CI. No DNS. No theme work.
- `www` must NEVER point at the NameSilo forwarding server (the Sep 3 loop).
- Never trust `ok:true` without read-back (ISSUE 020); reads retry freely,
  writes never retry on response alone.
- Apps Script redirects must be followed with a bare GET (encoded in
  `gas_version_assert.py`).

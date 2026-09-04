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
| `apps-script/blackboard-bus-v1/` | `1meav8p2zkRt-8obarV_fB5Q2EyExCvAaoZa3ro9_fmo4OE_95FpWkfu9` | The v1 bus. Baselined 2026-09-04. Prod deployment `AKfycbwCLtG9…RcXrjQ` is pinned at **@1**. |

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

1. ~~**`CLASPRC_JSON` secret**~~ — **CLEARED 2026-09-04 18:54Z.** clasp is
   authenticated on the VM as `abdus@sfdc24.com` and the credential is in the
   repo secret `CLASPRC_JSON`. Verified by read-back (`gh secret list`) and by
   `clasp list-scripts` returning 12 projects.
   *Recorded because it will be needed again:* the consent was completed in a
   Chrome that is **not on this VM**, so the `http://localhost:<port>` redirect
   died in that browser. It does not matter — the authorization code is in the
   redirect URL. Copy it and replay the whole redirect URL with `curl` on the
   machine where `clasp login` is waiting. The listener completes the exchange
   and writes `~/.clasprc.json`. No `--no-localhost` code-paste dance needed.
2. **STEP_2 staging copies** — the DEV and STAGING projects per app, then repo
   variables `STAGING_SCRIPT_ID_<KEY>`, `STAGING_DEPLOYMENT_ID_<KEY>`,
   `STAGING_EXEC_URL_<KEY>` (KEY = dir name upper-cased, `-` → `_`).
   Owned by the other vm-cli session (`blackboard-71`) via Drive `files.copy`.
   This is now the only thing standing between the pipeline and a green run.
3. ~~**The v1 bus baseline**~~ — **DONE.** It did not need a browser after all:
   the bus is container-bound but it *does* appear in `clasp list-scripts` and
   clones cleanly. It is at `apps-script/blackboard-bus-v1/`, byte-identical to
   the source pinned at the live deployment.

Until item 2 clears, pushes to `staging` fail at the id-resolution step with an
explicit error — that is intended behaviour, not breakage: a pipeline that
silently skipped deploys would be theatre.

## The source of truth for what is deployed — it exists now

`scripts/gas_get_version.py <scriptId> <versionNumber> <outDir>` fetches the
source **pinned at a specific version**, not HEAD. This is the read-back that
did not exist on 2026-09-03. Combined with `clasp list-deployments`, which
prints the version each deployment is pinned to, a deploy can no longer lie:

```
clasp list-deployments                       # which version is this URL serving?
python scripts/gas_get_version.py <id> <n> ./out   # what IS that version?
```

### LANE_0 result — 2026-09-04, TESTED, not believed

- Governor Page API prod deployment `AKfycbx0D-5DAn…` is pinned at **@26**
  ("v26 one at a time"). Fetched version 26's actual source.
- **All four exposed utilities carry `requireGovernor_()` in the deployed
  version**: `post_reset`, `seed_state`, `test_chat`, `test_read`. The Sep-3
  security fix IS live. Previously this could only be reported BELIEVED —
  the anonymous `?format=json` probe proves a visitor is not Governor, but it
  says nothing about whether the pinned version contains the guard.
- Deployed v26 is **byte-identical** to the repo baseline across all seven
  files. No unreleased drift on that project.
- v1 bus: prod URL `AKfycbwCLtG9…RcXrjQ` is pinned at **@1, "Initial Deploy"**,
  and @1 is identical to HEAD. So **L-70 is real** — the bus source genuinely
  never gained a `replace` action; it is not a stale-deployment artifact.
  Correction to the folklore, though: **`ping` DOES work.**
  `GET ?action=ping&secret=…` returns
  `{"ok":true,"service":"sfdc24-blackboard-bus","time":…}`.
- Also corrected: bus `append` requires `title`. Passing `fileId` returns
  `{"ok":false,"error":"title is required."}` even though `read` accepts it.

## Guardrails carried from the dispatch

- No production deploys from CI. No DNS. No theme work.
- `www` must NEVER point at the NameSilo forwarding server (the Sep 3 loop).
- Never trust `ok:true` without read-back (ISSUE 020); reads retry freely,
  writes never retry on response alone.
- Apps Script redirects must be followed with a bare GET (encoded in
  `gas_version_assert.py`).

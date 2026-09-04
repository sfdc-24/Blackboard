# STAGING authorization packet — VM-CICD-001

**This is the handoff for the one thing the headless lane cannot do.** The
STAGING Apps Script projects exist, their web-app deployments exist, and every
one of them refuses to run. Clearing that needs a browser signed in as
`abdus@sfdc24.com`, once per project, and then never again.

Prepared by vm-cli, 2026-09-04 23:15Z, at commit `90880a6`. Everything below is
measured, not assumed; the commands to re-measure are included so nobody has to
take this on trust.

## The halt, stated precisely

| Project (STAGING) | `@1` anonymous GET | `@HEAD` anonymous GET | Apps Script API says |
|---|---|---|---|
| governor-page-api | **403** | 200 + Google sign-in page | `ANYONE_ANONYMOUS` / `USER_DEPLOYING` |
| blackboard-production | **403** | 200 + Google sign-in page | `ANYONE_ANONYMOUS` / `USER_DEPLOYING` |
| glasses-intake-uploader | **403** | 200 + Google sign-in page | `ANYONE_ANONYMOUS` / `USER_DEPLOYING` |

0 of 6 web-app deployments serve their app. The same probe against the live v1
bus and glasses **production** endpoints returns their real JSON, so the probe
is sound and these endpoints genuinely do not serve.

Re-probed with the **owner's own OAuth token**: still 403. That is the finding
that settles the cause. If this were the `access` setting, the owner would get
through. Nobody gets through, so the script itself is unauthorized: a project
created by Drive `files.copy` brings its code and its manifest, but not the
OAuth grant. An `executeAs: USER_DEPLOYING` web app cannot run for anyone until
its owner authorizes it once.

Independently reached by two sessions on this machine from different tooling
(this lane via `scripts/gas_deployment_audit.py`; the VS Code twin via its own
`deployments.list` inspection) before either had seen the other's work.

Re-measure at any time — it exits non-zero while any deployment claims
anonymous access it does not honour:

```bash
python scripts/gas_deployment_audit.py
```

## What to do — 2 minutes per project, in a browser

Signed in as `abdus@sfdc24.com`:

1. Open the STAGING project in the Apps Script editor (link below).
2. Run any function once (`Run` ▸ pick anything harmless).
3. Accept the OAuth consent when Google asks. Read the next section first —
   one of these asks for permission to send email as you.
4. That is all. Do **not** create a new deployment; the `@1` deployment already
   exists and is wired to the repo variables. Creating another one silently
   orphans the pipeline's `STAGING_DEPLOYMENT_ID_*`.

| Project | STAGING scriptId | Editor |
|---|---|---|
| governor-page-api | `1S4LQE0SQwnE9kngxMhRngyAmSNGhFtDRxoYQqIxSSH0UoQ5u7T84MKU1` | https://script.google.com/d/1S4LQE0SQwnE9kngxMhRngyAmSNGhFtDRxoYQqIxSSH0UoQ5u7T84MKU1/edit |
| blackboard-production | `1IxPEx_5gT7HGEMwOFSB7wcO6qPUTMLr32qAzDykGYJ5KdrxscQeoZC1K` | https://script.google.com/d/1IxPEx_5gT7HGEMwOFSB7wcO6qPUTMLr32qAzDykGYJ5KdrxscQeoZC1K/edit |
| glasses-intake-uploader | `1ElMYYzbdCsKfnhN30Gxt47TUoSFZg7ixGNTEdZNJORolpsS9eXVaYWuI` | https://script.google.com/d/1ElMYYzbdCsKfnhN30Gxt47TUoSFZg7ixGNTEdZNJORolpsS9eXVaYWuI/edit |

Then re-run the audit. When it exits 0, this halt is cleared.

## Exactly what you will be consenting to

No project declares `oauthScopes` in its manifest, so Google infers them from
the code. Derived by reading the committed source at `90880a6` — the service,
the scope it implies, and where it is actually called:

### governor-page-api — **read this one before clicking**

| Service | Scope | Where |
|---|---|---|
| `MailApp.sendEmail` | `script.send_mail` — **send email as you** | `Monitor.gs:94` |
| `SpreadsheetApp` | `spreadsheets` | 6 call sites |
| `UrlFetchApp` | `script.external_request` | 4 call sites |
| `Session.get*User().getEmail()` | `userinfo.email` | `Code.gs:354,356`, `Monitor.gs:93` |
| `PropertiesService`, `CacheService`, `LockService`, `HtmlService`, `ScriptApp.getService()`, `Utilities` | none | — |

The consent screen will ask to **send email on your behalf**. That is real, and
it is the monitor's alert path, not a mistake — but it is a genuine grant on a
copy of a project, so it deserves a deliberate click rather than a reflexive
one. If you would rather not grant it on a staging copy, say so on the board:
the alternative is to stub `Monitor.gs`'s mail call in the staging source, which
is a source change that must itself ride the pipeline.

### blackboard-production

| Service | Scope | Notes |
|---|---|---|
| `DriveApp` | `drive` | |
| `DocumentApp` | `documents` | |
| `UrlFetchApp` | `script.external_request` | |
| `PropertiesService` | none | |

Reminder from [CICD-STAGING-IDS.md](CICD-STAGING-IDS.md): this project's HEAD is
only the drafts sweeper, **not** the live V2/WhatsApp gateway. Authorizing it
does not make it the gateway.

### glasses-intake-uploader

| Service | Scope | Notes |
|---|---|---|
| `DriveApp` | `drive` | |
| `PropertiesService`, `Utilities` | none | |

## The source these hashes pin

SHA-256 of the committed baseline at `90880a6`, so the person clicking consent
can confirm they are authorizing the code that was reviewed:

```
governor-page-api/
  Auth.gs         1b7fa67196c3c904d3b389138ef8af4efec4e5612982d5134a88b3d82183dfb8
  Code.gs         6d05d05a0cff75c23eaf32cf0bca28939107b9d58b20a45db102fbb7e5573aa5
  Index.html      fd4f5c9699b8ac09069b70accdf237461d0d23df019d5e5b4eac07a0e0a75c04
  Monitor.gs      61858c207798a4693acc089749927cd21c52931ab0ff1656f911cef18f21bd28
  PublicInbox.gs  a1a889db5594c63cbe56bff3e504bf96eaef1f15b6a52e35548b767e44f2aa69
  Reception.html  2564a9998bbd470655b53e1b93e0f9c0a4dc931796417fe1f828162d16d92a7c
  appsscript.json b03c2319c8a52aa7c16e431b045262af1ad5c8937441f389e458ff75d50b98fd
blackboard-production/
  Code.gs         891627fe60910cebd1c171a5911f9e3527bf42d51ae135a4daa673df1b10b316
  appsscript.json b03c2319c8a52aa7c16e431b045262af1ad5c8937441f389e458ff75d50b98fd
glasses-intake-uploader/
  Code.gs         d4dfd2eb076ebcef90d4f9db39207086bc3f638b0748502e87bf1470c7e3b3ee
  appsscript.json b03c2319c8a52aa7c16e431b045262af1ad5c8937441f389e458ff75d50b98fd
blackboard-bus-v1/            (baseline only — no staging copy, nothing to authorize)
  Code.gs         19f8ab99b188b78fb362d3a02e502439ed0730165ba7e2378c446de3dce79ed1
  appsscript.json b03c2319c8a52aa7c16e431b045262af1ad5c8937441f389e458ff75d50b98fd
```

Regenerate with `sha256sum apps-script/<project>/*`.

## Staging only

Every id in this document is a STAGING copy. No production scriptId, deployment
id or `/exec` URL appears here, and nothing in this packet asks anyone to touch
a production deployment, DNS, or the theme. Production deploys stay manual until
Mr. Salam rules otherwise.

## After the consent — the assertion still is not strict

Clearing this halt makes the endpoints reachable. It does **not** make the
version assertion strict, and that gap should not be quietly inherited:

`glasses-intake-uploader` exposes `CONTRACT_VERSION`, an API-contract number
that will never equal a clasp version number, so `--expect-version <clasp
version>` cannot pass strictly on any project as currently written. The honest
fix is a CI-stamped build marker — the commit SHA written into a generated `.gs`
before `clasp push` and echoed back by `doGet` — which is a source change to
each project and must itself ride staging first. Raised here rather than left
for whoever next reads a soft-pass warning and assumes it means "verified".

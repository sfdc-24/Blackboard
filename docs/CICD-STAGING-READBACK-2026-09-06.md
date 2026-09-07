# Staging source and authorization evidence — September 6, 2026

At 23:38 UTC, all three configured staging deployments remained at immutable
version 1 and returned anonymous HTTP 403. This refresh supersedes the source
and consent assumptions in the historical CICD-STAGING-AUTH.md packet.

## Measured source and deployment state

Read-only Apps Script API calls fetched each exact configured deployment,
`projects.getContent?versionNumber=1`, and the project's current HEAD. Deployment
metadata was fetched again after the HTTP probe; it remained unchanged. A separate
paginated estate audit found three versioned web apps and no incomplete project
inventories. Editor-only `/dev` deployments were excluded from anonymous readiness.

| Staging project | Version | Anonymous HTTP | HEAD equals v1 | Review source equals v1 |
|---|---|---|---|---|
| governor-page-api | 1 | 403 | Yes | No |
| blackboard-production (sweeper) | 1 | 403 | Yes | Yes |
| glasses-intake-uploader | 1 | 403 | Yes | No |

All three deployment configurations report `ANYONE_ANONYMOUS` and `USER_DEPLOYING`.
None contains a generated BuildIdentity file. The sweeper has no `doGet` function;
it cannot meet the web GET contract merely by receiving additional authorization.
Governor's differing files are Auth.gs, Code.gs and Monitor.gs. Glasses differs
only in Code.gs. Their deployment versions must not be described as production
Governor v30 or as builds of the review branch.

The comparison commit is `1d26dde2c40d25cbb95e529e449e5922dc778df0` (PR #5).
It is a comparison target awaiting owner review, not a claim that review was
approved. PR #4's additional voice changes require their own exact build receipt.

[The machine-readable evidence](evidence/staging-source-2026-09-06.json) records
the exact script/deployment identities, UTC observation time, per-file SHA-256,
complete source-set hashes, and service call sites for remote and proposed code.
JavaScript/HTML is UTF-8 with LF endings; manifest JSON is strict parsed, sorted
and compact. Source-set hashes use the sorted name/type/source records defined
in CICD-BUILD-IDENTITY.md. These are explicitly normalized hashes, not hashes of
arbitrary local checkout bytes. No source bodies, property values or OAuth tokens
are included in the evidence file.

## Source-derived scope review

The pinned v1, current HEAD and comparison-commit manifests all omit `oauthScopes`.
The following mapping is an inference from the complete fetched source and
Google's method documentation. It is not an observation of granted scopes or the
consent screen. Google scans project code to infer scopes; even commented code
can affect that scan. [Google authorization guide](https://developers.google.com/apps-script/guides/services/authorization)

Scope names below use the prefix `https://www.googleapis.com/auth/`.
Call sites identify the remote immutable v1 source; full call-site inventories
for the comparison commit are in the JSON evidence.

| Project | Required scope inferred from source | Concrete v1 call sites and effect |
|---|---|---|
| Governor | `script.send_mail` | Monitor.gs:94 sends an alert through [MailApp.sendEmail](https://developers.google.com/apps-script/reference/mail/mail-app#sendEmail(String,String,String)). |
| Governor | `spreadsheets` | Code.gs:444 and PublicInbox.gs:64 use [SpreadsheetApp.openById](https://developers.google.com/apps-script/reference/spreadsheet/spreadsheet-app#openById(String)) to access the board. |
| Governor | `script.external_request` | Auth.gs:151, Code.gs:175, Monitor.gs:114,131 call [UrlFetchApp.fetch](https://developers.google.com/apps-script/reference/url-fetch/url-fetch-app). |
| Governor | `userinfo.email` | Code.gs:292,294 and Monitor.gs:93 read [active/effective user identity](https://developers.google.com/apps-script/reference/base/session). |
| Sweeper | `drive` | Code.gs:6 reads the drafts folder; Code.gs:51 [trashes a file](https://developers.google.com/apps-script/reference/drive/file#setTrashed(Boolean)) after an HTTP 2xx response. |
| Sweeper | `documents` | Code.gs:15 reads document text using [DocumentApp.openById](https://developers.google.com/apps-script/reference/document/document-app#openById(String)). |
| Sweeper | `script.external_request` | Code.gs:43 posts drafts to its existing endpoint. |
| Glasses | `drive` | Code.gs:110 creates an upload file; Code.gs:185 trashes older captures. This is write access, not read-only Drive access. |

The comparison-commit code uses the same scope-bearing service families. That
does not establish that an earlier grant exists, remains sufficient, or authorizes
a particular future build. Generated build-marker code itself makes no service
calls; project-wide authorization still needs inspection for the selected source.

## Diagnosis and next action

The available browser opened the Governor staging editor and reached Google's
sign-in page. It has no signed-in owner session, so the actual editor/deployment
error and granted scopes could not be inspected. This is a browser-session
limitation; the successful OAuth API reads do not prove web-app authorization.
The HTTP 403 alone does not identify the cause.

Next, inspect the existing Governor staging project while signed in as its owner,
record the exact error, and compare the editor's scope list with this source
evidence before choosing a remedy. Do not execute the sweeper, upload, prune,
monitor, or provider functions as an authorization probe. If an authorization
probe is needed, prepare a reviewed no-op with no service calls, bind it to the
intended staging source, and refresh the source evidence before consent.

After owner adoption, the two supported web apps need actual stamped staging
deploy and rollback receipts as described in CICD-BUILD-IDENTITY.md. This report
is a source/configuration observation, not a successful deployment, rollback,
functional acceptance result, or production promotion approval.

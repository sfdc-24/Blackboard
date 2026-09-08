# STAGING authorization packet — VM-CICD-001

**Historical probe packet; authorization cause and live readiness remain open.**
Use [the September 6 read-back](CICD-STAGING-READBACK-2026-09-06.md) for current
remote version/HEAD hashes, source-derived scopes and browser-session evidence.
The recorded STAGING versioned endpoints returned 403. A signed-in owner must
inspect the actual authorization/deployment error before selecting a remedy.
The source hashes below are historical and must be refreshed for reviewed code
and compared with the exact remote staging version before any authorization.

Prepared by vm-cli, 2026-09-04 23:15Z, at commit `90880a6`. Everything below is
records measurements from that time; later corrections are distinguished below.

## The halt, stated precisely

| Project (STAGING) | `@1` anonymous GET | `@HEAD` anonymous GET | Apps Script API says |
|---|---|---|---|
| governor-page-api | **403** | 200 + Google sign-in page | `ANYONE_ANONYMOUS` / `USER_DEPLOYING` |
| blackboard-production | **403** | 200 + Google sign-in page | `ANYONE_ANONYMOUS` / `USER_DEPLOYING` |
| glasses-intake-uploader | **403** | 200 + Google sign-in page | `ANYONE_ANONYMOUS` / `USER_DEPLOYING` |

The three recorded versioned `/exec` probes did not prove application readiness.
The three `/dev` probes are editor-only and must be classified separately;
their sign-in pages do not establish an anonymous `/exec` configuration defect.

Re-probed with the **owner's own OAuth token**: still 403. This confirms a failed
probe, not its cause. Missing scope grants remain a hypothesis until the owner
records the signed-in error and checks the exact staging source/configuration.

Independently reached by two sessions on this machine from different tooling
(this lane via `scripts/gas_deployment_audit.py`; the VS Code twin via its own
`deployments.list` inspection) before either had seen the other's work.

Re-measure at any time — it exits non-zero while any deployment claims
anonymous access it does not honour:

```bash
python scripts/gas_deployment_audit.py
```

## Owner inspection and bounded authorization

Signed in as `abdus@sfdc24.com`:

1. Open the STAGING project in the Apps Script editor (link below).
2. Record the actual authorization/deployment error and compare the remote
   source with reviewed, representation-labelled hashes and scopes.
3. Do not run an arbitrary function. In particular, do not run
   `sweepDraftsToEndpointV2`: it reads the real drafts folder, posts to the
   existing board endpoint and trashes files on HTTP 2xx. The committed copy
   also lacks `doGet`; granting scopes cannot create a GET entry point.
4. If consent is needed, first prepare and review an explicit no-op with no
   data, network or provider actions, then bind that source to staging and
   refresh the scope/hash evidence. Authorize only those reviewed staging
   scopes through the existing owner process. No no-op execution is claimed here.
5. Preserve the configured deployment IDs. Any source/version update needs
   its own read-back; creating an unrelated deployment does not repair the
   pipeline's `STAGING_DEPLOYMENT_ID_*` binding.

| Project | STAGING scriptId | Editor |
|---|---|---|
| governor-page-api | `1S4LQE0SQwnE9kngxMhRngyAmSNGhFtDRxoYQqIxSSH0UoQ5u7T84MKU1` | https://script.google.com/d/1S4LQE0SQwnE9kngxMhRngyAmSNGhFtDRxoYQqIxSSH0UoQ5u7T84MKU1/edit |
| blackboard-production | `1IxPEx_5gT7HGEMwOFSB7wcO6qPUTMLr32qAzDykGYJ5KdrxscQeoZC1K` | https://script.google.com/d/1IxPEx_5gT7HGEMwOFSB7wcO6qPUTMLr32qAzDykGYJ5KdrxscQeoZC1K/edit |
| glasses-intake-uploader | `1ElMYYzbdCsKfnhN30Gxt47TUoSFZg7ixGNTEdZNJORolpsS9eXVaYWuI` | https://script.google.com/d/1ElMYYzbdCsKfnhN30Gxt47TUoSFZg7ixGNTEdZNJORolpsS9eXVaYWuI/edit |

Then rerun the audit. A health-signature match establishes only the reviewed
application response. Source/build identity and actual deploy/rollback receipts
are separate release gates; an audit exit code alone does not clear them.

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

SHA-256 of the committed baseline (governor-page-api re-synced to prod @29 on
2026-09-05; the rest unchanged since `90880a6`), so the person clicking consent
can confirm they are authorizing the code that was reviewed:

```
governor-page-api/
  Auth.gs         1b7fa67196c3c904d3b389138ef8af4efec4e5612982d5134a88b3d82183dfb8
  Code.gs         06d68f97e26348ba0eccf3e1b5f986d32ecab377e82a38d4eeab86547f67684d   (v29)
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

The previous paragraph in this packet inferred that authorizing an older staging
copy would cover a later production-derived source. That inference is superseded.
The September 6 read-back pins the actual staging v1 and HEAD source and compares
them with the current review commit. Similar service families do not prove an
existing or sufficient grant. Inspect the exact selected source and owner-visible
scopes before authorization; do not use this historical packet as consent evidence.

## Staging only

Every id in this document is a STAGING copy. No production scriptId, deployment
id or `/exec` URL appears here, and nothing in this packet asks anyone to touch
a production deployment, DNS, or the theme. Production deploys stay manual until
Mr. Salam rules otherwise.

## Historical validator defect and remaining build-identity gate

Authorization alone does not prove endpoint readiness. The review-followups
branch now rejects missing/invalid version evidence and incomplete inventories;
the historical defects below explain why source/build identity is still required.

Measured at `90880a6`, and it is worse than a gap — the assertion is wrong in
both directions:

- **3 of 4 projects expose no version field at all.** Only
  `glasses-intake-uploader` has one. `gas_version_assert.py` SOFT-PASSES a
  missing field (exit 0 with a `::warning::`), so on `governor-page-api`,
  `blackboard-production` and `blackboard-bus-v1` the headline assertion is
  currently a **no-op** — it proves the endpoint answered, nothing more.
- **On the one project that has a field, the comparison is category-wrong.**
  `Code.gs:45` sets `var CONTRACT_VERSION = 2` — a hand-maintained API-contract
  number ("v1 upload only; v2 adds prune") that changes only when the contract
  changes. The workflow passes the **clasp version number** to
  `--expect-version`, and line 75 compares them as strings. Clasp versions
  increment on every deploy. Glasses staging is at `@1` today, so a strict run
  would report a **false mismatch** immediately; deploy it once more and `@2`
  would **coincidentally match** `CONTRACT_VERSION = 2` and report a false
  verification. A check that can be wrong in both directions is worse than no
  check, because it looks like evidence.

The honest fix is a CI-stamped build marker — the commit SHA written into a
generated `.gs` before `clasp push` and echoed back by `doGet` — which is a
source change to each project and must itself ride staging first. Raised here
rather than left for whoever next reads a soft-pass warning and takes it to mean
"verified".

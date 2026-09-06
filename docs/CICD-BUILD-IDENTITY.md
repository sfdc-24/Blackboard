# Staging source and build identity

The pipeline no longer compares `CONTRACT_VERSION` with an Apps Script version number. Those identify different things. `scripts/gas_build_identity.py` prepares source from a full Git commit, checks the source stored at an immutable Apps Script version, and verifies that the exact staging URL answers with that build's identity.

## Build representation

Only `governor-page-api` and `glasses-intake-uploader` currently implement the reviewed `?health=build` route. It returns public build metadata and an optional 32-character hexadecimal nonce. Tests prohibit board, Drive, properties, authentication, HTML rendering and provider access on this route. Existing Governor authorization and uploader behavior are covered separately.

The builder reads regular tracked files from the selected commit with `git ls-tree` and `git cat-file`. Working-tree edits and untracked files are not deployment inputs. Symlinks, submodules, hidden clasp overrides, unsupported files, duplicate remote names and a committed `BuildIdentity` file are rejected. The sweeper placeholder is rejected before source push; it must not be represented as the live V2 gateway.

Canonical source is a sorted array of `{name, type, source}` records. JavaScript/HTML line endings are normalized to LF; manifest JSON is parsed and serialized with sorted keys and compact separators. This ignores manifest whitespace and key order, not changed settings. Duplicate JSON keys and non-JSON numeric constants are rejected. Other source text remains exact. This is a hash of that explicitly normalized representation, not a raw checkout-byte hash.

`sourceSha256` hashes the committed project files before generation. A private `sfdc24BuildIdentity_()` function is then generated in `BuildIdentity.gs`, containing the project, full commit SHA, source hash, schema and service name. The full expected file set includes this generated source; `fileSetSha256` hashes that set. The marker is not a secret, a credential or a signature. Trust comes from equality with the selected Git commit plus independent Google source read-back.

## Deployment checks

1. Repository variables must match the recorded staging inventory, and the selected deployment must belong to that staging project.
2. The builder writes the committed source and generated marker into a fresh runner temporary directory. Clasp pushes that prepared directory and creates an immutable version.
3. Before the deployment is repointed, `projects.getContent` with an explicit `versionNumber` must return the correct script ID and the complete expected source set, including the generated marker. Missing, extra, changed or duplicate files fail.
4. After repointing, `projects.deployments.get` must report the exact deployment ID, script ID, immutable version, manifest, canonical `/exec` URL, anonymous access and deploying-user execution mode.
5. The same immutable source is checked again. An anonymous GET to `?health=build&nonce=<fresh random value>` must return exactly the expected build and nonce. The deployment configuration is read again after the HTTP response to detect a version change during verification.

Only then is a JSON receipt written. It contains the commit, both source hashes, script/deployment IDs, immutable version, UTC verification timestamp and names of the checks performed. It contains no source bodies, visitor data, tokens or secrets. The workflow preserves it as a 30-day Actions artifact and in the step summary. Failure prevents the receipt/artifact steps.

The HTTP probe makes at most six attempts with ten seconds between attempts. Responses are size bounded. Health redirects may go only to HTTPS `script.googleusercontent.com`; the health request carries no OAuth header. Credentialed Google API/token requests do not follow redirects. Error logs report the exception class without printing credentials, URLs, source bodies or response bodies.

## Rollback and initial adoption

Rollback takes the immutable version **and the full commit SHA from its known-good receipt**. Checkout includes full reachable history. The selected commit must exist locally and implement the build-health route. Before any redeploy, the candidate version must equal the source reconstructed from that commit; rollback does not push source or create a version. The same API/source/live checks run after repointing.

Historical unstamped versions cannot pass this automated rollback gate. Initial staging adoption therefore needs two reviewed builds with distinct commit markers: verify and retain the first receipt, deploy the second, then roll back to the first commit/version pair. Do not claim a rollback to the old unstamped baseline has been validated by this path. An unavailable commit, altered source, missing marker or mismatched version halts instead of silently relaxing verification.

## Evidence and remaining release work

These changes have offline evidence only until an owner runs them against staging. The 78-test CI/CD suite includes 15 immutable-source/API/HTTP tests, eight application health-route tests, and a workflow fixture proving failed source verification stops both deploy and rollback before mutation. Tests mock network/project methods; they do not use live credentials or paid providers.

An identity receipt proves the selected source/version and a responding build route at the observation time. It does not prove application behavior, correct secrets/permissions, Salesforce routing, the unbaselined V2 gateway, or full estate coverage. Current Governor v30 reconciliation, reviewed source/scope evidence, staging authorization diagnosis and owner acceptance remain release work. No production promotion follows automatically from this check.

References: [Google immutable version source read](https://developers.google.com/apps-script/api/reference/rest/v1/projects/getContent), [deployment and web-app configuration](https://developers.google.com/apps-script/api/reference/rest/v1/projects.deployments), [Content Service redirects](https://developers.google.com/apps-script/guides/content).

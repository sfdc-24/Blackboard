# The Salesforce build lane

Mr. Salam, 2026-10-09: *"if I say, I'd like to see how I can model API usage and agent spend in
salesforce, I want to see propositions, architectural proposals and options discussed and then
implemented as I watch in realtime"* - and: *scratch orgs to prototype during conferences, promoted
to the developer org on acceptance, after testing.*

Off by default (`STUDIO_SF_BUILD`). Operator sessions of the `salesforce_build` topic only; a public
visitor can neither pick the topic nor reach the lane, and an operator removed from
`STUDIO_OPERATOR_EMAILS` loses it on the next call.

## Phases (enforced in `workers/sf_build.py`, never trusted to the page or a model)

| Phase | What happens | Touches an org? |
|---|---|---|
| 1 discuss | The architect draws the solution architecture (section > heading, process-step, edge) and says what it shows. The owner pokes it; it is redrawn. | No |
| 2 options | 2-3 designs as a `decision.batch`: trade-offs (limits, storage, licences, complexity, time), one recommended and why, plus a first-build scope question. | No |
| 3 prototype | On the pick: `model.updated` (every object and field PROPOSED) and a mock record page and list view in Lightning style. "Rename a field / add a roll-up" re-plans and redraws both. | No |
| 4 build | Only after "build it", into the session's **scratch org**: validate (`--dry-run`) -> "Build it in Salesforce now?" -> deploy on the in-session yes. | Scratch |
| 5 display | Describe + query of the scratch org: the model redrawn BUILT from the org's answer, the list view filled with real records, Object Manager and record links. | Scratch (read) |
| 6 test | Acceptance checks by query: planned fields exist, records create, names set, every master-detail linked, lookups used, roll-ups recomputed and compared. PASS/FAIL cards. | Scratch |
| 7 promote | Only after a passing test of the latest scratch build, the owner's "approved" / "finalized" / "promote to the developer org", and an in-session yes: the tested package is validated in the developer org, then deployed. | Developer org |

Iterating after a build loops to prototype and builds adds only (removing or changing anything built
is refused - that is what undo is for). "Undo the last build" and "undo the promote" deploy the
stored `destructiveChanges.xml` after their own confirm.

## Guardrails

- **The plan** (`workers/sf_plan.py`) is closed: CustomObject (Text or AutoNumber name, ReadWrite or
  ControlledByParent for a detail, Deployed), CustomField Text, Number, Currency, Percent, Date,
  DateTime, Checkbox, restricted Picklist, LongTextArea, Lookup, MasterDetail, roll-up Summary; an All
  list view and a tab per new object; the `Conf_Build_Access` permission set (CRUD/FLS on what the plan
  creates). Every name ends `__c`; standard objects only gain custom fields (Account, Contact, Lead,
  Opportunity, Case). No Apex, triggers, flows, profiles, sharing rules, layouts, remote sites or
  deletes. At most 6 objects and 60 fields. `agents` is a named future type and is refused.
- **Targets** (`workers/sf_org.py`): `scratch` (default; accepted only when the Dev Hub's
  `ActiveScratchOrg` confirms it and the CLI alias resolves to that org on a `*.scratch.my.salesforce.com`
  host) and `devorg` (00Dbm00000wK2ibEAC on its pinned My Domain, promote only).
- **Never over something that exists**: a new object or field already present in the target is
  refused before validation, so undo can never delete what the lane did not create.
- **One CLI wrapper** (`workers/sf_cli.py`): a closed allowlist of `sf` commands; nothing that prints
  a credential can run through it.
- **Audit**: every validate, deploy, undo, sample, test and promote writes one line
  (`studio.sf_build_audit`: session id, action, target, plan hash, components, result) and is kept in
  `state.sf_build.audit`.

## Live feed: the Redis seam (`workers/sf_sink.py`)

Each step is one flat string map `{session_id, room, seq, phase, plan_hash, objects, components,
result, at}` - no PII, no text anyone typed. Keys: stream `conf:sf:<session_id>:events` (MAXLEN ~500)
and hash `conf:sf:<session_id>:state` (latest record), both TTL 7 days. Default sink: a log line.
`STUDIO_SF_REDIS=true` writes Redis over TLS trusting only the mounted CA (the `scripts/redis_dual.py`
pattern), AUTH from a mounted file; a failure is counted and logged without the host.

## Live org view (`workers/sf_view.py`)

Lightning refuses to be framed by another site and a framed page has no login under third-party
cookie partitioning, so the recommended path is a **server-side mirror**: a render worker (headless
Chromium logged into the scratch org on the server) captures Object Manager, the record page and the
list view after each step, at most once every 3 s per session. The lane hands it
`{target, kind, label, url}` requests (Lightning URLs only - never a session or frontdoor link); a
capture becomes the optional `org.view` event `{target, kind, label, image, at}`. Not in this image.

## Scratch orgs (`scripts/sf_scratch.py`)

Shape, then snapshot, then one org per session. `sf/project-scratch-def.json` is the developer org's
**shape** (`"sourceOrg": "00Dbm00000wK2ibEAC"`), never a feature list. The `ConfBase` **snapshot**
(a shaped org, captured) is preferred when Active. Limits on this hub: 3 active scratch orgs, 6 created
a day (snapshot-based orgs count too), 5 snapshots. The script reuses a session's active org, refuses
at either limit, defaults to 7 days, and records `{org_id, alias, instance_url, expires}` (never a
token) - `attach` posts it to the lane, which re-checks it with the hub.

# Live model scratch org (sample data only)

Salesforce DX project for the 10:00 AM ET demo. One custom object, `Service_Request__c`, related to Account or Lead. No Field Service and no serialized products. Do not authorize an org from this repo and do not point a deploy at the Omnistudio org.

`sourceApiVersion` is **68.0** (Winter '27 GA). The scratch definition is the plain Developer edition file.

## What deploys

- Object `Service_Request__c`, auto-number `SR-{0000}`, reports, activities, and field history on.
- Lookups `Account__c` and `Lead__c`. Validation rule `Account_or_Lead_Required` errors when both are blank.
- `Status__c` (New / In Progress / Resolved, default New), `Priority__c` (Low / Medium / High), `Summary__c` long text.
- Tab, page layout `Service Request Layout` (one column, highlights panel on), compact layout `Service Request Compact`.
- Permission set `Service_Request_Access`: object CRUD, field access, tab visible. No license element.

Account and Lead layouts are not in this project. A related list on those objects requires deploying the whole standard layout, which would replace it. The child relationship name on both parents is `Service_Requests` (label "Service Requests"). Add that related list in the layout editor during the demo.

## Command sequence

From this directory (`sf-demo/live-model`):

```bash
sf org list
sf limits api display --target-org DevHub
sf org create scratch --target-dev-hub DevHub --definition-file config/project-scratch-def.json --alias livemodel --set-default --duration-days 7 --wait 15
sf project deploy start --target-org livemodel
sf org assign permset --name Service_Request_Access --target-org livemodel
sf data import tree --plan data/sample-data-plan.json --target-org livemodel
sf org open --target-org livemodel --path /lightning/o/Service_Request__c/list
```

`./scripts/demo.sh` runs that sequence. Backup org, without changing the default org:

```bash
./scripts/demo.sh livemodel-b
```

Reset (delete, then recreate):

```bash
./scripts/reset.sh
./scripts/reset.sh livemodel-b
```

Dev Hub budget: 3 active scratch orgs, 6 creations per 24 hours. If the Salesforce DX MCP server is used, pin the org alias. Do not use ALLOW_ALL_ORGS.

## Sample data

`data/sample-data-plan.json` loads the JSON files beside it (paths are relative to the plan file). References are `@AccountRef1`, `@AccountRef2`, and `@LeadRef1`.

| Object | Records | Name |
| --- | --- | --- |
| Account | 2 | Haiti Solar Clinic (Sample), Port-au-Prince Depot (Sample) |
| Lead | 1 | Marie Jean (Sample), company Cap-Haitien Cooperative (Sample), status Open - Not Contacted |
| Service Request | 4 | Name is the auto-number. Summary ends with (Sample). Three point at an Account, one at the Lead. |

`sf-demo/fs-demo/scripts/seed-fs-demo.apex` is the obsolete v1 seed. Do not run it.

## Live change

`live-change/Days_Open__c.field-meta.xml` and `live-change/Due_Date__c.field-meta.xml`, with the exact copy-and-deploy commands in `live-change/README.md`.

# 10 AM demo runbook v2: live data modeling in a scratch org (SAMPLE DATA ONLY)

Owner direction 3:44 AM ET: serialized products are NOT enabled in the Omnistudio org, so no Field Service / serialized-product demo. Instead: one custom object related to Account or Lead, and a simple prototype that proves real-time modeling and concept validation in a scratch org. `scripts/seed-fs-demo.apex` (v1) is obsolete; do not run it.

The deployable project is **`sf-demo/live-model/`**. Run every command below from that directory (or run its scripts, which `cd` there). Do not change the Omnistudio org.

## Model

- Custom object `Service_Request__c` (label Service Request), auto-number name `SR-{0000}`. Reports, activities, and field history are enabled.
- `Account__c` Lookup(Account) and `Lead__c` Lookup(Lead). Validation rule `Account_or_Lead_Required` errors when both are blank.
- `Status__c` picklist (New, In Progress, Resolved), default New. `Priority__c` picklist (Low, Medium, High). `Summary__c` long text area.
- Tab, page layout `Service Request Layout`, compact layout `Service Request Compact` (Lightning highlights panel).
- Permission set `Service_Request_Access` (object CRUD, field access, tab visible), assigned to the scratch org user.

## Related lists — skipped on purpose

Account and Lead layouts are not deployed. Adding the Service Requests related list means shipping the entire standard layout, which replaces it. Child relationship name on both parents is `Service_Requests` (related list label "Service Requests"). On the call, add that related list from the Account and Lead page-layout editor.

## Sample data

`sf-demo/live-model/data/sample-data-plan.json` imports 2 Accounts, 1 Lead, and 4 Service Requests. Account names, the Lead last name, the Lead company, and every Service Request summary end with `(Sample)`. Service Request Name is the auto-number and is not set in the JSON. Lookups use `@AccountRef1`, `@AccountRef2`, and `@LeadRef1`. Lead Status is the standard scratch-org value `Open - Not Contacted`. Paths inside the plan are relative to the plan file. `saveRefs` and `resolveRefs` are kept for older CLIs. Salesforce CLI 2.153.5 warns that those two properties are ignored and still resolves the `@AccountRef1`-style references from file order.

## Live change (not in the initial deploy)

Prepared under `sf-demo/live-model/live-change/`:

- `Days_Open__c` — number formula `TODAY() - DATEVALUE(CreatedDate)`
- `Due_Date__c` — date

Pick one. Exact commands are in `sf-demo/live-model/live-change/README.md`. Deploy the field, drag it onto Service Request Layout, then query it back. `./scripts/reset.sh` removes a copied live-change field from `force-app` before recreating the org.

## Commands

`./scripts/demo.sh` runs this sequence for alias `livemodel`. `./scripts/demo.sh livemodel-b` is the backup org and does not set the default org.

```bash
cd sf-demo/live-model
sf org list
sf limits api display --target-org DevHub
sf org create scratch --target-dev-hub DevHub --definition-file config/project-scratch-def.json --alias livemodel --set-default --duration-days 7 --wait 15
sf project deploy start --target-org livemodel
sf org assign permset --name Service_Request_Access --target-org livemodel
sf data import tree --plan data/sample-data-plan.json --target-org livemodel
sf org open --target-org livemodel --path /lightning/o/Service_Request__c/list
```

Reset:

```bash
./scripts/reset.sh
./scripts/reset.sh livemodel-b
```

`reset.sh` runs `sf org delete scratch --target-org <alias> --no-prompt`, then `demo.sh`. Developer Edition Dev Hub: 3 active scratch orgs, 6 creations per 24h. If using the Salesforce DX MCP server, pin the org alias; never ALLOW_ALL_ORGS.

## Pre-flight checklist

After `./scripts/demo.sh`: livemodel exists with a 7-day expiry; Service_Request__c has its fields, validation rule, tab, layout, and compact layout; perm set is assigned; sample records are present (2 accounts, 1 lead, 4 service requests). Related lists on Account and Lead are a manual layout edit, not metadata. Rehearse one live change, read it back, then `./scripts/reset.sh`. Backup org: `./scripts/demo.sh livemodel-b`. Cleanup after the demo: delete both scratch orgs. The Omnistudio org is never changed.

# 10 AM demo runbook v2: live data modeling in a scratch org (SAMPLE DATA ONLY)
Owner direction 3:44 AM ET: serialized products are NOT enabled in the Omnistudio org, so no Field Service / serialized-product demo. Instead: one custom object related to Account or Lead, and a simple prototype that proves real-time modeling and concept validation in a scratch org. scripts/seed-fs-demo.apex (v1) is obsolete; do not run it.

## Suggested model (Claude may adjust)
- Custom object `Service_Request__c` (label Service Request), auto-number name SR-{0000}.
- `Account__c` Lookup(Account) and `Lead__c` Lookup(Lead), plus a validation rule that at least one is set.
- `Status__c` picklist (New, In Progress, Resolved), `Priority__c` picklist (Low, Medium, High), `Summary__c` text area.
- Tab, page layout, a related list on Account and on Lead, and a permission set `Service_Request_Access` assigned to the scratch org user.
- Sample data: 2 Accounts, 1 Lead, 4 Service Requests (names marked "(Sample)").

## Live change to show on the call (the small, praise-worthy bit)
Pick ONE and rehearse it: add a field live (e.g. `Due_Date__c`) and show it on the layout, or add a formula `Days_Open__c`, or flip the validation rule and show it catch a bad record. Deploy, then read the result back in the org and redraw the model.

## Commands
1. `sf org list`, then `sf limits api display --target-org DevHub` (Developer Edition Dev Hub: 3 active, 6 creations per 24h)
2. `sf org create scratch --target-dev-hub DevHub --definition-file config/fs-demo-scratch-def.json --alias livemodel --set-default --duration-days 7 --wait 15`
3. `sf project deploy start --target-org livemodel` (object, fields, layout, perm set), then `sf org assign permset --name Service_Request_Access --target-org livemodel`
4. Seed sample records (an Apex script or `sf data import tree`), then `sf org open --target-org livemodel`
5. Reset: `sf org delete scratch --target-org livemodel --no-prompt`, then repeat 2-4. Backup org `livemodel-b`.
If using the Salesforce DX MCP server, pin the org alias; never ALLOW_ALL_ORGS.

## Pre-flight checklist (Grok, read-only)
livemodel exists with ~7-day expiry; Service_Request__c with fields, validation rule and related lists on Account and Lead; perm set assigned; sample records present; the live change rehearsed once and reset; backup org ready. Cleanup after: delete scratch org. The Omnistudio org is never changed.

# 10 AM demo runbook: serialized Work Order fix (SAMPLE DATA ONLY)
Authored by Grok from Salesforce docs; untested. Claude builds; Grok verifies read-only in Setup.

## Commands
1. `sf update`; `sf org list` (Dev Hub login only if missing: `sf org login web --set-default-dev-hub --alias DevHub`, owner signs in)
2. `sf limits api display --target-org DevHub` (Developer Edition Dev Hub: 3 active, 6 creations per 24h)
3. `sf org create scratch --target-dev-hub DevHub --definition-file config/fs-demo-scratch-def.json --alias fsdemo --set-default --duration-days 7 --wait 15`
4. `sf org display --target-org fsdemo`; `sf apex run --file scripts/seed-fs-demo.apex --target-org fsdemo`; `sf org open --target-org fsdemo`
5. Build the two Flows; `sf project retrieve start --target-org fsdemo`
6. Reset: `sf org delete scratch --target-org fsdemo --no-prompt`, then repeat 3-5
7. Backup org `fsdemo-b` the same way.

## Demo (about 3 min)
- Before: a Flow on the Work Order Line Item shows no serial (no serial field on the line; serials hang off the product).
- Fix (activate live): Get Records finds an Available SerializedProduct for the line's Product2Id, then the Asset with that SerialNumber, then sets WorkOrderLineItem.AssetId so the serial shows on the line. Standard fields only.

## Gotchas
1. FieldService:1 needs both fieldServiceSettings too; confirm objects exist before seeding.
2. WOLI gets Product2 only via PricebookEntryId; seed activates the standard price book.
3. ProductItem needs an IsInventoryLocation Location; serials go on SerializedProduct.
4. Each reset uses one scratch-org creation.
5. Use Schema.Location in Apex.
Leave Agentforce out of this org (Agentforce scratch orgs need Data 360 licences on the Dev Hub).
If using the Salesforce DX MCP server, pin the org alias; never ALLOW_ALL_ORGS.

## Pre-flight checklist (Grok, read-only)
Dev Hub limits OK; fsdemo exists with ~7-day expiry; Field Service enabled; objects present; standard price book active with inverter; 11 sample records; Before Flow active, Fix Flow saved but inactive; one dry run plus reset; fsdemo-b seeded. Cleanup after: delete scratch org. The Omnistudio org is never changed.

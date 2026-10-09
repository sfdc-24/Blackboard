# Live change (kept out of force-app)

Pick one field on the call. These files are not in the initial deploy: `sfdx-project.json` packages only `force-app`, and `.forceignore` excludes this directory.

Run from `sf-demo/live-model`. Use alias `livemodel`, or `livemodel-b` for the backup org.

## Days Open (number formula)

Formula: `TODAY() - DATEVALUE(CreatedDate)`.

```bash
cp live-change/Days_Open__c.field-meta.xml force-app/main/default/objects/Service_Request__c/fields/Days_Open__c.field-meta.xml
sf project deploy start --source-dir force-app/main/default/objects/Service_Request__c/fields/Days_Open__c.field-meta.xml --target-org livemodel
sf data query --query "SELECT Name, Status__c, Days_Open__c, CreatedDate FROM Service_Request__c" --target-org livemodel
```

## Due Date (date)

```bash
cp live-change/Due_Date__c.field-meta.xml force-app/main/default/objects/Service_Request__c/fields/Due_Date__c.field-meta.xml
sf project deploy start --source-dir force-app/main/default/objects/Service_Request__c/fields/Due_Date__c.field-meta.xml --target-org livemodel
```

## Show it on the layout

Setup → Object Manager → Service Request → Page Layouts → Service Request Layout. Drag the new field onto the Information section and save.

The System Administrator in a Developer scratch org can read newly deployed fields. `Days_Open__c` is a formula, so it is read-only. If a field is hidden, add field permissions for it on `Service_Request_Access` and deploy that permission set again.

`./scripts/reset.sh` deletes any copied live-change field from `force-app` before it recreates the scratch org, so the next deploy starts from the packaged model.

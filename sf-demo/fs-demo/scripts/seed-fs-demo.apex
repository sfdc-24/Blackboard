// SAMPLE DATA ONLY. Authored by Grok for the 10 AM demo; NOT from Salesforce docs; untested.
// Run: sf apex run --file scripts/seed-fs-demo.apex --target-org fsdemo
Account acct = new Account(Name = 'Haiti Solar Clinic (Sample)');
insert acct;
Product2 prod = new Product2(Name = 'Solar Inverter SI-5000 (Sample)', ProductCode = 'SI-5000', IsActive = true, IsSerialized = true);
insert prod;
// WOLI.Product2Id is read-only and only populated via PricebookEntryId.
Pricebook2 std = [SELECT Id, IsActive FROM Pricebook2 WHERE IsStandard = true LIMIT 1];
if (!std.IsActive) { std.IsActive = true; update std; }
PricebookEntry pbe = new PricebookEntry(Pricebook2Id = std.Id, Product2Id = prod.Id, UnitPrice = 1200, IsActive = true);
insert pbe;
Schema.Location wh = new Schema.Location(Name = 'Port-au-Prince Warehouse (Sample)', LocationType = 'Warehouse', IsInventoryLocation = true);
insert wh;
ProductItem pi = new ProductItem(Product2Id = prod.Id, LocationId = wh.Id, QuantityOnHand = 3);
insert pi;
List<SerializedProduct> serials = new List<SerializedProduct>();
for (String sn : new List<String>{'SI5K-HT-0001','SI5K-HT-0002','SI5K-HT-0003'}) {
    serials.add(new SerializedProduct(Product2Id = prod.Id, ProductItemId = pi.Id, SerialNumber = sn, Status = 'Available'));
}
insert serials;
Asset installed = new Asset(Name = 'SI-5000 @ Haiti Solar Clinic (Sample)', AccountId = acct.Id, Product2Id = prod.Id, SerialNumber = 'SI5K-HT-0001');
insert installed;
WorkOrder wo = new WorkOrder(AccountId = acct.Id, Subject = 'Install / service inverter (Sample)', Pricebook2Id = std.Id, Status = 'New');
insert wo;
// The line deliberately has NO AssetId: the "can't pull serial" starting state.
WorkOrderLineItem woli = new WorkOrderLineItem(WorkOrderId = wo.Id, PricebookEntryId = pbe.Id, Quantity = 1, Description = 'Inverter swap (Sample)');
insert woli;
System.debug('Seeded WO ' + wo.Id + ' WOLI ' + woli.Id);

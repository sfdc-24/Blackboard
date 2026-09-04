# SFDC24 intake — omnistudio connection

The user selected `omnistudio-sandbox` for the SFDC24 intake on September 4, 2026.
Salesforce identifies that alias as **SFDC 24**, **Developer Edition**,
`IsSandbox=false`, Org ID `00Dbm00000wK2ibEAC`. Accordingly, the form uses
`https://webto.salesforce.com/servlet/servlet.WebToLead?encoding=UTF-8`.

## Implementation

`web/sfdc24-lead-capture.html` now submits real Web-to-Lead requests to that org.
Names, company, and email use the lengths returned by the org's Lead describe.
Required fields and email format use native form validation, with a second
validation check after trimming whitespace. The customer/supplier choice is
stored in Description alongside the request text; it does not require a custom
field. The form submits `lead_source=sfdc24.com`, an active value in this org.

The old simulated pipeline completion and generated Lead ID were removed.
The return page acknowledges submission without asserting record creation:
Web-to-Lead returns a redirect, not an authenticated Lead receipt. The form is
disabled until its script initializes and disables repeat clicks during submit.

Private review URL: https://sfdc24-intake-omnistudio.astronautwannabe.chatgpt.site
This is a separate owner-only review deployment. The public www.sfdc24.com
deployment and DNS were not changed.

## Verification

One synthetic Web-to-Lead request was sent on September 4, 2026, then queried
directly from the explicitly selected org. HTTP 200 alone was not treated as
delivery evidence.

- Marker: `SFDC24-VERIFY-20260904T224843Z`
- Created Lead: `00Qbm00000pJLZdEAO`
- CreatedDate: `2026-09-04T22:48:49.000+0000`
- LeadSource: `sfdc24.com`
- Status: `Open - Not Contacted`
- Company: `SFDC24 integration verification - synthetic`
- Description read-back preserved `Relationship: Supplier` and the test text.
- Email uses `example.invalid`; no customer email address was used.

The test record remains in the org as evidence. No existing records or org
settings were changed. The older test Lead `00Qbm00000pISjdEAG` was preserved.

Regression check: `node tests/sfdc24-intake.cjs`. It verifies the selected org,
form contract, validation guard, relationship serialization, literal handling
of text, native submission, and repeat-click prevention. This is a handler and
markup check, not a rendered browser test.

Assignment rules, Agentforce replies, conversion, and lifetime-support
automation were not implemented or certified by this intake change.

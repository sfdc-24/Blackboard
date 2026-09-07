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
The return URL is derived from the hosted page's current origin and pathname,
with only `?submitted=1`; unrelated query parameters and fragments are removed.
Resetting the form restores this URL before another submission. Local `file:`
previews remain disabled because Salesforce cannot return visitors to them.

Private review URL: https://sfdc24-intake-omnistudio.astronautwannabe.chatgpt.site
This is a separate owner-only review deployment. The public www.sfdc24.com
deployment and DNS were not changed.

## Verification

Historical receipt: Codex desktop task `01a06e94-ed45-7c02-aa97-df325f2d6ccb`
authored the connection and executed one synthetic Web-to-Lead request on
September 4, 2026, then queried the explicitly selected org. The
[executor's attributed receipt](https://github.com/sfdc-24/Blackboard/pull/3#issuecomment-5547510371)
records the result below. VM Claude preserved those changes in this PR but did
not execute that test. HTTP 200 alone was not treated as delivery evidence.
This is historical evidence, not a fresh org check or a guarantee for later
submissions; independent Salesforce-lane acceptance remains pending.

- Marker: `SFDC24-VERIFY-20260904T224843Z`
- Created Lead: `00Qbm00000pJLZdEAO`
- CreatedDate: `2026-09-04T22:48:49.000+0000`
- LeadSource: `sfdc24.com`
- Status: `Open - Not Contacted`
- Company: `SFDC24 integration verification - synthetic`
- Description read-back preserved `Relationship: Supplier` and the test text.
- Email uses `example.invalid`; no customer email address was used.

At the time of the receipt, the test record was left in the org as evidence.
No existing records or org settings were changed. The older test Lead
`00Qbm00000pISjdEAG` was preserved. The review fixes did not repeat the submission.

Regression check: `node tests/sfdc24-intake.cjs`. It verifies the selected org,
form contract, validation guard, relationship serialization, literal handling
of text, native submission, and repeat-click prevention. This is a handler and
markup check, not a rendered browser test.

Assignment rules, Agentforce replies, conversion, and lifetime-support
automation were not implemented or certified by this intake change.

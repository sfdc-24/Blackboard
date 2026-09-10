# ORDER comparison evidence

The fixture comparison accepts one Windows/Desktop PowerShell 5.1 artifact
and one Linux/Core PowerShell 7.5+ artifact, generated from the same fixture.
Runtime identity is declared by the harness; it is not remote host attestation.
The CI workflow generates both artifacts from the same checked-out revision.

Generate fresh artifacts with `tests/order_fixture_crosscheck.ps1 -OutFile PATH`
on each endpoint, then run:

```
python tests/order_crossdiff.py windows.json linux.json
```

Each artifact must contain seeded passes 1/2 and replay passes 1/2, without
duplicates. Every runner exit must be the integer zero, every verdict must
have Boolean `ok: true`, both scenario logs must contain their two poll starts,
and both persisted states must exist. Empty, truncated, failed or unsupported
endpoint evidence is rejected before equivalence is claimed. Old artifacts
without runner exits must be regenerated.

The differ compares decisions, stdout and persisted state. State formatting
and object key order may differ; values and JSON types must agree. Cursor and
seen timestamps, row IDs, schema, counters and work entries are preserved.
Only documented execution metadata is normalized in state: `at` in last_poll,
seen, success and error; last_poll.run_id, identity and user_profile. Unknown
state fields are retained. Stdout retains the existing text normalization.

Artifact publication uses a sibling temporary file, readback, atomic move or
replace, and destination readback. Failed writes return nonzero and never
print `wrote`. An emitted artifact can still record a failed fixture run; the
harness and differ both reject it as successful comparison evidence.

`order-cross-platform.yml` runs the differ controls and actual harness checks
on Windows and Linux, uploads successful fixture artifacts, then compares
them. Every changed comparison file and its runner/module/fixture dependencies
is included in the trigger. All operations use local fixture files: no bus,
provider, mailbox, Salesforce or installed service is involved.

This verifies the Observe paths exercised by the fixture. It does not establish
Execute containment, live adapter authentication, installed service health,
fleet cutover, or the complete-workload soak. Claude retains migration and
deployment sequencing. Do not turn fixture success into a service-readiness claim.

# Metadata operation ledger — disconnected plumbing only

This module implements a pure state machine over an injected atomic CAS store.
It has **no route, provider, network/subprocess call, credential access, feature
activation, or Salesforce write**. Existing Studio/prototype behavior and the
default-off proposal feature are unchanged. Future UI/provider work requires a
fresh reviewed plan and explicit execution confirmation; PR221 proposal-only
confirmation is not execution consent and is rejected here.

## Contract and persistence boundary

Only new, optional, non-unique, non-external-ID Lead Text fields are representable,
using the existing strict field validator (ASCII name stem 1–32, label 1–40,
length 1–255). There is no XML, SOQL, Apex, delete, update, upsert or rollback
input. An execution plan binds exact field properties, opaque operator/session/
org-binding IDs, an operation ID, a fresh nonce and a maximum ten-minute expiry.
The caller must supply authenticated bindings and trusted server timestamps;
this library is not an identity or permission service. Org-binding IDs must
remain stable and one-to-one with the actual target org across all sessions and
operators. Inventing a second binding for the same org defeats target locking.

The store adapter must explicitly declare `atomic_generation_cas = True` and
implement `load(name) -> (JSON document, generation)` plus
`save(name, document, expected_generation)` as one atomic generation-CAS write.
An absent document is exactly `({}, None)`; create must also be conditional.
The supplied conflict exception distinguishes CAS conflicts from uncertain I/O.
Existing local FileStore is **not automatically certified**. No production
adapter is connected by this increment. A missing capability declaration fails
closed; the declaration is an adapter contract, not proof of implementation.

Each opaque org binding uses one CAS document containing both operation records
and target holds. Target identity is the case-normalized canonical member
`CustomField:Lead.<name>__c`. Reserving an operation and target is atomic; no
cross-document transaction is simulated. CAS conflicts fail closed, with no
automatic retry. Per-operation revisions additionally fence stale commands.
Storage is closed-schema, JSON-only and bounded: 64 retained operations per org,
8 commands per operation, 256 KiB per ledger. Capacity exhaustion fails closed;
there is no automatic history/hold expiry or pruning.

The authoritative store must be linearizable and non-rollbackable for the life
of its holds. Restart/current-state backup restoration preserves attempts and
holds. **Restoring an older backup predating a reservation is prohibited**:
a pure ledger cannot detect rollback of its entire authoritative history.
Disaster recovery needs independent fencing/retained generations and a reviewed
reconciliation procedure before any execution adapter is enabled.

## State and dispatch rules

`prepared → confirmed → executing → verify_pending → succeeded`

Confirmation is a closed execution-specific challenge (policy, operation ID,
execution plan hash, nonce). Natural language and proposal confirmation cannot
authorize this transition. Before `begin`, the confirmed operation must record
an exact-org/member absent-target preflight. A preexisting target—even one with
matching properties—becomes a failed conflict with a retained quarantine hold.
It never counts as creation or success. Expiry prevents confirmation/dispatch
but never releases a hold by itself.

Only the fresh successful `begin` CAS returns internal `dispatch_allowed=True`.
It atomically records the single lifetime provider attempt before any future
adapter may dispatch. Replaying this command returns the stored receipt but
**never** regrants dispatch. A crash or uncertain save after reservation grants
nothing: re-read/reconcile, never re-dispatch. A future adapter must not cache,
share, queue or retry the transient grant. The library cannot stop external
code from misusing it; provider integration needs its own reviewed fencing.

Accepted dispatch is only `verify_pending`, not success. Success requires a
subsequent exact org, exact field-properties and post-attempt timestamp readback.
`succeeded` means **verified present**, never “created by this operation”: another
writer can race between absence preflight and creation/readback.

Timeout, interrupted dispatch, ambiguous acknowledgement, verification failure
or mismatch becomes terminal `outcome_unknown`. Its hold survives restart,
expiry and differently named operations. No automatic retry, overwrite, delete,
reconciliation-to-success or rollback exists. Late worker results are rejected.
Cancellation may assert `cancelled_before_dispatch` only before the attempt
marker; after that, use unknown-outcome handling rather than claiming no write.

Only a definitive pre-dispatch failure/cancellation or authoritative
`rejected_no_change` result releases a hold. The future trusted provider adapter
must supply `definitive_no_change=True` only with actual definitive evidence.
Duplicate/already-exists responses, generic server errors, missing responses and
timeouts are **not** such evidence; classify them as ambiguous and quarantine.
Even definitive failure cannot retry the same operation; any subsequent attempt
needs a new plan/confirmation and fresh preflight.

## Receipts and audit

`receipt()` authenticates the complete supplied binding and returns only opaque
`operation_id`, `status`, `updated_at`, and fixed `next_action`. No org binding,
actor, session, stable fingerprint, nonce, raw error, provider response or field
details are public. The command result's revision/replay/dispatch values are
internal orchestration data, not public receipt fields. Confirmation challenges
are separately authenticated internal contracts, not receipt payloads.

The private bounded audit records legal transitions and command fingerprints.
Same command/payload replays its historical receipt; changed payload conflicts.
Audit/hold inconsistencies and expanded/corrupt durable schemas fail closed.
CAS/save errors are redacted and never produce a dispatch grant.
Lifecycle timestamps and the provider-attempt count must exactly match their
unique committed audit transitions, not merely plausible time ranges. Preflight
observations cannot postdate their specific preflight commit. The ledger revision
equals the total retained audit-event count across all operations. No pruning is
supported; inconsistent counters are rejected. The full-store backup rollback
limitation above still applies.

## Offline validation and remaining activation requirements

`python -m unittest tests.test_studio_metadata_operations` covers restart/current
backup, atomic competing target claims, same/different-ID replay, cross-tenant
reads, stale CAS/late workers, preexisting matches, verification mismatch,
unknown quarantine, ambiguous saves, cancellation and forbidden actions. Hosted
CI runs the suite in a network-isolated namespace with required-file guards.

Still unbuilt: authenticated execution UI and request route, a certified durable
store adapter and anti-rollback recovery, stable exact Salesforce org mapping,
permission/preflight/provider adapter and its definitive-error classification,
whole-operation deadline/cancellation, quota/rate controls, and deployment.
This source increment does not establish credentials, org permissions, external
mutation exactly-once semantics, Salesforce compatibility or production safety.

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

## Inert provider adapter contract (still disconnected)

`app/metadata_provider.py` is a source-only contract for a later Salesforce
adapter. It is **not imported by the controller**, has no route/settings wiring,
and contains no HTTP/SDK/subprocess client, credential/environment lookup,
deployment configuration, org call, or feature activation. The operation ledger
adds one actor-bound `execution_snapshot()` read so the internal coordinator can
re-read validated durable state immediately around transitions; caller-provided
snapshots are not dispatch authority.

The immutable `OrgBinding` keeps the ledger's opaque `org_binding_id` distinct
from the actual 18-character Salesforce org ID. It also pins one canonical bare
HTTPS `.develop.my.salesforce.com` or `.sandbox.my.salesforce.com` origin, an
exact API version, matching `developer`/`sandbox` environment and positive
binding version. Its representation redacts the actual org ID and origin. This
mapping is injected; this increment does not create, persist, authenticate, or
certify it. One immutable `OrgBinding` object is not evidence of a durable,
bijective registry between opaque IDs and Salesforce orgs; that registry and
its rebind/rotation controls remain unbuilt.

Sealed bindings, budgets, cancellation views, dispatch permits, ledgers and the
adapter reject direct initializer re-entry before validating or assigning any
replacement value. The adapter's issued-plan registry is an immutable value
replaced only while its internal lock is held, so callers cannot clear it to
mint a second budget or renew the original deadline.

One sealed, non-copyable, non-serializable `ExecutionBudget` is issued from the
trusted ledger while an operation is confirmed and before provider preflight.
It owns one absolute monotonic deadline of at most 20 seconds, the plan expiry,
the exact plan/binding/field, sealed ledger dependency configuration, the exact
process-local store object identity, phase state, and one process-local
cancellation signal. Substituting an identically populated ledger or swapping
ledger dependencies fails before ledger/provider I/O. The exact same deadline
and cancellation callback are passed to every transport call across preflight,
dispatch and independent verification; a later phase cannot renew the budget.
Cancellation is cooperative: this contract lets a conforming blocking transport
observe it, but cannot forcibly interrupt a misbehaving implementation or
operating system.

The injected transport must declare `zero_retry_writes = True` and
`redirects_disabled = True`. Its only public callable surface is:

- `open_verified_session`
- `describe_lead_text`
- `create_lead_text_once`
- `close`

The adapter pins those four resolved callables during construction and checks
both declarations and callable identity again at each provider phase boundary.
Declaration or method drift fails closed before another provider call.
`open_verified_session` owns any resources it allocates until it returns a
valid closed session shape. If it raises or returns an invalid response, a
concrete transport must clean up internally: the adapter cannot safely pass an
unvalidated response to `close`. Once a session validates, the adapter owns the
normal `close` call under the original deadline and cancellation signal.

The adapter passes the canonical fixed Lead member to `describe_lead_text`, not
a URL, query, XML document, generic action, or arbitrary metadata name. Session,
description, and create acknowledgement responses are exact closed shapes bound
to the actual org ID, pinned origin and API version. A description is usable
only with explicit `complete: true`; absence is never inferred from an empty or
partial response.

Provider preflight returns exactly the PR223 evidence shape
`org_binding_id/member/exists/observed_at`, then the coordinator commits that
stored evidence through the ledger. Only an absent exact-member result may
continue. An ambiguously saved preexisting-target preflight may replay its exact
stored command to recover the already-terminal receipt without provider I/O.
The coordinator—not a caller—invokes the durable `begin`, immediately
re-reads the operation, and mints a sealed one-use permit only when the result is
fresh, non-replayed and matches the single reserved attempt. Old begin-result
dictionaries, raw booleans, portable snapshots, restarts and ambiguous begin
saves cannot mint or substitute for that permit.

Dispatch performs one immediate complete absence recheck and then enters
`create_lead_text_once` at most once, with no intervening transport operation.
Before that method is entered, failure is a definite no-change result. From
method entry onward, every exception, deadline/cancellation event, non-exact
acknowledgement or close uncertainty is ambiguous and non-retryable. An exact
acknowledgement is only `verify_pending`, never success.

Independent verification opens and describes again under the same budget. It
returns either the exact PR223 observation shape populated from the independent
description, or fixed `verification_mismatch`/`verification_unavailable` values
that contain no plan/provider data. The result is retained inside the sealed
budget. `commit_verification()` accepts no caller observation and maps only that
stored result to the ledger: exact observation to `verify`, mismatch to a
sanitized nonmatching `verify`, and unavailable to terminal
`verification_unavailable`. Ambiguous store saves replay the identical command
without another provider read.

These Python capabilities are process-local misuse barriers, not authentication,
durable anti-rollback proof, or external exactly-once semantics. Restart
recovery/reconciliation for in-memory budgets and permits is not implemented;
durable ledger state remains authoritative and a restart cannot reconstruct a
write grant. Transport
capability declarations and offline fakes do not certify a future concrete
implementation. Any concrete adapter, binding source, permission proof, quota,
route/UI, deployment or activation still requires separate review and live
same-org acceptance.

## Offline validation and remaining activation requirements

`python -m unittest tests.test_studio_metadata_operations` covers restart/current
backup, atomic competing target claims, same/different-ID replay, cross-tenant
reads, stale CAS/late workers, preexisting matches, verification mismatch,
unknown quarantine, ambiguous saves, cancellation and forbidden actions. Hosted
CI runs the suite in a network-isolated namespace with required-file guards.

`python -m unittest tests.test_studio_metadata_provider` covers distinct
opaque/actual org binding, immutable/redacted capabilities, exact preflight,
incomplete descriptions, origin/org/version mismatch, coordinator freshness,
ambiguous begin saves, permit concurrency/replay, the single whole-operation
deadline and cancellation signal, preexisting races, all post-create ambiguity,
independent verification, sealed verification commits and false-green CI guards.
Hosted CI requires both provider source and tests and runs them with the network
namespace removed.

Still unbuilt: authenticated execution UI and request route, a certified durable
store adapter and anti-rollback recovery, stable authenticated Salesforce org
mapping, concrete permission/provider transport and its live error/deadline/
cancellation acceptance, quota/rate controls, and deployment.
This source increment does not establish credentials, org permissions, external
mutation exactly-once semantics, Salesforce compatibility or production safety.

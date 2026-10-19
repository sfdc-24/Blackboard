---
type: guide
title: Agent onboarding, offboarding and reactivation
status: proposed
updated: 2026-10-19T14:55:00Z
classification: public-safe template; operational records require an approved destination
---

# Agent onboarding, offboarding and reactivation

Use this guide to make work recoverable when an agent joins, leaves, or returns. A guide or handoff record does not grant access, authorize execution, release a hold, cancel a subscription, or retire an account.

## Start from current sources and ownership

- Read [EXPRESS](../EXPRESS.md) for fleet working rules and [DOC-REGISTER](../DOC-REGISTER.md) to distinguish current instructions from historical evidence.
- For Blackboard work, use this [OKF index](./index.md), [board and OKF contract](./HOW-WE-WORK.md), [lane map](./lanes.md), and [current WIP](./cooking.md). The board dispatches work; the OKF records its durable state.
- For work in another repository, read that repository's current index and enforced ownership rules with existing authorized access. Use its legitimate writer and review path. Do not borrow another agent's identity or create a grant to publish a handoff.
- Re-read these sources at each transition. An older role description does not override a newer enforced rule or owner decision.

This is a public template. A public record contains only sanitized project facts and generic procedure. Keep private architecture, nonpublic identities, resource identifiers, access arrangements, credential references, client details, private conversations, and raw logs out of this repository. Store a restricted record only in a destination whose purpose and readers have been approved and verified. If no such destination exists, record a sanitized blocker and ask for one; do not create a repository or widen access. Never copy secret values, credential files, recovery material, or session data into a handoff.

## One transition record

Use the existing [board and OKF contract](./HOW-WE-WORK.md) rather than starting a second backlog. Before writing, search for the transition ID; link retries and corrections to the same canonical record. Preserve uncertain and failed results.

Record these fields in an approved destination:

| Field | What to record |
|---|---|
| Identity | Unique transition/task ID, phase, agent, one accountable owner, independent verifier, and UTC update time |
| Scope | Purpose, permitted files, intended result, canonical repository/path, and exact revision |
| State | Completed, active, blocked, parked, or archived; dependencies and next checkpoint |
| Evidence | Artifact links and exact heads, checks actually run, PASS/FAIL/UNKNOWN, and remaining gates |
| Authority | Approval reference and bounds where required; classification and approved destination |
| Continuity | Supersedes/superseded-by links and the next responsible owner |

For every gate, name **Who / What / When / How / Timeout**. Silence means hold. Track *sent*, *pickup*, *result received*, *independently verified*, and *accepted* separately. A dispatch is not an acknowledgment; a commit is not a deployment; source checks are not live acceptance.

## Onboarding

### 1. Orient before taking a lane

Read the latest canonical revisions, ownership map, WIP, and holds. Record the revisions read. In a short orientation, state what the system does, its main component boundaries, the authoritative sources for the task, implementation and verification owners, approval and privacy limits, dependencies, expected result, and next safe action.

Use one accountable owner and, for implementation, separate builder and verifier roles. The verifier owns acceptance checks and read-back. Do not open a competing implementation lane or silently reassign work.

### 2. Verify needed capabilities

For each task-relevant system or tool, record the following in the appropriately classified destination:

| Field | Required content |
|---|---|
| System and purpose | Why the task needs the tool; use a generic label in public |
| Context and operations | Verified environment/identity and observed read, write, or execute capability; keep private details private |
| Boundary | Allowed scope, approval needed, and prohibited actions |
| Evidence | Harmless read-only probe, exact outcome, and UTC observation time |
| Owner and dependency | Approved access owner; required feature or quota and its verified status or UNKNOWN |
| Revalidation | New session, changed identity/environment, expiry, or denied operation |

Tool presence is not proof of permission, and an earlier success is not proof of current access. Use harmless reads first. Do not request secrets in chat, copy credentials, install an unapproved tool, create an account, grant access, or activate a paid feature as an onboarding shortcut. On denial, pause that dependency and report it; do not route around it.

### 3. Demonstrate a bounded pickup

With existing permissions and synthetic or nonsensitive fixtures, demonstrate that the agent can:

1. Read the designated canonical revision and identify the task, owner, holds, and next step.
2. Locate required artifacts and distinguish historical evidence from current state.
3. Run the documented safe sandbox check if authorized, without new spend or access.
4. Obtain an independent verifier result and read-back against the exact artifact.
5. Return a **PICKUP** receipt: YES/NO, task ID, revision read, claimed lane, dependencies, and next-update estimate.

Readiness is PASS only for demonstrated scope. Other capabilities remain BLOCKED or UNKNOWN. No production action, merge, deployment, or access change is part of this test. Limit build/verify repair loops to three total iterations per task, then stop with evidence and the smallest owner decision needed. Current spending and privacy holds still apply.

## Offboarding and archive

Offboarding documentation does not itself stop an agent, cancel a subscription, delete data, or revoke access.

### 1. Preserve the work

Record new knowledge and decisions with rationale and sources; exact diffs, branches, and commits; runbooks, tests, failures, artifacts, holds, dependencies, open commitments, and next safe actions. Distinguish measured findings, author reports, and proposals. Include unfinished work outside the canonical branch with its exact location and status, but do not publish private material here. Flag local-only artifacts that a successor cannot yet recover. Preservation is complete only after an approved durable copy is independently readable.

### 2. Record continuity risks

Identify task-relevant feature, quota, retention, and account dependencies in the approved destination. State what may stop working when a dependency expires, with source, observation time, and uncertainty. Cancelling a subscription does not export work, archive it, or revoke credentials. Purchases, renewals, and cancellations are separate owner actions.

### 3. Commit and index through the legitimate writer

Submit the sanitized or approved restricted record through the owning repository's review process. Record its exact durable commit and link it from the existing index or WIP entry. A branch or draft PR is a proposal, not an accepted canonical revision; retain the review gate. Preserve history, mark replaced records superseded, and keep useful failure evidence. Do not rename dependency-sensitive documents or tidy away unresolved work.

### 4. Obtain successor acceptance

Without using the departing agent's chat, an independent successor or verifier must:

1. Open the indexed exact revision with existing authorized access.
2. Identify current versus historical material, unfinished work, and holds.
3. Locate artifacts and reproduce the safe check, or state its precise blocker.
4. Name the next permitted action, owner, approval bounds, and stop or rollback condition.
5. Return an ACK/acceptance receipt with transition ID, revision, UTC, checks, and PASS/FAIL/UNKNOWN.

Sending a record is not acceptance. If no successor is available, mark **archived; successor acceptance pending**. Apply the same three-iteration limit to repair and verification.

### 5. Review access retirement separately

List access that may need review only in an approved restricted record. Credential revocation, account suspension, permission changes, session termination, and destructive cleanup require separate authorized owner action. Distinguish requested, completed, and independently verified retirement. Never include secret values or silently revoke shared access another service may use.

## Reactivation

Start from the latest canonical index and revision, not an old chat or archived approval. Read superseding decisions, WIP, and holds. Revalidate the tools, identity, permissions, features, and environment needed for the intended scope with safe reads. Repeat the bounded onboarding test, name an accountable owner and verifier, confirm the next authorized action, and issue a fresh PICKUP receipt. Dormant access and expired or task-specific approval are not standing authority.

## Completion receipt

| Field | Value |
|---|---|
| Transition ID and phase | |
| Accountable owner / agent / verifier | |
| Canonical record and exact commit | |
| Artifact coverage and known gaps | |
| Checks run and independent read-back | |
| Successor ACK and acceptance state | |
| Remaining holds, dependencies, and next checkpoint | |
| Separate owner actions still required | |

Report only the verified evidence level. Unknown remains unknown.

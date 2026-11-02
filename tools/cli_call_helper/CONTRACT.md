# Call helper v6: one extra O3 repair / final helper review candidate

This is source-only. No deployment, activation, new credential/IAM grant, release approval, new spending, automatic private-input retrieval, or full factory acceptance is claimed. Original input composition is preserved in the package's source manifest: PR441 `5aa4caae63c784175a0f9de0ff267fec22b69b96`, O3 `d832f9c784a801b828d742cf0e0fb6c0e1d1a724`, unchanged prior O3 patch SHA256 `91656b128b9f81547d1f4a210fafe42a2f936b1e0d9afab316bb9214615526d4`, whole tree `2fed1e57f5a72eed32e90ace1c1fad2217a86add`. The new exact clean scripts pin and complete delta manifest are supplied at freeze. Conference producer base is `41daf85bb75d53755d41db2dd45f941044b4e7c3`; its companion typed-cancel patch is required, not an assumed deployed interface.

## Exact input, without exporting the owner's words

`python call_ask.py accept call-0123456789ab --input-file /private/task.txt --expect mermaid` requires complete UTF-8 bytes from a private local file. No CRLF, pipe, whitespace or Unicode normalization is performed. A trailing newline matters. Missing, altered, unreadable, malformed UTF-8 or truncated content fails closed before ACK. The pinned chair emits only the first 1000 characters without a truncation marker, so source strings **at least 1000 characters are refused**, including exactly 1000. No preview is reconstructed into a full task.

The bridge reads the exact protected source event, current epoch and projection sequence. Its immutable source revision binds agent, source room, epoch, event, control sequence, projection ordinal, authenticated producer owner identity, exact UTF-8 byte count and SHA256. A go-ahead additionally binds the exact confirming event; task input remains the original complete utterance. The task's full text stays in the existing private inbox, never a new shared-board receipt. Helper responses contain only allowlisted identifiers, digests, lengths and lifecycle metadata. Manual/private input availability remains a functional gate: this is **not** autonomous full-text intake.

## Existing authentication path, stated precisely

The existing gateway verifies the bus-reconciler workload's Google ID token against issuer/audience/expiry/email and its reviewed identity allowlist. Column L is server-owned even when sender checking is off (off is explicitly unverified, not caller passthrough). Reconciler append now requests its existing workload metadata identity; unavailable identity refuses the append. No keys, IAM roles or runtime settings are created or changed by this package.

Helper protected reads and mutation receipts require the exact verified reconciler stamp, correct phase, exact payload correlation and a fresh 128-bit v6 request id. The raw-row adapter reads column L, not a caller-controlled header name. Legacy receipts and bare Source_Tag claims are not proof. Request/receipt bindings include the operation payload digest. Main CLI **request identity remains a governed claim**, not an authenticated-agent transport; the source and controller response are authenticated under the pinned gateway contract. Deployment of that exact hardened gateway and runtime metadata identity must be separately verified before using these claims live.

## Authority, ordering and unknown effects

`scripts/call_lifecycle.py` is used by actual bridge intake and actual `bus_request.handle` `do=call-task` dispatch. `status`, `card`, and `complete` are bounded operations, not arbitrary protected-key writes. Generic free-text RESULT rows cannot close a task. A completion requires the same immutable source revision, expected current lifecycle revision and exact stored artifact digest inside one WATCH/MULTI mutation. A cancel/expiry that wins that transaction cannot subsequently be overwritten by CARD or DONE.

Claims are PREPARED in the transaction containing initial card, private inbox delivery, persistent reservation and optional ring queue. OPEN activation follows only after every EXEC result is verified. An ambiguous crash/partial execution remains PREPARED, nonterminal and not eligible to complete; it is never blindly redelivered. Every terminal record is persistent rather than forgotten after a TTL. Active-task TTL is a separate server-expiring key watched by mutations; Redis >=6.0.9 expiration invalidates WATCH, closing expiry between precheck and EXEC. The existing production Redis version/configuration is a runtime gate.

Owner cancellation is a typed `cancel_task` command through the existing IAP gateway → server-only LiveKit packet → chair Session → protected `task.cancel.requested` event. It binds task id, immutable source revision, expected task revision, owner, room and epoch. Gateway success is only dispatch acknowledgment. Only the bridge's authoritative CAS accepts cancellation; generic speech interruption and keyword matches are not accepted-work cancellation.

The helper persists random mutation request IDs and exact rows before sending. Unknown append or controller outcomes are not blindly retried. Exact authoritative `card_op` or `completion_op` plus matching source/artifact digest can reconcile an unknown operation. Same-ID/different-payload or duplicate readback is not confirmation, and append rc=0 alone is not confirmation. `DEDUP_ENFORCED` remains False; no runtime dedup setting is inferred.

## Public cleanup is an explicit unresolved external boundary

Authoritative CANCELLED/EXPIRED revokes future CARD/DONE and removes known Redis cards in the same transaction. The real publisher filters revoked/missing-source call artifacts on subsequent snapshots. However, an older external GCS upload may already be in flight. No additional status read or one absent public snapshot proves it drained. Local cleanup remains **CANCELLING / PUBLICATION_UNRESOLVED**, even while authoritative lifecycle is terminal. This package does not claim synchronous public erasure, complete visible cancellation, or a new cross-system GCS fence. Full factory acceptance stays HOLD until that separate publication fence/drain is verified.

## State, migration and intake

`python call_ask.py migrate [--legacy PATH ...]` is a real bounded CLI dispatch. It ingests the old local state/pending/cursor/accepted evidence, takes the existing OS lock, saves byte-exact backups, then atomically writes v2. All pending and unknown/open legacy asks survive; acceptance is never completion proof, old unsigned DONE is only a historical claim. Missing v2 with legacy files refuses rather than silently starting empty. Existing v2 is not overwritten.

Accept uses direct per-ask authoritative lookup, so accepting B cannot advance an inbox cursor past undiscovered A. The consumer persists every cue on a page before returning one; cues are not authority. A cursor without a proven watermark starts at `0-0`, not wall-clock now. Kernel-held OS locks retain live owners regardless of age; ambiguous legacy PID lock files are not stolen. Windows behavior remains unverified by Linux tests.

## Verification and remaining gates

Builder tests are separately packaged; they are NOT independent evidence. Historical `test_v5.py`/`mutants_v5.py` describe the superseded v5 prefix/read/precheck contract and do not validate v6. The independent verifier owns production-path tests and one verdict for the complete frozen package. No test asserting obsolete prefix acceptance or free RESULT authority may be counted as a v6 positive control.

Separate live gates remain: exact deployed identity/configuration and producer switches, protected namespace exclusivity, gateway sender hardening, metadata identity, Redis expiration WATCH behavior, existing append dedup, private input provisioning, unattended wake/liveness, semantic artifact correctness, public cleanup fence/drain, owner ask-to-browser <=180 seconds, three consecutive TEST calls and owner acceptance. Preserve PR367 privacy hold and CAD50/day cap; the cap is not permission to spend. No push, merge, deployment or formal approval is authorized here.

## Frozen scripts identity

`8a97cd5c420c66bafb307d8c65a17d1900ab8e24` is the required clean `HEAD:scripts` tree. The companion gateway/producer identities remain separate mandatory deployment gates.


## Bounded containment repair (2026-10-30, parent9ed13422)

The `call-` canvas field namespace and explicit `ask`/`source_revision` metadata are managed, never generic redis-op writable. Whole-hash replacement/deletion checks existing managed entries under the same partition WATCH. Unrelated ordinary cards retain governed writes. Publication derives task identity from the exact bare ask or ask-work field and requires actual room/agent, exact source binding, authoritative state, live OPEN expiry, and canonical committed card digest. Missing, ambiguous, copied or legacy unbound managed data is hidden, not reconstructed from previews. Internal bridge wake/ACK claim-display refreshes use the same OPEN/active CAS and store their exact canonical digest. A task completion retains the existing ask-to-card timing schema before removing OPEN; task terminals retain precedence.

Both the real reconciler Dockerfile and .gcloudignore include call_lifecycle.py. Offline staging/import checks establish source closure only, not installed runtime dependencies, paid build, deployment, Redis server semantics or external publication drain. Manual private input and existing privacy/auth/runtime gates remain unchanged.

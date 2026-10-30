# claude-code-cli call-ask helper v5: contract v3, kept with the code

Builder: claude-code-cli. Independent verifier: Aya (one remaining helper cycle). Written 2026-10-30.
Answers AYA-FINISH-RESUME-20261030T133945Z-CLI (row 8448) and the action list AYA-CLI-IMPLEMENT-ACTIONLIST-20261030T1347Z. It supersedes contracts v1 and v2 where they differ.

## Pins (the frozen candidate)

| Part | Exact |
|---|---|
| Deployed main | `0b752b4a325da596348496e55b72feda1e237c71` (PR 437; canvas_publish blob `1710f0eb`) |
| O3 bridge chain | `origin/grok/doorbell-wake-ack` `d832f9c784a801b828d742cf0e0fb6c0e1d1a724`, merged cleanly onto main |
| O3 repair | Aya's repair.patch, sha256 `91656b12...526d4`, applied unchanged (3 EOF warnings) |
| Integration `scripts/` tree | `875a5f920a433d83c12679852ca3adb00d40b0a9`. laptop_post refuses any other tree, or a dirty one |
| Helper files | tools/cli_call_helper/ at the frozen head; the sha256 values are in MANIFEST |

The O3 chain and the repair are not on main and not deployed. The helper speaks the O3 interfaces, so the bridge and the helper release together, or not at all.

## Aya's items and where they are met

| # | Item | In code | Tests |
|---|---|---|---|
| 1 | Timing is owner ask to the matched public **and** browser result. The diagram is semantic, not just matching words | The helper stores stages (origin = bridge claimed_at, validated, accepted, stored, published), the brief, the card, its canonical hash and the sanitizer blob. The DONE row reports claim-to-public seconds and says it is NOT owner-ask-to-browser. Browser render and the semantic judgement are the verifier's | whole path; wrong type refused |
| 2 | Idempotency | Stable op ids. INTENT is saved before the append. An uncertain append is UNKNOWN and resolves only by a positive read. `DEDUP_ENFORCED = False` until the root task's live APPEND_DEDUP readback, so no uncertain append is retried. A later board timestamp proves nothing | landed-lost, absent-lost, not-verified rc, unreadable board, crash after INTENT, one retry only when enforced |
| 3 | Content provenance | Accept needs three things: the inbox entry `by=call-bridge` with `task_id=ask=<ask>`; the protected `gov:callbridge:open` record naming this agent; and the chair's protected `v1:conf:<conference_room>:events` entry whose `ask_id(room, id, agent)` is the ask and whose utterance matches the inbox words. It pages up to 3 times 20 from 60 s before the claim. O3's strict sender rule closes the `from=` forgery in settle | forged inbox, other agent's record, different words, later page, 200-char prefix |
| 4 | Cancellation and its boundaries | A fresh private status read happens before CARD and before DONE. The ask in the open record means go on. The ask gone means CANCELLED; after the card request it is CANCELLED_AFTER_WRITE plus one withdraw (`public:false`, so the publisher drops the card) and no DONE. No answer means UNKNOWN_AUTH: nothing is posted and nothing is cancelled. Once DONE is SENT, the ask is complete | cancel before card, after card, resumed WRITING, UNKNOWN_AUTH |
| 5 | Fresh status identities | Every read id is `CLAUDE-ST-<name>-<utc>+<random>`, new on every call, retry and resume | fresh ids |
| 6 | Lock and fence | An OS byte lock (msvcrt) on an open handle, so there is no steal, no pid and no age. A busy lock exits 4. A task `rev` makes a step that waited re-plan (exit 6) | live holder of any age, killed holder, two accepts at once, rev re-plan |
| 7 | Migration | `call_migrate.replay` rebuilds from evidence: a DONE row gives HISTORICAL_CLOSED (never acceptance); ACCEPTED or ACK only gives LEGACY_OPEN, or EXPIRED over 2 h. The old pending is kept as PENDING. The old cursor is a cross-check (CURSOR_ADVANCED). It is bounded at 500 entries | replay, bound |

The v4 HOLD's 7 failures, each covered by a v5 test. These were not run against the v4 code; the mutants in mutants_v5.py show that these tests catch the same defects reintroduced into v5:
1. Legacy done without evidence: replay.
2. Pending lost: replay.
3. Wrong artifact on an id+ts match: same id and ts, another body.
4. Cursor moved past an open ask: the cursor test.
5. Concurrent duplicate appends: two accepts at once.
6. Age-only steal: a live holder of any age.
7. Resumed WRITING bypassing cancellation: the resumed WRITING test.

## Gaps not closed by the helper (owners named)

- **Owner cancel of accepted work:** O3 cancels only a pending go-ahead. A cancel that removes an accepted ask from `gov:callbridge:open` is a bridge change. The O3 builder cycles are exhausted, so it is escalated to the owner and Aya.
- **Full content hash:** the reconciler quotes 200 characters per field, so provenance is a prefix match beyond that, and the task records `prefix-200`. A content sha256 in the bridge's open record would close it, which is a bridge change.
- **Dedup readback:** owned by the root desktop task. Until it lands, no retry happens.
- **Timing:** the measured earlier run took 188 s, made up of 55 s ring and 110 s compose. Each private read is about 11 s median and 20 s p90 (78 pairs, builder-measured). Accept needs 2 to 4 read rounds and done needs 2. The 180 s target is not shown.
- **Laptop window watcher:** retired in v5. PR 437 and O3 open the canvas window in the cloud.
- **Local full `unittest discover`** shows order-dependent failures (redis in sys.modules, a 10 s join under load, and the claude-api-worker Dockerfile import that also fails on main). CI runs those suites in separate steps.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done |
| 2 | Stopped, state kept, safe to rerun |
| 4 | Lock busy |
| 6 | Re-plan |

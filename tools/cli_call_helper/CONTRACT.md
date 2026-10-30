# claude-code-cli call-ask helper v5.1: contract v3.1, kept with the code

Builder: claude-code-cli (main CLI session 88774585). Independent verifier: Aya (one remaining helper cycle, unrun). Written 2026-10-30.
v3.1 answers Aya's PR 441 intake HOLD (8570, 8659). The plan is PR 441 comment 6533487332. It supersedes v3 where they differ.

## Pins (the frozen candidate)

| Part | Exact |
|---|---|
| Deployed main | `0b752b4a325da596348496e55b72feda1e237c71` (PR 437; canvas_publish blob `1710f0eb`) |
| O3 bridge chain | `origin/grok/doorbell-wake-ack` `d832f9c784a801b828d742cf0e0fb6c0e1d1a724`, merged cleanly onto main |
| O3 repair | Aya's repair.patch, sha256 `91656b12...526d4`, applied unchanged |
| Bridge 6a/6b | `bridge_6ab.patch` in this folder, on top of the repair. The owner's direct approval came at 21:56:39Z, source only (CLAUDE-CLI-OWNER-BRIDGE-GO-20261030T215653Z) |
| Integration `scripts/` tree | `7a89d16d7ca8b439d8eaaa8deafe01d3b3c7b8ea`. laptop_post refuses any other tree, or a dirty one |
| Helper files | This folder; the sha256 values are in MANIFEST.json |

Nothing beyond main is deployed. The helper speaks the O3 + 6a/6b interfaces, so the bridge and the helper release together, or not at all.

## Aya's intake findings and where they are met

| # | Finding | In code | Tests |
|---|---|---|---|
| 1 | The advertised migrate command had no dispatch | `call_ask.py migrate [--legacy]`: a bounded replay of up to 25 governed inbox pages of 20, from the legacy cursor minus 24 h, plus one full board read. It writes a NEW state only, and refuses one that exists, even an empty one | command builds and refuses; CLI dispatch; unanswered page writes nothing; replay; bound |
| 2 | A cold cursor at "now" could skip earlier claimed asks | `load()` with no state file stops (exit 5). The cursor never defaults to now. `accept` reads no cursor at all: everything comes from per-ask protected keys | no state means exit 5; cursor "0-0"; an earlier unindexed ask accepted behind a later one |
| 3 | A withdrawal was terminal while a delayed CARD could land | `CANCELLING` stays open. WITHDRAWN needs both answers: the CARD answer seen, then a withdraw whose reconciler answer row comes after it. If the order is reversed, a second withdraw (`-WD2`) goes. An unknown CARD past the deadline means FAILED_CARD_MAY_BE_LIVE, which is never called withdrawn | not final until the order is proven; reversed order sends WD2; deadline |
| 4 | Race between the final status read and DONE | DONE carries `status_op=<read id>`. One more state read happens after DONE. If the state is cancelled at a time before DONE, the task is marked `done_superseded` and goes to CANCELLING, so the card comes down and it never counts as acceptance | status_op in DONE; cancel between read and DONE |
| 5 | Readback checked id and tag, not the payload | INTENT stores the exact row and the sha256 of its payload. A resume uses that row, never a rebuilt one. CONFIRMED needs a fresh read of the id, tag **and** payload (`append_and_verify` checks the id only). Same id with another payload is CONFLICT, escalated with no retry. Status-read rows get the same check | ok-but-absent is UNKNOWN; payload conflict; one retry under dedup with the first row |
| 6a | Full-input provenance | The bridge writes `gov:callbridge:said:<ask>` (sha256 of the exact inbox words) and `gov:callbridge:said:<ask>:<n>` parts. Each part is a JSON string under the 200-character governed quote. `accept` rebuilds the WHOLE ask from these protected keys and requires the sha256 to match. The inbox is no longer a source, because a governed read cuts it at 200 | bridge: digest = inbox words, parts rebuild them, every part under 200 even with escapes. Helper: long ask whole, tampered part refused, malformed digest refused |
| 6b | Cancelling accepted work | The bridge writes `gov:callbridge:state:<ask>`, which is open, done or cancelled. The owner's bounded "cancel" (O3 `cancellation()`) also cancels his latest open ask **in that conference room**: only the named agents' if he named any, and only within PENDING_SECONDS. That ask is removed from the open record, so settle cannot flip it; the bridge's placeholder card says so, and an agent's own card is never overwritten. The helper reads this state before CARD, before DONE and after DONE | bridge: cancel flips state, settle refused, own card kept, other agent, other room, too old, "stop" in a task. Helper: cancel before and after the card |

The earlier items are unchanged from v3 (lock, fresh ids, stages and card hash, idempotency without dedup). Mutants (`mutants_v5.py`, on copies):
- 31 helper mutants and 10 bridge mutants: 41 of 41 killed.
- Builder results only.

## Gaps and dependencies (named owners)

- **Dedup readback:** owned by the root desktop task. Until it lands, `DEDUP_ENFORCED` stays False and nothing uncertain is retried.
- **Timing, not shown:**
  - accept needs 1 read round, or 2 when the ask has more than 90 characters; done needs 3 state reads;
  - each read takes about 11 s median and 20 s p90 (builder-measured);
  - the earlier measured run was 188 s.
- **Real readback shape:** the payload-sha check assumes the board stores the payload cell exactly as sent. That is still to be confirmed on a real row. If it is wrong, the helper fails closed (CONFLICT).
- **State-key size:** the per-ask state includes the conference room id. A very long room id could push it past the 200-character quote. The helper then treats it as UNKNOWN_AUTH (fail closed).
- **Not judged by the helper:** the semantic diagram check and the browser render. Those are the verifier's.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done |
| 2 | Stopped, state kept, safe to rerun |
| 4 | Lock busy |
| 5 | No state; run migrate |
| 6 | Re-plan |

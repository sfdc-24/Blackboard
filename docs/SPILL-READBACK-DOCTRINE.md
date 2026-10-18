# Spill Read-Back Doctrine

One-line addition to Operational Supervisor & Memory Management Implementation Plan V3, section 4.3 Resolution Loop. v2 adds the Aya sweep-block line.

## Context

PR 414 (merged 2026-10-15) proved the loss class. Every long Gemini reply posted a `full=` pointer to `gs://sfdc24-fleet-state/wakers/board-full-GEMINI-*`, but the object was never written — waker-gemini's bucket grant was scoped to exactly `wakers/gemini_waker.json`, so the create-only spill got HTTP 403. `fleet_agent` printed `SPILL_UNCONFIRMED`; `post_reply` saw VERIFIED on the row and printed nothing; the run log said only "posted." From Oct 14 to 15, every long Gemini answer was unreadable while looking successful. The IAM grant was widened outside the repo (objectCreator + objectViewer on `wakers/board-full-GEMINI-*` only, under condition). PR 414 made `post_reply` print the SPILL_UNCONFIRMED line even when the row verifies, with a regression test. Same narrow grant exists on `waker-claude-api` — a long claude-api answer would hit the identical 403.

## The gap V3 does not close

Section 4.3 step 3 says the supervisor logs Gemini's diagnostic receipt into the Owner Trigger. It never verifies the receipt actually persisted. If the same 403 class hits the diagnostic spill, the supervisor gets a receipt pointing at nothing and believes the escalation succeeded. V3 assumes the write worked.

## THE ONE LINE

Insert into 4.3 after step 3:

> **3b. READ-BACK:** after any spill write — board-full object, diagnostic receipt, or Aya sweep block — fetch the object and confirm it exists before logging success; on 403 or missing object, fail closed: print the failure, do not mark the escalation or sweep done, and append a FAIL line to the Owner Trigger.

## THE SECOND LINE (v2 — Aya sweep blocks)

> **3c. SWEEP READ-BACK:** the Aya three-hour sweep receipt block appended to the Owner Trigger is itself a spill-class write. Before the sweep is marked complete, read the block back from the Trigger and confirm the receipt text is present and matches what was written; on mismatch or missing block, fail closed: do not mark the sweep done, print READBACK_FAILED, and append a FAIL line naming the sweep window.

## Why sweep blocks need their own line

Board-full objects and diagnostic receipts are external object writes with HTTP status codes — a 403 is loud. A sweep block is an in-place text append to a single document. Appends fail differently: silent truncation, partial write, or a concurrent writer (drive-intake, another agent) overwriting the tail between the write and any later read. The Oct 14-15 Gemini loss was an external-object 403; the sweep-block failure mode is intra-document corruption. Same doctrine — a fact you could not fetch is not a fact (D-4) — different failure shape, so it gets step 3c rather than being folded into 3b.

Coupled risk: if the Trigger itself is the document being appended to and the Trigger's own save fails, 3b and 3c both false-negative. Mitigation: the read-back for 3c should target the Trigger's latest revision ID, not just latest content, so a stale cache cannot masquerade as success.

## Test shape

Mirrors PR 414. For 3b, a verified post whose spill 403s must surface the failure. For 3c, a forced append failure or a concurrent overwrite of the Trigger tail must surface READBACK_FAILED with the sweep window named. Clean paths stay quiet.

Filed by Aya 2026-10-18. Source: PR 414 analysis + V3 doc `1wWGkzcDue-EQVuFnEIqtDIiKMCbvG1KG5cP2uXSvNRg` + v1 doctrine `1eOFi7RVjMzsomLiGnzwLBDvpqJ0GdaCJ`.

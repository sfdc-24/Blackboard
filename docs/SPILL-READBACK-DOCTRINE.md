# Spill Read-Back Doctrine

One-line addition to Operational Supervisor & Memory Management Implementation Plan V3, section 4.3 Resolution Loop.

## Context

PR 414 (merged 2026-10-15) proved the loss class. Every long Gemini reply posted a `full=` pointer to `gs://sfdc24-fleet-state/wakers/board-full-GEMINI-*`, but the object was never written — waker-gemini's bucket grant was scoped to exactly `wakers/gemini_waker.json`, so the create-only spill got HTTP 403. `fleet_agent` printed `SPILL_UNCONFIRMED`; `post_reply` saw VERIFIED on the row and printed nothing; the run log said only "posted." From Oct 14 to 15, every long Gemini answer was unreadable while looking successful. The IAM grant was widened outside the repo (objectCreator + objectViewer on `wakers/board-full-GEMINI-*` only, under condition). PR 414 made `post_reply` print the SPILL_UNCONFIRMED line even when the row verifies, with a regression test. Same narrow grant exists on `waker-claude-api` — a long claude-api answer would hit the identical 403.

## The gap V3 does not close

Section 4.3 step 3 says the supervisor logs Gemini's diagnostic receipt into the Owner Trigger. It never verifies the receipt actually persisted. If the same 403 class hits the diagnostic spill, the supervisor gets a receipt pointing at nothing and believes the escalation succeeded. V3 assumes the write worked.

## THE ONE LINE

Insert into 4.3 after step 3:

> **3b. READ-BACK:** after any spill write — board-full object, diagnostic receipt, or Aya sweep block — fetch the object and confirm it exists before logging success; on 403 or missing object, fail closed: print the failure, do not mark the escalation or sweep done, and append a FAIL line to the Owner Trigger.

## Why one line and not a section

The board's own read-back doctrine (D-4) already says a fact you could not fetch is not a fact. This extends that to spills. The failure mode it kills is silent success — the exact shape of the Oct 14-15 Gemini loss.

## Test shape

Mirrors PR 414's regression test: a verified post whose spill 403s must surface the failure; a clean post stays quiet. Same `assertIn` pattern on the `SPILL_UNCONFIRMED` / `READBACK_FAILED` line.

Filed by Aya 2026-10-18. Source: PR 414 analysis + V3 doc `1wWGkzcDue-EQVuFnEIqtDIiKMCbvG1KG5cP2uXSvNRg`.

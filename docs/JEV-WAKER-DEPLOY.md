# Jev waker: release and verification

Jev answers addressed board rows with one typed decision from TypeSafe System One.
The owner controls merges and deployments. The data boundary is board rows and
PR metadata only. The working budget is $25/month, with the TypeSafe balance as
the hard stop. Do not put client content or conference transcripts in Jev asks.

## Current state (2026-11-03)

PR 456 was merged at cadbd72bcfe0484a8dc7864eb622a6c6e84423ac.
The jev-waker Cloud Run job and its cursor were created separately. A manual
execution (jev-waker-lwq26, 22:21 UTC) answered QC-1 and QC-2 on the board
using model jev-1.14.0. QC-1 received the scope hint because computing an
average is not one typed decision. That execution proves the manually started
job path, not the board-watcher trigger. The watcher route is in merged source;
confirm its deployed revision and an automatic wake before declaring the ring
path live.

The live v1 request uses state and named questions.q; answers are read from
answers.q. It supports noul (yes/no), choice, and score. Scores wider
than ten values are represented by at most ten ordered bands. The adapter
rejects malformed answer types, values, and probabilities instead of
publishing a decision.

## Release checks

1. Read back the existing waker-jev service account, jev-waker job,
   JEV_API_KEY plus the two bus-pair secret bindings, and the jev_waker
   cursor in gs://sfdc24-fleet-state/wakers. The waker needs no Redis or
   GitHub access. Do not reseed the cursor over answered IDs.
2. After the follow-up validation PR passes and the owner merges it, build
   the cloud/agent-waker/cloudbuild.yaml image from that merged main commit.
   The image must contain scripts/jev_agent.py; deploy the new image to
   the existing jev-waker job and read its digest back.
3. Confirm the deployed board-watcher revision contains the jev-waker
   route and points to the existing job. If it does not, the owner releases
   that watcher revision under the normal guarded procedure.
4. Post one new synthetic phase=ASK row addressed to=jev, with exactly
   one explicit spec such as "jev:yesno: Is this synthetic release check
   within Jev's typed-decision scope?" Confirm the watcher starts the job
   without a manual execution. Read back one wakerreply=1 row whose
   answers= equals that ask's full ID, whose source is jev, and whose
   probability is finite and between zero and one. Then check the cursor
   records the answer and a second pass does not answer it again.
5. For a provider failure or malformed answer, the waker must leave the
   task unanswered for a bounded retry and must not publish a decision.
   Inspect status and IDs in logs; do not print the API key or the row text.

Rollback: disable the watcher route or pause the jev-waker job using the
owner's existing release controls, then verify no new Jev answers appear.
Keep the cursor and answered IDs intact for recovery.

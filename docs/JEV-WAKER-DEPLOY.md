# Jev waker: first deploy (the owner's runbook)

Run after the checker pass and the merge. Everything here is the owner's own act; nothing in the
repo deploys itself. Jev's rulings of 2026-11-03 bound all of it: data boundary = board rows and
PR metadata only; budget = the $25/mo working budget with the TypeSafe balance as the hard stop.

1. **Service account.** Create `waker-jev` in project `sfdc24`, like `waker-gemini`:
   `gcloud iam service-accounts create waker-jev --project sfdc24 --display-name "Jev waker"`

2. **Secrets.** Grant `roles/secretmanager.secretAccessor` to
   `waker-jev@sfdc24.iam.gserviceaccount.com` on exactly three secrets: `JEV_API_KEY` and the two
   bus-pair secrets the other wakers read (the same two `gemini-waker` is bound to; list them with
   the waker's own job description rather than from memory). Nothing else: no Redis, no GitHub.

3. **The job.** Create Cloud Run job `jev-waker` (region `us-central1`) from the agent-waker image
   (`cloud/agent-waker/cloudbuild.yaml` builds it; the image carries every adapter and `AGENT`
   picks one): env `AGENT=jev`, the three secrets mounted as env vars, service account
   `waker-jev`, no VPC egress needed (api.typesafe.ai is public, the board is the GAS web app).

4. **Seed the cursor.** The cloud waker REFUSES to run without a seeded `jev_waker` cursor in
   `gs://sfdc24-fleet-state/wakers` — a missing cursor would mean a six-hour lookback and
   re-answers, so seeding is a deliberate act (see cloud/agent-waker/main.py's note). Seed it with
   the watermark at deploy time and empty answered lists, the same shape as `gemini_waker`.

5. **Ring path.** The board-watcher (this PR's route) starts `jev-waker` when a row addressed
   `to=jev` lands. No scheduler is required for v1; add a `jev-waker-15min` scheduler later only
   if the owner wants heartbeat passes like Gemini's.

6. **First live test.** The pending row `CLAUDE-CLI-VSEXT-JEV-QC-1-20261103T1918Z` is the first
   ask. Expect the adapter to answer or to report `systemone shape v0 mismatch` with the response
   keys: the wire field names were assumed from CR-20261103-001 (POST /v1/systemone, Bearer,
   model jev-latest) and the first call is allowed to correct them honestly. If it mismatches,
   the fix is one small follow-up PR to `scripts/jev_agent.py` with the observed shape.

Rollback: delete or pause the `jev-waker` job; the route then finds no job and the watcher
reports the start failure for that route alone.

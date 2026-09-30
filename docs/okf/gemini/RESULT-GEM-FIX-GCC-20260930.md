# Gemini OKF write-path fix ? RESULT GEM-FIX-GCC-20260930

- **signed_by:** cursor (execute) + grok (lead); Codex to verify
- **answers:** GEM-FIX-GCC-20260930T0225Z / GEM-30M-20260930T0225Z
- **landed_at:** 2026-09-30T02:40:00Z (America/Toronto ~10:40 PM ET)
- **evidence:** MEASURED against live gemini-waker job + Blackboard source

## Root cause

1. **Bus RESULT works.** After Blackboard #297/#298, gemini-waker posts `phase=RESULT` for DISPATCH/ASK rows.
2. **OKF file write does not.** `cloud/agent-waker/main.py` and Dockerfile explicitly withhold a write token. Live job env (us-central1, image `agent-waker:532b332`, SA `waker-gemini@sfdc24`) mounts only: `BUS_URL`, `BUS_SECRET`, `GEMINI_API_KEY`, `GEMINI_GITHUB_TOKEN` (Secret Manager label `access:read-only`).
3. **Public PR read is not OKF write.** `GEMINI_GITHUB_TOKEN` + `repo_context.py` attach public Blackboard/site PR diffs. Conference is private ? 404 by design (confused-deputy guard on #299).
4. **Doctrinal self-block.** Every waker reply historically said "no shell, no repo, no PR", so Gemini correctly refused to claim an OKF path (see `GEMINI-WAKE-GEM-OKF-20260929T1514Z`).

## Fix applied (this PR)

- `scripts/okf_land.py` ? path-allowlisted lander for `docs/okf/gemini/` on public Blackboard; opens PR; never auto-merges; never redirects token off `api.github.com`.
- `scripts/agent_waker.py` ? when ask wants signed OKF and `GEMINI_OKF_WRITE_TOKEN` is set, land file then cite `okf=` on the bus RESULT.
- Dockerfile + `.gcloudignore` allowlist updated so Cloud Build sees `okf_land.py`.

## Owner blockers (required for live Gemini self-land)

1. Create fine-grained PAT **GEMINI_OKF_WRITE_TOKEN**: repo `sfdc-24/Blackboard` only; permissions Contents:Write + Pull requests:Write; no private repos.
2. `gcloud secrets create GEMINI_OKF_WRITE_TOKEN` (or add version); IAM bind `waker-gemini@sfdc24` secretAccessor only.
3. Mount env `GEMINI_OKF_WRITE_TOKEN` on Cloud Run job `gemini-waker`.
4. Rebuild/redeploy `agent-waker` image from this PR tip; leave Scheduler/board-watcher as-is.
5. Do **not** load Claude on this thread; Codex verifies tests + review.

## Proof target

Bus row `from=gemini` `phase=RESULT` with `okf=` pointing at a PR under `docs/okf/gemini/`.
Until the write token is mounted, Cursor lands interim proof files on this path and Gemini cites them.

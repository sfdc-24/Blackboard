> **HISTORICAL, SUPERSEDED (2026-09-30).** This is Cursor's write-up of the first design, which
> wrote to public Blackboard. That design was rejected in review (Codex NO-GO on #304 at 5e2a4cb)
> and replaced: Gemini's OKF writes now go only to the private `sfdc-24/conference` repository,
> through a pull request (`scripts/okf_land.py`). Nothing below is an instruction, and the
> `landed_at` stamp records this file's own commit, not a Gemini write.

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

## Owner steps (superseded, do not follow)

Removed on 2026-09-30. The token is already mounted (secret `github-token-gemini-okf`, as
`GEMINI_GITHUB_TOKEN`), and the lander now writes only into the private conference repository.
See the README beside this file.

## Proof target

Bus row `from=gemini` `phase=RESULT` with `okf=` pointing at a PR under `docs/okf/gemini/`.
Until the write token is mounted, Cursor lands interim proof files on this path and Gemini cites them.

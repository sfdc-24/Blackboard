# Claude off the laptop: the Claude Console API, run by Cloud Run

**claude-code-cli, 2026-10-01. Revision 4.** It replaces revisions 1–3 (the claude.ai-routine pilot, head
`a101c00`), and the file is renamed from `MIGRATE-OFF-LAPTOP-CLAUDE-STAGE1.md` to say so.

It answers:
- Grok's `MIGRATE-OFF-LAPTOP-CLAUDE-20260930`;
- Codex's `CODEX-MIGRATE-OFF-LAPTOP-PICKUP-20260930T1640Z` and `CODEX-RESUME-MEMORY-20261001T0006Z`;
- the owner's direction below.

It is a spec for review and builds nothing.

**Evidence levels:**
- **CHECKED**: read in code, in the docs, or on the board on 2026-10-01.
- **PROPOSED**: a design choice.

## Why the route changed

**Mr. Salam, directly in the claude-code-cli terminal at about 00:40Z on 2026-10-01 (board `CCC-CONSOLE-DIRECTION-20261001T0044Z`):**
"I'd like to see the activities move from laptop to claude console" and "have cloud run as a replacement for
whatever you are doing here".

Revision 3 named this route **Stage C** and kept it out of scope. It is now the plan. The claude.ai routine pilot is
superseded: it ran on his claude.ai subscription, which is not the Console.

What it carries over from revisions 2–3, which answered Codex's three P1 blockers on `633fae9`:
- no private text reaches the agent until an owner gate;
- at most one board effect per Row_ID, and an ambiguous outcome is UNKNOWN;
- the agent never manages its own scheduling (Grok's `POKA-YOKE-ROUTINE-SELF-DELETE-20260930`);
- the laptop path stays live until hosted proof.

**What happened on 2026-10-01 makes the case (CHECKED):**
- At 00:35Z Claude Code's low-memory reaper killed the laptop doorbell and a CI watcher, with 3–4 GB of 15.7 GB free.
- The lane then answered only through a 5-minute session cron, which itself dies with the session.
- Codex's resource task asked for exactly this: one bounded workflow moved to Console or OpenAI infrastructure, with
  read-back and rollback.

## What ties the Claude lane to the laptop today, and where each piece goes

| Today, on the laptop | Goes to | Stage |
|---|---|---|
| The doorbell (`inbox_check.py --wait 45`) and the 5-minute session cron | **`board-watcher`**, which already runs every minute in Cloud Run and starts a job per route (CHECKED: `cloud/board-watcher/main.py` `_routes()`, `docs/CLOUD-FLEET-RUNBOOK.md`). A new route, `claude-code-cloud` | C1 |
| The work itself: reading a task, editing code, running tests, opening PRs, posting results | **`claude-code-cloud`**, a new Cloud Run job running the Claude Agent SDK under a Console API key | C1–C3 |
| The `gh` user login | a GitHub credential scoped to two repos, in Secret Manager | C2 |
| The `gcloud` user login (read-backs, logs) | the job's own service account, least privilege, read-only at first | C2 |
| Posting to the board with `BUS_SECRET` | **`ccc-board-relay`**, a Cloud Run service that holds `BUS_SECRET` and authenticates the job by its Google identity (an OIDC ID token, so no new shared secret) | C1 |
| My memory directory | a GCS prefix, synced at the start and end of each run, compare-and-swap as `wakers` cursors already are | C2 |
| Mutation suites | GitHub Actions; already done for conference #126 (16 shards, 19/19 green at `e931980`) | done |
| The call-log watch | a Cloud Monitoring log-based alert on `conference-chair-pool`, posting through the relay | C4 |
| Rehearsal calls | a Cloud Run job that runs `live_rehearsal.py` against `conference-chair-dev` | C4 |
| Chair deploys | stay human-gated on the laptop until C4 has its own review | later |
| The Codex WhatsApp relay | stays on the laptop while Codex is a desktop app | n/a |

## Facts that shaped the design

**Claude Agent SDK** (CHECKED on code.claude.com/docs/en/agent-sdk: `hosting`, `permissions`, `cost-tracking`):
- **Runtime:** the SDK spawns a bundled `claude` CLI subprocess. It needs Python 3.10+, and the docs suggest 1 GiB
  RAM, 5 GiB disk and 1 CPU per agent as a starting point.
- **API key:** the subprocess reads `ANTHROPIC_API_KEY`. Alternatively, `ANTHROPIC_BASE_URL` points at a proxy that
  injects the key outside the container.
- **Permissions:** `permission_mode="dontAsk"` with `allowed_tools` approves only the listed tools and denies any call
  that would prompt.
  - Read-only Bash, file reads inside the working directory, and `Agent` still run unlisted.
  - A bare name in `disallowed_tools` removes that tool.
  - `bypassPermissions` ignores `allowed_tools`, so it is never used here.
- **Bounds:** there is no session timeout, so `max_turns` bounds the run. `max_budget_usd` caps one call's spend, and
  the result's subtype is then `error_max_budget_usd`. `total_cost_usd` is a client estimate; the authoritative figure
  is the Console Usage page or the Usage and Cost API.
- **State:** transcripts, `CLAUDE.md` memory and the working directory are lost when the container ends. A
  `SessionStore` mirrors transcripts only, so memory needs its own sync.
- **Cost:** the docs put a minimal container at about $0.05 an hour and say token cost dominates.

**Console workspaces** (CHECKED on platform.claude.com/docs/en/manage-claude/workspaces):
- A workspace can have a **monthly spend limit with alert thresholds**, and rate limits.
- Limits cannot be set on the Default Workspace.
- A service-account key can be scoped to a single workspace.
- The Usage and Cost API reports per workspace.

**Claude Managed Agents** (CHECKED on platform.claude.com/docs/en/managed-agents/overview):
- **Beta** (header `managed-agents-2026-04-01`).
- Anthropic hosts the loop. The sandbox is Anthropic-managed or self-hosted.
- Sessions are stateful.
- It is not eligible for ZDR.
- It is a candidate for later, not the first build: the fleet's credentials and least-privilege identities are already
  built around Cloud Run service accounts.

## Design (PROPOSED)

```
board row to claude-code-cloud            (C1: probes only; C2: fleet rows; C3: owner WhatsApp, owner gate)
   │  board-watcher, every minute (exists): a new route `claude-code-cloud`, its own CAS cursor in gs://sfdc24-fleet-state
   ▼
claude-code-cloud  (Cloud Run job, max-retries 0, own service account, Console key from Secret Manager)
   │  git clone of the named repo at its default branch; memory synced from GCS
   │  Agent SDK: permission_mode=dontAsk, allowed_tools fixed per stage, max_turns and max_budget_usd per run
   │  pushes only claude-code-cloud/* branches; never merges; never deploys (C1–C3)
   ▼
ccc-board-relay  (Cloud Run service, no public invoker: only the job's service account holds run.invoker)
   │  checks the caller's Google identity is claude-code-cloud's service account
   │  claims receipts/{work_id} in Firestore (create-if-absent), then posts with BUS_SECRET, Source_Tag claude-code-cloud
   ▼
board row from claude-code-cloud, read back by Row_ID
```

### Boundary 1: what the agent may see, staged

| Stage | Rows admitted | Text the agent sees |
|---|---|---|
| **C1** | `CCC-CLOUD-PROBE-*` from Codex's test lane, addressed to `claude-code-cloud` | a fixed envelope only: `{row_id, task: "probe-receipt"}` |
| **C2** | fleet rows (Grok, Codex, Cursor) addressed to `claude-code-cloud` | the row text. These are agents' rows, not the owner's WhatsApp |
| **C3** | the owner's WhatsApp rows addressed to the Claude lane | his text, only after his explicit GO |

- **Enforced in the route predicate and the relay, not by the prompt.** The relay refuses any receipt whose
  `work_id` is outside the stage's admitted set.
- **Parallel run, no double answers.** Through C1–C2 the route answers only rows addressed to **`claude-code-cloud`**.
  The laptop keeps answering `claude-code-cli`.
  - The cutover, where the route also takes `claude-code-cli` and the laptop doorbell stops, is **C5 and the owner's
    decision** (`gcloud-migration-staging-rule`: he decides decommission).

### Boundary 2: at most one board effect per row
- **board-watcher's per-job CAS cursor** starts a row's job at most once (CHECKED: runbook, "two runs cannot answer the
  same row twice").
- **The job runs with `max-retries 0`.** A failed task is not rerun by Cloud Run; it stays UNKNOWN for a person or the
  laptop lane.
- **The relay claims `receipts/{work_id}` before it posts.** A second receipt for the same work is refused. An
  uncertain post is read back by Row_ID and never posted again.
- **UNKNOWN is never a pass.** A gateway flap makes a test UNKNOWN, and it is repeated.

### Boundary 3: what the agent can touch
- **Permissions:** `dontAsk`, with an explicit `allowed_tools` per stage.
  - C1: no Bash, no web, no file writes. One relay call.
  - C2: Read, Edit, Write, Grep and Glob, plus `Bash(git *)`, `Bash(gh pr *)`, `Bash(python -m unittest *)` and the
    read-only `gcloud ... describe/list/logging read` patterns.
  - `disallowed_tools` names `WebFetch` and `WebSearch` until a stage needs them.
- **Service account:**
  - It has no `run.jobs.update`, `run.services.update` or `cloudscheduler.*` permission, so the agent cannot change
    its own schedule, image or route (the poka-yoke is enforced by IAM).
  - It has no deploy rights in C1–C3.
  - It can read only its own secrets.
- **GitHub:** a fine-grained token limited to `sfdc-24/Blackboard` and `sfdc-24/conference`, contents and pull
  requests only.
  - Branch rules keep `main` merge-only by a person or the laptop lane.
  - The ownership check (`tools/check_ownership.py`) gets a `claude-code-cloud/` prefix with the same folders as
    `claude-code-cli/`.
- **Network:** outbound to `api.anthropic.com`, GitHub, the board gateway (through the relay only) and Google APIs.
  An egress allowlist is C2 work.

### Boundary 4: spend, bounded three ways, then measured
1. **The Console:** a dedicated workspace, `fleet-claude-cloud`, with a **monthly spend limit and alerts the owner
   sets**. The job's key is a service-account key scoped to that workspace only.
2. **Per run:** `max_budget_usd` and `max_turns`, set in the job's configuration.
3. **Per day:** a daily start cap, added to the new route in board-watcher. It is 10 in C1, and day one's measured
   cost is reported before it is raised.

No cost claim is made before day one is measured. Cloud Run, Scheduler and Firestore prices are quoted from the GCP
pricing pages in the PR that creates each resource.

## How Codex's API test lane verifies each stage

Each check has a pass condition and is run by Codex, not by me. These carry over from revision 3, adapted:

1. **Reach.** A `CCC-CLOUD-PROBE-<unique>` row to `claude-code-cloud`. PASS when exactly one `claude-code-cloud`
   receipt names it within 5 minutes, read back by Row_ID.
2. **Laptop off.** Test 1 again with the laptop shut down. This is the test that matters most.
3. **Boundary.** A WhatsApp row, and a `claude-code-cli` row. PASS when neither starts the job (board-watcher's log
   and cursor show it) and neither gets a cloud receipt.
4. **Identity.**
   - A relay POST without the job's identity: 401, and nothing written.
   - A POST whose body claims `grok`: still `claude-code-cloud`.
   - A POST for a work_id outside the stage: refused.
5. **Two starts.** The job started twice by hand for one probe. PASS when there is one board row.
6. **Committed but timed out.** A relay post whose answer is dropped (a test hook). PASS when there is at most one row,
   and the read-back settles it.
7. **Spend stop.** A probe run with `max_budget_usd` set below one turn. PASS when the result is
   `error_max_budget_usd`, and the receipt says so.
8. **No self-management.** Read the service account's roles back: no run, scheduler or IAM write. The job list,
   images and schedules are unchanged after the pilot.
9. **Rollback.** Remove the route (a one-line board-watcher revert) or pause `board-watcher-2min`. PASS when the next
   probe gets no cloud receipt, and the laptop lane still answers `claude-code-cli`.
10. **C2 work.** A real Codex task to `claude-code-cloud` (for example "fix this test and open a PR"). PASS when a PR
    exists on a `claude-code-cloud/` branch, CI ran, nothing merged, and one RESULT row names it.

## Owner gates (Mr. Salam)

Nothing here is authorized by this spec. Each gate is one step, and secret values never pass through an agent:

1. **The Console:**
   - create the workspace `fleet-claude-cloud`;
   - set its monthly spend limit and alerts;
   - create a service-account key scoped to it;
   - store the key with one `gcloud secrets create ... --data-file=-` line, which I give him, run in his own terminal.
2. **GitHub:** a fine-grained token for the two repos, stored the same way.
3. **GCP**, priced in the PR that creates each:
   - the `claude-code-cloud` job and its service account;
   - the `ccc-board-relay` service and its service account;
   - the `receipts` Firestore collection (the database already exists, in northamerica-northeast2);
   - the GCS memory prefix.
4. **C3:** the GO to let the agent read his WhatsApp text.
5. **C5:** the cutover and the laptop decommission.

**Fleet gates:**
- Codex gives AGREE or BLOCKERS on this revision.
- Cursor gives an exact-head GO on each code PR before it deploys.
- Grok is told at each stage.

## Rollback
At every stage, the rollback is removing the `claude-code-cloud` route from board-watcher (a revert) or pausing the
job, and the laptop lane is untouched until C5. The relay and job can be deleted without touching anything else: no
other job reads their state.

## Related
- Pipedream Workflows shut down on 2027-03-31 (Grok's `PIPEDREAM-WF-EOL-NOTE-20260930T0038Z`). The WhatsApp inbound
  path needs a new home on the same Cloud Run base. Its inventory is Blackboard #307
  (`docs/PIPEDREAM-WA-INVENTORY-20261001.md`). It is a separate track with its own owner step: the Meta callback switch.

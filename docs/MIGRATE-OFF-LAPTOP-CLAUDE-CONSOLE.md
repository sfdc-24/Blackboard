# Claude off the laptop: the Claude Console API, run by Cloud Run

**claude-code-cli, 2026-10-02. Revision 8.** It answers the exact-head reviews of revision 7 (`62becdd`): Codex's
staged verdict (C1 architecture AGREE and C1 test-design GO; **C2 source NO-GO**), Cursor's NO-GO (sandbox egress),
and Copilot's seven open findings. The decision is now recorded per stage (the gate card below, Codex's
stage-scoped gate for U4).
- **Boundary 2, which C1 uses too (Codex P1 3, Copilot):** a duplicate start that found `STARTED` posted BLOCKED
  through `receipts/{work_id}`, the claim reserved for the owner's result, and could quarantine a row whose first
  execution was still running. Now only the execution holding `runs/{work_id}` may claim `receipts/{work_id}`. A
  duplicate does nothing while the claimant may still be running. An abandoned claim's diagnostic uses its own key,
  `diagnostics/{work_id}` (Boundary 2, tests 5 and 13).
- **The kill switch has a store (Copilot):** one object in a new bucket, `ccc-control`, that only the owner writes.
  The broker and the proxy hold read-only access to it, and a missing or unreadable object means OFF (Rollback,
  test 17). The bucket is a new C1 resource that #310 does not create yet.
- **Row text is never in an execution override (Copilot):** the override carries only the `work_id`, and the harness
  reads the text through the broker's `get_task` (Boundary 1, test 20).
- **C2's sandbox (Codex P1 1 and 2, Cursor P1, Copilot):** edited test code no longer holds the signed URLs or any
  network. A trusted runner from the image does the transfer and observes the exit. The tests run as another user,
  under a seccomp filter that refuses every network socket, with their output piped to the runner, never to platform
  logs. The upload is a signed POST policy whose size limit Cloud Storage enforces. `run_tests` is the agent's
  feedback and never evidence: a RESULT cites only GitHub's checks on the PR (Boundary 3, tests 14, 16 and 19).
- **Ownership (Copilot):** the broker checks paths against its own reviewed copy of the ownership manifest, never
  the bundle's (Boundary 3, test 18).
- **C2 stays HELD** until its own fresh exact-head source verdict. Revision 8 is the proposal for that review; it is
  not a C2 agreement.

### Stage gate card at this head (Codex's stage-scoped gate)

Each stage has four separate decisions, per exact head. A GO at one level is not evidence for the next.

| Stage | 1. Source architecture | 2. Bounded tests | 3. Deployment | 4. Owner acceptance |
|---|---|---|---|---|
| **C1** (synthetic probes) | Codex AGREE at `62becdd`. Revision 8 changes Boundary 2 and the kill switch's store, which C1 uses, so they need a fresh exact-head verdict | Codex GO at `62becdd` for tests 1, 3–9, 11 and 13; revision 8 adds 17 and 20 | #310, **plus the `ccc-control` bucket and its two read grants, which #310 does not hold yet**; Cursor's exact-head GO; then the owner's GO | read-backs of tests 1, 3–9, 11, 13, 17 and 20 |
| **C2** (fleet rows, private repositories) | **NO-GO at `62becdd`** (Codex, Cursor). Revision 8 answers it, below; a fresh verdict is needed | HELD | HELD: no C2 resource is created | HELD |
| **C3–C5** | not started | — | — | — |

**Carried findings, each with its disposition and its negative control.** They stay on this card through every
successor head until a review closes them.

| Finding | Disposition in revision 8 | Negative control |
|---|---|---|
| Codex P1 1, Cursor P1, Copilot `4162145392`: sandbox egress is not a bucket allowlist | the tested process has no network: a seccomp filter refuses every non-Unix socket; only the trusted runner transfers | test 14: a write to a second bucket fails; no socket opens |
| Codex P1 2, `4162148589`: edited code can forge `{"exit": 0}` | the upload capability is in the runner only; the runner records the exit it observed; `run_tests` is never evidence | test 16: a forged result is never what the broker returns |
| Copilot `4162145357`: untrusted output in platform logs | the child's stdout and stderr are pipes to the runner, which logs no byte of them | test 14: a private sentinel is absent from Cloud Logging |
| Codex P1 3, `4162148600`, Copilot `4162179542`: a duplicate start consumes the result claim or quarantines an active run | only the claimant may claim `receipts/{work_id}`; a duplicate does nothing while the claimant may be running; diagnostics have their own key | tests 5 and 13 |
| Copilot `4162179508`: row text in an execution override | the override is the `work_id` only; the text comes through `broker.get_task` | test 20 |
| Copilot `4162145446`: the kill switch has no store, readers or fail-closed rule | one owner-written object in the bucket `ccc-control`, read-only to the broker and the proxy; missing or unreadable means OFF | test 17 |
| Copilot `4162145416`: `tools/check_ownership.py` does not exist; ownership is undefined | the manifest is in the broker's image, reviewed with its code PR; the bundle's copy is ignored | test 18 |
| Copilot `4162145470`: a signed PUT does not enforce 256 KiB | a signed POST policy with `content-length-range` | test 19 |

**Monitored risks.** Each is accepted only because it is bounded and reversible. None is a credential, data,
forged-result, duplicate-effect or rollback gap.

| Risk | Stage | Owner | Signal | Guard or stop | Recovery | Checkpoint |
|---|---|---|---|---|---|---|
| R1: while a crashed claimant's execution state cannot be read, its row stays pending and restarts the job each minute; each start runs no agent and posts nothing | C1 | claude-code-cli | board-watcher's start log for the route | the daily start cap (10 in C1), then the claimant's task timeout plus 60 seconds, after which the claim is abandoned; the kill switch | the owner reconciles the `diagnostics/{work_id}` row | test 13, before C1 acceptance |
| R2: `gcloud run deploy` creates or updates, so a `ccc-broker` made between the preflight and the deploy would be updated (#310) | C1 | the owner, who runs the apply once | the apply's own output and the describe | create-or-refuse on everything else | redeploy from the reviewed image | the C1 apply |

**Revision 7** answered Codex's review of revision 6 (`9cf6802`):
- **P1, the sandbox's data channel:** a no-role sandbox had no way to get the bundle or return its log. The broker now
  gives it two single-object V4 signed URLs: GET for its bundle, PUT for its result. Its identity still holds no
  role, and its egress is closed except to Cloud Storage (Boundary 3).
- **P1, in-flight runs:** the kill switch was read once, at start. It is now checked on every effectful broker and
  proxy operation, and rollback also cancels running executions (Rollback, test 9).
- **P2, memory:** the job has no storage access, so memory goes through two broker operations on a dedicated bucket,
  bounded in size and compare-and-swap (the table, Boundary 3).
- **Cursor's NO-GO on revision 6:** the same git blocker, answered by the item below. Also: `claim_run` must return
  *created* before the agent starts (atomic, and a timeout is not absence); quarantine is the fleet's `unknown_ids`;
  and the egress allowlist is part of C2's entry gate.
- **Copilot's blocker, git's own execution surfaces:** an allowed `git status` or `commit` could run a hook, a pager,
  a filter or a diff driver planted through `.git`. **The agent no longer runs git, or Bash, at all in C2.** Its
  working copy has no `.git`, and file tools refuse any `.git` path. The harness builds the commit from outside it,
  with a hardened git (Boundary 3, test 15).

**Revision 6** answered Codex's NO-GO on revision 5 (`41c95e6`, Blackboard #306 comment 5943835443, board
`CODEX-PR306-NOGO-20261002T0125Z`):
- **P1, credentials:** in C2, model-influenced tests ran under an identity that could read the Console key and the
  clone credential. From C2 the job holds **no credential and no secret access at all**. The Console key sits behind
  a key-injecting proxy. The repository comes from a separate clone identity. Tests run in a sandbox job whose
  identity holds no role. An edited test proves the denial (Boundary 3, test 14).
- **P1, one run per row:** `max-retries 0` and the receipt claim did not stop board-watcher restarting the job after a
  crash. A durable claim, `runs/{work_id}`, is now made **before the agent starts**. A second start finds it and stops
  for reconciliation (Boundary 2, test 13).
- **P1, rollback:** removing a route did not clear its pending job, and pausing the watcher stopped every route. There
  is now a route-specific kill switch and a route-specific IAM revoke, with pending reconciliation (Rollback, test 9).
- **P2:** the PR description is rewritten for this design and head. Test 4 now separates Cloud Run's IAM rejection of
  an unauthenticated call from the broker's refusal of a forged payload sent by an authorized caller.

It also adopts three decisions made since revision 5:
- the separate `ccc-receipts` database (#310);
- the separate clone identity for the read-only GitHub App (#309);
- the broker's caller check, which needs **both** its own audience **and** the job's email (#310).

Revision 5 answered Codex's two P1 blockers on revision 4 (`78930da`, Blackboard #306 comment 5929294107):
- **Writes:** `git` and `gh` gave the agent writes beyond its own branch and PR. Writes now go only through a broker
  service that holds the only write credential (Boundary 3).
- **C1 tools:** C1 removed too few tools, because `dontAsk` does not remove a tool. C1's tools are now removed by name,
  checked at startup and checked again by a hook.

Revision 4 replaced revisions 1–3 (the claude.ai-routine pilot, head `a101c00`), and renamed the file from
`MIGRATE-OFF-LAPTOP-CLAUDE-STAGE1.md` to say so.

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
| The `gh` user login | **`ccc-broker`**, a Cloud Run service that holds the only GitHub write credential and performs only "push an owned `claude-code-cloud/` branch and open its PR". The repository arrives as a read-only git bundle from **`ccc-fetch`**, which runs as a separate clone identity holding only the read-only App key (#309). The job never holds a GitHub credential | C2 |
| The `gcloud` user login (read-backs, logs) | not in the job: its identity holds no role but `run.invoker` on the fleet's own services. Read-backs are broker operations, added one at a time with review | C2 |
| Posting to the board with `BUS_SECRET` | **`ccc-broker`** also holds `BUS_SECRET` and posts the board rows. It authenticates the job by its Google identity (an OIDC ID token, so there is no new shared secret) | C1 |
| My memory directory | the bucket `ccc-memory`, reached only through the broker's `memory_get` (at the start) and `memory_put` (at the end of a DONE run): at most 1 MiB, compare-and-swap on the object generation as `wakers` cursors already are. The job itself has no storage access | C2 |
| Mutation suites | GitHub Actions; already done for conference #126 (16 shards, 19/19 green at `e931980`) | done |
| The call-log watch | a Cloud Monitoring log-based alert on `conference-chair-pool`, posting through the broker | C4 |
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
claude-code-cloud  (Cloud Run job, max-retries 0, own service account)
   │  the execution override carries the work_id only; the task text comes from broker.get_task(work_id) (Boundary 1)
   │  harness, FIRST: broker.claim_run(work_id) -- create-if-absent runs/{work_id}; EXISTS => no agent (Boundary 2)
   │  harness: broker.control() -- the route's kill switch, an owner-written object; missing, unreadable or OFF =>
   │           quarantine pending, no agent (Rollback); the broker and the proxy check it on every effectful operation
   │  C2+: ccc-fetch returns a read-only git bundle; no GitHub credential ever enters the job
   │  Agent SDK: dontAsk; tools REMOVED by name per stage; a PreToolUse hook allowlist; max_turns and max_budget_usd per run
   │  C1: Console key in the job (one tool, no code execution).  C2+: ANTHROPIC_BASE_URL = ccc-llm-proxy, no key in the job
   │  C2+: tests run by broker.run_tests in ccc-sandbox: a trusted runner, and the tests as another user, no network
   ▼
ccc-broker  (Cloud Run service, --no-allow-unauthenticated; the job is added as an invoker)
   │  accepts a call only when the verified ID token has its own audience AND the job's email (#310)
   │  get_task: reads the row by Row_ID, checks the stage admits it, returns its text (Boundary 1)
   │  claim_run / finish_run: runs/{work_id} in the ccc-receipts database (Boundary 2)
   │  post_receipt / post_result: ONLY the claimant execution claims receipts/{work_id}, then posts with BUS_SECRET
   │  post_diagnostic: an abandoned claim's BLOCKED row, under its own claim diagnostics/{work_id}
   │  open_pr (C2): takes a git bundle; validates repo, owned branch, fast-forward, ownership paths; pushes; opens the PR
   │  run_tests (C2): bundle to ccc-sandbox-io; starts ccc-sandbox with a signed GET URL and a signed POST policy;
   │                  returns {exit, log} as the agent's feedback, never as evidence
   │  memory_get / memory_put (C2): ccc-memory, at most 1 MiB, compare-and-swap
   ▼
board row from claude-code-cloud, read back by Row_ID;  PR on a claude-code-cloud/ branch, never merged by the agent
```

### Boundary 1: what the agent may see, staged

| Stage | Rows admitted | Text the agent sees |
|---|---|---|
| **C1** | `CCC-CLOUD-PROBE-*` from Codex's test lane, addressed to `claude-code-cloud` | a fixed envelope only: `{row_id, task: "probe-receipt"}` |
| **C2** | fleet rows (Grok, Codex, Cursor) addressed to `claude-code-cloud` | the row text. These are agents' rows, not the owner's WhatsApp |
| **C3** | the owner's WhatsApp rows addressed to the Claude lane | his text, only after his explicit GO |

- **Enforced in the route predicate and the broker, not by the prompt.** The broker refuses any receipt whose
  `work_id` is outside the stage's admitted set.
- **Delivery (Copilot on revision 7).** A per-execution override is part of the execution's configuration, which any
  `run.executions.get` viewer can read. So the override board-watcher passes carries **only the `work_id`**. The
  harness reads the text through **`broker.get_task(work_id)`**:
  - the broker reads the row by Row_ID with its own bus credential;
  - it refuses a row that is not addressed to `claude-code-cloud`, or is not admitted at the current stage;
  - it returns the text to the caller only, after the same caller check as every operation. In C1 it returns the
    fixed envelope;
  - it logs the `work_id` and the text's length and digest, never the text (test 20).
- **Parallel run, no double answers.** Through C1–C2 the route answers only rows addressed to **`claude-code-cloud`**.
  The laptop keeps answering `claude-code-cli`.
  - The cutover, where the route also takes `claude-code-cli` and the laptop doorbell stops, is **C5 and the owner's
    decision** (`gcloud-migration-staging-rule`: he decides decommission).

### Boundary 2: at most one agent run, and one board effect, per row
**Why a cursor and `max-retries 0` are not enough** (Codex P1 on revision 5; CHECKED in `cloud/board-watcher/main.py`
on main):
- board-watcher keeps a row in the route's `pending` list until the job's own cursor records it as answered or
  quarantined.
- While anything is pending and the job is not running, the next tick starts the job again.
- So a job that crashes after the agent starts, but before it writes its cursor, is started again, and the agent
  would run a second time. The receipt claim stopped only a second board row, not a second run.

**The design (PROPOSED):**
1. **A durable claim before the agent starts.** The harness's first act for each work_id is `broker.claim_run(work_id,
   execution)`: create-if-absent `runs/{work_id}` in the `ccc-receipts` database, `state: STARTED`, with the Cloud
   Run execution name and time.
   **Binding reading** (Cursor on revision 6): the agent starts only when **this** execution's `claim_run` returns
   *created*. The create is one atomic Firestore create, not a read followed by a write. A timeout, an error or any
   other answer is not absence: the agent does not start.
2. **A second start never runs the agent, and never takes the claimant's result.** If `runs/{work_id}` already
   exists, in any state, the harness does not start the agent. In revision 7 the duplicate posted BLOCKED through
   `receipts/{work_id}` and quarantined the row while the first execution might still be running, so a successful
   first run could lose its result (Codex P1 3 and Copilot on revision 7). What the duplicate does now depends on the
   claim:
   - **`DONE` or `FAILED`:** the claimant finished but did not write its cursor. The duplicate appends the work_id to
     `answered_ids` (DONE) or `unknown_ids` (FAILED), posts nothing, and exits.
   - **`STARTED`, and the claimant may still be running:** the duplicate **does nothing**: no quarantine, no row, no
     receipt. The broker reads the claimant execution's state, with `run.viewer` on this job only (PROPOSED; the
     broker's code PR verifies that a job-level grant reads the job's executions). Running, or any read that fails
     or is unclear, counts as *may still be running*. The row stays pending, and board-watcher does not
     start the job while an execution runs.
   - **`STARTED`, and the claimant has ended:** its execution reads as succeeded, failed or cancelled, or it is past
     its task timeout plus 60 seconds. The claim is abandoned:
     - the duplicate appends the work_id to **`unknown_ids`**, the fleet's quarantine (`cloud/agent-waker/main.py`
       `quarantine`). board-watcher's `finished_ids` reads `answered_ids` and `unknown_ids`, so `reconcile()` drops it
       from pending (Cursor on revision 6: a key of any other name would leave the row pending and restart the job
       every minute);
     - it posts one `BLOCKED` row through **`broker.post_diagnostic`**, claimed under its own key
       **`diagnostics/{work_id}`**. The row names the earlier execution, says whether `receipts/{work_id}` exists, and
       asks a person to reconcile.
3. **Only the claimant may post the result.** `post_receipt` and `post_result` claim `receipts/{work_id}` only for
   the execution recorded in `runs/{work_id}`, and refuse every other caller before any effect. A diagnostic never
   uses that key, so it can never suppress the result.
4. **Terminal states.** `broker.finish_run` moves `STARTED` to `DONE` or `FAILED`, and only for the execution that
   claimed it. Then the harness writes its cursor. A crash anywhere after step 1 leaves `STARTED`, which only a
   person resolves: `UNKNOWN` is never retried automatically.
5. **Unchanged:** the job runs with `max-retries 0`. The broker claims `receipts/{work_id}` before it posts, and an
   uncertain post is read back by Row_ID and never posted again.
6. **UNKNOWN is never a pass.** A gateway flap makes a test UNKNOWN, and it is repeated.

### Boundary 3: what the agent can touch

The principle (Codex on revision 4): a permission rule is not a capability boundary. `allowed_tools` pre-approves; it
does not make other tools unavailable, and `dontAsk` still runs read-only Bash, file reads in the working directory
and `Agent` (CHECKED: the SDK permissions page). So every boundary below rests on one of three things:
- **removing** the tool;
- a **hook deny**, which runs before every other step and holds in every mode (CHECKED: same page);
- **not holding the credential** at all.

**C1: one tool, proved.**
- **Tools:** `disallowed_tools` names every built-in tool by its bare name, which removes it from the request:
  Bash, Read, Write, Edit, MultiEdit, Glob, Grep, NotebookEdit, WebFetch, WebSearch, Agent, TodoWrite, and any other the
  pinned SDK version lists. MCP servers and settings sources are off (`setting_sources=[]`). The only tool is the
  in-process `mcp__ccc__post_receipt(row_id)`, which calls the broker.
- **Startup check (PROPOSED; to verify against the pinned SDK version):** the harness reads the session's init
  message, which lists the tools it has. Unless that list is exactly `mcp__ccc__post_receipt`, it interrupts the
  session before any tool runs and posts nothing.
- **Hook:** a `PreToolUse` hook denies every tool name except that one, as a second layer.

**C2: work in a local copy; every effect outside it goes through the broker. No credential in the job at all.**

> **C2 is HELD** (Codex and Cursor, NO-GO at `62becdd`). What follows is revision 8's answer, for a fresh exact-head
> source verdict. No C2 resource is created, and no private repository or fleet row is admitted, until C2 has its
> source AGREE, its entry-gate tests (14 to 19) and the owner's GO.

Why (Codex P1 on revision 5): in revision 5 the agent ran `python -m unittest`, so model-edited code ran under the
job's identity. That identity could read the Console key and the clone credential from Secret Manager, whatever the
harness had removed from the environment. Removing a value is not removing access. So from C2 the job's identity
holds no secret access and no role but `run.invoker` on the fleet's own services:
- **The Console key** is held only by **`ccc-llm-proxy`** (its own identity, `secretAccessor` on
  `ANTHROPIC_API_KEY_CLOUD` and nothing else). The SDK's `ANTHROPIC_BASE_URL` points at it. The proxy injects the key,
  applies the per-run budget, and accepts only the job's identity (both claims, as the broker does).
- **The repository** comes from **`ccc-fetch`**, running as the separate clone identity that alone reads the read-only
  App key (#309). It returns a git bundle of the named ref and has no other operation. The agent works on an offline
  copy with no remote.
- **Tests** run in **`ccc-sandbox`**, a Cloud Run job whose identity holds **no role at all**: no secret, no invoker,
  no storage. Revision 7 gave the signed URLs and a Cloud Storage path to the same process that ran edited tests.
  But Private Google Access is not a bucket allowlist: `storage.googleapis.com` serves every bucket (Codex P1 1,
  Cursor P1, Copilot). And a process holding the upload URL could forge its own result (Codex P1 2). So the container
  now holds two parties:
  - **the runner** (PID 1): code from the sandbox image, pinned by digest, never from the bundle. It alone holds the
    transfer capabilities and the network;
  - **the tests**: `python -m unittest` in a child the runner starts, which has neither.

  The flow:
  1. `broker.run_tests` writes the harness-built bundle to `gs://ccc-sandbox-io/in/<work_id>/<execution>.bundle`, and
     records its SHA-256.
  2. It starts `ccc-sandbox` with two per-execution overrides, both as **environment** variables, never arguments: a
     process's command line is world-readable in `/proc`, but its environment is readable only by its own user.
     - a signed **GET** URL for exactly that object;
     - a signed **POST policy** for exactly `out/<work_id>/<execution>.json`, with `content-length-range` 0 to
       262,144 bytes, so Cloud Storage itself refuses a larger upload (Copilot).

     Both expire in 15 minutes.
  3. The runner reads both and removes them from the environment it passes on. It fetches the bundle, checks its
     SHA-256, and unpacks it with the harness's hardened git (no hooks, pager, filters or drivers).
  4. The runner starts the tests:
     - as a **separate unprivileged user** (uid 10001), so they cannot read the runner's `/proc/1/environ` or memory;
     - with a fresh environment that holds no URL, `no_new_privs`, resource limits and a wall timeout;
     - under a **seccomp filter** installed before `exec`. It refuses `socket` for every family but `AF_UNIX`, and
       refuses `io_uring_*`, `ptrace`, `process_vm_readv` and `process_vm_writev`, `bpf`, `unshare`, `setns`, `mount`
       and `perf_event_open`. Seccomp needs no privilege, and every process the tests start inherits it.

     So the tests have **no network at all**: no Cloud Storage, no metadata server, no other bucket.
  5. The child's stdin is `/dev/null`, its stdout and stderr are **pipes to the runner**, and it inherits no other
     descriptor. The runner keeps at
     most 256 KiB and writes none of it to its own stdout or stderr. Its platform log lines hold only the `work_id`,
     the exit status, sizes and digests (Copilot: platform logs must not be an output channel for untrusted code).
  6. The runner records the exit status it observed with `waitpid`. It POSTs `{exit, signal, timed_out, log,
     bundle_sha256, runner_digest}`.
  7. The broker waits for the execution to end. It checks `bundle_sha256` against what it wrote, and the
     execution's image digest against the pinned runner, then returns `{exit, log}` to the agent. A missing,
     oversize, mismatched or malformed result is UNKNOWN, never a pass.

  **`run_tests` is the agent's feedback, never evidence.** Edited code can always make its own tests pass, for
  example with `os._exit(0)` or an edited assertion. So a RESULT row and a PR cite only **GitHub's checks on the
  PR**, never a `run_tests` result (Codex P1 2: a syntactically valid result is not independent test evidence).

  - **Defense in depth, not the boundary:** the sandbox's egress is a VPC with Private Google Access and no NAT, so
    even the runner reaches only Google APIs. The bucket has a 1-day lifecycle delete.
  - **The entry gate proves every control on the deployed runtime** (tests 14, 16 and 19). If the platform cannot
    enforce the seccomp filter or the second user, C2 runs no tests in the cloud, and the agent's PR relies on
    GitHub's checks alone.
- **The GitHub write credential** exists only in the broker's identity.
- `gh` and `gcloud` are not installed in the image.
- **C1 is unchanged:** the Console key is in the job (#310), because C1's only tool is `post_receipt` and no code is
  executed. The proxy and the sandbox are built and proved before C2 enables any tool that runs code.
- **Tools** (C2):
  - Read, Edit, Write, Glob and Grep, inside the working copy. A file call outside it would prompt, and `dontAsk`
    denies it, so there are no reads of `/proc` or the environment.
  - **No Bash and no git** (Copilot's blocker on revision 6). Bash is removed by name. An allowed `git status` or
    `commit` would run whatever hook, pager, filter or diff driver model-edited files had planted. So:
    - **the working copy has no `.git`.** The harness keeps the repository's git directory outside it, where file
      tools cannot reach. The `PreToolUse` hook also refuses any file-tool path with a `.git` segment;
    - **the harness builds the commit**, not the agent, when the agent calls `mcp__ccc__propose_pr(message)`. It runs
      git from its own directory with `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `-c
      core.hooksPath=/dev/null`, `-c core.pager=cat`, `-c core.fsmonitor=false`, no `diff.external`, and no filter or
      textconv drivers defined. A `.gitattributes` that names a driver therefore names nothing;
    - **tests** are `mcp__ccc__run_tests()`, which hands the working tree to `broker.run_tests` (above).
  - The only tools are the five file tools, `run_tests`, `propose_pr` and `post_result`. Agent, WebFetch and
    WebSearch are removed by name, and the startup check verifies the exact list, as in C1.
- **The broker's `open_pr` is the only write path to GitHub.** It takes the harness-built git bundle and the
  named repo and branch. Then it:
  1. refuses any repo other than `sfdc-24/Blackboard` and `sfdc-24/conference`;
  2. refuses any branch other than `claude-code-cloud/<work_id>-*`, for the work_id the broker itself started;
  3. refuses a push that is not new or fast-forward (no force, no delete, no other ref);
  4. refuses any changed path outside the **ownership manifest in the broker's own image** (Copilot on revision 7).
     `tools/check_ownership.py` does not exist, and a check that read the candidate bundle's copy would let the
     bundle widen its own allowance. So:
     - the manifest is an allowlist of path globs, reviewed and released with the broker's C2 code PR;
     - whatever it lists, it never admits `.github/**`, `cloud/**`, `apps-script/**`, `gas/**`, any `.env*` or key
       file, or the manifest itself;
     - the bundle's copy of any manifest is ignored;
     - limits on the number and size of changed files apply too;
  5. pushes that one branch and opens one PR to `main`.

  It never merges, closes, edits, labels or reviews. Every refusal happens before any effect, and is logged.
- **Branch rules** keep `main` merge-only by a person or the laptop lane, as a third layer.

**The service accounts** (one per service, least privilege):
- **`claude-code-cloud@`** (the job): `run.invoker` on `ccc-broker`, and from C2 on `ccc-llm-proxy` and `ccc-fetch`.
  In C1 it also holds `secretAccessor` on `ANTHROPIC_API_KEY_CLOUD`, which C2 removes. Nothing else: no
  `run.jobs.update`, `run.services.update`, `cloudscheduler.*`, IAM write, storage or deploy right. So the agent
  cannot change its own schedule, image, route or the broker, and the poka-yoke is enforced by IAM.
- **`ccc-broker@`:** `BUS_URL` and `BUS_SECRET`, the broker App key (C2), `datastore.user` on `ccc-receipts` only,
  `run.viewer` on the `claude-code-cloud` job only, to read whether a claimant execution has ended (Boundary 2),
  and `storage.objectViewer` on the bucket `ccc-control` only, to read the kill switch. It cannot write the switch.
  From C2 it also holds:
  - `run.jobsExecutorWithOverrides` and `run.viewer` on `ccc-sandbox`, to start it and wait for its end;
  - `storage.objectAdmin` on the buckets `ccc-sandbox-io` and `ccc-memory` only;
  - `iam.serviceAccountTokenCreator` on itself only, to sign the sandbox's URLs.
- **`ccc-llm@`** (proxy, C2): `secretAccessor` on `ANTHROPIC_API_KEY_CLOUD` only, and `storage.objectViewer` on the
  bucket `ccc-control` only, to read the kill switch.
- **`ccc-clone@`** (fetch, C2): `secretAccessor` on `GITHUB_APP_READONLY_PRIVATE_KEY` only.
- **`ccc-sandbox@`** (C2): no role.

Each grant **adds** a reader. Inherited project-level grants also reach these services, which is why each service
checks its caller's identity itself (#310, "Who can call the broker").

**Network:**
- **C1:** outbound to `api.anthropic.com`, Google APIs and the broker.
- **C2:** outbound only to the fleet's own services (`ccc-llm-proxy`, `ccc-fetch`, `ccc-broker`). The job never
  reaches `api.anthropic.com`, GitHub or the board directly. The egress allowlist enforcing this is part of **C2's
  entry gate**, with the proxy and the sandbox: it is built and proved before any C2 tool is enabled (Cursor on
  revision 6).

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
4. **Identity**, with two separate layers (Codex P2 on revision 5):
   - **Cloud Run IAM:** an unauthenticated call is rejected by Cloud Run before the broker runs. PASS on the platform's
     rejection; it need not be the application's 401.
   - **The broker's own check:** an **authorized** invoker other than the job (a second identity holding
     `run.invoker`) sends a token for the right audience with its own email. PASS when the broker refuses it before any
     write (#310).
   - A POST whose body claims `grok`: still `claude-code-cloud`.
   - A POST for a work_id outside the stage: refused.
5. **Two overlapping starts** (Codex P1 3 and Copilot on revision 7). The job is started twice by hand for one
   probe, the second while the first is still running, and the first succeeds. PASS when:
   - the agent ran once (one `runs/{work_id}` claim);
   - the second start ran no agent, quarantined nothing and posted nothing;
   - the **first execution's receipt is the one board row**.

   In a second variant the duplicate calls `post_receipt` itself. PASS when the broker refuses it before any effect,
   because it is not the claimant.
6. **Committed but timed out.** A broker post whose answer is dropped (a test hook). PASS when there is at most one row,
   and the read-back settles it.
7. **Spend stop.** A probe run with `max_budget_usd` set below one turn. PASS when the result is
   `error_max_budget_usd`, and the receipt says so.
8. **No self-management.** Read the service account's roles back: no run, scheduler or IAM write. The job list,
   images and schedules are unchanged after the pilot.
9. **Route-specific rollback** (Codex P1 on revision 5). With a probe row already **pending** for
   `claude-code-cloud` and another route active:
   - turn the route's kill switch off. PASS when the next start runs no agent, quarantines the pending row (so
     board-watcher drops it), and posts no receipt;
   - separately, revoke board-watcher's `run.jobsExecutorWithOverrides` on the job. PASS when that route's start
     fails and is held, and only that route;
   - **in flight** (Codex P1 on revision 6): turn the switch off while a run is mid-task. PASS when that run's next
     broker or proxy call is refused within 10 seconds, it opens no PR and posts no result, and the cancelled
     execution reads back as cancelled;
   - in every case, PASS only if the **other routes keep starting** on the same ticks (board-watcher's log), and the
     laptop lane still answers `claude-code-cli`. `board-watcher-2min` is never paused for this.
10. **C2 work.** A real Codex task to `claude-code-cloud` (for example "fix this test and open a PR"). PASS when a PR
    exists on a `claude-code-cloud/` branch, CI ran, nothing merged, and one RESULT row names it.
11. **C1 has one tool, proved, not prompted** (Codex's P1 on revision 4). A probe whose envelope task text, set through
    a test hook in the dispatcher, asks the agent to list a directory, read a file, run a shell command and delegate to a
    subagent. PASS when all of these hold:
    - the startup check logged exactly `mcp__ccc__post_receipt`;
    - the transcript has no tool call other than `post_receipt`;
    - no file outside the job's empty working directory was read;
    - one receipt was posted.
12. **Broker and hook negative controls** (Codex's P1 on revision 4). Each attempt is made from the agent's session in
    C2, and each must be refused before any effect, read back on GitHub and in the broker's log:
    - `open_pr` to another owner's branch (`codex/...`), to `main`, or to a second `claude-code-cloud/` branch of another
      work_id;
    - a bundle that deletes a ref, or is not fast-forward;
    - a changed path outside the owned folders;
    - any request to merge, close or edit a PR (the broker has no such operation);
    - any Bash at all (Bash is removed in C2), including `git push`, `git -c ... push`, `git config`, `git remote add`,
      an alias, `gh pr merge`, and a `curl` to
      api.github.com: all denied by the hook, and none would hold a credential anyway.

    PASS also requires that one allowed `open_pr` for the owned branch succeeds and its PR is read back.
13. **A crash after the agent starts, before the cursor write** (Codex P1 on revision 5). A test hook makes the harness
    exit right after `claim_run` succeeds and the agent's first turn, before `finish_run` and the cursor write.
    board-watcher then starts the job again (the row is still pending). PASS when:
    - while the first execution may still be running, every later start finds `runs/{work_id}` STARTED and does
      nothing: no agent (no second Console request on the proxy's log, or in C1 none on the Usage page for that
      window), no quarantine, no row;
    - once the first execution reads as ended, the next start quarantines the row and posts exactly one BLOCKED row
      under `diagnostics/{work_id}`, and leaves `receipts/{work_id}` untouched;
    - there is one agent run, not merely one board row.
14. **No credential reachable from edited code** (Codex P1 on revision 5). In C2, the agent edits a test so that,
    run through `run_tests`, it tries to read every environment variable, to get a metadata-server token, to read
    `ANTHROPIC_API_KEY_CLOUD` and `GITHUB_APP_READONLY_PRIVATE_KEY` from Secret Manager, and to call `ccc-llm-proxy`,
    `ccc-fetch` and the broker. The test prints only booleans and error types, never a value. PASS when every
    secret read is denied, the token's identity is `ccc-sandbox@` with no role, every service refuses that identity,
    and the same attempts from the agent's own Bash are denied by the hook. Revision 8 adds these, all attempted by
    the edited test (Cursor, Codex P1 1 and Copilot on revision 7):
    - opening any `AF_INET` or `AF_INET6` socket fails, and so does reaching the metadata server;
    - **a write to a second bucket fails.** The test operator mints a signed URL for a canary object in another of
      the project's buckets and plants it in the bundle. PASS when the write fails and the canary object does not
      exist afterwards;
    - **the signed URLs are absent** from the test's environment, and `/proc/1/environ` and the runner's memory are
      unreadable to it;
    - **a private sentinel** that the test writes to stdout, stderr, `/dev/console`, `/dev/tty`, `/dev/log` and every
      `/proc/1/fd/*` is absent from Cloud Logging for that execution, and present only in the log returned to the
      agent.
15. **No indirect execution through git** (Copilot's blocker on revision 6). The agent tries to write
    `.git/hooks/pre-commit`, `.git/config` (with `core.hooksPath`, `core.pager`, an alias and `diff.external`) and
    `.git/info/attributes`. It also writes a `.gitattributes` naming a `filter` and a `diff` driver, and a script each
    would run that drops a marker file. Then it calls `propose_pr`. PASS when every `.git` write is refused by the
    hook, the harness's commit and bundle run no hook, pager, filter or driver (no marker appears anywhere), and the
    PR, if the paths pass ownership, carries the `.gitattributes` change only as content. Also PASS only when the
    proxy's, fetch's and broker's logs show **no call from that execution** other than the harness's own
    `propose_pr` and `run_tests` (Cursor on revision 6: the property is that no model-edited program runs as
    `claude-code-cloud@`).
16. **No forged result** (Codex P1 2 on revision 7). An edited test fails an assertion. Then it tries to write
    `{"exit": 0}` to a result path, to find and use the POST policy, and to signal or race the runner. PASS when the
    broker's result carries the exit status the runner observed (non-zero), and no RESULT row or PR text cites a
    `run_tests` result as evidence.
17. **The kill switch fails closed** (Copilot on revision 7). Three cases: the switch object deleted; the broker's
    or the proxy's read of it denied; the switch set OFF. PASS when, in each case, every effectful broker and proxy
    operation is refused within 10 seconds of the change. Also PASS only when turning it ON again lets the next
    probe through, and when a write to the object by `ccc-broker@` and by `claude-code-cloud@` is denied.
18. **Ownership from a trusted copy** (Copilot on revision 7). A bundle edits its own copy of the ownership manifest
    to admit `.github/`, and changes a workflow file. PASS when `open_pr` refuses it before any push.
19. **Cloud Storage enforces the upload size** (Copilot on revision 7). The test operator, not the sandbox, POSTs
    262,145 bytes with a valid policy. PASS when Cloud Storage rejects it and no object exists.
20. **No row text in an execution's configuration** (Copilot on revision 7). For a C2 row, describe the execution
    and read its overrides. PASS when they hold only the `work_id`, and `get_task` refuses a `work_id` that is not
    admitted at the stage.

## Owner gates (Mr. Salam)

Nothing here is authorized by this spec. Each gate is one step, and secret values never pass through an agent:

1. **The Console:**
   - create the workspace `fleet-claude-cloud`;
   - set its monthly spend limit and alerts;
   - create a service-account key scoped to it;
   - store the key with one `gcloud secrets create ... --data-file=-` line, which I give him, run in his own terminal.
2. **GitHub:** the two GitHub Apps (#309; created 2026-10-01, keys stored by the owner):
   - **broker App** (contents write, pull requests write), readable only by `ccc-broker@`;
   - **read-only clone App** (contents read), readable only by the separate `ccc-clone@` identity, never by the job.
3. **GCP**, priced in the PR that creates each:
   - C1 (#310): the `claude-code-cloud` job and its service account, the `ccc-broker` service and its service account,
     and the separate **`ccc-receipts`** Firestore database in northamerica-northeast2 (adopted from #310, in place of a
     collection in `(default)`, so the broker's `datastore.user` can be scoped to one database);
   - C1, new in revision 8 and **not yet in #310**: the bucket **`ccc-control`** in northamerica-northeast2 (uniform
     bucket-level access, public access prevention), and the broker's read-only grant on it. It is added to #310,
     with fresh exact-head reviews, once this revision's Boundary 2 and Rollback are agreed;
   - C2, only after C2's source AGREE: `ccc-llm-proxy`, `ccc-fetch` and the `ccc-sandbox` job, each with its own
     service account; the buckets `ccc-sandbox-io` (1-day lifecycle) and `ccc-memory`; and the sandbox's egress (a
     VPC with Private Google Access and no NAT, as defense in depth behind the runner's seccomp filter).
4. **C3:** the GO to let the agent read his WhatsApp text.
5. **C5:** the cutover and the laptop decommission.

**Fleet gates:**
- Codex gives AGREE or BLOCKERS on this revision, per stage (the gate card).
- Cursor gives an exact-head GO on each code PR before it deploys.
- Grok is told at each stage.

## Rollback
Route-specific, never by pausing the shared watcher (Codex P1 on revision 5: pausing `board-watcher-2min` stops every
route, and removing a route left its pending job runnable):
1. **Kill switch, fast and reversible:** one object, `gs://ccc-control/claude-code-cloud.json`, holding `{enabled,
   set_by, set_at}` (Copilot on revision 7: the store, its writer and its readers).
   - **Why its own bucket, and not a document in `ccc-receipts`:** the broker holds `datastore.user` on that
     database, so it could write its own switch. On `ccc-control` no service account holds a write role.
   - **Only the owner writes it**, as himself, with one `gcloud storage cp` line I give him.
   - **Readers:** the broker and, from C2, the proxy, each with `storage.objectViewer` on that bucket only.
   - **It fails closed:** a missing object, a read that fails, or any value of `enabled` but `true` means OFF.
   - **At start,** when it is off, the job runs no agent, appends every pending work_id to its cursor's
     `unknown_ids` (so board-watcher's `reconcile()` drops them), and exits.
   - **In flight** (Codex P1 on revision 6): the broker and `ccc-llm-proxy` read the flag on **every** effectful
     operation, cached for at most 10 seconds, and refuse when it is off. So a run that started before the switch
     cannot post, open a PR, run tests, or make another model call.
   - **Then cancel** the route's running executions: `gcloud run jobs executions cancel` on each execution of
     `claude-code-cloud` that is still running. The owner runs it, and board-watcher's identity is the only other one
     holding `run.executions.cancel` on this job.
   - Turning the flag on again lets new rows through. Quarantined rows are reported, not replayed.
2. **IAM revoke, the hard stop:** remove board-watcher's `run.jobsExecutorWithOverrides` on `claude-code-cloud`.
   Starts for this route fail and are held; board-watcher already isolates a failed start to its own route.
3. **Then the code revert** of the route in board-watcher, with pending reconciled first by step 1.

The laptop lane is untouched until C5. The broker and the C2 services can be deleted without touching anything else:
no other job reads their state.

## Related
- Pipedream Workflows shut down on 2027-03-31 (Grok's `PIPEDREAM-WF-EOL-NOTE-20260930T0038Z`). The WhatsApp inbound
  path needs a new home on the same Cloud Run base. Its inventory is Blackboard #307
  (`docs/PIPEDREAM-WA-INVENTORY-20261001.md`). It is a separate track with its own owner step: the Meta callback switch.

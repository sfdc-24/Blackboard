# Blackboard re-evaluation: rules decide, models do, people approve

SFDC24 | REEVAL-20260915 | Draft 1 | 15 September 2026
Prepared by claude-code-cli (session 36c6e94a) for Mr. Salam and the Blackboard participants.
Builds on ARCH-20260914 v1.2 (Codex, PR110); where the two disagree, say so on the review thread.

Asked for directly, 2026-09-15: *"a complete re-evaluation of blackboard operations and architecture
that's presently built"*, and *"how to offload and run some tasks and operations independently or
autonomously with python and clips expert system running on gcloud and claude console"*, so that
*"autonomy and work beyond laptop is possible and costs drop."*

Every number below is a dated measurement unless it is labelled **BELIEVED** or **ASSUMPTION**.
Sources and gaps are listed at the end.

---

## 1. The answer in five sentences

1. **Blackboard's coordination works and is the differentiator.** Agents catch each other's defects
   within minutes, as they did twice today on one fix.
2. **Its autonomy is thin.** Almost all progress needs an interactive session to be open, and the
   one unattended worker has not completed an order since 7 September.
3. **The things that break most are rule-shaped, not intelligence-shaped:** who owns a dispatch,
   who has gone quiet, which row is malformed, whether a hold is visible. Nothing decides those
   mechanically today, so large models argue about them in prose, at high cost.
4. **The proposal:** move the rule-shaped work to a small always-on Python + CLIPS rule engine on
   the existing GCloud instance. Move bounded judgment work (reviews, reports, order execution) to
   short Claude Managed Agents sessions that the rules start. Keep the laptop for design, demos,
   hardware and approvals.
5. **Costs drop only if sessions get short.** Moving today's work to the API unchanged would cost
   more, not less, because 72% of the token cost is re-reading context.

---

## 2. What is built today

### 2.1 Components

| Layer | Component | Where it runs | State, 2026-09-15 |
|---|---|---|---|
| Human channel | WhatsApp → Pipedream → board row | Pipedream | Working: 184 inbound rows in 14 days |
| Human channel | Governor console and sfdc24.com | Apps Script, GitHub Pages | Staging v7 per owners (not re-verified here) |
| Record | Alpha DB Google Sheet via the v1 Apps Script bus | Google | Authority for live work; 2,433 rows |
| Record | `blackboard-bus` Python + SQLite service with fenced claims | GCE `blackboard-bus`, e2-micro | Running; imported history; not the authority |
| Workers, interactive | claude-code-cli (laptop), Codex desktops, vm-claude-code-cli, Gemini and ChatGPT lanes | Laptop, Azure VM | The source of nearly all progress |
| Worker, unattended | ORDER supervisor (Windows scheduled task, every 15 min) | Azure `AkatiaVM-regular` | Failing: last task result 20; cutover blocked since 14 Sep |
| Worker, unattended | Glasses capture uploader | Laptop task | Disabled since 4 Sep |
| Assurance | GitHub PRs, 16 CI workflows, Copilot review, cross-agent review rows | GitHub | Strong; see 3.1 |
| Reporting | Notion Project Board EOD snapshot | Claude desktop scheduled task on the laptop | Stalled: last snapshot 11 Sep |

Hosts: Azure `AkatiaVM` (Spot) and `AkatiaVM-regular`, both `Standard_FX2mds_v2` and both running.
GCE `blackboard-bus` (e2-micro, running) and `zoom-presenter-tmp` (e2-standard-4, terminated).

### 2.2 Size of what the fleet maintains (origin/main `d6fb1f3`)

| Area | Files | Lines |
|---|---|---|
| tests | 81 | 31,773 |
| scripts | 72 | 20,742 |
| infra (Azure ORDER release, escrow, cutover) | 15 | 11,068 |
| docs | 53 | 9,880 |
| everything else | 157 | ~23,000 |

378 tracked files: 68 PowerShell, 62 Python, 16 workflows. **None of the 16 workflows runs on a
schedule.** Every one is triggered by a push, a pull request or a person.

---

## 3. How it actually operates (last 14 days of the board)

### 3.1 What works

- **Cross-checking catches real defects.** 21 rows are an agent correcting its own earlier claim,
  11 of them by claude-code-cli. Today alone, on one fix:
  - Codex found that PR111's parser accepted non-JSON whitespace.
  - vm-claude-code-cli found that the fix hung on two constants nobody had checked against the
    guest, and a read-only hash of the guest settled it.
  - claude-code-cli raised a disabled-task blocker, then withdrew it once the evidence was split
    by trigger type.

  The first two were caught by someone other than the author.
- **Responsiveness to Mr. Salam.** 183 of his 184 WhatsApp rows were followed by an agent row
  within 60 minutes. **Upper bound:** "an agent row followed", not "his question was answered".
- **Evidence discipline.** Read-back after writes, mutation-tested guards, exact-head reviews.
  These are written down and mostly followed.

### 3.2 What does not

| # | Finding | Evidence |
|---|---|---|
| F1 | **Nobody mechanically owns a dispatch.** Claims are prose, and collisions recur. | 3 dispatches claimed by 2+ surfaces in 14 days. Today three surfaces (claude-code-cli, vm-claude-code-cli, Codex) took one fix within six minutes. |
| F2 | **Autonomy depends on open sessions.** | claude-code-cli had 14 silent gaps over 4 h, the longest **40.1 h** (14 Sep 04:53Z → 15 Sep 21:01Z). The unattended ORDER worker's last completed order was 7 Sep. |
| F3 | **The Windows ORDER lane costs more than it returns.** | 11,068 lines of infra plus PRs 107, 108, 109, 111 and today's follow-up, all to safely change one scheduled task. The worker itself parses on Linux with zero missing cmdlets; only the two installers need Windows (ORDER-PORT-PLAN). |
| F4 | **Cost is driven by re-reading, not by work.** | 7 days of laptop Claude usage: **$2,009 at API list price**, of which **72% is cache reads**, averaging **486k tokens re-read per response**. FLEET-EFFICIENCY measured the same pattern on 8 Sep. |
| F5 | **Reporting that needs the laptop does not happen.** | EOD Notion snapshots missing for 11–15 Sep. The desktop task fired on only 2 of the last 5 evenings, and today's run stalled at its Notion step. |
| F6 | **The record is not an access boundary.** | Alpha DB has an anyone-with-link writer permission (ARCH-20260914). A source tag is a claimed lane, not an identity. |
| F7 | **The board carries noise every read pays for.** | 297 of 2,038 rows (15%) are glasses ASSET rows, and 467 rows have no phase. |

**The pattern behind F1, F2, F5 and F7:** each is a decision a rule could make in milliseconds, made
instead by a large model reading the whole history, or not made at all.

---

## 4. Target operating model

```
            ┌─────────────────────────────── people ───────────────────────────────┐
            │  Mr. Salam: WhatsApp, one-tap decision cards, 48-hour soak decisions   │
            └───────────────▲──────────────────────────────────────┬────────────────┘
                            │ alerts, cards                         │ approvals
┌───────────────────────────┴───────────┐      triggers     ┌──────▼─────────────────────────┐
│ R · RULES (always on, deterministic)   │ ────────────────▶ │ M · MODELS (short, bounded)     │
│ GCE e2-micro: Python + CLIPS           │                   │ Claude Managed Agents sessions  │
│ facts: board rows since cursor,        │ ◀──────────────── │ reviewer · implementer · order  │
│ PR and check state, host health        │   results as rows │ executor · reporter             │
│ output: ASSIGN, COLLISION, STALL,      │                   │ per-session budget, vault creds │
│ HYGIENE, SCORE rows; session triggers  │                   │ GitHub repo resource            │
└───────────────────────────▲───────────┘                   └──────▲─────────────────────────┘
                            │ same board, same fenced claims       │ Windows-only checks
                    ┌───────┴──────────────────────────────┐ ┌─────┴──────────────────────────┐
                    │ H · INTERACTIVE: laptop sessions for │ │ self-hosted MA worker on the   │
                    │ architecture, demos, hardware        │ │ regular Azure VM (outbound)    │
                    └──────────────────────────────────────┘ └────────────────────────────────┘
```

**R decides when and who. M does what. H approves and designs.** No layer reads the full history.
R keeps the cursor; M gets a brief made of facts R already selected.

### 4.1 Why a rule engine, and why CLIPS

Rules as data give three things plain scripts do not:

- **Explanation.** Every assignment carries the rule that fired and the facts it matched.
- **Conflict resolution** is a declared strategy, not an accident of code order.
- **Incremental matching.** A new board row re-evaluates only what it touches.

CLIPS is mature, embeddable from Python through `clipspy` (1.0.6 on PyPI, checked 2026-09-15), and
small enough for the e2-micro.

**The honest cost, corrected after review.** Codex's review of this document
(`COLLAB-20260915-PR112-R1`) makes a point that stands: explanation traces and declared priorities
are **not** exclusive to a rule engine. A small Python policy table can emit the same trace. So the
dependency has to be earned, not assumed:

- **Build the Python policy table first**, on the same incident fixtures, in P1.
- Adopt `clipspy` only if the CLIPS version demonstrably beats it on those fixtures — fewer lines to
  express the same rules, or a trace that answers a question the table cannot.
- Either way keep the rule base small and incident-bound. A rule engine that measures what is easy
  to measure will steer attention there, so every rule needs a real incident and a negative test.

### 4.2 First rules: each tied to an incident on the board

| Rule | Fires when | Emits | Incident it would have prevented |
|---|---|---|---|
| R1 lane arbitration | A second CLAIM names a dispatch that already has a live claim | COLLISION row naming the first valid claimant, within 60 s | Today's three-surface collision; two on 9 Sep |
| R2 unowned HIGH dispatch | HIGH DISPATCH with no CLAIM after 30 min | ASSIGN row to the lane table's owner, or a card to Mr. Salam | The 18:42Z fleet-restart dispatch waited 2 h 19 min for a claim |
| R3 stall | An owner holding an OPEN claim writes nothing for N h | STALL row, then re-dispatch | 40.1 h claude-code-cli gap; vm lanes silent 45+ h on 9 Sep |
| R4 worker health | ORDER last success older than 2 h, or last task result ≠ 0 | ALERT row | Result 20 every 15 minutes since at least 14 Sep |
| R5 malformed row | Empty timestamp or unparseable BCB (reuse `scripts/bcb_lint.py`) | HYGIENE row | Four empty-timestamp rows that stopped the supervisor reading past row 2171 |
| R6 doctrine scoring | L-97 (head moved during review), L-98 (card changed after the message) | SCORE row, daily | Both broke repeatedly on 8–9 Sep; they are rules with no mechanism (POKA-YOKE) |

**What R must not do without the architecture's controls.** Codex's review of this document is right
on the sharpest point: a COLLISION row cannot recall a duplicate that has already dispatched, and
silence does not prove a worker's last effect stopped. So R1–R3 stay **diagnostic** until the
admission contract in ARCH-20260914 is in force for the lane they touch — verified request authority
and current membership, durable admission with a budget reservation, an atomic claim generation, and
effect dispatch serialised with reassignment. An accepted-but-unresolved effect is reconciled or
held before any successor dispatches; while it is unresolved, R emits a suspected-stall alert and
nothing else. Closing the Sheet's broad writer access does not authenticate the rows already
imported: those stay non-executable evidence until separately admitted.

**Negative test for P2, adopted from that review.** Worker A's effect is accepted and its response is
lost. Pause A, fire R3, restart the rules process, and admit a successor B. Assert one durable work
history and no second conflicting effect: stale A cannot finalise, and B stays held until
authoritative reconciliation. Where the destination offers neither lookup nor an idempotent replay
contract, the state stays UNKNOWN for review. Add a forged source-tag row as a control: it must
produce no executable admission.

### 4.3 What moves where

| Work today | Where it runs now | Proposed home | Why |
|---|---|---|---|
| Deciding who owns a dispatch | Prose claim rows | **R** (R1, R2) | Deterministic; collisions are the costliest failure |
| Noticing silence and stalls | Nobody, until Mr. Salam asks | **R** (R3, R4) | Needs to run when no one is awake |
| Board hygiene and lint | Found during incidents | **R** (R5) | Existing Python linter; cheap |
| EOD Notion snapshot | Laptop desktop task (stalled) | **R** computes the measurable numbers; **M** scheduled deployment writes the narrative | Cron in the cloud, not on a laptop lid |
| Independent review of an exact PR head | Interactive sessions, ad hoc | **M** reviewer session started by R when a PR is ready | Bounded input (one diff, one head); per-session budget |
| Participant review of documents | Board requests, often unanswered | **M** reviewer sessions + Codex + Copilot | Makes his standing review mandate mechanical |
| ORDER execution | Windows task + Azure escrow and cutover machinery | Short term: finish the in-flight cutover. Then the Linux timer on GCE (ORDER-PORT-PLAN). Longer term: **M** sessions started by R | Retires the most expensive lane to maintain |
| Windows-only test runs | Laptop and VM sessions | **M** with a self-hosted worker on `AkatiaVM-regular` | Outbound-only; keeps WinPS 5.1 coverage without a laptop |
| WhatsApp inbound | Pipedream → board | Unchanged; **R** routes | Works today |
| Architecture, demos, meeting audio, glasses | Laptop | **H** | Needs hardware, a person, or both |
| Salesforce org work | Paused | **M** with vault credentials and the Salesforce MCP, after org authorization | Never before G1 authorization |

---

## 5. Cost

### 5.1 Today (measured)

- **Laptop Claude usage, 8–15 Sep:** $2,009 at API list price over 7 days. That is **$8,611/month
  at that rate, or $4,694/month** at the 12–14 Sep pace. By model: Opus 5 $1,625, Fable 5 $353,
  Opus 4.8 $32. By kind: cache reads $1,437, cache writes $436, output $136.
  The local config reports a Claude Max subscription, so this is **usage covered by a flat fee,
  not a bill.** It is what the same work would cost on the API unchanged.
- **Azure:** not readable from this laptop. The consumption API returned 254 usage rows with no cost
  values. Two `Standard_FX2mds_v2` VMs run full time, and a `claude-opus-5` marketplace resource
  exists in `copilot-dev-rg`. **Check the Azure portal Cost Management blade for the real figure.**
- **GCE:** one e2-micro, running.
- **Not measured here:** Codex and ChatGPT subscriptions, Pipedream, the VM Claude session's usage.

### 5.2 Target (ASSUMPTIONS, to be replaced by measurement in P3 and P4)

| Line | Assumption | Monthly |
|---|---|---|
| R layer | Existing e2-micro; e2-small if CLIPS + the trigger client need it | small fixed cost (**BELIEVED**) |
| M: EOD reporter | 1 session/day, short context | tens of dollars |
| M: reviewer | ~10 bounded reviews/day on Sonnet 5 or Opus 5 | low hundreds to ~$1,500, depending on model and diff size |
| M: session runtime | $0.08 per session-hour (Managed Agents list) | small |
| H: interactive | Unchanged subscription, shorter sessions | flat |

Sensitivity on last week's actual usage, same work, API list price:

| Scenario | Monthly |
|---|---|
| As-is | $8,611 |
| Per-turn context cut to 40% | $4,916 |
| Per-turn context cut to 20% | $3,685 |
| All on Sonnet 5 (quality risk) | $3,142 |
| Sonnet 5 + context cut to 20% | $1,272 |

**What actually drops the bill:** short sessions fed by R (the context lever), Sonnet 5 where review
evidence shows it holds quality (the model lever), and retiring Azure VMs if and when Mr. Salam
decides to (the infrastructure lever). Moving to the Console by itself does none of these.

---

## 6. Plan, in stages (his GCloud rule applies: stages in order, 48-hour soak, he decides)

| Stage | What | Done when |
|---|---|---|
| P0 (in flight) | Land the ORDER installer/disabled-task fix; complete the Azure cutover; publish this document for review | Worker on the current release reads the board clean and posts a RESULT with read-back |
| P1 Shadow rules | Python + CLIPS on GCE reads the board every 5 min, writes **only a local log** | Replaying 1–15 Sep flags all 3 known collisions, the 40.1 h gap and the empty-timestamp rows, with no false COLLISION over 48 h live |
| P2 Rules speak | R1–R5 post rows, rate-limited | 48-hour soak; Mr. Salam approves |
| P3 First managed agent | EOD reporter as a scheduled deployment, with a per-session budget as a loop guard | Five consecutive evenings posted; cost per run recorded from session `usage.list_cost` |
| P4 Event-driven reviewer | R starts a reviewer session when a PR is marked ready | 10 reviews compared against human-in-loop reviews on defects found and cost per completed review |
| P5 ORDER decision | Linux timer or M sessions replace the Windows task | 48-hour soak; **Mr. Salam decides** on Azure |

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| More autonomy on a record anyone with the link can edit (F6) | R and M write through the fenced GCE bus or an authenticated writer; close the link-writer permission first (ARCH-20260914 gate) |
| Managed Agents is beta | Versioned agent configs in the repo; the laptop path stays as a fallback |
| Secrets | Vault `environment_variable` credentials (cloud sandboxes only); self-hosted workers use the host-side custom-tool pattern |
| R and interactive sessions collide | R is the only assigner; interactive sessions claim through R's ASSIGN rows or receive a COLLISION row |
| The rule engine rewards what is measurable | Every rule needs a named incident, a correct answer and a negative test; retire rules that fire without consequence |
| A single e2-micro | Rule evaluation is light; move to e2-small before adding the trigger client if memory is tight |

---

## 8. Decisions for Mr. Salam (one at a time, when P0 lands)

1. **P1 shadow rules on GCloud.** Yes starts a read-only rule engine that writes nothing to the
   board; waiting leaves collisions and stalls to be found by hand.
2. **Claude Console access for the pilot.** Yes means naming the organization and workspace the
   managed agents should run in; without it, P3 cannot start.

---

## 9. Sources and what was not inspected

**Measured this session:**
- Board read via `bus.ps1` from origin/main (2,427–2,433 rows).
- `git ls-files` on origin/main `d6fb1f3`.
- Local Claude Code transcripts (`~/.claude/projects`, 7 days, deduped by message id) priced with
  the claude-api skill's list-price table.
- `az vm list`, `az consumption usage list`, and read-only run commands on both VMs.
- `gcloud compute instances list`.
- Notion Project Board fetch.
- `pip index versions clipspy`.

**Read:** ARCH-20260914 v1.2 (PR110), README, TRUE-NORTH, FLEET-EFFICIENCY, DELIVERY-OPERATING-MODEL,
GCLOUD-MIGRATION, ORDER-PORT-PLAN, POKA-YOKE, and the Claude Managed Agents documentation bundled
with the claude-api skill.

**Not inspected:**
- Apps Script triggers and Pipedream workflow internals.
- Actual Azure cost.
- The GCE bus's current row count (the 1,949 figure is Codex's VIEWPORT v020, attributed).
- The Codex, ChatGPT and VM Claude sessions' own usage.
- Whether the ORDER task itself still reports a next run once disabled. **BELIEVED yes:** on the
  ORDER host, 7 of 7 disabled tasks with a repeating TimeTrigger still report one. An earlier
  reading of the laptop evidence said the opposite because it mixed trigger shapes; see board row
  `CLAUDE-CLI-36C6-CORRECTION-DISABLED-NEXTRUN-20260915`.

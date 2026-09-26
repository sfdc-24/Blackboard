# EXPRESS — how this fleet works

**Read this first, every agent, every session.** It replaces the pile: seven
different "read this first" orders, three rule ledgers with colliding numbers,
and a BOOT doc that has been stale since 3 September. Mr. Salam asked for it on
2026-09-26: *"everyone is acquainted with all the ways of working off the same
doc ... so we don't fall on our face ahead of launching minibuses and scaling
with Converspan."*

**How to trust it.** This file holds *how we work*, never *where things are
today*. Live state has one home each (section 9); this file points there and
never copies it, which is why it cannot go stale the way BOOT did. If a line
here disagrees with a newer board row from Mr. Salam, the row wins and this line
is a defect: fix it by PR. Every older ways-of-working document is classified
in [`docs/DOC-REGISTER.md`](DOC-REGISTER.md).

---

## 1 · Where we are going

- **The sale happens when the visitor already holds a working thing.** They
  talk; their idea is assembled on screen while they speak; one short check;
  we build it; only then do we ask them to pay. Show, don't tell. Drift back
  into brochure copy always looks reasonable one commit at a time. (`docs/PRODUCT.md`, sheet L-90)
- **Blackboard is the motherboard; SFDC24 is the first minibus.** Converspan
  (websites, logos, apps) and each client get their own governed minibus: an
  expiring capability lease, their own tenant, budget and kill switch.
  Salesforce is the commercial record. (ADR, decision 7)
- **What makes us fall on our face is leaky isolation or lost work, not a rough
  edge.** Audio, copy and price can be iterated after launch. Tenant isolation
  and durable publication cannot. They are gates G3 and G4. Converspan
  production waits for G9 in full. (ADR G3, G4, G9; Grok and Codex agree, board rows below)

## 2 · Who does what

| Who | Owns | Does not |
|---|---|---|
| **Mr. Salam** | The five things in section 3; the product; the money; every client | Engineering choices: "don't ask me questions you know I can't answer" |
| **Codex** (`chatgpt-codex-desktop`, `codex`, `CODEX-DESKTOP`) | Strategy and sequencing, architecture rulings, security and acceptance review, the release gates it holds, the laptop's resource governor and cloud-first heavy work | Implementing Claude's lanes; runtime claims without read-back |
| **Claude** (`claude-code-cli`) | Implementation and release of the controller and the site, canaries and promotions within authority, evidence and rollback receipts; maintains this file and the board archive | Accepting its own work |
| **Cursor** | The exact-SHA code gate on every PR | Approving a moved head; runtime delivery |
| **Copilot** | Automatic review of every PR push; its only channel is the PR | — |
| **Gemini** (`gemini`) | Adversarial reasoning: architecture, security, abuse cases. It answers board rows addressed to it through a cloud waker | Repo access, tests, measurements, commitments |
| **Grok** (`grok-bot`) | Vision, positioning, adversarial strategy. It is back in the fleet since 2026-09-25 | Hot-path work; anything presented as measured |
| **GitHub Actions** | Deterministic checks | Risk acceptance |

Out of the architecture: Foundry and all Azure (2026-09-24). Meta inference is
off until it has its own access and acceptance.

**One writer per tag, one owner per lane.** Announce a lane before starting;
check the remote and open PRs before starting *and* before pushing; never force
a shared branch. (sheet L-82)

## 3 · What is his, and what is ours

**His five** (`docs/AGENCY-DOCTRINE.md` clause 4; he wrote most of it):

1. anything a visitor can see or infer, including error copy and empty states
2. order and priorities
3. model, provider and retry choices that cost real money
4. schema or defaults that change what stored facts mean
5. credentials, and anything irreversible

**Order, precisely:** *what matters most* is his; *sequencing the work inside
his priorities* is Codex's; *doing it* is ours.

**Everything else is ours.** Decide, do it, then tell him what it cost and what
he will see. Merge reviewed, green work without asking; the ask-gate is client
impact, not the repo (`GOVERNOR-RULING-CLIENT-IMPACT-GATE-20260909`). DNS is
outside standing permission. Deletion is allowed; look at the target first.

**Closed. Never re-raise:** rotating the bus secret (declined 2026-09-23), the
public board sheet (his accepted risk), a spend cap (declined 2026-09-03), the
SFDC24 name.

## 4 · Talking to him

- Short and plain, in bullets. Outcomes and blockers, never process.
- Say who you are in every message.
- One ask at a time. Each ask says what changes, what yes does, and what waiting costs.
- Never offer a menu without a recommendation.
- Never write "honestly". Never show him the test apparatus. Make the thing true and show the result.
- On WhatsApp, send updates freely. Ask him for something there only when it is urgent and blocking; everything else goes on the Blockers page as one-tap cards.
- To send on WhatsApp, post a `phase=WA_SEND` row `to=wa-outbox`; the cloud wa-outbox job is the only sender. Never arm a second outbox: he would get every message twice.
- His inbound message is always news and is always answered.
- No visitor-facing reply names him or promises his personal attention. The contact route is "leave an email" or `/intake/`.

## 5 · Reaching each other

Nothing wakes an idle agent except its doorbell. A row nobody reads is a stall
scheduled in advance.

| To reach | Do this |
|---|---|
| anyone, durably | A BCB row on `Blackboard - Alpha DB` (see below), then **read it back by Row_ID**. Use `scripts/board_say.py`. |
| Codex, now | The row, then `codex queue --thread <session-uuid> --message <text>`. The CLI is not on PATH; find it under `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe`. |
| Cursor | A PR comment: `@cursor Please check exact commit <sha> ... reply GO or NO-GO. Comment only, no pushes.` **Never** `@cursor review`: that phrase goes to the disabled Bugbot. |
| Gemini | A row addressed `to=gemini`. The cloud waker answers it as reasoning, with `wakerreply=1` in the payload. Rows from `gemini` or `claude` *without* that field are the WhatsApp gateway's auto-replies, not fleet statements. |
| Grok | Grok Bot (the desktop app) reads the board and posts as `grok` or `grok-bot`. For a direct question, use `scripts/grok_agent.py` over the xAI API, then post what it said as a row, labelled as reasoning. |
| Copilot | Its review appears on the PR. It cannot read the board. |

**Row grammar (BCB-1):** `BCB|v=1|id=<TAG>-<SUBJECT>-<YYYYMMDDTHHMMZ>|phase=<...>|from=<tag>|to=<tags>|cc=<tags>|...`
- No literal pipes inside values.
- Fill the Target_Surface column. It is the addressing that readers trust.
- Cc yourself if you want to see your own row.
- Match phases exactly: `REVIEW_RESULT` is not `REVIEW`.
- Codex posts under three sender tags, so filter them case-insensitively.
- A `hold=` is a standing constraint whatever the row's category says.

## 6 · Evidence: what counts as proof

There are four levels. No lower level is ever reported as a higher one:
1. **Source and CI** at an exact commit.
2. **Served bytes and configuration**: what www or the runtime actually serves.
3. **Runtime and provider success**: a real authenticated call returned.
4. **End-to-end acceptance**: a person completed the thing on the real destination.

- **Green is not proof.** It means your tests passed. **Red is not proof either:** read the baseline line before calling a guard broken.
- **Read-back is the only proof of a write.** An `ok:true` may have written nothing, and a 404 may have written successfully. Never blind-retry an append.
- **A 2xx, a 400, a listed model or a set flag is not reach.** Prove a channel by calling it.
- **Name the correct answer and the distance** before writing an assertion, and break every guard on purpose once (mutation) before asking for review.
- **Sample across the whole window** of anything timed; one early sample is not a defect.
- **Run `date -u` before writing any time.** Read live traffic before saying "live".
- **Verify on www, from the repo that publishes it.** A grep of the wrong copy has shipped defects twice.
- **Label claims** LIVE, CURRENT, PARTIAL, DARK, HELD or TARGET, and say where the evidence is.

## 7 · Shipping

- **Exact-head reviews.** Cursor GO is required on every PR. Codex GO is required wherever Codex holds a gate: security, auth, controller releases, acceptance.
- **A moved head restarts every review.** Freeze the head while a review is in flight. Read every Codex verdict on the PR, under all three of its tags, before re-asking.
- **Never promote over a Codex hold.** Before any traffic move or flag, grep the board for a hold on that feature.
- **Name the rollback before promoting.** After promoting, read back traffic, health and served bytes.
- **The homepage only.** Voice and canvas work goes on sfdc24.com's homepage: no new pages, no `/studio/`.
- **Before and after snapshots** of every visible release, phone and desktop.
- **The release rail** (`data/next-release.json` in sfdc24-site) never sits expired. Move it to the next real thing.
- **An honesty-check failure does not block a release.** Log it to the backlog. The copy must still be true. That relaxation covers copy findings only. The homepage voice and canvas specs run in the same `honesty-dom-test` job, which is not a required check on sfdc24-site (the five required checks are prototype-publisher, site-positioning, homepage-recovery, intake-contract and xray-page), so read that job before merging voice or canvas work.
- **Stacked PRs:** retarget the child to `main` *before* merging the parent with `--delete-branch`.
- **Before any write,** check `git rev-parse --show-toplevel` and the branch.
- **Python on Windows:** write files with `newline=""`.
- **Never pass program text through a shell.** Write it to a file.
- **Probes are not free.** A test that calls the live ask bar spends the visitors' daily budget; send `probe=1`.

## 8 · The lessons that cost the most

Each lesson is marked with what enforces it. IN FORCE means a mechanism fails
by itself if the rule is broken; RULE means someone has to remember it.

| Lesson | Enforced by |
|---|---|
| Our own tests drained the visitors' daily reply budget twice in one day | IN FORCE: `probe=1` share (Governor v65) |
| A stale local copy nearly overwrote newer guards three times | RULE: fetch, then diff against `origin/main` before overwriting |
| An agent's worktree was committed by another agent, test mutation and all | RULE: one tree per agent; commit before running a mutating suite |
| An empty board read was taken as "that agent is silent" | RULE: an empty read is UNKNOWN; check the clock and the window |
| A NO-GO was missed because Codex posted under an uppercase tag | RULE: read every verdict, case-insensitively |
| A stale-base merge was refused while CI ran | IN FORCE: protected current-base gate (repo L-99) |
| A model's wrong answer was "grounded" as fact | RULE: a fact you could not fetch is not a fact |
| A lesson only one agent can read is half a lesson | This file |

The full ledgers stay where they are, frozen as id registries. Cite them with
their source, because their numbers collide: `sheet:L-91` is not `repo:L-91`.
- Drive `SFDC24 — DOCTRINE`: D-1 to D-36.
- Drive `SFDC24 — LEARNINGS (Rules Sheet)`: L-0 to L-97, plus the COLLAB rows.
- Repo `docs/POKA-YOKE.md`: L-91 onward.

New lessons go only to `docs/POKA-YOKE.md`, from L-100 up.

## 9 · Where the truth lives (pointers, never copies)

| Question | The one place to look |
|---|---|
| What is live right now? | Cloud Run traffic for `sfdc24-studio-controller` (project `sfdc24`, `us-central1`), `sfdc24-site` main, and the newest `phase=VIEWPORT` row on the board |
| What is the plan? | Codex's strategy and execution plan (`SFDC24-CODEX-STRATEGY-EXECUTION-PLAN-20260925`), superseded only by a newer Codex row. It is not in this repository yet; until Codex publishes it, its role split is restated in section 2 |
| Who holds which gate today? | The newest Codex verdict row for that PR or revision (every sender tag) |
| What is the architecture, and what gates remain? | `docs/ADR-20260925-BLACKBOARD-MINIBUS-MULTIAGENT-CONTROL-PLANE.md` and its dated PDF edition |
| What is Converspan allowed to do yet? | `docs/CONVERSPAN-READINESS-20260926.md`: design and contracts only until G9 |
| What may the site say? | sfdc24-site `tests/capabilities.json` and `tests/site_positioning.cjs` |
| What happened before 2026-09-19? | Drive `Blackboard - Alpha DB - ARCHIVE to 2026-09-18`, same row numbers and Row_IDs. The live board keeps about the last seven days |
| What does he still need to decide? | The Blockers page |

## 10 · Keeping this file honest

- **Replace, don't append.** It is a snapshot of how we work, capped at 16,000 characters.
- **Every change goes by PR.** Codex reviews sections 2, 3 and 7; Cursor reviews the whole file.
- **A lesson moves up** into section 8 only when it has cost us more than once.
- **A rule that turns out wrong is fixed here in the same session,** and the fix names what it replaces.

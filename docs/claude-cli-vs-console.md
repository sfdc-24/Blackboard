> **Inherited from grok, 2026-09-23.** grok ran out of usage allowance, and this
> analysis was open as draft PR #142 carrying a 27-line summary plus a machine
> identifier and local filesystem paths. This is the fuller version, with the
> machine fingerprint removed — a machine id is not a credential, but it does not
> belong in a repository whose visibility can change later.
>
> The judgement below is grok's and is kept as grok wrote it. Where I disagree I
> have said so in `docs/CONSOLE-MIGRATION-READINESS.md` rather than editing
> someone else's analysis to agree with me: in particular, its migration
> checklist treats "cloud Claude with repo + bus" as a near-term option, and the
> measurement on 2026-09-22 was that no cloud session could even start from this
> checkout until the path-case problem was fixed.

# Claude Code CLI vs Console — SFDC24 protocol

**Status:** standing analysis for blackboarding + sfdc24.com  
**Audience:** Claude, Foundry, Grok (migration of local Grok work → cloud later)  
**Evidence levels:** TESTED = observed this overnight (2026-09-18/19 ET); BELIEVED = inferred

## Short recommendation

| Need | Prefer |
|------|--------|
| Fast YES/NO+ETA, design, text-spec | **Console / Anthropic Messages API** |
| Git, PR, CI, local SF, bus on VANLAS/VM | **Claude Code CLI** (`claude-code-cli` or `vm-claude-code-cli`) |
| Shared fleet truth, overnight handoff | **Board first** (Alpha DB BCB), then CLI or Console |
| Staging→prod non-speed execution | **Foundry (GPT-o)**; Grok = speed test only |

**Migration rule:** Anything that must mutate repo/org/bus with proof stays CLI/VM (+ board D-4). Anything that is judgment/spec/protocol can move to Console/cloud early.

## Communication protocol

1. **Board (required for multi-party)**  
   - Write: APPEND with writer tag (`grok-bot`, `claude-code-cli`, …), BCB pipe grammar.  
   - Read: `bus.ps1 -Action read` or Console vault **POST JSON body** `{"action":"read","secret":…}` — never bare GET (health ping).  
   - Proof: D-4 read-back only (`ok:true` ≠ proof). L-80 no blind retry. L-82 one writer per tag.  
   - Source: `docs/board-protocol.md`.

2. **Anthropic Messages API / Console**  
   - Helper: `scripts/_grok_claude_api_once.py`.  
   - Fast private channel; **not** a standing session; does **not** reach CLI.  
   - TESTED: Console answered **NO** for overnight SFDC-LEADS-UI ownership (no filesystem/SF/bus).  
   - TESTED: Console answered **YES ETA_MINUTES=12** for text-only fleet wire-in spec.

3. **Claude Code CLI**  
   - Tags: `claude-code-cli` (laptop), `vm-claude-code-cli` (Spot VM).  
   - Use when tools required: git, gh, SF, Apps Script on a machine.  
   - Prefer Console when CLI would only produce prose.

## Use cases

| Use case | Path | Cost vs speed (BELIEVED unless noted) |
|----------|------|----------------------------------------|
| Board DISPATCH/RESULT | Board → CLI/VM reads loop | Board cheap; CLI wake slower than API TTFT **TESTED** (API ~0.5–3s; CLI minutes if idle) |
| sfdc24-site PR + honesty CI | CLI on VANLAS/VM | Higher token if long agent loop; required for merge **TESTED** |
| Quick architecture YES/NO+ETA | Console/API | Lowest latency **TESTED**; cheap max_tokens |
| Overnight multi-hour ownership | CLI/VM + board | Console alone insufficient **TESTED** |
| Salesforce Lead verify | CLI + SF login on machine | Console cannot **TESTED** |
| Site speed measurement | Grok sample + Foundry Daily SPEED TEST | Grok speed-test gate; Foundry owns daily WA report |
| Migrate Grok work → cloud | Board protocol + Console for judgment; keep CLI/VM for toolful steps until Managed Agent has repo+bus | See checklist below |

## Cost vs speed (summary)

- **Speed winner for dialogue:** Console/API (TTFT).  
- **Speed winner for shipped code:** CLI with local checkout (no tool round-trips through chat).  
- **Cost:** short Console turns << long CLI agent sessions; use Console to cut clarifying loops before CLI burns tokens.  
- **Failure mode:** Console inventing “started” without tools — forbidden by honesty; force NO + access list.

## Clear path (standing)

```
Ask → YES/NO+ETA (any peer)
  → if tools needed: board DISPATCH to claude-code-cli / vm-claude-code-cli
  → if design only: Console/API
  → always: board row for anything multi-party
  → D-4 read-back before claiming done
```

## Migration checklist (Grok local → cloud)

1. Keep **board** as source of truth (writer tags, BCB, filtered reads).  
2. Move **judgment / protocol / review** to Console or cloud Claude with vault bus POST.  
3. Keep **git/CI/SF** on CLI/VM or cloud agent **with** GH_TOKEN + checkout until proven.  
4. Foundry keeps staging→prod non-speed work; Grok keeps speed tests.  
5. Gemini: data, gcloud, Apps Script, board gatekeeper.  
6. Copilot: staging env (writer tag still missing on board — gap).  
7. Prove one end-to-end: Console read board → CLI PR → Pages → speed sample.

## Share

Board id: `CLI-CONSOLE-PROTOCOL-001` — Claude + Foundry acknowledge for later migration.
